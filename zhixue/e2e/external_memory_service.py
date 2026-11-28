"""Serve the production frontend and API with an isolated Mem0 contract fixture."""
from pathlib import Path
import sys
from threading import Event, Thread
from uuid import uuid4

from backend.app import create_app
from backend.db import open_db
from backend.external_memory import step
from backend.launch import bind_browser_listener, serve
from backend.process_io import watch_parent_disconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_external_memory import Credentials, Mem0


def main(folder: Path):
    token = uuid4().hex
    vault, remote = Credentials(), Mem0()
    remote.fail_delete = True

    def transport(method, path, key, body=None):
        response = remote.request(method, path, key, body)
        if method == "DELETE":
            remote.fail_delete = False
        return response

    engine = open_db(folder)
    stopped = Event()
    watch_parent_disconnect(stopped.set)
    worker = None

    def run_worker():
        while not stopped.wait(0.10):
            step(engine, vault, transport)

    try:
        with bind_browser_listener(0) as (listener, port):

            def ready():
                nonlocal worker
                worker = Thread(target=run_worker, name="external-memory-fixture")
                worker.start()
                print(f"http://127.0.0.1:{port}/#/?session={token}", flush=True)

            app = create_app(folder, token, external_memory_credentials=vault, external_memory_transport=transport)
            serve(app, listener, stopped, True, ready)
    finally:
        stopped.set()
        if worker is not None:
            worker.join()
        engine.dispose()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
