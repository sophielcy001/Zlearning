from calendar import monthrange
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .content import is_model_lesson, lesson_course
from .daily_plan import forecast, load_plan
from .db import AssessmentGrade, AssessmentObjective, AssessmentRound, Attempt, Lesson, Project, StudyPlanSnapshot, transaction
from .project_board import scheduled_lesson
from .study_time import CATEGORIES, TimeCategory, confirmed_times, correct_time


Period = Literal["day", "week", "month"]
OUTCOMES = ("passed", "hinted", "failed", "ungraded")
PREDICTION_DAYS = 90


class CorrectionInput(BaseModel):
    sourceOn: date | None
    reportOn: date | None
    seconds: int = Field(strict=True, ge=0)
    category: TimeCategory
    baseRevision: int = Field(strict=True, ge=0)


@dataclass(frozen=True)
class ReviewEvent:
    lesson: str
    key: str
    number: int
    submitted: date
    outcome: str
    group: str | None


def review_events(db: Session, through: date) -> list[ReviewEvent]:
    legacy = select(Attempt.lesson_id, Attempt.key, Attempt.round_number, Attempt.submitted_on, Attempt.result).where(
        Attempt.submitted_on <= through,
    )
    result = [ReviewEvent(lesson, key, number, day, details["outcome"], None)
              for lesson, key, number, day, details in db.execute(legacy)]
    last_grade = (select(func.max(AssessmentGrade.version)).where(AssessmentGrade.round_id == AssessmentRound.id)
                  .correlate(AssessmentRound).scalar_subquery())
    formal = (select(AssessmentRound.lesson_id, AssessmentRound.key, AssessmentRound.number, AssessmentRound.submitted_on,
                     AssessmentRound.id, AssessmentGrade.result)
              .select_from(AssessmentRound)
              .outerjoin(AssessmentGrade, (AssessmentGrade.round_id == AssessmentRound.id) & (AssessmentGrade.version == last_grade)
                         & (AssessmentRound.state == "graded"))
              .where(AssessmentRound.mode == "formal", AssessmentRound.submitted_on <= through))
    result.extend(ReviewEvent(lesson, key, number, day, grade["outcome"] if grade else "ungraded", group)
                  for lesson, key, number, day, group, grade in db.execute(formal))
    return sorted(result, key=lambda item: (item.submitted, item.number, item.lesson, item.key))


def report_window(period: Period, anchor: date) -> tuple[date, date]:
    if period == "day":
        return anchor, anchor
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    return anchor.replace(day=1), anchor.replace(day=monthrange(anchor.year, anchor.month)[1])


def project_predictions(db: Session, folder: Path, today: date) -> list[dict]:
    counts = (select(Project.id, Project.topic, Lesson.ready, func.count(Lesson.id))
              .join(Lesson, Lesson.project_id == Project.id)
              .where(Project.paused.is_(False), Lesson.learned_on.is_(None), scheduled_lesson())
              .group_by(Project.id, Project.topic, Lesson.ready)
              .order_by(Project.topic, Project.id))
    projects = {}
    for identity, topic, ready, count in db.execute(counts):
        entry = projects.setdefault(identity, {"projectId": identity, "projectTopic": topic, "left": 0, "notReady": 0})
        entry["left"] += count
        if not ready:
            entry["notReady"] += count
    if not projects:
        return []
    scheduled = defaultdict(list)
    if any(entry["notReady"] == 0 for entry in projects.values()):
        projection = forecast(load_plan(db, folder, today, days=PREDICTION_DAYS))
        for day in projection["days"]:
            for task in day["tasks"]:
                if task["kind"] == "lesson":
                    scheduled[task["projectId"]].append(task)
    for identity, entry in projects.items():
        tasks = scheduled[identity]
        entry["expectedOn"] = tasks[-1]["date"] if not entry["notReady"] and len(tasks) == entry["left"] else None
        entry["timerEstimate"] = entry["expectedOn"] is not None and all(item["estimateSource"] == "timer" for item in tasks)
    return list(projects.values())


