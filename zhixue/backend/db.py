from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, LargeBinary, String, Text, UniqueConstraint,
    create_engine, event, func, text,
)
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from .model_costs import MoneyLimit
from .storage_files import sqlite_file_uri, sync_directory


ROOT = Path(__file__).resolve().parents[1]


class Base(DeclarativeBase):
    pass


class InstalledSkill(Base):
    __tablename__ = "installed_skills"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict] = mapped_column(JSON)
    activation: Mapped[int]


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("name", "position"),)

    name: Mapped[str] = mapped_column(ForeignKey("installed_skills.name", ondelete="CASCADE"), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int]
    source: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict] = mapped_column(JSON)


class ProfessionalAgent(Base):
    __tablename__ = "professional_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str]
    revision: Mapped[int]
    definition: Mapped[dict] = mapped_column(JSON)
    confirmation: Mapped[str] = mapped_column(String(64))


class LocalMemory(Base):
    __tablename__ = "local_memories"
    __table_args__ = (
        Index("ix_local_memories_review", "state", "review_on"),
        CheckConstraint("state IN ('active', 'pending', 'deleted')", name="memory_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    text: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str]
    revision: Mapped[int]
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("professional_agents.id"))
    source_message_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    origin: Mapped[str] = mapped_column(server_default="explicit")
    review_on: Mapped[date | None]
    updated_at: Mapped[datetime]


class RepositoryMcpConnection(Base):
    __tablename__ = "repository_mcp_connections"

    provider: Mapped[str] = mapped_column(primary_key=True)
    revision: Mapped[int]
    enabled: Mapped[bool] = mapped_column(Boolean)
    repositories: Mapped[list[str]] = mapped_column(JSON)
    tools: Mapped[list[str]] = mapped_column(JSON)
    version: Mapped[str | None]
    credential_ref: Mapped[str | None] = mapped_column(String(36))
    credential_hash: Mapped[str | None] = mapped_column(String(64))


class ExternalMemoryConnection(Base):
    __tablename__ = "external_memory_connection"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int]
    reference: Mapped[str] = mapped_column(String(36))
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(JSON)


class ExternalMemoryCopy(Base):
    __tablename__ = "external_memory_copies"
    __table_args__ = (Index("ix_external_memory_queue", "attempt", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(36), index=True)
    memory_revision: Mapped[int]
    memory_scope: Mapped[str]
    content_hash: Mapped[str] = mapped_column(String(64))
    confirmation: Mapped[str] = mapped_column(String(64))
    state: Mapped[str]
    remote_ids: Mapped[list[str]] = mapped_column(JSON)
    delete_requested: Mapped[bool] = mapped_column(Boolean)
    attempt: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topic: Mapped[str]
    outcome: Mapped[str]
    foundation: Mapped[str]
    sample: Mapped[bool]
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_learned_at: Mapped[datetime | None] = mapped_column(DateTime)
    priority: Mapped[str | None]
    lessons: Mapped[list["Lesson"]] = relationship(
        order_by=lambda: func.coalesce(Lesson.reading_order, Lesson.position), lazy="selectin",
    )
    task: Mapped["Task"] = relationship(lazy="selectin")


class ProjectTag(Base):
    __tablename__ = "project_tags"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True, index=True)
    name: Mapped[str]


class ProjectView(Base):
    __tablename__ = "project_views"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    definition: Mapped[dict] = mapped_column(JSON)


class ModelConnection(Base):
    __tablename__ = "model_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str]
    provider: Mapped[str]
    protocol: Mapped[str]
    endpoint: Mapped[str]
    model: Mapped[str]
    credential_ref: Mapped[str | None] = mapped_column(String(36))
    revision: Mapped[int]
    test_token: Mapped[str | None] = mapped_column(String(36))
    last_test: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    pricing: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class ModelRole(Base):
    __tablename__ = "model_roles"

    role: Mapped[str] = mapped_column(primary_key=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("model_connections.id"), index=True)


class RecognitionGrant(Base):
    __tablename__ = "recognition_grants"

    kind: Mapped[str] = mapped_column(primary_key=True)
    approval_id: Mapped[str] = mapped_column(String(36))
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("model_connections.id", ondelete="SET NULL"))
    connection_revision: Mapped[int | None]
    scope: Mapped[str | None]
    receipt: Mapped[str | None] = mapped_column(String(64))


class ModelCredentialPending(Base):
    __tablename__ = "model_credential_pending"

    reference: Mapped[str] = mapped_column(String(36), primary_key=True)
    reserved: Mapped[bool]


