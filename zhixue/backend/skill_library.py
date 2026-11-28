import re
from difflib import unified_diff
from hashlib import sha256
from hmac import compare_digest, digest

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .db import InstalledSkill, SkillVersion, transaction
from .mobile_api import workspace_id


MAX_DOCUMENT_BYTES = 256 * 1024
MAX_FRONTMATTER_BYTES = 16 * 1024
FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class SkillLoader(yaml.BaseLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self.depth = 0

    def compose_node(self, parent, index):
        event = self.peek_event()
        if event.anchor or event.tag or self.depth >= 24:
            raise yaml.YAMLError("Unsupported skill frontmatter.")
        self.depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        result = super().construct_mapping(node, deep)
        if len(result) != len(node.value):
            raise yaml.YAMLError("Duplicate skill field.")
        return result


def field(data: dict, name: str, limit: int, *, required: bool = False) -> str | None:
    if name not in data and not required:
        return None
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise HTTPException(422, f"Skill 的 {name} 字段需要有效的文字。")
    return value.strip()


async def skill_document(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_DOCUMENT_BYTES:
            raise HTTPException(413, "SKILL.md 不能超过 256 KiB。")
        raw.extend(chunk)
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "请使用 UTF-8 编码的 SKILL.md。") from None
    normalized = source.removeprefix("\ufeff").replace("\r\n", "\n")
    header = FRONTMATTER.match(normalized)
    if not header or not normalized[header.end():].strip():
        raise HTTPException(422, "SKILL.md 需要 YAML 元数据和正文。")
    if len(header[1].encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise HTTPException(422, "Skill 元数据不能超过 16 KiB。")
    try:
        data = yaml.load(header[1], Loader=SkillLoader)
    except yaml.YAMLError:
        raise HTTPException(422, "无法读取 Skill 的 YAML 元数据。请检查格式与重复字段。") from None
    if not isinstance(data, dict):
        raise HTTPException(422, "Skill 元数据需要使用字段和值。")
    name = field(data, "name", 64, required=True)
    if not NAME.fullmatch(name):
        raise HTTPException(422, "Skill 名称只能使用小写字母、数字和单个连字符。")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict) or not all(isinstance(value, str) for value in metadata.values()):
        raise HTTPException(422, "Skill 的 metadata 字段需要使用文字键值。")
    return {
        "name": name,
        "description": field(data, "description", 1024, required=True),
        "license": field(data, "license", 1024),
        "compatibility": field(data, "compatibility", 500),
        "version": field(metadata, "version", 128),
        "declaredTools": field(data, "allowed-tools", 1024),
        "source": source,
        "checksum": sha256(raw).hexdigest(),
    }


FIELDS = ("declaredTools", "compatibility", "license", "version", "description")
CHECKSUM = re.compile(r"[0-9a-f]{64}")


class RollbackInput(BaseModel):
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: str = Field(pattern=r"^[0-9a-f]{64}$")


def skill_view(skill: InstalledSkill | SkillVersion, *, detail: bool = False) -> dict:
    result = {**skill.manifest, "name": skill.name, "checksum": skill.checksum, "status": "installed"}
    if detail:
        result["source"] = skill.source
    return result


def installed_skill(db, name: str) -> InstalledSkill:
    skill = db.get(InstalledSkill, name)
    if skill is None:
        raise HTTPException(404, "找不到已安装的 Skill。")
    return skill


def historical_version(db, name: str, checksum: str) -> SkillVersion:
    version = db.get(SkillVersion, (name, checksum))
    if version is None:
        raise HTTPException(404, "找不到这个 Skill 历史版本。")
    return version


def activate(skill: InstalledSkill, version: SkillVersion) -> None:
    skill.checksum = version.checksum
    skill.source = version.source
    skill.manifest = version.manifest
    skill.activation += 1


def source_diff(before: str, after: str) -> str | None:
    left, right = before.splitlines(), after.splitlines()
    # Keep comparison work bounded for imported documents with unusually many lines.
    if len(left) * len(right) > 2_250_000:
        return None
    return "\n".join(unified_diff(left, right, fromfile="当前版本", tofile="目标版本", lineterm=""))


def skill_library_router(engine, session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/skills")
    secret = session_token.encode("utf-8")

    def signature(db, action: str, current: InstalledSkill, target: str) -> str:
        parts = ("skill-version", action, workspace_id(db), current.name, str(current.activation), current.checksum, target)
        return digest(secret, "\0".join(parts).encode("utf-8"), "sha256").hex()

    def comparison(db, action: str, current: InstalledSkill, target: dict) -> dict:
        return {
            "current": skill_view(current, detail=True),
            "changes": [
                {"field": key, "before": current.manifest[key], "after": target[key]}
                for key in FIELDS if current.manifest[key] != target[key]
            ],
            "diff": source_diff(current.source, target["source"]),
            "confirmation": signature(db, action, current, target["checksum"]),
        }

    def require_confirmation(db, action, current, target, provided):
        if not provided or not CHECKSUM.fullmatch(provided) or not compare_digest(
            signature(db, action, current, target), provided
        ):
            raise HTTPException(409, "Skill 当前版本或文件已变化，请重新查看差异后确认。")

    @router.get("")
    def list_skills():
        with transaction(engine, write=False) as db:
            rows = db.scalars(select(InstalledSkill).order_by(InstalledSkill.name))
            return {"items": [skill_view(skill) for skill in rows]}

    @router.get("/{name}")
    def detail(name: str):
        with transaction(engine, write=False) as db:
            return skill_view(installed_skill(db, name), detail=True)

    @router.get("/{name}/versions")
    def list_versions(name: str):
        with transaction(engine, write=False) as db:
            current = installed_skill(db, name)
            rows = db.scalars(select(SkillVersion).where(SkillVersion.name == name).order_by(SkillVersion.position.desc()))
            return {
                "currentChecksum": current.checksum,
                "items": [dict(skill_view(row), position=row.position) for row in rows],
            }

    @router.get("/{name}/versions/{checksum}")
    def read_version(name: str, checksum: str):
        with transaction(engine, write=False) as db:
            current = installed_skill(db, name)
            version = historical_version(db, name, checksum)
            document = skill_view(version, detail=True)
            return {
                "document": document,
                "position": version.position,
                "comparison": None if current.checksum == checksum else comparison(db, "rollback", current, document),
            }

    @router.post("/{name}/rollback")
    def rollback(name: str, requested: RollbackInput):
        with transaction(engine) as db:
            current = installed_skill(db, name)
            version = historical_version(db, name, requested.checksum)
            require_confirmation(db, "rollback", current, version.checksum, requested.confirmation)
            activate(current, version)
            return skill_view(current, detail=True)

    @router.post("/preview")
    def preview(document: dict = Depends(skill_document)):
        with transaction(engine, write=False) as db:
            current = db.get(InstalledSkill, document["name"])
            if current is not None and current.checksum != document["checksum"]:
                document["update"] = comparison(db, "update", current, document)
            return document

    @router.post("")
    def install(request: Request, document: dict = Depends(skill_document)):
        name = document.pop("name")
        source = document.pop("source")
        checksum = document.pop("checksum")
        if request.headers.get("x-skill-checksum") != checksum:
            raise HTTPException(409, "文件内容与预览不一致，请重新预览。")
        provided = request.headers.get("x-skill-update-confirmation")
        with transaction(engine) as db:
            current = db.get(InstalledSkill, name)
            if current is None:
                if provided:
                    raise HTTPException(409, "Skill 当前版本已变化，请重新预览。")
                current = InstalledSkill(name=name, checksum=checksum, source=source, manifest=document, activation=1)
                db.add(current)
                db.flush()
                db.add(SkillVersion(name=name, checksum=checksum, position=1, source=source, manifest=document))
            elif current.checksum != checksum:
                require_confirmation(db, "update", current, checksum, provided)
                version = db.get(SkillVersion, (name, checksum))
                if version is None:
                    position = db.scalar(select(func.max(SkillVersion.position)).where(SkillVersion.name == name))
                    version = SkillVersion(name=name, checksum=checksum, position=position + 1, source=source, manifest=document)
                    db.add(version)
                activate(current, version)
            return skill_view(current, detail=True)

    return router

