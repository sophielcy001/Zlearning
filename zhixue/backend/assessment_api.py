from hmac import compare_digest, digest
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import select

from .assessment import (
    assessment_review_info, assessment_status, basis_hash, finish_grade, new_job, new_round, pending_round,
    queue_remediation, recipient_models, review_basis, round_for, round_view, schedule_objectives,
    schedule_remediations, settings_view, withdraw_remediation,
)
from .assessment_input import MAX_QUESTIONS
from .assessment_notes import MAX_EXCERPTS, NoteLocator, is_current_job, note_options, public_notes, select_excerpts
from .assessment_worker import authorized_recipient
from .content import is_model_lesson
from .course_sources import HexDigest, sources_current
from .daily_plan import finish_booking, freeze_plan_baseline, review_task_id
from .db import (
    AssessmentGrade, AssessmentJob, AssessmentObjective, AssessmentPlan, AssessmentRound, CourseVersion,
    Lesson, Project, RemediationCard, Review, transaction,
)
from .model_failover import BackupChoices, backup_options
from .model_study import when_utc
from .model_study_input import CallLimit, Input, connection_view
from .remediation import card_citations, card_entries, card_file_id, card_usable
from .schedule import next_review_date


class PreviewInput(BackupChoices):
    maxCalls: CallLimit | None = None
    remediation: Annotated[bool, Field(strict=True)] = False
    noteExcerpts: list[NoteLocator] | None = Field(default=None, max_length=MAX_EXCERPTS)


class AuthorizeInput(PreviewInput):
    confirmation: HexDigest


class PrepareInput(Input):
    mode: Literal["formal", "practice"]


AnswerText = Annotated[str, Field(max_length=3000)]


class DraftInput(Input):
    answers: list[AnswerText] = Field(max_length=MAX_QUESTIONS)


class SubmitInput(DraftInput):
    action: Literal["submit", "unknown"]


def lesson_target(db, identity: UUID) -> Lesson:
    lesson = db.get(Lesson, str(identity))
    if lesson is None or not is_model_lesson(lesson.outline):
        raise HTTPException(404, "找不到此模型课程。")
    return lesson


def target_objective(db, lesson: Lesson, key: str) -> tuple[Project, AssessmentObjective, Review]:
    objective = db.get(AssessmentObjective, (lesson.id, key))
    review = db.get(Review, (lesson.id, key))
    if objective is None or review is None:
        raise HTTPException(404, "找不到此复习目标。")
    return db.get(Project, lesson.project_id), objective, review


def target_round(db, identity: UUID):
    saved = db.get(AssessmentRound, str(identity))
    if saved is None:
        raise HTTPException(404, "找不到这份题组。")
    lesson = db.get(Lesson, saved.lesson_id)
    project, objective, review = target_objective(db, lesson, saved.key)
    return saved, lesson, project, objective, review