class Task(Base):
    __tablename__ = "tasks"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    status: Mapped[str] = mapped_column(default="queued")
    error: Mapped[str | None]


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (UniqueConstraint("project_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    position: Mapped[int]
    reading_order: Mapped[int | None]
    version: Mapped[int] = mapped_column(default=1)
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    learned_on: Mapped[date | None] = mapped_column(Date, index=True)
    outline: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    reviews: Mapped[list["Review"]] = relationship(order_by="Review.key", lazy="selectin")


class LessonReadingPosition(Base):
    __tablename__ = "lesson_reading_positions"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[str] = mapped_column(String(64))
    point: Mapped[dict] = mapped_column(JSON)


class ModelStudy(Base):
    __tablename__ = "model_studies"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(36))
    connection_revision: Mapped[int]
    goal_fields: Mapped[list[dict]] = mapped_column(JSON)
    max_calls: Mapped[int | None]
    backup: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    focus_id: Mapped[str | None] = mapped_column(ForeignKey("lessons.id", ondelete="SET NULL"))
    stopped: Mapped[bool]
    reason: Mapped[str | None]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)


class GenerationStep(Base):
    __tablename__ = "generation_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("model_studies.project_id", ondelete="CASCADE"), index=True)
    lesson_id: Mapped[str | None] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), unique=True)
    kind: Mapped[str]
    state: Mapped[str]
    priority: Mapped[int | None] = mapped_column(index=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    active_call: Mapped[str | None] = mapped_column(String(36))
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    error: Mapped[str | None]
    finding_id: Mapped[str | None] = mapped_column(String(36))
    publication_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_review: Mapped[bool] = mapped_column(Boolean, default=False)
    revision_request: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class ModelUse(Base):
    __tablename__ = "model_uses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("model_studies.project_id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("generation_steps.id", ondelete="CASCADE"))
    connection: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    billing_day: Mapped[date] = mapped_column(Date, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status: Mapped[str]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    auto_retry: Mapped[bool]
    phase: Mapped[str | None]
    failover_reason: Mapped[str | None]
    assessment_job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    question_id: Mapped[str | None] = mapped_column(String(36), index=True)
    consolidation_id: Mapped[str | None] = mapped_column(String(36), index=True)


class GenerationSettings(Base):
    __tablename__ = "generation_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    daily_limit: Mapped[int | None]
    concurrency: Mapped[int]
    auto_continue_daily_budget: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    daily_cost_limit: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    default_task_cost_limit: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class ModelCharge(Base):
    __tablename__ = "model_charges"
    __table_args__ = (
        Index("ix_model_charges_day", "billing_day", "started_at"),
        Index("ix_model_charges_scope", "kind", "scope_id"),
    )

    source: Mapped[str] = mapped_column(primary_key=True)
    call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str]
    scope_id: Mapped[str] = mapped_column(String(36))
    phase: Mapped[str | None]
    recipient: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    billing_day: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    total_tokens: Mapped[int | None]
    thinking_tokens: Mapped[int | None]
    audio_seconds: Mapped[float | None]
    pricing: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    actual_currency: Mapped[str | None] = mapped_column(String(3))
    actual_amount: Mapped[str | None]
    estimated_amount: Mapped[str | None]
    reserved_amount: Mapped[str | None]


