from datetime import UTC
from hashlib import sha256
from hmac import compare_digest, digest
import json
import re
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr, StringConstraints
from sqlalchemy import select

from .db import ChatMessage, Conversation, ProfessionalAgent, RepositoryMcpConnection, transaction
from .mcp_protocol import ENDPOINTS, TOOLS, McpError, McpSession
from .mobile_api import workspace_id
from .model_credentials import CREDENTIAL_FAILURES, NativeCredentialStore
from .model_study import when_utc
from .model_study_input import Input, Revision


NAMES = {"github": "GitHub", "gitlab": "GitLab"}
ERRORS = {
    "configuration": "MCP 服务地址不可用。",
    "credential": "MCP 拒绝了凭据或访问权限。",
    "network": "暂时无法连接 MCP 服务。",
    "rate": "MCP 已限制本次请求，请稍后重试。",
    "response": "MCP 返回的响应无法核实。",
    "protocol": "MCP 协议版本不受支持。",
    "tool": "MCP 无法读取指定条目。请核对仓库、编号和凭据权限。",
}
MAX_EXCERPT = 3000
READ_TTL = 15 * 60
Provider = Literal["github", "gitlab"]
Kind = Literal["issue", "merge"]
Repository = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
RepositoryGrant = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=508)]
Number = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


class RepositoryMcpCredentials(NativeCredentialStore):
    scope = "repository-mcp"
    username = "mcp-access-token"


class Settings(Input):
    revision: Annotated[int, Field(strict=True, ge=0)]
    enabled: bool
    repositories: list[Repository] = Field(max_length=40)
    credential: SecretStr | None = None


class Read(Input):
    agentId: UUID
    threadId: UUID
    messageId: UUID
    repository: RepositoryGrant
    kind: Kind
    number: Number


class McpEvidence(Input):
    repository: RepositoryGrant
    kind: Kind
    number: Number
    source: Annotated[str, StringConstraints(min_length=8, max_length=650)]
    excerpt: Annotated[str, StringConstraints(min_length=1, max_length=MAX_EXCERPT)]
    connectionRevision: Revision
    agentRevision: Revision
    expiresAt: Annotated[int, Field(strict=True, ge=1)]
    truncated: bool
    confirmation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def checksum(value):
    return sha256(value.encode("utf-8")).hexdigest()


