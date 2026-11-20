from collections.abc import Iterator
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, lazyload

from .assessment import assessment_review_info, assessment_status
from .content import Objective, lesson_course
from .db import AssessmentObjective, Lesson, Project, Review


def review_status(paused: bool, eligible: bool, due: date | None, day: date) -> str:
    if due is None:
        return "complete"
    if paused or not eligible:
        return "paused"
    return "due" if due <= day else "upcoming"


def review_info(project_id: str, project_topic: str, review: Review, objective: Objective, status: str) -> dict:
    return {
        "kind": "local",
        "projectId": project_id, "projectTopic": project_topic, "lessonId": review.lesson_id,
        "key": objective.key, "title": objective.title, "dueOn": review.due_on, "stage": review.stage,
        "status": status,
    }


def review_queue(db: Session, folder: Path, day: date, through: date | None = None) -> Iterator[tuple[Project, Lesson, Review, dict]]:
    records = (select(Project, Lesson, Review, AssessmentObjective).select_from(Review)
               .join(Lesson, Lesson.id == Review.lesson_id).join(Project, Project.id == Lesson.project_id)
               .outerjoin(AssessmentObjective, (AssessmentObjective.lesson_id == Review.lesson_id)
                          & (AssessmentObjective.key == Review.key))
               .options(lazyload("*"))
               .order_by(Project.created_on.desc(), Project.id, Lesson.position, Review.key))
    if through is not None:
        records = records.where(
            Review.due_on <= through, Project.paused.is_(False), Lesson.ready.is_(True), Lesson.learned_on.is_not(None),
        )
    for project, lesson, review, target in db.execute(records):
        if lesson.outline:
            if target:
                status = assessment_status(db, folder, project, lesson, review, target, day)
                yield project, lesson, review, assessment_review_info(project, review, target, status)
            continue
        course = lesson_course(project.sample, lesson.position, lesson.outline)
        objective = next((item for item in course.objectives if item.key == review.key), None)
        if objective:
            status = review_status(project.paused, lesson.ready and lesson.learned_on is not None, review.due_on, day)
            yield project, lesson, review, review_info(project.id, project.topic, review, objective, status)
