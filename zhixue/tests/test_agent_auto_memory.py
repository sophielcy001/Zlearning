import json
from uuid import uuid4

from test_chat_model import confirmed, draft, worker
from test_local_memory import entries
from test_memory_inference import API, ORIGINAL, RESULT, work
from test_model_study import ok, session, setup_model
from test_professional_agent import AGENTS, create_agent


def send(http, agent, text=ORIGINAL):
    route = f"/api/chat-model/{uuid4()}"
    source = draft(text, agentId=agent["id"])
    preview, payload = confirmed(http, route, source)
    ok(http.post(route + "/send", json=payload))
    ok(http.post(route + "/send", json=payload))
    return preview, source["id"]


def new_model(http, *, name="试印专用模型", model="print-tutor"):
    return ok(http.post("/api/models/connections", json={
        "name": name, "provider": "custom", "protocol": "chat-completions",
        "endpoint": "http://127.0.0.1:9210/v1", "model": model,
    }))


def test_auto_memory_is_independent_and_sends_one_original_to_the_agent_model(tmp_path):
    chat_calls, inference = [], []
    with session(tmp_path) as http:
        setup_model(http)
        model = new_model(http)
        _, _, default = create_agent(http, skills=[], memoryScopes=["private"])
        preview, _ = send(http, default, "没有授权，不发送这段。")
        assert not preview["agent"].get("autoMemory")
        assert worker(tmp_path, chat_calls)
        assert not work(tmp_path, inference, {"text": RESULT})

        _, grant, agent = create_agent(http, connectionId=model["id"], skills=[], autoMemory=True)
        assert grant["autoMemory"] and grant["recipient"]["id"] == model["id"]
        assert agent["autoMemory"] and "autoMemoryRecipient" not in agent
        assert not agent.get("memoryScopes")
        route = f"/api/chat-model/{uuid4()}"
        source = draft(ORIGINAL, agentId=agent["id"])
        preview, payload = confirmed(http, route, source)
        assert preview["agent"]["autoMemory"]
        assert not work(tmp_path, inference, {"text": RESULT})
        ok(http.post(route + "/send", json=payload))
        ok(http.post(route + "/send", json=payload))
        assert not work(tmp_path, inference, {"text": RESULT})
        assert worker(tmp_path, chat_calls)
        assert chat_calls[-1][0].startswith("http://127.0.0.1:9210/")
        from backend.db import open_db
        from backend.memory_inference import dispatch_memory_inference, reserve_memory_inference
        from datetime import UTC, datetime

        engine = open_db(tmp_path)
        try:
            claim = reserve_memory_inference(engine, datetime.now(UTC))
            assert claim is not None and claim.model == "print-tutor"
            assert claim.address.startswith("http://127.0.0.1:9210/")

            def transport(address, _headers, body):
                inference.append((address, body["messages"][-1]["content"]))
                return 200, json.dumps({
                    "choices": [{"message": {"content": json.dumps({"text": RESULT})}}],
                }).encode()

            dispatch_memory_inference(engine, claim, transport=transport)
        finally:
            engine.dispose()

        [(address, prompt)] = inference
        assert address.startswith("http://127.0.0.1:9210/")
        assert json.loads(prompt.split("原话 JSON\n")[1]) == {"text": ORIGINAL}
        [entry] = entries(http, "private")
        assert entry["agentId"] == agent["id"] and entry["source"]["messageId"] == source["id"]
        assert entry["origin"] == "inferred" and entry["text"] == RESULT
        assert entries(http, "shared") == [] and entries(http, "pending") == []
        assert ok(http.get(AGENTS + f"/{agent['id']}/memories"))["items"] == []
        assert not work(tmp_path, inference, {"text": RESULT})



def update_grant(http, initial, saved, *, enabled):
    form = {**initial, "revision": saved["revision"], "autoMemory": enabled}
    disclosure = ok(http.post(AGENTS + "/preview", json=form))
    return ok(http.post(AGENTS, json={**form, "confirmation": disclosure["confirmation"]}))


