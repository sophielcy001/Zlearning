"""Phone timer operations and reconciliation against the workspace timeline."""
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from .content import lesson_course
from .daily_plan import freeze_plan_baseline
from .db import Lesson, MobileChange, Project, StudyPlanSnapshot, Timer, TimerSpan, transaction
from .mobile_api import Change, previous, workspace_id
from .project_activity import study_moment
from .study_time import Activity, record_interval
from .timer import active_timer, finish_timer, record_timer_activity, timer_lesson


MAX_LIVE_AGE = 8
CLOCK_TOLERANCE = 2


class PhoneSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beganAt: AwareDatetime
    endedAt: AwareDatetime
    reliable: bool
    dayShift: int = Field(ge=-36_600, le=36_600)
    startedBy: UUID | None = None


class PhoneTimerEvent(Change):
    kind: Literal["timer"]
    timerId: UUID
    lessonId: UUID
    category: Activity
    reviewKey: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    action: Literal["start", "pause", "resume", "finish"]
    live: bool = False
    present: bool = False
    expected: UUID | None = None
    span: PhoneSpan | None = None


class ReadClock(BaseModel):
    workspace: UUID
    timerId: UUID | None = None


class TimeResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["keep", "confirm"]
    beganAt: AwareDatetime | None = None
    endedAt: AwareDatetime | None = None


