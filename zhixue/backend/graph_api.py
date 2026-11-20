from collections.abc import Callable
from datetime import date, datetime
from hmac import compare_digest
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .db import LibraryGraphRevision, transaction
from .graph_archive import ConfirmImport, PreviewImport, archive_document, import_preview
from .graph_catalog import folder_name, graph_catalog
from .graph_layout import add_anchors
from .graph_state import GraphDocument, GraphEdit, GraphView, append_revision, apply_edit, effective_links, node_point, workspace
from .library_index import LibraryIndex


def graph_data(graph, nodes, automatic, projects) -> dict:
    document = GraphDocument.model_validate(graph.document)
    folders = set()
    hidden = set(document.hidden)
    for identity, entry in nodes.items():
        entry.update(position=node_point(graph, document, identity), hidden=identity in hidden)
        relative = entry["folder"]
        if relative:
            parts = relative.split("/")
            folders.update("/".join(parts[:index + 1]) for index in range(len(parts)))
    return {
        "revision": graph.revision, "head": graph.head, "canUndo": graph.head is not None, "canRedo": bool(graph.redo),
        "nodes": list(nodes.values()), "edges": effective_links(document, nodes, automatic),
        "projects": projects, "folders": [{"path": item, "name": folder_name(item)} for item in sorted(folders)],
        "view": GraphView.model_validate(graph.view), "groups": document.groups, "favorites": document.favorites,
    }


def graph_router(
    engine: Engine, folder: Path, index: LibraryIndex, today: Callable[[Session], date], now: Callable[[], datetime],
    session_token: str,
) -> APIRouter:
    router = APIRouter(prefix="/api/library/graph")

    def snapshot(db):
        graph = workspace(db)
        nodes, links, projects = graph_catalog(db, folder, today(db))
        graph.anchors = add_anchors(graph.anchors, nodes, links)
        return graph, nodes, links, projects

    @router.get("")
    def get_graph():
        index.refresh()
        with transaction(engine) as db:
            return graph_data(*snapshot(db))

    @router.post("/edit")
    def edit_graph(payload: GraphEdit):
        index.refresh()
        with transaction(engine) as db:
            graph, nodes, links, projects = snapshot(db)
            apply_edit(db, graph, payload, nodes, links, now())
            return graph_data(graph, nodes, links, projects)

    @router.put("/view")
    def save_view(payload: GraphView):
        with transaction(engine) as db:
            workspace(db).view = payload.model_dump(mode="json")
        return payload

    @router.get("/archive")
    def export_graph():
        index.refresh()
        with transaction(engine) as db:
            graph, nodes, links, _ = snapshot(db)
            return archive_document(graph, nodes, links)

    @router.post("/import/preview")
    def preview_archive(payload: PreviewImport):
        index.refresh()
        with transaction(engine) as db:
            graph, nodes, links, _ = snapshot(db)
            preview, _, _ = import_preview(graph, nodes, links, payload, session_token)
            return preview

    @router.post("/import")
    def confirm_archive(payload: ConfirmImport):
        index.refresh()
        with transaction(engine) as db:
            graph, nodes, links, projects = snapshot(db)
            preview, document, view = import_preview(graph, nodes, links, payload, session_token)
            if not compare_digest(preview["confirmation"], payload.confirmation):
                raise HTTPException(409, "图谱或导入文件已变化，请重新预览。")
            append_revision(db, graph, document, "合并导入" if payload.mode == "merge" else "从文件恢复", now())
            graph.view = view.model_dump(mode="json")
            return graph_data(graph, nodes, links, projects)

    @router.get("/history")
    def history(before: int | None = Query(None, ge=1)):
        statement = select(
            LibraryGraphRevision.id, LibraryGraphRevision.label, LibraryGraphRevision.created_at,
        ).order_by(LibraryGraphRevision.id.desc()).limit(26)
        if before is not None:
            statement = statement.where(LibraryGraphRevision.id < before)
        with transaction(engine, write=False) as db:
            rows = db.execute(statement).all()
        return {
            "items": [{"id": item.id, "name": item.label, "at": item.created_at} for item in rows[:25]],
            "next": rows[24].id if len(rows) > 25 else None,
        }

    return router
