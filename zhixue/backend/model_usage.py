from collections.abc import Callable
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from .db import AgentAuthoringJob, ChatModelCall, GenerationSettings, GoalAuthoringJob, GraphRecommendation, LibraryClassification, MemoryInferenceJob, ModelCharge, ModelUse, NoteCheckBatch, NoteSuggestion, PrerequisiteCall, StageReviewCall
from .model_ledger import DIRECT_CALL_SOURCES, prepare_call_charge, prepare_charge, utc_clock
from .model_study_input import DEFAULT_CONCURRENCY
from .money_budget import DAILY_BUDGET_REASONS, authorize_charge
from .project_activity import SHANGHAI


CALL_TABLES = (ModelUse, ChatModelCall, PrerequisiteCall, StageReviewCall, NoteSuggestion, NoteCheckBatch, GraphRecommendation, LibraryClassification, GoalAuthoringJob, AgentAuthoringJob, MemoryInferenceJob)


def count_calls(db, predicate) -> int:
    counts = (select(func.count()).select_from(table).where(*predicate(table)).scalar_subquery()
              for table in CALL_TABLES)
    return db.scalar(select(sum(counts)))


def active_calls(db) -> int:
    return (count_calls(db, lambda table: (table.started_at.is_not(None), table.finished_at.is_(None)))
            + charge_count(db, ModelCharge.source.in_(DIRECT_CALL_SOURCES), ModelCharge.finished_at.is_(None)))


def billed_calls(db, day: date) -> int:
    return (charge_count(db, ModelCharge.billing_day == day)
            + count_calls(db, lambda table: (table.billing_day == day, table.status == "reserved")))


def charge_count(db, *scope) -> int:
    return db.scalar(select(func.count()).select_from(ModelCharge).where(ModelCharge.status != "not_sent", *scope))


def billing_day(now: datetime) -> date:
    instant = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return instant.astimezone(SHANGHAI).date()


def daily_capacity(limit: int | None, used: int) -> bool:
    return limit is None or used < limit


def auto_resume_enabled(settings: dict, used: int) -> bool:
    return settings["autoContinueDailyBudget"] and daily_capacity(settings["dailyLimit"], used)


def queued_with_daily_recovery(state, reason, blocked_on, day: date, automatic: bool):
    queued = state == "queued"
    if automatic:
        queued |= ((state == "blocked") & reason.in_(DAILY_BUDGET_REASONS) & (blocked_on < day))
    return queued


def admit_reserved_call(db, call, now: Callable[[], datetime], prompt: str, max_output: int,
                        *, recipient: dict | None = None, scope_id: str | None = None) -> str | None:
    clock = utc_clock(now())
    call.billing_day = billing_day(clock)
    settings = db.get(GenerationSettings, 1)
    if settings is not None and not daily_capacity(settings.daily_limit, billed_calls(db, call.billing_day) - 1):
        issue = "daily_budget"
    else:
        charge = prepare_call_charge(db, call, clock, recipient, scope_id)
        issue = authorize_charge(db, charge, prompt, max_output)
    if issue:
        call.status, call.finished_at = "not_sent", clock
    else:
        call.status = "sending"
    return issue


def settle_interrupted_use(call: ModelUse | PrerequisiteCall, clock: datetime) -> bool:
    """Close a call after worker restart and indicate whether it was still unsent."""
    unsent = call.status == "reserved"
    db = object_session(call)
    if not unsent and db is not None:
        charge = db.get(ModelCharge, (call.__tablename__, call.id))
        unsent = charge is not None and charge.status == "not_sent"
    call.status = "not_sent" if unsent else "uncertain"
    call.finished_at = max(clock, call.started_at)
    return unsent


def admit_probe(db, attempt: str, identity: str, clock: datetime, recipient: dict,
                prompt: str, max_output: int) -> str | None:
    settings = db.get(GenerationSettings, 1)
    day = billing_day(clock)
    if active_calls(db) >= (settings.concurrency if settings else DEFAULT_CONCURRENCY):
        return "concurrency"
    if settings is not None and not daily_capacity(settings.daily_limit, billed_calls(db, day)):
        return "daily_budget"
    charge = prepare_charge(db, "probe", attempt, "probe", identity, day, clock, recipient)
    return authorize_charge(db, charge, prompt, max_output, task_id=attempt)
