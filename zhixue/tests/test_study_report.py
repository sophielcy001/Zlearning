from contextlib import closing
from datetime import datetime, timedelta
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.project_activity import study_moment
from test_backups import accepted
from test_daily_plan import confirm, create_project, preview
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


def test_rereading_after_learning_stays_in_report_without_delaying_future_courses(tmp_path):
    now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        path = first_lesson(http, tmp_path)
        lesson_id = path.rsplit("/", 1)[-1]
        first = timer_action(http, "start", lessonId=lesson_id, category="learning")["active"]
        now += timedelta(minutes=6)
        timer_action(http, "finish", timerId=first["id"])
        accepted(http.put(f"{path}/learned", json={"learned": True}))

        now += timedelta(days=1)
        rereading = timer_action(http, "start", lessonId=lesson_id, category="learning")["active"]
        now += timedelta(minutes=90)
        timer_action(http, "finish", timerId=rereading["id"])

        now += timedelta(days=1)
        plan = accepted(http.get("/api/study-plan"))
        next_lessons = [task for day in plan["days"] for task in day["tasks"] if task["kind"] == "lesson"]
        assert next_lessons
        assert all((task["minutes"], task["estimateSource"]) == (6, "timer") for task in next_lessons)

        february = report(http, period="month")
        assert february["totals"]["actualSeconds"] == 5760
        assert february["efficiency"] == {"trackedLessons": 1, "secondsPerLesson": 360}
        prediction, = february["predictions"]
        assert len(next_lessons) == prediction["left"]
        assert prediction["expectedOn"] == next_lessons[-1]["date"]
        assert prediction["timerEstimate"] is True


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
        assert [(item["key"], item["round"], item["on"], item["outcome"]) for item in data["reviewRecords"]] == [
            ("rows-columns", 2, "2027-02-03", "passed"),
            ("unique-key", 1, "2027-02-02", "failed"),
            ("rows-columns", 1, "2027-02-02", "hinted"),
        ]
        latest = data["reviewRecords"][0]
        assert latest["lessonId"] == path.rsplit("/", 1)[-1]
        assert (latest["title"], latest["lessonTitle"], latest["projectTopic"], latest["groupId"]) == (
            "区分行与列", "理解表、行和列", "SQLite 入门", None,
        )
        earlier = report(http, period="day", day="2027-02-02")
        assert {item["key"] for item in earlier["weak"]} == {"rows-columns", "unique-key"}
        assert earlier["reviewRecords"] == data["reviewRecords"][1:]
        assert report(http, day="2027-02-01")["reviewRecords"] == []


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


@pytest.mark.parametrize("opened_plan", [False, True], ids=["without-snapshot", "with-provisional-snapshot"])
def test_resuming_a_desktop_timer_after_midnight_freezes_that_days_plan(tmp_path, opened_plan):
    now = datetime(2027, 2, 1, 15, 57)
    with local_app(tmp_path, lambda: now) as http:
        path = first_lesson(http, tmp_path)
        started = timer_action(http, "start", lessonId=path.rsplit("/", 1)[-1])
        timer = started["active"]["id"]
        now += timedelta(seconds=30)
        timer_action(http, "pause", timerId=timer)

        now = datetime(2027, 2, 1, 16, 10)
        if opened_plan:
            assert accepted(http.get("/api/study-plan"))["days"][0]["budgetMinutes"] == 30
        hours = "/api/study-plan/days/2027-02-02"
        accepted(http.put(hours, json={"windows": [{"start": "09:00", "end": "09:15"}]}))
        assert timer_action(http, "resume", timerId=timer)["active"]["id"] == timer
        assert report(http)["totals"]["planBudgetSeconds"] == 900

        accepted(http.put(hours, json={"windows": [{"start": "09:00", "end": "09:45"}]}))
        assert accepted(http.get("/api/study-plan"))["days"][0]["budgetMinutes"] == 45
        # Replaying an already-applied continue request must preserve the baseline.
        assert timer_action(http, "resume", timerId=timer)["active"]["elapsed"] == 30
        now += timedelta(minutes=2)
        timer_action(http, "finish", timerId=timer)

    with local_app(tmp_path, lambda: now) as http:
        first = report(http, day="2027-02-01")["totals"]
        second = report(http, day="2027-02-02")["totals"]
        assert (first["plannedSeconds"], first["actualSeconds"]) == (1800, 30)
        assert (second["plannedSeconds"], second["actualSeconds"]) == (900, 120)
        assert second["planDifferenceSeconds"] == -780
        assert report(http, period="month")["totals"]["recordedPlanDays"] == 2


