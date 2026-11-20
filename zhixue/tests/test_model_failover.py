from datetime import UTC, datetime
import json
from uuid import uuid4

from sqlalchemy import select

from backend.db import ModelUse, StageReviewCall, open_db, transaction
from backend.stage_review_worker import process_one as process_stage_review
from test_course_verification import DRAFT, FINDINGS, PLAN, SOURCE, publish
from test_model_study import MAIN_INPUT, ok, run, session, setup_model
from test_stage_model_review import SELECTION, VALID, approved, evidence_attempt, reply_with, setup_models


CLOCK = datetime(2027, 2, 5, 12, tzinfo=UTC)


def connection(http, model):
    return ok(http.post("/api/models/connections", json={
        "name": model, "provider": "custom", "protocol": "chat-completions",
        "endpoint": "http://127.0.0.1:9109/v1", "model": model,
    }))


def roles(http, *, review_model="course-reviewer"):
    main = setup_model(http)
    standby = connection(http, "main-standby")
    reviewer = connection(http, review_model) if review_model != standby["model"] else standby
    review_standby = connection(http, "review-standby")
    ok(http.patch("/api/models/roles", json={"review": reviewer["id"], "mainBackup": standby["id"],
                                             "reviewBackup": review_standby["id"]}))
    return main, standby, reviewer, review_standby


def goal_request(http, allow=False, limit=8):
    thread = ok(http.post("/api/conversations", json={
        "id": str(uuid4()), "message": {"id": str(uuid4()), "text": f"我想{MAIN_INPUT}"},
    }))
    path = f"/api/conversations/{thread['id']}/goal"
    draft = ok(http.post(path))
    inputs = {"goalId": draft["id"], "revision": draft["revision"], "values": {"outcome": "说明活字试印"}}
    view = ok(http.post(path + "/model-preview", json={**inputs, "maxCalls": limit, "allowBackup": allow}))
    return path, inputs, view


def submit(http, path, inputs, preview):
    project = ok(http.post(path + "/submit", json={**inputs, "modelConsent": preview["consent"]}))["projectId"]
    return f"/api/projects/{project}"


def source_request(http, allow=False):
    source_id = ok(http.post("/api/files", json={"name": "印刷记录.txt", "content": f"不发送的前言\n{SOURCE}\n私人行"}))["id"]
    view = ok(http.get(f"/api/files/{source_id}?at=2"))
    return {"sources": [{"fileId": source_id, "revision": view["revision"], "at": 2}], "allowBackup": allow}


def authorize_review(http, project, request):
    path = project + "/course-review"
    view = ok(http.post(path + "/preview", json=request))
    ok(http.post(path + "/authorize", json={**request, "confirmation": view["confirmation"]}))
    return view


def receiver(log, fail):
    def transport(address, headers, body):
        model = body["model"]
        prompt = body["messages"][0]["content"]
        log.append((model, prompt))
        status = fail.get(model, 200)
        if status != 200:
            return status, b"{}"
        output = PLAN if "制定学习路线" in prompt else FINDINGS if "独立复核" in prompt else DRAFT
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(output)}}]}).encode()
    return transport


