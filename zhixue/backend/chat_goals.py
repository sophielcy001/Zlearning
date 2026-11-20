from datetime import date
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from .db import ChatAttachment, ChatMessage, Conversation, ImportedFile, Lesson, Project
from .project_versions import core_fields, goal_field


EXAMPLE_MESSAGE = "我想系统学习 SQLite，从学习记录中筛选数据。"
EXAMPLE_GOAL = {"topic": "SQLite 入门", "outcome": "能读取、筛选和排序本地学习记录", "foundation": "用过表格，第一次写 SQL"}
RECENT_GOAL_MESSAGES = 3
TOPIC_PREFIX = re.compile(r"^(?:我想|想要|想)?\s*(?:深入|系统)?\s*(?:学习|练习|了解|理解|掌握)")


def suggested_topic(text: str) -> str:
    value = re.split(r"[，。！？,!?；;\n]", text, maxsplit=1)[0]
    return TOPIC_PREFIX.sub("", value.strip()).strip()[:180]


def evidence(kind: str, identity: str, label: str, excerpt: str, *, target: str | None = None) -> dict:
    return {"id": f"{kind}:{identity}", "kind": kind, "label": label, "excerpt": excerpt, "target": target}


def terms_for(topic: str) -> list[str]:
    words = [word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9_+#.-]{2,}", topic)]
    return list(dict.fromkeys([topic.casefold(), *words]))[:6]


def nearby_chat(db, conversation_id: str, topic: str) -> list[dict]:
    conditions = [func.instr(func.unicode_fold(ChatMessage.text), term) > 0 for term in terms_for(topic) if len(term) >= 3]
    if not conditions:
        return []
    candidates = db.execute(select(
        ChatMessage.id, ChatMessage.text, Conversation.id.label("thread"), Conversation.title,
    ).join(Conversation, Conversation.id == ChatMessage.conversation_id).where(
        ChatMessage.role == "user", ChatMessage.conversation_id != conversation_id, or_(*conditions),
    ).order_by(ChatMessage.created_at.desc(), ChatMessage.id).limit(16))
    found = {}
    for row in candidates:
        found.setdefault(row.thread, evidence("history", row.id, row.title, row.text[:320], target=row.thread))
        if len(found) >= 2:
            break
    return list(found.values())


def related_progress(db, topic: str) -> dict | None:
    folded = topic.casefold()
    for project_id, title, completed in db.execute(select(
        Project.id, Project.topic, func.count(Lesson.id),
    ).join(Lesson, and_(Lesson.project_id == Project.id, Lesson.learned_on.is_not(None))).where(
        func.length(Project.topic) >= 2, func.instr(folded, func.unicode_fold(Project.topic)) > 0,
    ).group_by(Project.id, Project.topic, Project.last_learned_at).order_by(
        func.length(Project.topic).desc(), Project.last_learned_at.desc(), Project.id,
    ).limit(1)):
        return evidence("project", project_id, title, f"相关项目「{title}」已学 {completed} 节。", target=project_id)
    return None


def add_goal_message(thread: Conversation, identity: str, text: str) -> None:
    goal = thread.goal
    if goal is None:
        return
    latest = evidence("message", identity, "后续说明", text[:320])
    recent = [source["id"] for source in goal["evidence"] if source["kind"] == "message"][:RECENT_GOAL_MESSAGES - 1]
    retained = set(recent).union(identity for field in goal["fields"] for identity in field["evidenceIds"])
    sources = [source for source in goal["evidence"] if source["kind"] != "message" or source["id"] in retained]
    thread.goal = {**goal, "revision": goal["revision"] + 1, "evidence": [latest, *sources]}


def make_goal(db, thread: Conversation, message: ChatMessage, *, example: bool = False,
              previous_messages: list[ChatMessage] | None = None) -> dict:
    source = evidence("message", message.id, "本次学习需求", message.text[:320])
    topic = EXAMPLE_GOAL["topic"] if example else suggested_topic(message.text)
    related = None if example else related_progress(db, topic)
    references = [source]
    if not example:
        previous = previous_messages
        if previous is None:
            previous = db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == thread.id, ChatMessage.role == "user", ChatMessage.position < message.position,
            ).order_by(ChatMessage.position.desc()).limit(RECENT_GOAL_MESSAGES - 1))
        references.extend(evidence("message", item.id, "本对话的先前说明", item.text[:320]) for item in previous)
        references.extend(nearby_chat(db, thread.id, topic))
    if related:
        references.append(related)
    for identity, name in db.execute(select(ImportedFile.id, ImportedFile.name).join(
        ChatAttachment, ChatAttachment.file_id == ImportedFile.id,
    ).where(ChatAttachment.conversation_id == thread.id).order_by(ChatAttachment.added_at, ImportedFile.id)):
        references.append(evidence("file", identity, name, "", target=identity))

    fields = core_fields(EXAMPLE_GOAL if example else {"topic": topic}, "suggestion")
    fields[0].update(origin="suggestion" if example else "inference", evidenceIds=[source["id"]])
    if not example and related:
        fields[2].update(value=related["excerpt"][:300], origin="record", evidenceIds=[related["id"]])

    if not example:
        declared_outcome = re.search(r"(?:希望能够|希望能|希望做到|目标是)([^。；;\n]+)", message.text)
        if declared_outcome:
            fields[1].update(value=declared_outcome[1].strip()[:500], origin="fact", evidenceIds=[source["id"]])
        declared_basis = re.search(r"(?:^|[，。；;\n])\s*(我(?:已经|只)?(?:学过|会用|没学过|第一次接触)[^，。；;\n]+)", message.text)
        if declared_basis:
            fields[2].update(value=declared_basis[1].strip()[:300], origin="fact", evidenceIds=[source["id"]])
        if any(word in message.text for word in ("口语", "听力", "表达", "会话")):
            fields.append(goal_field("practiceScene", "练习场景", 180, "", "suggestion"))
        if any(word in message.text for word in ("考试", "备考", "考证")):
            fields.append(goal_field("deadline", "目标日期", 10, "", "suggestion", field_type="date"))
        if any(word in message.text for word in ("项目", "实战", "制作", "搭建", "开发")):
            fields.append(goal_field("deliverable", "练手成果", 300, "", "suggestion"))
    return {"id": str(uuid4()), "revision": 1, "fields": fields, "evidence": references}


def edit_goal(current: dict, values: dict[str, str], removed: list[str]) -> dict:
    keys = {field["key"] for field in current["fields"]}
    known_sources = {item["id"] for item in current["evidence"]}
    if values.keys() - keys or set(removed) - known_sources:
        raise HTTPException(422, "目标字段或依据已变更，请刷新目标卡。")
    excluded = set(removed)
    fields = []
    for field in current["fields"]:
        value = values.get(field["key"], field["value"]).strip()
        if len(value) > field["limit"]:
            raise HTTPException(422, f"请缩短「{field['label']}」。")
        if field["type"] == "date" and value:
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError:
                raise HTTPException(422, "请检查目标日期。") from None
        if value != field["value"]:
            fields.append({**field, "value": value, "origin": "edited", "evidenceIds": []})
        elif excluded.intersection(field["evidenceIds"]):
            fields.append({**field, "value": "", "origin": "suggestion", "evidenceIds": []})
        else:
            fields.append(field)
    remaining = [item for item in current["evidence"] if item["id"] not in excluded]
    if fields == current["fields"] and remaining == current["evidence"]:
        return current
    return {**current, "revision": current["revision"] + 1, "fields": fields, "evidence": remaining}
