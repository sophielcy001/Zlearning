from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from errno import EDQUOT, ENOSPC
from hmac import compare_digest, digest
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from stat import S_ISDIR, S_ISREG
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.orm import Session

from .backup_catalog import BackupCatalog, CatalogItem
from .backup_format import EXTENSION, BackupError, BackupHeader, damaged
from .backup_snapshot import (
    DATABASE, VerifiedBackup, create_backup, current_view, plain, read_backup, scan_library, verified_database,
)
from .backup_work import backup_workspace
from .db import ROOT, LibraryTrash, MobileWorkspace, open_db, transaction, upgrade
from .external_memory import retain_external_memory
from .file_locations import identity_of
from .library_trash import archive_path
from .model_ledger import retain_charges
from .restored_tasks import hold_restored_tasks
from .storage_files import sync_directory


RESTORE_FOLDER = ".restore-work"
OPERATION = re.compile(r"switch-[0-9a-f]{32}")
ITEMS = (DATABASE, "library")
JOURNAL = "journal.json"
RECEIPT = "last-restore.json"


def restore_root(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    root = folder / RESTORE_FOLDER
    root.mkdir(mode=0o700, exist_ok=True)
    mode = root.lstat()
    if not S_ISDIR(mode.st_mode) or not plain(mode) or root.resolve() != root:
        raise BackupError("restore_directory", "整体恢复工作目录无法安全访问。")
    return root


@contextmanager
def exclusive_lease(path: Path, *, timeout: float = 0):
    if path.resolve() != path or (path.exists() and not S_ISREG(path.lstat().st_mode)):
        raise BackupError("restore_directory", "数据目录的锁文件无法安全访问。")
    with closing(sqlite3.connect(path, timeout=timeout)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise BackupError("busy", "同一数据目录正在被另一处智学服务使用。") from None
            raise
        yield


def move(source: Path, target: Path) -> None:
    source.replace(target)
    sync_directory(source.parent)
    if target.parent != source.parent:
        sync_directory(target.parent)


def write_json(path: Path, value: dict) -> None:
    handle = NamedTemporaryFile(dir=path.parent, prefix=".receipt-", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        move(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_receipt(root: Path) -> dict | None:
    path = root / RECEIPT
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("state") in {"complete", "failed"} else None
    except (ValueError, OSError):
        return None


def incomplete_recovery() -> BackupError:
    return BackupError("recovery_required", "数据切换尚未回退完成。请检查磁盘和权限后重新启动智学。")


def previous_restore_pending() -> BackupError:
    return BackupError("previous_restore_pending", "上一项恢复已提交，回执或临时目录尚未处理完。请检查磁盘空间和文件占用后重新预览。")


def settle_operation(
    folder: Path, job: Path, *, before_restore: bool = False, existing_receipt: dict | None = None,
) -> dict | None:
    journal = job / JOURNAL
    if not journal.exists():
        shutil.rmtree(job)
        return None
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        original, receipt = record["original"], record["receipt"]
        if (not isinstance(receipt, dict) or not isinstance(original, list) or DATABASE not in original
                or any(item not in ITEMS for item in original)):
            raise ValueError("invalid restore journal")
        phase = record["phase"]
        if phase not in {"committed", "switching"}:
            raise ValueError("unknown restore phase")
        pending_backup = receipt.get("rollbackBackupId")
        existing_backup = (existing_receipt or {}).get("rollbackBackupId")
        superseded = (
            isinstance(pending_backup, str) and isinstance(existing_backup, str) and pending_backup != existing_backup
            and BackupCatalog(folder).recorded_before(pending_backup, existing_backup)
        )
        if phase == "committed" or superseded:
            if any(not (folder / item).exists() for item in ITEMS):
                raise ValueError("missing committed data")
        elif not before_restore:
            for item in ITEMS:
                live, retained = folder / item, job / "retained" / item
                if retained.exists():
                    if live.exists():
                        move(live, job / "discarded" / item)
                    move(retained, live)
                elif item in original:
                    if not live.exists():
                        raise ValueError("missing original data")
                elif live.exists() and not (job / "incoming" / item).exists():
                    move(live, job / "discarded" / item)
            receipt = {**receipt, "state": "failed", "code": "interrupted", "detail": "恢复中断，已保留原先的学习数据。"}
        else:
            raise ValueError("unfinished restore phase")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        raise incomplete_recovery() from error
    if not superseded:
        destination = job.parent / RECEIPT
        try:
            try:
                write_json(destination, receipt)
            except OSError as error:
                if error.errno not in {ENOSPC, EDQUOT}:
                    raise
                # The live pair is already settled. Release the unused copy while retaining the journal for a retry.
                unused = "retained" if phase == "committed" else "discarded"
                shutil.rmtree(job / unused, ignore_errors=True)
                write_json(destination, receipt)
        except OSError as error:
            raise (previous_restore_pending() if before_restore else incomplete_recovery()) from error
    shutil.rmtree(job, ignore_errors=True)
    if before_restore and job.exists():
        raise previous_restore_pending()
    return existing_receipt if superseded else receipt


def settle_pending_operations(folder: Path, root: Path, *, before_restore: bool = False) -> None:
    for job in sorted(root.iterdir()):
        if OPERATION.fullmatch(job.name):
            if job.resolve() != job or not S_ISDIR(job.lstat().st_mode) or not plain(job.lstat()):
                raise incomplete_recovery()
            settle_operation(folder, job, before_restore=before_restore, existing_receipt=read_receipt(root))


def recover_full_restore(folder: Path) -> dict | None:
    root = restore_root(folder)
    settle_pending_operations(folder, root)
    return read_receipt(root)


def switch_workspace(folder: Path, job: Path, receipt: dict,
                     expected_library: tuple[list[str], dict[str, tuple]]) -> None:
    (job / "retained").mkdir(mode=0o700)
    (job / "discarded").mkdir(mode=0o700)
    original = [name for name in ITEMS if (folder / name).exists()]
    if DATABASE not in original or any(not (job / "incoming" / name).exists() for name in ITEMS):
        raise BackupError("restore_write", "当前数据或恢复暂存不完整。")
    record = {"phase": "switching", "original": original, "receipt": receipt}
    journal = job / JOURNAL
    write_json(journal, record)
    sync_directory(job.parent)
    sync_directory(folder)
    try:
        for name in original:
            move(folder / name, job / "retained" / name)
        for name in ITEMS:
            move(job / "incoming" / name, folder / name)
        if scan_library(job / "retained") != expected_library:
            raise BackupError("stale", "切换期间知识库文件发生变化，已保留当前数据。请重新查看差异。")
        write_json(journal, {**record, "phase": "committed"})
    except (OSError, BackupError):
        resolved = settle_operation(folder, job)
        if resolved and resolved["state"] == "complete":
            return
        raise
    # A committed marker is sufficient to finish cleanup on the next start if storage is temporarily unavailable.
    try:
        settle_operation(folder, job)
    except BackupError:
        pass


def restore_confirmation(secret: bytes, backup: VerifiedBackup, fingerprint: str) -> str:
    fields = ["whole-backup", str(backup.header.identity), backup.revision, fingerprint]
    content = json.dumps(fields, separators=(",", ":")).encode("utf-8")
    return digest(secret, content, "sha256").hex()


def schema_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
    return config


def seal_database(database: Path) -> None:
    with closing(sqlite3.connect(database, isolation_level=None, timeout=5)) as connection:
        if connection.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
            raise BackupError("busy", "SQLite 暂时无法保留所需的 WAL 模式。")
        if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise BackupError("busy", "SQLite 仍有未结束的写入，请重新预览后再恢复。")
    if any(database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise BackupError("busy", "SQLite 日志仍被占用，请结束其他数据操作后再恢复。")
    with database.open("r+b") as handle:
        os.fsync(handle.fileno())


def rebind_trash_originals(db: Session, incoming: Path, backup: VerifiedBackup) -> None:
    revisions = {file.path: file.revision for file in backup.manifest.files if file.path.startswith("library/.trash/")}
    if not revisions:
        return
    folder = incoming.resolve()
    for entry in db.scalars(select(LibraryTrash).where(LibraryTrash.state == "trashed")):
        path = archive_path(folder, entry)
        # read_backup verified the extracted bytes against this manifest before the workspace can switch.
        if revisions.get(path.relative_to(folder).as_posix()) != entry.revision:
            continue
        identity = identity_of(path)
        if identity is None:
            raise BackupError("restore_write", "已校验的回收站原件在恢复暂存目录中缺失。")
        entry.file_key, entry.birth_time = identity.key, identity.birth


def prepare_restored_database(incoming: Path, backup: VerifiedBackup, current_engine) -> tuple[int, int]:
    database = incoming / DATABASE
    manifest = backup.manifest
    counts, _ = verified_database(database, manifest.database_schema)
    if counts != manifest.counts:
        raise damaged()
    scripts = ScriptDirectory.from_config(schema_config())
    if manifest.database_schema not in {item.revision for item in scripts.walk_revisions()}:
        raise BackupError("unsupported_schema", "备份数据库版本超出本版智学支持范围。")
    stage = open_db(incoming)
    try:
        upgrade(stage)
        with transaction(current_engine, write=False) as current, transaction(stage) as db:
            retain_charges(current, db)
            retain_external_memory(current, db, datetime.now(UTC).replace(tzinfo=None))
            db.get(MobileWorkspace, 1).identity = str(uuid4())
            rebind_trash_originals(db, incoming, backup)
        held = hold_restored_tasks(stage, backup.header.created_at)
    finally:
        stage.dispose()
    verified_database(database, scripts.get_current_head())
    seal_database(database)
    for directory in sorted(manifest.directories, key=lambda name: name.count("/"), reverse=True):
        sync_directory(incoming / directory)
    sync_directory(incoming)
    return held


def save_rollback(engine, folder: Path, catalog: BackupCatalog, password: str, work: Path) -> CatalogItem:
    identity = uuid4()
    created = datetime.now(UTC).replace(microsecond=0)
    path = folder / "backups" / "pre-restore" / f"zhixue-pre-restore-{created:%Y%m%d-%H%M%S}-{identity.hex}{EXTENSION}"
    item = CatalogItem(str(identity), path, created.isoformat(), "writing", None)
    catalog.record(item)
    def verified(amount: int, checksum: str) -> None:
        catalog.record(replace(item, size=amount, checksum=checksum))

    size = create_backup(engine, folder, path, BackupHeader.create(identity, created), password, work,
                         before_publish=verified)
    sync_directory(path.parent)
    sync_directory(path.parent.parent)
    result = replace(item, size=size, status="ready")
    catalog.record(result)
    return catalog.pin(identity, True)


def restore_full(engine, folder: Path, item: CatalogItem, expected: UUID, password: str, confirmation: str,
                 secret: bytes, operation: str, progress: Callable[[str], None]) -> dict:
    with exclusive_lease(folder / ".worker-lease.sqlite3", timeout=5), backup_workspace(folder) as work:
        root = restore_root(folder)
        settle_pending_operations(folder, root, before_restore=True)
        catalog = BackupCatalog(folder)
        catalog.recover_interrupted()
        job = root / f"switch-{operation}"
        job.mkdir(mode=0o700)
        incoming = job / "incoming"
        incoming.mkdir(mode=0o700)
        try:
            progress("verifying")
            backup = read_backup(item.path, password, destination=incoming)
            _, _, _, fingerprint, timer = current_view(engine, folder)
            if timer:
                raise BackupError("timer_active", "请先结束或暂存当前计时，再重新预览恢复。")
            if backup.header.identity != expected or not compare_digest(confirmation, restore_confirmation(secret, backup, fingerprint)):
                raise BackupError("stale", "备份或当前学习数据已变化，请重新查看差异。")
            held_tasks, held_timers = prepare_restored_database(incoming, backup, engine)
            progress("saving_current")
            rollback = save_rollback(engine, folder, catalog, password, work)
            library = scan_library(folder)
            if current_view(engine, folder)[3] != fingerprint:
                raise BackupError("stale", "保存快照期间学习数据已变化，请重新查看差异。")
            progress("installing")
            engine.dispose()
            seal_database(folder / DATABASE)
            receipt = {"id": operation, "backupId": str(expected), "state": "complete", "heldTasks": held_tasks,
                       "heldTimers": held_timers, "rollbackBackupId": rollback.id}
            switch_workspace(folder, job, receipt, library)
            return receipt
        finally:
            if job.exists() and not (job / JOURNAL).exists():
                shutil.rmtree(job, ignore_errors=True)