def test_main_and_review_backups_resume_their_roles_and_keep_grounded_course(tmp_path):
    log = []
    send = receiver(log, {"isolated-model": 503, "course-reviewer": 401})
    with session(tmp_path) as http:
        _, standby, _, review_standby = roles(http)
        path, inputs, preview = goal_request(http, allow=True)
        assert preview["backup"]["id"] == standby["id"] and not log
        project = submit(http, path, inputs, preview)
        assert run(tmp_path, send, now=lambda: CLOCK)
        assert ok(http.get(project + "/generation"))["state"] == "queued"
    with session(tmp_path) as http:
        assert run(tmp_path, send, now=lambda: CLOCK)
        lesson = ok(http.get(project))["lessons"][0]["id"]
        assert run(tmp_path, send, now=lambda: CLOCK)
        grant = authorize_review(http, project, source_request(http, allow=True))
        assert grant["backup"]["id"] == review_standby["id"]
        assert run(tmp_path, send, now=lambda: CLOCK)
        assert run(tmp_path, send, now=lambda: CLOCK)
        assert publish(tmp_path)
        assert [model for model, _ in log] == [
            "isolated-model", "main-standby", "main-standby", "course-reviewer", "review-standby",
        ]
        assert log[0][1] == log[1][1] and log[3][1] == log[4][1]
        assert SOURCE in log[4][1] and "私人行" not in log[4][1]
        assert ok(http.get(f"/api/lessons/{lesson}/verification"))["reviewer"]["id"] == review_standby["id"]
        assert ok(http.get(project + "/generation"))["usage"]["calls"] == 5
        with transaction(open_db(tmp_path), write=False) as db:
            calls = db.scalars(select(ModelUse).where(ModelUse.failover_reason.is_not(None))).all()
            assert {(call.phase, call.connection["model"], call.failover_reason) for call in calls} == {
                ("route", "main-standby", "service_unavailable"), ("draft", "main-standby", "service_unavailable"),
                ("review", "review-standby", "unauthorized"),
            }
            assert len([call for call in db.scalars(select(ModelUse)).all() if call.auto_retry]) == 2


def test_configured_backup_has_no_permission_without_opt_in_and_pin_expires_on_change(tmp_path):
    log = []
    send = receiver(log, {"isolated-model": 401})
    with session(tmp_path) as http:
        _, original, _, _ = roles(http)
        path, inputs, unsigned = goal_request(http)
        assert unsigned.get("backup") is None
        unauthorized = submit(http, path, inputs, unsigned)
        assert run(tmp_path, send, now=lambda: CLOCK)
        assert not run(tmp_path, send, now=lambda: CLOCK)
        assert ok(http.get(unauthorized + "/generation"))["reason"] == "unauthorized"
        path, inputs, signed = goal_request(http, allow=True)
        replacement = connection(http, "new-main-backup")
        ok(http.patch("/api/models/roles", json={"mainBackup": replacement["id"]}))
        assert http.post(path + "/submit", json={**inputs, "modelConsent": signed["consent"]}).status_code == 409
        ok(http.patch("/api/models/roles", json={"mainBackup": original["id"]}))
        target = submit(http, path, inputs, signed)
        ok(http.patch("/api/models/roles", json={"mainBackup": replacement["id"]}))
        assert run(tmp_path, send, now=lambda: CLOCK)
        assert not run(tmp_path, send, now=lambda: CLOCK)
        assert ok(http.get(target + "/generation"))["reason"] == "unauthorized"
        assert [item[0] for item in log] == ["isolated-model", "isolated-model"]


def test_failover_reserves_shared_task_limit_and_review_never_uses_actual_draft_author(tmp_path):
    log = []
    send = receiver(log, {"isolated-model": 503})
    with session(tmp_path) as http:
        _, main_standby, _, review_standby = roles(http, review_model="main-standby")
        endpoint, inputs, preview = goal_request(http, allow=True, limit=1)
        project = submit(http, endpoint, inputs, preview)
        assert run(tmp_path, send, now=lambda: CLOCK)
        assert not run(tmp_path, send, now=lambda: CLOCK)
        assert ok(http.get(project + "/generation"))["reason"] == "task_budget"
        assert len(log) == 1
        ok(http.post(project + "/generation/control", json={"action": "resume", "maxCalls": 4}))
        assert run(tmp_path, send, now=lambda: CLOCK)
        assert run(tmp_path, send, now=lambda: CLOCK)
        lesson = ok(http.get(project))["lessons"][0]["id"]
        view = authorize_review(http, project, source_request(http, allow=True))
        assert view["reviewer"]["id"] == main_standby["id"]
        assert run(tmp_path, send, now=lambda: CLOCK)
        assert publish(tmp_path)
        assert ok(http.get(f"/api/lessons/{lesson}/verification"))["reviewer"]["id"] == review_standby["id"]
        assert [model for model, _ in log] == ["isolated-model", "main-standby", "main-standby", "review-standby"]


