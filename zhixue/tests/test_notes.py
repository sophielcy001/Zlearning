from contextlib import closing, contextmanager
import os
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.db import ROOT, open_db
from backend.worker import prepare_one


@contextmanager
def app(folder, **options):
    with TestClient(create_app(folder, "note-test"), base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer note-test"}, **options) as http:
        yield http


def create_note(http, folder):
    project = http.post("/api/projects", json={
        "id": "10000000-0000-4000-8000-000000000001", "topic": "SQLite", "outcome": "确认笔记版本",
    }).json()
    assert prepare_one(folder)
    lesson = project["lessons"][0]["id"]
    path = folder / "library" / "notes" / f"{lesson}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# Note\r\nBaseline\r\n")
    return f"/api/lessons/{lesson}/note", path


def read(http, endpoint):
    result = http.get(endpoint)
    assert result.status_code == 200
    return result.json()


def save(http, endpoint, revision, content):
    return http.put(endpoint, json={"baseRevision": revision, "content": content})


def conflict_url(endpoint, response):
    assert response.status_code == 409, response.text
    return f"{endpoint}/conflicts/{response.json()['conflictId']}"


def resolve(http, conflict, current, action, **extra):
    preview = read(http, conflict)
    data = {"currentRevision": current, "action": action, "draftRevision": preview["draft"]["revision"],
            "externalRevision": preview["external"]["revision"] if preview["external"] else None}
    return http.post(conflict, json={**data, **extra})


def test_external_edit_conflict_survives_restart_and_merge_updates_index(tmp_path):
    with app(tmp_path) as http:
        endpoint, path = create_note(http, tmp_path)
        original = read(http, endpoint)
        assert original["content"] == "# Note\r\nBaseline\r\n"
        path.write_bytes(b"# Note\r\nExternal reference\r\n")
        conflict = conflict_url(endpoint, save(http, endpoint, original["revision"], "# Note\nApp draft\n"))
        assert path.read_bytes() == b"# Note\r\nExternal reference\r\n"
        assert conflict_url(endpoint, save(http, endpoint, original["revision"], "# Note\nApp draft\n")) == conflict

    with app(tmp_path) as http:
        assert read(http, endpoint)["conflicts"][0]["id"] == conflict.rsplit("/", 1)[-1]
        preview = read(http, conflict)
        assert preview["base"]["content"] == original["content"]
        assert "External reference" in preview["current"]["content"]
        assert "App draft" in preview["draft"]["content"]
        assert any(line.startswith("-External reference") for line in preview["diff"])
        assert any(line.startswith("+App draft") for line in preview["diff"])
        external_hit = http.get("/api/files/search", params={"q": "External reference"}).json()["hits"][0]

        path.write_bytes(b"# Note\nExternal second revision\n")
        stale = resolve(http, conflict, preview["current"]["revision"], "merge", content="Merge in progress")
        assert stale.status_code == 409
        assert path.read_bytes() == b"# Note\nExternal second revision\n"
    with app(tmp_path) as http:
        latest = read(http, conflict)
        assert latest["draft"]["content"] == "Merge in progress"
        result = resolve(http, conflict, latest["current"]["revision"], "merge", content="# Note\nCombined reference\n")
        assert result.status_code == 200
        assert path.read_bytes() == b"# Note\nCombined reference\n"
        assert read(http, endpoint)["conflicts"] == []
        current_hit, = http.get("/api/files/search", params={"q": "Combined reference"}).json()["hits"]
        assert current_hit["id"] == external_hit["id"]
        assert current_hit["revision"] == result.json()["revision"]
        stale_link = http.get(f"/api/files/{external_hit['id']}", params={"revision": external_hit["revision"], "at": 2})
        assert stale_link.status_code == 409
        assert read(http, f"{endpoint}/versions/{original['revision']}")["content"] == original["content"]


def test_external_removal_remains_missing_until_explicit_resolution(tmp_path):
    with app(tmp_path) as http:
        endpoint, path = create_note(http, tmp_path)
        original = read(http, endpoint)
        path.unlink()
        conflict = conflict_url(endpoint, save(http, endpoint, original["revision"], "Unsaved draft"))
        assert not path.exists()
        preview = read(http, conflict)
        assert preview["current"] is None and preview["draft"]["content"] == "Unsaved draft"
        result = resolve(http, conflict, None, "keep")
        assert result.status_code == 200 and result.json()["revision"] is None
        assert not path.exists()
        item = next(file for file in http.get("/api/files").json() if file["id"] == f"note-{path.stem}")
        assert item["status"] == "missing"
        resource = read(http, endpoint)
        assert resource["conflicts"] == []
        assert {original["revision"], preview["draft"]["revision"]} <= {version["revision"] for version in resource["versions"]}


