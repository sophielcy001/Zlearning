from datetime import UTC, datetime
from difflib import unified_diff
from hashlib import sha256
from itertools import islice
from pathlib import Path
import os
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Lesson, LibraryLocation, NoteConflict, NoteLesson, NoteVersion, NoteWrite, Project
from .file_locations import LocationTracker, checked_path, identity_of
from .library import FileSnapshot, FileTooLarge, file_signature, local_path, read_snapshot
from .project_activity import touch_project


MAX_NOTE_CHARS = 50_000


class NoteFileError(ValueError):
    pass


def plain_path(path: Path) -> Path:
    try:
        resolved = path.resolve()
    except RuntimeError as error:
        raise NoteFileError("笔记路径包含无法解析的符号链接。") from error
    if resolved != path:
        raise NoteFileError("笔记路径通过符号链接指向了其他位置。请检查本地文件夹。")
    return path


def note_path(folder: Path, note_id: str) -> Path:
    return plain_path(local_path(folder.resolve(), "notes", note_id))


def note_snapshot(path: Path) -> FileSnapshot | None:
    try:
        snapshot = read_snapshot(path)
    except FileNotFoundError:
        return None
    except FileTooLarge as error:
        raise NoteFileError("笔记超过可编辑大小。可从知识库检查原件。") from error
    try:
        if len(snapshot.content.decode("utf-8")) > MAX_NOTE_CHARS:
            raise NoteFileError("笔记超过可编辑大小。可从知识库检查原件。")
    except UnicodeDecodeError as error:
        raise NoteFileError("笔记需要使用 UTF-8 编码。请在外部编辑器检查原件。") from error
    return snapshot


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def remember(db: Session, note_id: str, content: bytes, source: str, *, observed=False) -> str:
    revision = sha256(content).hexdigest()
    version = db.get(NoteVersion, (note_id, revision))
    if version is None:
        db.add(NoteVersion(note_id=note_id, revision=revision, content=content, created_at=now_utc(),
                           source=source, seen_on_disk=observed))
    elif observed:
        version.seen_on_disk = True
    return revision


def observe(db: Session, note_id: str, snapshot: FileSnapshot | None) -> str | None:
    return remember(db, note_id, snapshot.content, "disk", observed=True) if snapshot is not None else None


def version_data(db: Session, note_id: str, revision: str | None) -> dict | None:
    version = db.get(NoteVersion, (note_id, revision)) if revision is not None else None
    if version is None:
        return None
    return {"revision": version.revision, "content": version.content.decode("utf-8"),
            "createdAt": version.created_at.replace(tzinfo=UTC).isoformat(), "source": version.source}


def conflict_data(conflict: NoteConflict) -> dict:
    return {"id": conflict.id, "kind": conflict.kind, "createdAt": conflict.created_at.replace(tzinfo=UTC).isoformat(),
            "resolution": conflict.resolution}


def diff_lines(before: dict | None, after: dict) -> list[str]:
    old = before["content"].splitlines(keepends=True) if before else []
    new = after["content"].splitlines(keepends=True)
    return [line.rstrip("\r\n") for line in islice(unified_diff(old, new), 2, None)]


def remember_conflict(db: Session, note_id: str, kind: str, base: str | None, external: str | None, draft: str) -> str:
    existing = db.scalar(select(NoteConflict.id).where(
        NoteConflict.note_id == note_id, NoteConflict.resolution.is_(None), NoteConflict.kind == kind,
        NoteConflict.base_revision == base, NoteConflict.external_revision == external, NoteConflict.draft_revision == draft,
    ))
    if existing:
        return existing
    identity = str(uuid4())
    db.add(NoteConflict(id=identity, note_id=note_id, kind=kind, created_at=now_utc(), base_revision=base,
                        external_revision=external, draft_revision=draft))
    return identity


def stage_write(db: Session, note_id: str, target_path: str, expected: str | None, desired: str, conflict: NoteConflict | None = None,
                resolution: str | None = None) -> str:
    identity = str(uuid4())
    db.add(NoteWrite(id=identity, note_id=note_id, expected_revision=expected, desired_revision=desired,
                     resolution_id=conflict.id if conflict else None, resolution=resolution, completed=False,
                     target_path=target_path))
    return identity


def write_paths(folder: Path, write_id: str) -> tuple[Path, Path]:
    journal = plain_path(folder.resolve() / "library" / ".note-writes")
    return plain_path(journal / f"{write_id}.new"), plain_path(journal / f"{write_id}.previous")


def track_capture(db: Session, write: NoteWrite, path: Path) -> str | None:
    snapshot = note_snapshot(path)
    revision = observe(db, write.note_id, snapshot)
    write.captured_signature = snapshot.signature if snapshot else None
    write.captured_revision = revision
    return revision


def reject_write(db: Session, write: NoteWrite, reason: str, latest: str | None) -> None:
    original = db.get(NoteConflict, write.resolution_id) if write.resolution_id else None
    if original and original.resolution is None:
        # Retain the previously recorded branches in history, and keep the newest attempted merge visible.
        original.draft_revision = write.desired_revision
        if reason == "interrupted":
            original.kind = reason
        if latest is not None:
            original.external_revision = latest
        write.result_conflict_id = original.id
    else:
        write.result_conflict_id = remember_conflict(
            db, write.note_id, reason, write.expected_revision, latest, write.desired_revision,
        )
    write.completed = True


