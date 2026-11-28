from collections import defaultdict
import ctypes
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from stat import S_ISREG
from struct import unpack_from
import sys

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .db import LibraryLocation, LibraryTrash
from .documents import FORMATS
from .library import FileTooLarge, LibraryDocument, read_snapshot


class InvalidLocation(ValueError):
    pass


class LocationConflict(ValueError):
    pass


@dataclass(frozen=True)
class FileIdentity:
    key: str | None
    birth: str | None


@dataclass(frozen=True)
class FileLocation:
    path: Path
    status: str
    message: str | None = None


def checked_path(folder: Path, relative: str) -> Path:
    parts = relative.split("/")
    if (len(parts) < 2 or parts[0] != "library"
            or any(not part or part.startswith(".") or part.endswith((" ", "."))
                   or any(ord(char) < 32 or char in '\\:<>|?*"' for char in part) for part in parts)):
        raise InvalidLocation("请选择知识库内的普通文件，路径需以 library/ 开头。")
    path = folder.resolve().joinpath(*parts)
    try:
        if path.resolve() != path:
            raise InvalidLocation("文件或上级目录通过链接指向了其他位置。")
    except RuntimeError as error:
        raise InvalidLocation("文件路径包含无法解析的链接。") from error
    return path


def _load_linux_statx():
    if sys.platform != "linux":
        return None
    try:
        statx = ctypes.CDLL(None).statx
    except (AttributeError, OSError):
        return None
    statx.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p)
    statx.restype = ctypes.c_int
    return statx


_linux_statx = _load_linux_statx()
_STATX_INO = 0x0100
_STATX_BTIME = 0x0800
_AT_FDCWD = -100
_AT_SYMLINK_NOFOLLOW = 0x0100


def _linux_birth_time(path: Path, stat: os.stat_result) -> int | None:
    if _linux_statx is None:
        return None
    # Linux's fixed statx ABI has a 256-byte buffer with native-endian fields.
    result = ctypes.create_string_buffer(256)
    wanted = _STATX_INO | _STATX_BTIME
    if _linux_statx(_AT_FDCWD, os.fsencode(path), _AT_SYMLINK_NOFOLLOW, wanted, result):
        return None
    mask, = unpack_from("=I", result)
    if mask & wanted != wanted:
        return None
    inode, = unpack_from("=Q", result, 32)
    major, minor = unpack_from("=II", result, 136)
    # A pathname may have been replaced between the two metadata lookups.
    if inode != stat.st_ino or (major, minor) != (os.major(stat.st_dev), os.minor(stat.st_dev)):
        return None
    seconds, nanos = unpack_from("=qI", result, 80)
    return seconds * 1_000_000_000 + nanos


def identity_of(path: Path) -> FileIdentity | None:
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not S_ISREG(stat.st_mode):
        raise InvalidLocation("当前位置不是普通文件。")
    birth = getattr(stat, "st_birthtime_ns", None)
    if birth is None and (seconds := getattr(stat, "st_birthtime", None)) is not None:
        birth = int(seconds * 1_000_000_000)
    if birth is None:
        birth = _linux_birth_time(path, stat)
    if birth is None and sys.platform == "win32" and sys.version_info < (3, 12):
        # Windows Python 3.11 reports creation time as st_ctime_ns.
        birth = stat.st_ctime_ns or None
    key = f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else None
    return FileIdentity(key, str(birth) if birth is not None else None)


def known_body(path: Path) -> str | None:
    try:
        return read_snapshot(path).revision
    except (OSError, FileTooLarge):
        return None


def legacy_key(fingerprint: str | None) -> str | None:
    try:
        _, signature = json.loads(fingerprint)
        device, inode, *_ = signature.split(":")
        if device.isdigit() and inode.isdigit() and int(inode):
            return f"{device}:{inode}"
    except (ValueError, TypeError, AttributeError):
        pass
    return None


