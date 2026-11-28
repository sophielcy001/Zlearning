from contextlib import closing
from datetime import date
from errno import EIO
from io import BytesIO
import json
from pathlib import Path
from shutil import copyfile, copytree
import sqlite3

from alembic import command
from alembic.config import Config
from docx import Document
from fastapi.testclient import TestClient
import pymupdf
import pytest
from sqlalchemy.exc import OperationalError

from backend import db as database_module, documents
from backend.app import create_app
from backend.db import ROOT, open_db, upgrade
from backend.storage_files import sqlite_file_uri
from backend.worker import prepare_one


BASE_URL = "http://127.0.0.1"
TOKEN = "library-test-session"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def open_client(folder):
    return TestClient(create_app(folder, TOKEN, today=lambda: date(2026, 11, 13)), base_url=BASE_URL, headers=HEADERS)


def upload(http, name, content, duplicate="ask"):
    return http.post("/api/files/upload", params={"name": name, "duplicate": duplicate},
                     headers={"Content-Type": "application/octet-stream"}, content=content)


def search(http, query, **filters):
    response = http.get("/api/files/search", params={"q": query, **filters})
    response.raise_for_status()
    return response.json()


@pytest.fixture
def library(tmp_path):
    with open_client(tmp_path) as http:
        yield http, tmp_path


def test_text_copy_duplicate_choices_and_precise_chinese_hits(library):
    http, folder = library
    source = "\ufeff# 随堂资料\r\n\r\n确认复习安排，再验证 SQLite 查询结果。\r\n".encode()
    created = upload(http, "资料.md", source)
    assert created.status_code == 201
    identity = created.json()["id"]
    assert upload(http, "另一份.md", source).json() == {"status": "duplicate", "id": identity, "name": "资料.md"}
    assert upload(http, "另一份.md", source, "reuse").json()["id"] == identity
    copy = upload(http, "另一份.md", source, "copy")
    assert copy.status_code == 201 and copy.json()["id"] != identity
    assert (folder / "library" / "imports" / f"{identity}.md").read_bytes() == source
    original = http.get(f"/api/files/{identity}/original")
    assert original.content == source
    assert original.headers["content-disposition"].startswith("attachment")

    for phrase in ("复习安排", "复习", "习", "验证 sqlite 查询"):
        match = next(item for item in search(http, phrase)["hits"] if item["id"] == identity)
        assert match["location"] == {"number": 3, "label": "第 3 行"}
        assert "复习安排" in match["snippet"]
        assert match["path"].endswith(f"/{identity}.md")
        preview = http.get(f"/api/files/{identity}", params={"at": 3, "revision": match["revision"]}).json()
        assert preview["anchor"]["text"] == "确认复习安排，再验证 SQLite 查询结果。"


@pytest.mark.parametrize("query", ("复", "复习", "复习计划"))
def test_keyword_snippet_keeps_chinese_match_after_casefold_expansion(library, query):
    http, _ = library
    line = "Straße " * 90 + "现在记录中文复习计划和重点。" + " 后续正文" * 48
    imported = upload(http, "混排.txt", ("学习记录\n" + line).encode())
    assert imported.status_code == 201
    hit, = search(http, query, format="txt")["hits"]
    assert hit["id"] == imported.json()["id"]
    assert hit["location"] == {"number": 2, "label": "第 2 行"}
    assert "中文复习计划" in hit["snippet"]
    detail = http.get(f"/api/files/{hit['id']}", params={"at": 2, "revision": hit["revision"]})
    detail.raise_for_status()
    assert detail.json()["anchor"]["text"] == line


