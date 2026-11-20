from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.worker import prepare_one
from test_backups import accepted
from test_demo import BASE_URL, GOAL, HEADERS


PLAN = "/api/study-plan"
FIRST_DAY = date(2027, 1, 31)


def client(folder, now=None):
    app = create_app(folder, "demo-test-session", today=lambda: FIRST_DAY, now=now)
    return TestClient(app, base_url=BASE_URL, headers=HEADERS)


def set_windows(http, start="09:00", end="09:15"):
    return accepted(http.put(f"{PLAN}/settings", json={
        "weekly": [[{"start": start, "end": end}] for _ in range(7)],
    }))


def create_project(http, folder, priority):
    project = accepted(http.post("/api/projects", json={**GOAL, "id": str(uuid4())}))
    while prepare_one(folder):
        pass
    accepted(http.put(f"/api/projects/{project['id']}/metadata", json={"priority": priority, "tags": []}))
    return project


def lesson_api(project):
    return f"/api/lessons/{project['lessons'][0]['id']}"


def test_shared_budget_preserves_due_dates_and_does_not_double_book_completed_reviews(tmp_path):
    with client(tmp_path) as http:
        set_windows(http)
        low = create_project(http, tmp_path, "low")
        high = create_project(http, tmp_path, "high")
        first = accepted(http.get(PLAN))["days"][0]
        assert first["budgetMinutes"] == first["plannedMinutes"] == 15
        assert [task["lessonId"] for task in first["tasks"]] == [high["lessons"][0]["id"]]

        lesson = lesson_api(low)
        accepted(http.put(f"{lesson}/learned", json={"learned": True}))
        assert accepted(http.get(PLAN))["days"][0]["tasks"] == []
        accepted(http.post("/api/demo/next-day"))
        for day in ("2027-02-01", "2027-02-02"):
            accepted(http.put(f"{PLAN}/days/{day}", json={"windows": [{"start": "09:00", "end": "09:05"}]}))
        original_reviews = accepted(http.get("/api/reviews"))["reviews"]
        first, second, third = accepted(http.get(PLAN))["days"][:3]
        review, = first["tasks"]
        overflow, = second["tasks"]
        assert review["kind"] == overflow["kind"] == "review"
        assert review["key"] != overflow["key"]
        assert review["dueOn"] == overflow["dueOn"] == "2027-02-01"
        assert third["tasks"][0]["projectId"] == high["id"]
        assert accepted(http.get("/api/reviews"))["reviews"] == original_reviews

        accepted(http.post(f"{lesson}/reviews/{review['key']}/submit", json={
            "round": 1, "answers": [], "action": "unknown",
        }))
        first, second, third = accepted(http.get(PLAN))["days"][:3]
        assert first["estimatedCompletedMinutes"] == 5 and first["tasks"] == []
        repaired, = second["tasks"]
        assert repaired["key"] == review["key"] and repaired["weak"]
        assert repaired["dueOn"] == "2027-02-02"
        assert third["tasks"][0]["key"] == overflow["key"]
        assert third["tasks"][0]["dueOn"] == "2027-02-01"

        accepted(http.put(f"/api/projects/{low['id']}/pause", json={"paused": True}))
        assert all(task["projectId"] != low["id"] for day in accepted(http.get(PLAN))["days"] for task in day["tasks"])
        accepted(http.put(f"/api/projects/{low['id']}/pause", json={"paused": False}))
        assert accepted(http.get(PLAN))["days"][1]["tasks"] == [repaired]


