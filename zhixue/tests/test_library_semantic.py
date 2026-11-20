from io import BytesIO
import sqlite3
from threading import Event

from PIL import Image
import pymupdf

from backend import library_semantic
from test_library import library, search, upload


INDEX = "/api/files/semantic-index"
CONCEPT = "把资料拆开记住固定大小的每一部分"
RELATED = "Chunk long documents into overlapping passages before indexing."


class FixtureEncoder:
    def __init__(self, _assets):
        pass

    def query(self, _query):
        return vector(True)

    def passages(self, texts, _stop):
        return [[(0, len(body), vector("passages" in body or CONCEPT in body))]
                for body in texts]


def vector(related):
    import numpy as np

    return np.array([related, not related, *([0] * 382)], dtype="<f4")


def prepare(monkeypatch, folder):
    downloads = []
    monkeypatch.setattr(library_semantic, "install_model", lambda assets, _: downloads.append(assets))
    monkeypatch.setattr(library_semantic, "LocalEmbeddings", FixtureEncoder)
    worker = library_semantic.SemanticIndex(folder)
    return worker, downloads


def finish(worker):
    stop = Event()
    while worker.step(stop):
        pass


def test_hybrid_reuses_locations_and_filters_without_a_network_call(library, monkeypatch):
    http, folder = library
    worker, downloads = prepare(monkeypatch, folder)
    original = f"Intro\n{RELATED}\n{CONCEPT} 是一个已知关键词。\n".encode()
    inside = upload(http, "讲义.md", original).json()["id"]
    outside = upload(http, "目录外.txt", RELATED.encode(), "copy").json()["id"]
    assert http.post("/api/files/tags", json={"ids": [inside], "tag": "学习", "action": "add"}).status_code == 200
    assert http.put(f"/api/files/{inside}/favorite", json={"favorite": True}).status_code == 200

    assert not worker.step(Event())
    assert http.get(INDEX).json()["state"] == "disabled"
    waiting = search(http, CONCEPT, mode="hybrid")
    assert waiting["hits"][0]["location"]["number"] == 3
    assert waiting["semantic"]["state"] == "disabled"
    assert downloads == []

    assert http.post(INDEX).json()["state"] == "preparing"
    finish(worker)
    assert len(downloads) == 1
    status = http.get(INDEX).json()
    assert status["state"] == "ready" and status["completed"] == status["total"]
    assert {hit["id"] for hit in search(http, CONCEPT, mode="hybrid")["hits"]} == {inside, outside}
    combined = search(http, CONCEPT, mode="hybrid", format="md", tag="学习", shelf="favorites", directory="library/imports")
    keyword, semantic = combined["hits"]
    assert keyword["match"] == "both" and keyword["location"]["number"] == 3
    assert semantic["match"] == "semantic" and semantic["snippet"] == RELATED
    assert semantic["location"] == {"number": 2, "label": "第 2 行"}
    preview = http.get(f"/api/files/{inside}", params={"at": 2, "revision": semantic["revision"]}).json()
    assert preview["anchor"]["text"] == RELATED
    assert http.get(f"/api/files/{inside}/original").content == original
    assert search(http, CONCEPT)["hits"][0]["location"]["number"] == 3

    path = folder / semantic["path"]
    snapshot = path.read_bytes()
    path.write_bytes(snapshot.replace(RELATED.encode(), b"Changed completely."))
    pending = search(http, CONCEPT, mode="hybrid", format="md")
    assert pending["semantic"]["state"] == "indexing"
    assert [hit["location"]["number"] for hit in pending["hits"]] == [3]
    finish(worker)
    matches = search(http, CONCEPT, mode="hybrid", format="md")
    assert matches["semantic"]["state"] == "ready"
    assert all(RELATED not in hit["snippet"] for hit in matches["hits"])
    assert len(downloads) == 1
    assert library_semantic.SemanticIndex(folder).status()["state"] == "ready"


def test_semantic_waits_for_reviewed_ocr_and_returns_its_revision(library, monkeypatch):
    http, folder = library
    worker, _ = prepare(monkeypatch, folder)
    with BytesIO() as image:
        Image.new("RGB", (12, 12), "white").save(image, "PNG")
        identity = upload(http, "扫描资料.png", image.getvalue()).json()["id"]
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((60, 80), "Preface")
        pdf.new_page().insert_text((60, 80), RELATED)
        pdf_id = upload(http, "讲义.pdf", pdf.tobytes()).json()["id"]

    http.post(INDEX).raise_for_status()
    finish(worker)
    empty = search(http, CONCEPT, mode="hybrid", format="png")
    assert empty["hits"] == []
    assert http.get(INDEX).json()["total"] == 2
    assert search(http, "扫描资料", mode="hybrid", format="png")["hits"][0]["match"] == "keyword"
    pdf_hit, = search(http, CONCEPT, mode="hybrid", format="pdf")["hits"]
    assert pdf_hit["id"] == pdf_id and pdf_hit["location"]["number"] == 2

    original = http.get(f"/api/files/{identity}").json()
    response = http.put(f"/api/files/{identity}/pages/1/ocr", json={
        "revision": original["revision"], "textRevision": None, "text": RELATED,
    })
    assert response.status_code == 200, response.text
    finish(worker)
    image_hit, = search(http, CONCEPT, mode="hybrid", format="png")["hits"]
    assert image_hit["location"]["number"] == 1
    revision = image_hit["location"]["textRevision"]
    assert revision != original["revision"]
    anchor = http.get(f"/api/files/{identity}", params={"at": 1, "revision": revision}).json()["anchor"]
    assert anchor["text"] == RELATED


def test_failed_download_falls_back_and_restart_keeps_completed_passages(library, monkeypatch):
    http, folder = library
    worker, downloads = prepare(monkeypatch, folder)
    upload(http, "书摘.md", f"{CONCEPT}\n{RELATED}".encode())
    http.post(INDEX).raise_for_status()
    install = library_semantic.install_model

    def unavailable(*_):
        raise OSError("PRIVATE_SENTINEL")

    monkeypatch.setattr(library_semantic, "install_model", unavailable)
    assert not worker.step(Event())
    failed = search(http, CONCEPT, mode="hybrid")
    assert failed["hits"][0]["snippet"] == CONCEPT
    assert failed["semantic"]["state"] == "failed"
    assert "PRIVATE_SENTINEL" not in str(failed)
    assert not worker.step(Event())

    monkeypatch.setattr(library_semantic, "install_model", install)
    http.post(INDEX).raise_for_status()
    restarted = library_semantic.SemanticIndex(folder)
    finish(restarted)
    assert len(downloads) == 1
    assert restarted.status()["completed"] > 0
    assert http.post("/api/files/reindex").status_code == 200
    restarted = library_semantic.SemanticIndex(folder)
    assert restarted.status()["state"] == "indexing"
    assert restarted.status()["completed"] == 0
    finish(restarted)
    assert restarted.status()["state"] == "ready"
    assert len(downloads) == 1
    with sqlite3.connect(folder / "zhixue.sqlite3") as primary:
        assert primary.execute("PRAGMA quick_check").fetchone() == ("ok",)