class LocationTracker:
    def __init__(self, db: Session, folder: Path):
        self.db = db
        self.folder = folder.resolve()
        self.locations = {row.id: row for row in db.scalars(select(LibraryLocation))}
        self.claimed = {self.path_key(row.path): row.id for row in self.locations.values()}
        self._inventory = None
        self._by_key = defaultdict(list)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.folder).as_posix()

    @staticmethod
    def path_key(relative: str) -> str:
        return os.path.normcase(relative)

    def inventory(self) -> dict[Path, FileIdentity]:
        if self._inventory is None:
            entries = {}
            library_root = self.folder / "library"
            if not self.real_directory(library_root):
                self._inventory = entries
                return entries
            for directory, subfolders, filenames in os.walk(library_root, followlinks=False):
                root = Path(directory)
                subfolders[:] = sorted(name for name in subfolders if not name.startswith(".") and self.real_directory(root / name))
                for filename in filenames:
                    path = root / filename
                    if path.suffix.lower().removeprefix(".") not in FORMATS:
                        continue
                    try:
                        path = checked_path(self.folder, self.relative(path))
                        stat = identity_of(path)
                    except (InvalidLocation, OSError):
                        continue
                    if stat is not None:
                        entries[path] = stat
                        if stat.key:
                            self._by_key[stat.key].append(path)
            self._inventory = entries
        return self._inventory

    @staticmethod
    def real_directory(path: Path) -> bool:
        try:
            return path.resolve() == path
        except (OSError, RuntimeError):
            return False

    def lookup(self, file_id: str, default: Path) -> LibraryLocation | None:
        row = self.locations.get(file_id)
        if row is not None:
            return row
        indexed = self.db.execute(text(
            "SELECT path, fingerprint, revision, status FROM library_index_documents WHERE id = :id",
        ), {"id": file_id}).first()
        if indexed is not None:
            path = indexed.path or self.relative(default)
            row = LibraryLocation(id=file_id, path=path, file_key=legacy_key(indexed.fingerprint),
                                  birth_time=None, revision=indexed.revision, missing=indexed.status == "missing")
            if self.owned_elsewhere(path, file_id):
                return None
            self.db.add(row)
            self.locations[file_id] = row
            self.claimed[self.path_key(path)] = file_id
        return row

    def owned_elsewhere(self, path: str, file_id: str) -> bool:
        owner = self.claimed.get(self.path_key(path))
        return owner is not None and owner != file_id

    def bound_to_other_file(self, file_id: str, identity: FileIdentity) -> bool:
        if not identity.key:
            return False
        for other in self.locations.values():
            if other.id == file_id or other.file_key != identity.key:
                continue
            try:
                current = identity_of(checked_path(self.folder, other.path))
            except (OSError, InvalidLocation):
                continue
            if current and current.key == identity.key:
                return True
        return False

    def bind(self, file_id: str, path: Path, identity: FileIdentity, revision: str | None) -> None:
        relative = self.relative(path)
        if self.owned_elsewhere(relative, file_id):
            raise LocationConflict("此路径已关联到知识库中的另一份文件。")
        row = self.locations.get(file_id)
        if row is None:
            row = LibraryLocation(id=file_id)
            self.locations[file_id] = row
            self.db.add(row)
        elif row.path:
            self.claimed.pop(self.path_key(row.path), None)
        row.path, row.file_key, row.birth_time, row.revision = relative, identity.key, identity.birth, revision
        row.missing = False
        self.claimed[self.path_key(relative)] = file_id

    def resolve(self, file_id: str, default: Path) -> FileLocation:
        # Use the original extension as the document format even if the folder name changed.
        row = self.lookup(file_id, default)
        if row is not None and row.trash_id is not None:
            entry = self.db.get(LibraryTrash, row.trash_id)
            status = "purged" if entry and entry.state == "purged" else "trashed"
            message = "原件已从回收站清理。" if status == "purged" else "原件在知识库回收站。"
            return FileLocation(self.folder / entry.original_path if entry else default, status, message)
        path = self.folder / row.path if row else default
        invalid = None
        try:
            path = checked_path(self.folder, self.relative(path))
            if self.owned_elsewhere(self.relative(path), file_id):
                return FileLocation(path, "blocked", "原路径已关联其他文件。请在知识库修复文件位置。")
            current = identity_of(path)
        except InvalidLocation as error:
            current = None
            invalid = str(error)
        except OSError:
            return FileLocation(path, "unreadable", "原路径暂时不可读。请检查文件类型、链接和权限。")
        if row is None:
            if invalid is not None:
                return FileLocation(path, "unreadable", invalid)
            if current is not None:
                self.bind(file_id, path, current, known_body(path))
            return FileLocation(path, "ready" if current is not None else "missing")

        if current is not None and row.file_key == current.key and (
            (row.birth_time is not None and row.birth_time == current.birth and (row.file_key or not row.missing))
            or (not row.missing and (row.birth_time is None or current.birth is None))
        ):
            if row.birth_time is None and current.birth is not None:
                row.birth_time = current.birth
            if row.missing:
                row.missing = False
            return FileLocation(path, "ready")

        candidates = self.matches(row, default.suffix.lower(), exclude=path)
        if len(candidates) == 1:
            found, stat = candidates[0]
            if not self.owned_elsewhere(self.relative(found), file_id) and not self.bound_to_other_file(file_id, stat):
                self.bind(file_id, found, stat, known_body(found))
                return FileLocation(found, "ready")
        if candidates:
            row.missing = True
            return FileLocation(path, "blocked", "找到多个可能位置或路径已占用。请在知识库确认文件位置。")
        if current is not None:
            current_revision = known_body(path)
            if not row.missing or (current_revision is not None and current_revision == row.revision):
                self.bind(file_id, path, current, current_revision)
                return FileLocation(path, "ready")
            return FileLocation(path, "blocked", "原路径出现了其他内容。请在知识库确认要关联的文件。")
        row.missing = True
        return FileLocation(path, "blocked" if invalid else "missing", invalid)

    def matches(self, row: LibraryLocation, suffix: str, *, exclude: Path):
        if not row.file_key:
            return []
        entries = self.inventory()
        matches = []
        for path in self._by_key[row.file_key]:
            if path == exclude or path.suffix.lower() != suffix:
                continue
            stat = entries[path]
            credible = row.birth_time is not None and row.birth_time == stat.birth
            if not credible and (sys.platform == "win32" or row.birth_time is None or stat.birth is None):
                # Windows can carry a prior occupant's creation time over to the renamed file.
                credible = row.revision is not None and known_body(path) == row.revision
            if credible:
                matches.append((path, stat))
        return matches

    def documents(self, entries: list[LibraryDocument]) -> list[LibraryDocument]:
        resolved = []
        for item in entries:
            location = self.resolve(item.id, item.path)
            name = location.path.name if item.kind == "import" and location.path.name != item.path.name else item.name
            resolved.append(replace(item, path=location.path, name=name, location_status=location.status,
                                    location_message=location.message))
        return resolved

    def repairable(self, file_id: str, default: Path) -> FileLocation:
        result = self.resolve(file_id, default)
        if result.status != "missing" and result.status != "blocked":
            raise LocationConflict("原文件目前可打开或暂时无法检查，请先刷新知识库。")
        return result

    def candidate(self, file_id: str, relative: str, suffix: str) -> tuple[Path, FileIdentity]:
        path = checked_path(self.folder, relative)
        if path.suffix.lower() != suffix:
            raise InvalidLocation("候选文件的类型和原文件不同。")
        if self.owned_elsewhere(relative, file_id):
            raise LocationConflict("此路径已关联到知识库中的另一份文件。")
        info = identity_of(path)
        if info is None:
            raise LocationConflict("候选文件已移动或删除，请重新选择。")
        if self.bound_to_other_file(file_id, info):
            raise LocationConflict("该文件的另一个位置已经关联到知识库。")
        return path, info
