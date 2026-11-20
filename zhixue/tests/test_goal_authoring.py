from datetime import UTC, datetime
import json
from uuid import uuid4

from backend.db import open_db
from backend.worker import prepare_one
from test_model_study import ok, session, setup_model
from test_study_intake import goal_values, message, start


def make_goal(http, text="我想学习 Python。", **options):
    _, thread = start(http, text, **options)
    path = f"/api/conversations/{thread['id']}/goal"
    saved = ok(http.post(path))
    request = {"id": str(uuid4()), "goalId": saved["id"], "revision": saved["revision"], "values": {}}
    return thread, path, saved, request


def queue(http, path, payload):
    preview = ok(http.post(path + "/ai/preview", json=payload))
    authorized = {**payload, "consent": preview["consent"]}
    return preview, authorized


def model_once(folder, calls, output):
    from backend.goal_authoring_worker import dispatch_goal_authoring, reserve_goal_authoring

    def transport(address, headers, request):
        calls.append(request["messages"][-1]["content"])
        body = {"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 240}}
        return 200, json.dumps(body, ensure_ascii=False).encode()

    engine = open_db(folder)
    try:
        claim = reserve_goal_authoring(engine, datetime.now(UTC))
        if claim:
            dispatch_goal_authoring(engine, claim, transport=transport)
        return claim is not None
    finally:
        engine.dispose()


def field(key, value, origin, refs, label=None):
    return {"key": key, "value": value, "origin": origin, "sources": refs, **({"label": label} if label else {})}


def test_goal_authoring_discloses_sources_and_only_applies_an_explicit_candidate(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        start(http, "我想练习钢琴。无关对话私有文字。")
        _, history = start(http, "以前用 Python 做过一次简单筛选。")
        project = ok(http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "Python", "outcome": "完成一节数据入门",
        }))
        assert prepare_one(tmp_path)
        lesson = ok(http.get(f"/api/projects/{project['id']}"))["lessons"][0]
        ok(http.put(f"/api/lessons/{lesson['id']}/learned", json={"learned": True}))
        source = ok(http.post("/api/files", json={"name": "练习材料.txt", "content": "每周手工表格\n有两张数据表"}))
        _, thread = start(http, "我只会写 for 循环。", attachments=[source["id"]])
        conversation = f"/api/conversations/{thread['id']}"
        ok(http.post(conversation + "/messages", json=message("我想学习 Python。")))
        path = conversation + "/goal"
        original = ok(http.post(path))
        request = {"id": str(uuid4()), "goalId": original["id"], "revision": original["revision"],
                   "values": {"outcome": "把手工汇总变成独立脚本"}}
        base, _ = queue(http, path, request)
        material = base["material"]["sources"]
        assert all(row["kind"] != "file" for row in material)
        assert any("已学 1 节" in row["text"] and lesson["title"] in row["text"] for row in material)
        assert any("以前用 Python" in row["text"] for row in material)
        assert any("我只会写 for 循环" in row["text"] for row in material)
        nearby, = [row for row in original["evidence"] if row["target"] == history["id"]]
        scope = {**request, "withExcerpts": True, "removeEvidence": [nearby["id"]]}
        preview, authorized = queue(http, path, scope)
        permitted = preview["material"]["sources"]
        file, = [row for row in permitted if row["kind"] == "file"]
        user, = [row for row in permitted if "我只会写 for 循环" in row["text"]]
        assert "两张数据表" in file["text"] and not any("以前用 Python" in row["text"] for row in permitted)
        assert ok(http.get(path + "/ai"))["job"] is None
        assert not model_once(tmp_path, calls, {})
        assert http.post(path + "/ai", json={**authorized, "withExcerpts": False}).status_code == 409

        job = ok(http.post(path + "/ai", json=authorized))
        assert job["state"] == "queued"
        assert ok(http.post(path + "/ai", json=authorized))["id"] == job["id"]
        proposal = {"fields": [
            field("topic", "Python 表格汇总", "inference", [user["key"]]),
            field("foundation", "我只会写 for 循环", "fact", [user["key"]]),
            field("outcome", "覆盖人工填写的文本", "suggestion", []),
            field("practiceData", "每周手工表格", "fact", [file["key"]], "练习数据"),
        ], "question": "还有其他目标吗？"}
        assert model_once(tmp_path, calls, proposal)
        assert not model_once(tmp_path, calls, proposal)
        assert len(calls) == 1 and "两张数据表" in calls[0] and "已学 1 节" in calls[0]
        assert "无关对话" not in calls[0] and "以前用 Python" not in calls[0]
        ready = ok(http.get(path + "/ai"))["job"]
        assert ready["state"] == "ready" and ready["suggestion"]["question"] is None
        assert ok(http.get(conversation))["goal"] == original
        edits = {key: value for key, value in request.items() if key != "id"}
        adoption = path + f"/ai/{job['id']}/apply"
        edits["removeEvidence"] = [nearby["id"]]
        updated = ok(http.post(adoption, json=edits))
        assert ok(http.post(adoption, json=edits)) == updated
        assert http.post(adoption, json={**edits, "values": {"outcome": "另一处未保存编辑"}}).status_code == 409
        values = goal_values(updated)
        assert values["outcome"] == "把手工汇总变成独立脚本" and values["practiceData"] == "每周手工表格"
        fields = {entry["key"]: entry for entry in updated["fields"]}
        assert fields["foundation"]["origin"] == "fact" and fields["practiceData"]["origin"] == "inference"
        assert nearby["id"] not in {item["id"] for item in updated["evidence"]}
        assert ok(http.get(conversation))["projects"] == []
        saved = ok(http.post(path + "/submit", json={
            "goalId": updated["id"], "revision": updated["revision"], "values": {},
        }))
        assert saved["projectId"] == original["id"]


