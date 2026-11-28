from uuid import uuid4

from test_chat_model import confirmed, draft, worker
from test_model_study import ok, session, setup_model
from test_skill_library import NEW, SOURCE, confirm_update, install, upload


AGENTS = "/api/agents"


def create_agent(http, **changes):
    data = {
        "id": str(uuid4()), "name": "试印练习导师", "purpose": "帮助整理试印的检查顺序",
        "responsibility": "每次先问一个检查问题。", "connectionId": None, "skills": ["study-checklist"],
        **changes,
    }
    preview = ok(http.post(AGENTS + "/preview", json=data))
    assert all(item["name"] in data["skills"] for item in preview["skills"])
    return data, preview, ok(http.post(AGENTS, json={**data, "confirmation": preview["confirmation"]}))


def test_agent_requires_confirmation_and_checks_each_skill_grant(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        install(http)
        data, preview, saved = create_agent(http)
        assert saved["revision"] == 1
        assert ok(http.get(AGENTS))["items"] == [saved]
        assert ok(http.post(AGENTS, json={**data, "confirmation": preview["confirmation"]})) == saved
        assert http.post(AGENTS, json={**data, "confirmation": preview["confirmation"], "skills": []}).status_code == 409
        assert http.post(AGENTS + "/preview", json={**data, "id": str(uuid4()), "skills": ["missing"]}).status_code == 409

        route = f"/api/chat-model/{uuid4()}"
        assert http.post(route + "/preview", json=draft("问题", skill="study-checklist")).status_code == 409
        another_data, _, _ = create_agent(http, name="未授权导师", skills=[])
        assert http.post(route + "/preview", json=draft("问题", agentId=another_data["id"], skill="study-checklist")).status_code == 409
        assign = draft("问题", agentId=saved["id"], skill="study-checklist")
        scope, accepted = confirmed(http, route, assign)
        assert scope["agent"]["skill"]["source"] == SOURCE.decode("utf-8")
        assert scope["agent"]["responsibility"] == saved["responsibility"]

        edit = {**data, "revision": saved["revision"], "skills": []}
        revision = ok(http.post(AGENTS + "/preview", json=edit))
        updated = ok(http.post(AGENTS, json={**edit, "confirmation": revision["confirmation"]}))
        assert updated["revision"] == 2 and updated["skills"] == []
        assert http.post(route + "/send", json=accepted).status_code == 409


def test_submitted_chat_reuses_the_original_skill_and_switch_preserves_history(tmp_path):
    route = f"/api/chat-model/{uuid4()}"
    conversation = route.replace("/chat-model/", "/conversations/")
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        alternative = ok(http.post("/api/models/connections", json={
            "name": "备用练习连接", "provider": "custom", "protocol": "chat-completions",
            "endpoint": "http://127.0.0.1:9210/v1", "model": "specific-tutor",
        }))
        first_version = install(http)
        _, _, agent = create_agent(http, connectionId=alternative["id"])
        first = draft("第一轮", agentId=agent["id"], skill="study-checklist")
        before, submitted = confirmed(http, route, first)
        assert before["recipient"]["id"] == alternative["id"]
        ok(http.post(route + "/send", json=submitted))
        next_message = draft("第二轮", agentId=agent["id"], skill="study-checklist")
        _, stale = confirmed(http, route, next_message)
        latest = ok(confirm_update(http, NEW, ok(upload(http, "/preview", NEW))))
        assert http.post(route + "/send", json=stale).status_code == 409
        fresh, request = confirmed(http, route, next_message)
        assert fresh["agent"]["skill"]["checksum"] == latest["checksum"]
        ok(http.post(route + "/send", json=request))

    assert worker(tmp_path, calls)
    assert worker(tmp_path, calls)
    one, two = [prompt for _, prompt in calls]
    assert first_version["checksum"] in one and latest["checksum"] not in one
    assert latest["checksum"] in two and "Read Search" in two
    assert all(address.startswith("http://127.0.0.1:9210/") for address, _ in calls)
    with session(tmp_path) as http:
        thread = ok(http.get(conversation))
        assert thread["messages"][1]["modelReply"]["agent"]["skill"]["checksum"] == first_version["checksum"]
        assert "source" not in thread["messages"][1]["modelReply"]["agent"]["skill"]
        _, _, other = create_agent(http, name="另一位导师", skills=[])
        change, payload = confirmed(http, route, draft("第三轮", agentId=other["id"]))
        assert [entry["text"] for entry in change["history"] if entry["role"] == "user"] == ["第一轮", "第二轮"]
        assert change["agent"]["skill"] is None
        same_thread = ok(http.post(route + "/send", json=payload))
        assert same_thread["id"] == thread["id"]
        assert same_thread["messages"][:4] == thread["messages"]

def test_reserved_reply_and_automatic_retry_keep_the_original_skill(tmp_path):
    from datetime import UTC, datetime, timedelta
    import json

    from backend.chat_model_worker import dispatch_reply, reserve_reply
    from backend.db import open_db

    calls = []
    route = f"/api/chat-model/{uuid4()}"
    with session(tmp_path) as http:
        setup_model(http)
        first = install(http)
        _, _, agent = create_agent(http)
        _, payload = confirmed(http, route, draft("检查次序", agentId=agent["id"], skill="study-checklist"))
        ok(http.post(route + "/send", json=payload))

        engine = open_db(tmp_path)
        now = datetime.now(UTC)
        try:
            claim = reserve_reply(engine, now)
            assert claim and first["checksum"] in claim.prompt
            second = ok(confirm_update(http, NEW, ok(upload(http, "/preview", NEW))))

            def transport(_address, _headers, payload):
                calls.append(payload["messages"][-1]["content"])
                if len(calls) == 1:
                    return 503, b"{}"
                return 200, json.dumps({"choices": [{"message": {"content": "按原版完成"}}]}).encode()

            dispatch_reply(engine, claim, transport=transport, now=lambda: now)
            retry = reserve_reply(engine, now + timedelta(seconds=10))
            assert retry and retry.id != claim.id and retry.prompt == claim.prompt
            dispatch_reply(engine, retry, transport=transport, now=lambda: now + timedelta(seconds=10))
            assert len(calls) == 2 and all(first["checksum"] in call and second["checksum"] not in call for call in calls)
        finally:
            engine.dispose()


def test_missing_historical_skill_blocks_the_worker_before_dispatch(tmp_path):
    from backend.db import SkillVersion, open_db, transaction

    identity = str(uuid4())
    route = f"/api/chat-model/{identity}"
    with session(tmp_path) as http:
        setup_model(http)
        first = install(http)
        _, _, agent = create_agent(http)
        _, payload = confirmed(http, route, draft("准备试印", agentId=agent["id"], skill="study-checklist"))
        ok(http.post(route + "/send", json=payload))
        engine = open_db(tmp_path)
        try:
            with transaction(engine) as db:
                db.delete(db.get(SkillVersion, ("study-checklist", first["checksum"])))
        finally:
            engine.dispose()
        calls = []
        assert not worker(tmp_path, calls) and not calls
        result = ok(http.get(f"/api/conversations/{identity}"))["messages"][-1]["modelReply"]
        assert result["state"] == "blocked" and result["errorCode"] == "skill_unavailable"


def test_full_backup_restores_agent_grants_and_the_chat_skill_reference(tmp_path):
    from test_backups import accepted, inspect_backup, make_backup
    from test_full_restore import app_client, request_restore, wait_restore

    thread_id = str(uuid4())
    route = f"/api/chat-model/{thread_id}"
    with app_client(tmp_path) as http:
        setup_model(http)
        first = install(http)
        data, _, agent = create_agent(http)
        _, payload = confirmed(http, route, draft("检查工具与版框", agentId=agent["id"], skill="study-checklist"))
        ok(http.post(route + "/send", json=payload))
        backup = make_backup(http)

        change = {**data, "revision": agent["revision"], "skills": []}
        prepared = ok(http.post(AGENTS + "/preview", json=change))
        ok(http.post(AGENTS, json={**change, "confirmation": prepared["confirmation"]}))
        plan = accepted(inspect_backup(http, backup))
        assert plan["counts"]["backup"]["professional_agents"] == 1
        request_restore(http, backup, plan)
        assert wait_restore(http)["state"] == "complete"

        assert ok(http.get(AGENTS + "?skill=study-checklist"))["items"] == [agent]
        receipt = ok(http.get(f"/api/conversations/{thread_id}"))["messages"][-1]["modelReply"]
        assert receipt["state"] == "blocked" and receipt["errorCode"] == "restored"
        assert receipt["agent"]["skill"]["checksum"] == first["checksum"]


def test_fixed_agent_model_is_eligible_for_manual_budget_recovery(tmp_path):
    from test_daily_budget import RECOVERY, SETTINGS

    route = f"/api/chat-model/{uuid4()}"
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        fixed = ok(http.post("/api/models/connections", json={
            "name": "练习专用连接", "provider": "custom", "protocol": "chat-completions",
            "endpoint": "http://127.0.0.1:9240/v1", "model": "fixed-tutor",
        }))
        _, _, agent = create_agent(http, connectionId=fixed["id"], skills=[])
        ok(http.put(SETTINGS, json={"concurrency": 2, "dailyLimit": 1}))
        for index in range(2):
            question = draft(f"第 {index + 1} 轮", agentId=agent["id"])
            _, payload = confirmed(http, route, question)
            ok(http.post(route + "/send", json=payload))
            assert worker(tmp_path, calls) == (index == 0)
        ok(http.put(SETTINGS, json={"concurrency": 2, "dailyLimit": 2}))
        latest = ok(http.get(RECOVERY))
        task = next(item for item in latest["tasks"] if item["id"] == question["id"])
        assert task["kind"] == "chat" and task["canResume"]
        resumed = ok(http.post(RECOVERY, json={
            "billingDay": latest["billingDay"], "tasks": [{"kind": "chat", "id": task["id"]}],
        }))
        assert len(resumed["resumed"]) == 1
        assert worker(tmp_path, calls)
        assert len(calls) == 2 and all(endpoint.startswith("http://127.0.0.1:9240/") for endpoint, _ in calls)

