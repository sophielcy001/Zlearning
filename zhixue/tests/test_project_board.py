from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
import sqlite3
from time import perf_counter
from uuid import UUID

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.db import open_db


DAY = date(2027, 2, 3)


@contextmanager
def browser(folder, day=DAY, now=datetime(2027, 2, 3, 4)):
    app = create_app(folder, "board-test", today=lambda: day, now=lambda: now)
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer board-test"}) as http:
        yield http


def submit(http, url, **payload):
    response = http.post(f"/api/{url}", json=payload)
    assert response.is_success, response.text
    return response.json()


def query(http, pages=None, **view):
    return submit(http, "project-board/query", view=view, pages=pages or {})


def project_id(number):
    return str(UUID(int=number))


def seed(folder, count=25):
    with sqlite3.connect(folder / "zhixue.sqlite3") as db:
        for i in range(1, count + 1):
            paused = 13 <= i <= 17
            done = 18 <= i <= 25
            db.execute(
                """INSERT INTO projects (id, topic, outcome, foundation, sample, paused, created_on,
                    created_at, updated_at, last_learned_at, priority)
                   VALUES (?, ?, ?, '', 1, ?, '2027-01-01', '2027-01-01 00:00:00.000000', ?, ?, ?)""",
                (project_id(i), f"验证项目 {i:03}", "Python 100%" if i == 25 else "练习",
                 paused, "2027-02-02 12:00:00.000000" if i == 25 else "2027-01-01 00:00:00.000000",
                 "2027-02-02 12:00:00.000000" if done else None, "high" if i == 25 else None),
            )
            db.execute("INSERT INTO tasks VALUES (?, ?, NULL)", (project_id(i), "queued" if count > 25 else "ready"))
            if i != 12:
                db.execute(
                    "INSERT INTO lessons (id, project_id, position, version, ready, learned_on) VALUES (?, ?, 0, 1, 1, ?)",
                    (project_id(i + 1000), project_id(i), "2027-02-02" if done else None),
                )
        if count >= 25:
            db.execute("INSERT INTO project_tags VALUES (?, 'ai', 'AI')", (project_id(25),))
            db.execute("INSERT INTO project_tags VALUES (?, '编程', '编程')", (project_id(1),))
            for i, due in [(13, "2027-02-03"), (18, "2027-02-03"), (19, None)]:
                db.execute("INSERT INTO reviews VALUES (?, 'rows-columns', 0, ?, 1, '[]', 0)", (project_id(i + 1000), due))


def numbers(result, key):
    return [UUID(item["id"]).int for item in result["pages"][key]["items"]]


def test_paginates_each_status_and_clamps_after_changes(tmp_path):
    with browser(tmp_path) as http:
        seed(tmp_path)
        first = query(http)
        assert first["total"] == 25
        assert first["counts"] == {"active": 12, "paused": 5, "done": 8}
        assert [first["pages"][key]["lastPage"] for key in first["counts"]] == [4, 2, 3]
        assert sum(len(page["items"]) for page in first["pages"].values()) == 9
        assert numbers(first, "active") == [1, 2, 3]
        assert numbers(query(http, {"active": 2}), "active") == [4, 5, 6]
        assert numbers(query(http, {"active": 2}), "paused") == numbers(first, "paused")
        assert numbers(query(http, {"done": 3}), "done") == [24, 25]
        for i in (24, 25):
            assert http.put(f"/api/projects/{project_id(i)}/pause", json={"paused": True}).is_success
        clamped = query(http, {"done": 3})
        assert clamped["pages"]["done"]["page"] == 2
        assert numbers(clamped, "done") == [21, 22, 23]
        none = query(http, search="没有匹配")
        assert none["hasProjects"] and none["total"] == 0
        assert all(page["lastPage"] == 0 and page["items"] == [] for page in none["pages"].values())


def test_filters_the_full_set_with_and_or_exclusion_and_literal_search(tmp_path):
    with browser(tmp_path) as http:
        seed(tmp_path)
        status = {"field": "status", "op": "in", "values": ["active", "done"]}
        tag = {"field": "tag", "op": "in", "values": ["ai", "编程"]}
        priority = {"field": "priority", "op": "is", "value": "high"}
        recent = {"field": "updated", "op": "recent", "days": 2}
        exclusions = {"field": "status", "op": "not_in", "values": ["done", "paused"]}
        assert numbers(query(http, layout="table", filters=[status, tag, priority, recent]), "all") == [25]
        assert numbers(query(http, layout="cards", combinator="or", filters=[tag, priority]), "all") == [25, 1]
        assert numbers(query(http, layout="table", filters=[tag, exclusions]), "all") == [1]
        assert numbers(query(http, search="PYTHON 100%", layout="table"), "all") == [25]
        assert query(http, search="Python 100_")["total"] == 0
        assert query(http, filters=[{"field": "tag", "op": "not_in", "values": ["AI"]}])["total"] == 24
        assert query(http, filters=[{"field": "tag", "op": "none"}])["total"] == 23
        assert query(http, filters=[{"field": "priority", "op": "is", "value": None}])["total"] == 24