def test_changed_goal_and_edited_attachment_stop_an_already_authorized_request(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        source = ok(http.post("/api/files", json={"name": "核对资料.txt", "content": "先前确认的正文"}))
        _, path, original, payload = make_goal(http, attachments=[source["id"]])
        _, request = queue(http, path, {**payload, "withExcerpts": True})
        ok(http.post(path + "/ai", json=request))
        stored = ok(http.get(f"/api/files/{source['id']}"))
        (tmp_path / stored["path"]).write_text("变化后未确认的正文", encoding="utf-8")
        assert model_once(tmp_path, calls, {})
        assert calls == []
        job = ok(http.get(path + "/ai"))["job"]
        assert job["state"] == "failed" and job["reason"] == "scope_changed"
        assert ok(http.post(path)) == original

        _, next_request = queue(http, path, {**payload, "id": str(uuid4())})
        ok(http.post(path + "/ai", json=next_request))
        assert model_once(tmp_path, calls, {"fields": [], "question": "你希望学完后完成什么？"})
        ready = ok(http.get(path + "/ai"))["job"]
        assert ready["suggestion"]["question"] == "你希望学完后完成什么？"
        ok(http.put(path, json={"goalId": original["id"], "revision": original["revision"],
                               "values": {"outcome": "用户随后明确的目标"}}))
        assert http.post(path + f"/ai/{ready['id']}/apply", json={
            "goalId": original["id"], "revision": original["revision"], "values": {},
        }).status_code == 409


def test_goal_authoring_uses_shared_budget_and_recovers_a_reserved_call(tmp_path):
    from backend.goal_authoring_worker import reserve_goal_authoring, settle_goal_authoring

    budget = "/api/generation/daily-budget"
    now = datetime.now(UTC)
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        ok(http.put("/api/generation/settings", json={"dailyLimit": 1, "concurrency": 1}))
        _, first, _, request = make_goal(http)
        _, confirmed = queue(http, first, request)
        ok(http.post(first + "/ai", json=confirmed))
        assert model_once(tmp_path, calls, {"fields": [], "question": "你想完成什么？"})
        assert ok(http.get(budget))["used"] == 1

        thread, next_path, _, request = make_goal(http, text="我想学习如何筛选数据。")
        _, confirmed = queue(http, next_path, request)
        job = ok(http.post(next_path + "/ai", json=confirmed))
        assert not model_once(tmp_path, calls, {})
        assert ok(http.get(next_path + "/ai"))["job"]["reason"] == "daily_budget"
        ok(http.put("/api/generation/settings", json={"dailyLimit": 2, "concurrency": 1}))
        eligible = ok(http.get(budget))
        task, = eligible["tasks"]
        assert task["conversationId"] == thread["id"]
        identity = {"id": job["id"], "kind": "goal_authoring"}
        resumed = ok(http.post(budget, json={"billingDay": eligible["billingDay"], "tasks": [identity]}))
        assert resumed["resumed"] == [identity]
        engine = open_db(tmp_path)
        try:
            claim = reserve_goal_authoring(engine, now)
            assert claim and claim.id == job["id"]
            settle_goal_authoring(engine, now)
        finally:
            engine.dispose()
        assert ok(http.get(budget))["used"] == 1
        assert ok(http.get(next_path + "/ai"))["job"]["state"] == "queued"
        assert model_once(tmp_path, calls, {"fields": [], "question": "你想完成什么？"})
        assert len(calls) == ok(http.get(budget))["used"] == 2


def test_goal_authoring_restores_reviewable_draft_and_holds_pending_request(tmp_path):
    from test_backups import choose_destination, inspect_backup, make_backup
    from test_full_restore import app_client, request_restore, wait_restore

    producer, receiver, media = (tmp_path / name for name in ("producer", "receiver", "media"))
    calls = []
    with app_client(producer) as http:
        setup_model(http)
        source = ok(http.post("/api/files", json={"name": "带回的练习.txt", "content": "导出两组练习数据。"}))
        _, ready_path, _, request = make_goal(http, attachments=[source["id"]])
        request["values"] = {"foundation": "我熟悉表格函数"}
        preview, authorized = queue(http, ready_path, {**request, "withExcerpts": True})
        file, = [entry for entry in preview["material"]["sources"] if entry["kind"] == "file"]
        ready_id = ok(http.post(ready_path + "/ai", json=authorized))["id"]
        assert model_once(producer, calls, {"fields": [field("outcome", "导出两组练习数据", "suggestion", [file["key"]])]})
        ready_goal = {key: request[key] for key in ("goalId", "revision")}
        _, pending_path, _, request = make_goal(http, text="我想学习带参数的命令。")
        _, authorized = queue(http, pending_path, request)
        ok(http.post(pending_path + "/ai", json=authorized))
        choose_destination(http, media)
        saved = make_backup(http)

    with app_client(receiver) as http:
        choose_destination(http, media)
        plan = ok(inspect_backup(http, saved))
        assert plan["counts"]["backup"]["goal_authoring_jobs"] == 2
        request_restore(http, saved, plan)
        restored = wait_restore(http)
        assert restored["state"] == "complete" and restored["heldTasks"] == 1
        pending = ok(http.get(pending_path + "/ai"))["job"]
        assert pending["state"] == "failed" and pending["reason"] == "restored"
        assert not model_once(receiver, calls, {})
        recovered = ok(http.get(ready_path + "/ai"))["job"]
        approved = recovered["approvedEdits"]
        assert approved["values"] == {"foundation": "我熟悉表格函数"}
        applied = ok(http.post(ready_path + f"/ai/{ready_id}/apply", json=ready_goal | approved))
        assert goal_values(applied)["outcome"] == "导出两组练习数据"
        assert goal_values(applied)["foundation"] == "我熟悉表格函数"
        assert len(calls) == 1


def test_removing_evidence_revokes_both_queued_calls_and_older_previews(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        ok(http.put('/api/generation/settings', json={'dailyLimit': 1, 'concurrency': 1}))
        _, first, _, authorized = make_goal(http)
        _, confirmed = queue(http, first, authorized)
        ok(http.post(first + '/ai', json=confirmed))
        assert model_once(tmp_path, calls, {'fields': []})
        source = ok(http.post('/api/files', json={'name': '应撤回.txt', 'content': '授权撤回后不应发送的资料'}))
        _, path, goal, request = make_goal(http, attachments=[source['id']])
        _, confirmed = queue(http, path, {**request, 'withExcerpts': True})
        _, old_preview = queue(http, path, {**request, 'id': str(uuid4()), 'withExcerpts': True})
        ok(http.post(path + '/ai', json=confirmed))
        assert not model_once(tmp_path, calls, {})
        assert ok(http.get(path + '/ai'))['job']['reason'] == 'daily_budget'
        revised = ok(http.post(path + '/ai/revoke', json={'goalId': goal['id'], 'revision': goal['revision']}))
        assert revised['revision'] == goal['revision'] + 1 and revised['evidence'] == goal['evidence']
        ok(http.put('/api/generation/settings', json={'dailyLimit': 3, 'concurrency': 1}))
        assert http.post(path + '/ai', json=old_preview).status_code == 409
        assert ok(http.get(path + '/ai'))['job']['state'] == 'cancelled'
        assert not model_once(tmp_path, calls, {})
        assert len(calls) == 1


def test_an_older_fact_cannot_replace_the_current_explicit_goal(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        start(http, '我以前想用 Python 做一个网页。')
        _, path, _, request = make_goal(http, text='我想学习 Python，目标是做一个书籍目录。')
        preview, confirmed = queue(http, path, request)
        earlier, = [source for source in preview['material']['sources'] if source['kind'] == 'history']
        ok(http.post(path + '/ai', json=confirmed))
        assert model_once(tmp_path, calls, {'fields': [field('outcome', '做一个网页', 'fact', [earlier['key']])]})
        ready = ok(http.get(path + '/ai'))['job']
        assert goal_values(ready['suggestion'])['outcome'] == '做一个书籍目录'
