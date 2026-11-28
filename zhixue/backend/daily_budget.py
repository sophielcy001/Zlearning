from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .assessment import assessment_usage, basis_in_course
from .assessment_notes import is_current_job
from .assessment_worker import authorized_recipient, job_context
from .content import lesson_course
from .course_revision import active_revision, revision_issue
from .course_sources import sources_current
from .db import (
    AgentAuthoringJob, AssessmentJob, AssessmentPlan, ChatMessage, ChatReply, Conversation, GenerationStep, GoalAuthoringJob, GraphRecommendation, Lesson, LessonAssistantPlan, LessonConsolidation,
    LessonConsolidationPlan, LessonQuestion, LibraryClassification, MemoryInferenceJob, ModelStudy, Note, NoteCheckPlan, NoteSuggestion, PrerequisiteTask, Project, RemediationCard,
    StageAssessment, StageReview, Task, transaction,
)
from .goal_authoring import scope_is_current as goal_scope_is_current
from .professional_agent import pinned_reply_connection
from .lesson_answer_worker import check_scope, context_available
from .lesson_assistant import published_version, usage as answer_usage
from .memory_inference import checked_source as memory_source_is_current
from .memory_inference import pinned_memory_connection
from .model_costs import MoneyLimit
from .model_study import ERRORS, generation_amount_issue, generation_recipient, next_generation_step, settings_view, sync_task
from .model_study_input import Input, pinned_connection
from .model_usage import billed_calls, billing_day, daily_capacity
from .money_budget import (
    BUDGET_REASONS, DAILY_BUDGET_REASONS, MONEY_ERRORS, STOPPED_COST, assessment_budget_scope,
    daily_cost_view, held_issue, set_task_limit, task_limit,
)
from .note_ai import course_context, note_available
from .note_check_worker import note_check_amount_issue, reviewer as note_reviewer
from .prerequisite import authorization_issue, usage as prerequisite_usage
from .prerequisite_worker import has_formal_course
from .stage_review_worker import job_rows as stage_review_jobs, stage_review_ready
from .study_consolidation import usage as consolidation_usage
from .study_consolidation_worker import validate_scope


Kind = Literal["generation", "assessment", "answer", "chat", "consolidation", "prerequisite", "stage_review",
               "graph", "classification", "goal_authoring", "agent_authoring", "memory_inference", "note_ai", "note_check"]
Job = (ModelStudy | AssessmentJob | LessonQuestion | ChatReply | LessonConsolidation | PrerequisiteTask
       | StageReview | GraphRecommendation | LibraryClassification | GoalAuthoringJob | AgentAuthoringJob | MemoryInferenceJob | NoteSuggestion | NoteCheckPlan)
REASON_JOBS = (ModelStudy, LessonConsolidation, PrerequisiteTask, StageReview)


class TaskIdentity(BaseModel):
    kind: Kind
    id: UUID


class ResumeInput(BaseModel):
    billingDay: date
    tasks: list[TaskIdentity] = Field(min_length=1)


class TaskLimitInput(Input):
    maxCost: MoneyLimit | None = None
    useDefaultCost: Literal[True] | None = None


