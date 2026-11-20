from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.backup_snapshot import DATABASE, read_backup
from backend.db import ROOT, GoalVersion, open_db, transaction
from backend.project_versions import goal_field
from backend.worker import prepare_one


URL = "/api/conversations"
AUTH = {"Authorization": "Bearer intake-fixture-session"}


def open_client(folder):
    return TestClient(create_app(folder, "intake-fixture-session"), base_url="http://127.0.0.1", headers=AUTH)


def accepted(response):
    assert response.is_success, response.text
    return response.json()


def message(text):
    return {"id": str(uuid4()), "text": text}


def start(http, text, **options):
    payload = {"id": str(uuid4()), "message": message(text), **options}
    created = http.post(URL, json=payload)
    assert created.status_code == 201, created.text
    return payload, created.json()


def goal_values(draft):
    return {field["key"]: field["value"] for field in draft["fields"]}


def test_conversation_history_is_durable_and_retries_reuse_the_same_message(tmp_path):
    with open_client(tmp_path) as http:
        payload, first = start(http, "我想理解 SQL 的筛选")
        assert first["goal"] is None and first["messages"][0]["role"] == "user"
        assert first["messages"][0]["text"] == payload["message"]["text"]
        assert first["messages"][1]["role"] == "demo"
        assert accepted(http.post(URL, json=payload))["id"] == first["id"]
        followup = message("用一个很小的表格举例")
        endpoint = f"{URL}/{first['id']}/messages"
        updated = accepted(http.post(endpoint, json=followup))
        assert len(updated["messages"]) == 4
        assert accepted(http.post(endpoint, json=followup))["messages"] == updated["messages"]
        assert http.post(endpoint, json={**followup, "text": "另一个要求"}).status_code == 409
        _, other = start(http, "我想了解法语发音")
        assert other["id"] != first["id"]
        assert not accepted(http.get("/api/state"))["recentProjects"]

    with open_client(tmp_path) as http:
        history = accepted(http.get(URL))
        assert history["total"] == 2 and {item["id"] for item in history["items"]} == {first["id"], other["id"]}
        assert accepted(http.get(f"{URL}/{first['id']}"))["messages"] == updated["messages"]
        assert len(accepted(http.get(f"{URL}/{other['id']}"))["messages"]) == 2


def test_goal_draft_uses_optional_fields_and_editable_evidence_then_creates_one_versioned_project(tmp_path):
    with open_client(tmp_path) as http:
        source = accepted(http.post("/api/files", json={"name": "口语笔记.txt", "content": "点餐问候"}))
        _, thread = start(http, "我想练习法语口语", attachments=[source["id"]])
        endpoint = f"{URL}/{thread['id']}/goal"
        draft = accepted(http.post(endpoint))
        assert goal_values(draft)["topic"] == "法语口语"
        assert "practiceScene" in goal_values(draft)
        assert any(field["origin"] == "inference" for field in draft["fields"])
        file_source, = [item for item in draft["evidence"] if item["kind"] == "file"]
        assert file_source["target"] == source["id"]
        edits = {
            "goalId": draft["id"], "revision": draft["revision"],
            "values": {**goal_values(draft), "outcome": "用法语完成一次点餐", "practiceScene": "餐馆"},
            "removeEvidence": [file_source["id"]],
        }
        saved = accepted(http.put(endpoint, json=edits))
        assert saved["revision"] > draft["revision"]
        assert file_source["id"] not in {item["id"] for item in saved["evidence"]}
        assert http.put(endpoint, json=edits).status_code == 409
        assert not accepted(http.get("/api/state"))["recentProjects"]
        assert accepted(http.post(endpoint)) == saved

    with open_client(tmp_path) as http:
        recovered = accepted(http.get(f"{URL}/{thread['id']}"))
        assert goal_values(recovered["goal"])["practiceScene"] == "餐馆"
        assert recovered["attachments"][0]["id"] == source["id"]
        submission = {"goalId": saved["id"], "revision": saved["revision"], "values": goal_values(saved)}
        provided = accepted(http.post(f"{endpoint}/submit", json=submission))
        repeated = accepted(http.post(f"{endpoint}/submit", json=submission))
        assert repeated == provided
        assert provided["projectId"] == draft["id"] and provided["goalVersion"] == 1 and provided["routeVersion"] == 1
        assert http.post(f"{endpoint}/submit", json={**submission, "values": {
            **submission["values"], "outcome": "其他成果",
        }}).status_code == 409
        versions = accepted(http.get(f"/api/projects/{provided['projectId']}/versions"))
        goal_version, = versions["goals"]
        route_version, = versions["routes"]
        assert goal_version["conversationId"] == thread["id"]
        assert goal_values(goal_version)["outcome"] == "用法语完成一次点餐"
        assert "file" not in {item["kind"] for item in goal_version["evidence"]}
        project = accepted(http.get(f"/api/projects/{provided['projectId']}"))
        assert [item["id"] for item in route_version["nodes"]] == [item["id"] for item in project["lessons"]]
        assert route_version["goalVersion"] == goal_version["version"] == 1
        archived = accepted(http.get(f"{URL}/{thread['id']}"))
        assert archived["goal"] is None and archived["projects"][0]["id"] == provided["projectId"]
        assert len(accepted(http.get("/api/state"))["recentProjects"]) == 1


