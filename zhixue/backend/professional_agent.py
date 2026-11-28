from hmac import compare_digest, digest
import re
from unicodedata import normalize
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field, StringConstraints
from sqlalchemy import select

from .agent_knowledge import check_folders
from .agent_memory import MemoryScope, granted_memories, memory_receipt
from .db import InstalledSkill, MemoryInferenceJob, ModelConnection, ProfessionalAgent, SkillVersion, transaction
from .mobile_api import fingerprint, workspace_id
from .model_study import when_utc
from .model_study_input import Input, Revision, connection_view, main_connection, pinned_connection, usable_connection
from .repository_mcp import RepositoryGrant, connection_choices, grants_for
from .scoped_recommendation_worker import ACTIVE_STATES, cancel_scoped


SkillName = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Instructions = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
KnowledgeFolder = Annotated[str, StringConstraints(min_length=1, max_length=1400)]


class AgentDraft(Input):
    id: UUID
    revision: Revision | None = None
    name: Title
    purpose: Instructions
    responsibility: Instructions
    connectionId: UUID | None = None
    skills: list[SkillName] = Field(default_factory=list, max_length=40)
    knowledgeFolders: list[KnowledgeFolder] = Field(default_factory=list, max_length=12, exclude_if=lambda folders: not folders)
    memoryScopes: list[MemoryScope] = Field(default_factory=list, max_length=2, exclude_if=lambda scopes: not scopes)
    autoMemory: bool = Field(default=False, exclude_if=lambda enabled: not enabled)
    mcpRepositories: list[RepositoryGrant] = Field(default_factory=list, max_length=40, exclude_if=lambda keys: not keys)


class AgentConfirmation(AgentDraft):
    confirmation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def agent_view(agent: ProfessionalAgent) -> dict:
    definition = {key: value for key, value in agent.definition.items() if key != "autoMemoryRecipient"}
    return {"id": agent.id, "revision": agent.revision, **definition}


def selected_model(db, connection_id: str | None):
    return db.get(ModelConnection, connection_id) if connection_id else main_connection(db)


def automatic_memory_connection(db, agent):
    if not agent or not agent.definition.get("autoMemory"):
        return None
    connection = selected_model(db, agent.definition["connectionId"])
    if usable_connection(connection) and fingerprint(connection_view(connection)) == agent.definition.get("autoMemoryRecipient"):
        return connection
    return None


def pinned_reply_connection(db, reply):
    connection_id = reply.agent_scope.get("connectionId") if reply.agent_scope else None
    if connection_id is None:
        return pinned_connection(db, "main", reply.recipient)
    connection = selected_model(db, connection_id)
    if usable_connection(connection) and connection_view(connection) == reply.recipient:
        return connection
    return None


WORD = re.compile(r"[a-z][a-z0-9]{3,}")
HAN = re.compile(r"[\u3400-\u9fff]+")


def matching_terms(text: str) -> set[str]:
    normalized = normalize("NFKC", text).casefold()
    terms = set(WORD.findall(normalized))
    for run in HAN.findall(normalized):
        terms.update(run[i:i + 3] for i in range(len(run) - 2))
    return terms


def match_skill(db, names: list[str], question: str) -> str | None:
    if not names:
        return None
    normalized = normalize("NFKC", question).casefold()
    words = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized))
    terms = matching_terms(normalized)
    # Read only the authorized summaries. Load the selected Skill's body for the preview below.
    candidates = db.execute(select(InstalledSkill.name, InstalledSkill.manifest).where(InstalledSkill.name.in_(names)))
    scores = []
    for name, manifest in candidates:
        if name in words:
            scores.append(((1, 0), name))
            continue
        overlap = len(terms & matching_terms(manifest["description"]))
        if overlap >= 2:
            scores.append(((0, overlap), name))
    scores.sort(reverse=True)
    if scores and (len(scores) == 1 or scores[0][0] > scores[1][0]):
        return scores[0][1]
    return None


def chat_agent_scope(db, agent_id: str | None, skill_name: str | None, *, match_question: str | None = None) -> dict | None:
    if agent_id is None:
        if skill_name or match_question is not None:
            raise HTTPException(409, "请先选择已授权此 Skill 的专业 Agent。")
        return None
    agent = db.get(ProfessionalAgent, agent_id)
    if agent is None:
        raise HTTPException(409, "这个专业 Agent 已不可用，请重新选择。")
    view = agent_view(agent)
    if match_question is not None:
        skill_name = match_skill(db, view["skills"], match_question)
    pinned = None
    if skill_name:
        if skill_name not in view["skills"]:
            raise HTTPException(409, "所选专业 Agent 尚未获得此 Skill 的授权。")
        current = db.get(InstalledSkill, skill_name)
        version = db.get(SkillVersion, (skill_name, current.checksum)) if current else None
        if version is None:
            raise HTTPException(409, "所选 Skill 当前不可用。")
        pinned = {
            "name": version.name, "checksum": version.checksum, "position": version.position,
            "version": version.manifest["version"], "declaredTools": version.manifest["declaredTools"],
            "source": version.source,
        }
    return {
        "id": view["id"], "revision": view["revision"], "name": view["name"],
        "responsibility": view["responsibility"], "connectionId": view["connectionId"], "skill": pinned,
        **({"skillMode": "automatic"} if match_question is not None else {}),
    }


