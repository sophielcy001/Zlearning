from datetime import UTC, datetime, timedelta
from io import BytesIO
import sqlite3
from zipfile import ZipFile

from backend import library_copies, library_metadata

from test_library_folders import create
from test_library_locations import app, get, upload


def tag(http, ids, name, action="add"):
    response = http.post("/api/files/tags", json={"ids": ids, "tag": name, "action": action})
    assert response.is_success, response.text
    return response.json()


def preview(http, action, ids, parent=None):
    response = http.post("/api/files/batch/preview", json={"action": action, "ids": ids, "parent": parent})
    assert response.is_success, response.text
    return response.json()["entries"]


def confirm(http, action, entries, parent=None):
    files = [{key: entry[key] for key in ("id", "source", "target", "token", "copyId") if key in entry}
             for entry in entries if entry.get("token")]
    return http.post("/api/files/batch/confirm", json={"action": action, "files": files, "parent": parent})


def test_tags_filter_first_and_metadata_survives_reindex_and_restart(tmp_path):
    query = "共用检索材料"
    with app(tmp_path) as http:
        many_hits = upload(http, "A.md", (query + "\n").encode() * 85)
        target = upload(http, "Z.md", (query + "正确文档").encode())
        assert get(http, "/api/files/search", q=query)["hasMore"]
        tag(http, [target, many_hits], "复习资料")
        tag(http, [many_hits], "复习资料", "remove")
        tag(http, [target], "Café")
        tag(http, [target], "CAFE\u0301")
        assert get(http, f"/api/files/{target}")["tags"] == [
            {"key": "café", "name": "Café"}, {"key": "复习资料", "name": "复习资料"},
        ]
        assert http.put(f"/api/files/{target}/favorite", json={"favorite": True}).is_success
        assert http.post(f"/api/files/{many_hits}/opened").is_success
        opened = http.post(f"/api/files/{target}/opened").json()["lastViewedAt"]
        assert get(http, "/api/files/search", q=query, shelf="recent")["hits"][0]["id"] == target
        rejected = http.post("/api/files/tags", json={"ids": [target, "missing"], "tag": "错误批次", "action": "add"})
        assert rejected.status_code == 409
        assert http.post("/api/files/reindex").is_success

    with app(tmp_path) as http:
        hits = get(http, "/api/files/search", q=query, tag="CAFE\u0301", format="md", shelf="favorites")
        hit, = hits["hits"]
        assert hit["id"] == target and hit["lastViewedAt"] == opened and hit["favorite"] is True
        assert not hits["hasMore"]
        assert get(http, "/api/files/search", q=query, tag="错误批次")["hits"] == []
        file = get(http, f"/api/files/{target}")
        deleted = http.post(f"/api/files/{target}/trash", json={key: file[key] for key in ("path", "revision")})
        assert deleted.is_success, deleted.text
        assert get(http, "/api/files/search", q=query, tag="café")["hits"] == []
        entry = deleted.json()
        restored = http.post(f"/api/files/trash/{entry['id']}/restore", json={
            "path": file["path"], "revision": entry["revision"],
        })
        assert restored.is_success, restored.text
        assert get(http, f"/api/files/{target}")["tags"] == file["tags"]
        assert get(http, f"/api/files/{target}")["favorite"] is True


def test_recent_follows_commit_order_with_identical_or_reversed_wall_clock(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 21, 2, 38, 54, 443324, tzinfo=UTC)]

    class FixedClock:
        @classmethod
        def now(cls, _timezone):
            return clock[0]

    monkeypatch.setattr(library_metadata, "datetime", FixedClock)
    phrase = "并列排序样本"
    with app(tmp_path) as http:
        early = upload(http, "A.md", (phrase + "甲").encode())
        latest = upload(http, "Z.md", (phrase + "乙").encode())
        old = upload(http, "旧记录.md", (phrase + "丙").encode())
        get(http, "/api/files")

        def opened(file):
            return http.post(f"/api/files/{file}/opened").json()["lastViewedAt"]

        assert opened(early) == opened(latest)
        hits = get(http, "/api/files/search", q=phrase, shelf="recent")["hits"]
        assert [hit["id"] for hit in hits] == [latest, early]
        with sqlite3.connect(tmp_path / "zhixue.sqlite3") as db:
            db.execute("INSERT INTO library_file_activity (file_id, favorite, last_viewed_at) VALUES (?, ?, ?)",
                       (old, 1, (clock[0] + timedelta(days=1)).isoformat()))
        clock[0] -= timedelta(minutes=1)
        assert opened(early) == clock[0].isoformat(timespec="microseconds")

    with app(tmp_path) as http:
        hits = get(http, "/api/files/search", q=phrase, shelf="recent")["hits"]
        assert [hit["id"] for hit in hits] == [early, latest, old]
        files = {file["id"]: file for file in get(http, "/api/files")}
        assert files[early]["lastViewedOrder"] > files[latest]["lastViewedOrder"] > 0
        assert files[early]["lastViewedAt"] < files[latest]["lastViewedAt"]
        assert files[old]["favorite"] is True and files[old].get("lastViewedOrder") is None


