from hmac import compare_digest, digest
import json
import os
from pathlib import Path
from stat import S_ISREG
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from sqlalchemy import select

from .backup_format import BackupError
from .backup_snapshot import ArchiveFile, VerifiedBackup, current_revision, plain, read_backup, stamp, transfer_file
from .db import BackupFileOrigin, ImportedFile, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path, identity_of
from .library import local_path
from .library_api import validate_filename


def copy_name(file: ArchiveFile) -> str | None:
    suffix = Path(file.path).suffix.lower()
    filename = Path(file.name)
    stem = filename.stem if filename.suffix.lower() == suffix else file.name
    candidate = f"{stem[:166]}（备份副本）{suffix}"
    try:
        return validate_filename(candidate)
    except ValueError:
        return None


def sign_plan(secret: bytes, backup: VerifiedBackup, file: ArchiveFile, identity: UUID, current: str) -> str:
    fields = [str(backup.header.identity), backup.revision, file.path, file.revision, identity.hex, current]
    return digest(secret, json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode(), "sha256").hex()


def confirmation(secret: bytes, backup: VerifiedBackup, file: ArchiveFile, current: str) -> str:
    identity = uuid4()
    return f"{identity.hex}.{current}.{sign_plan(secret, backup, file, identity, current)}"


def parse_plan(token: str, secret: bytes, backup: VerifiedBackup, file: ArchiveFile) -> tuple[str, str]:
    try:
        raw_identity, current, signature = token.split(".")
        identity = UUID(hex=raw_identity)
        if compare_digest(signature, sign_plan(secret, backup, file, identity, current)):
            return str(identity), current
    except (TypeError, ValueError):
        pass
    raise BackupError("stale", "找回确认已失效，请重新预览。")


def copy_path(folder: Path, record: BackupFileOrigin) -> Path:
    path = local_path(folder, "imports", record.file_id, Path(record.name).suffix.lower())
    return checked_path(folder, path.relative_to(folder).as_posix())


def register_copy(db, folder: Path, path: Path, record: BackupFileOrigin) -> None:
    info = identity_of(path)
    if info is None:
        raise BackupError("missing_copy", "找回的副本未写入，请重试。")
    LocationTracker(db, folder).bind(record.file_id, path, info, record.source_revision)
    db.add(ImportedFile(id=record.file_id, name=record.name, created_on=record.created_on))
    record.published = True


def matches_copy(path: Path, record: BackupFileOrigin) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return S_ISREG(info.st_mode) and plain(info) and transfer_file(path, stamp(info)) == record.source_revision


def recover_copies(engine, folder: Path) -> None:
    with transaction(engine) as db:
        pending = db.scalars(select(BackupFileOrigin).where(BackupFileOrigin.published.is_(False))).all()
        for record in pending:
            try:
                path = copy_path(folder, record)
                if matches_copy(path, record):
                    register_copy(db, folder, path, record)
                else:
                    db.delete(record)
            except (OSError, InvalidLocation, LocationConflict, BackupError):
                # The publication is still journaled for the next attempt.
                continue


def restore_copy(engine, folder: Path, archive: Path, expected_id: UUID, password: str, relative: str,
                 token: str, secret: bytes, day, work: Path) -> dict:
    with TemporaryDirectory(dir=work, prefix="restore-") as staging:
        source = Path(staging) / "copy"
        with source.open("wb") as handle:
            backup = read_backup(archive, password, extract={relative: handle})
            handle.flush()
            os.fsync(handle.fileno())
        if backup.header.identity != expected_id:
            raise BackupError("stale", "此备份已变更，请重新预览。")
        file = next((file for file in backup.manifest.files if file.path == relative), None)
        name = copy_name(file) if file else None
        if not file or not name:
            raise BackupError("file_type", "请选择可找回的知识库资料或笔记。")
        identity, seen = parse_plan(token, secret, backup, file)

        with transaction(engine) as db:
            existing = db.get(BackupFileOrigin, identity)
            if existing and existing.published:
                return {"id": identity, "name": existing.name}
            if current_revision(folder, relative) != seen:
                raise BackupError("stale", "原位置已变化，请重新预览后再找回。")
            if existing is None:
                db.add(BackupFileOrigin(
                    file_id=identity, backup_id=str(expected_id), source_path=relative, source_revision=file.revision,
                    name=name, created_on=day(db), published=False,
                ))

        failure = None
        with transaction(engine) as db:
            record = db.get(BackupFileOrigin, identity)
            if record is None:
                raise BackupError("stale", "找回确认已失效，请重新预览。")
            if not record.published:
                path = copy_path(folder, record)
                if path.exists() and matches_copy(path, record):
                    register_copy(db, folder, path, record)
                elif current_revision(folder, relative) != seen:
                    db.delete(record)
                    failure = BackupError("stale", "原位置已变化，请重新预览后再找回。")
                else:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        os.link(source, path)
                    except OSError:
                        db.delete(record)
                        failure = BackupError("copy_write", "副本写入失败，请检查知识库目录和剩余空间。")
                    else:
                        register_copy(db, folder, path, record)
        if failure:
            raise failure
        return {"id": identity, "name": name}
