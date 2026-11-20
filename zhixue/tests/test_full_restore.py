from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from errno import ENOSPC
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend import backup_restore
from backend.backup_restore import restore_root, recover_full_restore
from backend.db import GenerationStep, ModelUse, open_db, transaction
from backend.model_study import dispatch, reserve_call
from backend.restore_runtime import RestoreRuntime, attach_restore
from test_backups import (
    API, PASSWORD, accepted, choose_destination, create_project, import_reference, inspect_backup, make_backup,
)
from test_model_study import consent_request, setup_model


@contextmanager
def app_client(folder, pause=lambda: None, resume=lambda: None, *, now=None):
    runtime = RestoreRuntime(pause, resume)
    app = create_app(folder, "restore-test", now=now)
    attach_restore(app, runtime)
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer restore-test"}) as http:
        try:
            yield http
        finally:
            runtime.close()


def request_restore(http, backup, preview, password=PASSWORD):
    response = http.post(f"{API}/{backup['id']}/restore", json={
        "password": password, "confirmation": preview["restore"]["confirmation"],
    })
    assert response.status_code == 202, response.text
    return response.json()


def wait_restore(http):
    deadline = monotonic() + 12
    while monotonic() < deadline:
        status = accepted(http.get(f"{API}/restore-status"))
        if status["state"] != "running":
            return status
        sleep(.04)
    raise AssertionError("Restore stayed in progress")


def add_project(http, topic):
    return accepted(http.post("/api/projects", json={"id": str(uuid4()), "topic": topic, "outcome": "隔离恢复验收"}))


def test_full_restore_replaces_the_pair_keeps_current_encrypted_backup_and_holds_old_tasks(tmp_path):
    with app_client(tmp_path) as http:
        original_workspace = accepted(http.get('/api/mobile/catalog'))['workspace']
        project = create_project(http, tmp_path)
        previous = add_project(http, "保留旧课程准备任务")
        old = import_reference(http, body="恢复前学过的资料.txt")
        note_id = str(uuid4())
        lessons = [lesson["id"] for lesson in project["lessons"][:2]]
        note = accepted(http.post("/api/notes", json={"id": note_id, "title": "两个知识点共用", "lessonIds": lessons}))
        endpoint = f"/api/notes/{note_id}"
        original = accepted(http.get(endpoint))
        note_saved = accepted(http.put(endpoint, json={"content": "备份中的笔记正文", "baseRevision": original["revision"]}))
        item = make_backup(http)
        accepted(http.put(endpoint + "/details", json={
            "title": "恢复前的新名称", "lessonIds": [], "metadataRevision": note["metadataRevision"],
        }))
        accepted(http.put(endpoint, json={"content": "恢复前的编辑", "baseRevision": note_saved["revision"]}))
        added = import_reference(http, body="当前新资料.txt")
        other = add_project(http, "本次应回退的当前项目")
        plan = accepted(inspect_backup(http, item))
        assert plan["restore"]["available"] is True
        assert plan["counts"]["backup"]["notes"] == 1 and plan["counts"]["backup"]["note_lessons"] == 2
        note_entry = next(file for file in plan["files"] if file["path"] == f"library/notes/{note_id}.md")
        assert note_entry["name"] == "两个知识点共用.md"
        assert any(file["path"] == added["path"] for file in plan["currentOnly"])
        request_restore(http, item, plan)
        result = wait_restore(http)
        assert result["state"] == "complete", result
        assert result["heldTasks"] == 1
        assert accepted(http.get("/api/mobile/catalog"))["workspace"] != original_workspace
        rollback = next(entry for entry in accepted(http.get(API))["items"] if entry["id"] == result["rollbackBackupId"])
        assert rollback["pinned"]
        saved = accepted(inspect_backup(http, rollback))
        assert saved["counts"]["backup"]["projects"] == 3
        assert any(file["path"] == added["path"] for file in saved["files"])
        assert accepted(http.get(f"/api/projects/{project['id']}"))["task"]["status"] == "ready"
        assert http.get(f"/api/projects/{other['id']}").status_code == 404
        assert accepted(http.get(f"/api/projects/{previous['id']}"))["task"]["status"] == "restore_pending"
        assert (tmp_path / old["path"]).read_text(encoding="utf-8")
        assert not (tmp_path / added["path"]).exists()
        assert accepted(http.post(f"/api/projects/{previous['id']}/retry"))["task"]["status"] == "queued"
        restored = accepted(http.get(endpoint + "/details"))
        assert restored["title"] == "两个知识点共用" and {item["id"] for item in restored["lessons"]} == set(lessons)
        assert accepted(http.get(endpoint))["revision"] == note_saved["revision"]
        assert (tmp_path / note_entry["path"]).read_text(encoding="utf-8") == "备份中的笔记正文"
        assert [path.name for path in (tmp_path / "library" / "notes").glob("*.md")] == [f"{note_id}.md"]


