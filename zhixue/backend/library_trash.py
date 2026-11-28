from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import file_digest
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from .db import LibraryLocation, LibraryOcrPage, LibraryOcrVersion, LibraryTrash, transaction
from .file_locations import FileIdentity, InvalidLocation, LocationConflict, LocationTracker, checked_path, identity_of
from .library import file_signature, wiki_target_key


PENDING = ("deleting", "restoring", "purging")
VISIBLE = ("trashed", *PENDING)


@dataclass(frozen=True)
class FileVersion:
    identity: FileIdentity
    revision: str
    signature: str


def file_version(path: Path) -> FileVersion | None:
    identity = identity_of(path)
    if identity is None:
        return None
    before = file_signature(path)
    with path.open("rb") as handle:
        revision = file_digest(handle, "sha256").hexdigest()
    if file_signature(path) != before or identity_of(path) != identity:
        raise LocationConflict("文件正在变化，请刷新后重试。")
    return FileVersion(identity, revision, before)


def archive_path(folder: Path, entry: LibraryTrash) -> Path:
    archive = folder.resolve() / "library" / ".trash" / str(UUID(entry.id)) / f"original.{entry.format}"
    try:
        safe = archive.resolve() == archive
    except RuntimeError:
        safe = False
    if not safe:
        raise InvalidLocation("回收站路径已被链接到其他位置。")
    return archive


def describe(folder: Path, entry: LibraryTrash) -> dict:
    try:
        archive = archive_path(folder, entry)
        available = identity_of(archive) is not None
    except (OSError, InvalidLocation, RuntimeError):
        available = False
    return {
        "id": entry.id, "fileId": entry.file_id, "name": entry.name, "originalPath": entry.original_path,
        "archivePath": f"library/.trash/{entry.id}/original.{entry.format}", "deletedAt": entry.deleted_at.isoformat() + "Z",
        "revision": entry.revision, "kind": entry.kind, "format": entry.format,
        "projectId": entry.project_id, "projectTopic": entry.project_topic, "state": entry.state,
        "error": entry.error, "available": available,
    }


def file_tombstone(db: Session, file_id: str) -> dict | None:
    location = db.get(LibraryLocation, file_id)
    entry = db.get(LibraryTrash, location.trash_id) if location and location.trash_id else None
    if entry is None:
        return None
    purged = entry.state == "purged"
    return {
        "id": file_id, "name": entry.name, "kind": entry.kind, "format": entry.format, "path": entry.original_path,
        "projectId": entry.project_id, "projectTopic": entry.project_topic, "revision": entry.revision,
        "status": "purged" if purged else "trashed", "trashId": entry.id, "pageCount": None, "unreadablePages": [],
        "message": "回收站原件已清理。学习记录与引用仍保留。" if purged else "文件已移入知识库回收站。",
    }


def matches(entry: LibraryTrash, version: FileVersion | None) -> bool:
    return bool(version and version.revision == entry.revision
                and (entry.file_key is None or entry.file_key == version.identity.key)
                and (entry.birth_time is None or entry.birth_time == version.identity.birth))


def same_archived_identity(entry: LibraryTrash, version: FileVersion | None) -> bool:
    return bool(version and entry.file_key and entry.file_key == version.identity.key
                and (entry.birth_time is None or entry.birth_time == version.identity.birth))


def link_without_replacing(source: Path, destination: Path) -> bool:
    try:
        os.link(source, destination)
    except FileExistsError:
        return False
    return True


def unlink_archive(path: Path) -> None:
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass  # An externally added entry, if any, stays in the directory.


def release_location(db: Session, entry: LibraryTrash) -> None:
    location = db.get(LibraryLocation, entry.file_id)
    location.trash_id = None


def complete_delete(db: Session, folder: Path, entry: LibraryTrash) -> None:
    source = checked_path(folder, entry.original_path)
    archive = archive_path(folder, entry)
    archived = file_version(archive)
    if archived is None:
        if not matches(entry, file_version(source)):
            release_location(db, entry)
            entry.state, entry.error = "cancelled", "原件已变化或不在原位置，请刷新后重新确认。"
            return
        try:
            archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(source, archive)
        except OSError:
            if identity_of(archive) is not None:
                raise
            release_location(db, entry)
            entry.state, entry.error = "cancelled", "移入未完成。请检查原位置、文件占用和目录权限。"
            return
        archived = file_version(archive)
    if not matches(entry, archived):
        # The file may have changed in the interval just before it was moved.
        if archived is not None and link_without_replacing(archive, source):
            unlink_archive(archive)
            release_location(db, entry)
            entry.state, entry.error = "cancelled", "检测到移动期间原件变化，已放回原位置。请重新确认。"
            return
        # Preserve what was moved when the original path has since been occupied.
        entry.error = "移动期间原件发生变化。已保留回收站中的文件，请检查后处理。"
        if archived is not None:
            entry.revision, entry.file_key, entry.birth_time = archived.revision, archived.identity.key, archived.identity.birth
    location = db.get(LibraryLocation, entry.file_id)
    location.path = archive.relative_to(folder).as_posix()
    entry.state = "trashed"