def test_legacy_tab_import_preserves_draft_and_safe_retries(tmp_path):
    legacy = {
        "messages": [{"role": "user", "text": "我想学习 SQLite"}, {"role": "demo", "text": "从查询开始。"}],
        "goal": {"id": str(uuid4()), "topic": "SQLite 入门", "outcome": "会排序", "foundation": "了解表格"},
    }
    with open_client(tmp_path) as http:
        imported = accepted(http.post(f"{URL}/legacy", json=legacy))
        assert imported["messages"][0]["text"] == "我想学习 SQLite"
        assert goal_values(imported["goal"]) == {key: value for key, value in legacy["goal"].items() if key != "id"}
        assert imported["goal"]["id"] == legacy["goal"]["id"]
        assert accepted(http.post(f"{URL}/legacy", json=legacy))["id"] == imported["id"]
        assert accepted(http.get(URL))["total"] == 1


def test_related_learned_project_is_opt_in_and_backups_include_conversations(tmp_path):
    with open_client(tmp_path) as http:
        project = accepted(http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "SQLite", "outcome": "读取表格", "foundation": "",
        }))
        unrelated = accepted(http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "大提琴", "outcome": "演奏练习曲", "foundation": "",
        }))
        while prepare_one(tmp_path):
            pass
        for target in [project, unrelated]:
            accepted(http.put(f"/api/lessons/{target['lessons'][0]['id']}/learned", json={"learned": True}))
        _, thread = start(http, "我想深入学习 SQLite 索引")
        endpoint = f"{URL}/{thread['id']}/goal"
        draft = accepted(http.post(endpoint))
        evidence, = [item for item in draft["evidence"] if item["kind"] == "project"]
        assert evidence["target"] == project["id"]
        assert "已学 1 节" in goal_values(draft)["foundation"]
        trimmed = accepted(http.put(endpoint, json={
            "goalId": draft["id"], "revision": draft["revision"],
            "values": goal_values(draft), "removeEvidence": [evidence["id"]],
        }))
        assert goal_values(trimmed)["foundation"] == ""

        archive = accepted(http.post("/api/backups", json={"password": "synthetic-intake-backup"}))
        path = Path(archive["directory"]) / archive["name"]
        copied = tmp_path / "checked-chat.sqlite3"
        with copied.open("wb") as handle:
            verified = read_backup(path, "synthetic-intake-backup", extract={DATABASE: handle})
        assert verified.manifest.database.path == DATABASE
        assert verified.manifest.counts["conversations"] == 1
        assert verified.manifest.counts["chat_messages"] == 2
        assert verified.manifest.counts["goal_versions"] == 2
        assert verified.manifest.counts["route_versions"] == 2
        with closing(sqlite3.connect(copied)) as snapshot:
            assert snapshot.execute(
                "SELECT text FROM chat_messages WHERE conversation_id=? AND role='user'", (thread["id"],),
            ).fetchone() == ("我想深入学习 SQLite 索引",)


