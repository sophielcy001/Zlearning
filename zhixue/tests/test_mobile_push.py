from datetime import datetime, timedelta
from hashlib import sha256
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
import http_ece
import pytest

from backend.db import open_db
from backend.mobile_cipher import decode, encode, public_key
from backend.mobile_push import MobilePush
from backend.reminder_delivery import reserve_push, settle_push
from backend.web_push import PushRegistration, PushSubscription, PushTarget, WebPushSender
from test_backups import accepted
from test_daily_plan import confirm, preview
from test_demo import first_lesson
from test_reminders import action, check, learn, settings
from test_study_report import local_app


def reserve(engine, folder, now, receiver="phone-1"):
    return reserve_push(
        engine, folder, lambda db: now.date() + timedelta(days=int(now.hour >= 16)), now, receiver,
    )


@pytest.mark.parametrize("quiet_between", [False, True], ids=["expired-lease", "through-quiet-hours"])
def test_fixed_appointment_push_survives_restart_and_shares_one_receipt(tmp_path, quiet_between):
    now = datetime(2027, 1, 31, 10, 59)
    engine = open_db(tmp_path)
    try:
        with local_app(tmp_path, lambda: now) as http:
            first_lesson(http, tmp_path)
            plan = accepted(http.get("/api/study-plan"))
            task = plan["days"][0]["tasks"][0]
            change, result = preview(http, task, plan["today"], "19:00")
            accepted(confirm(http, change, result))
            assert reserve(engine, tmp_path, now) is None
            now += timedelta(minutes=1)
            job = reserve(engine, tmp_path, now)
            assert job is not None
            assert job.lesson_id == task["lessonId"]
            assert check(http, "scheduled", day=plan["today"])["reminders"] == []

        now += timedelta(seconds=56)
        with local_app(tmp_path, lambda: now) as http:
            if quiet_between:
                settings(http, quiet={"start": "19:00", "end": "19:02"})
                assert reserve(engine, tmp_path, now) is None
                now += timedelta(seconds=65)
            renewed = reserve(engine, tmp_path, now)
            assert renewed is not None
            assert renewed.id == job.id
            assert renewed.expires_at > job.expires_at
            settle_push(engine, job, delivered=False)
            assert reserve(engine, tmp_path, now) == renewed
            settle_push(engine, job, delivered=True)
            assert check(http)["reminders"] == []
            opened = accepted(http.post("/api/mobile/reminders/check", json={
                "workspace": job.workspace, "trigger": "foreground", "notice": job.id,
            }))
            card, = opened["reminders"]
            assert card["kind"] == "lesson" and card["lessonId"] == task["lessonId"]
            assert accepted(http.get("/api/timer"))["active"] is None
            action(http, card, "dismiss")
            assert reserve(engine, tmp_path, now) is None
    finally:
        engine.dispose()


def test_push_honors_quiet_hours_and_does_not_claim_unrequested_daily_or_tomorrow(tmp_path):
    now = datetime(2027, 1, 30, 10, 30)
    engine = open_db(tmp_path)
    try:
        with local_app(tmp_path, lambda: now) as http:
            lesson = first_lesson(http, tmp_path)
            learn(http, lesson)
            now += timedelta(days=1)
            assert reserve(engine, tmp_path, now) is None
            daily, = check(http)["reminders"]
            settings(http, quiet={"start": "19:00", "end": "19:30"})
            action(http, daily, "in_30_minutes")
            now += timedelta(minutes=30)
            assert reserve(engine, tmp_path, now) is None
            now += timedelta(minutes=30)
            scheduled = reserve(engine, tmp_path, now)
            assert scheduled is not None and scheduled.lesson_id is None
            settle_push(engine, scheduled, delivered=False)
            fallback, = check(http, "scheduled", day="2027-01-31")["reminders"]
            assert fallback["id"] == daily["id"] and fallback["version"] == daily["version"] + 1
            action(http, fallback, "tomorrow")
            now += timedelta(days=1)
            assert reserve(engine, tmp_path, now) is None
            follow_up, = check(http)["reminders"]
            assert follow_up["version"] == fallback["version"] + 1
            action(http, follow_up, "in_30_minutes")
            now += timedelta(minutes=30)
            settings(http, enabled=False)
            assert reserve(engine, tmp_path, now) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("provider_status", [201, 410])
