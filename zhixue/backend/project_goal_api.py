from hashlib import sha256
from hmac import compare_digest, digest
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import select

from .chat_goals import edit_goal
from .db import GoalVersion, ModelStudy, RouteVersion, transaction
from .model_study_input import Input, Revision, fields_to_send
from .project_activity import touch_project
from .project_versions import goal_date_field, goal_field, goal_values, require_project


class GoalRevision(Input):
    baseGoalVersion: Revision
    baseRouteVersion: Revision
    values: dict[str, str]


class ConfirmGoalRevision(GoalRevision):
    confirmation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def revision_material(project_id: str, data: GoalRevision) -> bytes:
    return json.dumps(
        ["project-goal-revision", project_id, data.model_dump(exclude={"confirmation"})],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def proposed_goal(db, project_id: str, data: GoalRevision):
    route = db.scalar(select(RouteVersion).where(RouteVersion.project_id == project_id)
                      .order_by(RouteVersion.version.desc()).limit(1))
    if route is None or (route.version, route.goal_version) != (data.baseRouteVersion, data.baseGoalVersion):
        raise HTTPException(409, "目标或路线版本已变化，请刷新后重新预览。")
    goal = db.get(GoalVersion, (project_id, route.goal_version))
    fields = goal.definition["fields"]
    deadline = goal_date_field(fields)
    if deadline is None and "deadline" in data.values:
        fields = [*fields, goal_field("deadline", "目标日期", 10, "", "suggestion", field_type="date")]
    elif deadline is not None and deadline["key"] in data.values:
        fields = [{**field, "type": "date", "limit": 10} if field is deadline else field for field in fields]
    before = {**goal.definition, "revision": goal.definition.get("revision", goal.version), "fields": fields}
    after = edit_goal(before, data.values, [])
    values = goal_values(after)
    if not values.get("topic") or not values.get("outcome"):
        raise HTTPException(422, "请填写学习主题和预期成果。")
    if after is before:
        raise HTTPException(422, "修改至少一项学习目标后即可预览。")
    return goal, route, before, after


def project_goal_router(engine, session_token, project_moment) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{identity}/goal-revision")
    secret = session_token.encode("utf-8")

    @router.post("/preview")
    def preview(identity: UUID, data: GoalRevision):
        project_id = str(identity)
        with transaction(engine, write=False) as db:
            require_project(db, project_id)
            goal, route, before, after = proposed_goal(db, project_id, data)
            changes = [
                {"key": new["key"], "label": new["label"], "before": old["value"], "after": new["value"]}
                for old, new in zip(before["fields"], after["fields"], strict=True)
                if old["value"] != new["value"]
            ]
            study = db.get(ModelStudy, project_id)
            return {
                "nextGoalVersion": goal.version + 1,
                "nextRouteVersion": route.version + 1,
                "changes": changes,
                "requiresModelConsent": bool(
                    study and study.deleted_at is None and fields_to_send(after) != study.goal_fields
                ),
                "confirmation": digest(secret, revision_material(project_id, data), "sha256").hex(),
            }

    @router.post("/confirm")
    def confirm(identity: UUID, data: ConfirmGoalRevision):
        project_id = str(identity)
        material = revision_material(project_id, data)
        if not compare_digest(data.confirmation, digest(secret, material, "sha256").hex()):
            raise HTTPException(409, "目标确认与预览内容不一致，请重新预览。")
        receipt = sha256(material).hexdigest()
        result = {"goalVersion": data.baseGoalVersion + 1, "routeVersion": data.baseRouteVersion + 1}
        with transaction(engine) as db:
            project = require_project(db, project_id)
            saved = db.get(GoalVersion, (project_id, result["goalVersion"]))
            if saved is not None and saved.submission_digest == receipt:
                return result
            goal, route, _, definition = proposed_goal(db, project_id, data)
            moment = project_moment(db)
            values = goal_values(definition)
            project.topic = values["topic"]
            project.outcome = values["outcome"]
            project.foundation = values.get("foundation", "")
            touch_project(project, moment)
            db.add(GoalVersion(
                project_id=project_id, version=result["goalVersion"], conversation_id=goal.conversation_id,
                created_at=moment, definition=definition, submission_digest=receipt,
            ))
            db.flush()
            db.add(RouteVersion(
                project_id=project_id, version=result["routeVersion"], goal_version=result["goalVersion"],
                created_at=moment, definition=route.definition,
            ))
            return result

    return router
