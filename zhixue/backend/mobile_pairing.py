"""Desktop endpoint for pairing and encrypted mobile operations."""
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest
import json
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time
from typing import Annotated
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
import urllib3

from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .mobile_cipher import MAX_ANSWER, MAX_REQUEST, TOKEN, decode, encode, open_message, origin, pair_keys, public_key, seal_message, token
from .web_push import PushRegistration, PushSubscription, PushTarget


INVITE_SECONDS = 300
LOG = logging.getLogger(__name__)
DEVICE_NAME = Annotated[str, Field(min_length=1, max_length=50)]
Revision = Annotated[str | None, Query(pattern=TOKEN.pattern)]
VAULT_ERROR = "系统凭据库暂不可用，请解锁后重试。"
PUSH_OPERATIONS = {"push-status", "push-subscribe", "push-unsubscribe"}


class PairingCredentialStore(NativeCredentialStore):
    scope = "mobile-sync"
    username = "device-pairing"


class InviteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relay: str = Field(max_length=450)
    mobile: str = Field(max_length=450)


class Approval(BaseModel):
    code: str = Field(pattern=r"^[0-9A-F]{8}$")


class Enrollment(BaseModel):
    publicKey: str = Field(pattern=r"^[A-Za-z0-9_-]{87}$")
    name: DEVICE_NAME


class SavedPair(BaseModel):
    room: str = Field(pattern=TOKEN.pattern)
    key: str = Field(pattern=TOKEN.pattern)
    relay: str = Field(max_length=450)
    workspace: str
    name: DEVICE_NAME
    push: PushRegistration | None = Field(default=None, repr=False)


class PushEdit(BaseModel):
    workspace: UUID
    subscription: PushSubscription | None = Field(default=None, repr=False)
    endpointId: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


@dataclass(eq=False)
class Link:
    relay: str
    room: str
    key: bytes = field(repr=False)
    workspace: str
    name: str = ""
    deadline: float = float("inf")
    stopped: Event = field(default_factory=Event, repr=False)
    thread: Thread | None = field(default=None, repr=False)
    push: PushRegistration | None = field(default=None, repr=False)


@dataclass
class Candidate:
    code: str
    point: str
    link: Link


@dataclass
class Invitation:
    link: Link
    url: str = field(repr=False)
    private: ec.EllipticCurvePrivateKey = field(repr=False)
    expires_at: int
    candidate: Candidate | None = None


