from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import defer

from .content import is_practice
from .db import Project, RouteVersion, StageAssessment, StageAssessmentAttempt, StageReview
from .stage_questions import SQLITE_STAGE_QUESTIONS
from .stage_review import model_review_view, stop_review


def phase_key(name: str, nodes: list[dict]) -> str:
    return sha256(json.dumps([name, sorted(node["id"] for node in nodes)]).encode("utf-8")).hexdigest()


def planned_phases(nodes: list[dict]) -> dict[str, tuple[str, list[dict]]]:
    phases: dict[tuple[str, str | None], list[dict]] = {}
    for node in nodes:
        if not is_practice(node):
            phases.setdefault((node["phase"], node.get("stageId")), []).append(node)
    planned = {}
    for (name, _), members in phases.items():
        core = [node for node in members if node.get("required", True)] or members
        planned[phase_key(name, core)] = (name, core)
    return planned


def latest_assessment_route(db, project_id: str) -> RouteVersion | None:
    return db.scalar(select(RouteVersion).where(RouteVersion.project_id == project_id)
                     .order_by(RouteVersion.version.desc()).limit(1))


def assessment_phases(db, project: Project):
    route = latest_assessment_route(db, project.id)
    if route is None:
        return None, {}, {}
    planned = planned_phases(route.definition["nodes"])
    learned = {lesson.id for lesson in project.lessons if lesson.learned_on is not None}
    completed = {key: phase for key, phase in planned.items()
                 if all(node["id"] in learned for node in phase[1])}
    return route.version, planned, completed


def completed_phases(db, project: Project) -> tuple[int | None, dict[str, tuple[str, list[dict]]]]:
    version, _, completed = assessment_phases(db, project)
    return version, completed


def ensure_assessments(db, project: Project, now: datetime) -> tuple[list[StageAssessment], set[str], set[str]]:
    route_version, planned, completed = assessment_phases(db, project)
    existing = {row.phase_key: row for row in db.scalars(
        select(StageAssessment).where(StageAssessment.project_id == project.id)
        .order_by(StageAssessment.created_at, StageAssessment.id),
    )}
    lessons = {lesson.id: lesson for lesson in project.lessons}
    for key, (name, nodes) in completed.items():
        if key in existing:
            continue
        criteria = []
        for node in nodes:
            lesson = lessons[node["id"]]
            criterion = {"lessonId": lesson.id, "title": node["title"], "outcome": node["outcome"]}
            if project.sample and lesson.outline is None and 0 <= lesson.position < len(SQLITE_STAGE_QUESTIONS):
                criterion["variants"] = [[asdict(question) for question in variant]
                                         for variant in SQLITE_STAGE_QUESTIONS[lesson.position]]
            criteria.append(criterion)
        row = StageAssessment(id=str(uuid4()), project_id=project.id, phase_key=key, phase=name,
                              route_version=route_version, created_at=now, criteria=criteria, attempt_count=0,
                              status="awaiting", revision=1)
        db.add(row)
        existing[key] = row
    ordered = [existing[key] for key in completed]
    ordered.extend(row for key, row in existing.items() if key not in completed)
    return ordered, set(completed), set(planned)


def stop_retired_reviews(db, project_id: str, phase_keys: set[str], now: datetime) -> None:
    if not phase_keys:
        return
    reviews = db.execute(
        select(StageReview, StageAssessment.phase_key).select_from(StageAssessment)
        .join(StageAssessmentAttempt, StageAssessmentAttempt.assessment_id == StageAssessment.id)
        .join(StageReview, StageReview.id == StageAssessmentAttempt.id)
        .where(StageAssessment.project_id == project_id, StageReview.state.in_(("queued", "running", "blocked")))
    )
    for job, phase in reviews:
        if phase in phase_keys:
            stop_review(db, job, now)


def model_review_states(db, project_id: str) -> dict[str, str]:
    states = db.execute(
        select(StageAssessment.id, StageReview.state).select_from(StageAssessment)
        .join(StageAssessmentAttempt, StageAssessmentAttempt.assessment_id == StageAssessment.id)
        .join(StageReview, StageReview.id == StageAssessmentAttempt.id)
        .where(StageAssessment.project_id == project_id, StageAssessment.status == "awaiting_review",
               StageAssessment.attempt_count == StageAssessmentAttempt.number)
    )
    return {identity: state for identity, state in states}


