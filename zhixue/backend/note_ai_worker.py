from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from .db import NoteSuggestion, transaction
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_protocols import TextReply, Transport, post_json
from .model_study import settings_view, utc_now, when_utc
from .model_study_input import parse_model_json, pinned_connection
from .model_ledger import record_call_usage, request_model
from .model_usage import (
    active_calls, admit_reserved_call, auto_resume_enabled, billed_calls, billing_day, daily_capacity,
    queued_with_daily_recovery,
)
from .note_ai import SuggestionReply, course_context, fail, model_prompt, note_available


MAX_OUTPUT_TOKENS = 3500


@dataclass(frozen=True)
class NoteAIClaim:
    id: str
    model: str
    provider: str
    protocol: str
    address: str
    credential: str | None


def block(job: NoteSuggestion, reason: str, day) -> None:
    job.state, job.error, job.daily_blocked_on = "blocked", reason, day


def reserve_note_ai(engine, now: datetime) -> NoteAIClaim | None:
    clock, day = when_utc(now), billing_day(now)
    with transaction(engine) as db:
        settings = settings_view(db)
        if active_calls(db) >= settings["concurrency"]:
            return None
        used = billed_calls(db, day)
        pending = select(NoteSuggestion).where(queued_with_daily_recovery(
            NoteSuggestion.state, NoteSuggestion.error, NoteSuggestion.daily_blocked_on, day,
            auto_resume_enabled(settings, used),
        )).order_by(
            NoteSuggestion.created_at, NoteSuggestion.id,
        )
        for job in db.scalars(pending):
            model = pinned_connection(db, "main", job.connection)
            if model is None:
                fail(job, "configuration_changed")
                continue
            if not note_available(db, job.note_id):
                fail(job, "note_unavailable")
                continue
            if not daily_capacity(settings["dailyLimit"], used):
                block(job, "daily_budget", day)
                continue
            job.state, job.started_at, job.billing_day, job.status = "running", clock, day, "reserved"
            job.error, job.finished_at, job.daily_blocked_on = None, None, None
            return NoteAIClaim(job.id, model.model, model.provider, model.protocol, job.connection["address"],
                               model.credential_ref)
    return None


def finish(job: NoteSuggestion, status: str, markdown: str | None, usage: dict | None,
           clock: datetime, *, sent: bool) -> None:
    job.status, job.finished_at = status if sent else "not_sent", clock
    record_call_usage(job, usage)
    if job.state == "dismissed":
        return
    if markdown is None:
        fail(job, status)
    else:
        job.state, job.result, job.context = "ready", markdown, None


def complete_note_ai(engine, claim: NoteAIClaim, status: str, markdown: str | None, usage: dict | None,
                     now: datetime, *, sent: bool) -> None:
    with transaction(engine) as db:
        job = db.get(NoteSuggestion, claim.id)
        if job and job.finished_at is None and job.status in {"reserved", "sending"}:
            finish(job, status, markdown, usage, when_utc(now), sent=sent and job.status == "sending")


def admit(engine, claim: NoteAIClaim, now: Callable[[], datetime]) -> str | None:
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        job = db.get(NoteSuggestion, claim.id)
        if not job or job.state != "running" or job.status != "reserved" or job.finished_at is not None:
            return None
        issue = None
        if pinned_connection(db, "main", job.connection) is None:
            issue = "configuration_changed"
        elif not note_available(db, job.note_id):
            issue = "note_unavailable"
        elif (lesson := job.context["lessonId"]) is not None:
            try:
                if course_context(db, folder, job.note_id, lesson) != job.context["course"]:
                    issue = "course_changed"
            except HTTPException:
                issue = "course_changed"
        if issue is not None:
            finish(job, issue, None, None, when_utc(now()), sent=False)
        else:
            prompt = model_prompt(job.operation, job.context)
            issue = admit_reserved_call(db, job, now, prompt, MAX_OUTPUT_TOKENS)
            if issue is None:
                return prompt
            block(job, issue, job.billing_day)
    return None


def dispatch_note_ai(engine, claim: NoteAIClaim, *, vault: CredentialStore | None = None,
                     transport: Transport = post_json, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_note_ai(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_note_ai(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    prompt = admit(engine, claim, now)
    if prompt is None:
        return

    reply = request_model(engine, NoteSuggestion, claim.id, claim, key, prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    status, markdown = reply.status, None
    if status == "passed":
        try:
            markdown = parse_model_json(SuggestionReply, reply.text)["markdown"]
        except ValueError:
            status = "invalid_response"
    complete_note_ai(engine, claim, status, markdown, reply.usage, now(), sent=True)


def settle_interrupted_note_ai(engine, now: datetime) -> None:
    with transaction(engine) as db:
        calls = select(NoteSuggestion).where(
            NoteSuggestion.started_at.is_not(None), NoteSuggestion.finished_at.is_(None),
        )
        for job in db.scalars(calls):
            if job.state == "running" and job.status == "reserved":
                job.state, job.status, job.started_at, job.billing_day = "queued", None, None, None
            else:
                finish(job, "uncertain", None, None, when_utc(now), sent=job.status == "sending")
