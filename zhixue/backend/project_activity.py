from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .db import DemoClock, Project


SHANGHAI = ZoneInfo("Asia/Shanghai")


def study_day(db: Session, today: date) -> date:
    clock = db.get(DemoClock, 1)
    return today + timedelta(days=clock.offset if clock else 0)


def study_moment(now: datetime, day: date | None = None) -> datetime:
    instant = now if now.tzinfo else now.replace(tzinfo=UTC)
    local = instant.astimezone(SHANGHAI).replace(tzinfo=None)
    return datetime.combine(day, local.time()) if day is not None else local


def touch_project(project: Project, moment: datetime) -> None:
    project.updated_at = max(project.updated_at or moment, moment)


def record_learning(project: Project, moment: datetime) -> None:
    project.last_learned_at = max(project.last_learned_at or moment, moment)
