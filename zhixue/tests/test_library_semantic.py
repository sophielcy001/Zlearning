from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
import sqlite3
from threading import Event

import pytest

from PIL import Image
import pymupdf

from backend import library_semantic
from backend import library_metadata
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


def test_first_semantic_request_publishes_only_a_committed_cache(library, monkeypatch):
    http, folder = library
    worker, downloads = prepare(monkeypatch, folder)
    contents = (CONCEPT + "\n" + RELATED).encode()
    identity = upload(http, "首次启用语义索引.txt", contents).json()["id"]
    original = library_semantic.request_index
    writing, commit = Event(), Event()

    def hold_before_commit(db):
        original(db)
        writing.set()
        assert commit.wait(8)

    monkeypatch.setattr(library_semantic, "request_index", hold_before_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        enabling = pool.submit(http.post, INDEX)
        try:
            assert writing.wait(8)
            initial = search(http, CONCEPT, mode="hybrid")
            assert initial["semantic"]["state"] == "disabled"
            assert http.get(INDEX).json()["state"] == "disabled"
            assert [(hit["id"], hit["match"]) for hit in initial["hits"]] == [(identity, "keyword")]
            assert not worker.cache.is_file()
            assert not worker.step(Event())
            assert downloads == []
        finally:
            commit.set()
        response = enabling.result(timeout=8)
    assert response.status_code == 202
    assert response.json()["state"] == "preparing"
    finish(worker)
    final = search(http, CONCEPT, mode="hybrid")
    assert final["semantic"]["state"] == "ready"
    assert [hit["match"] for hit in final["hits"]] == ["both", "semantic"]
    assert final["hits"][-1]["location"]["number"] == 2
    assert http.get(f"/api/files/{identity}/original").content == contents


def test_interrupted_first_semantic_request_can_retry_without_publishing_partial_cache(library, monkeypatch):
    http, folder = library
    worker, _ = prepare(monkeypatch, folder)
    upload(http, "首次启用失败重试.txt", RELATED.encode())
    original = library_semantic.request_index

    def interrupt_before_commit(db):
        original(db)
        raise OSError("INTERRUPTED_CANDIDATE")

    monkeypatch.setattr(library_semantic, "request_index", interrupt_before_commit)
    assert http.post(INDEX).status_code == 503
    assert http.get(INDEX).json()["state"] == "disabled"
    assert not worker.cache.exists()
    assert not list(worker.cache.parent.glob(".semantic-rebuild-*"))
    monkeypatch.setattr(library_semantic, "request_index", original)
    assert http.post(INDEX).status_code == 202
    finish(worker)
    assert worker.status()["state"] == "ready"


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


def test_recent_hybrid_keeps_visit_order_across_keyword_and_semantic_hits(library, monkeypatch):
    http, folder = library
    worker, _ = prepare(monkeypatch, folder)
    stamp = datetime(2026, 11, 21, 10, 30, tzinfo=UTC)

    class ReversedClock:
        @classmethod
        def now(cls, _timezone):
            return stamp

    monkeypatch.setattr(library_metadata, "datetime", ReversedClock)
    older = upload(http, "旧关键词.txt", CONCEPT.encode()).json()["id"]
    recent = upload(http, "新语义.txt", RELATED.encode()).json()["id"]
    legacy = upload(http, "旧版访问.txt", (RELATED + " Earlier bookmark.").encode()).json()["id"]
    unvisited = upload(http, "未读文件.txt", (RELATED + " Unopened.").encode()).json()["id"]

    assert http.post(INDEX).status_code == 202
    finish(worker)
    assert worker.status()["state"] == "ready"
    assert http.post(f"/api/files/{older}/opened").is_success
    stamp -= timedelta(minutes=3)
    assert http.post(f"/api/files/{recent}/opened").is_success
    with sqlite3.connect(folder / "zhixue.sqlite3") as db:
        db.execute("INSERT INTO library_file_activity (file_id, favorite, last_viewed_at) VALUES (?, ?, ?)",
                   (legacy, 1, (stamp + timedelta(days=1)).isoformat()))

    data = search(http, CONCEPT, mode="hybrid", shelf="recent")
    assert [(hit["id"], hit["match"]) for hit in data["hits"]] == [
        (recent, "semantic"), (older, "both"), (legacy, "semantic"),
    ]
    assert data["hits"][0]["lastViewedAt"] < data["hits"][1]["lastViewedAt"]
    assert not data["hasMore"]
    ordinary = search(http, CONCEPT, mode="hybrid")
    assert ordinary["hits"][0]["id"] == older
    assert {item["id"] for item in ordinary["hits"]} == {older, recent, legacy, unvisited}


def test_recent_semantic_applies_visit_order_before_limiting_candidates(library, monkeypatch):
    http, folder = library
    worker, _ = prepare(monkeypatch, folder)
    older = upload(http, "大量旧命中.txt", ((RELATED + "\n") * 85).encode()).json()["id"]
    new_body = ("读过的最新资料" + "\n" + RELATED).encode()
    recent = upload(http, "刚打开.txt", new_body).json()["id"]
    assert http.post(INDEX).status_code == 202
    finish(worker)
    assert worker.status()["state"] == "ready"
    assert http.post(f"/api/files/{older}/opened").is_success
    assert http.post(f"/api/files/{recent}/opened").is_success

    data = search(http, CONCEPT, mode="hybrid", shelf="recent")
    assert data["hasMore"] and len(data["hits"]) == library_semantic.MAX_HITS
    first = data["hits"][0]
    assert first["id"] == recent and first["match"] == "semantic"
    assert first["location"] == {"number": 2, "label": "第 2 行"}
    assert all(hit["id"] == older for hit in data["hits"][1:])
    current = http.get(f"/api/files/{recent}", params={"at": 2, "revision": first["revision"]}).json()
    assert current["anchor"]["text"] == RELATED


def test_hybrid_keeps_the_complete_winning_span_and_original_location(library, monkeypatch):
    http, folder = library
    worker, _ = prepare(monkeypatch, folder)
    context = "语义索引支持按原件位置读回可检索的文本。" * 12
    winning = context + RELATED
    before, after = "片段之前的文字 ", " 后续关键词：" + CONCEPT
    full = before + winning + after

    class PositionedEncoder(FixtureEncoder):
        def passages(self, texts, _stopped):
            return [[(len(before), len(before) + len(winning), vector(True))]
                    if body == full else [(0, len(body), vector(False))] for body in texts]

    monkeypatch.setattr(library_semantic, "LocalEmbeddings", PositionedEncoder)
    identity = upload(http, "较长的检索片段.txt", ("开头\n" + full).encode()).json()["id"]
    assert http.post(INDEX).status_code == 202
    finish(worker)

    result, = search(http, "请介绍重叠分片", mode="hybrid")["hits"]
    assert result["match"] == "semantic" and result["id"] == identity
    assert result["snippet"].startswith("…") and result["snippet"].endswith("…")
    assert RELATED not in result["snippet"]
    assert result["semanticExcerpt"] == "…" + winning + "…"
    assert result["location"] == {"number": 2, "label": "第 2 行"}
    preview = http.get(f"/api/files/{identity}", params={"at": 2, "revision": result["revision"]}).json()
    assert preview["anchor"]["text"] == full

    exact, = search(http, CONCEPT)["hits"]
    both, = search(http, CONCEPT, mode="hybrid")["hits"]
    assert both["match"] == "both" and both["semanticExcerpt"] == result["semanticExcerpt"]
    assert both["snippet"] == exact["snippet"] and CONCEPT in exact["snippet"]
    assert "semanticExcerpt" not in exact


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


@pytest.mark.parametrize(("endpoint", "status_code"), [(INDEX, 202), ("/api/files/reindex", 200)])
def test_corrupt_semantic_cache_rebuilds_and_preserves_keyword_search(library, monkeypatch, endpoint, status_code):
    http, folder = library
    worker, _ = prepare(monkeypatch, folder)
    contents = (CONCEPT + "\n" + RELATED).encode()
    identity = upload(http, "索引恢复.txt", contents).json()["id"]
    assert http.post(INDEX).status_code == 202
    finish(worker)
    assert worker.status()["state"] == "ready"
    before = search(http, CONCEPT, mode="hybrid")["hits"]
    assert [hit["match"] for hit in before] == ["both", "semantic"]

    worker.cache.write_bytes(b"interrupted cache database" * 180)
    damaged = search(http, CONCEPT, mode="hybrid")
    assert damaged["semantic"]["state"] == "failed"
    assert [hit["match"] for hit in damaged["hits"]] == ["keyword"]
    assert damaged["hits"][0]["id"] == identity
    response = http.post(endpoint)
    assert response.status_code == status_code, response.text
    assert worker.status()["state"] == "preparing"

    finish(library_semantic.SemanticIndex(folder))
    restored = search(http, CONCEPT, mode="hybrid")
    assert restored["semantic"]["state"] == "ready"
    assert restored["hits"] == before
    assert http.get(f"/api/files/{identity}/original").content == contents
    assert search(http, CONCEPT)["hits"][0]["id"] == identity


def test_locked_semantic_cache_keeps_its_completed_passages(library, monkeypatch):
    http, folder = library
    worker, _ = prepare(monkeypatch, folder)
    upload(http, "占用缓存.txt", RELATED.encode())
    assert http.post(INDEX).status_code == 202
    finish(worker)
    expected = worker.status()
    assert expected["state"] == "ready" and expected["completed"] == 1

    with sqlite3.connect(worker.cache) as writer:
        writer.execute("BEGIN IMMEDIATE")
        busy = http.post(INDEX)
        assert busy.status_code == 503

    assert worker.status() == expected
    hit, = search(http, CONCEPT, mode="hybrid")["hits"]
    assert hit["match"] == "semantic"

def test_replacement_rejects_vectors_finished_for_the_previous_cache(library, monkeypatch):
    http, folder = library
    worker, _ = prepare(monkeypatch, folder)
    upload(http, "正在索引.txt", RELATED.encode())
    assert http.post(INDEX).status_code == 202
    assert worker.step(Event())
    encoding, release = Event(), Event()

    class InFlightEncoder(FixtureEncoder):
        def passages(self, texts, stopped):
            encoding.set()
            assert release.wait(8)
            return super().passages(texts, stopped)

    monkeypatch.setattr(library_semantic, "LocalEmbeddings", InFlightEncoder)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker.step, Event())
        try:
            assert encoding.wait(8)
            worker.cache.write_bytes(b"truncated semantic cache after preparation" * 100)
            assert http.post(INDEX).status_code == 202
        finally:
            release.set()
        assert pending.result(timeout=8) is False

    with sqlite3.connect(worker.cache) as cache:
        assert cache.execute("SELECT count(*) FROM passages").fetchone() == (0,)
    monkeypatch.setattr(library_semantic, "LocalEmbeddings", FixtureEncoder)
    finish(library_semantic.SemanticIndex(folder))
    assert worker.status()["state"] == "ready"


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
