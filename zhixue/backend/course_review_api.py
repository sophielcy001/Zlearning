from hmac import compare_digest, digest
import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import select

from .course_review import finding_sections, independent_reviewer
from .course_revision import active_revision
from .course_source_updates import affected_sections, cited_files, source_update_detail
from .course_sources import HexDigest, SourceLocator, locate_sources, observe_sources, read_source_revisions, sources_current
from .db import CourseFinding, CourseReviewPlan, CourseVersion, GenerationStep, Lesson, ModelStudy, Project, transaction
from .generation_api import require_active, target
from .library_index import LibraryIndex
from .model_failover import FAILOVER_FAILURES
from .model_study import ERRORS, step_rows, sync_task
from .model_study_input import Input, connection_view


class ReviewSources(Input):
    sources: list[SourceLocator] = Field(min_length=1, max_length=6)
    allowBackup: bool = False


class AuthorizeReview(ReviewSources):
    confirmation: HexDigest


def sign_review(secret: bytes, project_id: str, current: str | None, reviewer: dict, sources: list[dict],
                backup: dict | None = None) -> str:
    values = ["independent-course-review", project_id, current, reviewer, sources]
    if backup is not None:
        values.append(backup)
    fields = json.dumps(values,
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return digest(secret, fields.encode("utf-8"), "sha256").hex()


def review_plan_view(db, project: Project, study: ModelStudy) -> dict:
    plan = db.get(CourseReviewPlan, project.id)
    recipient = independent_reviewer(db, study)
    reviewer = connection_view(recipient) if recipient else None
    return {
        "reviewer": reviewer, "authorized": bool(plan and plan.reviewer == reviewer),
        "sources": plan.sources if plan else [],
        "backup": plan.backup["connection"] if plan and plan.backup else None,
    }


def course_review_router(engine, folder: Path, session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api")
    secret = session_token.encode("utf-8")
    index = LibraryIndex(engine, folder)

    def selection(db, identity: UUID, data: ReviewSources):
        project, study = target(db, identity)
        require_active(study)
        model = independent_reviewer(db, study)
        if model is None:
            raise HTTPException(409, "请在管理中心选择与主模型独立的复核连接。")
        alternate = independent_reviewer(db, study, "reviewBackup") if data.allowBackup else None
        if data.allowBackup and (alternate is None or alternate.id == model.id):
            raise HTTPException(409, "请配置另一条与主模型独立的复核备用连接。")
        backup = connection_view(alternate) if alternate else None
        return project, study, connection_view(model), backup, locate_sources(db, folder, data.sources)

    @router.get("/projects/{identity}/course-review")
    def status(identity: UUID):
        with transaction(engine, write=False) as db:
            project, study = target(db, identity)
            return review_plan_view(db, project, study)

    @router.post("/projects/{identity}/course-review/preview")
    def preview(identity: UUID, data: ReviewSources):
        for file_id in {str(locator.fileId) for locator in data.sources}:
            index.refresh(identity=file_id)
        with transaction(engine) as db:
            project, _, reviewer, backup, sources = selection(db, identity, data)
            existing = db.get(CourseReviewPlan, project.id)
            return {
                "reviewer": reviewer, "backup": backup, "sources": sources,
                "confirmation": sign_review(secret, project.id, existing.id if existing else None, reviewer, sources, backup),
            }

    @router.post("/projects/{identity}/course-review/authorize")
    def authorize(identity: UUID, data: AuthorizeReview):
        with transaction(engine) as db:
            project, study, reviewer, backup, sources = selection(db, identity, data)
            plan = db.get(CourseReviewPlan, project.id)
            token = sign_review(secret, project.id, plan.id if plan else None, reviewer, sources, backup)
            if not compare_digest(token, data.confirmation):
                raise HTTPException(409, "资料、模型或复核设置已变更，请重新预览。")
            db.merge(CourseReviewPlan(project_id=project.id, id=str(uuid4()), reviewer=reviewer, sources=sources,
                                      backup={"connection": backup} if backup else None))
            steps = step_rows(db, project.id)
            review_failure = (study.reason in FAILOVER_FAILURES
                              and any(step.state == "review_failed" and step.error == study.reason for step in steps)
                              and not any(step.state == "failed" for step in steps))
            if study.reason in {"review_configuration_changed", "review_sources_changed"} or review_failure:
                study.reason = None
            for step in steps:
                if active_revision(step):
                    continue
                if step.state in {"awaiting_review", "review_failed"}:
                    step.state = "review_pending"
                    step.error = None
                    step.available_at = None
            sync_task(db, study, project)
            return review_plan_view(db, project, study)

    @router.post("/projects/{identity}/course-review/revoke")
    def revoke(identity: UUID):
        with transaction(engine) as db:
            project, study = target(db, identity)
            require_active(study)
            if plan := db.get(CourseReviewPlan, project.id):
                db.delete(plan)
            if study.reason in {"review_configuration_changed", "review_sources_changed"}:
                study.reason = None
            for step in step_rows(db, project.id):
                if active_revision(step):
                    continue
                if step.state in {"review_pending", "review_failed"}:
                    step.state = "awaiting_review"
                    step.error = None
                    step.available_at = None
                    step.manual_review = False
            sync_task(db, study, project)
            return review_plan_view(db, project, study)

    @router.get("/lessons/{identity}/verification")
    def verification(identity: UUID, publishedVersion: int | None = None):
        lesson_id = str(identity)
        with observe_sources(engine) as db:
            lesson = db.get(Lesson, lesson_id)
            if lesson is None:
                raise HTTPException(404, "找不到该知识点。")
            step = db.scalar(select(GenerationStep).where(GenerationStep.lesson_id == lesson_id))
            finding = db.get(CourseFinding, step.finding_id) if step and step.finding_id else None
            current = db.get(CourseVersion, (lesson.id, publishedVersion or lesson.version)) if lesson.ready else None
            return {
                "state": step.state if step else "local", "error": ERRORS.get(step.error) if step else None,
                "findingId": finding.id if finding else None, "reviewer": finding.reviewer if finding else None,
                "sections": finding_sections(db, finding, step) if finding else [],
                "published": current.sections if current else [],
                "publishedVersion": current.version if current else None,
                "sourcesChanged": bool(current and not sources_current(db, folder, [
                    ref for section in current.sections for ref in section["citations"]
                ])),
            }

    def current_impact(db, identity):
        lesson = db.get(Lesson, str(identity))
        if lesson is None:
            raise HTTPException(404, "找不到该知识点。")
        course = db.get(CourseVersion, (lesson.id, lesson.version)) if lesson.ready else None
        sections = course.sections if course else []
        revisions = read_source_revisions(db, folder, cited_files(sections))
        return course, affected_sections(db, sections, revisions), revisions

    @router.get("/lessons/{identity}/source-updates")
    def source_updates(identity: UUID):
        with observe_sources(engine) as db:
            course, impacted, revisions = current_impact(db, identity)
            changed_files = cited_files(impacted)
            if not changed_files:
                return source_update_detail(db, course, impacted, revisions)
        for file_id in sorted(changed_files):
            index.refresh(identity=file_id)
        with observe_sources(engine) as db:
            return source_update_detail(db, *current_impact(db, identity))

    @router.get("/lessons/{identity}/versions")
    def versions(identity: UUID):
        with transaction(engine, write=False) as db:
            rows = db.scalars(select(CourseVersion).where(CourseVersion.lesson_id == str(identity)).order_by(CourseVersion.version.desc()))
            return {"items": [{"version": row.version, "publishedAt": row.published_at.isoformat(),
                               "revision": row.revision} for row in rows]}

    @router.get("/lessons/{identity}/versions/{number}")
    def read_version(identity: UUID, number: int):
        with transaction(engine, write=False) as db:
            saved = db.get(CourseVersion, (str(identity), number))
            if saved is None:
                raise HTTPException(404, "找不到这个课程版本。")
            return {"version": saved.version, "revision": saved.revision, "sections": saved.sections,
                    "markdown": saved.markdown}

    return router
