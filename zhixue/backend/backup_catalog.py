from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
from stat import S_ISREG
from uuid import UUID

from .backup_format import EXTENSION, BackupError, BackupHeader, incomplete_file
from .backup_snapshot import plain, stamp, transfer_file
from .backup_work import WORK_FOLDER
from .storage_files import sqlite_file_uri


@dataclass(frozen=True)
class CatalogItem:
    id: str
    path: Path
    created_at: str
    status: str
    size: int | None
    error: str | None = None
    pinned: bool = False
    checksum: str | None = None
    scheduled_day: str | None = None

    def view(self) -> dict:
        return {
            "id": self.id, "name": self.path.name, "directory": str(self.path.parent),
            "createdAt": self.created_at, "status": self.status, "size": self.size,
            "error": self.error, "pinned": self.pinned, "automatic": self.scheduled_day is not None,
        }


class BackupCatalog:
    def __init__(self, folder: Path):
        self.folder = folder
        self.default_directory = folder / "backups" / "archives"
        self.database = folder / "backup-catalog.sqlite3"

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), directory TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    size INTEGER,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS pins (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS archive_metadata (id TEXT PRIMARY KEY, checksum TEXT, scheduled_day TEXT);
                CREATE TABLE IF NOT EXISTS backup_schedule (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    credential_ref TEXT,
                    last_at TEXT,
                    state TEXT,
                    issue TEXT,
                    processed_day TEXT
                );
                CREATE TABLE IF NOT EXISTS backup_credential_cleanup (reference TEXT PRIMARY KEY);
                """
            )
        except BaseException:
            connection.close()
            raise
        return connection

    def directory(self) -> Path:
        with closing(self.connect()) as db:
            row = db.execute("SELECT directory FROM settings WHERE id=1").fetchone()
            return Path(row["directory"]) if row else self.default_directory

    def change_directory(self, directory: str | None) -> Path:
        requested = Path(directory).expanduser() if directory else self.default_directory
        if not requested.is_absolute():
            raise BackupError("directory", "请输入本机或网络备份文件夹的绝对路径。")
        destination = requested.resolve()
        library = self.folder / "library"
        if any(destination.is_relative_to(reserved) for reserved in (library, library.resolve(), self.folder / WORK_FOLDER, self.folder / ".restore-work")):
            raise BackupError("directory", "请选择知识库和应用工作目录之外的备份位置。")
        destination.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO settings VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET directory=excluded.directory",
                       (str(destination),))
            db.execute("UPDATE backup_schedule SET processed_day=NULL, last_at=NULL, state=NULL, issue=NULL")
        return destination

    def record(self, item: CatalogItem) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, size=excluded.size, error=excluded.error",
                (item.id, str(item.path), item.created_at, item.status, item.size, item.error),
            )

            if item.checksum is not None or item.scheduled_day is not None:
                db.execute(
                    "INSERT INTO archive_metadata VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "checksum=excluded.checksum, scheduled_day=excluded.scheduled_day",
                    (item.id, item.checksum, item.scheduled_day),
                )

    def remember_verified(self, identity: str, checksum: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO archive_metadata (id, checksum) VALUES (?, ?) ON CONFLICT(id) DO UPDATE "
                "SET checksum=excluded.checksum WHERE archive_metadata.checksum IS NULL",
                (identity, checksum),
            )

    def records(self) -> list[CatalogItem]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT r.*, m.checksum, m.scheduled_day, p.id IS NOT NULL AS pinned "
                "FROM records r LEFT JOIN archive_metadata m ON m.id=r.id LEFT JOIN pins p ON p.id=r.id"
            )
            return [CatalogItem(
                row["id"], Path(row["path"]), row["created_at"], row["status"], row["size"],
                row["error"], bool(row["pinned"]), row["checksum"], row["scheduled_day"],
            ) for row in rows]

    def recorded_before(self, first: str, second: str) -> bool:
        if not self.database.is_file():
            return False
        # Full restores insert their rollback rows in switch order under the backup lease.
        address = sqlite_file_uri(self.database.absolute(), mode="ro")
        with closing(sqlite3.connect(address, uri=True, timeout=5)) as db:
            positions = dict(db.execute("SELECT id, rowid FROM records WHERE id IN (?, ?)", (first, second)))
        return first in positions and second in positions and positions[first] < positions[second]

    def forget(self, item: CatalogItem) -> None:
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM records WHERE id=?", (item.id,))
            db.execute("DELETE FROM archive_metadata WHERE id=?", (item.id,))

    def recover_interrupted(self) -> None:
        for item in self.records():
            if item.status == "expiring":
                if item.path.parent.resolve() == item.path.parent and item.path.parent.is_dir():
                    try:
                        item.path.lstat()
                    except FileNotFoundError:
                        self.forget(item)
                    else:
                        self.record(replace(item, status="ready"))
            if item.status != "writing":
                continue
            path = item.path
            if path.parent.resolve() != path.parent:
                continue
            failed = replace(item, status="failed", size=None, error="上次创建备份时应用中断。")
            if not path.parent.is_dir():
                if item.size is None:
                    self.record(failed)
                continue
            observed = self.read_item(path)
            try:
                incomplete_file(path).unlink(missing_ok=True)
                published = observed is not None and observed.id == item.id and observed.size == item.size
                if published and item.checksum is not None:
                    published = transfer_file(path, stamp(path.lstat())) == item.checksum
            except (OSError, BackupError):
                continue
            self.record(replace(item, status="ready") if published else failed)

    def items(self) -> tuple[Path, list[CatalogItem], str | None]:
        directory = self.directory()
        recorded = self.records()
        with closing(self.connect()) as db:
            pinned = {row["id"] for row in db.execute("SELECT id FROM pins")}
            checksums = dict(db.execute("SELECT id, checksum FROM archive_metadata WHERE checksum IS NOT NULL"))

        issue = None
        try:
            observed = self.discover(directory, {item.id: item for item in recorded if item.status == "ready"}, checksums)
        except OSError:
            observed = {}
            issue = "备份文件夹暂时不可读取，请检查目录或重新连接磁盘。"
        for record in recorded:
            identity = record.id
            if record.status in ("failed", "writing", "expiring"):
                observed[identity] = record
            else:
                found = observed.get(identity)
                if found is None:
                    found = self.read_item(record.path)
                if found is not None and found.id == identity:
                    if found.path == record.path and found.size == record.size:
                        found = replace(found, status="ready")
                    observed[identity] = replace(found, scheduled_day=record.scheduled_day, checksum=record.checksum)
                else:
                    observed[identity] = replace(record, status="unavailable", error="找不到可读的备份文件。")
        results = sorted(observed.values(), key=lambda item: (item.created_at, item.id), reverse=True)
        return directory, [replace(item, pinned=item.id in pinned) for item in results], issue

    def get(self, identity: UUID) -> CatalogItem:
        _, items, _ = self.items()
        for item in items:
            if item.id == str(identity):
                return item
        raise BackupError("not_found", "找不到这份备份，请检查保存位置。")

    def pin(self, identity: UUID, pinned: bool) -> CatalogItem:
        item = self.get(identity)
        if item.status not in ("ready", "candidate"):
            raise BackupError("unavailable", "备份文件可用后才能标记保留。")
        with closing(self.connect()) as db, db:
            if pinned:
                db.execute("INSERT OR IGNORE INTO pins VALUES (?)", (str(identity),))
            else:
                db.execute("DELETE FROM pins WHERE id=?", (str(identity),))
        return replace(item, pinned=pinned)

    def alternatives(self, item: CatalogItem) -> Iterator[CatalogItem]:
        for path in sorted(item.path.parent.iterdir()):
            if path != item.path and (other := self.read_item(path)) is not None and other.id == item.id:
                yield other

    def discover(self, directory: Path, recorded: dict[str, CatalogItem], checksums: dict[str, str]) -> dict[str, CatalogItem]:
        groups: dict[str, list[CatalogItem]] = {}
        try:
            candidates = sorted(directory.iterdir())
        except FileNotFoundError:
            if directory == self.default_directory:
                return {}
            raise
        for path in candidates:
            if (item := self.read_item(path)) is not None:
                groups.setdefault(item.id, []).append(item)

        result = {}
        for identity, items in groups.items():
            original = recorded.get(identity)
            ranked = items
            if original is not None:
                ranked = sorted(items, key=lambda item: (item.size == original.size, item.path == original.path),
                                reverse=True)
            checksum = checksums.get(identity)
            chosen = ranked[0]
            candidates = ranked if original is None or original.size is None else [
                item for item in ranked if item.size == original.size
            ]
            if checksum and len(candidates) > 1:
                # A moved backup can have a verified digest without a local creation record.
                for item in candidates:
                    try:
                        info = item.path.lstat()
                        if not S_ISREG(info.st_mode) or not plain(info) or info.st_size != item.size:
                            continue
                        if transfer_file(item.path, stamp(info)) == checksum:
                            chosen = item
                            break
                    except (OSError, BackupError):
                        continue
            result[identity] = replace(chosen, checksum=checksum)
        return result

    @staticmethod
    def read_item(path: Path) -> CatalogItem | None:
        if path.suffix.lower() != EXTENSION:
            return None
        try:
            info = path.lstat()
            if not S_ISREG(info.st_mode) or not plain(info):
                return None
            with path.open("rb") as handle:
                header = BackupHeader.read(handle)
        except (OSError, BackupError):
            return None
        return CatalogItem(str(header.identity), path, header.created_at.isoformat(), "candidate", info.st_size)
