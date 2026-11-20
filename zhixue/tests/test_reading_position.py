from test_backups import (
    accepted, choose_destination, create_project, inspect_backup, make_backup, open_client,
)


def position(detail, block, excerpt):
    return {"revision": detail["readingRevision"], "point": {
        "block": block, "offset": 6, "excerpt": excerpt, "excerptOffset": 3,
    }}


def test_reading_positions_are_per_lesson_and_survive_restart_and_backup(tmp_path):
    with open_client(tmp_path) as http:
        project = create_project(http, tmp_path)
        first, second, *_ = [f"/api/lessons/{lesson['id']}" for lesson in project["lessons"]]
        initial = accepted(http.get(first))
        assert initial["readingPosition"] is None
        saved = position(initial, 2, "阅读锚点甲")
        another = position(accepted(http.get(second)), 4, "阅读锚点乙")
        assert accepted(http.put(first + "/reading-position", json=saved))["readingPosition"] == saved
        accepted(http.put(second + "/reading-position", json=another))
        assert accepted(http.get("/api/state"))["timer"]["active"] is None
        assert accepted(http.get(first))["project"]["learned"] == 0
        choose_destination(http, tmp_path / "archive")
        item = make_backup(http)
        assert accepted(inspect_backup(http, item))["counts"]["backup"]["lesson_reading_positions"] == 2

    with open_client(tmp_path) as http:
        assert accepted(http.get(first))["readingPosition"] == saved
        assert accepted(http.get(second))["readingPosition"] == another
        reset = {"revision": saved["revision"], "point": None}
        accepted(http.put(first + "/reading-position", json=reset))
        assert accepted(http.get(first))["readingPosition"] is None
        assert accepted(http.get(second))["readingPosition"] == another


def test_delayed_write_for_changed_course_cannot_replace_previous_reading_anchor(tmp_path):
    with open_client(tmp_path) as http:
        project = create_project(http, tmp_path)
        identity = project["lessons"][0]["id"]
        path = f"/api/lessons/{identity}"
        initial = accepted(http.get(path))
        saved = position(initial, 2, "这个段落后来仍可能保留")
        accepted(http.put(path + "/reading-position", json=saved))
        course = tmp_path / "library" / "courses" / f"{identity}.md"
        course.write_text(initial["body"] + "\n## 用户写入的新段落\n", encoding="utf-8")
        current = accepted(http.get(path))
        assert current["readingRevision"] != initial["readingRevision"]
        assert current["readingPosition"] == saved
        stale = position(initial, 4, "这是旧页面较晚发出的保存")
        assert http.put(path + "/reading-position", json=stale).status_code == 409
        assert accepted(http.get(path))["readingPosition"] == saved
        current_position = position(current, 8, "这是新正文的阅读位置")
        accepted(http.put(path + "/reading-position", json=current_position))
        assert accepted(http.get(path))["readingPosition"] == current_position
