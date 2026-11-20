from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, or_, select

from .course_publication import original_matches, save_course_version, section_markdown
from .course_review import same_model
from .db import GenerationStep, Lesson, LessonConsolidation, LessonConsolidationPlan, LessonQuestion, ModelUse, Project, transaction
from .file_locations import InvalidLocation, LocationConflict
from .lesson_assistant import published_version, source_sections
from .library import FileTooLarge
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_protocols import TextReply, Transport, post_json
from .model_failover import TRANSIENT, choose_recipient, switch_after_failure
from .model_study import settings_view, utc_now, when_utc
from .model_study_input import connection_view, parse_model_json
from .model_ledger import record_call_usage, request_model
from .model_usage import (
    active_calls, admit_reserved_call, auto_resume_enabled, billed_calls, billing_day, daily_capacity,
    queued_with_daily_recovery, settle_interrupted_use,
)
from .project_activity import study_moment, touch_project
from .study_consolidation import (
    ConsolidationDraft, ConsolidationReview, authorization_issue, block, check_draft, consolidation_scope,
    material_current, prompt_for, question_material, usage,
)


MAX_OUTPUT_TOKENS = 3200


@dataclass(frozen=True)
class ConsolidationClaim:
    id: str
    job_id: str
    phase: str
    model: str
    address: str
    provider: str
    protocol: str
    credential: str | None
    prompt: str


def queued_jobs(db, clock: datetime, *, publishing: bool, recover: bool = False):
    phase = LessonConsolidation.phase
    pending = queued_with_daily_recovery(LessonConsolidation.state, LessonConsolidation.reason,
                                         LessonConsolidation.daily_blocked_on, billing_day(clock), recover)
    return db.scalars(select(LessonConsolidation).join(
        Lesson, Lesson.id == LessonConsolidation.lesson_id,
    ).join(Project, Project.id == Lesson.project_id).where(
        pending, phase == "publish" if publishing else phase.in_(("draft", "review")),
        Lesson.learned_on.is_not(None), Project.paused.is_(False),
        or_(LessonConsolidation.available_at.is_(None), LessonConsolidation.available_at <= clock),
    ).order_by(LessonConsolidation.created_at, LessonConsolidation.id)).all()


def validate_scope(db, folder, job, lesson, version, plan) -> str | None:
    problem = authorization_issue(db, job, plan)
    if problem is not None:
        return problem
    if not version or not original_matches(db, folder, lesson, version):
        return "course_changed"
    if job.material is not None and not material_current(db, folder, job.material, version, plan):
        return "evidence_unavailable"
    if job.phase == "review" and not job.author:
        return "review_independence"
    if job.phase in {"draft", "review"} and choose_recipient(db, consolidation_scope(job, plan), job.failovers) is None:
        return "review_independence" if job.phase == "review" and same_model(job.author, plan.reviewer) else "configuration_changed"
    return None


def reserve(engine, now: datetime) -> ConsolidationClaim | None:
    clock = when_utc(now)
    day = billing_day(clock)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        settings = settings_view(db)
        active = active_calls(db)
        if active >= settings["concurrency"]:
            return None
        daily = billed_calls(db, day)
        for job in queued_jobs(db, clock, publishing=False, recover=auto_resume_enabled(settings, daily)):
            lesson = db.get(Lesson, job.lesson_id)
            plan = db.get(LessonConsolidationPlan, lesson.id)
            version = published_version(db, lesson)
            if problem := validate_scope(db, folder, job, lesson, version, plan):
                block(job, problem)
                continue
            if job.material is None:
                questions = [question for identity in job.question_ids if (question := db.get(LessonQuestion, identity))]
                if any(question.state in {"queued", "running"} for question in questions):
                    continue
                sections = source_sections(db, folder, version, plan.proofs)
                material = question_material(db, questions, sections)
                if material is None:
                    block(job, "evidence_unavailable")
                    continue
                if not material["pairs"]:
                    job.state, job.reason = "no_changes", None
                    continue
                job.material = material
            if plan.max_calls is not None and usage(db, lesson.id) >= plan.max_calls:
                block(job, "task_budget")
                continue
            step = db.scalar(select(GenerationStep).where(GenerationStep.lesson_id == lesson.id))
            if step is None:
                block(job, "course_changed")
                continue
            if not daily_capacity(settings["dailyLimit"], daily):
                job.daily_blocked_on = day
                block(job, "daily_budget")
                continue
            recipient = choose_recipient(db, consolidation_scope(job, plan), job.failovers)
            model = recipient.connection
            connection = connection_view(model)
            identity = str(uuid4())
            db.add(ModelUse(
                id=identity, project_id=lesson.project_id, step_id=step.id, consolidation_id=job.id, connection=connection,
                started_at=clock, billing_day=day, status="reserved", auto_retry=False, phase=f"consolidation_{job.phase}",
                failover_reason=recipient.failover_reason,
            ))
            job.state, job.reason, job.active_call, job.daily_blocked_on = "running", None, identity, None
            return ConsolidationClaim(identity, job.id, job.phase, model.model, connection["address"], model.provider,
                                      model.protocol, model.credential_ref, prompt_for(job))
    return None