class ModelTaskBudget(Base):
    __tablename__ = "model_task_budgets"

    kind: Mapped[str] = mapped_column(primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    limit_override: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    held_request: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class CourseReviewPlan(Base):
    __tablename__ = "course_review_plans"

    project_id: Mapped[str] = mapped_column(ForeignKey("model_studies.project_id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String(36))
    reviewer: Mapped[dict] = mapped_column(JSON)
    backup: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    sources: Mapped[list[dict]] = mapped_column(JSON)


class CourseFinding(Base):
    __tablename__ = "course_findings"

    id: Mapped[str] = mapped_column(ForeignKey("model_uses.id", ondelete="CASCADE"), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str] = mapped_column(String(36))
    reviewer: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    sources: Mapped[list[dict]] = mapped_column(JSON)
    sections: Mapped[list[dict]] = mapped_column(JSON)


class CourseVersion(Base):
    __tablename__ = "course_versions"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[str] = mapped_column(String(36))
    published_at: Mapped[datetime] = mapped_column(DateTime)
    sections: Mapped[list[dict]] = mapped_column(JSON)
    markdown: Mapped[str]
    revision: Mapped[str] = mapped_column(String(64))


class CourseAnchorLink(Base):
    __tablename__ = "course_anchor_links"
    __table_args__ = (
        ForeignKeyConstraint(["lesson_id", "source_version"], ["course_versions.lesson_id", "course_versions.version"], ondelete="CASCADE"),
        ForeignKeyConstraint(["lesson_id", "target_version"], ["course_versions.lesson_id", "course_versions.version"], ondelete="CASCADE"),
    )

    lesson_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_version: Mapped[int] = mapped_column(primary_key=True)
    source_section: Mapped[int] = mapped_column(primary_key=True)
    source_start: Mapped[int] = mapped_column(primary_key=True)
    source_end: Mapped[int] = mapped_column(primary_key=True)
    target_version: Mapped[int]
    target_section: Mapped[int]
    target_start: Mapped[int]
    target_end: Mapped[int]
    mapping_revision: Mapped[int]


class CourseDisposition(Base):
    __tablename__ = "course_dispositions"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True)
    section_index: Mapped[int] = mapped_column(primary_key=True)
    adopted_finding: Mapped[str | None] = mapped_column(String(36))
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime)
    deferred_finding: Mapped[str | None] = mapped_column(String(36))


class AssessmentPlan(Base):
    __tablename__ = "assessment_plans"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(36))
    primary_model: Mapped[dict] = mapped_column(JSON)
    review_model: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    backups: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    note_scope: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    sections: Mapped[list[dict]] = mapped_column(JSON)
    max_calls: Mapped[int | None]
    remediation: Mapped[bool] = mapped_column(Boolean, default=False)


class AssessmentObjective(Base):
    __tablename__ = "assessment_objectives"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str]
    ability: Mapped[str]
    criterion: Mapped[str]
    basis: Mapped[dict] = mapped_column(JSON)
    held: Mapped[bool] = mapped_column(Boolean, default=False)


class AssessmentRound(Base):
    __tablename__ = "assessment_rounds"
    __table_args__ = (UniqueConstraint("lesson_id", "key", "number", "mode"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), index=True)
    key: Mapped[str] = mapped_column(String(36))
    number: Mapped[int]
    mode: Mapped[str]
    course_version: Mapped[int]
    state: Mapped[str]
    group: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    draft: Mapped[list[str]] = mapped_column(JSON, default=list)
    hinted: Mapped[bool] = mapped_column(Boolean, default=False)
    submission: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    submitted_on: Mapped[date | None] = mapped_column(Date, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    previous_stage: Mapped[int | None]
    previous_due_on: Mapped[date | None] = mapped_column(Date)


class AssessmentGrade(Base):
    __tablename__ = "assessment_grades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    round_id: Mapped[str] = mapped_column(ForeignKey("assessment_rounds.id"), index=True)
    version: Mapped[int]
    reviewer: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    result: Mapped[dict] = mapped_column(JSON)


class RemediationCard(Base):
    __tablename__ = "remediation_cards"

    grade_id: Mapped[str] = mapped_column(ForeignKey("assessment_grades.id"), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), index=True)
    round_id: Mapped[str] = mapped_column(ForeignKey("assessment_rounds.id"), index=True)
    basis: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    draft: Mapped[list[dict] | None] = mapped_column(JSON(none_as_null=True))
    author: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    findings: Mapped[list[dict] | None] = mapped_column(JSON(none_as_null=True))
    reviewer: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    revision: Mapped[str | None] = mapped_column(String(64))
    publish_error: Mapped[str | None]


class AssessmentJob(Base):
    __tablename__ = "assessment_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), index=True)
    kind: Mapped[str]
    round_id: Mapped[str | None] = mapped_column(ForeignKey("assessment_rounds.id"), index=True)
    remediation_id: Mapped[str | None] = mapped_column(ForeignKey("remediation_cards.grade_id"), index=True)
    basis: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    failovers: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    first_due_on: Mapped[date | None] = mapped_column(Date)
    state: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    active_call: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None]
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)


class Review(Base):
    __tablename__ = "reviews"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    stage: Mapped[int] = mapped_column(default=0)
    due_on: Mapped[date | None] = mapped_column(Date)
    round_number: Mapped[int] = mapped_column(default=1)
    answers: Mapped[list[int | None]] = mapped_column(JSON, default=list)
    hinted: Mapped[bool] = mapped_column(Boolean, default=False)


class Attempt(Base):
    __tablename__ = "attempts"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    round_number: Mapped[int] = mapped_column(primary_key=True)
    submitted_on: Mapped[date] = mapped_column(Date, index=True)
    previous_due_on: Mapped[date] = mapped_column(Date)
    course_version: Mapped[int]
    input: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)


