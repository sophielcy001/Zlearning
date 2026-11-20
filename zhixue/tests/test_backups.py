from contextlib import closing
from errno import ENOSPC
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend import backup_snapshot
from backend.app import create_app
from backend.backup_work import WORK_FOLDER, backup_workspace
from backend.worker import prepare_one


API = "/api/backups"
PASSWORD = "独立备份 密码 2026"
SAVED = "archive-test-private-note"


def open_client(folder):
    return TestClient(
        create_app(folder, "backup-test-session"), base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer backup-test-session"},
    )


def accepted(response):
    assert response.is_success, response.text
    return response.json()


def choose_destination(http, path):
    return accepted(http.put(f"{API}/destination", json={"directory": str(path)}))


def make_backup(http, password=PASSWORD):
    response = http.post(API, json={"password": password})
    assert response.status_code == 201, response.text
    return response.json()


def inspect_backup(http, item, password=PASSWORD):
    return http.post(f"{API}/{item['id']}/preview", json={"password": password})


def restore_file(http, item, selection, password=PASSWORD):
    return http.post(f"{API}/{item['id']}/files/restore", json={
        "password": password, "path": selection["path"], "confirmation": selection["confirmation"],
    })


def create_project(http, folder):
    project = accepted(http.post("/api/projects", json={
        "id": str(uuid4()), "topic": "SQLite 入门", "outcome": "检索学习记录", "foundation": "",
    }))
    while prepare_one(folder):
        pass
    return project


def import_reference(http, body=SAVED):
    identity = accepted(http.post("/api/files/upload", params={"name": "参考资料.txt"}, content=body.encode()))["id"]
    return accepted(http.get(f"/api/files/{identity}"))


def test_portable_backup_contains_learning_state_and_library_and_remembers_pin(tmp_path):
    device = tmp_path / "first"
    removable = tmp_path / "removable"
    with open_client(device) as http:
        project = create_project(http, device)
        document = import_reference(http)
        choose_destination(http, removable)
        item = make_backup(http)
        created = removable / item["name"]
        archive = created.read_bytes()
        assert item["status"] == "ready" and item["size"] == len(archive)
        assert SAVED.encode() not in archive and PASSWORD.encode() not in archive
        assert item["pinned"] is False
        pinned = accepted(http.patch(f"{API}/{item['id']}/retention", json={"pinned": True}))
        assert pinned["pinned"] is True
        preview = accepted(inspect_backup(http, item))
        assert preview["counts"]["backup"]["projects"] == preview["counts"]["current"]["projects"] == 1
        assert preview["counts"]["backup"]["lessons"] == len(project["lessons"])
        original = next(file for file in preview["files"] if file["path"] == document["path"])
        assert original["name"] == "参考资料.txt" and original["change"] == "same"
        assert not preview["currentOnly"]

    with open_client(device) as http:
        settings = accepted(http.get(API))
        assert settings["directory"] == str(removable)
        assert settings["items"][0]["pinned"]

    second = tmp_path / "second"
    # The only data brought to the second device is the encrypted file, even if renamed.
    second_media = tmp_path / "second-media"
    second_media.mkdir()
    shutil.copyfile(created, second_media / "换机查验.zhixue-backup")
    with open_client(second) as http:
        choose_destination(http, second_media)
        entry, = accepted(http.get(API))["items"]
        assert entry["id"] == item["id"] and entry["name"] == "换机查验.zhixue-backup"
        preview = accepted(inspect_backup(http, entry))
        assert preview["counts"]["current"]["projects"] == 0
        assert preview["counts"]["backup"]["projects"] == 1
        original = next(file for file in preview["files"] if file["path"] == document["path"])
        assert original["change"] == "missing" and original["confirmation"]


def test_portable_duplicate_can_be_previewed_and_recovered_after_restart(tmp_path):
    workspace, receiver = tmp_path / "旧工作区", tmp_path / "换机工作区"
    with open_client(workspace) as http:
        original = import_reference(http)
        archive = make_backup(http)
    media = tmp_path / "换机归档 # %"
    media.mkdir()
    intact = media / "z-完好.zhixue-backup"
    damaged = media / "!开头损坏.zhixue-backup"
    payload = (workspace / "backups" / "archives" / archive["name"]).read_bytes()
    corrupt = payload[:-1] + bytes([payload[-1] ^ 1])
    damaged.write_bytes(corrupt)
    intact.write_bytes(payload)

    with open_client(receiver) as http:
        choose_destination(http, media)
        before, = accepted(http.get(API))["items"]
        assert before["id"] == archive["id"] and before["name"] == damaged.name
        wrong = inspect_backup(http, archive, password="wrong-password")
        assert wrong.status_code == 422 and wrong.json()["code"] == "authentication"
        assert accepted(http.get(API))["items"][0]["name"] == damaged.name
        preview = accepted(inspect_backup(http, archive))
        selection = next(file for file in preview["files"] if file["path"] == original["path"])
        assert selection["change"] == "missing"

    with open_client(receiver) as http:
        selected, = accepted(http.get(API))["items"]
        assert selected["name"] == intact.name and selected["status"] == "candidate"
        copied = accepted(restore_file(http, archive, selection))
        recovered = accepted(http.get(f"/api/files/{copied['id']}"))
        assert copied["id"] != original["id"]
        assert (receiver / recovered["path"]).read_text(encoding="utf-8") == SAVED
    assert intact.read_bytes() == payload and damaged.read_bytes() == corrupt


