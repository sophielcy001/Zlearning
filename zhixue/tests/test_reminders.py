from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from test_backups import accepted
from test_course_verification import authorize, model_traffic, prepare, publish
from test_daily_plan import confirm, preview
from test_demo import first_lesson
from test_model_study import run, session
from test_study_report import local_app


API = "/api/reminders"


def check(http, trigger="foreground", day=None, shown=()):
    return accepted(http.post(f"{API}/check", json={"trigger": trigger, "day": day, "shown": list(shown)}))


def action(http, card, value):
    return accepted(http.post(f"{API}/{card['id']}/action", json={"version": card["version"], "action": value}))


def settings(http, enabled=True, quiet=None):
    return accepted(http.put(f"{API}/settings", json={"enabled": enabled, "quiet": quiet}))


def learn(http, lesson):
    accepted(http.put(f"{lesson}/learned", json={"learned": True}))


def test_first_foreground_check_after_midnight_is_merged_and_deduplicated_across_restart(tmp_path):
    now = datetime(2027, 1, 31, 15, 59)
    with local_app(tmp_path, lambda: now) as http:
        lesson = first_lesson(http, tmp_path)
        learn(http, lesson)
        before = check(http)
        assert before["day"] == "2027-01-31" and before["reminders"] == []
        now += timedelta(minutes=2)
        assert check(http, "scheduled", day=before["day"])["reminders"] == []
        assert check(http, "scheduled", day="2027-02-01")["reminders"] == []
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: check(http), range(2)))
        occurrence, = [card for reply in replies for card in reply["reminders"]]
        assert occurrence["kind"] == "daily" and occurrence["reviews"] == 2 and occurrence["disputes"] == 0
        same, = check(http, shown=[{"id": occurrence["id"], "version": occurrence["version"]}])["reminders"]
        assert same == occurrence
        assert check(http)["reminders"] == []
        review = accepted(http.get("/api/reviews"))
        action(http, occurrence, "dismiss")
        assert accepted(http.get("/api/reviews")) == review

    with local_app(tmp_path, lambda: now) as http:
        assert check(http)["reminders"] == []
        now += timedelta(days=1)
        delivered, = check(http)["reminders"]
        assert delivered["version"] == occurrence["version"] + 1 and delivered["reviews"] == 2


def test_snooze_obeys_quiet_hours_and_can_wait_until_tomorrow_without_rewriting_due_dates(tmp_path):
    now = datetime(2027, 1, 30, 13, 50)
    with local_app(tmp_path, lambda: now) as http:
        lesson = first_lesson(http, tmp_path)
        learn(http, lesson)
        now += timedelta(days=1)
        quiet = {"start": "22:00", "end": "08:00"}
        assert settings(http, quiet=quiet)["quiet"] == quiet
        occurrence, = check(http)["reminders"]
        original = accepted(http.get("/api/reviews"))
        requested = action(http, occurrence, "in_30_minutes")
        now += timedelta(minutes=1)
        assert action(http, occurrence, "in_30_minutes") == requested
        assert check(http, "scheduled", day="2027-01-31")["nextCheckSeconds"] is None
        now += timedelta(minutes=29)
        assert check(http, "scheduled", day="2027-01-31")["reminders"] == []
        now += timedelta(hours=10)
        assert check(http, "scheduled", day="2027-01-31")["reminders"] == []
        delivered, = check(http)["reminders"]
        assert delivered["reviews"] == 2 and delivered["version"] == occurrence["version"] + 1
        assert http.post(f"{API}/{occurrence['id']}/action", json={
            "version": occurrence["version"], "action": "dismiss",
        }).status_code == 409
        action(http, delivered, "tomorrow")
        assert check(http)["reminders"] == []
        assert {item["dueOn"] for item in accepted(http.get("/api/reviews"))["reviews"]} == {
            item["dueOn"] for item in original["reviews"]
        }

    with local_app(tmp_path, lambda: now) as http:
        now += timedelta(days=1)
        assert settings(http, enabled=False, quiet=quiet)["enabled"] is False
        assert check(http)["reminders"] == []
        settings(http, quiet=quiet)
        again, = check(http)["reminders"]
        assert again["version"] == delivered["version"] + 1


