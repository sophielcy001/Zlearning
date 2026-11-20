from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.schedule import next_review_date
from backend.worker import prepare_one


BASE_URL = "http://127.0.0.1"
HEADERS = {"Authorization": "Bearer demo-test-session"}
GOAL = {
    "id": "00000000-0000-4000-8000-000000000001",
    "topic": "SQLite 入门",
    "outcome": "能从本地学习记录中筛选数据",
    "foundation": "懂一点表格",
}


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, "demo-test-session", today=lambda: date(2027, 1, 31))
    with TestClient(app, base_url=BASE_URL, headers=HEADERS) as http:
        yield http, tmp_path


def project(http):
    response = http.post("/api/projects", json=GOAL)
    assert response.status_code == 201
    return response.json()


def first_lesson(http, folder):
    saved = project(http)
    while prepare_one(folder):
        pass
    return f"/api/lessons/{saved['lessons'][0]['id']}"


def test_goal_retry_reuses_project_and_rejects_different_content(client):
    http, _ = client
    first = project(http)
    assert project(http)["id"] == first["id"]
    assert len(first["lessons"]) == 3
    assert http.post("/api/projects", json={**GOAL, "topic": "别的主题"}).status_code == 409
    assert len(http.get("/api/state").json()["recentProjects"]) == 1


def test_worker_resumes_after_an_existing_checkpoint_and_writes_real_files(client):
    http, folder = client
    saved = project(http)
    assert saved["task"]["status"] == "queued"
    assert prepare_one(folder)
    original = folder / "library" / "courses" / f"{saved['lessons'][0]['id']}.md"
    modified = original.stat().st_mtime_ns
    reopened = create_app(folder, "demo-test-session")
    with TestClient(reopened, base_url=BASE_URL, headers=HEADERS) as restarted:
        midway = restarted.get(f"/api/projects/{saved['id']}").json()
        assert midway["task"]["completed"] == 1
        assert prepare_one(folder)
        assert prepare_one(folder)
        assert not prepare_one(folder)
        ready = restarted.get(f"/api/projects/{saved['id']}").json()
        assert ready["task"]["status"] == "ready"
        assert all(lesson["ready"] for lesson in ready["lessons"])
        assert original.stat().st_mtime_ns == modified
        assert "SQLite" in original.read_text(encoding="utf-8")


def test_failed_course_write_keeps_the_completed_checkpoint_for_retry(client):
    http, folder = client
    saved = project(http)
    assert prepare_one(folder)
    second = folder / "library" / "courses" / f"{saved['lessons'][1]['id']}.md"
    second.mkdir()
    assert prepare_one(folder)
    failed = http.get(f"/api/projects/{saved['id']}").json()["task"]
    assert (failed["status"], failed["completed"]) == ("failed", 1)
    second.rmdir()
    assert http.post(f"/api/projects/{saved['id']}/retry").status_code == 200
    assert prepare_one(folder) and prepare_one(folder)
    assert http.get(f"/api/projects/{saved['id']}").json()["task"]["status"] == "ready"


def test_note_is_a_local_file_and_remains_available_after_reopen(client):
    http, folder = client
    url = first_lesson(http, folder)
    lesson_id = url.rsplit("/", 1)[-1]
    assert http.put(f"{url}/note", json={"content": "# 我的笔记\n\n表由行和列组成。"}).status_code == 200
    note = folder / "library" / "notes" / f"{lesson_id}.md"
    assert "表由行和列组成" in note.read_text(encoding="utf-8")
    with TestClient(create_app(folder, "demo-test-session"), base_url=BASE_URL, headers=HEADERS) as restarted:
        assert restarted.get(url).json()["note"].startswith("# 我的笔记")


def test_import_has_a_safe_local_identity_and_preview(client):
    http, folder = client
    content = "\ufeff# 示例\r\n文本资料可以在知识库阅读。"
    rejected = http.post("/api/files", json={"name": "../test.md", "content": content})
    assert rejected.status_code == 422
    imported = http.post("/api/files", json={"name": "示例.md", "content": content})
    assert imported.status_code == 201
    file_id = imported.json()["id"]
    assert http.get(f"/api/files/{file_id}").json()["content"] == content
    assert (folder / "library" / "imports" / f"{file_id}.md").read_bytes() == content.encode("utf-8")


