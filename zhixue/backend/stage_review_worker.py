from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select

from .db import Project, StageAssessment, StageAssessmentAttempt, StageReview, StageReviewCall, transaction
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_protocols import TextReply, Transport, post_json
from .model_failover import FAILOVER_FAILURES, TRANSIENT
from .model_study import settings_view, utc_now, when_utc
from .model_study_input import connection_view, parse_model_json
from .model_ledger import record_call_usage, request_model
from .model_usage import (
    active_calls, admit_reserved_call, auto_resume_enabled, billed_calls, billing_day, daily_capacity,
    queued_with_daily_recovery,
)
from .stage_assessments import apply_stage_result, awaiting_stage_review
from .stage_review import (
    StageReviewReport, grounded_findings, pending_backup, prompt_for, reserve_release, usage, validated_recipient,
)


MAX_OUTPUT_TOKENS = 6000


@dataclass(frozen=True)
class Claim:
    id: str
    review_id: str
    provider: str
    protocol: str
    model: str
    address: str
    credential: str | None
    prompt: str


def after_pause(job: StageReview, call: StageReviewCall, clock: datetime) -> None:
    reserve_release(job, call, clock)
    if job.state != "stopped":
        job.state = "queued"


def block(job: StageReview, reason: str, *, day=None) -> None:
    job.state, job.reason = "blocked", reason
    job.active_call, job.available_at, job.daily_blocked_on = None, None, day


def job_rows():
    return (select(StageReview, StageAssessmentAttempt, StageAssessment, Project).select_from(StageReview)
            .join(StageAssessmentAttempt, StageAssessmentAttempt.id == StageReview.id)
            .join(StageAssessment, StageAssessment.id == StageAssessmentAttempt.assessment_id)
            .join(Project, Project.id == StageAssessment.project_id))


def stage_review_ready(db, job, attempt, stage, project) -> bool:
    return (awaiting_stage_review(db, stage, attempt, project)
            and validated_recipient(db, stage, attempt, job)[1] is None
            and (job.max_calls is None or usage(db, job.id) < job.max_calls))


def reserve(engine, now: datetime) -> Claim | None:
    clock = when_utc(now)
    day = billing_day(clock)
    with transaction(engine) as db:
        settings = settings_view(db)
        if active_calls(db) >= settings["concurrency"]:
            return None
        used = billed_calls(db, day)
        permitted = queued_with_daily_recovery(StageReview.state, StageReview.reason, StageReview.daily_blocked_on, day,
                                               auto_resume_enabled(settings, used))
        rows = job_rows().where(
            permitted, Project.paused.is_(False), StageAssessment.skipped_at.is_(None),
            StageReview.active_call.is_(None), StageAssessmentAttempt.outcome == "awaiting_review",
            or_(StageReview.available_at.is_(None), StageReview.available_at <= clock),
        ).order_by(StageReview.created_at, StageReview.id)
        for job, attempt, stage, project in db.execute(rows):
            if not awaiting_stage_review(db, stage, attempt, project):
                continue
            recipient, issue = validated_recipient(db, stage, attempt, job)
            if issue:
                block(job, issue)
                continue
            if job.max_calls is not None and usage(db, job.id) >= job.max_calls:
                block(job, "task_budget")
                continue
            if not daily_capacity(settings["dailyLimit"], used):
                block(job, "daily_budget", day=day)
                continue
            identity = str(uuid4())
            reference = connection_view(recipient)
            db.add(StageReviewCall(id=identity, review_id=job.id, approval_id=job.approval_id, scope=job.scope,
                                   connection=reference, failover_reason=job.failover_reason,
                                   started_at=clock, billing_day=day, status="reserved"))
            job.state, job.reason, job.active_call, job.daily_blocked_on = "running", None, identity, None
            return Claim(identity, job.id, recipient.provider, recipient.protocol, recipient.model,
                         reference["address"], recipient.credential_ref, prompt_for(job.scope))
    return None


