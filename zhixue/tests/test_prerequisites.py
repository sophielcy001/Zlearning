import json

import pytest

from backend.db import ModelCharge, PrerequisiteCall, PrerequisiteTask, open_db, transaction
from backend.model_study import reserve_call, utc_now, when_utc
from backend.prerequisite_worker import (
    MAX_OUTPUT_TOKENS, dispatch, process_one, publish_one, reserve, settle_interrupted,
)
from test_backups import create_project
from test_course_verification import model_traffic as course_traffic
from test_model_study import consent_request, ok, run, session, setup_model


SOURCE = "准备语句中的占位符由驱动绑定参数值，输入值不作为 SQL 指令的一部分执行。"
DRAFT = {"sections": [{"title": "把值交给占位符", "text": "绑定参数让输入值与 SQL 指令保持分开。", "checks": ["参数值的处理方式"]}]}
VERIFIED = {"sections": [{
    "index": 0, "verdict": "supported", "reason": "原件明确区分值和指令。",
    "citations": [{"source": "S1", "quote": SOURCE}], "dependsOn": [],
}]}


def models(http):
    setup_model(http)
    other = ok(http.post("/api/models/connections", json={
        "name": "前置知识独立复核", "provider": "custom", "protocol": "chat-completions",
        "endpoint": "http://127.0.0.1:9109/v1", "model": "prerequisite-review",
    }))
    ok(http.patch("/api/models/roles", json={"review": other["id"]}))
    return other


def input_source(http):
    identity = ok(http.post("/api/files", json={
        "name": "绑定参数资料.txt", "content": f"内部草稿说明\n{SOURCE}\n从未授权的私有附注",
    }))["id"]
    source = ok(http.get(f"/api/files/{identity}?at=2"))
    return {"fileId": identity, "at": 2, "revision": source["revision"]}


def grant(http, path, request):
    preview = ok(http.post(path + "/preview", json=request))
    return ok(http.post(path + "/generate", json={**request, "confirmation": preview["confirmation"]}))


