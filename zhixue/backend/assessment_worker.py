from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import case, func, or_, select

from .assessment import assessment_usage, basis_in_course, finish_grade, install_objectives, objective_data, queue_remediation, recipient_models
from .assessment_input import GradeReply, ObjectivesReply, QuestionGroup, check_grade, check_group, compose_prompt
from .assessment_notes import (
    GENERATING, UNSUPPORTED, NoteDecisions, accept_results, check_decisions, current_focus, is_current_job,
    note_pending, remember_focus, select_excerpts, verification_prompt, verified_sections,
)
from .course_review import ReviewReply
from .course_sources import sources_current
from .db import (
    AssessmentGrade, AssessmentJob, AssessmentObjective, AssessmentPlan, AssessmentRound, GenerationStep, Lesson, ModelUse,
    Project, RemediationCard, Review, transaction,
)
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_protocols import TextReply, Transport, post_json
from .model_failover import TRANSIENT, RoleScope, choose_recipient, switch_after_failure
from .model_study import settings_view, utc_now, when_utc
from .model_study_input import connection_view, parse_model_json
from .model_ledger import record_call_usage, request_model
from .model_usage import (
    active_calls, admit_reserved_call, auto_resume_enabled, billed_calls, billing_day, daily_capacity,
    queued_with_daily_recovery, settle_interrupted_use,
)
from .money_budget import DAILY_BUDGET_REASONS
from .project_activity import study_moment, touch_project
from .remediation import RemediationDraft, assess_remediation, check_draft, missed_questions, remediation_prompt


MAX_OUTPUT_TOKENS = 4600


@dataclass(frozen=True)
class AssessmentClaim:
    id: str
    job_id: str
    phase: str
    plan_id: str
    model: str
    address: str
    provider: str
    protocol: str
    credential: str | None
    prompt: str
    basis: dict
    group: dict | None
    previous: tuple[str, ...]
    card_draft: list[dict] | None = None
    issues: list[dict] | None = None
    notes: list[dict] | None = None
    note_sections: list[dict] | None = None


def job_context(db, job: AssessmentJob):
    saved = db.get(AssessmentRound, job.round_id) if job.round_id else None
    objective = db.get(AssessmentObjective, (saved.lesson_id, saved.key)) if saved else None
    return saved, objective, objective.basis if objective else job.basis


def assessment_scope(db, job: AssessmentJob, plan: AssessmentPlan | None) -> RoleScope | None:
    if plan is None:
        return None
    main, reviewer = recipient_models(db)
    if main is None or (job.remediation_id and (
        not plan.remediation or not reviewer or connection_view(reviewer) != plan.review_model
    )):
        return None
    author = None
    if job.kind == "recheck":
        original = db.scalar(select(AssessmentGrade).where(
            AssessmentGrade.round_id == job.round_id,
        ).order_by(AssessmentGrade.version).limit(1))
        author = original.reviewer if original else None
    if job.kind == "remediation_check":
        card = db.get(RemediationCard, job.remediation_id)
        if not card or not card.author:
            return None
        author = card.author
    backups = plan.backups or {}
    if job.kind == "note_verify":
        return (RoleScope("review", plan.review_model, backups.get("review"), plan.primary_model)
                if reviewer and is_current_job(plan, job) else None)
    if job.kind in {"recheck", "remediation_check"}:
        return RoleScope("review", plan.review_model, backups.get("review"), author) if reviewer else None
    return RoleScope("main", plan.primary_model, backups.get("main"))


def authorized_recipient(db, job: AssessmentJob, plan: AssessmentPlan | None):
    scope = assessment_scope(db, job, plan)
    return choose_recipient(db, scope, job.failovers) if scope else None


def block(job: AssessmentJob, reason: str):
    job.state = "blocked"
    job.error = reason
    job.active_call = None
    job.available_at = None


def recent_questions(db, saved: AssessmentRound) -> tuple[str, ...]:
    groups = db.scalars(select(AssessmentRound.group).where(
        AssessmentRound.lesson_id == saved.lesson_id, AssessmentRound.key == saved.key,
        AssessmentRound.id != saved.id, AssessmentRound.group.is_not(None),
    ).order_by(AssessmentRound.number.desc(), AssessmentRound.mode).limit(2))
    return tuple(item["text"] for group in groups for item in group["questions"])


def configuration_error(job: AssessmentJob, plan: AssessmentPlan | None) -> str:
    if not plan:
        return "authorization_revoked"
    if job.remediation_id:
        return "remediation_configuration_changed"
    return "review_configuration_changed" if job.kind in {"recheck", "note_verify"} else "configuration_changed"