class LocalPracticeAttempt(Base):
    __tablename__ = "local_practice_attempts"
    __table_args__ = (ForeignKeyConstraint(
        ["lesson_id", "key", "source_round"], ["attempts.lesson_id", "attempts.key", "attempts.round_number"],
        ondelete="CASCADE",
    ),)

    lesson_id: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    source_round: Mapped[int] = mapped_column(primary_key=True)
    input: Mapped[dict] = mapped_column(JSON)
    view: Mapped[dict] = mapped_column(JSON)


class MobileWorkspace(Base):
    __tablename__ = "mobile_workspace"

    singleton: Mapped[int] = mapped_column(primary_key=True)
    identity: Mapped[str] = mapped_column(String(36))


class MobileChange(Base):
    __tablename__ = "mobile_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str]
    received_at: Mapped[datetime]
    details: Mapped[dict] = mapped_column(JSON)
    reply: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class ImportedFile(Base):
    __tablename__ = "imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str]
    created_on: Mapped[date] = mapped_column(Date)


class LibraryWebSnapshot(Base):
    __tablename__ = "library_web_snapshots"
    __table_args__ = (Index("ix_library_web_snapshots_url_revision", "url", "revision"),)

    file_id: Mapped[str] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), primary_key=True)
    requested_url: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[str]
    encoding: Mapped[str]
    revision: Mapped[str]


class LibraryOcrPage(Base):
    __tablename__ = "library_ocr_pages"

    file_id: Mapped[str] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), primary_key=True)
    page: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[str]
    text_revision: Mapped[str]
    body: Mapped[str] = mapped_column(Text)


class LibraryOcrVersion(Base):
    __tablename__ = "library_ocr_versions"

    file_id: Mapped[str] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), primary_key=True)
    page: Mapped[int] = mapped_column(primary_key=True)
    text_revision: Mapped[str] = mapped_column(primary_key=True)
    source_revision: Mapped[str]
    body: Mapped[str] = mapped_column(Text)
    saved_at: Mapped[datetime | None]


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    lesson_id: Mapped[str | None] = mapped_column(ForeignKey("lessons.id", ondelete="SET NULL"), index=True)
    creation_digest: Mapped[str] = mapped_column(String(64))
    goal: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    position: Mapped[int]
    role: Mapped[str]
    text: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime)


class TeachingPreference(Base):
    __tablename__ = "teaching_preferences"
    __table_args__ = (CheckConstraint("style IN ('direct', 'guided')", name="teaching_style_supported"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    style: Mapped[str] = mapped_column(String(16))


class ChatReply(Base):
    __tablename__ = "chat_replies"
    __table_args__ = (Index("ix_chat_replies_queue", "state", "available_at", "created_at"),)

    id: Mapped[str] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True)
    reply_id: Mapped[str] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), unique=True)
    confirmation: Mapped[str] = mapped_column(String(64))
    recipient: Mapped[dict] = mapped_column(JSON)
    history: Mapped[list[dict]] = mapped_column(JSON)
    style: Mapped[str] = mapped_column(String(16), server_default="direct")
    agent_scope: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    state: Mapped[str]
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)
    retries: Mapped[int]
    active_call: Mapped[str | None] = mapped_column(String(36))


class ChatModelCall(Base):
    __tablename__ = "chat_model_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reply_id: Mapped[str] = mapped_column(ForeignKey("chat_replies.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    billing_day: Mapped[date] = mapped_column(Date, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status: Mapped[str]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]


class LessonAssistantPlan(Base):
    __tablename__ = "lesson_assistant_plans"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int]
    revision: Mapped[str] = mapped_column(String(64))
    recipient: Mapped[dict] = mapped_column(JSON)
    backups: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    proofs: Mapped[list[str]] = mapped_column(JSON)
    max_calls: Mapped[int | None]


class LessonQuestion(Base):
    __tablename__ = "lesson_questions"

    id: Mapped[str] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True)
    reply_id: Mapped[str] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), unique=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String(36))
    context: Mapped[dict] = mapped_column(JSON)
    failovers: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    selection: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    state: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    active_call: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None]
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    support: Mapped[str | None]
    citations: Mapped[list[dict] | None] = mapped_column(JSON(none_as_null=True))
    recipient: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)


