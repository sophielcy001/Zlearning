from backend import library_folders

from test_library_locations import app, get, upload


def create(http, name, parent="library"):
    result = http.post("/api/files/folders", json={"parent": parent, "name": name})
    assert result.status_code == 201, result.text
    return result.json()["path"]


def preview(http, change):
    result = http.post("/api/files/relocation/preview", json=change)
    assert result.is_success, result.text
    return result.json()


def confirm(http, change, plan):
    return http.put("/api/files/relocation", json={**change, "token": plan["token"]})


def test_real_folders_moves_and_search_keep_old_wiki_links(tmp_path):
    shared = "目录检索关键材料"
    with app(tmp_path) as http:
        sources = create(http, "资料")
        empty = create(http, "空目录", sources)
        archive = create(http, "资料归档")
        external = tmp_path / "library" / "外部创建目录"
        external.mkdir()
        assert {entry["path"]: entry["parent"] for entry in get(http, "/api/files/folders")} == {
            "library": None, sources: "library", archive: "library", empty: sources,
            "library/外部创建目录": "library",
        }
        original = upload(http, "原始资料.md", shared.encode())
        other = upload(http, "其他资料.md", (shared + "另一个目录").encode())
        change = {"fileId": original, "source": f"library/imports/{original}.md", "parent": empty, "name": "第一版.md"}
        plan = preview(http, change)
        assert plan["fileCount"] == plan["trackedCount"] == 1
        assert confirm(http, change, plan).json()["path"] == empty + "/第一版.md"
        change = {"fileId": other, "source": f"library/imports/{other}.md", "parent": archive, "name": "其他资料.md"}
        assert confirm(http, change, preview(http, change)).is_success

        link = upload(http, "双链接.md", f"[[{empty}/第一版.md]]".encode())
        assert get(http, f"/api/files/{original}/backlinks")[0]["id"] == link
        (tmp_path / empty / "未登记.bin").write_bytes(b"second physical file")
        change = {"source": sources, "parent": "library/外部创建目录", "name": "已归档"}
        plan = preview(http, change)
        assert (plan["fileCount"], plan["trackedCount"]) == (2, 1)
        assert confirm(http, change, plan).is_success
        current = "library/外部创建目录/已归档"
        assert not (tmp_path / sources).exists()
        assert (tmp_path / current / "空目录" / "未登记.bin").read_bytes() == b"second physical file"
        assert get(http, f"/api/files/{original}")["path"] == current + "/空目录/第一版.md"
        assert {row["id"] for row in get(http, "/api/files/search", q=shared, directory=current)["hits"]} == {original}
        assert {row["id"] for row in get(http, "/api/files/search", q=shared, directory=archive)["hits"]} == {other}

    with app(tmp_path) as http:
        assert http.post("/api/files/reindex").is_success
        assert get(http, "/api/files/wiki/resolve", target=empty + "/第一版.md")["choices"][0]["id"] == original
        assert get(http, f"/api/files/{original}/backlinks")[0]["id"] == link
        assert http.get(f"/api/files/{original}/original").content == shared.encode()
        replacement = upload(http, "后来导入.md", b"a different source")
        create(http, "资料")
        create(http, "空目录", sources)
        change = {"fileId": replacement, "source": f"library/imports/{replacement}.md", "parent": empty, "name": "第一版.md"}
        assert confirm(http, change, preview(http, change)).is_success
        conflict = get(http, "/api/files/wiki/resolve", target=empty + "/第一版.md")
        assert conflict["state"] == "ambiguous"
        assert {item["id"] for item in conflict["choices"]} == {original, replacement}


def test_stale_folder_preview_and_native_rename_preserve_existing_target(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        sources = create(http, "文件夹")
        child = create(http, "子目录", sources)
        source = tmp_path / sources
        change = {"source": sources, "parent": "library", "name": "归档"}
        plan = preview(http, change)
        (tmp_path / child / "外部新文件.txt").write_bytes(b"added")
        assert confirm(http, change, plan).status_code == 409
        assert source.is_dir() and not (tmp_path / "library/归档").exists()
        descendant = http.post("/api/files/relocation/preview", json={**change, "parent": child})
        assert descendant.status_code == 409
        assert "子目录" in descendant.json()["detail"]
        for name in ("../外部", ".", "嵌套\\目录", "NUL.txt", "空白 "):
            assert http.post("/api/files/folders", json={"parent": sources, "name": name}).status_code == 422
        native = library_folders.rename_without_replacing

        def concurrent(origin, target):
            target.mkdir()
            native(origin, target)

        plan = preview(http, change)
        monkeypatch.setattr(library_folders, "rename_without_replacing", concurrent)
        assert confirm(http, change, plan).status_code == 409
        assert (tmp_path / child / "外部新文件.txt").read_bytes() == b"added"
        assert (tmp_path / "library/归档").is_dir()


def test_path_recovers_when_move_finishes_before_the_database_update(tmp_path, monkeypatch):
    with app(tmp_path) as http:
        folder = create(http, "移动目标")
        identity = upload(http, "恢复测试.md", b"local data")
        change = {"fileId": identity, "source": f"library/imports/{identity}.md", "parent": folder, "name": "恢复原件.md"}
        plan = preview(http, change)
        native = library_folders.rename_without_replacing

        def finish_filesystem_only(origin, target):
            native(origin, target)
            raise OSError("simulated interruption")

        monkeypatch.setattr(library_folders, "rename_without_replacing", finish_filesystem_only)
        assert confirm(http, change, plan).status_code == 409

    with app(tmp_path) as http:
        item = get(http, f"/api/files/{identity}")
        assert item["path"] == plan["path"]
        assert item["status"] == "ready"
        assert http.get(f"/api/files/{identity}/original").content == b"local data"
        assert get(http, "/api/files/wiki/resolve", target="恢复测试")["choices"][0]["id"] == identity
