from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from threading import Event, Thread
from urllib.parse import urlsplit
from uuid import uuid4
import wave

from fastapi.testclient import TestClient
from PIL import Image
import pymupdf
import pytest

from backend import model_protocols, recognition
from backend.app import create_app
from backend.backup_snapshot import DATABASE, read_backup
from test_backups import PASSWORD, inspect_backup, make_backup
from test_full_restore import app_client, request_restore, wait_restore
from test_library import upload
from test_voice import wav


HEADERS = {"Authorization": "Bearer recognition-test"}
NOW = datetime(2026, 11, 17, 14, 30, tzinfo=UTC)


def checked(response):
    assert response.is_success, response.text
    return response.json()


@contextmanager
def endpoint(*, started=None, release=None, voice_usage=None, finish_reason="stop"):
    calls = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.path, self.headers["Content-Type"], body))
            if started is not None:
                started.set()
            if release is not None and not release.wait(8):
                self.send_error(504)
                return
            if self.path.endswith("/audio/transcriptions"):
                response = {"text": "由服务返回的语音草稿"}
                if voice_usage is not None:
                    response["usage"] = voice_usage
            elif self.path.endswith("/chat/completions"):
                response = {"choices": [{"message": {"content": "仅所选页的文字"}, "finish_reason": finish_reason}],
                            "usage": {"prompt_tokens": 25, "completion_tokens": 6}}
            else:
                self.send_error(404)
                return
            result = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as service:
        thread = Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{service.server_port}/v1", calls
        finally:
            service.shutdown()
            thread.join()


def open_client(folder):
    return TestClient(create_app(folder, "recognition-test", now=lambda: NOW),
                      base_url="http://127.0.0.1", headers=HEADERS)


def connection(http, address, model="recognition-test"):
    return checked(http.post("/api/models/connections", json={
        "name": "独立识别接收方", "protocol": "chat-completions", "provider": "custom",
        "endpoint": address, "model": model,
    }))


def consent(http, kind, service):
    path = "/api/recognition/" + kind
    selected = {"connectionId": service["id"]}
    preview = checked(http.post(path + "/preview", json=selected))
    payload = {**selected, "confirmation": preview["confirmation"]}
    granted = checked(http.post(path + "/authorize", json=payload))
    return preview, granted["authorization"], payload


def transcribe(http, approval, recording=None):
    return http.post("/api/voice/external", params={"language": "en", "authorizationId": approval},
                     content=wav() if recording is None else recording, headers={"Content-Type": "audio/wav"})


def multimode_pdf():
    with pymupdf.open() as document:
        for color in [(0, 0, 1), (1, 0, 0)]:
            page = document.new_page(width=120, height=80)
            page.draw_rect(page.rect, color=None, fill=color)
        return document.tobytes()