class LessonConsolidationPlan(Base):
    __tablename__ = "lesson_consolidation_plans"

    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int]
    revision: Mapped[str] = mapped_column(String(64))
    main: Mapped[dict] = mapped_column(JSON)
    reviewer: Mapped[dict] = mapped_column(JSON)
    backups: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    proofs: Mapped[list[str]] = mapped_column(JSON)
    max_calls: Mapped[int | None]


class LessonConsolidation(Base):
    __tablename__ = "lesson_consolidations"
    __table_args__ = (Index("ix_lesson_consolidations_queue", "state", "available_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(36))
    course_version: Mapped[int]
    question_ids: Mapped[list[str]] = mapped_column(JSON)
    phase: Mapped[str]
    state: Mapped[str]
    reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    active_call: Mapped[str | None] = mapped_column(String(36))
    material: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    failovers: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    draft: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    finding: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    author: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    reviewer: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    published_version: Mapped[int | None]
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)


class PrerequisiteTask(Base):
    __tablename__ = "prerequisite_tasks"
    __table_args__ = (
        Index("ix_prerequisite_tasks_topic", "topic_key", "created_at"),
        Index("ix_prerequisite_tasks_queue", "state", "available_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    target_lesson_id: Mapped[str | None] = mapped_column(ForeignKey("lessons.id", ondelete="SET NULL"))
    topic: Mapped[str]
    topic_key: Mapped[str]
    scope: Mapped[dict] = mapped_column(JSON)
    failovers: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    max_calls: Mapped[int | None]
    state: Mapped[str]
    phase: Mapped[str]
    reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    active_call: Mapped[str | None] = mapped_column(String(36))
    draft: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    author: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    findings: Mapped[list[dict] | None] = mapped_column(JSON(none_as_null=True))
    reviewer: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    revision: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)


class PrerequisiteCall(Base):
    __tablename__ = "prerequisite_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("prerequisite_tasks.id", ondelete="CASCADE"), index=True)
    phase: Mapped[str]
    connection: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    billing_day: Mapped[date] = mapped_column(Date, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status: Mapped[str]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    failover_reason: Mapped[str | None]
    auto_retry: Mapped[bool] = mapped_column(Boolean, default=False)


class ChatAttachment(Base):
    __tablename__ = "chat_attachments"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("imports.id"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime)


class GoalVersion(Base):
    __tablename__ = "goal_versions"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    definition: Mapped[dict] = mapped_column(JSON)
    submission_digest: Mapped[str | None] = mapped_column(String(64))


class RouteVersion(Base):
    __tablename__ = "route_versions"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "goal_version"], ["goal_versions.project_id", "goal_versions.version"],
                             ondelete="CASCADE"),
    )

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    goal_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    definition: Mapped[dict] = mapped_column(JSON)


