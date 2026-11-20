from hmac import compare_digest, digest
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import func, select

from .chat_api import ChatText, MessageInput, append_message, get_thread, linked_course_action, thread_page
from .chat_goals import add_goal_message
from .content import is_model_lesson
from .course_sources import HexDigest
from .db import ChatMessage, Lesson, LessonAssistantPlan, LessonQuestion, transaction
from .lesson_assistant import (
    LessonSelection, assistant_settings, block_question, model_recipient, normalized_selection,
    published_version, question_context, question_view, source_sections,
)
from .model_failover import backup_options
from .model_study import when_utc
from .model_study_input import CallLimit, Input, connection_view
from .teaching_style import teaching_style


class ScopeInput(Input):
    maxCalls: CallLimit | None = None
    allowMainBackup: bool = False


class ScopeConsent(ScopeInput):
    confirmation: HexDigest


class AskInput(Input):
    id: UUID
    text: ChatText
    selection: LessonSelection | None = None


class FavoriteInput(Input):
    favorite: Annotated[bool, Field(strict=True)]


def find_lesson(db, identity: str | None) -> Lesson:
    lesson = db.get(Lesson, identity) if identity else None
    if not lesson or not is_model_lesson(lesson.outline):
        raise HTTPException(404, "找不到这节模型课程。")
    return lesson


def find_question(db, identity: UUID) -> LessonQuestion:
    question = db.get(LessonQuestion, str(identity))
    if not question:
        raise HTTPException(404, "找不到这个课内问题。")
    return question


