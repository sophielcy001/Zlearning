from contextlib import contextmanager
from datetime import UTC, datetime
from errno import EDQUOT, ENOSPC
from pathlib import Path
import sqlite3
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy.exc import SQLAlchemyError

from .backup_automatic import AutomaticBackups
from .backup_catalog import BackupCatalog, CatalogItem
from .backup_copies import confirmation, copy_name, recover_copies, restore_copy
from .backup_format import BackupError, damaged
from .backup_jobs import save_archive
from .backup_restore import restore_confirmation, restore_full
from .backup_snapshot import (
    DATABASE, ArchivePath, current_view, library_path, read_backup, verified_database,
)
from .backup_work import WORK_FOLDER, backup_workspace
from .file_locations import InvalidLocation, LocationConflict


ERROR_STATUS = {
    "not_found": 404, "unavailable": 409, "changed": 409, "pending_write": 409,
    "stale": 409, "busy": 409, "copy_write": 409, "exists": 409, "credential_store": 409,
}


class BackupRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse({"detail": "请检查备份密码和所选文件。"}, status_code=422)
            except BackupError as failure:
                return JSONResponse(
                    {"detail": str(failure), "code": failure.code}, status_code=ERROR_STATUS.get(failure.code, 422),
                )
            except (OSError, sqlite3.Error, SQLAlchemyError, InvalidLocation, LocationConflict) as error:
                origin = getattr(error, "orig", error)
                space = getattr(origin, "errno", None) in (ENOSPC, EDQUOT) or getattr(
                    origin, "sqlite_errorcode", None
                ) == sqlite3.SQLITE_FULL
                message = "保存空间不足，请检查目标磁盘和本机剩余空间。" if space else "备份读写失败，请检查文件位置、目录权限和备份状态。"
                return JSONResponse({"detail": message}, status_code=507 if space else 409)

        return handle


class BackupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasswordInput(BackupInput):
    password: Annotated[SecretStr, Field(min_length=1, max_length=512)]


class DestinationInput(BackupInput):
    directory: Annotated[str, Field(min_length=1, max_length=2048)] | None

    @field_validator("directory")
    @classmethod
    def safe_directory(cls, directory: str | None) -> str | None:
        if directory and any(ord(char) < 32 for char in directory):
            raise ValueError("目录包含无效字符。")
        return directory


class RetentionInput(BackupInput):
    pinned: bool


class CopyInput(PasswordInput):
    path: ArchivePath
    confirmation: Annotated[str, Field(min_length=1, max_length=180)]

    @field_validator("path")
    @classmethod
    def safe_path(cls, path: str) -> str:
        if not library_path(path):
            raise ValueError("请选择知识库中的文件。")
        return path


class RestoreInput(PasswordInput):
    confirmation: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


@contextmanager
def backup_operation(engine, catalog: BackupCatalog):
    with backup_workspace(catalog.folder) as work:
        catalog.recover_interrupted()
        recover_copies(engine, catalog.folder)
        yield work


def recover_backups(engine, folder: Path) -> None:
    if not (folder / WORK_FOLDER).exists():
        return
    try:
        with backup_workspace(folder, allocate=False):
            BackupCatalog(folder).recover_interrupted()
            recover_copies(engine, folder)
    except (OSError, BackupError, sqlite3.Error, SQLAlchemyError):
        # A second application can still hold the lease. Retry recovery during the next backup operation.
        pass


def require_archive(catalog: BackupCatalog, identity: UUID) -> CatalogItem:
    item = catalog.get(identity)
    if item.status not in ("ready", "candidate"):
        raise BackupError("unavailable", item.error or "这份备份暂时不可读取。")
    return item


