from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, or_, select

from .course_publication import course_markdown, put_version
from .course_review import ReviewReply, assess
from .db import Lesson, PrerequisiteCall, PrerequisiteTask, Project, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path
from .library import FileTooLarge, local_path, prerequisite_file_id
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_protocols import TextReply, Transport, post_json
from .model_failover import TRANSIENT, choose_recipient, switch_after_failure
from .model_study import settings_view, utc_now, when_utc
from .model_study_input import connection_view, parse_model_json
from .model_ledger import record_call_usage, request_model
from .model_usage import (
    active_calls, admit_reserved_call, auto_resume_enabled, billed_calls, billing_day, daily_capacity,
    queued_with_daily_recovery, settle_interrupted_use,
)
from .prerequisite import (
    PrerequisiteDraft, authorization_issue, existing_course, prerequisite_scope, prompt_for, usage, verified_sections,
)


MAX_OUTPUT_TOKENS = 4096


@dataclass(frozen=True)
class PrerequisiteClaim:
    id: str
    task_id: str
    phase: str
    model: str
    address: str
    provider: str
    protocol: str
    credential: str | None
    prompt: str


def block(task: PrerequisiteTask, reason: str) -> None:
    task.state, task.reason, task.available_at, task.active_call = "blocked", reason, None, None


def queued(db, clock: datetime, *, publish: bool, recover: bool = False):
    phase = PrerequisiteTask.phase
    pending = queued_with_daily_recovery(PrerequisiteTask.state, PrerequisiteTask.reason,
                                         PrerequisiteTask.daily_blocked_on, billing_day(clock), recover)
    return db.scalars(select(PrerequisiteTask).join(Lesson, Lesson.id == PrerequisiteTask.lesson_id).join(
        Project, Project.id == Lesson.project_id,
    ).where(
        pending, Project.paused.is_(False),
        phase == "publish" if publish else phase.in_(("draft", "review")),
        or_(PrerequisiteTask.available_at.is_(None), PrerequisiteTask.available_at <= clock),
    ).order_by(PrerequisiteTask.created_at, PrerequisiteTask.id)).all()


def has_formal_course(db, folder: Path, task: PrerequisiteTask) -> bool:
    target = db.get(Lesson, task.target_lesson_id) if task.target_lesson_id else None
    return existing_course(db, folder, target) is not None


def reserve(engine, now: datetime) -> PrerequisiteClaim | None:
    clock = when_utc(now)
    day = billing_day(clock)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        settings = settings_view(db)
        if active_calls(db) >= settings["concurrency"]:
            return None
        daily = billed_calls(db, day)
        for task in queued(db, clock, publish=False, recover=auto_resume_enabled(settings, daily)):
            if has_formal_course(db, folder, task):
                task.state, task.reason = "superseded", None
                continue
            if issue := authorization_issue(db, folder, task):
                block(task, issue)
                continue
            if task.max_calls is not None and usage(db, task.id) >= task.max_calls:
                block(task, "task_budget")
                continue
            if not daily_capacity(settings["dailyLimit"], daily):
                task.daily_blocked_on = day
                block(task, "daily_budget")
                continue
            recipient = choose_recipient(db, prerequisite_scope(task), task.failovers)
            model = recipient.connection
            reference = connection_view(model)
            identity = str(uuid4())
            db.add(PrerequisiteCall(
                id=identity, task_id=task.id, phase=task.phase, connection=reference, started_at=clock,
                billing_day=day, status="reserved", auto_retry=False, failover_reason=recipient.failover_reason,
            ))
            task.state, task.reason, task.active_call, task.daily_blocked_on = "running", None, identity, None
            return PrerequisiteClaim(identity, task.id, task.phase, model.model, reference["address"], model.provider,
                                     model.protocol, model.credential_ref, prompt_for(task))
    return None


