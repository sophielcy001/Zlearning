import argparse
import asyncio
import secrets
import socket
import subprocess
import sys
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from time import sleep

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .app import create_app
from .backup_format import BackupError
from .backup_restore import exclusive_lease, recover_full_restore, restore_root
from .db import ROOT
from .process_io import watch_parent_disconnect
from .restore_runtime import RestoreRuntime, attach_restore
from .workspace_setup import SERVICE_LOCK, WorkspaceError, WorkspaceSetup, attach_workspace, setup_app


# The OS can allocate these Fetch-restricted ports in the ephemeral range.
# https://fetch.spec.whatwg.org/#port-blocking
BROWSER_RESTRICTED_PORTS = frozenset({
    1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566,
    6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080,
})


@contextmanager
def bind_browser_listener(port: int):
    for _ in range(16):
        with socket.socket() as listener:
            if sys.platform == "win32":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", port))
            assigned_port = listener.getsockname()[1]
            if port or (assigned_port >= 1024 and assigned_port not in BROWSER_RESTRICTED_PORTS):
                yield listener, assigned_port
                return
    raise OSError("无法分配浏览器可访问的本地端口。")


def worker_command(data: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", str(data)]
    return [sys.executable, "-m", "backend.worker", str(data)]


class WorkerProcess:
    def __init__(self, data: Path, server: uvicorn.Server):
        self.data = data
        self.server = server
        self.lock = Lock()
        self.child: subprocess.Popen | None = None

    def start(self) -> None:
        with self.lock:
            if self.server.should_exit or self.child is not None:
                return
            child = subprocess.Popen(
                worker_command(self.data), cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self.child = child
        Thread(target=self.watch, args=(child,), daemon=True).start()

    def watch(self, child) -> None:
        child.wait()
        with self.lock:
            if child is self.child and not self.server.should_exit:
                print("课程准备进程已退出，请重新启动智学。", file=sys.stderr)
                self.server.should_exit = True

    def stop(self, timeout: float = 4) -> None:
        with self.lock:
            child, self.child = self.child, None
            if child is None:
                return
            child.stdin.close()
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


class LocalServer(uvicorn.Server):
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self.lifespan.state["zhixue_shutting_down"] = True
        await super().shutdown(sockets)


class LocalHTTPProtocol(H11Protocol):
    def connection_made(self, transport: asyncio.Transport) -> None:
        super().connection_made(transport)
        # The loop can deliver an accepted socket after Uvicorn snapshots its connections.
        if self.app_state.get("zhixue_shutting_down"):
            transport.close()


def serve(app, listener: socket.socket, stopped: Event, stdio: bool, ready,
          data: Path | None = None, recovered: dict | None = None, *, worker_factory=WorkerProcess) -> bool:
    server = LocalServer(uvicorn.Config(
        app, log_level="warning" if stdio else "info", access_log=False, timeout_graceful_shutdown=3,
        loop="asyncio", http=LocalHTTPProtocol, ws="none", lifespan="on",
    ))
    worker = worker_factory(data, server) if data is not None else None
    runtime = None
    if worker is None:
        def open_workspace():
            server.should_exit = True
        app.state.open_workspace = open_workspace
    else:
        runtime = RestoreRuntime(lambda: worker.stop(timeout=25), worker.start, recovered)
        attach_restore(app, runtime)

    started = False

    def watch_process():
        nonlocal started
        while not server.started and not server.should_exit:
            if stopped.wait(0.02):
                server.should_exit = True
        if server.should_exit:
            return
        if worker is not None:
            try:
                worker.start()
            except OSError as error:
                print(f"无法启动课程准备进程：{error}", file=sys.stderr)
                server.should_exit = True
                return
        started = True
        if ready is not None:
            ready()
        while not server.should_exit:
            if stopped.wait(0.05):
                server.should_exit = True

    watcher = Thread(target=watch_process, daemon=True)
    watcher.start()
    try:
        # The setup and study servers share a listener. Windows IOCP cannot
        # register that same socket with a second event loop after setup.
        with listener.dup() as channel, asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(server.serve(sockets=[channel]))
    except KeyboardInterrupt:
        stopped.set()
    finally:
        server.should_exit = True
        watcher.join(timeout=1)
        if runtime is not None:
            runtime.close()
        if worker is not None:
            worker.stop()
    return started or (server.started and stopped.is_set())


def run(data: Path, port: int, stdio: bool, open_browser: bool, *, desktop_offline: bool = False) -> int:
    if not (ROOT / "dist" / "index.html").is_file():
        print("请先在 zhixue 目录运行 npm run build。", file=sys.stderr)
        return 1
    control = data.resolve()
    control.mkdir(parents=True, exist_ok=True)
    stopped = Event()

    if stdio:
        watch_parent_disconnect(stopped.set)
    with exclusive_lease(control / ".zhixue-launch.sqlite3"), bind_browser_listener(port) as (listener, port):
        setup = WorkspaceSetup(control)
        token = secrets.token_urlsafe(32)

        def announce():
            url = f"http://127.0.0.1:{port}/#/?session={token}"
            if stdio:
                print(url, flush=True)
            else:
                print(f"智学本地原型 · http://127.0.0.1:{port}/", flush=True)
                if open_browser:
                    webbrowser.open(url)
                else:
                    print(url, flush=True)

        first = setup.selection is None
        if first:
            started = serve(setup_app(setup, token), listener, stopped, stdio, announce)
            if stopped.is_set() or setup.selection is None:
                return 0 if started else 1

        selected = setup.selection
        assert selected is not None
        workspace = selected.folder
        root = restore_root(workspace)
        with exclusive_lease(root / SERVICE_LOCK):
            with exclusive_lease(workspace / ".worker-lease.sqlite3", timeout=8):
                recovered = recover_full_restore(workspace)
            if not selected.complete:
                (workspace / "library").mkdir(mode=0o700, exist_ok=True)
            app = create_app(
                workspace, token, desktop_offline=control / "offline-study.sqlite3" if desktop_offline else None,
            )
            attach_workspace(app, setup, workspace)
            started = serve(app, listener, stopped, stdio, None if first else announce, workspace, recovered)
            return 0 if started else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="启动智学本地原型")
    parser.add_argument("--port", type=int, default=3220)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--stdio", action="store_true", help="向父进程报告地址并随 stdin 关闭退出")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务并在终端显示本次浏览器会话地址")
    parser.add_argument("--desktop-offline", action="store_true", help="为桌面内置离线页在本机保留记录")
    args = parser.parse_args()
    if args.data is None and getattr(sys, "frozen", False):
        parser.error("打包后的服务需要通过 --data 指定可写的数据目录。")
    data = args.data if args.data is not None else ROOT / "data" / "browser"
    try:
        return run(data, args.port, args.stdio, not args.no_browser, desktop_offline=args.desktop_offline)
    except (OSError, BackupError, WorkspaceError) as error:
        print(f"无法启动本地服务：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
