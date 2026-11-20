from hmac import compare_digest
from pathlib import PurePath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import Field, StringConstraints, field_validator

from .db import Project, StageAssessment, StageAssessmentAttempt, StageReview, StageReviewCall, transaction
from .documents import MAX_DOCUMENT_BYTES, extract
from .model_study import when_utc
from .model_study_input import Input, connection_view
from .stage_assessments import (
    apply_stage_result, assessment_summary, assessment_view, attempt_view, completed_phases, ensure_assessments, grade_quiz,
    model_review_states, review_outcome, stage_quiz,
)
from .stage_review import (
    AuthorizeReview, ReviewPreview, confirm_scope, make_scope, scope_locators, stage_backup, stage_recipient, stop_review,
)


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)]
Quote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
EvidenceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
MAX_EVIDENCE_BYTES = 250_000


class SkipInput(Input):
    skipped: bool
    expectedRevision: int = Field(ge=1)


class AttemptInput(Input):
    id: UUID
    expectedAttempt: int = Field(ge=0)


class QuizInput(AttemptInput):
    kind: Literal["quiz"]
    answers: list[Annotated[int, Field(strict=True, ge=0, le=2)]] = Field(min_length=1, max_length=100)


class Evidence(Input):
    name: EvidenceName
    kind: Literal["code", "project", "report", "response"]
    text: str = Field(min_length=1, max_length=MAX_EVIDENCE_BYTES)
    coverage: Text | None = None

    @field_validator("text")
    @classmethod
    def readable_text(cls, value: str) -> str:
        if "\x00" in value or not value.removeprefix("\ufeff").strip():
            raise ValueError("证据需要包含可读文字。")
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise ValueError("证据文字编码无效。") from error
        return value.replace("\r\n", "\n").replace("\r", "\n")


class EvidenceInput(AttemptInput):
    kind: Literal["evidence"]
    evidence: list[Evidence] = Field(min_length=1, max_length=8)


SubmissionInput = Annotated[QuizInput | EvidenceInput, Field(discriminator="kind")]


class Citation(Input):
    evidenceIndex: int = Field(strict=True, ge=0, le=7)
    quote: Quote


class Finding(Input):
    lessonId: UUID
    verdict: Literal["met", "needs_work", "insufficient"]
    note: Text
    citation: Citation | None = None


class ReviewInput(Input):
    findings: list[Finding] = Field(min_length=1, max_length=100)


def require_project(db, project_id: UUID) -> Project:
    project = db.get(Project, str(project_id))
    if project is None:
        raise HTTPException(404, "找不到学习项目。")
    return project


def require_stage(db, project_id: UUID, stage_id: UUID) -> tuple[StageAssessment, bool]:
    project = require_project(db, project_id)
    row = db.get(StageAssessment, str(stage_id))
    if row is None or row.project_id != project.id:
        raise HTTPException(404, "找不到阶段测评。")
    _, completed = completed_phases(db, project)
    return row, row.phase_key in completed


def require_attempt(db, row: StageAssessment, attempt_id: UUID) -> StageAssessmentAttempt:
    attempt = db.get(StageAssessmentAttempt, str(attempt_id))
    if attempt is None or attempt.assessment_id != row.id:
        raise HTTPException(404, "找不到这次测评记录。")
    return attempt


def bound_findings(row: StageAssessment, attempt: StageAssessmentAttempt, data: ReviewInput) -> list[dict]:
    by_lesson = {str(item.lessonId): item for item in data.findings}
    lesson_ids = [item["lessonId"] for item in row.criteria]
    if len(by_lesson) != len(data.findings) or by_lesson.keys() != set(lesson_ids):
        raise HTTPException(422, "请逐项评价阶段要求。")
    evidence = attempt.submission["evidence"]
    for finding in by_lesson.values():
        citation = finding.citation
        if citation is None:
            if finding.verdict != "insufficient":
                raise HTTPException(422, "请为每个已作判断的要求引用提交的原文。")
        elif citation.evidenceIndex >= len(evidence) or citation.quote not in evidence[citation.evidenceIndex]["text"]:
            raise HTTPException(422, "引文须与本次提交的证据原文一致。")
    return [by_lesson[identity].model_dump(mode="json") for identity in lesson_ids]


