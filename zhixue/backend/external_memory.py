from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest, digest
import json
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr
from sqlalchemy import delete, select

from .db import ExternalMemoryConnection, ExternalMemoryCopy, LocalMemory, transaction
from .mem0 import RECIPIENT, ExternalRequestError, add_copy, find_copies, remove_copy, request_mem0
from .model_credentials import CREDENTIAL_FAILURES, NativeCredentialStore
from .model_study import utc_now, when_utc
from .model_study_input import Input, Revision


TERMINAL = ("removed", "cancelled", "failed")
ERRORS = {
    "credentials": "外接服务凭据暂不可用。请重新输入原密钥后重试。",
    "network": "暂时无法连接 Mem0，请检查网络后重试。",
    "not_sent": "未能建立 Mem0 连接，请检查网络后重试。",
    "rejected": "Mem0 拒绝保存这条记忆。请检查连接后重新确认发送。",
    "invalid_response": "Mem0 返回的回执无法核实，请重试查询。",
    "unknown_upload": "暂时无法确认这次外部保存。可重试查询，避免重复上传。",
    "delete_unconfirmed": "Mem0 尚未确认删除。可重试同步删除。",
}
Scope = Literal["shared", "private"]


class ExternalMemoryCredentials(NativeCredentialStore):
    scope = "external-memory"
    username = "mem0-api-key"


class Settings(Input):
    revision: Annotated[int, Field(strict=True, ge=0)]
    scopes: list[Scope] = Field(max_length=2)
    apiKey: SecretStr | None = None


class Export(Input):
    id: UUID
    memoryId: UUID
    revision: Revision


