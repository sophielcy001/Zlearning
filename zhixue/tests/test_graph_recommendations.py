from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4

from backend.db import GraphRecommendation, open_db, transaction
from backend.graph_recommendation_worker import dispatch_recommendation, reserve_recommendation, settle_recommendations
from backend.model_usage import billed_calls, billing_day
from backend.restored_tasks import hold_restored_tasks
from test_library_graph import GRAPH, document, edit, graph, nodes
from test_model_study import ok, session, setup_model


QUEUE = GRAPH + "/recommendations"
BUDGET = "/api/generation/daily-budget"
RESPONSE = {
    "links": [
        {"source": "N1", "target": "N2", "both": True, "reason": "两项材料可以对照阅读。"},
        {"source": "N2", "target": "N3", "both": False, "reason": "可以补做练习。"},
        {"source": "N2", "target": "N1", "both": True, "reason": "重复连线"},
        {"source": "N3", "target": "N3", "both": False, "reason": "节点指向自己"},
        {"source": "N1", "target": "N98", "both": False, "reason": "无法识别的编号"},
    ],
    "groups": [
        {"name": "数据基础", "members": ["N1", "N2"], "reason": "围绕基础概念。"},
        {"name": "含重复成员的分组", "members": ["N2", "N3"], "reason": "与已建议的分组冲突。"},
        {"name": "含无效成员", "members": ["N1", "N99"], "reason": "无法识别的编号"},
    ],
}


def propose(http, ids, *, excerpts=False):
    data = {"id": str(uuid4()), "nodeIds": ids, "withExcerpts": excerpts}
    preview = ok(http.post(QUEUE + "/preview", json=data))
    return {**data, "consent": preview["consent"]}, preview


def listing(http):
    return ok(http.get(QUEUE))


def mock_reply(result=RESPONSE, on_call=None):
    def send(address, headers, request):
        prompt = request["messages"][0]["content"]
        if on_call is not None:
            on_call(prompt)
        return 200, json.dumps({
            "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 59, "completion_tokens": 39},
        }).encode()
    return send


def work(folder, *, transport=None, now=None):
    engine = open_db(folder)
    try:
        clock = now or datetime.now(UTC)
        claim = reserve_recommendation(engine, clock)
        if claim is not None:
            dispatch_recommendation(engine, claim, transport=transport or mock_reply(), now=lambda: clock)
        return claim
    finally:
        engine.dispose()


def decision(http, items, action, revision=None):
    if revision is None:
        revision = graph(http)["revision"]
    return http.post(QUEUE + "/review", json={
        "ids": [item["id"] for item in items], "action": action, "revision": revision,
    })


