from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, or_, select

from .course_sources import sources_current
from .db import (
    ChatMessage, Conversation, GenerationStep, Lesson, LessonAssistantPlan, LessonQuestion,
    ModelUse, Project, transaction,
)
from .lesson_assistant import ModelAnswer, answer_citations, block_question, compose_prompt, published_version, usage
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


MAX_OUTPUT_TOKENS = 3200


@dataclass(frozen=True)
class AnswerClaim:
    id: str
    question_id: str
    model: str
    address: str
    provider: str
    protocol: str
    credential: str | None
    prompt: str


def answer_scope(plan: LessonAssistantPlan) -> RoleScope:
    return RoleScope("main", plan.recipient, (plan.backups or {}).get("main"))


def check_scope(db, record: LessonQuestion, plan: LessonAssistantPlan | None) -> str | None:
    if plan is None:
        return "authorization_revoked"
    if record.plan_id != plan.id or choose_recipient(db, answer_scope(plan), record.failovers) is None:
        return "configuration_changed"
    lesson = db.get(Lesson, record.lesson_id)
    version = published_version(db, lesson)
    if (not version or version.version != plan.version or version.revision != plan.revision
            or version.revision != record.context["revision"]
            or any(part["proof"] not in plan.proofs for part in record.context["sections"])):
        return "course_changed"
    return None


def context_available(db, folder: Path, context: dict) -> bool:
    previous = context["previous"]
    return sources_current(db, folder, [*context["sources"], *(previous["sources"] if previous else [])])


def reserve_answer(engine, now: datetime) -> AnswerClaim | None:
    clock = when_utc(now)
    day = billing_day(clock)
    with transaction(engine) as db:
        settings = settings_view(db)
        active = active_calls(db)
        if active >= settings["concurrency"]:
            return None
        daily = billed_calls(db, day)
        pending = queued_with_daily_recovery(LessonQuestion.state, LessonQuestion.error, LessonQuestion.daily_blocked_on,
                                             day, auto_resume_enabled(settings, daily))
        folder = Path(engine.url.database).resolve().parent
        records = db.scalars(select(LessonQuestion).join(Lesson, Lesson.id == LessonQuestion.lesson_id).join(
            Project, Project.id == Lesson.project_id,
        ).where(
            pending, Project.paused.is_(False),
            or_(LessonQuestion.available_at.is_(None), LessonQuestion.available_at <= clock),
        ).order_by(LessonQuestion.created_at, LessonQuestion.id)).all()
        for record in records:
            plan = db.get(LessonAssistantPlan, record.lesson_id)
            problem = check_scope(db, record, plan)
            if problem:
                block_question(record, problem)
                continue
            if plan.max_calls is not None and usage(db, record.lesson_id) >= plan.max_calls:
                block_question(record, "task_budget")
                continue
            if record.error in DAILY_BUDGET_REASONS and not context_available(db, folder, record.context):
                block_question(record, "evidence_unavailable")
                continue
            step = db.scalar(select(GenerationStep).where(GenerationStep.lesson_id == record.lesson_id))
            if step is None:
                block_question(record, "course_changed")
                continue
            if not daily_capacity(settings["dailyLimit"], daily):
                record.daily_blocked_on = day
                block_question(record, "daily_budget")
                continue
            question = db.get(ChatMessage, record.id)
            recipient = choose_recipient(db, answer_scope(plan), record.failovers)
            model = recipient.connection
            connection = connection_view(model)
            identity = str(uuid4())
            db.add(ModelUse(
                id=identity, project_id=step.project_id, step_id=step.id, question_id=record.id, connection=connection,
                started_at=clock, billing_day=day, status="reserved", auto_retry=False, phase="lesson_answer",
                failover_reason=recipient.failover_reason,
            ))
            record.active_call = identity
            record.state, record.error, record.daily_blocked_on = "running", None, None
            return AnswerClaim(identity, record.id, model.model, connection["address"], model.provider, model.protocol,
                               model.credential_ref, compose_prompt(question, record))
    return None


