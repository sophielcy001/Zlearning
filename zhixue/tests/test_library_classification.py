from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from backend import library_classification
from backend.db import LibraryClassification, open_db, transaction
from backend.library_classification_worker import dispatch_classification, reserve_classification, settle_classifications
from backend.model_usage import billed_calls, billing_day
from backend.restored_tasks import hold_restored_tasks
from test_graph_recommendations import mock_reply, propose as propose_graph, work as work_graph
from test_library_folders import create
from test_library_graph import GRAPH, document
from test_library_organization import tag
from test_model_study import ok, session, setup_model


QUEUE = "/api/files/classification"
BUDGET = "/api/generation/daily-budget"


def request(http, files, folders=(), *, excerpts=True, automatic=False):
    payload = {"id": str(uuid4()), "fileIds": files, "folders": list(folders),
               "withExcerpts": excerpts, "autoApply": automatic}
    preview = ok(http.post(QUEUE + "/preview", json=payload))
    return {**payload, "consent": preview["consent"]}, preview


def mock(*items, on_call=None):
    return mock_reply({"suggestions": list(items)}, on_call=on_call)


def recommendation(file="F1", *, folder="D1", tags=("课程资料",)):
    return {"file": file, "folder": folder, "tags": list(tags), "reason": "材料适合按主题查找。"}


def work(workspace, *, transport, clock=None):
    instant = clock or datetime.now(UTC)
    engine = open_db(workspace)
    try:
        claim = reserve_classification(engine, instant)
        if claim:
            dispatch_classification(engine, claim, transport=transport, now=lambda: instant)
        return claim
    finally:
        engine.dispose()


def view(http):
    return ok(http.get(QUEUE))


def get_file(http, identity):
    return ok(http.get(f"/api/files/{identity}"))


def review(http, ids, choice="apply"):
    return ok(http.post(QUEUE + "/review", json={"ids": ids, "choice": choice}))


def test_classification_discloses_only_previewed_material_and_applies_reviewed_changes(tmp_path):
    traffic = []
    body = "用于整理的基础内容。" + "数" * 1010 + "\n不在正文摘录范围内"
    with session(tmp_path) as http:
        setup_model(http)
        course = create(http, "课程归档")
        create(http, "未授权的文件夹")
        first = document(http, "公开数据.md", body)
        second = document(http, "复习资料.txt", "用于本次排序的例题内容。")
        private = document(http, "个人未选资料.md", "绝不发送的样例文本")
        original = get_file(http, first)
        note = document(http, "回读.md", f"[[{original['path']}]]")
        tag(http, [first], "既有标签")

        data, preview = request(http, [first, second], [course])
        assert preview["recipient"]["model"] == "isolated-model" and preview["consent"]["maxCalls"] == 1
        assert sum(len(excerpt["text"]) for excerpt in preview["files"][0]["excerpts"]) == 1000
        assert preview["files"][0]["tags"] == ["既有标签"]
        assert work(tmp_path, transport=mock()) is None
        assert http.post(QUEUE, json={**data, "autoApply": True}).status_code == 409
        assert http.post(QUEUE, json={**data, "fileIds": [first, private]}).status_code == 409
        assert http.post(QUEUE, json={**data, "folders": ["library/未授权的文件夹"]}).status_code == 409
        queued = ok(http.post(QUEUE, json=data))
        assert ok(http.post(QUEUE, json=data)) == queued
        assert work(tmp_path, transport=mock(
            recommendation(tags=("既有标签", "算法", "算法")),
            recommendation("F2", folder="D9", tags=("复习",)), recommendation("F99"), on_call=traffic.append,
        ))
        sent, = traffic
        assert json.loads(sent.split("\n知识库整理发送范围 JSON\n")[1]) == {
            "files": preview["files"], "folders": preview["folders"],
        }
        assert all(value not in sent for value in (
            first, second, private, note, str(tmp_path), "未授权的文件夹", "个人未选资料", "绝不发送", "不在正文摘录范围内",
        ))
        assert get_file(http, first)["path"] == original["path"]
        assert not get_file(http, second)["tags"]
        assert view(http)["jobs"][0]["usage"] == {"inputTokens": 59, "outputTokens": 39}

    with session(tmp_path) as http:
        suggestions = {candidate["fileId"]: candidate for candidate in view(http)["pending"]}
        first_suggestion = suggestions[first]
        assert first_suggestion["tags"] == ["算法"] and first_suggestion["destination"] == course
        assert suggestions[second]["destination"] is None
        outcome, = review(http, [first_suggestion["id"]])["reviewed"]
        assert outcome["state"] == "applied" and outcome["result"]["tags"] == ["算法"]
        assert outcome["result"]["path"].startswith(course + "/")
        assert (tmp_path / outcome["result"]["path"]).read_bytes() == body.encode()
        assert ok(http.get("/api/files/wiki/resolve", params={"target": original["path"]}))["choices"][0]["id"] == first
        assert ok(http.get(f"/api/files/{first}/backlinks"))[0]["id"] == note
        assert review(http, [suggestions[second]["id"]], "reject")["reviewed"][0]["state"] == "rejected"
        assert not view(http)["pending"]


