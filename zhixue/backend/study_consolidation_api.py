from hmac import compare_digest, digest
import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException

from .assessment import recipient_models
from .course_sources import HexDigest
from .db import Lesson, LessonConsolidation, LessonConsolidationPlan, LessonQuestion, transaction
from .lesson_assistant import published_version, source_sections
from .model_failover import BackupChoices, backup_options
from .model_study_input import CallLimit, connection_view
from .study_consolidation import (
    PAIR_LIMIT, QUOTES_PER_PAIR, approved_models, block, lesson_jobs, question_material, resume, settings_view, today_questions,
)


class PreviewInput(BackupChoices):
    maxCalls: CallLimit | None = None


class AuthorizeInput(PreviewInput):
    confirmation: HexDigest


def model_lesson(db, identity: UUID) -> Lesson:
    lesson = db.get(Lesson, str(identity))
    if lesson is None or lesson.outline is None:
        raise HTTPException(404, "找不到这节模型课程。")
    return lesson


def sample_pairs(db, lesson, sections, now):
    candidates = {item.id: item for item in today_questions(db, lesson, now)}
    for job in lesson_jobs(db, lesson.id):
        if job.state not in {"complete", "no_changes"}:
            for identity in job.question_ids:
                if identity not in candidates and (question := db.get(LessonQuestion, identity)):
                    candidates[identity] = question
    pairs = []
    for question in candidates.values():
        if material := question_material(db, [question], sections):
            pairs.extend(material["pairs"])
    return [{key: item[key] for key in ("id", "question", "answer")} for item in pairs]


def study_consolidation_router(engine, folder: Path, session_token: str, now) -> APIRouter:
    router = APIRouter(prefix="/api/lessons/{identity}/consolidation")
    secret = session_token.encode("utf-8")

    def fields_for(db, lesson: Lesson, payload: PreviewInput):
        main, reviewer = recipient_models(db)
        if main is None or reviewer is None:
            raise HTTPException(409, "请在管理中心配置主模型和独立的复核连接。")
        version = published_version(db, lesson)
        sections = source_sections(db, folder, version) if version else []
        if not sections:
            raise HTTPException(409, "本节暂无可作为答疑整理依据的核验正文。")
        return {
            "main": connection_view(main), "review": connection_view(reviewer), "version": version.version,
            "revision": version.revision, "sections": sections, "maxCalls": payload.maxCalls,
            "pairLimit": PAIR_LIMIT, "quotesPerPair": QUOTES_PER_PAIR,
            "pairs": sample_pairs(db, lesson, sections, now()),
            **backup_options(db, main, reviewer, allow_main=payload.allowMainBackup, allow_review=payload.allowReviewBackup),
        }

    def sign(lesson: Lesson, plan: LessonConsolidationPlan | None, fields: dict) -> str:
        material = json.dumps(["lesson-consolidation", lesson.id, plan.id if plan else None, fields],
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return digest(secret, material.encode("utf-8"), "sha256").hex()

    @router.get("")
    def settings(identity: UUID):
        with transaction(engine, write=False) as db:
            return settings_view(db, model_lesson(db, identity))

    @router.post("/preview")
    def preview(identity: UUID, payload: PreviewInput):
        with transaction(engine) as db:
            lesson = model_lesson(db, identity)
            plan = db.get(LessonConsolidationPlan, lesson.id)
            fields = fields_for(db, lesson, payload)
            return {**fields, "confirmation": sign(lesson, plan, fields)}

    @router.post("/authorize")
    def authorize(identity: UUID, payload: AuthorizeInput):
        with transaction(engine) as db:
            lesson = model_lesson(db, identity)
            old = db.get(LessonConsolidationPlan, lesson.id)
            fields = fields_for(db, lesson, payload)
            if not compare_digest(sign(lesson, old, fields), payload.confirmation):
                raise HTTPException(409, "答疑、核验资料或接收模型已改变，请重新预览。")
            proofs = [section["proof"] for section in fields["sections"]]
            stable = (old and old.main == fields["main"] and old.reviewer == fields["review"]
                      and old.proofs == proofs)
            plan = db.merge(LessonConsolidationPlan(
                lesson_id=lesson.id, id=old.id if stable else str(uuid4()),
                version=fields["version"], revision=fields["revision"],
                main=fields["main"], reviewer=fields["review"], backups=fields.get("backups"),
                proofs=proofs, max_calls=payload.maxCalls,
            ))
            for job in lesson_jobs(db, lesson.id):
                if job.state == "queued" and job.plan_id != plan.id:
                    resume(job, plan)
                if job.state == "blocked" and job.reason in {
                    "authorization_required", "authorization_revoked", "configuration_changed", "review_independence",
                }:
                    resume(job, plan)
            return settings_view(db, lesson)

    @router.post("/revoke")
    def revoke(identity: UUID):
        with transaction(engine) as db:
            lesson = model_lesson(db, identity)
            if plan := db.get(LessonConsolidationPlan, lesson.id):
                db.delete(plan)
                for job in lesson_jobs(db, lesson.id):
                    if job.state == "queued":
                        block(job, "authorization_revoked")
            return settings_view(db, lesson)

    @router.post("/jobs/{job_id}/retry")
    def retry(identity: UUID, job_id: UUID):
        with transaction(engine) as db:
            lesson = model_lesson(db, identity)
            job = db.get(LessonConsolidation, str(job_id))
            if not job or job.lesson_id != lesson.id:
                raise HTTPException(404, "找不到这次答疑整理。")
            if job.state == "blocked":
                plan = db.get(LessonConsolidationPlan, lesson.id)
                if approved_models(db, plan)[0] is None:
                    raise HTTPException(409, "请重新确认整理和独立复核的发送范围。")
                resume(job, plan)
            return settings_view(db, lesson)

    return router
