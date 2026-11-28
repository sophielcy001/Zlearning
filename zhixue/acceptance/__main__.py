import argparse
from pathlib import Path
from secrets import token_urlsafe
import sys
from threading import Thread
from time import sleep
import webbrowser

import uvicorn

from backend.backup_restore import exclusive_lease
from backend.backup_format import BackupError
from backend.db import ROOT
from backend.launch import bind_browser_listener
from backend.process_io import watch_parent_disconnect
from .server import RunStore, create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="启动智学独立验收网页")
    parser.add_argument("--reports", type=Path, default=Path.home() / ".zhixue" / "acceptance",
                        help="验收历史目录")
    parser.add_argument("--port", type=int, default=0, help="本机端口，默认自动分配")
    parser.add_argument("--no-browser", action="store_true", help="在终端显示本次网页地址")
    parser.add_argument("--stdio", action="store_true", help="在终端显示会话地址并随 stdin 关闭退出")
    args = parser.parse_args()
    if not (ROOT / "dist" / "index.html").is_file():
        parser.error("请先在 zhixue 目录运行 npm run build。")
    reports = args.reports.resolve()
    try:
        reports.mkdir(mode=0o700, parents=True, exist_ok=True)
        with exclusive_lease(reports / ".acceptance-lease.sqlite3"), bind_browser_listener(args.port) as (channel, port):
            token = token_urlsafe(32)
            address = f"http://127.0.0.1:{port}/#session={token}"
            store = RunStore(reports)
            app = create_app(store, token)
            server = uvicorn.Server(uvicorn.Config(
                app, access_log=False, log_level="warning", loop="asyncio", http="h11", ws="none",
                timeout_graceful_shutdown=5,
            ))

            def announce():
                while not server.started and not server.should_exit:
                    sleep(.02)
                if server.started:
                    opened = not (args.no_browser or args.stdio) and webbrowser.open(address)
                    print(f"智学独立验收 · http://127.0.0.1:{port}/" if opened else address, flush=True)

            def stop_on_disconnect():
                store.request_shutdown()
                server.should_exit = True

            if args.stdio:
                watch_parent_disconnect(stop_on_disconnect)
            Thread(target=announce, daemon=True).start()
            server.run(sockets=[channel])
            if not server.started:
                return 1
        return 0
    except (OSError, BackupError) as error:
        print(f"无法启动独立验收网页：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
