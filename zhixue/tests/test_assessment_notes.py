from copy import deepcopy
from datetime import timedelta
import json

import pytest

from backend.assessment_worker import dispatch_assessment, reserve_assessment
from backend.db import ModelUse, open_db, transaction
from backend.model_study import utc_now
from backend.note_check_worker import dispatch_note_check, reserve_note_check
from backend.note_checks import EDIT_DELAY
from test_model_assessments import COMPOSED, GRADE, SOURCE, assess_one, review, source_lesson
from test_model_study import ok, session
from test_note_ai import new_note
from test_note_checks import authorize as grant_check, transport as check_transport


GOOD = "排妥活字后，使用版框来固定字行。"
QUESTIONABLE = "纸张必须先加热三十分钟。"
FLAGGED = "用沸水冲洗木制活字才能固色。"
PRIVATE = "未被选中的个人备注🙂。"


def locator(http, endpoint, text):
    saved = ok(http.get(endpoint))
    start = saved["content"].index(text)
    return {"noteId": endpoint.rsplit("/", 1)[1], "revision": saved["revision"], "start": start, "end": start + len(text)}


def disclose(http, route, locators):
    payload = {"maxCalls": 12, "noteExcerpts": locators}
    result = ok(http.post(route + "/preview", json=payload))
    return {**payload, "confirmation": result["confirmation"]}, result


def verdict(key="N1", quote=SOURCE):
    return {"key": key, "verdict": "supported", "reason": "原资料直接支持这一步。",
            "evidence": {"section": 0, "quote": quote}}


def fields(prompt, delimiter="复习数据 JSON"):
    return json.loads(prompt.split(f"\n{delimiter}\n", 1)[1])


def test_only_independently_supported_note_material_reaches_question_generation(tmp_path):
    prompts = []
    original = "\r\n\r\n".join([PRIVATE, GOOD, FLAGGED, QUESTIONABLE])
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        note, endpoint = new_note(http, text=original)
        route = f"/api/lessons/{lesson}/assessment"
        assert next(item for item in ok(http.get(route + "/note-options")) if item["id"] == note)["linked"] is False

        grant_check(http, endpoint + "/checks")
        engine = open_db(tmp_path)
        try:
            clock = utc_now() + EDIT_DELAY
            claim = reserve_note_check(engine, clock)
            dispatch_note_check(engine, claim,
                                transport=check_transport([], [{"quote": FLAGGED, "reason": "找不到支持该结论的依据。"}]),
                                now=lambda: clock)
        finally:
            engine.dispose()
        locations = [locator(http, endpoint, sample) for sample in (GOOD, FLAGGED, QUESTIONABLE)]
        grant, preview = disclose(http, route, locations)
        assert [item["text"] for item in preview["noteExcerpts"]] == [GOOD, QUESTIONABLE]
        assert preview["noteExcerpts"][0]["line"] == 3
        assert "待核对" in preview["excludedNotes"][0]["reason"]
        assert ok(http.get(route))["jobs"] == []
        assert http.post(route + "/authorize", json={**grant, "noteExcerpts": locations[:1]}).status_code == 409

        ok(http.post(route + "/authorize", json=grant))
        assert [job["kind"] for job in ok(http.get(route))["jobs"]].count("note_verify") == 1
        assert assess_one(tmp_path, {"decisions": [verdict(), verdict("N3", "资料中其实没有这段说明。")]}, prompts)
        reviewer, prompt = prompts[0]
        assert reviewer == "independent-model"
        evidence = fields(prompt, "笔记核验 JSON")
        assert evidence["notes"] == [{"key": "N1", "text": GOOD}, {"key": "N3", "text": QUESTIONABLE}]
        assert FLAGGED not in prompt and PRIVATE not in prompt
        [accepted, rejected] = ok(http.get(route))["noteExcerpts"]
        assert accepted["verified"] and not rejected["verified"] and rejected["issue"]

        assert assess_one(tmp_path, COMPOSED, prompts)
        material = fields(prompts[1][1])
        assert material["verifiedNoteFocus"] == [GOOD]
        assert material["section"]["evidence"] == [SOURCE]
        assert QUESTIONABLE not in prompts[1][1] and FLAGGED not in prompts[1][1]

        item = review(http, lesson)
        ok(http.post("/api/demo/next-day"))
        course_group = ok(http.get(f"/api/lessons/{lesson}/assessments/{item['key']}"))["group"]
        assert "noteReferences" not in course_group
        target = "/api/assessments/" + course_group["id"]
        ok(http.post(target + "/submit", json={"action": "submit", "answers": ["用版框固定。", "纸张尺寸。"]}))
        assert assess_one(tmp_path, GRADE, prompts)
        grade = fields(prompts[2][1])
        assert "verifiedNoteFocus" not in grade and GOOD not in prompts[2][1]
        assert all(question["quote"] == SOURCE for question in grade["questions"])
        result = ok(http.get(target))
        [reference] = result["noteReferences"]
        assert reference["text"] == GOOD and reference["noteId"] == note and "at=3" in reference["route"]
        assert result["result"]["outcome"] == "passed"
        assert ok(http.get(endpoint))["content"] == original