def test_stale_preview_cannot_select_a_different_draft_from_another_editor(tmp_path):
    with app(tmp_path) as http:
        endpoint, path = create_note(http, tmp_path)
        original = read(http, endpoint)
        path.write_bytes(b"External edit")
        conflict = conflict_url(endpoint, save(http, endpoint, original["revision"], "App copy"))
        previous = read(http, conflict)
        assert resolve(http, conflict, original["revision"], "merge", content="Alternative merge").status_code == 409

        stale = resolve(http, conflict, previous["current"]["revision"], "draft", draftRevision=previous["draft"]["revision"])
        assert stale.status_code == 409
        assert path.read_bytes() == b"External edit"
        latest = read(http, conflict)
        assert latest["draft"]["content"] == "Alternative merge"


def test_file_changed_at_displacement_is_preserved(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        endpoint, path = create_note(http, tmp_path)
        current = read(http, endpoint)
        original_rename = os.rename

        def edit_at_rename(source, destination, *args, **kwargs):
            if source == path:
                path.write_bytes(b"Changed as the app was writing")
            return original_rename(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "rename", edit_at_rename)
        conflict = conflict_url(endpoint, save(http, endpoint, current["revision"], "App copy"))
        assert path.read_bytes() == b"Changed as the app was writing"
        info = read(http, conflict)
        assert info["current"]["content"] == "Changed as the app was writing"
        assert info["draft"]["content"] == "App copy"


def test_interrupt_after_capture_keeps_both_versions_for_recovery(tmp_path, monkeypatch):
    with app(tmp_path, raise_server_exceptions=False) as http:
        endpoint, path = create_note(http, tmp_path)
        version = read(http, endpoint)
        original_link = os.link

        def interrupt_before_publish(source, destination, *args, **kwargs):
            if destination == path:
                raise RuntimeError("simulated interruption before publication")
            return original_link(source, destination, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(os, "link", interrupt_before_publish)
            response = save(http, endpoint, version["revision"], "App candidate")
            assert response.status_code == 500
        assert not path.exists()

    with app(tmp_path) as http:
        resource = read(http, endpoint)
        [pending] = resource["conflicts"]
        assert pending["kind"] == "interrupted"
        conflict = f"{endpoint}/conflicts/{pending['id']}"
        diff = read(http, conflict)
        assert diff["external"]["content"] == version["content"]
        assert diff["draft"]["content"] == "App candidate"
        assert resolve(http, conflict, None, "external").status_code == 200
        assert path.read_bytes() == version["content"].encode()


def test_replacement_created_after_capture_is_never_replaced(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        endpoint, path = create_note(http, tmp_path)
        current = read(http, endpoint)
        original_link = os.link

        def create_at_publish(source, destination, *args, **kwargs):
            if destination == path:
                path.write_bytes(b"New version at the original path")
            return original_link(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "link", create_at_publish)
        conflict = conflict_url(endpoint, save(http, endpoint, current["revision"], "App candidate"))
        assert path.read_bytes() == b"New version at the original path"
        assert read(http, conflict)["draft"]["content"] == "App candidate"


def test_interrupt_after_publication_recognizes_the_new_current_version(tmp_path, monkeypatch):
    with app(tmp_path, raise_server_exceptions=False) as http:
        endpoint, path = create_note(http, tmp_path)
        version = read(http, endpoint)

        def interrupt_after_link(*args):
            raise RuntimeError("simulated interruption after publication")

        with monkeypatch.context() as fault:
            fault.setattr("backend.notes.accept_write", interrupt_after_link)
            assert save(http, endpoint, version["revision"], "Already published").status_code == 500
        assert path.read_bytes() == b"Already published"

    with app(tmp_path) as http:
        resource = read(http, endpoint)
        assert resource["content"] == "Already published"
        assert resource["conflicts"] == []
        assert read(http, f"{endpoint}/versions/{version['revision']}")["content"] == version["content"]


@pytest.mark.skipif(os.name == "nt", reason="A Windows editor may hold a handle that disallows renaming")
def test_external_writer_on_an_open_handle_keeps_its_tail_edit(tmp_path):
    with app(tmp_path) as http:
        endpoint, path = create_note(http, tmp_path)
        current = read(http, endpoint)
        with path.open("r+b") as external:
            assert save(http, endpoint, current["revision"], "App saved").status_code == 200
            external.seek(0)
            external.write(b"Late external edit")
            external.truncate()
        assert path.read_bytes() == b"App saved"
        [detected] = read(http, endpoint)["conflicts"]
        assert detected["kind"] == "detached"
        conflict = f"{endpoint}/conflicts/{detected['id']}"
        versions = read(http, conflict)
        assert versions["external"]["content"] == "Late external edit"
        assert versions["draft"]["content"] == "App saved"
        assert resolve(http, conflict, versions["current"]["revision"], "external").status_code == 200
        assert path.read_bytes() == b"Late external edit"


@pytest.mark.skipif(os.name != "nt", reason="Requires a Windows sharing violation")
def test_windows_editor_blocks_rename_and_keeps_the_draft_ready_to_merge(tmp_path):
    editor_text = "Late edit from an editor keeping its Windows handle open"
    draft_text = "Draft from the app while the file was held"
    merged_text = "Combined note after the Windows editor has released the file"
    with app(tmp_path) as http:
        endpoint, path = create_note(http, tmp_path)
        original = read(http, endpoint)
        with path.open("r+b") as editor:
            response = save(http, endpoint, original["revision"], draft_text)
            assert "conflictId" in response.json(), response.text
            conflict = conflict_url(endpoint, response)
            preview = read(http, conflict)
            assert preview["kind"] == "interrupted"
            assert preview["current"]["content"] == preview["external"]["content"] == original["content"]
            assert preview["draft"]["content"] == draft_text
            assert conflict_url(endpoint, save(http, endpoint, original["revision"], draft_text)) == conflict
            assert path.read_bytes() == original["content"].encode("utf-8")
            editor.seek(0)
            editor.write(editor_text.encode("utf-8"))
            editor.truncate()
        assert path.read_bytes() == editor_text.encode("utf-8")

    with app(tmp_path) as http:
        note = read(http, endpoint)
        assert [entry["id"] for entry in note["conflicts"]] == [conflict.rsplit("/", 1)[-1]]
        preview = read(http, conflict)
        assert preview["current"]["content"] == editor_text
        assert preview["draft"]["content"] == draft_text
        assert "-" + editor_text in preview["diff"] and "+" + draft_text in preview["diff"]
        result = resolve(http, conflict, preview["current"]["revision"], "merge", content=merged_text)
        assert result.status_code == 200
        assert path.read_bytes() == merged_text.encode("utf-8")
        assert read(http, endpoint)["conflicts"] == []
        [hit] = http.get("/api/files/search", params={"q": merged_text}).json()["hits"]
        assert hit["id"] == f"note-{path.stem}" and hit["revision"] == result.json()["revision"]


def test_shared_note_keeps_one_file_and_indexes_every_associated_project(tmp_path):
    with app(tmp_path) as http:
        first = http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "SQLite 实践", "outcome": "练习查询",
        }).json()
        second = http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "数据建模", "outcome": "设计字段",
        }).json()
        while prepare_one(tmp_path):
            pass
        first_lesson = first["lessons"][0]["id"]
        second_lesson = second["lessons"][0]["id"]
        shared = str(uuid4())
        create = {"id": shared, "title": "查询练习", "lessonIds": []}
        response = http.post("/api/notes", json=create)
        assert response.status_code == 201, response.text
        assert http.post("/api/notes", json=create).json() == response.json()
        path = tmp_path / "library" / "notes" / f"{shared}.md"
        assert path.read_bytes() == b""
        document = f"/api/files/note-{shared}"
        endpoint = f"/api/notes/{shared}"
        initial = read(http, endpoint)
        assert read(http, document)["projectIds"] == []
        text = "跨项目共享的唯一检索句"
        saved = save(http, endpoint, initial["revision"], text)
        assert saved.is_success
        base = saved.json()["revision"]

        metadata = {"title": "共同查询练习", "lessonIds": [first_lesson, second_lesson, first_lesson], "metadataRevision": 1}
        attached = http.put(endpoint + "/details", json=metadata)
        assert attached.is_success, attached.text
        assert {item["id"] for item in attached.json()["lessons"]} == {first_lesson, second_lesson}
        assert http.put(endpoint + "/details", json={**metadata, "lessonIds": []}).status_code == 409
        assert read(http, endpoint)["revision"] == base

        another = str(uuid4())
        assert http.post("/api/notes", json={"id": another, "title": "个人记录", "lessonIds": [first_lesson]}).is_success
        assert {item["id"] for item in read(http, f"/api/lessons/{first_lesson}/notes")} == {shared, another}
        assert [item["id"] for item in read(http, f"/api/lessons/{second_lesson}/notes")] == [shared]
        for project in (first, second):
            hits = http.get("/api/files/search", params={"q": text, "kind": "note", "project": project["id"]}).json()["hits"]
            assert [hit["id"] for hit in hits] == [f"note-{shared}"]
            assert set(hits[0]["projectIds"]) == {first["id"], second["id"]}
        assert set(path.parent.glob("*.md")) == {path, path.with_name(f"{another}.md")}

        moved = path.with_name("共同查询练习.md")
        path.rename(moved)
        assert read(http, document)["path"].endswith("/共同查询练习.md")
        moved.write_text("外部编辑的一版", encoding="utf-8")
        conflict = conflict_url(endpoint, save(http, endpoint, base, "应用草稿"))
        preview = read(http, conflict)
        assert preview["current"]["content"] == "外部编辑的一版" and preview["draft"]["content"] == "应用草稿"
        assert resolve(http, conflict, preview["current"]["revision"], "merge", content="同一文件合并后的版本").is_success
        assert not path.exists() and moved.read_text(encoding="utf-8") == "同一文件合并后的版本"

        detached = http.put(endpoint + "/details", json={"title": metadata["title"], "lessonIds": [], "metadataRevision": 2})
        assert detached.is_success, detached.text
        assert read(http, document)["projectIds"] == []
        assert read(http, endpoint)["content"] == "同一文件合并后的版本"
        assert read(http, f"/api/lessons/{first_lesson}/notes") == [{"id": another, "title": "个人记录"}]


