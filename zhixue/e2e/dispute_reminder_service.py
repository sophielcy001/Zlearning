"""Launch an isolated app with persisted review tasks and actionable course disputes."""
from pathlib import Path
import sys
from threading import Event
from uuid import uuid4

from backend.app import create_app
from backend.launch import bind_browser_listener, serve
from backend.process_io import watch_parent_disconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_course_verification import authorize, model_traffic, prepare, publish
from test_demo import first_lesson
from test_model_study import ok, run, session


def seed(folder: Path, *, phone_first: bool):
    calls = []
    with session(folder) as http:
        project, _, _, evidence = prepare(http, folder, calls)
        authorize(http, project, evidence)
        assert run(folder, model_traffic(calls)) and publish(folder)
        lesson = first_lesson(http, folder)
        ok(http.put(lesson + "/learned", json={"learned": True}))
        ok(http.post("/api/demo/next-day"))
        if phone_first:
            workspace = ok(http.get("/api/mobile/catalog"))["workspace"]
            [reminder] = ok(http.post("/api/mobile/reminders/check", json={
                "workspace": workspace, "trigger": "foreground",
            }))["reminders"]
            assert reminder["reviews"] == 2


def main(folder: Path, *, phone_first: bool, seed_only: bool):
    seed(folder, phone_first=phone_first)
    if seed_only:
        return
    token = uuid4().hex
    stopped = Event()
    watch_parent_disconnect(stopped.set)
    with bind_browser_listener(0) as (listener, port):
        app = create_app(folder, token)
        serve(app, listener, stopped, True,
              lambda: print(f"http://127.0.0.1:{port}/#/?session={token}", flush=True))


if __name__ == "__main__":
    main(Path(sys.argv[1]), phone_first="--phone-first" in sys.argv, seed_only="--seed-only" in sys.argv)