def test_note_version_is_rechecked_on_authorize_and_immediately_before_dispatch(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        note, endpoint = new_note(http, text=GOOD)
        route = f"/api/lessons/{lesson}/assessment"
        old, _ = disclose(http, route, [locator(http, endpoint, GOOD)])
        file = tmp_path / "library" / "notes" / f"{note}.md"
        file.write_text(PRIVATE + "\n" + GOOD, encoding="utf-8")
        assert http.post(route + "/authorize", json=old).status_code == 409
        grant, _ = disclose(http, route, [locator(http, endpoint, GOOD)])
        ok(http.post(route + "/authorize", json=grant))

        engine = open_db(tmp_path)
        try:
            claim = reserve_assessment(engine, utc_now())
            assert claim.phase == "note_verify"
            file.write_text("笔记已经是另一个版本。", encoding="utf-8")
            dispatch_assessment(engine, claim, transport=lambda *args: pytest.fail("stale note was sent"))
            with transaction(engine, write=False) as db:
                assert db.get(ModelUse, claim.id).status == "not_sent"
        finally:
            engine.dispose()
        view = ok(http.get(route))
        assert "版本" in view["noteExcerpts"][0]["issue"] and view["noteState"] == "ready"
        assert assess_one(tmp_path, COMPOSED, prompts)
        assert "verifiedNoteFocus" not in fields(prompts[0][1])


@pytest.mark.parametrize("phase", ["note_verify", "objectives"])
def test_output_using_a_note_edited_during_model_call_is_not_adopted(tmp_path, phase):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        note, endpoint = new_note(http, lesson, GOOD)
        route = f"/api/lessons/{lesson}/assessment"
        grant, _ = disclose(http, route, [locator(http, endpoint, GOOD)])
        ok(http.post(route + "/authorize", json=grant))
        file = tmp_path / "library" / "notes" / f"{note}.md"

        def external_write():
            file.write_text("外部编辑后的新词句。", encoding="utf-8")

        assert assess_one(tmp_path, {"decisions": [verdict()]}, prompts, on_request=external_write if phase == "note_verify" else None)
        assert assess_one(tmp_path, deepcopy(COMPOSED), prompts, on_request=external_write if phase == "objectives" else None)
        if phase == "note_verify":
            assert "verifiedNoteFocus" not in fields(prompts[-1][1])
            assert review(http, lesson)["kind"] == "model"
        else:
            assert fields(prompts[-1][1])["verifiedNoteFocus"] == [GOOD]
            assert ok(http.get("/api/reviews"))["reviews"] == []
            assert any("出题期间" in (job["error"] or "") for job in ok(http.get(route))["jobs"])
            ok(http.post(route + "/retry"))
            assert assess_one(tmp_path, COMPOSED, prompts)
            assert "verifiedNoteFocus" not in fields(prompts[-1][1])
            assert review(http, lesson)["kind"] == "model"

def test_question_dispatch_rebuilds_prompt_if_selected_note_changed_after_reservation(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        note, endpoint = new_note(http, lesson, GOOD)
        route = f"/api/lessons/{lesson}/assessment"
        grant, _ = disclose(http, route, [locator(http, endpoint, GOOD)])
        ok(http.post(route + "/authorize", json=grant))
        assert assess_one(tmp_path, {"decisions": [verdict()]}, prompts)
        engine = open_db(tmp_path)
        try:
            claim = reserve_assessment(engine, utc_now())
            assert claim.phase == "objectives" and fields(claim.prompt)["verifiedNoteFocus"] == [GOOD]
            (tmp_path / "library" / "notes" / f"{note}.md").write_text("另一个已编辑的版本。", encoding="utf-8")

            def provider(_address, _headers, payload):
                prompt = payload["messages"][0]["content"]
                assert GOOD not in prompt and "verifiedNoteFocus" not in fields(prompt)
                return 200, json.dumps({"choices": [{"message": {"content": json.dumps(COMPOSED)}}]}).encode()

            dispatch_assessment(engine, claim, transport=provider)
        finally:
            engine.dispose()
        assert review(http, lesson)["kind"] == "model"
        assert not ok(http.get(route))["noteExcerpts"][0]["verified"]

