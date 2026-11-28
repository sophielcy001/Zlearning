import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from backend.app import create_app
from backend.chat_model_worker import dispatch_reply, reserve_reply
from backend.db import ROOT, open_db
from backend.model_study import dispatch, reserve_call
from test_backups import inspect_backup, make_backup
from test_chat_model import confirmed, draft
from test_full_restore import app_client, request_restore, wait_restore
from test_model_study import DRAFT, PLAN, SESSION, consent_request, ok, run


WHEN = datetime(2027, 2, 1, 5, tzinfo=UTC)
LEDGER = "/api/generation/usage"


def ledger_client(folder, transport=None):
    return TestClient(
        create_app(folder, SESSION, now=lambda: WHEN, model_transport=transport),
        base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {SESSION}"},
    )


def connection(http, endpoint="http://127.0.0.1:9109/v1"):
    return ok(http.post("/api/models/connections", json={
        "name": "本机用量测试", "provider": "custom", "protocol": "chat-completions",
        "endpoint": endpoint, "model": "ledger-model",
    }))


def set_pricing(http, model, input_price, output_price, currency="CNY"):
    current = next(item for item in ok(http.get("/api/models"))["connections"] if item["id"] == model["id"])
    return ok(http.put(f"/api/models/connections/{model['id']}/pricing", json={
        "revision": model["revision"],
        "previousPricing": current["pricing"],
        "pricing": {"currency": currency, "inputPerMillion": input_price, "outputPerMillion": output_price},
    }))


def reply(content, usage):
    return 200, json.dumps({"choices": [{"message": {"content": content}}], "usage": usage}).encode()


def test_generation_preserves_each_price_snapshot_and_usage_after_deletion(tmp_path):
    calls = []
    with ledger_client(tmp_path) as http:
        model = connection(http)
        priced = set_pricing(http, model, "2.00", "4")
        assert priced["revision"] == model["revision"]
        ok(http.patch("/api/models/roles", json={"main": model["id"]}))
        endpoint, goal, preview = consent_request(http)
        generation = "/api/projects/" + ok(http.post(endpoint, json={
            **goal, "modelConsent": preview["consent"],
        }))["projectId"] + "/generation"

        def send(address, headers, payload):
            calls.append(payload["model"])
            is_route = len(calls) == 1
            if is_route:
                set_pricing(http, model, "10", "20")
            return reply(json.dumps(PLAN if is_route else DRAFT), {
                "prompt_tokens": 120, "completion_tokens": 90, "total_tokens": 210,
                "cost": 55,
            })

        assert run(tmp_path, send, now=lambda: WHEN)
        assert run(tmp_path, send, now=lambda: WHEN)
        assert len(calls) == 2
        ledger = ok(http.get(LEDGER))
        assert ledger["billingDay"] == "2027-02-01"
        assert ledger["summary"]["currencies"] == [{"currency": "CNY", "actual": None, "estimated": "0.0036"}]
        assert ledger["summary"]["totalTokens"] == 420
        assert ledger["summary"]["unknownCalls"] == 0 and ledger["summary"]["inFlight"] == 0
        recorded = {item["estimatedCost"]["amount"]: item for item in ledger["items"]}
        assert set(recorded) == {"0.0006", "0.003"}
        old = recorded["0.0006"]
        assert old["kind"] == "generation" and old["phase"] == "route"
        assert old["pricing"]["inputPerMillion"] == "2" and old["actualCost"] is None
        assert recorded["0.003"]["phase"] == "draft"
        assert ok(http.get(generation))["usage"]["costs"]["currencies"] == ledger["summary"]["currencies"]

        preview = ok(http.post(generation + "/delete-preview"))
        assert ok(http.post(generation + "/delete", json={"confirmation": preview["confirmation"]}))["state"] == "deleted"
        assert ok(http.get(LEDGER)) == ledger


