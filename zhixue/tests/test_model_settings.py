from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.db import open_db
from backend.model_credentials import reserve_credential


API = "/api/models"
TOKEN = "isolated-model-settings-session"
KEY = "test-only-zhixue-provider-marker"
CHAT = {"choices": [{"message": {"content": "OK"}}]}


class TestVault:
    __test__ = False

    def __init__(self):
        self.values = {}
        self.locked = False

    def get(self, reference):
        if self.locked:
            raise OSError(KEY)
        return self.values.get(reference)

    def put(self, reference, key):
        if self.locked:
            raise OSError(key)
        self.values[reference] = key

    def discard(self, reference):
        if self.locked:
            raise OSError(KEY)
        self.values.pop(reference, None)


def open_client(folder, vault, transport=None):
    return TestClient(
        create_app(folder, TOKEN, credential_store=vault, model_transport=transport),
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def settings(**changes):
    return {
        "name": "隔离测试连接",
        "provider": "minimax",
        "protocol": "chat-completions",
        "endpoint": "https://example.invalid/v1",
        "model": "test-model",
        **changes,
    }


def create(http, **changes):
    response = http.post(f"{API}/connections", json=settings(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def change(http, connection, **changes):
    return http.put(
        f"{API}/connections/{connection['id']}",
        json=settings(revision=connection["revision"], **changes),
    )


def probe(http, connection):
    return http.post(
        f"{API}/connections/{connection['id']}/test",
        json={"revision": connection["revision"]},
    )


def delete(http, connection):
    return http.delete(f"{API}/connections/{connection['id']}", params={"revision": connection["revision"]})


def test_presets_and_independent_role_assignments_survive_restart(tmp_path):
    vault = TestVault()
    with open_client(tmp_path, vault) as http:
        initial = http.get(API).json()
        assert initial["defaultProvider"] == "minimax"
        assert [item["id"] for item in initial["providers"]] == [
            "minimax", "openai", "anthropic", "gemini", "deepseek", "qwen", "zhipu", "kimi", "doubao", "custom",
        ]
        assert initial["roles"] == dict.fromkeys(("main", "review", "mainBackup", "reviewBackup"))
        assert initial["connections"] == []

        main = create(http)
        review = create(http, name="独立复核", provider="anthropic", protocol="anthropic-messages")
        assignments = {"main": main["id"], "review": review["id"], "mainBackup": review["id"]}
        assert http.patch(f"{API}/roles", json=assignments).json()["roles"] == {**assignments, "reviewBackup": None}

        missing = http.patch(f"{API}/roles", json={"main": None, "reviewBackup": str(uuid4())})
        assert missing.status_code == 404
        assert delete(http, review).status_code == 409

    with open_client(tmp_path, vault) as http:
        assert http.get(API).json()["roles"] == {**assignments, "reviewBackup": None}
        assert http.patch(f"{API}/roles", json={"review": None, "mainBackup": None}).status_code == 200
        assert delete(http, review).status_code == 200
        assert http.get(API).json()["roles"]["main"] == main["id"]
        assert probe(http, main).json()["status"] == "missing_key"


def test_keys_only_round_trip_through_vault_and_validation_does_not_echo_them(tmp_path, caplog):
    vault = TestVault()
    with open_client(tmp_path, vault) as http:
        for bad_input in (
            settings(endpoint="http://outside.example/v1", apiKey=KEY),
            settings(endpoint=f"https://user:{KEY}@example.invalid", apiKey=KEY),
            settings(endpoint=f"https://example.invalid/v1?key={KEY}", apiKey=KEY),
            settings(apiKey={"value": KEY}),
            settings(apiKey=f"{KEY}\r\ninjected: test"),
        ):
            rejected = http.post(f"{API}/connections", json=bad_input)
            assert rejected.status_code == 422 and KEY not in rejected.text
        bad_json = http.post(
            f"{API}/connections", content=f'{{"apiKey":"{KEY}"', headers={"Content-Type": "application/json"},
        )
        assert bad_json.status_code == 422 and KEY not in bad_json.text

        connection = create(http, apiKey=KEY)
        assert connection["hasCredential"] and list(vault.values.values()) == [KEY]
        assert KEY not in json.dumps(connection) and KEY not in http.get(API).text
        assert capture_files(tmp_path, KEY) == []

        renamed = change(http, connection, name="已重新命名")
        assert renamed.status_code == 200 and renamed.json()["hasCredential"]
        changed_destination = change(http, renamed.json(), endpoint="https://another.invalid/v1")
        assert changed_destination.status_code == 409
        assert http.get(API).json()["connections"][0]["endpoint"] == connection["endpoint"]
        assert change(http, connection, apiKey=None).status_code == 409
        assert delete(http, connection).status_code == 409

        replacement = change(http, renamed.json(), endpoint="https://another.invalid/v1", apiKey="second-test-key")
        assert replacement.status_code == 200
        assert list(vault.values.values()) == ["second-test-key"]
        cleared = change(http, replacement.json(), endpoint="https://another.invalid/v1", apiKey=None)
        assert cleared.status_code == 200 and not cleared.json()["hasCredential"]
        assert vault.values == {}

    assert capture_files(tmp_path, KEY) == [] and KEY not in caplog.text


def capture_files(folder, marker):
    return [path.name for path in folder.rglob("*.sqlite3*") if marker.encode() in path.read_bytes()]


def test_vault_errors_leave_saved_key_intact_and_deleted_keys_retire_on_restart(tmp_path):
    vault = TestVault()
    with open_client(tmp_path, vault) as http:
        connection = create(http, apiKey=KEY)
        vault.locked = True
        failed = change(http, connection, apiKey="replace-when-locked")
        assert failed.status_code == 503 and KEY not in failed.text
        assert http.get(API).json()["connections"] == [connection]
        assert probe(http, connection).json()["status"] == "credential_store"
        deleted = delete(http, connection)
        assert deleted.json() == {"deleted": True, "credentialCleanupPending": True}
        assert list(vault.values.values()) == [KEY]
    vault.locked = False
    with open_client(tmp_path, vault) as http:
        assert http.get(API).json()["connections"] == []
        assert vault.values == {}


def test_restart_retires_an_interrupted_reservation_and_keeps_active_keys(tmp_path):
    vault = TestVault()
    with open_client(tmp_path, vault) as http:
        connection = create(http, apiKey=KEY)

    engine = open_db(tmp_path)
    try:
        orphan = reserve_credential(engine)
        vault.put(orphan, "test-only-interrupted-save")
    finally:
        engine.dispose()

    with open_client(tmp_path, vault) as http:
        assert http.get(API).json()["connections"] == [connection]
        assert list(vault.values.values()) == [KEY]


@pytest.mark.parametrize(("provider", "protocol", "path", "model", "header", "token_field", "result"), [
    ("minimax", "chat-completions", "/v1", "test-model", "Authorization", "max_completion_tokens", CHAT),
    ("openai", "chat-completions", "/v1/chat/completions", "test-model", "Authorization", "max_completion_tokens", CHAT),
    ("deepseek", "chat-completions", "", "test-model", "Authorization", "max_tokens", CHAT),
    ("qwen", "chat-completions", "/compatible-mode/v1", "test-model", "Authorization", "max_tokens", CHAT),
    ("zhipu", "chat-completions", "/api/paas/v4", "test-model", "Authorization", "max_tokens", CHAT),
    ("kimi", "chat-completions", "/v1", "test-model", "Authorization", "max_completion_tokens", CHAT),
    ("doubao", "chat-completions", "/api/v3", "test-model", "Authorization", "max_completion_tokens", CHAT),
    ("anthropic", "anthropic-messages", "", "test-model", "x-api-key", "max_tokens",
     {"content": [{"type": "text", "text": "OK"}]}),
    ("minimax", "anthropic-messages", "/anthropic", "test-model", "Authorization", "max_tokens",
     {"content": [{"type": "text", "text": "OK"}]}),
    ("gemini", "gemini-generate-content", "/v1beta", "models/test-model", "x-goog-api-key", "maxOutputTokens",
     {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}),
])
def test_provider_protocol_contracts(tmp_path, provider, protocol, path, model, header, token_field, result):
    calls = []

    def transport(address, headers, payload):
        calls.append((address, headers, payload))
        return 200, json.dumps(result).encode()

    endpoint = f"https://example.invalid{path}"
    with open_client(tmp_path, TestVault(), transport) as http:
        connection = create(http, apiKey=KEY, provider=provider, protocol=protocol, endpoint=endpoint, model=model)
        assert calls == []
        reply = probe(http, connection).json()
        assert reply["status"] == "passed" and reply["capabilities"] == ["text"]
        assert "OK" not in json.dumps(reply) and KEY not in json.dumps(reply)
        assert len(calls) == 1
        address, headers, payload = calls[0]
        assert address == connection["testAddress"]
        assert headers[header] == ("Bearer " + KEY if header == "Authorization" else KEY)
        if protocol == "chat-completions":
            expected_path = path if path.endswith("/chat/completions") else f"{path}/chat/completions"
            assert address == f"https://example.invalid{expected_path}"
            assert payload["model"] == model and 0 < payload[token_field] <= 512
            assert payload["messages"] == [{"role": "user", "content": "Reply with OK."}]
        elif protocol == "anthropic-messages":
            assert address == f"https://example.invalid{path}/v1/messages"
            assert headers["anthropic-version"] == "2023-06-01"
            assert payload["model"] == model and 0 < payload[token_field] <= 512
            assert payload["messages"] == [{"role": "user", "content": "Reply with OK."}]
        else:
            assert address == "https://example.invalid/v1beta/models/test-model:generateContent"
            assert 0 < payload["generationConfig"][token_field] <= 512
            assert payload["contents"] == [{"role": "user", "parts": [{"text": "Reply with OK."}]}]


def test_response_errors_and_usage_are_sanitized(tmp_path):
    results = iter([
        (401, {"error": {"message": KEY}}),
        (429, {"error": {"code": "insufficient_quota", "message": KEY}}),
        (200, {"choices": [{"message": {"content": "", "reasoning_content": KEY}}]}),
        (200, {**CHAT, "usage": {"prompt_tokens": 6, "completion_tokens": 2, "private": KEY}}),
    ])

    def transport(*_):
        status, response = next(results)
        return status, json.dumps(response).encode()

    with open_client(tmp_path, TestVault(), transport) as http:
        connection = create(http, apiKey=KEY)
        for status in ("unauthorized", "quota", "no_text", "passed"):
            response = probe(http, connection)
            assert KEY not in response.text
            result = response.json()
            assert result["status"] == status
            assert result["capabilities"] == (["text"] if status == "passed" else [])
            assert http.get(API).json()["connections"][0]["test"] == result
        assert result["usage"] == {"inputTokens": 6, "outputTokens": 2}
        updated = change(http, connection, model="another-model")
        assert updated.status_code == 200 and updated.json()["test"] is None


def test_old_confirmation_and_in_flight_result_cannot_apply_to_edited_model(tmp_path):
    entered = Event()
    release = Event()

    def transport(*_):
        entered.set()
        assert release.wait(5)
        return 200, json.dumps(CHAT).encode()

    with open_client(tmp_path, TestVault(), transport) as http, ThreadPoolExecutor(1) as executor:
        connection = create(http, apiKey=KEY)
        future = executor.submit(probe, http, connection)
        try:
            assert entered.wait(5)
            updated = change(http, connection, model="edited-while-testing")
            assert updated.status_code == 200
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 409
        assert probe(http, connection).status_code == 409
        assert http.get(API).json()["connections"][0]["test"] is None


@contextmanager
def serve(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            worker.join()


def test_loopback_probe_does_not_follow_redirect_or_resubmit(tmp_path):
    calls = []

    class Service(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.path, self.headers.get("Authorization"), json.loads(body)))
            if self.path.startswith("/redirect/"):
                self.send_response(307)
                self.send_header("Location", "/redirect-target")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(CHAT).encode())

        def log_message(self, *_):
            pass

    with serve(Service) as address, open_client(tmp_path, TestVault()) as http:
        good = create(http, provider="custom", endpoint=f"{address}/v1", apiKey=KEY)
        moved = create(http, provider="custom", endpoint=f"{address}/redirect", apiKey=KEY)
        assert probe(http, good).json()["status"] == "passed"
        assert probe(http, moved).json()["status"] == "redirect"
    assert [call[0] for call in calls] == ["/v1/chat/completions", "/redirect/chat/completions"]
    assert all(call[1] == f"Bearer {KEY}" for call in calls)
