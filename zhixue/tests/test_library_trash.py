from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

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


def test_trash_previews_actual_wiki_references_with_bounded_sources(tmp_path):
    with app(tmp_path) as http:
        target = import_file(http, "独立原件.txt", "原件内容")
        unrelated = import_file(http, "未被引用.txt", "无引用原件")
        with_links = import_file(http, "资料01.md", f"[[{target}|指定原件]] [[独立原件]]\n[[独立原件]]")
        extra = [import_file(http, f"资料{number:02d}.md", f"资料 {number} [[{target}]]") for number in range(2, 8)]
        original = tmp_path / read(http, f"/api/files/{target}")["path"]
        original.write_text(f"自引用 [[{target}]]", encoding="utf-8")

        preview = read(http, f"/api/files/{target}/trash")
        impact = preview["wikiImpact"]
        assert impact["sourceCount"] == 7
        assert len(impact["sources"]) == 5 and impact["sources"][0]["id"] == with_links
        assert impact["sources"][0]["lines"] == 2
        assert all(item["id"] != target and item["path"].startswith("library/") for item in impact["sources"])
        assert original.read_text(encoding="utf-8") == f"自引用 [[{target}]]"

        linked_source = tmp_path / read(http, f"/api/files/{with_links}")["path"]
        linked_source.write_text("外部删除了链接。", encoding="utf-8")
        move_to_trash(http, extra[0])
        requested = http.post("/api/files/trash/preview", json={"ids": [target, "missing-file", unrelated]})
        assert requested.is_success, requested.text
        first, missing, last = requested.json()["entries"]
        assert [first["id"], missing["id"], last["id"]] == [target, "missing-file", unrelated]
        assert first["preview"]["wikiImpact"]["sourceCount"] == 5
        assert len(first["preview"]["wikiImpact"]["sources"]) == 5
        assert not {with_links, extra[0]} & {item["id"] for item in first["preview"]["wikiImpact"]["sources"]}
        assert "找不到" in missing["error"] and "preview" not in missing
        assert last["preview"]["wikiImpact"] == {"sourceCount": 0, "sources": []}

        trashed = move_to_trash(http, target)
        assert read(http, "/api/files/wiki/resolve", target=target)["choices"][0]["status"] == "trashed"
        assert restore(http, trashed).is_success
        assert read(http, f"/api/files/{target}/trash")["wikiImpact"] == first["preview"]["wikiImpact"]


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


@pytest.mark.parametrize("legacy_archive", [False, True])
def test_relocated_trash_restore_keeps_old_wiki_paths_and_avoids_silent_retarget(tmp_path, legacy_archive):
    with app(tmp_path) as http:
        identity = import_file(http, "起始材料.md", "# 独立原件")
        url = f"/api/files/{identity}"
        original = read(http, url)
        old_paths = [original["path"], original["path"].removeprefix("library/")]
        link = import_file(http, "链接材料.md", "\n\n".join(f"[[{target}]]" for target in old_paths + ["起始材料"]))
        for target in old_paths + ["起始材料"]:
            assert read(http, "/api/files/wiki/resolve", target=target)["choices"][0]["id"] == identity
        entry = move_to_trash(http, identity)
        during_trash = read(http, "/api/files/wiki/resolve", target=old_paths[0])
        assert during_trash["state"] == "resolved"
        assert [(x["id"], x["status"]) for x in during_trash["choices"]] == [(identity, "trashed")]
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as db, db:
            assert db.execute("SELECT COUNT(*) FROM library_wiki_names WHERE file_id=?", (identity,)).fetchone() == (0,)
            if legacy_archive:
                db.execute("DELETE FROM library_wiki_aliases WHERE file_id=?", (identity,))

    restored_path = "library/imports/恢复后的起始材料.md"
    with app(tmp_path) as http:
        assert restore(http, entry, restored_path).is_success
        assert http.post("/api/files/reindex").is_success
        expected_targets = old_paths if legacy_archive else old_paths + ["起始材料"]
        for target in expected_targets:
            resolution = read(http, "/api/files/wiki/resolve", target=target)
            assert resolution["state"] == "resolved", (target, resolution)
            assert [(row["id"], row["path"]) for row in resolution["choices"]] == [(identity, restored_path)]
        incoming = read(http, url + "/backlinks")
        assert [(row["id"], row["at"]) for row in incoming] == [(link, index * 2 + 1) for index in range(len(expected_targets))]
        anchor = read(http, url, at=1, revision=original["revision"])
        assert anchor["anchor"]["text"] == "# 独立原件"

        replacement = import_file(http, "原位置的另一份材料.md", "# 另一份文档")
        other = read(http, f"/api/files/{replacement}")
        (tmp_path / other["path"]).rename(tmp_path / old_paths[0])
        conflict = read(http, "/api/files/wiki/resolve", target=old_paths[0])
        assert conflict["state"] == "ambiguous"
        assert {row["id"] for row in conflict["choices"]} == {identity, replacement}
        assert {row["at"] for row in read(http, url + "/backlinks")} == ({5} if not legacy_archive else set())


