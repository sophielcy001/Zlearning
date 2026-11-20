from pathlib import PurePosixPath, PureWindowsPath

import pytest

from backend.storage_files import sqlite_file_uri


@pytest.mark.parametrize(("path", "expected"), [
    (PureWindowsPath(r"\\nas.example\share\数据库 # %.sqlite3"),
     "file:////nas.example/share/%E6%95%B0%E6%8D%AE%E5%BA%93%20%23%20%25.sqlite3"),
    (PureWindowsPath(r"C:\learning data\backup #.sqlite3"),
     "file:///C:/learning%20data/backup%20%23.sqlite3"),
    (PurePosixPath("/home/demo/backup ?#.sqlite3"),
     "file:///home/demo/backup%20%3F%23.sqlite3"),
])
@pytest.mark.parametrize("mode", ["ro", "rw"])
def test_database_file_uri_preserves_path_and_access_mode(path, expected, mode):
    assert sqlite_file_uri(path, mode=mode) == f"{expected}?mode={mode}"

