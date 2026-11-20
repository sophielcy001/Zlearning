from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from .content import lesson_course
from .db import Lesson, Project, Review, Timer, TimerSpan
from .project_activity import record_learning, study_moment
from .study_time import Activity, record_span


class TimerInput(BaseModel):
    action: Literal["start", "pause", "resume", "finish", "stash", "confirm"]
    lessonId: UUID | None = None
    timerId: UUID | None = None
    category: Activity = "learning"
    reviewKey: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None


def active_timer(db: Session) -> Timer | None:
    return db.scalars(select(Timer).where(Timer.status.in_(("running", "paused"))).limit(1)).first()


def elapsed(timer: Timer, now: datetime) -> int:
    active_seconds = max(0, int((now - timer.started_at).total_seconds())) if timer.started_at else 0
    return timer.elapsed + active_seconds


def set_status(db: Session, timer: Timer, status: str, now: datetime, study_time: datetime) -> None:
    if timer.device == "phone":
        if timer.status in ("running", "paused"):
            timer.interrupted_at = now
    else:
        spent = elapsed(timer, now) - timer.elapsed
        record_span(db, timer, spent)
        timer.elapsed += spent
    timer.started_at = now if status == "running" else None
    timer.started_on = study_time.date() if status == "running" else None
    timer.updated_at = now
    timer.status = status


def timer_view(db: Session, now: datetime) -> dict:
    def describe(timer: Timer | None):
        if timer is None:
            return None
        lesson = db.get(Lesson, timer.lesson_id)
        project = db.get(Project, lesson.project_id)
        return {"id": timer.id, "lessonId": lesson.id, "title": lesson_course(project.sample, lesson.position, lesson.outline).title,
                "status": timer.status, "elapsed": elapsed(timer, now), "measuredAt": now.replace(tzinfo=UTC).isoformat(),
                "device": timer.device, "category": timer.category, "reviewKey": timer.review_key,
                "reviewKind": ("model" if lesson.outline else "local") if timer.review_key else None}

    ordered = select(Timer).order_by(Timer.updated_at.desc(), Timer.id)
    return {"active": describe(active_timer(db)),
            "stashed": [describe(timer) for timer in db.scalars(ordered.where(Timer.status == "stashed"))],
            "lastSaved": describe(db.scalars(ordered.where(Timer.status == "finished").limit(1)).first())}


def finish_timer(db: Session, timer: Timer, now: datetime, study_time: datetime) -> None:
    if timer.status == "stashed":
        study_time -= now - timer.updated_at
    set_status(db, timer, "finished", now, study_time)
    record_timer_activity(db, timer, study_time)


def record_timer_activity(db: Session, timer: Timer, study_time: datetime) -> None:
    if timer.elapsed <= 0:
        return
    db.flush()
    last = db.scalar(select(TimerSpan).where(TimerSpan.timer_id == timer.id)
                     .order_by(TimerSpan.ended_at.desc(), TimerSpan.id.desc()).limit(1))
    moment = study_moment(last.ended_at - timedelta(microseconds=1), last.day) if last is not None else study_time
    lesson = db.get(Lesson, timer.lesson_id)
    record_learning(db.get(Project, lesson.project_id), moment)


def timer_lesson(db: Session, lesson_id: UUID | None, category: Activity, review_key: str | None) -> Lesson:
    lesson = db.get(Lesson, str(lesson_id)) if lesson_id else None
    if not lesson or not lesson.ready:
        raise HTTPException(409, "课程准备完成后可以开始计时。")
    if category == "review":
        if review_key is None or db.get(Review, (lesson.id, review_key)) is None:
            raise HTTPException(422, "请从知识目标的复习页开始复习计时。")
    elif review_key is not None:
        raise HTTPException(422, "此计时活动无需指定复习目标。")
    return lesson


def change_timer(db: Session, payload: TimerInput, now: datetime, study_time: datetime,
                 before_start: Callable[[], None]) -> dict:
    current = active_timer(db)
    if payload.action == "start":
        review_key = payload.reviewKey
        lesson = timer_lesson(db, payload.lessonId, payload.category, review_key)
        if (current and current.device == "desktop" and current.lesson_id == lesson.id and current.review_key == review_key
                and (current.category == payload.category or (current.category == "unclassified" and payload.category == "learning"))):
            return timer_view(db, now)
        before_start()
        if current:
            finish_timer(db, current, now, study_time)
        db.add(Timer(id=str(uuid4()), lesson_id=lesson.id, status="running", elapsed=0, started_at=now, updated_at=now,
                     category=payload.category, review_key=review_key, started_on=study_time.date()))
    else:
        timer = db.get(Timer, str(payload.timerId)) if payload.timerId else None
        if timer is None:
            raise HTTPException(404, "找不到这条计时记录。")
        if timer.device == "phone" and payload.action in ("pause", "resume"):
            raise HTTPException(409, "请在正在计时的手机上暂停或继续。")
        transitions = {
            "pause": ({"running", "paused"}, "paused"), "resume": ({"paused", "running"}, "running"),
            "finish": ({"running", "paused", "finished"}, "finished"),
            "stash": ({"running", "paused", "stashed"}, "stashed"), "confirm": ({"stashed", "finished"}, "finished"),
        }
        allowed, target = transitions[payload.action]
        if timer.status not in allowed:
            raise HTTPException(409, "计时状态已变化，请刷新后重试。")
        if timer.status != target:
            if target == "finished":
                finish_timer(db, timer, now, study_time)
            else:
                set_status(db, timer, target, now, study_time)
    db.flush()
    return timer_view(db, now)
