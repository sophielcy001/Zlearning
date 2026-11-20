from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from stat import S_ISREG

from .backup_catalog import BackupCatalog, CatalogItem
from .backup_format import BackupError
from .backup_snapshot import plain, read_backup, stamp, transfer_file
from .project_activity import study_moment


def unchanged(path: Path, expected: tuple) -> bool:
    try:
        return path.resolve() == path and stamp(path.lstat()) == expected
    except OSError:
        return False


def archive_proof(catalog: BackupCatalog, item: CatalogItem, password: str) -> tuple | None:
    try:
        info = item.path.lstat()
        if (item.path.resolve() != item.path or not S_ISREG(info.st_mode) or not plain(info)
                or info.st_size != item.size):
            return None
        before = stamp(info)
        if item.checksum is not None:
            return before if transfer_file(item.path, before) == item.checksum else None
        verified = read_backup(item.path, password)
        if str(verified.header.identity) != item.id or verified.header.created_at.isoformat() != item.created_at:
            return None
    except (OSError, BackupError):
        return None
    if unchanged(item.path, before):
        catalog.record(replace(item, checksum=verified.checksum))
        return before
    return None


def expire(catalog: BackupCatalog, item: CatalogItem, proof: tuple, keeper: tuple[Path, tuple]) -> None:
    catalog.record(replace(item, status="expiring"))
    if not unchanged(*keeper):
        catalog.record(item)
        raise BackupError("retention_changed", "拟保留的归档已变化，稍后重新检查过期备份。")
    if unchanged(item.path, proof):
        item.path.unlink(missing_ok=True)
        catalog.forget(item)
    else:
        catalog.record(item)


def prune_archives(catalog: BackupCatalog, directory: Path, today: date, password: str) -> None:
    cutoff = today - timedelta(days=29)
    older = defaultdict(list)
    for item in catalog.records():
        if item.pinned or item.status != "ready" or item.path.parent != directory:
            continue
        try:
            day = study_moment(datetime.fromisoformat(item.created_at)).date()
        except ValueError:
            continue
        if day < cutoff:
            older[day.year, day.month].append(item)
    for month in older.values():
        if len(month) < 2:
            continue
        keeper = None
        for item in sorted(month, key=lambda item: (item.created_at, item.id), reverse=True):
            proof = archive_proof(catalog, item, password)
            if proof is None:
                continue
            if keeper is None:
                keeper = (item.path, proof)
            else:
                expire(catalog, item, proof, keeper)