def complete(engine, claim: ConsolidationClaim, status: str, report: dict | None,
             usage_report: dict | None, now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        call = db.get(ModelUse, claim.id)
        job = db.get(LessonConsolidation, claim.job_id)
        if not call or call.finished_at is not None or not job or job.active_call != call.id:
            return
        call.status, call.finished_at = status if sent else "not_sent", clock
        record_call_usage(call, usage_report)
        job.active_call, job.available_at = None, None
        lesson = db.get(Lesson, job.lesson_id)
        plan = db.get(LessonConsolidationPlan, lesson.id)
        if report is None:
            retries = db.scalar(select(func.count()).select_from(ModelUse).where(
                ModelUse.consolidation_id == job.id, ModelUse.auto_retry.is_(True),
            ))
            switched = (authorization_issue(db, job, plan) is None
                        and switch_after_failure(db, consolidation_scope(job, plan), job, call, status, retries))
            if switched or (status in TRANSIENT and retries < 2):
                call.auto_retry = True
                job.state, job.reason = "queued", status
                job.available_at = None if switched else clock + timedelta(seconds=2 + 4 * retries)
            else:
                block(job, status)
            return
        version = published_version(db, lesson)
        problem = validate_scope(db, folder, job, lesson, version, plan)
        if claim.phase == "draft":
            job.draft, job.author = report["section"], call.connection
            if job.draft is None:
                job.state, job.reason = "no_changes", None
                return
            job.phase = "review"
        else:
            job.finding, job.reviewer = check_draft(report, job), call.connection
            if job.finding["state"] == "verified":
                job.phase = "publish"
            else:
                problem = problem or "not_verified"
        if problem:
            block(job, problem)
        else:
            job.state, job.reason = "queued", None


def dispatch(engine, claim: ConsolidationClaim, *, transport: Transport = post_json,
             vault: CredentialStore | None = None, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        job = db.get(LessonConsolidation, claim.job_id)
        call = db.get(ModelUse, claim.id)
        if not call or call.status != "reserved" or call.finished_at is not None or not job or job.active_call != call.id:
            return
        lesson = db.get(Lesson, job.lesson_id)
        if lesson.learned_on is None or db.get(Project, lesson.project_id).paused:
            call.status, call.finished_at = "not_sent", when_utc(now())
            job.state, job.active_call = "queued", None
            return
        plan = db.get(LessonConsolidationPlan, lesson.id)
        problem = validate_scope(db, folder, job, lesson, published_version(db, lesson), plan)
        if not problem:
            recipient = choose_recipient(db, consolidation_scope(job, plan), job.failovers)
            if connection_view(recipient.connection) != call.connection:
                problem = "configuration_changed"
        if not problem and plan.max_calls is not None and usage(db, lesson.id) > plan.max_calls:
            problem = "task_budget"
        clock = when_utc(now())
        if not problem and (budget_issue := admit_reserved_call(db, call, now, claim.prompt, MAX_OUTPUT_TOKENS)):
            job.daily_blocked_on = call.billing_day
            block(job, budget_issue)
            return
    if problem:
        complete(engine, claim, problem, None, None, clock, sent=False)
        return
    reply = request_model(engine, ModelUse, claim.id, claim, key, claim.prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    status, report = reply.status, None
    if status == "passed":
        try:
            schema = ConsolidationDraft if claim.phase == "draft" else ConsolidationReview
            report = parse_model_json(schema, reply.text)
        except ValueError:
            status = "invalid_response"
    complete(engine, claim, status, report, reply.usage, now(), sent=True)


def publish_one(engine, folder: Path, now=utc_now) -> bool:
    clock = now()
    with transaction(engine) as db:
        jobs = queued_jobs(db, when_utc(clock), publishing=True)
        if not jobs:
            return False
        job = jobs[0]
        lesson = db.get(Lesson, job.lesson_id)
        plan = db.get(LessonConsolidationPlan, lesson.id)
        version = published_version(db, lesson)
        if problem := validate_scope(db, folder, job, lesson, version, plan):
            block(job, problem)
            return True
        if any(item["text"] == job.draft["text"] for item in version.sections):
            job.state, job.reason = "no_changes", None
            return True
        index = max((39, *(item["index"] for item in version.sections))) + 1
        pairs = job.material["pairs"]
        provenance = {"id": job.id, "questions": [item["id"] for item in pairs], "reviewer": job.reviewer,
                      "conversations": list(dict.fromkeys(item["conversationId"] for item in pairs if item.get("conversationId")))}
        addition = {**job.draft, **job.finding, "index": index, "disposition": "pending",
                    "consolidation": provenance}
        manuscript = version.markdown + "\n" + section_markdown(addition) + "\n"
        try:
            job.published_version = save_course_version(db, folder, lesson, version, version.finding_id,
                                                        [*version.sections, addition], manuscript, clock)
        except (OSError, InvalidLocation, FileTooLarge, LocationConflict):
            block(job, "publication_failed")
        else:
            job.state, job.reason = "complete", None
            touch_project(db.get(Project, lesson.project_id), study_moment(clock))
        return True


def process_one(engine, *, transport: Transport = post_json, vault: CredentialStore | None = None, now=utc_now) -> bool:
    claim = reserve(engine, now())
    if claim is None:
        return False
    dispatch(engine, claim, transport=transport, vault=vault, now=now)
    return True


def settle_interrupted(engine, now: datetime) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        calls = db.scalars(select(ModelUse).where(ModelUse.consolidation_id.is_not(None), ModelUse.finished_at.is_(None)))
        for call in calls:
            unsent = settle_interrupted_use(call, clock)
            job = db.get(LessonConsolidation, call.consolidation_id)
            if job and job.active_call == call.id:
                if unsent:
                    job.state, job.active_call = "queued", None
                else:
                    block(job, "uncertain")