def accept_write(db: Session, write: NoteWrite, folder: Path, target: Path, moment) -> None:
    db.get(NoteVersion, (write.note_id, write.desired_revision)).seen_on_disk = True
    if (identity := identity_of(target)) is not None:
        LocationTracker(db, folder).bind(f"note-{write.note_id}", target, identity, write.desired_revision)
    if write.resolution_id:
        conflict = db.get(NoteConflict, write.resolution_id)
        if conflict.resolution is None:
            conflict.resolution = write.resolution
    projects = select(Project).where(Project.id.in_(
        select(Lesson.project_id).join(NoteLesson, NoteLesson.lesson_id == Lesson.id)
        .where(NoteLesson.note_id == write.note_id)
    ))
    at = moment(db)
    for project in db.scalars(projects):
        touch_project(project, at)
    write.completed = True


def link_if_absent(source: Path, target: Path) -> bool:
    try:
        os.link(source, target)
    except FileExistsError:
        return False
    return True


def create_stage(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def finish_write(db: Session, folder: Path, write: NoteWrite, moment) -> None:
    if write.completed:
        return
    file_id = f"note-{write.note_id}"
    if write.target_path:
        registered = db.get(LibraryLocation, file_id)
        if registered and (registered.path != write.target_path or registered.trash_id is not None):
            reject_write(db, write, "changed", None)
            return
        target = checked_path(folder, write.target_path)
    else:
        target = note_path(folder, write.note_id)
    staged, previous = write_paths(folder, write.id)
    interrupted = staged.exists() or previous.exists()
    if not interrupted:
        tracker = LocationTracker(db, folder)
        location = tracker.resolve(file_id, local_path(folder.resolve(), "notes", write.note_id))
        if location.path != target or location.status not in {"ready", "missing"}:
            reject_write(db, write, "changed", None)
            return
    expected_identity = identity_of(target) if not interrupted else None
    latest = observe(db, write.note_id, note_snapshot(target))

    if interrupted:
        # The previous process may have published, or external software may have changed the path since.
        # Only acknowledge an already visible result. An ambiguous or missing path goes to the review UI.
        captured = track_capture(db, write, previous)
        if latest == write.desired_revision:
            if captured and captured not in (write.expected_revision, latest):
                remember_conflict(db, write.note_id, "detached", write.expected_revision, captured, latest)
            accept_write(db, write, folder, target, moment)
        else:
            reject_write(db, write, "interrupted", latest or captured)
        return
    if latest != write.expected_revision:
        reject_write(db, write, "changed", latest)
        return

    desired = db.get(NoteVersion, (write.note_id, write.desired_revision))
    target.parent.mkdir(parents=True, exist_ok=True)
    create_stage(staged, desired.content)
    if latest is not None:
        # Check that both locations support hard links before displacing the live file.
        probe = staged.with_suffix(".check")
        os.link(staged, probe)
        probe.unlink()
        try:
            os.rename(target, previous)
        except FileNotFoundError:
            reject_write(db, write, "changed", observe(db, write.note_id, note_snapshot(target)))
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) != 32:
                raise
            # The editor allowed reading but prevented renaming. Keep the draft available for review.
            reject_write(db, write, "interrupted", observe(db, write.note_id, note_snapshot(target)))
            return
        try:
            displaced = track_capture(db, write, previous)
        except (OSError, NoteFileError):
            link_if_absent(previous, target)
            raise
        if displaced != write.expected_revision or identity_of(previous) != expected_identity:
            link_if_absent(previous, target)
            reject_write(db, write, "changed", observe(db, write.note_id, note_snapshot(target)) or displaced)
            return

    # An external editor may create a replacement after the displacement. Never replace that entry.
    if not link_if_absent(staged, target):
        reject_write(db, write, "changed", observe(db, write.note_id, note_snapshot(target)))
        return
    accept_write(db, write, folder, target, moment)


def recover_writes(db: Session, folder: Path, note_id: str, moment) -> list[str]:
    pending = db.scalars(select(NoteWrite).where(NoteWrite.note_id == note_id, NoteWrite.completed.is_(False))).all()
    for write in pending:
        finish_write(db, folder, write, moment)
    return [write.id for write in pending]


def check_detached_files(db: Session, folder: Path, note_id: str, current: str | None) -> None:
    writes = db.scalars(select(NoteWrite).where(NoteWrite.note_id == note_id, NoteWrite.captured_signature.is_not(None)))
    for write in writes:
        _, previous = write_paths(folder, write.id)
        try:
            if file_signature(previous) == write.captured_signature:
                continue
        except FileNotFoundError:
            continue
        before = write.captured_revision
        after = track_capture(db, write, previous)
        if after and after not in (before, current):
            remember_conflict(db, note_id, "detached", before, after, current or write.desired_revision)


def discard_stage(folder: Path, write_id: str) -> None:
    # The write record is already committed. A failed cleanup can leave a redundant hard link.
    try:
        staged, _ = write_paths(folder, write_id)
        staged.unlink(missing_ok=True)
        staged.with_suffix(".check").unlink(missing_ok=True)
    except (OSError, NoteFileError):
        pass