@pytest.mark.parametrize(("target", "status"), [("2027-02-01", "missed"), ("2027-06-01", "future")])
def test_prediction_explains_the_project_lock_until_it_is_released(tmp_path, target, status):
    now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        lesson = first_lesson(http, tmp_path).rsplit("/", 1)[-1]
        other = create_project(http, tmp_path, "low")
        plan = accepted(http.get("/api/study-plan"))
        task = next(task for day in plan["days"] for task in day["tasks"] if task["lessonId"] == lesson)
        move, checked = preview(http, task, target, "19:00")
        assert checked["confirmation"]
        accepted(confirm(http, move, checked))
        if status == "missed":
            now += timedelta(days=1)

        def predictions():
            return {item["projectId"]: item for item in report(http)["predictions"]}

        entries = predictions()
        blocked = entries[task["projectId"]]
        unaffected = entries[other["id"]]
        assert blocked["projectTopic"] == unaffected["projectTopic"]
        assert blocked["expectedOn"] is None
        assert blocked["pendingLessonLocks"] == [{
            "id": task["id"], "title": task["title"], "status": status,
            "date": target, "start": "19:00", "end": "19:15",
        }]
        assert unaffected["expectedOn"] is not None and unaffected["pendingLessonLocks"] == []
        pending, = accepted(http.get("/api/study-plan"))["pendingLocks"]
        assert pending["id"] == task["id"] and pending["date"] == target

        move, checked = preview(http, task)
        assert checked["confirmation"]
        accepted(confirm(http, move, checked))
        repaired = predictions()[task["projectId"]]
        assert repaired["expectedOn"] is not None and repaired["pendingLessonLocks"] == []


def test_plan_coverage_and_previous_efficiency_use_only_the_selected_dates(tmp_path):
    now = datetime(2027, 1, 25, 8)
    with local_app(tmp_path, lambda: now) as http:
        project = create_project(http, tmp_path, None)
        for lesson, day, minutes in zip(project["lessons"], (25, 27, 1), (8, 20, 6), strict=True):
            now = datetime(2027, 1 if day > 1 else 2, day, 8)
            timer = timer_action(http, "start", lessonId=lesson["id"])["active"]
            now += timedelta(minutes=minutes)
            timer_action(http, "finish", timerId=timer["id"])
            accepted(http.put(f"/api/lessons/{lesson['id']}/learned", json={"learned": True}))

        now = datetime(2027, 2, 1, 15, 55)
        carried = timer_action(http, "start", lessonId=project["lessons"][2]["id"], category="notes")["active"]
        now += timedelta(minutes=10)
        timer_action(http, "finish", timerId=carried["id"])

        current = report(http, period="week")
        assert (current["from"], current["through"]) == ("2027-02-01", "2027-02-02")
        totals = current["totals"]
        assert (totals["periodDays"], totals["recordedPlanDays"]) == (2, 1)
        assert (totals["actualSeconds"], totals["actualOnPlannedDaysSeconds"], totals["actualWithoutPlanSeconds"]) == (960, 660, 300)
        assert totals["planDifferenceSeconds"] == 660 - totals["plannedSeconds"]
        assert current["efficiency"] == {"trackedLessons": 1, "secondsPerLesson": 360}
        assert current["previousEfficiency"] == {
            "from": "2027-01-25", "through": "2027-01-26", "trackedLessons": 1, "secondsPerLesson": 480,
        }

        daily = report(http)
        assert (daily["totals"]["periodDays"], daily["totals"]["recordedPlanDays"]) == (1, 0)
        assert daily["totals"]["plannedSeconds"] is None
        assert daily["totals"]["actualWithoutPlanSeconds"] == 300
        assert daily["previousEfficiency"] == {
            "from": "2027-02-01", "through": "2027-02-01", "trackedLessons": 1, "secondsPerLesson": 360,
        }
        monthly = report(http, period="month")
        assert monthly["totals"]["actualWithoutPlanSeconds"] == 300
        assert monthly["previousEfficiency"] == {
            "from": "2027-01-01", "through": "2027-01-02", "trackedLessons": 0, "secondsPerLesson": None,
        }

        accepted(http.put("/api/study-plan/days/2027-02-02", json={"windows": []}))
        accepted(http.get("/api/study-plan"))
        zero = report(http)["totals"]
        assert (zero["periodDays"], zero["recordedPlanDays"], zero["plannedSeconds"]) == (1, 1, 0)
        assert (zero["actualOnPlannedDaysSeconds"], zero["actualWithoutPlanSeconds"], zero["planDifferenceSeconds"]) == (300, 0, 300)


