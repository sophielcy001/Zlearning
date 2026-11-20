from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from uuid import uuid4

from sqlalchemy import select

from .db import ChatMessage, ChatModelCall, ChatReply, Conversation, transaction
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_failover import TRANSIENT
from .model_protocols import Transport, post_json
from .model_study import settings_view, utc_now, when_utc
from .model_study_input import pinned_connection
from .model_ledger import record_call_usage, request_model
from .model_usage import (
    active_calls, admit_reserved_call, auto_resume_enabled, billed_calls, billing_day,
    daily_capacity, queued_with_daily_recovery,
)
from .teaching_style import style_instruction


MAX_OUTPUT_TOKENS = 1800


@dataclass(frozen=True)
class ChatReplyClaim:
    id: str
    reply_id: str
    provider: str
    protocol: str
    address: str
    model: str
    credential: str | None
    prompt: str


def block(reply: ChatReply, reason: str) -> None:
    reply.state, reply.error, reply.active_call, reply.available_at = "blocked", reason, None, None


def reserve_reply(engine, now: datetime) -> ChatReplyClaim | None:
    with transaction(engine) as db:
        settings = settings_view(db)
        if active_calls(db) >= settings["concurrency"]:
            return None
        clock, day = when_utc(now), billing_day(now)
        used = billed_calls(db, day)
        eligible = queued_with_daily_recovery(
            ChatReply.state, ChatReply.error, ChatReply.daily_blocked_on,
            day, auto_resume_enabled(settings, used),
        )
        pending = db.scalars(select(ChatReply).where(
            eligible, ChatReply.available_at.is_(None) | (ChatReply.available_at <= clock),
        ).order_by(ChatReply.created_at, ChatReply.id)).all()
        for reply in pending:
            connection = pinned_connection(db, "main", reply.recipient)
            if connection is None:
                block(reply, "configuration_changed")
                continue
            if not daily_capacity(settings["dailyLimit"], used):
                reply.daily_blocked_on = day
                block(reply, "daily_budget")
                continue
            identity = str(uuid4())
            db.add(ChatModelCall(id=identity, reply_id=reply.id, started_at=clock, billing_day=day, status="reserved"))
            reply.state, reply.error, reply.active_call, reply.daily_blocked_on = "running", None, identity, None
            question = db.get(ChatMessage, reply.id)
            messages = [*reply.history, {"role": "user", "text": question.text}]
            prompt = (
                "你是智学的学习对话助手。回答最后一条用户问题，参考同一对话的前文。"
                "按问题使用的语言回答。不确定时说明不确定。当前只生成聊天回复。"
                f"{style_instruction(reply.style)}\n对话 JSON\n" + json.dumps(messages, ensure_ascii=False)
            )
            return ChatReplyClaim(identity, reply.id, connection.provider, connection.protocol,
                                  reply.recipient["address"], connection.model, connection.credential_ref, prompt)
    return None


def complete_reply(engine, claim: ChatReplyClaim, outcome: str, text: str | None, usage: dict | None,
                   now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        call = db.get(ChatModelCall, claim.id)
        reply = db.get(ChatReply, claim.reply_id)
        if not call or not reply or call.finished_at is not None or reply.active_call != call.id:
            return
        call.status, call.finished_at = (outcome if sent else "not_sent"), clock
        record_call_usage(call, usage)
        reply.active_call, reply.available_at = None, None
        if outcome == "passed" and text:
            answer = db.get(ChatMessage, reply.reply_id)
            answer.text, answer.created_at = text, clock
            thread = db.get(Conversation, answer.conversation_id)
            thread.updated_at = max(thread.updated_at, clock)
            reply.state, reply.error = "answered", None
        elif sent and outcome in TRANSIENT and reply.retries < 2:
            reply.available_at = clock + timedelta(seconds=2 + 4 * reply.retries)
            reply.retries += 1
            reply.state, reply.error = "queued", outcome
        else:
            block(reply, outcome)


def dispatch_reply(engine, claim: ChatReplyClaim, *, transport: Transport = post_json,
                   vault: CredentialStore | None = None, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_reply(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_reply(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    clock = when_utc(now())
    with transaction(engine) as db:
        call = db.get(ChatModelCall, claim.id)
        reply = db.get(ChatReply, claim.reply_id)
        if not call or not reply or call.status != "reserved" or call.finished_at is not None or reply.active_call != call.id:
            return
        if pinned_connection(db, "main", reply.recipient) is None:
            call.status, call.finished_at = "not_sent", clock
            block(reply, "configuration_changed")
            return
        if issue := admit_reserved_call(db, call, now, claim.prompt, MAX_OUTPUT_TOKENS):
            reply.daily_blocked_on = call.billing_day
            block(reply, issue)
            return
    response = request_model(engine, ChatModelCall, claim.id, claim, key, claim.prompt, MAX_OUTPUT_TOKENS, transport, now)
    outcome, text = response.status, response.text
    if outcome == "passed" and (not text or (key and key in text)):
        outcome, text = "invalid_response", None
    complete_reply(engine, claim, outcome, text, response.usage, now(), sent=True)


def settle_interrupted_replies(engine, now: datetime) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        for call in db.scalars(select(ChatModelCall).where(ChatModelCall.finished_at.is_(None))):
            unsent = call.status == "reserved"
            call.status, call.finished_at = ("not_sent" if unsent else "uncertain"), max(clock, call.started_at)
            reply = db.get(ChatReply, call.reply_id)
            if reply and reply.active_call == call.id:
                if unsent:
                    reply.state, reply.error, reply.active_call = "queued", None, None
                else:
                    block(reply, "uncertain")
