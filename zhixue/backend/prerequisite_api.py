from hmac import compare_digest, digest
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, StringConstraints

from .assessment import recipient_models
from .course_review import same_model
from .course_sources import HexDigest, SourceLocator, locate_sources
from .db import Lesson, PrerequisiteTask, Project, transaction
from .library_index import LibraryIndex
from .model_failover import BackupChoices, backup_options
from .model_study import when_utc
from .model_study_input import CallLimit, connection_view
from .prerequisite import (
    PREREQUISITE_ERRORS, authorization_issue, lookup, requested_topic, task_view,
)


TopicText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class PreviewInput(BackupChoices):
    topic: TopicText | None = None
    targetLessonId: UUID | None = None
    sources: list[SourceLocator] = Field(min_length=1, max_length=6)
    maxCalls: CallLimit | None = None
    resumeJobId: UUID | None = None


class GenerateInput(PreviewInput):
    confirmation: HexDigest


def origin(db, identity: UUID):
    lesson = db.get(Lesson, str(identity))
    if lesson is None:
        raise HTTPException(404, "找不到原课程。")
    return lesson, db.get(Project, lesson.project_id)


def related_task(db, project: Project, identity: UUID) -> PrerequisiteTask:
    task = db.get(PrerequisiteTask, str(identity))
    if task is None or db.get(Lesson, task.lesson_id).project_id != project.id:
        raise HTTPException(404, "找不到这次前置讲解。")
    return task


def prerequisite_router(engine, folder: Path, session_token: str, now) -> APIRouter:
    router = APIRouter(prefix="/api/lessons/{identity}/prerequisites")
    secret = session_token.encode("utf-8")
    index = LibraryIndex(engine, folder)

    def scope_for(db, lesson, project, payload: PreviewInput):
        topic = requested_topic(db, lesson, project, payload.topic, payload.targetLessonId)
        main, reviewer = recipient_models(db)
        if main is None or reviewer is None:
            raise HTTPException(409, "请在管理中心设置可用的主模型和独立复核模型。")
        resume = related_task(db, project, payload.resumeJobId) if payload.resumeJobId else None
        if resume and (resume.topic_key != topic.key or resume.state not in {"blocked", "stopped"} or resume.active_call):
            raise HTTPException(409, "此讲解的状态已变化，请刷新后继续。")
        review = connection_view(reviewer)
        backups = backup_options(db, main, reviewer, allow_main=payload.allowMainBackup,
                                 allow_review=payload.allowReviewBackup)
        review_backup = backups.get("backups", {}).get("review")
        if (resume and resume.author and same_model(resume.author, review)
                and (review_backup is None or same_model(resume.author, review_backup))):
            raise HTTPException(409, "请选择与草稿作者独立的复核模型。")
        fields = {"topic": topic.name, "targetLessonId": topic.target.id if topic.target else None,
                  "main": connection_view(main), "review": review,
                  "sources": locate_sources(db, folder, payload.sources), "maxCalls": payload.maxCalls, **backups}
        raw = json.dumps(["prerequisite-learning", lesson.id, resume.id if resume else None, fields],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return topic, resume, fields, digest(secret, raw.encode("utf-8"), "sha256").hex()

    @router.get("")
    def status(identity: UUID, topic: Annotated[str | None, Query(max_length=160)] = None,
               targetLessonId: UUID | None = None):
        with transaction(engine) as db:
            lesson, project = origin(db, identity)
            selected = requested_topic(db, lesson, project, topic, targetLessonId)
            return lookup(db, folder, project, selected)

    @router.post("/preview")
    def preview(identity: UUID, payload: PreviewInput):
        for file in {str(item.fileId) for item in payload.sources}:
            index.refresh(identity=file)
        with transaction(engine) as db:
            lesson, project = origin(db, identity)
            _, _, fields, token = scope_for(db, lesson, project, payload)
            return {**fields, "confirmation": token}

    @router.post("/generate")
    def generate(identity: UUID, payload: GenerateInput):
        with transaction(engine) as db:
            lesson, project = origin(db, identity)
            topic, resume, fields, token = scope_for(db, lesson, project, payload)
            if not compare_digest(token, payload.confirmation):
                raise HTTPException(409, "主题、资料或接收模型已变化，请重新预览发送范围。")
            result = lookup(db, folder, project, topic)
            if result["kind"] in {"course", "explanation"} or (result["kind"] == "task" and resume is None):
                return result
            scope = {name: fields[name] for name in ("main", "review", "sources", "backups") if name in fields}
            if resume:
                if resume.scope["sources"] != scope["sources"]:
                    resume.findings, resume.reviewer = None, None
                    resume.phase = "review" if resume.draft else "draft"
                resume.scope, resume.max_calls = scope, payload.maxCalls
                resume.state, resume.reason, resume.available_at = "queued", None, None
            else:
                resume = PrerequisiteTask(
                    id=str(uuid4()), lesson_id=lesson.id, target_lesson_id=fields["targetLessonId"],
                    topic=topic.name, topic_key=topic.key, scope=scope, max_calls=payload.maxCalls,
                    state="queued", phase="draft", created_at=when_utc(now()),
                )
                db.add(resume)
            return {"topic": topic.name, "targetLessonId": fields["targetLessonId"],
                    "kind": "task", "job": task_view(db, project, resume)}

    @router.post("/jobs/{task_id}/retry")
    def retry(identity: UUID, task_id: UUID):
        with transaction(engine) as db:
            _, project = origin(db, identity)
            task = related_task(db, project, task_id)
            if task.state in {"blocked", "stopped"} and not task.active_call:
                if issue := authorization_issue(db, folder, task):
                    raise HTTPException(409, PREREQUISITE_ERRORS[issue])
                task.state, task.reason, task.available_at = "queued", None, None
            return task_view(db, project, task)

    @router.post("/jobs/{task_id}/stop")
    def stop(identity: UUID, task_id: UUID):
        with transaction(engine) as db:
            _, project = origin(db, identity)
            task = related_task(db, project, task_id)
            if task.state in ("queued", "running", "blocked"):
                task.state, task.reason, task.available_at, task.daily_blocked_on = "stopped", None, None, None
            return task_view(db, project, task)

    return router