def test_confirmed_transport_cancellation_releases_cost_and_survives_worker_restart(tmp_path):
    from sqlalchemy import select

    from acceptance.usage import read_usage
    from backend.db import ModelCharge, ModelUse, transaction
    from backend.model_protocols import TransportNotSent
    from backend.model_study import settle_interrupted
    from backend.model_usage import billed_calls, billing_day
    from backend.money_budget import balance

    def decline(*_):
        raise TransportNotSent()

    with ledger_client(tmp_path) as http:
        model = connection(http)
        set_pricing(http, model, "2", "4")
        ok(http.patch("/api/models/roles", json={"main": model["id"]}))
        endpoint, goal, preview = consent_request(http, 1)
        ok(http.post(endpoint, json={**goal, "modelConsent": preview["consent"]}))
        with pytest.raises(TransportNotSent):
            run(tmp_path, decline, now=lambda: WHEN)

        ledger = ok(http.get(LEDGER))
        assert ledger["total"] == ledger["summary"]["unknownCalls"] == 0
        assert ledger["items"] == []
        usage = read_usage(tmp_path / "zhixue.sqlite3", final=True)
        assert usage["计入调用"] == usage["费用未知的调用"] == 0
        assert usage["发送前取消或拦截"] == 1

        engine = open_db(tmp_path)
        try:
            with transaction(engine, write=False) as db:
                call = db.scalars(select(ModelUse)).one()
                charge = db.get(ModelCharge, (ModelUse.__tablename__, call.id))
                assert call.status == "sending" and charge.status == "not_sent" and charge.finished_at is not None
                assert billed_calls(db, billing_day(WHEN)) == 0
                assert balance(db, "CNY").view() == {"settled": "0", "reserved": "0", "unknownCalls": 0}
            settle_interrupted(engine, WHEN)
            with transaction(engine, write=False) as db:
                assert db.scalars(select(ModelUse)).one().status == "not_sent"
        finally:
            engine.dispose()

        assert run(tmp_path, lambda *_: reply(json.dumps(PLAN), {"prompt_tokens": 6, "completion_tokens": 4}), now=lambda: WHEN)
        updated = ok(http.get(LEDGER))
        assert updated["total"] == 1 and updated["summary"]["unknownCalls"] == 0


def test_connection_tests_record_documented_actual_zero_and_unknown_cost(tmp_path):
    usage = iter((
        {"prompt_tokens": 6, "completion_tokens": 2, "cost": 0.012345678, "is_byok": False},
        {"prompt_tokens": 7, "completion_tokens": 3, "cost": 0, "is_byok": False},
        {"prompt_tokens": 8, "completion_tokens": 4, "cost": 0, "is_byok": True},
        {"prompt_tokens": 9, "total_tokens": 18, "cost": 222, "is_byok": False},
        {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.45},
        {"prompt_tokens": 11, "completion_tokens": 4, "is_byok": False, "prompt_tokens_details": {"cache_write_tokens": 5}},
        {"prompt_tokens": 12, "completion_tokens": 4, "prompt_cache_hit_tokens": 4},
    ))

    with ledger_client(tmp_path, lambda *_: reply("OK", next(usage))) as http:
        native = connection(http, "https://openrouter.ai/api/v1")
        unsupported = connection(http, "http://127.0.0.1:9119/v1")
        set_pricing(http, native, "3", "8", "USD")
        set_pricing(http, unsupported, "3", "8", "USD")
        for model in (native, native, native, unsupported, native, native, unsupported):
            status = ok(http.post(f"/api/models/connections/{model['id']}/test", json={"revision": model["revision"]}))
            assert status["status"] == "passed"
        ledger = ok(http.get(LEDGER))
        assert ledger["total"] == 7 and ledger["summary"]["calls"] == 7
        assert ledger["summary"]["unknownCalls"] == 5
        assert ledger["summary"]["currencies"] == [{"currency": "USD", "actual": "0.012345678", "estimated": None}]
        items = {item["inputTokens"]: item for item in ledger["items"]}
        assert all(item["kind"] == "probe" for item in items.values())
        assert items[6]["actualCost"] == {"currency": "USD", "amount": "0.012345678"} and items[6]["estimatedCost"] is None
        assert items[7]["actualCost"] == {"currency": "USD", "amount": "0"}
        assert items[8]["actualCost"] is None and items[8]["estimatedCost"] is None
        assert items[9]["totalTokens"] == 18 and items[9]["estimatedCost"] is None
        assert all(items[index]["actualCost"] is None and items[index]["estimatedCost"] is None for index in (10, 11, 12))


def test_price_change_preserves_authority_and_unsent_claim_does_not_create_charge(tmp_path):
    with ledger_client(tmp_path) as http:
        setup = connection(http)
        ok(http.patch("/api/models/roles", json={"main": setup["id"]}))
        endpoint, goal, confirmation = consent_request(http)
        set_pricing(http, setup, "2", "5")
        study_id = ok(http.post(endpoint, json={**goal, "modelConsent": confirmation["consent"]}))["projectId"]
        engine = open_db(tmp_path)
        try:
            claim = reserve_call(engine, WHEN)
            assert claim
            ok(http.post(f"/api/projects/{study_id}/generation/control", json={"action": "stop"}))

            def unexpectedly_sent(*_):
                raise AssertionError("Stopped claim should never reach transport.")

            dispatch(engine, claim, transport=unexpectedly_sent, now=lambda: WHEN)
        finally:
            engine.dispose()
        assert ok(http.get(LEDGER))["summary"]["calls"] == 0


