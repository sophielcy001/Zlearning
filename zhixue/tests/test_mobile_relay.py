from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

from fastapi.testclient import TestClient

from backend.mobile_cipher import decode, encode, open_message, seal_message, token
from backend.mobile_relay import create_relay


ORIGIN = "https://mobile.example.test"


def wait_for_offer(http, path, ticket):
    deadline = monotonic() + 2
    while monotonic() < deadline:
        offer = http.get(f"{path}/offer")
        if offer.status_code == 200:
            assert offer.json() == {"ticket": ticket}
            return
        sleep(0.02)
    raise AssertionError("Desktop rendezvous did not become available")


def test_relay_only_forwards_in_flight_and_releases_the_ticket():
    room, ticket, reply_token = token(), token(), token()
    path = f"/v1/{room}"
    request = bytes(range(128))
    reply = bytes(range(128, 224))
    with TestClient(create_relay(ORIGIN)) as http, ThreadPoolExecutor(max_workers=2) as workers:
        assert http.get(f"{path}/offer").status_code == 404
        assert http.post(f"{path}/exchange/{ticket}", content=request).status_code == 404
        cors = http.options(f"{path}/exchange/{ticket}", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Content-Type",
        })
        assert cors.headers["access-control-allow-origin"] == ORIGIN
        assert http.get(f"{path}/offer", headers={"Origin": "https://elsewhere.example"}).status_code == 403
        desktop = workers.submit(http.post, f"{path}/wait/{ticket}", headers={"X-Reply-Token": reply_token})
        wait_for_offer(http, path, ticket)
        phone = workers.submit(http.post, f"{path}/exchange/{ticket}", content=request, headers={"Origin": ORIGIN})
        assert desktop.result(timeout=2).content == request
        assert http.get(f"{path}/offer").status_code == 404
        assert http.post(f"{path}/answer/{ticket}", content=reply, headers={"X-Reply-Token": token()}).status_code == 404
        http.post(f"{path}/answer/{ticket}", content=reply, headers={"X-Reply-Token": reply_token}).raise_for_status()
        assert phone.result(timeout=2).content == reply
        assert http.post(f"{path}/exchange/{ticket}", content=request).status_code == 404
        assert http.post(f"{path}/answer/{ticket}", content=reply, headers={"X-Reply-Token": reply_token}).status_code == 404
        assert not http.app.state.rendezvous


def test_cipher_binds_each_message_to_live_ticket_direction_and_request():
    import pytest
    from cryptography.exceptions import InvalidTag

    root = decode(token())
    room, ticket = token(), token()
    payload = {"op": "receive", "change": {"content": "手机号端离线笔记 📒"}}
    packet = seal_message(root, room, ticket, "request", payload)
    assert "离线笔记".encode() not in packet
    assert open_message(root, room, ticket, "request", packet) == payload
    for different_root, different_room, different_ticket, direction in [
        (decode(token()), room, ticket, "request"),
        (root, token(), ticket, "request"),
        (root, room, token(), "request"),
        (root, room, ticket, "answer"),
    ]:
        with pytest.raises(InvalidTag):
            open_message(different_root, different_room, different_ticket, direction, packet)
    reply = seal_message(root, room, ticket, "answer", {"status": 200}, packet)
    assert open_message(root, room, ticket, "answer", reply, packet)["status"] == 200
    with pytest.raises(InvalidTag):
        open_message(root, room, ticket, "answer", reply, packet + b"x")
    assert encode(decode(room)) == room


