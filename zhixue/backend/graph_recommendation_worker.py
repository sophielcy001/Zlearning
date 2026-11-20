from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .db import GraphRecommendation, transaction
from .graph_catalog import graph_catalog
from .graph_recommendations import RecommendationReply, Unavailable, build_scope, prompt_for_scope, stage_candidates
from .graph_state import GraphDocument, workspace
from .library_index import LibraryIndex
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_ledger import request_model
from .model_protocols import TextReply, Transport, post_json
from .model_study import utc_now, when_utc
from .model_study_input import parse_model_json, pinned_connection
from .model_usage import admit_reserved_call
from .scoped_recommendation_worker import ScopedClaim, block, finish_scoped as finish, reserve_scoped, settle_scoped


MAX_OUTPUT_TOKENS = 2800


class RecommendationClaim(ScopedClaim):
    pass


def reserve_recommendation(engine, now: datetime) -> RecommendationClaim | None:
    return reserve_scoped(engine, GraphRecommendation, RecommendationClaim, now)


def current_scope(db, folder: Path, job: GraphRecommendation):
    nodes, automatic, _ = graph_catalog(db, folder, None)
    document = GraphDocument.model_validate(workspace(db).document)
    requested = [job.scope["bindings"][node["key"]]["id"] for node in job.scope["material"]["nodes"]]
    try:
        fresh = build_scope(db, folder, nodes, automatic, document, requested, job.scope["withExcerpts"])
    except Unavailable:
        return None
    return (document, automatic) if fresh == job.scope else None


def admit(engine, claim: RecommendationClaim, folder: Path, now: Callable[[], datetime]) -> str | None:
    with transaction(engine) as db:
        clock = when_utc(now())
        job = db.get(GraphRecommendation, claim.id)
        if not job or job.state != "running" or job.status != "reserved" or job.finished_at is not None:
            return None
        if pinned_connection(db, "main", job.connection) is None:
            finish(job, "configuration_changed", None, clock, sent=False)
        elif current_scope(db, folder, job) is None:
            finish(job, "scope_changed", None, clock, sent=False)
        else:
            prompt = prompt_for_scope(job.scope)
            if issue := admit_reserved_call(db, job, now, prompt, MAX_OUTPUT_TOKENS):
                block(job, job.billing_day, issue)
            else:
                return prompt
    return None


def complete_recommendation(engine, claim: RecommendationClaim, status: str, result: dict | None, usage: dict | None,
                            now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        job = db.get(GraphRecommendation, claim.id)
        if not job or job.finished_at is not None or job.status not in {"reserved", "sending"}:
            return
        if job.state == "running" and status == "passed":
            current = current_scope(db, folder, job) if result is not None else None
            if current is not None:
                document, automatic = current
                job.result_count = stage_candidates(db, job, result, document, automatic, clock)
            else:
                status = "scope_changed" if result is not None else "invalid_response"
        finish(job, status, usage, clock, sent=sent and job.status == "sending")


def dispatch_recommendation(engine, claim: RecommendationClaim, *, vault: CredentialStore | None = None,
                            transport: Transport = post_json, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_recommendation(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_recommendation(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    folder = Path(engine.url.database).resolve().parent
    index = LibraryIndex(engine, folder)
    index.refresh()
    prompt = admit(engine, claim, folder, now)
    if prompt is None:
        return

    reply = request_model(engine, GraphRecommendation, claim.id, claim, key, prompt, MAX_OUTPUT_TOKENS, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    status, result = reply.status, None
    if status == "passed":
        try:
            result = parse_model_json(RecommendationReply, reply.text)
        except ValueError:
            status = "invalid_response"
        if result is not None:
            index.refresh()
    complete_recommendation(engine, claim, status, result, reply.usage, now(), sent=True)


def settle_recommendations(engine, now: datetime) -> None:
    settle_scoped(engine, GraphRecommendation, now)
