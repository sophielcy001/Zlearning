from hmac import compare_digest, digest as mac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field, StringConstraints, model_validator

from .agent_knowledge import granted_folders, knowledge_scope
from .agent_memory import MAX_SELECTED_MEMORIES, memory_preview
from .chat_api import MessageInput, add_exchange, attach_file, digest, thread_page
from .chat_model import completed_history
from .db import ChatMessage, ChatReply, Conversation, ProfessionalAgent, transaction
from .model_study_input import connection_view, main_connection, usable_connection
from .professional_agent import SkillName, agent_receipt, automatic_memory_connection, chat_agent_scope, selected_model
from .repository_mcp import McpEvidence, evidence_scope
from .teaching_style import teaching_style


class ChatDraft(MessageInput):
    includeHistory: bool = True
    retry: bool = False
    attachments: list[UUID] = Field(default_factory=list, max_length=100)
    agentId: UUID | None = None
    skill: SkillName | None = None
    autoSkill: bool = Field(default=False, exclude_if=lambda value: not value)
    memoryIds: list[UUID] = Field(default_factory=list, max_length=MAX_SELECTED_MEMORIES, exclude_if=lambda values: not values)
    knowledgeQuery: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120, pattern=r"^[^\x00]+$")] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    mcpEvidence: McpEvidence | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def valid_options(self):
        if self.autoSkill and (self.skill or self.retry):
            raise ValueError("自动匹配仅用于新问题，不能同时指定 Skill。")
        if len(set(self.memoryIds)) != len(self.memoryIds):
            raise ValueError("每条记忆只能选择一次。")
        if self.retry and (self.knowledgeQuery is not None or self.mcpEvidence is not None):
            raise ValueError("重新确认回复时不能附带历史检索。可在新问题中重新检索。")
        return self


class ConfirmedChat(ChatDraft):
    confirmation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def model_scope(db, identity: str, draft: ChatDraft, secret: bytes, workspace, now) -> tuple[dict, Conversation | None, ChatReply | None]:
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
    agent_id = str(draft.agentId) if draft.agentId else None
    skill = draft.skill
    if draft.retry:
        saved_agent = previous.agent_scope if previous else None
        original_skill = saved_agent["skill"] if saved_agent else None
        if agent_id or skill:
            raise HTTPException(409, "请用原 Agent 的发送确认重试这条问题。")
        agent_id = saved_agent["id"] if saved_agent else None
        skill = original_skill["name"] if original_skill else None
    agent = chat_agent_scope(db, agent_id, skill, match_question=draft.text if draft.autoSkill else None)
    if draft.knowledgeQuery is not None:
        agent["knowledge"] = knowledge_scope(db, workspace, agent_id, draft.knowledgeQuery)
    if draft.memoryIds:
        memories = memory_preview(db, agent_id, [str(identity) for identity in draft.memoryIds])
        agent["memories"] = memories
    if draft.mcpEvidence is not None:
        agent["mcp"] = evidence_scope(db, agent_id, identity, str(draft.id), draft.mcpEvidence, secret, now)
    if draft.retry and agent and saved_agent.get("skillMode") == "automatic":
        agent["skillMode"] = "automatic"
    connection = selected_model(db, agent["connectionId"] if agent else None)
    if not usable_connection(connection):
        raise HTTPException(409, "所选模型当前不可用，请检查管理中心的模型连接和凭据。")

    if agent and not draft.retry:
        current = db.get(ProfessionalAgent, agent["id"])
        if current.definition.get("autoMemory"):
            agent["autoMemory"] = automatic_memory_connection(db, current) is not None

    history = []
    if draft.includeHistory and thread:
        history = previous.history if previous else completed_history(db, identity)
    recipient = connection_view(connection)
    style = teaching_style(db)
    fingerprint = digest({
        "purpose": "chat-reply", "thread": identity, "new": thread is None,
        "draft": draft.model_dump(mode="json", exclude={"confirmation"}),
        "previous": previous.confirmation if previous else None, "recipient": recipient, "history": history, "style": style,
        "agent": agent,
    })
    return {
        "recipient": recipient, "question": draft.text, "history": history, "style": style,
        **({"agent": agent} if agent else {}),
        "confirmation": mac(secret, fingerprint.encode("ascii"), "sha256").hex(),
    }, thread, previous


def chat_model_router(engine, now, session_token: str, index) -> APIRouter:
    router = APIRouter(prefix="/api/chat-model")
    secret = session_token.encode("utf-8")

    def refresh_knowledge(draft):
        if draft.knowledgeQuery is not None:
            with transaction(engine, write=False) as db:
                granted_folders(db, index.folder, str(draft.agentId) if draft.agentId else None)
            index.refresh()

    @router.get("")
    def availability():
        with transaction(engine, write=False) as db:
            return {"available": usable_connection(main_connection(db))}

    @router.post("/{identity}/preview")
    def preview(identity: UUID, draft: ChatDraft):
        refresh_knowledge(draft)
        with transaction(engine, write=False) as db:
            scope, _, _ = model_scope(db, str(identity), draft, secret, index.folder, now)
            return scope

    @router.post("/{identity}/send", status_code=201)
    def send(identity: UUID, data: ConfirmedChat):
        thread_id = str(identity)
        fingerprint = digest({"thread": thread_id, "submission": data.model_dump(mode="json")})
        if data.knowledgeQuery is not None:
            with transaction(engine, write=False) as db:
                existing = db.get(ChatReply, str(data.id))
                if existing and existing.confirmation == fingerprint:
                    return thread_page(db, db.get(Conversation, thread_id))
            refresh_knowledge(data)
        with transaction(engine) as db:
            existing = db.get(ChatReply, str(data.id))
            if existing and existing.confirmation == fingerprint:
                return thread_page(db, db.get(Conversation, thread_id))

            scope, thread, previous = model_scope(db, thread_id, data, secret, index.folder, now)
            if not compare_digest(scope["confirmation"], data.confirmation):
                raise HTTPException(409, "问题、对话历史、模型或 Agent 发送范围已变更，请重新预览。")
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
            previous.agent_scope = agent_receipt(scope.get("agent"))
            previous.state, previous.retries, previous.error = "queued", 0, None
            previous.active_call, previous.available_at, previous.daily_blocked_on = None, None, None
            thread.updated_at = clock
            return thread_page(db, thread)

    return router
