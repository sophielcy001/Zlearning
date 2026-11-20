from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from backend.app import create_app
from backend.db import StageReviewCall, open_db, transaction
from backend.model_usage import billed_calls, billing_day
from backend.stage_review_worker import dispatch, process_one, reserve, settle_interrupted
from test_daily_budget import create_generation
from test_demo import BASE_URL, GOAL, HEADERS, first_lesson
from test_model_study import ok, run, setup_model, transport_recorder


BEFORE = datetime(2027, 2, 3, 15, 58, tzinfo=UTC)
AFTER = datetime(2027, 2, 3, 16, 1, tzinfo=UTC)
PUBLIC = "CREATE TABLE learning_log (id INTEGER PRIMARY KEY, topic TEXT);"
PRIVATE = "仅保存在本机的第二行"
SELECTION = {"excerpts": [{"evidenceIndex": 0, "firstLine": 1, "lastLine": 1}], "maxCalls": 3}
VALID = {"findings": [{"criterion": 1, "verdict": "met", "note": "声明了独立记录编号作为主键。",
                       "citations": [{"excerpt": 1, "quote": "id INTEGER PRIMARY KEY"}]}]}


def open_client(folder, now):
    app = create_app(folder, "demo-test-session", now=now)
    return TestClient(app, base_url=BASE_URL, headers=HEADERS)


def review_model(http, model="phase-review"):
    return ok(http.post("/api/models/connections", json={
        "name": "阶段成果复核", "provider": "custom", "protocol": "chat-completions",
        "endpoint": "http://127.0.0.1:9108/v1", "model": model,
    }))


def setup_models(http):
    main = setup_model(http)
    reviewer = review_model(http)
    ok(http.patch("/api/models/roles", json={"review": reviewer["id"]}))
    return main, reviewer


def evidence_attempt(http, folder):
    lesson = first_lesson(http, folder)
    saved = ok(http.put(f"{lesson}/learned", json={"learned": True}))
    assert saved["learnedOn"]
    path = f"/api/projects/{GOAL['id']}/stage-assessments"
    stage, = ok(http.get(path))["items"]
    stage = f"{path}/{stage['id']}"
    payload = {
        "id": str(uuid4()), "kind": "evidence", "expectedAttempt": 0,
        "evidence": [{"name": "results.sql", "kind": "code", "text": f"{PUBLIC}\n{PRIVATE}"},
                     {"name": "private-name.md", "kind": "report", "text": "与本次评审无关的另一个文件"}],
    }
    saved = ok(http.post(f"{stage}/attempts", json=payload))
    attempt = saved["attempts"][0]
    return stage, f"{stage}/attempts/{attempt['id']}/model-review"


def approved(http, url, data=SELECTION):
    preview = ok(http.post(f"{url}/preview", json=data))
    return preview, {**data, "approvalId": preview["approvalId"], "confirmation": preview["confirmation"]}


def reply_with(value, seen, *, status=200, on_send=None):
    def transport(address, headers, request):
        seen.append((address, request))
        if on_send:
            on_send()
        body = {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 180, "completion_tokens": 63}}
        return status, json.dumps(body).encode()
    return transport


def test_review_sends_only_previewed_lines_to_configured_independent_model(tmp_path):
    clock = lambda: BEFORE
    with open_client(tmp_path, clock) as http:
        main, review = setup_models(http)
        stage, url = evidence_attempt(http, tmp_path)
        engine = open_db(tmp_path)
        try:
            ok(http.patch("/api/models/roles", json={"review": main["id"]}))
            assert http.post(f"{url}/preview", json=SELECTION).status_code == 409
            ok(http.patch("/api/models/roles", json={"review": review["id"]}))
            preview, auth = approved(http, url)
            assert preview["scope"]["connection"]["id"] == review["id"]
            assert preview["scope"]["excerpts"][0]["text"] == PUBLIC
            assert PRIVATE not in json.dumps(preview, ensure_ascii=False)
            assert not process_one(engine, now=clock, transport=reply_with(VALID, []))
            changed = {**auth, "excerpts": [{"evidenceIndex": 0, "firstLine": 1, "lastLine": 2}]}
            assert http.post(url, json=changed).status_code == 409
            swapped = review_model(http, "different-review")
            ok(http.patch("/api/models/roles", json={"review": swapped["id"]}))
            assert http.post(url, json=auth).status_code == 409
            ok(http.patch("/api/models/roles", json={"review": review["id"]}))
            assert ok(http.post(url, json=auth))["attempts"][0]["modelReview"]["state"] == "queued"
            assert ok(http.post(url, json=auth))["attempts"][0]["modelReview"]["state"] == "queued"
            seen = []
            assert process_one(engine, transport=reply_with(VALID, seen), now=clock)
            assert not process_one(engine, transport=reply_with(VALID, seen), now=clock)
            assert len(seen) == 1
            address, request = seen[0]
            assert address == preview["scope"]["connection"]["address"] and request["model"] == review["model"]
            outgoing = request["messages"][0]["content"]
            assert "不得声称执行" in outgoing and PRIVATE not in outgoing
            assert "private-name.md" not in outgoing and "与本次评审无关" not in outgoing
            assert PUBLIC in outgoing
            final = ok(http.get(stage))
            assert final["status"] == "passed"
            attempt = final["attempts"][0]
            assert attempt["scope"] == "static" and attempt["reviewer"] == "model"
            assert attempt["executionObserved"] is False
            assert attempt["findings"][0]["citations"][0] == {
                "evidenceIndex": 0, "firstLine": 1, "lastLine": 1, "quote": "id INTEGER PRIMARY KEY",
            }
            with transaction(engine, write=False) as db:
                call, = db.scalars(select(StageReviewCall)).all()
                assert call.input_tokens == 180 and call.output_tokens == 63
                assert billed_calls(db, billing_day(BEFORE)) == 1
        finally:
            engine.dispose()