def test_sender_uses_encrypted_web_push_and_releases_a_rejected_claim(tmp_path, provider_status):
    now = datetime(2027, 1, 31, 10, 59)
    browser = ec.generate_private_key(ec.SECP256R1())
    secret = bytes(range(16))
    subscription = PushSubscription(
        endpoint="https://fcm.googleapis.com/fcm/send/fixture",
        keys={"p256dh": public_key(browser), "auth": encode(secret)},
    )
    configured = PushRegistration.create()
    target = PushTarget("fixture", configured.private, subscription, "a" * 64, "https://relay.example.test")
    sender = WebPushSender()
    captured = []
    expired = []

    def receive(method, endpoint, **options):
        assert method == "POST" and endpoint == subscription.endpoint
        assert options["redirect"] is False
        captured.append((options["headers"], options["body"]))
        return SimpleNamespace(status=provider_status, close=lambda: None, release_conn=lambda: None)

    class Pair:
        gate = staticmethod(lambda: None)
        current_push = staticmethod(lambda: target)
        expire_push = staticmethod(expired.append)

    sender.http.request = receive
    engine = open_db(tmp_path)
    try:
        with local_app(tmp_path, lambda: now) as http:
            first_lesson(http, tmp_path)
            plan = accepted(http.get("/api/study-plan"))
            task = plan["days"][0]["tasks"][0]
            change, result = preview(http, task, plan["today"], "19:00")
            accepted(confirm(http, change, result))
            push = MobilePush(Pair(), engine, tmp_path, lambda db: now.date(), lambda: now, sender=sender)
            push.tick()
            assert not captured
            now += timedelta(minutes=1)
            push.tick()
            (headers, body), = captured
            assert headers["Content-Encoding"] == "aes128gcm"
            assert 0 < int(headers["TTL"]) <= 45
            payload = json.loads(http_ece.decrypt(body, private_key=browser, auth_secret=secret, version="aes128gcm"))
            assert payload == {"v": 1, "delivery": payload["delivery"], "workspace": payload["workspace"],
                               "lessonId": task["lessonId"], "pairing": "a" * 64}
            assert headers["Topic"] == payload["delivery"].replace("-", "")
            token, public = headers["Authorization"].removeprefix("vapid t=").split(", k=")
            encoded_header, encoded_claims, signature = token.split(".")
            assert json.loads(decode(encoded_header))["alg"] == "ES256"
            assert json.loads(decode(encoded_claims))["aud"] == "https://fcm.googleapis.com"
            assert public == configured.view()["publicKey"]
            raw = decode(signature)
            verifier = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(public))
            verifier.verify(utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                            f"{encoded_header}.{encoded_claims}".encode(), ec.ECDSA(hashes.SHA256()))
            if provider_status == 201:
                assert not expired and check(http)["reminders"] == []
            else:
                assert expired == [target]
                fallback, = check(http)["reminders"]
                assert fallback["lessonId"] == task["lessonId"]
    finally:
        sender.close()
        engine.dispose()


def test_web_push_retry_replays_the_same_encrypted_delivery_after_lost_confirmation(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class LostConfirmation(Exception):
        pass

    now = datetime(2027, 1, 31, 10, 59)
    browser = ec.generate_private_key(ec.SECP256R1())
    auth = bytes(range(16))
    subscription = PushSubscription(
        endpoint="https://fcm.googleapis.com/fcm/send/restart-fixture",
        keys={"p256dh": public_key(browser), "auth": encode(auth)},
    )
    pairing = sha256(b"notice-fixture-pairing-room").hexdigest()
    target = PushTarget("fixture", PushRegistration.create().private, subscription, pairing, "https://relay.example.test")
    packets = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/subscription"
            packets.append((dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    pair = SimpleNamespace(gate=lambda: None, current_push=lambda: target, expire_push=lambda _: None)
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as provider:
        thread = Thread(target=provider.serve_forever, daemon=True)
        thread.start()

        def send_once(*, lose_confirmation):
            engine = open_db(tmp_path)
            sender = WebPushSender()
            request = sender.http.request

            def route_to_fixture(method, endpoint, **options):
                assert method == "POST" and endpoint == subscription.endpoint
                result = request(method, f"http://127.0.0.1:{provider.server_port}/subscription", **options)
                if lose_confirmation:
                    assert result.status == 201
                    result.close()
                    result.release_conn()
                    raise LostConfirmation()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(sender.http, "request", route_to_fixture)
                try:
                    push = MobilePush(pair, engine, tmp_path, lambda db: now.date(), lambda: now, sender=sender)
                    if lose_confirmation:
                        with pytest.raises(LostConfirmation):
                            push.tick()
                    else:
                        push.tick()
                finally:
                    push.close()
                    engine.dispose()

        try:
            with local_app(tmp_path, lambda: now) as http:
                first_lesson(http, tmp_path)
                plan = accepted(http.get("/api/study-plan"))
                task = plan["days"][0]["tasks"][0]
                change, result = preview(http, task, plan["today"], "19:00")
                accepted(confirm(http, change, result))
                now += timedelta(minutes=1)
                send_once(lose_confirmation=True)

            now += timedelta(seconds=56)
            with local_app(tmp_path, lambda: now) as http:
                send_once(lose_confirmation=False)
                assert len(packets) == 2
                payloads = []
                topics = []
                for headers, ciphertext in packets:
                    assert headers["Content-Encoding"] == "aes128gcm"
                    assert 0 < int(headers["TTL"]) <= 45
                    payloads.append(json.loads(http_ece.decrypt(
                        ciphertext, private_key=browser, auth_secret=auth, version="aes128gcm",
                    )))
                    topics.append(headers["Topic"])
                first, replay = payloads
                assert first == replay and first["lessonId"] == task["lessonId"]
                assert topics == [first["delivery"].replace("-", "")] * 2
                assert packets[0][1] != packets[1][1]
                assert check(http)["reminders"] == []
                opened = accepted(http.post("/api/mobile/reminders/check", json={
                    "workspace": first["workspace"], "notice": first["delivery"], "trigger": "foreground",
                }))
                card, = opened["reminders"]
                assert card["lessonId"] == first["lessonId"]
                assert accepted(http.get("/api/timer"))["active"] is None
                lesson = accepted(http.get("/api/mobile/lessons/" + first["lessonId"]))
                assert lesson["workspace"] == first["workspace"]
                (tmp_path / "push-replay.json").write_text(json.dumps({
                    "payloads": payloads, "topics": topics, "lesson": lesson,
                }, ensure_ascii=False), encoding="utf-8")
        finally:
            provider.shutdown()
            thread.join()