class DesktopPairing:
    def __init__(self, folder: Path, vault: CredentialStore, workspace: Callable, invoke: Callable, gate: Callable):
        self.reference = sha256(os.path.normcase(str(folder)).encode("utf-8")).hexdigest()
        self.vault = vault
        self.workspace = workspace
        self.invoke = invoke
        self.gate = gate
        self.lock = Lock()
        self.revision = token()
        self.pair: Link | None = None
        self.invitation: Invitation | None = None
        self.threads: list[Thread] = []
        self.storage_error: str | None = None
        self.http = urllib3.PoolManager(
            num_pools=2, maxsize=2, retries=False, timeout=urllib3.Timeout(connect=2.5, read=4.5),
        )

    def start(self) -> None:
        try:
            raw = self.vault.get(self.reference)
            if not raw:
                return
            saved = SavedPair.model_validate_json(raw)
            relay = origin(saved.relay)
            key = decode(saved.key)
            decode(saved.room)
        except (*CREDENTIAL_FAILURES, ValueError):
            self.storage_error = VAULT_ERROR
            return
        if saved.workspace != self.workspace():
            self.discard_saved()
            return
        self.pair = Link(relay, saved.room, key, saved.workspace, saved.name, push=saved.push)
        self.listen(self.pair)

    def listen(self, link: Link) -> None:
        link.thread = Thread(target=self.forward, args=(link,), name="mobile-rendezvous", daemon=True)
        self.threads = [thread for thread in self.threads if thread.is_alive()] + [link.thread]
        link.thread.start()

    def close(self) -> None:
        with self.lock:
            self.drop_invite()
            if self.pair is not None:
                self.pair.stopped.set()
                self.pair = None
            threads = self.threads.copy()
        for thread in threads:
            thread.join(timeout=8)
        self.http.clear()

    def discard_saved(self) -> bool:
        try:
            self.vault.discard(self.reference)
        except CREDENTIAL_FAILURES:
            self.storage_error = VAULT_ERROR
            return False
        self.storage_error = None
        return True

    def drop_invite(self) -> None:
        if self.invitation is not None:
            self.invitation.link.stopped.set()
            self.invitation = None
            self.revision = token()

    def current_workspace(self) -> str:
        current = self.workspace()
        invite = self.invitation
        if invite is not None and (invite.link.workspace != current or monotonic() >= invite.link.deadline):
            self.drop_invite()
        if self.pair is not None and self.pair.workspace != current:
            self.pair.stopped.set()
            self.pair = None
            self.revision = token()
            self.discard_saved()
        return current

    def check_revision(self, revision: str | None) -> None:
        if revision is not None and not compare_digest(self.revision, revision):
            raise HTTPException(409, "配对状态已变化。请核对当前设备后重试。")

    def view(self) -> dict:
        invite = self.invitation
        candidate = invite.candidate if invite is not None else None
        return {
            "revision": self.revision,
            "paired": {"name": self.pair.name} if self.pair is not None else None,
            "invitation": {"url": invite.url, "expiresAt": invite.expires_at} if invite is not None else None,
            "candidate": {"name": candidate.link.name, "code": candidate.code} if candidate is not None else None,
            "error": self.storage_error,
        }

    def status(self) -> dict:
        with self.lock:
            self.current_workspace()
            return self.view()

    def invite(self, data: InviteInput, revision: Revision = None) -> dict:
        try:
            relay, mobile = origin(data.relay), origin(data.mobile, mobile=True)
        except ValueError:
            raise HTTPException(422, "请填写 HTTPS 地址。本机验收可使用 127.0.0.1。") from None
        if relay == mobile:
            raise HTTPException(422, "手机页面和中继需要使用不同的来源。")
        with self.lock:
            workspace = self.current_workspace()
            self.check_revision(revision)
            if self.pair is not None:
                raise HTTPException(409, "已有配对，请先撤销旧手机。")
            self.drop_invite()
            room, secret = token(), token()
            private = ec.generate_private_key(ec.SECP256R1())
            expires = int(time()) + INVITE_SECONDS
            invitation = {"v": 1, "relay": relay, "room": room, "secret": secret,
                          "publicKey": public_key(private), "expiresAt": expires}
            fragment = encode(json.dumps(invitation, separators=(",", ":")).encode("ascii"))
            link = Link(relay, room, decode(secret), workspace, deadline=monotonic() + INVITE_SECONDS)
            self.invitation = Invitation(link, f"{mobile}/mobile/#/?pair={fragment}", private, expires)
            self.revision = token()
            self.listen(link)
            return self.view()

    def approve(self, data: Approval, revision: Revision = None) -> dict:
        with self.lock:
            self.current_workspace()
            self.check_revision(revision)
            candidate = self.invitation.candidate if self.invitation is not None else None
            if candidate is None or not compare_digest(candidate.code, data.code):
                raise HTTPException(409, "配对请求已失效，请从电脑生成新邀请。")
            link = candidate.link
            self.save_link(link, None)
            self.drop_invite()
            self.pair = link
            self.listen(link)
            return self.view()

    def save_link(self, link: Link, push: PushRegistration | None) -> None:
        try:
            self.vault.put(self.reference, SavedPair(
                workspace=link.workspace, relay=link.relay, name=link.name,
                room=link.room, key=encode(link.key), push=push,
            ).model_dump_json())
        except CREDENTIAL_FAILURES:
            self.storage_error = VAULT_ERROR
            raise HTTPException(503, VAULT_ERROR) from None
        self.storage_error = None
        link.push = push

    def edit_push(self, link: Link, data: dict, workspace: str) -> dict:
        change = PushEdit.model_validate(data)
        if str(change.workspace) != workspace:
            raise HTTPException(409, "提醒对应的工作区已更换。")
        push = link.push or PushRegistration.create()
        if push.subscription is not None and push.subscription.expired(time()):
            push = push.model_copy(update={"subscription": None})
        operation = data["op"]
        if operation == "push-subscribe":
            if change.subscription is None or change.subscription.expired(time()):
                raise HTTPException(422, "手机推送订阅已失效，请重新启用。")
            push = push.model_copy(update={"subscription": change.subscription})
        if operation == "push-unsubscribe" and push.subscription and change.endpointId == push.subscription.fingerprint:
            push = push.model_copy(update={"subscription": None})
        if link.push != push:
            self.save_link(link, push)
        return push.view()

    def current_push(self) -> PushTarget | None:
        with self.lock:
            if self.pair is None or self.pair.push is None or self.pair.push.subscription is None:
                return None
            self.current_workspace()
            return self.push_target()

    def push_target(self) -> PushTarget | None:
        link = self.pair
        if link is None or link.push is None or link.push.subscription is None:
            return None
        push = link.push
        subscription = push.subscription
        receiver = sha256(f"{link.room}/{subscription.fingerprint}".encode("ascii")).hexdigest()
        contact = link.relay if link.relay.startswith("https://") else None
        pairing = sha256(link.room.encode("ascii")).hexdigest()
        return PushTarget(receiver, push.private, subscription, pairing, contact)

    def expire_push(self, target: PushTarget) -> None:
        with self.lock:
            if self.push_target() == target:
                link = self.pair
                self.save_link(link, link.push.model_copy(update={"subscription": None}))

    def cancel(self, revision: Revision = None) -> dict:
        with self.lock:
            self.current_workspace()
            self.check_revision(revision)
            self.drop_invite()
            return self.view()

    def revoke(self, revision: Revision = None) -> dict:
        with self.lock:
            self.current_workspace()
            self.check_revision(revision)
            if not self.discard_saved():
                raise HTTPException(503, VAULT_ERROR)
            if self.pair is not None:
                self.pair.stopped.set()
                self.pair = None
                self.revision = token()
            self.drop_invite()
            return self.view()

    def enroll(self, invite: Invitation, data: dict) -> dict:
        if data.get("op") != "pair":
            raise HTTPException(403, "请先在电脑批准配对。")
        incoming = Enrollment.model_validate(data)
        candidate = invite.candidate
        if candidate is not None and candidate.point != incoming.publicKey:
            raise HTTPException(409, "此邀请已有一份待批准请求。")
        if candidate is None:
            root, room, code = pair_keys(invite.private, incoming.publicKey, invite.link.key, invite.link.room)
            name = incoming.name.strip() or "手机"
            invite.candidate = Candidate(code, incoming.publicKey, Link(
                invite.link.relay, room, root, invite.link.workspace, name,
            ))
            self.revision = token()
        return {"state": "approval"}

    def handle(self, link: Link, data: dict) -> dict:
        runtime = self.gate()
        if runtime is not None and not runtime.enter():
            return {"status": 503, "detail": "整体恢复期间暂不可同步。"}
        try:
            with self.lock:
                workspace = self.current_workspace()
                if self.invitation is not None and self.invitation.link is link:
                    result = self.enroll(self.invitation, data)
                elif self.pair is link:
                    operation = data.get("op")
                    if operation == "ping":
                        result = {"workspace": workspace}
                    elif isinstance(operation, str) and operation in PUSH_OPERATIONS:
                        result = self.edit_push(link, data, workspace)
                    else:
                        result = self.invoke(data)
                else:
                    raise HTTPException(410, "配对已撤销、到期或工作区已更换。")
                return {"status": 200, "data": jsonable_encoder(result)}
        except HTTPException as error:
            return {"status": error.status_code, "detail": error.detail}
        except ValueError:
            return {"status": 422, "detail": "手机请求的格式不受支持。"}
        except Exception as error:
            LOG.error("Mobile operation failed (%s)", type(error).__name__)
            return {"status": 500, "detail": "电脑暂时无法处理手机请求，请重试。"}
        finally:
            if runtime is not None:
                runtime.leave()

    def forward(self, link: Link) -> None:
        base = f"{link.relay}/v1/{link.room}"
        while not link.stopped.is_set() and monotonic() < link.deadline:
            ticket = token()
            headers = {"X-Reply-Token": token()}
            try:
                response = self.http.request(
                    "POST", f"{base}/wait/{ticket}", headers=headers, redirect=False, preload_content=False,
                )
                try:
                    status = response.status
                    packet = response.read(MAX_REQUEST + 1, decode_content=False) if status == 200 else b""
                finally:
                    response.close()
                    response.release_conn()
                if status == 408:
                    continue
                if status != 200 or len(packet) > MAX_REQUEST:
                    link.stopped.wait(0.6)
                    continue
                try:
                    data = open_message(link.key, link.room, ticket, "request", packet)
                except (InvalidTag, ValueError, RecursionError):
                    link.stopped.wait(0.2)
                    continue
                value = self.handle(link, data)
                if link.stopped.is_set() and value["status"] != 410:
                    return
                sealed = seal_message(link.key, link.room, ticket, "answer", value, packet)
                if len(sealed) > MAX_ANSWER:
                    sealed = seal_message(link.key, link.room, ticket, "answer", {
                        "status": 413, "detail": "内容超出手机单次下载大小。",
                    }, packet)
                headers["Content-Type"] = "application/octet-stream"
                response = self.http.request(
                    "POST", f"{base}/answer/{ticket}", body=sealed, headers=headers, redirect=False,
                    preload_content=False,
                )
                response.close()
                response.release_conn()
            except (urllib3.exceptions.HTTPError, OSError):
                link.stopped.wait(0.8)


def pairing_router(pairing: DesktopPairing) -> APIRouter:
    router = APIRouter(prefix="/api/device-sync")
    router.add_api_route("", pairing.status, methods=["GET"])
    router.add_api_route("/invite", pairing.invite, methods=["POST"])
    router.add_api_route("/invite", pairing.cancel, methods=["DELETE"])
    router.add_api_route("/approve", pairing.approve, methods=["POST"])
    router.add_api_route("", pairing.revoke, methods=["DELETE"])
    return router
