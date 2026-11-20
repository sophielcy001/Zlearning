from base64 import b64encode
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from hmac import compare_digest, digest
from http.client import HTTPException as HTTPClientError
from io import BytesIO
import json
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4
import wave

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .db import GenerationSettings, ModelCharge, ModelConnection, RecognitionGrant, transaction
from .mobile_api import workspace_id
from .model_costs import ModelUsage
from .model_credentials import CREDENTIAL_FAILURES
from .model_ledger import prepare_charge, save_charge_reply, utc_clock
from .model_protocols import (
    CHAT, COMPLETION_LIMIT_PROVIDERS, TextReply, fields, http_failure, parse_text_reply,
    post_bytes, reply_usage, response_object, test_address, token_usage, visible_text,
)
from .model_study_input import DEFAULT_CONCURRENCY
from .model_usage import active_calls, billed_calls, billing_day, daily_capacity
from .money_budget import MONEY_ERRORS, amount_issue
from .voice import MAX_SECONDS, SAMPLE_RATE


RecognitionKind = Literal["voice", "ocr"]
CALL_KIND = {"voice": "voice_transcription", "ocr": "page_ocr"}
SCOPE = {
    "voice": f"仅发送这次主动录制的单段 WAV 音频（最长 {MAX_SECONDS} 秒）、录音语言和模型名称。",
    "ocr": "仅发送这次选择的单页 PNG 图像（最长边 1400 像素）与识字指令、模型名称。",
}
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_OCR_TOKENS = 4096
OCR_PROMPT = "转写这一页可辨认的文字，尽量保留行序和公式。只返回识别出的文字。"
APPROVAL_ERROR = "此识别用途的授权已变化，请核对接收方后重新授权。"
REPLY_ERRORS = {
    "network": "无法连接所选识别服务，请检查连接后手动重试。",
    "timeout": "外部识别超时。可改用本机识别或手动输入。",
    "redirect": "识别服务地址发生跳转，请在服务连接中核对地址。",
    "unauthorized": "识别服务拒绝了凭据。请检查所选连接的密钥与权限。",
    "quota": "识别服务的额度或余额不足。",
    "rate_limited": "识别服务限流，请稍后手动重试。",
    "request_rejected": "识别服务不接受此请求，请核对模型和接口是否支持此用途。",
}


class ServiceChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connectionId: UUID


class AuthorizeChoice(ServiceChoice):
    confirmation: str = Field(pattern=r"^[0-9a-f]{64}$")


def endpoint_for(connection: ModelConnection, kind: RecognitionKind) -> str:
    if kind == "ocr":
        return test_address(connection.endpoint, CHAT, connection.model)
    address = connection.endpoint.removesuffix("/chat/completions")
    return address if address.endswith("/audio/transcriptions") else f"{address}/audio/transcriptions"


def destination(connection: ModelConnection, kind: RecognitionKind) -> dict:
    return {
        "connectionId": connection.id, "revision": connection.revision, "name": connection.name,
        "model": connection.model, "endpoint": endpoint_for(connection, kind), "kind": kind, "scope": SCOPE[kind],
    }


def current_connection(db, grant: RecognitionGrant | None) -> ModelConnection | None:
    if grant is None or not grant.connection_id or grant.scope != SCOPE.get(grant.kind):
        return None
    connection = db.get(ModelConnection, grant.connection_id)
    if connection and connection.protocol == CHAT and connection.revision == grant.connection_revision:
        return connection
    return None


def require_grant(db, kind: RecognitionKind, identity: str) -> ModelConnection:
    grant = db.get(RecognitionGrant, kind)
    connection = current_connection(db, grant)
    if connection is None or grant.approval_id != identity:
        raise HTTPException(409, APPROVAL_ERROR)
    return connection


def multipart_recording(samples: bytes, language: str, model: str) -> tuple[str, bytes]:
    recording = BytesIO()
    with wave.open(recording, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(samples)
    boundary = uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in (("model", model), ("language", language), ("response_format", "json"))
    ]
    parts.extend((
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recording.wav"\r\n'
        'Content-Type: audio/wav\r\n\r\n'.encode(),
        recording.getvalue(), f"\r\n--{boundary}--\r\n".encode(),
    ))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def transcription_usage(data: dict) -> ModelUsage | None:
    raw = fields(data.get("usage"))
    usage = token_usage(raw, (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                              ("totalTokens", "total_tokens")))
    seconds = raw.get("seconds")
    if raw.get("type") == "duration" and type(seconds) in (int, Decimal) and 0 <= seconds < 10**12:
        usage["audioSeconds"] = float(seconds)
    return usage or None