def test_revoke_cancels_queued_and_inflight_automatic_results(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        data, _, agent = create_agent(http, skills=[], autoMemory=True)
        send(http, agent)
        assert worker(tmp_path, [])
        send(http, agent, "以后练习前先做一个样例。")
        assert worker(tmp_path, [])
        jobs = ok(http.get(API))["items"]
        assert len(jobs) == 2 and all(job["automatic"] and job["state"] == "queued" for job in jobs)

        def revoke():
            saved = update_grant(http, data, agent, enabled=False)
            assert not saved.get("autoMemory")

        assert work(tmp_path, calls, {"text": RESULT}, during=revoke)
        assert len(calls) == 1
        for job in jobs:
            assert ok(http.get(f"{API}/{job['id']}"))["state"] == "cancelled"
        assert entries(http, "private") == []
        assert not work(tmp_path, calls, {"text": RESULT})
        preview, _ = send(http, agent, "关闭后发送的新问题。")
        assert not preview["agent"].get("autoMemory")
        assert worker(tmp_path, [])
        assert not work(tmp_path, calls, {"text": RESULT})
        assert len(calls) == 1


def test_connection_change_invalidates_old_permission_until_reconfirmed(tmp_path):
    calls = []
    with session(tmp_path) as http:
        first = setup_model(http)
        data, _, agent = create_agent(http, skills=[], autoMemory=True)
        send(http, agent)
        assert worker(tmp_path, [])
        [job] = ok(http.get(API))["items"]
        other = new_model(http)
        assert other["id"] != first["id"]
        ok(http.patch("/api/models/roles", json={"main": other["id"]}))
        assert not work(tmp_path, calls, {"text": RESULT})
        assert ok(http.get(f"{API}/{job['id']}"))["errorCode"] == "configuration_changed"
        preview, _ = send(http, agent, "更换模型后尚未确认。")
        assert preview["recipient"]["id"] == other["id"] and preview["agent"]["autoMemory"] is False
        assert worker(tmp_path, [])
        assert not work(tmp_path, calls, {"text": RESULT})
        agent = update_grant(http, data, agent, enabled=True)
        preview, _ = send(http, agent, ORIGINAL)
        assert preview["agent"]["autoMemory"] and preview["recipient"]["id"] == other["id"]
        assert worker(tmp_path, [])
        assert work(tmp_path, calls, {"text": RESULT})
        assert len(calls) == 1


def test_automatic_job_using_agent_connection_resumes_after_daily_limit(tmp_path):
    from test_daily_budget import RECOVERY, SETTINGS

    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        model = new_model(http)
        _, _, agent = create_agent(http, connectionId=model["id"], skills=[], autoMemory=True)
        ok(http.put(SETTINGS, json={"concurrency": 2, "dailyLimit": 1}))
        send(http, agent)
        assert worker(tmp_path, [])
        assert not work(tmp_path, calls, {"text": RESULT})
        [job] = ok(http.get(API))["items"]
        assert job["errorCode"] == "daily_budget"
        ok(http.put(SETTINGS, json={"concurrency": 2, "dailyLimit": 2}))
        choices = ok(http.get(RECOVERY))
        task = next(task for task in choices["tasks"] if task["id"] == job["id"])
        assert task["kind"] == "memory_inference" and task["canResume"]
        ok(http.post(RECOVERY, json={"billingDay": choices["billingDay"],
                                   "tasks": [{"id": job["id"], "kind": "memory_inference"}]}))
        assert work(tmp_path, calls, {"text": RESULT})
        assert len(calls) == 1 and len(entries(http, "private")) == 1


def test_restore_keeps_saved_memory_and_revokes_background_authorization(tmp_path):
    from test_backups import accepted, inspect_backup, make_backup
    from test_full_restore import app_client, request_restore, wait_restore

    with app_client(tmp_path) as http:
        setup_model(http)
        _, _, agent = create_agent(http, skills=[], autoMemory=True, memoryScopes=["private"])
        send(http, agent)
        assert worker(tmp_path, [])
        assert work(tmp_path, [], {"text": RESULT})
        send(http, agent, "每次先核对再学习。")
        assert worker(tmp_path, [])
        queued = next(job for job in ok(http.get(API))["items"] if job["state"] == "queued")
        backup = make_backup(http)
        plan = accepted(inspect_backup(http, backup))
        request_restore(http, backup, plan)
        assert wait_restore(http)["state"] == "complete"
        [saved_agent] = ok(http.get(AGENTS))["items"]
        assert not saved_agent.get("autoMemory") and saved_agent["memoryScopes"] == ["private"]
        assert ok(http.get(f"{API}/{queued['id']}"))["errorCode"] == "restored"
        [saved] = entries(http, "private")
        assert saved["text"] == RESULT
        assert not work(tmp_path, [], {"text": RESULT})
        preview, _ = send(http, saved_agent, "恢复后发送的新问题。")
        assert not preview["agent"].get("autoMemory")
        assert worker(tmp_path, [])
        assert not work(tmp_path, [], {"text": RESULT})
