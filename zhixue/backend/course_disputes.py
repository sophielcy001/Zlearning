from hmac import compare_digest, digest
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field
from sqlalchemy import select, text

from .course_publication import release_course
from .course_review import Index, finding_sections
from .course_revision import active_revision
from .course_sources import HexDigest, sources_current
from .db import CourseDisposition, CourseFinding, CourseReviewPlan, GenerationStep, Lesson, ModelStudy, Project, transaction
from .file_locations import InvalidLocation, LocationConflict
from .library import FileTooLarge
from .model_study import review_recipient, sync_task, when_utc
from .model_study_input import Input
from .project_activity import study_moment, touch_project


class DisputeItem(Input):
    lessonId: UUID
    index: Index
    findingId: UUID


class DisputeBatch(Input):
    action: Literal["adopt", "recheck", "later"]
    items: list[DisputeItem] = Field(min_length=1, max_length=40)


class DisputeApply(DisputeBatch):
    confirmation: HexDigest


def pending_disputes_count(db, *, actionable_only: bool = False) -> int:
    return db.scalar(text("""
        SELECT count(*) FROM generation_steps s
        JOIN course_findings f ON f.id=s.finding_id
        JOIN model_studies m ON m.project_id=s.project_id
        JOIN projects p ON p.id=s.project_id
        JOIN json_each(f.sections) part
        LEFT JOIN course_dispositions d ON d.lesson_id=s.lesson_id AND d.section_index=json_extract(part.value, '$.index')
        WHERE s.result IS NOT NULL AND json_extract(part.value, '$.state')!='verified'
            AND d.adopted_finding IS NULL AND (d.deferred_finding IS NULL OR d.deferred_finding!=f.id)
            AND (:actionable_only = 0 OR (s.state NOT IN ('running', 'review_pending', 'reviewing')
                AND (s.revision_request IS NULL OR json_extract(s.revision_request, '$.status') IS NOT NULL)
                AND NOT s.publication_pending AND NOT p.paused AND NOT m.stopped AND m.deleted_at IS NULL))
    """), {"actionable_only": int(actionable_only)})


def sign_batch(secret: bytes, data: DisputeBatch) -> str:
    fields = [data.action, [item.model_dump(mode="json") for item in data.items]]
    body = json.dumps(["course-disputes", fields], sort_keys=True, separators=(",", ":"))
    return digest(secret, body.encode("utf-8"), "sha256").hex()


def active_item(db, item: DisputeItem):
    step = db.scalar(select(GenerationStep).where(GenerationStep.lesson_id == str(item.lessonId)))
    if step is None or step.result is None or step.finding_id != str(item.findingId):
        raise HTTPException(409, "争议内容已变化，请查看最新复核记录。")
    if active_revision(step):
        raise HTTPException(409, "请先处理当前课程修订稿。")
    finding = db.get(CourseFinding, step.finding_id)
    index = item.index
    if index >= len(finding.sections) or finding.sections[index]["state"] == "verified":
        raise HTTPException(409, "该片段当前没有未解决的争议。")
    return step, finding


def apply_dispute(db, folder: Path, step: GenerationStep, finding: CourseFinding,
                  item: DisputeItem, action: str, clock) -> None:
    lesson = db.get(Lesson, step.lesson_id)
    study = db.get(ModelStudy, step.project_id)
    project = db.get(Project, study.project_id)
    if action == "recheck":
        plan = db.get(CourseReviewPlan, project.id)
        if not plan or not review_recipient(db, step, study, plan):
            raise HTTPException(409, "请先在项目中确认复核模型与发送范围。")
        if not sources_current(db, folder, plan.sources):
            raise HTTPException(409, "复核资料已经变化，请先在项目中更新资料。")
        if study.stopped or project.paused:
            raise HTTPException(409, "请先继续生成任务或恢复学习项目。")
        if step.state not in {"reviewing", "review_pending"}:
            step.state = "review_pending"
            step.manual_review = True
            step.error = None
            step.available_at = None
        if study.reason in {"review_sources_changed", "review_configuration_changed"}:
            study.reason = None
    else:
        choice = db.get(CourseDisposition, (lesson.id, item.index))
        if choice is None:
            choice = CourseDisposition(lesson_id=lesson.id, section_index=item.index)
            db.add(choice)
        if action == "later":
            choice.deferred_finding = finding.id
        elif choice.adopted_finding is None:
            choice.adopted_finding = finding.id
            choice.adopted_at = when_utc(clock)
            release_course(db, folder, lesson, finding, step, clock, verified=False)
    touch_project(project, study_moment(clock))
    sync_task(db, study, project)