def test_gemini_reports_thinking_and_only_estimates_simple_pricing(tmp_path):
    def response(metadata):
        return 200, json.dumps({
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}], "usageMetadata": metadata,
        }).encode()

    counters = {"promptTokenCount": 100, "candidatesTokenCount": 10, "thoughtsTokenCount": 20, "totalTokenCount": 130}
    usage = iter((counters, {**counters, "cachedContentTokenCount": 60}))
    with ledger_client(tmp_path, lambda *_: response(next(usage))) as http:
        model = ok(http.post("/api/models/connections", json={
            "name": "本机思考用量", "provider": "custom", "protocol": "gemini-generate-content",
            "endpoint": "http://127.0.0.1:9109/v1beta", "model": "gemini-fixture",
        }))
        set_pricing(http, model, "2", "4")
        for _ in range(2):
            assert ok(http.post(f"/api/models/connections/{model['id']}/test", json={
                "revision": model["revision"],
            }))["usage"]["thinkingTokens"] == 20
        ledger = ok(http.get(LEDGER))
        assert ledger["summary"]["thinkingTokens"] == 40 and ledger["summary"]["totalTokens"] == 260
        assert ledger["summary"]["unknownCalls"] == 1
        assert ledger["summary"]["currencies"] == [{"currency": "CNY", "actual": None, "estimated": "0.00032"}]


def test_upgrade_preserves_0047_ledger_usage_without_guessing_historical_prices(tmp_path):
    recipient = {"id": str(uuid4()), "revision": 1, "name": "旧模型连接", "provider": "custom", "model": "legacy"}
    engine = open_db(tmp_path)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
    try:
        with engine.connect() as db:
            db.exec_driver_sql("BEGIN IMMEDIATE")
            config.attributes["connection"] = db
            command.upgrade(config, "0047_goal_authoring")
            stamp = WHEN.replace(tzinfo=None).isoformat(sep=" ")
            for state, finish, counts in (("passed", stamp, (6, 2)), ("not_sent", stamp, (None, None)),
                                          ("sending", None, (None, None)), ("reserved", None, (None, None))):
                db.exec_driver_sql(
                    "INSERT INTO note_suggestions (id, operation, confirmation, connection, state, created_at, started_at,"
                    " billing_day, finished_at, status, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), "rewrite", "0" * 64, json.dumps(recipient), "done", stamp, stamp,
                     "2027-02-01", finish, state, *counts),
                )
            thread, question, answer = [str(uuid4()) for _ in range(3)]
            db.exec_driver_sql("INSERT INTO conversations (id, title, created_at, updated_at, creation_digest) VALUES (?,?,?,?,?)",
                               (thread, "旧回复重试", stamp, stamp, "1" * 64))
            db.exec_driver_sql("INSERT INTO chat_messages (id, conversation_id, position, role, text, created_at) VALUES (?,?,?,?,?,?)",
                               [(question, thread, 1, "user", "问题", stamp), (answer, thread, 2, "assistant", "旧答案", stamp)])
            db.exec_driver_sql(
                "INSERT INTO chat_replies (id, reply_id, confirmation, recipient, history, state, created_at, retries) VALUES (?,?,?,?,?,?,?,?)",
                (question, answer, "2" * 64, json.dumps({**recipient, "model": "last-retry"}), "[]", "answered", stamp, 1),
            )
            for count in (2, 4):
                db.exec_driver_sql(
                    "INSERT INTO chat_model_calls (id, reply_id, started_at, billing_day, finished_at, status, input_tokens) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid4()), question, stamp, "2027-02-01", stamp, "passed", count),
                )
            db.commit()
    finally:
        engine.dispose()
    with ledger_client(tmp_path) as http:
        ledger = ok(http.get(LEDGER))
        assert ledger["summary"]["calls"] == 4 and ledger["summary"]["inputTokens"] == 12
        assert ledger["summary"]["unknownCalls"] == 4 and ledger["summary"]["currencies"] == []
        assert {item["status"] for item in ledger["items"]} == {"passed", "uncertain"}
        assert all(item["pricing"] is None and item["startedAt"] == "2027-02-01T05:00:00Z" for item in ledger["items"])
        notes = [item for item in ledger["items"] if item["kind"] == "note_ai"]
        assert all(item["recipient"]["model"] == "legacy" for item in notes)
        chats = [item for item in ledger["items"] if item["kind"] == "chat"]
        assert len(chats) == 2 and all(item["recipient"] is None for item in chats)
        assert ok(http.get(LEDGER + "?billingDay=2027-01-31"))["total"] == 0
    assert len(list((tmp_path / "backups/database-upgrades").glob("*.sqlite3"))) == 1


