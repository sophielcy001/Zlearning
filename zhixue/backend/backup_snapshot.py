from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from gzip import BadGzipFile, GzipFile
from hashlib import sha256
from io import BufferedReader
import json
import os
from pathlib import Path, PureWindowsPath
import sqlite3
from stat import S_ISDIR, S_ISREG
from typing import Annotated, Callable
import zlib

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.engine import Engine

from .backup_format import (
    CHUNK_BYTES, LENGTH, ArchiveReader, ArchiveWriter, BackupError, BackupHeader, damaged, incomplete_file, read_exact,
)
from .content import lesson_course
from .db import Base, transaction
from .library_trash import PENDING as TRASH_PENDING
from .storage_files import sqlite_file_uri


DATABASE = "zhixue.sqlite3"
MANIFEST_LIMIT = 16 * 1024 * 1024
COUNTED_TABLES = (
    "projects", "lessons", "reviews", "attempts", "timers", "imports", "note_versions",
    "note_conflicts", "notes", "note_lessons", "note_suggestions", "project_views", "model_connections", "model_roles", "tasks",
    "note_check_plans", "note_check_batches",
    "conversations", "chat_messages", "chat_attachments", "chat_replies", "chat_model_calls", "teaching_preferences", "goal_authoring_jobs", "goal_versions", "route_versions",
    "model_studies", "generation_steps", "generation_settings", "model_uses", "model_charges", "model_task_budgets",
    "course_review_plans", "course_findings", "course_versions", "course_anchor_links", "course_dispositions",
    "assessment_plans", "assessment_objectives", "assessment_rounds", "assessment_grades", "assessment_jobs",
    "remediation_cards",
    "stage_assessments", "stage_assessment_attempts", "stage_reviews", "stage_review_calls",
    "lesson_assistant_plans", "lesson_questions", "lesson_reading_positions",
    "lesson_consolidation_plans", "lesson_consolidations",
    "prerequisite_tasks", "prerequisite_calls",
    "study_plan_settings", "study_day_overrides", "study_plan_locks",
    "timer_spans", "timer_corrections", "study_plan_snapshots",
    "reminder_settings", "reminder_receipts", "library_ocr_pages", "library_web_snapshots",
    "library_graph", "library_graph_revisions", "graph_recommendations", "graph_candidates", "library_wiki_aliases",
    "library_file_tags", "library_file_activity",
    "library_file_copies",
    "library_classifications", "library_classification_candidates", "mobile_changes",
)
REPARSE_POINT = 0x400
EntrySize = Annotated[int, Field(strict=True, ge=0)]
ArchivePath = Annotated[str, Field(min_length=1, max_length=2048)]


def library_path(value: str) -> bool:
    parts = value.split("/")
    return len(parts) >= 2 and parts[0] == "library" and all(
        part not in ("", ".", "..")
        and not any(ord(char) < 32 or char in '\\:' for char in part)
        for part in parts
    )


class ArchiveFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: ArchivePath
    name: Annotated[str, Field(min_length=1, max_length=512)]
    size: EntrySize
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database_schema: Annotated[str, Field(min_length=1, max_length=99)]
    counts: dict[str, EntrySize]
    database: ArchiveFile
    directories: list[ArchivePath]
    files: list[ArchiveFile]

    @model_validator(mode="after")
    def safe_paths(self):
        if self.database.path != DATABASE or self.database.name != DATABASE:
            raise ValueError("Invalid database entry.")
        paths = [file.path for file in self.files]
        if any(not library_path(path) for path in paths) or len(set(paths)) != len(paths):
            raise ValueError("Invalid library entries.")
        dirs = set(self.directories)
        if len(dirs) != len(self.directories) or any(path != "library" and not library_path(path) for path in dirs):
            raise ValueError("Invalid library directories.")
        if dirs.intersection(paths) or any(
            path.rpartition("/")[0] not in dirs for path in [*paths, *(dirs - {"library"})]
        ):
            raise ValueError("Invalid library layout.")
        return self


@dataclass(frozen=True)
class VerifiedBackup:
    header: BackupHeader
    manifest: Manifest
    revision: str
    checksum: str


def changed() -> BackupError:
    return BackupError("changed", "备份读取期间有学习数据或知识库文件发生变化，请重新操作。")


