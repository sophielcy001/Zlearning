from hmac import compare_digest, digest
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import select

from .content import is_practice
from .db import CourseFinding, CourseReviewPlan, CourseVersion, GenerationSettings, GenerationStep, ModelStudy, Project, transaction
from .model_costs import MoneyLimit
from .model_study import generation_amount_issue, resume_study, settings_view, step_rows, study_view, sync_task, update_window, when_utc
from .model_study_input import (
    CallLimit, GenerationConsent, Input, MAX_WORKERS, generation_goal_fields, preview_consent, verify_consent,
)
from .money_budget import MONEY_ERRORS, set_task_limit


class Focus(Input):
    lessonId: UUID


class Control(Input):
    action: Literal["stop", "resume"]
    maxCalls: CallLimit | None = None
    maxCost: MoneyLimit | None = None
    useDefaultCost: Literal[True] | None = None


class ReauthorizePreview(Input):
    maxCalls: CallLimit | None = None
    maxCost: MoneyLimit | None = None
    allowBackup: bool = False


class SettingsInput(Input):
    dailyLimit: CallLimit | None
    concurrency: Annotated[int, Field(strict=True, ge=1, le=MAX_WORKERS)]
    autoContinueDailyBudget: Annotated[bool, Field(strict=True)] | None = None
    dailyCostLimit: MoneyLimit | None = None
    defaultTaskCostLimit: MoneyLimit | None = None


class DeleteInput(Input):
    confirmation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def target(db, identity: UUID) -> tuple[Project, ModelStudy]:
    study = db.get(ModelStudy, str(identity))
    if study is None:
        raise HTTPException(404, "找不到此学习项目的模型任务。")
    return db.get(Project, study.project_id), study


def task_target(db, identity: UUID) -> tuple[Project, ModelStudy | None]:
    project = db.get(Project, str(identity))
    if project is None:
        raise HTTPException(404, "找不到此学习项目的任务。")
    return project, db.get(ModelStudy, project.id)


def require_active(study: ModelStudy) -> None:
    if study.deleted_at is not None:
        raise HTTPException(409, "此生成任务已删除。")


def control_local(project: Project, data: Control) -> dict:
    task = project.task
    if task.status == "deleted":
        raise HTTPException(409, "此生成任务已删除。")
    if data.model_fields_set & {"maxCalls", "maxCost", "useDefaultCost"}:
        raise HTTPException(422, "本地课程准备无需设置模型额度。")
    if data.action == "stop" and task.status in ("queued", "failed", "restore_pending"):
        task.status = "stopped"
    elif data.action == "resume" and task.status in ("stopped", "failed", "restore_pending"):
        task.status = "queued"
        task.error = None
    return {"state": task.status, "error": task.error}


def local_deletion_view(secret: bytes, project: Project) -> dict:
    lessons = sorted((lesson.id, lesson.ready) for lesson in project.lessons if not is_practice(lesson.outline))
    scope = json.dumps(["delete-local-generation", project.id, project.task.status, lessons]).encode("utf-8")
    return {
        "drafts": 0, "pending": sum(not ready for _, ready in lessons), "running": 0,
        "routeNodes": len(lessons), "confirmation": digest(secret, scope, "sha256").hex(),
    }


def deletion_view(secret: bytes, project: Project, steps: list[GenerationStep], plan: CourseReviewPlan | None) -> dict:
    items = sorted((step.id, step.state, step.active_call, step.finding_id, step.publication_pending, step.manual_review,
             step.kind == "draft" and step.result is not None, step.revision_request) for step in steps)
    route = sorted(lesson.id for lesson in project.lessons)
    scope = json.dumps(["delete-generation", project.id, route, plan.id if plan else None, items]).encode("utf-8")
    return {
        "drafts": sum(step.kind == "draft" and step.result is not None for step in steps),
        "pending": sum(step.publication_pending or step.state in ("pending", "failed", "review_pending", "review_failed") for step in steps),
        "running": sum(step.active_call is not None for step in steps),
        "routeNodes": len(route), "confirmation": digest(secret, scope, "sha256").hex(),
    }


