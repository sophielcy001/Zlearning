from datetime import UTC, date, datetime
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, select, update

from .course_review import review_connection, same_model
from .course_sources import sources_current
from .db import (
    AssessmentGrade, AssessmentJob, AssessmentObjective, AssessmentPlan, AssessmentRound, CourseVersion,
    Lesson, ModelUse, Project, RemediationCard, Review,
)
from .model_failover import public_failovers
from .model_study import COUNTED, ERRORS, when_utc
from .model_study_input import connection_view, main_connection
from .project_activity import SHANGHAI, record_learning, study_day, study_moment, touch_project
from .remediation import card_status
from .schedule import next_review_date, settle_review


ASSESSMENT_ERRORS = {
    "note_material_changed": "出题期间笔记版本或标注已变化。可重试，按当前可用范围准备题组。",
    **ERRORS, "configuration_changed": "复习模型连接已变更，请重新确认发送范围。",
    "authorization_revoked": "本节复习授权已撤销。重新确认后可继续。",
    "evidence_unavailable": "复习所用依据已变化或暂时不可用。原档位与作答记录已保留。",
    "course_revision_changed": "课程依据已经修订，请在课程中确认新的复习依据。",
    "invalid_response": "模型未返回符合当前题组与依据的内容，可以重试。",
    "task_budget": "本节复习已达到调用次数上限。",
    "review_configuration_changed": "请授权一个独立的复核模型，再重新核查。",
    "remediation_configuration_changed": "请重新确认补学的生成和独立核验模型。",
}
UNSETTLED = ("grading", "rechecking")


def basis_hash(section: dict) -> str:
    fields = [section["index"], section["title"], section["text"], section["citations"]]
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


def review_basis(db, folder: Path, lesson: Lesson) -> list[dict]:
    versions = list(db.scalars(select(CourseVersion).where(
        CourseVersion.lesson_id == lesson.id,
    ).order_by(CourseVersion.version)))
    if not versions or versions[-1].version != lesson.version:
        return []
    dates = {}
    for version in versions:
        day = version.published_at.replace(tzinfo=UTC).astimezone(SHANGHAI).date().isoformat()
        for section in version.sections:
            if section["state"] == "verified":
                dates.setdefault(basis_hash(section), day)
    current = []
    for section in versions[-1].sections:
        if section["state"] != "verified" or not section["citations"] or not sources_current(db, folder, section["citations"]):
            continue
        proof = basis_hash(section)
        current.append({
            **{key: section[key] for key in ("index", "title", "text", "citations")},
            "proof": proof, "version": lesson.version, "verifiedOn": dates[proof],
            **({"previousProof": section["previousProof"]} if section.get("previousProof") else {}),
        })
    return current


def basis_in_course(db, lesson: Lesson, basis: dict | None) -> bool:
    if basis is None:
        return False
    version = db.get(CourseVersion, (lesson.id, lesson.version))
    return bool(version and any(
        section["index"] == basis["index"] and section["state"] == "verified" and basis_hash(section) == basis["proof"]
        for section in version.sections
    ))


def suspend_revised_rounds(db, lesson: Lesson, objective_ids: list[str]) -> None:
    affected = db.scalars(select(AssessmentRound).where(
        AssessmentRound.lesson_id == lesson.id, AssessmentRound.key.in_(objective_ids),
        AssessmentRound.state.in_(("preparing", "ready", *UNSETTLED)),
    )).all()
    current = {}
    for saved in affected:
        review = db.get(Review, (lesson.id, saved.key))
        if saved.state == "rechecking":
            last = db.scalar(select(AssessmentGrade).where(AssessmentGrade.round_id == saved.id)
                             .order_by(AssessmentGrade.version.desc()).limit(1))
            if last is not None and saved.mode == "formal":
                review.stage = last.result["stage"]
                due = last.result["dueOn"]
                review.due_on = date.fromisoformat(due) if due else None
                review.round_number = max(review.round_number, saved.number + 1)
        elif saved.number == review.round_number:
            current[review.key] = review
        saved.state = "superseded"
        db.execute(update(AssessmentJob).where(
            AssessmentJob.round_id == saved.id, AssessmentJob.state != "complete",
        ).values(state="complete", error="course_revision_changed", available_at=None))
    for review in current.values():
        review.round_number += 1


def recipient_models(db):
    main = main_connection(db)
    if main is None or (main.provider != "custom" and not main.credential_ref):
        return None, None
    review = review_connection(db)
    if review is None or (review.provider != "custom" and not review.credential_ref):
        return main, None
    return main, None if same_model(connection_view(main), connection_view(review)) else review


