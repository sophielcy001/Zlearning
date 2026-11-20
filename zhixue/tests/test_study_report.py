from contextlib import closing
from datetime import datetime, timedelta
import sqlite3

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.project_activity import study_moment
from test_backups import accepted
from test_demo import BASE_URL, HEADERS, first_lesson


REPORT = "/api/study-report"


def local_app(folder, now):
    app = create_app(folder, "demo-test-session", today=lambda: study_moment(now()).date(), now=now)
    return TestClient(app, base_url=BASE_URL, headers=HEADERS)


def report(http, period="day", day=None):
    params = {"period": period}
    if day is not None:
        params["on"] = day
    return accepted(http.get(REPORT, params=params))


def timer_action(http, action, **fields):
    return accepted(http.post("/api/timer", json={"action": action, **fields}))


def test_stashed_cross_midnight_timer_is_counted_only_after_confirmation_and_can_be_corrected(tmp_path):
    now = datetime(2027, 1, 31, 15, 50)
    with local_app(tmp_path, lambda: now) as http:
        path = first_lesson(http, tmp_path)
        started = timer_action(http, "start", lessonId=path.rsplit("/", 1)[-1], category="learning")
        timer = started["active"]["id"]
        now += timedelta(minutes=20)
        assert timer_action(http, "pause", timerId=timer)["active"]["elapsed"] == 1200
        now += timedelta(minutes=15)
        timer_action(http, "resume", timerId=timer)
        now += timedelta(minutes=10)
        assert timer_action(http, "stash", timerId=timer)["stashed"][0]["elapsed"] == 1800
        assert report(http)["totals"]["actualSeconds"] == 0

    with local_app(tmp_path, lambda: now) as http:
        now += timedelta(days=1)
        assert timer_action(http, "confirm", timerId=timer)["lastSaved"]["elapsed"] == 1800
        assert report(http, day="2027-02-02")["totals"]["actualSeconds"] == 0
        jan31 = report(http, day="2027-01-31")
        assert jan31["totals"]["actualSeconds"] == 600
        assert jan31["totals"]["plannedSeconds"] == jan31["totals"]["planBudgetSeconds"] == 1800
        assert jan31["totals"]["planDifferenceSeconds"] == -1200
        feb = report(http, period="month")
        assert (feb["from"], feb["through"]) == ("2027-02-01", "2027-02-02")
        assert feb["totals"]["actualSeconds"] == 1200
        entry, = feb["timeRecords"]
        assert (entry["sourceOn"], entry["recordedSeconds"], entry["seconds"], entry["category"]) == ("2027-02-01", 1200, 1200, "learning")

        correction = {"sourceOn": "2027-02-01", "reportOn": "2027-02-01", "seconds": 420,
                      "category": "notes", "baseRevision": 0}
        route = f"{REPORT}/timers/{timer}/correction"
        assert accepted(http.put(route, json=correction))["revision"] == 1
        assert accepted(http.put(route, json=correction))["revision"] == 1
        assert http.put(route, json={**correction, "seconds": 500}).status_code == 409
        assert http.put(route, json={**correction, "baseRevision": 1, "reportOn": "2027-02-02"}).status_code == 422
        corrected = report(http, period="month")
        assert corrected["totals"]["actualSeconds"] == 420
        assert corrected["totals"]["byCategory"]["notes"] == 420
        assert corrected["timeRecords"][0]["recordedSeconds"] == 1200
        assert report(http, day="2027-01-31")["totals"]["actualSeconds"] == 600
        assert accepted(http.get("/api/timer"))["lastSaved"]["elapsed"] == 1800


def test_timers_classify_by_explicit_activity_and_corrected_history_drives_new_lesson_estimates(tmp_path):
    now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        path = first_lesson(http, tmp_path)
        lesson = path.rsplit("/", 1)[-1]
        first = timer_action(http, "start", lessonId=lesson)["active"]
        now += timedelta(minutes=9)
        notes = timer_action(http, "start", lessonId=lesson, category="notes")
        assert notes["lastSaved"]["elapsed"] == 540
        assert notes["active"]["id"] != first["id"]
        now += timedelta(minutes=4)
        timer_action(http, "finish", timerId=notes["active"]["id"])
        accepted(http.put(f"{path}/learned", json={"learned": True}))
        correction = {"sourceOn": "2027-02-01", "reportOn": "2027-02-01", "seconds": 360,
                      "category": "learning", "baseRevision": 0}
        accepted(http.put(f"{REPORT}/timers/{first['id']}/correction", json=correction))
        now += timedelta(days=1)
        plan = accepted(http.get("/api/study-plan"))
        course = next(task for day in plan["days"] for task in day["tasks"] if task["kind"] == "lesson")
        assert (course["estimateSource"], course["minutes"]) == ("timer", 6)
        data = report(http, day="2027-02-01")
        entry, = [item for item in data["timeRecords"] if item["category"] == "notes"]
        assert entry["seconds"] == 240
        assert data["efficiency"] == {"trackedLessons": 1, "secondsPerLesson": 360}
        assert data["predictions"][0]["timerEstimate"]
        assert data["predictions"][0]["expectedOn"] == "2027-02-02"


