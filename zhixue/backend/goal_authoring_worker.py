from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .db import GoalAuthoringJob, transaction
from .goal_authoring import GoalReply, create_suggestion, goal_prompt, scope_is_current
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_ledger import request_model
from .model_protocols import TextReply, Transport, post_json
from .model_study import utc_now, when_utc
from .model_study_input import parse_model_json, pinned_connection
from .model_usage import admit_reserved_call
from .scoped_recommendation_worker import ScopedClaim, block, finish_scoped, reserve_scoped, settle_scoped


MAX_OUTPUT_TOKENS = 2600


class GoalAuthoringClaim(ScopedClaim):
    pass


def reserve_goal_authoring(engine, now: datetime) -> GoalAuthoringClaim | None:
    return reserve_scoped(engine, GoalAuthoringJob, GoalAuthoringClaim, now)


def admit_goal_authoring(engine, claim: GoalAuthoringClaim, now: Callable[[], datetime]) -> str | None:
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        clock = when_utc(now())
        job = db.get(GoalAuthoringJob, claim.id)
        if not job or job.state != "running" or job.status != "reserved" or job.finished_at is not None:
            return None
        if pinned_connection(db, "main", job.connection) is None:
            finish_scoped(job, "configuration_changed", None, clock, sent=False)
        elif not scope_is_current(db, folder, job):
            finish_scoped(job, "scope_changed", None, clock, sent=False)
        else:
            prompt = goal_prompt(job.scope)
            if issue := admit_reserved_call(db, job, now, prompt, MAX_OUTPUT_TOKENS):
                block(job, job.billing_day, issue)
            else:
                return prompt
    return None


def complete_goal_authoring(engine, claim: GoalAuthoringClaim, status: str, response: dict | None, usage: dict | None,
                            now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        job = db.get(GoalAuthoringJob, claim.id)
        if not job or job.finished_at is not None or job.status not in {"reserved", "sending"}:
            return
        if job.state == "running" and status == "passed":
            if response is None:
                status = "invalid_response"
            elif pinned_connection(db, "main", job.connection) is None:
                status = "configuration_changed"
            elif not scope_is_current(db, folder, job):
                status = "scope_changed"
            else:
                try:
                    job.result = create_suggestion(job.scope, response)
                    job.result_count = len(job.result["suggestion"]["fields"])
                except ValueError:
                    status = "invalid_response"
        finish_scoped(job, status, usage, clock, sent=sent and job.status == "sending")


def dispatch_goal_authoring(engine, claim: GoalAuthoringClaim, *, vault: CredentialStore | None = None,
                            transport: Transport = post_json, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_goal_authoring(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_goal_authoring(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    prompt = admit_goal_authoring(engine, claim, now)
    if prompt is None:
        return
    reply = request_model(engine, GoalAuthoringJob, claim.id, claim, key, prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    status, response = reply.status, None
    if status == "passed":
        try:
            response = parse_model_json(GoalReply, reply.text)
        except ValueError:
            status = "invalid_response"
    complete_goal_authoring(engine, claim, status, response, reply.usage, now(), sent=True)


def settle_goal_authoring(engine, now: datetime) -> None:
    settle_scoped(engine, GoalAuthoringJob, now)
