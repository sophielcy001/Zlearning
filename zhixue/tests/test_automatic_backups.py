from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from errno import ENOSPC
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient

from backend import backup_snapshot
from backend.app import create_app
from backend.backup_automatic import AutomaticBackups
from backend.backup_catalog import BackupCatalog
from backend.backup_jobs import save_archive
from backend.backup_work import backup_workspace
from backend.db import open_db
from test_backups import API, PASSWORD, accepted, choose_destination, import_reference, inspect_backup, open_client
from test_model_settings import TestVault


SCHEDULE = f"{API}/automatic"
ROTATED = "测试新密码不会回写已有归档"
TOKEN = "automatic-backup-test-session"


def client(folder, vault):
    return TestClient(create_app(folder, TOKEN, backup_credentials=vault), base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {TOKEN}"})


def clock(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def cycle(folder, vault, at):
    engine = open_db(folder)
    try:
        AutomaticBackups(BackupCatalog(folder), vault).check(engine, at)
    finally:
        engine.dispose()


def archive_rows(http):
    return accepted(http.get(API))["items"]


def test_existing_catalog_exposes_a_disabled_automatic_schedule(tmp_path):
    with open_client(tmp_path) as http:
        state = accepted(http.get(API))["automatic"]
        assert state["enabled"] is False
        assert state["lastAt"] is None


def test_scheduled_backup_runs_once_per_shanghai_day_rotates_password_and_recovers_locked_vault(tmp_path):
    vault = TestVault()
    before_midnight = clock("2026-11-14T15:57:00")
    after_midnight = before_midnight + timedelta(minutes=4)
    with client(tmp_path, vault) as http:
        document = import_reference(http)
        assert accepted(http.put(SCHEDULE, json={"password": PASSWORD}))["enabled"]
        assert len(vault.values) == 1
        cycle(tmp_path, vault, before_midnight)
        cycle(tmp_path, vault, before_midnight + timedelta(minutes=2))
        first, = archive_rows(http)
        assert first["automatic"] and first["status"] == "ready"
        assert any(item["path"] == document["path"] for item in accepted(inspect_backup(http, first))["files"])

        vault.locked = True
        cycle(tmp_path, vault, after_midnight)
        failed = accepted(http.get(API))["automatic"]
        assert failed["lastState"] == "failed" and failed["enabled"]
        assert "系统凭据库" in failed["error"]
        assert "test-only" not in str(failed)
        vault.locked = False
        cycle(tmp_path, vault, after_midnight + timedelta(minutes=1))
        assert len(archive_rows(http)) == 1

        response = accepted(http.put(SCHEDULE, json={"password": ROTATED}))
        assert response["enabled"] and not response["credentialCleanupPending"]
        assert list(vault.values.values()) == [ROTATED]
        cycle(tmp_path, vault, after_midnight + timedelta(minutes=2))
        newest, previous = archive_rows(http)
        assert previous["id"] == first["id"]
        assert newest["automatic"] and newest["status"] == "ready"
        assert inspect_backup(http, newest, ROTATED).is_success
        assert not inspect_backup(http, newest, PASSWORD).is_success
        assert inspect_backup(http, previous, PASSWORD).is_success
        assert not inspect_backup(http, previous, ROTATED).is_success
        assert not accepted(http.delete(SCHEDULE))["enabled"]
        assert not vault.values
        cycle(tmp_path, vault, after_midnight + timedelta(days=1))
        assert len(archive_rows(http)) == 2
    for path in tmp_path.rglob("*.sqlite3*"):
        assert PASSWORD.encode() not in path.read_bytes() and ROTATED.encode() not in path.read_bytes()


def test_failed_password_rotation_keeps_the_old_key_and_recovers_the_uncommitted_one(tmp_path):
    class InterruptedVault(TestVault):
        broken = False

        def put(self, reference, value):
            super().put(reference, value)
            if self.broken:
                raise OSError(value)

        def discard(self, reference):
            if self.broken:
                raise OSError("system store currently locked")
            super().discard(reference)

    vault = InterruptedVault()
    with client(tmp_path, vault) as http:
        assert accepted(http.put(SCHEDULE, json={"password": PASSWORD}))["enabled"]
        vault.broken = True
        expired = http.put(SCHEDULE, json={"password": ROTATED})
        assert expired.status_code == 409 and ROTATED not in expired.text
        status = accepted(http.get(API))["automatic"]
        assert status["enabled"] and status["credentialCleanupPending"]
        vault.broken = False
        cycle(tmp_path, vault, clock("2026-11-15T04:00:00"))
        assert list(vault.values.values()) == [PASSWORD]
        item, = archive_rows(http)
        assert inspect_backup(http, item, PASSWORD).is_success


def test_missing_destination_and_disk_full_are_retryable_without_falling_back_to_the_local_disk(tmp_path, monkeypatch):
    vault = TestVault()
    at = clock("2026-11-15T04:00:00")
    media = tmp_path / "media"
    disconnected = tmp_path / "detached"
    with client(tmp_path, vault) as http:
        choose_destination(http, media)
        assert accepted(http.put(SCHEDULE, json={"password": PASSWORD}))["enabled"]
        media.rename(disconnected)
        cycle(tmp_path, vault, at)
        status = accepted(http.get(API))["automatic"]
        assert status["lastState"] == "failed" and "重新连接" in status["error"]
        assert not media.exists()
        assert not (tmp_path / "backups" / "archives").exists()
        disconnected.rename(media)

        def full(*_args, **_kwargs):
            raise OSError(ENOSPC, "simulated full storage")

        with monkeypatch.context() as patch:
            patch.setattr(backup_snapshot, "write_payload", full)
            cycle(tmp_path, vault, at + timedelta(minutes=6))
        assert "空间不足" in accepted(http.get(API))["automatic"]["error"]
        assert not list(media.iterdir())
        cycle(tmp_path, vault, at + timedelta(minutes=12))
        rows = archive_rows(http)
        assert {item["status"] for item in rows} == {"ready", "failed"}
        item = next(item for item in rows if item["status"] == "ready")
        assert inspect_backup(http, item).is_success


def test_retention_keeps_recent_monthly_pinned_unmanaged_and_changed_files(tmp_path):
    vault = TestVault()
    media, other = tmp_path / "media", tmp_path / "other"
    catalog = BackupCatalog(tmp_path)
    with client(tmp_path, vault) as http:
        choose_destination(http, media)
        user_file = import_reference(http)
        trash = tmp_path / "library" / ".trash" / "用户回收资料.txt"
        trash.parent.mkdir(exist_ok=True)
        trash.write_text("回收站由用户管理", encoding="utf-8")
        engine = open_db(tmp_path)
        try:
            def archive(day, directory=media):
                with backup_workspace(tmp_path) as work:
                    return save_archive(engine, catalog, work, PASSWORD, clock(f"{day}T04:00:00"), directory=directory)

            recent = archive("2026-10-17")
            older = archive("2026-09-02")
            missing = archive("2026-09-10")
            latest = archive("2026-09-28")
            changed = archive("2026-09-29")
            pinned = archive("2026-03-04")
            march = archive("2026-03-10")
            off_directory = archive("2026-03-13", other)
            external = media / off_directory.path.name
            shutil.copyfile(off_directory.path, external)
            missing.path.unlink()
            original = changed.path.read_bytes()
            changed.path.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
            accepted(http.patch(f"{API}/{pinned.id}/retention", json={"pinned": True}))
            assert accepted(http.put(SCHEDULE, json={"password": ROTATED}))["enabled"]
        finally:
            engine.dispose()

        cycle(tmp_path, vault, clock("2026-11-15T04:00:00"))
        assert not older.path.exists()
        assert not any(row["id"] == older.id for row in archive_rows(http))
        assert recent.path.is_file() and latest.path.is_file() and pinned.path.is_file() and march.path.is_file()
        assert off_directory.path.is_file() and external.is_file()
        assert changed.path.read_bytes() != original
        assert inspect_backup(http, latest.view(), PASSWORD).is_success
        assert (tmp_path / user_file["path"]).is_file() and trash.read_text(encoding="utf-8") == "回收站由用户管理"


def test_restart_detects_auto_archive_published_before_catalog_commit_and_avoids_a_duplicate(tmp_path):
    vault = TestVault()
    at = clock("2026-11-15T04:00:00")
    with client(tmp_path, vault) as http:
        assert accepted(http.put(SCHEDULE, json={"password": PASSWORD}))["enabled"]

    child = """
from datetime import datetime
from pathlib import Path
import os, sys
from backend.backup_automatic import AutomaticBackups
from backend.backup_catalog import BackupCatalog
from backend.db import open_db

class DemoStore:
    def get(self, reference):
        return sys.argv[3]

original = BackupCatalog.record
def crash_after_publication(self, item):
    if item.scheduled_day and item.status == "ready":
        os._exit(91)
    return original(self, item)
BackupCatalog.record = crash_after_publication
folder = Path(sys.argv[1]).resolve()
AutomaticBackups(BackupCatalog(folder), DemoStore()).check(open_db(folder), datetime.fromisoformat(sys.argv[2]))
"""
    crashed = subprocess.run([sys.executable, "-c", child, str(tmp_path), at.isoformat(), PASSWORD], check=False)
    assert crashed.returncode == 91
    cycle(tmp_path, vault, at + timedelta(minutes=1))
    with client(tmp_path, vault) as http:
        saved, = archive_rows(http)
        assert saved["automatic"] and saved["status"] == "ready"
        assert inspect_backup(http, saved).is_success


def test_legacy_archives_stay_until_the_original_password_can_verify_them(tmp_path):
    vault = TestVault()
    at = clock("2026-11-15T04:00:00")
    catalog = BackupCatalog(tmp_path)
    with client(tmp_path, vault) as http:
        engine = open_db(tmp_path)
        try:
            old = []
            for day in (2, 26):
                with backup_workspace(tmp_path) as work:
                    old.append(save_archive(engine, catalog, work, PASSWORD, clock(f"2026-06-{day:02d}T04:00:00")))
            with closing(catalog.connect()) as db, db:
                # Archives from the preceding catalog version had no encrypted content checksum.
                db.execute("DELETE FROM archive_metadata")
        finally:
            engine.dispose()
        assert accepted(http.put(SCHEDULE, json={"password": ROTATED}))["enabled"]
        cycle(tmp_path, vault, at)
        assert all(item.path.is_file() for item in old)
        for item in old:
            assert inspect_backup(http, item.view(), PASSWORD).is_success
        cycle(tmp_path, vault, at + timedelta(days=1))
        assert not old[0].path.exists()
        assert inspect_backup(http, old[1].view(), PASSWORD).is_success


def test_retention_retries_if_the_monthly_keeper_disappears_during_pruning(tmp_path, monkeypatch):
    vault = TestVault()
    at = clock("2026-11-15T04:00:00")
    catalog = BackupCatalog(tmp_path)
    with client(tmp_path, vault) as http:
        engine = open_db(tmp_path)
        try:
            with backup_workspace(tmp_path) as work:
                expired = save_archive(engine, catalog, work, PASSWORD, clock("2026-08-02T04:00:00"))
            with backup_workspace(tmp_path) as work:
                keeper = save_archive(engine, catalog, work, PASSWORD, clock("2026-08-28T04:00:00"))
        finally:
            engine.dispose()

        set_aside = tmp_path / "monthly-offline"
        record = BackupCatalog.record

        def disconnect(self, item):
            record(self, item)
            if item.status == "expiring":
                keeper.path.rename(set_aside)

        accepted(http.put(SCHEDULE, json={"password": PASSWORD}))
        with monkeypatch.context() as patch:
            patch.setattr(BackupCatalog, "record", disconnect)
            cycle(tmp_path, vault, at)
        assert expired.path.is_file() and not keeper.path.exists()
        state = accepted(http.get(API))["automatic"]
        assert state["lastState"] == "ready" and "清理过期归档暂缓" in state["error"]
        set_aside.rename(keeper.path)
        cycle(tmp_path, vault, at + timedelta(minutes=6))
        assert keeper.path.is_file() and not expired.path.exists()
        assert len([item for item in archive_rows(http) if item["automatic"]]) == 1