def test_ready_lesson_legacy_elapsed_stays_available_and_can_be_assigned_to_a_day(tmp_path):
    now = datetime(2027, 2, 1, 8)
    timer_id = "00000000-0000-4000-8000-000000000021"
    with local_app(tmp_path, lambda: now) as http:
        path = first_lesson(http, tmp_path)
        lesson = path.rsplit("/", 1)[-1]
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as connection, connection:
            connection.execute("INSERT INTO timers (id, lesson_id, status, elapsed, updated_at) VALUES (?, ?, ?, ?, ?)",
                               (timer_id, lesson, "finished", 1800, now.isoformat(" ")))
        data = report(http)
        assert data["totals"]["actualSeconds"] == 0
        unassigned, = data["unassignedTimers"]
        assert unassigned["recordedSeconds"] == 1800 and unassigned["sourceOn"] is None
        route = f"{REPORT}/timers/{timer_id}/correction"
        accepted(http.put(route, json={"sourceOn": None, "reportOn": "2027-01-31", "baseRevision": 0,
                                       "seconds": 1200, "category": "assistant"}))
        earlier = report(http, day="2027-01-31")
        assert not earlier["unassignedTimers"]
        assert earlier["totals"]["byCategory"]["assistant"] == 1200
        assert earlier["timeRecords"][0]["recordedSeconds"] == 1800
        assert report(http)["totals"]["actualSeconds"] == 0


def test_review_report_keeps_assisted_attempts_separate_and_retires_weakness_after_independent_pass(tmp_path):
    now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        path = first_lesson(http, tmp_path)
        accepted(http.put(f"{path}/learned", json={"learned": True}))
        now += timedelta(days=1)
        rows = f"{path}/reviews/rows-columns"
        accepted(http.post(f"{rows}/hint", json={"round": 1}))
        accepted(http.post(f"{rows}/submit", json={"round": 1, "answers": [0, 1], "action": "submit"}))
        accepted(http.post(f"{path}/reviews/unique-key/submit", json={"round": 1, "answers": [], "action": "unknown"}))
        data = report(http, period="week")
        assert data["totals"]["review"] == {"passed": 0, "hinted": 1, "failed": 1, "ungraded": 0}
        assert data["totals"]["learned"] == 1
        assert {item["key"] for item in data["weak"]} == {"rows-columns", "unique-key"}
        now += timedelta(days=1)
        accepted(http.post(f"{rows}/submit", json={"round": 2, "answers": [1, 2], "action": "submit"}))
        data = report(http, period="month")
        assert data["totals"]["review"] == {"passed": 1, "hinted": 1, "failed": 1, "ungraded": 0}
        assert [item["key"] for item in data["weak"]] == ["unique-key"]
        earlier = report(http, period="day", day="2027-02-02")
        assert {item["key"] for item in earlier["weak"]} == {"rows-columns", "unique-key"}


def test_report_plan_baseline_stays_at_first_activity_when_writable_hours_change(tmp_path):
    now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        path = first_lesson(http, tmp_path)
        accepted(http.get("/api/study-plan"))
        accepted(http.put("/api/study-plan/days/2027-02-01", json={"windows": [{"start": "09:00", "end": "09:15"}]}))
        timer_action(http, "start", lessonId=path.rsplit("/", 1)[-1], category="assistant")
        accepted(http.put("/api/study-plan/days/2027-02-01", json={"windows": [{"start": "09:00", "end": "09:45"}]}))
        after = accepted(http.get("/api/study-plan"))["days"][0]
        assert after["budgetMinutes"] == 45
        data = report(http)
        assert data["totals"]["plannedSeconds"] == data["totals"]["planBudgetSeconds"] == 900
        assert data["totals"]["recordedPlanDays"] == 1
