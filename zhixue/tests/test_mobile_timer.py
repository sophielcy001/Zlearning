from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest

from test_demo import first_lesson
from test_study_report import accepted, local_app, report, timer_action


PATH = "/api/mobile/timer"


def stamp(value):
    return value.replace(tzinfo=UTC).isoformat()


class Phone:
    def __init__(self, http, folder):
        self.http = http
        self.lesson = first_lesson(http, folder).rsplit("/", 1)[-1]
        self.workspace = http.get("/api/mobile/catalog").json()["workspace"]
        self.timer = str(uuid4())

    def event(self, action, at, *, start=None, reliable=True, live=False, expected=None):
        return {
            "id": str(uuid4()), "workspace": self.workspace, "kind": "timer", "timerId": self.timer,
            "lessonId": self.lesson, "category": "learning", "action": action, "capturedAt": stamp(at),
            "live": live, "expected": expected,
            "span": {"beganAt": stamp(start), "endedAt": stamp(at), "reliable": reliable, "dayShift": 0} if start else None,
        }

    def send(self, event):
        response = self.http.post(PATH, json=event)
        assert response.is_success, response.text
        return response.json()

    def state(self):
        response = self.http.get(PATH, params={"workspace": self.workspace, "timerId": self.timer})
        assert response.is_success, response.text
        return response.json()


def test_live_phone_and_desktop_take_over_each_other_and_keep_exact_spans(tmp_path):
    start = now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        phone = Phone(http, tmp_path)
        original = timer_action(http, "start", lessonId=phone.lesson)["active"]
        now += timedelta(seconds=40)
        begin = phone.event("start", now, live=True, expected=original["id"])
        assert phone.send(begin)["state"] == "saved"
        assert phone.state()["active"]["id"] == phone.timer
        assert http.get("/api/timer").json()["active"]["device"] == "phone"
        assert http.get("/api/timer").json()["lastSaved"]["elapsed"] == 40
        now += timedelta(seconds=20)
        takeover = timer_action(http, "start", lessonId=phone.lesson)
        assert takeover["active"]["id"] not in (phone.timer, original["id"])
        assert takeover["active"]["device"] == "desktop"
        assert phone.state()["interruptedAt"] == stamp(now)
        finish = phone.event("finish", now, start=start + timedelta(seconds=40))
        result = phone.send(finish)
        assert result["state"] == "saved" and result["seconds"] == 20
        assert phone.send(finish) == result
        assert phone.send(begin)["state"] == "saved"
        assert phone.state()["active"]["id"] == takeover["active"]["id"]
        data = report(http)
        assert data["totals"]["actualSeconds"] == 60
        assert {row["recordedSeconds"] for row in data["timeRecords"]} == {20, 40}


def test_late_phone_intervals_apply_uncovered_parts_and_keep_overlap_for_review(tmp_path):
    start = now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        phone = Phone(http, tmp_path)
        now += timedelta(seconds=40)
        desktop = timer_action(http, "start", lessonId=phone.lesson)["active"]
        now += timedelta(seconds=30)
        timer_action(http, "finish", timerId=desktop["id"])
        now += timedelta(seconds=20)
        phone.send(phone.event("start", start))
        finish = phone.event("finish", now, start=start)
        received = phone.send(finish)
        assert received["state"] == "held" and received["seconds"] == 60
        assert phone.send(finish) == received
        pending, = http.get(PATH + "/conflicts").json()["records"]
        assert pending["span"] == finish["span"]
        assert pending["overlaps"][0]["timerId"] == desktop["id"]
        assert pending["seconds"] == 60
        assert report(http)["totals"]["actualSeconds"] == 90
        response = http.post(PATH + f"/conflicts/{finish['id']}", json={"action": "keep"})
        assert response.is_success
        assert not http.get(PATH + "/conflicts").json()["records"]
        assert report(http)["totals"]["actualSeconds"] == 90


def test_offline_pause_and_resume_replay_excludes_the_gap_across_shanghai_midnight(tmp_path):
    start = datetime(2027, 2, 1, 15, 59, 20)
    paused = datetime(2027, 2, 1, 16, 0, 10)
    resumed = datetime(2027, 2, 1, 16, 1)
    finished = resumed + timedelta(seconds=25)
    now = finished + timedelta(seconds=5)
    with local_app(tmp_path, lambda: now) as http:
        phone = Phone(http, tmp_path)
        begin = phone.event("start", start)
        pause = phone.event("pause", paused, start=start)
        resume = phone.event("resume", resumed)
        finish = phone.event("finish", finished, start=resumed)
        pause["span"]["startedBy"] = begin["id"]
        finish["span"]["startedBy"] = resume["id"]
        for event in (begin, pause, resume, finish):
            event["present"] = False

        assert phone.send(begin)["seconds"] == 0
        first = phone.send(pause)
        assert first["state"] == "saved" and first["seconds"] == 50
        assert phone.send(pause) == first
        assert phone.send(resume)["seconds"] == 0
        second = phone.send(finish)
        assert second["state"] == "saved" and second["seconds"] == 25
        assert phone.send(finish) == second
        assert phone.state()["active"] is None and phone.state()["pendingReview"] == 0
        earlier = report(http, day="2027-02-01")["totals"]
        later = report(http, day="2027-02-02")["totals"]
        assert (earlier["plannedSeconds"], earlier["actualSeconds"]) == (None, 40)
        assert (later["plannedSeconds"], later["actualSeconds"]) == (None, 35)
        assert report(http, period="month")["totals"]["actualSeconds"] == 75