def new_job(db, lesson_id: str, kind: str, now: datetime, *, round_id: str | None = None,
            basis: dict | None = None, first_due_on: date | None = None, remediation_id: str | None = None) -> AssessmentJob:
    job = AssessmentJob(id=str(uuid4()), lesson_id=lesson_id, kind=kind, round_id=round_id,
                        basis=basis, first_due_on=first_due_on, remediation_id=remediation_id,
                        state="queued", created_at=when_utc(now))
    db.add(job)
    return job


def schedule_objectives(db, lesson: Lesson, plan: AssessmentPlan, now: datetime) -> None:
    if lesson.learned_on is None:
        return
    planned = {job.basis["proof"] for job in db.scalars(select(AssessmentJob).where(
        AssessmentJob.lesson_id == lesson.id, AssessmentJob.kind == "objectives",
    ))}
    for basis in plan.sections:
        if basis["proof"] not in planned:
            eligible_on = max(lesson.learned_on, date.fromisoformat(basis["verifiedOn"]))
            new_job(db, lesson.id, "objectives", now, basis=basis, first_due_on=next_review_date(eligible_on, 0))


def assessment_usage(db, lesson_id: str) -> int:
    return db.scalar(select(func.count()).select_from(ModelUse).join(
        AssessmentJob, AssessmentJob.id == ModelUse.assessment_job_id,
    ).where(AssessmentJob.lesson_id == lesson_id, COUNTED))


def queued_assessments(db) -> int:
    return db.scalar(select(func.count()).select_from(AssessmentJob).join(
        Lesson, Lesson.id == AssessmentJob.lesson_id,
    ).join(Project, Project.id == Lesson.project_id).where(
        (AssessmentJob.state == "running") | (
            (AssessmentJob.state == "queued") & Lesson.learned_on.is_not(None) & Project.paused.is_(False)
        ),
    ))


def settings_view(db, lesson: Lesson, plan: AssessmentPlan | None) -> dict:
    jobs = list(db.scalars(select(AssessmentJob).where(AssessmentJob.lesson_id == lesson.id).order_by(AssessmentJob.created_at)))
    main, reviewer = recipient_models(db)
    return {
        "granted": plan is not None,
        "authorized": bool(plan and main and plan.primary_model == connection_view(main)),
        "reviewAuthorized": bool(plan and reviewer and plan.review_model == connection_view(reviewer)),
        "remediation": bool(plan and plan.remediation),
        "backups": (plan.backups or {}) if plan else {},
        "maxCalls": plan.max_calls if plan else None, "calls": assessment_usage(db, lesson.id),
        "jobs": [{"kind": job.kind, "state": job.state, "roundId": job.round_id,
                  "error": ASSESSMENT_ERRORS.get(job.error),
                  "failovers": public_failovers(job.failovers, ASSESSMENT_ERRORS)}
                 for job in jobs if job.state != "complete"],
    }


def new_round(db, lesson: Lesson, review: Review, mode: str, course_version: int,
              group: dict | None = None) -> AssessmentRound:
    saved = AssessmentRound(
        id=str(uuid4()), lesson_id=lesson.id, key=review.key, number=review.round_number, mode=mode,
        course_version=course_version, state="ready" if group else "preparing", group=group, draft=[], hinted=False,
    )
    db.add(saved)
    return saved


def install_objectives(db, job: AssessmentJob, lesson: Lesson, payload: dict) -> None:
    basis = job.basis
    linked = [item for item in db.scalars(select(AssessmentObjective).where(AssessmentObjective.lesson_id == lesson.id))
              if item.basis["proof"] == basis.get("previousProof")]
    for index, values in enumerate(payload["objectives"]):
        identity = str(uuid5(NAMESPACE_URL, f"zhixue/{lesson.id}/objective/{basis['index']}/{index}/{values['ability']}"))
        existing = db.get(AssessmentObjective, (lesson.id, identity))
        if existing is not None and existing.basis["proof"] == basis["proof"]:
            continue
        matches = [item for item in linked if item.ability == values["ability"] and item.title == values["title"]
                   and item.criterion == values["criterion"] and not basis_in_course(db, lesson, item.basis)]
        if len(matches) == 1:
            prior = matches[0]
            prior.basis, prior.held = basis, False
            review = db.get(Review, (lesson.id, prior.key))
            if review.due_on is not None and round_for(db, review, "formal") is None:
                new_round(db, lesson, review, "formal", basis["version"], values["group"])
            continue
        if existing is not None:
            identity = str(uuid5(NAMESPACE_URL, f"zhixue/{lesson.id}/objective/{basis['proof']}/{index}/{values['ability']}"))
            if db.get(AssessmentObjective, (lesson.id, identity)) is not None:
                continue
        objective = AssessmentObjective(lesson_id=lesson.id, key=identity, title=values["title"], ability=values["ability"],
                                         criterion=values["criterion"], basis=basis, held=False)
        review = Review(lesson_id=lesson.id, key=identity, stage=0, due_on=job.first_due_on, round_number=1)
        db.add_all([objective, review])
        new_round(db, lesson, review, "formal", basis["version"], values["group"])