def stamp(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def plain(info: os.stat_result) -> bool:
    return not (getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def scan_library(folder: Path) -> tuple[list[str], dict[str, tuple]]:
    root = folder / "library"
    try:
        mode = root.lstat()
    except FileNotFoundError:
        return [], {}
    if not S_ISDIR(mode.st_mode) or not plain(mode):
        raise BackupError("library_path", "知识库根目录需为普通文件夹。")
    directories = ["library"]
    entries = {}
    waiting = [root]
    try:
        while waiting:
            directory = waiting.pop()
            if directory.resolve() != directory or not S_ISDIR(directory.lstat().st_mode):
                raise changed()
            for path in directory.iterdir():
                relative = path.relative_to(folder).as_posix()
                info = path.lstat()
                if not plain(info) or not library_path(relative):
                    raise BackupError("library_path", f"请检查知识库中不支持的链接或文件名（{relative}）。")
                if S_ISDIR(info.st_mode):
                    directories.append(relative)
                    waiting.append(path)
                elif S_ISREG(info.st_mode):
                    entries[relative] = stamp(info)
                else:
                    raise BackupError("library_path", f"知识库中存在链接或特殊文件（{relative}）。")
    except FileNotFoundError:
        raise changed() from None
    return sorted(directories), entries


def transfer_file(path: Path, expected: tuple, target=None) -> str:
    digest = sha256()
    try:
        if path.resolve() != path:
            raise changed()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not S_ISREG(info.st_mode) or stamp(info) != expected:
                raise changed()
            while block := source.read(CHUNK_BYTES):
                digest.update(block)
                if target is not None:
                    target.write(block)
            if path.resolve() != path or stamp(os.fstat(source.fileno())) != expected or stamp(path.lstat()) != expected:
                raise changed()
    except FileNotFoundError:
        raise changed() from None
    return digest.hexdigest()


def current_revision(folder: Path, relative: str) -> str:
    path = folder / relative
    if not library_path(relative) or path.resolve() != path:
        raise BackupError("library_path", "原位置含链接或无法安全读取。")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    if not S_ISREG(info.st_mode) or not plain(info):
        return "occupied"
    return transfer_file(path, stamp(info))


def existing_tables(db: sqlite3.Connection) -> list[str]:
    names = {name for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    return [table for table in COUNTED_TABLES if table in names]


def table_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in existing_tables(db)}


def table_versions(db: sqlite3.Connection, tables: list[str] | None = None) -> dict[str, str]:
    result = {}
    for table in tables if tables is not None else existing_tables(db):
        columns = "*"
        if table == "model_connections":
            price = "pricing" if any(row[1] == "pricing" for row in db.execute("PRAGMA table_info(model_connections)")) else "NULL"
            columns = f"id, name, provider, protocol, endpoint, model, {price}"
        digest = sha256()
        for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY rowid"):
            logical = tuple(("bytes", sha256(value).hexdigest()) if isinstance(value, bytes) else value for value in row)
            digest.update(repr(logical).encode("utf-8") + b"\n")
        result[table] = digest.hexdigest()
    return result


def file_names(db: sqlite3.Connection) -> dict[str, str]:
    locations = dict(db.execute("SELECT id, path FROM library_locations"))
    result = {}
    for identity, name in db.execute("SELECT id, name FROM imports"):
        default = f"library/imports/{identity}{Path(name).suffix.lower()}"
        result[locations.get(identity, default)] = name
    outline_column = "lessons.outline" if any(row[1] == "outline" for row in db.execute("PRAGMA table_info(lessons)")) else "NULL"
    for identity, position, sample, outline in db.execute(
        f"SELECT lessons.id, lessons.position, projects.sample, {outline_column} FROM lessons JOIN projects ON projects.id=lessons.project_id"
    ):
        title = lesson_course(sample, position, json.loads(outline) if outline else None).title
        result[locations.get(f"course-{identity}", f"library/courses/{identity}.md")] = f"{title}.md"
        result[locations.get(f"note-{identity}", f"library/notes/{identity}.md")] = f"{title} · 笔记.md"
    if "notes" in existing_tables(db):
        for identity, title in db.execute("SELECT id, title FROM notes WHERE title IS NOT NULL"):
            path = locations.get(f"note-{identity}", f"library/notes/{identity}.md")
            result[path] = f"{title}.md"
    for identity, name, format_ in db.execute("SELECT id, name, format FROM library_trash WHERE state='trashed'"):
        if Path(name).suffix.lower() != f".{format_}":
            name = f"{name}.{format_}"
        result[f"library/.trash/{identity}/original.{format_}"] = name
    if "remediation_cards" in existing_tables(db):
        for identity, basis, number in db.execute(
            "SELECT cards.grade_id, cards.basis, rounds.number FROM remediation_cards cards "
            "JOIN assessment_rounds rounds ON rounds.id=cards.round_id WHERE cards.published_at IS NOT NULL"
        ):
            title = json.loads(basis)["title"]
            path = locations.get(f"supplement-{identity}", f"library/supplements/{identity}.md")
            result[path] = f"{title} · 第 {number} 轮错因补学.md"
    if "prerequisite_tasks" in existing_tables(db):
        for identity, title in db.execute("SELECT id, topic FROM prerequisite_tasks WHERE published_at IS NOT NULL"):
            file_id = f"prerequisite-{identity}"
            path = locations.get(file_id, f"library/supplements/{file_id}.md")
            result[path] = f"{title} · 前置知识.md"
    return result


@contextmanager
def observe_state(engine: Engine, folder: Path):
    path = folder / DATABASE
    with closing(sqlite3.connect(sqlite_file_uri(path, mode="ro"), uri=True, timeout=5)) as source:
        # Pair the initial filesystem inventory with the database read snapshot at a writer boundary.
        with transaction(engine):
            before = source.execute("PRAGMA data_version").fetchone()[0]
            source.execute("BEGIN")
            schema = source.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            placeholders = ", ".join("?" for _ in TRASH_PENDING)
            if (source.execute("SELECT 1 FROM note_writes WHERE completed=0 LIMIT 1").fetchone()
                    or source.execute(f"SELECT 1 FROM library_trash WHERE state IN ({placeholders}) LIMIT 1", TRASH_PENDING).fetchone()
                    or source.execute("SELECT 1 FROM backup_file_origins WHERE published=0 LIMIT 1").fetchone()
                    or source.execute("SELECT 1 FROM library_file_copies WHERE state='staged' LIMIT 1").fetchone()
                    or source.execute("SELECT 1 FROM library_classification_candidates WHERE state='approved' LIMIT 1").fetchone()):
                raise BackupError("pending_write", "知识库有尚未完成的笔记或文件操作，请完成恢复后重试。")
            directories, entries = scan_library(folder)
        yield source, schema, directories, entries
        if scan_library(folder) != (directories, entries):
            raise changed()
        source.commit()
        if source.execute("PRAGMA data_version").fetchone()[0] != before:
            raise changed()


def verified_database(path: Path, expected_schema: str, *, compare: bool = False) -> tuple[dict[str, int], dict[str, str]]:
    try:
        with closing(sqlite3.connect(sqlite_file_uri(path, mode="ro"), uri=True)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise damaged()
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise damaged()
            if connection.execute("SELECT version_num FROM alembic_version").fetchone() != (expected_schema,):
                raise damaged()
            return table_counts(connection), table_versions(connection) if compare else {}
    except sqlite3.Error:
        raise damaged() from None


def stage_database(source: sqlite3.Connection, destination: Path, schema: str) -> dict[str, int]:
    with closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA secure_delete=ON")
        # Connection settings are portable. OS credential references and old probe results are machine-specific.
        target.execute("UPDATE model_connections SET credential_ref=NULL, last_test=NULL, test_token=NULL")
        target.execute("DELETE FROM model_credential_pending")
        target.execute("DELETE FROM reminder_push_claims")
        if target.execute("SELECT 1 FROM sqlite_schema WHERE name='recognition_grants'").fetchone():
            target.execute("DELETE FROM recognition_grants")
        target.commit()
    counts, _ = verified_database(destination, schema)
    return counts


def write_payload(writer: ArchiveWriter, folder: Path, database: Path, manifest: Manifest, entries: dict[str, tuple]) -> None:
    description = manifest.model_dump_json().encode("utf-8")
    if len(description) > MANIFEST_LIMIT:
        raise BackupError("too_large", "知识库文件清单超过当前备份格式支持的大小。")
    with GzipFile(fileobj=writer, mode="wb", filename="", compresslevel=3, mtime=0) as payload:
        payload.write(LENGTH.pack(len(description)))
        payload.write(description)
        for file in [manifest.database, *manifest.files]:
            path = database if file.path == DATABASE else folder / file.path
            expected = entries[file.path]
            if transfer_file(path, expected, payload) != file.revision:
                raise changed()
    writer.finish()


def create_backup(
    engine: Engine, folder: Path, destination: Path, header: BackupHeader, password: str, work: Path,
    before_publish: Callable[[int, str], None], *, create_directory: bool = True,
) -> int:
    if create_directory:
        destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = incomplete_file(destination)
    handle = temporary.open("xb")
    try:
        with handle:
            database = work / DATABASE
            with observe_state(engine, folder) as (source, schema, directories, entries):
                counts = stage_database(source, database, schema)
                names = file_names(source)
                files = [
                    ArchiveFile(path=path, name=names.get(path, Path(path).name), size=entries[path][2],
                                revision=transfer_file(folder / path, entries[path]))
                    for path in sorted(entries)
                ]
                database_stamp = stamp(database.lstat())
                manifest = Manifest(
                    database_schema=schema, counts=counts, directories=directories, files=files,
                    database=ArchiveFile(path=DATABASE, name=DATABASE, size=database_stamp[2],
                                         revision=transfer_file(database, database_stamp)),
                )
                write_payload(ArchiveWriter(handle, header, password), folder, database, manifest,
                              {**entries, DATABASE: database_stamp})
            handle.flush()
            os.fsync(handle.fileno())
            size = handle.tell()
        verification = read_backup(temporary, password)
        if verification.header.identity != header.identity or verification.manifest != manifest:
            raise damaged()
        if destination.exists():
            raise BackupError("exists", "备份文件名已被占用，请重新创建备份。")
        before_publish(size, verification.checksum)
        os.replace(temporary, destination)
        return size
    finally:
        temporary.unlink(missing_ok=True)


def make_archive_directories(destination: Path, manifest: Manifest) -> None:
    if os.name == "nt":
        for item in [*manifest.directories, *(file.path for file in manifest.files)]:
            if any(PureWindowsPath(part).is_reserved() or part.endswith((".", " "))
                   or any(char in '<>"|?*' for char in part) for part in item.split("/")):
                raise BackupError("library_path", "备份含有当前系统无法恢复的文件名。")
    for directory in sorted(manifest.directories, key=lambda path: path.count("/")):
        (destination / directory).mkdir(mode=0o700)
    if "library" not in manifest.directories:
        (destination / "library").mkdir(mode=0o700)


def read_backup(path: Path, password: str, *, extract: dict[str, object] | None = None,
                destination: Path | None = None) -> VerifiedBackup:
    try:
        if path.resolve() != path or not S_ISREG(path.lstat().st_mode):
            raise BackupError("unavailable", "备份文件的位置已变更，请重新选择。")
        original_stamp = stamp(path.lstat())
        with path.open("rb") as handle, BufferedReader(ArchiveReader(handle, password)) as stream:
            with GzipFile(fileobj=stream, mode="rb") as payload:
                length, = LENGTH.unpack(read_exact(payload, LENGTH.size))
                if not 1 <= length <= MANIFEST_LIMIT:
                    raise damaged()
                description = read_exact(payload, length)
                manifest = Manifest.model_validate_json(description)
                if destination is not None:
                    make_archive_directories(destination, manifest)
                for file in [manifest.database, *manifest.files]:
                    digest = sha256()
                    target = ((destination / file.path).open("xb") if destination is not None
                              else nullcontext(extract.get(file.path) if extract else None))
                    with target as output:
                        remaining = file.size
                        while remaining:
                            data = read_exact(payload, min(CHUNK_BYTES, remaining))
                            digest.update(data)
                            if output is not None:
                                output.write(data)
                            remaining -= len(data)
                        if digest.hexdigest() != file.revision:
                            raise damaged()
                        if destination is not None:
                            output.flush()
                            os.fsync(output.fileno())
                if payload.read(1) or stream.read(1) or not stream.raw.finished:
                    raise damaged()
                if original_stamp != stamp(os.fstat(handle.fileno())) or original_stamp != stamp(path.lstat()):
                    raise changed()
                return VerifiedBackup(stream.raw.header, manifest, sha256(description).hexdigest(), stream.raw.digest.hexdigest())
    except (BadGzipFile, EOFError, ValidationError, zlib.error):
        raise damaged() from None


def current_view(engine: Engine, folder: Path) -> tuple[dict[str, int], dict[str, str], dict[str, dict], str, bool]:
    with observe_state(engine, folder) as (source, schema, directories, entries):
        counts = table_counts(source)
        versions = table_versions(source)
        names = file_names(source)
        files = {
            path: {"path": path, "name": names.get(path, Path(path).name), "size": info[2],
                   "revision": transfer_file(folder / path, info)}
            for path, info in entries.items()
        }
        tables = {name for (name,) in source.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        other = sorted(tables.intersection(Base.metadata.tables) - versions.keys())
        state = {
            "schema": schema, "tables": {**versions, **table_versions(source, other)}, "directories": directories,
            "files": {path: item["revision"] for path, item in files.items()},
        }
        fingerprint = sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        timer = source.execute("SELECT 1 FROM timers WHERE status IN ('running', 'paused') LIMIT 1").fetchone()
    return counts, versions, files, fingerprint, timer is not None