def verification_material(db, folder, job, plan) -> tuple[list[dict], list[dict]]:
    notes, excluded = select_excerpts(db, folder, plan.note_scope["selected"])
    sections = verified_sections(db, folder, plan) if notes else []
    if not sections:
        excluded.extend({"key": item["key"], "title": item["title"], "reason": UNSUPPORTED} for item in notes)
        plan.note_scope = {**plan.note_scope, "approved": [], "excluded": excluded}
        job.state, job.active_call, job.error = "complete", None, None
        return [], []
    return notes, sections


def reserve_assessment(engine, now: datetime, *, remediation_jobs: bool | None = None) -> AssessmentClaim | None:
    clock = when_utc(now)
    day = billing_day(clock)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        settings = settings_view(db)
        active = active_calls(db)
        if active >= settings["concurrency"]:
            return None
        daily = billed_calls(db, day)
        pending = queued_with_daily_recovery(AssessmentJob.state, AssessmentJob.error, AssessmentJob.daily_blocked_on,
                                             day, auto_resume_enabled(settings, daily))
        query = select(AssessmentJob).join(Lesson, Lesson.id == AssessmentJob.lesson_id).join(
            Project, Project.id == Lesson.project_id,
        ).where(
            pending, Project.paused.is_(False), Lesson.ready.is_(True), Lesson.learned_on.is_not(None),
            or_(AssessmentJob.available_at.is_(None), AssessmentJob.available_at <= clock),
        )
        if remediation_jobs is not None:
            query = query.where(
                AssessmentJob.remediation_id.is_not(None) if remediation_jobs else AssessmentJob.remediation_id.is_(None),
            )
        jobs = db.scalars(query.order_by(
            case({"grade": 0, "recheck": 0, "note_verify": 1, "questions": 2, "objectives": 3},
                 value=AssessmentJob.kind, else_=4),
            AssessmentJob.created_at, AssessmentJob.id,
        )).all()
        for job in jobs:
            card = db.get(RemediationCard, job.remediation_id) if job.remediation_id else None
            if job.remediation_id and (card is None or not card.active):
                job.state, job.error = "complete", None
                continue
            plan = db.get(AssessmentPlan, job.lesson_id)
            checking_note = job.kind == "note_verify"
            if checking_note and not is_current_job(plan, job):
                job.state, job.error = "complete", None
                continue
            if job.kind in GENERATING and note_pending(db, plan):
                continue
            recipient = authorized_recipient(db, job, plan)
            if recipient is None:
                block(job, configuration_error(job, plan))
                continue
            if plan.max_calls is not None and assessment_usage(db, job.lesson_id) >= plan.max_calls:
                block(job, "task_budget")
                continue
            saved, objective, basis = job_context(db, job)
            if card:
                basis = card.basis
            if not checking_note and basis is not None and not basis_in_course(db, db.get(Lesson, job.lesson_id), basis):
                block(job, "course_revision_changed")
                continue
            if not checking_note and (basis is None or not any(item["proof"] == basis["proof"] for item in plan.sections)
                    or (job.error in DAILY_BUDGET_REASONS and not sources_current(db, folder, basis["citations"]))):
                block(job, "evidence_unavailable")
                continue
            step = db.scalar(select(GenerationStep).where(GenerationStep.lesson_id == job.lesson_id))
            if step is None:
                block(job, "evidence_unavailable")
                continue
            if not daily_capacity(settings["dailyLimit"], daily):
                job.daily_blocked_on = day
                block(job, "daily_budget")
                continue
            prior = recent_questions(db, saved) if job.kind == "questions" else ()
            group = saved.group if saved else None
            answers = saved.submission["answers"] if saved and saved.submission else None
            issues = missed_questions(saved, db.get(AssessmentGrade, card.grade_id)) if card else None
            model = recipient.connection
            address = connection_view(model)
            notes, note_sections = None, None
            if checking_note:
                notes, note_sections = verification_material(db, folder, job, plan)
                if not notes:
                    continue
                prompt = verification_prompt(notes, note_sections)
            elif card:
                prompt = remediation_prompt(job.kind, card, issues)
            else:
                if job.kind in GENERATING:
                    notes = current_focus(db, folder, plan, basis, address)
                prompt = compose_prompt(job.kind, basis, objective=objective_data(objective) if objective else None,
                                        previous=list(prior), group=group, answers=answers, note_focus=notes)
            identity = str(uuid4())
            db.add(ModelUse(
                id=identity, project_id=step.project_id, step_id=step.id, assessment_job_id=job.id, connection=address,
                started_at=clock, billing_day=day, status="reserved", auto_retry=False, phase=f"assessment_{job.kind}",
                failover_reason=recipient.failover_reason,
            ))
            job.state = "running"
            job.active_call = identity
            job.error, job.daily_blocked_on = None, None
            return AssessmentClaim(identity, job.id, job.kind, plan.id, model.model, address["address"], model.provider,
                                   model.protocol, model.credential_ref, prompt, basis or {}, group, prior,
                                   card.draft if card else None, issues, notes, note_sections)
    return None