def test_offline_phone_replay_protects_another_active_phone_timer_across_restart(tmp_path):
    began = now = datetime(2027, 2, 1, 15, 59, 30)
    online_began = began + timedelta(seconds=20)
    offline_ended = began + timedelta(seconds=50)
    online_ended = began + timedelta(seconds=80)
    with local_app(tmp_path, lambda: now) as http:
        offline = Phone(http, tmp_path)
        foreground = Phone(http, tmp_path)
        assert offline.workspace == foreground.workspace and offline.timer != foreground.timer
        foreground.lesson = next(row["id"] for row in http.get("/api/mobile/catalog").json()["lessons"]
                                 if row["id"] != offline.lesson)
        start = offline.event("start", began)
        assert offline.send(start)["state"] == "saved"
        assert offline.state()["active"] is None

        now = online_began
        foreground_start = foreground.event("start", now, live=True)
        assert foreground.send(foreground_start)["claimedAt"] == stamp(now)
        assert foreground.state()["active"] == {"id": foreground.timer, "device": "phone"}
        now = offline_ended
        offline_finish = offline.event("finish", now, start=began)
        received = offline.send(offline_finish)
        assert received == {"id": offline_finish["id"], "state": "held", "reason": "overlap", "seconds": 20}
        assert report(http, period="month")["totals"]["actualSeconds"] == 20

    with local_app(tmp_path, lambda: now) as reopened:
        offline.http = foreground.http = reopened
        assert offline.send(offline_finish) == received
        state = foreground.state()
        assert state["active"] == {"id": foreground.timer, "device": "phone"} and state["pendingReview"] == 1
        pending, = reopened.get(PATH + "/conflicts").json()["records"]
        assert pending["id"] == offline_finish["id"]
        assert pending["overlaps"] == [{
            "beganAt": stamp(online_began), "endedAt": stamp(offline_ended),
            "timerId": foreground.timer, "device": "phone",
        }]

        now = online_ended
        foreground_finish = foreground.event("finish", now, start=online_began)
        foreground_finish["span"]["startedBy"] = foreground_start["id"]
        online_result = foreground.send(foreground_finish)
        assert online_result["state"] == "saved" and online_result["seconds"] == 60
        assert foreground.send(foreground_finish) == online_result
        assert foreground.state()["active"] is None
        endpoint = PATH + "/conflicts/" + offline_finish["id"]
        resolution = reopened.post(endpoint, json={"action": "keep"})
        assert resolution.is_success and reopened.post(endpoint, json={"action": "keep"}).json() == resolution.json()
        assert foreground.state()["pendingReview"] == 0
        first = report(reopened, day="2027-02-01")["totals"]["actualSeconds"]
        second = report(reopened, day="2027-02-02")["totals"]["actualSeconds"]
        monthly = report(reopened, period="month")
        total = monthly["totals"]["actualSeconds"]
        assert (first, second, total) == (30, 50, 80)
        by_timer = {timer: sum(row["recordedSeconds"] for row in monthly["timeRecords"] if row["timerId"] == timer)
                    for timer in (offline.timer, foreground.timer)}
        assert by_timer == {offline.timer: 20, foreground.timer: 60}
    (tmp_path / "手机活动计时与离线重叠.json").write_text(json.dumps({
        "offlineOperation": offline_finish["id"], "activePhoneTimer": foreground.timer,
        "offlineCounted": received["seconds"], "offlineHeld": 30, "activeCounted": online_result["seconds"],
        "sameReceiptAfterRestart": True, "reviewCountAfterResolution": 0,
        "days": {"2027-02-01": first, "2027-02-02": second}, "monthSeconds": total, "perTimerSeconds": by_timer,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_lost_clock_keeps_raw_times_and_user_can_confirm_without_double_count(tmp_path):
    now = datetime(2027, 2, 1, 16, 0, 40)
    began = now - timedelta(seconds=90)
    with local_app(tmp_path, lambda: now) as http:
        phone = Phone(http, tmp_path)
        phone.send(phone.event("start", began))
        finish = phone.event("finish", now, start=began, reliable=False)
        assert phone.send(finish)["state"] == "held"
        assert report(http, period="month")["totals"]["actualSeconds"] == 0
        input = {"action": "confirm", "beganAt": stamp(began), "endedAt": stamp(now)}
        endpoint = PATH + f"/conflicts/{finish['id']}"
        response = http.post(endpoint, json=input)
        assert response.is_success, response.text
        assert response.json()["seconds"] == 90
        assert http.post(endpoint, json=input).json() == response.json()
        assert report(http, day="2027-02-01")["totals"]["actualSeconds"] == 50
        assert report(http, day="2027-02-02")["totals"]["actualSeconds"] == 40


@pytest.mark.parametrize("reliable", [True, False], ids=["automatic", "manual-clock-confirmation"])
def test_offline_phone_spans_preserve_saved_plan_baselines_for_their_learning_days(tmp_path, reliable):
    now = began = datetime(2027, 2, 1, 15, 59, 30)
    prior_day, resume_day = "2027-02-01", "2027-02-02"

    def plan_hours(http, day, end):
        accepted(http.put(f"/api/study-plan/days/{day}", json={"windows": [{"start": "09:00", "end": end}]}))
        return accepted(http.get("/api/study-plan"))["days"][0]["budgetMinutes"]

    with local_app(tmp_path, lambda: now) as http:
        phone = Phone(http, tmp_path)
        assert plan_hours(http, prior_day, "09:15") == 15
        now += timedelta(minutes=2)
        assert plan_hours(http, resume_day, "09:20") == 20

        phone.send(phone.event("start", began))
        finish = phone.event("finish", now, start=began, reliable=reliable)
        reply = phone.send(finish)
        assert phone.send(finish) == reply
        if reliable:
            assert (reply["state"], reply["seconds"]) == ("saved", 120)
            baseline = 1200
        else:
            assert (reply["state"], reply["reason"]) == ("held", "clock")
            assert report(http)["totals"]["actualSeconds"] == 0
            assert plan_hours(http, resume_day, "09:25") == 25
            decision = {"action": "confirm", "beganAt": stamp(began), "endedAt": stamp(now)}
            path = PATH + f"/conflicts/{finish['id']}"
            result = accepted(http.post(path, json=decision))
            assert result["seconds"] == 120
            assert accepted(http.post(path, json=decision)) == result
            baseline = 1500

        assert plan_hours(http, resume_day, "09:45") == 45
        assert report(http)["totals"]["planBudgetSeconds"] == baseline

    with local_app(tmp_path, lambda: now) as reopened:
        phone.http = reopened
        replayed = phone.send(finish)
        if reliable:
            assert replayed == reply
        else:
            assert replayed == result
        first = report(reopened, day=prior_day)["totals"]
        second = report(reopened, day=resume_day)["totals"]
        assert (first["planBudgetSeconds"], first["actualSeconds"]) == (900, 30)
        assert (second["planBudgetSeconds"], second["actualSeconds"]) == (baseline, 90)
        assert second["planDifferenceSeconds"] == 90 - second["plannedSeconds"]


def test_a_delayed_mobile_start_cannot_replace_the_current_desktop_timer(tmp_path):
    now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        phone = Phone(http, tmp_path)
        desktop = timer_action(http, "start", lessonId=phone.lesson)["active"]
        now += timedelta(seconds=5)
        began = now
        stale = phone.event("start", began, live=True, expected=desktop["id"])
        now += timedelta(seconds=25)
        assert phone.send(stale)["state"] == "saved"
        assert phone.state()["active"]["id"] == desktop["id"]
        offline = phone.send(phone.event("pause", now, start=began))
        assert offline["state"] == "held" and offline["seconds"] == 0
        assert phone.state()["active"]["id"] == desktop["id"]
        resumed = phone.event("resume", now, live=True, expected=desktop["id"])
        assert phone.send(resumed)["claimedAt"] == stamp(now)
        assert phone.state()["active"]["id"] == phone.timer
        now += timedelta(seconds=10)
        finish = phone.event("finish", now, start=now - timedelta(seconds=10))
        assert phone.send(finish)["seconds"] == 10
        assert report(http)["totals"]["actualSeconds"] == 40


def test_confirmation_of_uncertain_phone_time_checks_current_overlap(tmp_path):
    now = datetime(2027, 2, 1, 8)
    with local_app(tmp_path, lambda: now) as http:
        phone = Phone(http, tmp_path)
        began = now
        phone.send(phone.event("start", began))
        now += timedelta(seconds=50)
        finish = phone.event("finish", now, start=began, reliable=False)
        assert phone.send(finish)["reason"] == "clock"
        desktop = timer_action(http, "start", lessonId=phone.lesson)["active"]
        now += timedelta(seconds=30)
        response = http.post(PATH + f"/conflicts/{finish['id']}", json={
            "action": "confirm", "beganAt": stamp(began), "endedAt": stamp(now),
        })
        assert response.is_success
        assert response.json()["seconds"] == 50
        timer_action(http, "finish", timerId=desktop["id"])
        assert report(http)["totals"]["actualSeconds"] == 80
