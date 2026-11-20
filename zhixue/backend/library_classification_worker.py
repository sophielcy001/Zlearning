from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .db import LibraryClassification, transaction
from .library_classification import ClassificationReply, apply_approved, current_scope, prompt_for_classification, stage_candidates
from .library_index import LibraryIndex
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_ledger import request_model
from .model_protocols import TextReply, Transport, post_json
from .model_study import utc_now, when_utc
from .model_study_input import parse_model_json, pinned_connection
from .model_usage import admit_reserved_call
from .scoped_recommendation_worker import ScopedClaim, block, finish_scoped, reserve_scoped, settle_scoped


MAX_OUTPUT_TOKENS = 2800


class ClassificationClaim(ScopedClaim):
    pass


def reserve_classification(engine, now: datetime) -> ClassificationClaim | None:
    return reserve_scoped(engine, LibraryClassification, ClassificationClaim, now)


def admit(engine, claim: ClassificationClaim, workspace: Path, now: Callable[[], datetime]) -> str | None:
    with transaction(engine) as db:
        clock = when_utc(now())
        job = db.get(LibraryClassification, claim.id)
        if job is None or job.state != "running" or job.status != "reserved" or job.finished_at is not None:
            return None
        if pinned_connection(db, "main", job.connection) is None:
            finish_scoped(job, "configuration_changed", None, clock, sent=False)
        elif job.scope is None or not current_scope(db, workspace, job.scope):
            finish_scoped(job, "scope_changed", None, clock, sent=False)
        else:
            prompt = prompt_for_classification(job.scope)
            if issue := admit_reserved_call(db, job, now, prompt, MAX_OUTPUT_TOKENS):
                block(job, job.billing_day, issue)
            else:
                return prompt
    return None


def complete_classification(engine, claim: ClassificationClaim, status: str, result: dict | None, usage: dict | None,
                            now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    workspace = Path(engine.url.database).resolve().parent
    approved = []
    with transaction(engine) as db:
        job = db.get(LibraryClassification, claim.id)
        if job is None or job.finished_at is not None or job.status not in {"reserved", "sending"}:
            return
        if job.state == "running" and status == "passed":
            if result is None:
                status = "invalid_response"
            elif pinned_connection(db, "main", job.connection) is None:
                status = "configuration_changed"
            elif job.scope is None or not current_scope(db, workspace, job.scope):
                status = "scope_changed"
            else:
                candidates = stage_candidates(db, job, result, clock)
                job.result_count = len(candidates)
                if job.auto_apply:
                    approved = candidates
        finish_scoped(job, status, usage, clock, sent=sent and job.status == "sending")
    if approved:
        apply_approved(engine, workspace, LibraryIndex(engine, workspace), lambda: now, approved)


def dispatch_classification(engine, claim: ClassificationClaim, *, vault: CredentialStore | None = None,
                            transport: Transport = post_json, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_classification(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_classification(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    workspace = Path(engine.url.database).resolve().parent
    index = LibraryIndex(engine, workspace)
    index.refresh()
    prompt = admit(engine, claim, workspace, now)
    if prompt is None:
        return

    reply = request_model(engine, LibraryClassification, claim.id, claim, key, prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    status, result = reply.status, None
    if status == "passed":
        try:
            result = parse_model_json(ClassificationReply, reply.text)
        except ValueError:
            status = "invalid_response"
        if result is not None:
            index.refresh()
    complete_classification(engine, claim, status, result, reply.usage, now(), sent=True)


def settle_classifications(engine, now: datetime) -> None:
    settle_scoped(engine, LibraryClassification, now)
