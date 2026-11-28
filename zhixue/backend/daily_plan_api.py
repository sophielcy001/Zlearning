from collections.abc import Callable
from dataclasses import replace
from datetime import date
from hashlib import sha256
from hmac import compare_digest, digest
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .course_source_updates import pending_source_updates
from .course_sources import observe_sources
from .daily_plan import (
    Availability, Booking, Slot, StudyPlan, appointment_subject, fits, forecast, limits, load_availability,
    load_plan, minute, save_plan_baseline, schedule_status, standing_locks,
)
from .db import (
    GenerationStep, Lesson, Project, ReminderPushClaim, ReminderReceipt, StudyDayOverride,
    StudyPlanLock, StudyPlanSettings, transaction,
)


ClockText = Annotated[str, StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
EndText = Annotated[str, StringConstraints(pattern=r"^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)$")]
Confirmation = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}(\.[0-9a-f]{64}){2}$")]


class TimeWindow(BaseModel):
    start: ClockText
    end: EndText


class WeeklyWindows(BaseModel):
    weekly: list[list[TimeWindow]] = Field(min_length=7, max_length=7)


class DayWindows(BaseModel):
    windows: list[TimeWindow] | None


class Destination(BaseModel):
    date: date
    start: ClockText


class Move(BaseModel):
    taskId: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    placement: Destination | None


class ConfirmMove(Move):
    confirmation: Confirmation


def normalized(windows: list[TimeWindow]) -> list[dict]:
    result: list[dict] = []
    for window in sorted(windows, key=lambda item: item.start):
        if window.end <= window.start:
            raise HTTPException(422, "结束时间需要晚于开始时间。跨午夜请分到两天填写。")
        if result and window.start < result[-1]["end"]:
            raise HTTPException(422, "同一天的学习时段有重叠，请调整后再保存。")
        if result and window.start == result[-1]["end"]:
            result[-1]["end"] = window.end
        else:
            result.append(window.model_dump())
    return result


def protect_locked_hours(db: Session, today: date, before: Availability, after: Availability) -> None:
    for booking in standing_locks(db).values():
        slot = booking.slot
        if slot.day >= today and fits(slot, before.windows(slot.day)) and not fits(slot, after.windows(slot.day)):
            position = slot.view(locked=True)
            raise HTTPException(409, f"{slot.day} 的「{booking.title}」已锁定在 "
                                f"{position['start']}–{position['end']}。请先改期或解除锁定。")


def task_change(plan: StudyPlan, move: Move) -> tuple[Booking | None, list[dict]]:
    existing = plan.locks.get(move.taskId)
    target = move.placement
    if target is None:
        if existing is None:
            raise HTTPException(409, "这个任务没有待处理的锁定时段。")
        return None, []

    task = plan.tasks.get(move.taskId)
    if task is None:
        if existing and existing.due and target.date < existing.due and not existing.paused:
            return None, [{"code": "before_due", "message": f"正式复习最早可安排在 {existing.due}。"}]
        raise HTTPException(409, "这个任务目前无法安排，请刷新课程表并检查项目及复习状态。")

    details = task.details
    duration = existing.slot.minutes if existing else task.minutes
    booking = Booking(
        move.taskId, details["lessonId"], details.get("key"), task.review_round,
        Slot(target.date, minute(target.start), duration),
        existing.estimate_source if existing else details["estimateSource"],
        details["title"], details["projectTopic"], details.get("dueOn"),
    )
    slot = booking.slot
    if slot.day < plan.today:
        return booking, [{"code": "past", "message": "请选择今天或之后的日期。"}]
    if slot.day < task.earliest:
        return booking, [{"code": "before_due", "message": f"正式复习最早可安排在 {task.earliest}。"}]

    windows = plan.availability.windows(slot.day)
    conflicts = []
    if not fits(slot, windows):
        conflicts.append({"code": "outside_hours", "message": "所选时间超出当天可学习时段，请调整开始时间或日期。"})
    siblings = [other for identity, other in plan.locks.items() if identity != move.taskId and other.slot.day == slot.day]
    for other in sorted(siblings, key=lambda item: (item.slot.start, item.task_id)):
        if slot.start < other.slot.end and other.slot.start < slot.end:
            conflicts.append({"code": "locked", "message": f"与「{other.title}」的锁定时段重叠。", "task": other.view()})
    if not conflicts and slot.day == plan.today:
        budget = sum(end - start for start, end in limits(windows))
        reserved = sum(other.slot.minutes for other in siblings if schedule_status(plan, other) == "scheduled")
        if plan.completed + reserved + slot.minutes > budget:
            conflicts.append({"code": "budget", "message": "扣除当天已完成任务和已锁定任务后，剩余时间不足。"})
    return booking, conflicts


def reset_appointment_reminders(db: Session, before: Booking | None, after: Booking | None) -> None:
    subjects = {
        appointment_subject(booking.lesson_id, booking.slot)
        for booking in (before, after)
        if booking is not None and booking.review_key is None
    }
    if subjects:
        db.execute(delete(ReminderPushClaim).where(ReminderPushClaim.subject.in_(subjects)))
        db.execute(delete(ReminderReceipt).where(ReminderReceipt.subject.in_(subjects)))


def position(item: dict | None) -> dict | None:
    return {key: item[key] for key in ("date", "start", "end", "locked")} if item is not None else None


def placed_items(plan: dict) -> dict[str, dict]:
    tasks = {item["id"]: item for day in plan["days"] for item in day["tasks"]}
    tasks.update((item["id"], item) for item in plan["pendingLocks"])
    return tasks