def test_learning_schedules_next_day_and_reviews_advance_independently(client):
    http, folder = client
    lesson = first_lesson(http, folder)
    http.put(f"{lesson}/learned", json={"learned": True}).raise_for_status()
    reviews = http.get("/api/reviews").json()["reviews"]
    assert len(reviews) == 2
    assert {item["dueOn"] for item in reviews} == {"2027-02-01"}
    assert {item["status"] for item in reviews} == {"upcoming"}
    first, second = reviews
    review = f"{lesson}/reviews/{first['key']}"
    attempt = {"round": 1, "answers": [0, 1], "action": "submit"}
    assert http.post(f"{review}/submit", json=attempt).status_code == 409
    http.post("/api/demo/next-day").raise_for_status()
    assert "correct" not in http.get(review).json()["questions"][0]
    result = http.post(f"{review}/submit", json=attempt).json()
    assert result["outcome"] == "passed"
    assert result["dueOn"] == "2027-02-03"
    assert http.post(f"{review}/submit", json=attempt).json() == result
    assert http.post(f"{review}/submit", json={**attempt, "answers": [1, 1]}).status_code == 409
    queue = http.get("/api/reviews").json()["reviews"]
    assert next(item for item in queue if item["key"] == second["key"])["dueOn"] == "2027-02-01"


def test_hint_and_draft_survive_refresh_and_schedule_a_check_without_advancing(client):
    http, folder = client
    lesson = first_lesson(http, folder)
    http.put(f"{lesson}/learned", json={"learned": True}).raise_for_status()
    http.post("/api/demo/next-day").raise_for_status()
    review = f"{lesson}/reviews/rows-columns"
    http.put(f"{review}/draft", json={"round": 1, "answers": [0, None]}).raise_for_status()
    http.post(f"{review}/hint", json={"round": 1}).raise_for_status()
    detail = http.get(review).json()
    assert detail["answers"] == [0, None]
    assert detail["hint"]
    result = http.post(f"{review}/submit", json={"round": 1, "answers": [0, 1], "action": "submit"}).json()
    assert (result["outcome"], result["stage"], result["dueOn"]) == ("hinted", 0, "2027-02-02")
    assert http.get(review).json()["questions"] != detail["questions"]


def test_review_failure_resets_only_that_objective_and_keeps_lesson_learned(tmp_path):
    day = date(2027, 2, 1)
    with TestClient(create_app(tmp_path, "demo-test-session", today=lambda: day), base_url=BASE_URL, headers=HEADERS) as http:
        lesson = first_lesson(http, tmp_path)
        http.put(f"{lesson}/learned", json={"learned": True}).raise_for_status()
        day += timedelta(days=1)
        review = f"{lesson}/reviews/rows-columns"
        http.post(f"{review}/submit", json={"round": 1, "answers": [0, 1], "action": "submit"}).raise_for_status()
        day += timedelta(days=2)
        result = http.post(f"{review}/submit", json={"round": 2, "answers": [], "action": "unknown"}).json()
        assert (result["outcome"], result["stage"], result["dueOn"]) == ("failed", 0, "2027-02-05")
        saved = http.get(f"/api/projects/{GOAL['id']}").json()
        assert saved["learned"] == 1
        reviews = http.get("/api/reviews").json()["reviews"]
        other = next(item for item in reviews if item["key"] == "unique-key")
        assert (other["stage"], other["dueOn"]) == (0, "2027-02-02")


def test_passing_the_one_year_round_completes_the_review(tmp_path):
    day = date(2027, 1, 31)
    with TestClient(create_app(tmp_path, "demo-test-session", today=lambda: day), base_url=BASE_URL, headers=HEADERS) as http:
        lesson = first_lesson(http, tmp_path)
        http.put(f"{lesson}/learned", json={"learned": True}).raise_for_status()
        day += timedelta(days=1)
        review = f"{lesson}/reviews/rows-columns"
        for round_number in range(1, 11):
            answers = [0, 1] if round_number % 2 else [1, 2]
            result = http.post(f"{review}/submit", json={"round": round_number, "answers": answers, "action": "submit"}).json()
            assert result["outcome"] == "passed"
            if round_number < 10:
                assert result["stage"] == round_number
                day = date.fromisoformat(result["dueOn"])
        assert result["dueOn"] is None
        assert http.get(review).json()["status"] == "complete"