def normalize_repository(provider, path):
    parts = path.split("/")
    if (not 2 <= len(parts) <= (2 if provider == "github" else 10)
            or any(not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,99}", part) for part in parts)):
        raise HTTPException(422, "请输入完整的仓库路径，例如 owner/repository。")
    return path.lower() if provider == "github" else path


def split_repository(key):
    provider, _, path = key.partition(":")
    if provider not in ENDPOINTS:
        raise HTTPException(422, "请选择 GitHub 或 GitLab 仓库。")
    return provider, normalize_repository(provider, path)


def grants_for(db, keys):
    normalized = {}
    for key in keys:
        provider, path = split_repository(key)
        config = db.get(RepositoryMcpConnection, provider)
        if not config or path not in config.repositories:
            raise HTTPException(409, "所选 MCP 仓库已移除，请重新选择。")
        normalized[provider + ":" + path] = config.revision
    return normalized


def token_for(config, vault):
    if not config or not config.credential_ref or not config.credential_hash:
        return None
    try:
        token = vault.get(config.credential_ref)
    except CREDENTIAL_FAILURES:
        return None
    return token if token and compare_digest(checksum(token), config.credential_hash) else None


def settings_view(provider, config, vault):
    return {
        "id": provider, "name": NAMES[provider], "endpoint": ENDPOINTS[provider],
        "revision": config.revision if config else 0, "version": config.version if config else None,
        "enabled": config.enabled if config else False, "connected": bool(token_for(config, vault)),
        "repositories": config.repositories if config else [], "tools": config.tools if config else [],
    }


def connections(db):
    return {item.provider: item for item in db.scalars(select(RepositoryMcpConnection))}


def connection_choices(db, vault, agent=None):
    allowed = set(agent.definition.get("mcpRepositories", [])) if agent else None
    options = []
    for provider, row in sorted(connections(db).items()):
        if not token_for(row, vault):
            continue
        if agent and not row.enabled:
            continue
        for path in row.repositories:
            identity = provider + ":" + path
            if allowed is None or identity in allowed:
                options.append({
                    "id": identity, "name": NAMES[provider] + " · " + path,
                    "endpoint": ENDPOINTS[provider],
                    "kinds": [kind for kind, tool in TOOLS[provider].items() if tool in row.tools],
                })
    return options


def catalog_entries(db, agents, vault):
    for provider, connection in sorted(connections(db).items()):
        info = settings_view(provider, connection, vault)
        grants = []
        for agent in agents:
            valid = [key.partition(":")[2] for key in agent.definition.get("mcpRepositories", [])
                     if key.startswith(provider + ":") and key.partition(":")[2] in connection.repositories]
            if valid:
                grants.append({"id": agent.id, "name": agent.name, "repositories": valid,
                               "available": info["connected"] and info["enabled"]})
        status = "unavailable" if not info["connected"] else "enabled" if info["enabled"] and grants else "installed"
        yield {
            "id": provider, "kind": "mcp", "name": NAMES[provider] + " MCP",
            "description": "读取已授权仓库的 Issue 与合并请求。",
            "source": ENDPOINTS[provider], "version": info["version"],
            "permission": "只读指定仓库中的 Issue 与合并请求",
            "dependencies": "官方托管 MCP、网络与有效凭据",
            "status": status, "canUpdate": False, "agents": grants,
        }


def repository_authorization(db, agent_id, identity, kind):
    provider, path = split_repository(identity)
    agent = db.get(ProfessionalAgent, agent_id)
    config = db.get(RepositoryMcpConnection, provider)
    canonical = provider + ":" + path
    if (not agent or not config or not config.enabled or not config.credential_ref or not config.credential_hash
            or path not in config.repositories or canonical not in agent.definition.get("mcpRepositories", [])
            or TOOLS[provider][kind] not in config.tools):
        raise HTTPException(409, "此 Agent 当前没有读取该仓库条目的授权。")
    return agent, config, path


def evidence_current(db, agent_scope):
    info = agent_scope.get("mcp") if agent_scope else None
    if not info:
        return True
    try:
        agent, config, _ = repository_authorization(db, agent_scope["id"], info["repository"], info["kind"])
    except HTTPException:
        return False
    return agent.revision == info["agentRevision"] and config.revision == info["connectionRevision"]


def epoch(now):
    return int(when_utc(now()).replace(tzinfo=UTC).timestamp())


def sign(secret, workspace, agent, thread, message, data):
    value = json.dumps(["repository-mcp", workspace, agent, thread, message, data], ensure_ascii=False,
                       separators=(",", ":"), sort_keys=True)
    return digest(secret, value.encode("utf-8"), "sha256").hex()


def evidence_scope(db, agent_id, thread, message, evidence, secret, now):
    if not agent_id:
        raise HTTPException(409, "请选择获授权的专业 Agent。")
    data = evidence.model_dump(exclude={"confirmation"})
    if (not compare_digest(evidence.confirmation, sign(secret, workspace_id(db), agent_id, thread, message, data))
            or evidence.expiresAt < epoch(now)
            or not evidence_current(db, {"id": agent_id, "mcp": data})):
        raise HTTPException(409, "仓库资料或读取授权已变化，请重新读取并预览。")
    return data


def source_url(provider, repository, kind, number):
    if provider == "github":
        suffix = "issues" if kind == "issue" else "pull"
        return f"https://github.com/{repository}/{suffix}/{number}"
    suffix = "issues" if kind == "issue" else "merge_requests"
    return f"https://gitlab.com/{repository}/-/{suffix}/{number}"


def response_excerpt(result, token):
    blocks = result.get("content")
    text = "\n\n".join(item["text"] for item in blocks
                        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
                        ) if isinstance(blocks, list) else ""
    if not text and isinstance(result.get("structuredContent"), dict):
        text = json.dumps(result["structuredContent"], ensure_ascii=False, indent=2)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text if char in ("\n", "\t") or char.isprintable()).strip()
    if not text or token in text:
        raise McpError("response")
    return text[:MAX_EXCERPT], len(text) > MAX_EXCERPT


