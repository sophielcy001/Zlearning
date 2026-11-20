from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .db import LibraryGraph, LibraryGraphRevision
from .graph_catalog import edge_key


NODE_ID = r"(?:course-|note-|supplement-|prerequisite-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
NodeId = Annotated[str, StringConstraints(pattern=rf"^{NODE_ID}$")]
EdgeId = Annotated[str, StringConstraints(pattern=rf"^{NODE_ID}\|{NODE_ID}$")]
Coordinate = Annotated[float, Field(allow_inf_nan=False, ge=-1_000_000, le=1_000_000)]
Caption = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
LinkStyle = Literal["forward", "reverse", "both", "hidden"]


class GraphPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(GraphPayload):
    x: Coordinate
    y: Coordinate


class GraphView(GraphPayload):
    center: Point | None = None
    zoom: float = Field(default=1, ge=.025, le=4, allow_inf_nan=False)
    folder: str = Field(default="", max_length=4096)
    project: UUID | None = None
    due: bool = False
    local: bool = False
    focus: NodeId | None = None
    motion: bool | None = None
    collapsed: list[UUID] = Field(default_factory=list, max_length=5000)


class Group(GraphPayload):
    id: UUID
    name: Caption
    members: list[NodeId] = Field(min_length=2, max_length=50_000)


class SavedView(GraphPayload):
    id: UUID
    name: Caption
    view: GraphView


class GraphDocument(GraphPayload):
    positions: dict[NodeId, Point] = Field(default_factory=dict, max_length=50_000)
    links: dict[EdgeId, LinkStyle] = Field(default_factory=dict, max_length=80_000)
    hidden: list[NodeId] = Field(default_factory=list, max_length=50_000)
    groups: list[Group] = Field(default_factory=list, max_length=5000)
    favorites: list[SavedView] = Field(default_factory=list, max_length=5000)


class MoveNode(GraphPayload):
    kind: Literal["move"]
    id: NodeId
    position: Point


class MoveGroup(GraphPayload):
    kind: Literal["moveGroup"]
    id: UUID
    delta: Point


class SetHidden(GraphPayload):
    kind: Literal["visibility"]
    ids: list[NodeId] = Field(min_length=1, max_length=50_000)
    hidden: bool


class SetEdge(GraphPayload):
    kind: Literal["link"]
    source: NodeId
    target: NodeId
    direction: Literal["one-way", "both", "remove", "default"]


class SetGroup(GraphPayload):
    kind: Literal["group"]
    group: Group


class DropGroup(GraphPayload):
    kind: Literal["dropGroup"]
    id: UUID


class SaveView(GraphPayload):
    kind: Literal["favorite"]
    favorite: SavedView


class DropView(GraphPayload):
    kind: Literal["dropFavorite"]
    id: UUID


class StepHistory(GraphPayload):
    kind: Literal["undo", "redo"]


class RestoreRevision(GraphPayload):
    kind: Literal["restore"]
    id: int = Field(strict=True, ge=1)


Edit = MoveNode | MoveGroup | SetHidden | SetEdge | SetGroup | DropGroup | SaveView | DropView | StepHistory | RestoreRevision


class GraphEdit(GraphPayload):
    revision: int = Field(strict=True, ge=0)
    action: Annotated[Edit, Field(discriminator="kind")]


def workspace(db) -> LibraryGraph:
    graph = db.get(LibraryGraph, 1)
    if graph is None:
        graph = LibraryGraph(id=1, revision=0, head=None, redo=[], anchors={}, document={}, view={})
        db.add(graph)
    return graph


def node_point(graph: LibraryGraph, document: GraphDocument, identity: str) -> Point:
    if identity in document.positions:
        return document.positions[identity]
    x, y = graph.anchors[identity]
    return Point(x=x, y=y)


def available_node(nodes: dict, identity: str, hidden: set[str]) -> None:
    if identity not in nodes or identity in hidden:
        raise HTTPException(409, "这个节点已移除或隐藏，请刷新图谱。")


def apply_node_edit(document, operation, nodes, automatic) -> str:
    hidden = set(document.hidden)
    if isinstance(operation, MoveNode):
        available_node(nodes, operation.id, hidden)
        document.positions[operation.id] = operation.position
        return "移动节点"
    if isinstance(operation, SetHidden):
        ids = set(operation.ids)
        if operation.hidden and not ids <= nodes.keys():
            raise HTTPException(409, "有节点已移除，请刷新图谱。")
        document.hidden = sorted(hidden | ids if operation.hidden else hidden - ids)
        return "隐藏节点" if operation.hidden else "恢复隐藏节点"

    available_node(nodes, operation.source, hidden)
    available_node(nodes, operation.target, hidden)
    if operation.source == operation.target:
        raise HTTPException(422, "请选择两个不同的节点。")
    key = edge_key(operation.source, operation.target)
    baseline = automatic.get(key)
    if operation.direction == "remove":
        direction = "hidden" if baseline else None
    elif operation.direction == "default":
        direction = None
    elif operation.direction == "one-way":
        direction = "forward" if operation.source < operation.target else "reverse"
    else:
        direction = "both"
    if direction and direction != baseline:
        document.links[key] = direction
    else:
        document.links.pop(key, None)
    return "删除连线" if operation.direction == "remove" else "编辑连线"


