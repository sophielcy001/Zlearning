from datetime import UTC, datetime
import json
from uuid import uuid4

from test_model_study import ok, session, setup_model


API = "/api/agent-authoring"
DESCRIPTION = "帮助我检查试印前的版框，先给出一条简短提示。"
SUGGESTION = {"name": "试印练习导师", "responsibility": "提示先确认版框平整，再询问检查结果。"}
PROMPT_LABEL = "需求描述 JSON"


def propose(http, purpose=DESCRIPTION):
    data = {"id": str(uuid4()), "purpose": purpose}
    found = ok(http.post(API + "/preview", json=data))
    assert found["purpose"] == purpose
    return {**data, "consent": found["consent"]}


def drive(folder, calls, response):
    from backend.agent_authoring import dispatch_agent_authoring, reserve_agent_authoring
    from backend.db import open_db

    def transport(address, _headers, request):
        calls.append((address, request["messages"][-1]["content"]))
        return 200, json.dumps({
            "choices": [{"message": {"content": json.dumps(response, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 23, "completion_tokens": 12},
        }).encode()

    engine = open_db(folder)
    try:
        claim = reserve_agent_authoring(engine, datetime.now(UTC))
        if claim:
            dispatch_agent_authoring(engine, claim, transport=transport)
        return claim is not None
    finally:
        engine.dispose()


def test_only_confirmed_description_is_sent_and_result_remains_an_editable_draft(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        payload = propose(http)
        assert not drive(tmp_path, calls, SUGGESTION)
        assert not calls
        assert http.post(API, json={**payload, "purpose": "修改后的范围"}).status_code == 409
        assert http.post(API, json={**payload, "consent": {**payload["consent"], "maxCalls": 2}}).status_code == 409
        queued = ok(http.post(API, json=payload))
        assert queued["state"] == "queued"
        assert ok(http.post(API, json=payload)) == queued
        assert ok(http.get("/api/agents"))["items"] == []
        assert drive(tmp_path, calls, SUGGESTION)
        assert not drive(tmp_path, calls, SUGGESTION)
        assert len(calls) == 1
        assert json.loads(calls[0][1].split(PROMPT_LABEL + "\n", 1)[1]) == {"purpose": DESCRIPTION}
        [job] = ok(http.get(API))["items"]
        assert job["state"] == "ready"
        assert job["suggestion"] == {**SUGGESTION, "purpose": DESCRIPTION}
        assert ok(http.get("/api/agents"))["items"] == []
        charges = ok(http.get("/api/generation/usage"))
        assert any(row["kind"] == "agent_authoring" for row in charges["items"])

    with session(tmp_path) as http:
        assert ok(http.get(API + "/" + payload["id"]))["suggestion"]["name"] == SUGGESTION["name"]


def test_invalid_model_permissions_cancel_and_changed_recipient(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        cancelled = propose(http)
        ok(http.post(API, json=cancelled))
        assert ok(http.delete(API + "/" + cancelled["id"]))["state"] == "cancelled"
        assert not drive(tmp_path, calls, SUGGESTION)

        payload = propose(http)
        ok(http.post(API, json=payload))
        assert drive(tmp_path, calls, {**SUGGESTION, "skills": ["fake"], "tools": ["Read"]})
        bad = ok(http.get(API + "/" + payload["id"]))
        assert bad["state"] == "failed" and bad["errorCode"] == "invalid_response" and bad["suggestion"] is None
        assert ok(http.get("/api/agents"))["items"] == []

        stale = propose(http)
        connection = ok(http.post("/api/models/connections", json={
            "name": "变更后的服务", "provider": "custom", "protocol": "chat-completions",
            "endpoint": "http://127.0.0.1:9225/v1", "model": "other",
        }))
        ok(http.patch("/api/models/roles", json={"main": connection["id"]}))
        assert http.post(API, json=stale).status_code == 409
        assert len(calls) == 1

def test_budget_hold_can_be_recovered_without_a_second_confirmation(tmp_path):
    settings = "/api/generation/settings"
    recovery = "/api/generation/daily-budget"
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        ok(http.put(settings, json={"concurrency": 2, "dailyLimit": 1}))
        first = propose(http)
        ok(http.post(API, json=first))
        assert drive(tmp_path, calls, SUGGESTION)
        second = propose(http, "整理练习反馈")
        ok(http.post(API, json=second))
        assert not drive(tmp_path, calls, SUGGESTION)
        assert ok(http.get(API + "/" + second["id"]))["errorCode"] == "daily_budget"
        ok(http.put(settings, json={"concurrency": 2, "dailyLimit": 2}))
        current = ok(http.get(recovery))
        task = next(item for item in current["tasks"] if item["id"] == second["id"])
        assert task["kind"] == "agent_authoring" and task["canResume"]
        data = {"billingDay": current["billingDay"], "tasks": [{"id": second["id"], "kind": "agent_authoring"}]}
        assert len(ok(http.post(recovery, json=data))["resumed"]) == 1
        assert drive(tmp_path, calls, SUGGESTION)
        assert ok(http.get(API + "/" + second["id"]))["state"] == "ready"


def test_restored_pending_draft_stops_before_sending(tmp_path):
    from test_backups import accepted, inspect_backup, make_backup
    from test_full_restore import app_client, request_restore, wait_restore

    calls = []
    with app_client(tmp_path) as http:
        setup_model(http)
        data = propose(http)
        ok(http.post(API, json=data))
        archive = make_backup(http)
        plan = accepted(inspect_backup(http, archive))
        assert plan["counts"]["backup"]["agent_authoring_jobs"] == 1
        request_restore(http, archive, plan)
        assert wait_restore(http)["state"] == "complete"
        job = ok(http.get(API + "/" + data["id"]))
        assert job["state"] == "failed" and job["errorCode"] == "restored"
        assert not drive(tmp_path, calls, SUGGESTION)
        assert not calls

