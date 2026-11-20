from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2

from test_backups import accepted, import_reference, open_client


@contextmanager
def launch(folder):
    with subprocess.Popen(
        [sys.executable, "-m", "backend.launch", "--stdio", "--port", "0", "--data", str(folder)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as child:
        try:
            url = urlsplit(child.stdout.readline().strip())
            session, = parse_qs(urlsplit(url.fragment).query)["session"]
            with httpx2.Client(base_url=f"{url.scheme}://{url.netloc}", headers={"Authorization": f"Bearer {session}"},
                               trust_env=False, timeout=8) as http:
                yield http, child.pid
        finally:
            child.stdin.close()
            assert child.wait(timeout=12) == 0


def test_first_start_offers_a_storage_choice_before_creating_learning_data(tmp_path):
    with launch(tmp_path) as (http, _):
        response = http.get("/api/setup")
        assert response.status_code == 200
        state = response.json()
        assert state["stage"] == "storage"
        assert Path(state["library"]) == tmp_path / "library"
        assert not (tmp_path / "zhixue.sqlite3").exists()
        assert not (tmp_path / "library").exists()
        assert accepted(http.get("/api/timer"))["active"] is None
        assert http.post("/api/timer", json={"action": "start"}).status_code == 409


def until(operation):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        try:
            if value := operation():
                return value
        except httpx2.RequestError:
            pass
        sleep(.07)
    raise AssertionError("Local workspace did not become ready")


def reached(http, stage):
    response = http.get("/api/setup", timeout=.8)
    return response.is_success and response.json()["stage"] == stage


def test_selected_location_becomes_the_live_service_and_resumes_after_restart(tmp_path):
    control, chosen = tmp_path / "control", tmp_path / "我的学习资料"
    with launch(control) as (http, process):
        assert http.get("/api/setup", headers={"Authorization": ""}).status_code == 401
        assert http.get("/api/setup", headers={"Origin": "https://example.invalid"}).status_code == 403
        assert http.get("/api/projects").status_code == 409
        assert http.post("/api/setup/complete").status_code == 409
        selected = http.put("/api/setup/storage", json={"directory": str(chosen)})
        assert selected.status_code == 202, selected.text
        assert selected.json()["stage"] == "opening"
        until(lambda: reached(http, "models"))
        assert Path(accepted(http.get("/api/setup"))["library"]) == chosen / "library"
        assert (chosen / "zhixue.sqlite3").is_file()
        assert not (control / "zhixue.sqlite3").exists()
        assert accepted(http.get("/api/models"))["defaultProvider"] == "minimax"
        assert not accepted(http.get("/api/state"))["recentProjects"]
        assert http.put("/api/setup/storage", json={"directory": None}).status_code == 409

    with launch(control) as (http, next_process):
        assert process != next_process
        assert reached(http, "models")
        assert accepted(http.post("/api/setup/complete"))["stage"] == "complete"
        reference = import_reference(http, body="实际保存在选择的知识库")
        assert (chosen / reference["path"]).read_text(encoding="utf-8") == "实际保存在选择的知识库"
        identity = str(uuid4())
        accepted(http.post("/api/projects", json={"id": identity, "topic": "SQLite", "outcome": "学习选定位置的课程"}))
        until(lambda: accepted(http.get(f"/api/projects/{identity}"))["task"]["status"] == "ready")
        assert list((chosen / "library" / "courses").glob("*.md"))
        assert not (control / "library").exists()

    with launch(control) as (http, _):
        assert reached(http, "complete")
        assert accepted(http.get(f"/api/files/{reference['id']}"))["content"] == "实际保存在选择的知识库"
        assert accepted(http.get(f"/api/projects/{identity}"))["task"]["status"] == "ready"


def test_setup_rejects_occupied_and_relative_paths_then_accepts_default(tmp_path):
    control, occupied = tmp_path / "control", tmp_path / "occupied"
    occupied.mkdir()
    original = occupied / "原始资料.txt"
    original.write_text("保留本来的目录内容", encoding="utf-8")
    with launch(control) as (http, _):
        for path in ("relative", str(occupied)):
            response = http.put("/api/setup/storage", json={"directory": path})
            assert response.status_code == 409, response.text
            assert reached(http, "storage")
        assert list(occupied.iterdir()) == [original]
        assert original.read_text(encoding="utf-8") == "保留本来的目录内容"
        assert http.put("/api/setup/storage", json={"directory": None}).status_code == 202
        until(lambda: reached(http, "models"))
        assert Path(accepted(http.get("/api/setup"))["library"]) == control / "library"


def test_a_legacy_learning_directory_opens_directly_and_keeps_previous_files(tmp_path):
    with open_client(tmp_path) as http:
        file = import_reference(http, body="升级之前的参考资料")
    with launch(tmp_path) as (http, _):
        assert reached(http, "complete")
        assert accepted(http.get(f"/api/files/{file['id']}"))["content"] == "升级之前的参考资料"
        assert http.put("/api/setup/storage", json={"directory": str(tmp_path / "unneeded")}).status_code == 409
        assert not (tmp_path / "unneeded").exists()


def test_unavailable_selected_storage_is_reported_without_creating_a_replacement_workspace(tmp_path):
    control, chosen = tmp_path / "control", tmp_path / "portable"
    with launch(control) as (http, _):
        assert http.put("/api/setup/storage", json={"directory": str(chosen)}).status_code == 202
        until(lambda: reached(http, "models"))
    detached = tmp_path / "detached"
    chosen.rename(detached)
    failed = subprocess.run(
        [sys.executable, "-m", "backend.launch", "--stdio", "--port", "0", "--data", str(control)],
        input="", capture_output=True, text=True, timeout=8,
    )
    assert failed.returncode == 1 and "知识库位置暂不可用" in failed.stderr
    assert not chosen.exists()
    assert not (control / "zhixue.sqlite3").exists()
    assert (detached / "zhixue.sqlite3").is_file()
