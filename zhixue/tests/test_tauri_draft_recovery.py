import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.backup_format import BackupError
from backend.backup_restore import exclusive_lease
from backend.tauri_draft_recovery import recover_drafts


@pytest.fixture
def target(tmp_path):
    data = tmp_path / "tauri-data"
    offline = data / "offline-study.sqlite3"
    data.mkdir()
    app = create_app(data, "recovery-test", desktop_offline=offline)
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer recovery-test"}) as client:
        workspace = client.get("/api/mobile/catalog").json()["workspace"]
    return data, offline, workspace


def draft(workspace, answer, *, identity="paper"):
    return {
        "kind": "review", "key": f"{workspace}:review:{identity}",
        "snapshot": {"workspace": workspace, "id": identity, "title": "旧题组",
                     "questions": [{"text": "选择一项", "options": ["甲", "乙", "丙"]}]},
        "draft": [answer],
    }


def note(workspace):
    return {
        "kind": "lesson", "key": f"{workspace}:lesson:offline",
        "snapshot": {"workspace": workspace, "id": "offline", "title": "旧课程", "body": "课程正文"},
        "note": {"content": "未同步的原笔记", "editedAt": "2026-05-01T12:00:00Z", "revision": "baseline"},
    }


def bundle(*origins):
    return {"format": "zhixue-tauri-drafts-v1", "origins": [
        {"port": port, "records": records} for port, records in origins
    ]}


def sqlite_rows(file):
    with closing(sqlite3.connect(file)) as conn:
        return conn.execute("SELECT key, revision, json_extract(payload, '$.draft[0]') FROM offline_study ORDER BY key").fetchall()


def test_preview_deduplicates_identical_versions_and_requires_a_choice_for_different_answers(target):
    data, offline, workspace = target
    first, second, local_note = draft(workspace, 0), draft(workspace, 1), note(workspace)
    key = first["key"]
    source = bundle((40101, [first, local_note]), (40102, [second]), (40103, [first]))

    preview = recover_drafts(data, source)
    assert preview["ready"] == [local_note["key"]]
    assert preview["conflicts"] == [{"key": key, "variants": [[40101, 40103], [40102]]}]
    assert sqlite_rows(offline) == []
    assert recover_drafts(data, source, apply=True)["imported"] == [local_note["key"]]
    assert recover_drafts(data, source, apply=True, choices={key: 40102})["imported"] == [key]
    assert sqlite_rows(offline) == [(local_note["key"], 1, None), (key, 1, 1)]
    assert recover_drafts(data, source, apply=True, choices={key: 40102})["alreadyPresent"] == [local_note["key"], key]
    assert recover_drafts(data, source, apply=True, choices={key: 40101})["keptCurrent"] == [key]
    assert sqlite_rows(offline)[1] == (key, 1, 1)


def test_import_preserves_tombstones_and_skips_other_workspaces_or_submitted_answers(target):
    data, offline, workspace = target
    removed = draft(workspace, 0)
    submitted = {**draft(workspace, 1, identity="submitted"), "submission": {"id": "sent"}}
    unrelated = draft("other-workspace", 1)
    with closing(sqlite3.connect(offline)) as db, db:
        db.execute("INSERT INTO offline_study VALUES ('items', ?, 7, NULL)", (removed["key"],))
    report = recover_drafts(data, bundle((40200, [removed, submitted, unrelated])), apply=True)
    assert report["keptCurrent"] == [removed["key"]]
    assert report["skipped"] == {"otherWorkspace": 1, "notDraft": 1}
    assert report["imported"] == []
    assert sqlite_rows(offline) == [(removed["key"], 7, None)]


def test_recovery_refuses_unknown_choices_and_a_running_tauri_service(target):
    data, offline, workspace = target
    first = draft(workspace, 1)
    source = bundle((40201, [first]))
    with pytest.raises(ValueError, match="端口"):
        recover_drafts(data, source, apply=True, choices={first["key"]: 40202})
    assert sqlite_rows(offline) == []
    with exclusive_lease(data / ".zhixue-launch.sqlite3"):
        with pytest.raises(BackupError, match="正在被另一处"):
            recover_drafts(data, source, apply=True)
    assert sqlite_rows(offline) == []