def apply_group_edit(graph, document, operation, nodes) -> str:
    if isinstance(operation, DropGroup):
        document.groups = [group for group in document.groups if group.id != operation.id]
        return "取消分组"
    if isinstance(operation, SetGroup):
        group = operation.group
        others = [current for current in document.groups if current.id != group.id]
        members = set(group.members)
        if len(members) < 2 or not members <= nodes.keys():
            raise HTTPException(409, "请选择至少两个仍在知识库中的节点。")
        if any(members.intersection(current.members) for current in others):
            raise HTTPException(409, "有节点已属于其他分组，请先调整原分组。")
        if any(current.name.casefold() == group.name.casefold() for current in others):
            raise HTTPException(409, "已有同名分组。")
        group.members = sorted(members)
        document.groups = [*others, group]
        return "保存分组"

    group = next((item for item in document.groups if item.id == operation.id), None)
    if group is None:
        raise HTTPException(409, "找不到分组，请刷新图谱。")
    for identity in group.members:
        if identity not in graph.anchors:
            continue
        previous = node_point(graph, document, identity)
        try:
            document.positions[identity] = Point(x=previous.x + operation.delta.x, y=previous.y + operation.delta.y)
        except ValueError as failure:
            raise HTTPException(422, "移动位置超过图谱范围。") from failure
    return "移动分组"


def apply_favorite(document, operation) -> str:
    if isinstance(operation, DropView):
        document.favorites = [view for view in document.favorites if view.id != operation.id]
        return "删除收藏视角"
    favorite = operation.favorite
    existing = [view for view in document.favorites if view.id != favorite.id]
    if any(view.name.casefold() == favorite.name.casefold() for view in existing):
        raise HTTPException(409, "已有同名收藏视角。")
    document.favorites = [*existing, favorite]
    return "收藏视角"


def append_revision(db, graph, document, label, now) -> None:
    snapshot = document.model_dump(mode="json")
    if snapshot == GraphDocument.model_validate(graph.document).model_dump(mode="json"):
        return
    graph.revision += 1
    db.add(LibraryGraphRevision(id=graph.revision, parent=graph.head, label=label, created_at=now, document=snapshot))
    graph.head = graph.revision
    graph.redo = []
    graph.document = snapshot


def step_revision(db, graph, kind: str) -> None:
    if kind == "undo" and graph.head is not None:
        current = db.get(LibraryGraphRevision, graph.head)
        graph.redo = [*graph.redo, graph.head]
        graph.head = current.parent
    elif kind == "redo" and graph.redo:
        graph.head = graph.redo[-1]
        graph.redo = graph.redo[:-1]
    else:
        return
    snapshot = db.get(LibraryGraphRevision, graph.head) if graph.head is not None else None
    graph.document = snapshot.document if snapshot else {}
    graph.revision += 1


def apply_edit(db, graph, payload, nodes, automatic, now) -> None:
    if graph.revision != payload.revision:
        raise HTTPException(409, "图谱已被更新，请刷新后重试。")
    operation = payload.action
    if isinstance(operation, StepHistory):
        step_revision(db, graph, operation.kind)
        return
    if isinstance(operation, RestoreRevision):
        previous = db.get(LibraryGraphRevision, operation.id)
        if previous is None:
            raise HTTPException(404, "找不到这个图谱版本。")
        append_revision(db, graph, GraphDocument.model_validate(previous.document), "恢复历史版本", now)
        return

    document = GraphDocument.model_validate(graph.document)
    if isinstance(operation, (MoveNode, SetHidden, SetEdge)):
        label = apply_node_edit(document, operation, nodes, automatic)
    elif isinstance(operation, (SetGroup, DropGroup, MoveGroup)):
        label = apply_group_edit(graph, document, operation, nodes)
    else:
        label = apply_favorite(document, operation)
    append_revision(db, graph, document, label, now)


def effective_links(document: GraphDocument, nodes: dict, automatic: dict) -> list[dict]:
    result = []
    for key, direction in sorted((automatic | document.links).items()):
        a, b = key.split("|")
        if a not in nodes or b not in nodes or direction == "hidden":
            continue
        source, target = (b, a) if direction == "reverse" else (a, b)
        result.append({"id": key, "source": source, "target": target, "both": direction == "both",
                       "manual": key in document.links, "automatic": key in automatic})
    return result
