from uuid import uuid4

from test_chat_model import confirmed, draft, worker
from test_library_folders import confirm as move, create, preview as inspect_move
from test_library_locations import upload
from test_model_study import ok, session, setup_model
from test_professional_agent import AGENTS, create_agent


def material(http, folder, filename, body):
    identity = upload(http, filename, body.encode())
    change = {"fileId": identity, "source": f"library/imports/{identity}.md", "parent": folder, "name": filename}
    assert move(http, change, inspect_move(http, change)).is_success
    return identity


def test_agent_knowledge_only_sends_confirmed_snippets_in_granted_directory(tmp_path):
    calls = []
    identity = str(uuid4())
    route = f"/api/chat-model/{identity}"
    with session(tmp_path) as http:
        setup_model(http)
        allowed = create(http, "试印")
        child = create(http, "课堂", allowed)
        denied = create(http, "试印备份")
        local_id = material(http, child, "检查.md", "试印检查：使用局部证据")
        material(http, denied, "私有.md", "试印检查：保密证据")
        _, _, agent = create_agent(http, skills=[], knowledgeFolders=[allowed])
        _, _, other = create_agent(http, name="无授权导师", skills=[])
        assert agent["knowledgeFolders"] == [allowed]
        message = draft("如何验收？", agentId=agent["id"])
        plain, _ = confirmed(http, route, message)
        assert "knowledge" not in plain["agent"]
        for changes in ({}, {"agentId": other["id"]}):
            assert http.post(route + "/preview", json=draft("问题", knowledgeQuery="试印检查", **changes)).status_code == 409
        preview, payload = confirmed(http, route, {**message, "knowledgeQuery": "试印检查"})
        excerpts = preview["agent"]["knowledge"]["excerpts"]
        assert [(item["id"], item["excerpt"]) for item in excerpts] == [(local_id, "试印检查：使用局部证据")]
        assert not worker(tmp_path, calls)
        ok(http.post(route + "/send", json=payload))
    assert worker(tmp_path, calls, answer="已使用局部证据")
    assert len(calls) == 1 and "使用局部证据" in calls[0][1]
    assert all(value not in calls[0][1] for value in ("保密证据", child, local_id))
    with session(tmp_path) as http:
        reply = ok(http.get(f"/api/conversations/{identity}"))["messages"][-1]["modelReply"]
        assert reply["agent"]["knowledge"]["excerpts"][0]["id"] == local_id
        scope, _ = confirmed(http, route, draft("继续讨论"))
        assert scope["history"] == []


def test_changed_source_invalidates_preview_and_revocation_blocks_queued_send(tmp_path):
    calls = []
    identity = str(uuid4())
    route = f"/api/chat-model/{identity}"
    with session(tmp_path) as http:
        setup_model(http)
        folder = create(http, "可读目录")
        material(http, folder, "练习.md", "练习依据一")
        data, _, agent = create_agent(http, skills=[], knowledgeFolders=[folder])
        message = draft("根据作业回答", agentId=agent["id"], knowledgeQuery="练习依据")
        _, stale = confirmed(http, route, message)
        (tmp_path / folder / "练习.md").write_text("练习依据二", encoding="utf-8")
        assert http.post(route + "/send", json=stale).status_code == 409
        scope, available = confirmed(http, route, message)
        assert scope["agent"]["knowledge"]["excerpts"][0]["excerpt"] == "练习依据二"
        ok(http.post(route + "/send", json=available))
        changed = {**data, "revision": agent["revision"], "knowledgeFolders": []}
        prepared = ok(http.post(AGENTS + "/preview", json=changed))
        ok(http.post(AGENTS, json={**changed, "confirmation": prepared["confirmation"]}))
    worker(tmp_path, calls)
    assert calls == []
    with session(tmp_path) as http:
        result = ok(http.get(f"/api/conversations/{identity}"))["messages"][-1]["modelReply"]
        assert result["state"] == "blocked" and result["errorCode"] == "knowledge_changed"
        assert http.post(route + "/send", json=available).status_code == 201

        preview, new_retry = confirmed(http, route, draft(
            message["text"], id=message["id"], retry=True,
        ))
        assert "knowledge" not in preview["agent"]
        ok(http.post(route + "/send", json=new_retry))
    worker(tmp_path, calls)
    assert len(calls) == 1 and "练习依据" not in calls[0][1]


def test_move_after_confirmation_blocks_sending_original_excerpt(tmp_path):
    identity = str(uuid4())
    route = f"/api/chat-model/{identity}"
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        grant = create(http, "甲")
        outside = create(http, "甲的备份")
        source = material(http, grant, "来源.md", "该依据原先可见")
        _, _, agent = create_agent(http, skills=[], knowledgeFolders=[grant])
        _, request = confirmed(http, route, draft("回答问题", agentId=agent["id"], knowledgeQuery="该依据"))
        ok(http.post(route + "/send", json=request))
        change = {"fileId": source, "source": grant + "/来源.md", "parent": outside, "name": "来源.md"}
        ok(move(http, change, inspect_move(http, change)))
        assert worker(tmp_path, calls) and calls == []
        result = ok(http.get(f"/api/conversations/{identity}"))["messages"][-1]["modelReply"]
        assert result["state"] == "blocked" and result["errorCode"] == "knowledge_changed"


def test_agent_config_refuses_malformed_folder_and_missing_grant_path(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        data, _, _ = create_agent(http, skills=[])
        new = {**data, "id": str(uuid4())}
        for path in ("library/../秘密", "library\\密码", "outside", "library/缺失"):
            response = http.post(AGENTS + "/preview", json={**new, "knowledgeFolders": [path]})
            assert response.status_code == 409, response.text