@dataclass(frozen=True)
class RecoveryTask:
    kind: Kind
    job: Job
    project: Project | None
    lesson: Lesson | None = None
    stage: StageAssessment | None = None

    @property
    def identity(self) -> str:
        return self.job.project_id if isinstance(self.job, ModelStudy) else self.job.id

    @property
    def budget_identity(self) -> str:
        if isinstance(self.job, AssessmentJob):
            return assessment_budget_scope(self.job)
        if isinstance(self.job, NoteCheckPlan):
            return self.job.budget_scope_id or self.job.id
        return self.identity

    @property
    def reason(self) -> str:
        if isinstance(self.job, NoteCheckPlan):
            return self.job.budget_reason or "daily_budget"
        return self.job.reason if isinstance(self.job, REASON_JOBS) else self.job.error

    def can_resume(self, db: Session, now: datetime, has_slots: bool) -> bool:
        if not has_slots or self.reason not in MONEY_ERRORS:
            return has_slots
        if isinstance(self.job, ModelStudy):
            issue = generation_amount_issue(db, self.job, self.project, now)
        elif isinstance(self.job, NoteCheckPlan):
            issue = note_check_amount_issue(db, self.job, billing_day(now))
        else:
            issue = held_issue(db, self.kind, self.budget_identity, billing_day(now))
        return issue is None

    def cost_info(self, db: Session, now: datetime, has_slots: bool) -> dict:
        limit, inherited = task_limit(db, self.kind, self.budget_identity)
        return {"reason": self.reason, "error": ERRORS[self.reason], "canResume": self.can_resume(db, now, has_slots),
                "maxCost": limit, "usesDefaultCost": inherited}

    def view(self, db: Session) -> dict:
        if isinstance(self.job, (NoteSuggestion, NoteCheckPlan)):
            note = db.get(Note, self.job.note_id)
            return {"id": self.identity, "kind": self.kind, "title": note.title or "未命名笔记",
                    "projectId": None, "projectTopic": "知识库", "lessonId": None, "phase": None, "noteId": note.id,
                    "blockedOn": self.job.blocked_on if isinstance(self.job, NoteCheckPlan) else self.job.daily_blocked_on}
        if isinstance(self.job, (GraphRecommendation, LibraryClassification)):
            title = "图谱推荐" if isinstance(self.job, GraphRecommendation) else "知识库 AI 整理"
            return {"id": self.identity, "kind": self.kind, "title": title, "projectId": None,
                    "projectTopic": "知识库", "lessonId": None, "phase": None, "blockedOn": self.job.daily_blocked_on}
        if isinstance(self.job, (AgentAuthoringJob, MemoryInferenceJob)):
            title = "AI 记忆整理" if isinstance(self.job, MemoryInferenceJob) else "专业 Agent 草稿"
            return {"id": self.identity, "kind": self.kind, "title": title, "projectId": None,
                    "projectTopic": "管理中心", "lessonId": None, "phase": None, "blockedOn": self.job.daily_blocked_on}
        if isinstance(self.job, GoalAuthoringJob):
            return {"id": self.identity, "kind": self.kind, "title": "学习目标草稿", "projectId": None,
                    "projectTopic": "聊天", "lessonId": None, "phase": None,
                    "conversationId": self.job.conversation_id, "blockedOn": self.job.daily_blocked_on}
        if isinstance(self.job, ChatReply):
            question = db.get(ChatMessage, self.job.id)
            thread = db.get(Conversation, question.conversation_id)
            return {"id": self.identity, "kind": self.kind, "title": " ".join(question.text.split())[:80],
                    "projectId": None, "projectTopic": "聊天", "lessonId": None, "phase": None,
                    "conversationId": thread.id, "blockedOn": self.job.daily_blocked_on}
        title, phase = self.project.topic, None
        if self.lesson is not None:
            title = lesson_course(self.project.sample, self.lesson.position, self.lesson.outline).title
        if isinstance(self.job, PrerequisiteTask):
            title, phase = self.job.topic, self.job.phase
        elif isinstance(self.job, LessonQuestion):
            question = db.get(ChatMessage, self.job.id)
            if question:
                excerpt = " ".join(question.text.split())
                title = excerpt if len(excerpt) <= 80 else excerpt[:80] + "…"
        elif isinstance(self.job, LessonConsolidation):
            phase = self.job.phase
        elif isinstance(self.job, AssessmentJob):
            phase = self.job.kind
        if self.stage is not None:
            title = self.stage.phase
        return {"id": self.identity, "kind": self.kind, "title": title, "projectId": self.project.id,
                "projectTopic": self.project.topic, "lessonId": self.lesson.id if self.lesson else None,
                "phase": phase, "blockedOn": self.job.daily_blocked_on,
                **({"stageId": self.stage.id} if self.stage is not None else {})}

    def resume(self, db: Session) -> None:
        job = self.job
        if isinstance(job, NoteCheckPlan):
            job.blocked_on = job.budget_reason = None
            return
        job.daily_blocked_on = None
        if isinstance(job, ModelStudy):
            job.reason = None
            job.stopped = False
            sync_task(db, job, self.project)
        elif isinstance(job, (GraphRecommendation, LibraryClassification, GoalAuthoringJob, AgentAuthoringJob, MemoryInferenceJob, NoteSuggestion)):
            job.state, job.error = "queued", None
        else:
            job.state, job.available_at = "queued", None
            if isinstance(job, (AssessmentJob, LessonQuestion, ChatReply)):
                job.error = None
            else:
                job.reason = None

    def stop(self, db: Session) -> None:
        job = self.job
        if isinstance(job, NoteCheckPlan):
            job.budget_reason = STOPPED_COST
        elif isinstance(job, REASON_JOBS):
            job.reason = STOPPED_COST
            if isinstance(job, ModelStudy):
                job.stopped = True
                sync_task(db, job, self.project)
            elif isinstance(job, (PrerequisiteTask, StageReview)):
                job.state = "stopped"
        else:
            job.error = STOPPED_COST


