from hashlib import sha256

import pymupdf

from test_backups import accepted
from test_library import open_client, search, upload
from test_library_trash import move_to_trash


OLD = "本页记录图表标题。\n耐久测试旧稿八百小时。"
NEW = "本页记录图表标题。\n耐久测试新稿五百小时。"


def image_bytes(color):
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 48, 48), False)
    image.clear_with(color)
    return image.tobytes("png")


def raster(http):
    original = image_bytes(230)
    file = accepted(upload(http, "图表原图.png", original))
    path = "/api/files/" + file["id"]
    info = accepted(http.get(path))
    return path, info, original


def correct(http, endpoint, source, text, base=None):
    return accepted(http.put(endpoint, json={"revision": source, "textRevision": base, "text": text}))["textRevision"]


def test_ocr_history_compares_restores_and_reindexes_the_same_original(tmp_path):
    with open_client(tmp_path) as http:
        file, info, original = raster(http)
        source = info["revision"]
        ocr = file + "/pages/1/ocr"
        versions = ocr + "/versions"
        old = correct(http, ocr, source, OLD)
        new = correct(http, ocr, source, NEW, old)
        params = {"revision": source}
        timeline = accepted(http.get(versions, params=params))
        assert timeline["currentTextRevision"] == new
        assert {row["textRevision"] for row in timeline["versions"]} == {old, new}
        assert all(row["sourceRevision"] == source and row["savedAt"] for row in timeline["versions"])
        review = accepted(http.get(versions + "/" + old, params=params))
        assert review["current"]["text"] == NEW and review["selected"]["text"] == OLD
        assert review["canRestore"]
        assert "-耐久测试新稿五百小时。" in review["diff"] and "+耐久测试旧稿八百小时。" in review["diff"]

        stale = {"revision": source, "textRevision": old}
        restore = versions + "/" + old + "/restore"
        assert http.post(restore, json=stale).status_code == 409
        assert accepted(http.get(file, params={"at": 1}))["content"] == NEW
        assert accepted(http.post(restore, json={**stale, "textRevision": new})) == {"textRevision": old}
        assert search(http, "新稿五百小时")["hits"] == []
        [hit] = search(http, "旧稿八百小时")["hits"]
        assert hit["location"]["textRevision"] == old
        assert http.get(file + "/original").content == original

    with open_client(tmp_path) as http:
        assert accepted(http.get(file, params={"at": 1, "revision": old}))["content"] == OLD
        timeline = accepted(http.get(versions, params=params))
        assert timeline["currentTextRevision"] == old and len(timeline["versions"]) == 2


def test_ocr_history_keeps_earlier_original_readable_and_blocks_cross_original_restore(tmp_path):
    with open_client(tmp_path) as http:
        file, info, _ = raster(http)
        source = info["revision"]
        ocr = file + "/pages/1/ocr"
        versions = ocr + "/versions"
        old = correct(http, ocr, source, OLD)
        changed = image_bytes(150)
        (tmp_path / info["path"]).write_bytes(changed)
        source_now = sha256(changed).hexdigest()
        assert accepted(http.get(file))["revision"] == source_now
        assert http.get(versions, params={"revision": source}).status_code == 409
        params = {"revision": source_now}
        listed = accepted(http.get(versions, params=params))
        assert listed["currentTextRevision"] is None
        [archived] = listed["versions"]
        assert archived["sourceRevision"] == source and archived["textRevision"] == old
        view = accepted(http.get(versions + "/" + old, params=params))
        assert view["selected"]["text"] == OLD and view["current"] is None and not view["canRestore"]
        current = correct(http, ocr, source_now, NEW)
        assert http.post(versions + "/" + old + "/restore", json={
            "revision": source_now, "textRevision": current,
        }).status_code == 409
        assert search(http, "旧稿八百小时")["hits"] == []
        assert http.get(file + "/original").content == changed


def test_purging_a_scanned_original_also_clears_ocr_history(tmp_path):
    from backend.db import LibraryOcrVersion, open_db, transaction
    from sqlalchemy import func, select

    with open_client(tmp_path) as http:
        file, info, _ = raster(http)
        source = info["revision"]
        ocr = file + "/pages/1/ocr"
        old = correct(http, ocr, source, OLD)
        correct(http, ocr, source, NEW, old)
        entry = move_to_trash(http, info["id"])
        endpoint = f"/api/files/trash/{entry['id']}/purge"
        assert accepted(http.post(endpoint, json={"revision": entry["revision"]}))["state"] == "purged"
        assert http.get(ocr + "/versions", params={"revision": source}).status_code in (404, 409)
    engine = open_db(tmp_path)
    try:
        with transaction(engine, write=False) as db:
            assert db.scalar(select(func.count()).select_from(LibraryOcrVersion)) == 0
    finally:
        engine.dispose()


def test_removed_pdf_pages_keep_read_only_corrections_and_never_create_phantom_pages(tmp_path):
    def pdf_bytes(pages):
        with pymupdf.open() as document:
            for _ in range(pages):
                document.new_page()
            return document.tobytes()

    with open_client(tmp_path) as http:
        created = accepted(upload(http, "多页资料.pdf", pdf_bytes(3)))
        file = "/api/files/" + created["id"]
        before = accepted(http.get(file))
        assert before["pageCount"] == 3
        active = file + "/pages/1/ocr"
        removed = file + "/pages/3/ocr"
        first = correct(http, active, before["revision"], "原件第一页的说明。")
        correction = correct(http, removed, before["revision"], "只出现在旧第三页的文字。")
        replacement = pdf_bytes(1)
        (tmp_path / before["path"]).write_bytes(replacement)
        updated = accepted(http.get(file))
        assert updated["pageCount"] == 1
        scope = {"revision": updated["revision"]}

        timeline = accepted(http.get(removed + "/versions", params={**scope, "includePages": True}))
        assert timeline["pages"] == [1, 3] and timeline["pageCount"] == 1
        assert timeline["currentTextRevision"] is None
        assert [version["textRevision"] for version in timeline["versions"]] == [correction]
        current_page = accepted(http.get(active + "/versions", params={**scope, "includePages": True}))
        assert current_page["pages"] == [1, 3]
        assert [version["textRevision"] for version in current_page["versions"]] == [first]

        view = accepted(http.get(removed + "/versions/" + correction, params=scope))
        assert view["selected"]["text"] == "只出现在旧第三页的文字。"
        assert view["current"] is None and not view["canRestore"]
        assert http.get(file + "/pages/2/ocr/versions", params=scope).status_code == 404
        assert http.get(removed + "/image", params=scope).status_code == 404
        assert http.put(removed, json={**scope, "textRevision": None, "text": "不能写入已删除页面"}).status_code == 404
        restore = removed + "/versions/" + correction + "/restore"
        assert http.post(restore, json={**scope, "textRevision": None}).status_code == 409
        assert search(http, "旧第三页")["hits"] == []
        assert http.get(file + "/original").content == replacement