def generation_router(engine, session_token: str, now) -> APIRouter:
    router = APIRouter(prefix="/api")
    secret = session_token.encode("utf-8")

    @router.get("/generation/settings")
    def settings():
        with transaction(engine, write=False) as db:
            return settings_view(db)

    @router.put("/generation/settings")
    def save_settings(data: SettingsInput):
        with transaction(engine) as db:
            saved = db.get(GenerationSettings, 1)
            if saved is None:
                saved = GenerationSettings(id=1, auto_continue_daily_budget=False)
                db.add(saved)
            saved.daily_limit, saved.concurrency = data.dailyLimit, data.concurrency
            if data.autoContinueDailyBudget is not None:
                saved.auto_continue_daily_budget = data.autoContinueDailyBudget
            if "dailyCostLimit" in data.model_fields_set:
                saved.daily_cost_limit = data.dailyCostLimit.model_dump() if data.dailyCostLimit else None
            if "defaultTaskCostLimit" in data.model_fields_set:
                saved.default_task_cost_limit = data.defaultTaskCostLimit.model_dump() if data.defaultTaskCostLimit else None
            db.flush()
            return settings_view(db)

    @router.get("/projects/{identity}/generation")
    def read(identity: UUID):
        with transaction(engine, write=False) as db:
            project, study = target(db, identity)
            return study_view(db, study, project)

    @router.put("/projects/{identity}/generation/focus")
    def focus(identity: UUID, data: Focus):
        with transaction(engine) as db:
            project, study = target(db, identity)
            require_active(study)
            lesson_id = str(data.lessonId)
            lesson = next((item for item in project.lessons if item.id == lesson_id), None)
            if lesson is None:
                raise HTTPException(404, "此知识点不在当前学习路线中。")
            if is_practice(lesson.outline):
                raise HTTPException(422, "实战任务由本机准备。")
            study.focus_id = lesson_id
            update_window(db, study, project.lessons)
            sync_task(db, study, project)
            return study_view(db, study, project)

    @router.post("/projects/{identity}/generation/control")
    def control(identity: UUID, data: Control):
        with transaction(engine) as db:
            project, study = task_target(db, identity)
            if study is None:
                return control_local(project, data)
            require_active(study)
            if data.action == "stop":
                study.stopped = True
                sync_task(db, study, project)
            else:
                if project.paused:
                    raise HTTPException(409, "请先恢复学习项目。")
                if "maxCalls" in data.model_fields_set:
                    study.max_calls = data.maxCalls
                if data.useDefaultCost and "maxCost" in data.model_fields_set:
                    raise HTTPException(422, "请选择任务上限或使用默认上限。")
                if data.useDefaultCost or "maxCost" in data.model_fields_set:
                    set_task_limit(db, "generation", project.id, data.maxCost, bool(data.useDefaultCost))
                clock = now()
                if study.reason in MONEY_ERRORS and (issue := generation_amount_issue(db, study, project, clock)):
                    study.reason = issue
                    sync_task(db, study, project)
                else:
                    resume_study(db, study, project, clock)
            return study_view(db, study, project)

    @router.get("/projects/{identity}/generation/drafts/{lesson}")
    def draft(identity: UUID, lesson: UUID):
        with transaction(engine, write=False) as db:
            project, _ = target(db, identity)
            step = db.scalar(select(GenerationStep).where(GenerationStep.project_id == project.id, GenerationStep.lesson_id == str(lesson)))
            if step is None or step.result is None:
                raise HTTPException(404, "该知识点的待核验草稿暂未生成。")
            return step.result

    @router.post("/projects/{identity}/generation/authorize/preview")
    def reauthorize_preview(identity: UUID, data: ReauthorizePreview):
        with transaction(engine, write=False) as db:
            project, study = target(db, identity)
            require_active(study)
            return preview_consent(db, project.id, generation_goal_fields(db, study), data.maxCalls, secret,
                                   allow_backup=data.allowBackup, max_cost=data.maxCost)

    @router.post("/projects/{identity}/generation/authorize")
    def reauthorize(identity: UUID, data: GenerationConsent):
        with transaction(engine) as db:
            project, study = target(db, identity)
            require_active(study)
            if project.paused:
                raise HTTPException(409, "请先恢复学习项目。")
            inputs = generation_goal_fields(db, study)
            connection, backup = verify_consent(db, project.id, inputs, data, secret)
            study.goal_fields = inputs
            study.connection_id = connection.id
            study.connection_revision = connection.revision
            study.backup = backup
            study.max_calls = data.maxCalls
            if data.maxCost is not None:
                set_task_limit(db, "generation", project.id, data.maxCost, False)
            resume_study(db, study, project, now())
            return study_view(db, study, project)

    @router.post("/projects/{identity}/generation/delete-preview")
    def delete_preview(identity: UUID):
        with transaction(engine, write=False) as db:
            project, study = task_target(db, identity)
            if study is None:
                if project.task.status == "deleted":
                    raise HTTPException(409, "此生成任务已删除。")
                return local_deletion_view(secret, project)
            require_active(study)
            return deletion_view(secret, project, step_rows(db, project.id), db.get(CourseReviewPlan, project.id))

    @router.post("/projects/{identity}/generation/delete")
    def delete(identity: UUID, data: DeleteInput):
        with transaction(engine) as db:
            project, study = task_target(db, identity)
            if study is None:
                task = project.task
                if task.status != "deleted":
                    preview = local_deletion_view(secret, project)
                    if not compare_digest(data.confirmation, preview["confirmation"]):
                        raise HTTPException(409, "任务范围已变化，请重新预览。")
                    task.status, task.error = "deleted", None
                return {"state": task.status, "error": task.error}
            if study.deleted_at is not None:
                return study_view(db, study, project)
            steps = step_rows(db, project.id)
            plan = db.get(CourseReviewPlan, project.id)
            if not compare_digest(data.confirmation, deletion_view(secret, project, steps, plan)["confirmation"]):
                raise HTTPException(409, "任务范围已变化，请重新预览。")
            study.deleted_at = when_utc(now())
            study.reason = None
            if plan is not None:
                db.delete(plan)
            retained = select(CourseVersion.finding_id)
            for finding in db.scalars(select(CourseFinding).join(
                GenerationStep, GenerationStep.lesson_id == CourseFinding.lesson_id,
            ).where(GenerationStep.project_id == project.id, CourseFinding.id.not_in(retained))):
                db.delete(finding)
            for step in steps:
                step.state = "discarded"
                step.priority = None
                step.result = None
                step.error = None
                step.finding_id = None
                step.publication_pending = False
                step.manual_review = False
                if step.revision_request and step.revision_request.get("status") != "accepted":
                    step.revision_request = None
            sync_task(db, study, project)
            return study_view(db, study, project)

    return router