class StageAssessment(Base):
    __tablename__ = "stage_assessments"
    __table_args__ = (UniqueConstraint("project_id", "phase_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    phase_key: Mapped[str] = mapped_column(String(64))
    phase: Mapped[str]
    route_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    criteria: Mapped[list[dict]] = mapped_column(JSON)
    attempt_count: Mapped[int]
    status: Mapped[str]
    revision: Mapped[int]
    skipped_at: Mapped[datetime | None] = mapped_column(DateTime)


class StageAssessmentAttempt(Base):
    __tablename__ = "stage_assessment_attempts"
    __table_args__ = (UniqueConstraint("assessment_id", "number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(ForeignKey("stage_assessments.id", ondelete="CASCADE"))
    number: Mapped[int]
    kind: Mapped[str]
    submitted_at: Mapped[datetime] = mapped_column(DateTime)
    submission: Mapped[dict] = mapped_column(JSON)
    outcome: Mapped[str]
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class StageReview(Base):
    __tablename__ = "stage_reviews"
    __table_args__ = (Index("ix_stage_reviews_queue", "state", "available_at", "created_at"),)

    id: Mapped[str] = mapped_column(ForeignKey("stage_assessment_attempts.id", ondelete="CASCADE"), primary_key=True)
    approval_id: Mapped[str] = mapped_column(String(36))
    confirmation: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict] = mapped_column(JSON)
    max_calls: Mapped[int | None]
    state: Mapped[str]
    reason: Mapped[str | None]
    failover_reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    active_call: Mapped[str | None] = mapped_column(String(36))
    held_call: Mapped[str | None] = mapped_column(String(36))
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)


class StageReviewCall(Base):
    __tablename__ = "stage_review_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("stage_reviews.id", ondelete="CASCADE"), index=True)
    approval_id: Mapped[str] = mapped_column(String(36))
    scope: Mapped[dict] = mapped_column(JSON)
    connection: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    failover_reason: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(DateTime)
    billing_day: Mapped[date] = mapped_column(Date, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status: Mapped[str]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    auto_retry: Mapped[bool] = mapped_column(Boolean, default=False)
    report: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class BackupFileOrigin(Base):
    __tablename__ = "backup_file_origins"

    file_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    backup_id: Mapped[str] = mapped_column(String(36))
    source_path: Mapped[str]
    source_revision: Mapped[str] = mapped_column(String(64))
    name: Mapped[str]
    created_on: Mapped[date] = mapped_column(Date)
    published: Mapped[bool]


class StudyPlanSettings(Base):
    __tablename__ = "study_plan_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    weekly: Mapped[list] = mapped_column(JSON)


class StudyDayOverride(Base):
    __tablename__ = "study_day_overrides"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    windows: Mapped[list] = mapped_column(JSON)


class StudyPlanLock(Base):
    __tablename__ = "study_plan_locks"
    __table_args__ = (
        CheckConstraint("start_minute >= 0 AND minutes > 0 AND start_minute + minutes <= 1440",
                        name="ck_study_plan_locks_time"),
        CheckConstraint("(review_key IS NULL) = (review_round IS NULL)", name="ck_study_plan_locks_round"),
    )

    task_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    review_key: Mapped[str | None]
    review_round: Mapped[int | None]
    day: Mapped[date] = mapped_column(Date, index=True)
    start_minute: Mapped[int]
    minutes: Mapped[int]
    estimate_source: Mapped[str]
    completed_on: Mapped[date | None] = mapped_column(Date)


class ReminderSettings(Base):
    __tablename__ = "reminder_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    quiet_start: Mapped[int | None]
    quiet_end: Mapped[int | None]


class ReminderReceipt(Base):
    __tablename__ = "reminder_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject: Mapped[str] = mapped_column(String(100), unique=True)
    lesson_id: Mapped[str | None] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    sent_on: Mapped[date] = mapped_column(Date)
    version: Mapped[int]
    action: Mapped[str | None]
    snooze_at: Mapped[datetime | None] = mapped_column(DateTime)
    snooze_on: Mapped[date | None] = mapped_column(Date)
    delivery_key: Mapped[str | None] = mapped_column(String(36))


class ReminderPushClaim(Base):
    __tablename__ = "reminder_push_claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject: Mapped[str] = mapped_column(String(100), unique=True)
    workspace: Mapped[str] = mapped_column(String(36))
    receiver: Mapped[str] = mapped_column(String(64))
    lesson_id: Mapped[str | None] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    prior_version: Mapped[int]
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class DemoClock(Base):
    __tablename__ = "demo_clock"

    id: Mapped[int] = mapped_column(primary_key=True)
    offset: Mapped[int]


class Timer(Base):
    __tablename__ = "timers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"))
    status: Mapped[str]
    elapsed: Mapped[int]
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    category: Mapped[str] = mapped_column(server_default="unclassified")
    review_key: Mapped[str | None]
    started_on: Mapped[date | None] = mapped_column(Date)
    device: Mapped[str] = mapped_column(server_default="desktop")
    interrupted_at: Mapped[datetime | None] = mapped_column(DateTime)


class TimerSpan(Base):
    __tablename__ = "timer_spans"
    __table_args__ = (
        CheckConstraint("seconds > 0", name="ck_timer_spans_seconds"),
        Index("ix_timer_spans_timer_day", "timer_id", "day"),
        Index("ix_timer_spans_interval", "ended_at", "began_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timer_id: Mapped[str] = mapped_column(ForeignKey("timers.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date, index=True)
    began_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime] = mapped_column(DateTime)
    seconds: Mapped[int]


class TimerCorrection(Base):
    __tablename__ = "timer_corrections"
    __table_args__ = (CheckConstraint("seconds >= 0", name="ck_timer_corrections_seconds"),)

    timer_id: Mapped[str] = mapped_column(ForeignKey("timers.id", ondelete="CASCADE"), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(10), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    report_on: Mapped[date | None] = mapped_column(Date)
    seconds: Mapped[int]
    category: Mapped[str]
    saved_at: Mapped[datetime] = mapped_column(DateTime)


class StudyPlanSnapshot(Base):
    __tablename__ = "study_plan_snapshots"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    budget_minutes: Mapped[int]
    items: Mapped[list] = mapped_column(JSON)
    frozen: Mapped[bool] = mapped_column(Boolean)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(120))
    metadata_revision: Mapped[int] = mapped_column(default=1)


class NoteLesson(Base):
    __tablename__ = "note_lessons"

    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True, index=True)


