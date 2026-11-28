"""Launch the app with a deterministic official-MCP protocol fixture."""
import json
from pathlib import Path
import sys
from threading import Event, Thread
from uuid import uuid4

from backend.app import create_app
from backend.chat_model_worker import dispatch_reply, reserve_reply
from backend.db import open_db
from backend.launch import bind_browser_listener, serve
from backend.model_study import utc_now
from backend.process_io import watch_parent_disconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_repository_mcp import Remote, Vault


def main(folder: Path):
    token = uuid4().hex
    vault, remote = Vault(), Remote()
    engine = open_db(folder)
    stopped = Event()
    watch_parent_disconnect(stopped.set)
    worker = None

    def reply(_address, _headers, request):
        prompt = request["messages"][-1]["content"]
        text = "模型收到了已确认的仓库片段。" if remote.text in prompt else "这是独立的聊天回复。"
        report = {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 31, "completion_tokens": 12}}
        return 200, json.dumps(report, ensure_ascii=False).encode()

    def run_worker():
        while not stopped.wait(0.10):
            claim = reserve_reply(engine, utc_now())
            if claim:
                dispatch_reply(engine, claim, transport=reply, workspace=folder)

    try:
        with bind_browser_listener(0) as (listener, port):
            def ready():
                nonlocal worker
                worker = Thread(target=run_worker, name="repository-mcp-fixture")
                worker.start()
                print(f"http://127.0.0.1:{port}/#/?session={token}", flush=True)

            app = create_app(folder, token, repository_mcp_credentials=vault, repository_mcp_transport=remote.request)
            serve(app, listener, stopped, True, ready)
    finally:
        stopped.set()
        if worker is not None:
            worker.join()
        engine.dispose()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
