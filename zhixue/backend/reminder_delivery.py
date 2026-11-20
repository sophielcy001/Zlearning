"""Use the workspace's reminder receipt for both foreground and Web Push delivery."""
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .db import ReminderPushClaim, ReminderReceipt, transaction
from .mobile_api import workspace_id
from .project_activity import study_moment
from .reminders import DAILY, active_appointments, daily_details, issue, preferences


LEASE = timedelta(seconds=55)


@dataclass(frozen=True)
class PushNotice:
    id: str
    workspace: str
    lesson_id: str | None
    expires_at: datetime


def ready_for_push(db: Session, folder: Path, today: date, now: datetime,
                   local: datetime) -> Iterator[tuple[str, str | None, int]]:
    appointments = active_appointments(db, today)
    subjects = [DAILY, *(item.subject for item in appointments)]
    receipts = {row.subject: row for row in db.scalars(
        select(ReminderReceipt).where(ReminderReceipt.subject.in_(subjects)),
    )}

    def due(row: ReminderReceipt | None) -> bool:
        return row is not None and row.sent_on <= today and row.snooze_at is not None and row.snooze_at <= now

    for appointment in appointments:
        receipt = receipts.get(appointment.subject)
        start = datetime.combine(appointment.slot.day, time.min) + timedelta(minutes=appointment.slot.start)
        scheduled = receipt is None and start <= local < start + timedelta(minutes=appointment.slot.minutes)
        if scheduled or due(receipt):
            yield appointment.subject, appointment.lesson_id, receipt.version if receipt else 0
    daily = receipts.get(DAILY)
    if due(daily):
        details = daily_details(db, folder, today, mobile=True)
        if details and details["reviews"]:
            yield DAILY, None, daily.version


def reserve_push(engine: Engine, folder: Path, current_day: Callable[[Session], date],
                 now: datetime, receiver: str) -> PushNotice | None:
    with transaction(engine) as db:
        today = current_day(db)
        workspace = workspace_id(db)
        local = study_moment(now, today)
        config = preferences(db)
        if not config.enabled:
            db.execute(delete(ReminderPushClaim))
            return None
        if config.quiet_ends(local) is not None:
            return None
        ready = list(ready_for_push(db, folder, today, now, local))
        ready_keys = {(subject, version) for subject, _, version in ready}
        claim = None
        for row in db.scalars(select(ReminderPushClaim)):
            if (claim is None and row.receiver == receiver and row.workspace == workspace and row.day == today
                    and (row.subject, row.prior_version) in ready_keys):
                claim = row
            else:
                db.delete(row)
        if claim is None and not ready:
            return None
        if claim is None or claim.expires_at <= now:
            expires = now + LEASE
            if config.quiet_start is not None:
                start = datetime.combine(local.date(), time.min) + timedelta(minutes=config.quiet_start)
                if start <= local:
                    start += timedelta(days=1)
                expires = min(expires, now + (start - local))
            if claim is None:
                db.flush()
                subject, lesson, version = ready[0]
                claim = ReminderPushClaim(id=str(uuid4()), subject=subject, workspace=workspace, receiver=receiver,
                                          lesson_id=lesson, day=today, prior_version=version, expires_at=expires)
                db.add(claim)
            else:
                # Keep the Web Push Topic and notification tag stable after a lost acknowledgement.
                claim.expires_at = expires
        return PushNotice(claim.id, workspace, claim.lesson_id, claim.expires_at)


def settle_push(engine: Engine, notice: PushNotice, *, delivered: bool) -> None:
    with transaction(engine) as db:
        claim = db.get(ReminderPushClaim, notice.id)
        if claim is None or (not delivered and claim.expires_at != notice.expires_at):
            return
        if delivered and claim.workspace == workspace_id(db):
            previous = db.scalar(select(ReminderReceipt).where(ReminderReceipt.subject == claim.subject))
            if (previous.version if previous else 0) == claim.prior_version:
                issue(db, previous, claim.subject, claim.lesson_id, claim.day, {}, claim.id)
        db.delete(claim)
