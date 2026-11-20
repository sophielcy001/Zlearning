from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from .db import transaction
from .model_ledger import record_call_usage
from .model_study import settings_view, when_utc
from .model_study_input import pinned_connection
from .model_usage import active_calls, auto_resume_enabled, billed_calls, billing_day, daily_capacity, queued_with_daily_recovery


ACTIVE_STATES = ("queued", "running", "blocked")


@dataclass(frozen=True)
class ScopedClaim:
    id: str
    model: str
    provider: str
    protocol: str
    address: str
    credential: str | None


def block(job, day, reason: str = "daily_budget") -> None:
    job.state, job.error, job.daily_blocked_on = "blocked", reason, day


def fail(job, reason: str) -> None:
    job.state, job.error, job.scope, job.daily_blocked_on = "failed", reason, None, None


def reserve_scoped(engine, table, claim_type, now: datetime):
    clock, day = when_utc(now), billing_day(now)
    with transaction(engine) as db:
        settings = settings_view(db)
        if active_calls(db) >= settings["concurrency"]:
            return None
        used = billed_calls(db, day)
        eligible = queued_with_daily_recovery(
            table.state, table.error, table.daily_blocked_on, day, auto_resume_enabled(settings, used),
        )
        pending = select(table).where(eligible).order_by(table.created_at, table.id)
        for job in db.scalars(pending):
            model = pinned_connection(db, "main", job.connection)
            if model is None:
                fail(job, "configuration_changed")
                continue
            if not daily_capacity(settings["dailyLimit"], used):
                block(job, day)
                continue
            job.state, job.error, job.daily_blocked_on = "running", None, None
            job.started_at, job.billing_day, job.finished_at, job.status = clock, day, None, "reserved"
            return claim_type(job.id, model.model, model.provider, model.protocol, job.connection["address"], model.credential_ref)
    return None


def finish_scoped(job, status: str, usage: dict | None, clock: datetime, *, sent: bool) -> None:
    job.status, job.finished_at = status if sent else "not_sent", clock
    record_call_usage(job, usage)
    if job.state == "cancelled":
        return
    if status != "passed":
        fail(job, status)
    else:
        job.scope = None
        job.state = "ready"


def cancel_scoped(job, clock: datetime) -> None:
    if job.state in ACTIVE_STATES:
        job.state, job.error, job.scope, job.daily_blocked_on = "cancelled", None, None, None
        if job.status == "reserved":
            job.status, job.finished_at = "not_sent", clock


def settle_scoped(engine, table, now: datetime) -> None:
    with transaction(engine) as db:
        unsettled = select(table).where(table.started_at.is_not(None), table.finished_at.is_(None))
        for job in db.scalars(unsettled):
            if job.state == "running" and job.status == "reserved":
                job.state, job.status, job.started_at, job.billing_day = "queued", None, None, None
            else:
                finish_scoped(job, "uncertain", None, when_utc(now), sent=job.status == "sending")