def objective_data(objective: AssessmentObjective) -> dict:
    return {key: getattr(objective, key) for key in ("title", "ability", "criterion")}


def round_for(db, review: Review, mode: str, *, number: int | None = None) -> AssessmentRound | None:
    return db.scalar(select(AssessmentRound).where(
        AssessmentRound.lesson_id == review.lesson_id, AssessmentRound.key == review.key,
        AssessmentRound.number == (review.round_number if number is None else number), AssessmentRound.mode == mode,
    ))


def pending_round(db, review: Review) -> AssessmentRound | None:
    return db.scalar(select(AssessmentRound).where(
        AssessmentRound.lesson_id == review.lesson_id, AssessmentRound.key == review.key,
        AssessmentRound.mode == "formal", AssessmentRound.state.in_(UNSETTLED),
    ).limit(1))


def delayed_review(db, review: Review) -> bool:
    if review.due_on is None or review.round_number == 1:
        return False
    result = db.scalar(
        select(AssessmentGrade.result)
        .join(AssessmentRound, AssessmentRound.id == AssessmentGrade.round_id)
        .where(AssessmentRound.lesson_id == review.lesson_id, AssessmentRound.key == review.key,
               AssessmentRound.mode == "formal", AssessmentRound.number < review.round_number,
               AssessmentRound.state.in_(("graded", "superseded")))
        .order_by(AssessmentRound.number.desc(), AssessmentGrade.version.desc()).limit(1)
    )
    return bool(result and result.get("gradingDelayed") and result["dueOn"] == review.due_on.isoformat())


def assessment_status(db, folder: Path, project: Project, lesson: Lesson, review: Review,
                      objective: AssessmentObjective, day: date) -> str:
    objective.held = (not basis_in_course(db, lesson, objective.basis)
                      or not sources_current(db, folder, objective.basis["citations"]))
    if project.paused or lesson.learned_on is None or not lesson.ready:
        return "paused"
    if pending_round(db, review) is not None:
        return "waiting"
    if review.due_on is None:
        return "complete"
    if objective.held:
        return "held"
    return "due" if review.due_on <= day else "upcoming"


def assessment_review_info(project: Project, review: Review, objective: AssessmentObjective, status: str) -> dict:
    return {
        "kind": "model", "projectId": project.id, "projectTopic": project.topic, "lessonId": review.lesson_id,
        "key": review.key, "title": objective.title, "ability": objective.ability, "dueOn": review.due_on,
        "stage": review.stage, "status": status,
    }


def withdraw_remediation(db, round_id: str) -> None:
    db.execute(update(RemediationCard).where(
        RemediationCard.round_id == round_id, RemediationCard.active.is_(True),
    ).values(active=False))
    db.execute(update(AssessmentJob).where(
        AssessmentJob.round_id == round_id, AssessmentJob.remediation_id.is_not(None),
        AssessmentJob.state.in_(("queued", "blocked")),
    ).values(state="complete", error=None, available_at=None))


def record_remediation(db, saved: AssessmentRound, grade: AssessmentGrade) -> RemediationCard | None:
    if not any(not item["met"] for item in grade.result["items"]):
        return None
    card = db.get(RemediationCard, grade.id)
    if card is None:
        objective = db.get(AssessmentObjective, (saved.lesson_id, saved.key))
        card = RemediationCard(grade_id=grade.id, lesson_id=saved.lesson_id, round_id=saved.id, basis=objective.basis,
                               active=True, created_at=grade.created_at)
        db.add(card)
        db.flush()
    return card


def queue_remediation(db, card: RemediationCard | None, plan: AssessmentPlan | None, now: datetime) -> None:
    if (card is None or not card.active or card.approved or card.findings is not None or plan is None or not plan.remediation
            or not any(basis["proof"] == card.basis["proof"] for basis in plan.sections)):
        return
    job = db.scalar(select(AssessmentJob.id).where(
        AssessmentJob.remediation_id == card.grade_id, AssessmentJob.state != "complete",
    ).limit(1))
    if job is None:
        kind = "remediation_check" if card.draft else "remediation"
        new_job(db, card.lesson_id, kind, now, round_id=card.round_id, remediation_id=card.grade_id)


