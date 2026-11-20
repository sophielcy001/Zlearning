from dataclasses import dataclass
from datetime import date, timedelta
from math import ceil
from pathlib import Path
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session, lazyload

from .content import lesson_course
from .db import (
    AssessmentGrade, AssessmentObjective, AssessmentRound, Attempt, Lesson, Project, Review, StudyDayOverride,
    StudyPlanLock, StudyPlanSettings, StudyPlanSnapshot,
)
from .project_board import scheduled_lesson
from .review_queue import review_queue
from .study_time import confirmed_times


PLAN_DAYS = 7
REVIEW_MINUTES = 5
LESSON_MINUTES = 15
DEFAULT_WINDOWS = [{"start": "19:00", "end": "19:30"}]
PRIORITY = {"high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class StudyTask:
    order: tuple
    minutes: int
    earliest: date
    details: dict
    review_round: int | None = None


@dataclass(frozen=True)
class Slot:
    day: date
    start: int
    minutes: int

    @property
    def end(self) -> int:
        return self.start + self.minutes

    def view(self, *, locked: bool) -> dict:
        return {"date": self.day, "start": clock(self.start), "end": clock(self.end), "locked": locked}


@dataclass(frozen=True)
class Booking:
    task_id: str
    lesson_id: str
    review_key: str | None
    review_round: int | None
    slot: Slot
    estimate_source: str
    title: str
    project_topic: str
    due: date | None
    paused: bool = False

    def view(self) -> dict:
        return {
            "id": self.task_id, "lessonId": self.lesson_id, "projectTopic": self.project_topic, "title": self.title,
            "kind": "review" if self.review_key is not None else "lesson", "dueOn": self.due,
            "minutes": self.slot.minutes, "estimateSource": self.estimate_source, **self.slot.view(locked=True),
        }

    def record(self) -> StudyPlanLock:
        return StudyPlanLock(
            task_id=self.task_id, lesson_id=self.lesson_id, review_key=self.review_key, review_round=self.review_round,
            day=self.slot.day, start_minute=self.slot.start, minutes=self.slot.minutes,
            estimate_source=self.estimate_source, completed_on=None,
        )


@dataclass(frozen=True)
class Availability:
    weekly: list[list[dict]]
    overrides: dict[date, list[dict]]
    configured: bool

    def windows(self, day: date) -> list[dict]:
        return self.overrides.get(day, self.weekly[day.weekday()])


@dataclass(frozen=True)
class StudyPlan:
    today: date
    through: date
    availability: Availability
    tasks: dict[str, StudyTask]
    locks: dict[str, Booking]
    completed: int

def minute(text: str) -> int:
    hours, minutes = map(int, text.split(":"))
    return hours * 60 + minutes


def clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def course_estimates(db: Session, today: date) -> dict[str, int]:
    completed = dict(db.execute(
        select(Lesson.id, Lesson.project_id).where(Lesson.learned_on < today, scheduled_lesson())
    ).tuples().all())
    if not completed:
        return {}
    total_by_lesson: dict[str, int] = {}
    for entry in confirmed_times(db, through=today - timedelta(days=1)):
        lesson_id = entry.timer.lesson_id
        is_learning = entry.category == "learning" or (entry.source_on is None and entry.category == "unclassified")
        if lesson_id in completed and is_learning and entry.seconds > 0:
            total_by_lesson[lesson_id] = total_by_lesson.get(lesson_id, 0) + entry.seconds
    histories: dict[str, list[int]] = {}
    for lesson_id, seconds in total_by_lesson.items():
        histories.setdefault(completed[lesson_id], []).append(seconds)
    return {pid: ceil(median(seconds) / 60) for pid, seconds in histories.items()}


def completed_minutes(db: Session, today: date, estimates: dict[str, int]) -> int:
    courses = sum(estimates.get(pid, LESSON_MINUTES) for pid in db.scalars(
        select(Lesson.project_id).where(Lesson.learned_on == today, scheduled_lesson())
    ))
    local_count = db.scalar(select(func.count()).select_from(Attempt).where(Attempt.submitted_on == today))
    model_count = db.scalar(select(func.count()).select_from(AssessmentRound).where(
        AssessmentRound.mode == "formal", AssessmentRound.submitted_on == today,
    ))
    return courses + REVIEW_MINUTES * (local_count + model_count)


def lesson_task_id(lesson_id: str) -> str:
    return f"lesson:{lesson_id}"


def review_task_id(lesson_id: str, key: str, round_number: int) -> str:
    return f"review:{lesson_id}:{key}:{round_number}"


def finish_booking(db: Session, task_id: str, day: date) -> None:
    row = db.get(StudyPlanLock, task_id)
    if row is not None and row.completed_on is None:
        row.completed_on = day


def weak_review(db: Session, review: Review, kind: str) -> bool:
    if kind == "local":
        attempt = db.get(Attempt, (review.lesson_id, review.key, review.round_number - 1))
        result = attempt.result if attempt else None
    else:
        result = db.scalar(
            select(AssessmentGrade.result).join(AssessmentRound, AssessmentRound.id == AssessmentGrade.round_id)
            .where(AssessmentRound.lesson_id == review.lesson_id, AssessmentRound.key == review.key,
                   AssessmentRound.mode == "formal", AssessmentRound.state == "graded",
                   AssessmentRound.number < review.round_number)
            .order_by(AssessmentRound.number.desc(), AssessmentGrade.version.desc()).limit(1)
        )
    return result is not None and result["outcome"] in ("hinted", "failed")


def available_tasks(db: Session, folder: Path, today: date, through: date, estimates: dict[str, int]) -> list[StudyTask]:
    tasks: list[StudyTask] = []
    for project, lesson, review, details in review_queue(db, folder, today, through):
        due = review.due_on
        if due is None or details["status"] not in ("due", "upcoming"):
            continue
        weak = weak_review(db, review, details["kind"])
        order = (0, not weak, due, -PRIORITY.get(project.priority, 0), project.id, lesson.position, review.key)
        tasks.append(StudyTask(order, REVIEW_MINUTES, due, {
            "id": review_task_id(lesson.id, review.key, review.round_number),
            "kind": "review", "projectId": project.id, "projectTopic": project.topic, "lessonId": lesson.id,
            "key": review.key, "reviewKind": details["kind"], "title": details["title"], "dueOn": due,
            "weak": weak, "estimateSource": "default",
        }, review.round_number))
    records = db.execute(
        select(Project, Lesson).join(Lesson, Lesson.project_id == Project.id).options(lazyload("*"))
        .where(Project.paused.is_(False), Lesson.ready.is_(True), Lesson.learned_on.is_(None), scheduled_lesson())
    )
    for project, lesson in records:
        order = (1, -PRIORITY.get(project.priority, 0), project.created_on, project.id,
                 lesson.reading_order if lesson.reading_order is not None else lesson.position, lesson.id)
        tasks.append(StudyTask(order, estimates.get(project.id, LESSON_MINUTES), today, {
            "id": lesson_task_id(lesson.id), "kind": "lesson", "projectId": project.id,
            "projectTopic": project.topic, "lessonId": lesson.id,
            "title": lesson_course(project.sample, lesson.position, lesson.outline).title,
            "estimateSource": "timer" if project.id in estimates else "default",
        }))
    return sorted(tasks, key=lambda task: task.order)


def load_availability(db: Session, today: date) -> Availability:
    settings = db.get(StudyPlanSettings, 1)
    weekly = settings.weekly if settings else [DEFAULT_WINDOWS for _ in range(7)]
    overrides = {item.day: item.windows for item in db.scalars(select(StudyDayOverride).where(StudyDayOverride.day >= today))}
    return Availability(weekly, overrides, settings is not None)


def standing_locks(db: Session) -> dict[str, Booking]:
    submitted = select(AssessmentRound.id).where(
        AssessmentRound.lesson_id == StudyPlanLock.lesson_id, AssessmentRound.key == StudyPlanLock.review_key,
        AssessmentRound.mode == "formal", AssessmentRound.number == StudyPlanLock.review_round,
        AssessmentRound.submitted_on.is_not(None),
    ).exists()
    rows = (select(StudyPlanLock, Project.topic, Project.sample, Project.paused, Lesson.position, Lesson.outline,
                   Review.due_on, AssessmentObjective.title).select_from(StudyPlanLock)
            .join(Lesson, Lesson.id == StudyPlanLock.lesson_id).join(Project, Project.id == Lesson.project_id)
            .outerjoin(Review, (Review.lesson_id == Lesson.id) & (Review.key == StudyPlanLock.review_key))
            .outerjoin(AssessmentObjective, (AssessmentObjective.lesson_id == Lesson.id)
                       & (AssessmentObjective.key == StudyPlanLock.review_key))
            .where(
                StudyPlanLock.completed_on.is_(None),
                (StudyPlanLock.review_key.is_(None) & Lesson.learned_on.is_(None))
                | (Review.due_on.is_not(None) & (Review.round_number <= StudyPlanLock.review_round) & ~submitted)
            ))
    locks = {}
    for row, topic, sample, paused, position, outline, due, title in db.execute(rows):
        if title is None:
            course = lesson_course(sample, position, outline)
            title = next((item.title for item in course.objectives if item.key == row.review_key), course.title)
        locks[row.task_id] = Booking(
            row.task_id, row.lesson_id, row.review_key, row.review_round, Slot(row.day, row.start_minute, row.minutes),
            row.estimate_source, title, topic, due, paused,
        )
    return locks


def load_plan(db: Session, folder: Path, today: date, target: date | None = None, *, days: int = PLAN_DAYS) -> StudyPlan:
    through = today + timedelta(days=days - 1)
    locks = standing_locks(db)
    latest_booking = max((booking.slot.day for booking in locks.values()), default=through)
    eligibility_through = max(through, target or through, latest_booking)
    estimates = course_estimates(db, today)
    tasks = {task.details["id"]: task for task in available_tasks(db, folder, today, eligibility_through, estimates)}
    return StudyPlan(today, through, load_availability(db, today), tasks, locks, completed_minutes(db, today, estimates))


def limits(windows: list[dict]) -> list[tuple[int, int]]:
    return [(minute(window["start"]), minute(window["end"])) for window in windows]


def fits(slot: Slot, windows: list[dict]) -> bool:
    return any(start <= slot.start and slot.end <= end for start, end in limits(windows))


def schedule_status(plan: StudyPlan, booking: Booking) -> str:
    slot = booking.slot
    if slot.day < plan.today:
        return "missed"
    if booking.paused:
        return "paused"
    if booking.due and slot.day < booking.due:
        return "before_due"
    if booking.task_id not in plan.tasks:
        return "unavailable"
    if not fits(slot, plan.availability.windows(slot.day)):
        return "outside_hours"
    return "future" if slot.day > plan.through else "scheduled"


def remove_slot(open_slots: list[tuple[int, int]], slot: Slot) -> None:
    for index, (start, end) in enumerate(open_slots):
        if start <= slot.start and slot.end <= end:
            open_slots[index:index + 1] = [
                segment for segment in ((start, slot.start), (slot.end, end)) if segment[0] < segment[1]
            ]
            return
    raise ValueError("A booked time overlaps another booking or exceeds availability.")


def schedule_day(day: date, windows: list[dict], completed: int, backlog: list[StudyTask],
                 locked: list[Booking], candidates: dict[str, StudyTask]) -> tuple[dict, list[StudyTask]]:
    open_slots = limits(windows)
    budget = sum(end - start for start, end in open_slots)
    tasks = []
    for booking in locked:
        slot = booking.slot
        remove_slot(open_slots, slot)
        task = candidates[booking.task_id]
        tasks.append({**task.details, **slot.view(locked=True), "minutes": slot.minutes,
                      "estimateSource": booking.estimate_source})
    reserve = completed
    while reserve and open_slots:
        start, end = open_slots[0]
        used = min(reserve, end - start)
        remove_slot(open_slots, Slot(day, start, used))
        reserve -= used
    remaining = []
    for task in backlog:
        if task.earliest > day or not open_slots:
            remaining.append(task)
            continue
        start = next((start for start, end in open_slots if end - start >= task.minutes), None)
        if start is None:
            remaining.append(task)
            continue
        slot = Slot(day, start, task.minutes)
        remove_slot(open_slots, slot)
        tasks.append({**task.details, **slot.view(locked=False), "minutes": slot.minutes})
    planned = sum(task["minutes"] for task in tasks)
    return {
        "date": day, "windows": windows, "tasks": sorted(tasks, key=lambda task: task["start"]),
        "budgetMinutes": budget, "plannedMinutes": planned, "estimatedCompletedMinutes": completed,
        "remainingMinutes": max(0, budget - completed - planned),
    }, remaining


def forecast(plan: StudyPlan) -> dict:
    blocks: dict[date, list[Booking]] = {}
    pending = []
    for booking in sorted(plan.locks.values(), key=lambda item: (item.slot.day, item.slot.start, item.task_id)):
        status = schedule_status(plan, booking)
        if status == "scheduled":
            blocks.setdefault(booking.slot.day, []).append(booking)
        else:
            pending.append({**booking.view(), "status": status})
    backlog = [task for identity, task in plan.tasks.items() if identity not in plan.locks and task.earliest <= plan.through]
    days = []
    for offset in range((plan.through - plan.today).days + 1):
        day = plan.today + timedelta(days=offset)
        schedule, backlog = schedule_day(
            day, plan.availability.windows(day), plan.completed if offset == 0 else 0,
            backlog, blocks.get(day, []), plan.tasks,
        )
        days.append({**schedule, "override": day in plan.availability.overrides})
    reviews = sum(task.details["kind"] == "review" for task in backlog)
    return {
        "today": plan.today, "weekly": plan.availability.weekly, "configured": plan.availability.configured, "days": days,
        "unplannedReviews": reviews, "unplannedLessons": len(backlog) - reviews,
        "unplanned": [{**task.details, "minutes": task.minutes, "earliest": task.earliest} for task in backlog],
        "pendingLocks": pending,
    }


def save_plan_baseline(db: Session, day: dict, *, freeze: bool = False) -> None:
    record = db.get(StudyPlanSnapshot, day["date"])
    if record is not None and record.frozen:
        return
    if day["estimatedCompletedMinutes"] > 0:
        if record is not None and freeze:
            record.frozen = True
        return
    items = [{"id": task["id"], "minutes": task["minutes"]} for task in day["tasks"]]
    if record is None:
        db.add(StudyPlanSnapshot(day=day["date"], budget_minutes=day["budgetMinutes"], items=items, frozen=freeze))
    else:
        record.items, record.budget_minutes, record.frozen = items, day["budgetMinutes"], freeze


def freeze_plan_baseline(db: Session, folder: Path, today: date) -> None:
    record = db.get(StudyPlanSnapshot, today)
    if record is not None and record.frozen:
        return
    plan = load_plan(db, folder, today, days=1)
    save_plan_baseline(db, forecast(plan)["days"][0], freeze=True)
