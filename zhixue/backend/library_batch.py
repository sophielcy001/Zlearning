from hashlib import sha256
from hmac import compare_digest, digest
import json
from pathlib import Path
from secrets import token_bytes
from tempfile import TemporaryFile
from typing import Literal
from unicodedata import category, normalize
from urllib.parse import quote
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from .db import LibraryFileCopy, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path
from .library_copies import CopyIntent, directory_identity, finish_copy, stage_copy
from .library_folders import WINDOWS_DEVICE, Relocation, apply_move, child_path, directory_path, plan_move
from .library_metadata import MAX_SELECTION, FileId, FileSelection, selected_documents
from .library_trash import FileVersion, file_version


BatchAction = Literal["move", "copy", "export"]
BUFFER = 128 * 1024


class BatchPreview(FileSelection):
    action: BatchAction
    parent: str | None = Field(default=None, min_length=1, max_length=1400)


class BatchProof(BaseModel):
    id: FileId
    source: str = Field(min_length=1, max_length=1400)
    target: str = Field(min_length=1, max_length=1400)
    token: str = Field(pattern="^[a-f0-9]{64}$")
    copyId: UUID | None = None


class BatchConfirmation(BaseModel):
    action: BatchAction
    parent: str | None = Field(default=None, min_length=1, max_length=1400)
    files: list[BatchProof] = Field(min_length=1, max_length=MAX_SELECTION)

    @field_validator("files")
    @classmethod
    def distinct(cls, files: list[BatchProof]) -> list[BatchProof]:
        if len({file.id for file in files}) != len(files):
            raise ValueError("一个批次中每份文件只能选择一次。")
        return files


def original(tracker: LocationTracker, entry) -> tuple[Path, FileVersion]:
    if entry is None:
        raise LocationConflict("文件记录已移除。")
    row = tracker.locations.get(entry.id)
    if row is None or row.missing or row.trash_id or row.path != entry.path:
        raise LocationConflict("原件已移动、缺失或在回收站。请检查文件位置。")
    path = checked_path(tracker.folder, row.path)
    version = file_version(path)
    if (version is None or version.identity.key != row.file_key
            or row.birth_time and version.identity.birth != row.birth_time):
        raise LocationConflict("原件的位置已变化，请刷新后重试。")
    return path, version


def confirmation(secret: bytes, action: BatchAction, file_id: str, source: str, target: str,
                 version: FileVersion, parent: str | None, copy_id: str | None = None):
    fields = [action, file_id, source, target, version.revision, version.identity.key, version.identity.birth, parent, copy_id]
    return digest(secret, json.dumps(fields, ensure_ascii=True).encode(), "sha256").hex()


def safe_file_label(name: str, file_format: str, *, copy: bool = False) -> str:
    suffix = "." + file_format
    stem = name[:-len(suffix)] if name.lower().endswith(suffix) else name
    stem = "".join("_" if char in '/\\:<>|?*"' or category(char) in {"Cc", "Cs"} else char for char in stem)
    stem = stem.strip(" .").encode()[:180].decode(errors="ignore").rstrip(" .") or "资料"
    if WINDOWS_DEVICE.fullmatch(stem.partition(".")[0].rstrip(" ")):
        stem = "资料-" + stem
    return stem + ("（副本）" if copy else "") + suffix


def unique_label(name: str, reserved: set[str]) -> str:
    candidate = name
    stem, _, suffix = name.rpartition(".")
    count = 2
    while normalize("NFC", candidate).casefold() in reserved:
        candidate = f"{stem} ({count}).{suffix}"
        count += 1
    return candidate


def local_error(failure: Exception) -> str:
    if isinstance(failure, (LocationConflict, InvalidLocation)):
        return str(failure)
    if isinstance(failure, FileExistsError):
        return "目标文件夹中已有同名条目。"
    return "无法处理这份文件，请检查文件完整性、同卷位置、权限和剩余空间。"


def export_member(output: ZipFile, member: str, path: Path, version: FileVersion) -> None:
    hasher = sha256()
    with path.open("rb") as original_bytes, output.open(member, "w", force_zip64=True) as destination:
        while chunk := original_bytes.read(BUFFER):
            hasher.update(chunk)
            destination.write(chunk)
    if hasher.hexdigest() != version.revision:
        raise LocationConflict(f"{member} 在导出过程中有改动，请重新预览。")