def test_confirmed_route_order_reuses_existing_lessons_and_rejects_stale_previews(tmp_path):
    with open_client(tmp_path) as http:
        created = accepted(http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "SQLite 入门", "outcome": "练习查询", "foundation": "",
        }))
        while prepare_one(tmp_path):
            pass
        ids = [lesson["id"] for lesson in created["lessons"]]
        original = accepted(http.get(f"/api/lessons/{ids[0]}"))
        accepted(http.put(f"/api/lessons/{ids[0]}/note", json={"content": "# 可以继续编辑"}))
        accepted(http.put(f"/api/lessons/{ids[0]}/learned", json={"learned": True}))
        url = f"/api/projects/{created['id']}"
        preview = f"{url}/route-order/preview"
        confirm = f"{url}/route-order/confirm"
        assert http.post(preview, json={"baseVersion": 1, "order": ids[:2]}).status_code == 422
        order = {"baseVersion": 1, "order": [ids[2], ids[0], ids[1]]}
        proposed = accepted(http.post(preview, json=order))
        stale = {"baseVersion": 1, "order": list(reversed(ids))}
        stale_preview = accepted(http.post(preview, json=stale))
        assert [lesson["id"] for lesson in accepted(http.get(url))["lessons"]] == ids
        assert http.post(confirm, json={**order, "confirmation": stale_preview["confirmation"]}).status_code == 409
        applied = accepted(http.post(confirm, json={**order, "confirmation": proposed["confirmation"]}))
        assert applied["version"] == 2
        assert accepted(http.post(confirm, json={**order, "confirmation": proposed["confirmation"]})) == applied
        assert http.post(confirm, json={**stale, "confirmation": stale_preview["confirmation"]}).status_code == 409

    with open_client(tmp_path) as http:
        project = accepted(http.get(url))
        assert [lesson["id"] for lesson in project["lessons"]] == order["order"]
        assert project["lessons"][1]["learnedOn"]
        assert accepted(http.get("/api/state"))["continuation"]["nextLesson"]["id"] == ids[2]
        preserved = accepted(http.get(f"/api/lessons/{ids[0]}"))
        assert preserved["body"] == original["body"] and "# 可以继续编辑" in preserved["note"]
        versions = accepted(http.get(f"{url}/versions"))["routes"]
        assert [[node["id"] for node in item["nodes"]] for item in versions] == [order["order"], ids]


def test_local_course_actions_are_bound_to_the_linked_lesson_before_any_route_write(tmp_path):
    with open_client(tmp_path) as http:
        project = accepted(http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "SQLite", "outcome": "练习查询", "foundation": "",
        }))
        identity = project["lessons"][1]["id"]
        scope = {"projectId": project["id"], "lessonId": identity}
        version_path = f"/api/projects/{project['id']}/versions"
        before = accepted(http.get(version_path))
        command = "这节我已经会了。"
        payload, first = start(http, command, lessonId=identity)
        assert first["courseAction"] == {"kind": "known", **scope}
        assert first["messages"][0]["text"] == command
        assert first["messages"][1]["role"] == "assistant"
        assert accepted(http.post(URL, json=payload)) == first
        endpoint = f"{URL}/{first['id']}/messages"
        update = message("请把这个知识点提前！")
        second = accepted(http.post(endpoint, json=update))
        assert second["courseAction"] == {"kind": "earlier", **scope}
        assert accepted(http.post(endpoint, json=update)) == second
        edit = accepted(http.post(endpoint, json=message("修改我的学习目标")))
        assert edit["courseAction"] == {"kind": "goal", **scope}
        assert edit["messages"][-2]["courseCommand"] is True
        assert http.post(f"{URL}/{first['id']}/goal").status_code == 409
        ordinary = accepted(http.post(endpoint, json=message("怎样理解“这节我已经会了”？")))
        assert ordinary.get("courseAction") is None and ordinary["messages"][-1]["role"] == "demo"
        prepared = accepted(http.post(f"{URL}/{first['id']}/goal"))
        assert [(source["label"], source["excerpt"]) for source in prepared["evidence"] if source["kind"] == "message"] == [
            ("本次学习需求", "怎样理解“这节我已经会了”？"),
        ]
        for question in ("这节我已经会了？", "把这个知识点提前?!"):
            ordinary = accepted(http.post(endpoint, json=message(question)))
            assert ordinary.get("courseAction") is None and ordinary["messages"][-1]["role"] == "demo"
        _, unlinked = start(http, command)
        assert unlinked.get("courseAction") is None
        assert accepted(http.get(version_path)) == before

    with open_client(tmp_path) as http:
        replay = accepted(http.get(f"{URL}/{first['id']}"))
        assert replay.get("courseAction") is None
        assert replay["messages"][0]["text"] == command
        assert accepted(http.get(version_path)) == before