def test_unmarking_or_pausing_preserves_existing_due_date(client):
    http, folder = client
    lesson = first_lesson(http, folder)
    http.put(f"{lesson}/learned", json={"learned": True}).raise_for_status()
    http.post("/api/demo/next-day").raise_for_status()
    http.put(f"{lesson}/learned", json={"learned": False}).raise_for_status()
    assert {item["status"] for item in http.get("/api/reviews").json()["reviews"]} == {"paused"}
    http.put(f"{lesson}/learned", json={"learned": True}).raise_for_status()
    url = f"/api/projects/{GOAL['id']}/pause"
    http.put(url, json={"paused": True}).raise_for_status()
    assert {item["status"] for item in http.get("/api/reviews").json()["reviews"]} == {"paused"}
    http.put(url, json={"paused": False}).raise_for_status()
    assert {item["status"] for item in http.get("/api/reviews").json()["reviews"]} == {"due"}
    assert {item["dueOn"] for item in http.get("/api/reviews").json()["reviews"]} == {"2027-02-01"}


def test_custom_project_has_readable_template_but_no_sourced_review(client):
    http, folder = client
    response = http.post("/api/projects", json={**GOAL, "topic": "法语口语"})
    response.raise_for_status()
    while prepare_one(folder):
        pass
    url = f"/api/lessons/{response.json()['lessons'][0]['id']}"
    assert http.get(url).json()["source"] is None
    http.put(f"{url}/learned", json={"learned": True}).raise_for_status()
    assert http.get("/api/reviews").json()["reviews"] == []


def test_api_requires_a_local_session(client):
    http, _ = client
    assert http.get("/api/state", headers={"Authorization": ""}).status_code == 401
    denied = http.post("/api/projects", json=GOAL, headers={"Origin": "https://elsewhere.example"})
    assert denied.status_code == 403
    assert http.get("/api/state").json()["recentProjects"] == []


def test_timer_keeps_the_duration_across_pages_and_an_explicit_pause(tmp_path):
    now = datetime(2027, 1, 31, 8, 30)
    app = create_app(tmp_path, "demo-test-session", now=lambda: now)
    with TestClient(app, base_url=BASE_URL, headers=HEADERS) as http:
        lesson = first_lesson(http, tmp_path).rsplit("/", 1)[-1]
        started = http.post("/api/timer", json={"action": "start", "lessonId": lesson}).json()["active"]
        assert started["status"] == "running"
        timer_id = started["id"]
        now += timedelta(seconds=9)
        http.get("/api/state").raise_for_status()
        stopped = http.post("/api/timer", json={"action": "pause", "timerId": timer_id}).json()["active"]
        assert (stopped["status"], stopped["elapsed"]) == ("paused", 9)
        now += timedelta(seconds=20)
        assert http.get("/api/timer").json()["active"]["elapsed"] == 9
        http.post("/api/timer", json={"action": "resume", "timerId": timer_id}).raise_for_status()
        now += timedelta(seconds=5)
        completed = http.post("/api/timer", json={"action": "finish", "timerId": timer_id}).json()
        assert completed["active"] is None
        assert completed["lastSaved"]["elapsed"] == 14


def test_stashed_timer_excludes_time_offline_and_can_be_confirmed_after_restart(tmp_path):
    now = datetime(2027, 1, 31, 8, 30)
    def create():
        return create_app(tmp_path, "demo-test-session", now=lambda: now)

    with TestClient(create(), base_url=BASE_URL, headers=HEADERS) as http:
        lesson = first_lesson(http, tmp_path).rsplit("/", 1)[-1]
        timer_id = http.post("/api/timer", json={"action": "start", "lessonId": lesson}).json()["active"]["id"]
        now += timedelta(seconds=18)
        assert http.post("/api/timer", json={"action": "stash", "timerId": timer_id}).json()["active"] is None

    now += timedelta(days=1)
    with TestClient(create(), base_url=BASE_URL, headers=HEADERS) as http:
        assert http.get("/api/timer").json()["stashed"][0]["elapsed"] == 18
        saved = http.post("/api/timer", json={"action": "confirm", "timerId": timer_id}).json()
        assert saved["stashed"] == []
        assert saved["lastSaved"]["elapsed"] == 18


@pytest.mark.parametrize(("start", "stage", "expected"), [
    (date(2027, 1, 31), 6, date(2027, 2, 28)),
    (date(2028, 1, 31), 6, date(2028, 2, 29)),
    (date(2027, 1, 31), 7, date(2027, 3, 31)),
    (date(2028, 2, 29), 9, date(2029, 2, 28)),
])
def test_review_intervals_use_calendar_months(start, stage, expected):
    assert next_review_date(start, stage) == expected


def test_local_voice_page_limits_media_permissions_to_microphone(client):
    http, _ = client
    policy = http.get("/healthz").headers["Permissions-Policy"]
    assert "microphone=(self)" in policy
    assert "camera=()" in policy