def test_upgrade_preserves_legacy_note_identity_moved_path_and_conflict(tmp_path):
    with app(tmp_path) as http:
        endpoint, original = create_note(http, tmp_path)
        identity = endpoint.split("/")[-2]
        revision = read(http, endpoint)["revision"]
        moved = original.with_name("旧笔记外部改名.md")
        original.rename(moved)
        document = f"/api/files/note-{identity}"
        assert read(http, document)["path"].endswith("/旧笔记外部改名.md")
        moved.write_text("保存前的新外部文本", encoding="utf-8")
        old_conflict = conflict_url(endpoint, save(http, endpoint, revision, "旧版待合并的草稿"))
        conflict_id = old_conflict.split("/")[-1]

    engine = open_db(tmp_path)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0029_web_snapshots")
            row = connection.exec_driver_sql("SELECT lesson_id, draft_revision FROM note_conflicts WHERE id = ?", (conflict_id,)).one()
            assert row[0] == identity
    finally:
        engine.dispose()

    with app(tmp_path) as http:
        new_endpoint = f"/api/notes/{identity}"
        current = read(http, new_endpoint)
        assert current == read(http, endpoint)
        assert current["conflicts"][0]["id"] == conflict_id
        preview = read(http, new_endpoint + "/conflicts/" + conflict_id)
        assert preview["base"]["revision"] == revision
        assert preview["draft"]["content"] == "旧版待合并的草稿"
        assert preview["current"]["content"] == "保存前的新外部文本"
        assert read(http, new_endpoint + "/details")["lessons"][0]["id"] == identity
        assert read(http, document)["path"].endswith("/旧笔记外部改名.md")
        archived = read(http, new_endpoint + "/versions/" + revision)
        assert archived["content"] == "# Note\r\nBaseline\r\n"
        assert resolve(http, new_endpoint + "/conflicts/" + conflict_id, current["revision"], "draft").is_success
        assert moved.read_text(encoding="utf-8") == "旧版待合并的草稿"
        assert not original.exists()
    [backup] = (tmp_path / "backups" / "database-upgrades").glob("*.sqlite3")
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0029_web_snapshots",)