def test_voice_auth_is_pinned_and_reused_and_revocation_blocks_old_approval(tmp_path, monkeypatch):
    from backend import voice_api

    monkeypatch.setattr(voice_api, "recognizer_available", lambda: True)
    monkeypatch.setattr(voice_api, "transcribe_local", lambda *_: "本机草稿")
    with endpoint() as (address, calls), open_client(tmp_path) as http:
        service = connection(http, address)
        assert checked(http.get("/api/recognition/voice"))["authorization"] is None
        assert transcribe(http, str(uuid4())).status_code == 409
        assert checked(http.post("/api/voice/transcribe", headers={"Content-Type": "audio/wav"}, content=wav()))["text"] == "本机草稿"
        assert not calls

        preview, approval, old_payload = consent(http, "voice", service)
        assert preview["endpoint"] == address + "/audio/transcriptions" and preview["kind"] == "voice"
        assert preview["model"] == "recognition-test"
        assert transcribe(http, approval["id"], b"not a recording").status_code == 422
        assert not calls
        for _ in range(2):
            assert checked(transcribe(http, approval["id"])) == {"text": "由服务返回的语音草稿"}
        assert [item[0] for item in calls] == ["/v1/audio/transcriptions"] * 2
        media = BytesParser(policy=default).parsebytes(b"Content-Type: " + calls[0][1].encode() + b"\r\n\r\n" + calls[0][2])
        parts = {part.get_param("name", header="Content-Disposition"): part.get_payload(decode=True)
                 for part in media.iter_parts()}
        assert parts["model"] == b"recognition-test" and parts["language"] == b"en"
        with wave.open(BytesIO(parts["file"])) as recording:
            assert recording.getframerate() == 16_000 and recording.readframes(8000) == b"\x10\x01" * 8000
        assert checked(http.get("/api/conversations"))["items"] == []

        assert http.delete("/api/recognition/voice").is_success
        assert http.post("/api/recognition/voice/authorize", json=old_payload).status_code == 409
        assert transcribe(http, approval["id"]).status_code == 409 and len(calls) == 2


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_external_ocr_sends_only_requested_page_and_requires_manual_save(tmp_path, finish_reason):
    with endpoint(finish_reason=finish_reason) as (address, calls), open_client(tmp_path) as http:
        service = connection(http, address, "vision-test")
        _, voice, _ = consent(http, "voice", service)
        source = multimode_pdf()
        file = checked(upload(http, "需要保留的原文件.pdf", source))
        original = f"/api/files/{file['id']}"
        revision = checked(http.get(original))["revision"]
        path = original + "/pages/2/ocr"
        request = {"revision": revision, "authorizationId": voice["id"]}
        assert http.post(path + "/external", json=request).status_code == 409
        preview, page, _ = consent(http, "ocr", service)
        assert preview["endpoint"] == address + "/chat/completions"
        request["authorizationId"] = page["id"]
        assert http.post(path + "/external", json={**request, "revision": "0" * 64}).status_code == 409
        assert http.post(original + "/pages/3/ocr/external", json=request).status_code == 404
        assert not calls

        draft = checked(http.post(path + "/external", json=request))
        assert draft == {"text": "仅所选页的文字", "truncated": finish_reason == "length"}
        route, content_type, data = calls[0]
        assert route == "/v1/chat/completions" and content_type == "application/json"
        assert "需要保留的原文件".encode() not in data and b"%PDF" not in data
        parts = json.loads(data)["messages"][0]["content"]
        images = [item["image_url"]["url"] for item in parts if item["type"] == "image_url"]
        assert len(images) == 1
        header, encoded = images[0].split(",", 1)
        assert header == "data:image/png;base64"
        with Image.open(BytesIO(b64decode(encoded))) as image:
            assert image.getpixel((30, 30))[:3] == (255, 0, 0)
        assert checked(http.get("/api/files/search", params={"q": "仅所选页"}))["hits"] == []
        checked(http.put(path, json={"revision": revision, "textRevision": None, "text": "手动校对所选页"}))
        hits = checked(http.get("/api/files/search", params={"q": "校对所选"}))["hits"]
        assert hits[0]["location"]["number"] == 2 and http.get(original + "/original").content == source


def test_recognition_grant_survives_restart_but_changed_recipient_requires_new_consent(tmp_path):
    with endpoint() as (old_address, sent_old), endpoint() as (new_address, sent_new):
        with open_client(tmp_path) as http:
            service = connection(http, old_address)
            _, approval, _ = consent(http, "voice", service)
        with open_client(tmp_path) as http:
            assert checked(http.get("/api/recognition/voice"))["authorization"] == approval
            assert transcribe(http, approval["id"]).is_success
            checked(http.put(f"/api/models/connections/{service['id']}", json={
                "name": service["name"], "protocol": "chat-completions", "provider": "custom",
                "endpoint": new_address, "model": "changed", "revision": service["revision"],
            }))
            assert checked(http.get("/api/recognition/voice"))["authorization"] is None
            assert transcribe(http, approval["id"]).status_code == 409
            assert len(sent_old) == 1 and not sent_new


def test_recognition_respects_shared_count_and_unknown_cost_and_records_usage(tmp_path):
    with endpoint(voice_usage={"type": "duration", "seconds": 1.25}) as (address, calls), open_client(tmp_path) as http:
        _, approval, _ = consent(http, "voice", connection(http, address))
        settings = "/api/generation/settings"
        checked(http.put(settings, json={"concurrency": 2, "dailyLimit": 1, "dailyCostLimit": {"currency": "CNY", "amount": "1"}}))
        response = transcribe(http, approval["id"])
        assert response.status_code == 409 and "金额" in response.json()["detail"]
        assert not calls
        checked(http.put(settings, json={"concurrency": 2, "dailyLimit": 1, "dailyCostLimit": None}))
        assert transcribe(http, approval["id"]).is_success
        assert transcribe(http, approval["id"]).status_code == 409 and len(calls) == 1
        ledger = checked(http.get("/api/generation/usage", params={"billingDay": "2026-11-17"}))
        assert ledger["total"] == 1 and ledger["summary"]["unknownCalls"] == 1
        assert ledger["summary"]["reportedCalls"] == 1 and ledger["summary"]["audioSeconds"] == 1.25
        assert ledger["items"][0]["kind"] == "voice_transcription"
        assert ledger["items"][0]["audioSeconds"] == 1.25
        assert ledger["items"][0]["actualCost"] is None and ledger["items"][0]["estimatedCost"] is None


