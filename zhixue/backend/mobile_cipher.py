"""Cryptographic framing shared by the desktop and the mobile PWA."""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
import json
import re
from secrets import token_bytes
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


MAX_REQUEST = 512 * 1024
MAX_ANSWER = 8 * 1024 * 1024
OVERHEAD = 12 + 16
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url value")
    decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if encode(decoded) != value:
        raise ValueError("Noncanonical base64url value")
    return decoded


def token() -> str:
    return encode(token_bytes(32))


def origin(value: str, *, mobile: bool = False) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    paths = ("", "/", "/mobile", "/mobile/") if mobile else ("", "/")
    if (
        not re.fullmatch(r"[a-z0-9.-]+", host)
        or parts.username is not None or parts.path not in paths or parts.query or parts.fragment
        or (parts.scheme != "https" and (parts.scheme != "http" or host != "127.0.0.1"))
        or parts.port == 0
    ):
        raise ValueError("Use an HTTPS origin or a local 127.0.0.1 test address")
    port = parts.port
    if port == {"http": 80, "https": 443}[parts.scheme]:
        port = None
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def derive(secret: bytes, salt: bytes, context: str, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=salt,
        info=("zhixue/mobile/v1/" + context).encode("ascii"),
    ).derive(secret)


def public_key(private: ec.EllipticCurvePrivateKey) -> str:
    return encode(private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    ))


def pair_keys(private: ec.EllipticCurvePrivateKey, peer: str, invite_secret: bytes, invite_room: str):
    point = decode(peer)
    if len(point) != 65 or point[0] != 4:
        raise ValueError("Expected an uncompressed P-256 point")
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    shared = private.exchange(ec.ECDH(), public)
    root = derive(shared, invite_secret, f"{invite_room}/pair-key")
    room = encode(derive(shared, invite_secret, f"{invite_room}/pair-room"))
    code = derive(shared, invite_secret, f"{invite_room}/pair-code", 4).hex().upper()
    return root, room, code


def seal_message(root: bytes, room: str, ticket: str, direction: str, value: dict, request: bytes = b"") -> bytes:
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    nonce = token_bytes(12)
    cipher = AESGCM(derive(root, decode(ticket), f"{room}/{direction}"))
    return nonce + cipher.encrypt(nonce, data, sha256(request).digest() if request else None)


def open_message(root: bytes, room: str, ticket: str, direction: str, packet: bytes, request: bytes = b"") -> dict:
    if len(packet) < OVERHEAD:
        raise ValueError("Truncated encrypted message")
    cipher = AESGCM(derive(root, decode(ticket), f"{room}/{direction}"))
    value = json.loads(cipher.decrypt(packet[:12], packet[12:], sha256(request).digest() if request else None))
    if not isinstance(value, dict):
        raise ValueError("Expected a message object")
    return value
