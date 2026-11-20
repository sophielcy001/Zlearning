from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from .assessment import delayed_review, queued_assessments, schedule_objectives
from .assessment_api import assessment_router
from .content import is_model_lesson, is_practice, lesson_course
from .lesson_assistant_api import lesson_assistant_router
from .backup_api import backup_router, recover_backups
from .chat_api import conversation_router
from .chat_model_api import chat_model_router
from .course_disputes import course_disputes_router, pending_disputes_count
from .course_reading import ReadingPositionInput, course_document, position_view
from .course_revision_api import course_revision_router
from .course_review_api import course_review_router
from .daily_budget import daily_budget_router
from .daily_plan import finish_booking, freeze_plan_baseline, lesson_task_id
from .daily_plan_api import daily_plan_router
from .db import (
    AssessmentPlan, Attempt, CourseVersion, DemoClock, GoalVersion, Lesson, LessonReadingPosition, ModelStudy, Note,
    NoteLesson, Project, Review, open_db, transaction, upgrade,
)
from .generation_api import generation_router
from .goal_authoring_api import goal_authoring_router
from .graph_api import graph_router
from .graph_recommendation_api import graph_recommendation_router
from .file_locations import LocationTracker
from .library import local_path
from .library_api import library_router
from .library_classification import apply_approved as recover_classifications
from .library_classification_api import classification_router
from .library_copies import recover_library_copies
from .library_index import LibraryIndex
from .library_trash import recover_library_trash
from .local_reviews import find_review, require_current_round, submit_local_review
from .desktop_offline import desktop_offline_router
from .local_service import local_app, mount_frontend
from .mobile_api import mobile_endpoints, workspace_id
from .mobile_pairing import DesktopPairing, PairingCredentialStore, pairing_router
from .mobile_push import MobilePush
from .mobile_timer import timer_endpoints
from .model_costs import MoneyLimit
from .model_credentials import CredentialStore, NativeCredentialStore, retry_credential_cleanup
from .model_protocols import Transport, post_json
from .model_ledger import ledger_router, settle_open_charges
from .model_settings_api import model_settings_router
from .model_study import resume_study, sync_task, when_utc
from .model_usage import billing_day
from .note_ai import note_ai_router
from .note_checks_api import note_checks_router
from .notes import NoteFileError, note_snapshot
from .notes_api import note_router
from .project_activity import SHANGHAI, record_learning, study_day, study_moment, touch_project
from .prerequisite_api import prerequisite_router
from .project_board import app_project_summary, project_board_router, row_date, tags_for
from .project_goal_api import project_goal_router
from .project_versions import core_fields, create_local_project, goal_values, latest_route, version_router
from .reminders import reminder_endpoints
from .review_queue import review_info, review_queue, review_status
from .schedule import next_review_date
from .stage_assessment_api import stage_assessment_router
from .stage_assessments import ensure_assessments
from .study_consolidation import schedule_consolidation
from .study_consolidation_api import study_consolidation_router
from .study_report import study_report_router
from .teaching_style import teaching_style_router
from .timer import TimerInput, change_timer, timer_view
from .recognition import Recognition, recognition_router
from .voice_api import voice_router
from .workspace_setup import setup_router


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=180)]
Choice = Annotated[int, Field(strict=True, ge=0, le=2)]


class GoalInput(BaseModel):
    id: UUID
    topic: ShortText
    outcome: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    foundation: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


class PauseInput(BaseModel):
    paused: bool


class LearnedInput(BaseModel):
    learned: bool


class RoundInput(BaseModel):
    round: int = Field(ge=1)


class AnswerInput(RoundInput):
    answers: list[Choice | None] = Field(max_length=2)


class SubmitInput(AnswerInput):
    action: Literal["submit", "unknown"]


def required(db: Session, model, key):
    item = db.get(model, key)
    if item is None:
        raise HTTPException(404, "找不到这条记录。")
    return item