def schedule_remediations(db, lesson: Lesson, plan: AssessmentPlan, now: datetime) -> None:
    if not plan.remediation:
        return
    latest = select(AssessmentGrade.id).where(AssessmentGrade.round_id == AssessmentRound.id).order_by(
        AssessmentGrade.version.desc(),
    ).limit(1).correlate(AssessmentRound).scalar_subquery()
    for saved, grade in db.execute(select(AssessmentRound, AssessmentGrade).join(
        AssessmentGrade, AssessmentGrade.id == latest,
    ).where(AssessmentRound.lesson_id == lesson.id, AssessmentRound.state == "graded")):
        queue_remediation(db, record_remediation(db, saved, grade), plan, now)


def round_view(db, saved: AssessmentRound) -> dict:
    job = db.scalar(select(AssessmentJob).where(
        AssessmentJob.round_id == saved.id, AssessmentJob.remediation_id.is_(None), AssessmentJob.state != "complete",
    ).order_by(AssessmentJob.created_at.desc()).limit(1))
    grades = list(db.scalars(select(AssessmentGrade).where(
        AssessmentGrade.round_id == saved.id,
    ).order_by(AssessmentGrade.version)))
    submitted = saved.submission
    card = db.get(RemediationCard, grades[-1].id) if grades else None
    if card:
        remediation = card_status(db, card)
        remediation["error"] = ASSESSMENT_ERRORS.get(remediation["error"])
    elif grades and any(not item["met"] for item in grades[-1].result["items"]):
        remediation = {"id": grades[-1].id, "state": "authorization_required", "error": None, "fileId": None}
    else:
        remediation = None
    return {
        "id": saved.id, "round": saved.number, "mode": saved.mode, "state": saved.state, "courseVersion": saved.course_version,
        "questions": [{"text": item["text"]} for item in saved.group["questions"]] if saved.group else [],
        "draft": saved.draft, "hint": saved.group["hint"] if saved.hinted else None,
        "answers": submitted["answers"] if submitted else [],
        "result": grades[-1].result if grades else None,
        "history": [{**grade.result, "reviewer": grade.reviewer, "evaluatedAt": grade.created_at.isoformat() + "Z"} for grade in grades],
        "jobState": job.state if job else None,
        "error": ASSESSMENT_ERRORS["course_revision_changed"] if saved.state == "superseded"
                 else ASSESSMENT_ERRORS.get(job.error) if job else None,
        "remediation": remediation,
        **({"noteReferences": saved.group["noteReferences"]}
           if grades and saved.group and saved.group.get("noteReferences") else {}),
    }


def finish_grade(db, saved: AssessmentRound, review: Review, project: Project, report: dict,
                 finished_at: datetime, *, reviewer: dict | None, identity: str) -> None:
    questions = saved.group["questions"]
    by_index = {item["index"]: item for item in report["items"]}
    if saved.submission["action"] == "unknown":
        outcome = "failed"
    elif saved.hinted:
        outcome = "hinted"
    else:
        outcome = "passed" if all(by_index[i]["met"] for i, question in enumerate(questions) if question["required"]) else "failed"
    scored_on = study_day(db, finished_at.replace(tzinfo=UTC).astimezone(SHANGHAI).date())
    if saved.mode == "formal":
        review.stage, review.due_on = settle_review(saved.submitted_on, saved.previous_stage, outcome)
        review.round_number = saved.number + 1
    due = review.due_on
    explanation = report["explanation"]
    result = {
        "outcome": outcome, "stage": review.stage, "dueOn": due.isoformat() if due else None,
        "explanation": explanation, "submittedOn": saved.submitted_on.isoformat(), "scoredOn": scored_on.isoformat(),
        "gradingDelayed": bool(saved.mode == "formal" and due and scored_on > saved.submitted_on and due <= scored_on),
        "items": [{**by_index[index], "required": question["required"]} for index, question in enumerate(questions)],
    }
    version = db.scalar(select(func.max(AssessmentGrade.version)).where(AssessmentGrade.round_id == saved.id)) or 0
    grade = AssessmentGrade(id=identity, round_id=saved.id, version=version + 1, reviewer=reviewer,
                            created_at=finished_at, result=result)
    db.add(grade)
    saved.state = "graded"
    db.flush()
    queue_remediation(db, record_remediation(db, saved, grade), db.get(AssessmentPlan, saved.lesson_id), finished_at)
    record_learning(project, study_moment(saved.submitted_at, saved.submitted_on))
    touch_project(project, study_moment(finished_at, scored_on))
