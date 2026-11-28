from datetime import UTC, datetime
from uuid import uuid4

from backend.chat_model_worker import dispatch_reply, reserve_reply
from backend.db import open_db
from test_chat_model import confirmed, draft, worker
from test_local_memory import API, add, review
from test_model_study import ok, session, setup_model
from test_professional_agent import AGENTS, create_agent


def available(http, agent):
    return ok(http.get(f"{AGENTS}/{agent['id']}/memories"))["items"]


def test_chat_can_select_only_authorized_memory_and_keeps_it_out_of_future_history(tmp_path):
    identity = str(uuid4())
    route = f"/api/chat-model/{identity}"
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        _, _, agent = create_agent(http, skills=[], memoryScopes=["private", "shared"])
        _, _, other = create_agent(http, name="另一位导师", skills=[], memoryScopes=["private"])
        _, public = add(http, "已确认公共内容")
        _, pending = add(http, "待确认的共享内容")
        _, private = add(http, "自己的专属内容", agentId=agent["id"])
        _, foreign = add(http, "另一位导师的专属内容", agentId=other["id"])
        ok(review(http, "accept", public))
        assert {entry["id"] for entry in available(http, agent)} == {public["id"], private["id"]}
        assert {entry["id"] for entry in available(http, other)} == {foreign["id"]}
        for unauthorized in (pending, foreign):
            assert http.post(route + "/preview", json=draft(
                "怎么开始练习？", agentId=agent["id"], memoryIds=[unauthorized["id"]],
            )).status_code == 409
        answer, payload = confirmed(http, route, draft(
            "怎么开始练习？", agentId=agent["id"], memoryIds=[private["id"]],
        ))
        assert [row["text"] for row in answer["agent"]["memories"]] == [private["text"]]
        assert not worker(tmp_path, calls)
        saved = ok(http.post(route + "/send", json=payload))
        assert "text" not in saved["messages"][-1]["modelReply"]["agent"]["memories"][0]

    assert worker(tmp_path, calls, answer="回答重复了自己的专属内容")
    assert len(calls) == 1
    prompt = calls[0][1]
    assert private["text"] in prompt
    for value in (private["id"], public["text"], pending["text"], foreign["text"]):
        assert value not in prompt
    with session(tmp_path) as http:
        next_scope, _ = confirmed(http, route, draft("继续", agentId=other["id"]))
        assert next_scope["history"] == [] and "memories" not in next_scope["agent"]


def test_edit_rejects_stale_scope_and_revocation_prevents_an_already_reserved_call(tmp_path):
    identity = str(uuid4())
    route = f"/api/chat-model/{identity}"
    with session(tmp_path) as http:
        setup_model(http)
        data, preview, agent = create_agent(http, skills=[], memoryScopes=["private"])
        assert preview["memoryScopes"] == ["private"]
        _, chosen = add(http, "旧记忆", agentId=agent["id"])
        question = draft("安排下一课", agentId=agent["id"], memoryIds=[chosen["id"]])
        _, stale = confirmed(http, route, question)
        ok(http.put(f"{API}/{chosen['id']}", json={"revision": chosen["revision"], "text": "替换后记忆"}))
        assert http.post(route + "/send", json=stale).status_code == 409
        fresh, submission = confirmed(http, route, question)
        assert fresh["agent"]["memories"][0]["text"] == "替换后记忆"
        ok(http.post(route + "/send", json=submission))
        engine = open_db(tmp_path)
        try:
            claim = reserve_reply(engine, datetime.now(UTC))
            assert claim and "替换后记忆" in claim.prompt
            edit = {**data, "memoryScopes": [], "revision": agent["revision"]}
            grant = ok(http.post(AGENTS + "/preview", json=edit))
            ok(http.post(AGENTS, json={**edit, "confirmation": grant["confirmation"]}))
            calls = []
            dispatch_reply(engine, claim, transport=lambda *request: calls.append(request))
            assert not calls
        finally:
            engine.dispose()
        result = ok(http.get(f"/api/conversations/{identity}"))["messages"][-1]["modelReply"]
        assert result["state"] == "blocked" and result["errorCode"] == "memory_changed"


def test_deleted_memory_is_excluded_from_queued_replies_and_pending_share_stays_private(tmp_path):
    route = f"/api/chat-model/{uuid4()}"
    with session(tmp_path) as http:
        setup_model(http)
        _, _, agent = create_agent(http, skills=[], memoryScopes=["private"])
        _, _, reader = create_agent(http, skills=[], memoryScopes=["shared"])
        _, memory = add(http, "暂不公开", agentId=agent["id"])
        request = ok(http.post(API + "/" + memory["id"] + "/share", json={"revision": memory["revision"]}))
        assert [item["id"] for item in available(http, agent)] == [memory["id"]]
        assert available(http, reader) == []
        _, payload = confirmed(http, route, draft("问题", agentId=agent["id"], memoryIds=[memory["id"]]))
        ok(http.post(route + "/send", json=payload))
        ok(http.post(API + "/" + memory["id"] + "/delete", json={"revision": request["revision"]}))
        assert available(http, agent) == []
    calls = []
    assert not worker(tmp_path, calls) and calls == []