def complete_answer(engine, claim: AnswerClaim, outcome: str, result: dict | None, usage: dict | None,
                    now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        call = db.get(ModelUse, claim.id)
        record = db.get(LessonQuestion, claim.question_id)
        if not call or not record or call.finished_at is not None or record.active_call != call.id:
            return
        call.status = outcome if sent else "not_sent"
        call.finished_at = clock
        record_call_usage(call, usage)
        record.active_call, record.available_at = None, None
        if result is not None and not context_available(db, folder, record.context):
            result, outcome = None, "evidence_unavailable"
            call.status = outcome
        if result is not None:
            reply = db.get(ChatMessage, record.reply_id)
            reply.text, reply.created_at = result["answer"], clock
            thread = db.get(Conversation, reply.conversation_id)
            thread.updated_at = max(thread.updated_at, clock)
            record.state, record.error = "answered", None
            record.support, record.citations = answer_citations(result, record.context)
            record.recipient = call.connection
            return
        retries = db.scalar(select(func.count()).select_from(ModelUse).where(
            ModelUse.question_id == record.id, ModelUse.auto_retry.is_(True),
        ))
        plan = db.get(LessonAssistantPlan, record.lesson_id)
        switched = (plan is not None and record.plan_id == plan.id
                    and switch_after_failure(db, answer_scope(plan), record, call, outcome, retries))
        if switched or (outcome in TRANSIENT and retries < 2):
            call.auto_retry = True
            record.state, record.error = "queued", outcome
            record.available_at = None if switched else clock + timedelta(seconds=2 + 4 * retries)
        else:
            block_question(record, outcome)


def dispatch_answer(engine, claim: AnswerClaim, *, transport: Transport = post_json,
                    vault: CredentialStore | None = None, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_answer(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_answer(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        record = db.get(LessonQuestion, claim.question_id)
        call = db.get(ModelUse, claim.id)
        if not call or call.status != "reserved" or call.finished_at is not None or not record or record.active_call != claim.id:
            return
        lesson = db.get(Lesson, record.lesson_id)
        if db.get(Project, lesson.project_id).paused:
            call.status, call.finished_at = "not_sent", when_utc(now())
            record.state, record.active_call = "queued", None
            return
        plan = db.get(LessonAssistantPlan, record.lesson_id)
        problem = check_scope(db, record, plan)
        if not problem:
            recipient = choose_recipient(db, answer_scope(plan), record.failovers)
            if connection_view(recipient.connection) != call.connection:
                problem = "configuration_changed"
        if not problem and not context_available(db, folder, record.context):
            problem = "evidence_unavailable"
        clock = when_utc(now())
        if not problem and plan.max_calls is not None and usage(db, record.lesson_id) > plan.max_calls:
            problem = "task_budget"
        if not problem and (budget_issue := admit_reserved_call(db, call, now, claim.prompt, MAX_OUTPUT_TOKENS)):
            record.daily_blocked_on = call.billing_day
            block_question(record, budget_issue)
            return
    if problem:
        complete_answer(engine, claim, problem, None, None, clock, sent=False)
        return
    reply = request_model(engine, ModelUse, claim.id, claim, key, claim.prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    outcome, result = reply.status, None
    if outcome == "passed":
        try:
            result = parse_model_json(ModelAnswer, reply.text)
        except ValueError:
            outcome = "invalid_response"
    complete_answer(engine, claim, outcome, result, reply.usage, now(), sent=True)


def process_one(engine, *, transport: Transport = post_json, vault: CredentialStore | None = None, now=utc_now) -> bool:
    claim = reserve_answer(engine, now())
    if claim is None:
        return False
    dispatch_answer(engine, claim, transport=transport, vault=vault, now=now)
    return True


def settle_interrupted_answers(engine, now: datetime) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        for call in db.scalars(select(ModelUse).where(ModelUse.question_id.is_not(None), ModelUse.finished_at.is_(None))):
            unsent = settle_interrupted_use(call, clock)
            record = db.get(LessonQuestion, call.question_id)
            if record and record.active_call == call.id:
                if unsent:
                    record.state, record.active_call = "queued", None
                else:
                    block_question(record, "uncertain")