def test_weekly_windows_day_overrides_and_timer_estimates_survive_restart(tmp_path):
    now = datetime(2027, 1, 31, 11)

    def clock():
        return now

    with client(tmp_path, clock) as http:
        windows = [{"start": "09:00", "end": "09:05"}, {"start": "10:00", "end": "10:20"}]
        weekly = [windows, [], [], [], [], [], [{"start": "19:00", "end": "19:30"}]]
        accepted(http.put(f"{PLAN}/settings", json={"weekly": weekly}))
        invalid = [{"start": "19:00", "end": "19:30"}, {"start": "19:10", "end": "19:50"}]
        assert http.put(f"{PLAN}/days/2027-02-01", json={"windows": invalid}).status_code == 422
        assert http.put(f"{PLAN}/days/2027-02-01", json={"windows": [{"start": "12:00", "end": "00:00"}]}).status_code == 422
        accepted(http.put(f"{PLAN}/days/2027-02-02", json={"windows": [{"start": "23:45", "end": "24:00"}]}))
        accepted(http.put(f"{PLAN}/days/2027-02-07", json={"windows": []}))
        project = create_project(http, tmp_path, None)
        clocked = accepted(http.post("/api/timer", json={"action": "start", "lessonId": project["lessons"][0]["id"]}))
        now += timedelta(minutes=8)
        accepted(http.post("/api/timer", json={"action": "finish", "timerId": clocked["active"]["id"]}))
        accepted(http.put(f"{lesson_api(project)}/learned", json={"learned": True}))
        accepted(http.post("/api/demo/next-day"))

    with client(tmp_path, clock) as http:
        plan = accepted(http.get(PLAN))
        assert plan["weekly"] == weekly
        monday, tuesday, *_, sunday = plan["days"]
        assert monday["windows"] == windows
        assert monday["budgetMinutes"] == 25 and monday["plannedMinutes"] == 18
        assert [(task["start"], task["end"]) for task in monday["tasks"]] == [
            ("09:00", "09:05"), ("10:00", "10:05"), ("10:05", "10:13"),
        ]
        lesson = monday["tasks"][-1]
        assert (lesson["minutes"], lesson["estimateSource"]) == (8, "timer")
        assert (tuesday["budgetMinutes"], tuesday["override"], tuesday["tasks"][0]["start"]) == (15, True, "23:45")
        assert (sunday["budgetMinutes"], sunday["override"]) == (0, True)
        accepted(http.put(f"{PLAN}/days/2027-02-07", json={"windows": None}))
        sunday = accepted(http.get(PLAN))["days"][-1]
        assert (sunday["budgetMinutes"], sunday["override"]) == (30, False)


def preview(http, task, day=None, start=None):
    move = {"taskId": task["id"], "placement": {"date": day, "start": start} if day is not None else None}
    return move, accepted(http.post(f"{PLAN}/moves/preview", json=move))


def confirm(http, move, result):
    return http.post(f"{PLAN}/moves/confirm", json={**move, "confirmation": result["confirmation"]})


def test_move_preview_preserves_review_due_dates_and_cannot_confirm_a_stale_plan(tmp_path):
    with client(tmp_path) as http:
        set_windows(http, end="09:25")
        project = create_project(http, tmp_path, "high")
        accepted(http.put(f"{lesson_api(project)}/learned", json={"learned": True}))
        earliest = accepted(http.get(PLAN))["days"][1]["tasks"][0]
        _, premature = preview(http, earliest, "2027-01-31", "09:00")
        assert premature["confirmation"] is None
        assert [conflict["code"] for conflict in premature["conflicts"]] == ["before_due"]

        accepted(http.post("/api/demo/next-day"))
        original = accepted(http.get(PLAN))
        review, other_review, _ = original["days"][0]["tasks"]
        original_queue = accepted(http.get("/api/reviews"))
        target = "2027-02-02"
        move, result = preview(http, review, target, "09:00")
        assert result["confirmation"] and not result["conflicts"]
        assert result["before"]["date"] == "2027-02-01"
        assert result["after"] == {"date": target, "start": "09:00", "end": "09:05", "locked": True}
        assert any(item["id"] == other_review["id"] for item in result["changes"])
        assert accepted(http.get(PLAN)) == original

        accepted(http.put(f"{PLAN}/days/{target}", json={"windows": [{"start": "09:00", "end": "09:10"}]}))
        assert confirm(http, move, result).status_code == 409
        assert not any(task["locked"] for day in accepted(http.get(PLAN))["days"] for task in day["tasks"])
        move, result = preview(http, review, target, "09:00")
        invalid = {**move, "placement": {"date": target, "start": "09:05"}}
        assert confirm(http, invalid, result).status_code == 409
        changed = accepted(confirm(http, move, result))["plan"]
        assert accepted(confirm(http, move, result))["plan"] == changed
        assert changed["weekly"] == original["weekly"]
        locked = next(task for task in changed["days"][1]["tasks"] if task["id"] == review["id"])
        assert locked["locked"] and locked["dueOn"] == "2027-02-01"
        assert accepted(http.get("/api/reviews")) == original_queue
        assert [(day["date"], day["budgetMinutes"]) for day in changed["days"][:2]] == [
            ("2027-02-01", 25), ("2027-02-02", 10),
        ]