def test_pdf_search_locates_page_and_reports_image_only_page(library):
    http, folder = library
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((70, 72), "首页概览", fontname="china-s")
        pdf.new_page().insert_text((70, 72), "数据库索引实战", fontname="china-s")
        image_page = pdf.new_page()
        image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
        image.clear_with(200)
        image_page.insert_image(pymupdf.Rect(25, 25, 150, 150), pixmap=image)
        source = pdf.tobytes()

    created = upload(http, "教程.pdf", source)
    assert created.status_code == 201
    identity = created.json()["id"]
    match, = search(http, "索引实战", format="pdf")["hits"]
    assert match["location"] == {"number": 2, "label": "第 2 页"}
    page = http.get(f"/api/files/{identity}", params={"at": 2}).json()
    assert page["pageCount"] == 3 and page["status"] == "partial"
    assert page["unreadablePages"] == [3]
    assert "数据库索引实战" in page["anchor"]["text"]
    blank = http.get(f"/api/files/{identity}", params={"at": 3}).json()
    assert blank["anchor"]["text"] == ""
    assert http.get(f"/api/files/{identity}/original").content == source
    page_image = http.get(f"/api/files/{identity}/pages/2/image", params={"revision": match["revision"]})
    assert page_image.headers["content-type"] == "image/png"
    assert page_image.content.startswith(b"\x89PNG\r\n\x1a\n")
    relocated = folder / "library" / "归档教程.pdf"
    (folder / match["path"]).rename(relocated)
    moved = http.get(f"/api/files/{identity}", params={"revision": match["revision"], "at": 2}).json()
    assert moved["path"] == "library/归档教程.pdf"
    assert moved["anchor"] == page["anchor"]
    assert http.get(f"/api/files/{identity}/pages/2/image", params={"revision": match["revision"]}).content == page_image.content


def test_docx_search_preserves_body_order_and_table_location(library):
    http, _ = library
    doc = Document()
    doc.add_paragraph("第一个主题")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "任务恢复"
    table.cell(0, 1).text = "表格证据"
    doc.add_paragraph("结尾复习")
    buffer = BytesIO()
    doc.save(buffer)
    source = buffer.getvalue()
    identity = upload(http, "参考.docx", source).json()["id"]
    match, = search(http, "表格证据", format="docx")["hits"]
    assert match["location"] == {"number": 2, "label": "表 1 · 第 1 行"}
    preview = http.get(f"/api/files/{identity}", params={"at": 2}).json()
    assert preview["content"].index("第一个主题") < preview["content"].index("表格证据") < preview["content"].index("结尾复习")
    assert http.get(f"/api/files/{identity}/original").content == source


def test_search_updates_after_external_edit_and_rebuild_and_never_returns_deleted_body(library):
    http, folder = library
    identity = upload(http, "索引验证.txt", "第一行\n数据库持久化".encode()).json()["id"]
    old, = search(http, "数据库持久化")["hits"]
    path = folder / "library" / "imports" / f"{identity}.txt"
    path.write_text("改为冷启动记录\n第三轮回归", encoding="utf-8")
    assert search(http, "数据库持久化")["hits"] == []
    new, = search(http, "第三轮回归")["hits"]
    assert old["revision"] != new["revision"]
    assert http.get(f"/api/files/{identity}", params={"at": 2, "revision": old["revision"]}).status_code == 409
    http.post("/api/files/reindex").raise_for_status()
    assert search(http, "第三轮回归")["hits"][0]["id"] == identity
    path.unlink()
    assert search(http, "第三轮回归")["hits"] == []
    assert http.get(f"/api/files/{identity}").json()["status"] == "missing"


@pytest.mark.parametrize("extension", ("txt", "md"))
def test_text_preview_rejects_a_missing_line_without_falling_back_to_the_whole_file(library, extension):
    http, folder = library
    body = "第一行\n\n原来的第三行。"
    source = upload(http, f"定位样本.{extension}", body.encode()).json()
    endpoint = f"/api/files/{source['id']}"
    original = http.get(endpoint).json()
    empty_line = http.get(endpoint, params={"at": 2, "revision": original["revision"]})
    empty_line.raise_for_status()
    assert empty_line.json()["anchor"] == {"number": 2, "label": "第 2 行", "text": ""}
    missing = http.get(endpoint, params={"at": 4, "revision": original["revision"]})
    assert missing.status_code == 404
    assert "找不到文件中的这一行或段落" in missing.json()["detail"]
    assert http.get(endpoint, params={"at": 4}).status_code == 404
    assert http.get(endpoint).json()["content"] == body

    (folder / original["path"]).write_bytes("当前仅保留第一行。".encode())
    assert http.get(endpoint, params={"at": 3, "revision": original["revision"]}).status_code == 409
    current = http.get(endpoint).json()
    assert http.get(endpoint, params={"at": 3, "revision": current["revision"]}).status_code == 404
    line = http.get(endpoint, params={"at": 1, "revision": current["revision"]}).json()["anchor"]
    assert line == {"number": 1, "label": "第 1 行", "text": "当前仅保留第一行。"}