def assessment_summary(row: StageAssessment, eligible: bool, current_scope: bool) -> dict:
    return {
        "id": row.id, "phase": row.phase, "routeVersion": row.route_version,
        "status": "skipped" if row.skipped_at else row.status, "eligible": eligible, "currentScope": current_scope,
        "hasQuiz": all("variants" in criterion for criterion in row.criteria),
        "attemptCount": row.attempt_count, "revision": row.revision,
    }


def stage_quiz(row: StageAssessment) -> list[dict]:
    if any("variants" not in criterion for criterion in row.criteria):
        return []
    return [
        {**question, "lessonId": criterion["lessonId"]}
        for criterion in row.criteria
        for question in criterion["variants"][row.attempt_count % len(criterion["variants"])]
    ]


def grade_quiz(quiz: list[dict], answers: list[int]) -> dict:
    graded = [{**item, "answer": answer, "matched": answer == item["correct"]}
              for item, answer in zip(quiz, answers, strict=True)]
    findings = [
        {"lessonId": lesson_id, "verdict": "met" if all(item["matched"] for item in graded if item["lessonId"] == lesson_id)
         else "needs_work"}
        for lesson_id in dict.fromkeys(item["lessonId"] for item in graded)
    ]
    return {"reviewer": "quiz", "findings": findings, "questions": graded}


def review_outcome(findings: list[dict]) -> str:
    verdicts = {item["verdict"] for item in findings}
    if "needs_work" in verdicts:
        return "needs_work"
    return "insufficient" if "insufficient" in verdicts else "passed"


def apply_stage_result(row: StageAssessment, attempt: StageAssessmentAttempt, result: dict) -> None:
    attempt.result = result
    attempt.outcome = row.status = review_outcome(result["findings"])
    row.revision += 1


def attempt_summary(attempt: StageAssessmentAttempt) -> dict:
    return {"id": attempt.id, "number": attempt.number, "kind": attempt.kind,
            "outcome": attempt.outcome, "submittedAt": attempt.submitted_at}


def attempt_view(db, attempt: StageAssessmentAttempt) -> dict:
    evidence = attempt.submission.get("evidence", [])
    return {
        **attempt_summary(attempt), "evidence": evidence, "scope": "static" if evidence else "quiz",
        "findings": [], **(attempt.result or {}),
        "modelReview": model_review_view(db, attempt.id) if evidence else None,
    }


def assessment_view(db, row: StageAssessment, eligible: bool) -> dict:
    latest = db.scalar(select(StageAssessmentAttempt).where(
        StageAssessmentAttempt.assessment_id == row.id, StageAssessmentAttempt.number == row.attempt_count,
    ))
    older = db.scalars(select(StageAssessmentAttempt).where(
        StageAssessmentAttempt.assessment_id == row.id, StageAssessmentAttempt.number < row.attempt_count,
    ).order_by(StageAssessmentAttempt.number.desc()).options(
        defer(StageAssessmentAttempt.submission), defer(StageAssessmentAttempt.result),
    ))
    attempts = [attempt_view(db, latest)] if latest else []
    attempts.extend(attempt_summary(attempt) for attempt in older)
    current_scope = eligible
    if not current_scope and (route := latest_assessment_route(db, row.project_id)):
        current_scope = row.phase_key in planned_phases(route.definition["nodes"])
    return {
        **assessment_summary(row, eligible, current_scope),
        "criteria": [{key: criterion[key] for key in ("lessonId", "title", "outcome")} for criterion in row.criteria],
        "quiz": [{key: question[key] for key in ("lessonId", "prompt", "options")} for question in stage_quiz(row)],
        "attempts": attempts,
    }


def awaiting_stage_review(db, row: StageAssessment, attempt: StageAssessmentAttempt, project: Project) -> bool:
    if row.skipped_at or project.paused or attempt.result is not None or attempt.number != row.attempt_count:
        return False
    _, completed = completed_phases(db, project)
    return row.phase_key in completed