def test_fixed_lesson_is_independent_of_daily_reminder_and_resnoozing_requires_an_outstanding_lock(tmp_path):
    now = datetime(2027, 1, 31, 10, 59)
    with local_app(tmp_path, lambda: now) as http:
        lesson_url = first_lesson(http, tmp_path)
        plan = accepted(http.get("/api/study-plan"))
        today = plan["today"]
        task = plan["days"][0]["tasks"][0]
        change, result = preview(http, task, today, "19:00")
        accepted(confirm(http, change, result))
        response = check(http)
        assert response["reminders"] == [] and response["nextCheckSeconds"] == 60
        now += timedelta(minutes=1)
        fixed, = check(http, "scheduled", day=today)["reminders"]
        assert fixed["kind"] == "lesson" and fixed["lessonId"] == task["lessonId"] and fixed["startsAt"] == "19:00"
        assert accepted(http.get("/api/timer"))["active"] is None
        assert check(http)["reminders"] == []
        action(http, fixed, "tomorrow")

    with local_app(tmp_path, lambda: now) as http:
        now += timedelta(days=1)
        assert check(http, "scheduled", day=today)["reminders"] == []
        next_card, = check(http)["reminders"]
        assert next_card["id"] == fixed["id"] and next_card["slotDay"] == today
        shown = [{"id": next_card["id"], "version": next_card["version"]}]
        assert check(http, shown=shown)["reminders"] == [next_card]
        action(http, next_card, "in_30_minutes")
        learn(http, lesson_url)
        now += timedelta(minutes=30)
        assert check(http)["reminders"] == []


def test_resolved_reviews_are_not_repeated_after_a_snooze(tmp_path):
    now = datetime(2027, 1, 31, 10)
    with local_app(tmp_path, lambda: now) as http:
        lesson = first_lesson(http, tmp_path)
        learn(http, lesson)
        now += timedelta(days=1)
        occurrence, = check(http)["reminders"]
        action(http, occurrence, "in_30_minutes")
        assert check(http)["nextCheckSeconds"] == 1800
        for review in accepted(http.get("/api/reviews"))["reviews"]:
            accepted(http.post(f"{lesson}/reviews/{review['key']}/submit", json={
                "round": 1, "action": "unknown", "answers": [],
            }))
        now += timedelta(minutes=31)
        assert check(http, "scheduled", day="2027-02-01")["reminders"] == []
        assert check(http)["nextCheckSeconds"] is None


