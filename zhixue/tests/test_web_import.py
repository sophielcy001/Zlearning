from contextlib import contextmanager
from ipaddress import ip_address
import socket

import pytest

from backend import web_fetch
from backend.documents import extract_html
from backend.web_fetch import Snapshot
from backend.course_sources import SourceLocator, locate_sources
from backend.db import open_db, transaction

from test_library import open_client, search


ORIGINAL = "https://docs.example.test/start"
SOURCE = "https://docs.example.test/guide"
HTML = """<!doctype html>
<html><head><title>网页学习资料</title><script>秘密脚本标记</script></head>
<body><nav>全站栏目</nav><main>
<h1>学习索引</h1><p>第一段介绍公开资料保存。</p>
<h2>精确定位</h2><p>唯一正文演示。</p><pre>SELECT a
  FROM sample;</pre>
<img alt="外部图像"><div hidden>隐藏内容标记</div>
</main><footer>底部导航标记</footer></body></html>
""".encode("utf-8")


def test_web_import_stores_original_links_search_and_reuses_duplicate(tmp_path, monkeypatch):
    from backend import library_api

    fetched = []

    def download(url):
        fetched.append(url)
        return Snapshot(url, SOURCE, "2026-11-15T14:00:00+00:00", "utf-8", "网页学习资料", HTML)

    monkeypatch.setattr(library_api, "download_page", download)
    with open_client(tmp_path) as http:
        added = http.post("/api/files/web", json={"url": ORIGINAL})
        assert added.status_code == 201, added.text
        identity = added.json()["id"]
        endpoint = f"/api/files/{identity}"
        result = http.get(endpoint).json()
        assert result["format"] == "html"
        assert result["webSource"]["url"] == SOURCE
        assert result["webSource"]["requestedUrl"] == ORIGINAL
        assert result["webSource"]["fetchedAt"] == "2026-11-15T14:00:00+00:00"
        assert result["webSource"]["matchesSnapshot"] is True
        assert result["sections"] == [
            {"number": 1, "label": "网页第 1 节 · 学习索引"},
            {"number": 2, "label": "网页第 2 节 · 精确定位"},
        ]
        match, = search(http, "唯一正文演示", format="html")["hits"]
        assert match["location"] == result["sections"][1]
        linked = http.get(endpoint, params={"revision": match["revision"], "at": 2}).json()
        assert "唯一正文演示" in linked["anchor"]["text"]
        assert "SELECT a\n  FROM sample;" in linked["anchor"]["text"]
        assert "第一段介绍" not in linked["content"]
        assert http.get(endpoint, params={"at": 3}).status_code == 404
        for excluded in ("秘密脚本标记", "全站栏目", "外部图像", "隐藏内容标记", "底部导航标记"):
            assert search(http, excluded)["hits"] == []

        original = http.get(f"{endpoint}/original")
        assert original.content == HTML
        assert original.headers["content-type"] == "application/octet-stream"
        assert original.headers["content-disposition"].startswith("attachment")
        assert http.post("/api/files/reindex").status_code == 200
        assert search(http, "唯一正文演示")["hits"][0]["location"] == match["location"]

        duplicate = http.post("/api/files/web", json={"url": ORIGINAL}).json()
        assert duplicate["status"] == "duplicate" and duplicate["id"] == identity
        assert fetched == [ORIGINAL, ORIGINAL]
        copied = http.post("/api/files/web/copy", json={"fileId": identity, "revision": duplicate["revision"]})
        assert copied.status_code == 201 and copied.json()["id"] != identity
        assert fetched == [ORIGINAL, ORIGINAL]
        assert http.get(f"/api/files/{copied.json()['id']}/original").content == HTML
        assert http.get(f"/api/files/{copied.json()['id']}").json()["webSource"] == result["webSource"]

        engine = open_db(tmp_path)
        try:
            with transaction(engine, write=False) as db:
                locator = SourceLocator(fileId=identity, at=2, revision=match["revision"])
                evidence, = locate_sources(db, tmp_path, [locator])
                assert evidence["label"] == match["location"]["label"]
                assert "唯一正文演示" in evidence["excerpt"]
        finally:
            engine.dispose()

        (tmp_path / result["path"]).write_bytes(HTML.replace("唯一正文演示".encode(), "新改动正文".encode()))
        assert http.get(endpoint, params={"revision": match["revision"], "at": 2}).status_code == 409
        assert http.get(endpoint).json()["webSource"]["matchesSnapshot"] is False
        assert http.post("/api/files/web/copy", json={"fileId": identity, "revision": match["revision"]}).status_code == 409
        assert all(hit["id"] != identity for hit in search(http, "唯一正文演示")["hits"])

    with open_client(tmp_path) as restarted:
        restored = restarted.get(endpoint).json()
        assert restored["webSource"]["url"] == SOURCE
        assert "新改动正文" in restored["content"]
        assert fetched == [ORIGINAL, ORIGINAL]