def test_portable_duplicate_full_restore_uses_verified_archive(tmp_path):
    device = tmp_path / "producer"
    with app_client(device) as http:
        original = import_reference(http, body="换机需要的资料")
        archive = make_backup(http)
    media = tmp_path / "副本 # %"
    media.mkdir()
    payload = (device / "backups" / "archives" / archive["name"]).read_bytes()
    damaged = media / "!截断.zhixue-backup"
    intact = media / "z-原件.zhixue-backup"
    damaged.write_bytes(payload[:-5])
    intact.write_bytes(payload)

    receiver = tmp_path / "receiver"
    with app_client(receiver) as http:
        current = import_reference(http, body="恢复前保留")
        choose_destination(http, media)
        assert accepted(http.get(API))["items"][0]["name"] == damaged.name
        preview = accepted(inspect_backup(http, archive))
        assert preview["restore"]["confirmation"]
        operation = request_restore(http, archive, preview)
        result = wait_restore(http)
        assert result["state"] == "complete" and result["id"] == operation["id"], result
        assert accepted(http.get(f"/api/files/{original['id']}"))["content"] == "换机需要的资料"
        assert http.get(f"/api/files/{current['id']}").status_code == 404
        rollback = accepted(inspect_backup(http, {"id": result["rollbackBackupId"]}))
        assert any(file["path"] == current["path"] for file in rollback["files"])
    assert intact.read_bytes() == payload and damaged.read_bytes() == payload[:-5]


def test_full_restore_rejects_stale_current_state_and_wrong_password_without_switching(tmp_path):
    with app_client(tmp_path) as http:
        old = import_reference(http, body="资料.txt")
        item = make_backup(http)
        original = accepted(inspect_backup(http, item))
        (tmp_path / old["path"]).write_text("更新后的文字", encoding="utf-8")
        request_restore(http, item, original)
        assert wait_restore(http)["code"] == "stale"
        assert (tmp_path / old["path"]).read_text(encoding="utf-8") == "更新后的文字"
        confirmed = accepted(inspect_backup(http, item))
        request_restore(http, item, confirmed, "wrong-password-not-for-reporting")
        failed = wait_restore(http)
        assert failed["state"] == "failed" and "wrong-password" not in json.dumps(failed)
        assert (tmp_path / old["path"]).read_text(encoding="utf-8") == "更新后的文字"
        assert len(accepted(http.get(API))["items"]) == 1
        archive = Path(item["directory"]) / item["name"]
        with archive.open("r+b") as handle:
            handle.seek(-1, 2)
            tail = handle.read(1)
            handle.seek(-1, 2)
            handle.write(bytes((tail[0] ^ 1,)))
        request_restore(http, item, confirmed)
        assert wait_restore(http)["state"] == "failed"
        assert (tmp_path / old["path"]).read_text(encoding="utf-8") == "更新后的文字"
        assert len(accepted(http.get(API))["items"]) == 1