def as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def stamp(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat()


def split_overlap(db, timer: Timer, start: datetime, end: datetime, now: datetime):
    blocks = [(span.began_at, span.ended_at, span.timer_id, device) for span, device in db.execute(
        select(TimerSpan, Timer.device).join(Timer, Timer.id == TimerSpan.timer_id)
        .where(TimerSpan.began_at < end, TimerSpan.ended_at > start)
    )]
    current = active_timer(db)
    if current and current.id != timer.id and current.started_at:
        blocks.append((current.started_at.replace(microsecond=0), now, current.id, current.device))
    if timer.interrupted_at is not None:
        blocks.append((timer.interrupted_at.replace(microsecond=0), end, timer.id, "handoff"))

    allowed = []
    overlaps = []
    cursor = start
    for left, right, identity, device in sorted(blocks):
        left, right = max(left, start), min(right, end)
        if left >= right:
            continue
        overlaps.append({"beganAt": stamp(left), "endedAt": stamp(right), "timerId": identity, "device": device})
        if left > cursor:
            allowed.append((cursor, left))
        cursor = max(right, cursor)
    if cursor < end:
        allowed.append((cursor, end))
    return allowed, overlaps


def account_span(db, timer: Timer, span: PhoneSpan, now: datetime) -> tuple[dict, list[dict]]:
    pending = {"state": "held", "reason": "clock", "seconds": 0}
    if not span.reliable:
        return pending, []
    try:
        start, end = as_utc(span.beganAt), as_utc(span.endedAt)
        shift = timedelta(days=span.dayShift)
        # Reject unusable date arithmetic before recording any daily split.
        study_moment(start).date() + shift
        study_moment(end).date() + shift
    except (OverflowError, ValueError):
        return pending, []
    if end < start or end - start > timedelta(days=366) or end > now + timedelta(seconds=CLOCK_TOLERANCE):
        return pending, []
    if span.startedBy is not None:
        beginning = db.get(MobileChange, str(span.startedBy))
        if beginning is not None and beginning.kind == "timer" and beginning.details.get("timerId") == timer.id:
            joined = (beginning.reply or {}).get("claimedAt")
            if joined is not None:
                arrived = as_utc(datetime.fromisoformat(joined))
                if start <= arrived <= start + timedelta(seconds=MAX_LIVE_AGE + CLOCK_TOLERANCE):
                    start = min(end, arrived)
    start = start.replace(microsecond=0)
    end = min(end, now).replace(microsecond=0)
    if end <= start:
        return {"state": "saved", "seconds": 0}, []
    available, overlaps = split_overlap(db, timer, start, end, now.replace(microsecond=0))
    recorded_days: set[date] = set()
    for left, right in available:
        recorded_days.update(record_interval(db, timer, left, right, shift))
    if recorded_days:
        # A replay can preserve a saved plan, but cannot reconstruct an earlier schedule.
        db.execute(update(StudyPlanSnapshot).where(
            StudyPlanSnapshot.day.in_(recorded_days), StudyPlanSnapshot.frozen.is_(False),
        ).values(frozen=True))
    added = sum(int((right - left).total_seconds()) for left, right in available)
    timer.elapsed += added
    result = {"state": "held", "reason": "overlap"} if overlaps else {"state": "saved"}
    return {**result, "seconds": added}, overlaps


def accept_event(db, data: PhoneTimerEvent, now: datetime, moment: datetime, before_start) -> tuple[dict, list[dict]]:
    identity = str(data.timerId)
    timer = db.get(Timer, identity)
    if data.action == "start":
        if timer is not None:
            raise HTTPException(409, "此手机计时编号已使用。")
        lesson = timer_lesson(db, data.lessonId, data.category, data.reviewKey)
        timer = Timer(id=identity, lesson_id=lesson.id, review_key=data.reviewKey, category=data.category,
                      device="phone", status="offline", elapsed=0, updated_at=now)
        db.add(timer)
    elif timer is None or timer.device != "phone" or (timer.lesson_id, timer.category, timer.review_key) != (
        str(data.lessonId), data.category, data.reviewKey,
    ):
        raise HTTPException(409, "找不到相符的手机计时。")
    if data.span is not None and data.action not in ("pause", "finish"):
        raise HTTPException(422, "暂停或结束时才可提交计时时段。")

    result = {"state": "saved", "seconds": 0}
    overlaps: list[dict] = []
    if data.action in ("start", "resume"):
        if timer.status in ("running", "finished", "stashed") or timer.interrupted_at is not None:
            return result, overlaps
        current = active_timer(db)
        captured = as_utc(data.capturedAt)
        age = (now - captured).total_seconds()
        observed = str(data.expected) if data.expected is not None else None
        current_id = current.id if current is not None else None
        can_take_over = (
            data.live and -CLOCK_TOLERANCE <= age <= MAX_LIVE_AGE and observed == current_id
            and (current is None or current.updated_at <= captured + timedelta(seconds=CLOCK_TOLERANCE))
        )
        if (current is None and data.present) or current is timer or can_take_over:
            before_start()
            if current is not None and current is not timer:
                finish_timer(db, current, now, moment)
            timer.status, timer.started_at, timer.started_on = "running", now, moment.date()
            if can_take_over:
                result["claimedAt"] = stamp(now)
    else:
        if data.span is not None:
            result, overlaps = account_span(db, timer, data.span, now)
        if timer.status not in ("finished", "stashed"):
            if data.action == "finish":
                timer.status = "finished"
            elif timer.status == "running":
                timer.status = "paused"
            timer.started_at = None
            timer.started_on = None
        if timer.status == "finished":
            record_timer_activity(db, timer, moment)
    timer.updated_at = now
    return result, overlaps


def timer_endpoints(engine, folder, day, get_now):
    router = APIRouter(prefix="/api/mobile/timer")
    review_needed = (MobileChange.kind == "timer", MobileChange.reply["state"].as_string() == "held")

    @router.get("")
    def read(workspace: UUID, timerId: UUID | None = None):
        with transaction(engine, write=False) as db:
            if workspace_id(db) != str(workspace):
                raise HTTPException(409, "原工作区已更换。")
            now = get_now()
            current = active_timer(db)
            local = db.get(Timer, str(timerId)) if timerId else None
            interrupted = local.interrupted_at if local is not None and local.device == "phone" else None
            return {"workspace": str(workspace), "serverAt": stamp(now), "dayShift": (day(db) - study_moment(now).date()).days,
                    "active": {"id": current.id, "device": current.device} if current is not None else None,
                    "interruptedAt": stamp(interrupted) if interrupted is not None else None,
                    "pendingReview": db.scalar(select(func.count()).select_from(MobileChange).where(*review_needed))}

    @router.post("")
    def receive(data: PhoneTimerEvent):
        with transaction(engine) as db:
            saved, marker = previous(db, data)
            if saved is not None:
                return saved.reply
            now, current_day = get_now(), day(db)
            def freeze():
                freeze_plan_baseline(db, folder, current_day)

            result, overlaps = accept_event(db, data, now, study_moment(now, current_day), freeze)
            reply = {"id": str(data.id), **result}
            details = jsonable_encoder(data.model_dump(exclude={"id", "workspace", "kind"}))
            if data.span is not None and data.span.startedBy is None:
                details["span"].pop("startedBy", None)
            db.add(MobileChange(id=reply["id"], fingerprint=marker, kind="timer", received_at=now,
                                details={**details, "overlaps": overlaps}, reply=reply))
            return reply

    @router.get("/conflicts")
    def conflicts():
        with transaction(engine, write=False) as db:
            rows = db.scalars(select(MobileChange).where(*review_needed).order_by(MobileChange.received_at.desc(), MobileChange.id))
            records = []
            for row in rows:
                lesson = db.get(Lesson, row.details["lessonId"])
                project = db.get(Project, lesson.project_id) if lesson is not None else None
                title = lesson_course(project.sample, lesson.position, lesson.outline).title if project is not None else None
                records.append({**row.details, **row.reply, "title": title})
            return {"records": records}

    @router.post("/conflicts/{identity}")
    def resolve(identity: UUID, data: TimeResolution):
        with transaction(engine) as db:
            item = db.get(MobileChange, str(identity))
            if item is None or item.kind != "timer" or (item.reply or {}).get("state") not in ("held", "resolved"):
                raise HTTPException(404, "找不到这份待核对计时。")
            decision = data.model_dump(mode="json")
            if item.reply["state"] == "resolved":
                if item.details.get("decision") != decision:
                    raise HTTPException(409, "这份计时已核对。")
                return item.reply
            reply = item.reply
            details = item.details
            if data.action == "confirm":
                if reply.get("reason") != "clock" or not data.beganAt or not data.endedAt or data.beganAt >= data.endedAt:
                    raise HTTPException(422, "请给出核对后的开始和结束时间。")
                timer = db.get(Timer, details["timerId"])
                if timer is None:
                    raise HTTPException(409, "原计时已不存在。")
                now = get_now()
                corrected = PhoneSpan(beganAt=data.beganAt, endedAt=data.endedAt, reliable=True, dayShift=details["span"]["dayShift"])
                result, overlaps = account_span(db, timer, corrected, now)
                if result.get("reason") == "clock":
                    raise HTTPException(422, "此时段超出可确认的时间范围。")
                details = {**details, "confirmedOverlaps": overlaps}
                reply = {**reply, "seconds": result["seconds"]}
                if timer.status == "finished":
                    record_timer_activity(db, timer, study_moment(now, day(db)))
            item.details = {**details, "decision": decision}
            item.reply = {**reply, "state": "resolved", "resolution": data.action}
            return item.reply

    def dispatch(command: dict):
        if command.get("op") == "timer-state":
            request = ReadClock.model_validate(command)
            return read(request.workspace, request.timerId)
        return receive(PhoneTimerEvent.model_validate(command.get("event")))

    return router, dispatch
