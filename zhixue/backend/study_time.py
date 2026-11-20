from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Timer, TimerCorrection, TimerSpan
from .project_activity import SHANGHAI, study_moment


Activity = Literal["learning", "review", "assistant", "notes"]
TimeCategory = Literal["learning", "review", "assistant", "notes", "unclassified"]
CATEGORIES = ("learning", "review", "assistant", "notes", "unclassified")


def record_span(db: Session, timer: Timer, seconds: int) -> None:
    if not seconds or timer.started_at is None or timer.started_on is None:
        return
    # Use the same whole-second duration as Timer.elapsed across midnight.
    start = timer.started_at.replace(microsecond=0)
    end = start + timedelta(seconds=seconds)
    record_interval(db, timer, start, end, timer.started_on - study_moment(start).date())


def record_interval(db: Session, timer: Timer, start: datetime, end: datetime, shift: timedelta) -> None:
    while start < end:
        local_day = study_moment(start).date()
        next_day = datetime.combine(local_day + timedelta(days=1), time.min, SHANGHAI)
        boundary = next_day.astimezone(UTC).replace(tzinfo=None)
        stop = min(boundary, end)
        db.add(TimerSpan(timer_id=timer.id, day=local_day + shift, began_at=start, ended_at=stop,
                         seconds=int((stop - start).total_seconds())))
        start = stop


@dataclass(frozen=True)
class TimeEntry:
    timer: Timer
    source_on: date | None
    report_on: date | None
    recorded_seconds: int
    seconds: int
    category: str
    revision: int = 0

    def view(self) -> dict:
        return {
            "timerId": self.timer.id, "lessonId": self.timer.lesson_id, "reviewKey": self.timer.review_key,
            "sourceOn": self.source_on, "reportOn": self.report_on, "recordedSeconds": self.recorded_seconds,
            "seconds": self.seconds, "recordedCategory": self.timer.category, "category": self.category, "revision": self.revision,
        }


def source_key(day: date | None) -> str:
    return day.isoformat() if day is not None else ""


def apply_correction(timer: Timer, day: date | None, seconds: int, latest: TimerCorrection | None) -> TimeEntry:
    original = TimeEntry(timer, day, day, seconds, seconds, timer.category)
    if latest is None:
        return original
    return replace(original, report_on=latest.report_on, seconds=latest.seconds, category=latest.category, revision=latest.revision)


def confirmed_times(db: Session, start: date | None = None, through: date | None = None) -> list[TimeEntry]:
    latest = (select(TimerCorrection.timer_id, TimerCorrection.source_key, func.max(TimerCorrection.revision).label("revision"))
              .group_by(TimerCorrection.timer_id, TimerCorrection.source_key).subquery())
    changes = {(item.timer_id, item.source_key): item for item in db.scalars(
        select(TimerCorrection).join(latest, (TimerCorrection.timer_id == latest.c.timer_id)
                                     & (TimerCorrection.source_key == latest.c.source_key)
                                     & (TimerCorrection.revision == latest.c.revision))
    )}
    grouped = (select(Timer, TimerSpan.day, func.sum(TimerSpan.seconds))
               .select_from(TimerSpan).join(Timer, Timer.id == TimerSpan.timer_id).where(Timer.status == "finished")
               .group_by(Timer.id, TimerSpan.day))
    if start is not None:
        grouped = grouped.where(TimerSpan.day >= start)
    if through is not None:
        grouped = grouped.where(TimerSpan.day <= through)
    result = [apply_correction(timer, day, seconds, changes.get((timer.id, source_key(day))))
              for timer, day, seconds in db.execute(grouped)]
    recorded = (select(func.sum(TimerSpan.seconds)).where(TimerSpan.timer_id == Timer.id).correlate(Timer).scalar_subquery())
    remaining = Timer.elapsed - func.coalesce(recorded, 0)
    for timer, seconds in db.execute(select(Timer, remaining).where(Timer.status == "finished", remaining > 0)):
        entry = apply_correction(timer, None, seconds, changes.get((timer.id, "")))
        if entry.report_on is None or ((start is None or start <= entry.report_on) and (through is None or entry.report_on <= through)):
            result.append(entry)
    return result


def correct_time(db: Session, identity: str, source_on: date | None, report_on: date | None, seconds: int,
                 category: str, base_revision: int, now: datetime) -> TimeEntry:
    timer = db.get(Timer, identity)
    if timer is None or timer.status != "finished":
        raise HTTPException(404, "找不到已确认的计时记录。")
    if source_on is not None and report_on != source_on:
        raise HTTPException(422, "有计时区间的记录使用原学习日。")
    if report_on is not None and seconds > 86_400:
        raise HTTPException(422, "一天内的有效时长最多为 24 小时。")
    spans = select(func.coalesce(func.sum(TimerSpan.seconds), 0)).where(TimerSpan.timer_id == timer.id)
    recorded = (db.scalar(spans.where(TimerSpan.day == source_on)) if source_on is not None
                else timer.elapsed - db.scalar(spans))
    if recorded <= 0:
        raise HTTPException(404, "找不到该日期的原始计时。")
    key = source_key(source_on)
    latest = db.scalar(select(TimerCorrection).where(TimerCorrection.timer_id == identity, TimerCorrection.source_key == key)
                       .order_by(TimerCorrection.revision.desc()).limit(1))
    saved = apply_correction(timer, source_on, recorded, latest)
    if (saved.report_on, saved.seconds, saved.category) == (report_on, seconds, category):
        return saved
    if saved.revision != base_revision:
        raise HTTPException(409, "此计时已校正，请刷新报告后再修改。")
    correction = TimerCorrection(timer_id=identity, source_key=key, revision=saved.revision + 1,
                                 report_on=report_on, seconds=seconds, category=category, saved_at=now)
    db.add(correction)
    return apply_correction(timer, source_on, recorded, correction)