def test_progress_and_review_states_are_independent(tmp_path):
    with browser(tmp_path) as http:
        seed(tmp_path)
        absent = {"field": "progress", "op": "none"}
        missing = query(http, filters=[absent])["pages"]["active"]["items"][0]
        assert missing["id"] == project_id(12) and missing["progress"] is None
        assert http.get(f"/api/projects/{project_id(12)}").json()["status"] == "active"
        assert query(http, filters=[{"field": "progress", "op": "lt", "value": 10}])["total"] == 16
        assert query(http, filters=[{"field": "progress", "op": "between", "low": 90, "high": 100}])["total"] == 8
        assert query(http, filters=[{"field": "learned", "op": "never"}])["total"] == 17
        assert query(http, filters=[{"field": "learned", "op": "between", "fromDate": "2027-02-02", "toDate": "2027-02-02"}])["total"] == 8
        due = query(http, filters=[{"field": "review", "op": "due"}])["pages"]["done"]["items"]
        assert [(item["id"], item["status"], item["reviewStatus"], item["due"]) for item in due] == [(project_id(18), "done", "due", 1)]
        completed = query(http, search="019")["pages"]["done"]["items"][0]
        assert completed["reviewStatus"] == "complete" and completed["due"] == 0
        paused = query(http, search="013")["pages"]["paused"]["items"][0]
        assert paused["reviewStatus"] == "paused" and paused["due"] == 0
        assert query(http, filters=[{"field": "review", "op": "not_due"}])["total"] == 24
        assert (http.get("/api/reviews").json()["reviews"][0]["projectId"]) in {project_id(13), project_id(18), project_id(19)}


@pytest.mark.parametrize("filter_value", [
    {"field": "status", "op": "in", "values": []},
    {"field": "tag", "op": "in", "values": [" "]},
    {"field": "progress", "op": "gte", "value": 101},
    {"field": "progress", "op": "between", "low": 70, "high": 30},
    {"field": "updated", "op": "between", "fromDate": "2027-02-03", "toDate": "2027-02-01"},
    {"field": "learned", "op": "recent", "days": 0},
    {"field": "updated", "op": "never"},
])
def test_invalid_filter_is_rejected(tmp_path, filter_value):
    with browser(tmp_path) as http:
        assert http.post("/api/project-board/query", json={"view": {"filters": [filter_value]}}).status_code == 422


def test_saved_view_persists_definition_and_recalculates_dates(tmp_path):
    settings = {
        "layout": "cards", "search": "025", "filters": [{"field": "updated", "op": "recent", "days": 2}],
        "sort": {"key": "priority", "direction": "desc"}, "boardSize": 5, "pageSize": 24,
        "columns": [{"key": "done", "visible": True}, {"key": "active", "visible": False},
                    {"key": "paused", "visible": True}],
    }
    with browser(tmp_path) as http:
        seed(tmp_path)
        saved = submit(http, "project-views", name="近期", definition=settings)
        assert query(http, **saved["definition"])["total"] == 1
        assert http.post("/api/project-views", json={"name": "近期", "definition": settings}).status_code == 409
    with browser(tmp_path, date(2027, 2, 5)) as http:
        assert http.get("/api/project-views").json() == [saved]
        assert "pages" not in saved["definition"]
        assert query(http, **saved["definition"])["total"] == 0
        path = f"/api/project-views/{saved['id']}"
        response = http.put(path, json={"name": "固定日期", "definition": {**settings, "filters": []}})
        assert response.status_code == 200
        assert query(http, **response.json()["definition"])["total"] == 1
        assert http.delete(path).status_code == 204
        assert http.get("/api/project-views").json() == []
        assert query(http)["total"] == 25


