import os
from pathlib import Path, PurePath
from typing import Literal


def sqlite_file_uri(path: PurePath, *, mode: Literal["ro", "rw"]) -> str:
    uri = path.as_uri()
    if path.drive.startswith("\\\\"):
        # Keep the UNC server in the path and give SQLite an empty authority.
        uri = "file://" + uri.removeprefix("file:")
    return f"{uri}?mode={mode}"


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
