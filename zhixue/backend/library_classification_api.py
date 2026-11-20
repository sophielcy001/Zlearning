from collections.abc import Callable
from datetime import datetime
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.engine import Engine

from .db import LibraryClassification, LibraryClassificationCandidate, transaction
from .library_classification import (
    CHOICES, MAX_FILES, MAX_FOLDERS, Unavailable, apply_approved, approve, build_scope, public_candidate, public_job,
)
from .library_index import LibraryIndex
from .library_metadata import FileId
from .model_study import when_utc
from .model_study_input import Consent, connection_view, preview_consent, verify_consent
from .scoped_recommendation_worker import ACTIVE_STATES, cancel_scoped


class ScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    fileIds: list[FileId] = Field(min_length=1, max_length=MAX_FILES)
    folders: list[Annotated[str, Field(min_length=1, max_length=1400)]] = Field(max_length=MAX_FOLDERS)
    withExcerpts: bool = Field(default=True, strict=True)
    autoApply: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def distinct(self):
        if len(self.fileIds) != len(set(self.fileIds)) or len(self.folders) != len(set(self.folders)):
            raise ValueError("请为本批整理选择不同的文件和目标文件夹。")
        return self


class QueueInput(ScopeInput):
    consent: Consent


class Decisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] = Field(min_length=1, max_length=100)
    choice: CHOICES | Literal["resume"]


def classification_router(engine: Engine, workspace: Path, index: LibraryIndex, now: Callable[[], datetime],
                          session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/files/classification")
    secret = session_token.encode()

    def requested_scope(db, payload: ScopeInput) -> dict:
        try:
            return build_scope(db, workspace, payload.fileIds, payload.folders, with_excerpts=payload.withExcerpts,
                               auto_apply=payload.autoApply)
        except Unavailable as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/preview")
    def preview(payload: ScopeInput):
        index.refresh()
        with transaction(engine, write=False) as db:
            scope = requested_scope(db, payload)
            disclosure = preview_consent(db, f"library-classification/{payload.id}", [scope], 1, secret)
            return {"recipient": disclosure["connection"], "consent": disclosure["consent"],
                    "autoApply": scope["autoApply"], **scope["material"]}

    @router.post("", status_code=202)
    def queue(payload: QueueInput):
        if payload.consent.maxCalls != 1 or payload.consent.allowBackup:
            raise HTTPException(409, "请重新确认本批整理的发送范围。")
        index.refresh()
        with transaction(engine) as db:
            identity = str(payload.id)
            previous = db.get(LibraryClassification, identity)
            if previous is not None:
                if not compare_digest(previous.confirmation, payload.consent.token):
                    raise HTTPException(409, "这个整理请求标识已使用，请重新预览。")
                return public_job(previous)
            scope = requested_scope(db, payload)
            connection, _ = verify_consent(db, f"library-classification/{identity}", [scope], payload.consent, secret)
            job = LibraryClassification(
                id=identity, confirmation=payload.consent.token, connection=connection_view(connection), scope=scope,
                auto_apply=payload.autoApply, file_count=len(payload.fileIds), state="queued", result_count=0, created_at=when_utc(now()),
            )
            db.add(job)
            return public_job(job)

    @router.get("")
    def queue_status():
        with transaction(engine, write=False) as db:
            jobs = select(LibraryClassification).order_by(LibraryClassification.created_at.desc(), LibraryClassification.id)
            active = list(db.scalars(jobs.where(LibraryClassification.state.in_(ACTIVE_STATES))))
            recent = list(db.scalars(jobs.where(LibraryClassification.state.not_in(ACTIVE_STATES)).limit(10)))
            candidates = select(LibraryClassificationCandidate).order_by(
                LibraryClassificationCandidate.created_at.desc(), LibraryClassificationCandidate.id,
            )
            pending = candidates.where(LibraryClassificationCandidate.state.in_(("pending", "approved")))
            history = (candidates.where(LibraryClassificationCandidate.state.not_in(("pending", "approved")))
                       .order_by(None).order_by(LibraryClassificationCandidate.decided_at.desc()).limit(40))
            return {
                "jobs": [public_job(job) for job in [*active, *recent]],
                "pending": [public_candidate(item) for item in db.scalars(pending)],
                "history": [public_candidate(item) for item in db.scalars(history)],
            }

    @router.post("/{identity}/cancel")
    def cancel(identity: UUID):
        with transaction(engine) as db:
            job = db.get(LibraryClassification, str(identity))
            if job is None:
                raise HTTPException(404, "找不到这条知识库整理请求。")
            cancel_scoped(job, when_utc(now()))
            return public_job(job)

    @router.post("/review")
    def review(data: Decisions):
        ids = list(dict.fromkeys(str(identity) for identity in data.ids))
        approved, reviewed, skipped = [], [], []
        with transaction(engine) as db:
            current = {item.id: item for item in db.scalars(select(LibraryClassificationCandidate).where(
                LibraryClassificationCandidate.id.in_(ids),
            ))}
            clock = when_utc(now())
            for identity in ids:
                item = current.get(identity)
                if item is None:
                    skipped.append({"id": identity, "reason": "找不到这份建议。"})
                elif data.choice == "resume" and item.state == "approved":
                    approved.append(identity)
                elif item.state != "pending" or data.choice == "resume":
                    skipped.append({"id": identity, "reason": "这份建议的审核状态已经变化。"})
                elif approve(item, data.choice, clock):
                    if item.state == "approved":
                        approved.append(identity)
                    else:
                        reviewed.append(identity)
                else:
                    skipped.append({"id": identity, "reason": "这份建议没有所选操作。"})
        apply_approved(engine, workspace, index, now, approved)
        with transaction(engine, write=False) as db:
            return {"reviewed": [public_candidate(db.get(LibraryClassificationCandidate, identity)) for identity in [*approved, *reviewed]],
                    "skipped": skipped}

    return router