def agent_receipt(scope: dict | None) -> dict | None:
    if scope is None:
        return None
    skill = scope["skill"]
    sanitized = {**scope, "skill": {key: value for key, value in skill.items() if key != "source"} if skill else None}
    if "memories" in scope:
        sanitized["memories"] = memory_receipt(scope["memories"])
    return sanitized


def professional_agent_router(engine, workspace, session_token: str, now, mcp_vault) -> APIRouter:
    router = APIRouter(prefix="/api/agents")
    secret = session_token.encode("utf-8")

    def options(db, draft: AgentDraft) -> tuple[dict, ProfessionalAgent | None]:
        existing = db.get(ProfessionalAgent, str(draft.id))
        if draft.revision != (existing.revision if existing else None):
            raise HTTPException(409, "Agent 配置已变化，请重读后重新确认。")
        folders = list(dict.fromkeys(draft.knowledgeFolders))
        check_folders(workspace, folders)
        repositories = grants_for(db, draft.mcpRepositories)
        names = list(dict.fromkeys(draft.skills))
        skills = []
        for name in names:
            skill = db.get(InstalledSkill, name)
            if skill is None:
                raise HTTPException(409, f"Skill {name} 当前不可用，请重新选择。")
            skills.append({
                "name": name, "checksum": skill.checksum, "version": skill.manifest["version"],
                "declaredTools": skill.manifest["declaredTools"],
            })
        connection = selected_model(db, str(draft.connectionId) if draft.connectionId else None)
        if draft.connectionId and not usable_connection(connection):
            raise HTTPException(409, "所选模型连接当前不可用，请重新选择。")
        recipient = connection_view(connection) if usable_connection(connection) else None
        if draft.autoMemory and recipient is None:
            raise HTTPException(409, "自动整理需要先配置可用的模型连接。")
        basis = fingerprint({
            "kind": "professional-agent", "workspace": workspace_id(db),
            "configuration": draft.model_dump(mode="json", exclude={"confirmation"}),
            "recipient": recipient, "skills": skills,
            **({"mcpConnections": repositories} if repositories else {}),
        })
        return {
            "recipient": recipient, "skills": skills,
            **({"knowledgeFolders": folders} if folders else {}),
            **({"memoryScopes": list(dict.fromkeys(draft.memoryScopes))} if draft.memoryScopes else {}),
            **({"autoMemory": True} if draft.autoMemory else {}),
            **({"mcpRepositories": list(repositories)} if repositories else {}),
            "confirmation": digest(secret, basis.encode("ascii"), "sha256").hex(),
        }, existing

    @router.get("")
    def agents(skill: SkillName | None = None):
        with transaction(engine, write=False) as db:
            agents = [agent_view(agent) for agent in db.scalars(select(ProfessionalAgent).order_by(
                ProfessionalAgent.name, ProfessionalAgent.id
            ))]
            return {"items": [agent for agent in agents if skill is None or skill in agent["skills"]]}

    @router.get("/{identity}/memories")
    def memory_choices(identity: UUID):
        with transaction(engine, write=False) as db:
            return {"items": granted_memories(db, str(identity))}

    @router.get("/{identity}/mcp-repositories")
    def mcp_choices(identity: UUID):
        with transaction(engine, write=False) as db:
            agent = db.get(ProfessionalAgent, str(identity))
            if agent is None:
                raise HTTPException(404, "找不到此专业 Agent。")
            return {"items": connection_choices(db, mcp_vault, agent)}

    @router.post("/preview")
    def preview(draft: AgentDraft):
        with transaction(engine, write=False) as db:
            prepared, _ = options(db, draft)
            return prepared

    @router.post("")
    def confirm(draft: AgentConfirmation):
        submission = fingerprint(draft.model_dump(mode="json"))
        with transaction(engine) as db:
            existing = db.get(ProfessionalAgent, str(draft.id))
            if existing and existing.confirmation == submission:
                return agent_view(existing)
            preview, existing = options(db, draft)
            if not compare_digest(preview["confirmation"], draft.confirmation):
                raise HTTPException(409, "Agent 的配置、模型或授权已变化，请重新预览。")
            fields = draft.model_dump(mode="json", exclude={"id", "revision", "confirmation"})
            fields["skills"] = list(dict.fromkeys(draft.skills))
            if draft.knowledgeFolders:
                fields["knowledgeFolders"] = preview["knowledgeFolders"]
            if draft.memoryScopes:
                fields["memoryScopes"] = preview["memoryScopes"]
            if draft.mcpRepositories:
                fields["mcpRepositories"] = preview["mcpRepositories"]
            if draft.autoMemory:
                fields["autoMemoryRecipient"] = fingerprint(preview["recipient"])
            if existing is None:
                existing = ProfessionalAgent(id=str(draft.id), name=draft.name, revision=1)
                db.add(existing)
            else:
                existing.name = draft.name
                existing.revision += 1
                if existing.definition.get("autoMemory"):
                    clock = when_utc(now())
                    for job in db.scalars(select(MemoryInferenceJob).where(
                        MemoryInferenceJob.automatic.is_(True), MemoryInferenceJob.state.in_(ACTIVE_STATES),
                        MemoryInferenceJob.scope["agentId"].as_string() == existing.id,
                    )):
                        cancel_scoped(job, clock)
            existing.definition = fields
            existing.confirmation = submission
            return agent_view(existing)

    return router