def scope_signature(secret: bytes, lesson: Lesson, plan: AssessmentPlan | None, fields: dict) -> str:
    raw = json.dumps(["model-assessment", lesson.id, lesson.version, plan.id if plan else None, fields],
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return digest(secret, raw.encode("utf-8"), "sha256").hex()


def allow_mode(mode: str, status: str, objective: AssessmentObjective) -> None:
    if (objective.held or (mode == "formal" and status != "due")
            or (mode == "practice" and status not in {"upcoming", "complete", "due"})):
        raise HTTPException(409, "当前轮次暂不可作答，请检查学习状态与可核验依据。")


def editable(db, folder: Path, saved: AssessmentRound, lesson: Lesson, project: Project,
             objective: AssessmentObjective, review: Review, day) -> None:
    if saved.state != "ready" or saved.number != review.round_number:
        raise HTTPException(409, "此题组已提交或有更新的轮次。")
    allow_mode(saved.mode, assessment_status(db, folder, project, lesson, review, objective, day), objective)


def submit_round(db, folder: Path, identity: UUID, payload: SubmitInput, now, day) -> dict:
    saved, lesson, project, objective, review = target_round(db, identity)
    values = {"action": payload.action, "answers": [answer.strip() for answer in payload.answers]}
    if saved.submission:
        if values != saved.submission:
            raise HTTPException(409, "已保存此轮的另一份答案。")
        return round_view(db, saved)
    today = day(db)
    editable(db, folder, saved, lesson, project, objective, review, today)
    questions = saved.group["questions"]
    if payload.action == "submit" and (len(values["answers"]) != len(questions) or not all(values["answers"])):
        raise HTTPException(422, "请答完本组所有题目。")
    if len(values["answers"]) > len(questions):
        raise HTTPException(422, "答案数量与题组不符。")
    clock = now()
    if saved.mode == "formal":
        freeze_plan_baseline(db, folder, today)
    saved.submission = values
    saved.submitted_on = today
    saved.submitted_at = when_utc(clock)
    saved.previous_due_on = review.due_on
    saved.previous_stage = review.stage
    if saved.mode == "formal":
        finish_booking(db, review_task_id(lesson.id, saved.key, saved.number), today)
    if payload.action == "unknown":
        explanation = "。".join(item["criterion"].rstrip("。") for item in questions) + "。"
        report = {"items": [{"index": i, "met": False, "reason": item["criterion"]} for i, item in enumerate(questions)],
                  "explanation": explanation}
        finish_grade(db, saved, review, project, report, when_utc(clock), reviewer=None, identity=str(uuid4()))
    else:
        saved.state = "grading"
        new_job(db, lesson.id, "grade", clock, round_id=saved.id)
    return round_view(db, saved)


def retry_jobs(db, lesson: Lesson, folder: Path, day, identity: str | None = None) -> None:
    query = select(AssessmentJob).where(AssessmentJob.lesson_id == lesson.id, AssessmentJob.state == "blocked")
    plan = db.get(AssessmentPlan, lesson.id)
    if plan is None or not plan.remediation:
        query = query.where(AssessmentJob.remediation_id.is_(None))
    if identity:
        query = query.where(AssessmentJob.round_id == identity)
    for job in db.scalars(query):
        if job.kind == "note_verify" and not is_current_job(plan, job):
            job.state, job.error = "complete", None
            continue
        if job.kind == "objectives" and job.error == "evidence_unavailable" and sources_current(db, folder, job.basis["citations"]):
            job.first_due_on = next_review_date(day, 0)
        job.state = "queued"
        job.error = None
        job.available_at = None


def assessment_router(engine, folder: Path, session_token: str, now, day) -> APIRouter:
    router = APIRouter(prefix="/api")
    secret = session_token.encode("utf-8")

    def selection(db, lesson, payload: PreviewInput, old: AssessmentPlan | None) -> dict:
        primary, secondary = recipient_models(db)
        if primary is None:
            raise HTTPException(409, "请先配置主模型及凭据。")
        if payload.remediation and secondary is None:
            raise HTTPException(409, "自动错因补学需要可独立核验的复核模型。")
        sections = review_basis(db, folder, lesson)
        if not sections:
            raise HTTPException(409, "本节暂无可用的已核验依据，暂缓出题。")
        fields = {"main": connection_view(primary), "review": connection_view(secondary) if secondary else None,
                  "sections": sections, "maxCalls": payload.maxCalls, "remediation": payload.remediation,
                  **backup_options(db, primary, secondary, allow_main=payload.allowMainBackup,
                                   allow_review=payload.allowReviewBackup)}
        locators = ([item.model_dump(mode="json") for item in payload.noteExcerpts] if payload.noteExcerpts is not None
                    else old.note_scope["selected"] if old and old.note_scope else [])
        if locators:
            keys = {(item["noteId"], item["revision"], item["start"], item["end"]) for item in locators}
            if len(keys) != len(locators):
                raise HTTPException(422, "请去掉重复的笔记选段。")
            notes, excluded = select_excerpts(db, folder, locators)
            if notes and secondary is None:
                raise HTTPException(409, "核验笔记选段需要可独立复核的模型。")
            fields.update(noteExcerpts=notes, excludedNotes=excluded)
        return fields

    @router.get("/lessons/{identity}/assessment")
    def settings(identity: UUID):
        with transaction(engine, write=False) as db:
            lesson = lesson_target(db, identity)
            plan = db.get(AssessmentPlan, lesson.id)
            return {**settings_view(db, lesson, plan), **public_notes(db, folder, plan)}

    @router.get("/lessons/{identity}/assessment/note-options")
    def available_notes(identity: UUID):
        with transaction(engine, write=False) as db:
            lesson = lesson_target(db, identity)
            return note_options(db, lesson.id)

    @router.post("/lessons/{identity}/assessment/preview")
    def preview(identity: UUID, payload: PreviewInput):
        with transaction(engine) as db:
            lesson = lesson_target(db, identity)
            plan = db.get(AssessmentPlan, lesson.id)
            fields = selection(db, lesson, payload, plan)
            return {**fields, "confirmation": scope_signature(secret, lesson, plan, fields)}

    @router.post("/lessons/{identity}/assessment/authorize")
    def authorize(identity: UUID, payload: AuthorizeInput):
        with transaction(engine) as db:
            lesson = lesson_target(db, identity)
            old = db.get(AssessmentPlan, lesson.id)
            fields = selection(db, lesson, payload, old)
            if not compare_digest(payload.confirmation, scope_signature(secret, lesson, old, fields)):
                raise HTTPException(409, "模型、课程版本或依据发生了变化，请重新预览。")
            if old and old.note_scope:
                previous = db.get(AssessmentJob, old.note_scope["jobId"])
                if previous and previous.state in {"queued", "blocked"}:
                    previous.state, previous.error = "complete", None
            plan = db.merge(AssessmentPlan(lesson_id=lesson.id, id=str(uuid4()), primary_model=fields["main"],
                                           review_model=fields["review"], sections=fields["sections"], max_calls=payload.maxCalls,
                                           backups=fields.get("backups"),
                                           note_scope=None,
                                           remediation=payload.remediation))
            if notes := fields.get("noteExcerpts"):
                check = new_job(db, lesson.id, "note_verify", now())
                plan.note_scope = {"jobId": check.id, "selected": notes}
            retry_jobs(db, lesson, folder, day(db))
            schedule_objectives(db, lesson, plan, now())
            schedule_remediations(db, lesson, plan, now())
            return {**settings_view(db, lesson, plan), **public_notes(db, folder, plan)}

    @router.post("/lessons/{identity}/assessment/retry")
    def retry_settings(identity: UUID):
        with transaction(engine) as db:
            lesson = lesson_target(db, identity)
            plan = db.get(AssessmentPlan, lesson.id)
            if plan is None:
                raise HTTPException(409, "请先确认复习发送范围。")
            retry_jobs(db, lesson, folder, day(db))
            return settings_view(db, lesson, plan)

    @router.post("/lessons/{identity}/assessment/revoke")
    def revoke(identity: UUID):
        with transaction(engine) as db:
            lesson = lesson_target(db, identity)
            if plan := db.get(AssessmentPlan, lesson.id):
                db.delete(plan)
            for job in db.scalars(select(AssessmentJob).where(
                AssessmentJob.lesson_id == lesson.id, AssessmentJob.state == "queued",
            )):
                job.state = "blocked"
                job.error = "authorization_revoked"
                job.available_at = None
            return settings_view(db, lesson, None)

    @router.get("/lessons/{identity}/assessments/{key}")
    def objective_view(identity: UUID, key: str):
        with transaction(engine) as db:
            lesson = lesson_target(db, identity)
            project, objective, review = target_objective(db, lesson, key)
            status = assessment_status(db, folder, project, lesson, review, objective, day(db))
            active = pending_round(db, review) if status == "waiting" else round_for(db, review, "formal") if status == "due" else None
            previous = round_for(db, review, "formal", number=review.round_number - 1) if review.round_number > 1 else None
            return {
                **assessment_review_info(project, review, objective, status), "round": review.round_number,
                "group": round_view(db, active) if active else None, "latest": round_view(db, previous) if previous else None,
            }

    @router.post("/lessons/{identity}/assessments/{key}/prepare")
    def prepare(identity: UUID, key: str, payload: PrepareInput):
        with transaction(engine) as db:
            lesson = lesson_target(db, identity)
            project, objective, review = target_objective(db, lesson, key)
            status = assessment_status(db, folder, project, lesson, review, objective, day(db))
            allow_mode(payload.mode, status, objective)
            saved = round_for(db, review, payload.mode)
            if saved is None:
                active = db.get(AssessmentPlan, lesson.id)
                if not active or not settings_view(db, lesson, active)["authorized"]:
                    raise HTTPException(409, "请在课程中重新确认复习发送范围。")
                saved = new_round(db, lesson, review, payload.mode, objective.basis["version"])
                db.flush()
                new_job(db, lesson.id, "questions", now(), round_id=saved.id)
            return round_view(db, saved)

    @router.get("/assessments/{identity}")
    def get_round(identity: UUID):
        with transaction(engine, write=False) as db:
            saved, _, _, _, _ = target_round(db, identity)
            return round_view(db, saved)

    @router.put("/assessments/{identity}/draft")
    def draft(identity: UUID, payload: DraftInput):
        with transaction(engine) as db:
            saved, lesson, project, objective, review = target_round(db, identity)
            editable(db, folder, saved, lesson, project, objective, review, day(db))
            if len(payload.answers) > len(saved.group["questions"]):
                raise HTTPException(422, "答案数量与题组不符。")
            saved.draft = payload.answers
            return round_view(db, saved)

    @router.post("/assessments/{identity}/hint")
    def hint(identity: UUID):
        with transaction(engine) as db:
            saved, lesson, project, objective, review = target_round(db, identity)
            editable(db, folder, saved, lesson, project, objective, review, day(db))
            saved.hinted = True
            return round_view(db, saved)

    @router.post("/assessments/{identity}/submit")
    def submit(identity: UUID, payload: SubmitInput):
        with transaction(engine) as db:
            return submit_round(db, folder, identity, payload, now, day)

    @router.post("/assessments/{identity}/retry")
    def retry_round(identity: UUID):
        with transaction(engine) as db:
            saved, lesson, _, _, _ = target_round(db, identity)
            retry_jobs(db, lesson, folder, day(db), saved.id)
            return round_view(db, saved)

    @router.post("/assessments/{identity}/recheck")
    def recheck(identity: UUID):
        with transaction(engine) as db:
            saved, lesson, project, objective, review = target_round(db, identity)
            if saved.state == "rechecking":
                return round_view(db, saved)
            if (saved.state != "graded" or saved.submission["action"] != "submit" or saved.mode != "formal"
                    or saved.number + 1 != review.round_number or pending_round(db, review) is not None):
                raise HTTPException(409, "请核查最近一轮已正式评价的有效答案。")
            if saved.course_version < objective.basis["version"]:
                raise HTTPException(409, "本轮使用的是修订前的依据，原评价与作答已保留。")
            if assessment_status(db, folder, project, lesson, review, objective, day(db)) in {"paused", "held"} or objective.held:
                raise HTTPException(409, "课程复习已暂停或依据待处理，请恢复后继续。")
            job = new_job(db, lesson.id, "recheck", now(), round_id=saved.id)
            if not authorized_recipient(db, job, db.get(AssessmentPlan, lesson.id)):
                raise HTTPException(409, "请先在课程中确认独立复核模型的发送范围。")
            review.due_on, review.stage = saved.previous_due_on, saved.previous_stage
            saved.state = "rechecking"
            withdraw_remediation(db, saved.id)
            return round_view(db, saved)

    @router.post("/assessments/{identity}/remediation/retry")
    def retry_remediation(identity: UUID):
        with transaction(engine) as db:
            saved, lesson, _, _, _ = target_round(db, identity)
            latest = db.scalar(select(AssessmentGrade.id).where(AssessmentGrade.round_id == saved.id).order_by(
                AssessmentGrade.version.desc(),
            ).limit(1))
            card = db.get(RemediationCard, latest) if latest else None
            if saved.state != "graded" or card is None or not card.active:
                raise HTTPException(409, "当前评价没有可恢复的补学任务。")
            if card.published_at:
                return round_view(db, saved)
            if card.publish_error:
                card.publish_error = None
            else:
                plan = db.get(AssessmentPlan, lesson.id)
                if plan is None or not plan.remediation:
                    raise HTTPException(409, "请先在课程中确认错因补学的发送范围。")
                job = db.scalar(select(AssessmentJob).where(
                    AssessmentJob.remediation_id == card.grade_id, AssessmentJob.state != "complete",
                ).limit(1))
                if job and job.state == "blocked":
                    job.state, job.error, job.available_at = "queued", None, None
                if job is None and card.findings and not card.approved:
                    new_job(db, card.lesson_id, "remediation", now(), round_id=card.round_id, remediation_id=card.grade_id)
                else:
                    queue_remediation(db, card, plan, now())
            return round_view(db, saved)

    @router.get("/lessons/{identity}/remediation")
    def list_remediation(identity: UUID, version: Annotated[int | None, Field(ge=1)] = None):
        with transaction(engine) as db:
            lesson = lesson_target(db, identity)
            published = db.get(CourseVersion, (lesson.id, version or lesson.version))
            if published is None:
                return {"cards": []}
            anchors = {basis_hash(section) for section in published.sections if section["state"] == "verified"}
            results = []
            for card in db.scalars(select(RemediationCard).where(
                RemediationCard.lesson_id == lesson.id, RemediationCard.active.is_(True),
                RemediationCard.published_at.is_not(None),
            ).order_by(RemediationCard.created_at.desc(), RemediationCard.published_at.desc(), RemediationCard.grade_id)):
                if card.basis["proof"] not in anchors or not card_usable(db, folder, card):
                    continue
                saved = db.get(AssessmentRound, card.round_id)
                citations = card_citations(card)
                results.append({
                    "id": card.grade_id, "assessmentId": saved.id, "objectiveKey": saved.key,
                    "sectionIndex": card.basis["index"], "sectionTitle": card.basis["title"],
                    "courseVersion": card.basis["version"], "round": saved.number,
                    "scoredOn": db.get(AssessmentGrade, card.grade_id).result["scoredOn"],
                    "fileId": card_file_id(card), "revision": card.revision, "entries": card_entries(card, saved),
                    "citations": citations, "sourcesChanged": not sources_current(db, folder, citations),
                })
            return {"cards": results}

    return router
