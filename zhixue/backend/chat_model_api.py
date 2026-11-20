from hmac import compare_digest, digest as mac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field

from .chat_api import MessageInput, add_exchange, attach_file, digest, thread_page
from .chat_model import completed_history
from .db import ChatMessage, ChatReply, Conversation, transaction
from .model_study_input import connection_view, main_connection, usable_connection
from .teaching_style import teaching_style


class ChatDraft(MessageInput):
    includeHistory: bool = True
    retry: bool = False
    attachments: list[UUID] = Field(default_factory=list, max_length=100)


class ConfirmedChat(ChatDraft):
    confirmation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def model_scope(db, identity: str, draft: ChatDraft, secret: bytes) -> tuple[dict, Conversation | None, ChatReply | None]:
    connection = main_connection(db)
    if not usable_connection(connection):
        raise HTTPException(409, "请先在管理中心配置主模型及凭据。")
    thread = db.get(Conversation, identity)
    if thread is not None and thread.lesson_id:
        raise HTTPException(409, "请在课内助手中继续这段课程对话。")
    previous = db.get(ChatReply, str(draft.id))
    question = db.get(ChatMessage, str(draft.id))
    if draft.retry:
        if (previous is None or question is None or question.conversation_id != identity
                or question.text != draft.text or previous.state != "blocked" or draft.attachments):
            raise HTTPException(409, "这条问题的回复状态已变化，请重读对话。")
    elif question is not None:
        raise HTTPException(409, "这条问题已保存，请重读对话。")
    history = []
    if draft.includeHistory and thread:
        history = previous.history if previous else completed_history(db, identity)
    recipient = connection_view(connection)
    style = teaching_style(db)
    fingerprint = digest({
        "purpose": "chat-reply", "thread": identity, "new": thread is None,
        "draft": draft.model_dump(mode="json", exclude={"confirmation"}),
        "previous": previous.confirmation if previous else None, "recipient": recipient, "history": history, "style": style,
    })
    return {
        "recipient": recipient, "question": draft.text, "history": history, "style": style,
        "confirmation": mac(secret, fingerprint.encode("ascii"), "sha256").hex(),
    }, thread, previous


def chat_model_router(engine, now, session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/chat-model")
    secret = session_token.encode("utf-8")

    @router.get("")
    def availability():
        with transaction(engine, write=False) as db:
            return {"available": usable_connection(main_connection(db))}

    @router.post("/{identity}/preview")
    def preview(identity: UUID, draft: ChatDraft):
        with transaction(engine, write=False) as db:
            scope, _, _ = model_scope(db, str(identity), draft, secret)
            return scope

    @router.post("/{identity}/send", status_code=201)
    def send(identity: UUID, data: ConfirmedChat):
        thread_id = str(identity)
        fingerprint = digest({"thread": thread_id, "submission": data.model_dump(mode="json")})
        with transaction(engine) as db:
            existing = db.get(ChatReply, str(data.id))
            if existing and existing.confirmation == fingerprint:
                return thread_page(db, db.get(Conversation, thread_id))

            scope, thread, previous = model_scope(db, thread_id, data, secret)
            if not compare_digest(scope["confirmation"], data.confirmation):
                raise HTTPException(409, "问题、对话历史、教学风格或主模型已变更，请重新预览发送范围。")
            clock = now()
            if thread is None:
                thread = Conversation(id=thread_id, title="新对话", created_at=clock, updated_at=clock,
                                      creation_digest=fingerprint)
                db.add(thread)
                db.flush()
            if previous is None:
                for file in dict.fromkeys(data.attachments):
                    attach_file(db, thread, file, clock)
                question, answer = add_exchange(db, thread, data, clock, role="assistant", reply_text="")
                db.flush()
                previous = ChatReply(id=question.id, reply_id=answer.id, created_at=clock)
                db.add(previous)
            previous.confirmation, previous.recipient, previous.history = fingerprint, scope["recipient"], scope["history"]
            previous.style = scope["style"]
            previous.state, previous.retries, previous.error = "queued", 0, None
            previous.active_call, previous.available_at, previous.daily_blocked_on = None, None, None
            thread.updated_at = clock
            return thread_page(db, thread)

    return router
