import json
from pathlib import Path
from uuid import uuid4

from backend.db import open_db
from test_model_study import consent_request, ok, run, session, setup_model


SOURCE = "拣字后用版框固定活字，试印前需要检查字行是否平整。"
PLAN = {"stages": [{"name": "印刷基础", "lessons": [
    {"key": "type", "title": "活字排版", "outcome": "说明试印前的检查内容"},
]}]}
DRAFT = {"sections": [
    {"title": "固定活字", "text": "将排好的活字固定在版框中。", "checks": ["固定方式"]},
    {"title": "温度", "text": "试印必须把纸加热至固定高温。", "checks": ["温度依据"]},
    {"title": "温控步骤", "text": "基于前述固定温度设定恒温。", "checks": ["温度前提"]},
    {"title": "版面规格", "text": "每一行都必须排 exactly 三十个字。", "checks": ["行数依据"]},
]}
FINDINGS = {"sections": [
    {"index": 0, "verdict": "supported", "reason": "原文直接描述了在版框中固定活字。", "citations": [{"source": "S1", "quote": SOURCE}], "dependsOn": []},
    {"index": 1, "verdict": "disputed", "reason": "原文只涉及版面检查。", "citations": [], "dependsOn": []},
    {"index": 2, "verdict": "supported", "reason": "流程依赖尚待查证的温度。", "citations": [{"source": "S1", "quote": SOURCE}], "dependsOn": [1]},
    {"index": 3, "verdict": "supported", "reason": "宣称有出处。", "citations": [{"source": "S1", "quote": "每行限定为三十字。"}], "dependsOn": []},
]}


def model_traffic(calls, on_review=None, *, route=PLAN):
    def transport(address, headers, payload):
        prompt = payload["messages"][0]["content"]
        calls.append((payload["model"], prompt))
        if payload["model"] == "independent-model":
            if on_review is not None:
                on_review()
            reply = FINDINGS
        else:
            reply = route if "制定学习路线" in prompt else DRAFT
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
    return transport


def prepare(http, folder, calls, *, reviewer="independent-model", limit=3, route=PLAN):
    setup_model(http)
    independent = ok(http.post("/api/models/connections", json={
        "name": "独立复核", "provider": "custom", "protocol": "chat-completions",
        "endpoint": "http://127.0.0.1:9109/v1", "model": reviewer,
    }))
    ok(http.patch("/api/models/roles", json={"review": independent["id"]}))
    submit, goal, grant = consent_request(http, max_calls=limit)
    project = ok(http.post(submit, json={**goal, "modelConsent": grant["consent"]}))["projectId"]
    assert run(folder, model_traffic(calls, route=route))
    assert run(folder, model_traffic(calls))
    lesson = ok(http.get(f"/api/projects/{project}"))["lessons"][0]["id"]
    file = ok(http.post("/api/files", json={"name": "印刷资料.txt", "content": f"本机资料备注\n{SOURCE}\n私有附注"}))["id"]
    item = ok(http.get(f"/api/files/{file}?at=2"))
    payload = {"sources": [{"fileId": file, "revision": item["revision"], "at": 2}]}
    return project, lesson, item, payload


def authorize(http, project, payload):
    endpoint = f"/api/projects/{project}/course-review"
    preview = ok(http.post(endpoint + "/preview", json=payload))
    return ok(http.post(endpoint + "/authorize", json={**payload, "confirmation": preview["confirmation"]}))


def publish(folder):
    from backend.course_publication import publish_one

    engine = open_db(folder)
    try:
        return publish_one(engine, folder)
    finally:
        engine.dispose()