def test_correction_history_keeps_each_original_day_and_appends_reused_values(tmp_path):
    now = datetime(2027, 2, 1, 15, 50)
    first_day, second_day = "2027-02-01", "2027-02-02"
    with local_app(tmp_path, lambda: now) as http:
        lesson = first_lesson(http, tmp_path).rsplit("/", 1)[-1]
        timer = timer_action(http, "start", lessonId=lesson, category="learning")["active"]["id"]
        now += timedelta(minutes=20)
        timer_action(http, "finish", timerId=timer)
        history_path = f"{REPORT}/timers/{timer}/corrections"
        history = lambda day: accepted(http.get(history_path, params={"sourceOn": day}))["versions"]
        original, = history(first_day)
        assert original == {
            "revision": 0, "reportOn": first_day, "seconds": 600, "category": "learning", "savedAt": None,
        }

        correction = f"{REPORT}/timers/{timer}/correction"
        first = {"sourceOn": first_day, "reportOn": first_day}
        accepted(http.put(correction, json={**first, "seconds": 420, "category": "notes", "baseRevision": 0}))
        accepted(http.put(correction, json={**first, "seconds": 360, "category": "assistant", "baseRevision": 1}))
        accepted(http.put(correction, json={"sourceOn": second_day, "reportOn": second_day,
                                           "seconds": 180, "category": "learning", "baseRevision": 0}))
        versions = history(first_day)
        assert [(item["revision"], item["seconds"], item["category"]) for item in versions] == [
            (2, 360, "assistant"), (1, 420, "notes"), (0, 600, "learning"),
        ]
        assert all(datetime.fromisoformat(item["savedAt"]).utcoffset() == timedelta() for item in versions[:-1])
        assert [(item["revision"], item["seconds"]) for item in history(second_day)] == [(1, 180), (0, 600)]
        assert http.get(history_path, params={"sourceOn": "2027-02-03"}).status_code == 404

        reuse = {**first, "seconds": original["seconds"], "category": original["category"]}
        assert http.put(correction, json={**reuse, "baseRevision": 1}).status_code == 409
        assert history(first_day) == versions
        assert accepted(http.put(correction, json={**reuse, "baseRevision": 2}))["revision"] == 3
        assert history(first_day)[1:] == versions
        assert report(http, day=first_day)["totals"]["actualSeconds"] == 600
        assert report(http, day=second_day)["totals"]["actualSeconds"] == 180
        assert accepted(http.get("/api/timer"))["lastSaved"]["elapsed"] == 1200


def test_legacy_correction_history_keeps_prior_report_dates_across_restart(tmp_path):
    now = datetime(2027, 2, 2, 8)
    timer = "00000000-0000-4000-8000-000000000289"
    history = f"{REPORT}/timers/{timer}/corrections"
    with local_app(tmp_path, lambda: now) as http:
        lesson = first_lesson(http, tmp_path).rsplit("/", 1)[-1]
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as connection, connection:
            connection.execute("INSERT INTO timers (id, lesson_id, status, elapsed, updated_at) VALUES (?, ?, ?, ?, ?)",
                               (timer, lesson, "finished", 600, now.isoformat(" ")))
        correction = f"{REPORT}/timers/{timer}/correction"
        fields = {"sourceOn": None, "seconds": 480, "category": "notes"}
        accepted(http.put(correction, json={**fields, "reportOn": "2027-02-01", "baseRevision": 0}))
        accepted(http.put(correction, json={**fields, "reportOn": "2027-02-02", "baseRevision": 1}))

    with local_app(tmp_path, lambda: now) as http:
        versions = accepted(http.get(history))["versions"]
        assert [(item["revision"], item["reportOn"], item["seconds"]) for item in versions] == [
            (2, "2027-02-02", 480), (1, "2027-02-01", 480), (0, None, 600),
        ]
        saved = accepted(http.put(correction, json={"sourceOn": None, "reportOn": None,
                                                   "seconds": 600, "category": "unclassified", "baseRevision": 2}))
        assert saved["revision"] == 3
        assert report(http)["totals"]["actualSeconds"] == 0
        assert report(http)["unassignedTimers"][0]["recordedSeconds"] == 600
        assert accepted(http.get(history))["versions"][1:] == versions
