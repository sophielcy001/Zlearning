from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from .course_review import same_model
from .db import NoteCheckBatch, NoteCheckPlan, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_protocols import TextReply, Transport, post_json
from .model_study import settings_view, utc_now, when_utc
from .model_study_input import connection_view, main_connection, parse_model_json, pinned_connection
from .model_ledger import record_call_usage, request_model
from .model_usage import active_calls, admit_reserved_call, auto_resume_enabled, billed_calls, billing_day, daily_capacity
from .money_budget import DAILY_BUDGET_REASONS, request_issue
from .note_checks import (
    CheckReply, check_prompt, course_for_check, findings_for, next_check_text, prepare_batches, read_check_note, record_snapshot,
    suspend_unavailable,
)
from .notes import NoteFileError, note_snapshot, version_data


MAX_OUTPUT_TOKENS = 4000


@dataclass(frozen=True)
class NoteCheckClaim:
    id: str
    model: str
    provider: str
    protocol: str
    address: str
    credential: str | None


def reviewer(db, plan):
    model = pinned_connection(db, "review", plan.recipient)
    main = main_connection(db)
    if model and (not main or not same_model(plan.recipient, connection_view(main))):
        return model
    return None


def note_check_amount_issue(db, plan: NoteCheckPlan, day) -> str | None:
    current = reviewer(db, plan)
    if current is None:
        return "cost_unavailable"
    text = next_check_text(db, plan)
    return request_issue(db, "note_check", plan.budget_scope_id or plan.id, day, current,
                         check_prompt(text, plan.course), MAX_OUTPUT_TOKENS) if text is not None else None


def reserve_note_check(engine, now: datetime) -> NoteCheckClaim | None:
    clock, day = when_utc(now), billing_day(now)
    ready = NoteCheckPlan.problem.is_(None), NoteCheckPlan.available_at <= clock
    with transaction(engine, write=False) as db:
        if db.scalar(select(NoteCheckPlan.note_id).where(*ready).limit(1)) is None:
            return None
    with transaction(engine) as db:
        settings = settings_view(db)
        if active_calls(db) >= settings["concurrency"]:
            return None
        used = billed_calls(db, day)
        plans = db.scalars(select(NoteCheckPlan).where(*ready).order_by(NoteCheckPlan.available_at, NoteCheckPlan.note_id))
        for plan in plans:
            if plan.blocked_on is not None:
                if not (plan.blocked_on < day and (plan.budget_reason or "daily_budget") in DAILY_BUDGET_REASONS
                        and auto_resume_enabled(settings, used)):
                    continue
                plan.blocked_on = plan.budget_reason = None
            model = reviewer(db, plan)
            if model is None:
                plan.problem = "configuration_changed"
                continue
            busy = db.scalar(select(NoteCheckBatch.id).where(
                NoteCheckBatch.plan_id == plan.id, NoteCheckBatch.state == "running",
            ).limit(1))
            if busy:
                continue
            saved = version_data(db, plan.note_id, plan.revision)
            if saved is None:
                plan.problem = "note_unavailable"
                continue
            batch = next((part for part in prepare_batches(db, plan, saved["content"]) if part.state == "queued"), None)
            if batch is None:
                plan.available_at = None
                continue
            if not daily_capacity(settings["dailyLimit"], used):
                plan.blocked_on, plan.budget_reason = day, "daily_budget"
                continue
            batch.state, batch.status, batch.started_at, batch.billing_day = "running", "reserved", clock, day
            return NoteCheckClaim(batch.id, model.model, model.provider, model.protocol,
                                  plan.recipient["address"], model.credential_ref)
    return None


def finish(batch, outcome, result, usage, clock, *, sent):
    batch.status, batch.finished_at = outcome if sent else "not_sent", clock
    record_call_usage(batch, usage)
    if result is None:
        batch.state, batch.error = "failed", outcome
    else:
        batch.state, batch.result = "done", result


def complete_note_check(engine, claim: NoteCheckClaim, status: str, result: dict | None, usage: dict | None,
                        now: datetime, *, sent: bool) -> None:
    with transaction(engine) as db:
        batch = db.get(NoteCheckBatch, claim.id)
        if batch and batch.finished_at is None and batch.state == "running":
            finish(batch, status, result, usage, when_utc(now), sent=sent and batch.status == "sending")


def reset_reservation(batch):
    batch.state, batch.status = "queued", None
    batch.started_at, batch.billing_day, batch.finished_at = None, None, None


def admit_check(engine, claim: NoteCheckClaim, now: Callable[[], datetime]) -> tuple[str, dict | None] | None:
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        clock = when_utc(now())
        batch = db.get(NoteCheckBatch, claim.id)
        if batch is None or batch.state != "running" or batch.status != "reserved":
            return None
        plan = db.scalar(select(NoteCheckPlan).where(NoteCheckPlan.id == batch.plan_id))
        if plan is None or plan.revision != batch.revision:
            db.delete(batch)
            return None
        if reviewer(db, plan) is None:
            plan.problem = "configuration_changed"
        if plan.problem is not None:
            finish(batch, plan.problem, None, None, clock, sent=False)
            return None
        try:
            location, signature = read_check_note(LocationTracker(db, folder), plan.note_id)
            snapshot = note_snapshot(location.path) if signature is not None else None
        except (NoteFileError, InvalidLocation, LocationConflict, OSError):
            snapshot = None
        if snapshot is None:
            db.delete(batch)
            suspend_unavailable(db, plan, clock)
            return None
        if snapshot.revision != batch.revision:
            db.delete(batch)
            record_snapshot(db, plan, snapshot, clock)
            return None
        if plan.lesson_id:
            try:
                if course_for_check(db, folder, plan.note_id, plan.lesson_id) != plan.course:
                    plan.problem = "course_changed"
            except HTTPException:
                plan.problem = "course_changed"
            if plan.problem:
                finish(batch, plan.problem, None, None, clock, sent=False)
                return None
        text = snapshot.content.decode("utf-8")[batch.start:batch.end]
        prompt = check_prompt(text, plan.course)
        if issue := admit_reserved_call(db, batch, now, prompt, MAX_OUTPUT_TOKENS, recipient=plan.recipient,
                                        scope_id=plan.budget_scope_id or plan.id):
            plan.blocked_on, plan.budget_reason = batch.billing_day, issue
            reset_reservation(batch)
            return None
        return text, plan.course


def dispatch_note_check(engine, claim: NoteCheckClaim, *, vault: CredentialStore | None = None,
                        transport: Transport = post_json, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_note_check(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_note_check(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    material = admit_check(engine, claim, now)
    if material is None:
        return
    text, course = material
    reply = request_model(engine, NoteCheckBatch, claim.id, claim, key, check_prompt(text, course), MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    status, result = reply.status, None
    if status == "passed":
        try:
            result = findings_for(parse_model_json(CheckReply, reply.text), text, course)
        except ValueError:
            status = "invalid_response"
    complete_note_check(engine, claim, status, result, reply.usage, now(), sent=True)


def settle_note_checks(engine, now: datetime):
    with transaction(engine) as db:
        for batch in db.scalars(select(NoteCheckBatch).where(NoteCheckBatch.state == "running")):
            if batch.status == "reserved":
                reset_reservation(batch)
            else:
                finish(batch, "uncertain", None, None, when_utc(now), sent=True)