def test_course_and_note_are_indexed_with_project_and_kind_filters(library):
    http, folder = library
    project_id = "4aa47baf-b1e0-4a70-b950-3e6dfbb90395"
    created = http.post("/api/projects", json={"id": project_id, "topic": "SQLite 入门", "outcome": "查学习记录"})
    created.raise_for_status()
    while prepare_one(folder):
        pass
    lesson_id = created.json()["lessons"][0]["id"]
    http.put(f"/api/lessons/{lesson_id}/note", json={"content": "# 观察\n过滤条件帮助理解学习记录"}).raise_for_status()
    result = search(http, "学习记录", project=project_id)["hits"]
    assert {item["kind"] for item in result} == {"course", "note"}
    note, = search(http, "过滤条件", kind="note", project=project_id)["hits"]
    assert note["id"] == f"note-{lesson_id}"
    assert search(http, "过滤条件", kind="course")["hits"] == []
    assert search(http, "过滤条件", project="b9fb428d-5ae5-480b-aa2a-de169a470fe9")["hits"] == []


def test_failed_parse_keeps_original_and_can_only_match_its_name(library):
    http, _ = library
    source = b"not a PDF: bogus searchable text"
    identity = upload(http, "损坏的参考.pdf", source).json()["id"]
    detail = http.get(f"/api/files/{identity}").json()
    assert detail["status"] == "failed" and detail["message"]
    assert search(http, "bogus searchable")["hits"] == []
    match, = search(http, "损坏的参考")["hits"]
    assert match["location"] is None and match["status"] == "failed"
    assert http.get(f"/api/files/{identity}/original").content == source
    assert upload(http, "../别处.pdf", source).status_code == 422
    assert search(http, '" OR *')['hits'] == []


def create_legacy_database(folder, revision="0002_timers"):
    engine = open_db(folder)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL").close()
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        connection.exec_driver_sql("INSERT INTO demo_clock (id, offset) VALUES (1, 7)")
        connection.commit()
    return engine


def migration_state(path):
    with closing(sqlite3.connect(sqlite_file_uri(Path(path), mode="ro"), uri=True)) as db:
        return (db.execute("SELECT version_num FROM alembic_version").fetchone()[0],
                db.execute("SELECT offset FROM demo_clock WHERE id=1").fetchone()[0],
                db.execute("SELECT name FROM sqlite_master WHERE name='library_fts'").fetchone())


def test_real_previous_database_has_a_readable_pre_upgrade_backup(tmp_path):
    engine = create_legacy_database(tmp_path)
    try:
        upgrade(engine)
        upgrade(engine)
        [snapshot] = list((tmp_path / "backups" / "database-upgrades").glob("*.sqlite3"))
        assert migration_state(snapshot) == ("0002_timers", 7, None)
        assert migration_state(tmp_path / "zhixue.sqlite3") == ("0066_trash_reference_status", 7, ("library_fts",))
    finally:
        engine.dispose()


