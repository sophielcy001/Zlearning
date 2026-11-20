from datetime import datetime, timezone

from backend.course_publication import course_markdown, save_course_version
from backend.db import Lesson, open_db, transaction
from test_library import library, upload


def get(http, endpoint, **params):
    response = http.get(f"/api/files/{endpoint}", params=params)
    response.raise_for_status()
    return response.json()


def test_wiki_links_follow_ids_and_show_ambiguous_titles(library):
    http, folder = library
    first = upload(http, "Café.md", b"first").json()["id"]
    second = upload(http, "Cafe\u0301.txt", b"second").json()["id"]
    raw_id = f"[[{first}|咖啡资料]]"
    lines = [
        "---",
        f"metadata: '{raw_id}'",
        "---",
        "# 整理数据库",
        "",
        "> 先读参考",
        f"> 然后查看 {raw_id} 和 {raw_id}",
        "",
        "- 段落起点",
        "- [[Café]]",
        "",
        rf"转义 \{raw_id}、嵌入 !{raw_id}、代码 `{raw_id}` 和公式 ${raw_id}$。",
        f"[原生链接 {raw_id}](https://example.com/)",
        "",
        "~~~md",
        raw_id,
        "~~~",
        "",
    ]
    original = ("\ufeff" + "\r\n".join(lines)).encode()
    source = upload(http, "复习.md", original).json()["id"]

    options = get(http, "wiki/suggestions", q="CAFE\u0301")
    assert {option["id"] for option in options} == {first, second}
    first_path = next(option["path"] for option in options if option["id"] == first)
    duplicate = get(http, "wiki/resolve", target="café")
    assert duplicate["state"] == "ambiguous" and {choice["id"] for choice in duplicate["choices"]} == {first, second}
    assert get(http, "wiki/resolve", target=first_path)["choices"][0]["id"] == first
    assert get(http, "wiki/resolve", target="尚无此资料") == {"state": "missing", "choices": []}

    hit, = get(http, f"{first}/backlinks")
    assert (hit["id"], hit["at"], hit["snippet"]) == (source, 7, lines[6])
    assert get(http, source, at=hit["at"], revision=hit["revision"])["anchor"]["text"] == lines[6]
    assert get(http, f"{second}/backlinks") == []
    assert http.get(f"/api/files/{source}/original").content == original

    moved = folder / "library" / "改名后的资料.md"
    (folder / first_path).rename(moved)
    by_id = get(http, "wiki/resolve", target=first.upper())
    assert by_id["state"] == "resolved" and by_id["choices"][0]["path"] == "library/改名后的资料.md"
    assert get(http, "wiki/resolve", target="改名后的资料")["choices"][0]["id"] == first
    assert get(http, "wiki/resolve", target="café")["choices"][0]["id"] == second
    title_hit, = get(http, f"{second}/backlinks")
    assert title_hit["at"] == 10

    endpoint = f"/api/files/{first}/trash"
    preview = get(http, f"{first}/trash")
    trashed = http.post(endpoint, json={"path": preview["path"], "revision": preview["revision"]})
    trashed.raise_for_status()
    assert get(http, "wiki/resolve", target=first)["choices"][0]["status"] == "trashed"
    assert get(http, "wiki/suggestions", q="改名后的资料") == []
    entry = trashed.json()
    restored = http.post(f"/api/files/trash/{entry['id']}/restore", json={
        "path": entry["originalPath"], "revision": entry["revision"],
    })
    restored.raise_for_status()
    assert get(http, "wiki/resolve", target=first)["choices"][0]["status"] == "ready"
    assert get(http, f"{first}/backlinks") == [hit]

    source_path = folder / hit["path"]
    source_path.write_text("重新整理，删除这条引用。\n", encoding="utf-8")
    assert get(http, f"{first}/backlinks") == []
    assert get(http, f"{second}/backlinks") == []
    assert http.get(f"/api/files/{source}", params={"at": hit["at"], "revision": hit["revision"]}).status_code == 409
    source_path.write_text(raw_id + "\n", encoding="utf-8")
    http.post("/api/files/reindex").raise_for_status()
    assert get(http, f"{first}/backlinks")[0]["at"] == 1


def test_wiki_links_use_verified_course_sections_without_rewriting_archive(library):
    http, folder = library
    target = upload(http, "公开资料.txt", b"reading").json()["id"]
    project = http.post("/api/projects", json={
        "id": "40e2006f-6e3c-46ab-af06-2a3364e90a2d", "topic": "数据库课", "outcome": "能解释数据库的索引",
    })
    project.raise_for_status()
    lesson_id = project.json()["lessons"][0]["id"]
    identity = f"course-{lesson_id}"
    link = f"[[{target}|公开资料]]"
    sections = [
        {"title": "关系与表", "text": "正文第一行。\n这里可查看 " + link, "state": "verified", "citations": []},
        {"title": "查询例子", "text": "示例 " + link, "state": "adopted", "citations": []},
    ]
    markdown = course_markdown("数据库课", sections)
    engine = open_db(folder)
    try:
        with transaction(engine) as db:
            save_course_version(db, folder, db.get(Lesson, lesson_id), None, lesson_id, sections,
                                markdown, datetime.now(timezone.utc))
    finally:
        engine.dispose()

    first, second = get(http, f"{target}/backlinks")
    assert first["id"] == second["id"] == identity
    assert (first["at"], second["at"]) == (6, 12)
    assert "\\[\\[" in first["snippet"] and get(http, identity, at=6)["anchor"]["text"] == first["snippet"]
    assert http.get(f"/api/files/{identity}/original").content == markdown.encode()