def project_info(db: Session, project: Project) -> dict:
    route = latest_route(db, project.id)
    nodes = {} if route is None else {node["id"]: node for node in route.definition["nodes"]}
    lessons = [{
        "id": lesson.id,
        "title": (course := lesson_course(project.sample, lesson.position, lesson.outline)).title,
        "phase": (node := nodes.get(lesson.id, lesson.outline or {})).get("phase", course.phase),
        "stageId": node.get("stageId"),
        "branch": node.get("branch"),
        "outcome": course.outcome,
        "ready": lesson.ready,
        "learnedOn": lesson.learned_on,
        "prerequisites": lesson.outline["prerequisites"] if lesson.outline else [],
        "kind": "practice" if is_practice(lesson.outline) else "lesson",
        "learningMark": node.get("learningMark"),
        "required": (lesson.outline or {}).get("required", True),
        "preparationError": (lesson.outline or {}).get("preparationError"),
    } for lesson in project.lessons]
    learned = sum(lesson.learned_on is not None for lesson in project.lessons)
    generated = [lesson for lesson in lessons if lesson["kind"] == "lesson"]
    core = [lesson for lesson in lessons if lesson["required"]] or generated
    return {
        "id": project.id, "topic": project.topic, "outcome": project.outcome, "foundation": project.foundation,
        "sample": project.sample, "createdOn": project.created_on, "learned": learned, "lessons": lessons,
        "generationMode": "model" if db.get(ModelStudy, project.id) else "local",
        "routeVersion": route.version if route is not None else None,
        "status": "paused" if project.paused else "done" if core and all(lesson["learnedOn"] for lesson in core) else "active",
        "priority": project.priority, "tags": tags_for(db, [project.id]).get(project.id, []),
        "updatedOn": row_date(project.updated_at) or project.created_on,
        "lastLearnedOn": row_date(project.last_learned_at),
        "task": {"status": project.task.status, "error": project.task.error,
                 "completed": sum(lesson["ready"] for lesson in generated), "total": len(generated)},
    }


