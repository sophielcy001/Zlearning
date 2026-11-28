from typing import Literal

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from .agent_knowledge import check_folders
from .db import InstalledSkill, ProfessionalAgent, SkillVersion, transaction
from .repository_mcp import catalog_entries


LIBRARY_SEARCH = {
    "id": "library-search", "kind": "tool", "name": "知识库检索",
    "description": "按关键词查找已授权文件夹内可引用的资料片段。",
    "source": "智学内置", "version": "1",
    "permission": "只读检索 Agent 已授权的文件夹及其子目录",
    "dependencies": "本机知识库索引",
}


def library_search_entry(workspace, agents):
    grants = []
    for agent in agents:
        folders = agent.definition.get("knowledgeFolders", [])
        if not folders:
            continue
        try:
            check_folders(workspace, folders)
            available = True
        except HTTPException:
            available = False
        grants.append({"id": agent.id, "name": agent.name, "folders": folders, "available": available})
    status = "installed"
    if grants:
        status = "enabled" if any(grant["available"] for grant in grants) else "unavailable"
    return {**LIBRARY_SEARCH, "status": status, "canUpdate": False, "agents": grants}


def skill_entries(db, agents):
    grants = {}
    for agent in agents:
        for name in agent.definition["skills"]:
            grants.setdefault(name, []).append({"id": agent.id, "name": agent.name})
    newest = select(SkillVersion.name, func.max(SkillVersion.position).label("position")).group_by(SkillVersion.name).subquery()
    rows = db.execute(select(
        InstalledSkill.name, InstalledSkill.checksum, InstalledSkill.manifest, SkillVersion.position, newest.c.position,
    ).outerjoin(SkillVersion, (SkillVersion.name == InstalledSkill.name) & (SkillVersion.checksum == InstalledSkill.checksum))
        .outerjoin(newest, newest.c.name == InstalledSkill.name).order_by(InstalledSkill.name))
    for name, checksum, manifest, current, latest in rows:
        authorized = grants.get(name, [])
        status = "unavailable" if current is None else "enabled" if authorized else "installed"
        yield {
            **manifest, "id": name, "kind": "skill", "name": name, "checksum": checksum, "source": "本地 SKILL.md",
            "permission": "在确认发送范围后附带 Skill 原文",
            "dependencies": manifest["compatibility"], "status": status, "position": current,
            "canUpdate": current is not None and latest > current, "agents": authorized,
        }


def capability_catalog_router(engine, workspace, mcp_vault):
    router = APIRouter(prefix="/api/capabilities")

    @router.get("")
    def catalog(kind: Literal["tool", "mcp", "skill"]):
        with transaction(engine, write=False) as db:
            agents = db.scalars(select(ProfessionalAgent).order_by(ProfessionalAgent.name, ProfessionalAgent.id)).all()
            if kind == "mcp":
                items = list(catalog_entries(db, agents, mcp_vault))
            elif kind == "tool":
                items = [library_search_entry(workspace, agents)]
            else:
                items = list(skill_entries(db, agents))
            return {"items": items}

    return router