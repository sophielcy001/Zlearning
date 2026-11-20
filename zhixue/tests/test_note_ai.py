import json
from uuid import uuid4

from backend.db import NoteSuggestion, open_db, transaction
from backend.model_study import utc_now
from backend.model_usage import active_calls, billed_calls, billing_day
from backend.note_ai_worker import admit, dispatch_note_ai, reserve_note_ai, settle_interrupted_note_ai
from backend.restored_tasks import hold_restored_tasks
from backend.worker import prepare_one
from test_course_verification import DRAFT
from test_model_assessments import source_lesson
from test_model_study import ok, session, setup_model


def new_note(http, lesson=None, text=""):
    identity = str(uuid4())
    ok(http.post("/api/notes", json={"id": identity, "title": "个人笔记", "lessonIds": [lesson] if lesson else []}))
    endpoint = f"/api/notes/{identity}"
    if text:
        current = ok(http.get(endpoint))
        ok(http.put(endpoint, json={"baseRevision": current["revision"], "content": text}))
    return identity, endpoint


def preview(http, endpoint, operation, text="", **scope):
    request = {"id": str(uuid4()), "operation": operation, "text": text, **scope}
    details = ok(http.post(endpoint + "/assistant/preview", json=request))
    return {**request, "confirmation": details["confirmation"]}, details