def test_recommendations_disclose_only_chosen_material_and_review_separately(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        setup_model(http)
        a = document(http, "数据结构.md", "本次选用的数据结构简介")
        b = document(http, "例题.md", "本次选用的对照例题")
        c = document(http, "练习.md", "本次选用的练习概要")
        hidden_material = "这段原文没有进入所选范围"
        unused = document(http, "未选文档.md", hidden_material)
        baseline = edit(http, graph(http), "move", id=a, position={"x": 99, "y": 202})

        request, preview = propose(http, [a, b, c], excerpts=True)
        assert preview["recipient"]["model"] == "isolated-model"
        assert preview["consent"]["maxCalls"] == 1
        assert [item["excerpt"] for item in preview["nodes"]] == [
            "本次选用的数据结构简介", "本次选用的对照例题", "本次选用的练习概要",
        ]
        assert hidden_material not in json.dumps(preview, ensure_ascii=False)
        assert work(tmp_path) is None and listing(http)["jobs"] == []
        assert http.post(QUEUE, json={**request, "nodeIds": [a, b, unused]}).status_code == 409
        queued = ok(http.post(QUEUE, json=request))
        assert queued["state"] == "queued" and ok(http.post(QUEUE, json=request)) == queued
        assert work(tmp_path, transport=mock_reply(on_call=prompts.append))
        [prompt] = prompts
        assert json.loads(prompt.split("\n图谱发送范围 JSON\n")[1]) == {key: preview[key] for key in ("nodes", "existingLinks")}
        assert all(value not in prompt for value in (a, b, c, unused, hidden_material, "library/"))
        assert graph(http)["edges"] == [] and graph(http)["groups"] == []
        assert ok(http.get(GRAPH + "/archive"))["edges"] == []
        ready = listing(http)
        assert ready["jobs"][0]["usage"] == {"inputTokens": 59, "outputTokens": 39}
        assert ready["jobs"][0]["count"] == 3

    with session(tmp_path) as http:
        candidates = listing(http)["candidates"]
        first = next(item for item in candidates if item["kind"] == "link" and item["source"] == a)
        discarded = next(item for item in candidates if item["kind"] == "link" and item["source"] == b)
        group = next(item for item in candidates if item["kind"] == "group")
        assert ok(decision(http, [discarded], "reject", revision=0))["rejected"] == 1
        assert decision(http, [first, group], "accept", revision=0).status_code == 409
        view = ok(decision(http, [first, group], "accept"))
        saved = view["graph"]
        assert view["accepted"] == 2 and saved["revision"] == baseline["revision"] + 1
        [linked] = saved["edges"]
        assert linked["both"] and linked["manual"] and {linked["source"], linked["target"]} == {a, b}
        assert saved["groups"][0]["name"] == "数据基础"
        assert nodes(saved)[a]["position"] == {"x": 99, "y": 202}
        assert ok(decision(http, [first], "accept"))["skipped"][0]["id"] == first["id"]
        assert listing(http)["candidates"] == []
        assert not edit(http, saved, "undo")["edges"]
        duplicate, _ = propose(http, [a, b, c], excerpts=True)
        ok(http.post(QUEUE, json=duplicate))
        assert work(tmp_path)
        assert not listing(http)["candidates"]
        again, preview = propose(http, [a, b, c])
        assert all("excerpt" not in node for node in preview["nodes"])
        ok(http.post(QUEUE, json=again))
        again_prompts = []
        assert work(tmp_path, transport=mock_reply({"links": [], "groups": []}, on_call=again_prompts.append))
        assert "本次选用的数据结构简介" not in again_prompts[0]


def test_changed_scope_is_neither_sent_nor_adopted_as_ready(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        a = document(http, "输入甲.md", "当前第一份正文")
        b = document(http, "输入乙.md", "当前第二份正文")
        path = Path(tmp_path, nodes(graph(http))[a]["path"])
        first, _ = propose(http, [a, b], excerpts=True)
        path.write_text("确认前修改了正文", encoding="utf-8")
        assert http.post(QUEUE, json=first).status_code == 409

        second, _ = propose(http, [a, b], excerpts=True)
        ok(http.post(QUEUE, json=second))
        engine = open_db(tmp_path)
        try:
            clock = datetime.now(UTC)
            claim = reserve_recommendation(engine, clock)
            path.write_text("派发前再次修改正文", encoding="utf-8")
            calls = []
            dispatch_recommendation(engine, claim, transport=mock_reply(on_call=calls.append), now=lambda: clock)
            assert not calls
            with transaction(engine, write=False) as db:
                row = db.get(GraphRecommendation, second["id"])
                assert row.status == "not_sent" and billed_calls(db, billing_day(clock)) == 0
        finally:
            engine.dispose()

        third, _ = propose(http, [a, b], excerpts=True)
        ok(http.post(QUEUE, json=third))
        assert work(tmp_path, transport=mock_reply(on_call=lambda _: path.write_text("模型在途时的修订", encoding="utf-8")))
        displayed = listing(http)
        assert not displayed["candidates"]
        job = next(item for item in displayed["jobs"] if item["id"] == third["id"])
        assert job["state"] == "failed" and "已变化" in job["error"] and job["usage"]["inputTokens"] == 59

        fourth, _ = propose(http, [a, b], excerpts=True)
        ok(http.post(QUEUE, json=fourth))
        assert work(tmp_path)
        candidates = listing(http)["candidates"]
        assert candidates
        path.write_text("候选出现之后再次修改", encoding="utf-8")
        skipped = ok(decision(http, candidates, "accept"))
        assert skipped["accepted"] == 0 and len(skipped["skipped"]) == len(candidates)
        assert not skipped["graph"]["edges"] and not skipped["graph"]["groups"]


def test_manual_group_is_preserved_after_recommendation(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        a, b, c = [document(http, f"文件{i}.md", f"描述{i}") for i in range(3)]
        request, _ = propose(http, [a, b, c])
        ok(http.post(QUEUE, json=request))
        assert work(tmp_path)
        original = {"id": str(uuid4()), "name": "手动分组", "members": [a, c]}
        data = edit(http, graph(http), "group", group=original)
        group = next(item for item in listing(http)["candidates"] if item["kind"] == "group")
        result = ok(decision(http, [group], "accept", revision=data["revision"]))
        assert "已属于" in result["skipped"][0]["reason"]
        [remaining] = result["graph"]["groups"]
        assert remaining == {**original, "members": sorted(original["members"])}


def test_graph_budget_threads_cancellation_and_restored_requests_use_shared_rules(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        a = document(http, "甲.md", "甲")
        b = document(http, "乙.md", "乙")
        clock = datetime.now(UTC)
        empty = mock_reply({"links": [], "groups": []})
        settings = "/api/generation/settings"
        ok(http.put(settings, json={"concurrency": 1, "dailyLimit": 1}))
        first, _ = propose(http, [a, b])
        second, _ = propose(http, [a, b])
        ok(http.post(QUEUE, json=first))
        ok(http.post(QUEUE, json=second))
        assert work(tmp_path, transport=empty, now=clock)
        assert work(tmp_path, now=clock) is None
        assert any(item["id"] == second["id"] and item["budgetBlocked"] for item in listing(http)["jobs"])
        assert ok(http.get(BUDGET))["used"] == 1

        ok(http.put(settings, json={"concurrency": 1, "dailyLimit": 10}))
        budget = ok(http.get(BUDGET))
        [pending] = budget["tasks"]
        assert pending["kind"] == "graph" and pending["id"] == second["id"]
        assert ok(http.post(BUDGET, json={
            "billingDay": budget["billingDay"], "tasks": [{"kind": "graph", "id": second["id"]}],
        }))["resumed"] == [{"kind": "graph", "id": second["id"]}]
        assert work(tmp_path, transport=mock_reply(on_call=lambda _: ok(http.post(QUEUE + "/" + second["id"] + "/cancel"))), now=clock)
        assert not listing(http)["candidates"]
        assert ok(http.get(BUDGET))["used"] == 2

        pending, _ = propose(http, [a, b])
        ok(http.post(QUEUE, json=pending))
        engine = open_db(tmp_path)
        try:
            assert reserve_recommendation(engine, clock)
            assert reserve_recommendation(engine, clock) is None
            settle_recommendations(engine, clock + timedelta(seconds=1))
            with transaction(engine, write=False) as db:
                job = db.get(GraphRecommendation, pending["id"])
                assert job.state == "queued" and job.started_at is None
            assert hold_restored_tasks(engine, clock)[0] == 1
            assert reserve_recommendation(engine, clock) is None
        finally:
            engine.dispose()
        restored = next(item for item in listing(http)["jobs"] if item["id"] == pending["id"])
        assert restored["state"] == "failed" and "备份" in restored["error"]


def test_graph_recommendations_resume_on_a_new_billing_day_when_enabled(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        a = document(http, "甲.md", "本日隔离资料甲")
        b = document(http, "乙.md", "本日隔离资料乙")
        settings = "/api/generation/settings"
        ok(http.put(settings, json={"concurrency": 2, "dailyLimit": 1, "autoContinueDailyBudget": True}))
        first, _ = propose(http, [a, b])
        second, _ = propose(http, [a, b])
        ok(http.post(QUEUE, json=first))
        ok(http.post(QUEUE, json=second))
        before = datetime(2027, 3, 3, 15, 59, tzinfo=UTC)
        after = before + timedelta(minutes=2)
        empty = mock_reply({"links": [], "groups": []})
        assert work(tmp_path, transport=empty, now=before)
        assert work(tmp_path, transport=empty, now=before) is None
        assert work(tmp_path, transport=empty, now=after)
        engine = open_db(tmp_path)
        try:
            with transaction(engine, write=False) as db:
                assert billed_calls(db, billing_day(before)) == billed_calls(db, billing_day(after)) == 1
                assert db.get(GraphRecommendation, second["id"]).state == "ready"
        finally:
            engine.dispose()
