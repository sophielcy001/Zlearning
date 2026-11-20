from hmac import compare_digest
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from .chat_api import DraftInput, DraftRevision, current_goal, digest, get_thread
from .db import GoalAuthoringJob, transaction
from .goal_authoring import GoalOptions, build_goal_scope, goal_job_view, suggestion_basis
from .model_study import when_utc
from .model_study_input import Consent, connection_view, preview_consent, verify_consent
from .scoped_recommendation_worker import ACTIVE_STATES, cancel_scoped


class QueueGoal(GoalOptions):
    consent: Consent


def goal_authoring_router(engine, folder, index, now, session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/conversations/{identity}/goal/ai")
    secret = session_token.encode()

    def same_goal(thread):
        active = thread.goal
        if active is None:
            raise HTTPException(409, "请先在对话中准备学习目标卡。")
        return GoalAuthoringJob.conversation_id == thread.id, GoalAuthoringJob.goal_id == active["id"]

    def owned_job(db, thread, job_id: UUID) -> GoalAuthoringJob:
        job = db.get(GoalAuthoringJob, str(job_id))
        if job is None or job.conversation_id != thread.id:
            raise HTTPException(404, "找不到这次目标整理。")
        return job

    @router.get("")
    def status(identity: UUID):
        with transaction(engine, write=False) as db:
            thread = get_thread(db, identity)
            job = None
            if thread.goal:
                job = db.scalars(select(GoalAuthoringJob).where(*same_goal(thread)).order_by(
                    GoalAuthoringJob.created_at.desc(), GoalAuthoringJob.id.desc(),
                ).limit(1)).first()
            return {"job": goal_job_view(job)}

    @router.post("/preview")
    def preview(identity: UUID, options: GoalOptions):
        if options.withExcerpts:
            index.refresh()
        with transaction(engine, write=False) as db:
            thread = get_thread(db, identity)
            scope = build_goal_scope(db, folder, thread, options)
            disclosed = preview_consent(db, f"goal-authoring/{thread.id}/{options.id}", [scope], 1, secret)
            return {
                "recipient": disclosed["connection"], "consent": disclosed["consent"],
                "material": scope["material"], "unavailable": scope["unavailable"],
            }

    @router.post("", status_code=202)
    def queue(identity: UUID, payload: QueueGoal):
        if payload.consent.maxCalls != 1 or payload.consent.allowBackup:
            raise HTTPException(409, "请重新确认本次目标整理的发送范围。")
        if payload.withExcerpts:
            index.refresh()
        with transaction(engine) as db:
            thread = get_thread(db, identity)
            request_fingerprint = digest({"thread": thread.id, "input": payload.model_dump(mode="json")})
            previous = db.get(GoalAuthoringJob, str(payload.id))
            if previous:
                if not compare_digest(previous.confirmation, request_fingerprint):
                    raise HTTPException(409, "这个整理请求标识已使用，请重新预览。")
                return goal_job_view(previous)
            active = db.scalar(select(GoalAuthoringJob.id).where(
                *same_goal(thread), GoalAuthoringJob.state.in_(ACTIVE_STATES),
            ).limit(1))
            if active:
                raise HTTPException(409, "这张目标卡已有整理请求，可以等待或取消后再试。")
            scope = build_goal_scope(db, folder, thread, payload)
            model, _ = verify_consent(db, f"goal-authoring/{thread.id}/{payload.id}", [scope], payload.consent, secret)
            job = GoalAuthoringJob(
                id=str(payload.id), conversation_id=thread.id, goal_id=str(payload.goalId),
                confirmation=request_fingerprint, connection=connection_view(model), scope=scope,
                state="queued", created_at=when_utc(now()),
            )
            db.add(job)
            return goal_job_view(job)

    @router.post("/revoke")
    def revoke(identity: UUID, revision: DraftRevision):
        with transaction(engine) as db:
            thread = get_thread(db, identity)
            goal = current_goal(thread, revision)
            moment = now()
            pending = db.scalars(select(GoalAuthoringJob).where(
                *same_goal(thread), GoalAuthoringJob.state.in_(ACTIVE_STATES),
            ))
            for job in pending:
                cancel_scoped(job, when_utc(moment))
            thread.goal = {**goal, "revision": goal["revision"] + 1}
            thread.updated_at = moment
            return thread.goal

    @router.post("/{job_id}/apply")
    def apply(identity: UUID, job_id: UUID, data: DraftInput):
        index.refresh()
        with transaction(engine) as db:
            thread = get_thread(db, identity)
            job = owned_job(db, thread, job_id)
            if thread.goal is None or thread.goal["id"] != job.goal_id:
                raise HTTPException(409, "这张目标卡已提交或变更。")
            fingerprint = digest(data.model_dump(mode="json"))
            if job.state == "applied":
                if job.result and compare_digest(job.result["appliedRequest"], fingerprint):
                    return thread.goal
                raise HTTPException(409, "这份目标草稿已经采用，请刷新后继续编辑。")
            if job.state != "ready" or not job.result:
                raise HTTPException(409, "这次整理还没有可采用的草稿。")
            options = GoalOptions(**data.model_dump(), id=job_id, withExcerpts=job.result["withExcerpts"])
            scope = build_goal_scope(db, folder, thread, options)
            if not compare_digest(suggestion_basis(scope), job.result["basis"]):
                raise HTTPException(409, "目标卡编辑或资料已变化，请重新预览整理范围。")
            thread.goal = {**scope["goal"], **job.result["suggestion"], "revision": data.revision + 1}
            thread.updated_at = now()
            job.state, job.result = "applied", {"appliedRequest": fingerprint}
            return thread.goal

    @router.delete("/{job_id}")
    def cancel(identity: UUID, job_id: UUID):
        with transaction(engine) as db:
            job = owned_job(db, get_thread(db, identity), job_id)
            if job.state == "ready":
                job.state, job.result = "cancelled", None
            else:
                cancel_scoped(job, when_utc(now()))
            return goal_job_view(job)

    return router
