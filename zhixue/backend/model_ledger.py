from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, object_session

from .db import (
    AssessmentJob, ChatModelCall, ChatReply, GoalAuthoringJob, GraphRecommendation, LibraryClassification, ModelCharge,
    ModelConnection, ModelUse, NoteCheckBatch, NoteSuggestion, PrerequisiteCall, StageReviewCall, transaction,
)
from .model_costs import ModelUsage, amount_text, estimate_usage
from .model_protocols import TextReply, Transport, call_text
from .money_budget import assessment_budget_scope


CALL_SCOPES = {
    ChatModelCall: ("chat", "reply_id"),
    PrerequisiteCall: ("prerequisite", "task_id"),
    StageReviewCall: ("stage_review", "review_id"),
    NoteSuggestion: ("note_ai", "id"),
    NoteCheckBatch: ("note_check", "plan_id"),
    GraphRecommendation: ("graph", "id"),
    LibraryClassification: ("classification", "id"),
    GoalAuthoringJob: ("goal_authoring", "id"),
}
DIRECT_CALL_SOURCES = ("probe", "recognition")
RECIPIENT_FIELDS = ("id", "revision", "name", "provider", "model")
PAGE_SIZE = 20


def safe_recipient(recipient: dict | None) -> dict | None:
    if recipient is None:
        return None
    return {key: recipient[key] for key in RECIPIENT_FIELDS if key in recipient}


def prepare_charge(db: Session, source: str, identity: str, kind: str, scope: str, day: date, clock: datetime,
                   recipient: dict | None, *, phase: str | None = None) -> ModelCharge:
    connection = db.get(ModelConnection, recipient["id"]) if recipient else None
    pricing = connection.pricing if connection and connection.revision == recipient.get("revision") else None
    return ModelCharge(
        source=source, call_id=identity, kind=kind, scope_id=scope, phase=phase,
        recipient=safe_recipient(recipient), pricing=pricing, billing_day=day, started_at=clock, status="sending",
    )


def prepare_call_charge(db: Session, call, clock: datetime, recipient: dict | None = None,
                        scope_id: str | None = None) -> ModelCharge:
    phase = getattr(call, "phase", None)
    if isinstance(call, ModelUse):
        if call.assessment_job_id:
            job = db.get(AssessmentJob, call.assessment_job_id)
            kind, identity = "assessment", assessment_budget_scope(job) if job else call.assessment_job_id
        else:
            kind, identity = next(((kind, target) for kind, target in (
                ("answer", call.question_id), ("consolidation", call.consolidation_id),
            ) if target), ("generation", call.project_id))
    else:
        kind, identity_field = CALL_SCOPES[type(call)]
        identity = getattr(call, identity_field)
    if isinstance(call, ChatModelCall):
        reply = db.get(ChatReply, call.reply_id)
        recipient = reply.recipient if reply else None
    elif isinstance(call, NoteSuggestion):
        phase = call.operation
    return prepare_charge(db, call.__tablename__, call.id, kind, scope_id or identity, call.billing_day, clock,
                          recipient or getattr(call, "connection", None), phase=phase)


def finish_charge(charge: ModelCharge, status: str, usage: ModelUsage | None, clock: datetime) -> None:
    if charge.finished_at is not None:
        return
    data = usage or {}
    charge.finished_at = max(clock, charge.started_at)
    charge.status = "uncertain" if status == "not_sent" else status
    charge.input_tokens, charge.output_tokens = data.get("inputTokens"), data.get("outputTokens")
    charge.total_tokens, charge.thinking_tokens = data.get("totalTokens"), data.get("thinkingTokens")
    charge.audio_seconds = data.get("audioSeconds")
    charge.actual_currency, charge.actual_amount = data.get("actualCurrency"), data.get("actualAmount")
    if charge.actual_amount is None:
        charge.estimated_amount = estimate_usage(charge.pricing, data)
        if data.get("specialPricing"):
            # The response invalidates the standard token-rate reservation.
            charge.reserved_amount = None


def utc_clock(clock: datetime) -> datetime:
    return clock.astimezone(UTC).replace(tzinfo=None) if clock.tzinfo is not None else clock


def save_charge_reply(engine, source: str, identity: str, status: str, usage: ModelUsage | None, clock: datetime) -> None:
    with transaction(engine) as db:
        charge = db.get(ModelCharge, (source, identity))
        if charge is not None:
            finish_charge(charge, status, usage, utc_clock(clock))


def request_model(engine, table, identity: str, recipient, key: str | None, prompt: str, max_output: int,
                  transport: Transport, now: Callable[[], datetime]) -> TextReply:
    reply = call_text(recipient.provider, recipient.protocol, recipient.address, recipient.model, key, prompt, max_output, transport)
    # A failure applying the response must not roll back the provider's usage.
    save_charge_reply(engine, table.__tablename__, identity, reply.status, reply.usage, now())
    return reply


def record_call_usage(call, usage: ModelUsage | None) -> None:
    db = object_session(call)
    charge = db.get(ModelCharge, (call.__tablename__, call.id))
    if usage is None and charge is not None and charge.finished_at is not None:
        call.input_tokens, call.output_tokens = charge.input_tokens, charge.output_tokens
    else:
        data = usage or {}
        call.input_tokens, call.output_tokens = data.get("inputTokens"), data.get("outputTokens")
    if charge is not None:
        finish_charge(charge, call.status, usage, call.finished_at)


