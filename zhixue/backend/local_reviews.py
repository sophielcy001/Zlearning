from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .content import Objective, lesson_course
from .daily_plan import finish_booking, freeze_plan_baseline, review_task_id
from .db import Attempt, Lesson, Project, Review
from .project_activity import record_learning
from .review_queue import review_status
from .schedule import settle_review


def find_review(db: Session, lesson_id: UUID, key: str) -> tuple[Project, Lesson, Review, Objective]:
    lesson = db.get(Lesson, str(lesson_id))
    if lesson is None:
        raise HTTPException(404, "找不到这条记录。")
    project = db.get(Project, lesson.project_id)
    review = db.get(Review, (lesson.id, key))
    if review is None:
        raise HTTPException(404, "找不到这条记录。")
    course = lesson_course(project.sample, lesson.position, lesson.outline)
    objective = next((item for item in course.objectives if item.key == key), None)
    if objective is None:
        raise HTTPException(404, "此知识目标使用模型复习入口。")
    return project, lesson, review, objective


def require_current_round(project: Project, lesson: Lesson, review: Review, round_number: int, day: date) -> None:
    if review.round_number != round_number or review_status(project.paused, lesson.ready and lesson.learned_on is not None, review.due_on, day) != "due":
        raise HTTPException(409, "当前轮次暂不可提交，请刷新复习页。")


def submit_local_review(db: Session, folder: Path, target, round_number: int,
                        selected: list[int | None], action: str, day: date, moment) -> dict:
    project, lesson, review, objective = target
    key = review.key
    attempt_key = (lesson.id, key, round_number)
    input_data = {"answers": selected, "action": action}
    previous = db.get(Attempt, attempt_key)
    if previous:
        if previous.input != input_data:
            raise HTTPException(409, "该轮复习已提交另一份答案。")
        return previous.result
    require_current_round(project, lesson, review, round_number, day)
    answers = [item.correct for item in objective.questions(round_number)]
    if action == "submit" and (len(selected) != len(answers) or None in selected):
        raise HTTPException(422, "请先答完本组题目。")

    freeze_plan_baseline(db, folder, day)
    previous_due = review.due_on
    if action == "unknown":
        outcome = "failed"
    elif review.hinted:
        outcome = "hinted"
    elif selected != answers:
        outcome = "failed"
    else:
        outcome = "passed"
    review.stage, review.due_on = settle_review(day, review.stage, outcome)

    review.round_number += 1
    review.answers = []
    review.hinted = False
    result = {"outcome": outcome, "stage": review.stage,
              "dueOn": review.due_on.isoformat() if review.due_on else None,
              "explanation": objective.explanation, "submittedOn": day.isoformat()}
    db.add(Attempt(
        lesson_id=lesson.id, key=key, round_number=round_number, submitted_on=day,
        previous_due_on=previous_due, course_version=lesson.version, input=input_data, result=result,
    ))
    finish_booking(db, review_task_id(lesson.id, key, round_number), day)
    record_learning(project, moment(db))
    return result
