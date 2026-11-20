from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import ceil
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .content import lesson_course
from .course_disputes import pending_disputes_count
from .daily_budget import recoverable
from .daily_plan import Slot, clock, fits, load_availability, minute
from .daily_plan_api import ClockText
from .db import Lesson, Project, ReminderPushClaim, ReminderReceipt, ReminderSettings, StudyPlanLock, transaction
from .mobile_api import mobile_review_ready, workspace_id
from .model_usage import billing_day
from .project_activity import study_moment
from .review_queue import review_queue


DAILY = "daily"
BUDGET = "daily-budget"
Version = Annotated[int, Field(strict=True, ge=1)]
ReminderAction = Literal["dismiss", "in_30_minutes", "tomorrow"]


class QuietHours(BaseModel):
    start: ClockText
    end: ClockText


class SettingsInput(BaseModel):
    enabled: bool
    quiet: QuietHours | None


class Shown(BaseModel):
    id: UUID
    version: Version


class CheckInput(BaseModel):
    trigger: Literal["foreground", "scheduled"]
    day: date | None = None
    shown: list[Shown] = Field(default_factory=list)
    delivery: UUID | None = None


class ActionInput(BaseModel):
    action: ReminderAction
    version: Version


class MobileSettingsScope(BaseModel):
    workspace: UUID


class MobileCheck(CheckInput):
    workspace: UUID
    notice: UUID | None = None


class MobileSettings(SettingsInput):
    workspace: UUID


class MobileAction(ActionInput):
    workspace: UUID
    id: UUID


@dataclass(frozen=True)
class Preferences:
    enabled: bool
    quiet_start: int | None
    quiet_end: int | None

    def view(self) -> dict:
        quiet = None if self.quiet_start is None else {"start": clock(self.quiet_start), "end": clock(self.quiet_end)}
        return {"enabled": self.enabled, "quiet": quiet}

    def quiet_ends(self, local: datetime) -> datetime | None:
        start, end = self.quiet_start, self.quiet_end
        if start is None or end is None:
            return None
        current = local.hour * 60 + local.minute
        if start < end:
            muted = start <= current < end
        else:
            muted = current >= start or current < end
        if not muted:
            return None
        day = local.date() + timedelta(days=int(start > end and current >= start))
        return datetime.combine(day, time.min) + timedelta(minutes=end)


@dataclass(frozen=True)
class CourseAppointment:
    subject: str
    lesson_id: str
    slot: Slot
    title: str
    topic: str

    def view(self) -> dict:
        return {"kind": "lesson", "lessonId": self.lesson_id, "title": self.title, "projectTopic": self.topic,
                "slotDay": self.slot.day, "startsAt": clock(self.slot.start)}


def preferences(db: Session) -> Preferences:
    row = db.get(ReminderSettings, 1)
    return Preferences(row.enabled, row.quiet_start, row.quiet_end) if row else Preferences(True, None, None)


def active_appointments(db: Session, today: date) -> list[CourseAppointment]:
    old_snooze = select(ReminderReceipt.lesson_id).where(
        ReminderReceipt.lesson_id.is_not(None),
        (ReminderReceipt.sent_on == today) | ReminderReceipt.action.in_(("in_30_minutes", "tomorrow")),
    )
    current = (select(StudyPlanLock, Project.topic, Project.sample, Lesson.position, Lesson.outline)
               .select_from(StudyPlanLock).join(Lesson, Lesson.id == StudyPlanLock.lesson_id)
               .join(Project, Project.id == Lesson.project_id)
               .where(StudyPlanLock.review_key.is_(None), StudyPlanLock.completed_on.is_(None),
                      Lesson.ready.is_(True), Lesson.learned_on.is_(None), Project.paused.is_(False),
                      StudyPlanLock.day <= today,
                      (StudyPlanLock.day == today) | StudyPlanLock.lesson_id.in_(old_snooze))
               .order_by(StudyPlanLock.day, StudyPlanLock.start_minute, StudyPlanLock.lesson_id))
    availability = load_availability(db, today)
    appointments = []
    for row, topic, sample, position, outline in db.execute(current):
        slot = Slot(row.day, row.start_minute, row.minutes)
        if row.day == today and not fits(slot, availability.windows(today)):
            continue
        subject = f"lesson:{row.lesson_id}:{row.day.isoformat()}:{row.start_minute}"
        title = lesson_course(sample, position, outline).title
        appointments.append(CourseAppointment(subject, row.lesson_id, slot, title, topic))
    return appointments


def card(receipt: ReminderReceipt, details: dict) -> dict:
    return {"id": receipt.id, "version": receipt.version, **details}


