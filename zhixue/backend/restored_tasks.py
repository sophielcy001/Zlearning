from datetime import UTC, datetime

from sqlalchemy import case, delete, or_, select, update

from .db import (
    AgentAuthoringJob, AssessmentJob, ChatModelCall, ChatReply, GenerationStep, GoalAuthoringJob, GraphRecommendation, LessonConsolidation, LessonQuestion, MemoryInferenceJob, ModelStudy, ModelUse, NoteCheckBatch, NoteCheckPlan,
    LibraryClassification, LibraryClassificationCandidate, NoteSuggestion, PrerequisiteCall, ProfessionalAgent,
    PrerequisiteTask, RecognitionGrant, RemediationCard, StageReview, StageReviewCall, Task, Timer, transaction,
)
from .project_activity import study_moment
from .model_ledger import settle_open_charges
from .money_budget import BUDGET_REASONS, STOPPED_COST
from .timer import set_status


WAITING = "从备份恢复，等待继续。"
RESTORABLE_BUDGET_HOLDS = tuple(reason for reason in BUDGET_REASONS if reason != STOPPED_COST)


def hold_generation(db) -> int:
    can_dispatch = or_(ModelStudy.reason.is_(None), ModelStudy.reason.in_(RESTORABLE_BUDGET_HOLDS))
    pending = or_(
        GenerationStep.state.in_(("running", "reviewing")), GenerationStep.publication_pending.is_(True),
        GenerationStep.state.in_(("revision_ready", "revision_unverified")),
        GenerationStep.state.in_(("pending", "review_pending")) & can_dispatch,
    )
    studies = db.scalars(select(ModelStudy).where(
        ModelStudy.stopped.is_(False), ModelStudy.deleted_at.is_(None),
        select(GenerationStep.id).where(GenerationStep.project_id == ModelStudy.project_id, pending).exists(),
    )).all()
    for study in studies:
        study.stopped, study.reason, study.daily_blocked_on = True, "restored", None
        task = db.get(Task, study.project_id)
        task.status, task.error = "restore_pending", WAITING
    db.execute(update(GenerationStep).where(GenerationStep.state.in_(("running", "reviewing"))).values(
        state=case((GenerationStep.state == "reviewing", "review_failed"), else_="failed"),
        error="restored", active_call=None, available_at=None,
    ))
    local = db.execute(update(Task).where(Task.status == "queued", Task.project_id.not_in(select(ModelStudy.project_id)))
                       .values(status="restore_pending", error=WAITING)).rowcount
    return len(studies) + local


def hold_restored_tasks(engine, at: datetime) -> tuple[int, int]:
    clock = at.astimezone(UTC).replace(tzinfo=None)
    with transaction(engine) as db:
        settle_open_charges(db, clock)
        db.execute(delete(RecognitionGrant))
        for agent in db.scalars(select(ProfessionalAgent)):
            permissions = {"autoMemory", "autoMemoryRecipient", "mcpRepositories"}
            if permissions.intersection(agent.definition):
                agent.definition = {key: value for key, value in agent.definition.items() if key not in permissions}
                agent.revision += 1
                agent.confirmation = ""
        count = hold_generation(db)
        jobs = (
            (AssessmentJob, "error", "blocked"), (LessonQuestion, "error", "blocked"), (ChatReply, "error", "blocked"),
            (LessonConsolidation, "reason", "blocked"), (PrerequisiteTask, "reason", "stopped"),
            (StageReview, "reason", "stopped"),
        )
        for model, reason, state in jobs:
            active = or_(model.state.in_(("queued", "running")),
                         (model.state == "blocked") & getattr(model, reason).in_(RESTORABLE_BUDGET_HOLDS))
            changed = db.execute(update(model).where(active).values(
                state=state, active_call=None, available_at=None, daily_blocked_on=None, **{reason: "restored"},
            )).rowcount
            count += changed
            db.execute(update(model).where(model.active_call.is_not(None)).values(active_call=None))
        count += db.execute(update(RemediationCard).where(
            RemediationCard.active.is_(True), RemediationCard.approved.is_(True),
            RemediationCard.published_at.is_(None), RemediationCard.publish_error.is_(None),
        ).values(publish_error="restored")).rowcount
        count += db.execute(update(NoteSuggestion).where(
            NoteSuggestion.state.in_(("queued", "running"))
            | ((NoteSuggestion.state == "blocked") & NoteSuggestion.error.in_(RESTORABLE_BUDGET_HOLDS)),
        ).values(
            state="failed", error="restored", context=None, daily_blocked_on=None,
        )).rowcount
        count += db.execute(update(NoteCheckPlan).values(problem="restored", blocked_on=None, budget_reason=None)).rowcount
        db.execute(update(NoteCheckBatch).where(NoteCheckBatch.state == "running").values(
            state="failed", error="restored",
        ))
        for model in (GraphRecommendation, LibraryClassification, GoalAuthoringJob, AgentAuthoringJob, MemoryInferenceJob):
            count += db.execute(update(model).where(model.state.in_(("queued", "running", "blocked"))).values(
                state="failed", error="restored", scope=None, daily_blocked_on=None,
            )).rowcount
        count += db.execute(update(LibraryClassificationCandidate).where(
            LibraryClassificationCandidate.state == "approved",
        ).values(state="pending", approved=None, decided_at=None)).rowcount
        for model in (ModelUse, ChatModelCall, PrerequisiteCall, StageReviewCall, NoteSuggestion, NoteCheckBatch,
                      GraphRecommendation, LibraryClassification, GoalAuthoringJob, AgentAuthoringJob, MemoryInferenceJob):
            db.execute(update(model).where(model.started_at.is_not(None), model.finished_at.is_(None)).values(
                finished_at=case((model.started_at > clock, model.started_at), else_=clock),
                status=case((model.status == "reserved", "not_sent"), else_="uncertain"),
            ))
        timers = db.scalars(select(Timer).where(Timer.status.in_(("running", "paused")))).all()
        for timer in timers:
            boundary = max(clock, timer.started_at or timer.updated_at) if timer.status == "running" else timer.updated_at
            set_status(db, timer, "stashed", boundary, study_moment(boundary))
        return count, len(timers)