def test_locked_tasks_are_retained_across_pause_restart_and_missed_days_until_released(tmp_path):
    today = FIRST_DAY.isoformat()
    with client(tmp_path) as http:
        set_windows(http, end="09:30")
        first_project = create_project(http, tmp_path, "high")
        second_project = create_project(http, tmp_path, "low")
        original = accepted(http.get(PLAN))
        task = original["days"][0]["tasks"][0]
        other = next(item for day in original["days"] for item in day["tasks"] if item["projectId"] == second_project["id"])
        move, result = preview(http, task, today, "09:10")
        saved = accepted(confirm(http, move, result))["plan"]
        assert len(saved["days"][0]["tasks"]) == 1
        assert saved["days"][0]["tasks"][0]["locked"]
        _, conflict = preview(http, other, today, "09:15")
        assert conflict["confirmation"] is None
        assert conflict["conflicts"][0]["code"] == "locked"
        assert conflict["conflicts"][0]["task"]["id"] == task["id"]
        _, outside = preview(http, other, today, "09:25")
        assert [item["code"] for item in outside["conflicts"]] == ["outside_hours"]
        assert http.put(f"{PLAN}/days/{today}", json={"windows": []}).status_code == 409
        assert http.put(f"{PLAN}/settings", json={"weekly": [[] for _ in range(7)]}).status_code == 409

        accepted(http.put(f"/api/projects/{first_project['id']}/pause", json={"paused": True}))
        paused = accepted(http.get(PLAN))
        reserved, = paused["pendingLocks"]
        assert reserved["id"] == task["id"] and reserved["status"] == "paused"
        assert all(item["projectId"] != first_project["id"] for day in paused["days"] for item in day["tasks"])
        _, blocked = preview(http, other, today, "09:10")
        assert [item["code"] for item in blocked["conflicts"]] == ["locked"]
        assert not paused["days"][0]["override"]

    with client(tmp_path) as http:
        accepted(http.put(f"/api/projects/{first_project['id']}/pause", json={"paused": False}))
        assert accepted(http.get(PLAN))["days"][0]["tasks"] == saved["days"][0]["tasks"]
        accepted(http.post("/api/demo/next-day"))
        current = accepted(http.get(PLAN))
        missed, = current["pendingLocks"]
        assert missed["id"] == task["id"] and missed["status"] == "missed"
        assert all(item["id"] != task["id"] for day in current["days"] for item in day["tasks"])
        move, released = preview(http, task)
        assert released["after"] is None or not released["after"]["locked"]
        updated = accepted(confirm(http, move, released))["plan"]
        assert not updated["pendingLocks"]
        assert any(item["id"] == task["id"] and not item["locked"] for day in updated["days"] for item in day["tasks"])