def test_encrypted_backup_and_restore_keep_usage_and_price_snapshots(tmp_path):
    def send(_, __, data):
        prompt = data["messages"][0]["content"]
        result = PLAN if "制定学习路线" in prompt else DRAFT
        return reply(json.dumps(result), {"prompt_tokens": 120, "completion_tokens": 90})

    with app_client(tmp_path, now=lambda: WHEN) as http:
        model = connection(http)
        set_pricing(http, model, "2", "4")
        ok(http.patch("/api/models/roles", json={"main": model["id"]}))
        endpoint, goal, preview = consent_request(http)
        ok(http.post(endpoint, json={**goal, "modelConsent": preview["consent"]}))
        assert run(tmp_path, send, now=lambda: WHEN)
        saved = ok(http.get(LEDGER))
        backup = make_backup(http)
        stale = ok(inspect_backup(http, backup))
        set_pricing(http, model, "10", "20")
        request_restore(http, backup, stale)
        assert wait_restore(http)["code"] == "stale"
        assert ok(http.get("/api/models"))["connections"][0]["pricing"]["inputPerMillion"] == "10"
        assert run(tmp_path, send, now=lambda: WHEN)
        incurred = ok(http.get(LEDGER))
        planned = ok(inspect_backup(http, backup))
        assert planned["counts"]["backup"]["model_charges"] == 1
        request_restore(http, backup, planned)
        assert wait_restore(http)["state"] == "complete"
        assert incurred["total"] == saved["total"] + 1
        assert ok(http.get(LEDGER)) == incurred
        assert ok(http.get("/api/models"))["connections"][0]["pricing"]["inputPerMillion"] == "2"


def test_provider_receipt_survives_a_failed_reply_write(tmp_path):
    with ledger_client(tmp_path) as http:
        model = connection(http)
        set_pricing(http, model, "2", "4")
        ok(http.patch("/api/models/roles", json={"main": model["id"]}))
        route = f"/api/chat-model/{uuid4()}"
        _, payload = confirmed(http, route, draft("测试用量结算"))
        ok(http.post(route + "/send", json=payload))
        engine = open_db(tmp_path)
        try:
            claim = reserve_reply(engine, WHEN)
            assert claim
            with engine.begin() as db:
                db.exec_driver_sql("""
                    CREATE TRIGGER fail_reply_save BEFORE UPDATE ON chat_messages
                    WHEN new.role = 'assistant'
                    BEGIN SELECT raise(ABORT, 'simulated reply write failure'); END
                """)
            with pytest.raises(IntegrityError, match="simulated reply write failure"):
                dispatch_reply(engine, claim, now=lambda: WHEN, transport=lambda *_: reply(
                    "放弃写入但保留用量", {"prompt_tokens": 10, "completion_tokens": 5},
                ))
        finally:
            engine.dispose()
        item, = ok(http.get(LEDGER))["items"]
        assert item["kind"] == "chat" and item["inputTokens"] == 10 and item["finishedAt"] is not None
        assert item["estimatedCost"] == {"currency": "CNY", "amount": "0.00004"}


def test_concurrent_price_edit_keeps_the_newer_amount_without_revoking_authorization(tmp_path):
    with ledger_client(tmp_path) as http:
        model = connection(http)
        endpoint = f"/api/models/connections/{model['id']}/pricing"
        pricing = {"currency": "CNY", "inputPerMillion": "2", "outputPerMillion": "4"}
        payload = {"revision": model["revision"], "previousPricing": None, "pricing": pricing}
        saved = ok(http.put(endpoint, json=payload))
        assert saved["pricing"] == pricing and saved["revision"] == model["revision"]
        assert http.put(endpoint, json={**payload, "pricing": None}).status_code == 409
        assert ok(http.get("/api/models"))["connections"][0]["pricing"] == pricing
