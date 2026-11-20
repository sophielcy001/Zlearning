from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import ModelStudy, open_db, transaction
from backend.project_activity import study_moment
from test_model_study import SESSION, consent_request, ok, run, setup_model, transport_recorder


RECOVERY = "/api/generation/daily-budget"
SETTINGS = "/api/generation/settings"
BEFORE = datetime(2027, 2, 1, 15, 59)
AFTER = BEFORE + timedelta(minutes=2)


def budget_client(folder, now):
    return TestClient(create_app(folder, SESSION, today=lambda: study_moment(now()).date(), now=now), base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {SESSION}"})


def create_generation(http, max_calls=None):
    endpoint, data, preview = consent_request(http, max_calls=max_calls)
    return ok(http.post(endpoint, json={**data, "modelConsent": preview["consent"]}))["projectId"]


def pulse(http):
    return ok(http.post("/api/reminders/check", json={"trigger": "foreground"}))["reminders"]


def test_daily_budget_defaults_to_one_recovery_notice_and_explicit_resume(tmp_path):
    now = BEFORE
    calls = []
    with budget_client(tmp_path, lambda: now) as http:
        setup_model(http)
        assert ok(http.put(SETTINGS, json={"concurrency": 3, "dailyLimit": 1}))["autoContinueDailyBudget"] is False
        identity = create_generation(http)
        target = f"/api/projects/{identity}/generation"
        assert run(tmp_path, transport_recorder(calls), now=lambda: now)
        assert not run(tmp_path, transport_recorder(calls), now=lambda: now)
        assert ok(http.get(target))["reason"] == "daily_budget"
        assert ok(http.get(RECOVERY))["tasks"] == []
        assert pulse(http) == []
        ok(http.post("/api/demo/next-day"))
        assert pulse(http) == []
        now = AFTER
        plan = ok(http.get(RECOVERY))
        task, = plan["tasks"]
        assert (plan["billingDay"], task["id"], task["kind"], task["blockedOn"]) == (
            "2027-02-02", identity, "generation", "2027-02-01",
        )
        notice, = pulse(http)
        assert (notice["kind"], notice["count"]) == ("budget", 1)
        ok(http.post("/api/demo/next-day"))
        assert pulse(http) == []
        assert not run(tmp_path, transport_recorder(calls), now=lambda: now)
        assert len(calls) == 1

    with budget_client(tmp_path, lambda: now) as http:
        assert pulse(http) == []
        data = {"billingDay": "2027-02-02", "tasks": [{"id": identity, "kind": "generation"}]}
        assert http.post(RECOVERY, json={**data, "billingDay": "2027-02-01"}).status_code == 409
        assert len(ok(http.post(RECOVERY, json=data))["resumed"]) == 1
        assert ok(http.post(RECOVERY, json=data))["resumed"] == []
        assert run(tmp_path, transport_recorder(calls), now=lambda: now)
        assert len(calls) == 2 and ok(http.get(target))["usage"]["calls"] == 2
        assert not run(tmp_path, transport_recorder(calls), now=lambda: now)


def test_opt_in_auto_resumes_only_cross_day_budget_and_preserves_newer_settings(tmp_path):
    now = BEFORE
    calls = []
    with budget_client(tmp_path, lambda: now) as http:
        setup_model(http)
        assert ok(http.put(SETTINGS, json={"concurrency": 3, "dailyLimit": 1, "autoContinueDailyBudget": True}))["autoContinueDailyBudget"]
        assert ok(http.put(SETTINGS, json={"concurrency": 4, "dailyLimit": 1}))["autoContinueDailyBudget"]
        allowed = create_generation(http)
        assert run(tmp_path, transport_recorder(calls), now=lambda: now)
        blocked = create_generation(http)
        paused = create_generation(http)
        stopped = create_generation(http)
        deleted = create_generation(http)
        assert not run(tmp_path, transport_recorder(calls), now=lambda: now)
        ok(http.post(f"/api/projects/{stopped}/generation/control", json={"action": "stop"}))
        ok(http.put(f"/api/projects/{paused}/pause", json={"paused": True}))
        deletion = f"/api/projects/{deleted}/generation"
        confirmation = ok(http.post(deletion + "/delete-preview"))["confirmation"]
        assert ok(http.post(deletion + "/delete", json={"confirmation": confirmation}))["state"] == "deleted"
        engine = open_db(tmp_path)
        try:
            with transaction(engine) as db:
                db.get(ModelStudy, blocked).max_calls = 1
                db.get(ModelStudy, blocked).connection_revision += 1
        finally:
            engine.dispose()
        assert len(calls) == 1 and pulse(http) == []
        now = AFTER
        assert pulse(http) == []
        assert run(tmp_path, transport_recorder(calls), now=lambda: now)
        assert len(calls) == 2
        assert ok(http.get(f"/api/projects/{allowed}/generation"))["usage"]["calls"] == 2
        assert not run(tmp_path, transport_recorder(calls), now=lambda: now)
        for identity in (stopped, paused, blocked, deleted):
            assert ok(http.get(f"/api/projects/{identity}/generation"))["usage"]["calls"] == 0


def test_reserved_call_rechecks_next_day_limit_before_transport(tmp_path):
    from backend.model_study import dispatch, reserve_call

    calls = []
    with budget_client(tmp_path, lambda: BEFORE) as http:
        setup_model(http)
        ok(http.put(SETTINGS, json={"concurrency": 4, "dailyLimit": 1}))
        old = create_generation(http)
        engine = open_db(tmp_path)
        try:
            reserved = reserve_call(engine, BEFORE)
            assert reserved is not None
            later = create_generation(http)
            fresh = reserve_call(engine, AFTER)
            assert fresh is not None
            dispatch(engine, fresh, transport=transport_recorder(calls), now=lambda: AFTER)
            dispatch(engine, reserved, transport=transport_recorder(calls), now=lambda: AFTER)
            assert len(calls) == 1
            assert ok(http.get(f"/api/projects/{later}/generation"))["usage"]["calls"] == 1
            stale = ok(http.get(f"/api/projects/{old}/generation"))
            assert stale["reason"] == "daily_budget" and stale["usage"]["calls"] == 0
        finally:
            engine.dispose()


def test_cross_day_never_resets_a_per_task_limit(tmp_path):
    now = BEFORE
    calls = []
    with budget_client(tmp_path, lambda: now) as http:
        setup_model(http)
        ok(http.put(SETTINGS, json={"concurrency": 2, "dailyLimit": 1, "autoContinueDailyBudget": True}))
        identity = create_generation(http, max_calls=1)
        target = f"/api/projects/{identity}/generation"
        assert run(tmp_path, transport_recorder(calls), now=lambda: now)
        assert not run(tmp_path, transport_recorder(calls), now=lambda: now)
        assert ok(http.get(target))["reason"] == "task_budget"
        now = AFTER
        assert not run(tmp_path, transport_recorder(calls), now=lambda: now)
        assert ok(http.get(RECOVERY))["tasks"] == []
        assert pulse(http) == []
        assert ok(http.post(RECOVERY, json={"billingDay": "2027-02-02", "tasks": [
            {"kind": "generation", "id": identity},
        ]}))["resumed"] == []
        assert ok(http.get(target))["maxCalls"] == 1 and len(calls) == 1


@pytest.mark.parametrize("automatic", [False, True])
def test_four_model_pipelines_can_continue_the_same_saved_tasks_after_midnight(tmp_path, automatic):
    from backend.assessment_worker import dispatch_assessment, reserve_assessment
    from backend.lesson_answer_worker import dispatch_answer, reserve_answer
    from backend.prerequisite_worker import dispatch as dispatch_prerequisite, publish_one, reserve as reserve_prerequisite
    from backend.study_consolidation_worker import dispatch as dispatch_consolidation, reserve as reserve_consolidation
    from test_lesson_assistant import PASS_REPLY, assistant_transport, open_thread, permit_assistant
    from test_model_assessments import COMPOSED, permit
    from test_prerequisites import DRAFT, VERIFIED, grant, input_source
    from test_study_consolidation import SUPPLEMENT, completed_course, permit_consolidation, traffic

    now = BEFORE
    with budget_client(tmp_path, lambda: now) as http:
        lesson, _ = completed_course(http, tmp_path)
        permit_assistant(http, lesson)
        thread = open_thread(http, lesson)
        def send_question(text):
            identity = str(uuid4())
            ok(http.post(f"/api/conversations/{thread}/questions", json={"id": identity, "text": text}))
            return identity

        engine = open_db(tmp_path)
        try:
            send_question("如何固定活字？")
            answered = reserve_answer(engine, now)
            assert answered
            dispatch_answer(engine, answered, transport=assistant_transport(PASS_REPLY, []), now=lambda: now)
            second = send_question("固定之后要检查什么？")
            permit(http, lesson)
            ok(http.put(f"/api/lessons/{lesson}/learned", json={"learned": True}))
            permit_consolidation(http, lesson)
            prerequisite = f"/api/lessons/{lesson}/prerequisites"
            request = {"topic": "预备知识临时补充", "sources": [input_source(http)], "maxCalls": 3}
            short_lesson = grant(http, prerequisite, request)["job"]["id"]
            first = reserve_prerequisite(engine, now)
            assert first and first.task_id == short_lesson
            dispatch_prerequisite(engine, first, transport=traffic(DRAFT, []), now=lambda: now)

            ok(http.put(SETTINGS, json={"concurrency": 4, "dailyLimit": 2, "autoContinueDailyBudget": automatic}))
            reserves = (reserve_answer, reserve_assessment, reserve_consolidation, reserve_prerequisite)
            assert all(reserve(engine, now) is None for reserve in reserves)
            assert ok(http.get(RECOVERY))["tasks"] == []
            stalled = ok(http.get(prerequisite, params={"topic": request["topic"]}))["job"]
            assert stalled["draft"] == DRAFT["sections"] and stalled["reason"] == "daily_budget"
            now = AFTER
            ok(http.put(SETTINGS, json={"concurrency": 4, "dailyLimit": 4}))
            recoverable = ok(http.get(RECOVERY))["tasks"]
            assert {item["kind"] for item in recoverable} == {"assessment", "answer", "consolidation", "prerequisite"}
            assert {item["blockedOn"] for item in recoverable} == {"2027-02-01"}
            titles = {item["kind"]: item["title"] for item in recoverable}
            assert titles["answer"] == "固定之后要检查什么？" and titles["prerequisite"] == request["topic"]
            reminders = [item for item in pulse(http) if item["kind"] == "budget"]
            if automatic:
                assert not reminders
            else:
                assert len(reminders) == 1 and reminders[0]["count"] == 4
                assert all(reserve(engine, now) is None for reserve in reserves)
                requested = [{"kind": item["kind"], "id": item["id"]} for item in recoverable]
                assert len(ok(http.post(RECOVERY, json={"billingDay": "2027-02-02", "tasks": requested}))["resumed"]) == 4

            answer = reserve_answer(engine, now)
            assert answer and answer.question_id == second
            dispatch_answer(engine, answer, transport=assistant_transport(PASS_REPLY, []), now=lambda: now)
            assessment = reserve_assessment(engine, now)
            assert assessment and assessment.phase == "objectives"
            dispatch_assessment(engine, assessment, transport=traffic(COMPOSED, []), now=lambda: now)
            consolidation = reserve_consolidation(engine, now)
            assert consolidation and consolidation.phase == "draft"
            dispatch_consolidation(engine, consolidation, transport=traffic(SUPPLEMENT, []), now=lambda: now)
            follow_up = reserve_prerequisite(engine, now)
            assert follow_up and follow_up.task_id == short_lesson and follow_up.phase == "review"
            dispatch_prerequisite(engine, follow_up, transport=traffic(VERIFIED, []), now=lambda: now)
            assert publish_one(engine, tmp_path, now=lambda: now)
            assert ok(http.get(prerequisite, params={"topic": request["topic"]}))["kind"] == "explanation"
            assert ok(http.get(RECOVERY))["used"] == 4
        finally:
            engine.dispose()
