from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from backend.db import open_db
from test_course_verification import SOURCE, authorize, model_traffic, prepare, publish
from test_model_study import admit_for_interruption, assert_interrupted_use, ok, run, session


GROUP = {
    "hint": "想想印版在试印前如何保持稳定。",
    "questions": [
        {"text": "如何固定排好的活字？", "criterion": "说明固定在版框内", "required": True, "quote": SOURCE},
        {"text": "固定后可以先检查哪项？", "criterion": "字行是否平整", "required": False, "quote": SOURCE},
    ],
}
COMPOSED = {"objectives": [
    {"title": "说明固定活字的方法", "ability": "apply", "criterion": "指出排字后使用版框固定活字", "group": GROUP},
]}
GRADE = {"items": [
    {"index": 0, "met": True, "reason": "答复指出用版框固定。"},
    {"index": 1, "met": False, "reason": "答复没有提到字行的平整度。"},
], "explanation": "已掌握固定活字的关键步骤，可补练试印前的检查。"}


def assess_one(folder, output, prompts, *, on_request=None):
    from backend.assessment_worker import process_one

    def transport(address, headers, payload):
        prompts.append((payload["model"], payload["messages"][0]["content"]))
        if on_request is not None:
            on_request()
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(output)}}]}).encode()

    engine = open_db(folder)
    try:
        return process_one(engine, transport=transport)
    finally:
        engine.dispose()


def source_lesson(http, folder, *, adopt=False):
    calls = []
    project, lesson, item, evidence = prepare(http, folder, calls)
    authorize(http, project, evidence)
    assert run(folder, model_traffic(calls)) and publish(folder)
    if adopt:
        audit = ok(http.get(f"/api/lessons/{lesson}/verification"))
        batch = {"action": "adopt", "items": [{"lessonId": lesson, "index": 1, "findingId": audit["findingId"]}]}
        preview = ok(http.post("/api/disputes/preview", json=batch))
        ok(http.post("/api/disputes/apply", json={**batch, "confirmation": preview["confirmation"]}))
    ok(http.put(f"/api/lessons/{lesson}/learned", json={"learned": True}))
    return lesson, item


def permit(http, lesson, max_calls=None):
    endpoint = f"/api/lessons/{lesson}/assessment"
    scope = ok(http.post(endpoint + "/preview", json={"maxCalls": max_calls}))
    ok(http.post(endpoint + "/authorize", json={"maxCalls": max_calls, "confirmation": scope["confirmation"]}))
    return scope


def review(http, lesson):
    items = ok(http.get("/api/reviews"))["reviews"]
    return next(item for item in items if item["lessonId"] == lesson)


def planned_reviews(http, lesson):
    days = ok(http.get("/api/study-plan"))["days"]
    return [task for day in days for task in day["tasks"] if task["kind"] == "review" and task["lessonId"] == lesson]