def test_import_router_reports_invalid_address_before_changing_library(tmp_path):
    with open_client(tmp_path) as http:
        response = http.post("/api/files/web", json={"url": "file:///library/local.html"})
        assert response.status_code == 422
        assert http.get("/api/files").json() == []


def test_web_html_uses_capture_encoding_then_detects_a_local_replacement(tmp_path, monkeypatch):
    from backend import library_api

    original = "<html><head><title>数据库</title></head><main><h1>索引</h1><p>第一份原始网页。</p></main></html>".encode("gb18030")
    monkeypatch.setattr(
        library_api, "download_page",
        lambda url: Snapshot(url, SOURCE, "2026-11-15T14:00:00+00:00", "gb18030", "数据库", original),
    )
    with open_client(tmp_path) as http:
        saved = http.post("/api/files/web", json={"url": ORIGINAL}).json()
        endpoint = f"/api/files/{saved['id']}"
        file = http.get(endpoint).json()
        assert "第一份原始网页" in file["content"]
        assert http.get(f"{endpoint}/original").content == original
        updated = '<meta charset="utf-8"><main><h1>可见标题</h1><p>改用 UTF-8 的正文。</p></main>'.encode()
        (tmp_path / file["path"]).write_bytes(updated)
        edited = http.get(endpoint).json()
        assert "改用 UTF-8 的正文" in edited["content"]
        assert edited["webSource"]["matchesSnapshot"] is False


def test_empty_and_bounded_html_extraction_mark_actual_available_sections(monkeypatch):
    from backend import documents

    hidden = b"<title>App title</title><body><script>window.makePage()</script><div id='root'></div></body>"
    assert extract_html(hidden, "").status == "empty"
    monkeypatch.setattr(documents, "MAX_TEXT_LENGTH", 28)
    bounded = extract_html(b"<main><h1>One</h1><p>Short.</p><h2>Two</h2><p>A longer section with unseen text.</p></main>", "")
    assert bounded.status == "partial" and len(bounded.sections) == 2
    assert "前 2 节共 28 个字符" in bounded.message
    assert "unseen" not in bounded.content


def dns_answers(addresses):
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        yield family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)


def test_proxy_placeholders_resolve_publicly_before_connecting(monkeypatch):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", lambda *_args, **_options: dns_answers(["198.18.0.5"]))
    requested = []

    def public_dns(host):
        requested.append(host)
        return (ip_address("151.101.128.223"),)

    monkeypatch.setattr(web_fetch, "public_dns", public_dns)
    assert web_fetch.public_addresses("docs.python.org", 443) == ("151.101.128.223",)
    with pytest.raises(web_fetch.WebPageError, match="非公开网络"):
        web_fetch.public_addresses("192.168.0.14", 443)
    with pytest.raises(web_fetch.WebPageError, match="非公开网络"):
        web_fetch.public_addresses("printer.local", 443)
    assert requested == ["docs.python.org"]
    monkeypatch.setattr(web_fetch, "public_dns", lambda host: (ip_address("127.0.0.1"),))
    with pytest.raises(web_fetch.WebPageError, match="非公开网络"):
        web_fetch.public_addresses("docs.python.org", 443)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.8", "::1", "224.0.0.1", "fd00::1", "::ffff:127.0.0.1"])