def issue(db: Session, receipt: ReminderReceipt | None, subject: str, lesson_id: str | None,
          today: date, details: dict, delivery: str | None) -> dict:
    if receipt is None:
        receipt = ReminderReceipt(id=str(uuid4()), subject=subject, lesson_id=lesson_id, sent_on=today, version=1)
        db.add(receipt)
    else:
        receipt.sent_on = today
        receipt.version += 1
        receipt.action = receipt.snooze_at = receipt.snooze_on = None
    receipt.delivery_key = delivery
    return card(receipt, details)


def snooze_time(receipt: ReminderReceipt | None, today: date, now: datetime) -> datetime | None:
    if receipt is None or receipt.sent_on > today:
        return None
    if receipt.snooze_at is not None:
        return receipt.snooze_at
    if receipt.snooze_on is not None and receipt.snooze_on <= today:
        return now
    return None


def is_snoozed(receipt: ReminderReceipt | None) -> bool:
    return receipt is not None and (receipt.snooze_at is not None or receipt.snooze_on is not None)


def daily_details(db: Session, folder: Path, today: date, *, mobile: bool) -> dict | None:
    due = [(review, item) for _, _, review, item in review_queue(db, folder, today, through=today)
           if item["status"] == "due"]
    disputes = pending_disputes_count(db, actionable_only=True)
    if not due and not disputes:
        return None
    reviews = sum(mobile_review_ready(db, review, item) for review, item in due) if mobile else len(due)
    return {"kind": "daily", "reviews": reviews, "disputes": disputes}


def check_reminders(db: Session, folder: Path, today: date, now: datetime, data: CheckInput, *, mobile=False) -> dict:
    response = {"day": today, "reminders": [], "nextCheckSeconds": None}
    prefs = preferences(db)
    if not prefs.enabled or (data.trigger == "scheduled" and data.day != today):
        return response
    local = study_moment(now, today)
    muted = prefs.quiet_ends(local) is not None
    appointments = active_appointments(db, today)
    subjects = [DAILY, *(item.subject for item in appointments)]
    if not mobile:
        subjects.append(BUDGET)
    receipts = {row.subject: row for row in db.scalars(select(ReminderReceipt).where(ReminderReceipt.subject.in_(subjects)))}
    pending = {row.subject: row for row in db.scalars(select(ReminderPushClaim).where(
        ReminderPushClaim.subject.in_(subjects), ReminderPushClaim.expires_at > now,
        ReminderPushClaim.workspace == workspace_id(db),
    ))}
    visible = {str(item.id): item.version for item in data.shown}
    delivery = str(data.delivery) if data.delivery is not None else None
    notice = str(data.notice) if isinstance(data, MobileCheck) and data.notice is not None else None
    reminders = response["reminders"]
    later: list[datetime] = []

    def presented(row: ReminderReceipt | None, day: date) -> bool:
        return row is not None and row.sent_on == day and row.action is None and (
            visible.get(row.id) == row.version or delivery is not None and row.delivery_key == delivery
            or notice is not None and row.delivery_key == notice
        )

    def available(when: datetime | None) -> bool:
        if when is None:
            return False
        earliest = max(now, when)
        local_earliest = local + (earliest - now)
        resume = prefs.quiet_ends(local_earliest)
        if resume is not None:
            earliest += resume - local_earliest
            local_earliest = resume
        if earliest > now:
            if local_earliest.date() == today:
                later.append(earliest)
            return False
        return True

    def waiting_for_push(subject: str) -> bool:
        claim = pending.get(subject)
        if claim is None:
            return False
        retry = min(claim.expires_at, now + timedelta(seconds=2)) if notice == claim.id else claim.expires_at
        available(retry)
        return True

    def merge(subject: str, day: date, details: Callable[[], dict | None]) -> None:
        if waiting_for_push(subject):
            return
        receipt = receipts.get(subject)
        follow_up = available(snooze_time(receipt, day, now))
        fresh = (data.trigger == "foreground" and not muted and not is_snoozed(receipt)
                 and (receipt is None or receipt.sent_on < day))
        if not (follow_up or fresh or (not muted and presented(receipt, day))):
            return
        payload = details()
        if payload:
            if mobile and subject == DAILY and not payload["reviews"]:
                return
            if follow_up or fresh:
                reminders.append(issue(db, receipt, subject, None, day, payload, delivery))
            else:
                reminders.append(card(receipt, payload))
        elif follow_up and receipt is not None:
            receipt.snooze_on = receipt.snooze_at = None
            receipt.action = "dismiss"

    def learning_items() -> dict | None:
        return daily_details(db, folder, today, mobile=mobile)

    def budget_items() -> dict | None:
        count = sum(1 for _ in recoverable(db, folder, now, reminder=True))
        return {"kind": "budget", "count": count} if count else None

    merge(DAILY, today, learning_items)
    if not mobile:
        merge(BUDGET, billing_day(now), budget_items)

    for appointment in appointments:
        if waiting_for_push(appointment.subject):
            continue
        receipt = receipts.get(appointment.subject)
        when = snooze_time(receipt, today, now)
        if receipt is None and appointment.slot.day == today:
            start = datetime.combine(today, time.min) + timedelta(minutes=appointment.slot.start)
            when = now + (start - local)
        if available(when):
            reminders.append(issue(db, receipt, appointment.subject, appointment.lesson_id, today, appointment.view(), delivery))
        elif not muted and presented(receipt, today):
            reminders.append(card(receipt, appointment.view()))

    if later:
        response["nextCheckSeconds"] = ceil((min(later) - now).total_seconds())
    return response