def create_app(
    folder: Path, session_token: str, today: Callable[[], date] | None = None,
    now: Callable[[], datetime] | None = None, *,
    credential_store: CredentialStore | None = None, model_transport: Transport | None = None,
    backup_credentials: CredentialStore | None = None, pairing_credentials: CredentialStore | None = None,
    workspace_cost_limit: MoneyLimit | None = None,
    desktop_offline: Path | None = None,
) -> FastAPI:
    folder = folder.resolve()
    engine = open_db(folder, workspace_cost_limit=workspace_cost_limit)
    get_today = today or (lambda: datetime.now(SHANGHAI).date())
    get_now = now or (lambda: datetime.now(UTC).replace(tzinfo=None))
    vault = credential_store if credential_store is not None else NativeCredentialStore()
    recognition = Recognition(engine, vault, get_now)

    @asynccontextmanager
    async def lifespan(_):
        try:
            upgrade(engine)
            with transaction(engine) as db:
                settle_open_charges(db, when_utc(get_now()), direct=True)
            recover_library_trash(engine, folder)
            recover_library_copies(engine, folder)
            recover_classifications(engine, folder, library_index, get_now)
            recover_backups(engine, folder)
            retry_credential_cleanup(engine, vault, startup=True)
            pairing.start()
            mobile_push.start()
            yield
        finally:
            mobile_push.close()
            pairing.close()
            engine.dispose()

    app = local_app(session_token, lifespan=lifespan)
    app.include_router(setup_router())
    if desktop_offline is not None:
        app.include_router(desktop_offline_router(desktop_offline))

    def demo_day(db: Session) -> date:
        return study_day(db, get_today())

    def project_moment(db: Session) -> datetime:
        return study_moment(get_now(), demo_day(db))

    @app.get("/api/state")
    def state():
        with transaction(engine, write=False) as db:
            day = demo_day(db)
            return {"today": day, "realToday": get_today(), "timer": timer_view(db, get_now()),
                    "phonePaired": pairing.pair is not None,
                    "assessmentQueued": queued_assessments(db),
                    "disputeCount": pending_disputes_count(db), **app_project_summary(db, day)}

    @app.get("/api/reviews")
    def reviews():
        with transaction(engine) as db:
            day = demo_day(db)
            items = []
            for _, _, review, item in review_queue(db, folder, day):
                if item["kind"] == "model" and item["status"] == "due" and delayed_review(db, review):
                    item["gradingDelayed"] = True
                items.append(item)
            return {"today": day, "reviews": items, "assessmentQueued": queued_assessments(db)}

    @app.get("/api/timer")
    def get_timer():
        with transaction(engine, write=False) as db:
            return timer_view(db, get_now())

    @app.post("/api/timer")
    def control_timer(payload: TimerInput):
        with transaction(engine) as db:
            now = get_now()
            day = demo_day(db)
            return change_timer(db, payload, now, study_moment(now, day), lambda: freeze_plan_baseline(db, folder, day))

    reminders, dispatch_reminders = reminder_endpoints(engine, folder, demo_day, get_now)
    app.include_router(reminders)
    app.include_router(daily_plan_router(engine, folder, demo_day, session_token))
    app.include_router(study_report_router(engine, folder, demo_day, get_now))
    app.include_router(project_board_router(engine, demo_day, project_moment))
    app.include_router(model_settings_router(engine, vault, model_transport or post_json, get_now,
                                             recognition.recipient_changes))
    app.include_router(backup_router(engine, folder, session_token, demo_day, backup_credentials))
    app.include_router(conversation_router(engine, get_now, project_moment, session_token))
    app.include_router(chat_model_router(engine, get_now, session_token))
    app.include_router(teaching_style_router(engine))
    app.include_router(lesson_assistant_router(engine, folder, session_token, get_now))
    app.include_router(version_router(engine, session_token, project_moment, get_now))
    app.include_router(project_goal_router(engine, session_token, project_moment))
    app.include_router(daily_budget_router(engine, folder, get_now))
    app.include_router(ledger_router(engine, lambda: billing_day(get_now())))
    app.include_router(generation_router(engine, session_token, get_now))
    app.include_router(course_review_router(engine, folder, session_token))
    app.include_router(course_revision_router(engine, folder, session_token, get_now))
    app.include_router(course_disputes_router(engine, folder, session_token, get_now))
    app.include_router(assessment_router(engine, folder, session_token, get_now, demo_day))
    app.include_router(stage_assessment_router(engine, project_moment, session_token, get_now))
    app.include_router(study_consolidation_router(engine, folder, session_token, get_now))
    app.include_router(prerequisite_router(engine, folder, session_token, get_now))

    @app.post("/api/projects", status_code=201)
    def new_project(payload: GoalInput):
        with transaction(engine) as db:
            identity = str(payload.id)
            values = (payload.topic, payload.outcome, payload.foundation)
            existing = db.get(Project, identity)
            if existing:
                first = db.get(GoalVersion, (identity, 1))
                initial = goal_values(first.definition) if first else {
                    "topic": existing.topic, "outcome": existing.outcome, "foundation": existing.foundation,
                }
                if values != (initial["topic"], initial["outcome"], initial.get("foundation", "")):
                    raise HTTPException(409, "这张目标卡已经提交，请新建另一张目标卡。")
                return project_info(db, existing)
            definition = {"fields": core_fields(payload.model_dump(exclude={"id"})), "evidence": []}
            project = create_local_project(db, identity, definition, project_moment(db))
            return project_info(db, project)

    @app.get("/api/projects/{identity}")
    def read_project(identity: UUID):
        with transaction(engine, write=False) as db:
            return project_info(db, required(db, Project, str(identity)))

    @app.put("/api/projects/{identity}/pause")
    def pause_project(identity: UUID, payload: PauseInput):
        with transaction(engine) as db:
            project = required(db, Project, str(identity))
            if project.paused != payload.paused:
                project.paused = payload.paused
                touch_project(project, project_moment(db))
                study = db.get(ModelStudy, project.id)
                if study is not None:
                    if not project.paused and not study.deleted_at and not study.stopped and not study.reason:
                        resume_study(db, study, project, get_now())
                    else:
                        sync_task(db, study, project)
            return project_info(db, project)

    @app.post("/api/projects/{identity}/retry")
    def retry_project(identity: UUID):
        with transaction(engine) as db:
            project = required(db, Project, str(identity))
            if project.task.status in {"failed", "restore_pending"} and db.get(ModelStudy, project.id) is None:
                project.task.status = "queued"
                project.task.error = None
            return project_info(db, project)

    @app.get("/api/lessons/{identity}")
    def read_lesson(identity: UUID):
        with transaction(engine) as db:
            lesson = required(db, Lesson, str(identity))
            project = required(db, Project, lesson.project_id)
            course = lesson_course(project.sample, lesson.position, lesson.outline)
            tracker = LocationTracker(db, folder)
            body, course_issue = course_document(tracker, folder, lesson)
            revision = sha256(body.encode("utf-8")).hexdigest() if body is not None else None
            note = None
            if db.get(Note, lesson.id) is None or db.get(NoteLesson, (lesson.id, lesson.id)) is not None:
                try:
                    note_location = tracker.resolve(f"note-{lesson.id}", local_path(folder, "notes", lesson.id))
                    note = note_snapshot(note_location.path) if note_location.status == "ready" else None
                except (OSError, NoteFileError):
                    pass  # The note editor surfaces its own file error without blocking the lesson.
            published = db.get(CourseVersion, (lesson.id, lesson.version)) if lesson.ready and is_model_lesson(lesson.outline) else None
            return {
                "project": project_info(db, project), "version": lesson.version, "body": body,
                "courseIssue": course_issue or (lesson.outline or {}).get("preparationError"),
                "courseVersion": published.version if published else None,
                "courseMatchesVerification": bool(published and revision == published.revision),
                "readingRevision": revision,
                "readingPosition": position_view(db.get(LessonReadingPosition, lesson.id)) if revision else None,
                "source": {"title": course.source[0], "url": course.source[1]} if course.source else None,
                "note": note.content.decode("utf-8") if note is not None else "",
                "objectives": [{"key": item.key, "title": item.title} for item in course.objectives],
            }

    @app.post("/api/lessons/{identity}/practice/retry")
    def retry_practice(identity: UUID):
        with transaction(engine) as db:
            lesson = required(db, Lesson, str(identity))
            if not is_practice(lesson.outline):
                raise HTTPException(404, "找不到此实战任务。")
            if lesson.outline.get("preparationError"):
                lesson.outline = {key: value for key, value in lesson.outline.items() if key != "preparationError"}
            return {"ready": lesson.ready}

    @app.put("/api/lessons/{identity}/reading-position")
    def save_reading_position(identity: UUID, payload: ReadingPositionInput):
        with transaction(engine) as db:
            lesson = required(db, Lesson, str(identity))
            body, _ = course_document(LocationTracker(db, folder), folder, lesson)
            if body is None or sha256(body.encode("utf-8")).hexdigest() != payload.revision:
                raise HTTPException(409, "课程正文已变化。重新打开课程后可继续记录阅读位置。")
            if payload.point is None:
                saved = db.get(LessonReadingPosition, lesson.id)
                if saved is not None:
                    db.delete(saved)
                return {"readingPosition": None}
            saved = db.merge(LessonReadingPosition(
                lesson_id=lesson.id, revision=payload.revision, point=payload.point.model_dump(),
            ))
            return {"readingPosition": position_view(saved)}

    app.include_router(note_router(engine, folder, project_moment))
    mobile, dispatch_study = mobile_endpoints(engine, folder, demo_day, get_now, project_moment)
    phone_timer, dispatch_timer = timer_endpoints(engine, folder, demo_day, get_now)

    def dispatch(command: dict):
        if command.get("op") in ("timer-state", "timer-event"):
            return dispatch_timer(command)
        if command.get("op") in ("reminder-check", "reminder-action", "reminder-settings", "reminder-save"):
            return dispatch_reminders(command)
        return dispatch_study(command)

    def mobile_workspace():
        with transaction(engine, write=False) as db:
            return workspace_id(db)

    pairing = DesktopPairing(
        folder, pairing_credentials if pairing_credentials is not None else PairingCredentialStore(),
        mobile_workspace, dispatch, lambda: getattr(app.state, "restore_runtime", None),
    )
    mobile_push = MobilePush(pairing, engine, folder, demo_day, get_now)
    app.include_router(mobile)
    app.include_router(phone_timer)
    app.include_router(pairing_router(pairing))
    app.include_router(note_ai_router(engine, folder, session_token, get_now))
    app.include_router(note_checks_router(engine, folder, session_token, project_moment, get_now))

    @app.put("/api/lessons/{identity}/learned")
    def set_learned(identity: UUID, payload: LearnedInput):
        with transaction(engine) as db:
            lesson = required(db, Lesson, str(identity))
            project = required(db, Project, lesson.project_id)
            if not lesson.ready:
                raise HTTPException(409, "课程准备完成后可以标为已学完。")
            if payload.learned and lesson.learned_on is None:
                moment = project_moment(db)
                scheduled = not (is_practice(lesson.outline) and lesson.outline.get("required") is False)
                if scheduled:
                    freeze_plan_baseline(db, folder, moment.date())
                lesson.learned_on = moment.date()
                if scheduled:
                    finish_booking(db, lesson_task_id(lesson.id), lesson.learned_on)
                record_learning(project, moment)
                touch_project(project, moment)
                existing = {item.key for item in lesson.reviews}
                for objective in lesson_course(project.sample, lesson.position, lesson.outline).objectives:
                    if objective.key not in existing:
                        db.add(Review(lesson_id=lesson.id, key=objective.key, due_on=next_review_date(lesson.learned_on, 0)))
                schedule_consolidation(db, lesson, get_now())
                ensure_assessments(db, project, moment)
            if not payload.learned and lesson.learned_on is not None:
                lesson.learned_on = None
                touch_project(project, project_moment(db))
            if payload.learned and (plan := db.get(AssessmentPlan, lesson.id)):
                schedule_objectives(db, lesson, plan, get_now())
            return {"learnedOn": lesson.learned_on}

    @app.get("/api/lessons/{identity}/reviews/{key}")
    def review_detail(identity: UUID, key: str):
        with transaction(engine, write=False) as db:
            project, lesson, review, objective = find_review(db, identity, key)
            previous = db.get(Attempt, (lesson.id, key, review.round_number - 1))
            status = review_status(project.paused, lesson.ready and lesson.learned_on is not None, review.due_on, demo_day(db))
            return {
                **review_info(project.id, project.topic, review, objective, status), "round": review.round_number,
                "questions": [{"text": item.text, "options": item.options} for item in objective.questions(review.round_number)],
                "answers": review.answers, "hint": objective.hint if review.hinted else None,
                "latest": previous.result if previous else None,
            }

    @app.put("/api/lessons/{identity}/reviews/{key}/draft")
    def save_review_draft(identity: UUID, key: str, payload: AnswerInput):
        with transaction(engine) as db:
            project, lesson, review, _ = find_review(db, identity, key)
            require_current_round(project, lesson, review, payload.round, demo_day(db))
            review.answers = payload.answers
            return {"saved": True}

    @app.post("/api/lessons/{identity}/reviews/{key}/hint")
    def review_hint(identity: UUID, key: str, payload: RoundInput):
        with transaction(engine) as db:
            project, lesson, review, objective = find_review(db, identity, key)
            require_current_round(project, lesson, review, payload.round, demo_day(db))
            review.hinted = True
            return {"hint": objective.hint}

    @app.post("/api/lessons/{identity}/reviews/{key}/submit")
    def submit_review(identity: UUID, key: str, payload: SubmitInput):
        with transaction(engine) as db:
            return submit_local_review(db, folder, find_review(db, identity, key), payload.round, payload.answers,
                                       payload.action, demo_day(db), project_moment)

    library_index = LibraryIndex(engine, folder)
    app.include_router(goal_authoring_router(engine, folder, library_index, get_now, session_token))
    app.include_router(classification_router(engine, folder, library_index, get_now, session_token))
    app.include_router(recognition_router(recognition, session_token))
    app.include_router(library_router(engine, folder, demo_day, library_index, recognition))
    app.include_router(graph_router(engine, folder, library_index, demo_day, get_now, session_token))
    app.include_router(graph_recommendation_router(engine, folder, library_index, demo_day, get_now, session_token))
    app.include_router(voice_router(recognition))

    @app.post("/api/demo/next-day")
    def advance_day():
        with transaction(engine) as db:
            clock = db.get(DemoClock, 1)
            if clock is None:
                db.add(DemoClock(id=1, offset=1))
            else:
                clock.offset += 1
            db.flush()
            return {"today": demo_day(db)}

    mount_frontend(app)
    return app