def test_upgrade_restores_old_path_alias_for_an_already_restored_file(tmp_path):
    with app(tmp_path) as http:
        identity = import_file(http, "旧版恢复样本.md", "# 已保存的原文")
        original_path = "library/imports/旧目录/学案.md"
        original_file = tmp_path / original_path
        original_file.parent.mkdir()
        tracked = read(http, f"/api/files/{identity}")["path"]
        (tmp_path / tracked).rename(original_file)
        read(http, "/api/files/wiki/resolve", target=original_path)
        entry = move_to_trash(http, identity)
        restored_path = "library/imports/已恢复学案.md"
        assert restore(http, entry, restored_path).is_success
        assert read(http, f"/api/files/{identity}")["path"] == restored_path

    # Old versions left no path alias after the original document left the index.
    with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as db, db:
        db.execute("DELETE FROM library_wiki_aliases WHERE file_id=?", (identity,))
        for key in (original_path, original_path.removeprefix("library/")):
            db.execute("DELETE FROM library_wiki_names WHERE file_id=? AND key=?", (identity, key))
        db.execute("DROP INDEX ix_library_wiki_aliases_key")
        db.execute("UPDATE alembic_version SET version_num='0064_ocr_history'")

    with app(tmp_path) as http:
        choices = read(http, "/api/files/wiki/resolve", target=original_path)["choices"]
        assert [(item["id"], item["path"]) for item in choices] == [(identity, restored_path)]
        assert read(http, f"/api/files/{identity}", at=1, revision=entry["revision"])["anchor"]["text"] == "# 已保存的原文"



def test_archived_wiki_paths_keep_reference_state_and_do_not_silently_retarget(tmp_path):
    with app(tmp_path) as http:
        original = import_file(http, "需追踪原件.md", "# 原件")
        route = f"/api/files/{original}"
        old_path = read(http, route)["path"]
        relative = old_path.removeprefix("library/")
        link_source = import_file(http, "原件的路径双链.md", f"[[{old_path}]]\n\n[[{relative}]]")
        before = read(http, route + "/backlinks")
        assert [(x["id"], x["at"]) for x in before] == [(link_source, 1), (link_source, 3)]
        entry = move_to_trash(http, original)
        http.post("/api/files/reindex").raise_for_status()
        assert original not in {x["id"] for x in read(http, "/api/files")}
        assert original not in {x["id"] for x in read(http, "/api/files/wiki/suggestions", q="需追踪原件")}
        for key in (old_path, relative, original, "需追踪原件"):
            result = read(http, "/api/files/wiki/resolve", target=key)
            assert result["state"] == "resolved"
            assert [(row["id"], row["status"]) for row in result["choices"]] == [(original, "trashed")]
        assert read(http, route + "/backlinks") == before
        replacement = import_file(http, "另一份独立资料.md", "# 后来占用路径")
        replacement_path = read(http, f"/api/files/{replacement}")["path"]
        (tmp_path / replacement_path).rename(tmp_path / old_path)
        for key in (old_path, relative):
            result = read(http, "/api/files/wiki/resolve", target=key)
            assert result["state"] == "ambiguous"
            assert {(row["id"], row["status"]) for row in result["choices"]} == {
                (original, "trashed"), (replacement, "ready"),
            }
        assert read(http, f"/api/files/{replacement}/backlinks") == []
        assert read(http, f"/api/files/{replacement}/trash")["wikiImpact"]["sourceCount"] == 0
        http.post(f"/api/files/trash/{entry['id']}/purge", json={"revision": entry["revision"]}).raise_for_status()
        result = read(http, "/api/files/wiki/resolve", target=old_path)
        assert result["state"] == "ambiguous"
        assert {(row["id"], row["status"]) for row in result["choices"]} == {
            (original, "purged"), (replacement, "ready"),
        }
        assert read(http, "/api/files/trash") == []
        assert read(http, "/api/files/wiki/resolve", target=original)["choices"][0]["status"] == "purged"
        move_to_trash(http, replacement)
        result = read(http, "/api/files/wiki/resolve", target=old_path)
        assert result["state"] == "ambiguous"
        assert {(row["id"], row["status"]) for row in result["choices"]} == {
            (original, "purged"), (replacement, "trashed"),
        }