class NoteSuggestion(Base):
    __tablename__ = "note_suggestions"
    __table_args__ = (Index("ix_note_suggestions_queue", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    note_id: Mapped[str | None] = mapped_column(ForeignKey("notes.id", ondelete="SET NULL"), index=True)
    operation: Mapped[str]
    confirmation: Mapped[str] = mapped_column(String(64))
    connection: Mapped[dict] = mapped_column(JSON)
    context: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    state: Mapped[str]
    error: Mapped[str | None]
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    billing_day: Mapped[date | None] = mapped_column(Date, index=True)
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status: Mapped[str | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]


class NoteCheckPlan(Base):
    __tablename__ = "note_check_plans"

    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), unique=True)
    budget_scope_id: Mapped[str | None] = mapped_column(String(36))
    confirmation: Mapped[str] = mapped_column(String(64))
    recipient: Mapped[dict] = mapped_column(JSON)
    lesson_id: Mapped[str | None] = mapped_column(String(36))
    course: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    revision: Mapped[str | None] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    problem: Mapped[str | None]
    blocked_on: Mapped[date | None] = mapped_column(Date)
    budget_reason: Mapped[str | None]


class NoteCheckBatch(Base):
    __tablename__ = "note_check_batches"
    __table_args__ = (
        UniqueConstraint("plan_id", "revision", "start"),
        Index("ix_note_check_batches_cache", "plan_id", "digest", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[str] = mapped_column(String(64))
    start: Mapped[int]
    end: Mapped[int]
    digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str]
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    error: Mapped[str | None]
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    billing_day: Mapped[date | None] = mapped_column(Date, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status: Mapped[str | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]


class NoteVersion(Base):
    __tablename__ = "note_versions"

    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime]
    source: Mapped[str]
    seen_on_disk: Mapped[bool]


class NoteConflict(Base):
    __tablename__ = "note_conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str]
    created_at: Mapped[datetime]
    base_revision: Mapped[str | None]
    external_revision: Mapped[str | None]
    draft_revision: Mapped[str]
    resolution: Mapped[str | None]


class NoteWrite(Base):
    __tablename__ = "note_writes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    expected_revision: Mapped[str | None]
    desired_revision: Mapped[str]
    resolution_id: Mapped[str | None]
    resolution: Mapped[str | None]
    completed: Mapped[bool]
    result_conflict_id: Mapped[str | None]
    captured_revision: Mapped[str | None]
    captured_signature: Mapped[str | None]
    target_path: Mapped[str | None]


class LibraryLocation(Base):
    __tablename__ = "library_locations"

    id: Mapped[str] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(unique=True)
    file_key: Mapped[str | None]
    birth_time: Mapped[str | None]
    revision: Mapped[str | None]
    missing: Mapped[bool]
    trash_id: Mapped[str | None]


class LibraryFileCopy(Base):
    __tablename__ = "library_file_copies"

    id: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str]
    target: Mapped[str]
    directory_key: Mapped[str]
    confirmation: Mapped[str]
    stage: Mapped[str]
    file_key: Mapped[str]
    birth_time: Mapped[str | None]
    revision: Mapped[str]
    created_on: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(index=True)
    error: Mapped[str | None]


class LibraryTrash(Base):
    __tablename__ = "library_trash"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str] = mapped_column(index=True)
    name: Mapped[str]
    kind: Mapped[str]
    format: Mapped[str]
    project_id: Mapped[str | None]
    project_topic: Mapped[str | None]
    original_path: Mapped[str]
    deleted_at: Mapped[datetime]
    revision: Mapped[str]
    file_key: Mapped[str | None]
    birth_time: Mapped[str | None]
    state: Mapped[str] = mapped_column(index=True)
    restore_path: Mapped[str | None]
    error: Mapped[str | None]



class LibraryGraph(Base):
    __tablename__ = "library_graph"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int]
    head: Mapped[int | None]
    redo: Mapped[list[int]] = mapped_column(JSON)
    anchors: Mapped[dict] = mapped_column(JSON)
    document: Mapped[dict] = mapped_column(JSON)
    view: Mapped[dict] = mapped_column(JSON)


class LibraryGraphRevision(Base):
    __tablename__ = "library_graph_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent: Mapped[int | None]
    label: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    document: Mapped[dict] = mapped_column(JSON)