def test_stage_review_fallback_uses_exact_evidence_authorized_for_both_recipients(tmp_path):
    with session(tmp_path) as http:
        _, primary = setup_models(http)
        alternate = connection(http, "stage-standby")
        ok(http.patch("/api/models/roles", json={"reviewBackup": alternate["id"]}))
        stage, endpoint = evidence_attempt(http, tmp_path)
        view, grant = approved(http, endpoint, {**SELECTION, "allowBackup": True})
        assert view["scope"]["backup"]["id"] == alternate["id"]
        ok(http.post(endpoint, json=grant))
        seen = []

        def transport(address, headers, request):
            return reply_with(VALID, seen, status=403 if request["model"] == primary["model"] else 200)(address, headers, request)

        engine = open_db(tmp_path)
        try:
            assert process_stage_review(engine, transport=transport, now=lambda: CLOCK)
            assert process_stage_review(engine, transport=transport, now=lambda: CLOCK)
            assert not process_stage_review(engine, transport=transport, now=lambda: CLOCK)
            detail = ok(http.get(stage))
            assert detail["status"] == "passed" and detail["attempts"][0]["model"]["id"] == alternate["id"]
            assert [req[1]["model"] for req in seen] == [primary["model"], alternate["model"]]
            assert seen[0][1]["messages"][0]["content"] == seen[1][1]["messages"][0]["content"]
            with transaction(engine, write=False) as db:
                call = db.scalar(select(StageReviewCall).where(StageReviewCall.failover_reason.is_not(None)))
                assert call.connection["id"] == alternate["id"] and call.failover_reason == "unauthorized"
        finally:
            engine.dispose()


def test_a_reserved_primary_call_follows_a_concurrent_authorized_switch_without_sending(tmp_path):
    from backend.model_study import dispatch, reserve_call
    from test_model_study import PLAN as LONG_ROUTE

    log = []
    with session(tmp_path) as http:
        roles(http)
        path, inputs, preview = goal_request(http, allow=True)
        project = submit(http, path, inputs, preview)

        def route_transport(address, headers, body):
            return reply_with(LONG_ROUTE, log)(address, headers, body)

        assert run(tmp_path, route_transport, now=lambda: CLOCK)
        log.clear()
        engine = open_db(tmp_path)
        try:
            first = reserve_call(engine, CLOCK)
            second = reserve_call(engine, CLOCK)
            assert first and second and first.step_id != second.step_id
            primary_fails = receiver(log, {"isolated-model": 503})
            dispatch(engine, first, transport=primary_fails, now=lambda: CLOCK)
            dispatch(engine, second, transport=primary_fails, now=lambda: CLOCK)
            assert [call[0] for call in log] == ["isolated-model"]
            with transaction(engine, write=False) as db:
                assert db.get(ModelUse, second.identity).status == "not_sent"
            assert run(tmp_path, primary_fails, now=lambda: CLOCK)
            assert run(tmp_path, primary_fails, now=lambda: CLOCK)
            assert [call[0] for call in log] == ["isolated-model", "main-standby", "main-standby"]
            assert ok(http.get(project + "/generation"))["usage"]["calls"] == 4
        finally:
            engine.dispose()


def test_daily_limit_parks_a_switch_and_the_next_day_worker_uses_the_saved_backup(tmp_path):
    from datetime import timedelta

    log = []
    now = CLOCK
    send = receiver(log, {"isolated-model": 503})
    with session(tmp_path) as http:
        roles(http)
        ok(http.put("/api/generation/settings", json={"concurrency": 2, "dailyLimit": 1, "autoContinueDailyBudget": True}))
        path, inputs, preview = goal_request(http, allow=True)
        project = submit(http, path, inputs, preview)
        assert run(tmp_path, send, now=lambda: now)
        assert not run(tmp_path, send, now=lambda: now)
        assert ok(http.get(project + "/generation"))["reason"] == "daily_budget"
        now += timedelta(days=1)
        assert run(tmp_path, send, now=lambda: now)
        assert [item[0] for item in log] == ["isolated-model", "main-standby"]
        assert len(ok(http.get(project))["lessons"]) == 1