@pytest.mark.parametrize("purged", [False, True])
def test_upgrade_legacy_trashed_or_purged_without_inventing_saved_names(tmp_path, purged):
    with app(tmp_path) as http:
        identity = import_file(http, "旧版标题.md", "# 数据迁移")
        route = f"/api/files/{identity}"
        path = "library/imports/旧路径 Cafe\u0301.md"
        (tmp_path / read(http, route)["path"]).rename(tmp_path / path)
        entry = move_to_trash(http, identity)
        if purged:
            http.post(f"/api/files/trash/{entry['id']}/purge", json={"revision": entry["revision"]}).raise_for_status()
        http.post("/api/files/reindex").raise_for_status()

    with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as db, db:
        db.execute("DELETE FROM library_wiki_aliases WHERE file_id=?", (identity,))
        db.execute("DROP INDEX IF EXISTS ix_library_wiki_aliases_key")
        db.execute("UPDATE alembic_version SET version_num='0065_trash_wiki_paths'")

    with app(tmp_path) as http:
        status = "purged" if purged else "trashed"
        for key in ("library/imports/旧路径 CAFÉ.md", "imports/旧路径 CAFÉ.md"):
            result = read(http, "/api/files/wiki/resolve", target=key)
            assert result["state"] == "resolved"
            assert [(x["id"], x["status"], x["path"]) for x in result["choices"]] == [(identity, status, path)]
        assert read(http, "/api/files/wiki/resolve", target="旧版标题") == {"state": "missing", "choices": []}
        assert read(http, "/api/files") == []


def test_restore_does_not_turn_a_previously_ambiguous_name_into_an_alias(tmp_path):
    with app(tmp_path) as http:
        original = import_file(http, "同名材料.md", "# 第一份")
        peer = import_file(http, "同名材料.md", "# 第二份")
        route = "/api/files/wiki/resolve"
        matches = read(http, route, target="同名材料")
        assert matches["state"] == "ambiguous" and {row["id"] for row in matches["choices"]} == {original, peer}
        entry = move_to_trash(http, original)
        assert restore(http, entry, "library/imports/改名后的第一份.md").is_success
        name = read(http, route, target="同名材料")
        assert name["state"] == "resolved" and [row["id"] for row in name["choices"]] == [peer]
        old_path = read(http, route, target=entry["originalPath"])
        assert old_path["state"] == "resolved" and [row["id"] for row in old_path["choices"]] == [original]


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



def test_restore_can_confirm_unchanged_bytes_with_a_new_archive_identity(tmp_path):
    with app(tmp_path) as http:
        identity = import_file(http, content="原件标识需要换机核对\n原文定位")
        detail = read(http, f"/api/files/{identity}")
        entry = move_to_trash(http, identity)
        archived = tmp_path / entry["archivePath"]
        original = archived.read_bytes()
        candidate = archived.with_name("copy.txt")

        def replace_archive(content):
            candidate.write_bytes(content)
            candidate.replace(archived)

        def attempt(confirmation=None, *, path=entry["originalPath"]):
            body = {"revision": entry["revision"], "path": path}
            if confirmation is not None:
                body["confirmation"] = confirmation
            return http.post(f"/api/files/trash/{entry['id']}/restore", json=body)

        def begin_review():
            response = attempt()
            assert response.status_code == 409
            assert response.json()["code"] == "archive_identity"
            return response.json()["confirmation"]

        replace_archive(original)
        confirmation = begin_review()
        assert archived.read_bytes() == original
        assert read(http, f"/api/files/{identity}")["status"] == "trashed"
        assert attempt(confirmation, path="library/imports/其他位置.txt").status_code == 409

        purge = http.post(f"/api/files/trash/{entry['id']}/purge", json={"revision": entry["revision"]})
        assert purge.status_code == 409 and archived.read_bytes() == original
        replace_archive(b"different content")
        changed = attempt(confirmation)
        assert changed.status_code == 409 and "confirmation" not in changed.json()
        assert archived.read_bytes() == b"different content"

        replace_archive(original)
        stale = attempt(confirmation)
        assert stale.status_code == 409 and "confirmation" not in stale.json()
        assert not (tmp_path / detail["path"]).exists()
        new_confirmation = begin_review()
        assert new_confirmation != confirmation
        restored = attempt(new_confirmation)
        assert restored.is_success, restored.text
        assert attempt(new_confirmation).is_success
        assert not archived.exists()
        assert (tmp_path / detail["path"]).read_bytes() == original
        result = read(http, f"/api/files/{identity}", at=2, revision=detail["revision"])
        assert result["id"] == identity and result["revision"] == detail["revision"]
        assert result["anchor"]["text"] == "原文定位"


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
        assert read(http, "/api/files") == []
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as db:
            names = db.execute("SELECT key FROM library_wiki_names WHERE file_id=?", (identity,)).fetchall()
            aliases = db.execute("SELECT key FROM library_wiki_aliases WHERE file_id=?", (identity,)).fetchall()
        assert not names and ("参考",) in aliases

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