@pytest.mark.parametrize("still_busy", [None, "receipt", "cleanup"])
def test_next_restore_settles_the_previous_commit_before_switching(tmp_path, monkeypatch, still_busy):
    write_json = backup_restore.write_json
    rmtree = backup_restore.shutil.rmtree
    receipt_locked = True
    busy_directory = None

    def locked_receipt(path, value):
        if receipt_locked and path.name == backup_restore.RECEIPT:
            raise PermissionError("restore receipt is busy")
        write_json(path, value)

    def locked_cleanup(path, *args, **kwargs):
        if Path(path) == busy_directory and kwargs.get("ignore_errors"):
            return
        rmtree(path, *args, **kwargs)

    monkeypatch.setattr(backup_restore, "write_json", locked_receipt)
    monkeypatch.setattr(backup_restore.shutil, "rmtree", locked_cleanup)
    with app_client(tmp_path) as http:
        original = import_reference(http, body="第一份备份中的资料")
        first_backup = make_backup(http)
        extra = import_reference(http, body="只在第二份备份中的资料")
        second_backup = make_backup(http)

        first = request_restore(http, first_backup, accepted(inspect_backup(http, first_backup)))
        assert wait_restore(http)["state"] == "complete"
        root = restore_root(tmp_path)
        first_job = root / f"switch-{first['id']}"
        journal = json.loads((first_job / backup_restore.JOURNAL).read_text(encoding="utf-8"))
        assert journal["phase"] == "committed"
        assert not (tmp_path / extra["path"]).exists()

        receipt_locked = still_busy == "receipt"
        busy_directory = first_job if still_busy == "cleanup" else None
        second = request_restore(http, second_backup, accepted(inspect_backup(http, second_backup)))
        result = wait_restore(http)
        if still_busy:
            assert (result["state"], result["code"]) == ("failed", "previous_restore_pending")
            assert list(root.glob("switch-*")) == [first_job]
            assert not (tmp_path / extra["path"]).exists()
            assert accepted(http.get(f"/api/files/{original['id']}"))["content"] == original["content"]
            receipt_locked, busy_directory = False, None
            second = request_restore(http, second_backup, accepted(inspect_backup(http, second_backup)))
            result = wait_restore(http)
        assert result["state"] == "complete"
        assert json.loads((root / backup_restore.RECEIPT).read_text(encoding="utf-8"))["id"] == second["id"]
        assert (tmp_path / extra["path"]).read_text(encoding="utf-8") == extra["content"]
        assert (tmp_path / original["path"]).read_text(encoding="utf-8") == original["content"]

    latest = recover_full_restore(tmp_path)
    assert latest["id"] == second["id"], "an older commit overwrote the new receipt on restart"
    assert latest["state"] == "complete"
    assert not list(root.glob("switch-*"))


