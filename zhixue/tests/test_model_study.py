from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import ModelCharge, ModelUse, open_db, transaction


SESSION = "model-study-integration"
MAIN_INPUT = "练习昆曲唱腔"
PLAN = {
    "stages": [
        {"name": "基础", "lessons": [
            {"key": f"k{number}", "title": f"唱腔练习 {number}", "outcome": f"完成练习 {number}",
             "prerequisites": [f"k{number - 1}"] if number else []}
            for number in range(3)
        ]},
        {"name": "实践", "lessons": [
            {"key": f"k{number}", "title": f"唱腔练习 {number}", "outcome": f"完成练习 {number}",
             "prerequisites": [f"k{number - 1}"]}
            for number in range(3, 8)
        ]},
    ],
}
DRAFT = {"sections": [{"title": "隔离草稿标题", "text": "隔离草稿正文", "checks": ["出处待查"]}]}


def session(folder):
    return TestClient(create_app(folder, SESSION), base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {SESSION}"})


def ok(response):
    assert response.is_success, response.text
    return response.json()


def setup_model(http):
    connection = ok(http.post("/api/models/connections", json={
        "name": "本机生成测试", "provider": "custom", "protocol": "chat-completions",
        "endpoint": "http://127.0.0.1:9109/v1", "model": "isolated-model",
    }))
    ok(http.patch("/api/models/roles", json={"main": connection["id"]}))
    return connection


def consent_request(http, max_calls=None):
    thread = ok(http.post("/api/conversations", json={
        "id": str(uuid4()), "message": {"id": str(uuid4()), "text": f"我想{MAIN_INPUT}"},
    }))
    endpoint = f"/api/conversations/{thread['id']}/goal"
    draft = ok(http.post(endpoint))
    payload = {"goalId": draft["id"], "revision": draft["revision"], "values": {"outcome": "唱完指定片段"}}
    disclosure = ok(http.post(f"{endpoint}/model-preview", json={**payload, "maxCalls": max_calls}))
    return f"{endpoint}/submit", payload, disclosure


def transport_recorder(calls, effects=None, *, route=PLAN):
    def transport(address, headers, request):
        calls.append((address, request))
        text = request["messages"][-1]["content"]
        if effects:
            effects(len(calls))
        result = route if "制定学习路线" in text else DRAFT
        body = {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 90}}
        return 200, json.dumps(body, ensure_ascii=False).encode()
    return transport


def run(folder, transport, **options):
    from backend.model_study import process_one

    engine = open_db(folder)
    try:
        return process_one(engine, transport=transport, **options)
    finally:
        engine.dispose()


def test_revised_goal_requires_fresh_model_consent_even_with_a_reserved_request(tmp_path):
    from backend.model_study import dispatch, reserve_call, utc_now

    observed = []
    transport = transport_recorder(observed)
    with session(tmp_path) as http:
        setup_model(http)
        submit, fields, initial_disclosure = consent_request(http, 10)
        created = ok(http.post(submit, json={**fields, "modelConsent": initial_disclosure["consent"]}))
        assert run(tmp_path, transport)
        base = f"/api/projects/{created['projectId']}"
        generation = base + "/generation"
        edit = base + "/goal-revision"
        old_scope = ok(http.post(generation + "/authorize/preview", json={"maxCalls": 10}))
        engine = open_db(tmp_path)
        try:
            reserved = reserve_call(engine, utc_now())
            unapproved = reserve_call(engine, utc_now())
            assert reserved is not None and unapproved is not None and "唱完指定片段" in reserved.prompt
            payload = {"baseGoalVersion": 1, "baseRouteVersion": 1, "values": {
                "outcome": "录制独立练习成果", "deadline": "2027-06-15",
            }}
            preview = ok(http.post(edit + "/preview", json=payload))
            assert preview["requiresModelConsent"]
            assert ok(http.get(generation))["authorizationCurrent"]
            ok(http.post(edit + "/confirm", json={**payload, "confirmation": preview["confirmation"]}))
            assert not ok(http.get(generation))["authorizationCurrent"]
            dispatch(engine, unapproved, transport=transport)
            assert ok(http.get(generation))["reason"] == "goal_changed"
            assert reserve_call(engine, utc_now()) is None
            assert http.post(generation + "/authorize", json=old_scope["consent"]).status_code == 409
            next_scope = ok(http.post(generation + "/authorize/preview", json={"maxCalls": 10}))
            assert {field["key"]: field["value"] for field in next_scope["inputs"]} == {
                "topic": "昆曲唱腔", "outcome": "录制独立练习成果", "deadline": "2027-06-15",
            }
            assert len(observed) == 1
            resumed = ok(http.post(generation + "/authorize", json=next_scope["consent"]))
            assert resumed["authorizationCurrent"] and resumed["maxCalls"] == 10
            dispatch(engine, reserved, transport=transport)
            assert len(observed) == 1 and ok(http.get(generation))["usage"]["calls"] == 1
            assert run(tmp_path, transport)
            prompt = observed[-1][1]["messages"][0]["content"]
            assert "录制独立练习成果" in prompt and "2027-06-15" in prompt and "唱完指定片段" not in prompt
            assert len(observed) == 2
        finally:
            engine.dispose()


