from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import create_app
from test_chat_model import confirmed, draft, worker
from test_model_study import SESSION, ok, setup_model
from test_professional_agent import AGENTS, create_agent


API = "/api/repository-mcp"
TOKEN = "isolated-mcp-credential"
TEXT = "先核对规格再安装样品。"


class Vault:
    def __init__(self):
        self.keys = {}

    def get(self, ref):
        return self.keys.get(ref)

    def put(self, ref, token):
        self.keys[ref] = token

    def discard(self, ref):
        self.keys.pop(ref, None)


class Remote:
    def __init__(self):
        self.calls = []
        self.text = TEXT
        self.on_read = lambda: None

    def request(self, method, endpoint, headers, message=None):
        assert headers["Authorization"] == "Bearer " + TOKEN
        self.calls.append((method, endpoint, dict(headers), message))
        github = "githubcopilot.com" in endpoint
        if method == "DELETE":
            assert headers["Mcp-Session-Id"] == "fixture-session"
            return 405, {}, None
        command = message["method"]
        assert method == "POST"
        if command == "initialize":
            assert "Mcp-Session-Id" not in headers
            return 200, {"mcp-session-id": "fixture-session"}, {
                "jsonrpc": "2.0", "id": message["id"], "result": {
                    "protocolVersion": "2025-06-18", "serverInfo": {"name": "fixture", "version": "1.2.3"},
                    "capabilities": {"tools": {}},
                },
            }
        assert headers["Mcp-Session-Id"] == "fixture-session" and headers["MCP-Protocol-Version"] == "2025-06-18"
        if command == "notifications/initialized":
            return 202, {}, None
        if command == "tools/list":
            names = ("issue_read", "pull_request_read", "issue_write") if github else (
                "get_work_item", "get_merge_request", "save_note")
            result = {"tools": [{"name": name} for name in names]}
        else:
            assert command == "tools/call"
            self.on_read()
            result = {"content": [{"type": "text", "text": self.text}]}
        return 200, {}, {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def tools(self):
        return [message["params"] for _, _, _, message in self.calls if message and message["method"] == "tools/call"]


def client(folder, vault, remote):
    return TestClient(create_app(folder, SESSION, repository_mcp_credentials=vault, repository_mcp_transport=remote.request),
                      base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {SESSION}"})


def configure(http, provider="github", repo="example/training", revision=0, *, token=TOKEN, enabled=True):
    return http.put(API + "/" + provider, json={
        "credential": token, "revision": revision, "repositories": [repo], "enabled": enabled,
    })


def read(http, agent, route, question, repository="github:example/training", kind="issue", number=12):
    return http.post(API + "/read", json={
        "agentId": agent["id"], "threadId": route.rsplit("/", 1)[-1], "messageId": question["id"],
        "repository": repository, "kind": kind, "number": number,
    })


def test_repository_evidence_needs_both_grants_and_only_sends_after_chat_confirmation(tmp_path):
    vault, remote = Vault(), Remote()
    with client(tmp_path, vault, remote) as http:
        setup_model(http)
        catalogs = ok(http.get("/api/capabilities?kind=mcp"))["items"]
        assert catalogs == []
        connection = ok(configure(http))
        assert connection["tools"] == ["issue_read", "pull_request_read"] and connection["connected"]
        assert TOKEN not in str(connection)
        assert not remote.tools()
        assert ok(http.get("/api/capabilities?kind=mcp"))["items"][0]["status"] == "installed"

        _, _, plain = create_agent(http, skills=[])
        route = "/api/chat-model/" + str(uuid4())
        question = draft("请解释这个练习步骤。", agentId=plain["id"])
        assert read(http, plain, route, question).status_code == 409
        assert not remote.tools()

        _, preview, agent = create_agent(http, skills=[], mcpRepositories=["github:example/training"])
        assert preview["mcpRepositories"] == ["github:example/training"]
        choices = ok(http.get(AGENTS + f"/{agent['id']}/mcp-repositories"))["items"]
        assert choices[0]["id"] == "github:example/training"
        question["agentId"] = agent["id"]
        assert read(http, agent, route, question, repository="github:someone/private").status_code == 409
        evidence = ok(read(http, agent, route, question))
        assert evidence["excerpt"] == TEXT
        assert evidence["source"] == "https://github.com/example/training/issues/12"
        assert remote.tools() == [{"name": "issue_read", "arguments": {
            "method": "get", "owner": "example", "repo": "training", "issue_number": 12,
        }}]
        assert all(headers["X-MCP-Readonly"] == "true" for _, _, headers, _ in remote.calls)
        assert all("readonly" in endpoint for _, endpoint, _, _ in remote.calls)
        assert http.post(route + "/preview", json={**question, "mcpEvidence": {**evidence, "excerpt": "改写片段"}}).status_code == 409
        assert http.post(route + "/preview", json={**question, "id": str(uuid4()), "mcpEvidence": evidence}).status_code == 409
        sent_scope, payload = confirmed(http, route, {**question, "mcpEvidence": evidence})
        assert sent_scope["agent"]["mcp"]["excerpt"] == TEXT
        assert not worker(tmp_path, [])
        ok(http.post(route + "/send", json=payload))
        calls = []
        assert worker(tmp_path, calls)
        assert TEXT in calls[0][1] and "https://github.com/example/training/issues/12" in calls[0][1]
        assert TOKEN not in calls[0][1]
        assert len(remote.tools()) == 1
        assert TOKEN.encode() not in (tmp_path / "zhixue.sqlite3").read_bytes()

def test_gitlab_reads_only_fixed_project_item_and_does_not_reuse_it_in_history(tmp_path):
    vault, remote = Vault(), Remote()
    with client(tmp_path, vault, remote) as http:
        setup_model(http)
        config = ok(configure(http, "gitlab", "Learning/sandbox/book"))
        assert config["tools"] == ["get_work_item", "get_merge_request"]
        repo = "gitlab:Learning/sandbox/book"
        _, _, agent = create_agent(http, skills=[], mcpRepositories=[repo])
        route = "/api/chat-model/" + str(uuid4())
        question = draft("这个变更要怎样复现练习？", agentId=agent["id"])
        evidence = ok(read(http, agent, route, question, repo, "merge", 7))
        assert evidence["source"] == "https://gitlab.com/Learning/sandbox/book/-/merge_requests/7"
        assert remote.tools() == [{"name": "get_merge_request", "arguments": {
            "project_id": "Learning/sandbox/book", "merge_request_iid": 7,
        }}]
        assert all(headers["X-Gitlab-Enabled-Mcp-Server-Tools"] == "get_work_item,get_merge_request"
                   and endpoint == "https://gitlab.com/api/v4/mcp" for _, endpoint, headers, _ in remote.calls)
        _, payload = confirmed(http, route, {**question, "mcpEvidence": evidence})
        ok(http.post(route + "/send", json=payload))
        assert worker(tmp_path, [], "回复使用了已确认资料。")
        thread = ok(http.get(route.replace("/chat-model/", "/conversations/")))
        assert thread["messages"][-1]["modelReply"]["agent"]["mcp"]["source"] == evidence["source"]
        followup, _ = confirmed(http, route, draft("另一条独立问题。", agentId=agent["id"], includeHistory=True))
        assert followup["history"] == []
        assert read(http, agent, route, question, repo, "issue", 7).status_code == 409


def test_revocation_after_reservation_or_during_read_stops_payload(tmp_path):
    from datetime import UTC, datetime
    from backend.chat_model_worker import dispatch_reply, reserve_reply
    from backend.db import open_db

    vault, remote = Vault(), Remote()
    with client(tmp_path, vault, remote) as http:
        setup_model(http)
        config = ok(configure(http))
        data, _, agent = create_agent(http, skills=[], mcpRepositories=["github:example/training"])
        route = "/api/chat-model/" + str(uuid4())
        question = draft("这是新问题。", agentId=agent["id"])
        evidence = ok(read(http, agent, route, question))
        _, payload = confirmed(http, route, {**question, "mcpEvidence": evidence})
        ok(http.post(route + "/send", json=payload))
        engine = open_db(tmp_path)
        try:
            claim = reserve_reply(engine, datetime.now(UTC))
            assert claim is not None and TEXT in claim.prompt

            changed = {**data, "revision": agent["revision"], "mcpRepositories": []}
            options = ok(http.post(AGENTS + "/preview", json=changed))
            agent = ok(http.post(AGENTS, json={**changed, "confirmation": options["confirmation"]}))
            def forbidden(*_):
                raise AssertionError("revoked evidence was sent")
            dispatch_reply(engine, claim, transport=forbidden)
        finally:
            engine.dispose()
        thread = ok(http.get(route.replace("/chat-model/", "/conversations/")))
        assert thread["messages"][-1]["modelReply"]["errorCode"] == "mcp_changed"
        assert thread["messages"][-1]["text"] == ""
        restored = {**data, "revision": agent["revision"]}
        options = ok(http.post(AGENTS + "/preview", json=restored))
        agent = ok(http.post(AGENTS, json={**restored, "confirmation": options["confirmation"]}))
        next_question = draft("改了连接。", agentId=agent["id"])

        def remove_during_call():
            ok(configure(http, repo="example/other", revision=config["revision"], token=None))

        remote.on_read = remove_during_call
        assert read(http, agent, route, next_question).status_code == 409
        status = ok(http.get(AGENTS))
        assert not any(entry.get("mcpRepositories") for entry in status["items"])
        assert read(http, agent, route, next_question).status_code == 409
        assert len(remote.tools()) == 2


def test_disabling_connection_invalidates_preview_and_hides_chat_choices(tmp_path):
    vault, remote = Vault(), Remote()
    with client(tmp_path, vault, remote) as http:
        setup_model(http)
        config = ok(configure(http))
        _, _, agent = create_agent(http, skills=[], mcpRepositories=["github:example/training"])
        route = "/api/chat-model/" + str(uuid4())
        question = draft("解释阅读材料。", agentId=agent["id"])
        evidence = ok(read(http, agent, route, question))
        _, payload = confirmed(http, route, {**question, "mcpEvidence": evidence})
        ok(configure(http, revision=config["revision"], token=None, enabled=False))
        assert http.post(route + "/send", json=payload).status_code == 409
        assert ok(http.get(AGENTS + f"/{agent['id']}/mcp-repositories"))["items"] == []
        assert not worker(tmp_path, [])
        assert len(remote.tools()) == 1


def test_restore_holds_mcp_grants_and_strips_credentials_from_portable_backup(tmp_path):
    from backend.restore_runtime import RestoreRuntime, attach_restore
    from test_backups import accepted, inspect_backup, make_backup
    from test_full_restore import request_restore, wait_restore

    vault, remote = Vault(), Remote()
    runtime = RestoreRuntime(lambda: None, lambda: None)
    http = client(tmp_path, vault, remote)
    attach_restore(http.app, runtime)
    with http:
        try:
            setup_model(http)
            ok(configure(http))
            _, _, agent = create_agent(http, skills=[], mcpRepositories=["github:example/training"])
            backup = make_backup(http)
            plan = accepted(inspect_backup(http, backup))
            request_restore(http, backup, plan)
            result = wait_restore(http)
            assert result["state"] == "complete", result
            config = next(row for row in ok(http.get(API))["items"] if row["id"] == "github")
            assert not config["enabled"] and not config["connected"]
            assert config["repositories"] == ["example/training"]
            assert ok(http.get(AGENTS + f"/{agent['id']}/mcp-repositories"))["items"] == []
            assert not ok(http.get(AGENTS))["items"][0].get("mcpRepositories")
            assert not remote.tools()
        finally:
            runtime.close()


def test_http_transport_reads_sse_reply_only_and_does_not_follow_redirects():
    import io
    import json
    from unittest.mock import Mock, patch

    from backend.mcp_protocol import ENDPOINTS, request_mcp

    correct = {"jsonrpc": "2.0", "id": 17, "result": {"tools": []}}
    stream = io.BytesIO((
        ": ping\n\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\"}\n\n"
        "data: {\"jsonrpc\":\"2.0\",\"id\":7,\"result\":{}}\n\n"
        f"event: message\ndata: {json.dumps(correct)}\n\n"
    ).encode("utf-8"))
    with patch("backend.mcp_protocol.HTTPSConnection") as tls:
        conn = tls.return_value
        response = Mock(status=200)
        response.getheaders.return_value = [("Content-Type", "text/event-stream")]
        response.readline.side_effect = stream.readline
        conn.getresponse.return_value = response
        assert request_mcp("POST", ENDPOINTS["github"], {}, {"id": 17})[2] == correct
        tls.assert_called_once_with("api.githubcopilot.com", timeout=8)
        response.status = 302
        assert request_mcp("POST", ENDPOINTS["github"], {}, {"id": 17})[0] == 302
        assert tls.call_count == 2
        assert all(call.args[1] == "/mcp/readonly" for call in conn.request.call_args_list)