def test_verified_model_lesson_schedules_and_grades_only_the_core_ability(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path, adopt=True)
        today = date.fromisoformat(ok(http.get("/api/reviews"))["today"])
        assert ok(http.get("/api/reviews"))["reviews"] == []
        scope = permit(http, lesson, max_calls=6)
        assert [basis["index"] for basis in scope["sections"]] == [0]
        assert assess_one(tmp_path, COMPOSED, prompts)
        item = review(http, lesson)
        assert item["kind"] == "model" and item["ability"] == "apply"
        assert item["dueOn"] == (today + timedelta(days=1)).isoformat()
        planned, = planned_reviews(http, lesson)
        assert (planned["reviewKind"], planned["key"], planned["dueOn"]) == ("model", item["key"], item["dueOn"])
        booking = {"taskId": planned["id"], "placement": {"date": item["dueOn"], "start": "19:00"}}
        token = ok(http.post("/api/study-plan/moves/preview", json=booking))["confirmation"]
        ok(http.post("/api/study-plan/moves/confirm", json={**booking, "confirmation": token}))
        locked, = planned_reviews(http, lesson)
        assert locked["locked"]
        path = f"/api/lessons/{lesson}/assessments/{item['key']}"
        assert SOURCE in prompts[0][1] and "加热" not in prompts[0][1] and "私有附注" not in prompts[0][1]
        ok(http.post("/api/demo/next-day"))
        group = ok(http.get(path))["group"]
        assert group["questions"] == [{"text": question["text"]} for question in GROUP["questions"]]
        target = f"/api/assessments/{group['id']}"
        answers = ["放进版框，锁紧活字。", "纸张尺寸。"]
        ok(http.put(target + "/draft", json={"answers": [answers[0], ""]}))
        assert ok(http.get(target))["draft"] == [answers[0], ""]
        submission = {"action": "submit", "answers": answers}
        assert ok(http.post(target + "/submit", json=submission))["state"] == "grading"
        assert ok(http.post(target + "/submit", json=submission))["state"] == "grading"
        waiting = review(http, lesson)
        assert waiting["dueOn"] == item["dueOn"] and waiting["status"] == "waiting"
        assert not waiting.get("gradingDelayed")
        assert planned_reviews(http, lesson) == []
        plan = ok(http.get("/api/study-plan"))
        assert plan["days"][0]["estimatedCompletedMinutes"] == 5 and not plan["pendingLocks"]
        report_path = f"/api/study-report?period=day&on={item['dueOn']}"
        ungraded = ok(http.get(report_path))
        assert ungraded["totals"]["review"] == {"passed": 0, "hinted": 0, "failed": 0, "ungraded": 1}
        record, = ungraded["reviewRecords"]
        assert (record["lessonId"], record["key"], record["round"], record["on"], record["outcome"], record["groupId"]) == (
            lesson, item["key"], 1, item["dueOn"], "ungraded", group["id"],
        )
        assert record["title"] == item["title"]
        assert assess_one(tmp_path, GRADE, prompts, on_request=lambda: [ok(http.post("/api/demo/next-day")) for _ in range(3)])
        saved = ok(http.get(target))
        assert saved["result"]["outcome"] == "passed" and saved["result"]["gradingDelayed"]
        graded = ok(http.get(report_path))
        assert graded["totals"]["review"] == {"passed": 1, "hinted": 0, "failed": 0, "ungraded": 0}
        assert graded["reviewRecords"] == [{**record, "outcome": "passed"}]
        assert ok(http.get("/api/study-report?period=day"))["reviewRecords"] == []
        assert saved["result"]["dueOn"] == (today + timedelta(days=3)).isoformat()
        current, = [entry for entry in ok(http.get("/api/reviews"))["reviews"] if entry["lessonId"] == lesson]
        assert current["stage"] == 1 and current["status"] == "due"
        assert current["dueOn"] == saved["result"]["dueOn"]
        assert current["gradingDelayed"] is True
        planned, = planned_reviews(http, lesson)
        assert planned["dueOn"] == current["dueOn"]
    with session(tmp_path) as http:
        assert review(http, lesson) == current
        next_round = ok(http.post(path + "/prepare", json={"mode": "formal"}))
        questions = {**GROUP, "questions": [{**GROUP["questions"][0], "text": "举例说明如何锁住排好的版。"}]}
        assert assess_one(tmp_path, questions, prompts)
        assert ok(http.post(f"/api/assessments/{next_round['id']}/submit",
                            json={"action": "unknown", "answers": []}))["state"] == "graded"
        ok(http.post("/api/demo/next-day"))
        following = review(http, lesson)
        assert following["status"] == "due" and following["stage"] == 0
        assert following["dueOn"] == (today + timedelta(days=5)).isoformat()
        assert not following.get("gradingDelayed")


