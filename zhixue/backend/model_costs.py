from decimal import Decimal, localcontext
from typing import Annotated, TypedDict

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


Price = Annotated[str, StringConstraints(pattern=r"^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,8})?$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: Currency
    inputPerMillion: Price
    outputPerMillion: Price

    @field_validator("inputPerMillion", "outputPerMillion")
    @classmethod
    def canonical_amount(cls, value: str) -> str:
        return amount_text(Decimal(value))


class MoneyLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: Currency
    amount: Price

    @field_validator("amount")
    @classmethod
    def canonical_amount(cls, value: str) -> str:
        return amount_text(Decimal(value))


class ModelUsage(TypedDict, total=False):
    inputTokens: int
    outputTokens: int
    totalTokens: int
    thinkingTokens: int
    audioSeconds: float
    actualCurrency: str
    actualAmount: str
    specialPricing: bool


def amount_text(amount: Decimal) -> str:
    value = format(amount, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def provider_amount(value) -> str | None:
    if type(value) not in (int, Decimal):
        return None
    amount = Decimal(value)
    if not amount.is_finite() or not 0 <= amount <= 100_000_000 or amount.as_tuple().exponent < -24:
        return None
    return "0" if amount.is_zero() else amount_text(amount)


def estimate_usage(pricing: dict | None, usage: ModelUsage) -> str | None:
    if pricing is None or usage.get("specialPricing") or "inputTokens" not in usage or "outputTokens" not in usage:
        return None
    with localcontext() as context:
        context.prec = 60
        total = (Decimal(pricing["inputPerMillion"]) * usage["inputTokens"]
                 + Decimal(pricing["outputPerMillion"]) * (usage["outputTokens"] + usage.get("thinkingTokens", 0)))
        return amount_text(total / 1_000_000)
