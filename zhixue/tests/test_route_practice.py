from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from backend.backup_snapshot import read_backup
from backend.library import local_path
from backend.worker import prepare_one
from test_model_study import consent_request, run, session, setup_model, transport_recorder
from test_study_intake import accepted, open_client


def project_path(identity):
    return f"/api/projects/{identity}"


def practice(after, *, required=False):
    return {
        "afterId": after, "title": "汇总一周学习记录", "outcome": "保存一张主题汇总表",
        "instructions": "选取五条学习记录，按主题归纳并核对总时长。\n\n把结果和检查过程写到笔记。",
        "required": required,
    }


def save_route(http, project, change):
    preview = accepted(http.post(f"{project}/route-order/preview", json=change))
    signed = {**change, "confirmation": preview["confirmation"]}
    saved = accepted(http.post(f"{project}/route-order/confirm", json=signed))
    return preview, saved


def test_route_marks_and_practice_keep_learning_history_and_retry_file_conflicts(tmp_path):
    identity = str(uuid4())
    project = project_path(identity)
    with open_client(tmp_path) as http:
        created = accepted(http.post("/api/projects", json={
            "id": identity, "topic": "SQLite", "outcome": "整理学习记录",
        }))
        while prepare_one(tmp_path):
            pass
        order = [lesson["id"] for lesson in created["lessons"]]
        for lesson in order:
            accepted(http.put(f"/api/lessons/{lesson}/learned", json={"learned": True}))
        first = f"/api/lessons/{order[0]}"
        accepted(http.put(first + "/note", json={"content": "旧课程的私人笔记"}))
        before = accepted(http.get(project + "/versions"))
        reviews = accepted(http.get("/api/reviews"))["reviews"]
        change = {"baseVersion": 1, "order": order, "marks": {
            order[0]: "known", order[1]: "to_learn", order[2]: "to_reinforce",
        }, "practice": practice(order[0])}
        endpoint = project + "/route-order"
        preview = accepted(http.post(endpoint + "/preview", json=change))
        added = preview["after"][1]
        assert added["kind"] == "practice" and added["phase"] == preview["before"][0]["phase"]
        assert accepted(http.get(project + "/versions")) == before
        assert not local_path(tmp_path, "courses", added["id"]).exists()
        signed = {**change, "confirmation": preview["confirmation"]}
        altered = {**signed, "practice": {**signed["practice"], "instructions": "替换未确认的说明"}}
        assert http.post(endpoint + "/confirm", json=altered).status_code == 409
        _, order_changed = save_route(http, project, {"baseVersion": 1, "order": list(reversed(order))})
        assert order_changed["version"] == 2
        assert http.post(endpoint + "/confirm", json=signed).status_code == 409
        current = {**change, "baseVersion": 2, "order": list(reversed(order))}
        preview = accepted(http.post(endpoint + "/preview", json=current))
        signed = {**current, "confirmation": preview["confirmation"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            saved, replayed = pool.map(
                lambda _: accepted(http.post(endpoint + "/confirm", json=signed)), range(2),
            )
        assert saved == replayed
        node = next(item for item in saved["nodes"] if item.get("kind") == "practice")
        assert node == preview["after"][-1] and saved["version"] == 3
        assert accepted(http.post(endpoint + "/confirm", json=signed)) == saved
        path = local_path(tmp_path, "courses", node["id"])
        path.write_text("外部同名文件", encoding="utf-8")
        assert prepare_one(tmp_path) and not prepare_one(tmp_path)
        lesson_path = f"/api/lessons/{node['id']}"
        state = accepted(http.get(project))
        entry = state["lessons"][-1]
        assert entry["preparationError"] and not entry["ready"]
        assert path.read_text(encoding="utf-8") == "外部同名文件"
        path.rename(path.with_suffix(".saved"))
        accepted(http.post(lesson_path + "/practice/retry"))
        assert prepare_one(tmp_path) and not prepare_one(tmp_path)
        detail = accepted(http.get(lesson_path))
        assert detail["body"] == path.read_text(encoding="utf-8")
        assert change["practice"]["instructions"] in detail["body"]
        assert detail["objectives"] == [] and detail["courseVersion"] is None
        selected = accepted(http.get(f"/api/files/course-{node['id']}"))
        assert selected["name"] == "汇总一周学习记录"
        assert accepted(http.get(project))["status"] == "done"
        board = accepted(http.post("/api/project-board/query", json={"view": {"layout": "board"}}))
        assert board["pages"]["done"]["items"][0]["progress"] == 100
        plan = accepted(http.get("/api/study-plan"))
        assert not any(item.get("lessonId") == node["id"] for day in plan["days"] for item in day["tasks"])
        accepted(http.put(lesson_path + "/learned", json={"learned": True}))
        after = accepted(http.get("/api/study-plan"))
        assert after["days"][0]["estimatedCompletedMinutes"] == plan["days"][0]["estimatedCompletedMinutes"]
        assert accepted(http.get("/api/reviews"))["reviews"] == reviews
        assert accepted(http.get(first))["note"] == "旧课程的私人笔记"

    with open_client(tmp_path) as http:
        route_history = accepted(http.get(project + "/versions"))["routes"]
        assert [route["version"] for route in route_history] == [3, 2, 1]
        assert route_history[-1] == before["routes"][0]
        state = accepted(http.get(project))
        labels = {item["id"]: item["learningMark"] for item in state["lessons"]}
        assert labels == {**change["marks"], node["id"]: None}
        assert state["lessons"][-1]["learnedOn"]
        required_change = {
            "baseVersion": 3, "order": [item["id"] for item in state["lessons"]],
            "marks": {order[0]: None}, "practice": {**practice(node["id"], required=True), "title": "必学实战"},
        }
        _, updated = save_route(http, project, required_change)
        required_id = updated["nodes"][-1]["id"]
        assert accepted(http.get(project))["status"] == "active"
        assert not next(item for item in updated["nodes"] if item["id"] == order[0]).get("learningMark")
        required_path = local_path(tmp_path, "courses", required_id)
        same_content = f"# 必学实战\n\n## 预期成果\n\n{change['practice']['outcome']}\n\n## 实战说明\n\n{change['practice']['instructions']}\n"
        required_path.write_bytes(same_content.encode())
        original_inode = required_path.stat().st_ino
        assert prepare_one(tmp_path)
        assert required_path.stat().st_ino == original_inode
        plan = accepted(http.get("/api/study-plan"))
        assert any(item.get("lessonId") == required_id for day in plan["days"] for item in day["tasks"])
        archive = accepted(http.post("/api/backups", json={"password": "isolated-route-practice"}))
        saved = read_backup(Path(archive["directory"], archive["name"]), "isolated-route-practice").manifest
        assert saved.counts["route_versions"] == 4
        entries = {record.path: record.revision for record in saved.files}
        for file in (path, required_path):
            assert entries[file.relative_to(tmp_path).as_posix()] == sha256(file.read_bytes()).hexdigest()


def test_model_route_practice_is_prepared_locally_and_keeps_the_model_window(tmp_path):
    traffic = []
    with session(tmp_path) as http:
        setup_model(http)
        submit, target, disclosure = consent_request(http, 10)
        created = accepted(http.post(submit, json={**target, "modelConsent": disclosure["consent"]}))
        assert run(tmp_path, transport_recorder(traffic))
        project = project_path(created["projectId"])
        generation = project + "/generation"
        accepted(http.post(generation + "/control", json={"action": "stop"}))
        before = accepted(http.get(generation))
        ids = [item["id"] for item in accepted(http.get(project))["lessons"]]
        _, saved = save_route(http, project, {"baseVersion": 1, "order": ids, "practice": practice(ids[0])})
        added = saved["nodes"][1]["id"]
        assert prepare_one(tmp_path) and not prepare_one(tmp_path)
        assert not run(tmp_path, transport_recorder(traffic)) and len(traffic) == 1
        after = accepted(http.get(generation))
        assert (after["window"], after["steps"], after["usage"], after["state"]) == (
            before["window"], before["steps"], before["usage"], "stopped",
        )
        assert http.put(generation + "/focus", json={"lessonId": added}).status_code == 422
        lesson_path = f"/api/lessons/{added}"
        assert "选取五条学习记录" in accepted(http.get(lesson_path))["body"]
        accepted(http.put(lesson_path + "/learned", json={"learned": True}))
        assert not any(item["lessonId"] == added for item in accepted(http.get("/api/reviews"))["reviews"])


def test_required_practice_does_not_replace_an_existing_stage_quiz(tmp_path):
    identity = str(uuid4())
    project = project_path(identity)
    with open_client(tmp_path) as http:
        created = accepted(http.post("/api/projects", json={"id": identity, "topic": "SQLite", "outcome": "练习查询"}))
        while prepare_one(tmp_path):
            pass
        order = [item["id"] for item in created["lessons"]]
        accepted(http.put(f"/api/lessons/{order[0]}/learned", json={"learned": True}))
        endpoint = project + "/stage-assessments"
        before = accepted(http.get(endpoint))
        assert any(stage["eligible"] and stage["hasQuiz"] for stage in before["items"])
        _, saved = save_route(http, project, {"baseVersion": 1, "order": order, "practice": practice(order[0], required=True)})
        assert accepted(http.get(endpoint)) == before
        assert prepare_one(tmp_path)
        accepted(http.put(f"/api/lessons/{saved['nodes'][1]['id']}/learned", json={"learned": True}))
        assert accepted(http.get(endpoint)) == before


def test_optional_practice_does_not_reopen_a_finished_optional_model_route(tmp_path):
    from test_course_verification import PLAN, authorize, model_traffic, prepare, publish

    calls = []
    optional_plan = {"stages": [{**stage, "lessons": [{**item, "required": False} for item in stage["lessons"]]}
                                for stage in PLAN["stages"]]}
    with session(tmp_path) as http:
        identity, lesson, _, scope = prepare(http, tmp_path, calls, route=optional_plan)
        authorize(http, identity, scope)
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        accepted(http.put(f"/api/lessons/{lesson}/learned", json={"learned": True}))
        project = project_path(identity)
        assert accepted(http.get(project))["status"] == "done"
        save_route(http, project, {"baseVersion": 1, "order": [lesson], "practice": practice(lesson)})
        assert accepted(http.get(project))["status"] == "done"
        board = accepted(http.post("/api/project-board/query", json={"view": {"layout": "board"}}))
        row, = board["pages"]["done"]["items"]
        assert row["progress"] == 100 and row["nextLesson"] is None