def preview_archive(engine, catalog: BackupCatalog, item: CatalogItem, password: str, secret: bytes, work: Path,
                    can_restore: bool = False) -> dict:
    folder = catalog.folder
    database = work / DATABASE
    def verify(candidate: CatalogItem):
        with database.open("wb") as handle:
            backup = read_backup(candidate.path, password, extract={DATABASE: handle})
        if str(backup.header.identity) != item.id:
            raise BackupError("stale", "此备份已变更，请重新选择。")
        counts, versions = verified_database(database, backup.manifest.database_schema, compare=True)
        if counts != backup.manifest.counts:
            raise damaged()
        return backup, counts, versions

    rejected_archive = {"authentication", "damaged"}
    try:
        backup, backup_counts, backup_versions = verify(item)
    except BackupError as original:
        if item.checksum is not None or original.code not in rejected_archive:
            raise
        for candidate in catalog.alternatives(item):
            try:
                backup, backup_counts, backup_versions = verify(candidate)
            except BackupError as failure:
                if failure.code not in rejected_archive:
                    raise
            else:
                break
        else:
            raise
    if item.checksum is None:
        catalog.remember_verified(item.id, backup.checksum)
    counts, versions, current, fingerprint, timer = current_view(engine, folder)
    backed_up = {file.path for file in backup.manifest.files}
    files = []
    for file in backup.manifest.files:
        original = current.get(file.path)
        revision = original["revision"] if original else "missing"
        if original is None and (folder / file.path).is_dir():
            revision = "occupied"
        name = copy_name(file)
        files.append({
            **file.model_dump(),
            "change": "missing" if revision == "missing" else "same" if revision == file.revision else "changed",
            "currentSize": original["size"] if original else None,
            "copyName": name,
            "confirmation": confirmation(secret, backup, file, revision) if name else None,
        })
    return {
        "id": item.id, "createdAt": backup.header.created_at.isoformat(), "databaseSchema": backup.manifest.database_schema,
        "counts": {"backup": backup.manifest.counts, "current": counts}, "files": files,
        "databaseChanges": [
            table for table in dict.fromkeys([*backup_versions, *versions])
            if versions.get(table) != backup_versions.get(table)
        ],
        "currentOnly": [current[path] for path in sorted(current.keys() - backed_up)],
        "restore": {"available": can_restore, "activeTimer": timer,
                    "confirmation": restore_confirmation(secret, backup, fingerprint) if can_restore and not timer else None},
    }


def backup_router(engine, folder: Path, session_token: str, day, backup_credentials=None) -> APIRouter:
    router = APIRouter(prefix="/api/backups", route_class=BackupRoute)
    catalog = BackupCatalog(folder)
    automatic = AutomaticBackups(catalog, backup_credentials)
    secret = session_token.encode("utf-8")

    @router.get("")
    def list_backups():
        directory, items, issue = catalog.items()
        if any(item.status in ("writing", "expiring") for item in items):
            recover_backups(engine, folder)
            directory, items, issue = catalog.items()
        return {"directory": str(directory), "items": [item.view() for item in items],
                "directoryIssue": issue, "automatic": automatic.view()}

    @router.put("/destination")
    def change_destination(payload: DestinationInput):
        with backup_workspace(folder, allocate=False):
            return {"directory": str(catalog.change_directory(payload.directory))}

    @router.get("/automatic")
    def automatic_status():
        return automatic.view()

    @router.put("/automatic")
    def configure_automatic(payload: PasswordInput):
        return automatic.change(payload.password.get_secret_value())

    @router.delete("/automatic")
    def stop_automatic():
        return automatic.change(None)

    @router.post("", status_code=201)
    def save_backup(payload: PasswordInput):
        with backup_operation(engine, catalog) as work:
            return save_archive(engine, catalog, work, payload.password.get_secret_value(), datetime.now(UTC)).view()

    @router.patch("/{identity}/retention")
    def retain(identity: UUID, payload: RetentionInput):
        with backup_workspace(folder, allocate=False):
            return catalog.pin(identity, payload.pinned).view()

    @router.post("/{identity}/preview")
    def preview(identity: UUID, payload: PasswordInput, request: Request):
        with backup_operation(engine, catalog) as work:
            return preview_archive(
                engine, catalog, require_archive(catalog, identity), payload.password.get_secret_value(), secret, work,
                can_restore=getattr(request.app.state, "restore_runtime", None) is not None,
            )

    @router.get("/restore-status")
    def restore_status(request: Request):
        runtime = getattr(request.app.state, "restore_runtime", None)
        return runtime.status() if runtime else {"state": "idle"}

    @router.post("/{identity}/restore", status_code=202)
    def begin_restore(identity: UUID, payload: RestoreInput, request: Request):
        runtime = getattr(request.app.state, "restore_runtime", None)
        if runtime is None:
            raise BackupError("unavailable", "请从智学启动入口打开整体恢复。")
        item = require_archive(catalog, identity)
        password = payload.password.get_secret_value()
        return runtime.submit(str(identity), lambda operation, progress: restore_full(
            engine, folder, item, identity, password, payload.confirmation, secret, operation, progress,
        ))

    @router.post("/{identity}/files/restore", status_code=201)
    def restore(identity: UUID, payload: CopyInput):
        with backup_operation(engine, catalog) as work:
            return restore_copy(
                engine, folder, require_archive(catalog, identity).path, identity, payload.password.get_secret_value(),
                payload.path, payload.confirmation, secret, day, work,
            )

    return router