def test_goal_revision_keeps_route_history_and_learning_records(tmp_path):
    inputs = {"id": str(uuid4()), "topic": "SQLite 入门", "outcome": "练习查询", "foundation": "会用表格"}
    project_path = f"/api/projects/{inputs['id']}"
    endpoint = f"{project_path}/goal-revision"
    with open_client(tmp_path) as http:
        project = accepted(http.post("/api/projects", json=inputs))
        while prepare_one(tmp_path):
            pass
        lessons = [lesson["id"] for lesson in project["lessons"]]
        lesson_path = f"/api/lessons/{lessons[0]}"
        original_lesson = accepted(http.get(lesson_path))
        accepted(http.put(f"{lesson_path}/note", json={"content": "保留原学习笔记"}))
        accepted(http.put(f"{lesson_path}/learned", json={"learned": True}))
        objective_path = f"{lesson_path}/reviews/{original_lesson['objectives'][0]['key']}"
        original_review = accepted(http.get(objective_path))
        original = accepted(http.get(f"{project_path}/versions"))
        payload = {
            "baseGoalVersion": 1, "baseRouteVersion": 1,
            "values": {"topic": "SQLite 查询进阶", "outcome": "用分组查询生成报表", "deadline": "2027-04-12"},
        }
        preview = accepted(http.post(f"{endpoint}/preview", json=payload))
        assert preview["changes"] == [
            {"key": "topic", "label": "学习主题", "before": inputs["topic"], "after": "SQLite 查询进阶"},
            {"key": "outcome", "label": "预期成果", "before": inputs["outcome"], "after": "用分组查询生成报表"},
            {"key": "deadline", "label": "目标日期", "before": "", "after": "2027-04-12"},
        ]
        assert not preview["requiresModelConsent"]
        invalid = {**payload, "values": {"deadline": "2027-02-29"}}
        assert http.post(f"{endpoint}/preview", json=invalid).status_code == 422
        assert accepted(http.get(f"{project_path}/versions")) == original
        forged = {**payload, "values": {"outcome": "未经预览的改动"}, "confirmation": preview["confirmation"]}
        assert http.post(f"{endpoint}/confirm", json=forged).status_code == 409
        reorder = {"baseVersion": 1, "order": list(reversed(lessons))}
        order_preview = accepted(http.post(f"{project_path}/route-order/preview", json=reorder))
        accepted(http.post(f"{project_path}/route-order/confirm", json={**reorder, "confirmation": order_preview["confirmation"]}))
        assert http.post(f"{endpoint}/confirm", json={**payload, "confirmation": preview["confirmation"]}).status_code == 409
        payload["baseRouteVersion"] = 2
        preview = accepted(http.post(f"{endpoint}/preview", json=payload))
        signed = {**payload, "confirmation": preview["confirmation"]}
        result = accepted(http.post(f"{endpoint}/confirm", json=signed))
        assert result == {"goalVersion": 2, "routeVersion": 3}
        assert accepted(http.post(f"{endpoint}/confirm", json=signed)) == result
        assert accepted(http.post("/api/projects", json=inputs))["topic"] == "SQLite 查询进阶"

    with open_client(tmp_path) as http:
        current = accepted(http.get(project_path))
        assert current["outcome"] == "用分组查询生成报表" and current["foundation"] == inputs["foundation"]
        assert [lesson["id"] for lesson in current["lessons"]] == reorder["order"]
        preserved = accepted(http.get(lesson_path))
        assert preserved["body"] == original_lesson["body"] and preserved["note"] == "保留原学习笔记"
        assert current["learned"] == 1
        assert accepted(http.get(objective_path)) == {**original_review, "projectTopic": current["topic"]}
        history = accepted(http.get(f"{project_path}/versions"))
        latest, previous = history["goals"]
        assert previous == original["goals"][0]
        assert goal_values(latest)["deadline"] == "2027-04-12"
        assert (history["routes"][0]["version"], history["routes"][0]["goalVersion"]) == (3, 2)
        assert history["routes"][0]["nodes"] == history["routes"][1]["nodes"]
        archive = accepted(http.post("/api/backups", json={"password": "test-goal-revision-backup"}))
        path = Path(archive["directory"]) / archive["name"]
        snapshot_path = tmp_path / "goal-history.sqlite3"
        with snapshot_path.open("wb") as handle:
            verified = read_backup(path, "test-goal-revision-backup", extract={DATABASE: handle})
        assert verified.manifest.counts["goal_versions"] == 2
        assert verified.manifest.counts["route_versions"] == 3
        with closing(sqlite3.connect(snapshot_path)) as database:
            assert database.execute("PRAGMA foreign_key_check").fetchall() == []
            assert database.execute("SELECT goal_version FROM route_versions WHERE version=3").fetchone() == (2,)