def test_nonpublic_dns_answers_are_never_connected(address, monkeypatch):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", lambda *_args, **_options: dns_answers([address]))
    monkeypatch.setattr(web_fetch, "open_response", lambda *_args: pytest.fail("Connected to a rejected address."))
    with pytest.raises(web_fetch.WebPageError, match="非公开网络"):
        web_fetch.fetch_snapshot(SOURCE)


class Reply:
    def __init__(self, status, headers, body=b""):
        self.status, self.headers, self.body = status, headers, body

    def read(self, limit, *, decode_content=False):
        assert decode_content is False
        return self.body[:limit]


def test_redirects_are_rechecked_and_original_response_bytes_are_preserved(monkeypatch):
    first = "https://docs.example.test/start"
    redirected = "https://course.example.test/guide"
    calls = []

    def resolve(host, port, **options):
        return dns_answers(["151.101.128.223" if host == "docs.example.test" else "151.101.0.223"])

    @contextmanager
    def connect(url, address):
        calls.append((url, address))
        if url == first:
            yield Reply(302, {"Location": redirected + "#heading"})
        else:
            yield Reply(200, {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(HTML))}, HTML)

    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(web_fetch, "open_response", connect)
    snapshot = web_fetch.fetch_snapshot(first)
    assert snapshot.url == redirected and snapshot.requested_url == first and snapshot.body == HTML
    assert calls == [(first, "151.101.128.223"), (redirected, "151.101.0.223")]

    def resolve_second_privately(host, port, **options):
        return dns_answers(["151.101.128.223" if host == "docs.example.test" else "127.0.0.1"])

    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", resolve_second_privately)
    calls.clear()
    with pytest.raises(web_fetch.WebPageError, match="非公开网络"):
        web_fetch.fetch_snapshot(first)
    assert calls == [(first, "151.101.128.223")]


def test_non_html_and_streamed_oversize_payloads_are_rejected(monkeypatch):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", lambda *_args, **_options: dns_answers(["151.101.128.223"]))
    for mime, data, status in [
        ("application/pdf", b"PDF", 415),
        ("text/html", b"x" * (web_fetch.MAX_HTML_BYTES + 1), 413),
    ]:
        @contextmanager
        def response(*_args):
            yield Reply(200, {"Content-Type": mime}, data)

        monkeypatch.setattr(web_fetch, "open_response", response)
        with pytest.raises(web_fetch.WebPageError) as failure:
            web_fetch.fetch_snapshot(SOURCE)
        assert failure.value.status == status


def test_full_restore_recovers_original_html_metadata_and_locations(tmp_path, monkeypatch):
    from backend import library_api
    from test_backups import accepted, inspect_backup, make_backup
    from test_full_restore import app_client, request_restore, wait_restore

    monkeypatch.setattr(
        library_api, "download_page",
        lambda url: Snapshot(url, SOURCE, "2026-11-15T14:00:00+00:00", "utf-8", "网页学习资料", HTML),
    )
    with app_client(tmp_path) as http:
        imported = accepted(http.post("/api/files/web", json={"url": ORIGINAL}))
        endpoint = f"/api/files/{imported['id']}"
        file = accepted(http.get(endpoint))
        archive = make_backup(http)
        (tmp_path / file["path"]).write_text("<main><h1>替换后的网页</h1></main>", encoding="utf-8")
        preview = accepted(inspect_backup(http, archive))
        assert preview["counts"]["backup"]["library_web_snapshots"] == 1
        request_restore(http, archive, preview)
        assert wait_restore(http)["state"] == "complete"
        result = accepted(http.get(endpoint, params={"at": 2, "revision": file["revision"]}))
        assert result["webSource"] == file["webSource"]
        assert result["anchor"]["label"] == "网页第 2 节 · 精确定位"
        assert http.get(f"{endpoint}/original").content == HTML
