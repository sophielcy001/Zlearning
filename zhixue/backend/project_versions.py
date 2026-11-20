from datetime import datetime
from hashlib import sha256
from hmac import compare_digest, digest
import json
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from .content import SQLITE_COURSES, course_at
from .db import GenerationStep, GoalVersion, Lesson, ModelConnection, ModelStudy, Project, RouteVersion, Task, transaction
from .model_study import sync_task, update_window, when_utc
from .project_activity import touch_project
from .stage_assessments import ensure_assessments, planned_phases, stop_retired_reviews


CORE_FIELDS = {
    "topic": ("学习主题", 180),
    "outcome": ("预期成果", 500),
    "foundation": ("已有基础", 300),
}


def goal_field(key: str, label: str, limit: int, value: str, origin: str, references=None, field_type="text") -> dict:
    return {
        "key": key, "label": label, "limit": limit, "value": value, "origin": origin,
        "evidenceIds": references or [], "type": field_type,
    }


def core_fields(values: dict, origin: str = "fact") -> list[dict]:
    return [goal_field(key, label, limit, values.get(key, ""), origin) for key, (label, limit) in CORE_FIELDS.items()]


def goal_values(definition: dict) -> dict:
    return {field["key"]: field["value"] for field in definition["fields"]}


def goal_date_field(fields: list[dict]) -> dict | None:
    return next((field for field in fields if field["key"] == "deadline"), None) or next((
        field for field in fields if field["label"] in {"目标日期", "截止日期", "目标截止日期"}
        or field["type"] == "date" and field["key"] in {"targetDate", "target_date"}
    ), None)


def fixture_route(sample: bool, positions: list[tuple[str, int]]) -> dict:
    nodes = []
    for identity, position in positions:
        course = course_at(sample, position)
        nodes.append({
            "id": identity, "order": position + 1, "phase": course.phase,
            "title": course.title, "outcome": course.outcome, "required": True,
        })
    return {"source": "sqlite-example" if sample else "local-template", "nodes": nodes}


def create_local_project(
    db, identity: str, definition: dict, moment: datetime, *,
    conversation_id: str | None = None, submission_digest: str | None = None,
) -> Project:
    values = goal_values(definition)
    sample = "sqlite" in values["topic"].casefold()
    project = Project(
        id=identity, topic=values["topic"], outcome=values["outcome"], foundation=values.get("foundation", ""),
        sample=sample, created_on=moment.date(), created_at=moment, updated_at=moment,
        lessons=[Lesson(id=str(uuid4()), position=position) for position in range(len(SQLITE_COURSES))], task=Task(),
    )
    db.add(project)
    db.flush()
    db.add(GoalVersion(project_id=identity, version=1, conversation_id=conversation_id, created_at=moment,
                       definition=definition, submission_digest=submission_digest))
    db.add(RouteVersion(
        project_id=identity, version=1, goal_version=1, created_at=moment,
        definition=fixture_route(sample, [(lesson.id, lesson.position) for lesson in project.lessons]),
    ))
    return project


def create_model_project(
    db, identity: str, definition: dict, moment: datetime, *, conversation_id: str, submission_digest: str,
    connection: ModelConnection, backup: dict | None, inputs: list[dict], limit: int | None,
) -> Project:
    values = goal_values(definition)
    project = Project(id=identity, topic=values["topic"], outcome=values["outcome"], foundation=values["foundation"],
                      sample=False, created_on=moment.date(), created_at=moment, updated_at=moment, task=Task())
    db.add(project)
    db.flush()
    db.add_all([
        GoalVersion(project_id=identity, version=1, conversation_id=conversation_id, created_at=moment,
                    definition=definition, submission_digest=submission_digest),
        ModelStudy(project_id=identity, connection_id=connection.id, connection_revision=connection.revision,
                   goal_fields=inputs, max_calls=limit, backup=backup, stopped=False),
    ])
    db.flush()
    db.add(GenerationStep(id=str(uuid4()), project_id=identity, kind="route", state="pending", priority=0))
    return project


LearningMark = Literal["known", "to_learn", "to_reinforce"]
RouteLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")]


class RoutePractice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    afterId: UUID
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160, pattern=r"^[^\r\n]+$")]
    outcome: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    instructions: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
    required: Annotated[bool, Field(strict=True)] = False


class StageAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchorId: UUID


class NewStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: RouteLabel


class RoutePlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: StageAnchor | NewStage
    branch: RouteLabel | None


class RouteOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseVersion: Annotated[int, Field(strict=True, ge=1)]
    order: list[UUID] = Field(min_length=1, max_length=10_000)
    marks: dict[UUID, LearningMark | None] = Field(default_factory=dict, max_length=10_000)
    practice: RoutePractice | None = None
    placements: dict[UUID, RoutePlacement] = Field(default_factory=dict, max_length=10_000)


class ConfirmRouteOrder(RouteOrder):
    confirmation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def require_project(db, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "找不到学习项目。")
    return project


def route_view(route: RouteVersion) -> dict:
    definition = {key: value for key, value in route.definition.items() if key != "editReceipt"}
    return {**definition, "version": route.version, "goalVersion": route.goal_version,
            "createdAt": route.created_at.isoformat()}


def latest_route(db, identity: str) -> RouteVersion | None:
    return db.scalar(select(RouteVersion).where(RouteVersion.project_id == identity)
                     .order_by(RouteVersion.version.desc()).limit(1))


def proposed_nodes(db, identity: UUID, data: RouteOrder, material: bytes) -> tuple[RouteVersion, list[dict]]:
    route = latest_route(db, str(identity))
    if route is None or route.version != data.baseVersion:
        raise HTTPException(409, "学习路线已更新，请查看最新版后重新预览。")
    existing = {node["id"]: node for node in route.definition["nodes"]}
    ordered = [str(item) for item in data.order]
    marks = {str(key): mark for key, mark in data.marks.items()}
    if len(ordered) != len(existing) or set(ordered) != existing.keys():
        raise HTTPException(422, "调整顺序时，请保留当前路线的全部知识点。")
    if marks.keys() - existing.keys():
        raise HTTPException(422, "请为当前路线中的知识点添加标记。")
    placements = {str(key): item for key, item in data.placements.items()}
    if placements.keys() - existing.keys():
        raise HTTPException(422, "请为当前路线中的知识点设置阶段和分支。")

    stage_names = {node["phase"] for node in existing.values()}
    nodes = []
    for key in ordered:
        node = dict(existing[key])
        if key in marks:
            if marks[key] is None:
                node.pop("learningMark", None)
            else:
                node["learningMark"] = marks[key]
        if placement := placements.get(key):
            target = placement.stage
            if isinstance(target, StageAnchor):
                anchor = existing.get(str(target.anchorId))
                if anchor is None:
                    raise HTTPException(422, "请在当前路线中选择阶段。")
                phase, stage_id = anchor["phase"], anchor.get("stageId")
            else:
                if target.name in stage_names:
                    raise HTTPException(422, "该阶段已存在，请选择已有阶段。")
                phase, stage_id = target.name, str(uuid5(identity, f"route-stage:{target.name}"))
            if (phase, stage_id) != (node["phase"], node.get("stageId")):
                node["phase"] = phase
                if stage_id is None:
                    node.pop("stageId", None)
                else:
                    node["stageId"] = stage_id
            if placement.branch != node.get("branch"):
                if placement.branch is None:
                    node.pop("branch", None)
                else:
                    node["branch"] = placement.branch
        nodes.append(node)
    if (practice := data.practice) is not None:
        anchor_id = str(practice.afterId)
        if anchor_id not in existing:
            raise HTTPException(422, "请在当前路线中选择实战所在的位置。")
        index = ordered.index(anchor_id)
        anchor = nodes[index]
        node = {
            "id": str(uuid5(identity, sha256(material).hexdigest())), "kind": "practice",
            "phase": anchor["phase"], "title": practice.title, "outcome": practice.outcome,
            "instructions": practice.instructions, "required": practice.required, "prerequisites": [],
        }
        node.update({key: anchor[key] for key in ("stageId", "branch") if key in anchor})
        nodes.insert(index + 1, node)
    nodes = [{**node, "order": index} for index, node in enumerate(nodes, 1)]
    if nodes == route.definition["nodes"]:
        raise HTTPException(422, "调整顺序、标记、归属或新增实战后即可预览变化。")
    return route, nodes