def test_metadata_and_reads_do_not_modify_learning_records(tmp_path):
    with browser(tmp_path) as http:
        seed(tmp_path)
        before = http.get("/api/reviews").json()
        before_timer = http.get("/api/timer").json()
        editor = f"/api/projects/{project_id(2)}/metadata"
        result = http.put(editor, json={"tags": [" AI ", "ai", "编程"], "priority": "low"})
        assert result.status_code == 200
        assert result.json()["tags"] == ["AI", "编程"]
        assert http.get("/api/project-tags").json() == ["AI", "编程"]
        info = http.get(f"/api/projects/{project_id(2)}").json()
        assert (info["priority"], info["tags"], info["updatedOn"], info["lastLearnedOn"]) == ("low", ["AI", "编程"], "2027-02-03", None)
        assert numbers(query(http, layout="table", sort={"key": "updated", "direction": "desc"}), "all")[:2] == [2, 25]
        assert http.get("/api/reviews").json() == before
        assert http.get("/api/timer").json() == before_timer
        assert http.put(editor, json={"tags": [], "priority": None}).is_success
        assert http.get(f"/api/projects/{project_id(2)}").json()["tags"] == []


def test_confirmed_timer_counts_at_its_stashed_learning_date(tmp_path):
    with browser(tmp_path, now=datetime(2027, 2, 3, 15, 58)) as http:
        seed(tmp_path, 1)
        timer = submit(http, "timer", action="start", lessonId=project_id(1001))["active"]
    with browser(tmp_path, now=datetime(2027, 2, 3, 15, 59)) as http:
        submit(http, "timer", action="stash", timerId=timer["id"])
        assert query(http, filters=[{"field": "learned", "op": "never"}])["total"] == 1
    with browser(tmp_path, date(2027, 2, 4), datetime(2027, 2, 3, 17)) as http:
        submit(http, "timer", action="confirm", timerId=timer["id"])
        result = query(http, filters=[{"field": "learned", "op": "recent", "days": 2}])
        card = result["pages"]["active"]["items"][0]
        assert card["lastLearnedOn"] == "2027-02-03"
        assert card["updatedOn"] == "2027-01-01"
        assert http.get("/api/reviews").json()["reviews"] == []


def test_large_board_and_global_summary_only_return_the_requested_rows(tmp_path):
    with browser(tmp_path) as http:
        seed(tmp_path, 600)
        summary = http.get("/api/state").json()
        assert "projects" not in summary and "reviews" not in summary
        assert len(summary["recentProjects"]) == 4 and summary["taskCount"] == 600
        assert len(http.get("/api/tasks?page=2").json()["items"]) == 20
        assert len(http.get("/api/projects/options").json()) == 600
        start = perf_counter()
        response = http.post("/api/project-board/query", json={"pages": {"active": 120}})
        duration = perf_counter() - start
        assert response.status_code == 200
        result = response.json()
        print(f"600 projects: {duration * 1000:.1f} ms, {len(response.content)} bytes")
        assert result["total"] == 600
        assert numbers(result, "active") == [371, 372, 373]
        assert sum(len(page["items"]) for page in result["pages"].values()) == 9
        assert all("lessons" not in item for page in result["pages"].values() for item in page["items"])
        table = query(http, {"all": 2}, layout="table", pageSize=48, sort={"key": "name", "direction": "asc"})
        assert numbers(table, "all") == list(range(49, 97))
        assert numbers(query(http, layout="cards", search="600"), "all") == [600]


def test_migration_preserves_history_and_creates_readable_backup(tmp_path):
    path = tmp_path / "zhixue.sqlite3"
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    engine = open_db(tmp_path)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0003_library")
    finally:
        engine.dispose()
    saved_id = project_id(1)
    lesson_id = project_id(1001)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO projects VALUES (?, '老项目', '目标', '', 1, 0, '2027-01-31')", (saved_id,))
        db.execute("INSERT INTO tasks VALUES (?, 'ready', NULL)", (saved_id,))
        db.execute("INSERT INTO lessons VALUES (?, ?, 0, 1, 1, '2027-02-01')", (lesson_id, saved_id))
        db.execute("INSERT INTO timers VALUES (?, ?, 'finished', 75, NULL, '2027-02-01 17:00:00')", (project_id(2001), lesson_id))
    with browser(tmp_path) as http:
        found = query(http)["pages"]["done"]["items"][0]
        assert found["id"] == saved_id and found["progress"] == 100
        assert found["lastLearnedOn"] == found["updatedOn"] == "2027-02-02"
        assert found["priority"] is None and found["tags"] == []
    backup, = (tmp_path / "backups" / "database-upgrades").glob("*.sqlite3")
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0003_library",)
        assert db.execute("SELECT elapsed FROM timers").fetchone() == (75,)