@pytest.mark.parametrize("bad_finding", [
    {"criterion": 1, "verdict": "met", "note": "声称引用了只留在本机的文字。",
     "citations": [{"excerpt": 1, "quote": PRIVATE}]},
    {"criterion": 1, "verdict": "met", "note": "没有出处。", "citations": []},
    {"criterion": 2, "verdict": "met", "note": "并不存在的学习要求。",
     "citations": [{"excerpt": 1, "quote": "id INTEGER PRIMARY KEY"}]},
])
def test_invalid_review_keeps_evidence_pending_and_can_be_reauthorized(tmp_path, bad_finding):
    clock = lambda: BEFORE
    with open_client(tmp_path, clock) as http:
        setup_models(http)
        stage, url = evidence_attempt(http, tmp_path)
        _, auth = approved(http, url)
        ok(http.post(url, json=auth))
        engine = open_db(tmp_path)
        try:
            assert process_one(engine, transport=reply_with({"findings": [bad_finding]}, []), now=clock)
            assert not process_one(engine, transport=reply_with(VALID, []), now=clock)
            result = ok(http.get(stage))
            assert result["status"] == "awaiting_review"
            attempt = result["attempts"][0]
            assert attempt["findings"] == [] and attempt["modelReview"]["reason"] == "invalid_response"
            _, auth = approved(http, url)
            ok(http.post(url, json=auth))
            assert process_one(engine, transport=reply_with(VALID, []), now=clock)
            assert ok(http.get(stage))["status"] == "passed"
        finally:
            engine.dispose()


def test_reservation_rechecks_recipient_and_actual_billing_day_then_resumes_in_shared_budget(tmp_path):
    now = BEFORE
    clock = lambda: now
    with open_client(tmp_path, clock) as http:
        _, reviewer = setup_models(http)
        stage, url = evidence_attempt(http, tmp_path)
        _, auth = approved(http, url)
        ok(http.post(url, json=auth))
        engine = open_db(tmp_path)
        try:
            claim = reserve(engine, now)
            assert claim
            other = review_model(http, "role-changed-while-reserved")
            ok(http.patch("/api/models/roles", json={"review": other["id"]}))
            seen = []
            dispatch(engine, claim, transport=reply_with(VALID, seen), now=clock)
            assert not seen
            assert ok(http.get(stage))["attempts"][0]["modelReview"]["reason"] == "configuration_changed"
            ok(http.patch("/api/models/roles", json={"review": reviewer["id"]}))
            _, auth = approved(http, url)
            ok(http.post(url, json=auth))
            claim = reserve(engine, now)
            assert claim
            now = AFTER
            ok(http.put("/api/generation/settings", json={"dailyLimit": 1, "concurrency": 2}))
            create_generation(http)
            assert run(tmp_path, transport_recorder([]), now=clock)
            dispatch(engine, claim, transport=reply_with(VALID, seen), now=clock)
            assert not seen
            assert ok(http.get(stage))["attempts"][0]["modelReview"]["reason"] == "daily_budget"
            now = AFTER + timedelta(days=1)
            ok(http.put("/api/generation/settings", json={"dailyLimit": 1, "concurrency": 2}))
            budget = ok(http.get("/api/generation/daily-budget"))
            ready, = budget["tasks"]
            assert ready["kind"] == "stage_review" and ready["stageId"] == stage.rsplit("/", 1)[-1]
            assert not process_one(engine, transport=reply_with(VALID, seen), now=clock)
            assert ok(http.post("/api/generation/daily-budget", json={
                "billingDay": budget["billingDay"], "tasks": [{"id": ready["id"], "kind": ready["kind"]}],
            }))["resumed"] == [{"id": ready["id"], "kind": "stage_review"}]
            assert process_one(engine, transport=reply_with(VALID, seen), now=clock)
            assert ok(http.get(stage))["status"] == "passed"
        finally:
            engine.dispose()


