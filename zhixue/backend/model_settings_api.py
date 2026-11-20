from _thread import LockType
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from sqlalchemy import func, select

from .db import ModelConnection, ModelCredentialPending, ModelRole, transaction
from .model_costs import Pricing
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, reserve_credential, retry_credential_cleanup
from .model_ledger import save_charge_reply
from .model_protocols import DEFAULT_PROVIDER, OUTPUT_LIMIT, PROMPT, PROVIDERS, ProbeResult, Transport, normalize_endpoint, probe_model, test_address
from .model_study import ERRORS, when_utc
from .model_usage import admit_probe


ROLES = ("main", "review", "mainBackup", "reviewBackup")
Revision = Annotated[int, Field(strict=True, ge=1)]
ShortName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=180)]
Endpoint = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
Protocol = Literal["chat-completions", "anthropic-messages", "gemini-generate-content"]


class ModelSettingsRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                # FastAPI's default validation response can include the submitted credential or raw body.
                location = error.errors()[0]["loc"][-1]
                message = {
                    "name": "请填写连接名称。",
                    "endpoint": "请填写有效的 API 地址。公网使用 HTTPS，本机可使用 HTTP，地址中不要嵌入凭据或查询参数。",
                    "model": "请填写服务商提供的模型名称。",
                    "apiKey": "请检查 API 密钥的格式。",
                    "protocol": "请为此服务商选择接口协议。",
                    "currency": "币种使用三个大写字母。",
                    "inputPerMillion": "输入单价须为非负十进制数，最多八位小数。",
                    "outputPerMillion": "输出单价须为非负十进制数，最多八位小数。",
                }.get(location, "请检查提交的模型设置。")
                return JSONResponse({"detail": message}, status_code=422)

        return handle


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionInput(ModelInput):
    name: ShortName
    provider: ShortName
    protocol: Protocol
    endpoint: Endpoint
    model: ShortName
    apiKey: Annotated[SecretStr, Field(min_length=1, max_length=4096)] | None = None

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        return normalize_endpoint(value)

    @field_validator("apiKey")
    @classmethod
    def valid_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and any(not 33 <= ord(char) <= 126 for char in value.get_secret_value()):
            raise ValueError("密钥包含空白或无法用于认证的字符。")
        return value


class EditConnection(ConnectionInput):
    revision: Revision


class TestConnection(ModelInput):
    revision: Revision


class UpdatePricing(ModelInput):
    revision: Revision
    previousPricing: Pricing | None
    pricing: Pricing | None


class RoleUpdates(ModelInput):
    main: UUID | None = None
    review: UUID | None = None
    mainBackup: UUID | None = None
    reviewBackup: UUID | None = None


def roles_view(db) -> dict:
    return {**dict.fromkeys(ROLES), **dict(db.execute(select(ModelRole.role, ModelRole.connection_id)).all())}


def connection_view(item: ModelConnection) -> dict:
    return {
        "id": item.id, "name": item.name, "provider": item.provider, "protocol": item.protocol,
        "endpoint": item.endpoint, "model": item.model, "hasCredential": item.credential_ref is not None,
        "testAddress": test_address(item.endpoint, item.protocol, item.model),
        "revision": item.revision, "test": item.last_test, "pricing": item.pricing,
    }


def get_connection(db, identity: str) -> ModelConnection:
    item = db.get(ModelConnection, identity)
    if item is None:
        raise HTTPException(404, "找不到这项服务连接。")
    return item


def retire_credential(db, reference: str | None) -> None:
    if reference:
        db.add(ModelCredentialPending(reference=reference, reserved=False))


def apply_connection(
    engine, store: CredentialStore, data: ConnectionInput, *, identity: str | None = None, revision: int | None = None,
) -> dict:
    if data.provider not in PROVIDERS or data.protocol not in PROVIDERS[data.provider][1]:
        raise HTTPException(422, "请为此服务商选择可用的接口协议。")
    try:
        test_address(data.endpoint, data.protocol, data.model)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None

    old_reference = None
    key = data.apiKey.get_secret_value() if data.apiKey is not None else None
    new_reference = reserve_credential(engine) if key is not None else None
    try:
        with transaction(engine) as db:
            if identity is None:
                item = ModelConnection(id=str(uuid4()), revision=0)
            else:
                item = get_connection(db, identity)
                if item.revision != revision:
                    raise HTTPException(409, "此连接已变更，请刷新后重新编辑。")

            destination = (data.provider, data.protocol, data.endpoint)
            same_destination = destination == (item.provider, item.protocol, item.endpoint)
            edits_key = "apiKey" in data.model_fields_set
            if item.credential_ref and not same_destination and not edits_key:
                raise HTTPException(409, "更改服务商、接口协议或地址时，请重新填写或移除原密钥。")
            if new_reference is not None and key is not None:
                reservation = db.get(ModelCredentialPending, new_reference)
                if reservation is None or not reservation.reserved:
                    raise HTTPException(409, "凭据写入已取消，请重新保存。")
                try:
                    store.put(new_reference, key)
                except CREDENTIAL_FAILURES:
                    raise HTTPException(503, "系统凭据库无法写入，请解锁后重试。") from None
                db.delete(reservation)
            if edits_key:
                old_reference = item.credential_ref
                retire_credential(db, old_reference)
                item.credential_ref = new_reference
            if not same_destination or item.model != data.model or edits_key:
                item.last_test = None
                item.test_token = None
                item.pricing = None
            item.name = data.name
            item.provider, item.protocol, item.endpoint = destination
            item.model = data.model
            item.revision += 1
            if identity is None:
                db.add(item)
            result = connection_view(item)
    finally:
        if new_reference or old_reference:
            retry_credential_cleanup(engine, store, abandon=new_reference)
    return result


