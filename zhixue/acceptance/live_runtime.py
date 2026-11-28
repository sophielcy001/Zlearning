"""Run the production API and Worker with a memory-only credential store."""
import argparse
from pathlib import Path
from secrets import token_urlsafe
import socket
import sys
from threading import Event, Lock, Thread

from sqlalchemy.exc import SQLAlchemyError

from backend.app import create_app
from backend.db import open_db
from backend.launch import serve
from backend.model_costs import MoneyLimit
from backend.model_protocols import TransportNotSent, post_json
from backend.process_io import watch_parent_disconnect
from backend.worker import run_queue

from .usage import write_usage_checkpoint


WORKER_START_FAILURE = 20
WORKER_RUN_FAILURE = 21
WORKER_EXIT_REASONS = {
    WORKER_START_FAILURE: "隔离模型 Worker 初始化失败。请检查本机临时目录和数据库。",
    WORKER_RUN_FAILURE: "隔离模型 Worker 运行失败。本轮联调已停止。",
}


class MemoryCredentials:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def put(self, reference: str, value: str) -> None:
        self.values[reference] = value

    def discard(self, reference: str) -> None:
        self.values.pop(reference, None)


def capped_transport(limit: int, before_send=None, *, stopped: Event | None = None):
    lock = Lock()

    def check_stopped():
        if stopped is not None and stopped.is_set():
            raise TransportNotSent()

    def send(address, headers, payload):
        nonlocal limit
        with lock:
            check_stopped()
            if limit <= 0:
                return 429, b""
            limit -= 1
        if before_send is not None:
            before_send()
        check_stopped()
        return post_json(address, headers, payload)

    return send


class SampleWorker:
    def __init__(self, folder, server, vault, transport, cost_limit=None):
        self.folder = folder
        self.server = server
        self.vault = vault
        self.transport = transport
        self.cost_limit = cost_limit
        self.stopped = Event()
        self.thread = None
        self.failure = None

    def work(self, engine):
        try:
            try:
                run_queue(engine, self.folder, self.stopped, vault=self.vault, transport=self.transport)
            finally:
                engine.dispose()
        except Exception:
            self.failure = WORKER_RUN_FAILURE
            self.server.should_exit = True
            print(WORKER_EXIT_REASONS[self.failure], file=sys.stderr)

    def start(self):
        self.stopped.clear()
        engine = None
        try:
            # Initialize before serve() announces the isolated HTTP address.
            engine = open_db(self.folder, workspace_cost_limit=self.cost_limit)
            thread = Thread(target=self.work, args=(engine,), name="acceptance-model-worker", daemon=True)
            thread.start()
        except Exception as error:
            self.failure = WORKER_START_FAILURE
            if engine is not None:
                engine.dispose()
            raise OSError(WORKER_EXIT_REASONS[self.failure]) from error
        self.thread = thread

    def stop(self, timeout=4):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                raise RuntimeError("Sample worker did not stop")
            self.thread = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--usage-checkpoint", type=Path)
    parser.add_argument("--cost-limit", type=MoneyLimit.model_validate_json)
    args = parser.parse_args()
    folder = args.data.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stopped = Event()
    token = token_urlsafe(32)
    vault = MemoryCredentials()

    database = folder / "zhixue.sqlite3"
    checkpoint_lock = Lock()

    def checkpoint(*, final=False):
        if args.usage_checkpoint is not None and database.is_file():
            with checkpoint_lock:
                try:
                    write_usage_checkpoint(args.usage_checkpoint, database, final=final)
                except (OSError, SQLAlchemyError):
                    print("无法保存隔离模型用量快照。", file=sys.stderr)

    transport = capped_transport(args.limit, checkpoint, stopped=stopped)

    worker = None

    def worker_factory(data, server):
        nonlocal worker
        worker = SampleWorker(data, server, vault, transport, args.cost_limit)
        return worker

    def stop_on_disconnect():
        stopped.set()
        if worker is not None:
            worker.stopped.set()

    watch_parent_disconnect(stop_on_disconnect)
    service_stopped = False
    try:
        with socket.socket() as listener:
            if sys.platform == "win32":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", args.port))
            port = listener.getsockname()[1]
            app = create_app(folder, token, credential_store=vault, model_transport=transport,
                             backup_credentials=vault, workspace_cost_limit=args.cost_limit)
            started = serve(
                app, listener, stopped, True,
                lambda: print(f"http://127.0.0.1:{port}/#/?session={token}", flush=True),
                folder, worker_factory=worker_factory,
            )
            service_stopped = True
            if worker is not None and worker.failure is not None:
                return worker.failure
            return 0 if started else 1
    finally:
        vault.values.clear()
        checkpoint(final=service_stopped)


if __name__ == "__main__":
    raise SystemExit(main())
