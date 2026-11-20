from io import BytesIO
import wave

from fastapi.testclient import TestClient
import pytest

from backend.app import create_app


def wav(pcm=b"\x10\x01" * 8000, *, rate=16_000, channels=1):
    file = BytesIO()
    with wave.open(file, "wb") as target:
        target.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        target.writeframes(pcm)
    return file.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, "voice-session"), base_url="http://127.0.0.1") as http:
        yield http


AUTH = {"Authorization": "Bearer voice-session", "Content-Type": "audio/wav"}
ENDPOINT = "/api/voice/transcribe"


def test_voice_requires_local_authenticated_session(client):
    assert client.get("/api/voice").status_code == 401
    assert client.post(ENDPOINT, headers=AUTH | {"Origin": "https://example.test"}, content=wav()).status_code == 403
    reply = client.get("/api/voice", headers=AUTH)
    assert reply.status_code == 200
    assert reply.json()["maxSeconds"] == 60
    assert reply.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("data", [
    b"not a wave file", wav(rate=44100), wav(channels=2), wav()[:-100], wav(b"\x00\x00" * 100),
])
def test_voice_rejects_invalid_pcm_before_recognition(client, data):
    response = client.post(ENDPOINT, headers=AUTH, content=data)
    assert response.status_code == 422 and isinstance(response.json()["detail"], str)


def test_voice_rejects_unsolicited_file_formats_and_limits_body_size(client):
    assert client.post(ENDPOINT, headers=AUTH | {"Content-Type": "application/json"}, content=wav()).status_code == 415
    assert client.post(ENDPOINT, headers=AUTH, content=b"x" * 2_100_000).status_code == 413


def test_voice_passes_pcm_to_local_adapter_without_saving_or_creating_a_message(client, tmp_path, monkeypatch):
    from backend import voice_api

    pcm = b"\x10\x01" * 8000
    called = []
    monkeypatch.setattr(voice_api, "recognizer_available", lambda: True)
    monkeypatch.setattr(voice_api, "transcribe_local", lambda samples, language: called.append((samples, language)) or "填写到草稿")
    before = set(tmp_path.rglob("*"))
    answer = client.post(f"{ENDPOINT}?language=en", headers=AUTH, content=wav(pcm))
    assert answer.status_code == 200 and answer.json() == {"text": "填写到草稿"}
    assert called == [(pcm, "en")]
    assert set(tmp_path.rglob("*")) == before
    assert client.get("/api/conversations", headers=AUTH).json() == {"items": [], "page": 1, "lastPage": 1, "total": 0}