def run_connection_test(engine, store: CredentialStore, transport: Transport, identity: str, revision: int,
                        now: Callable[[], datetime]) -> dict:
    attempt = str(uuid4())
    with transaction(engine) as db:
        item = get_connection(db, identity)
        if item.revision != revision:
            raise HTTPException(409, "连接已变更，请查看新地址后重新确认测试。")
        item.test_token = attempt
        provider, protocol, model, reference = item.provider, item.protocol, item.model, item.credential_ref
        address = test_address(item.endpoint, protocol, model)

    started = monotonic()
    try:
        key = store.get(reference) if reference else None
    except CREDENTIAL_FAILURES:
        result = ProbeResult("credential_store")
    else:
        if not key and (reference or provider != "custom"):
            result = ProbeResult("missing_key")
        else:
            with transaction(engine) as db:
                item = db.get(ModelConnection, identity)
                if item is None or item.revision != revision or item.test_token != attempt:
                    raise HTTPException(409, "此连接或测试已变更，请查看当前设置。")
                clock = when_utc(now())
                issue = admit_probe(db, attempt, identity, clock, connection_view(item), PROMPT, OUTPUT_LIMIT)
                if issue:
                    item.test_token = None
            if issue:
                raise HTTPException(409, ERRORS.get(issue, "模型请求并发数已满，请等现有请求完成后重试。"))
            result = probe_model(provider, protocol, address, model, key, transport)
            save_charge_reply(engine, "probe", attempt, result.status, result.usage, now())
    report = {
        "status": result.status,
        "checkedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "durationMs": round((monotonic() - started) * 1000),
        "capabilities": ["text"] if result.status == "passed" else [],
        "usage": {key: value for key, value in (result.usage or {}).items() if key.endswith("Tokens")} or None,
    }

    with transaction(engine) as db:
        item = db.get(ModelConnection, identity)
        current = item is not None and item.revision == revision and item.test_token == attempt
        if current:
            item.last_test = report
            item.test_token = None
    if not current:
        raise HTTPException(409, "此连接或测试已变更，请查看当前设置。")
    return report


def model_settings_router(engine, store: CredentialStore, transport: Transport, now: Callable[[], datetime],
                          recipient_changes: LockType) -> APIRouter:
    router = APIRouter(prefix="/api/models", route_class=ModelSettingsRoute)

    @router.get("")
    def settings():
        with transaction(engine, write=False) as db:
            return {
                "defaultProvider": DEFAULT_PROVIDER,
                "providers": [{"id": identity, "label": label, "protocols": protocols}
                              for identity, (label, protocols) in PROVIDERS.items()],
                "connections": [connection_view(item) for item in db.scalars(
                    select(ModelConnection).order_by(func.unicode_fold(ModelConnection.name), ModelConnection.id)
                )],
                "roles": roles_view(db),
                "testPrompt": PROMPT,
                "credentialCleanupPending": db.scalar(select(ModelCredentialPending.reference)
                    .where(ModelCredentialPending.reserved.is_(False)).limit(1)) is not None,
            }

    @router.post("/connections", status_code=201)
    def add_connection(data: ConnectionInput):
        return apply_connection(engine, store, data)

    @router.put("/connections/{identity}")
    def edit_connection(identity: UUID, data: EditConnection):
        with recipient_changes:
            return apply_connection(engine, store, data, identity=str(identity), revision=data.revision)

    @router.put("/connections/{identity}/pricing")
    def edit_pricing(identity: UUID, data: UpdatePricing):
        with transaction(engine) as db:
            item = get_connection(db, str(identity))
            if item.revision != data.revision:
                raise HTTPException(409, "此连接已变更，请查看当前设置后重新配置单价。")
            previous = data.previousPricing.model_dump() if data.previousPricing is not None else None
            if item.pricing != previous:
                raise HTTPException(409, "单价已在别处变更，请重新打开单价设置。")
            item.pricing = data.pricing.model_dump() if data.pricing is not None else None
            return connection_view(item)

    @router.delete("/connections/{identity}")
    def delete_connection(identity: UUID, revision: Annotated[int, Query(ge=1)]):
        with recipient_changes, transaction(engine) as db:
            item = get_connection(db, str(identity))
            if item.revision != revision:
                raise HTTPException(409, "此连接已变更，请查看当前设置后重新确认。")
            used = db.scalar(select(ModelRole.role).where(ModelRole.connection_id == item.id).limit(1))
            if used:
                raise HTTPException(409, "此连接正在使用，请先更换相关模型分工。")
            retire_credential(db, item.credential_ref)
            db.delete(item)
        return {"deleted": True, "credentialCleanupPending": retry_credential_cleanup(engine, store)}

    @router.post("/credential-cleanup")
    def clean_credentials():
        return {"credentialCleanupPending": retry_credential_cleanup(engine, store)}

    @router.patch("/roles")
    def select_roles(updates: RoleUpdates):
        with transaction(engine) as db:
            for role, connection in updates.model_dump(exclude_unset=True).items():
                current = db.get(ModelRole, role)
                if connection is not None:
                    target = get_connection(db, str(connection))
                    if current is None:
                        db.add(ModelRole(role=role, connection_id=target.id))
                    else:
                        current.connection_id = target.id
                elif current is not None:
                    db.delete(current)
            return {"roles": roles_view(db)}

    @router.post("/connections/{identity}/test")
    def test_connection(identity: UUID, data: TestConnection):
        return run_connection_test(engine, store, transport, str(identity), data.revision, now)

    return router
