from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import RawIOBase
from os import urandom
from pathlib import Path
from struct import Struct
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


EXTENSION = ".zhixue-backup"
CHUNK_BYTES = 1024 * 1024
MAGIC = b"ZXBKP\r\n\x1a"
VERSION = 1
HEADER = Struct(">8sB16sQ16s8s")
LENGTH = Struct(">I")
TAG_BYTES = 16


class BackupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def damaged() -> BackupError:
    return BackupError("damaged", "备份内容不完整或格式无效。")


def incomplete_file(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.partial")


def read_exact(handle, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise damaged()
    return data


@dataclass(frozen=True)
class BackupHeader:
    identity: UUID
    created_at: datetime
    encoded: bytes
    salt: bytes
    nonce_prefix: bytes

    @classmethod
    def create(cls, identity: UUID, created_at: datetime):
        salt, nonce_prefix = urandom(16), urandom(8)
        encoded = HEADER.pack(MAGIC, VERSION, identity.bytes, int(created_at.timestamp()), salt, nonce_prefix)
        return cls(identity, created_at, encoded, salt, nonce_prefix)

    @classmethod
    def read(cls, handle):
        encoded = read_exact(handle, HEADER.size)
        magic, version, identity, timestamp, salt, nonce_prefix = HEADER.unpack(encoded)
        if magic != MAGIC:
            raise damaged()
        if version != VERSION:
            raise BackupError("unsupported", "请使用支持此备份格式的新版本智学。")
        try:
            created_at = datetime.fromtimestamp(timestamp, UTC)
        except (ValueError, OverflowError, OSError):
            raise damaged() from None
        return cls(UUID(bytes=identity), created_at, encoded, salt, nonce_prefix)

    def cipher(self, password: str) -> AESGCM:
        key = Scrypt(salt=self.salt, length=32, n=2**17, r=8, p=1).derive(password.encode("utf-8"))
        return AESGCM(key)

    def nonce(self, record: int) -> bytes:
        if record >= 2**32:
            raise BackupError("too_large", "备份超过当前格式可写入的大小。")
        return self.nonce_prefix + LENGTH.pack(record)


class ArchiveWriter:
    """An encrypted frame stream. The authenticated empty frame commits the end of the payload."""

    def __init__(self, handle, header: BackupHeader, password: str):
        self.handle = handle
        self.header = header
        self.cipher = header.cipher(password)
        self.record = 0
        self.buffer = bytearray()
        self.handle.write(header.encoded)

    def _frame(self, block: bytes) -> None:
        encoded = self.cipher.encrypt(self.header.nonce(self.record), block, self.header.encoded)
        self.handle.write(LENGTH.pack(len(encoded)))
        self.handle.write(encoded)
        self.record += 1

    def write(self, content: bytes) -> int:
        remaining = memoryview(content)
        while remaining:
            take = min(len(remaining), CHUNK_BYTES - len(self.buffer))
            self.buffer.extend(remaining[:take])
            remaining = remaining[take:]
            if len(self.buffer) == CHUNK_BYTES:
                self._frame(bytes(self.buffer))
                self.buffer.clear()
        return len(content)

    def finish(self) -> None:
        if self.buffer:
            self._frame(bytes(self.buffer))
            self.buffer.clear()
        self._frame(b"")


class ArchiveReader(RawIOBase):
    """Only exposes bytes after the corresponding frame passes authentication."""

    def __init__(self, handle, password: str):
        super().__init__()
        self.handle = handle
        self.header = BackupHeader.read(handle)
        self.digest = sha256(self.header.encoded)
        self.cipher = self.header.cipher(password)
        self.record = 0
        self.remaining = memoryview(b"")
        self.finished = False

    def readable(self):
        return True

    def _frame(self) -> None:
        prefix = read_exact(self.handle, LENGTH.size)
        length, = LENGTH.unpack(prefix)
        if not TAG_BYTES <= length <= CHUNK_BYTES + TAG_BYTES:
            raise damaged()
        ciphertext = read_exact(self.handle, length)
        try:
            block = self.cipher.decrypt(self.header.nonce(self.record), ciphertext, self.header.encoded)
        except InvalidTag:
            raise BackupError("authentication", "备份密码错误或文件完整性校验失败。") from None
        self.digest.update(prefix)
        self.digest.update(ciphertext)
        self.record += 1
        self.remaining = memoryview(block)
        if not block:
            if self.handle.read(1):
                raise damaged()
            self.finished = True

    def readinto(self, target):
        if not target or self.finished:
            return 0
        if not self.remaining:
            self._frame()
        count = min(len(target), len(self.remaining))
        target[:count] = self.remaining[:count]
        self.remaining = self.remaining[count:]
        return count
