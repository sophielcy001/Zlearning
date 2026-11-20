from ctypes import CDLL, get_errno
from dataclasses import dataclass
from errno import EEXIST, ENOTSUP
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISDIR, S_ISREG
import sys
from unicodedata import normalize

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .db import BackupFileOrigin, LibraryFileCopy, LibraryLocation, LibraryTrash, NoteWrite, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path, identity_of
from .library import local_path


ROOT = "library"
WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", re.IGNORECASE)


class NewFolder(BaseModel):
    parent: str = Field(min_length=1, max_length=1400)
    name: str = Field(min_length=1, max_length=255)


class Relocation(NewFolder):
    source: str = Field(min_length=1, max_length=1400)
    fileId: str | None = Field(default=None, min_length=1, max_length=100)


class ConfirmRelocation(Relocation):
    token: str = Field(pattern="^[a-f0-9]{64}$")


def entry_stat(path: Path):
    info = path.lstat()
    if (getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
            or not (S_ISDIR(info.st_mode) or S_ISREG(info.st_mode))):
        raise InvalidLocation("文件或文件夹包含链接或特殊条目。请选择知识库内的普通位置。")
    return info


def directory_path(workspace: Path, relative: str, *, allow_missing_root: bool = False) -> Path:
    # Check the root's reparse point even when its lexical and resolved paths compare equal.
    path = workspace / ROOT if relative == ROOT else checked_path(workspace, relative)
    try:
        if path.resolve() != path:
            raise InvalidLocation("文件夹通过链接指向了其他位置。")
        if not S_ISDIR(entry_stat(path).st_mode):
            raise InvalidLocation("请选择实际存在的文件夹。")
    except FileNotFoundError:
        if not (allow_missing_root and relative == ROOT):
            raise LocationConflict("文件夹已移动或不存在，请刷新目录。") from None
    return path


def child_path(workspace: Path, parent: Path, name: str) -> Path:
    if (name.startswith(".") or name.endswith((" ", "."))
            or any(ord(char) < 32 or char in '/\\:<>|?*"' for char in name)
            or WINDOWS_DEVICE.fullmatch(name.partition(".")[0].rstrip(" "))):
        raise InvalidLocation("请输入单个可用于 Windows 和 macOS 的文件名或文件夹名。")
    relative = (parent / name).relative_to(workspace).as_posix()
    if len(relative) > 1400:
        raise InvalidLocation("目标路径过长，请使用更短的名称或选择上级文件夹。")
    return checked_path(workspace, relative)


def relative_entries(workspace: Path) -> list[dict]:
    base = directory_path(workspace, ROOT, allow_missing_root=True)
    entries = [{"path": ROOT, "parent": None, "name": "知识库根目录"}]
    pending = [base] if base.exists() else []
    while pending:
        parent = pending.pop()
        for path in sorted(parent.iterdir(), key=lambda entry: entry.name.casefold()):
            try:
                if path.name.startswith(".") or not path.is_dir():
                    continue
                directory_path(workspace, path.relative_to(workspace).as_posix())
            except (InvalidLocation, LocationConflict, FileNotFoundError):
                continue
            entries.append({"path": path.relative_to(workspace).as_posix(),
                            "parent": parent.relative_to(workspace).as_posix(), "name": path.name})
            pending.append(path)
    return entries