def complete(engine, claim: PrerequisiteClaim, status: str, report: dict | None,
             counters: dict | None, now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        call = db.get(PrerequisiteCall, claim.id)
        task = db.get(PrerequisiteTask, claim.task_id)
        if not call or call.finished_at is not None or not task or task.active_call != call.id:
            return
        call.finished_at, call.status = clock, status if sent else "not_sent"
        record_call_usage(call, counters)
        task.active_call, task.available_at = None, None
        stopped = task.state == "stopped"
        if report is None:
            if stopped:
                return
            retries = db.scalar(select(func.count()).select_from(PrerequisiteCall).where(
                PrerequisiteCall.task_id == task.id, PrerequisiteCall.auto_retry.is_(True),
            ))
            switched = switch_after_failure(db, prerequisite_scope(task), task, call, status, retries)
            if switched or (status in TRANSIENT and retries < 2):
                call.auto_retry = True
                task.state, task.reason = "queued", status
                task.available_at = None if switched else clock + timedelta(seconds=2 + 4 * retries)
            else:
                block(task, status)
            return
        issue = authorization_issue(db, folder, task)
        if claim.phase == "draft":
            task.draft, task.author = report, call.connection
            task.phase = "review"
        else:
            task.findings, task.reviewer = report["findings"], call.connection
            if verified_sections(task):
                task.phase = "publish"
            else:
                issue = issue or "not_verified"
        if stopped:
            return
        if issue:
            block(task, issue)
        else:
            task.state, task.reason = "queued", None


def dispatch(engine, claim: PrerequisiteClaim, *, transport: Transport = post_json,
             vault: CredentialStore | None = None, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        call = db.get(PrerequisiteCall, claim.id)
        task = db.get(PrerequisiteTask, claim.task_id)
        if not call or call.status != "reserved" or call.finished_at is not None or not task or task.active_call != call.id:
            return
        lesson = db.get(Lesson, task.lesson_id)
        project = db.get(Project, lesson.project_id)
        clock = when_utc(now())
        if task.state == "stopped" or project.paused or has_formal_course(db, folder, task):
            call.status, call.finished_at = "not_sent", clock
            task.active_call = None
            if task.state != "stopped":
                task.state = "queued" if project.paused else "superseded"
            return
        issue = authorization_issue(db, folder, task)
        if not issue:
            recipient = choose_recipient(db, prerequisite_scope(task), task.failovers)
            if connection_view(recipient.connection) != call.connection:
                issue = "configuration_changed"
        if not issue and task.max_calls is not None and usage(db, task.id) > task.max_calls:
            issue = "task_budget"
        if not issue and (budget_issue := admit_reserved_call(db, call, now, claim.prompt, MAX_OUTPUT_TOKENS)):
            task.daily_blocked_on = call.billing_day
            block(task, budget_issue)
            return
        if not issue:
            draft, sources = task.draft, task.scope["sources"]
    if issue:
        complete(engine, claim, issue, None, None, clock, sent=False)
        return
    reply = request_model(engine, PrerequisiteCall, claim.id, claim, key, claim.prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    status, report = reply.status, None
    if status == "passed":
        try:
            if claim.phase == "draft":
                report = parse_model_json(PrerequisiteDraft, reply.text)
            else:
                findings = parse_model_json(ReviewReply, reply.text)
                report = {"findings": assess(findings, draft, sources)}
        except ValueError:
            status = "invalid_response"
    complete(engine, claim, status, report, reply.usage, now(), sent=True)


def publish_one(engine, folder: Path, now=utc_now) -> bool:
    clock = when_utc(now())
    with transaction(engine) as db:
        tasks = queued(db, clock, publish=True)
        if not tasks:
            return False
        task = tasks[0]
        if has_formal_course(db, folder, task):
            task.state, task.reason = "superseded", None
            return True
        if issue := authorization_issue(db, folder, task):
            block(task, issue)
            return True
        content = course_markdown(f"{task.topic} · 前置知识", verified_sections(task)).encode("utf-8")
        revision = sha256(content).hexdigest()
        file_id = prerequisite_file_id(task.id)
        try:
            relative = local_path(folder, "supplements", file_id).relative_to(folder).as_posix()
            path = checked_path(folder, relative)
            identity = put_version(path, content, revision)
            LocationTracker(db, folder).bind(file_id, path, identity, revision)
        except (OSError, InvalidLocation, LocationConflict, FileTooLarge):
            block(task, "publication_failed")
        else:
            task.revision, task.published_at = revision, clock
            task.state, task.reason = "ready", None
        return True


def process_one(engine, *, transport: Transport = post_json, vault: CredentialStore | None = None, now=utc_now) -> bool:
    claim = reserve(engine, now())
    if claim is None:
        return False
    dispatch(engine, claim, transport=transport, vault=vault, now=now)
    return True


def settle_interrupted(engine, now: datetime) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        for call in db.scalars(select(PrerequisiteCall).where(PrerequisiteCall.finished_at.is_(None))):
            unsent = settle_interrupted_use(call, clock)
            task = db.get(PrerequisiteTask, call.task_id)
            if task and task.active_call == call.id:
                task.active_call = None
                if task.state == "stopped":
                    continue
                if unsent:
                    task.state = "queued"
                else:
                    block(task, "uncertain")