def complete_assessment(engine, claim: AssessmentClaim, status: str, report: dict | None,
                        usage: dict | None, now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        call = db.get(ModelUse, claim.id)
        job = db.get(AssessmentJob, claim.job_id)
        if call is None or call.finished_at is not None or job is None or job.active_call != claim.id:
            return
        call.status = status if sent else "not_sent"
        call.finished_at = clock
        record_call_usage(call, usage)
        job.active_call = None
        job.available_at = None
        plan = db.get(AssessmentPlan, job.lesson_id)
        if job.kind == "note_verify" and not is_current_job(plan, job):
            job.state, job.error = "complete", None
            return
        saved, objective, basis = job_context(db, job)
        lesson = db.get(Lesson, job.lesson_id)
        project = db.get(Project, lesson.project_id)
        if saved is not None and saved.state == "superseded":
            call.status = "discarded" if sent else "not_sent"
            job.state, job.error = "complete", "course_revision_changed"
            return
        card = db.get(RemediationCard, job.remediation_id) if job.remediation_id else None
        if job.remediation_id and (not card or not card.active):
            call.status = "discarded" if sent else "not_sent"
            job.state, job.error = "complete", None
            return
        if card:
            basis = card.basis
        if report is not None and job.kind != "note_verify" and not basis_in_course(db, lesson, claim.basis):
            report = None
            status = call.status = "course_revision_changed"
        if report is not None and job.kind != "note_verify" and not sources_current(db, folder, basis["citations"]):
            report = None
            status = "evidence_unavailable"
            call.status = status
        if report is not None and job.kind in GENERATING and claim.notes:
            current = current_focus(db, folder, plan, basis, call.connection) if plan and plan.id == claim.plan_id else []
            if {item["key"] for item in claim.notes} - {item["key"] for item in current}:
                report = None
                status = call.status = "note_material_changed"
        if report is None:
            if status == "evidence_unavailable" and objective:
                objective.held = True
            retries_for = ModelUse.assessment_job_id == job.id
            if card:
                retries_for = ModelUse.assessment_job_id.in_(select(AssessmentJob.id).where(
                    AssessmentJob.remediation_id == card.grade_id,
                ))
            retries = db.scalar(select(func.count()).select_from(ModelUse).where(retries_for, ModelUse.auto_retry.is_(True)))
            scope = assessment_scope(db, job, plan) if plan and plan.id == claim.plan_id else None
            switched = scope is not None and switch_after_failure(db, scope, job, call, status, retries)
            if switched or (status in TRANSIENT and retries < 2):
                call.auto_retry = True
                job.state = "queued"
                job.error = status
                job.available_at = None if switched else clock + timedelta(seconds=2 + retries * 4)
            else:
                block(job, status)
            return
        if job.kind == "note_verify":
            accept_results(db, folder, plan, claim.notes, claim.note_sections, report, call.connection)
        elif job.kind == "objectives":
            if claim.notes:
                remember_focus(report, job.kind, claim.notes)
            install_objectives(db, job, lesson, report)
        elif job.kind == "questions":
            if claim.notes:
                remember_focus(report, job.kind, claim.notes)
            saved.group = report
            saved.state = "ready"
        elif job.kind == "remediation":
            card.draft, card.author = report["entries"], call.connection
            card.findings, card.reviewer, card.approved = None, None, False
        elif job.kind == "remediation_check":
            card.findings, card.reviewer = report["findings"], call.connection
            card.approved = all(item["state"] == "verified" for item in card.findings)
        else:
            review = db.get(Review, (saved.lesson_id, saved.key))
            finish_grade(db, saved, review, project, report, clock, reviewer=call.connection, identity=call.id)
        job.state = "complete"
        job.error = None
        if job.kind == "remediation":
            queue_remediation(db, card, db.get(AssessmentPlan, lesson.id), now)
        touch_project(project, study_moment(now))


def dispatch_assessment(engine, claim: AssessmentClaim, *, transport: Transport = post_json,
                        vault: CredentialStore | None = None, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_assessment(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_assessment(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        job = db.get(AssessmentJob, claim.job_id)
        call = db.get(ModelUse, claim.id)
        if not call or call.status != "reserved" or call.finished_at is not None or not job or job.active_call != claim.id:
            return
        if job.state == "complete":
            call.status, call.finished_at = "not_sent", when_utc(now())
            job.active_call = None
            return
        card = db.get(RemediationCard, job.remediation_id) if job.remediation_id else None
        if job.remediation_id and (not card or not card.active):
            call.status = "not_sent"
            call.finished_at = when_utc(now())
            job.state, job.active_call, job.error = "complete", None, None
            return
        lesson = db.get(Lesson, job.lesson_id)
        if lesson.learned_on is None or db.get(Project, lesson.project_id).paused:
            call.status = "not_sent"
            call.finished_at = when_utc(now())
            job.state = "queued"
            job.active_call = None
            return
        plan = db.get(AssessmentPlan, job.lesson_id)
        recipient = authorized_recipient(db, job, plan)
        current = bool(plan and claim.plan_id == plan.id and recipient and connection_view(recipient.connection) == call.connection)
        problem = None
        clock = when_utc(now())
        if not current:
            problem = configuration_error(job, plan)
        elif job.kind != "note_verify" and not basis_in_course(db, lesson, claim.basis):
            problem = "course_revision_changed"
        elif job.kind != "note_verify" and not sources_current(db, folder, claim.basis["citations"]):
            problem = "evidence_unavailable"
        elif plan.max_calls is not None and assessment_usage(db, job.lesson_id) > plan.max_calls:
            problem = "task_budget"
        if problem is None:
            if job.kind == "note_verify":
                notes, sections = verification_material(db, folder, job, plan)
                if not notes:
                    call.status, call.finished_at = "not_sent", clock
                    return
                claim = replace(claim, notes=notes, note_sections=sections, prompt=verification_prompt(notes, sections))
            elif job.kind in GENERATING:
                _, objective, _ = job_context(db, job)
                notes = current_focus(db, folder, plan, claim.basis, call.connection)
                prompt = compose_prompt(job.kind, claim.basis, objective=objective_data(objective) if objective else None,
                                        previous=list(claim.previous), note_focus=notes)
                claim = replace(claim, notes=notes, prompt=prompt)
            if budget_issue := admit_reserved_call(db, call, now, claim.prompt, MAX_OUTPUT_TOKENS):
                job.daily_blocked_on = call.billing_day
                block(job, budget_issue)
                return
    if problem:
        complete_assessment(engine, claim, problem, None, None, clock, sent=False)
        return

    reply = request_model(engine, ModelUse, claim.id, claim, key, claim.prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    report = None
    status = reply.status
    if status == "passed":
        try:
            if claim.phase == "note_verify":
                candidate = parse_model_json(NoteDecisions, reply.text)
                check_decisions(candidate, claim.notes)
            elif claim.phase == "objectives":
                candidate = parse_model_json(ObjectivesReply, reply.text)
                names = {(item["title"], item["ability"]) for item in candidate["objectives"]}
                if len(names) != len(candidate["objectives"]):
                    raise ValueError("复习目标与能力难度重复。")
                for item in candidate["objectives"]:
                    check_group(item["group"], claim.basis)
            elif claim.phase == "questions":
                candidate = parse_model_json(QuestionGroup, reply.text)
                check_group(candidate, claim.basis)
                if all(item["text"] in claim.previous for item in candidate["questions"]):
                    raise ValueError("题组重复上一轮问法。")
            elif claim.phase == "remediation":
                candidate = parse_model_json(RemediationDraft, reply.text)
                check_draft(candidate, claim.issues)
            elif claim.phase == "remediation_check":
                findings = parse_model_json(ReviewReply, reply.text)
                candidate = {"findings": assess_remediation(findings, claim.card_draft, claim.basis)}
            else:
                candidate = parse_model_json(GradeReply, reply.text)
                check_grade(candidate, claim.group)
            report = candidate
        except ValueError:
            status = "invalid_response"
    complete_assessment(engine, claim, status, report, reply.usage, now(), sent=True)


def process_one(engine, *, transport: Transport = post_json, vault: CredentialStore | None = None, now=utc_now) -> bool:
    claim = reserve_assessment(engine, now())
    if claim is None:
        return False
    dispatch_assessment(engine, claim, transport=transport, vault=vault, now=now)
    return True


def settle_interrupted_assessments(engine, now: datetime) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        calls = db.scalars(select(ModelUse).where(ModelUse.assessment_job_id.is_not(None), ModelUse.finished_at.is_(None)))
        for call in calls:
            unsent = settle_interrupted_use(call, clock)
            job = db.get(AssessmentJob, call.assessment_job_id)
            if job and job.active_call == call.id:
                if job.state == "complete":
                    job.active_call = None
                elif unsent:
                    job.state, job.active_call = "queued", None
                else:
                    block(job, "uncertain")