def course_disputes_router(engine, folder: Path, session_token: str, now) -> APIRouter:
    router = APIRouter(prefix="/api/disputes")
    secret = session_token.encode("utf-8")

    @router.get("")
    def list_disputes(project: UUID | None = None, lesson: UUID | None = None,
                      status: Literal["all", "pending", "adopted", "later"] = "all",
                      page: Annotated[int, Query(ge=1)] = 1):
        with transaction(engine, write=False) as db:
            query = select(GenerationStep, Lesson, Project, CourseFinding).join(
                Lesson, Lesson.id == GenerationStep.lesson_id,
            ).join(Project, Project.id == GenerationStep.project_id).join(
                CourseFinding, CourseFinding.id == GenerationStep.finding_id,
            ).where(GenerationStep.result.is_not(None))
            if project:
                query = query.where(Project.id == str(project))
            entries = []
            courses = {}
            for step, point, owner, finding in db.execute(query.order_by(
                CourseFinding.created_at.desc(), CourseFinding.lesson_id,
            )):
                sections = [section for section in finding_sections(db, finding, step) if section["state"] != "verified"]
                if sections:
                    courses[point.id] = {"id": point.id, "title": point.outline["title"], "topic": owner.topic}
                if lesson and point.id != str(lesson):
                    continue
                entries.extend({
                    **section, "lessonId": point.id, "lessonTitle": point.outline["title"],
                    "projectId": owner.id, "projectTopic": owner.topic, "findingId": finding.id,
                    "reviewer": finding.reviewer, "foundAt": finding.created_at.isoformat(),
                } for section in sections if status in {"all", section["disposition"]})
            offset = (page - 1) * 40
            return {"items": entries[offset:offset + 40], "total": len(entries), "hasMore": len(entries) > offset + 40,
                    "courses": list(courses.values())}

    @router.post("/preview")
    def preview(data: DisputeBatch):
        with transaction(engine, write=False) as db:
            items = []
            for item in data.items:
                target = item.model_dump(mode="json")
                try:
                    step, finding = active_item(db, item)
                    title = step.result["sections"][item.index]["title"]
                    items.append({**target, "status": "ready", "title": title})
                except HTTPException as error:
                    items.append({**target, "status": "unavailable", "error": error.detail})
            return {"items": items, "confirmation": sign_batch(secret, data)}

    @router.post("/apply")
    def apply(data: DisputeApply):
        if not compare_digest(data.confirmation, sign_batch(secret, data)):
            raise HTTPException(409, "处理范围已经修改，请重新预览。")
        with transaction(engine) as db:
            items = []
            clock = now()
            for item in data.items:
                target = item.model_dump(mode="json")
                try:
                    with db.begin_nested():
                        step, finding = active_item(db, item)
                        apply_dispute(db, folder, step, finding, item, data.action, clock)
                    items.append({**target, "status": "applied"})
                except HTTPException as error:
                    items.append({**target, "status": "unavailable", "error": error.detail})
                except (OSError, InvalidLocation, FileTooLarge, LocationConflict):
                    items.append({**target, "status": "unavailable", "error": "课程文件无法安全保存，请检查知识库。"})
            return {"items": items}

    return router