def sign_scope(secret: bytes, lesson_id: str, plan: LessonAssistantPlan | None, fields: dict) -> str:
    raw = json.dumps(["lesson-assistant", lesson_id, plan.id if plan else None, fields],
                     sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return digest(secret, raw.encode("utf-8"), "sha256").hex()


def lesson_assistant_router(engine, folder: Path, session_token: str, now) -> APIRouter:
    router = APIRouter(prefix="/api")
    secret = session_token.encode("utf-8")

    def scope_fields(db, lesson: Lesson, data: ScopeInput) -> dict:
        recipient = model_recipient(db)
        if recipient is None:
            raise HTTPException(409, "请先配置主模型及凭据。")
        version = published_version(db, lesson)
        if version is None:
            raise HTTPException(409, "请先发布课程的核验正文。")
        sections = source_sections(db, folder, version)
        if not sections:
            raise HTTPException(409, "本节目前没有可用于答疑的核验依据。")
        return {"main": connection_view(recipient), "version": version.version, "revision": version.revision,
                "sections": sections, "maxCalls": data.maxCalls,
                **backup_options(db, recipient, None, allow_main=data.allowMainBackup)}

    @router.get("/lessons/{identity}/assistant")
    def settings(identity: UUID):
        with transaction(engine, write=False) as db:
            lesson = find_lesson(db, str(identity))
            return assistant_settings(db, lesson, db.get(LessonAssistantPlan, lesson.id))

    @router.post("/lessons/{identity}/assistant/preview")
    def preview(identity: UUID, data: ScopeInput):
        with transaction(engine) as db:
            lesson = find_lesson(db, str(identity))
            fields = scope_fields(db, lesson, data)
            plan = db.get(LessonAssistantPlan, lesson.id)
            return {**fields, "confirmation": sign_scope(secret, lesson.id, plan, fields)}

    @router.post("/lessons/{identity}/assistant/authorize")
    def authorize(identity: UUID, data: ScopeConsent):
        with transaction(engine) as db:
            lesson = find_lesson(db, str(identity))
            fields = scope_fields(db, lesson, data)
            old = db.get(LessonAssistantPlan, lesson.id)
            if not compare_digest(data.confirmation, sign_scope(secret, lesson.id, old, fields)):
                raise HTTPException(409, "模型或课程引用已变化，请重新预览。")
            proofs = [section["proof"] for section in fields["sections"]]
            version = fields["version"]
            stable = bool(old and old.version == version and old.revision == fields["revision"]
                          and old.recipient == fields["main"] and old.proofs == proofs)
            plan = db.merge(LessonAssistantPlan(
                lesson_id=lesson.id, id=old.id if stable else str(uuid4()), version=version, revision=fields["revision"],
                recipient=fields["main"], backups=fields.get("backups"), proofs=proofs, max_calls=data.maxCalls,
            ))
            return assistant_settings(db, lesson, plan)

    @router.post("/lessons/{identity}/assistant/revoke")
    def revoke(identity: UUID):
        with transaction(engine) as db:
            lesson = find_lesson(db, str(identity))
            if plan := db.get(LessonAssistantPlan, lesson.id):
                db.delete(plan)
            for record in db.scalars(select(LessonQuestion).where(
                LessonQuestion.lesson_id == lesson.id, LessonQuestion.state == "queued",
            )):
                block_question(record, "authorization_revoked")
            return assistant_settings(db, lesson, None)

    @router.post("/conversations/{identity}/questions", status_code=201)
    def ask(identity: UUID, data: AskInput):
        with transaction(engine) as db:
            thread = get_thread(db, identity)
            lesson = find_lesson(db, thread.lesson_id)
            question_id = str(data.id)
            existing = db.get(ChatMessage, question_id)
            record = db.get(LessonQuestion, question_id) if existing is not None else None
            course_action = linked_course_action(db, thread, data.text) if data.selection is None and record is None else None
            if course_action:
                _, saved = append_message(db, thread, MessageInput(id=data.id, text=data.text), when_utc(now()),
                                          course_action=course_action)
                if saved is None:
                    raise HTTPException(409, "这条消息已有其他内容，请重新发问。")
                return thread_page(db, thread, course_action=saved)
            if existing is not None:
                submitted = data.selection.model_dump() if data.selection else None
                if record is None or existing.conversation_id != thread.id or existing.text != data.text or record.selection != submitted:
                    raise HTTPException(409, "这条消息已有其他内容，请重新发问。")
                return thread_page(db, thread)
            plan = db.get(LessonAssistantPlan, lesson.id)
            if not assistant_settings(db, lesson, plan)["authorized"]:
                raise HTTPException(409, "请确认本节课内答疑的发送范围。")
            version = published_version(db, lesson)
            selected, selected_state = normalized_selection(version, data.selection)
            sections = source_sections(db, folder, version, plan.proofs)
            if not sections:
                raise HTTPException(409, "已授权的课程依据暂不可用，请检查原件。")
            if selected and selected_state == "verified" and not any(
                item["index"] == selected["sectionIndex"] for item in sections
            ):
                raise HTTPException(409, "选段的已核验依据暂不可用。")
            context = question_context(db, folder, lesson, version, sections, thread.id, data.text, selected, selected_state)
            context["teachingStyle"] = teaching_style(db)
            last = db.scalar(select(func.max(ChatMessage.position)).where(ChatMessage.conversation_id == thread.id)) or 0
            clock = when_utc(now())
            reply_id = str(uuid4())
            db.add_all([
                ChatMessage(id=question_id, conversation_id=thread.id, position=last + 1, role="user", text=data.text, created_at=clock),
                ChatMessage(id=reply_id, conversation_id=thread.id, position=last + 2, role="assistant", text="", created_at=clock),
            ])
            db.flush()
            db.add(LessonQuestion(id=question_id, reply_id=reply_id, lesson_id=lesson.id, plan_id=plan.id,
                                  context=context, selection=selected, state="queued", created_at=clock, favorite=False))
            add_goal_message(thread, question_id, data.text)
            thread.updated_at = clock
            return thread_page(db, thread)

    @router.post("/lesson-questions/{identity}/retry")
    def retry(identity: UUID):
        with transaction(engine) as db:
            record = find_question(db, identity)
            message = db.get(ChatMessage, record.id)
            thread = get_thread(db, message.conversation_id)
            if record.state == "blocked":
                lesson = find_lesson(db, record.lesson_id)
                plan = db.get(LessonAssistantPlan, record.lesson_id)
                if not assistant_settings(db, lesson, plan)["authorized"]:
                    raise HTTPException(409, "重新确认本节课内答疑的发送范围后可重试。")
                context = record.context
                if context["revision"] != plan.revision or any(section["proof"] not in plan.proofs for section in context["sections"]):
                    raise HTTPException(409, "原问题关联的课程依据已变更，请打开原文或基于当前版本重新提问。")
                record.plan_id = plan.id
                record.state, record.error, record.available_at = "queued", None, None
            return thread_page(db, thread)

    @router.put("/lesson-questions/{identity}/favorite")
    def favorite(identity: UUID, data: FavoriteInput):
        with transaction(engine) as db:
            record = find_question(db, identity)
            record.favorite = data.favorite
            return question_view(record)

    @router.get("/lessons/{identity}/assistant/favorites")
    def favorites(identity: UUID):
        with transaction(engine, write=False) as db:
            lesson = find_lesson(db, str(identity))
            rows = db.execute(select(LessonQuestion, ChatMessage).join(
                ChatMessage, ChatMessage.id == LessonQuestion.id,
            ).where(LessonQuestion.lesson_id == lesson.id, LessonQuestion.favorite.is_(True))
              .order_by(ChatMessage.created_at.desc(), ChatMessage.id))
            return {"items": [{**question_view(record), "conversationId": message.conversation_id,
                               "position": message.position, "text": message.text}
                              for record, message in rows]}

    return router
