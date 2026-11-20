"""Desktop-to-provider Web Push using RFC 8291 encryption and RFC 8292 VAPID."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from time import time
from typing import Literal
from urllib.parse import urlsplit

import certifi
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
import http_ece
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import urllib3

from .mobile_cipher import decode, encode, public_key


Delivery = Literal["accepted", "rejected", "expired", "uncertain"]
PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com"}


def provider_origin(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.port not in (None, 443) or parts.username is not None
            or "#" in endpoint or not parts.path.strip("/") or "\\" in endpoint
            or any(ord(char) < 33 or ord(char) >= 127 for char in endpoint)
            or not (host in PUSH_HOSTS or re.fullmatch(r"(?:[a-z0-9-]+\.)+push\.apple\.com", host))):
        raise ValueError("Unsupported Web Push endpoint")
    return f"https://{host}"


class PushKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p256dh: str = Field(min_length=87, max_length=87, repr=False)
    auth: str = Field(min_length=22, max_length=22, repr=False)

    @model_validator(mode="after")
    def validate_keys(self):
        public = decode(self.p256dh)
        if len(public) != 65 or len(decode(self.auth)) != 16:
            raise ValueError("Invalid Web Push keys")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
        return self


class PushSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint: str = Field(max_length=1700, repr=False)
    expirationTime: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    keys: PushKeys = Field(repr=False)

    @field_validator("endpoint")
    @classmethod
    def check_endpoint(cls, value: str) -> str:
        provider_origin(value)
        return value

    @property
    def fingerprint(self) -> str:
        return sha256(self.endpoint.encode("ascii")).hexdigest()

    def expired(self, timestamp: float) -> bool:
        return self.expirationTime is not None and self.expirationTime <= timestamp * 1000


def read_private(value: str) -> ec.EllipticCurvePrivateKey:
    raw = decode(value)
    if len(raw) != 32:
        raise ValueError("Invalid Web Push application key")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


class PushRegistration(BaseModel):
    model_config = ConfigDict(frozen=True)
    private: str = Field(min_length=43, max_length=43, repr=False)
    subscription: PushSubscription | None = Field(default=None, repr=False)

    @field_validator("private")
    @classmethod
    def check_private(cls, value: str) -> str:
        read_private(value)
        return value

    @classmethod
    def create(cls):
        key = ec.generate_private_key(ec.SECP256R1())
        return cls(private=encode(key.private_numbers().private_value.to_bytes(32, "big")))

    def view(self) -> dict:
        return {
            "publicKey": public_key(read_private(self.private)),
            "registered": self.subscription is not None,
            "endpointId": self.subscription.fingerprint if self.subscription else None,
        }


@dataclass(frozen=True)
class PushTarget:
    receiver: str
    private: str = field(repr=False)
    subscription: PushSubscription = field(repr=False)
    pairing: str
    contact: str | None = None


def vapid_header(private: ec.EllipticCurvePrivateKey, audience: str, expiry: int, contact: str | None) -> str:
    header = encode(b'{"typ":"JWT","alg":"ES256"}')
    claims = {"aud": audience, "exp": expiry}
    if contact:
        claims["sub"] = contact
    payload = encode(json.dumps(claims, separators=(",", ":")).encode("ascii"))
    signing_input = f"{header}.{payload}"
    r, s = utils.decode_dss_signature(private.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    signature = encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={signing_input}.{signature}, k={public_key(private)}"


class WebPushSender:
    def __init__(self):
        self.http = urllib3.PoolManager(
            num_pools=3, maxsize=1, retries=False, cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
            timeout=urllib3.Timeout(connect=2, read=3, total=5),
        )

    def send(self, target: PushTarget, payload: dict, ttl: int) -> Delivery:
        subscription = target.subscription
        timestamp = time()
        if subscription.expired(timestamp):
            return "expired"
        audience = provider_origin(subscription.endpoint)
        private = read_private(target.private)
        body = http_ece.encrypt(
            json.dumps(payload, separators=(",", ":")).encode("ascii"),
            private_key=ec.generate_private_key(ec.SECP256R1()), dh=decode(subscription.keys.p256dh),
            auth_secret=decode(subscription.keys.auth), version="aes128gcm",
        )
        headers = {
            "Authorization": vapid_header(private, audience, int(timestamp) + 3600, target.contact),
            "Content-Type": "application/octet-stream", "Content-Encoding": "aes128gcm",
            "TTL": str(ttl), "Topic": payload["delivery"].replace("-", ""), "Urgency": "normal",
        }
        try:
            response = self.http.request(
                "POST", subscription.endpoint, body=body, headers=headers, redirect=False, preload_content=False,
            )
            try:
                if 200 <= response.status < 300:
                    return "accepted"
                return "expired" if response.status in (404, 410) else "rejected"
            finally:
                response.close()
                response.release_conn()
        except (urllib3.exceptions.ConnectTimeoutError, urllib3.exceptions.SSLError):
            return "rejected"
        except (urllib3.exceptions.HTTPError, OSError):
            return "uncertain"

    def close(self) -> None:
        self.http.clear()