def complete(engine, claim: Claim, status: str, report: dict | None,
             usage_report: dict | None, now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        call = db.get(StageReviewCall, claim.id)
        job = db.get(StageReview, claim.review_id)
        if call is None or call.finished_at is not None or job is None or job.active_call != call.id:
            return
        attempt = db.get(StageAssessmentAttempt, job.id)
        stage = db.get(StageAssessment, attempt.assessment_id)
        project = db.get(Project, stage.project_id)
        findings = grounded_findings(stage, call.scope, report) if report is not None else None
        if report is not None and findings is None:
            status = "invalid_response"
        call.status, call.finished_at = status if sent else "not_sent", clock
        record_call_usage(call, usage_report)
        job.active_call, job.available_at = None, None
        if findings is not None:
            call.report = {"reviewer": "model", "model": call.connection or call.scope["connection"],
                           "failoverReason": call.failover_reason, "executionObserved": False,
                           "findings": findings, "reviewedAt": clock.isoformat() + "Z"}
            if attempt.result is not None:
                return
            if job.state == "stopped" or not awaiting_stage_review(db, stage, attempt, project):
                job.state, job.held_call = "held", call.id
                return
            apply_stage_result(stage, attempt, call.report)
            job.state, job.reason = "complete", None
            return
        if job.state == "stopped":
            return
        retries = db.scalar(select(func.count()).select_from(StageReviewCall).where(
            StageReviewCall.review_id == job.id, StageReviewCall.auto_retry.is_(True),
        ))
        switched = retries < 2 and status in FAILOVER_FAILURES and pending_backup(db, stage, job)
        if switched:
            job.failover_reason = status
        if switched or (sent and status in TRANSIENT and retries < 2):
            if job.max_calls is not None and usage(db, job.id) >= job.max_calls:
                block(job, "task_budget")
                return
            call.auto_retry = True
            job.state, job.reason = "queued", status
            job.available_at = None if switched else clock + timedelta(seconds=2 + 4 * retries)
        else:
            block(job, status)


def dispatch(engine, claim: Claim, *, transport: Transport = post_json,
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
    with transaction(engine) as db:
        call = db.get(StageReviewCall, claim.id)
        job = db.get(StageReview, claim.review_id)
        if call is None or call.status != "reserved" or job is None or job.active_call != call.id:
            return
        attempt = db.get(StageAssessmentAttempt, job.id)
        stage = db.get(StageAssessment, attempt.assessment_id)
        project = db.get(Project, stage.project_id)
        clock = when_utc(now())
        if job.state == "stopped" or not awaiting_stage_review(db, stage, attempt, project):
            after_pause(job, call, clock)
            return
        recipient, issue = validated_recipient(db, stage, attempt, job)
        if not issue and (job.approval_id != call.approval_id or job.scope != call.scope
                          or connection_view(recipient) != (call.connection or call.scope["connection"])):
            issue = "approval_changed"
        if not issue and job.max_calls is not None and usage(db, job.id) > job.max_calls:
            issue = "task_budget"
        if issue:
            reserve_release(job, call, clock)
            block(job, issue)
            return
        if issue := admit_reserved_call(db, call, now, claim.prompt, MAX_OUTPUT_TOKENS):
            block(job, issue, day=call.billing_day)
            return
    reply = request_model(engine, StageReviewCall, claim.id, claim, key, claim.prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    status, report = reply.status, None
    if status == "passed":
        try:
            report = parse_model_json(StageReviewReport, reply.text)
        except ValueError:
            status = "invalid_response"
    complete(engine, claim, status, report, reply.usage, now(), sent=True)


def process_one(engine, *, transport: Transport = post_json, vault: CredentialStore | None = None, now=utc_now) -> bool:
    claim = reserve(engine, now())
    if claim is None:
        return False
    dispatch(engine, claim, transport=transport, vault=vault, now=now)
    return True


def settle_interrupted(engine, now: datetime) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        calls = db.scalars(select(StageReviewCall).where(StageReviewCall.finished_at.is_(None)))
        for call in calls:
            job = db.get(StageReview, call.review_id)
            call.finished_at = clock
            if call.status == "reserved":
                call.status = "not_sent"
                if job is not None and job.active_call == call.id:
                    after_pause(job, call, clock)
                continue
            call.status = "uncertain"
            if job is not None and job.active_call == call.id:
                if job.state == "stopped":
                    job.active_call = None
                else:
                    block(job, "uncertain")