def test_goal_revision_reuses_an_existing_dynamic_target_date(tmp_path):
    identity = str(uuid4())
    project = f"/api/projects/{identity}"
    with open_client(tmp_path) as http:
        accepted(http.post("/api/projects", json={"id": identity, "topic": "复习计划", "outcome": "完成示例"}))
        engine = open_db(tmp_path)
        try:
            with transaction(engine) as db:
                goal = db.get(GoalVersion, (identity, 1))
                old_date = goal_field("targetDate", "目标日期", 240, "春假后", "suggestion")
                goal.definition = {**goal.definition, "fields": [*goal.definition["fields"], old_date]}
        finally:
            engine.dispose()
        assert accepted(http.get(project + "/versions"))["goalDateKey"] == "targetDate"
        endpoint = project + "/goal-revision"
        data = {"baseGoalVersion": 1, "baseRouteVersion": 1, "values": {"targetDate": "2027-04-12"}}
        conflicting = {**data, "values": {"deadline": "2027-04-12"}}
        assert http.post(endpoint + "/preview", json=conflicting).status_code == 422
        preview = accepted(http.post(endpoint + "/preview", json=data))
        assert preview["changes"] == [
            {"key": "targetDate", "label": "目标日期", "before": "春假后", "after": "2027-04-12"},
        ]
        accepted(http.post(endpoint + "/confirm", json={**data, "confirmation": preview["confirmation"]}))
        saved = accepted(http.get(project + "/versions"))
        latest = saved["goals"][0]
        assert "deadline" not in goal_values(latest) and goal_values(latest)["targetDate"] == "2027-04-12"
        assert next(field["type"] for field in latest["fields"] if field["key"] == "targetDate") == "date"
        assert goal_values(saved["goals"][1])["targetDate"] == "春假后"


def test_loading_earlier_messages_covers_the_complete_legacy_history(tmp_path):
    messages = [{"role": "user", "text": f"历史问题 {index}"} for index in range(125)]
    with open_client(tmp_path) as http:
        recent = accepted(http.post(f"{URL}/legacy", json={"messages": messages}))
        assert len(recent["messages"]) == 60
        chunks = [recent["messages"]]
        before = recent["olderBefore"]
        while before is not None:
            response = accepted(http.get(f"{URL}/{recent['id']}", params={"before": before}))
            chunks.insert(0, response["messages"])
            before = response["olderBefore"]
        assert [item["text"] for chunk in chunks for item in chunk] == [item["text"] for item in messages]


def test_0009_project_upgrade_backfills_versions_with_legacy_files_and_review_links(tmp_path):
    with open_client(tmp_path) as http:
        project = accepted(http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "SQLite 入门", "outcome": "学会排序", "foundation": "会用表格",
        }))
        while prepare_one(tmp_path):
            pass
        endpoint = f"/api/lessons/{project['lessons'][0]['id']}"
        accepted(http.put(f"{endpoint}/note", json={"content": "升级前笔记"}))
        accepted(http.put(f"{endpoint}/learned", json={"learned": True}))
        previous = accepted(http.get(endpoint))
        reviews = accepted(http.get("/api/reviews"))

    engine = open_db(tmp_path)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0009_backup_copies")
    finally:
        engine.dispose()
    with closing(sqlite3.connect(tmp_path / DATABASE)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0009_backup_copies",)

    with open_client(tmp_path) as http:
        version = accepted(http.get(f"/api/projects/{project['id']}/versions"))
        assert goal_values(version["goals"][0])["foundation"] == "会用表格"
        assert [node["id"] for node in version["routes"][0]["nodes"]] == [lesson["id"] for lesson in project["lessons"]]
        live = accepted(http.get(endpoint))
        assert live["note"] == previous["note"] and live["body"] == previous["body"]
        assert accepted(http.get("/api/reviews"))["reviews"] == reviews["reviews"]
        assert list((tmp_path / "backups" / "database-upgrades").glob("*.sqlite3"))