def test_restart_requeues_only_unsent_reservation_and_holds_late_result_after_stop(tmp_path):
    clock = lambda: BEFORE
    with open_client(tmp_path, clock) as http:
        setup_models(http)
        stage, url = evidence_attempt(http, tmp_path)
        _, auth = approved(http, url)
        ok(http.post(url, json=auth))
        engine = open_db(tmp_path)
        try:
            assert reserve(engine, BEFORE)
            settle_interrupted(engine, BEFORE)
            result = ok(http.get(stage))
            assert result["attempts"][0]["modelReview"]["state"] == "queued"
            seen = []
            assert process_one(engine, transport=reply_with(VALID, seen, on_send=lambda: ok(http.post(f"{url}/stop"))), now=clock)
            assert len(seen) == 1
            result = ok(http.get(stage))
            assert result["status"] == "awaiting_review" and result["attempts"][0]["modelReview"]["state"] == "held"
            applied = ok(http.post(f"{url}/apply"))
            assert applied["status"] == "passed" and len(seen) == 1
            with transaction(engine, write=False) as db:
                assert billed_calls(db, billing_day(BEFORE)) == 1
        finally:
            engine.dispose()


def test_automatic_retries_remain_bounded_across_reattestation_and_task_limit(tmp_path):
    now = BEFORE
    clock = lambda: now
    with open_client(tmp_path, clock) as http:
        setup_models(http)
        stage, url = evidence_attempt(http, tmp_path)
        _, auth = approved(http, url, {**SELECTION, "maxCalls": 2})
        ok(http.post(url, json=auth))
        engine = open_db(tmp_path)
        try:
            seen = []
            unavailable = reply_with(VALID, seen, status=503)
            assert process_one(engine, now=clock, transport=unavailable)
            assert not process_one(engine, now=clock, transport=unavailable)
            now += timedelta(seconds=8)
            assert process_one(engine, now=clock, transport=unavailable)
            assert ok(http.get(stage))["attempts"][0]["modelReview"]["reason"] == "task_budget"
            _, auth = approved(http, url, {**SELECTION, "maxCalls": 6})
            ok(http.post(url, json=auth))
            assert process_one(engine, now=clock, transport=unavailable)
            now += timedelta(seconds=8)
            assert process_one(engine, now=clock, transport=unavailable)
            now += timedelta(seconds=8)
            assert not process_one(engine, now=clock, transport=unavailable)
            job = ok(http.get(stage))["attempts"][0]["modelReview"]
            assert job["state"] == "blocked" and job["reason"] == "service_unavailable" and job["calls"] == 4
            assert len(seen) == 4
        finally:
            engine.dispose()


def test_unconfirmed_sent_request_requires_explicit_decision_after_restart(tmp_path):
    clock = lambda: BEFORE
    with open_client(tmp_path, clock) as http:
        setup_models(http)
        stage, url = evidence_attempt(http, tmp_path)
        _, auth = approved(http, url)
        ok(http.post(url, json=auth))
        engine = open_db(tmp_path)
        try:
            claim = reserve(engine, BEFORE)
            with transaction(engine) as db:
                db.get(StageReviewCall, claim.id).status = "sending"
            settle_interrupted(engine, BEFORE)
            job = ok(http.get(stage))["attempts"][0]["modelReview"]
            assert job["state"] == "blocked" and job["reason"] == "uncertain" and job["calls"] == 1
            assert not process_one(engine, now=clock, transport=reply_with(VALID, []))
            ok(http.post(f"{url}/stop"))
            assert not process_one(engine, now=clock, transport=reply_with(VALID, []))
            _, auth = approved(http, url)
            ok(http.post(url, json=auth))
            insufficient = {"findings": [{"criterion": 1, "verdict": "insufficient",
                                           "note": "证据不足以确认实际使用行为。", "citations": []}]}
            assert process_one(engine, now=clock, transport=reply_with(insufficient, []))
            assert ok(http.get(stage))["status"] == "insufficient"
        finally:
            engine.dispose()