@pytest.mark.parametrize("pending", ["older", "both", "newer", "older_failed"])
def test_legacy_restore_journals_keep_the_newest_receipt(tmp_path, monkeypatch, pending):
    write_json = backup_restore.write_json
    receipt_locked = pending in {"older", "both"}
    root = restore_root(tmp_path)

    def locked_receipt(path, value):
        if receipt_locked and path.name == backup_restore.RECEIPT:
            raise PermissionError("restore receipt is busy")
        write_json(path, value)

    monkeypatch.setattr(backup_restore, "write_json", locked_receipt)
    # UUID order deliberately differs from the order of the actual restore operations.
    identifiers = iter([UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"), UUID("11111111-1111-4111-8111-111111111111")])
    monkeypatch.setattr("backend.restore_runtime.uuid4", lambda: next(identifiers))
    with app_client(tmp_path) as http:
        original = import_reference(http, body="两份备份共有的原始文件")
        first_backup = make_backup(http)
        extra = import_reference(http, body="较新一次恢复才带回的文件")
        second_backup = make_backup(http)

        with monkeypatch.context() as failure:
            if pending == "older_failed":
                move = backup_restore.move
                rmtree = backup_restore.shutil.rmtree

                def interrupt_install(source, target):
                    if source.parent.name == "incoming" and target == tmp_path / "library":
                        raise OSError("simulated failure before the commit")
                    move(source, target)

                def leave_journal(path, *args, **kwargs):
                    if Path(path).parent == root and kwargs.get("ignore_errors"):
                        return
                    rmtree(path, *args, **kwargs)

                failure.setattr(backup_restore, "move", interrupt_install)
                failure.setattr(backup_restore.shutil, "rmtree", leave_journal)
            first = request_restore(http, first_backup, accepted(inspect_backup(http, first_backup)))
            assert wait_restore(http)["state"] == ("failed" if pending == "older_failed" else "complete")
        receipt_locked = pending in {"both", "newer"}
        # Simulate the shipped version that allowed a new switch before settling prior journals.
        with monkeypatch.context() as legacy:
            legacy.setattr(backup_restore, "settle_pending_operations", lambda *_a, **_kw: None)
            second = request_restore(http, second_backup, accepted(inspect_backup(http, second_backup)))
            assert wait_restore(http)["state"] == "complete"

    receipt_locked = False
    expected_jobs = {first["id"], second["id"]} if pending == "both" else {second["id"] if pending == "newer" else first["id"]}
    assert {job.name.removeprefix("switch-") for job in root.glob("switch-*")} == expected_jobs
    latest = recover_full_restore(tmp_path)
    assert latest["id"] == second["id"]
    assert latest["state"] == "complete"
    assert recover_full_restore(tmp_path) == latest
    assert not list(root.glob("switch-*"))
    assert (tmp_path / original["path"]).read_text(encoding="utf-8") == original["content"]
    assert (tmp_path / extra["path"]).read_text(encoding="utf-8") == extra["content"]
    with app_client(tmp_path) as http:
        assert accepted(http.get(f"/api/files/{extra['id']}"))["content"] == extra["content"]


def test_new_edits_to_retained_library_abort_switch_without_losing_data(tmp_path, monkeypatch):
    with app_client(tmp_path) as http:
        document = import_reference(http, body="旧资料")
        item = make_backup(http)
        project = add_project(http, "整库恢复前的新课程")
        preview = accepted(inspect_backup(http, item))
        move = backup_restore.move

        def external_edit(source, target):
            move(source, target)
            if source == tmp_path / "library":
                relative = Path(document["path"]).relative_to("library")
                (target / relative).write_text("切换时写入的外部修改", encoding="utf-8")

        monkeypatch.setattr(backup_restore, "move", external_edit)
        request_restore(http, item, preview)
        assert wait_restore(http)["code"] == "stale"
        assert (tmp_path / document["path"]).read_text(encoding="utf-8") == "切换时写入的外部修改"
        assert accepted(http.get(f"/api/projects/{project['id']}"))["id"] == project["id"]
        assert not list(restore_root(tmp_path).glob("switch-*"))


def test_restore_pauses_model_dispatch_and_caps_running_timer_at_backup_time(tmp_path):
    producer, receiver, media = (tmp_path / name for name in ("producer", "receiver", "media"))
    started = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(minutes=4)
    with app_client(producer, now=lambda: started) as http:
        local = create_project(http, producer)
        timer = accepted(http.post("/api/timer", json={"action": "start", "lessonId": local["lessons"][0]["id"]}))["active"]
        setup_model(http)
        endpoint, payload, disclosure = consent_request(http, max_calls=5)
        project_id = accepted(http.post(endpoint, json={**payload, "modelConsent": disclosure["consent"]}))["projectId"]
        choose_destination(http, media)
        engine = open_db(producer)
        try:
            claim = reserve_call(engine, started)
            assert claim is not None
            backups = []

            def backup_during_send(*_):
                backups.append(make_backup(http))
                return 503, b""

            dispatch(engine, claim, transport=backup_during_send, now=lambda: started)
            item, = backups
        finally:
            engine.dispose()

    with app_client(receiver, now=lambda: started + timedelta(days=5)) as http:
        choose_destination(http, media)
        plan = accepted(inspect_backup(http, item))
        request_restore(http, item, plan)
        result = wait_restore(http)
        assert result["state"] == "complete", result
        assert result["heldTasks"] == result["heldTimers"] == 1
        assert accepted(http.get(f"/api/projects/{project_id}"))["task"]["status"] == "restore_pending"
        state = accepted(http.get("/api/timer"))
        captured = datetime.fromisoformat(item["createdAt"]).replace(tzinfo=None)
        assert state["active"] is None
        pending, = state["stashed"]
        assert pending["id"] == timer["id"] and pending["elapsed"] == (captured - started).total_seconds()
        engine = open_db(receiver)
        try:
            with transaction(engine, write=False) as db:
                call = db.get(ModelUse, claim.identity)
                step = db.get(GenerationStep, claim.step_id)
                assert call.status == "uncertain" and call.finished_at == captured
                assert step.active_call is None
            assert reserve_call(engine, started + timedelta(days=5)) is None
        finally:
            engine.dispose()


def test_full_restore_drains_and_fences_writes_and_keeps_original_if_snapshot_runs_out_of_space(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    def pause():
        entered.set()
        assert release.wait(4)

    with app_client(tmp_path, pause) as http:
        old = import_reference(http, body="原稿.txt")
        item = make_backup(http)
        plan = accepted(inspect_backup(http, item))

        def full(*args, **kwargs):
            raise OSError(ENOSPC, "simulated full device")

        monkeypatch.setattr("backend.backup_restore.create_backup", full)
        start = request_restore(http, item, plan)
        try:
            assert entered.wait(3)
            assert accepted(http.get(f"{API}/restore-status"))["id"] == start["id"]
            assert accepted(http.get("/api/setup"))["stage"] == "complete"
            assert http.post("/api/setup/complete").status_code == 503
            write = http.post("/api/files", json={"name": "未到达的写入.txt", "content": "内容"})
            assert write.status_code == 503
        finally:
            release.set()
        assert wait_restore(http)["code"] == "space"
        assert (tmp_path / old["path"]).is_file()
        assert http.get("/api/files").is_success


def write_pair(folder, label):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "library").mkdir()
    (folder / "library" / "page.md").write_text(label, encoding="utf-8")
    with closing(sqlite3.connect(folder / "zhixue.sqlite3")) as db, db:
        db.execute("CREATE TABLE record (value TEXT NOT NULL)")
        db.execute("INSERT INTO record VALUES (?)", (label,))


def read_pair(folder):
    with closing(sqlite3.connect(folder / "zhixue.sqlite3")) as db:
        content = db.execute("SELECT value FROM record").fetchone()[0]
    return content, (folder / "library" / "page.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("committed", [False, True])
def test_full_volume_settles_receipt_after_reclaiming_unused_switch_files(tmp_path, monkeypatch, committed):
    write_pair(tmp_path, "current")
    root = restore_root(tmp_path)
    operation = root / ("switch-" + uuid4().hex)
    write_pair(operation / "incoming", "backup")
    save = backup_restore.write_json
    discarded = operation / ("retained" if committed else "discarded")
    receipt = {"id": "volume-fixture", "state": "complete"}
    receipt_space_failures = []

    def limited_volume(path, value):
        if path.name == backup_restore.JOURNAL and value["phase"] == "committed" and not committed:
            raise OSError(ENOSPC, "cannot write the commit marker")
        if path.name == backup_restore.RECEIPT and (discarded / "zhixue.sqlite3").exists():
            receipt_space_failures.append(path)
            raise OSError(ENOSPC, "cannot write the receipt while the staging payload remains")
        save(path, value)

    monkeypatch.setattr(backup_restore, "write_json", limited_volume)
    if committed:
        backup_restore.switch_workspace(tmp_path, operation, receipt, backup_restore.scan_library(tmp_path))
    else:
        with pytest.raises(OSError) as failure:
            backup_restore.switch_workspace(tmp_path, operation, receipt, backup_restore.scan_library(tmp_path))
        assert failure.value.errno == ENOSPC

    assert receipt_space_failures == [root / backup_restore.RECEIPT]
    assert not operation.exists()
    settled = recover_full_restore(tmp_path)
    assert settled["id"] == receipt["id"]
    assert settled["state"] == ("complete" if committed else "failed")
    expected = "backup" if committed else "current"
    assert read_pair(tmp_path) == (expected, expected)


@pytest.mark.parametrize("committed", [False, True])
def test_process_crash_recovers_old_or_committed_pair_from_durable_switch_marker(tmp_path, committed):
    write_pair(tmp_path, "current")
    operation = restore_root(tmp_path) / ("switch-" + uuid4().hex)
    write_pair(operation / "incoming", "backup")
    code = """
import json, os, sys
from pathlib import Path
import backend.backup_restore as restore
folder, job = map(Path, sys.argv[1:3])
late = sys.argv[3] == "True"
move = restore.move
save = restore.write_json
def crash_midway(source, target):
    move(source, target)
    if not late and target == folder / restore.DATABASE:
        os._exit(71)
def crash_after_commit(path, data):
    save(path, data)
    if late and data.get("phase") == "committed":
        os._exit(72)
restore.move = crash_midway
restore.write_json = crash_after_commit
restore.switch_workspace(folder, job, {"id": "crash-fixture", "state": "complete"}, restore.scan_library(folder))
"""
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(operation), str(committed)], check=False)
    assert child.returncode == (72 if committed else 71)
    recover_full_restore(tmp_path)
    expected = "backup" if committed else "current"
    assert read_pair(tmp_path) == (expected, expected)
    assert not operation.exists()
    recover_full_restore(tmp_path)
    assert read_pair(tmp_path) == (expected, expected)


def test_full_restore_recovers_reviewed_ocr_alongside_original(tmp_path):
    from test_library import scanned_sample, upload

    image = scanned_sample()[1]
    with app_client(tmp_path) as http:
        identity = accepted(upload(http, "离线示例.png", image))["id"]
        endpoint = f"/api/files/{identity}"
        original = accepted(http.get(endpoint))
        reviewer = f"{endpoint}/pages/1/ocr"
        body = {"revision": original["revision"], "textRevision": None, "text": "备份中的校对正文"}
        saved = accepted(http.put(reviewer, json=body))
        item = make_backup(http)
        accepted(http.put(reviewer, json={**body, **saved, "text": "备份后编辑"}))
        preview = accepted(inspect_backup(http, item))
        assert preview["counts"]["backup"]["library_ocr_pages"] == 1
        request_restore(http, item, preview)
        result = wait_restore(http)
        assert result["state"] == "complete", result
        restored = accepted(http.get(endpoint, params={"at": 1, "revision": saved["textRevision"]}))
        assert restored["content"] == body["text"]
        assert http.get(f"{endpoint}/original").content == image
        hits = accepted(http.get("/api/files/search", params={"q": "校对正文"}))["hits"]
        assert hits[0]["location"]["textRevision"] == saved["textRevision"]
