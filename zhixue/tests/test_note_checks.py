import json
from datetime import timedelta

from backend.db import NoteCheckBatch, open_db, transaction
from backend.model_study import utc_now
from backend.model_usage import active_calls, billed_calls, billing_day
from backend.note_check_worker import admit_check, dispatch_note_check, reserve_note_check, settle_note_checks
from backend.note_checks import CHARS_PER_CALL, EDIT_DELAY, pieces, scan_checks
from backend.restored_tasks import hold_restored_tasks
from test_course_verification import DRAFT
from test_model_assessments import source_lesson
from test_model_study import ok, session
from test_note_ai import new_note


def configure_reviewer(http):
    connection = ok(http.post("/api/models/connections", json={
        "name": "独立笔记复核", "provider": "custom", "protocol": "chat-completions",
        "endpoint": "http://127.0.0.1:9115/v1", "model": "checks-fixture",
    }))
    ok(http.patch("/api/models/roles", json={"review": connection["id"]}))
    return connection


def authorize(http, route, lesson=None):
    data = {"lessonId": lesson}
    preview = ok(http.post(route + "/preview", json=data))
    status = ok(http.post(route + "/authorize", json={**data, "confirmation": preview["confirmation"]}))
    return preview, status


def transport(calls, findings):
    def record(address, headers, request):
        prompt = request["messages"][0]["content"]
        calls.append((address, json.loads(prompt.split("\n检查材料 JSON\n", 1)[1])))
        reply = {"choices": [{"message": {"content": json.dumps({"findings": findings}, ensure_ascii=False)}}],
                 "usage": {"prompt_tokens": 52, "completion_tokens": 14}}
        return 200, json.dumps(reply, ensure_ascii=False).encode()
    return record


def drain(engine, clock, provider):
    while claim := reserve_note_check(engine, clock):
        dispatch_note_check(engine, claim, transport=provider, now=lambda: clock)


def test_opt_in_debounce_superseded_revision_and_exact_markers(tmp_path):
    correctable = "索引会删除数据行。"
    body = "# 课后札记\r\n\r\n🙂读后想法\r\n" + correctable + "\r\n重复句。重复句。"
    calls = []
    with session(tmp_path) as http:
        model = configure_reviewer(http)
        note, endpoint = new_note(http, text=body)
        route = endpoint + "/checks"
        assert ok(http.get(route))["state"] == "off"
        engine = open_db(tmp_path)
        try:
            scan_checks(engine, tmp_path, utc_now())
            assert reserve_note_check(engine, utc_now()) is None
            preview, initial = authorize(http, route)
            assert preview["text"] == body and preview["recipient"]["id"] == model["id"]
            assert initial["state"] == "waiting" and preview["currentCalls"] == 1
            now = utc_now()
            assert reserve_note_check(engine, now) is None
            before_edit = now + EDIT_DELAY
            reserved = reserve_note_check(engine, before_edit)
            file = tmp_path / "library" / "notes" / (note + ".md")
            latest = "添加开头。\r\n" + body
            file.write_bytes(latest.encode())
            provider = transport(calls, [
                {"quote": correctable, "reason": "索引主要帮助定位数据，建立索引本身并不会删除行。"},
                {"quote": "不存在的句子。", "reason": "生成了错误锚点"},
                {"quote": "重复句。", "reason": "原文有两个候选"},
            ])
            dispatch_note_check(engine, reserved, transport=provider, now=lambda: before_edit)
            assert not calls and ok(http.get(route))["findings"] == []
            assert reserve_note_check(engine, before_edit) is None

            clock = before_edit + EDIT_DELAY
            drain(engine, clock, provider)
            assert calls == [("http://127.0.0.1:9115/v1/chat/completions", {"note": latest, "verifiedSections": []})]
            result = ok(http.get(route))
            [finding] = result["findings"]
            start = latest.index(correctable)
            assert (finding["start"], finding["end"], finding["line"]) == (start, start + len(correctable), 5)
            assert finding["evidence"] is None
            assert result["state"] == "partial" and result["unlocated"] == 2 and result["completed"] == 1
            assert file.read_bytes() == latest.encode()
            with transaction(engine, write=False) as db:
                assert active_calls(db) == 0 and billed_calls(db, billing_day(clock)) == 1
            assert reserve_note_check(engine, clock + timedelta(minutes=2)) is None
        finally:
            engine.dispose()