def generation_ready(db: Session, folder: Path, study: ModelStudy, now: datetime) -> bool:
    step = next_generation_step(db, study, now)
    if step is None:
        return False
    reviewing = step.state == "review_pending"
    _, plan, issue = generation_recipient(db, study, step, reviewing=reviewing)
    if request := active_revision(step):
        return issue is None and revision_issue(db, folder, db.get(Lesson, step.lesson_id), request) is None
    return issue is None and (not reviewing or sources_current(db, folder, plan.sources))


def assessment_ready(db: Session, folder: Path, job: AssessmentJob, lesson: Lesson) -> bool:
    if not lesson.ready or lesson.learned_on is None:
        return False
    plan = db.get(AssessmentPlan, lesson.id)
    if authorized_recipient(db, job, plan) is None:
        return False
    if plan.max_calls is not None and assessment_usage(db, lesson.id) >= plan.max_calls:
        return False
    if job.kind == "note_verify":
        return is_current_job(plan, job)
    saved, _, basis = job_context(db, job)
    if job.round_id is not None and saved is None:
        return False
    if job.remediation_id is not None:
        card = db.get(RemediationCard, job.remediation_id)
        if not card or not card.active:
            return False
        basis = card.basis
    return (basis is not None and any(item["proof"] == basis["proof"] for item in plan.sections)
            and basis_in_course(db, lesson, basis)
            and sources_current(db, folder, basis["citations"]))


def answer_ready(db: Session, folder: Path, record: LessonQuestion, lesson: Lesson) -> bool:
    plan = db.get(LessonAssistantPlan, record.lesson_id)
    return (check_scope(db, record, plan) is None
            and (plan.max_calls is None or answer_usage(db, lesson.id) < plan.max_calls)
            and context_available(db, folder, record.context))


def consolidation_ready(db: Session, folder: Path, job: LessonConsolidation, lesson: Lesson) -> bool:
    if job.phase not in ("draft", "review") or lesson.learned_on is None:
        return False
    plan = db.get(LessonConsolidationPlan, lesson.id)
    return (validate_scope(db, folder, job, lesson, published_version(db, lesson), plan) is None
            and (plan.max_calls is None or consolidation_usage(db, lesson.id) < plan.max_calls))


def prerequisite_ready(db: Session, folder: Path, task: PrerequisiteTask, lesson: Lesson) -> bool:
    return (task.phase in ("draft", "review") and not has_formal_course(db, folder, task)
            and authorization_issue(db, folder, task) is None
            and (task.max_calls is None or prerequisite_usage(db, task.id) < task.max_calls))


