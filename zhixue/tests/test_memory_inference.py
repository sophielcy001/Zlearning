from datetime import UTC, datetime
import json
from uuid import uuid4

from test_local_memory import API as MEMORIES, entries, review
from test_model_study import ok, session, setup_model
from test_professional_agent import AGENTS, create_agent


API = "/api/memory-inference"
ORIGINAL = "我每次都想先看一个试印例子，再自己动手。"
RESULT = "先提供一例试印，再安排独立练习。"


def message(http, text=ORIGINAL):
    source = {"id": str(uuid4()), "text": text}
    ok(http.post("/api/conversations", json={"id": str(uuid4()), "message": source}))
    return source["id"]


def proposal(http, source, agent=None):
    request = {"id": str(uuid4()), "sourceMessageId": source, "agentId": agent}
    preview = ok(http.post(API + "/preview", json=request))
    return request, preview, {**request, "consent": preview["consent"]}


def work(folder, calls, result, during=lambda: None):
    from backend.db import open_db
    from backend.memory_inference import dispatch_memory_inference, reserve_memory_inference

    def transport(_address, _headers, request):
        calls.append(request["messages"][-1]["content"])
        during()
        return 200, json.dumps({
            "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 28, "completion_tokens": 11},
        }).encode()

    engine = open_db(folder)
    try:
        claim = reserve_memory_inference(engine, datetime.now(UTC))
        if claim:
            dispatch_memory_inference(engine, claim, transport=transport)
        return claim is not None
    finally:
        engine.dispose()


def test_shared_inference_uses_only_confirmed_original_and_waits_for_review(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        _, _, agent = create_agent(http, skills=[], memoryScopes=["shared"])
        source = message(http)
        _, preview, submitted = proposal(http, source)
        assert preview["text"] == ORIGINAL and preview["agent"] is None
        assert not work(tmp_path, calls, {"text": RESULT})
        assert http.post(API, json={**submitted, "agentId": agent["id"]}).status_code == 409
        queued = ok(http.post(API, json=submitted))
        assert ok(http.post(API, json=submitted)) == queued
        assert http.post(API + "/preview", json={"id": str(uuid4()), "sourceMessageId": source}).status_code == 409
        assert work(tmp_path, calls, {"text": RESULT})
        assert len(calls) == 1
        assert json.loads(calls[0].split("原话 JSON\n")[1]) == {"text": ORIGINAL}
        [pending] = entries(http, "pending")
        assert pending["text"] == RESULT and pending["origin"] == "inferred"
        assert pending["source"]["messageId"] == source
        assert entries(http, "shared") == []
        assert ok(http.get(AGENTS + f"/{agent['id']}/memories"))["items"] == []
        ok(review(http, "accept", pending))
        assert any(item["kind"] == "memory_inference" for item in ok(http.get("/api/generation/usage"))["items"])
    with session(tmp_path) as http:
        [saved] = entries(http, "shared")
        assert saved["text"] == RESULT and saved["origin"] == "inferred"


def test_private_result_is_scoped_and_manual_save_during_call_prevents_overwrite(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        _, _, owner = create_agent(http, skills=[], memoryScopes=["private"])
        _, _, other = create_agent(http, name="另一位导师", skills=[], memoryScopes=["private"])
        source = message(http)
        _, preview, payload = proposal(http, source, owner["id"])
        assert preview["agent"] == {"id": owner["id"], "name": owner["name"]}
        ok(http.post(API, json=payload))
        assert work(tmp_path, calls, {"text": RESULT})
        [record] = entries(http, "private")
        assert record["agentId"] == owner["id"] and record["origin"] == "inferred"
        assert ok(http.get(AGENTS + f"/{other['id']}/memories"))["items"] == []
        ok(http.post(MEMORIES + "/" + record["id"] + "/delete", json={"revision": record["revision"]}))
        assert http.post(API + "/preview", json={"id": str(uuid4()), "sourceMessageId": source}).status_code == 409

        next_source = message(http, "这是另外一条对话。")
        _, _, next_job = proposal(http, next_source)
        ok(http.post(API, json=next_job))
        def save_manually():
            ok(http.post(MEMORIES, json={"id": str(uuid4()), "text": "手动记录", "sourceMessageId": next_source}))
        assert work(tmp_path, calls, {"text": "不能覆盖的建议"}, during=save_manually)
        assert ok(http.get(API + "/" + next_job["id"]))["errorCode"] == "scope_changed"
        [pending] = entries(http, "pending")
        assert pending["text"] == "手动记录" and pending["origin"] == "explicit"


def test_cancelled_empty_and_out_of_schema_results_do_not_create_memories(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        source = message(http)
        _, _, cancelled = proposal(http, source)
        ok(http.post(API, json=cancelled))
        assert ok(http.delete(API + "/" + cancelled["id"]))["state"] == "cancelled"
        assert not work(tmp_path, calls, {"text": RESULT})
        for result, state in (({"text": RESULT, "agentId": str(uuid4())}, "failed"), ({"text": None}, "ready")):
            _, _, job = proposal(http, source)
            ok(http.post(API, json=job))
            assert work(tmp_path, calls, result)
            checked = ok(http.get(API + "/" + job["id"]))
            assert checked["state"] == state and checked["memoryId"] is None
        assert entries(http, "pending") == [] and entries(http, "private") == []

def test_memory_inference_obeys_daily_budget_and_can_resume(tmp_path):
    from test_daily_budget import RECOVERY, SETTINGS

    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        ok(http.put(SETTINGS, json={"concurrency": 2, "dailyLimit": 1}))
        _, _, first = proposal(http, message(http))
        ok(http.post(API, json=first))
        assert work(tmp_path, calls, {"text": None})
        _, _, second = proposal(http, message(http, "我想先练习一个短句。"))
        ok(http.post(API, json=second))
        assert not work(tmp_path, calls, {"text": None})
        assert ok(http.get(API + "/" + second["id"]))["errorCode"] == "daily_budget"
        ok(http.put(SETTINGS, json={"concurrency": 2, "dailyLimit": 2}))
        choices = ok(http.get(RECOVERY))
        task = next(task for task in choices["tasks"] if task["id"] == second["id"])
        assert task["kind"] == "memory_inference" and task["canResume"]
        ok(http.post(RECOVERY, json={"billingDay": choices["billingDay"],
                                   "tasks": [{"id": second["id"], "kind": "memory_inference"}]}))
        assert work(tmp_path, calls, {"text": RESULT})
        assert len(calls) == 2


def test_restored_inference_requires_new_confirmation(tmp_path):
    from test_backups import accepted, inspect_backup, make_backup
    from test_full_restore import app_client, request_restore, wait_restore

    with app_client(tmp_path) as http:
        setup_model(http)
        _, _, request = proposal(http, message(http))
        ok(http.post(API, json=request))
        backup = make_backup(http)
        plan = accepted(inspect_backup(http, backup))
        assert plan["counts"]["backup"]["memory_inference_jobs"] == 1
        request_restore(http, backup, plan)
        assert wait_restore(http)["state"] == "complete"
        assert ok(http.get(API + "/" + request["id"]))["errorCode"] == "restored"
        assert not work(tmp_path, [], {"text": RESULT})