def test_far_future_lock_remains_editable_and_completed_tasks_release_the_reservation(tmp_path):
    with client(tmp_path) as http:
        set_windows(http, end="09:30")
        project = create_project(http, tmp_path, "high")
        first = accepted(http.get(PLAN))["days"][0]["tasks"][0]
        target = "2027-03-17"
        move, result = preview(http, first, target, "09:05")
        updated = accepted(confirm(http, move, result))["plan"]
        future, = updated["pendingLocks"]
        assert future["status"] == "future" and future["date"] == target
        assert all(task["id"] != first["id"] for day in updated["days"] for task in day["tasks"])
        assert http.put(f"{PLAN}/days/{target}", json={"windows": []}).status_code == 409

        accepted(http.put(f"{lesson_api(project)}/learned", json={"learned": True}))
        restored = accepted(http.get(PLAN))
        assert not restored["pendingLocks"]
        assert restored["days"][0]["estimatedCompletedMinutes"] == 15
        accepted(http.put(f"{PLAN}/days/{target}", json={"windows": []}))
        _, blocked = preview(http, restored["days"][1]["tasks"][0], target, "09:05")
        assert blocked["conflicts"][0]["code"] == "outside_hours"


def test_finishing_and_undoing_a_course_does_not_restore_its_old_lock(tmp_path):
    today = FIRST_DAY.isoformat()
    with client(tmp_path) as http:
        set_windows(http, end="09:30")
        project = create_project(http, tmp_path, "high")
        first, second = accepted(http.get(PLAN))["days"][0]["tasks"]
        move, result = preview(http, first, today, "09:00")
        accepted(confirm(http, move, result))
        path = lesson_api(project)
        accepted(http.put(f"{path}/learned", json={"learned": True}))
        move, replacement = preview(http, second, today, "09:00")
        accepted(confirm(http, move, replacement))
        accepted(http.put(f"{path}/learned", json={"learned": False}))
        plan = accepted(http.get(PLAN))
        locked = [item for day in plan["days"] for item in day["tasks"] if item["locked"]]
        assert [item["id"] for item in locked] == [second["id"]]
        assert not plan["pendingLocks"]
        move, fresh = preview(http, first, today, "09:15")
        after = accepted(confirm(http, move, fresh))["plan"]
        assert [item["id"] for item in after["days"][0]["tasks"] if item["locked"]] == [second["id"], first["id"]]


def test_manual_booking_still_obeys_the_completed_and_locked_budget(tmp_path):
    today = FIRST_DAY.isoformat()
    with client(tmp_path) as http:
        set_windows(http, end="09:30")
        project = create_project(http, tmp_path, None)
        original = accepted(http.get(PLAN))
        first, second = original["days"][0]["tasks"]
        third, = original["days"][1]["tasks"]
        move, result = preview(http, third, today, "09:15")
        accepted(confirm(http, move, result))
        accepted(http.put(f"/api/lessons/{first['lessonId']}/learned", json={"learned": True}))
        _, blocked = preview(http, second, today, "09:00")
        assert blocked["confirmation"] is None
        assert [item["code"] for item in blocked["conflicts"]] == ["budget"]
        accepted(http.put(f"/api/lessons/{second['lessonId']}/learned", json={"learned": True}))
        today_plan = accepted(http.get(PLAN))["days"][0]
        assert today_plan["plannedMinutes"] == 15 and today_plan["estimatedCompletedMinutes"] == 30
        assert today_plan["remainingMinutes"] == 0 and today_plan["tasks"][0]["id"] == third["id"]


def test_conflicting_confirmations_from_two_tabs_book_only_one_task(tmp_path):
    today = FIRST_DAY.isoformat()
    with client(tmp_path) as http:
        set_windows(http, end="09:30")
        create_project(http, tmp_path, "medium")
        first, second = accepted(http.get(PLAN))["days"][0]["tasks"]
        moves = [preview(http, task, today, "09:00") for task in (first, second)]
        assert all(result["confirmation"] for _, result in moves)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(confirm, http, move, result) for move, result in moves]
            assert sorted(future.result().status_code for future in futures) == [200, 409]
        slots = accepted(http.get(PLAN))["days"][0]["tasks"]
        booked, = [item for item in slots if item["locked"]]
        automatic, = [item for item in slots if not item["locked"]]
        assert booked["start"] == "09:00" and automatic["start"] == "09:15"