@pytest.mark.parametrize("scenario", [
    "damaged-duplicate", "moved-truncated", "moved-same-size", "damaged-at-recorded-path",
])
def test_duplicate_header_does_not_hide_the_intact_backup(tmp_path, scenario):
    from backend.backup_catalog import BackupCatalog

    workspace = tmp_path / "workspace"
    archives = tmp_path / "archives"
    with open_client(workspace) as http:
        original = import_reference(http)
        choose_destination(http, archives)
        item = make_backup(http)
    intact = archives / item["name"]
    recorded_path = intact
    payload = intact.read_bytes()
    moved = scenario != "damaged-duplicate"
    damaged = archives / "!损坏副本.zhixue-backup"
    # Preserve the readable header, as an incomplete sync or a damaged copy could.
    corrupt = payload[:-5] if scenario == "moved-truncated" else payload[:-1] + bytes([payload[-1] ^ 1])
    if moved:
        renamed = archives / "z-移动后的完整备份.zhixue-backup"
        intact.rename(renamed)
        intact = renamed
    if scenario == "damaged-at-recorded-path":
        damaged = recorded_path
    damaged.write_bytes(corrupt)
    assert BackupCatalog.read_item(damaged).id == item["id"]
    damaged_first = damaged.name < intact.name
    assert damaged_first == (scenario != "damaged-at-recorded-path")

    current = workspace / original["path"]
    current.write_text("当前版本保留", encoding="utf-8")
    with open_client(workspace) as http:
        entry, = accepted(http.get(API))["items"]
        assert entry["id"] == item["id"]
        preview = accepted(inspect_backup(http, item))
        restored = next(row for row in preview["files"] if row["path"] == original["path"])
        assert entry["name"] == intact.name
        assert entry["status"] == ("candidate" if moved else "ready")
        copy = accepted(restore_file(http, item, restored))
        copied = accepted(http.get(f"/api/files/{copy['id']}"))
        assert copy["id"] != original["id"]
        assert (workspace / copied["path"]).read_text(encoding="utf-8") == SAVED
    assert current.read_text(encoding="utf-8") == "当前版本保留"
    assert intact.read_bytes() == payload and damaged.read_bytes() == corrupt
    (tmp_path / "同编号备份找回.json").write_text(json.dumps({
        "backupId": item["id"], "intactName": intact.name, "moved": moved,
        "selectedStatus": entry["status"], "sameHeader": True, "damagedFirst": damaged_first,
        "sameSize": len(payload) == len(corrupt), "damagedAtRecordedPath": damaged == recorded_path,
        "intactSHA256": sha256(payload).hexdigest(),
        "previewSucceeded": True, "copyMatchesOriginal": True, "currentFilePreserved": True,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_restore_defaults_to_copy_preserves_newer_file_and_checks_preview(tmp_path):
    with open_client(tmp_path) as http:
        saved = create_project(http, tmp_path)
        original = import_reference(http)
        item = make_backup(http)
        first = accepted(inspect_backup(http, item))
        selection = next(file for file in first["files"] if file["path"] == original["path"])
        path = tmp_path / original["path"]
        path.write_text("新版资料", encoding="utf-8")
        stale = restore_file(http, item, selection)
        assert stale.status_code == 409
        assert len(accepted(http.get("/api/files"))) == len(saved["lessons"]) + 1

        confirmed = accepted(inspect_backup(http, item))
        selection = next(file for file in confirmed["files"] if file["path"] == original["path"])
        assert selection["change"] == "changed"
        restored = restore_file(http, item, selection)
        assert restored.status_code == 201, restored.text
        assert restore_file(http, item, selection).json()["id"] == restored.json()["id"]
        file = accepted(http.get(f"/api/files/{restored.json()['id']}"))
        assert file["id"] != original["id"] and file["name"] == selection["copyName"]
        assert (tmp_path / file["path"]).read_text(encoding="utf-8") == SAVED
        assert path.read_text(encoding="utf-8") == "新版资料"
        assert accepted(http.get(f"/api/projects/{saved['id']}"))["task"]["status"] == "ready"
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as db:
            source = db.execute(
                "SELECT backup_id, source_path FROM backup_file_origins WHERE file_id = ?", (file["id"],),
            ).fetchone()
        assert source == (item["id"], original["path"])


def test_wrong_password_truncation_and_invalid_requests_publish_no_copies(tmp_path):
    with open_client(tmp_path) as http:
        original = import_reference(http)
        item = make_backup(http)
        media = Path(accepted(http.get(API))["directory"])
        preview = accepted(inspect_backup(http, item))
        selection = next(file for file in preview["files"] if file["path"] == original["path"])
        rejected = inspect_backup(http, item, "wrong-password-from-test")
        assert rejected.status_code == 422 and "wrong-password-from-test" not in rejected.text

        raw = "sensitive-in-invalid-body"
        malformed = http.post(API, content=f'{{"password":"{raw}"', headers={"Content-Type": "application/json"})
        assert malformed.status_code == 422 and raw not in malformed.text
        assert restore_file(http, item, {**selection, "path": "../zhixue.sqlite3"}).status_code == 422

        archive = media / item["name"]
        contents = archive.read_bytes()
        archive.write_bytes(contents[:-5])
        corrupted = restore_file(http, item, selection)
        assert corrupted.status_code == 422
        assert [result["id"] for result in accepted(http.get("/api/files"))] == [original["id"]]


@pytest.mark.parametrize("source", ["database", "library"])
def test_snapshot_is_not_published_when_a_writer_changes_the_source(tmp_path, monkeypatch, source):
    with open_client(tmp_path) as http:
        project = create_project(http, tmp_path)
        original = import_reference(http)
        original_writer = backup_snapshot.write_payload

        def write_then_change(*args, **kwargs):
            original_writer(*args, **kwargs)
            if source == "database":
                with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as writer, writer:
                    writer.execute("UPDATE projects SET outcome=? WHERE id=?", ("新目标", project["id"]))
            else:
                (tmp_path / original["path"]).write_text("并行用户编辑", encoding="utf-8")

        monkeypatch.setattr(backup_snapshot, "write_payload", write_then_change)
        failed = http.post(API, json={"password": PASSWORD})
        assert failed.status_code == 409 and failed.json()["code"] == "changed"
        catalog = accepted(http.get(API))
        entry, = catalog["items"]
        assert entry["status"] == "failed" and entry["size"] is None
        assert list(Path(catalog["directory"]).iterdir()) == []


def test_same_record_count_does_not_hide_changed_project(tmp_path):
    with open_client(tmp_path) as http:
        project = create_project(http, tmp_path)
        item = make_backup(http)
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as writer, writer:
            writer.execute("UPDATE projects SET outcome=? WHERE id=?", ("换了学习目标", project["id"]))
        preview = accepted(inspect_backup(http, item))
        assert preview["counts"]["backup"]["projects"] == preview["counts"]["current"]["projects"] == 1
        assert preview["databaseChanges"] == ["projects"]


def test_archive_includes_trash_and_large_file_and_scrubs_machine_credentials(tmp_path):
    with open_client(tmp_path) as http:
        original = import_reference(http)
        endpoint = f"/api/files/{original['id']}/trash"
        preview = accepted(http.get(endpoint))
        trashed = accepted(http.post(endpoint, json={"path": preview["path"], "revision": preview["revision"]}))
        opaque = os.urandom(2 * backup_snapshot.CHUNK_BYTES + 17)
        opaque_path = tmp_path / "library" / "large-reference.pdf"
        opaque_path.write_bytes(opaque)
        configuration = str(uuid4())
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as writer, writer:
            writer.execute(
                "INSERT INTO model_connections (id, name, provider, protocol, endpoint, model, credential_ref,"
                " revision, test_token, last_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (configuration, "本机模型", "minimax", "chat-completions", "https://example.invalid/v1",
                 "model-fixture", "isolate-machine-credential-pointer", 1, "local-test-token", '{"status":"passed"}'),
            )
        item = make_backup(http)
        media = Path(accepted(http.get(API))["directory"])
        archive = media / item["name"]
        assert archive.stat().st_size > 2 * backup_snapshot.CHUNK_BYTES
        target = tmp_path / "extracted-test.sqlite3"
        with target.open("wb") as handle:
            verified = backup_snapshot.read_backup(archive, PASSWORD, extract={backup_snapshot.DATABASE: handle})
        entries = {file.path: file for file in verified.manifest.files}
        assert entries["library/large-reference.pdf"].revision == sha256(opaque).hexdigest()
        assert entries[trashed["archivePath"]].revision == sha256(SAVED.encode()).hexdigest()
        with closing(sqlite3.connect(target)) as exported:
            assert exported.execute(
                "SELECT id, name, credential_ref, last_test, test_token FROM model_connections"
            ).fetchone() == (configuration, "本机模型", None, None, None)
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as live:
            assert live.execute("SELECT credential_ref FROM model_connections").fetchone() == (
                "isolate-machine-credential-pointer",
            )

        with archive.open("r+b") as handle:
            offset = backup_snapshot.CHUNK_BYTES + 128
            handle.seek(offset)
            byte, = handle.read(1)
            handle.seek(offset)
            handle.write(bytes([byte ^ 1]))
        assert inspect_backup(http, item).status_code == 422


def interrupted_post(folder, path, payload, hook):
    program = """
import json
import os
from pathlib import Path
import sys
from fastapi.testclient import TestClient
from backend.app import create_app
from backend import backup_catalog, backup_copies, backup_snapshot
""" + hook + """
data = json.load(sys.stdin)
with TestClient(create_app(Path(data["folder"]), "backup-test-session"), base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer backup-test-session"}) as http:
    response = http.post(data["path"], json=data["payload"])
    print(response.status_code, file=sys.stderr)
"""
    result = subprocess.run(
        [sys.executable, "-c", program], input=json.dumps({
            "folder": str(folder), "path": path, "payload": payload,
        }), text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 83, result.stderr


def test_restart_registers_verified_copy_after_process_exits_between_file_and_database(tmp_path):
    with open_client(tmp_path) as http:
        original = import_reference(http)
        item = make_backup(http)
        preview = accepted(inspect_backup(http, item))
        selection = next(file for file in preview["files"] if file["path"] == original["path"])
    interrupted_post(
        tmp_path, f"{API}/{item['id']}/files/restore",
        {"password": PASSWORD, "path": selection["path"], "confirmation": selection["confirmation"]},
        "backup_copies.register_copy = lambda *_: os._exit(83)\n",
    )
    with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as database:
        published, identity = database.execute("SELECT published, file_id FROM backup_file_origins").fetchone()
    assert published == 0
    with open_client(tmp_path) as http:
        assert not list((tmp_path / WORK_FOLDER).glob("job-*"))
        copy = accepted(http.get(f"/api/files/{identity}"))
        assert (tmp_path / copy["path"]).read_text(encoding="utf-8") == SAVED
        assert (tmp_path / original["path"]).read_text(encoding="utf-8") == SAVED
        assert restore_file(http, item, selection).json()["id"] == identity
        assert len(accepted(http.get("/api/files"))) == 2


@pytest.mark.parametrize("after_publish", [False, True])
def test_restart_recovers_backup_attempt_around_atomic_publication(tmp_path, after_publish):
    with open_client(tmp_path) as http:
        original = import_reference(http)
        media = Path(accepted(http.get(API))["directory"])
    if after_publish:
        hook = """
save = backup_catalog.BackupCatalog.record
def interrupt_on_ready(self, item):
    if item.status == "ready":
        os._exit(83)
    save(self, item)
backup_catalog.BackupCatalog.record = interrupt_on_ready
"""
    else:
        hook = """
write = backup_snapshot.write_payload
def interrupt_after_write(*args, **kwargs):
    write(*args, **kwargs)
    os._exit(83)
backup_snapshot.write_payload = interrupt_after_write
"""
    interrupted_post(tmp_path, API, {"password": PASSWORD}, hook)
    pattern = "*.zhixue-backup" if after_publish else ".*.partial"
    assert list(media.glob(pattern))
    assert list((tmp_path / WORK_FOLDER).glob("job-*"))
    with open_client(tmp_path) as http:
        entry, = accepted(http.get(API))["items"]
        assert entry["status"] == ("ready" if after_publish else "failed")
        assert not list(media.glob(".*.partial"))
        assert not list((tmp_path / WORK_FOLDER).glob("job-*"))
        if after_publish:
            result = accepted(inspect_backup(http, entry))
            assert any(file["path"] == original["path"] for file in result["files"])


def test_second_operation_waits_for_workspace_and_disk_failure_keeps_existing_backup(tmp_path, monkeypatch):
    with open_client(tmp_path) as http:
        original = make_backup(http)
        with backup_workspace(tmp_path):
            busy = http.post(API, json={"password": PASSWORD})
            assert busy.status_code == 409 and busy.json()["code"] == "busy"

        def full_disk(*args, **kwargs):
            raise OSError(ENOSPC, "simulated disk exhaustion")

        monkeypatch.setattr(backup_snapshot, "write_payload", full_disk)
        failed = http.post(API, json={"password": PASSWORD})
        assert failed.status_code == 507 and "空间不足" in failed.json()["detail"]
        items = accepted(http.get(API))["items"]
        assert {item["status"] for item in items} == {"ready", "failed"}
        assert inspect_backup(http, original).status_code == 200
        assert not list((tmp_path / WORK_FOLDER).glob("job-*"))