def stream_archive(handle):
    try:
        while chunk := handle.read(BUFFER):
            yield chunk
    finally:
        handle.close()


def batch_router(engine, workspace: Path, index, day) -> APIRouter:
    router = APIRouter(prefix="/batch")
    secret = token_bytes(32)

    def destination(data: BatchPreview | BatchConfirmation):
        if data.action == "export":
            return None, None
        if data.parent is None:
            raise InvalidLocation("请选择目标文件夹。")
        path = directory_path(workspace, data.parent)
        return path, directory_identity(path)

    def checked_destination(data: BatchPreview | BatchConfirmation):
        try:
            return destination(data)
        except InvalidLocation as failure:
            raise HTTPException(422, str(failure)) from failure
        except (OSError, LocationConflict) as failure:
            raise HTTPException(409, local_error(failure)) from failure

    @router.post("/preview")
    def preview(data: BatchPreview):
        index.refresh()
        parent, stamp = checked_destination(data)
        entries = []
        with transaction(engine) as db:
            documents = selected_documents(db, data.ids)
            tracker = LocationTracker(db, workspace)
            reserved = set()
            pending_sources = set()
            if data.action == "copy":
                try:
                    reserved.update(normalize("NFC", child.name).casefold() for child in parent.iterdir())
                except OSError as failure:
                    raise HTTPException(409, "无法读取目标文件夹，请检查目录权限。") from failure
                reserved.update(normalize("NFC", Path(row.path).name).casefold()
                                for row in tracker.locations.values() if Path(row.path).parent.as_posix() == data.parent)
                pending = db.execute(select(LibraryFileCopy.source_id, LibraryFileCopy.target).where(
                    LibraryFileCopy.state == "staged", LibraryFileCopy.source_id.in_(data.ids),
                ))
                pending_sources = {source for source, target in pending if Path(target).parent.as_posix() == data.parent}
            for identity in data.ids:
                item = documents.get(identity)
                entry = {"id": identity, "name": item.name if item else None}
                try:
                    if identity in pending_sources:
                        raise LocationConflict("该文件在此目录有待恢复的副本，请继续原批次或重启后再试。")
                    path, version = original(tracker, item)
                    copy_id = None
                    if data.action == "move":
                        plan = plan_move(db, workspace, Relocation(
                            source=item.path, fileId=identity, parent=data.parent, name=path.name,
                        ), tracker)
                        target = tracker.relative(plan.target)
                        if normalize("NFC", target).casefold() in reserved:
                            raise LocationConflict("所选文件中有同名原件，请先分别重命名。")
                        reserved.add(normalize("NFC", target).casefold())
                    else:
                        copying = data.action == "copy"
                        name = unique_label(safe_file_label(item.name, item.format, copy=copying), reserved)
                        reserved.add(normalize("NFC", name).casefold())
                        target = tracker.relative(child_path(workspace, parent, name)) if copying else name
                        if copying:
                            copy_id = str(uuid4())
                            entry["copyId"] = copy_id
                    entry.update(source=item.path, target=target, token=confirmation(
                        secret, data.action, identity, item.path, target, version, stamp, copy_id,
                    ))
                except (OSError, InvalidLocation, LocationConflict) as failure:
                    entry["error"] = local_error(failure)
                entries.append(entry)
        return {"entries": entries}

    def verified(tracker, item, file: BatchProof, action: BatchAction, stamp):
        path, version = original(tracker, item)
        copy_id = str(file.copyId) if file.copyId else None
        expected = confirmation(secret, action, file.id, file.source, file.target, version, stamp, copy_id)
        if not compare_digest(file.token, expected):
            raise LocationConflict("原件或目标已在预览后变化，请重新预览。")
        return path, version

    def move(data: BatchConfirmation):
        outcomes = []
        for file in data.files:
            try:
                with transaction(engine) as db:
                    _, stamp = destination(data)
                    tracker = LocationTracker(db, workspace)
                    item = selected_documents(db, [file.id]).get(file.id)
                    path, _ = verified(tracker, item, file, "move", stamp)
                    if item.path != file.target:
                        if item.path != file.source:
                            raise LocationConflict("原件已移到其他位置，请重新预览。")
                        plan = plan_move(db, workspace, Relocation(
                            source=file.source, fileId=file.id, parent=data.parent, name=path.name,
                        ), tracker)
                        if tracker.relative(plan.target) != file.target:
                            raise LocationConflict("目标位置已变化，请重新预览。")
                        apply_move(db, plan)
                outcomes.append({"id": file.id, "path": file.target})
            except (OSError, InvalidLocation, LocationConflict, SQLAlchemyError) as failure:
                outcomes.append({"id": file.id, "error": local_error(failure)})
        return {"entries": outcomes}

    def copy(data: BatchConfirmation):
        outcomes = []
        for file in data.files:
            try:
                if file.copyId is None:
                    raise LocationConflict("复制确认不完整，请重新预览。")
                copy_id = str(file.copyId)
                with transaction(engine) as db:
                    previous = db.get(LibraryFileCopy, copy_id)
                    if previous:
                        if (previous.source_id != file.id or previous.target != file.target
                                or not compare_digest(previous.confirmation, file.token)):
                            raise LocationConflict("复制确认已失效，请重新预览。")
                    else:
                        parent, stamp = destination(data)
                        tracker = LocationTracker(db, workspace)
                        item = selected_documents(db, [file.id]).get(file.id)
                        path, version = verified(tracker, item, file, "copy", stamp)
                        if item.path != file.source:
                            raise LocationConflict("原件已移动，请重新预览。")
                        target = child_path(workspace, parent, Path(file.target).name)
                        if tracker.relative(target) != file.target:
                            raise LocationConflict("目标位置已变化，请重新预览。")
                        if target.exists() or tracker.owned_elsewhere(file.target, copy_id):
                            raise FileExistsError()
                        stage_copy(db, workspace, path, version, CopyIntent(
                            copy_id, file.id, file.target, stamp, file.token,
                        ), day)
                try:
                    outcomes.append(finish_copy(engine, workspace, copy_id))
                except (OSError, InvalidLocation, LocationConflict, SQLAlchemyError):
                    outcomes.append({"id": file.id, "pending": True,
                                     "error": "副本已暂存。可继续本次复制，或在重启时恢复。"})
            except (OSError, InvalidLocation, LocationConflict, SQLAlchemyError) as failure:
                outcomes.append({"id": file.id, "error": local_error(failure)})
        return {"entries": outcomes}

    def export(data: BatchConfirmation):
        names = {normalize("NFC", file.target).casefold() for file in data.files}
        if len(names) != len(data.files):
            raise HTTPException(409, "导出名单中有同名文件，请重新预览。")
        archive = None
        try:
            archive = TemporaryFile(mode="w+b")
            with transaction(engine, write=False) as db, ZipFile(
                archive, "w", compression=ZIP_DEFLATED, compresslevel=3,
            ) as zip_archive:
                tracker = LocationTracker(db, workspace)
                matches = selected_documents(db, [file.id for file in data.files])
                for file in data.files:
                    item = matches.get(file.id)
                    path, version = verified(tracker, item, file, "export", None)
                    if item.path != file.source:
                        raise LocationConflict("有原件已移动，请重新预览。")
                    export_member(zip_archive, file.target, path, version)
            size = archive.tell()
            archive.seek(0)
        except (OSError, InvalidLocation, LocationConflict, SQLAlchemyError) as failure:
            if archive is not None:
                archive.close()
            raise HTTPException(409, local_error(failure)) from failure
        return StreamingResponse(
            stream_archive(archive), media_type="application/zip", background=BackgroundTask(archive.close),
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote("智学知识库文件.zip"),
                     "Content-Length": str(size)},
        )

    @router.post("/confirm")
    def confirm(data: BatchConfirmation):
        checked_destination(data)
        index.refresh()
        if data.action == "move":
            return move(data)
        if data.action == "copy":
            return copy(data)
        return export(data)

    return router
