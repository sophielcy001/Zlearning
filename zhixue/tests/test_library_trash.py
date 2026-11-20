from contextlib import contextmanager
import os
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.worker import prepare_one


@contextmanager
def app(folder, **options):
    with TestClient(create_app(folder, "trash-test"), base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer trash-test"}, **options) as http:
        yield http


def read(http, url, **params):
    result = http.get(url, params=params)
    assert result.is_success, result.text
    return result.json()


def import_file(http, name="参考.txt", content="用于引用的第二行\n学习资料回收验证"):
    result = http.post("/api/files/upload", params={"name": name}, content=content.encode())
    assert result.status_code == 201
    return result.json()["id"]


def move_to_trash(http, file_id):
    endpoint = f"/api/files/{file_id}/trash"
    preview = read(http, endpoint)
    result = http.post(endpoint, json={"path": preview["path"], "revision": preview["revision"]})
    assert result.is_success, result.text
    return result.json()


def restore(http, entry, path=None):
    return http.post(f"/api/files/trash/{entry['id']}/restore", json={
        "revision": entry["revision"], "path": path or entry["originalPath"],
    })


def test_trash_survives_restart_and_restore_preserves_pinned_reference(tmp_path):
    with app(tmp_path) as http:
        identity = import_file(http)
        url = f"/api/files/{identity}"
        file = read(http, url)
        entry = move_to_trash(http, identity)
        assert entry["fileId"] == identity and entry["originalPath"] == file["path"] and entry["deletedAt"]
        assert not (tmp_path / file["path"]).exists()
        archived = tmp_path / entry["archivePath"]
        assert archived.is_relative_to(tmp_path / "library" / ".trash") and archived.is_file()
        assert read(http, "/api/files") == []
        assert read(http, "/api/files/search", q="学习资料回收验证")["hits"] == []
        assert read(http, url, revision=file["revision"], at=2)["status"] == "trashed"
        assert http.get(url + "/original").status_code == 409
        http.post("/api/files/reindex").raise_for_status()
        assert read(http, "/api/files/search", q="参考")["hits"] == []
    with app(tmp_path) as http:
        item, = read(http, "/api/files/trash")
        assert item["id"] == entry["id"] and item["deletedAt"] == entry["deletedAt"]
        result = restore(http, item)
        assert result.is_success, result.text
        assert read(http, "/api/files/trash") == []
        restored = read(http, url, revision=file["revision"], at=2)
        assert restored["id"] == identity and restored["anchor"]["text"] == "学习资料回收验证"
        assert not archived.exists()


def test_restore_to_occupied_path_requires_alternative_and_preserves_both(tmp_path):
    with app(tmp_path) as http:
        identity = import_file(http)
        entry = move_to_trash(http, identity)
        old = tmp_path / entry["originalPath"]
        old.write_text("另一份内容", encoding="utf-8")
        assert restore(http, entry).status_code == 409
        assert old.read_text(encoding="utf-8") == "另一份内容"
        assert read(http, "/api/files/trash")[0]["id"] == entry["id"]
        alternative = "library/imports/恢复的参考.txt"
        assert restore(http, entry, "library/../other.txt").status_code == 422
        result = restore(http, entry, alternative)
        assert result.is_success, result.text
        assert old.read_text(encoding="utf-8") == "另一份内容"
        assert read(http, f"/api/files/{identity}")["path"] == alternative


def test_stale_delete_does_not_move_newer_content_and_external_removal_is_not_trash(tmp_path):
    with app(tmp_path) as http:
        identity = import_file(http)
        endpoint = f"/api/files/{identity}/trash"
        first = read(http, endpoint)
        path = tmp_path / first["path"]
        path.write_text("外部刚编辑的文件", encoding="utf-8")
        stale = http.post(endpoint, json={"path": first["path"], "revision": first["revision"]})
        assert stale.status_code == 409
        assert path.read_text(encoding="utf-8") == "外部刚编辑的文件"
        path.unlink()
        assert read(http, "/api/files/trash") == []
        assert read(http, f"/api/files/{identity}")["status"] == "missing"


def test_manually_purged_note_keeps_link_learning_record_and_history(tmp_path):
    with app(tmp_path) as http:
        created = http.post("/api/projects", json={
            "id": "10000000-0000-4000-8000-000000000001", "topic": "SQLite", "outcome": "删除笔记时保留学习记录",
        }).json()
        assert prepare_one(tmp_path)
        lesson = created["lessons"][0]["id"]
        lesson_path = f"/api/lessons/{lesson}"
        http.put(lesson_path + "/learned", json={"learned": True}).raise_for_status()
        http.put(lesson_path + "/note", json={"content": "笔记的原件和版本"}).raise_for_status()
        review = read(http, "/api/reviews")
        file_id = f"note-{lesson}"
        entry = move_to_trash(http, file_id)
        assert entry["kind"] == "note" and entry["projectId"] == created["id"]
        assert http.put(lesson_path + "/note", json={"content": "不应重新创建原路径"}).status_code == 409
        assert http.post(f"/api/files/trash/{entry['id']}/purge", json={"revision": "a" * 64}).status_code == 409
        assert (tmp_path / entry["archivePath"]).exists()
        assert http.post(f"/api/files/trash/{entry['id']}/purge", json={"revision": entry["revision"]}).is_success
        assert not (tmp_path / entry["archivePath"]).exists()
        assert read(http, "/api/files/trash") == []
        assert read(http, f"/api/files/{file_id}")["status"] == "purged"
        assert read(http, lesson_path)["project"]["lessons"][0]["learnedOn"] is not None
        assert read(http, "/api/reviews") == review
        assert read(http, f"{lesson_path}/note/versions/{entry['revision']}")["content"] == "笔记的原件和版本"


def test_restart_reconciles_interrupted_trash_and_restore(tmp_path, monkeypatch):
    with app(tmp_path, raise_server_exceptions=False) as http:
        identity = import_file(http)
        endpoint = f"/api/files/{identity}/trash"
        before = read(http, endpoint)
        original_rename = os.rename

        def stop_after_move(source, target, *args, **kwargs):
            original_rename(source, target, *args, **kwargs)
            if ".trash" in Path(target).parts:
                raise RuntimeError("interruption after archive move")

        with monkeypatch.context() as fault:
            fault.setattr(os, "rename", stop_after_move)
            response = http.post(endpoint, json={"path": before["path"], "revision": before["revision"]})
        assert response.status_code == 500

    with app(tmp_path, raise_server_exceptions=False) as http:
        entry, = read(http, "/api/files/trash")
        assert entry["state"] == "trashed" and entry["fileId"] == identity and entry["available"]
        payload_path = tmp_path / entry["archivePath"]
        original_unlink = Path.unlink

        def stop_after_removal(path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            if path == payload_path:
                raise RuntimeError("interruption after restore publication")

        with monkeypatch.context() as fault:
            fault.setattr(Path, "unlink", stop_after_removal)
            assert restore(http, entry).status_code == 500
        assert not payload_path.exists() and (tmp_path / before["path"]).exists()

    with app(tmp_path) as http:
        assert read(http, "/api/files/trash") == []
        restored = read(http, f"/api/files/{identity}", revision=before["revision"], at=2)
        assert restored["path"] == before["path"] and restored["anchor"]["number"] == 2


def test_edit_just_before_rename_is_returned_to_original_path(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        identity = import_file(http)
        endpoint = f"/api/files/{identity}/trash"
        before = read(http, endpoint)
        path = tmp_path / before["path"]
        original_rename = os.rename

        def edit_on_move(source, target, *args, **kwargs):
            if source == path:
                path.write_text("几乎同时发生的编辑", encoding="utf-8")
            return original_rename(source, target, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(os, "rename", edit_on_move)
            moved = http.post(endpoint, json={"path": before["path"], "revision": before["revision"]})
        assert moved.status_code == 409
        assert path.read_text(encoding="utf-8") == "几乎同时发生的编辑"
        assert read(http, "/api/files/trash") == []


def test_purge_keeps_an_external_edit_made_during_capture(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        entry = move_to_trash(http, import_file(http))
        archived = tmp_path / entry["archivePath"]
        prior = os.rename

        def edit_on_capture(source, target, *args, **kwargs):
            if source == archived:
                archived.write_text("刚写入回收站原件的内容", encoding="utf-8")
            return prior(source, target, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(os, "rename", edit_on_capture)
            result = http.post(f"/api/files/trash/{entry['id']}/purge", json={"revision": entry["revision"]})
        assert result.status_code == 409
        assert archived.read_text(encoding="utf-8") == "刚写入回收站原件的内容"
        assert read(http, "/api/files/trash")[0]["state"] == "trashed"


def test_failed_move_and_restore_keep_the_original_visible_at_its_actual_location(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        file_id = import_file(http)
        endpoint = f"/api/files/{file_id}"
        preview = read(http, endpoint + "/trash")

        def deny(*args, **kwargs):
            raise PermissionError("simulated busy file")

        with monkeypatch.context() as fault:
            fault.setattr(os, "rename", deny)
            failed = http.post(endpoint + "/trash", json={"path": preview["path"], "revision": preview["revision"]})
        assert failed.status_code == 409
        assert read(http, endpoint)["status"] == "ready"
        assert read(http, "/api/files/trash") == []

        entry = move_to_trash(http, file_id)
        with monkeypatch.context() as fault:
            fault.setattr(os, "link", deny)
            assert restore(http, entry).status_code == 409
        assert read(http, endpoint)["status"] == "trashed"
        assert read(http, "/api/files/trash")[0]["available"]
        assert restore(http, entry).is_success