def rename_without_replacing(source: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(source, target)
        return
    libc = CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        result = libc.renamex_np(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(ENOTSUP, "Exclusive rename is unavailable")
    if result != 0:
        code = get_errno()
        raise OSError(code, os.strerror(code), str(target))


def validate_destination(source: Path, target: Path, source_stat) -> None:
    if source.as_posix() == target.as_posix():
        raise LocationConflict("请选择不同的目录或名称。")
    try:
        current = entry_stat(target)
    except FileNotFoundError:
        return
    # A case-only name change can address the same directory entry on Windows/macOS.
    if (source.parent == target.parent and os.path.samestat(source_stat, current)
            and normalize("NFC", source.name).casefold() == normalize("NFC", target.name).casefold()
            and not any(entry.name == target.name for entry in target.parent.iterdir())):
        return
    raise FileExistsError(EEXIST, "Destination exists", str(target))


def tree_stamp(source: Path, source_stat, destination_parent: Path) -> tuple[str, int]:
    digest = sha256()
    folder_stat = entry_stat(destination_parent)
    digest.update(f"{folder_stat.st_dev}:{folder_stat.st_ino}\0".encode())
    count = 0
    pending = [(source, source_stat)]
    while pending:
        path, info = pending.pop()
        entry = (path.relative_to(source).as_posix(), info.st_mode, info.st_dev, info.st_ino,
                 info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        digest.update(json.dumps(entry, ensure_ascii=True).encode())
        if S_ISDIR(info.st_mode):
            pending.extend((child, entry_stat(child)) for child in sorted(path.iterdir()))
        else:
            count += 1
    return digest.hexdigest(), count


@dataclass(frozen=True)
class MovePlan:
    source: Path
    target: Path
    rows: dict[str, str]
    count: int
    token: str


def plan_move(db, workspace: Path, data: Relocation, tracker: LocationTracker | None = None) -> MovePlan:
    parent = directory_path(workspace, data.parent)
    target = child_path(workspace, parent, data.name)
    source = checked_path(workspace, data.source)
    source_info = entry_stat(source)
    is_folder = S_ISDIR(source_info.st_mode)
    if is_folder != (data.fileId is None):
        raise LocationConflict("原位置的条目类型已变化，请刷新。")
    if is_folder and parent.is_relative_to(source):
        raise LocationConflict("不能把文件夹移入自身或其子目录。")
    if not is_folder and source.suffix.lower() != target.suffix.lower():
        raise InvalidLocation("重命名时请保留文件扩展名。")
    validate_destination(source, target, source_info)

    tracker = tracker or LocationTracker(db, workspace)
    candidates = tracker.locations.values()
    if data.fileId is not None:
        row = tracker.locations.get(data.fileId)
        if row is None or row.missing or row.trash_id or row.path != data.source:
            raise LocationConflict("原件的位置已变化，请刷新文件预览。")
        candidates = (row,)

    def included(path: str | None) -> bool:
        return path is not None and (path == data.source or is_folder and path.startswith(data.source + "/"))

    rows = {}
    for row in candidates:
        if row.trash_id or row.missing or not included(row.path):
            continue
        old = workspace / row.path
        actual = identity_of(checked_path(workspace, row.path))
        if actual is None or actual.key != row.file_key or row.birth_time and actual.birth != row.birth_time:
            raise LocationConflict("目录内有文件发生变化，请刷新后重新预览。")
        destination = tracker.relative(target / old.relative_to(source)) if is_folder else tracker.relative(target)
        if tracker.owned_elsewhere(destination, row.id):
            raise LocationConflict("目标位置已关联另一份知识库文件。请选择其他名称。")
        rows[row.id] = destination
    if data.fileId is not None and data.fileId not in rows:
        raise LocationConflict("原件已变化，请刷新文件预览。")

    pending_notes = db.execute(select(NoteWrite.note_id, NoteWrite.target_path).where(NoteWrite.completed.is_(False)))
    pending_trash = db.execute(select(LibraryTrash.original_path, LibraryTrash.restore_path).where(
        LibraryTrash.state.in_(("deleting", "restoring")),
    ))
    if (any(f"note-{identity}" in rows or included(path or tracker.relative(local_path(workspace, "notes", identity)))
            for identity, path in pending_notes)
            or any(included(old) or included(restoring) for old, restoring in pending_trash)):
        raise LocationConflict("此位置有尚未完成的保存或恢复，请完成后重试。")
    pending_copies = db.scalars(select(BackupFileOrigin).where(BackupFileOrigin.published.is_(False)))
    if any(included(item.source_path) or included(tracker.relative(local_path(workspace, "imports", item.file_id,
           Path(item.name).suffix.lower()))) for item in pending_copies):
        raise LocationConflict("此位置有尚未完成的文件找回，请完成后重试。")
    pending_publications = db.scalars(select(LibraryFileCopy.target).where(LibraryFileCopy.state == "staged"))
    if any(included(path) or path == tracker.relative(target) for path in pending_publications):
        raise LocationConflict("此位置有尚未完成的文件复制，请完成后重试。")

    fingerprint, count = tree_stamp(source, source_info, parent)
    token = sha256(json.dumps([data.source, tracker.relative(target), data.fileId, sorted(rows.items()),
                              fingerprint], ensure_ascii=True).encode()).hexdigest()
    return MovePlan(source, target, rows, count, token)


def apply_move(db, plan: MovePlan) -> None:
    rename_without_replacing(plan.source, plan.target)
    # The existing inode and revision let the tracker recover an interrupted location update.
    for identity, destination in plan.rows.items():
        db.get(LibraryLocation, identity).path = destination


def folders_router(engine, workspace: Path, index) -> APIRouter:
    router = APIRouter()

    @router.get("/folders")
    def folders():
        try:
            return relative_entries(workspace)
        except (OSError, InvalidLocation, LocationConflict, RuntimeError) as error:
            raise HTTPException(409, "暂时无法读取实际目录，请检查知识库位置和权限。") from error

    @router.post("/folders", status_code=201)
    def create_folder(data: NewFolder):
        try:
            with transaction(engine):
                parent = directory_path(workspace, data.parent, allow_missing_root=True)
                if data.parent == ROOT:
                    parent.mkdir(exist_ok=True)
                    directory_path(workspace, data.parent)
                child = child_path(workspace, parent, data.name)
                child.mkdir()
                return {"path": child.relative_to(workspace).as_posix(), "parent": data.parent, "name": child.name}
        except InvalidLocation as error:
            raise HTTPException(422, str(error)) from error
        except FileExistsError as error:
            raise HTTPException(409, "当前文件夹已有同名条目。") from error
        except LocationConflict as error:
            raise HTTPException(409, str(error)) from error
        except (OSError, SQLAlchemyError, RuntimeError) as error:
            raise HTTPException(409, "无法创建文件夹，请检查知识库位置、权限和空间。") from error

    def relocate(data: Relocation, token: str | None = None):
        try:
            index.refresh()
            with transaction(engine) as db:
                plan = plan_move(db, workspace, data)
                path = plan.target.relative_to(workspace).as_posix()
                if token is None:
                    return {"source": data.source, "path": path, "fileCount": plan.count,
                            "trackedCount": len(plan.rows), "token": plan.token}
                if token != plan.token:
                    raise LocationConflict("文件内容或位置在预览后发生变化，请重新预览。")
                apply_move(db, plan)
            return {"path": path}
        except InvalidLocation as error:
            raise HTTPException(422, str(error)) from error
        except FileExistsError as error:
            raise HTTPException(409, "目标位置已有同名条目。") from error
        except LocationConflict as error:
            raise HTTPException(409, str(error)) from error
        except (OSError, SQLAlchemyError, RuntimeError) as error:
            raise HTTPException(409, "无法完成移动，请刷新目录并检查同卷位置和文件是否正在使用。") from error

    @router.post("/relocation/preview")
    def preview_relocation(data: Relocation):
        return relocate(data)

    @router.put("/relocation")
    def confirm_relocation(data: ConfirmRelocation):
        return relocate(data, data.token)

    return router