def reminder_endpoints(engine: Engine, folder: Path, current_day: Callable[[Session], date],
                       now: Callable[[], datetime]):
    router = APIRouter()
    desktop = "/api/reminders"
    mobile = "/api/mobile/reminders"

    def same_workspace(db: Session, workspace: UUID | None):
        if workspace is not None and workspace_id(db) != str(workspace):
            raise HTTPException(409, "提醒对应的工作区已更换。")

    def read_settings(workspace: UUID | None = None):
        with transaction(engine, write=False) as db:
            same_workspace(db, workspace)
            return preferences(db).view()

    @router.get(desktop + "/settings")
    def desktop_settings():
        return read_settings()

    @router.get(mobile + "/settings")
    def phone_settings(workspace: UUID):
        return read_settings(workspace)

    def save(data: SettingsInput, workspace: UUID | None = None):
        start = minute(data.quiet.start) if data.quiet else None
        end = minute(data.quiet.end) if data.quiet else None
        if start is not None and start == end:
            raise HTTPException(422, "请选择不同的免打扰开始与结束时间。")
        with transaction(engine) as db:
            same_workspace(db, workspace)
            db.merge(ReminderSettings(id=1, enabled=data.enabled, quiet_start=start, quiet_end=end))
            if not data.enabled:
                db.execute(delete(ReminderPushClaim))
        return Preferences(data.enabled, start, end).view()

    @router.put(desktop + "/settings")
    def save_settings(data: SettingsInput):
        return save(data)

    @router.put(mobile + "/settings")
    def phone_save_settings(data: MobileSettings):
        return save(data, data.workspace)

    def check(data: CheckInput, workspace: UUID | None = None):
        with transaction(engine) as db:
            same_workspace(db, workspace)
            return check_reminders(db, folder, current_day(db), now(), data, mobile=workspace is not None)

    @router.post(desktop + "/check")
    def desktop_check(data: CheckInput):
        return check(data)

    @router.post(mobile + "/check")
    def phone_check(data: MobileCheck):
        return check(data, data.workspace)

    def act(identity: UUID, data: ActionInput, workspace: UUID | None = None):
        with transaction(engine) as db:
            same_workspace(db, workspace)
            receipt = db.get(ReminderReceipt, str(identity))
            if receipt is None or workspace is not None and receipt.subject == BUDGET:
                raise HTTPException(404, "找不到这条提醒。")
            if receipt.version != data.version or receipt.action not in (None, data.action):
                raise HTTPException(409, "提醒已更新，请查看当前安排。")
            if receipt.action is None:
                receipt.action = data.action
                receipt.delivery_key = None
                if data.action == "in_30_minutes":
                    receipt.snooze_at = now() + timedelta(minutes=30)
                elif data.action == "tomorrow":
                    day = billing_day(now()) if receipt.subject == BUDGET else current_day(db)
                    receipt.snooze_on = day + timedelta(days=1)
            return {"id": receipt.id, "version": receipt.version, "action": receipt.action,
                    "snoozeAt": receipt.snooze_at, "snoozeOn": receipt.snooze_on}

    @router.post(desktop + "/{identity}/action")
    def desktop_act(identity: UUID, data: ActionInput):
        return act(identity, data)

    @router.post(mobile + "/action")
    def phone_act(data: MobileAction):
        return act(data.id, data, data.workspace)

    def dispatch(command: dict):
        match command.get("op"):
            case "reminder-check":
                return phone_check(MobileCheck.model_validate(command))
            case "reminder-action":
                return phone_act(MobileAction.model_validate(command))
            case "reminder-settings":
                return phone_settings(MobileSettingsScope.model_validate(command).workspace)
            case "reminder-save":
                return phone_save_settings(MobileSettings.model_validate(command))
            case _:
                raise HTTPException(422, "手机请求的操作不受支持。")

    return router, dispatch