def model_traffic(reply, prompts):
    def transport(address, headers, request):
        prompts.append((request["model"], request["messages"][0]["content"]))
        wire = {"choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}]}
        return 200, json.dumps(wire).encode()
    return transport


def one(folder, reply, prompts):
    engine = open_db(folder)
    try:
        return process_one(engine, transport=model_traffic(reply, prompts))
    finally:
        engine.dispose()


def publish(folder):
    engine = open_db(folder)
    try:
        return publish_one(engine, folder)
    finally:
        engine.dispose()


def test_existing_course_opens_without_a_model_or_learning_side_effects(tmp_path):
    with session(tmp_path) as http:
        project = create_project(http, tmp_path)
        first, second = project["lessons"][:2]
        path = f"/api/lessons/{second['id']}/prerequisites"
        existing = ok(http.get(path, params={"topic": first["title"]}))
        assert existing["kind"] == "course"
        assert existing["resource"]["lessonId"] == first["id"]
        assert existing["resource"]["body"]
        again = ok(http.get("/api/projects/" + project["id"]))
        assert [item["learnedOn"] for item in again["lessons"]] == [None] * len(project["lessons"])
        assert ok(http.get("/api/reviews"))["reviews"] == []


def test_new_prerequisite_is_verified_then_reopened_from_local_library_without_a_route_node(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        project = create_project(http, tmp_path)
        origin = project["lessons"][0]["id"]
        path = f"/api/lessons/{origin}/prerequisites"
        topic = "SQL 参数绑定"
        assert ok(http.get(path, params={"topic": topic}))["kind"] == "missing"
        models(http)
        request = {"topic": topic, "sources": [input_source(http)], "maxCalls": 5}
        preview = ok(http.post(path + "/preview", json=request))
        assert preview["main"]["model"] == "isolated-model" and preview["review"]["model"] == "prerequisite-review"
        assert preview["sources"][0]["excerpt"] == SOURCE
        assert http.post(path + "/generate", json={**request, "confirmation": "f" * 64}).status_code == 409
        assert not one(tmp_path, DRAFT, prompts)
        started = ok(http.post(path + "/generate", json={**request, "confirmation": preview["confirmation"]}))
        job = started["job"]["id"]
        repeated = ok(http.post(path + "/generate", json={**request, "confirmation": preview["confirmation"]}))
        assert repeated["job"]["id"] == job
        assert one(tmp_path, DRAFT, prompts)
        invalid = {"sections": [{**VERIFIED["sections"][0], "citations": [{"source": "S1", "quote": "不存在的引文"}]}]}
        assert one(tmp_path, invalid, prompts) and not publish(tmp_path)
        state = ok(http.get(path, params={"topic": topic}))["job"]
        assert state["state"] == "blocked" and state["reason"] == "not_verified" and state["phase"] == "review"
        assert state["draft"] == DRAFT["sections"]
        assert not ok(http.get("/api/files/search", params={"q": "输入值与 SQL 指令", "kind": "supplement"}))["hits"]
    with session(tmp_path) as http:
        ok(http.post(path + f"/jobs/{job}/retry"))
        assert one(tmp_path, VERIFIED, prompts) and publish(tmp_path)
        saved = ok(http.get(path, params={"topic": "sql　参数绑定"}))
        assert saved["kind"] == "explanation" and saved["resource"]["sections"][0]["citations"][0]["quote"] == SOURCE
        file = f"prerequisite-{job}"
        assert saved["resource"]["fileId"] == file
        entry = ok(http.get(f"/api/files/{file}"))
        assert entry["kind"] == "supplement" and entry["path"].startswith("library/supplements/")
        hits = ok(http.get("/api/files/search", params={"q": "输入值与 SQL 指令", "kind": "supplement"}))["hits"]
        assert any(hit["id"] == file for hit in hits)
        latest = ok(http.get("/api/projects/" + project["id"]))
        assert [lesson["id"] for lesson in latest["lessons"]] == [lesson["id"] for lesson in project["lessons"]]
        assert ok(http.get("/api/reviews"))["reviews"] == []
        assert [model for model, _ in prompts] == ["isolated-model", "prerequisite-review", "prerequisite-review"]
        assert all(SOURCE in prompt and "私有附注" not in prompt for _, prompt in prompts)


def test_declared_unready_prerequisite_shares_global_capacity_and_stop_fences_reserved_call(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        models(http)
        submit, fields, consent = consent_request(http, max_calls=6)
        project = ok(http.post(submit, json={**fields, "modelConsent": consent["consent"]}))["projectId"]
        route = {"stages": [{"name": "参数与查询", "lessons": [
            {"key": "binding", "title": "参数绑定", "outcome": "解释参数值的用途"},
            {"key": "query", "title": "准备查询", "outcome": "对照语句内容", "prerequisites": ["binding"]},
        ]}]}
        assert run(tmp_path, course_traffic([], route=route))
        target, origin = ok(http.get(f"/api/projects/{project}"))["lessons"]
        path = f"/api/lessons/{origin['id']}/prerequisites"
        assert http.get(path, params={"targetLessonId": origin["id"]}).status_code == 404
        lookup = ok(http.get(path, params={"targetLessonId": target["id"]}))
        assert lookup["kind"] == "missing" and lookup["topic"] == "参数绑定"
        request = {"targetLessonId": target["id"], "maxCalls": 2, "sources": [input_source(http)]}
        task = grant(http, path, request)["job"]
        ok(http.put("/api/generation/settings", json={"concurrency": 1, "dailyLimit": None}))
        engine = open_db(tmp_path)
        try:
            claim = reserve(engine, utc_now())
            assert claim and claim.task_id == task["id"]
            assert reserve_call(engine, utc_now()) is None
            ok(http.post(path + f"/jobs/{task['id']}/stop"))
            dispatch(engine, claim, transport=model_traffic(DRAFT, prompts))
            stopped = ok(http.get(path, params={"targetLessonId": target["id"]}))["job"]
            assert stopped["state"] == "stopped" and stopped["calls"] == 0 and not stopped["inFlight"] and not prompts
            assert reserve_call(engine, utc_now()) is not None
        finally:
            engine.dispose()


def test_scope_change_and_call_limit_resume_review_without_restarting_the_saved_draft(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        project = create_project(http, tmp_path)
        models(http)
        path = f"/api/lessons/{project['lessons'][0]['id']}/prerequisites"
        source = input_source(http)
        request = {"topic": "输入和指令", "sources": [source], "maxCalls": 1}
        preview = ok(http.post(path + "/preview", json=request))
        local = tmp_path / ok(http.get(f"/api/files/{source['fileId']}"))["path"]
        previous = local.read_bytes()
        local.write_bytes(previous + "\n刚改动的原件".encode())
        assert http.post(path + "/generate", json={**request, "confirmation": preview["confirmation"]}).status_code == 409
        assert not one(tmp_path, DRAFT, prompts)
        local.write_bytes(previous)
        task = grant(http, path, request)["job"]
        assert one(tmp_path, DRAFT, prompts) and not one(tmp_path, VERIFIED, prompts)
        stalled = ok(http.get(path, params={"topic": request["topic"]}))["job"]
        assert stalled["state"] == "blocked" and stalled["reason"] == "task_budget"
        assert stalled["phase"] == "review" and stalled["draft"] == DRAFT["sections"]
        continued = grant(http, path, {**request, "maxCalls": 2, "resumeJobId": task["id"]})["job"]
        assert continued["id"] == task["id"] and continued["phase"] == "review"
        ok(http.put("/api/generation/settings", json={"concurrency": 2, "dailyLimit": 1}))
        assert not one(tmp_path, VERIFIED, prompts)
        assert ok(http.get(path, params={"topic": request["topic"]}))["job"]["reason"] == "daily_budget"
        ok(http.put("/api/generation/settings", json={"concurrency": 2, "dailyLimit": 2}))
        ok(http.post(path + f"/jobs/{task['id']}/retry"))
        assert one(tmp_path, VERIFIED, prompts) and publish(tmp_path)
        assert [model for model, _ in prompts] == ["isolated-model", "prerequisite-review"]
        local.write_bytes(previous + "\n已变更原件".encode())
        stale = ok(http.get(path, params={"topic": request["topic"]}))
        assert stale["kind"] == "missing" and stale["previousUnavailable"]

@pytest.mark.parametrize("sending", [False, True], ids=["reserved", "sending"])
@pytest.mark.parametrize("stopped", [False, True], ids=["running", "stopped"])
def test_restart_retains_a_prerequisite_draft_and_recovers_its_pending_review(tmp_path, sending, stopped):
    from backend.model_ledger import settle_open_charges
    from backend.model_usage import admit_reserved_call

    prompts = []
    topic = "参数由驱动绑定"
    with session(tmp_path) as http:
        project = create_project(http, tmp_path)
        models(http)
        path = f"/api/lessons/{project['lessons'][0]['id']}/prerequisites"
        task = grant(http, path, {"topic": topic, "sources": [input_source(http)], "maxCalls": 2 + int(sending)})["job"]
        actions = f"{path}/jobs/{task['id']}"
        assert one(tmp_path, DRAFT, prompts)
        engine = open_db(tmp_path)
        try:
            claim = reserve(engine, utc_now())
            assert claim and claim.phase == "review"
            if sending:
                with transaction(engine) as db:
                    call = db.get(PrerequisiteCall, claim.id)
                    assert admit_reserved_call(db, call, utc_now, claim.prompt, MAX_OUTPUT_TOKENS) is None
            if stopped:
                ok(http.post(actions + "/stop"))
            settle_interrupted(engine, utc_now())
            with transaction(engine) as db:
                settle_open_charges(db, when_utc(utc_now()), direct=False)
            dispatch(engine, claim, transport=model_traffic(VERIFIED, prompts))
            with transaction(engine, write=False) as db:
                call = db.get(PrerequisiteCall, claim.id)
                assert call.finished_at >= call.started_at
                assert call.status == ("uncertain" if sending else "not_sent")
                saved = db.get(PrerequisiteTask, task["id"])
                assert saved.draft == DRAFT and saved.active_call is None
                charge = db.get(ModelCharge, (PrerequisiteCall.__tablename__, claim.id))
                if sending:
                    assert charge.status == "uncertain" and charge.actual_amount is None
                else:
                    assert charge is None
        finally:
            engine.dispose()
    with session(tmp_path) as http:
        restored = ok(http.get(path, params={"topic": topic}))["job"]
        assert restored["calls"] == 1 + int(sending) and not restored["inFlight"]
        assert restored["phase"] == "review"
        expected_state = "stopped" if stopped else "blocked" if sending else "queued"
        assert restored["state"] == expected_state
        assert restored["reason"] == ("uncertain" if expected_state == "blocked" else None)
        assert [model for model, _ in prompts] == ["isolated-model"]
        if sending or stopped:
            assert not one(tmp_path, VERIFIED, prompts)
            ok(http.post(actions + "/retry"))
        assert one(tmp_path, VERIFIED, prompts) and publish(tmp_path)
        assert [model for model, _ in prompts] == ["isolated-model", "prerequisite-review"]
        saved = ok(http.get(path, params={"topic": topic}))
        assert saved["kind"] == "explanation"
        assert saved["resource"]["sections"][0]["citations"][0]["quote"] == SOURCE