def budget_tasks(db: Session, folder: Path, now: datetime, *, reminder: bool = False,
                 requested: set[tuple[str, str]] | None = None) -> Iterator[RecoveryTask]:
    day = billing_day(now)
    settings = settings_view(db)
    reasons = DAILY_BUDGET_REASONS if reminder else BUDGET_REASONS

    def date_filter(column):
        if reminder and settings["autoContinueDailyBudget"]:
            return column.is_(None)
        return column.is_(None) | (column < day if reminder else column <= day)

    studies = (select(ModelStudy, Project).join(Project, Project.id == ModelStudy.project_id)
               .join(Task, Task.project_id == ModelStudy.project_id)
               .where(ModelStudy.reason.in_(reasons), ModelStudy.deleted_at.is_(None), Project.paused.is_(False),
                      (ModelStudy.stopped.is_(False) & (Task.status == "blocked")) | (ModelStudy.reason == STOPPED_COST),
                      date_filter(ModelStudy.daily_blocked_on)))
    for study, project in db.execute(studies):
        if requested is not None and ("generation", project.id) not in requested:
            continue
        if generation_ready(db, folder, study, now):
            yield RecoveryTask("generation", study, project)

    kinds = (
        ("assessment", AssessmentJob, AssessmentJob.error, assessment_ready),
        ("answer", LessonQuestion, LessonQuestion.error, answer_ready),
        ("consolidation", LessonConsolidation, LessonConsolidation.reason, consolidation_ready),
        ("prerequisite", PrerequisiteTask, PrerequisiteTask.reason, prerequisite_ready),
    )
    for kind, model, reason, ready in kinds:
        pending = (select(model, Lesson, Project).select_from(model).join(Lesson, Lesson.id == model.lesson_id)
                   .join(Project, Project.id == Lesson.project_id).where(
                       ((model.state == "blocked") | ((model.state == "stopped") & (reason == STOPPED_COST))),
                       reason.in_(reasons), Project.paused.is_(False),
                       model.active_call.is_(None), date_filter(model.daily_blocked_on),
                   ))
        for job, lesson, project in db.execute(pending):
            if requested is not None and (kind, job.id) not in requested:
                continue
            if kind != "prerequisite" and db.scalar(select(GenerationStep.id).where(GenerationStep.lesson_id == lesson.id)) is None:
                continue
            if ready(db, folder, job, lesson):
                yield RecoveryTask(kind, job, project, lesson)


    stage_jobs = stage_review_jobs().where(
        (StageReview.state == "blocked") | ((StageReview.state == "stopped") & (StageReview.reason == STOPPED_COST)),
        StageReview.reason.in_(reasons), StageReview.active_call.is_(None),
        StageAssessment.skipped_at.is_(None), Project.paused.is_(False), date_filter(StageReview.daily_blocked_on),
    )
    for job, attempt, stage, project in db.execute(stage_jobs):
        if requested is not None and ("stage_review", job.id) not in requested:
            continue
        if stage_review_ready(db, job, attempt, stage, project):
            yield RecoveryTask("stage_review", job, project, stage=stage)


    chat = select(ChatReply).where(
        ChatReply.state == "blocked", ChatReply.error.in_(reasons), date_filter(ChatReply.daily_blocked_on),
    )
    for reply in db.scalars(chat):
        if requested is not None and ("chat", reply.id) not in requested:
            continue
        if pinned_reply_connection(db, reply) is not None:
            yield RecoveryTask("chat", reply, None)

    for kind, model in (("graph", GraphRecommendation), ("classification", LibraryClassification),
                        ("goal_authoring", GoalAuthoringJob), ("agent_authoring", AgentAuthoringJob),
                        ("memory_inference", MemoryInferenceJob)):
        recommendations = select(model).where(
            model.state == "blocked", model.error.in_(reasons),
            model.scope.is_not(None), date_filter(model.daily_blocked_on),
        )
        for job in db.scalars(recommendations):
            if requested is not None and (kind, job.id) not in requested:
                continue
            if isinstance(job, GoalAuthoringJob) and not goal_scope_is_current(db, folder, job):
                continue
            if isinstance(job, MemoryInferenceJob):
                if memory_source_is_current(db, job) and pinned_memory_connection(db, job) is not None:
                    yield RecoveryTask(kind, job, None)
            elif pinned_connection(db, "main", job.connection) is not None:
                yield RecoveryTask(kind, job, None)

    suggestions = select(NoteSuggestion).where(
        NoteSuggestion.state == "blocked", NoteSuggestion.error.in_(reasons),
        NoteSuggestion.context.is_not(None), date_filter(NoteSuggestion.daily_blocked_on),
    )
    for job in db.scalars(suggestions):
        if requested is not None and ("note_ai", job.id) not in requested:
            continue
        if pinned_connection(db, "main", job.connection) is None or not note_available(db, job.note_id):
            continue
        if lesson := job.context["lessonId"]:
            try:
                if course_context(db, folder, job.note_id, lesson) != job.context["course"]:
                    continue
            except HTTPException:
                continue
        yield RecoveryTask("note_ai", job, None)

    checks = select(NoteCheckPlan).where(
        NoteCheckPlan.problem.is_(None), NoteCheckPlan.blocked_on.is_not(None), date_filter(NoteCheckPlan.blocked_on),
        NoteCheckPlan.budget_reason.in_(reasons) | NoteCheckPlan.budget_reason.is_(None),
    )
    for plan in db.scalars(checks):
        if requested is not None and ("note_check", plan.id) not in requested:
            continue
        if note_available(db, plan.note_id) and note_reviewer(db, plan):
            yield RecoveryTask("note_check", plan, None)


