from hashlib import sha256
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import or_, select

from .db import LocalMemory, ProfessionalAgent


MemoryScope = Literal["shared", "private"]
MAX_SELECTED_MEMORIES = 4


def granted_memories(db, agent_id, identities=None):
    agent = db.get(ProfessionalAgent, agent_id) if agent_id else None
    scopes = agent.definition.get("memoryScopes", []) if agent else []
    criteria = []
    if "shared" in scopes:
        criteria.append(LocalMemory.agent_id.is_(None) & (LocalMemory.state == "active"))
    if "private" in scopes:
        criteria.append((LocalMemory.agent_id == agent_id) & LocalMemory.state.in_(("active", "pending")))
    if not criteria:
        return []
    query = select(LocalMemory).where(or_(*criteria), LocalMemory.text.is_not(None))
    if identities is not None:
        query = query.where(LocalMemory.id.in_(identities))
    return [
        {"id": record.id, "revision": record.revision, "scope": "private" if record.agent_id else "shared", "text": record.text}
        for record in db.scalars(query.order_by(LocalMemory.updated_at.desc(), LocalMemory.id))
    ]


def selected_memories(db, agent_id, identities):
    available = {entry["id"]: entry for entry in granted_memories(db, agent_id, identities)}
    if len(available) != len(identities):
        return None
    return [available[identity] for identity in identities]


def memory_preview(db, agent_id, identities):
    entries = selected_memories(db, agent_id, identities)
    if entries is None:
        raise HTTPException(409, "所选记忆或 Agent 读取授权已变化，请重新选择。")
    return entries


def memory_receipt(entries):
    return [
        {"id": item["id"], "revision": item["revision"], "scope": item["scope"],
         "checksum": sha256(item["text"].encode("utf-8")).hexdigest()}
        for item in entries
    ]


def confirmed_memory_texts(db, scope):
    saved = scope.get("memories", []) if scope else []
    if not saved:
        return []
    entries = selected_memories(db, scope["id"], [item["id"] for item in saved])
    if entries is None or memory_receipt(entries) != saved:
        return None
    return [item["text"] for item in entries]