def test_revised_goal_keeps_independently_authorized_review_running(tmp_path):
    from test_course_verification import SOURCE, authorize, model_traffic, prepare, publish

    calls = []
    with session(tmp_path) as http:
        identity, lesson, _, scope = prepare(http, tmp_path, calls, limit=10, route=PLAN)
        project = f"/api/projects/{identity}"
        authorize(http, identity, scope)
        following = ok(http.get(project))["lessons"][1]["id"]
        ok(http.put(project + "/generation/focus", json={"lessonId": following}))
        data = {"baseGoalVersion": 1, "baseRouteVersion": 1, "values": {"outcome": "新目标留在本机"}}
        endpoint = project + "/goal-revision"
        preview = ok(http.post(endpoint + "/preview", json=data))
        ok(http.post(endpoint + "/confirm", json={**data, "confirmation": preview["confirmation"]}))
        assert run(tmp_path, model_traffic(calls))
        assert publish(tmp_path)
        assert len(calls) == 3 and calls[-1][0] == "independent-model"
        assert SOURCE in calls[-1][1] and "新目标留在本机" not in calls[-1][1]
        view = ok(http.get(f"/api/lessons/{lesson}"))
        assert view["courseVersion"] == 1
        generation = ok(http.get(project + "/generation"))
        assert not generation["authorizationCurrent"] and generation["reason"] == "goal_changed"
        assert not run(tmp_path, model_traffic(calls))


def test_consented_model_route_queues_five_and_keeps_drafts_out_of_published_body(tmp_path):
    calls = []
    with session(tmp_path) as http:
        connection = setup_model(http)
        endpoint, data, disclosure = consent_request(http)
        assert disclosure["connection"]["id"] == connection["id"]
        assert disclosure["connection"]["address"] == "http://127.0.0.1:9109/v1/chat/completions"
        auth = {**data, "modelConsent": disclosure["consent"]}
        modified = {**auth, "values": {"outcome": "未确认发送的替换数据"}}
        assert http.post(endpoint, json=modified).status_code == 409
        result = ok(http.post(endpoint, json=auth))
        assert ok(http.post(endpoint, json=auth)) == result
        project_url = f"/api/projects/{result['projectId']}"
        assert ok(http.get(project_url))["lessons"] == []
        assert run(tmp_path, transport_recorder(calls))
        course = ok(http.get(project_url))
        assert len(course["lessons"]) == 8 and course["generationMode"] == "model"
        outgoing = calls[0][1]["messages"][0]["content"]
        assert json.loads(outgoing.split("\n目标 JSON\n", 1)[1]) == {"topic": "昆曲唱腔", "outcome": "唱完指定片段"}
        lessons = [item["id"] for item in course["lessons"]]
        url = f"{project_url}/generation"
        initial = ok(http.get(url))
        assert initial["window"] == lessons[:3]
        focused = ok(http.put(f"{url}/focus", json={"lessonId": lessons[4]}))
        assert focused["window"] == lessons[2:7]
        assert run(tmp_path, transport_recorder(calls))
        assert "唱腔练习 4" in calls[-1][1]["messages"][0]["content"]
        states = ok(http.get(url))
        drafted = next(item for item in states["steps"] if item["lessonId"] == lessons[4])
        assert drafted["state"] == "awaiting_review"
        assert ok(http.get(f"{url}/drafts/{lessons[4]}")) == DRAFT
        reading = ok(http.get(f"/api/lessons/{lessons[4]}"))
        assert reading["body"] is None
        assert http.put(f"/api/lessons/{lessons[4]}/learned", json={"learned": True}).status_code == 409
        assert states["usage"]["calls"] == 2 and states["usage"]["outputTokens"] == 180

    with session(tmp_path) as http:
        restarted = ok(http.get(url))
        assert restarted["focusId"] == lessons[4] and restarted["window"] == lessons[2:7]
        assert run(tmp_path, transport_recorder(calls))
        assert "唱腔练习 4" not in calls[-1][1]["messages"][0]["content"]
        assert sum("制定学习路线" in call[1]["messages"][0]["content"] for call in calls) == 1


