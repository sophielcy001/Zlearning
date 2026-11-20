from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from errno import EDQUOT, ENOSPC
from pathlib import Path
import sqlite3
from time import monotonic
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .backup_catalog import BackupCatalog
from .backup_copies import recover_copies
from .backup_format import BackupError
from .backup_jobs import save_archive
from .backup_retention import archive_proof, prune_archives
from .backup_work import backup_workspace
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .project_activity import study_moment


CHECK_INTERVAL = 5
RETRY_AFTER = timedelta(minutes=5)
FILESYSTEM_FAILURES = (OSError, sqlite3.Error, SQLAlchemyError)
BACKUP_FAILURES = (BackupError, *FILESYSTEM_FAILURES)


class BackupPasswordStore(NativeCredentialStore):
    scope = "backup"
    username = "backup-password"


@dataclass(frozen=True)
class Schedule:
    credential_ref: str | None = None
    last_at: str | None = None
    state: str | None = None
    issue: str | None = None
    processed_day: str | None = None

    def due(self, day: date, at: datetime) -> bool:
        if not self.credential_ref or self.processed_day == day.isoformat():
            return False
        if self.last_at and self.issue:
            last = datetime.fromisoformat(self.last_at)
            if study_moment(last).date() == day and timedelta(0) <= at - last < RETRY_AFTER:
                return False
        return True


def backup_failure(error: Exception) -> str:
    if isinstance(error, BackupError):
        return str(error)
    origin = getattr(error, "orig", error)
    if (getattr(origin, "errno", None) in (ENOSPC, EDQUOT)
            or getattr(origin, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL):
        return "保存空间不足。请检查目标磁盘和本机剩余空间。"
    return "自动备份读写失败。请检查备份文件夹和权限。"


class AutomaticBackups:
    def __init__(self, catalog: BackupCatalog, store: CredentialStore | None = None):
        self.catalog = catalog
        self.store = store if store is not None else BackupPasswordStore()
        self.next_cleanup = 0.0

    def configuration(self) -> tuple[Schedule, list[str]]:
        with closing(self.catalog.connect()) as db:
            row = db.execute("SELECT credential_ref, last_at, state, issue, processed_day FROM backup_schedule WHERE id=1").fetchone()
            pending = [entry[0] for entry in db.execute("SELECT reference FROM backup_credential_cleanup")]
        return Schedule(*row) if row else Schedule(), pending

    def view(self) -> dict:
        state, pending = self.configuration()
        return {"enabled": state.credential_ref is not None, "lastAt": state.last_at,
                "lastState": state.state, "error": state.issue, "credentialCleanupPending": bool(pending)}

    def clean_credentials(self, pending: list[str]) -> None:
        self.next_cleanup = monotonic() + RETRY_AFTER.total_seconds()
        for reference in pending:
            try:
                self.store.discard(reference)
            except CREDENTIAL_FAILURES:
                continue
            with closing(self.catalog.connect()) as db, db:
                db.execute("DELETE FROM backup_credential_cleanup WHERE reference=?", (reference,))

    def change(self, password: str | None) -> dict:
        with backup_workspace(self.catalog.folder, allocate=False):
            previous, pending = self.configuration()
            self.clean_credentials(pending)
            reference = None
            if password is not None:
                reference = str(uuid4())
                with closing(self.catalog.connect()) as db, db:
                    db.execute("INSERT INTO backup_credential_cleanup VALUES (?)", (reference,))
                try:
                    self.store.put(reference, password)
                    if self.store.get(reference) != password:
                        raise OSError("System did not return the saved credentials")
                except CREDENTIAL_FAILURES:
                    self.clean_credentials([reference])
                    raise BackupError("credential_store", "无法把备份密码保存到系统凭据库。请解锁后重试。") from None
            with closing(self.catalog.connect()) as db, db:
                db.execute("""
                    INSERT INTO backup_schedule (id, credential_ref) VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET credential_ref=excluded.credential_ref,
                    last_at=NULL, state=NULL, issue=NULL
                    """, (reference,))
                if reference:
                    db.execute("DELETE FROM backup_credential_cleanup WHERE reference=?", (reference,))
                if previous.credential_ref:
                    db.execute("INSERT OR IGNORE INTO backup_credential_cleanup VALUES (?)", (previous.credential_ref,))
            if previous.credential_ref:
                self.clean_credentials([previous.credential_ref])
            return self.view()

    def report(self, at: datetime, state: str, issue: str | None, processed: str | None = None) -> None:
        with closing(self.catalog.connect()) as db, db:
            db.execute(
                "UPDATE backup_schedule SET last_at=?, state=?, issue=?, processed_day=COALESCE(?,processed_day) WHERE id=1",
                (at.isoformat(), state, issue, processed),
            )

    def destination(self) -> Path:
        directory = self.catalog.directory()
        if directory.resolve() != directory:
            raise BackupError("directory", "备份文件夹的位置已变化，请重新选择。")
        if directory == self.catalog.default_directory:
            directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise BackupError("directory", "备份文件夹当前不可用，请重新连接磁盘或网络位置。")
        return directory

    def save_due(self, engine: Engine, work: Path, at: datetime, day: date, reference: str) -> None:
        self.report(at, "saving", None)
        dated = day.isoformat()
        try:
            self.catalog.recover_interrupted()
            recover_copies(engine, self.catalog.folder)
            directory = self.destination()
            try:
                password = self.store.get(reference)
            except CREDENTIAL_FAILURES:
                password = None
            if not password:
                raise BackupError("credential_store", "无法读取本机备份密码，请解锁系统凭据库或重新设置密码。")
            completed = any(
                item.scheduled_day == dated and item.path.parent == directory and item.status == "ready"
                and archive_proof(self.catalog, item, password) is not None
                for item in self.catalog.records()
            )
            if not completed:
                save_archive(engine, self.catalog, work, password, at, directory=directory, scheduled_day=dated)
        except BACKUP_FAILURES as error:
            self.report(at, "failed", backup_failure(error))
            return
        try:
            prune_archives(self.catalog, directory, day, password)
        except BACKUP_FAILURES as error:
            self.report(at, "ready", f"已保存今日备份。清理过期归档暂缓。{backup_failure(error)}")
            return
        self.report(at, "ready", None, dated)

    def check(self, engine: Engine, now: datetime | None = None) -> None:
        if not self.catalog.database.is_file():
            return
        at = now if now is not None else datetime.now(UTC)
        day = study_moment(at).date()
        try:
            settings, pending = self.configuration()
            cleanup = bool(pending) and monotonic() >= self.next_cleanup
            if not cleanup and not settings.due(day, at):
                return
            with backup_workspace(self.catalog.folder) as work:
                settings, pending = self.configuration()
                if cleanup:
                    self.clean_credentials(pending)
                if settings.credential_ref and settings.due(day, at):
                    self.save_due(engine, work, at, day, settings.credential_ref)
        except BACKUP_FAILURES as error:
            if isinstance(error, BackupError) and error.code == "busy":
                return
            try:
                self.report(at, "failed", backup_failure(error))
            except FILESYSTEM_FAILURES:
                pass
