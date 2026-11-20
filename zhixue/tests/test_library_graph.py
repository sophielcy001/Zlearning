from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.graph_layout import add_anchors, cell
from backend.worker import prepare_one
from test_course_verification import authorize, model_traffic, prepare, publish
from test_demo import project
from test_library_trash import move_to_trash, restore
from test_model_study import PLAN, ok, run, session
from test_note_ai import new_note


GRAPH = "/api/library/graph"


def graph(http):
    return ok(http.get(GRAPH))


def nodes(data):
    return {node["id"]: node for node in data["nodes"]}


def pairs(data):
    return {frozenset((edge["source"], edge["target"])) for edge in data["edges"]}


def edit(http, data, kind, **action):
    return ok(http.post(GRAPH + "/edit", json={"revision": data["revision"], "action": {"kind": kind, **action}}))


def document(http, name, text):
    return ok(http.post("/api/files", json={"name": name, "content": text}))["id"]


def test_graph_reads_real_note_and_wiki_edges_and_current_review_state(tmp_path):
    app = create_app(tmp_path, "graph", today=lambda: date(2027, 1, 31))
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer graph"}) as http:
        saved = project(http)
        lesson_id = saved["lessons"][0]["id"]
        course_id = "course-" + lesson_id
        before = nodes(graph(http))[course_id]
        assert not before["ready"] and not before["learned"]
        while prepare_one(tmp_path):
            pass

        first = document(http, "入门.md", "第一份")
        second = document(http, "入门.txt", "第二份")
        note_id, endpoint = new_note(http, lesson_id, f"[[{first}|原件]]\n[[入门]]\n`[[{second}]]`\n")
        note_node = "note-" + note_id
        data = graph(http)
        entries = nodes(data)
        assert entries[course_id]["position"] == before["position"]
        assert entries[note_node]["projectIds"] == [saved["id"]]
        assert {pair for pair in pairs(data) if note_node in pair} == {
            frozenset((note_node, first)), frozenset((note_node, course_id)),
        }
        assert not entries[course_id]["review"]
        ok(http.put(f"/api/lessons/{lesson_id}/learned", json={"learned": True}))
        assert not nodes(graph(http))[course_id]["review"]
        ok(http.post("/api/demo/next-day"))
        ready = nodes(graph(http))[course_id]
        assert ready["learned"] and ready["review"]["lessonId"] == lesson_id
        assert ready["review"]["kind"] == "local" and not ready["weak"]

        archive = move_to_trash(http, first)
        active = nodes(graph(http))
        assert first not in active and active[second]["position"] == entries[second]["position"]
        # Removal makes the once ambiguous title resolve to the remaining document.
        assert frozenset((note_node, second)) in pairs(graph(http))
        assert restore(http, archive).is_success
        restored = graph(http)
        assert nodes(restored)[first]["position"] == entries[first]["position"]
        assert frozenset((note_node, second)) not in pairs(restored)
        assert f"[[{first}|原件]]" in ok(http.get(endpoint))["content"]


def test_graph_uses_verified_source_revision_and_known_prerequisites(tmp_path):
    with session(tmp_path) as http:
        calls = []
        project_id, lesson, source, evidence = prepare(http, tmp_path, calls, route=PLAN)
        authorize(http, project_id, evidence)
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        course = "course-" + lesson
        data = graph(http)
        assert frozenset((course, source["id"])) in pairs(data)
        assert nodes(data)[source["id"]]["projectIds"] == nodes(data)[course]["projectIds"]
        project_nodes = [node for node in data["nodes"] if node["lessonId"] and node["projectIds"] == nodes(data)[course]["projectIds"]]
        assert any(node["id"] != course and frozenset((node["id"], course)) in pairs(data) for node in project_nodes)

        actual = Path(tmp_path, nodes(data)[source["id"]]["path"])
        actual.write_text("资料被外部更新，原来的引文已失效。\n", encoding="utf-8")
        assert frozenset((course, source["id"])) not in pairs(graph(http))


def test_graph_edits_and_views_survive_restart_without_changing_originals(tmp_path):
    with session(tmp_path) as http:
        first = document(http, "材料.txt", "原始资料")
        second = document(http, "笔记段落.md", f"参考 [[{first}]]")
        data = graph(http)
        original = {node["id"]: Path(tmp_path, node["path"]).read_bytes() for node in data["nodes"]}
        assert len(data["edges"]) == 1 and data["edges"][0]["source"] == second
        moved = edit(http, data, "move", id=first, position={"x": 140.25, "y": 88.5})
        assert http.post(GRAPH + "/edit", json={
            "revision": data["revision"], "action": {"kind": "visibility", "ids": [first], "hidden": True},
        }).status_code == 409
        data = edit(http, moved, "link", source=second, target=first, direction="both")
        assert data["edges"][0]["both"] and data["edges"][0]["manual"]
        data = edit(http, data, "link", source=second, target=first, direction="remove")
        assert not data["edges"]
        data = edit(http, data, "undo")
        assert data["edges"][0]["both"] and data["canRedo"]
        data = edit(http, data, "redo")
        assert not data["edges"] and not data["canRedo"]
        data = edit(http, data, "link", source=second, target=first, direction="default")
        assert not data["edges"][0]["both"] and not data["edges"][0]["manual"]

        group_id = str(uuid4())
        group = {"id": group_id, "name": "真实文件关系", "members": [first, second]}
        grouped = edit(http, data, "group", group=group)
        data = edit(http, grouped, "moveGroup", id=group_id, delta={"x": -15, "y": 30})
        assert nodes(data)[first]["position"] == {"x": 125.25, "y": 118.5}
        assert nodes(data)[second]["position"]["x"] == nodes(grouped)[second]["position"]["x"] - 15
        data = edit(http, data, "visibility", ids=[first, second], hidden=True)
        hidden_revision = data["head"]
        assert all(item["hidden"] for item in data["nodes"])
        assert {item["id"] for item in ok(http.get("/api/files"))} == {first, second}
        view = {**data["view"], "motion": False, "collapsed": [group_id], "zoom": 1.5,
                "center": {"x": -15, "y": 33}, "folder": "imports"}
        assert ok(http.put(GRAPH + "/view", json=view)) == view
        data = edit(http, data, "favorite", favorite={"id": str(uuid4()), "name": "查看这个位置", "view": view})

    with session(tmp_path) as http:
        data = graph(http)
        assert data["view"] == view and data["favorites"][0]["view"] == view
        assert all(node["hidden"] for node in data["nodes"])
        restored = edit(http, data, "visibility", ids=[first, second], hidden=False)
        assert not any(node["hidden"] for node in restored["nodes"])
        historical = edit(http, restored, "restore", id=hidden_revision)
        assert all(node["hidden"] for node in historical["nodes"])
        assert any(item["id"] == hidden_revision for item in ok(http.get(GRAPH + "/history"))["items"])
        assert original == {node["id"]: Path(tmp_path, node["path"]).read_bytes() for node in data["nodes"]}
        assert ok(http.get("/api/files/trash")) == []