def settle_open_charges(db: Session, clock: datetime, *, direct: bool | None = None) -> None:
    statement = update(ModelCharge).where(ModelCharge.finished_at.is_(None))
    if direct is not None:
        match = ModelCharge.source.in_(DIRECT_CALL_SOURCES)
        statement = statement.where(match if direct else ~match)
    db.execute(statement.values(
        status="uncertain", finished_at=case((ModelCharge.started_at > clock, ModelCharge.started_at), else_=clock),
    ))


def retain_charges(current: Session, restored: Session) -> None:
    table = ModelCharge.__table__
    add = insert(table)
    newer = add.excluded
    more_complete = (
        (table.c.finished_at.is_(None) & newer.finished_at.is_not(None))
        | (table.c.actual_amount.is_(None) & newer.actual_amount.is_not(None))
        | (table.c.actual_amount.is_(None) & table.c.estimated_amount.is_(None) & newer.estimated_amount.is_not(None))
        | (table.c.actual_amount.is_(None) & table.c.estimated_amount.is_(None)
           & table.c.reserved_amount.is_(None) & newer.reserved_amount.is_not(None))
    )
    keep = add.on_conflict_do_update(
        index_elements=[table.c.source, table.c.call_id],
        set_={column.name: newer[column.name] for column in table.c if not column.primary_key},
        where=more_complete,
    )
    for rows in current.execute(select(table)).mappings().partitions(500):
        restored.execute(keep, [dict(row) for row in rows])


def sum_usage(db: Session, *scope) -> dict:
    records = db.execute(select(
        ModelCharge.finished_at, ModelCharge.actual_currency, ModelCharge.actual_amount, ModelCharge.estimated_amount,
        ModelCharge.pricing, ModelCharge.input_tokens, ModelCharge.output_tokens, ModelCharge.total_tokens,
        ModelCharge.thinking_tokens, ModelCharge.audio_seconds,
    ).where(*scope))
    currencies = {}
    counts = dict(calls=0, inFlight=0, unknownCalls=0, reportedCalls=0)
    usage = dict(inputTokens=None, outputTokens=None, totalTokens=None, thinkingTokens=None, audioSeconds=None)
    with localcontext() as context:
        context.prec = 60
        for row in records:
            counts["calls"] += 1
            if row.finished_at is None:
                counts["inFlight"] += 1
                continue
            if row.actual_amount is not None:
                currency, field, amount = row.actual_currency, "actual", row.actual_amount
            elif row.estimated_amount is not None:
                currency, field, amount = row.pricing["currency"], "estimated", row.estimated_amount
            else:
                counts["unknownCalls"] += 1
                currency = None
            if currency is not None:
                totals = currencies.setdefault(currency, {"actual": None, "estimated": None})
                totals[field] = (totals[field] or Decimal(0)) + Decimal(amount)
            supplied = (row.input_tokens, row.output_tokens, row.total_tokens, row.thinking_tokens, row.audio_seconds)
            if any(value is not None for value in supplied):
                counts["reportedCalls"] += 1
            for label, value in zip(usage, supplied):
                if value is not None:
                    usage[label] = (usage[label] or 0) + value
    return {
        **counts, **usage,
        "currencies": [{"currency": currency, **{key: amount_text(value) if value is not None else None
                                               for key, value in totals.items()}}
                       for currency, totals in sorted(currencies.items())],
    }


def charge_view(charge: ModelCharge) -> dict:
    return {
        "id": charge.call_id, "kind": charge.kind, "scopeId": charge.scope_id, "phase": charge.phase,
        "recipient": safe_recipient(charge.recipient), "pricing": charge.pricing, "status": charge.status,
        "startedAt": utc_clock(charge.started_at).isoformat() + "Z",
        "finishedAt": utc_clock(charge.finished_at).isoformat() + "Z" if charge.finished_at else None,
        "inputTokens": charge.input_tokens, "outputTokens": charge.output_tokens,
        "totalTokens": charge.total_tokens, "thinkingTokens": charge.thinking_tokens,
        "audioSeconds": charge.audio_seconds,
        "actualCost": {"currency": charge.actual_currency, "amount": charge.actual_amount} if charge.actual_amount is not None else None,
        "estimatedCost": {"currency": charge.pricing["currency"], "amount": charge.estimated_amount} if charge.estimated_amount is not None else None,
        "reservedCost": {"currency": charge.pricing["currency"], "amount": charge.reserved_amount}
        if charge.actual_amount is None and charge.estimated_amount is None and charge.reserved_amount is not None else None,
    }


def ledger_router(engine, today: Callable[[], date]) -> APIRouter:
    router = APIRouter(prefix="/api/generation")

    @router.get("/usage")
    def usage(billingDay: date | None = None, page: int = Query(default=1, ge=1, le=2**31 - 1)):
        day = billingDay or today()
        scope = ModelCharge.billing_day == day
        with transaction(engine, write=False) as db:
            summary = sum_usage(db, scope)
            total = summary["calls"]
            last = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, last)
            rows = db.scalars(select(ModelCharge).where(scope)
                              .order_by(ModelCharge.started_at.desc(), ModelCharge.source, ModelCharge.call_id)
                              .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE))
            return {"billingDay": day, "page": page, "lastPage": last, "total": total,
                    "summary": summary, "items": [charge_view(row) for row in rows]}

    return router