def recoverable(db: Session, folder: Path, now: datetime, *, reminder: bool = False,
                requested: set[tuple[str, str]] | None = None) -> Iterator[RecoveryTask]:
    day = billing_day(now)
    if daily_capacity(settings_view(db)["dailyLimit"], billed_calls(db, day)):
        for task in budget_tasks(db, folder, now, reminder=reminder, requested=requested):
            if task.can_resume(db, now, True):
                yield task


def daily_budget_router(engine: Engine, folder: Path, now: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/api/generation/daily-budget")

    @router.get("")
    def pending_tasks():
        with transaction(engine) as db:
            clock = now()
            day = billing_day(clock)
            settings = settings_view(db)
            used = billed_calls(db, day)
            has_slots = daily_capacity(settings["dailyLimit"], used)
            tasks = []
            for task in budget_tasks(db, folder, clock):
                if task.reason in MONEY_ERRORS:
                    tasks.append({**task.view(db), **task.cost_info(db, clock, has_slots)})
                elif has_slots:
                    tasks.append({**task.view(db), "canResume": True, "reason": task.reason})
            tasks.sort(key=lambda task: (task["projectTopic"], task["kind"], task["title"], task["id"]))
            return {"billingDay": day, "dailyLimit": settings["dailyLimit"], "used": used,
                    "dailyCost": daily_cost_view(db, day),
                    "autoContinueDailyBudget": settings["autoContinueDailyBudget"], "tasks": tasks}

    @router.post("")
    def resume_tasks(data: ResumeInput):
        with transaction(engine) as db:
            clock = now()
            if data.billingDay != billing_day(clock):
                raise HTTPException(409, "计费日已变化，请重新查看当前额度和待继续任务。")
            requested = {(item.kind, str(item.id)) for item in data.tasks}
            items = list(recoverable(db, folder, clock, requested=requested))
            resumed = [{"kind": item.kind, "id": item.identity} for item in items]
            for item in items:
                item.resume(db)
            return {"resumed": resumed, "skipped": len(requested) - len(items)}

    def monetary_task(db, clock: datetime, kind: Kind, identity: UUID) -> RecoveryTask:
        task = next(budget_tasks(db, folder, clock, requested={(kind, str(identity))}), None)
        if task is None or task.reason not in MONEY_ERRORS:
            raise HTTPException(409, "额度暂停任务已变化，请刷新后再试。")
        return task

    @router.put("/tasks/{kind}/{identity}/amount")
    def change_task_limit(kind: Kind, identity: UUID, data: TaskLimitInput):
        if bool(data.useDefaultCost) == ("maxCost" in data.model_fields_set):
            raise HTTPException(422, "请选择任务金额上限或使用默认上限。")
        with transaction(engine) as db:
            clock = now()
            task = monetary_task(db, clock, kind, identity)
            set_task_limit(db, kind, task.budget_identity, data.maxCost, bool(data.useDefaultCost))
            day = billing_day(clock)
            slots = daily_capacity(settings_view(db)["dailyLimit"], billed_calls(db, day))
            return task.cost_info(db, clock, slots)

    @router.post("/tasks/{kind}/{identity}/stop")
    def stop_task(kind: Kind, identity: UUID):
        with transaction(engine) as db:
            monetary_task(db, now(), kind, identity).stop(db)
            return {"stopped": True}

    return router