def fail_restore(entry: LibraryTrash, message: str) -> None:
    entry.state, entry.error, entry.restore_path = "trashed", message, None


def complete_restore(db: Session, folder: Path, entry: LibraryTrash) -> None:
    destination = checked_path(folder, entry.restore_path)
    archive = archive_path(folder, entry)
    tracker = LocationTracker(db, folder)
    if tracker.owned_elsewhere(entry.restore_path, entry.file_id):
        fail_restore(entry, "恢复位置已关联另一份文件，请选择其他路径。")
        return
    old = file_version(archive)
    current = file_version(destination)
    if old is None:
        # If the process exited after removing the archive link, the published entry is still identifiable.
        if not same_archived_identity(entry, current):
            fail_restore(entry, "找不到可确认的回收站原件。")
            return
    else:
        if current is None:
            if not matches(entry, old):
                fail_restore(entry, "回收站原件已变化，请检查后重新处理。")
                return
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                linked = link_without_replacing(archive, destination)
            except OSError:
                fail_restore(entry, "原件仍在回收站。请选择可写且与回收站同卷的位置。")
                return
            if not linked:
                fail_restore(entry, "恢复位置已出现其他文件，请选择其他路径。")
                return
        # Required both for normal publication and recovery after publishing, before unlink.
        if not destination.is_file() or not archive.samefile(destination):
            fail_restore(entry, "恢复位置已有文件，请选择其他路径。")
            return
        current = file_version(destination)
        if current is None:
            fail_restore(entry, "恢复位置已变化。原件仍在回收站。")
            return
        unlink_archive(archive)
    tracker.bind(entry.file_id, destination, current.identity, current.revision)
    release_location(db, entry)
    if entry.restore_path != entry.original_path:
        # Legacy trash entries still record the exact original path, even without a saved title alias.
        paths = (entry.original_path, entry.original_path.removeprefix("library/"))
        db.execute(text("INSERT OR IGNORE INTO library_wiki_aliases (file_id, key) VALUES (:id, :key)"),
                   [{"id": entry.file_id, "key": wiki_target_key(path)} for path in paths])
    entry.state, entry.error = "restored", None


def complete_purge(folder: Path, entry: LibraryTrash) -> None:
    archive = archive_path(folder, entry)
    captured = archive.with_name(f".clearing.{entry.format}")
    version = file_version(captured)
    if version is None:
        original = file_version(archive)
        if original is not None:
            if not matches(entry, original):
                entry.state, entry.error = "trashed", "回收站原件已发生变化，清理已停止。"
                return
            try:
                os.rename(archive, captured)
            except OSError:
                if identity_of(captured) is not None:
                    raise
                entry.state, entry.error = "trashed", "原件仍在回收站。请检查文件占用和目录权限。"
                return
            version = file_version(captured)
    if version is not None:
        if not matches(entry, version):
            if link_without_replacing(captured, archive):
                unlink_archive(captured)
                entry.state, entry.error = "trashed", "清理期间原件已发生变化，已放回回收站。"
            else:
                entry.error = "原件在清理期间发生变化，两个文件均已保留。请检查回收站目录。"
            return
        unlink_archive(captured)
    if file_version(archive) is not None:
        entry.state, entry.error = "trashed", "清理期间原位置出现了新文件，已保留该文件。"
    else:
        entry.state = "purged"


def finish_operation(db: Session, folder: Path, entry: LibraryTrash) -> None:
    if entry.state not in PENDING:
        return
    entry.error = None
    try:
        if entry.state == "deleting":
            complete_delete(db, folder, entry)
        elif entry.state == "restoring":
            complete_restore(db, folder, entry)
        else:
            complete_purge(folder, entry)
            if entry.state == "purged":
                db.execute(delete(LibraryOcrPage).where(LibraryOcrPage.file_id == entry.file_id))
                db.execute(delete(LibraryOcrVersion).where(LibraryOcrVersion.file_id == entry.file_id))
    except (OSError, InvalidLocation, LocationConflict) as error:
        entry.error = str(error) if isinstance(error, (InvalidLocation, LocationConflict)) else "文件暂时无法移动或清理。请检查文件占用和目录权限。"


def recover_library_trash(engine, folder: Path) -> None:
    with transaction(engine) as db:
        for entry in db.scalars(select(LibraryTrash).where(LibraryTrash.state.in_(PENDING))).all():
            finish_operation(db, folder, entry)