def record_transport(calls, result="## 建议正文", during=None):
    def transport(address, headers, payload):
        prompt = payload["messages"][0]["content"]
        calls.append((address, json.loads(prompt.split("\n发送范围 JSON\n", 1)[1])))
        if during:
            during()
        response = {
            "choices": [{"message": {"content": json.dumps({"markdown": result}, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 31, "completion_tokens": 7},
        }
        return 200, json.dumps(response, ensure_ascii=False).encode()
    return transport


def test_selection_preview_is_exact_and_job_never_writes_note(tmp_path):
    calls = []
    selected = "**复习片段** 与 $a+b$"
    content = f"未选文字\n\n{selected}\n\n个人备忘"
    with session(tmp_path) as http:
        note_id, endpoint = new_note(http, text=content)
        denied = http.post(endpoint + "/assistant/preview", json={"id": str(uuid4()), "operation": "explain", "text": selected})
        assert denied.status_code == 409
        model = setup_model(http)
        request, details = preview(http, endpoint, "organize", selected)
        assert details["recipient"]["id"] == model["id"] and details["maxCalls"] == 1
        assert details["text"] == selected and details["course"] is None
        assert ok(http.get(endpoint + "/assistant")) is None
        assert http.post(endpoint + "/assistant", json={**request, "text": content}).status_code == 409
        unconfirmed = http.post(endpoint + "/assistant", json={key: value for key, value in request.items() if key != "confirmation"})
        assert unconfirmed.status_code == 422

        accepted = ok(http.post(endpoint + "/assistant", json=request))
        assert accepted["state"] == "queued"
        assert ok(http.post(endpoint + "/assistant", json=request)) == accepted
        other, _ = new_note(http)
        assert http.get(f"/api/notes/{other}/assistant/{request['id']}").status_code == 404

        engine = open_db(tmp_path)
        try:
            claim = reserve_note_ai(engine, utc_now())
            dispatch_note_ai(engine, claim, transport=record_transport(calls))
            assert reserve_note_ai(engine, utc_now()) is None
        finally:
            engine.dispose()
        assert calls == [("http://127.0.0.1:9109/v1/chat/completions", {"selection": selected})]
        result = ok(http.get(endpoint + "/assistant"))
        assert result["state"] == "ready" and result["markdown"] == "## 建议正文"
        assert result["usage"] == {"inputTokens": 31, "outputTokens": 7}
        assert ok(http.post(endpoint + "/assistant", json=request)) == result
        assert ok(http.get(endpoint))["content"] == content
        assert (tmp_path / "library" / "notes" / f"{note_id}.md").read_text(encoding="utf-8") == content

    with session(tmp_path) as http:
        assert ok(http.get(endpoint + "/assistant")) == result
        ok(http.delete(endpoint + "/assistant/" + request["id"]))
        assert ok(http.get(endpoint + "/assistant")) is None
        assert ok(http.get(endpoint + "/assistant/" + request["id"]))["usage"] == result["usage"]


def test_course_scope_rechecks_association_and_original_at_send(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        project = ok(http.post("/api/projects", json={
            "id": str(uuid4()), "topic": "SQLite 实践", "outcome": "能说出索引的用途",
        }))
        while prepare_one(tmp_path):
            pass
        lesson = project["lessons"][0]["id"]
        _, outside = new_note(http)
        disallowed = http.post(outside + "/assistant/preview", json={
            "id": str(uuid4()), "operation": "draft", "lessonId": lesson,
        })
        assert disallowed.status_code == 409
        _, endpoint = new_note(http, lesson)
        request, details = preview(http, endpoint, "draft", lessonId=lesson)
        assert details["course"]["version"] is None and details["course"]["sections"][0]["text"]
        file = tmp_path / "library" / "courses" / f"{lesson}.md"
        file.write_text("# 讲稿在确认之前变化。\n", encoding="utf-8")
        assert http.post(endpoint + "/assistant", json=request).status_code == 409

        request, details = preview(http, endpoint, "draft", "整理例子", lessonId=lesson)
        ok(http.post(endpoint + "/assistant", json=request))
        engine = open_db(tmp_path)
        try:
            claim = reserve_note_ai(engine, utc_now())
            file.write_text("# 讲稿在发送之前再次变化。\n", encoding="utf-8")
            dispatch_note_ai(engine, claim, transport=record_transport(calls))
            assert calls == []
            assert ok(http.get(endpoint + "/assistant"))["state"] == "failed"
            with transaction(engine, write=False) as db:
                assert active_calls(db) == billed_calls(db, billing_day(utc_now())) == 0
        finally:
            engine.dispose()


def test_course_draft_only_uses_current_verified_sections(tmp_path):
    calls = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path, adopt=True)
        _, endpoint = new_note(http, lesson, text="未选定的个人注释")
        request, details = preview(http, endpoint, "draft", lessonId=lesson)
        basis = details["course"]
        assert basis["version"] == 2
        verified = DRAFT["sections"][0]
        assert basis["sections"] == [{"title": verified["title"], "text": verified["text"]}]
        ok(http.post(endpoint + "/assistant", json=request))
        engine = open_db(tmp_path)
        try:
            dispatch_note_ai(engine, reserve_note_ai(engine, utc_now()), transport=record_transport(calls))
        finally:
            engine.dispose()
        assert calls[0][1] == {"instruction": "", "course": {"title": basis["title"], "sections": basis["sections"]}}


def test_note_requests_obey_shared_capacity_and_recover_interrupted_sends(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        _, endpoint = new_note(http)
        ok(http.put("/api/generation/settings", json={"dailyLimit": 1, "concurrency": 1}))
        first, _ = preview(http, endpoint, "draft", "第一个请求")
        second, _ = preview(http, endpoint, "draft", "第二个请求")
        for request in (first, second):
            ok(http.post(endpoint + "/assistant", json=request))
        engine = open_db(tmp_path)
        clock = utc_now()
        try:
            claim = reserve_note_ai(engine, clock)
            assert claim.id == first["id"]
            assert reserve_note_ai(engine, clock) is None
            settle_interrupted_note_ai(engine, clock)
            recovered = reserve_note_ai(engine, clock)
            assert recovered.id == first["id"] and admit(engine, recovered, lambda: clock)
            settle_interrupted_note_ai(engine, clock)
            assert reserve_note_ai(engine, clock) is None
            with transaction(engine, write=False) as db:
                assert active_calls(db) == 0 and billed_calls(db, billing_day(clock)) == 1
                job = db.get(NoteSuggestion, recovered.id)
                assert job.status == "uncertain" and job.state == "failed"
            assert "今日 AI 调用次数" in ok(http.get(endpoint + "/assistant/" + second["id"]))["error"]
        finally:
            engine.dispose()


def test_cancelled_send_is_billed_but_late_result_cannot_reappear(tmp_path):
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        _, endpoint = new_note(http)
        request, _ = preview(http, endpoint, "draft", "测试取消")
        route = endpoint + "/assistant/" + request["id"]
        ok(http.post(endpoint + "/assistant", json=request))
        engine = open_db(tmp_path)
        try:
            claim = reserve_note_ai(engine, utc_now())
            dispatch_note_ai(engine, claim, transport=record_transport(calls, during=lambda: ok(http.delete(route))))
            assert len(calls) == 1 and ok(http.get(endpoint + "/assistant")) is None
            result = ok(http.get(route))
            assert result["state"] == "dismissed" and result["markdown"] is None
            assert result["usage"]["outputTokens"] == 7

            queued, _ = preview(http, endpoint, "draft", "备份期间排队")
            ok(http.post(endpoint + "/assistant", json=queued))
            assert hold_restored_tasks(engine, utc_now()) == (1, 0)
            assert reserve_note_ai(engine, utc_now()) is None
            assert "随备份恢复" in ok(http.get(endpoint + "/assistant"))["error"]
            assert ok(http.get(endpoint))["content"] == ""
        finally:
            engine.dispose()
