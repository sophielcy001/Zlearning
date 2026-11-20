from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
from stat import S_ISDIR
from tempfile import NamedTemporaryFile

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .db import ImportedFile, LibraryFileCopy, LibraryOcrPage, LibraryWebSnapshot, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path, identity_of
from .library_folders import directory_path, entry_stat, rename_without_replacing
from .library_trash import FileVersion, file_version


COPY_BUFFER = 128 * 1024


@dataclass(frozen=True)
class CopyIntent:
    id: str
    source_id: str
    target: str
    directory_key: str
    confirmation: str


def directory_identity(path: Path) -> str:
    info = entry_stat(path)
    if not S_ISDIR(info.st_mode):
        raise InvalidLocation("目标位置已不再是文件夹。")
    birth = getattr(info, "st_birthtime", "")
    return f"{info.st_dev}:{info.st_ino}:{birth}"


def staging_directory(workspace: Path) -> Path:
    path = workspace / ".library-copy-work"
    path.mkdir(mode=0o700, exist_ok=True)
    if path.resolve() != path or not S_ISDIR(entry_stat(path).st_mode):
        raise InvalidLocation("知识库复制的临时目录无法安全访问。")
    return path


def stage_copy(db, workspace: Path, source: Path, version: FileVersion, intent: CopyIntent, day) -> None:
    staging = None
    try:
        with source.open("rb") as original, NamedTemporaryFile(
            mode="wb", dir=staging_directory(workspace), prefix=f"{intent.id}-", suffix=".part", delete=False,
        ) as temporary:
            staging = Path(temporary.name)
            hasher = sha256()
            while data := original.read(COPY_BUFFER):
                hasher.update(data)
                temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        witness = identity_of(staging)
        if hasher.hexdigest() != version.revision or witness is None or not witness.key:
            raise LocationConflict("原件已变化或无法确认复制内容，请重新预览。")
    except (OSError, InvalidLocation, LocationConflict):
        if staging is not None:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    db.add(LibraryFileCopy(
        id=intent.id, source_id=intent.source_id, target=intent.target, directory_key=intent.directory_key,
        confirmation=intent.confirmation, stage=staging.name, file_key=witness.key, birth_time=witness.birth,
        revision=version.revision, created_on=day(db), state="staged",
    ))


def matches_staged(item: LibraryFileCopy, version: FileVersion | None) -> bool:
    return bool(version and version.identity.key == item.file_key and version.revision == item.revision
                and (item.birth_time is None or version.identity.birth == item.birth_time))


def fail_copy(item: LibraryFileCopy, stage: Path, message: str) -> dict:
    item.state, item.error = "failed", message
    try:
        if matches_staged(item, file_version(stage)):
            stage.unlink()
    except (OSError, InvalidLocation, LocationConflict):
        pass
    return {"id": item.source_id, "error": message}


def copy_metadata(db, item: LibraryFileCopy) -> None:
    webpage = db.get(LibraryWebSnapshot, item.source_id)
    if webpage is not None:
        db.add(LibraryWebSnapshot(file_id=item.id, requested_url=webpage.requested_url, url=webpage.url,
                                  fetched_at=webpage.fetched_at, encoding=webpage.encoding, revision=webpage.revision))
    for page in db.scalars(select(LibraryOcrPage).where(
        LibraryOcrPage.file_id == item.source_id, LibraryOcrPage.revision == item.revision,
    )):
        db.add(LibraryOcrPage(file_id=item.id, page=page.page, revision=page.revision,
                              text_revision=page.text_revision, body=page.body))
    db.execute(text("""INSERT INTO library_file_tags (file_id, key, name)
        SELECT :target, key, name FROM library_file_tags WHERE file_id=:source"""),
        {"target": item.id, "source": item.source_id})


def finish_copy(engine, workspace: Path, identity: str) -> dict:
    with transaction(engine) as db:
        item = db.get(LibraryFileCopy, identity)
        if item is None:
            raise LocationConflict("复制记录已变化，请重新预览。")
        if item.state == "failed":
            return {"id": item.source_id, "error": item.error}
        result = {"id": item.source_id, "path": item.target, "copyId": item.id}
        if item.state == "ready":
            return result

        work = staging_directory(workspace)
        stage = work / item.stage
        if stage.parent != work or stage.resolve() != stage:
            raise InvalidLocation("找不到可信的复制暂存文件。")
        try:
            target = checked_path(workspace, item.target)
            parent = directory_path(workspace, target.parent.relative_to(workspace).as_posix())
            if directory_identity(parent) != item.directory_key:
                return fail_copy(item, stage, "目标文件夹已变化，请重新预览。")
            tracker = LocationTracker(db, workspace)
            if tracker.owned_elsewhere(item.target, item.id):
                return fail_copy(item, stage, "目标位置已关联其他文件，请重新预览。")
            current = file_version(target)
        except (InvalidLocation, LocationConflict, FileNotFoundError):
            return fail_copy(item, stage, "目标位置已变化，请重新预览。")
        if current is None:
            if not matches_staged(item, file_version(stage)):
                return fail_copy(item, stage, "复制暂存文件不可用，请重新预览。")
            try:
                rename_without_replacing(stage, target)
            except OSError:
                # If publication happened just before interruption, keep the checkpoint for recovery.
                if matches_staged(item, file_version(target)):
                    raise
                return fail_copy(item, stage, "无法写入目标位置，请检查同名文件、同卷位置和目录权限。")
            current = file_version(target)
        if not matches_staged(item, current):
            return fail_copy(item, stage, "目标位置已有其他内容，请重新预览。")
        db.add(ImportedFile(id=item.id, name=target.name, created_on=item.created_on))
        db.flush()
        tracker.bind(item.id, target, current.identity, current.revision)
        copy_metadata(db, item)
        item.state = "ready"
        return result


def recover_library_copies(engine, workspace: Path) -> None:
    with transaction(engine, write=False) as db:
        pending = list(db.scalars(select(LibraryFileCopy.id).where(LibraryFileCopy.state == "staged")))
    for identity in pending:
        try:
            finish_copy(engine, workspace, identity)
        except (OSError, InvalidLocation, LocationConflict, SQLAlchemyError):
            # Keep the approved checkpoint for a subsequent retry if the volume is temporarily unavailable.
            continue
