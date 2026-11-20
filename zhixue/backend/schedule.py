from calendar import monthrange
from datetime import date, timedelta


DAYS = (1, 2, 3, 7, 14, 21)
MONTHS = (1, 2, 3, 12)
LAST_STAGE = len(DAYS) + len(MONTHS) - 1


def next_review_date(submitted_on: date, stage: int) -> date:
    if stage < len(DAYS):
        return submitted_on + timedelta(days=DAYS[stage])
    total = submitted_on.month - 1 + MONTHS[stage - len(DAYS)]
    year = submitted_on.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(submitted_on.day, monthrange(year, month)[1]))


def settle_review(submitted_on: date, stage: int, outcome: str) -> tuple[int, date | None]:
    if outcome == "hinted":
        return stage, next_review_date(submitted_on, 0)
    if outcome == "failed":
        return 0, next_review_date(submitted_on, 0)
    if stage == LAST_STAGE:
        return stage, None
    return stage + 1, next_review_date(submitted_on, stage + 1)
