from hmac import compare_digest, digest
import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete

from .course_review import review_connection, same_model
from .course_sources import HexDigest
from .db import NoteCheckBatch, NoteCheckPlan, transaction
from .model_study import settings_view, when_utc
from .model_study_input import Input, connection_view, main_connection, usable_connection
from .model_usage import billed_calls, billing_day, daily_capacity
from .money_budget import MONEY_ERRORS
from .note_check_worker import note_check_amount_issue
from .note_checks import CHARS_PER_CALL, ISSUES, check_view, course_for_check, pieces, revision_changed
from .notes import version_data
from .notes_api import open_note


class ReviewScope(Input):
    lessonId: UUID | None = None


class ReviewConsent(ReviewScope):
    confirmation: HexDigest


def note_checks_router(engine, folder: Path, session_token: str, project_moment, now) -> APIRouter:
    router = APIRouter(prefix="/api/notes/{identity}/checks")
    secret = session_token.encode()

    def disclose(db, note_id: str, revision: str | None, data: ReviewScope):
        model = review_connection(db)
        if not usable_connection(model):
            raise HTTPException(409, "请先在 API 与模型设置中配置复核模型。")
        recipient = connection_view(model)
        if (primary := main_connection(db)) is not None and same_model(recipient, connection_view(primary)):
            raise HTTPException(409, "请配置一条独立的复核模型连接。")
        body = version_data(db, note_id, revision)
        text = body["content"] if body else ""
        lesson_id = str(data.lessonId) if data.lessonId else None
        course = course_for_check(db, folder, note_id, lesson_id) if lesson_id else None
        signed = json.dumps(["note-check", note_id, revision, recipient, course],
                            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {"recipient": recipient, "course": course, "text": text, "revision": revision,
                "confirmation": digest(secret, signed.encode(), "sha256").hex(),
                "currentCalls": sum(1 for _ in pieces(text)), "charactersPerCall": CHARS_PER_CALL}

    def discard_queued(db, plan_id: str):
        db.execute(delete(NoteCheckBatch).where(NoteCheckBatch.plan_id == plan_id, NoteCheckBatch.state == "queued"))

    def current_plan(db, identity: UUID, plan_id: UUID):
        plan = db.get(NoteCheckPlan, str(identity))
        if plan is None or plan.id != str(plan_id):
            raise HTTPException(409, "后台检查设置已变化，请刷新后再操作。")
        return plan

    @router.get("")
    def state(identity: UUID):
        with transaction(engine, write=False) as db:
            return check_view(db, db.get(NoteCheckPlan, str(identity)))

    @router.post("/preview")
    def preview(identity: UUID, data: ReviewScope):
        with open_note(engine, folder, project_moment, str(identity)) as (db, note, revision, _path):
            return disclose(db, note.id, revision, data)

    @router.post("/authorize")
    def authorize(identity: UUID, data: ReviewConsent):
        with open_note(engine, folder, project_moment, str(identity)) as (db, note, revision, _path):
            scope = disclose(db, note.id, revision, data)
            if not compare_digest(scope["confirmation"], data.confirmation):
                raise HTTPException(409, "笔记版本或发送范围已变化，请重新查看。")
            plan = db.get(NoteCheckPlan, note.id)
            if plan and plan.confirmation == data.confirmation and not plan.problem and check_view(db, plan)["state"] != "partial":
                return check_view(db, plan)
            if plan:
                discard_queued(db, plan.id)
            else:
                plan = NoteCheckPlan(note_id=note.id, budget_scope_id=str(uuid4()))
                db.add(plan)
            plan.id, plan.confirmation = str(uuid4()), data.confirmation
            plan.recipient, plan.course = scope["recipient"], scope["course"]
            plan.lesson_id = str(data.lessonId) if data.lessonId else None
            plan.problem, plan.blocked_on, plan.budget_reason, plan.revision = None, None, None, None
            revision_changed(db, plan, revision, None, when_utc(now()))
            return check_view(db, plan)

    @router.delete("/{plan_id}")
    def stop(identity: UUID, plan_id: UUID):
        with transaction(engine) as db:
            plan = current_plan(db, identity, plan_id)
            discard_queued(db, plan.id)
            db.delete(plan)
            return check_view(db, None)

    @router.post("/{plan_id}/resume")
    def resume(identity: UUID, plan_id: UUID):
        with transaction(engine) as db:
            plan = current_plan(db, identity, plan_id)
            if plan.blocked_on:
                day = billing_day(now())
                if not daily_capacity(settings_view(db)["dailyLimit"], billed_calls(db, day)):
                    raise HTTPException(409, ISSUES["daily_budget"])
                if plan.budget_reason in MONEY_ERRORS and (issue := note_check_amount_issue(db, plan, day)):
                    raise HTTPException(409, ISSUES[issue])
            plan.blocked_on = plan.budget_reason = None
            return check_view(db, plan)

    return router