def media_reply(kind: RecognitionKind, address: str, key: str | None, content_type: str, body: bytes,
                upload_guard) -> TextReply:
    headers = {"Content-Type": content_type}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        status, raw = post_bytes(address, headers, body, timeout=45, upload_guard=upload_guard)
    except TimeoutError:
        return TextReply("timeout")
    except (OSError, HTTPClientError):
        return TextReply("network")
    data = response_object(raw)
    usage = None
    if data is not None:
        usage = transcription_usage(data) if kind == "voice" else reply_usage(CHAT, data, address)
    if issue := http_failure(status, data):
        return TextReply(issue, usage)
    if data is None:
        return TextReply("invalid_response")
    if kind == "ocr":
        return parse_text_reply(CHAT, data, usage)
    text = visible_text(data.get("text"))
    return TextReply("passed" if text else "no_text", usage, text)


class Recognition:
    def __init__(self, engine, vault, now):
        self.engine = engine
        self.vault = vault
        self.now = now
        self.recipient_changes = Lock()

    def voice(self, approval: str, samples: bytes, language: str) -> str:
        return self.send("voice", approval, lambda model, _: multipart_recording(samples, language, model)).text

    def ocr(self, approval: str, image: bytes) -> TextReply:
        if len(image) > MAX_PAGE_BYTES:
            raise HTTPException(422, "这页图像超出外部识别的发送范围，可使用本机识别或手动录入。")
        image_url = "data:image/png;base64," + b64encode(image).decode("ascii")

        def message(model, provider):
            limit = "max_completion_tokens" if provider in COMPLETION_LIMIT_PROVIDERS else "max_tokens"
            payload = {
                "model": model, limit: MAX_OCR_TOKENS,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": OCR_PROMPT}, {"type": "image_url", "image_url": {"url": image_url}},
                ]}],
            }
            return "application/json", json.dumps(payload).encode()

        return self.send("ocr", approval, message)

    def send(self, kind: RecognitionKind, approval: str, payload) -> TextReply:
        with transaction(self.engine, write=False) as db:
            connection = require_grant(db, kind, approval)
            revision, reference = connection.revision, connection.credential_ref
            model, provider = connection.model, connection.provider
        try:
            key = self.vault.get(reference) if reference else None
        except CREDENTIAL_FAILURES as error:
            raise HTTPException(409, "本机凭据暂不可用，请检查系统凭据设置。") from error
        if not key and (reference or provider != "custom"):
            raise HTTPException(409, "所选连接缺少可用密钥，请在服务连接中检查。")

        # Build the bounded request outside the write transaction, then recheck its recipient at dispatch.
        content_type, body = payload(model, provider)
        call_id = str(uuid4())
        with transaction(self.engine) as db:
            connection = require_grant(db, kind, approval)
            if connection.revision != revision or connection.credential_ref != reference:
                raise HTTPException(409, APPROVAL_ERROR)
            now = utc_clock(self.now())
            day = billing_day(now)
            settings = db.get(GenerationSettings, 1)
            if active_calls(db) >= (settings.concurrency if settings else DEFAULT_CONCURRENCY):
                raise HTTPException(429, "模型请求并发已满，请等待现有请求完成。")
            if settings and not daily_capacity(settings.daily_limit, billed_calls(db, day)):
                raise HTTPException(409, "今日模型调用次数已达上限。")
            if issue := amount_issue(db, CALL_KIND[kind], call_id, day, None, None):
                message = ("已设置金额上限，当前无法可靠预估媒体识别费用。仍可使用本机识别。"
                           if issue == "cost_unavailable" else MONEY_ERRORS[issue])
                raise HTTPException(409, message)
            address = endpoint_for(connection, kind)
            recipient = {**destination(connection, kind), "id": connection.id, "provider": connection.provider}
            charge = prepare_charge(db, "recognition", call_id, CALL_KIND[kind], call_id, day, now, recipient)
            # Audio duration and image units are not represented by the connection's text-token prices.
            charge.pricing = None
            db.add(charge)

        @contextmanager
        def upload_guard():
            # Allow changes during the connection handshake and while waiting for the response.
            with self.recipient_changes:
                with transaction(self.engine, write=False) as db:
                    connection = require_grant(db, kind, approval)
                    if connection.revision != revision or connection.credential_ref != reference:
                        raise HTTPException(409, APPROVAL_ERROR)
                yield

        reply = TextReply("uncertain")
        try:
            reply = media_reply(kind, address, key, content_type, body, upload_guard)
        except HTTPException:
            # The upload guard rejected this send before request headers or media reached the connection.
            reply = None
            with transaction(self.engine) as db:
                pending = db.get(ModelCharge, ("recognition", call_id))
                if pending is not None and pending.finished_at is None:
                    db.delete(pending)
            raise
        finally:
            if reply is not None:
                save_charge_reply(self.engine, "recognition", call_id, reply.status, reply.usage, self.now())
        if reply.status == "no_text":
            raise HTTPException(422, "外部服务没有识别出可用文字，可手动输入或继续使用本机识别。")
        if reply.status != "passed":
            raise HTTPException(502, REPLY_ERRORS.get(reply.status, "外部识别服务暂未返回有效结果。"))
        return reply