def repository_mcp_router(engine, vault, transport, token, now):
    router = APIRouter(prefix="/api/repository-mcp")
    secret = token.encode("utf-8")

    def at_revision(db, provider, revision):
        config = db.get(RepositoryMcpConnection, provider)
        if revision != (config.revision if config else 0):
            raise HTTPException(409, "MCP 配置已变化，请刷新后再确认。")
        return config

    @router.get("")
    def settings():
        with transaction(engine, write=False) as db:
            rows = connections(db)
            return {"items": [settings_view(provider, rows.get(provider), vault) for provider in ENDPOINTS]}

    @router.get("/repositories")
    def repositories():
        with transaction(engine, write=False) as db:
            return {"items": connection_choices(db, vault)}

    @router.put("/{provider}")
    def configure(provider: Provider, data: Settings):
        repositories = list(dict.fromkeys(normalize_repository(provider, path) for path in data.repositories))
        supplied = data.credential.get_secret_value().strip() if data.credential is not None else None
        if supplied is not None and (not 1 <= len(supplied) <= 2048 or not supplied.isascii()
                                     or not supplied.isprintable() or any(char.isspace() for char in supplied)):
            raise HTTPException(422, "请输入有效的 MCP 访问令牌。")
        with transaction(engine, write=False) as db:
            config = at_revision(db, provider, data.revision)
            credential = supplied if supplied is not None else token_for(config, vault)
            tools, version = (config.tools, config.version) if config else ([], None)
        if data.enabled or supplied is not None:
            if not credential or (data.enabled and not repositories):
                raise HTTPException(422, "请填写有效凭据并指定要授权的仓库。")
            try:
                with McpSession(provider, credential, transport) as session:
                    tools, version = session.tools(), session.version
                if not tools:
                    raise McpError("tool")
            except McpError as error:
                raise HTTPException(422, ERRORS[error.reason]) from None
        with transaction(engine) as db:
            config = at_revision(db, provider, data.revision)
            if config is None:
                config = RepositoryMcpConnection(provider=provider, revision=0, repositories=[])
                db.add(config)
            removed = {provider + ":" + path for path in set(config.repositories) - set(repositories)}
            if supplied is not None:
                ref = config.credential_ref or str(uuid4())
                try:
                    vault.put(ref, supplied)
                except CREDENTIAL_FAILURES:
                    raise HTTPException(503, "系统凭据库暂不可用。") from None
                config.credential_ref, config.credential_hash = ref, checksum(supplied)
            config.revision += 1
            config.enabled, config.repositories, config.tools, config.version = data.enabled, repositories, tools, version
            if removed:
                for agent in db.scalars(select(ProfessionalAgent)):
                    authorized = agent.definition.get("mcpRepositories", [])
                    remaining = [key for key in authorized if key not in removed]
                    if remaining != authorized:
                        agent.definition = {**agent.definition, "mcpRepositories": remaining}
                        agent.revision += 1
                        agent.confirmation = ""
            return settings_view(provider, config, vault)

    @router.post("/read")
    def read(data: Read):
        identity, message, thread = str(data.agentId), str(data.messageId), str(data.threadId)
        with transaction(engine, write=False) as db:
            conversation = db.get(Conversation, thread)
            if db.get(ChatMessage, message) or (conversation and conversation.lesson_id):
                raise HTTPException(409, "请选择一条待发送的新问题。")
            agent, config, repository = repository_authorization(db, identity, data.repository, data.kind)
            credential = token_for(config, vault)
            if not credential:
                raise HTTPException(409, "MCP 凭据暂不可用，请检查能力库配置。")
            provider = config.provider
            scope = {
                "repository": provider + ":" + repository, "kind": data.kind, "number": data.number,
                "source": source_url(provider, repository, data.kind, data.number),
                "connectionRevision": config.revision, "agentRevision": agent.revision,
            }
        try:
            with McpSession(provider, credential, transport) as session:
                excerpt, truncated = response_excerpt(session.read(repository, data.kind, data.number), credential)
        except McpError as error:
            raise HTTPException(502, ERRORS[error.reason]) from None
        with transaction(engine, write=False) as db:
            if db.get(ChatMessage, message) or not evidence_current(db, {"id": identity, "mcp": scope}):
                raise HTTPException(409, "仓库授权或消息已变化，请重新选择。")
            result = {**scope, "excerpt": excerpt, "truncated": truncated, "expiresAt": epoch(now) + READ_TTL}
            return {**result, "confirmation": sign(secret, workspace_id(db), identity, thread, message, result)}

    return router
