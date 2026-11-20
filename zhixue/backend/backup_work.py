from contextlib import closing, contextmanager
from pathlib import Path
import re
from shutil import rmtree
import sqlite3
from stat import S_ISDIR
from uuid import uuid4

from .backup_format import BackupError


WORK_FOLDER = ".backup-work"
JOB_NAME = re.compile(r"job-[0-9a-f]{32}")


@contextmanager
def backup_workspace(folder: Path, *, allocate: bool = True):
    """Keep staging on the local device and recover it only after the previous owner has gone."""
    root = folder / WORK_FOLDER
    root.mkdir(mode=0o700, exist_ok=True)
    lockfile = root / "lease.sqlite3"
    if root.resolve() != root or lockfile.resolve() != lockfile:
        raise BackupError("work_directory", "备份工作目录无法安全访问。")
    with closing(sqlite3.connect(lockfile, timeout=0)) as lease:
        try:
            lease.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise BackupError("busy", "另一项备份或找回操作正在执行，请稍后重试。") from None
            raise
        for path in root.iterdir():
            if JOB_NAME.fullmatch(path.name) and S_ISDIR(path.lstat().st_mode) and path.resolve() == path:
                rmtree(path)
        if not allocate:
            yield root
            return
        job = root / f"job-{uuid4().hex}"
        job.mkdir(mode=0o700)
        try:
            yield job
        finally:
            rmtree(job)
