from pathlib import Path

from backend.db import open_db
from test_model_assessments import COMPOSED, GRADE, GROUP, assess_one, review, source_lesson
from test_course_verification import SOURCE
from test_model_study import ok, session


DRAFT = {"entries": [
    {"index": 1, "cause": "回答没有说明要检查字行是否平整。", "explanation": "试印前沿着字行检查是否平整，再进行试印。"},
]}
CHECKED = {"sections": [
    {"index": 0, "verdict": "supported", "reason": "错因与已判定的题目相符，讲解有资料支持。",
     "citations": [{"source": "S1", "quote": SOURCE}], "dependsOn": []},
]}


def authorize_remediation(http, lesson, limit=8):
    endpoint = f"/api/lessons/{lesson}/assessment"
    options = {"maxCalls": limit, "remediation": True}
    scope = ok(http.post(endpoint + "/preview", json=options))
    ok(http.post(endpoint + "/authorize", json={**options, "confirmation": scope["confirmation"]}))
    return scope


def complete_round(http, folder, prompts, *, enabled=True):
    lesson, evidence = source_lesson(http, folder)
    if enabled:
        authorize_remediation(http, lesson)
    else:
        from test_model_assessments import permit
        permit(http, lesson)
    assert assess_one(folder, COMPOSED, prompts)
    item = review(http, lesson)
    ok(http.post("/api/demo/next-day"))
    group = ok(http.get(f"/api/lessons/{lesson}/assessments/{item['key']}"))["group"]
    target = f"/api/assessments/{group['id']}"
    answers = ["通过版框固定活字。这是正确题目上的私有备注。", "我只检查纸张尺寸。"]
    ok(http.post(target + "/submit", json={"action": "submit", "answers": answers}))
    assert assess_one(folder, GRADE, prompts)
    assert ok(http.get(target))["result"]["outcome"] == "passed"
    return lesson, target, evidence


def publish_card(folder):
    from backend.remediation import publish_remediation

    engine = open_db(folder)
    try:
        return publish_remediation(engine, folder)
    finally:
        engine.dispose()