def test_graph_anchors_keep_existing_nodes_stable_at_5000_nodes():
    keys = [f"course-{index}" for index in range(2500)] + [f"note-{index}" for index in range(2500)]
    catalog = {identity: {"kind": identity.split("-")[0], "projectIds": ["p"]} for identity in keys}
    left = {identity: catalog[identity] for identity in keys[:1000]}
    anchors = add_anchors({}, left, {})
    edges = {f"course-{index}|note-{index}": "forward" for index in range(2500)}
    layout = add_anchors(anchors, catalog, edges)
    assert len(layout) == len({cell(position) for position in layout.values()}) == 5000
    assert {identity: layout[identity] for identity in left} == anchors
    assert add_anchors(layout, catalog, edges) is layout


def test_graph_archive_import_previews_merge_and_restore_and_leaves_files_intact(tmp_path):
    with session(tmp_path) as http:
        note_text = "只存在于原件的自用正文。"
        reference = document(http, "公开参考.txt", "可以补充细节。")
        note_id, endpoint = new_note(http, text=f"{note_text}\n[[{reference}]]")
        note = "note-" + note_id
        data = graph(http)
        data = edit(http, data, "move", id=note, position={"x": 55, "y": -40})
        archived_view = {**data["view"], "focus": note, "local": True, "zoom": .92, "folder": "notes"}
        ok(http.put(GRAPH + "/view", json=archived_view))
        download = http.get(GRAPH + "/archive")
        archive = ok(download)
        assert note_text not in download.text
        assert archive["document"]["positions"][note] == {"x": 55, "y": -40}
        assert archive["edges"][0]["source"] == note and archive["edges"][0]["target"] == reference

        unknown = str(uuid4())
        archive["nodes"].append({"id": unknown, "kind": "import", "title": "属于另一个知识库"})
        archive["document"]["positions"][unknown] = {"x": 200, "y": 400}
        archive["document"]["links"][min(note, unknown) + "|" + max(note, unknown)] = "both"
        data = edit(http, data, "move", id=note, position={"x": 250, "y": 155})
        data = edit(http, data, "visibility", ids=[note], hidden=True)
        raw = ok(http.get(endpoint))
        user_content = note_text + "\n用户随后删除了双链。"
        ok(http.put(endpoint, json={"baseRevision": raw["revision"], "content": user_content}))
        current = graph(http)
        assert not current["edges"]
        ok(http.put(GRAPH + "/view", json={**archived_view, "folder": "", "focus": None, "local": False}))

        payload = {"revision": current["revision"], "mode": "merge", "archive": archive}
        digest = ok(http.post(GRAPH + "/import/preview", json=payload))
        assert (digest["matched"], digest["skipped"], digest["changes"]["links"]) == (2, 1, 1)
        assert digest["changes"]["positions"] == 0 and digest["changes"]["visibility"] == 0
        assert not graph(http)["edges"]
        tampered = {**payload, "mode": "restore", "confirmation": digest["confirmation"]}
        assert http.post(GRAPH + "/import", json=tampered).status_code == 409
        merged = ok(http.post(GRAPH + "/import", json={**payload, "confirmation": digest["confirmation"]}))
        assert nodes(merged)[note]["hidden"] and nodes(merged)[note]["position"] == {"x": 250, "y": 155}
        assert merged["edges"][0]["manual"] and not merged["edges"][0]["automatic"]
        assert unknown not in nodes(merged)

        payload = {**payload, "revision": merged["revision"], "mode": "restore"}
        digest = ok(http.post(GRAPH + "/import/preview", json=payload))
        assert digest["changes"]["view"] == digest["changes"]["positions"] == digest["changes"]["visibility"] == 1
        restored = ok(http.post(GRAPH + "/import", json={**payload, "confirmation": digest["confirmation"]}))
        assert restored["view"] == archived_view and not nodes(restored)[note]["hidden"]
        assert nodes(restored)[note]["position"] == {"x": 55, "y": -40}
        assert ok(http.get(endpoint))["content"] == user_content
        assert http.post(GRAPH + "/import", json={**payload, "confirmation": digest["confirmation"]}).status_code == 409
        assert edit(http, restored, "undo")["nodes"] == merged["nodes"]