class ScopedRecommendationColumns:
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    confirmation: Mapped[str] = mapped_column(String(64))
    connection: Mapped[dict] = mapped_column(JSON)
    scope: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    state: Mapped[str]
    error: Mapped[str | None]
    result_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    daily_blocked_on: Mapped[date | None] = mapped_column(Date)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    billing_day: Mapped[date | None] = mapped_column(Date, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status: Mapped[str | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]


class GoalAuthoringJob(ScopedRecommendationColumns, Base):
    __tablename__ = "goal_authoring_jobs"
    __table_args__ = (
        Index("ix_goal_authoring_queue", "state", "created_at"),
        Index("ix_goal_authoring_thread", "conversation_id", "goal_id", "created_at"),
    )

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    goal_id: Mapped[str] = mapped_column(String(36))
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class AgentAuthoringJob(ScopedRecommendationColumns, Base):
    __tablename__ = "agent_authoring_jobs"
    __table_args__ = (Index("ix_agent_authoring_queue", "state", "created_at"),)

    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class MemoryInferenceJob(ScopedRecommendationColumns, Base):
    __tablename__ = "memory_inference_jobs"
    __table_args__ = (Index("ix_memory_inference_queue", "state", "created_at"),)

    source_message_id: Mapped[str] = mapped_column(String(36), index=True)
    memory_id: Mapped[str | None] = mapped_column(String(36))
    automatic: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))


class GraphRecommendation(ScopedRecommendationColumns, Base):
    __tablename__ = "graph_recommendations"
    __table_args__ = (Index("ix_graph_recommendations_queue", "state", "created_at"),)


class LibraryClassification(ScopedRecommendationColumns, Base):
    __tablename__ = "library_classifications"
    __table_args__ = (Index("ix_library_classifications_queue", "state", "created_at"),)

    auto_apply: Mapped[bool]
    file_count: Mapped[int]


class LibraryClassificationCandidate(Base):
    __tablename__ = "library_classification_candidates"
    __table_args__ = (Index("ix_library_classification_candidates_queue", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("library_classifications.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str]
    basis: Mapped[dict] = mapped_column(JSON)
    suggestion: Mapped[dict] = mapped_column(JSON)
    with_excerpts: Mapped[bool]
    state: Mapped[str]
    approved: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class GraphCandidate(Base):
    __tablename__ = "graph_candidates"
    __table_args__ = (Index("ix_graph_candidates_queue", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("graph_recommendations.id", ondelete="CASCADE"))
    kind: Mapped[str]
    identity: Mapped[str] = mapped_column(String(64), index=True)
    presentation: Mapped[dict] = mapped_column(JSON)
    basis: Mapped[dict] = mapped_column(JSON)
    with_excerpts: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


def open_db(folder: Path, *, workspace_cost_limit: MoneyLimit | None = None) -> Engine:
    folder.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite", database=str(folder / "zhixue.sqlite3")))
    if workspace_cost_limit is not None:
        engine.update_execution_options(workspace_cost_limit=workspace_cost_limit.model_dump())

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.create_function("unicode_fold", 1, str.casefold, deterministic=True)

    return engine


def save_upgrade_backup(database: Path) -> None:
    backups = database.parent / "backups"
    folder = backups / "database-upgrades"
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = folder / f"{name}-{uuid4().hex[:8]}.sqlite3"
    with NamedTemporaryFile(dir=folder, prefix=".backup-", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        # A separate read connection copies committed pages while the upgrader holds the writer lock.
        uri = sqlite_file_uri(database.resolve(), mode="ro")
        with closing(sqlite3.connect(uri, uri=True)) as source, closing(sqlite3.connect(temporary)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE").fetchone()
            if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("升级前数据库备份未通过完整性检查。")
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        temporary.replace(destination)
        # Persist the snapshot name and any newly created directory entries before migrating.
        for directory in (folder, backups, database.parent):
            sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def upgrade(engine: Engine) -> None:
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL").close()
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = MigrationContext.configure(connection).get_current_heads()
        target = ScriptDirectory.from_config(config).get_heads()
        has_tables = connection.exec_driver_sql(
            "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name NOT GLOB 'sqlite_*')"
        ).scalar_one()
        if set(current) != set(target) and has_tables:
            save_upgrade_backup(Path(engine.url.database))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()


@contextmanager
def transaction(engine: Engine, *, write: bool = True):
    with Session(engine) as db, db.begin():
        db.execute(text("BEGIN IMMEDIATE" if write else "BEGIN"))
        yield db
