from collections.abc import Callable
from datetime import date, datetime
from hmac import compare_digest
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .db import GraphCandidate, GraphRecommendation, transaction
from .graph_api import graph_data
from .graph_catalog import graph_catalog
from .graph_layout import add_anchors
from .graph_recommendations import (
    ACTIVE_STATES, MAX_NODES, Unavailable, build_scope, conflict, public_candidate, public_job, source_nodes,
)
from .graph_state import (
    GraphDocument, GraphPayload, Group, NodeId, SetEdge, SetGroup, append_revision, apply_group_edit, apply_node_edit, workspace,
)
from .library_index import LibraryIndex
from .model_study import when_utc
from .model_study_input import Consent, connection_view, preview_consent, verify_consent
from .scoped_recommendation_worker import cancel_scoped


class ScopeInput(GraphPayload):
    id: UUID
    nodeIds: list[NodeId] = Field(min_length=2, max_length=MAX_NODES)
    withExcerpts: bool = Field(default=False, strict=True)

    @field_validator("nodeIds")
    @classmethod
    def distinct_nodes(cls, nodes):
        if len(nodes) != len(set(nodes)):
            raise ValueError("请为图谱推荐选择不同的节点。")
        return nodes


class RequestInput(ScopeInput):
    consent: Consent


class Decisions(GraphPayload):
    revision: int = Field(strict=True, ge=0)
    ids: list[UUID] = Field(min_length=1, max_length=200)
    action: Literal["accept", "reject"]


def pending_candidates(db: Session) -> list[dict]:
    rows = db.scalars(select(GraphCandidate).where(GraphCandidate.state == "pending").order_by(
        GraphCandidate.created_at.desc(), GraphCandidate.kind, GraphCandidate.id,
    ))
    return [public_candidate(candidate) for candidate in rows]


def graph_recommendation_router(
    engine: Engine, folder: Path, index: LibraryIndex, today: Callable[[Session], date], now: Callable[[], datetime],
    session_token: str,
) -> APIRouter:
    router = APIRouter(prefix="/api/library/graph/recommendations")
    secret = session_token.encode("utf-8")

    def requested_scope(db, payload: ScopeInput) -> dict:
        nodes, links, _ = graph_catalog(db, folder, None)
        document = GraphDocument.model_validate(workspace(db).document)
        try:
            return build_scope(db, folder, nodes, links, document, payload.nodeIds, payload.withExcerpts)
        except Unavailable as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/preview")
    def preview(payload: ScopeInput):
        index.refresh()
        with transaction(engine) as db:
            scope = requested_scope(db, payload)
            disclosure = preview_consent(db, f"graph-recommendation/{payload.id}", [scope], 1, secret)
            return {"recipient": disclosure["connection"], "consent": disclosure["consent"], **scope["material"]}

    @router.post("", status_code=202)
    def queue(payload: RequestInput):
        if payload.consent.maxCalls != 1 or payload.consent.allowBackup:
            raise HTTPException(409, "请重新确认本次图谱推荐的发送范围。")
        index.refresh()
        with transaction(engine) as db:
            identity = str(payload.id)
            existing = db.get(GraphRecommendation, identity)
            if existing is not None:
                if not compare_digest(existing.confirmation, payload.consent.token):
                    raise HTTPException(409, "这个推荐请求标识已使用，请重新预览。")
                return public_job(existing)
            scope = requested_scope(db, payload)
            recipient, _ = verify_consent(db, f"graph-recommendation/{identity}", [scope], payload.consent, secret)
            job = GraphRecommendation(
                id=identity, confirmation=payload.consent.token, connection=connection_view(recipient),
                scope=scope, state="queued", result_count=0, created_at=when_utc(now()),
            )
            db.add(job)
            return public_job(job)

    @router.get("")
    def review_queue():
        with transaction(engine, write=False) as db:
            base = select(GraphRecommendation).order_by(GraphRecommendation.created_at.desc(), GraphRecommendation.id.desc())
            active = list(db.scalars(base.where(GraphRecommendation.state.in_(ACTIVE_STATES))))
            recent = list(db.scalars(base.where(GraphRecommendation.state.not_in(ACTIVE_STATES)).limit(8)))
            jobs = sorted([*active, *recent], key=lambda job: (job.created_at, job.id), reverse=True)
            return {"jobs": [public_job(job) for job in jobs], "candidates": pending_candidates(db)}

    @router.post("/{identity}/cancel")
    def cancel(identity: UUID):
        with transaction(engine) as db:
            job = db.get(GraphRecommendation, str(identity))
            if job is None:
                raise HTTPException(404, "找不到这条图谱推荐请求。")
            cancel_scoped(job, when_utc(now()))
            return public_job(job)

    @router.post("/review")
    def review(payload: Decisions):
        if payload.action == "accept":
            index.refresh()
        with transaction(engine) as db:
            ids = list(dict.fromkeys(str(identity) for identity in payload.ids))
            items = {item.id: item for item in db.scalars(select(GraphCandidate).where(
                GraphCandidate.id.in_(ids), GraphCandidate.state == "pending",
            ))}
            clock = when_utc(now())
            outcomes = {"accepted": 0, "rejected": 0, "skipped": [], "graph": None}
            for identity in ids:
                if identity not in items:
                    outcomes["skipped"].append({"id": identity, "reason": "此项已经处理。"})
            if payload.action == "reject":
                for item in items.values():
                    item.state, item.decided_at = "rejected", clock
                outcomes["rejected"] = len(items)
                return outcomes
            if not items:
                return outcomes

            graph = workspace(db)
            if payload.revision != graph.revision:
                raise HTTPException(409, "图谱已被更新，请刷新后确认推荐。")
            nodes, automatic, projects = graph_catalog(db, folder, today(db))
            document = GraphDocument.model_validate(graph.document)
            hidden = set(document.hidden)
            available = {}
            for use_excerpts in (False, True):
                required = {node for item in items.values() if item.with_excerpts == use_excerpts for node in item.basis}
                if required:
                    available[use_excerpts] = source_nodes(db, folder, nodes, required - hidden, with_excerpts=use_excerpts)
            graph.anchors = add_anchors(graph.anchors, nodes, automatic)
            for identity in ids:
                item = items.get(identity)
                if item is None:
                    continue
                proofs = available[item.with_excerpts]
                state = "accepted"
                reason = conflict(item.kind, item.presentation, document, automatic)
                if any(proofs.get(node, {}).get("proof") != proof for node, proof in item.basis.items()):
                    reason = "相关节点、文本或可见状态已变化，请重新生成建议。"
                if reason is not None:
                    state = "stale"
                    outcomes["skipped"].append({"id": identity, "reason": reason})
                else:
                    if item.kind == "link":
                        link = item.presentation
                        apply_node_edit(document, SetEdge(
                            kind="link", source=link["source"], target=link["target"], direction="both" if link["both"] else "one-way",
                        ), nodes, automatic)
                    else:
                        group = Group(id=item.id, name=item.presentation["name"], members=item.presentation["members"])
                        apply_group_edit(graph, document, SetGroup(kind="group", group=group), nodes)
                    outcomes["accepted"] += 1
                item.state, item.decided_at = state, clock
            if outcomes["accepted"]:
                append_revision(db, graph, document, "采用图谱推荐", clock)
            outcomes["graph"] = graph_data(graph, nodes, automatic, projects)
            return outcomes

    return router
