from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import AssessmentJob, GenerationSettings, ModelCharge, ModelConnection, ModelTaskBudget
from .model_costs import MoneyLimit, amount_text, estimate_usage


STOPPED_COST = "cost_stopped"
MONEY_ERRORS = {
    "workspace_cost": "本次预估金额超出联调金额上限，未发送该请求。",
    "task_cost": "本次预估金额超出该任务的可用额度。可提高任务金额上限后继续。",
    "daily_cost": "本次预估金额超出今日可用额度。可提高每日上限，或等额度恢复后继续。",
    "cost_unavailable": "金额额度暂不可用。请检查模型单价、币种和账本中的未知费用，或改用调用次数额度。",
    STOPPED_COST: "已停止新增模型调用，任务进度已保留。",
}
DAILY_BUDGET_REASONS = ("daily_budget", "daily_cost")
BUDGET_REASONS = ("daily_budget", *MONEY_ERRORS)
# UTF-8 bytes with a margin for the single message and protocol framing.
# This is a local reservation estimate, not a model-specific token count.
INPUT_MARGIN = 128


class RequestSize(TypedDict):
    recipientId: str
    revision: int
    inputUnits: int
    maxOutput: int


@dataclass
class Balance:
    settled: Decimal = Decimal(0)
    reserved: Decimal = Decimal(0)
    unknown: int = 0

    def view(self) -> dict:
        return {"settled": amount_text(self.settled), "reserved": amount_text(self.reserved),
                "unknownCalls": self.unknown}


def balance(db: Session, currency: str, *scope) -> Balance:
    rows = db.execute(select(ModelCharge.actual_currency, ModelCharge.actual_amount,
                             ModelCharge.estimated_amount, ModelCharge.pricing,
                             ModelCharge.reserved_amount).where(ModelCharge.status != "not_sent", *scope))
    totals = Balance()
    with localcontext() as context:
        context.prec = 60
        for actual_currency, actual, estimated, pricing, reserved in rows:
            charge_currency = actual_currency if actual is not None else (pricing or {}).get("currency")
            if charge_currency != currency:
                totals.unknown += 1
            elif actual is not None or estimated is not None:
                totals.settled += Decimal(actual if actual is not None else estimated)
            elif reserved is not None:
                totals.reserved += Decimal(reserved)
            else:
                totals.unknown += 1
    return totals


def daily_cost_view(db: Session, day: date) -> dict | None:
    settings = db.get(GenerationSettings, 1)
    limit = settings.daily_cost_limit if settings else None
    return {"limit": limit, **balance(db, limit["currency"], ModelCharge.billing_day == day).view()} if limit else None


def assessment_budget_scope(job: AssessmentJob) -> str:
    return job.remediation_id or job.round_id or job.id


def task_limit(db: Session, kind: str, identity: str) -> tuple[dict | None, bool]:
    record = db.get(ModelTaskBudget, (kind, identity))
    if record is not None and record.limit_override is not None:
        return record.limit_override["limit"], False
    settings = db.get(GenerationSettings, 1)
    return settings.default_task_cost_limit if settings else None, True


def budget_record(db: Session, kind: str, identity: str) -> ModelTaskBudget:
    record = db.get(ModelTaskBudget, (kind, identity))
    if record is None:
        record = ModelTaskBudget(kind=kind, scope_id=identity)
        db.add(record)
    return record


def set_task_limit(db: Session, kind: str, identity: str, limit: MoneyLimit | None, inherit: bool) -> None:
    record = budget_record(db, kind, identity)
    record.limit_override = None if inherit else {"limit": limit.model_dump() if limit else None}
    db.flush()


def charge_quote(pricing: dict | None, size: RequestSize | None) -> str | None:
    if size is None:
        return None
    return estimate_usage(pricing, {"inputTokens": size["inputUnits"], "outputTokens": size["maxOutput"]})


def request_size(recipient: dict, prompt: str, max_output: int) -> RequestSize:
    return RequestSize(recipientId=recipient["id"], revision=recipient["revision"],
                       inputUnits=len(prompt.encode("utf-8")) + INPUT_MARGIN, maxOutput=max_output)


def amount_issue(db: Session, kind: str, identity: str, day: date,
                 pricing: dict | None, quote: str | None) -> str | None:
    task, _ = task_limit(db, kind, identity)
    settings = db.get(GenerationSettings, 1)
    daily = settings.daily_cost_limit if settings else None
    workspace = db.get_bind().get_execution_options().get("workspace_cost_limit")
    limits = (
        (workspace, "workspace_cost", ()),
        (task, "task_cost", (ModelCharge.kind == kind, ModelCharge.scope_id == identity)),
        (daily, "daily_cost", (ModelCharge.billing_day == day,)),
    )
    for limit, reason, scope in limits:
        if limit is None:
            continue
        maximum = Decimal(limit["amount"])
        if maximum == 0:
            return reason
        if quote is None or pricing["currency"] != limit["currency"]:
            return "cost_unavailable"
        totals = balance(db, limit["currency"], *scope)
        if totals.unknown:
            return "cost_unavailable"
        with localcontext() as context:
            context.prec = 60
            used = totals.settled + totals.reserved
            if used >= maximum or used + Decimal(quote) > maximum:
                return reason
    return None


def authorize_charge(db: Session, charge: ModelCharge, prompt: str, max_output: int,
                     *, task_id: str | None = None) -> str | None:
    recipient = charge.recipient
    size = request_size(recipient, prompt, max_output) if recipient else None
    quote = charge_quote(charge.pricing, size)
    identity = task_id or charge.scope_id
    issue = amount_issue(db, charge.kind, identity, charge.billing_day, charge.pricing, quote)
    if issue:
        if size is not None and charge.source != "probe":
            budget_record(db, charge.kind, identity).held_request = size
        return issue
    if charge.source != "probe":
        record = db.get(ModelTaskBudget, (charge.kind, identity))
        if record is not None:
            record.held_request = None
    charge.reserved_amount = quote
    db.add(charge)
    return None


def request_issue(db: Session, kind: str, identity: str, day: date,
                  connection: ModelConnection, prompt: str, max_output: int) -> str | None:
    size = request_size({"id": connection.id, "revision": connection.revision}, prompt, max_output)
    return amount_issue(db, kind, identity, day, connection.pricing, charge_quote(connection.pricing, size))


def held_issue(db: Session, kind: str, identity: str, day: date) -> str | None:
    record = db.get(ModelTaskBudget, (kind, identity))
    if record is None or record.held_request is None:
        return "cost_unavailable"
    size = record.held_request
    connection = db.get(ModelConnection, size["recipientId"])
    pricing = connection.pricing if connection and connection.revision == size["revision"] else None
    return amount_issue(db, kind, identity, day, pricing, charge_quote(pricing, size))
