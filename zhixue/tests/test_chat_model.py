from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from backend.db import open_db
from test_model_study import observe_worker, ok, session, setup_model


def draft(text, **options):
    return {"id": str(uuid4()), "text": text, **options}


def confirmed(http, route, question):
    preview = ok(http.post(route + "/preview", json=question))
    return preview, {**question, "confirmation": preview["confirmation"]}


def worker(folder, calls, answer="实测模型回复"):
    from backend.chat_model_worker import dispatch_reply, reserve_reply

    def transport(address, headers, request):
        calls.append((address, request["messages"][-1]["content"]))
        body = {"choices": [{"message": {"content": answer}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 35}}
        return 200, json.dumps(body, ensure_ascii=False).encode()

    engine = open_db(folder)
    try:
        claim = reserve_reply(engine, datetime.now(UTC))
        if claim:
            dispatch_reply(engine, claim, transport=transport)
        return claim is not None
    finally:
        engine.dispose()


def test_direct_conversation_persists_one_reply_and_only_sends_disclosed_history(tmp_path):
    thread_id = str(uuid4())
    route = f"/api/chat-model/{thread_id}"
    messages = f"/api/conversations/{thread_id}"
    sample = draft("第一个问题：哈希索引如何定位记录")
    calls = []
    with session(tmp_path) as http:
        connection = setup_model(http)
        ok(http.post("/api/conversations", json={
            "id": str(uuid4()), "message": draft("不要发送的另一段对话"),
        }))
        preview, payload = confirmed(http, route, sample)
        assert preview["recipient"]["id"] == connection["id"]
        assert preview["history"] == []
        assert http.get(messages).status_code == 404
        assert not worker(tmp_path, calls)
        assert http.post(route + "/send", json={**payload, "text": "未经授权的修改"}).status_code == 409
        queued = ok(http.post(route + "/send", json=payload))
        assert queued["goal"] is None
        assert [message["role"] for message in queued["messages"]] == ["user", "assistant"]
        assert queued["messages"][-1]["modelReply"]["state"] == "queued"
        assert ok(http.post(route + "/send", json=payload)) == queued
    assert worker(tmp_path, calls)
    assert not worker(tmp_path, calls)
    with session(tmp_path) as http:
        saved = ok(http.get(messages))
        assert saved["messages"][-1]["text"] == "实测模型回复"
        assert saved["messages"][-1]["modelReply"]["state"] == "answered"
        followup = draft("继续讲一下冲突时怎么办")
        scope, follow_payload = confirmed(http, route, followup)
        assert scope["history"] == [
            {"role": "user", "text": sample["text"]},
            {"role": "assistant", "text": "实测模型回复"},
        ]
        assert http.post(route + "/send", json={**follow_payload, "includeHistory": False}).status_code == 409
        restricted, follow_payload = confirmed(http, route, {**followup, "includeHistory": False})
        assert restricted["history"] == []
        ok(http.post(route + "/send", json=follow_payload))
        assert worker(tmp_path, calls, "第二次模型回复")
        assert len(calls) == 2
        assert sample["text"] in calls[0][1] and followup["text"] in calls[1][1]
        assert "另一段对话" not in calls[0][1] and sample["text"] not in calls[1][1]
        assert not ok(http.get("/api/conversations"))["total"] == 0
        assert ok(http.post(messages + "/goal"))["id"]


def test_reserved_and_sending_calls_follow_worker_recovery_without_duplicates(tmp_path):
    route = f"/api/chat-model/{uuid4()}"
    question = draft("一条需要在重启后保留的问题")
    with session(tmp_path) as http:
        setup_model(http)
        _, payload = confirmed(http, route, question)
        queued = ok(http.post(route + "/send", json=payload))
        original_ids = [message["id"] for message in queued["messages"]]
        from backend.chat_model_worker import reserve_reply, settle_interrupted_replies
        from backend.db import ChatModelCall, transaction

        engine = open_db(tmp_path)
        clock = datetime.now(UTC)
        try:
            before_send = reserve_reply(engine, clock)
            settle_interrupted_replies(engine, clock + timedelta(seconds=1))
            sending = reserve_reply(engine, clock + timedelta(seconds=2))
            assert before_send and sending and before_send.id != sending.id
            with transaction(engine) as db:
                assert db.get(ChatModelCall, before_send.id).status == "not_sent"
                db.get(ChatModelCall, sending.id).status = "sending"
            settle_interrupted_replies(engine, clock + timedelta(seconds=3))
            assert reserve_reply(engine, clock + timedelta(seconds=4)) is None
        finally:
            engine.dispose()
        state = ok(http.get(f"/api/conversations/{queued['id']}"))["messages"][-1]["modelReply"]
        assert state["state"] == "blocked" and state["errorCode"] == "uncertain"
        _, again = confirmed(http, route, {**question, "retry": True})
        retried = ok(http.post(route + "/send", json=again))
        assert [message["id"] for message in retried["messages"]] == original_ids
        assert retried["messages"][-1]["modelReply"]["state"] == "queued"
        assert http.post(route + "/send", json=payload).status_code == 409
        assert ok(http.post(route + "/send", json=again)) == retried


def test_role_changes_at_confirmation_and_dispatch_require_new_consent(tmp_path):
    route = f"/api/chat-model/{uuid4()}"
    with session(tmp_path) as http:
        first = setup_model(http)
        question = draft("这段问题只发送到确认时的连接。")
        _, submitted = confirmed(http, route, question)
        alternate = ok(http.post("/api/models/connections", json={
            "name": "另一个服务", "provider": "custom", "protocol": "chat-completions",
            "endpoint": "http://127.0.0.1:9210/v1", "model": "different-model",
        }))
        ok(http.patch("/api/models/roles", json={"main": alternate["id"]}))
        assert http.post(route + "/send", json=submitted).status_code == 409
        _, authorized = confirmed(http, route, question)
        saved = ok(http.post(route + "/send", json=authorized))
        from backend.chat_model_worker import dispatch_reply, reserve_reply

        engine = open_db(tmp_path)
        try:
            claim = reserve_reply(engine, datetime.now(UTC))
            assert claim is not None
            ok(http.patch("/api/models/roles", json={"main": first["id"]}))

            def no_send(*_):
                raise AssertionError("Obsolete authorization attempted network I/O")

            dispatch_reply(engine, claim, transport=no_send)
        finally:
            engine.dispose()
        thread = ok(http.get(f"/api/conversations/{saved['id']}"))
        assert thread["messages"][-1]["modelReply"]["errorCode"] == "configuration_changed"
        assert ok(http.get("/api/generation/daily-budget"))["used"] == 0
        preview, submitted = confirmed(http, route, {**question, "retry": True})
        assert preview["recipient"]["id"] == first["id"]
        ok(http.post(route + "/send", json=submitted))
        calls = []
        assert worker(tmp_path, calls)
        assert calls[0][0] == "http://127.0.0.1:9109/v1/chat/completions"


def test_chat_shares_daily_budget_and_restored_work_needs_confirmation(tmp_path):
    route = f"/api/chat-model/{uuid4()}"
    calls = []
    budget = "/api/generation/daily-budget"
    with session(tmp_path) as http:
        setup_model(http)
        ok(http.put("/api/generation/settings", json={"dailyLimit": 1, "concurrency": 2}))
        for text in ("第一轮已经用掉额度。", "第二轮应该暂停等待额度。"):
            question = draft(text)
            _, payload = confirmed(http, route, question)
            thread = ok(http.post(route + "/send", json=payload))
        assert worker(tmp_path, calls)
        assert not worker(tmp_path, calls)
        read = f"/api/conversations/{thread['id']}"
        blocked = ok(http.get(read))["messages"][-1]["modelReply"]
        assert blocked["errorCode"] == "daily_budget"
        assert ok(http.get(budget))["used"] == 1
        ok(http.put("/api/generation/settings", json={"dailyLimit": 2, "concurrency": 2}))
        eligible = ok(http.get(budget))
        logical_task = {"kind": "chat", "id": question["id"]}
        assert eligible["tasks"][0]["conversationId"] == thread["id"]
        recovered = ok(http.post(budget, json={"billingDay": eligible["billingDay"], "tasks": [logical_task]}))
        assert recovered["resumed"] == [logical_task]
        assert worker(tmp_path, calls)
        assert len(calls) == 2

        last = draft("恢复的这轮需要确认。")
        _, payload = confirmed(http, route, last)
        ok(http.post(route + "/send", json=payload))
        from backend.restored_tasks import hold_restored_tasks

        engine = open_db(tmp_path)
        try:
            assert hold_restored_tasks(engine, datetime.now(UTC)) == (1, 0)
        finally:
            engine.dispose()
        ok(http.put("/api/generation/settings", json={"dailyLimit": 3, "concurrency": 2}))
        assert not worker(tmp_path, calls)
        assert ok(http.get(read))["messages"][-1]["modelReply"]["errorCode"] == "restored"
        assert len(calls) == 2

def test_worker_presend_fault_keeps_the_saved_chat_question_and_allows_authorized_retry(tmp_path, monkeypatch):
    from sqlalchemy import select
    from backend.db import ChatModelCall, ModelCharge, transaction

    def raise_local_fault(*_args, **_kwargs):
        raise RuntimeError("injected unexpected chat dispatch fault")

    route = f"/api/chat-model/{uuid4()}"
    question = draft("工作线程出错后请保留这一条问题")
    with session(tmp_path) as http:
        setup_model(http)
        _, payload = confirmed(http, route, question)
        queued = ok(http.post(route + "/send", json=payload))
        conversation = f"/api/conversations/{queued['id']}"

        def blocked():
            saved = ok(http.get(conversation))
            reply = saved["messages"][-1]["modelReply"]
            return saved if reply["state"] == "blocked" else None

        with monkeypatch.context() as patch:
            patch.setattr("backend.worker.dispatch_reply", raise_local_fault)
            observed = observe_worker(tmp_path, blocked, transport=raise_local_fault)
        message_ids = [message["id"] for message in queued["messages"]]
        assert [message["id"] for message in observed["messages"]] == message_ids
        reply = observed["messages"][-1]["modelReply"]
        assert reply["errorCode"] == "dispatch_failed"
        assert "发送前" in reply["error"]
        engine = open_db(tmp_path)
        try:
            with transaction(engine, write=False) as db:
                call, = db.scalars(select(ChatModelCall))
                assert call.status == "not_sent" and call.finished_at is not None
                assert db.get(ModelCharge, (ChatModelCall.__tablename__, call.id)) is None
        finally:
            engine.dispose()

        _, again = confirmed(http, route, {**question, "retry": True})
        retried = ok(http.post(route + "/send", json=again))
        assert [message["id"] for message in retried["messages"]] == message_ids
        calls = []
        assert worker(tmp_path, calls) and not worker(tmp_path, calls)
        assert len(calls) == 1
        assert ok(http.get(conversation))["messages"][-1]["modelReply"]["state"] == "answered"