def test_disputes_and_reviews_share_one_reminder_and_rechecks_are_not_repeated(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, _, payload = prepare(http, tmp_path, calls, limit=6)
        authorize(http, project, payload)
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        review_lesson = first_lesson(http, tmp_path)
        learn(http, review_lesson)
        tomorrow = accepted(http.post("/api/demo/next-day"))["today"]
        first, = check(http)["reminders"]
        assert (first["reviews"], first["disputes"]) == (2, 3)

        finding = accepted(http.get(f"/api/lessons/{lesson}/verification"))["findingId"]
        request = {"action": "recheck", "items": [{"lessonId": lesson, "index": 1, "findingId": finding}]}
        loaded = accepted(http.post("/api/disputes/preview", json=request))
        accepted(http.post("/api/disputes/apply", json={**request, "confirmation": loaded["confirmation"]}))
        continued, = check(http, "scheduled", day=tomorrow, shown=[{"id": first["id"], "version": first["version"]}])["reminders"]
        assert (continued["reviews"], continued["disputes"], continued["version"]) == (2, 0, first["version"])
        assert accepted(http.get("/api/state"))["disputeCount"] == 3


def test_fixed_slot_reaches_the_end_of_quiet_hours_within_the_same_day(tmp_path):
    now = datetime(2027, 1, 31, 10, 59)
    with local_app(tmp_path, lambda: now) as http:
        first_lesson(http, tmp_path)
        plan = accepted(http.get("/api/study-plan"))
        day = plan["today"]
        task = plan["days"][0]["tasks"][0]
        move, result = preview(http, task, day, "19:00")
        accepted(confirm(http, move, result))
        assert http.put(f"{API}/settings", json={
            "enabled": True, "quiet": {"start": "18:00", "end": "18:00"},
        }).status_code == 422
        settings(http, quiet={"start": "18:30", "end": "20:00"})
        blocked = check(http)
        assert blocked["reminders"] == [] and blocked["nextCheckSeconds"] == 3660
        now += timedelta(seconds=3660)
        fixed, = check(http, "scheduled", day=day)["reminders"]
        assert fixed["lessonId"] == task["lessonId"]
        action(http, fixed, "dismiss")
        assert check(http)["reminders"] == []


def test_phone_and_desktop_claim_one_daily_reminder_and_retry_the_same_delivery(tmp_path):
    from uuid import uuid4

    now = datetime(2027, 1, 31, 12)
    mobile = "/api/mobile/reminders"
    with local_app(tmp_path, lambda: now) as http:
        lesson = first_lesson(http, tmp_path)
        learn(http, lesson)
        workspace = accepted(http.get("/api/mobile/catalog"))["workspace"]
        now += timedelta(days=1)
        body = {"workspace": workspace, "trigger": "foreground", "delivery": str(uuid4())}
        desktop = {"trigger": "foreground", "delivery": str(uuid4())}
        with ThreadPoolExecutor(max_workers=2) as pool:
            phone_response = pool.submit(http.post, mobile + "/check", json=body)
            desktop_response = pool.submit(http.post, API + "/check", json=desktop)
            first = accepted(phone_response.result()), accepted(desktop_response.result())
        single, = [card for reply in first for card in reply["reminders"]]
        first_path, first_body = (mobile, body) if first[0]["reminders"] else (API, desktop)
        assert accepted(http.post(first_path + "/check", json=first_body))["reminders"] == [single]
        assert accepted(http.post(mobile + "/check", json={**body, "delivery": str(uuid4())}))["reminders"] == []
        assert check(http)["reminders"] == []
        assert single["kind"] == "daily" and single["reviews"] == 2


def test_phone_snooze_preferences_and_workspace_scope_are_shared_with_desktop(tmp_path):
    from uuid import uuid4

    now = datetime(2027, 1, 31, 10)
    mobile = "/api/mobile/reminders"
    with local_app(tmp_path, lambda: now) as http:
        lesson = first_lesson(http, tmp_path)
        learn(http, lesson)
        now += timedelta(days=1)
        workspace = accepted(http.get("/api/mobile/catalog"))["workspace"]
        initial, = accepted(http.post(mobile + "/check", json={
            "trigger": "foreground", "workspace": workspace, "delivery": str(uuid4()),
        }))["reminders"]
        original = accepted(http.get("/api/reviews"))
        decision = {"workspace": workspace, "version": initial["version"], "action": "in_30_minutes"}
        endpoint = mobile + "/action"
        decision["id"] = initial["id"]
        mismatched = http.post(endpoint, json={**decision, "workspace": str(uuid4())})
        assert mismatched.status_code == 409
        assert accepted(http.post(endpoint, json=decision)) == accepted(http.post(endpoint, json=decision))
        assert check(http)["nextCheckSeconds"] == 1800
        quiet = {"start": "18:15", "end": "19:00"}
        assert accepted(http.put(mobile + "/settings", json={
            "workspace": workspace, "enabled": True, "quiet": quiet,
        })) == {"enabled": True, "quiet": quiet}
        assert accepted(http.get(API + "/settings"))["quiet"] == quiet
        now += timedelta(minutes=30)
        assert check(http, "scheduled", day="2027-02-01")["reminders"] == []
        now += timedelta(minutes=30)
        repeated, = check(http, "scheduled", day="2027-02-01")["reminders"]
        assert repeated["version"] == initial["version"] + 1
        assert accepted(http.get("/api/reviews")) == original
        assert accepted(http.post(mobile + "/check", json={"workspace": workspace, "trigger": "foreground"}))["reminders"] == []
        settings(http, enabled=False)
        assert not accepted(http.get(mobile + "/settings", params={"workspace": workspace}))["enabled"]
        assert check(http, shown=[{"id": repeated["id"], "version": repeated["version"]}])["reminders"] == []


def test_phone_does_not_consume_a_desktop_only_dispute_reminder(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, _, _, payload = prepare(http, tmp_path, calls, limit=6)
        authorize(http, project, payload)
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        workspace = accepted(http.get("/api/mobile/catalog"))["workspace"]
        phone = accepted(http.post("/api/mobile/reminders/check", json={
            "workspace": workspace, "trigger": "foreground",
        }))
        assert phone["reminders"] == []
        dispute, = check(http)["reminders"]
        assert dispute["reviews"] == 0 and dispute["disputes"] == 3