def test_approved_pair_rejects_other_operations_and_disconnects_on_workspace_replace(tmp_path, monkeypatch):
    import json
    from hashlib import sha256
    from urllib.parse import parse_qs, urlsplit
    from uuid import uuid4

    from cryptography.hazmat.primitives.asymmetric import ec

    from backend.app import create_app
    from backend.db import MobileWorkspace, open_db, transaction
    from backend.mobile_cipher import pair_keys, public_key
    from backend.mobile_pairing import DesktopPairing, InviteInput
    from backend.restore_runtime import RestoreRuntime, attach_restore

    from test_demo import BASE_URL, HEADERS
    from test_model_settings import TestVault

    vault = TestVault()
    links = []
    monkeypatch.setattr(DesktopPairing, "listen", lambda self, link: links.append((self, link)))
    app = create_app(tmp_path, "demo-test-session", pairing_credentials=vault)
    runtime = RestoreRuntime(lambda: None, lambda: None)
    attach_restore(app, runtime)
    endpoint = "/api/device-sync"
    with TestClient(app, base_url=BASE_URL, headers=HEADERS) as http:
        for data in ({"relay": "http://external.example", "mobile": ORIGIN}, {"relay": ORIGIN, "mobile": ORIGIN}):
            assert http.post(endpoint + "/invite", json=data).status_code == 422
        view = http.post(endpoint + "/invite", json={"relay": "https://relay.example.test", "mobile": ORIGIN}).json()
        fragment = urlsplit(view["invitation"]["url"]).fragment
        invite = json.loads(decode(parse_qs(fragment.split("?", 1)[1])["pair"][0]))
        desktop, invitation = links[0]
        assert desktop.handle(invitation, {"op": "catalog"})["status"] == 403
        phone = ec.generate_private_key(ec.SECP256R1())
        root, room, code = pair_keys(phone, invite["publicKey"], decode(invite["secret"]), invite["room"])
        result = desktop.handle(invitation, {"op": "pair", "publicKey": public_key(phone), "name": "测试手机"})
        assert result == {"status": 200, "data": {"state": "approval"}}
        pending = http.get(endpoint).json()
        assert pending["candidate"] == {"name": "测试手机", "code": code}
        stale = {"revision": view.get("revision", "A" * 43)}
        assert http.delete(endpoint + "/invite", params=stale).status_code == 409
        assert http.post(endpoint + "/invite", params=stale, json={
            "relay": "https://relay.example.test", "mobile": ORIGIN,
        }).status_code == 409
        assert http.post(endpoint + "/approve", params=stale, json={"code": code}).status_code == 409
        assert http.get(endpoint).json() == pending
        response = http.post(endpoint + "/approve", params={"revision": pending["revision"]}, json={"code": code})
        response.raise_for_status()
        approved = response.json()
        assert approved["paired"] == {"name": "测试手机"}
        assert approved["revision"] != pending["revision"]
        assert http.delete(endpoint, params={"revision": pending["revision"]}).status_code == 409
        assert http.get(endpoint).json() == approved
        _, paired = links[1]
        assert paired.key == root and paired.room == room
        assert invitation.stopped.is_set()
        assert desktop.handle(invitation, {"op": "catalog"})["status"] == 410
        assert desktop.handle(paired, {"op": "catalog"}) == {
            "status": 200, "data": {"workspace": paired.workspace, "lessons": [], "reviews": []},
        }
        assert desktop.handle(paired, {"op": "history"})["status"] == 422
        assert desktop.handle(paired, {"op": "receive", "change": {"kind": "note"}})["status"] == 422
        assert desktop.handle(paired, {"op": "PUT /api/models"})["status"] == 422
        status = {"op": "push-status", "workspace": paired.workspace}
        assert desktop.handle(invitation, status)["status"] == 410
        assert desktop.handle(paired, {**status, "workspace": str(uuid4())})["status"] == 409
        push = desktop.handle(paired, status)["data"]
        assert not push["registered"] and len(decode(push["publicKey"])) == 65
        subscription = {"endpoint": "https://fcm.googleapis.com/fcm/send/fixture",
                        "expirationTime": None, "keys": {"p256dh": public_key(phone), "auth": encode(bytes(16))}}
        register = {**status, "op": "push-subscribe", "subscription": subscription}
        assert desktop.handle(paired, {**register, "subscription": {
            **subscription, "endpoint": "https://local.example.test/notify",
        }})["status"] == 422
        assert desktop.handle(paired, register)["data"]["registered"]
        assert desktop.current_push().pairing == sha256(room.encode("ascii")).hexdigest()
        restored = DesktopPairing(tmp_path, vault, desktop.workspace, desktop.invoke, lambda: None)
        restored.start()
        after_restart = restored.handle(restored.pair, status)["data"]
        assert after_restart["registered"] and after_restart["publicKey"] == push["publicKey"]
        assert len(after_restart["endpointId"]) == 64
        assert restored.current_push().pairing == desktop.current_push().pairing
        restored.close()
        with runtime.condition:
            runtime.blocked = True
        assert desktop.handle(paired, {"op": "catalog"})["status"] == 503
        with runtime.condition:
            runtime.blocked = False
        engine = open_db(tmp_path)
        try:
            with transaction(engine) as db:
                db.get(MobileWorkspace, 1).identity = str(uuid4())
        finally:
            engine.dispose()
        assert desktop.handle(paired, {"op": "catalog"})["status"] == 410
        assert paired.stopped.is_set()
        assert vault.get(desktop.reference) is None
        assert http.get(endpoint).json()["paired"] is None
        replacement = desktop.invite(InviteInput(relay="https://relay.example.test", mobile=ORIGIN))
        assert replacement["invitation"]["url"] != view["invitation"]["url"]