def recognition_router(recognition: Recognition, session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/recognition")
    engine = recognition.engine
    secret = session_token.encode()

    def signature(workspace: str, connection: ModelConnection, kind: RecognitionKind, grant: RecognitionGrant | None) -> str:
        material = json.dumps(["recognition", workspace, destination(connection, kind), grant.approval_id if grant else None],
                              ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return digest(secret, material, "sha256").hex()

    def select_service(db, identity):
        connection = db.get(ModelConnection, str(identity))
        if connection is None or connection.protocol != CHAT:
            raise HTTPException(422, "请选择本用途兼容的服务连接。")
        return connection

    @router.get("/{kind}")
    def status(kind: RecognitionKind):
        with transaction(engine, write=False) as db:
            grant = db.get(RecognitionGrant, kind)
            connection = current_connection(db, grant)
            options = db.scalars(select(ModelConnection).where(ModelConnection.protocol == CHAT)
                                 .order_by(ModelConnection.name, ModelConnection.id))
            return {
                "connections": [destination(item, kind) for item in options],
                "authorization": {**destination(connection, kind), "id": grant.approval_id} if connection else None,
            }

    @router.post("/{kind}/preview")
    def preview(kind: RecognitionKind, data: ServiceChoice):
        with transaction(engine, write=False) as db:
            connection = select_service(db, data.connectionId)
            grant = db.get(RecognitionGrant, kind)
            return {**destination(connection, kind), "confirmation": signature(workspace_id(db), connection, kind, grant)}

    @router.post("/{kind}/authorize")
    def authorize(kind: RecognitionKind, data: AuthorizeChoice):
        receipt = sha256(data.confirmation.encode()).hexdigest()
        with recognition.recipient_changes, transaction(engine) as db:
            connection = select_service(db, data.connectionId)
            grant = db.get(RecognitionGrant, kind)
            if grant and grant.receipt == receipt and current_connection(db, grant) is connection:
                return {"authorization": {**destination(connection, kind), "id": grant.approval_id}}
            if not compare_digest(data.confirmation, signature(workspace_id(db), connection, kind, grant)):
                raise HTTPException(409, APPROVAL_ERROR)
            if grant is None:
                grant = RecognitionGrant(kind=kind)
                db.add(grant)
            grant.approval_id = str(uuid4())
            grant.connection_id, grant.connection_revision, grant.receipt = connection.id, connection.revision, receipt
            grant.scope = SCOPE[kind]
            return {"authorization": {**destination(connection, kind), "id": grant.approval_id}}

    @router.delete("/{kind}")
    def revoke(kind: RecognitionKind):
        with recognition.recipient_changes, transaction(engine) as db:
            grant = db.get(RecognitionGrant, kind)
            if grant is None:
                grant = RecognitionGrant(kind=kind)
                db.add(grant)
            grant.approval_id = str(uuid4())
            grant.connection_id = grant.connection_revision = grant.receipt = grant.scope = None
            return {"authorization": None}

    return router