def test_long_notes_are_covered_and_unchanged_chunks_are_reused(tmp_path):
    calls = []
    text = "".join(chr(0x6C00 + part) * 2999 + "\n" for part in range(16))
    parts = list(pieces(text))
    assert len(text) == 48_000 and len(parts) > 1
    with session(tmp_path) as http:
        configure_reviewer(http)
        identity, endpoint = new_note(http, text=text)
        route = endpoint + "/checks"
        preview, _ = authorize(http, route)
        assert preview["currentCalls"] == len(parts)
        engine = open_db(tmp_path)
        try:
            clock = utc_now() + EDIT_DELAY
            drain(engine, clock, transport(calls, []))
            assert "".join(material["note"] for _, material in calls) == text
            assert all(len(material["note"]) <= CHARS_PER_CALL for _, material in calls)
            assert ok(http.get(route))["state"] == "reviewed"

            updated = text[:14_000] + "改" + text[14_001:]
            (tmp_path / "library" / "notes" / f"{identity}.md").write_bytes(updated.encode("utf-8"))
            scan_checks(engine, tmp_path, clock)
            assert ok(http.get(route))["state"] == "waiting"
            drain(engine, clock + EDIT_DELAY, transport(calls, []))
            assert len(calls) == len(parts) + 1 and "改" in calls[-1][1]["note"]
            assert ok(http.get(route))["completed"] == len(parts)
        finally:
            engine.dispose()


def test_checks_use_verified_excerpt_and_stop_when_evidence_changes(tmp_path):
    calls = []
    with session(tmp_path) as http:
        lesson, source_item = source_lesson(http, tmp_path, adopt=True)
        raw = "这里可以省去版框。"
        _, endpoint = new_note(http, lesson, raw)
        route = endpoint + "/checks"
        scope, _ = authorize(http, route, lesson)
        verified = DRAFT["sections"][0]
        assert scope["course"]["version"] == 2
        assert scope["course"]["sections"] == [{"index": 0, "title": verified["title"], "text": verified["text"]}]
        quote = verified["text"]
        engine = open_db(tmp_path)
        try:
            clock = utc_now() + EDIT_DELAY
            provider = transport(calls, [{"quote": raw, "reason": "讲稿要求使用版框。",
                                          "evidence": {"section": 0, "quote": quote}}])
            drain(engine, clock, provider)
            [finding] = ok(http.get(route))["findings"]
            assert finding["evidence"]["route"].startswith(f"/lessons/{lesson}?readingVersion=2&section=0")
            assert finding["evidence"]["quote"] == quote

            revision = ok(http.get(endpoint))["revision"]
            ok(http.put(endpoint, json={"content": raw + "\n再核对一次。", "baseRevision": revision}))
            scan_checks(engine, tmp_path, clock)
            claim = reserve_note_check(engine, clock + EDIT_DELAY)
            (tmp_path / source_item["path"]).write_text("原依据被外部修改。", encoding="utf-8")
            dispatch_note_check(engine, claim, transport=provider, now=lambda: clock + EDIT_DELAY)
            status = ok(http.get(route))
            assert len(calls) == 1 and status["state"] == "paused" and status["findings"] == []
        finally:
            engine.dispose()


def test_review_capacity_interruption_restore_and_stop(tmp_path):
    calls = []
    with session(tmp_path) as http:
        configure_reviewer(http)
        _, endpoint = new_note(http, text="第一篇等待核对。")
        _, other = new_note(http, text="第二篇等待核对。")
        route, more = endpoint + "/checks", other + "/checks"
        _, first = authorize(http, route)
        _, second = authorize(http, more)
        ok(http.put("/api/generation/settings", json={"dailyLimit": 1, "concurrency": 1}))
        engine = open_db(tmp_path)
        try:
            clock = utc_now() + EDIT_DELAY
            claim = reserve_note_check(engine, clock)
            assert reserve_note_check(engine, clock) is None
            settle_note_checks(engine, clock)
            recovered = reserve_note_check(engine, clock)
            assert recovered.id == claim.id
            assert admit_check(engine, recovered, lambda: clock)[0] == "第一篇等待核对。"
            settle_note_checks(engine, clock)
            assert reserve_note_check(engine, clock) is None
            assert ok(http.get(more))["dailyBlocked"]
            assert http.post(more + "/" + second["id"] + "/resume").status_code == 409
            with transaction(engine, write=False) as db:
                assert db.get(NoteCheckBatch, claim.id).status == "uncertain"
                assert billed_calls(db, billing_day(clock)) == 1

            assert hold_restored_tasks(engine, clock) == (2, 0)
            assert ok(http.get(more))["state"] == "paused"
            assert reserve_note_check(engine, clock + timedelta(days=1)) is None
            ok(http.delete(route + "/" + first["id"]))
            assert ok(http.get(route))["state"] == "off"

            _, enabled = authorize(http, more)
            fresh = reserve_note_check(engine, clock + timedelta(days=1, seconds=3))
            assert enabled["id"] != second["id"]
            ok(http.delete(more + "/" + enabled["id"]))
            dispatch_note_check(engine, fresh, transport=transport(calls, []), now=lambda: clock + timedelta(days=1, seconds=3))
            assert calls == [] and ok(http.get(more))["state"] == "off"
        finally:
            engine.dispose()
