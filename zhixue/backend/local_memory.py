from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field, StringConstraints
from sqlalchemy import func, or_, select

from .db import ChatMessage, LocalMemory, ProfessionalAgent, transaction
from .external_memory import retire_external_memory
from .model_study_input import Input, Revision


MemoryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
MemoryView = Literal["shared", "private", "pending"]


class NewMemory(Input):
    id: UUID
    text: MemoryText
    sourceMessageId: UUID | None = None
    agentId: UUID | None = None


class Versioned(Input):
    revision: Revision


class EditMemory(Versioned):
    text: MemoryText


class Ref(Versioned):
    id: UUID


class Review(Input):
    action: Literal["accept", "reject", "defer"]
    items: list[Ref] = Field(min_length=1, max_length=100)


class ReviewEditItem(Ref):
    text: MemoryText


class ReviewEdits(Input):
    items: list[ReviewEditItem] = Field(min_length=1, max_length=100)


def public_memory(memory):
    source = {"kind": "manual"}
    if memory.source_message_id:
        source = {"kind": "chat", "messageId": memory.source_message_id, "conversationId": memory.conversation_id}
    return {
        "id": memory.id, "text": memory.text, "state": memory.state, "revision": memory.revision,
        "agentId": memory.agent_id, "origin": memory.origin, "source": source, "reviewOn": memory.review_on,
        "updatedAt": memory.updated_at,
    }


def current_memory(db, identity, revision):
    entry = db.get(LocalMemory, str(identity))
    if not entry or entry.state == "deleted" or entry.revision != revision:
        raise HTTPException(409, "记忆已变化，请刷新后重试。")
    return entry


def pending_entries(db, items: list[Ref] | list[ReviewEditItem]) -> list[LocalMemory]:
    if len({item.id for item in items}) != len(items):
        raise HTTPException(422, "请为每条选中的记忆提交一次处理。")
    entries = [current_memory(db, item.id, item.revision) for item in items]
    if any(entry.state != "pending" for entry in entries):
        raise HTTPException(409, "所选记忆的共享确认状态已变化。")
    return entries


def update_content(db, entry: LocalMemory, text: str, clock):
    entry.text = text
    entry.revision += 1
    entry.updated_at = clock
    retire_external_memory(db, entry.id, clock)


def retire(entry):
    entry.state = "deleted"
    entry.text = None
    entry.agent_id = None
    entry.review_on = None


def memory_router(engine, today, now):
    router = APIRouter(prefix="/api/memories")

    @router.get("/summary")
    def summary():
        day = today()
        with transaction(engine, write=False) as db:
            waiting = db.scalar(select(func.count()).select_from(LocalMemory).where(
                LocalMemory.state == "pending", or_(LocalMemory.review_on.is_(None), LocalMemory.review_on <= day),
            ))
            return {"waiting": waiting}

    @router.get("")
    def list_memories(view: MemoryView):
        shared = (LocalMemory.agent_id.is_(None), LocalMemory.state == "active")
        private = (LocalMemory.agent_id.is_not(None), LocalMemory.state.in_(("pending", "active")))
        criteria = {"shared": shared, "private": private, "pending": (LocalMemory.state == "pending",)}
        with transaction(engine, write=False) as db:
            entries = db.scalars(select(LocalMemory).where(*criteria[view]).order_by(
                LocalMemory.updated_at.desc(), LocalMemory.id,
            ))
            return {"items": [public_memory(entry) for entry in entries]}

    @router.post("", status_code=201)
    def add(data: NewMemory):
        identity = str(data.id)
        source_id = str(data.sourceMessageId) if data.sourceMessageId else None
        agent_id = str(data.agentId) if data.agentId else None
        with transaction(engine) as db:
            existing = db.get(LocalMemory, identity)
            if existing:
                if (existing.state != "deleted"
                        and (existing.text, existing.source_message_id, existing.agent_id) == (data.text, source_id, agent_id)):
                    return public_memory(existing)
                raise HTTPException(409, "这条记忆已存在或已停用。")
            if agent_id and not db.get(ProfessionalAgent, agent_id):
                raise HTTPException(409, "所选专业 Agent 已不可用。")
            source = db.get(ChatMessage, source_id) if source_id else None
            if source_id:
                if not source or source.role != "user":
                    raise HTTPException(409, "请选择自己的对话原话作为来源。")
                if db.scalar(select(LocalMemory.id).where(LocalMemory.source_message_id == source_id)):
                    raise HTTPException(409, "此消息已有记忆记录或已停用。")
            entry = LocalMemory(
                id=identity, text=data.text, agent_id=agent_id, origin="explicit",
                state="active" if agent_id else "pending", revision=1,
                source_message_id=source_id, conversation_id=source.conversation_id if source else None,
                updated_at=now(),
            )
            db.add(entry)
            return public_memory(entry)

    @router.get("/{identity}/source")
    def original(identity: UUID):
        with transaction(engine, write=False) as db:
            entry = db.get(LocalMemory, str(identity))
            source = db.get(ChatMessage, entry.source_message_id) if entry and entry.state != "deleted" and entry.source_message_id else None
            if not source or source.role != "user":
                raise HTTPException(404, "找不到这条记忆的对话原话。")
            return {"text": source.text, "conversationId": source.conversation_id}

    @router.put("/{identity}")
    def edit(identity: UUID, data: EditMemory):
        with transaction(engine) as db:
            entry = current_memory(db, identity, data.revision)
            update_content(db, entry, data.text, now())
            return public_memory(entry)

    @router.post("/{identity}/share")
    def request_share(identity: UUID, data: Versioned):
        with transaction(engine) as db:
            entry = current_memory(db, identity, data.revision)
            if not entry.agent_id or entry.state != "active":
                raise HTTPException(409, "请选择仍在使用的 Agent 专属记忆。")
            entry.state = "pending"
            entry.revision += 1
            entry.updated_at = now()
            retire_external_memory(db, entry.id, entry.updated_at)
            return public_memory(entry)

    @router.post("/{identity}/delete")
    def delete(identity: UUID, data: Versioned):
        with transaction(engine) as db:
            entry = current_memory(db, identity, data.revision)
            retire(entry)
            entry.revision += 1
            entry.updated_at = now()
            retire_external_memory(db, entry.id, entry.updated_at)
            return {"id": entry.id}

    @router.patch("/review")
    def edit_pending(data: ReviewEdits):
        with transaction(engine) as db:
            entries = pending_entries(db, data.items)
            changes = [(entry, item.text) for entry, item in zip(entries, data.items, strict=True)
                       if entry.text != item.text]
            clock = now()
            for entry, text in changes:
                update_content(db, entry, text, clock)
            return {"updated": len(changes)}

    @router.post("/review")
    def review(data: Review):
        with transaction(engine) as db:
            entries = pending_entries(db, data.items)
            clock = now()
            next_day = today() + timedelta(days=1)
            for entry in entries:
                entry.revision += 1
                entry.updated_at = clock
                if data.action == "defer":
                    entry.review_on = next_day
                    continue
                entry.review_on = None
                retire_external_memory(db, entry.id, clock)
                if data.action == "accept":
                    entry.agent_id = None
                    entry.state = "active"
                elif entry.agent_id:
                    entry.state = "active"
                else:
                    retire(entry)
            return {"updated": len(entries)}

    return router