def movement(before: dict, after: dict, identity: str) -> list[dict]:
    previous, future = placed_items(before), placed_items(after)
    changes = []
    for task_id in dict.fromkeys((*previous, *future)):
        if task_id == identity:
            continue
        earlier, later = position(previous.get(task_id)), position(future.get(task_id))
        if earlier != later:
            task = previous.get(task_id) or future[task_id]
            changes.append({"id": task_id, "title": task["title"], "projectTopic": task["projectTopic"],
                            "before": earlier, "after": later})
    return changes


def changed_forecast(plan: StudyPlan, identity: str, booking: Booking | None) -> dict:
    locks = dict(plan.locks)
    if booking is None:
        locks.pop(identity)
    else:
        locks[identity] = booking
    return forecast(replace(plan, locks=locks))


def as_json(data) -> bytes:
    return json.dumps(jsonable_encoder(data), sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(projection: dict, plan: StudyPlan, target: Destination | None) -> str:
    return sha256(as_json([projection, plan.availability.windows(target.date) if target else None])).hexdigest()


def signature(secret: bytes, move: Move, earlier: str, later: str) -> str:
    return digest(secret, as_json(["study-plan-move", move.model_dump(exclude={"confirmation"}), earlier, later]), "sha256").hex()


def daily_plan_router(engine: Engine, folder: Path, current_day: Callable[[Session], date], session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/study-plan")
    secret = session_token.encode("utf-8")

    @router.get("")
    def get_plan():
        with transaction(engine) as db:
            plan = forecast(load_plan(db, folder, current_day(db)))
            save_plan_baseline(db, plan["days"][0])
            updates = db.execute(select(Lesson.id, Lesson.outline, Project.topic, GenerationStep.revision_request)
                                 .join(GenerationStep, GenerationStep.lesson_id == Lesson.id)
                                 .join(Project, Project.id == Lesson.project_id)
                                 .where(func.json_extract(GenerationStep.revision_request, "$.status") == "accepted",
                                        func.coalesce(func.json_extract(GenerationStep.revision_request, "$.seen"), 0) == 0)
                                 .order_by(Project.updated_at.desc(), Lesson.id))
            plan["courseUpdates"] = [
                {"id": request["id"], "lessonId": identity, "title": outline["title"], "projectTopic": topic,
                 "version": request["publishedVersion"]}
                for identity, outline, topic, request in updates
            ]
            return plan

    @router.get("/source-updates")
    def source_updates():
        with observe_sources(engine) as db:
            return pending_source_updates(db, folder)

    @router.put("/settings")
    def save_week(payload: WeeklyWindows):
        weekly = [normalized(windows) for windows in payload.weekly]
        with transaction(engine) as db:
            today = current_day(db)
            before = load_availability(db, today)
            protect_locked_hours(db, today, before, replace(before, weekly=weekly))
            db.merge(StudyPlanSettings(id=1, weekly=weekly))
        return {"weekly": weekly}

    @router.put("/days/{day}")
    def save_day(day: date, payload: DayWindows):
        windows = normalized(payload.windows) if payload.windows is not None else None
        with transaction(engine) as db:
            today = current_day(db)
            before = load_availability(db, today)
            overrides = dict(before.overrides)
            if windows is not None:
                overrides[day] = windows
            else:
                overrides.pop(day, None)
            protect_locked_hours(db, today, before, replace(before, overrides=overrides))
            if windows is not None:
                db.merge(StudyDayOverride(day=day, windows=windows))
            elif (override := db.get(StudyDayOverride, day)) is not None:
                db.delete(override)
        return {"windows": windows}

    @router.post("/moves/preview")
    def preview_move(move: Move):
        with transaction(engine) as db:
            target = move.placement
            plan = load_plan(db, folder, current_day(db), target.date if target else None)
            booking, conflicts = task_change(plan, move)
            before = forecast(plan)
            earlier = position(placed_items(before).get(move.taskId))
            if conflicts:
                return {"before": earlier, "after": booking.slot.view(locked=True) if booking else None,
                        "conflicts": conflicts, "changes": [], "confirmation": None}
            after = changed_forecast(plan, move.taskId, booking)
            previous, proposed = fingerprint(before, plan, target), fingerprint(after, plan, target)
            return {"before": earlier, "after": position(placed_items(after).get(move.taskId)),
                    "conflicts": [], "changes": movement(before, after, move.taskId),
                    "confirmation": f"{previous}.{proposed}.{signature(secret, move, previous, proposed)}"}

    @router.post("/moves/confirm")
    def confirm_move(move: ConfirmMove):
        previous, proposed, signed = move.confirmation.split(".")
        stale = "课程表已有变化，请重新预览这次调整。"
        if not compare_digest(signed, signature(secret, move, previous, proposed)):
            raise HTTPException(409, stale)
        with transaction(engine) as db:
            target = move.placement
            plan = load_plan(db, folder, current_day(db), target.date if target else None)
            before = forecast(plan)
            current = fingerprint(before, plan, target)
            if current == proposed:
                return {"plan": before}
            if current != previous:
                raise HTTPException(409, stale)
            booking, conflicts = task_change(plan, move)
            if conflicts:
                raise HTTPException(409, stale)
            after = changed_forecast(plan, move.taskId, booking)
            if fingerprint(after, plan, target) != proposed:
                raise HTTPException(409, stale)
            reset_appointment_reminders(db, plan.locks.get(move.taskId), booking)
            if booking:
                db.merge(booking.record())
            elif (row := db.get(StudyPlanLock, move.taskId)) is not None:
                db.delete(row)
            save_plan_baseline(db, after["days"][0])
            return {"plan": after}

    return router