def route_edit_material(identity: UUID, data: RouteOrder) -> bytes:
    payload = data.model_dump(mode="json", exclude={"confirmation"})
    if not data.placements:
        payload.pop("placements")
    return json.dumps(
        ["route-edit", str(identity), payload],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def version_router(engine, session_token: str, project_moment, now) -> APIRouter:
    router = APIRouter(prefix="/api/projects")
    secret = session_token.encode("utf-8")

    @router.get("/{identity}/versions")
    def versions(identity: UUID):
        project_id = str(identity)
        with transaction(engine, write=False) as db:
            require_project(db, project_id)
            goals = db.scalars(select(GoalVersion).where(GoalVersion.project_id == project_id).order_by(GoalVersion.version.desc()))
            routes = db.scalars(select(RouteVersion).where(RouteVersion.project_id == project_id).order_by(RouteVersion.version.desc()))
            history = [
                {**item.definition, "version": item.version, "conversationId": item.conversation_id,
                 "createdAt": item.created_at.isoformat()}
                for item in goals
            ]
            date = goal_date_field(history[0]["fields"]) if history else None
            return {
                "goals": history,
                "routes": [route_view(item) for item in routes],
                "goalDateKey": date["key"] if date else "deadline",
            }

    @router.post("/{identity}/route-order/preview")
    def preview_order(identity: UUID, data: RouteOrder):
        project_id = str(identity)
        material = route_edit_material(identity, data)
        with transaction(engine, write=False) as db:
            project = require_project(db, project_id)
            route, nodes = proposed_nodes(db, identity, data, material)
            before = route.definition["nodes"]
            preview = {
                "baseVersion": route.version, "nextVersion": route.version + 1, "before": before, "after": nodes,
                "readyLessons": sum(item.ready for item in project.lessons),
                "learnedLessons": sum(item.learned_on is not None for item in project.lessons),
                "confirmation": digest(secret, material, "sha256").hex(),
            }
            if data.placements:
                old_phases, new_phases = planned_phases(before), planned_phases(nodes)
                if old_phases.keys() != new_phases.keys():
                    preview["stageImpact"] = {
                        "before": [{"phase": name, "lessons": [node["title"] for node in members]}
                                   for key, (name, members) in old_phases.items() if key not in new_phases],
                        "after": [{"phase": name, "lessons": [node["title"] for node in members]}
                                  for key, (name, members) in new_phases.items() if key not in old_phases],
                    }
            return preview

    @router.post("/{identity}/route-order/confirm")
    def confirm_order(identity: UUID, data: ConfirmRouteOrder):
        project_id = str(identity)
        material = route_edit_material(identity, data)
        if not compare_digest(data.confirmation, digest(secret, material, "sha256").hex()):
            raise HTTPException(409, "路线确认已失效，请重新预览。")
        receipt = sha256(material).hexdigest()
        with transaction(engine) as db:
            project = require_project(db, project_id)
            existing = db.get(RouteVersion, (project_id, data.baseVersion + 1))
            if existing and existing.definition.get("editReceipt") == receipt:
                return route_view(existing)
            route, nodes = proposed_nodes(db, identity, data, material)
            lessons = {item.id: item for item in project.lessons}
            if lessons.keys() != {str(item) for item in data.order}:
                raise HTTPException(409, "项目中的知识点已变更，请重新预览。")
            moment = project_moment(db)
            for node in nodes:
                lesson_id = node["id"]
                if lesson_id not in lessons:
                    lesson = Lesson(id=lesson_id, position=max(item.position for item in project.lessons) + 1,
                                    outline={key: value for key, value in node.items() if key != "order"})
                    project.lessons.append(lesson)
                    lessons[lesson_id] = lesson
                lessons[lesson_id].reading_order = node["order"] - 1
            revised = RouteVersion(
                project_id=project_id, version=route.version + 1, goal_version=route.goal_version, created_at=moment,
                definition={**route.definition, "nodes": nodes, "editReceipt": receipt},
            )
            db.add(revised)
            touch_project(project, moment)
            if data.placements:
                old_phases, new_phases = planned_phases(route.definition["nodes"]), planned_phases(nodes)
                if old_phases.keys() != new_phases.keys():
                    stop_retired_reviews(db, project.id, old_phases.keys() - new_phases.keys(), when_utc(now()))
                    ensure_assessments(db, project, moment)
            study = db.get(ModelStudy, project.id)
            if study is not None:
                update_window(db, study, [lessons[node["id"]] for node in nodes])
                sync_task(db, study, project)
            return route_view(revised)

    return router