class ConfirmedExport(Export):
    confirmation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def checksum(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def memory_scope(entry):
    return "private" if entry.agent_id else "shared"


def content_hash(entry):
    return checksum(json.dumps([entry.agent_id, entry.text], ensure_ascii=False))


def still_matches(entry, copy):
    return (entry is not None and entry.state == "active" and entry.revision == copy.memory_revision
            and memory_scope(entry) == copy.memory_scope and content_hash(entry) == copy.content_hash)


def active_copies():
    return select(ExternalMemoryCopy).where(ExternalMemoryCopy.state.not_in(TERMINAL))


def request_removal(copy, clock):
    if copy.state in TERMINAL or copy.delete_requested:
        return
    if copy.state == "queued":
        copy.state, copy.attempt = "cancelled", False
    else:
        copy.delete_requested = True
        copy.attempt = copy.state not in ("sending", "deleting")
    copy.error, copy.updated_at = None, clock


def retire_external_memory(db, memory_id, clock):
    for copy in db.scalars(active_copies().where(ExternalMemoryCopy.memory_id == memory_id)):
        request_removal(copy, clock)


def key_for(settings, vault):
    if settings is None:
        return None
    try:
        key = vault.get(settings.reference)
    except CREDENTIAL_FAILURES:
        return None
    return key if key and compare_digest(checksum(key), settings.key_hash) else None


def copy_view(copy):
    return {
        "id": copy.id, "memoryId": copy.memory_id, "scope": copy.memory_scope,
        "state": "pending_delete" if copy.delete_requested and copy.state not in TERMINAL else copy.state,
        "working": copy.attempt or copy.state in ("sending", "deleting"),
        "error": ERRORS.get(copy.error),
        "updatedAt": copy.updated_at,
    }


def settings_view(settings, vault):
    return {
        "connected": bool(key_for(settings, vault)),
        "revision": settings.revision if settings else 0, "scopes": settings.scopes if settings else [],
        "provider": "Mem0", "recipient": RECIPIENT,
    }


def external_memory_router(engine, now, session_token, vault, transport):
    router = APIRouter(prefix="/api/external-memory")
    secret = session_token.encode("utf-8")

    def settings_at(db, revision):
        config = db.get(ExternalMemoryConnection, 1)
        if revision != (config.revision if config else 0):
            raise HTTPException(409, "外接配置已变化，请刷新后重试。")
        return config

    def prepare(db, data):
        config = db.get(ExternalMemoryConnection, 1)
        entry = db.get(LocalMemory, str(data.memoryId))
        if entry is None or entry.state != "active" or entry.revision != data.revision:
            raise HTTPException(409, "请选择仍在使用的已确认记忆。")
        scope = memory_scope(entry)
        if not key_for(config, vault) or scope not in config.scopes:
            raise HTTPException(409, "请在外接记忆中启用这一发送范围。")
        if db.scalar(active_copies().where(ExternalMemoryCopy.memory_id == entry.id).limit(1)):
            raise HTTPException(409, "这条记忆已有外部副本或进行中的同步。")
        data_basis = [str(data.id), entry.id, entry.revision, scope, content_hash(entry), config.reference, config.revision]
        confirmation = digest(secret, json.dumps(data_basis).encode("ascii"), "sha256").hex()
        return entry, scope, confirmation

    @router.get("")
    def status():
        with transaction(engine, write=False) as db:
            config = db.get(ExternalMemoryConnection, 1)
            copies = db.scalars(select(ExternalMemoryCopy).order_by(
                ExternalMemoryCopy.created_at.desc(), ExternalMemoryCopy.id,
            ))
            return {**settings_view(config, vault), "copies": [copy_view(copy) for copy in copies]}

    @router.put("/settings")
    def save_settings(data: Settings):
        scopes = list(dict.fromkeys(data.scopes))
        key = data.apiKey.get_secret_value().strip() if data.apiKey is not None else None
        if key is not None and (not 1 <= len(key) <= 512 or not key.isascii() or not key.isprintable()
                                or any(char.isspace() for char in key)):
            raise HTTPException(422, "请输入有效的 Mem0 API 密钥。")
        if key is not None:
            with transaction(engine, write=False) as db:
                config = settings_at(db, data.revision)
                if config and not compare_digest(config.key_hash, checksum(key)) and db.scalar(active_copies().limit(1)):
                    raise HTTPException(409, "更换密钥前，请等待当前外部副本删除完成。")
            try:
                find_copies(transport, key, str(uuid4()))
            except ExternalRequestError as error:
                raise HTTPException(422, ERRORS.get(error.reason, "无法验证 Mem0 连接。")) from None

        with transaction(engine) as db:
            config = settings_at(db, data.revision)
            if key is None and scopes and not key_for(config, vault):
                raise HTTPException(409, "请先输入可用的 Mem0 API 密钥。")
            if not config and key is None:
                return settings_view(None, vault)
            copies = db.scalars(active_copies()).all()
            if config and key is not None and copies and not compare_digest(config.key_hash, checksum(key)):
                raise HTTPException(409, "更换密钥前，请等待当前外部副本删除完成。")
            if config is None:
                config = ExternalMemoryConnection(id=1, revision=0, reference=str(uuid4()), scopes=[])
                db.add(config)
            if key is not None:
                try:
                    vault.put(config.reference, key)
                except CREDENTIAL_FAILURES:
                    raise HTTPException(503, "系统凭据库暂不可用，请重试。") from None
                config.key_hash = checksum(key)
            clock = when_utc(now())
            for copy in copies:
                if copy.memory_scope in config.scopes and copy.memory_scope not in scopes:
                    request_removal(copy, clock)
                if key is not None and copy.error == "credentials":
                    copy.attempt, copy.error = True, None
            config.scopes, config.revision = scopes, config.revision + 1
            return settings_view(config, vault)

    @router.post("/preview")
    def preview(data: Export):
        with transaction(engine, write=False) as db:
            entry, scope, confirmation = prepare(db, data)
            return {"text": entry.text, "recipient": RECIPIENT, "scope": scope, "confirmation": confirmation}

    @router.post("/copies", status_code=202)
    def enqueue(data: ConfirmedExport):
        with transaction(engine) as db:
            existing = db.get(ExternalMemoryCopy, str(data.id))
            if existing is not None:
                if (existing.memory_id != str(data.memoryId) or existing.memory_revision != data.revision
                        or not compare_digest(existing.confirmation, data.confirmation)):
                    raise HTTPException(409, "这次外部保存的确认已使用。")
                return copy_view(existing)
            entry, scope, confirmation = prepare(db, data)
            if not compare_digest(confirmation, data.confirmation):
                raise HTTPException(409, "发送内容或外接授权已变化，请重新预览。")
            clock = when_utc(now())
            copy = ExternalMemoryCopy(
                id=str(data.id), memory_id=entry.id, memory_revision=entry.revision, memory_scope=scope,
                content_hash=content_hash(entry), confirmation=confirmation, state="queued",
                remote_ids=[], delete_requested=False, attempt=True, created_at=clock, updated_at=clock,
            )
            db.add(copy)
            return copy_view(copy)

    @router.post("/copies/{identity}/retry")
    def retry(identity: UUID):
        with transaction(engine) as db:
            copy = db.get(ExternalMemoryCopy, str(identity))
            if (copy is None or copy.attempt or copy.state in (*TERMINAL, "sending", "deleting")
                    or not (copy.state in ("queued", "uncertain") or copy.delete_requested)):
                raise HTTPException(409, "当前外部状态无法重试，请刷新。")
            copy.attempt, copy.error = True, None
            return copy_view(copy)

    @router.delete("/copies/{identity}")
    def remove(identity: UUID):
        with transaction(engine) as db:
            copy = db.get(ExternalMemoryCopy, str(identity))
            if copy is None:
                raise HTTPException(404, "找不到外部副本。")
            request_removal(copy, when_utc(now()))
            return copy_view(copy)

    return router


@dataclass(frozen=True)
class Claim:
    identity: str
    key: str = field(repr=False)
    operation: Literal["upload", "lookup", "delete"]
    text: str | None
    ids: list[str]


def reserve(engine, vault, clock):
    with transaction(engine) as db:
        copy = db.scalar(active_copies().where(ExternalMemoryCopy.attempt.is_(True)).order_by(
            ExternalMemoryCopy.created_at, ExternalMemoryCopy.id,
        ).limit(1))
        if copy is None:
            return None
        config = db.get(ExternalMemoryConnection, 1)
        entry = db.get(LocalMemory, copy.memory_id)
        if not still_matches(entry, copy):
            request_removal(copy, clock)
        if copy.state == "queued" and (config is None or copy.memory_scope not in config.scopes):
            request_removal(copy, clock)
        if copy.state == "cancelled":
            return None
        key = key_for(config, vault)
        copy.attempt, copy.updated_at = False, clock
        if not key:
            copy.error = "credentials"
            return None
        if copy.state == "queued":
            copy.state = "sending"
            return Claim(copy.id, key, "upload", entry.text, [])
        if copy.delete_requested:
            copy.state = "deleting"
            return Claim(copy.id, key, "delete", None, copy.remote_ids)
        copy.state = "sending"
        return Claim(copy.id, key, "lookup", None, [])


def step(engine, vault=None, transport=request_mem0, now=utc_now):
    vault = vault if vault is not None else ExternalMemoryCredentials()
    claim = reserve(engine, vault, when_utc(now()))
    if claim is None:
        return False
    ids, error = claim.ids, None
    try:
        if claim.operation == "upload":
            ids = add_copy(transport, claim.key, claim.identity, claim.text)
        elif not ids:
            ids = find_copies(transport, claim.key, claim.identity)
            if not ids:
                raise ExternalRequestError("unknown_upload")
        if claim.operation == "delete":
            for remote_id in ids:
                remove_copy(transport, claim.key, remote_id)
    except ExternalRequestError as problem:
        error = problem.reason
    with transaction(engine) as db:
        copy = db.get(ExternalMemoryCopy, claim.identity)
        if claim.operation == "upload" and error == "not_sent":
            copy.state = "cancelled" if copy.delete_requested else "queued"
        elif error == "rejected":
            copy.state = "cancelled" if copy.delete_requested else "failed"
        elif claim.operation == "delete" and error is None:
            copy.state = "removed"
        else:
            copy.state = "synced" if ids else "uncertain"
            copy.attempt = copy.delete_requested and bool(ids) and error is None
        copy.remote_ids, copy.error, copy.updated_at = ids, error, when_utc(now())
    return True


def settle(engine):
    with transaction(engine) as db:
        for copy in db.scalars(select(ExternalMemoryCopy).where(ExternalMemoryCopy.state.in_(("sending", "deleting")))):
            copy.state = "synced" if copy.remote_ids else "uncertain"
            copy.attempt = True


def retain_external_memory(current, restored, clock):
    """External effects follow the live installation, not the contents of the restored archive."""
    restored.execute(delete(ExternalMemoryCopy))
    restored.execute(delete(ExternalMemoryConnection))
    config = current.get(ExternalMemoryConnection, 1)
    if config is None:
        return
    restored.add(ExternalMemoryConnection(id=1, reference=config.reference, key_hash=config.key_hash,
                                          revision=config.revision + 1, scopes=[]))
    for row in current.scalars(select(ExternalMemoryCopy)):
        copy = ExternalMemoryCopy(**{column.name: getattr(row, column.name) for column in ExternalMemoryCopy.__table__.columns})
        if copy.state == "queued" or not still_matches(restored.get(LocalMemory, copy.memory_id), copy):
            request_removal(copy, clock)
        restored.add(copy)