def test_verified_fragments_publish_selectively_with_versioned_local_citation(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, item, payload = prepare(http, tmp_path, calls)
        endpoint = f"/api/projects/{project}/course-review"
        preview = ok(http.post(endpoint + "/preview", json=payload))
        assert len(calls) == 2 and preview["reviewer"]["model"] == "independent-model"
        assert preview["sources"][0]["excerpt"] == SOURCE
        assert not run(tmp_path, model_traffic(calls))
        ok(http.post(endpoint + "/authorize", json={**payload, "confirmation": preview["confirmation"]}))
        assert run(tmp_path, model_traffic(calls))
        assert publish(tmp_path)
        assert [role for role, _ in calls] == ["isolated-model", "isolated-model", "independent-model"]
        assert SOURCE in calls[2][1] and "私有附注" not in calls[2][1]
        view = ok(http.get(f"/api/lessons/{lesson}"))
        assert "固定活字" in view["body"] and "三十个字" not in view["body"]
        assert "温控步骤" not in view["body"] and "加热" not in view["body"]
        assert view["courseVersion"] == 1
        findings = ok(http.get(f"/api/lessons/{lesson}/verification"))
        assert [section["state"] for section in findings["sections"]] == [
            "verified", "disputed", "dependency_blocked", "insufficient",
        ]
        source = findings["sections"][0]["citations"][0]
        assert (source["fileId"], source["at"], source["revision"], source["quote"]) == (
            item["id"], 2, item["revision"], SOURCE,
        )
        disputes = ok(http.get("/api/disputes"))["items"]
        assert [item["index"] for item in disputes] == [1, 2, 3]
        assert view["objectives"] == []


def test_review_authorization_cannot_reuse_stale_file_preview(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, _, item, payload = prepare(http, tmp_path, calls)
        endpoint = f"/api/projects/{project}/course-review"
        preview = ok(http.post(endpoint + "/preview", json=payload))
        (Path(tmp_path) / item["path"]).write_text("资料已更改。", encoding="utf-8")
        assert http.post(endpoint + "/authorize", json={**payload, "confirmation": preview["confirmation"]}).status_code == 409
        assert not run(tmp_path, model_traffic(calls))
        assert len(calls) == 2


def test_source_changes_during_review_prevent_publication(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, item, payload = prepare(http, tmp_path, calls)
        assert authorize(http, project, payload)["authorized"]

        def replace_source():
            (tmp_path / item["path"]).write_text("最新印刷资料没有这段说明。", encoding="utf-8")

        assert run(tmp_path, model_traffic(calls, replace_source))
        assert publish(tmp_path)
        assert ok(http.get(f"/api/lessons/{lesson}"))["body"] is None
        task = ok(http.get(f"/api/projects/{project}/generation"))
        assert task["reason"] == "review_sources_changed" and task["usage"]["calls"] == 3
        assert not run(tmp_path, model_traffic(calls))


def test_stop_saves_review_findings_and_waits_to_publish(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, _, payload = prepare(http, tmp_path, calls)
        authorize(http, project, payload)
        endpoint = f"/api/projects/{project}/generation/control"
        assert run(tmp_path, model_traffic(calls, lambda: ok(http.post(endpoint, json={"action": "stop"}))))
        assert not publish(tmp_path)
        view = ok(http.get(f"/api/lessons/{lesson}/verification"))
        assert view["sections"][0]["state"] == "verified" and not view["published"]
        ok(http.post(endpoint, json={"action": "resume"}))
        assert publish(tmp_path)
        assert ok(http.get(f"/api/lessons/{lesson}"))["courseVersion"] == 1
        assert not run(tmp_path, model_traffic(calls))


def test_adopting_a_single_fragment_preserves_old_course_and_reports_stale_batch_items(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, _, payload = prepare(http, tmp_path, calls)
        authorize(http, project, payload)
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        audit = ok(http.get(f"/api/lessons/{lesson}/verification"))
        valid = {"lessonId": lesson, "index": 1, "findingId": audit["findingId"]}
        stale = {**valid, "index": 2, "findingId": str(uuid4())}
        data = {"action": "adopt", "items": [valid, stale]}
        preview = ok(http.post("/api/disputes/preview", json=data))
        assert [item["status"] for item in preview["items"]] == ["ready", "unavailable"]
        result = ok(http.post("/api/disputes/apply", json={**data, "confirmation": preview["confirmation"]}))
        assert [item["status"] for item in result["items"]] == ["applied", "unavailable"]
        course = ok(http.get(f"/api/lessons/{lesson}"))
        assert course["courseVersion"] == 2 and course["objectives"] == []
        assert "用户采纳，尚未核验通过" in course["body"] and "加热" in course["body"]
        assert "温控步骤" not in course["body"]
        saved = ok(http.get(f"/api/lessons/{lesson}/versions/1"))
        assert "固定活字" in saved["markdown"] and "加热" not in saved["markdown"]
        current = ok(http.get(f"/api/lessons/{lesson}/verification"))
        assert current["published"][1]["state"] == "disputed"
        assert current["published"][1]["disposition"] == "adopted"
        assert len(calls) == 3


def test_budget_block_does_not_hide_review_and_publication_still_in_progress(tmp_path):
    from backend.model_study import dispatch, reserve_call, utc_now

    calls = []
    next_point = {"key": "ink", "title": "试印", "outcome": "说明试印的检查步骤"}
    route = {"stages": [{"name": "排字与试印", "lessons": [*PLAN["stages"][0]["lessons"], next_point]}]}
    with session(tmp_path) as http:
        project, lesson, _, payload = prepare(http, tmp_path, calls, route=route)
        authorize(http, project, payload)
        endpoint = f"/api/projects/{project}/generation"
        engine = open_db(tmp_path)
        try:
            claim = reserve_call(engine, utc_now())
            assert claim is not None and claim.kind == "review"
            assert reserve_call(engine, utc_now()) is None
            state = ok(http.get(endpoint))
            assert state["state"] == "queued" and state["reason"] == "task_budget"
            dispatch(engine, claim, transport=model_traffic(calls))
            assert ok(http.get(endpoint))["state"] == "queued"
        finally:
            engine.dispose()
        assert publish(tmp_path)
        assert ok(http.get(endpoint))["state"] == "blocked"
        assert ok(http.get(f"/api/lessons/{lesson}"))["courseVersion"] == 1


def test_review_cannot_send_to_an_alias_of_the_generator(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, _, payload = prepare(http, tmp_path, calls, reviewer="isolated-model")
        preview = http.post(f"/api/projects/{project}/course-review/preview", json=payload)
        assert preview.status_code == 409
        assert not run(tmp_path, model_traffic(calls))
        assert ok(http.get(f"/api/lessons/{lesson}"))["body"] is None and len(calls) == 2


def test_deferred_fragment_can_be_rechecked_without_regenerating_the_draft(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, _, payload = prepare(http, tmp_path, calls, limit=5)
        authorize(http, project, payload)
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        audit_path = f"/api/lessons/{lesson}/verification"
        old = ok(http.get(audit_path))["findingId"]
        subject = {"lessonId": lesson, "index": 1, "findingId": old}
        for action in ("later", "recheck"):
            batch = {"action": action, "items": [subject]}
            preview = ok(http.post("/api/disputes/preview", json=batch))
            applied = ok(http.post("/api/disputes/apply", json={**batch, "confirmation": preview["confirmation"]}))
            assert applied["items"][0]["status"] == "applied"
        assert ok(http.get("/api/disputes?status=later"))["total"] == 1
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        assert ok(http.get(audit_path))["findingId"] != old
        assert ok(http.get("/api/disputes?status=later"))["total"] == 0
        assert ok(http.get("/api/state"))["disputeCount"] == 3
        assert [role for role, _ in calls] == ["isolated-model", "isolated-model", "independent-model", "independent-model"]
        assert ok(http.get(f"/api/lessons/{lesson}"))["courseVersion"] == 1


def test_source_move_preserves_locator_and_external_edit_marks_existing_release_stale(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, item, payload = prepare(http, tmp_path, calls)
        authorize(http, project, payload)
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        original = tmp_path / item["path"]
        target = original.with_name("更名后的来源.txt")
        original.rename(target)
        verify = f"/api/lessons/{lesson}/verification"
        assert not ok(http.get(verify))["sourcesChanged"]
        source_read = f"/api/files/{item['id']}?at=2&revision={item['revision']}"
        assert ok(http.get(source_read))["anchor"]["text"] == SOURCE
        target.write_text("试印资料的下一版。", encoding="utf-8")
        assert ok(http.get(verify))["sourcesChanged"]
        assert http.get(source_read).status_code == 409
        lesson_view = ok(http.get(f"/api/lessons/{lesson}"))
        assert lesson_view["courseVersion"] == 1 and lesson_view["courseMatchesVerification"]
        assert "固定活字" in lesson_view["body"] and "试印资料的下一版" not in lesson_view["body"]
