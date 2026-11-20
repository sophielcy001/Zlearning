from hashlib import sha256
import hmac
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import Field

from .graph_catalog import edge_key
from .graph_state import GraphDocument, GraphPayload, GraphView, NodeId, effective_links, node_point


class ArchiveNode(GraphPayload):
    id: NodeId
    title: str = Field(max_length=4096)
    kind: Literal["course", "note", "import", "supplement"]


class ArchiveEdge(GraphPayload):
    source: NodeId
    target: NodeId
    both: bool
    manual: bool
    automatic: bool


class GraphArchive(GraphPayload):
    format: Literal["zhixue-library-graph"]
    version: Literal[1]
    nodes: list[ArchiveNode] = Field(max_length=50_000)
    edges: list[ArchiveEdge] = Field(max_length=80_000)
    document: GraphDocument
    view: GraphView


class PreviewImport(GraphPayload):
    revision: int = Field(strict=True, ge=0)
    mode: Literal["merge", "restore"]
    archive: GraphArchive


class ConfirmImport(PreviewImport):
    confirmation: str = Field(pattern=r"^[a-f0-9]{64}$")


def archive_document(graph, nodes, automatic) -> dict:
    state = GraphDocument.model_validate(graph.document)
    links = effective_links(state, nodes, automatic)
    # Archive actual positions as observed, including anchors that the user has never dragged.
    state.positions = {identity: node_point(graph, state, identity) for identity in nodes}
    return {
        "format": "zhixue-library-graph", "version": 1,
        "nodes": [{key: node[key] for key in ("id", "title", "kind")} for node in nodes.values()],
        "edges": [{key: edge[key] for key in ("source", "target", "both", "manual", "automatic")} for edge in links],
        "document": state, "view": GraphView.model_validate(graph.view),
    }


def scoped_settings(state, archive, graph, automatic, live: set[str], mode: str) -> GraphDocument:
    imported = archive.document
    target = state.model_copy(deep=True) if mode == "merge" else GraphDocument()
    for identity, position in imported.positions.items():
        baseline = graph.anchors.get(identity)
        if (identity in live and identity not in target.positions
                and (baseline is None or position.x != baseline[0] or position.y != baseline[1])):
            target.positions[identity] = position

    target.hidden = sorted(set(target.hidden) | (set(imported.hidden) & live))
    for key, direction in imported.links.items():
        a, b = key.split("|")
        if a != b and a in live and b in live and key == edge_key(a, b):
            target.links.setdefault(key, direction)

    archived_edges = {}
    for edge in archive.edges:
        a, b = edge.source, edge.target
        if a == b or a not in live or b not in live:
            continue
        key = edge_key(a, b)
        direction = "both" if edge.both else "forward" if a < b else "reverse"
        archived_edges[key] = direction
        baseline = automatic.get(key)
        if baseline != direction and (mode == "restore" or baseline is None):
            target.links.setdefault(key, direction)
    if mode == "restore":
        for key in automatic.keys() - archived_edges.keys():
            a, b = key.split("|")
            if a in live and b in live:
                target.links.setdefault(key, "hidden")

    assigned = {identity for group in target.groups for identity in group.members}
    groups = {group.id for group in target.groups}
    group_names = {group.name.casefold() for group in target.groups}
    for group in imported.groups:
        members = sorted(set(group.members).intersection(live).difference(assigned))
        if len(members) < 2 or group.id in groups or group.name.casefold() in group_names:
            continue
        target.groups.append(group.model_copy(update={"members": members}))
        assigned.update(members)
        groups.add(group.id)
        group_names.add(group.name.casefold())

    favorites = {item.id for item in target.favorites}
    favorite_names = {item.name.casefold() for item in target.favorites}
    for item in imported.favorites:
        if item.id in favorites or item.name.casefold() in favorite_names:
            continue
        view = usable_view(item.view, live - set(target.hidden), groups)
        target.favorites.append(item.model_copy(update={"view": view}))
        favorites.add(item.id)
        favorite_names.add(item.name.casefold())

    # Revalidate the combined sizes at the same limits used for export and future edits.
    try:
        return GraphDocument.model_validate(target.model_dump(mode="json"))
    except ValueError as failure:
        raise HTTPException(422, "合并后的图谱设置太大，请缩小导入范围。") from failure


def usable_view(view: GraphView, nodes: set[str], groups: set) -> GraphView:
    return view.model_copy(update={
        "focus": view.focus if view.focus in nodes else None,
        "local": view.local and view.focus in nodes,
        "collapsed": [identity for identity in view.collapsed if identity in groups],
    })


def changed(before: dict, after: dict) -> int:
    return sum(before.get(key) != after.get(key) for key in before.keys() | after.keys())


def import_preview(graph, catalog: dict, automatic: dict, payload: PreviewImport, secret: str) -> tuple[dict, GraphDocument, GraphView]:
    if graph.revision != payload.revision:
        raise HTTPException(409, "图谱已被更新，请刷新后重新预览。")
    incoming = payload.archive.document
    declared = {node.id for node in payload.archive.nodes} | incoming.positions.keys() | set(incoming.hidden)
    declared.update(identity for key in incoming.links for identity in key.split("|"))
    declared.update(identity for group in incoming.groups for identity in group.members)
    declared.update(identity for edge in payload.archive.edges for identity in (edge.source, edge.target))
    live = set(catalog)
    matched = declared & live
    current = GraphDocument.model_validate(graph.document)
    current_view = GraphView.model_validate(graph.view)
    proposed = scoped_settings(current, payload.archive, graph, automatic, matched, payload.mode) if matched else current
    view = current_view
    if matched and payload.mode == "restore":
        view = usable_view(payload.archive.view, live - set(proposed.hidden), {item.id for item in proposed.groups})

    changes = {
        "positions": changed(current.positions, proposed.positions),
        "links": changed(current.links, proposed.links),
        "visibility": len(set(current.hidden) ^ set(proposed.hidden)),
        "groups": changed({item.id: item for item in current.groups}, {item.id: item for item in proposed.groups}),
        "favorites": changed({item.id: item for item in current.favorites}, {item.id: item for item in proposed.favorites}),
        "view": int(view != current_view),
    }
    evidence = {
        "input": payload.model_dump(mode="json", exclude={"confirmation"}),
        "matched": sorted(matched), "document": proposed.model_dump(mode="json"),
        "view": view.model_dump(mode="json"), "before": current_view.model_dump(mode="json"),
    }
    buffer = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    token = hmac.new(secret.encode("utf-8"), b"zhixue graph import v1\0" + buffer, sha256).hexdigest()
    return {"matched": len(matched), "skipped": len(declared - live), "changes": changes, "confirmation": token}, proposed, view