def stage_assessment_router(engine, project_moment, session_token: str, now) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/stage-assessments")
    secret = session_token.encode("utf-8")

    @router.get("")
    def list_assessments(project_id: UUID):
        with transaction(engine) as db:
            project = require_project(db, project_id)
            rows, eligible, planned = ensure_assessments(db, project, project_moment(db))
            states = model_review_states(db, project.id)
            return {"items": [
                {**assessment_summary(row, row.phase_key in eligible, row.phase_key in planned),
                 "modelReviewState": states.get(row.id)}
                for row in rows
            ]}

    @router.get("/{stage_id}")
    def get_assessment(project_id: UUID, stage_id: UUID):
        with transaction(engine, write=False) as db:
            row, eligible = require_stage(db, project_id, stage_id)
            return assessment_view(db, row, eligible)

    @router.put("/{stage_id}/skip")
    def set_skipped(project_id: UUID, stage_id: UUID, data: SkipInput):
        with transaction(engine) as db:
            row, eligible = require_stage(db, project_id, stage_id)
            if bool(row.skipped_at) == data.skipped:
                return assessment_view(db, row, eligible)
            if not eligible or row.revision != data.expectedRevision or row.status == "passed":
                raise HTTPException(409, "阶段状态已变化，请重新打开测评。")
            row.skipped_at = project_moment(db) if data.skipped else None
            row.revision += 1
            return assessment_view(db, row, eligible)

    @router.post("/{stage_id}/evidence-preview")
    async def preview_evidence(project_id: UUID, stage_id: UUID, request: Request,
                               name: str = Query(min_length=1, max_length=240)):
        def check_access():
            with transaction(engine, write=False) as db:
                row, eligible = require_stage(db, project_id, stage_id)
                if not eligible or row.skipped_at:
                    raise HTTPException(409, "阶段状态已变化，请重新打开测评。")

        name = name.strip()
        if not name or any(ord(char) < 32 or char in "/\\:" for char in name):
            raise HTTPException(422, "请选择一个有效的证据文件。")
        await run_in_threadpool(check_access)
        suffix = PurePath(name).suffix.casefold()[1:]
        document = suffix in {"pdf", "docx"}
        limit = MAX_DOCUMENT_BYTES if document else MAX_EVIDENCE_BYTES
        source = bytearray()
        async for chunk in request.stream():
            if len(source) + len(chunk) > limit:
                raise HTTPException(413, "证据文件过大，请选取相关部分后重试。")
            source.extend(chunk)
        parsed = await run_in_threadpool(extract, bytes(source), suffix if document else "txt")
        if parsed.status in {"failed", "empty"} or "\x00" in parsed.content:
            raise HTTPException(422, "没有可审查的文字。请选择 UTF-8 文本、可提取文字的 PDF 或 DOCX。")
        if len(parsed.content.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise HTTPException(413, "提取的证据文字超过 250 KB，请选取相关部分后重试。")
        coverage = parsed.message if document else None
        if suffix == "pdf" and coverage is None:
            coverage = f"已提取 PDF 的 {parsed.page_count} 页可读文字。"
        return {"name": name, "text": parsed.content, "coverage": coverage}

    @router.get("/{stage_id}/attempts/{attempt_id}")
    def get_attempt(project_id: UUID, stage_id: UUID, attempt_id: UUID):
        with transaction(engine, write=False) as db:
            row, _ = require_stage(db, project_id, stage_id)
            return attempt_view(db, require_attempt(db, row, attempt_id))

    @router.post("/{stage_id}/attempts", status_code=201)
    def submit_assessment(project_id: UUID, stage_id: UUID, data: SubmissionInput):
        submission = data.model_dump(mode="json", exclude={"id", "expectedAttempt", "kind"})
        if isinstance(data, EvidenceInput) and sum(len(item.text.encode("utf-8")) for item in data.evidence) > MAX_EVIDENCE_BYTES:
            raise HTTPException(422, "一次可提交的证据文字合计最多为 250 KB。")
        with transaction(engine) as db:
            row, eligible = require_stage(db, project_id, stage_id)
            if prior := db.get(StageAssessmentAttempt, str(data.id)):
                if (prior.assessment_id != row.id or prior.kind != data.kind or prior.submission != submission
                        or prior.number != data.expectedAttempt + 1):
                    raise HTTPException(409, "这次提交编号已有不同内容。")
                return assessment_view(db, row, eligible)
            if not eligible or row.skipped_at or row.attempt_count != data.expectedAttempt:
                raise HTTPException(409, "阶段状态已变化，请重新打开测评。")
            if row.status == "awaiting_review":
                raise HTTPException(409, "请先完成上一次成果评审。")
            result = None
            if isinstance(data, QuizInput):
                quiz = stage_quiz(row)
                if not quiz or len(quiz) != len(data.answers):
                    raise HTTPException(422, "请完成本次测评的全部题目。")
                result = grade_quiz(quiz, data.answers)
            outcome = review_outcome(result["findings"]) if result else "awaiting_review"
            row.attempt_count += 1
            row.revision += 1
            row.status = outcome
            db.add(StageAssessmentAttempt(id=str(data.id), assessment_id=row.id, number=row.attempt_count,
                                          kind=data.kind, submitted_at=project_moment(db), submission=submission,
                                          outcome=outcome, result=result))
            return assessment_view(db, row, eligible)

    @router.put("/{stage_id}/attempts/{attempt_id}/review")
    def review_evidence(project_id: UUID, stage_id: UUID, attempt_id: UUID, data: ReviewInput):
        with transaction(engine) as db:
            row, eligible = require_stage(db, project_id, stage_id)
            attempt = require_attempt(db, row, attempt_id)
            if attempt.kind != "evidence":
                raise HTTPException(409, "该次测评已有答题结果。")
            findings = bound_findings(row, attempt, data)
            if attempt.result is not None:
                if attempt.result["findings"] != findings:
                    raise HTTPException(409, "本次已保存评审结果，可重新测评。")
                return assessment_view(db, row, eligible)
            if not eligible or row.skipped_at or attempt.number != row.attempt_count:
                raise HTTPException(409, "阶段状态已变化，请重新打开测评。")
            if job := db.get(StageReview, attempt.id):
                stop_review(db, job, when_utc(now()))
                job.held_call = None
            apply_stage_result(row, attempt, {
                "reviewer": "self", "findings": findings, "reviewedAt": project_moment(db).isoformat(),
            })
            return assessment_view(db, row, eligible)

    def require_review_target(db, row, eligible, attempt):
        if (attempt.kind != "evidence" or attempt.result is not None or not eligible or row.skipped_at
                or row.attempt_count != attempt.number):
            raise HTTPException(409, "这次成果的测评状态已变化，请重新打开。")
        if db.get(Project, row.project_id).paused:
            raise HTTPException(409, "请先恢复学习项目。")

    def selectable_scope(db, row, eligible, attempt, job, data):
        require_review_target(db, row, eligible, attempt)
        if job and (job.active_call or job.state in {"queued", "running", "held"}):
            raise HTTPException(409, "请先处理已经授权的本次评审。")
        recipient = stage_recipient(db, row)
        if recipient is None:
            raise HTTPException(409, "请在管理中心设置独立的复核模型连接。")
        alternate = stage_backup(db, row, recipient) if data.allowBackup else None
        if data.allowBackup and alternate is None:
            raise HTTPException(409, "请配置另一条独立的复核备用连接。")
        try:
            backup = connection_view(alternate) if alternate else None
            return make_scope(row, attempt, data.excerpts, connection_view(recipient), backup)
        except ValueError as failure:
            raise HTTPException(422, str(failure)) from failure

    @router.post("/{stage_id}/attempts/{attempt_id}/model-review/preview")
    def preview_review(project_id: UUID, stage_id: UUID, attempt_id: UUID, data: ReviewPreview):
        with transaction(engine, write=False) as db:
            row, eligible = require_stage(db, project_id, stage_id)
            attempt = require_attempt(db, row, attempt_id)
            job = db.get(StageReview, attempt.id)
            scope = selectable_scope(db, row, eligible, attempt, job, data)
            approval = str(uuid4())
            confirmation = confirm_scope(secret, attempt.id, job.approval_id if job else None, approval, scope, data.maxCalls)
            return {"approvalId": approval, "scope": scope, "maxCalls": data.maxCalls, "confirmation": confirmation}

    @router.post("/{stage_id}/attempts/{attempt_id}/model-review")
    def authorize_review(project_id: UUID, stage_id: UUID, attempt_id: UUID, data: AuthorizeReview):
        with transaction(engine) as db:
            row, eligible = require_stage(db, project_id, stage_id)
            attempt = require_attempt(db, row, attempt_id)
            job = db.get(StageReview, attempt.id)
            approval = str(data.approvalId)
            if job and job.approval_id == approval and compare_digest(job.confirmation, data.confirmation):
                if (data.maxCalls != job.max_calls or data.excerpts != scope_locators(job.scope)
                        or data.allowBackup != bool(job.scope.get("backup"))):
                    raise HTTPException(409, "这次授权已记录其他选段或额度。")
                return assessment_view(db, row, eligible)
            scope = selectable_scope(db, row, eligible, attempt, job, data)
            confirmation = confirm_scope(secret, attempt.id, job.approval_id if job else None, approval, scope, data.maxCalls)
            if not compare_digest(confirmation, data.confirmation):
                raise HTTPException(409, "证据选段、额度或接收模型已变化，请重新预览。")
            if job is None:
                job = StageReview(id=attempt.id, created_at=when_utc(now()))
                db.add(job)
            keep_backup = job.reason == "task_budget" and job.scope == scope
            if not keep_backup:
                job.failover_reason = None
            job.approval_id, job.confirmation, job.scope = approval, confirmation, scope
            job.max_calls, job.state, job.reason = data.maxCalls, "queued", None
            job.available_at, job.daily_blocked_on = None, None
            db.flush()
            return assessment_view(db, row, eligible)

    @router.post("/{stage_id}/attempts/{attempt_id}/model-review/stop")
    def stop_model_review(project_id: UUID, stage_id: UUID, attempt_id: UUID):
        with transaction(engine) as db:
            row, eligible = require_stage(db, project_id, stage_id)
            attempt = require_attempt(db, row, attempt_id)
            job = db.get(StageReview, attempt.id)
            if not job or job.state not in {"queued", "running", "blocked", "stopped"}:
                raise HTTPException(409, "这次评审已无新增调用需要停止。")
            stop_review(db, job, when_utc(now()))
            return assessment_view(db, row, eligible)

    @router.post("/{stage_id}/attempts/{attempt_id}/model-review/apply")
    def apply_model_review(project_id: UUID, stage_id: UUID, attempt_id: UUID):
        with transaction(engine) as db:
            row, eligible = require_stage(db, project_id, stage_id)
            attempt = require_attempt(db, row, attempt_id)
            job = db.get(StageReview, attempt.id)
            if job and job.state == "complete":
                return assessment_view(db, row, eligible)
            require_review_target(db, row, eligible, attempt)
            call = db.get(StageReviewCall, job.held_call) if job and job.held_call else None
            if not call or call.review_id != attempt.id or call.report is None:
                raise HTTPException(409, "本次暂无已返回且可采用的评审。")
            apply_stage_result(row, attempt, call.report)
            job.state, job.reason, job.held_call = "complete", None, None
            return assessment_view(db, row, eligible)

    return router
