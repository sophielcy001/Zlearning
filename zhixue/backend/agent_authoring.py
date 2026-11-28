from datetime import datetime
from hmac import compare_digest
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from .chat_model import REPLY_ERRORS
from .db import AgentAuthoringJob, transaction
from .mobile_api import fingerprint, workspace_id
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_ledger import request_model
from .model_protocols import Transport, post_json
from .model_study import utc_now, when_utc
from .model_study_input import Consent, Input, connection_view, parse_model_json, pinned_connection, preview_consent, verify_consent
from .model_usage import admit_reserved_call
from .professional_agent import Instructions, Title
from .scoped_recommendation_worker import ScopedClaim, block, cancel_scoped, finish_scoped, reserve_scoped, settle_scoped


MAX_OUTPUT_TOKENS = 900


class DraftRequest(Input):
    id: UUID
    purpose: Instructions


class ConfirmedDraft(DraftRequest):
    consent: Consent


class AgentSuggestion(Input):
    name: Title
    responsibility: Instructions


def draft_view(job: AgentAuthoringJob) -> dict:
    return {
        "id": job.id, "state": job.state, "suggestion": job.result, "createdAt": job.created_at,
        "purpose": (job.scope or job.result or {}).get("purpose"),
        "errorCode": job.error, "error": REPLY_ERRORS.get(job.error, "暂时无法生成 Agent 草稿。") if job.error else None,
    }


def agent_authoring_router(engine, now, session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/agent-authoring")
    secret = session_token.encode("utf-8")

    def identity(db, draft):
        return f"agent-authoring/{workspace_id(db)}/{draft.id}"

    def required(db, job_id: UUID):
        job = db.get(AgentAuthoringJob, str(job_id))
        if job is None:
            raise HTTPException(404, "找不到这份 Agent 草稿。")
        return job

    @router.get("")
    def drafts():
        with transaction(engine, write=False) as db:
            jobs = db.scalars(select(AgentAuthoringJob).where(AgentAuthoringJob.state != "cancelled").order_by(
                AgentAuthoringJob.created_at.desc(), AgentAuthoringJob.id.desc(),
            ))
            return {"items": [draft_view(job) for job in jobs]}

    @router.post("/preview")
    def preview(draft: DraftRequest):
        with transaction(engine, write=False) as db:
            scope = {"purpose": draft.purpose}
            prepared = preview_consent(db, identity(db, draft), [scope], 1, secret)
            return {"recipient": prepared["connection"], "consent": prepared["consent"], **scope}

    @router.post("", status_code=202)
    def queue(draft: ConfirmedDraft):
        if draft.consent.maxCalls != 1 or draft.consent.allowBackup:
            raise HTTPException(409, "请重新确认这次起草的发送范围。")
        submitted = fingerprint(draft.model_dump(mode="json"))
        with transaction(engine) as db:
            existing = db.get(AgentAuthoringJob, str(draft.id))
            if existing:
                if not compare_digest(existing.confirmation, submitted):
                    raise HTTPException(409, "这次起草的请求已使用，请重新预览。")
                return draft_view(existing)
            scope = {"purpose": draft.purpose}
            model, _ = verify_consent(db, identity(db, draft), [scope], draft.consent, secret)
            job = AgentAuthoringJob(id=str(draft.id), confirmation=submitted, connection=connection_view(model),
                                    scope=scope, state="queued", created_at=when_utc(now()))
            db.add(job)
            return draft_view(job)

    @router.get("/{job_id}")
    def status(job_id: UUID):
        with transaction(engine, write=False) as db:
            return draft_view(required(db, job_id))

    @router.delete("/{job_id}")
    def discard(job_id: UUID):
        with transaction(engine) as db:
            job = required(db, job_id)
            if job.state in {"ready", "failed"}:
                job.state, job.result, job.error = "cancelled", None, None
            else:
                cancel_scoped(job, when_utc(now()))
            return draft_view(job)

    return router


class AgentAuthoringClaim(ScopedClaim):
    pass


def reserve_agent_authoring(engine, now: datetime) -> AgentAuthoringClaim | None:
    return reserve_scoped(engine, AgentAuthoringJob, AgentAuthoringClaim, now)


def settle_agent_authoring(engine, now: datetime) -> None:
    settle_scoped(engine, AgentAuthoringJob, now)


def admit_agent_authoring(engine, claim, now) -> str | None:
    with transaction(engine) as db:
        job = db.get(AgentAuthoringJob, claim.id)
        if not job or job.state != "running" or job.status != "reserved" or job.finished_at is not None:
            return None
        if pinned_connection(db, "main", job.connection) is None:
            finish_scoped(job, "configuration_changed", None, when_utc(now()), sent=False)
            return None
        prompt = (
            '为用户的学习需求准备可编辑的专业 Agent 草稿。只返回 JSON 对象 {"name":"名称","responsibility":"职责"}。'
            "使用需求的语言，名称简短，职责说明具体教学方法及适用主题。"
            "职责只能描述如何组织学习对话，不得建议权限、工具、外部操作或冒充已经完成了学习任务。\n需求描述 JSON\n"
            + json.dumps(job.scope, ensure_ascii=False)
        )
        if issue := admit_reserved_call(db, job, now, prompt, MAX_OUTPUT_TOKENS):
            block(job, job.billing_day, issue)
            return None
        return prompt


def complete_agent_authoring(engine, claim, status, response, usage, now, *, sent: bool) -> None:
    with transaction(engine) as db:
        job = db.get(AgentAuthoringJob, claim.id)
        if not job or job.finished_at is not None or job.status not in {"reserved", "sending"}:
            return
        if job.state == "running" and status == "passed":
            if response is None:
                status = "invalid_response"
            else:
                job.result = {**response, "purpose": job.scope["purpose"]}
                job.result_count = 1
        finish_scoped(job, status, usage, when_utc(now), sent=sent and job.status == "sending")


def dispatch_agent_authoring(engine, claim: AgentAuthoringClaim, *, vault: CredentialStore | None = None,
                             transport: Transport = post_json, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_agent_authoring(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_agent_authoring(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    prompt = admit_agent_authoring(engine, claim, now)
    if prompt is None:
        return
    reply = request_model(engine, AgentAuthoringJob, claim.id, claim, key, prompt, MAX_OUTPUT_TOKENS, transport, now)
    status, parsed = reply.status, None
    if status == "passed":
        try:
            if key and key in reply.text:
                raise ValueError("Credential appeared in model output.")
            parsed = parse_model_json(AgentSuggestion, reply.text)
        except ValueError:
            status = "invalid_response"
    complete_agent_authoring(engine, claim, status, parsed, reply.usage, now(), sent=True)