def test_failed_migration_keeps_the_old_schema_and_snapshot(tmp_path):
    engine = create_legacy_database(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE library_index_documents (conflict INT)")
        with pytest.raises(OperationalError, match="already exists"):
            upgrade(engine)
        [snapshot] = list((tmp_path / "backups" / "database-upgrades").glob("*.sqlite3"))
        assert migration_state(snapshot) == migration_state(tmp_path / "zhixue.sqlite3") == ("0002_timers", 7, None)
    finally:
        engine.dispose()


def test_upgrade_waits_for_the_snapshot_directory_to_sync(tmp_path, monkeypatch):
    old = "0051_course_anchor_links"
    folder = tmp_path / "backups" / "database-upgrades"
    state = (old, 7, ("library_fts",))
    engine = create_legacy_database(tmp_path, old)
    synced = []
    fail = True

    def sync(path):
        # The completed snapshot must be readable and the writer still on the old schema.
        [snapshot, *_] = folder.glob("*.sqlite3")
        assert migration_state(snapshot) == state
        assert migration_state(tmp_path / "zhixue.sqlite3") == state
        synced.append(path)
        if fail and path == folder.parent:
            raise OSError(EIO, "simulated directory sync failure")

    monkeypatch.setattr(database_module, "sync_directory", sync, raising=False)
    try:
        with pytest.raises(OSError, match="simulated directory sync failure"):
            upgrade(engine)
        assert synced == [folder, folder.parent]
        assert migration_state(tmp_path / "zhixue.sqlite3") == state

        fail = False
        synced.clear()
        upgrade(engine)
        assert synced == [folder, folder.parent, tmp_path]
        assert migration_state(tmp_path / "zhixue.sqlite3") == ("0066_trash_reference_status", 7, ("library_fts",))
    finally:
        engine.dispose()


def scanned_sample():
    with pymupdf.open() as original:
        page = original.new_page(width=560, height=210)
        page.insert_text((35, 65), "Zhixue Local Scan 2026", fontsize=32)
        page.insert_text((35, 140), "中文学习材料", fontsize=34, fontname="china-s")
        raster = page.get_pixmap(dpi=200)
    with pymupdf.open() as scanned:
        scanned.new_page(width=560, height=210).insert_image(pymupdf.Rect(0, 0, 560, 210), pixmap=raster)
        scanned.new_page().insert_text((40, 45), "Original searchable page")
        assert not scanned[0].get_text().strip()
        return scanned.tobytes(), raster.tobytes("png"), raster.tobytes("jpeg")


def test_ocr_worker_reads_bundled_models_under_unicode_application_path(tmp_path, monkeypatch):
    backend = tmp_path / "智学 🧪" / "backend"
    backend.mkdir(parents=True)
    copyfile(ROOT / "backend" / "__init__.py", backend / "__init__.py")
    module = backend / "documents.py"
    copyfile(ROOT / "backend" / "documents.py", module)
    copytree(ROOT / "backend" / "tessdata", backend / "tessdata")
    monkeypatch.setattr(documents, "__file__", str(module))

    scanned_pdf, _, _ = scanned_sample()
    result = json.loads(documents.run_parser(scanned_pdf, "ocr", "pdf", "1"))
    assert "Zhixue Local Scan 2026" in result["text"]
    assert "中文学习材料" in result["text"].replace(" ", "")


def test_scanned_pdf_draft_confirm_rebuild_and_versioned_reference(library):
    from backend.course_sources import SourceLocator, locate_sources, sources_current
    from backend.db import transaction

    http, folder = library
    source, _, _ = scanned_sample()
    identity = upload(http, "扫描复习.pdf", source).json()["id"]
    endpoint = f"/api/files/{identity}"
    original = http.get(endpoint).json()
    assert original["unreadablePages"] == [1]
    assert original["content"] == ""
    assert search(http, "Original searchable")["hits"][0]["location"]["number"] == 2
    ocr = f"{endpoint}/pages/1/ocr"
    revision = original["revision"]
    draft = http.post(ocr, json={"revision": revision})
    assert draft.status_code == 200, draft.text
    assert "Zhixue Local Scan 2026" in draft.json()["text"]
    assert "中文学习材料" in draft.json()["text"].replace(" ", "")
    assert search(http, "Zhixue Local Scan")["hits"] == []
    assert http.get(endpoint).json()["unreadablePages"] == [1]

    reviewed = "本地学习资料 校对结果 Alpha 2026"
    saved = http.put(ocr, json={"revision": revision, "textRevision": None, "text": reviewed})
    assert saved.status_code == 200, saved.text
    text_revision = saved.json()["textRevision"]
    match, = search(http, "校对结果")["hits"]
    assert match["location"] == {"number": 1, "label": "第 1 页 · 已校对 OCR", "textRevision": text_revision}
    linked = http.get(endpoint, params={"at": 1, "revision": text_revision})
    assert linked.status_code == 200 and linked.json()["content"] == reviewed
    assert linked.json()["revision"] == revision
    assert linked.json()["status"] == "ready"
    assert linked.json()["unreadablePages"] == []
    assert http.get(f"{endpoint}/original").content == source
    assert http.post("/api/files/reindex").status_code == 200

    with open_client(folder) as reopened:
        assert search(reopened, "校对结果")["hits"][0]["location"] == match["location"]
    engine = open_db(folder)
    locator = SourceLocator(fileId=identity, at=1, revision=text_revision)
    with transaction(engine, write=False) as db:
        evidence = locate_sources(db, folder, [locator])
        assert evidence[0]["excerpt"] == reviewed
        assert sources_current(db, folder, evidence)

    assert http.put(ocr, json={"revision": revision, "textRevision": None, "text": "同一原件上的过期编辑"}).status_code == 409
    assert http.put(ocr, json={"revision": revision, "textRevision": text_revision, "text": "重新校对 Beta"}).status_code == 200
    with transaction(engine, write=False) as db:
        assert not sources_current(db, folder, evidence)
    assert http.get(endpoint, params={"at": 1, "revision": text_revision}).status_code == 409
    assert search(http, "校对结果")["hits"] == []
    assert search(http, "重新校对")["hits"]
    engine.dispose()

    with pymupdf.open(stream=source, filetype="pdf") as changed:
        changed.new_page()
        (folder / original["path"]).write_bytes(changed.tobytes())
    assert http.put(ocr, json={"revision": revision, "textRevision": text_revision, "text": reviewed}).status_code == 409
    assert search(http, "重新校对")["hits"] == []
    assert http.get(endpoint).json()["unreadablePages"] == [1, 3]


@pytest.mark.parametrize("extension,sample_index", [("png", 1), ("jpg", 2), ("jpeg", 2)])
def test_image_original_preview_and_local_recognition(library, extension, sample_index):
    http, _ = library
    image = scanned_sample()[sample_index]
    created = upload(http, f"照片.{extension}", image)
    assert created.status_code == 201, created.text
    endpoint = f"/api/files/{created.json()['id']}"
    data = http.get(endpoint).json()
    assert data["format"] == extension and data["pageCount"] == 1 and data["unreadablePages"] == [1]
    assert http.get(f"{endpoint}/original").content == image
    rendered = http.get(f"{endpoint}/pages/1/image", params={"revision": data["revision"]})
    assert rendered.headers["content-type"] == "image/png"
    assert rendered.content.startswith(b"\x89PNG\r\n\x1a\n")
    draft = http.post(f"{endpoint}/pages/1/ocr", json={"revision": data["revision"]})
    assert draft.status_code == 200 and "Zhixue Local Scan 2026" in draft.json()["text"]
    assert http.post(f"{endpoint}/pages/2/ocr", json={"revision": data["revision"]}).status_code == 404
    saved = http.put(f"{endpoint}/pages/1/ocr", json={"revision": data["revision"], "textRevision": None, "text": "图片文字已校对"})
    assert saved.status_code == 200
    assert search(http, "图片文字", format=extension)["hits"][0]["location"]["label"] == "图片 · 已校对 OCR"


def test_blank_scan_and_invalid_image_keep_original_with_explicit_failure(library):
    http, _ = library
    with pymupdf.open() as document:
        page = document.new_page(width=250, height=100)
        blank = page.get_pixmap().tobytes("png")
    blank_id = upload(http, "空白.png", blank).json()["id"]
    preview = http.get(f"/api/files/{blank_id}").json()
    endpoint = f"/api/files/{blank_id}/pages/1/ocr"
    result = http.post(endpoint, json={"revision": preview["revision"]})
    assert result.status_code == 422 and "本页未识别到文字" in result.json()["detail"]
    assert http.get(f"/api/files/{blank_id}").json()["unreadablePages"] == [1]
    confirmed = http.put(endpoint, json={"revision": preview["revision"], "textRevision": None, "text": "手动补录位置"})
    assert confirmed.status_code == 200
    assert search(http, "手动补录")["hits"]
    assert http.get(f"/api/files/{blank_id}/original").content == blank

    malformed = b"not actually a JPEG"
    invalid_id = upload(http, "损坏.jpg", malformed).json()["id"]
    invalid = http.get(f"/api/files/{invalid_id}").json()
    assert invalid["status"] == "failed" and invalid["pageCount"] is None
    assert http.get(f"/api/files/{invalid_id}/original").content == malformed