def test_rechecking_withdraws_the_previous_schedule_and_preserves_both_grades(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        permit(http, lesson, max_calls=6)
        assert assess_one(tmp_path, COMPOSED, prompts)
        item = review(http, lesson)
        old_due = item["dueOn"]
        ok(http.post("/api/demo/next-day"))
        group = ok(http.get(f"/api/lessons/{lesson}/assessments/{item['key']}"))["group"]
        target = f"/api/assessments/{group['id']}"
        answers = ["固定在版框。", "检查字行。"]
        ok(http.post(target + "/submit", json={"action": "submit", "answers": answers}))
        assert assess_one(tmp_path, GRADE, prompts)
        assert review(http, lesson)["dueOn"] != old_due
        assert ok(http.post(target + "/recheck"))["state"] == "rechecking"
        assert review(http, lesson)["dueOn"] == old_due and review(http, lesson)["status"] == "waiting"
        failed = {**GRADE, "items": [{**GRADE["items"][0], "met": False}, GRADE["items"][1]]}
        assert assess_one(tmp_path, failed, prompts)
        assert prompts[-1][0] == "independent-model"
        checked = ok(http.get(target))
        assert checked["answers"] == answers
        assert [record["outcome"] for record in checked["history"]] == ["passed", "failed"]
        assert review(http, lesson)["stage"] == 0
        planned, = planned_reviews(http, lesson)
        assert planned["weak"]
        assert ok(http.get(f"/api/lessons/{lesson}"))["project"]["learned"] == 1


def test_early_practice_and_assisted_answers_leave_the_stage_unchanged(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, prompts)
        item = review(http, lesson)
        endpoint = f"/api/lessons/{lesson}/assessments/{item['key']}"
        assert ok(http.get(endpoint))["group"] is None
        group = ok(http.post(endpoint + "/prepare", json={"mode": "practice"}))
        practice = {**GROUP, "questions": [{**GROUP["questions"][0], "text": "举例说明如何锁住排好的版。"}]}
        assert assess_one(tmp_path, practice, prompts)
        assert GROUP["questions"][0]["text"] in prompts[-1][1]
        target = f"/api/assessments/{group['id']}"
        ok(http.post(target + "/submit", json={"action": "submit", "answers": ["用版框固定。"]}))
        assert assess_one(tmp_path, {**GRADE, "items": GRADE["items"][:1]}, prompts)
        assert ok(http.get(target))["result"]["outcome"] == "passed"
        assert review(http, lesson)["dueOn"] == item["dueOn"] and review(http, lesson)["stage"] == 0
        data = ok(http.get("/api/study-report?period=day"))
        assert data["totals"]["review"] == {"passed": 0, "hinted": 0, "failed": 0, "ungraded": 0}
        assert data["reviewRecords"] == []

        ok(http.post("/api/demo/next-day"))
        official = ok(http.get(endpoint))["group"]
        assert official["id"] != group["id"]
        target = f"/api/assessments/{official['id']}"
        assert ok(http.post(target + "/hint"))["hint"] == GROUP["hint"]
        ok(http.post(target + "/submit", json={"action": "submit", "answers": ["用版框。", "字行平整。"]}))
        assert assess_one(tmp_path, GRADE, prompts)
        result = ok(http.get(target))["result"]
        assert result["outcome"] == "hinted" and result["stage"] == 0
        assert result["dueOn"] == (date.fromisoformat(item["dueOn"]) + timedelta(days=1)).isoformat()


def test_missing_evidence_holds_only_the_affected_objective_and_restores_its_original_due_date(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, source = source_lesson(http, tmp_path)
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, prompts)
        before = review(http, lesson)
        ok(http.post("/api/demo/next-day"))
        source_path = Path(tmp_path) / source["path"]
        original = source_path.read_bytes()
        source_path.write_text("变更后的资料。", encoding="utf-8")
        assert planned_reviews(http, lesson) == []
        withheld = review(http, lesson)
        assert withheld["status"] == "held" and withheld["dueOn"] == before["dueOn"]
        assert ok(http.get("/api/state"))["dueCount"] == 0
        endpoint = f"/api/lessons/{lesson}/assessments/{before['key']}"
        assert http.post(endpoint + "/prepare", json={"mode": "formal"}).status_code == 409
        ok(http.post("/api/demo/next-day"))
        source_path.write_bytes(original)
        planned, = planned_reviews(http, lesson)
        assert planned["dueOn"] == before["dueOn"]
        resumed = review(http, lesson)
        assert resumed["status"] == "due"
        assert (resumed["stage"], resumed["dueOn"]) == (before["stage"], before["dueOn"])
        assert ok(http.get("/api/state"))["dueCount"] == 1


@pytest.mark.parametrize("sending", [False, True], ids=["before-send", "after-admission"])
def test_interrupted_grade_retains_answers_and_retries_based_on_send_state(tmp_path, sending):
    from backend.assessment_worker import dispatch_assessment, reserve_assessment, settle_interrupted_assessments
    from backend.model_study import utc_now

    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, prompts)
        item = review(http, lesson)
        ok(http.post("/api/demo/next-day"))
        group = ok(http.get(f"/api/lessons/{lesson}/assessments/{item['key']}"))["group"]
        target = f"/api/assessments/{group['id']}"
        answers = ["用版框固定。", "检查字行。"]
        ok(http.post(target + "/submit", json={"action": "submit", "answers": answers}))
        engine = open_db(tmp_path)
        try:
            claim = reserve_assessment(engine, utc_now())
            assert claim is not None and claim.phase == "grade"
            if sending:
                admit_for_interruption(engine, claim.id, claim.prompt, utc_now())
            settle_interrupted_assessments(engine, utc_now())
            assert_interrupted_use(engine, claim.id, sending)
            saved = ok(http.get(target))
            assert saved["answers"] == answers and saved["result"] is None
            assert saved["jobState"] == ("blocked" if sending else "queued")
            assert review(http, lesson)["dueOn"] == item["dueOn"]

            def unexpected_request(*_):
                raise AssertionError("过期的请求不能再发送。")

            dispatch_assessment(engine, claim, transport=unexpected_request)
            if sending:
                assert reserve_assessment(engine, utc_now()) is None
                ok(http.post(target + "/retry"))
            assert assess_one(tmp_path, GRADE, prompts)
            assert len(prompts) == 2 and ok(http.get(target))["result"]["outcome"] == "passed"
            assert not assess_one(tmp_path, GRADE, prompts)
        finally:
            engine.dispose()


def test_revoked_review_authority_stops_a_reserved_request_before_sending(tmp_path):
    from backend.assessment_worker import dispatch_assessment, reserve_assessment
    from backend.model_study import utc_now

    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        permit(http, lesson)
        engine = open_db(tmp_path)
        try:
            claim = reserve_assessment(engine, utc_now())
            assert claim is not None and claim.phase == "objectives"
            status = ok(http.post(f"/api/lessons/{lesson}/assessment/revoke"))
            assert not status["authorized"]

            def unexpected_request(*_):
                raise AssertionError("撤销授权后不能发送。")

            dispatch_assessment(engine, claim, transport=unexpected_request)
            assert ok(http.get("/api/reviews"))["reviews"] == []
            status = ok(http.get(f"/api/lessons/{lesson}/assessment"))
            assert status["calls"] == 0 and status["jobs"][0]["state"] == "blocked"
            permit(http, lesson)
            assert assess_one(tmp_path, COMPOSED, prompts)
            assert review(http, lesson)["stage"] == 0
        finally:
            engine.dispose()