def test_external_recognition_holds_a_shared_model_slot_until_the_response(tmp_path):
    started, release = Event(), Event()
    with endpoint(started=started, release=release) as (address, calls), open_client(tmp_path) as http:
        service = connection(http, address)
        _, approval, _ = consent(http, "voice", service)
        checked(http.put("/api/generation/settings", json={"dailyLimit": 3, "concurrency": 1}))
        probe = f"/api/models/connections/{service['id']}/test"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(transcribe, http, approval["id"])
            try:
                assert started.wait(5)
                attempt = http.post(probe, json={"revision": service["revision"]})
                assert attempt.status_code == 409 and "并发" in attempt.json()["detail"]
                attempt = transcribe(http, approval["id"])
                assert attempt.status_code == 429 and len(calls) == 1
            finally:
                release.set()
            assert checked(pending.result(5))["text"] == "由服务返回的语音草稿"
        assert http.post(probe, json={"revision": service["revision"]}).is_success
        assert [call[0] for call in calls] == ["/v1/audio/transcriptions", "/v1/chat/completions"]


def test_full_restore_omits_recognition_grants_and_expires_unconfirmed_scope(tmp_path):
    folder = tmp_path / "workspace"
    with endpoint() as (address, calls), app_client(folder, now=lambda: NOW) as http:
        service = connection(http, address)
        _, grant, _ = consent(http, "voice", service)
        selection = {"connectionId": service["id"]}
        unsigned_scope = checked(http.post("/api/recognition/ocr/preview", json=selection))
        archive = make_backup(http)
        database = tmp_path / "archive.sqlite3"
        with database.open("wb") as output:
            read_backup((Path(archive["directory"]) / archive["name"]).resolve(), PASSWORD, extract={DATABASE: output})
        with closing(sqlite3.connect(database)) as db:
            assert db.execute("SELECT count(*) FROM recognition_grants").fetchone() == (0,)
            assert db.execute("SELECT id, endpoint FROM model_connections").fetchall() == [(service["id"], address)]
        assert grant["id"].encode() not in database.read_bytes()
        assert checked(http.get("/api/recognition/voice"))["authorization"] == grant

        request_restore(http, archive, checked(inspect_backup(http, archive)))
        restored = wait_restore(http)
        assert restored["state"] == "complete", restored
        assert checked(http.get("/api/recognition/voice"))["authorization"] is None
        assert transcribe(http, grant["id"]).status_code == 409
        expired = {"confirmation": unsigned_scope["confirmation"], **selection}
        assert http.post("/api/recognition/ocr/authorize", json=expired).status_code == 409
        assert consent(http, "ocr", service)[1]["connectionId"] == service["id"]
        assert not calls


@pytest.mark.parametrize("change", ["revoke", "edit"])
def test_consent_changes_while_connecting_prevent_the_upload(tmp_path, monkeypatch, change):
    connected, release = Event(), Event()
    real_connect = model_protocols.HTTPConnection.connect
    with endpoint() as (address, calls), open_client(tmp_path) as http:
        service = connection(http, address)
        _, grant, _ = consent(http, "voice", service)
        port = urlsplit(address).port

        def paused_connect(client):
            real_connect(client)
            if client.port == port:
                connected.set()
                assert release.wait(6)

        monkeypatch.setattr(model_protocols.HTTPConnection, "connect", paused_connect)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(transcribe, http, grant["id"])
            try:
                assert connected.wait(5)
                if change == "revoke":
                    checked(http.delete("/api/recognition/voice"))
                else:
                    checked(http.put(f"/api/models/connections/{service['id']}", json={
                        "name": service["name"], "protocol": "chat-completions", "provider": "custom",
                        "endpoint": address, "model": "replacement", "revision": service["revision"],
                    }))
                assert not calls
            finally:
                release.set()
            assert pending.result(5).status_code == 409
        assert not calls
        assert checked(http.get("/api/generation/usage?billingDay=2026-11-17"))["total"] == 0


def test_saved_authorization_does_not_extend_to_a_changed_scope(tmp_path, monkeypatch):
    with endpoint() as (address, calls), open_client(tmp_path) as http:
        service = connection(http, address)
        _, grant, previous = consent(http, "voice", service)
        monkeypatch.setitem(recognition.SCOPE, "voice", "改版后的发送范围。")
        assert checked(http.get("/api/recognition/voice"))["authorization"] is None
        assert transcribe(http, grant["id"]).status_code == 409
        assert http.post("/api/recognition/voice/authorize", json=previous).status_code == 409
        assert consent(http, "voice", service)[1]["scope"] == "改版后的发送范围。"
        assert not calls