def test_auto_classification_checks_the_original_and_keeps_a_conflicting_target(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        target = create(http, "自动归档")
        first = document(http, "甲.md", "有目标文件同名冲突。")
        second = document(http, "乙.md", "可以移入允许的目录。")
        source = get_file(http, first)["path"]
        protected = tmp_path / target / source.rsplit("/", 1)[-1]
        protected.write_bytes(b"existing user content")
        data, preview = request(http, [first, second], [target], automatic=True, excerpts=False)
        assert preview["autoApply"] and all(not file["excerpts"] for file in preview["files"])
        ok(http.post(QUEUE, json=data))
        assert work(tmp_path, transport=mock(recommendation(), recommendation("F2")))
        assert not view(http)["pending"]
        outcomes = {item["fileId"]: item for item in view(http)["history"]}
        assert outcomes[first]["state"] == "partial" and "同名" in outcomes[first]["result"]["issue"]
        assert outcomes[second]["state"] == "applied" and outcomes[second]["result"]["path"].startswith(target)
        assert protected.read_bytes() == b"existing user content"
        assert get_file(http, first)["path"] == source
        assert get_file(http, first)["tags"][0]["name"] == "课程资料"


def test_classification_skips_changes_before_send_during_response_and_before_apply(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        original = document(http, "可编辑资料.md", "旧版内容")
        path = tmp_path / get_file(http, original)["path"]
        data, _ = request(http, [original])
        ok(http.post(QUEUE, json=data))
        path.write_text("派发前改过的正文", encoding="utf-8")
        requests = []
        assert work(tmp_path, transport=mock(recommendation(), on_call=requests.append))
        assert not requests and view(http)["jobs"][0]["state"] == "failed"
        assert ok(http.get(BUDGET))["used"] == 0
        data, _ = request(http, [original])
        ok(http.post(QUEUE, json=data))
        assert work(tmp_path, transport=mock(recommendation(), on_call=lambda _: path.write_text(
            "模型回应前的改动", encoding="utf-8",
        )))
        assert not view(http)["pending"] and view(http)["jobs"][0]["state"] == "failed"
        assert ok(http.get(BUDGET))["used"] == 1
        data, _ = request(http, [original])
        ok(http.post(QUEUE, json=data))
        assert work(tmp_path, transport=mock(recommendation()))
        awaiting, = view(http)["pending"]
        tag(http, [original], "用户刚加的标签")
        result, = review(http, [awaiting["id"]], "tags")["reviewed"]
        assert result["state"] == "stale"
        assert [item["name"] for item in get_file(http, original)["tags"]] == ["用户刚加的标签"]


def test_approved_classification_recovers_when_the_file_moves_before_sqlite_commit(tmp_path, monkeypatch):
    with session(tmp_path) as http:
        setup_model(http)
        target = create(http, "重新登记")
        identity = document(http, "待归档.md", "重启后仍按原权限写入。")
        source = get_file(http, identity)["path"]
        data, _ = request(http, [identity], [target])
        ok(http.post(QUEUE, json=data))
        assert work(tmp_path, transport=mock(recommendation()))
        candidate, = view(http)["pending"]
        native = library_classification.apply_move

        def interruption(db, plan):
            native(db, plan)
            raise OSError("interrupted after the filesystem move")

        monkeypatch.setattr(library_classification, "apply_move", interruption)
        receipt, = review(http, [candidate["id"]])["reviewed"]
        assert receipt["state"] == "approved"
        assert not (tmp_path / source).exists()
        backup = http.post("/api/backups", json={"password": "只用于本机测试的备份密码"})
        assert backup.status_code == 409 and backup.json()["code"] == "pending_write"

    with session(tmp_path) as http:
        receipt, = view(http)["history"]
        assert receipt["state"] == "applied" and receipt["id"] == candidate["id"]
        item = get_file(http, identity)
        assert item["path"].startswith(target) and item["tags"][0]["name"] == "课程资料"
        assert review(http, [candidate["id"]], "resume")["skipped"]


def test_classification_shares_the_graph_budget_and_restored_jobs_need_fresh_approval(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        a = document(http, "图与资料一.md", "隔离测试甲")
        b = document(http, "图与资料二.md", "隔离测试乙")
        clock = datetime.now(UTC)
        settings = "/api/generation/settings"
        ok(http.put(settings, json={"concurrency": 1, "dailyLimit": 1}))
        graph_data, _ = propose_graph(http, [a, b])
        ok(http.post(GRAPH + "/recommendations", json=graph_data))
        data, _ = request(http, [a])
        ok(http.post(QUEUE, json=data))
        assert work_graph(tmp_path, transport=mock_reply({"links": [], "groups": []}), now=clock)
        assert work(tmp_path, transport=mock(), clock=clock) is None and view(http)["jobs"][0]["budgetBlocked"]
        ok(http.put(settings, json={"concurrency": 1, "dailyLimit": 10}))
        budget = ok(http.get(BUDGET))
        assert [(item["kind"], item["id"]) for item in budget["tasks"]] == [("classification", data["id"])]
        assert ok(http.post(BUDGET, json={
            "billingDay": budget["billingDay"], "tasks": [{"kind": "classification", "id": data["id"]}],
        }))["resumed"] == [{"kind": "classification", "id": data["id"]}]
        cancel = lambda _: ok(http.post(f"{QUEUE}/{data['id']}/cancel"))
        assert work(tmp_path, transport=mock(recommendation(), on_call=cancel), clock=clock)
        assert view(http)["jobs"][0]["state"] == "cancelled" and not view(http)["pending"]
        deferred, _ = request(http, [a], automatic=True)
        ok(http.post(QUEUE, json=deferred))
        engine = open_db(tmp_path)
        try:
            assert reserve_classification(engine, clock)
            assert not reserve_classification(engine, clock)
            settle_classifications(engine, clock + timedelta(seconds=1))
            with transaction(engine, write=False) as db:
                assert db.get(LibraryClassification, deferred["id"]).state == "queued"
                assert billed_calls(db, billing_day(clock)) == 2
            assert hold_restored_tasks(engine, clock)[0] == 1
        finally:
            engine.dispose()
        restored = next(job for job in view(http)["jobs"] if job["id"] == deferred["id"])
        assert restored["state"] == "failed" and "备份" in restored["error"]
        assert work(tmp_path, transport=mock(), clock=clock) is None
