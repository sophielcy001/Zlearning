from contextlib import contextmanager
import os
from shutil import copyfile
import sqlite3

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import ROOT, open_db
from backend.worker import prepare_one


@contextmanager
def app(folder):
    with TestClient(create_app(folder, "location-test"), base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer location-test"}) as http:
        yield http


def upload(http, name, source):
    result = http.post("/api/files/upload", params={"name": name}, content=source)
    assert result.status_code == 201
    return result.json()["id"]


def get(http, url, **params):
    response = http.get(url, params=params)
    assert response.is_success, response.text
    return response.json()


def move(source, folder, name):
    destination = folder / "library" / "整理" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    return destination


def test_move_after_import_before_first_index_keeps_id_for_download_and_duplicates(tmp_path):
    source = "文件位置改变之后的唯一内容\n第二行可以定位".encode()
    with app(tmp_path) as http:
        identity = upload(http, "参考.txt", source)
        old = tmp_path / "library" / "imports" / f"{identity}.txt"
        moved = move(old, tmp_path, "改名后的参考.txt")
        duplicate = http.post("/api/files/upload", params={"name": "另一份.txt", "duplicate": "ask"}, content=source)
        assert duplicate.json()["id"] == identity and duplicate.json()["status"] == "duplicate"
    with app(tmp_path) as http:
        hit, = get(http, "/api/files/search", q="改名后的参考")["hits"]
        assert hit["id"] == identity
        assert hit["path"] == moved.relative_to(tmp_path).as_posix()
        assert http.get(f"/api/files/{identity}/original").content == source
        assert get(http, f"/api/files/{identity}", revision=hit["revision"], at=2)["anchor"]["text"] == "第二行可以定位"


def test_lesson_read_and_note_write_use_moved_files_without_recreating_old_paths(tmp_path):
    with app(tmp_path) as http:
        project = http.post("/api/projects", json={
            "id": "10000000-0000-4000-8000-000000000001", "topic": "SQLite", "outcome": "验证文件位置",
        }).json()
        assert prepare_one(tmp_path)
        lesson = project["lessons"][0]["id"]
        url = f"/api/lessons/{lesson}"
        saved = http.put(url + "/note", json={"content": "这条笔记一直使用同一文件身份"})
        assert saved.is_success
        prior = get(http, "/api/files/search", q="同一文件身份")["hits"][0]
        content = get(http, url)["body"]
        note = tmp_path / "library" / "notes" / f"{lesson}.md"
        course = tmp_path / "library" / "courses" / f"{lesson}.md"
        moved_note = move(note, tmp_path, "手动整理的笔记.md")
        move(course, tmp_path, "手动整理的课程.md")
        assert get(http, url)["body"] == content
        assert get(http, url + "/note")["revision"] == prior["revision"]
        assert get(http, f"/api/files/{prior['id']}", revision=prior["revision"], at=1)["anchor"]["number"] == 1
        result = http.put(url + "/note", json={"baseRevision": prior["revision"], "content": "位置改变之后追加内容"})
        assert result.is_success, result.text
        assert moved_note.read_text(encoding="utf-8") == "位置改变之后追加内容"
        assert not note.exists() and not course.exists()
        hit, = get(http, "/api/files/search", q="位置改变之后追加")["hits"]
        assert hit["id"] == prior["id"] and hit["path"] == "library/整理/手动整理的笔记.md"


def test_copy_at_old_note_path_during_save_does_not_receive_the_update(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        project = http.post("/api/projects", json={
            "id": "10000000-0000-4000-8000-000000000001", "topic": "SQLite", "outcome": "验证并发改名",
        }).json()
        assert prepare_one(tmp_path)
        lesson = project["lessons"][0]["id"]
        endpoint = f"/api/lessons/{lesson}/note"
        initial = http.put(endpoint, json={"content": "初始笔记版本"}).json()
        old = tmp_path / "library" / "notes" / f"{lesson}.md"
        moved = tmp_path / "library" / "其他位置.md"
        actual_rename = os.rename

        def move_and_create_copy(source, destination, *args, **kwargs):
            if source == old:
                actual_rename(old, moved)
                copyfile(moved, old)
            return actual_rename(source, destination, *args, **kwargs)

        with monkeypatch.context() as race:
            race.setattr(os, "rename", move_and_create_copy)
            response = http.put(endpoint, json={"baseRevision": initial["revision"], "content": "应用候选修改"})
        assert response.status_code == 409
        assert old.read_text(encoding="utf-8") == moved.read_text(encoding="utf-8") == "初始笔记版本"
        conflict = get(http, f"{endpoint}/conflicts/{response.json()['conflictId']}")
        assert conflict["draft"]["content"] == "应用候选修改"
        assert get(http, f"/api/files/note-{lesson}")["path"] == "library/其他位置.md"


def test_ambiguous_hardlinks_require_preview_and_remain_safe_if_candidate_changes(tmp_path):
    with app(tmp_path) as http:
        data = "待确认的原资料".encode()
        identity = upload(http, "原资料.txt", data)
        endpoint = f"/api/files/{identity}"
        original = get(http, endpoint)
        old = tmp_path / original["path"]
        primary = move(old, tmp_path, "版本一.txt")
        alternate = primary.with_name("版本二.txt")
        os.link(primary, alternate)
        record = get(http, endpoint)
        assert record["status"] == "missing"
        assert "多个" in record["message"]
        listing = get(http, endpoint + "/repair")
        assert set(listing["candidates"]) == {"library/整理/版本一.txt", "library/整理/版本二.txt"}
        payload = {"path": alternate.relative_to(tmp_path).as_posix()}
        preview = http.post(endpoint + "/repair/preview", json=payload)
        assert preview.is_success and preview.json()["sameContent"]
        alternate.write_bytes("准备确认时又编辑了候选文件".encode())
        stale = http.put(endpoint + "/repair", json={**payload, "revision": preview.json()["revision"]})
        assert stale.status_code == 409
        assert get(http, endpoint)["status"] == "missing"
        latest = http.post(endpoint + "/repair/preview", json=payload).json()
        assert not latest["sameContent"]
        repair = http.put(endpoint + "/repair", json={**payload, "revision": latest["revision"]})
        assert repair.is_success, repair.text
        assert get(http, endpoint)["path"] == payload["path"]
        assert http.get(endpoint, params={"revision": original["revision"]}).status_code == 409
        assert alternate.exists() and primary.exists()


def test_copy_and_delete_can_be_repaired_without_guessing_or_claiming_another_file(tmp_path):
    with app(tmp_path) as http:
        data = b"Unique imported copy"
        identity = upload(http, "归档.txt", data)
        occupied = upload(http, "已有同内容.txt", data + b".")
        endpoint = f"/api/files/{identity}"
        original = get(http, endpoint)
        old = tmp_path / original["path"]
        copied = old.parent / "手动转存.txt"
        copyfile(old, copied)
        old.unlink()
        assert get(http, endpoint)["status"] == "missing"
        old.write_text("另一个文件占用了原路径", encoding="utf-8")
        assert get(http, endpoint)["status"] == "missing"
        assert http.get(endpoint + "/original").status_code == 409
        other_path = get(http, f"/api/files/{occupied}")["path"]
        assert http.post(endpoint + "/repair/preview", json={"path": other_path}).status_code == 409
        assert http.post(endpoint + "/repair/preview", json={"path": "library/../zhixue.sqlite3"}).status_code == 422
        outside = tmp_path / "outside.txt"
        outside.write_text("不在知识库", encoding="utf-8")
        try:
            (old.parent / "alias.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        else:
            assert http.post(endpoint + "/repair/preview", json={"path": "library/imports/alias.txt"}).status_code == 422
        path = copied.relative_to(tmp_path).as_posix()
        preview = http.post(endpoint + "/repair/preview", json={"path": path}).json()
        assert preview["sameContent"]
        assert http.put(endpoint + "/repair", json={"path": path, "revision": preview["revision"]}).is_success
        assert get(http, endpoint, revision=original["revision"])["path"] == path
        assert old.read_text(encoding="utf-8") == "另一个文件占用了原路径"


def test_normal_atomic_edit_is_recognized_without_repair(tmp_path):
    with app(tmp_path) as http:
        identity = upload(http, "实时笔记.txt", b"Original content")
        endpoint = f"/api/files/{identity}"
        first = get(http, endpoint)
        original = tmp_path / first["path"]
        temporary = original.with_suffix(".tmp")
        temporary.write_bytes(b"Edited atomically")
        temporary.replace(original)
        edited = get(http, endpoint)
        assert edited["content"] == "Edited atomically" and edited["revision"] != first["revision"]


def test_upgrade_recovers_previously_indexed_file_moved_before_first_new_startup(tmp_path):
    with app(tmp_path) as http:
        source = b"Previously indexed file"
        identity = upload(http, "升级前资料.txt", source)
        endpoint = f"/api/files/{identity}"
        prior = get(http, endpoint)
    engine = open_db(tmp_path)
    try:
        config = Config()
        config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
        with engine.begin() as db:
            config.attributes["connection"] = db
            command.downgrade(config, "0005_note_versions")
    finally:
        engine.dispose()
    new_path = move(tmp_path / prior["path"], tmp_path, "升级之间已移动.txt")
    with app(tmp_path) as http:
        assert get(http, endpoint, revision=prior["revision"])["path"] == new_path.relative_to(tmp_path).as_posix()
        assert http.get(endpoint + "/original").content == source
    [backup] = list((tmp_path / "backups" / "database-upgrades").glob("*.sqlite3"))
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0005_note_versions",)