def learning_report(db: Session, folder: Path, today: date, period: Period, anchor: date) -> dict:
    start, full_end = report_window(period, anchor)
    through = min(today, full_end)
    earlier = start - timedelta(days=1) if start > date.min else None
    later = full_end + timedelta(days=1) if full_end < today else None
    previous_start, previous_end = report_window(period, earlier) if earlier is not None else (start, start)
    previous_through = min(previous_end, previous_start + (through - start)) if earlier is not None else previous_end
    trend_start = date.fromordinal(max(1, through.toordinal() - 6)) if period == "day" else start
    entries = confirmed_times(db, through=through)
    by_day = defaultdict(list)
    for entry in entries:
        by_day[entry.report_on].append(entry)
    measured = [entry for entry in entries if entry.report_on is not None and start <= entry.report_on]
    titles, local_objectives, learned, model_lessons = {}, {}, [], set()
    lesson_rows = (select(Lesson.id, Project.topic, Project.sample, Lesson.position, Lesson.outline, Lesson.learned_on)
                   .join(Project, Project.id == Lesson.project_id))
    for identity, topic, sample, position, outline, learned_on in db.execute(lesson_rows):
        course = lesson_course(sample, position, outline)
        titles[identity] = {"projectTopic": topic, "title": course.title}
        if is_model_lesson(outline):
            model_lessons.add(identity)
        else:
            local_objectives.update(((identity, item.key), item.title) for item in course.objectives)
        if learned_on is not None:
            learned.append({"lessonId": identity, "date": learned_on, **titles[identity]})
    learned_counts = Counter(item["date"] for item in learned)
    learned_dates = {item["lessonId"]: item["date"] for item in learned}
    tracked = Counter()
    for entry in entries:
        identity = entry.timer.lesson_id
        if (identity in learned_dates and entry.category == "learning" and entry.report_on is not None
                and entry.report_on <= learned_dates[identity]):
            tracked[identity] += entry.seconds

    def efficiency(begin: date, end: date) -> dict:
        seconds = [tracked[item["lessonId"]] for item in learned
                   if begin <= item["date"] <= end and tracked[item["lessonId"]] > 0]
        return {"trackedLessons": len(seconds), "secondsPerLesson": round(sum(seconds) / len(seconds)) if seconds else None}

    snapshots = list(db.scalars(select(StudyPlanSnapshot).where(StudyPlanSnapshot.day.between(trend_start, through))))
    plans = {item.day: sum(task["minutes"] for task in item.items) * 60 for item in snapshots}
    attempts = review_events(db, through)
    by_outcome = Counter(item.outcome for item in attempts if start <= item.submitted)
    targets = {}
    for item in attempts:
        if item.outcome != "ungraded":
            targets[(item.lesson, item.key)] = item
    objective_titles = {(lesson, key): title for lesson, key, title in db.execute(
        select(AssessmentObjective.lesson_id, AssessmentObjective.key, AssessmentObjective.title)
    )}
    weak = []
    for item in sorted(targets.values(), key=lambda event: (event.submitted, event.number, event.lesson, event.key), reverse=True):
        if item.outcome in ("hinted", "failed"):
            title = (objective_titles if item.group else local_objectives).get((item.lesson, item.key))
            if title is not None:
                weak.append({"lessonId": item.lesson, "key": item.key, "title": title,
                             "projectTopic": titles[item.lesson]["projectTopic"], "on": item.submitted,
                             "outcome": item.outcome, "groupId": item.group})
    plans_in_period = [snapshot for snapshot in snapshots if start <= snapshot.day]
    planned_seconds = sum(plans[item.day] for item in plans_in_period)
    actual_on_planned_days = sum(entry.seconds for entry in measured if entry.report_on in plans)
    categories = Counter()
    for entry in measured:
        categories[entry.category] += entry.seconds
    trend = []
    for offset in range((through - trend_start).days + 1):
        day = trend_start + timedelta(days=offset)
        trend.append({"date": day, "actualSeconds": sum(item.seconds for item in by_day[day]),
                      "plannedSeconds": plans.get(day), "learned": learned_counts[day]})

    def records(values):
        rows = []
        for entry in sorted(values, key=lambda item: (item.report_on or date.min, item.timer.updated_at, item.timer.id), reverse=True):
            lesson_id, key = entry.timer.lesson_id, entry.timer.review_key
            info = titles[lesson_id]
            model = lesson_id in model_lessons
            target_title = (objective_titles if model else local_objectives).get((lesson_id, key))
            rows.append({**entry.view(), "lessonTitle": info["title"], "projectTopic": info["projectTopic"],
                         "reviewTitle": target_title, "reviewKind": ("model" if model else "local") if target_title else None})
        return rows

    learned_in_period = sorted((item for item in learned if start <= item["date"] <= through),
                               key=lambda item: (item["date"], item["title"]), reverse=True)
    return {
        "today": today, "period": period, "from": start, "through": through, "previous": earlier, "next": later,
        "totals": {"actualSeconds": sum(categories.values()), "learned": len(learned_in_period),
                   "byCategory": {name: categories[name] for name in CATEGORIES}, "review": {name: by_outcome[name] for name in OUTCOMES},
                   "plannedSeconds": planned_seconds if plans_in_period else None, "recordedPlanDays": len(plans_in_period),
                   "planBudgetSeconds": sum(item.budget_minutes for item in plans_in_period) * 60 if plans_in_period else None,
                   "actualOnPlannedDaysSeconds": actual_on_planned_days if plans_in_period else None,
                   "planDifferenceSeconds": actual_on_planned_days - planned_seconds if plans_in_period else None},
        "trend": trend, "learned": learned_in_period,
        "weak": weak, "efficiency": efficiency(start, through),
        "previousEfficiency": efficiency(previous_start, previous_through) if earlier is not None else None,
        "predictions": project_predictions(db, folder, today), "predictionDays": PREDICTION_DAYS,
        "timeRecords": records(measured), "unassignedTimers": records(by_day[None]),
    }


def study_report_router(engine: Engine, folder: Path, current_day: Callable[[Session], date],
                        now: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/api/study-report")

    @router.get("")
    def get_report(period: Period = "week", on: date | None = None):
        with transaction(engine) as db:
            today = current_day(db)
            anchor = today if on is None else on
            if anchor > today:
                raise HTTPException(422, "请选择今天或更早的日期。")
            return learning_report(db, folder, today, period, anchor)

    @router.put("/timers/{identity}/correction")
    def update_timer(identity: UUID, data: CorrectionInput):
        with transaction(engine) as db:
            if data.reportOn is not None and data.reportOn > current_day(db):
                raise HTTPException(422, "请选择今天或更早的归入日期。")
            return correct_time(db, str(identity), data.sourceOn, data.reportOn, data.seconds,
                                data.category, data.baseRevision, now()).view()

    return router