def test_route_artifact_survives_stop_and_call_limit_without_hidden_dispatch(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        endpoint, data, preview = consent_request(http, max_calls=1)
        created = ok(http.post(endpoint, json={**data, "modelConsent": preview["consent"]}))
        project = f"/api/projects/{created['projectId']}"
        url = f"{project}/generation"

        def stop_on_first(count):
            if count == 1:
                ok(http.post(f"{url}/control", json={"action": "stop"}))

        assert run(tmp_path, transport_recorder(calls, stop_on_first))
        assert len(calls) == 1 and ok(http.get(project))["lessons"] == []
        assert ok(http.get(url))["state"] == "stopped"
        assert not run(tmp_path, transport_recorder(calls))
        ok(http.post(f"{url}/control", json={"action": "resume"}))
        assert len(ok(http.get(project))["lessons"]) == 8
        run(tmp_path, transport_recorder(calls))
        assert len(calls) == 1 and ok(http.get(url))["reason"] == "task_budget"
        ok(http.post(f"{url}/control", json={"action": "resume", "maxCalls": 2}))
        assert run(tmp_path, transport_recorder(calls)) and len(calls) == 2


def test_daily_limit_requires_manual_resume_after_shanghai_day_changes(tmp_path):
    calls = []
    before = datetime(2027, 2, 1, 15, 59, tzinfo=UTC)
    after = before + timedelta(minutes=2)
    with session(tmp_path) as http:
        setup_model(http)
        ok(http.put("/api/generation/settings", json={"concurrency": 2, "dailyLimit": 1}))
        endpoint, data, preview = consent_request(http)
        created = ok(http.post(endpoint, json={**data, "modelConsent": preview["consent"]}))
        url = f"/api/projects/{created['projectId']}/generation"
        assert run(tmp_path, transport_recorder(calls), now=lambda: before)
        assert not run(tmp_path, transport_recorder(calls), now=lambda: before)
        assert ok(http.get(url))["reason"] == "daily_budget"
        assert not run(tmp_path, transport_recorder(calls), now=lambda: after)
        assert len(calls) == 1
        ok(http.post(f"{url}/control", json={"action": "resume"}))
        assert run(tmp_path, transport_recorder(calls), now=lambda: after)
        assert len(calls) == 2


def admit_for_interruption(engine, identity, prompt, now):
    from backend.model_usage import admit_reserved_call

    with transaction(engine) as db:
        call = db.get(ModelUse, identity)
        assert admit_reserved_call(db, call, lambda: now, prompt, 120) is None
        assert call.status == "sending"


def assert_interrupted_use(engine, identity, sending):
    with transaction(engine, write=False) as db:
        call = db.get(ModelUse, identity)
        assert call.status == ("uncertain" if sending else "not_sent")
        assert call.finished_at >= call.started_at
        assert (db.get(ModelCharge, (ModelUse.__tablename__, identity)) is not None) == sending


def test_global_dispatch_limit_and_interrupted_request_cannot_publish_a_late_result(tmp_path):
    from backend.model_study import complete_call, reserve_call, settle_interrupted

    now = datetime(2027, 2, 1, 5, tzinfo=UTC)
    with session(tmp_path) as http:
        setup_model(http)
        endpoint, data, preview = consent_request(http)
        created = ok(http.post(endpoint, json={**data, "modelConsent": preview["consent"]}))
        url = f"/api/projects/{created['projectId']}/generation"
        assert run(tmp_path, transport_recorder([]), now=lambda: now)
        engine = open_db(tmp_path)
        try:
            first = reserve_call(engine, now)
            second = reserve_call(engine, now)
            assert first and second and first.step_id != second.step_id
            assert reserve_call(engine, now) is None
            admit_for_interruption(engine, first.identity, first.prompt, now)
            settle_interrupted(engine, now)
            assert_interrupted_use(engine, first.identity, sending=True)
            assert_interrupted_use(engine, second.identity, sending=False)
            assert ok(http.get(url))["reason"] == "uncertain"
            assert reserve_call(engine, now) is None
            complete_call(engine, first, "passed", DRAFT, {"outputTokens": 20}, now, sent=True)
            recovered = ok(http.get(url))
            assert all(item["state"] != "awaiting_review" for item in recovered["steps"])
            assert recovered["usage"]["calls"] == 2 and recovered["usage"]["outputTokens"] == 90
        finally:
            engine.dispose()


def test_worker_restarts_unsent_draft_at_the_same_phase_and_within_original_limit(tmp_path):
    from threading import Event, Thread

    from backend.model_study import reserve_call, utc_now
    from backend.worker import run_queue

    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        ok(http.put("/api/generation/settings", json={"dailyLimit": None, "concurrency": 1}))
        endpoint, data, preview = consent_request(http, 2)
        created = ok(http.post(endpoint, json={**data, "modelConsent": preview["consent"]}))
        path = f"/api/projects/{created['projectId']}/generation"
        first_stage = PLAN["stages"][0]
        single_lesson = {"stages": [{**first_stage, "lessons": first_stage["lessons"][:1]}]}
        reply = transport_recorder(calls, route=single_lesson)
        assert run(tmp_path, reply)
        first_engine = open_db(tmp_path)
        try:
            claim = reserve_call(first_engine, utc_now())
            assert claim and claim.kind == "draft"
        finally:
            first_engine.dispose()

        stopped = Event()
        received = Event()

        def transport(*args):
            response = reply(*args)
            stopped.set()
            received.set()
            return response

        engine = open_db(tmp_path)
        worker = Thread(target=run_queue, args=(engine, tmp_path, stopped), kwargs={"transport": transport})
        worker.start()
        try:
            try:
                resumed = received.wait(6)
            finally:
                stopped.set()
                worker.join(8)
            assert not worker.is_alive()
            assert resumed, ok(http.get(path))
            assert_interrupted_use(engine, claim.identity, sending=False)
        finally:
            engine.dispose()
        saved = ok(http.get(path))
        assert saved["usage"]["calls"] == len(calls) == 2
        assert saved["reason"] is None
        assert any(step["state"] == "awaiting_review" for step in saved["steps"])
        assert "制定学习路线" in calls[0][1]["messages"][-1]["content"]
        assert "起草一个知识点" in calls[1][1]["messages"][-1]["content"]


def test_delete_previews_scope_and_discards_late_draft_but_keeps_route_and_usage(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        endpoint, data, preview = consent_request(http)
        created = ok(http.post(endpoint, json={**data, "modelConsent": preview["consent"]}))
        project = f"/api/projects/{created['projectId']}"
        url = f"{project}/generation"
        assert run(tmp_path, transport_recorder(calls))
        assert run(tmp_path, transport_recorder(calls))
        original = ok(http.get(project))
        stale = ok(http.post(f"{url}/delete-preview"))

        def delete_while_running(_):
            assert http.post(f"{url}/delete", json={"confirmation": stale["confirmation"]}).status_code == 409
            current = ok(http.post(f"{url}/delete-preview"))
            assert current["drafts"] == 1 and current["running"] == 1 and current["routeNodes"] == 8
            assert ok(http.post(f"{url}/delete", json={"confirmation": current["confirmation"]}))["state"] == "deleted"

        assert run(tmp_path, transport_recorder(calls, delete_while_running))
        assert not run(tmp_path, transport_recorder(calls))
        assert http.post(f"{url}/control", json={"action": "resume"}).status_code == 409
        assert ok(http.get(project))["lessons"] == original["lessons"]
        assert ok(http.get("/api/state"))["taskCount"] == 0
        generation = ok(http.get(url))
        assert generation["usage"]["calls"] == 3 and generation["usage"]["outputTokens"] == 270
        assert all(item["state"] == "discarded" for item in generation["steps"])


def test_reassigning_main_requires_new_authorization_before_data_is_sent(tmp_path):
    calls = []
    with session(tmp_path) as http:
        first = setup_model(http)
        endpoint, data, preview = consent_request(http)
        created = ok(http.post(endpoint, json={**data, "modelConsent": preview["consent"]}))
        url = f"/api/projects/{created['projectId']}/generation"
        second = setup_model(http)
        assert second["id"] != first["id"]
        assert not run(tmp_path, transport_recorder(calls))
        assert ok(http.get(url))["reason"] == "configuration_changed" and not calls
        consent = ok(http.post(f"{url}/authorize/preview", json={}))
        assert consent["connection"]["id"] == second["id"]
        ok(http.post(f"{url}/authorize", json=consent["consent"]))
        assert run(tmp_path, transport_recorder(calls)) and len(calls) == 1

def observe_worker(folder, operation, *, transport):
    from threading import Event, Thread

    from acceptance.product import until
    from backend.worker import run_queue

    stopped = Event()
    engine = open_db(folder)
    worker = Thread(target=run_queue, args=(engine, folder, stopped), kwargs={"transport": transport})
    worker.start()
    try:
        return until(operation, "Worker did not record the interrupted operation")
    finally:
        stopped.set()
        worker.join(8)
        assert not worker.is_alive()
        engine.dispose()


@pytest.mark.parametrize("failure", ["before-send", "sending", "after-reply"])
def test_worker_dispatch_fault_preserves_send_boundary_and_settled_usage(tmp_path, monkeypatch, failure):
    from sqlalchemy import select

    def raise_local_fault(*_args, **_kwargs):
        raise RuntimeError("injected unexpected worker fault")

    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        ok(http.put("/api/generation/settings", json={"concurrency": 1, "dailyLimit": None}))
        endpoint, data, preview = consent_request(http, 2)
        created = ok(http.post(endpoint, json={**data, "modelConsent": preview["consent"]}))
        project = f"/api/projects/{created['projectId']}"
        path = project + "/generation"
        response = transport_recorder(calls)
        assert run(tmp_path, response)
        original = ok(http.get(project))["lessons"]
        assert original

        def transport(address, headers, request):
            if failure == "sending":
                calls.append((address, request))
                raise_local_fault()
            return response(address, headers, request)

        def failed():
            result = ok(http.get(path))
            return result if result["state"] == "blocked" else None

        with monkeypatch.context() as patch:
            if failure == "before-send":
                patch.setattr("backend.worker.dispatch", raise_local_fault)
            elif failure == "after-reply":
                patch.setattr("backend.model_study.parse_result", raise_local_fault)
            result = observe_worker(tmp_path, failed, transport=transport)
        sent = failure != "before-send"
        assert result["usage"]["calls"] == len(calls) == 1 + int(sent)
        assert result["reason"] == ("uncertain" if sent else "dispatch_failed")
        assert result["error"]
        assert ok(http.get(project))["lessons"] == original
        engine = open_db(tmp_path)
        try:
            with transaction(engine, write=False) as db:
                draft, = db.scalars(select(ModelUse).where(ModelUse.phase == "draft"))
                charge = db.get(ModelCharge, (ModelUse.__tablename__, draft.id))
                assert not draft.auto_retry and draft.finished_at is not None
                assert draft.status == ("uncertain" if sent else "not_sent")
                if failure == "after-reply":
                    assert charge.status == "passed"
                    assert charge.input_tokens == 30 and charge.output_tokens == 90
                    assert result["usage"]["reportedCalls"] == 2
                    assert result["usage"]["inputTokens"] == 60 and result["usage"]["outputTokens"] == 180
                elif sent:
                    assert charge.status == "uncertain"
                    assert charge.actual_amount is charge.estimated_amount is None
                else:
                    assert charge is None
        finally:
            engine.dispose()
        ok(http.post(path + "/control", json={"action": "resume", "maxCalls": 3 if sent else 2}))
        assert run(tmp_path, response)
        checked = ok(http.get(path))
        assert checked["usage"]["calls"] == len(calls) == 2 + int(sent)
        assert any(step["state"] == "awaiting_review" for step in checked["steps"])