def test_batch_move_previews_collisions_and_retries_a_partial_result(tmp_path):
    with app(tmp_path) as http:
        target = create(http, "按主题归档")
        sources = [upload(http, f"名称 {number}.md", f"移动测试 {number}".encode()) for number in range(3)]
        tag(http, sources, "资料")
        plans = preview(http, "move", sources, target)
        assert len(plans) == 3 and all(plan["token"] for plan in plans)
        first, second, third = plans
        (tmp_path / second["source"]).write_bytes(b"updated while preview was visible")
        (tmp_path / third["target"]).write_bytes(b"external name conflict")
        response = confirm(http, "move", plans, target)
        assert response.is_success, response.text
        results = response.json()["entries"]
        assert results[0] == {"id": first["id"], "path": first["target"]}
        assert "预览后变化" in results[1]["error"]
        assert "同名" in results[2]["error"]
        assert confirm(http, "move", plans, target).json()["entries"] == results
        assert (tmp_path / first["target"]).read_bytes() == "移动测试 0".encode()
        assert (tmp_path / third["target"]).read_bytes() == b"external name conflict"
        assert preview(http, "move", [third["id"]], target)[0]["error"]
        hits = get(http, "/api/files/search", q="移动测试", directory=target, tag="资料")["hits"]
        assert {hit["id"] for hit in hits} == {first["id"]}


def test_zip_export_keeps_duplicate_names_and_exact_original_bytes(tmp_path):
    with app(tmp_path) as http:
        first = upload(http, "同名资料.txt", b"first\r\nplain bytes")
        second = upload(http, "同名资料.txt", "第二份\n".encode())
        plans = preview(http, "export", [first, second])
        result = confirm(http, "export", plans)
        assert result.is_success and result.headers["content-type"] == "application/zip"
        assert int(result.headers["content-length"]) == len(result.content)
        with ZipFile(BytesIO(result.content)) as archive:
            assert archive.namelist() == ["同名资料.txt", "同名资料 (2).txt"]
            assert archive.read("同名资料.txt") == b"first\r\nplain bytes"
            assert archive.read("同名资料 (2).txt") == "第二份\n".encode()
        (tmp_path / plans[1]["source"]).write_bytes(b"changed since preview")
        rejected = confirm(http, "export", plans)
        assert rejected.status_code == 409 and "预览后变化" in rejected.json()["detail"]


def test_batch_copy_keeps_distinct_files_and_replaying_confirmation_is_idempotent(tmp_path):
    with app(tmp_path) as http:
        target = create(http, "副本整理")
        sources = [upload(http, "相同标题.md", f"同名原件内容 {number}".encode()) for number in range(2)]
        tag(http, sources, "复习")
        assert http.put(f"/api/files/{sources[0]}/favorite", json={"favorite": True}).is_success
        plans = preview(http, "copy", sources, target)
        assert [item["target"].rsplit("/", 1)[-1] for item in plans] == ["相同标题（副本）.md", "相同标题（副本） (2).md"]
        result = confirm(http, "copy", plans, target)
        assert result.is_success, result.text
        outcomes = result.json()["entries"]
        assert confirm(http, "copy", plans, target).json()["entries"] == outcomes
        assert len(list((tmp_path / target).iterdir())) == 2
        for number, outcome in enumerate(outcomes):
            assert outcome["copyId"] not in sources
            copied = get(http, f"/api/files/{outcome['copyId']}")
            assert copied["tags"] == [{"key": "复习", "name": "复习"}]
            assert copied["favorite"] is False
            assert copied["content"] == f"同名原件内容 {number}"
            assert get(http, f"/api/files/{sources[number]}")["path"] == plans[number]["source"]


def test_interrupted_copy_is_registered_once_after_restart(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        target = create(http, "复制恢复")
        source = upload(http, "中断前.md", "复制的原件应完整保留。".encode())
        plan, = preview(http, "copy", [source], target)
        native = library_copies.rename_without_replacing

        def interrupt_after_writing(stage, destination):
            native(stage, destination)
            raise OSError("simulated process interruption after atomic publication")

        monkeypatch.setattr(library_copies, "rename_without_replacing", interrupt_after_writing)
        result = confirm(http, "copy", [plan], target).json()["entries"][0]
        assert result["pending"] and "error" in result
        assert "待恢复" in preview(http, "copy", [source], target)[0]["error"]
        assert (tmp_path / plan["target"]).is_file()
        assert http.get(f"/api/files/{plan['copyId']}").status_code == 404
        snapshot = http.post("/api/backups", json={"password": "测试专用的备份密码"})
        assert snapshot.status_code == 409 and snapshot.json()["code"] == "pending_write"

    with app(tmp_path) as http:
        copy = get(http, f"/api/files/{plan['copyId']}")
        assert copy["content"] == "复制的原件应完整保留。"
        assert copy["path"] == plan["target"]
        assert get(http, f"/api/files/{source}")["path"] == plan["source"]
        replay = confirm(http, "copy", [plan], target).json()["entries"]
        assert replay == [{"id": source, "path": plan["target"], "copyId": copy["id"]}]
        assert len(list((tmp_path / target).iterdir())) == 1
