from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .backup_catalog import BackupCatalog, CatalogItem
from .backup_format import EXTENSION, BackupError, BackupHeader
from .backup_snapshot import create_backup


def save_archive(engine: Engine, catalog: BackupCatalog, work: Path, password: str, created: datetime,
                 *, directory: Path | None = None, scheduled_day: str | None = None) -> CatalogItem:
    identity = uuid4()
    created = created.astimezone(UTC).replace(microsecond=0)
    directory = directory if directory is not None else catalog.directory()
    path = directory / f"zhixue-{created:%Y%m%d-%H%M%S}-{identity.hex}{EXTENSION}"
    item = CatalogItem(str(identity), path, created.isoformat(), "writing", None, scheduled_day=scheduled_day)
    catalog.record(item)

    def validated(size: int, checksum: str) -> None:
        nonlocal item
        item = replace(item, size=size, checksum=checksum)
        catalog.record(item)

    try:
        create_backup(engine, catalog.folder, path, BackupHeader.create(identity, created), password, work, validated,
                      create_directory=scheduled_day is None)
    except (BackupError, OSError, sqlite3.Error, SQLAlchemyError) as error:
        message = str(error) if isinstance(error, BackupError) else "备份写入失败，请检查目录权限和剩余空间。"
        try:
            catalog.record(replace(item, status="failed", error=message))
        except (OSError, sqlite3.Error):
            pass
        raise
    result = replace(item, status="ready")
    catalog.record(result)
    return result