def test_wrong_answer_card_is_attached_and_indexed_only_after_independent_verification(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, assessment, evidence = complete_round(http, tmp_path, prompts)
        before = ok(http.get(f"/api/lessons/{lesson}"))["body"]
        location = f"/api/lessons/{lesson}/remediation"
        assert ok(http.get(assessment))["remediation"]["state"] == "drafting"
        assert assess_one(tmp_path, DRAFT, prompts)
        assert "我只检查纸张尺寸" in prompts[-1][1] and "私有备注" not in prompts[-1][1]
        assert ok(http.get(location))["cards"] == []
        assert not publish_card(tmp_path)
        assert assess_one(tmp_path, CHECKED, prompts)
        assert prompts[-1][0] == "independent-model"
        assert publish_card(tmp_path)
        card, = ok(http.get(location))["cards"]
        assert card["sectionIndex"] == 0 and card["courseVersion"] == 1
        assert card["entries"][0]["explanation"] == DRAFT["entries"][0]["explanation"]
        assert card["citations"][0]["fileId"] == evidence["id"]
        assert ok(http.get(f"/api/lessons/{lesson}"))["body"] == before
        files = ok(http.get("/api/files"))
        file, = [item for item in files if item["kind"] == "supplement"]
        assert file["id"] == card["fileId"]
        value = ok(http.get(f"/api/files/{file['id']}?revision={card['revision']}"))
        assert "试印前沿着字行检查" in value["content"] and "我只检查纸张尺寸" not in value["content"]
        assert (Path(tmp_path) / file["path"]).is_file()
        matches = ok(http.get("/api/files/search", params={"q": "试印前沿着字行", "kind": "supplement"}))["hits"]
        assert any(hit["id"] == card["fileId"] for hit in matches)


def test_remediation_scope_requires_consent_and_can_resume_existing_grade(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, assessment, _ = complete_round(http, tmp_path, prompts, enabled=False)
        assert ok(http.get(assessment))["remediation"]["state"] == "authorization_required"
        assert not assess_one(tmp_path, DRAFT, prompts)
        authorize_remediation(http, lesson)
        assert assess_one(tmp_path, DRAFT, prompts)
        assert ok(http.get(assessment))["remediation"]["state"] == "verifying"


def test_unsupported_citation_blocks_card_and_ready_cards_still_publish(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, assessment, _ = complete_round(http, tmp_path, prompts)
        assert assess_one(tmp_path, DRAFT, prompts)
        fake = {"sections": [{**CHECKED["sections"][0], "citations": [{"source": "S1", "quote": "资料不存在这段话。"}]}]}
        assert assess_one(tmp_path, fake, prompts)
        assert not publish_card(tmp_path)
        assert ok(http.get(assessment))["remediation"]["state"] == "disputed"
        assert not [file for file in ok(http.get("/api/files")) if file["kind"] == "supplement"]
        assert ok(http.get(assessment))["result"]["outcome"] == "passed"

        item = review(http, lesson)
        endpoint = f"/api/lessons/{lesson}/assessments/{item['key']}"
        for _ in range(2):
            ok(http.post("/api/demo/next-day"))
        next_group = ok(http.post(endpoint + "/prepare", json={"mode": "formal"}))
        changed = {**GROUP, "questions": [{**question, "text": f"另一种问法 · {question['text']}"} for question in GROUP["questions"]]}
        assert assess_one(tmp_path, changed, prompts)
        target = f"/api/assessments/{next_group['id']}"
        ok(http.post(target + "/submit", json={"action": "submit", "answers": ["固定在版框。", "平整度不知道。"]}))
        assert assess_one(tmp_path, GRADE, prompts) and assess_one(tmp_path, DRAFT, prompts)
        assert assess_one(tmp_path, CHECKED, prompts) and publish_card(tmp_path)
        card, = ok(http.get(f"/api/lessons/{lesson}/remediation"))["cards"]
        assert card["round"] == 2
        authorize_remediation(http, lesson, limit=12)
        ok(http.post(assessment + "/remediation/retry"))
        assert assess_one(tmp_path, DRAFT, prompts) and assess_one(tmp_path, CHECKED, prompts)
        assert publish_card(tmp_path)
        cards = ok(http.get(f"/api/lessons/{lesson}/remediation"))["cards"]
        assert [card["round"] for card in cards] == [2, 1]


def test_publish_conflict_can_retry_locally_and_keeps_the_original_file(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, assessment, _ = complete_round(http, tmp_path, prompts)
        assert assess_one(tmp_path, DRAFT, prompts) and assess_one(tmp_path, CHECKED, prompts)
        card = ok(http.get(assessment))["remediation"]
        assert card["state"] == "publishing"
        path = tmp_path / "library" / "supplements" / f"{card['id']}.md"
        path.parent.mkdir()
        path.write_text("本地有同名文件。", encoding="utf-8")
        assert publish_card(tmp_path)
        assert path.read_text(encoding="utf-8") == "本地有同名文件。"
        assert ok(http.get(assessment))["remediation"]["state"] == "blocked"
        assert ok(http.get(f"/api/lessons/{lesson}/remediation"))["cards"] == []
        path.rename(path.with_name("本地原文件.md"))
        ok(http.post(assessment + "/remediation/retry"))
        assert publish_card(tmp_path)
        assert not assess_one(tmp_path, DRAFT, prompts) and len(prompts) == 4
        assert ok(http.get(assessment))["remediation"]["state"] == "published"


def test_recheck_hides_the_old_annotation_and_preserves_the_archived_file(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, assessment, _ = complete_round(http, tmp_path, prompts)
        assert assess_one(tmp_path, DRAFT, prompts) and assess_one(tmp_path, CHECKED, prompts) and publish_card(tmp_path)
        cards = f"/api/lessons/{lesson}/remediation"
        old, = ok(http.get(cards))["cards"]
        file = ok(http.get(f"/api/files/{old['fileId']}"))
        archived = (tmp_path / file["path"]).read_bytes()
        assert ok(http.post(assessment + "/recheck"))["state"] == "rechecking"
        assert ok(http.get(cards))["cards"] == []
        assert not [item for item in ok(http.get("/api/files")) if item["kind"] == "supplement"]
        corrected = {**GRADE, "items": [{**item, "met": True} for item in GRADE["items"]]}
        assert assess_one(tmp_path, corrected, prompts)
        output = ok(http.get(assessment))
        assert len(output["history"]) == 2 and output["remediation"] is None
        assert (tmp_path / file["path"]).read_bytes() == archived
        assert not assess_one(tmp_path, DRAFT, prompts)


def test_recheck_during_verification_discards_the_pending_result(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, assessment, _ = complete_round(http, tmp_path, prompts)
        assert assess_one(tmp_path, DRAFT, prompts)
        assert assess_one(tmp_path, CHECKED, prompts, on_request=lambda: ok(http.post(assessment + "/recheck")))
        assert ok(http.get(assessment))["state"] == "rechecking"
        assert not publish_card(tmp_path)
        assert not (tmp_path / "library" / "supplements").exists()
        corrected = {**GRADE, "items": [{**item, "met": True} for item in GRADE["items"]]}
        assert assess_one(tmp_path, corrected, prompts)
        assert ok(http.get(f"/api/lessons/{lesson}/remediation"))["cards"] == []
