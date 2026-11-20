from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import open_db
from backend.model_study import utc_now
from backend.study_consolidation_worker import dispatch, process_one, publish_one, reserve, settle_interrupted
from test_course_verification import SOURCE, authorize, model_traffic, prepare, publish
from test_lesson_assistant import PASS_REPLY, ask_one, open_thread, permit_assistant
from test_model_assessments import COMPOSED, assess_one, permit, review
from test_model_study import admit_for_interruption, assert_interrupted_use, ok, run, session


EXPLANATION = "正式试印前，先把活字固定在版框内，再检查字行是否平整。"
SUPPLEMENT = {"section": {"title": "答疑补充 · 怎样准备试印", "text": EXPLANATION, "checks": ["试印前的步骤"]}}
ACCEPT = {"verdict": "supported", "reason": "两项操作均可在原文定位。", "citations": [{"source": "S1", "quote": SOURCE}]}


def completed_course(http, folder):
    calls = []
    project, lesson, item, evidence = prepare(http, folder, calls)
    authorize(http, project, evidence)
    assert run(folder, model_traffic(calls)) and publish(folder)
    return lesson, item


def ask_grounded(http, folder, lesson, question_text):
    permit_assistant(http, lesson)
    thread = open_thread(http, lesson)
    question_id = str(uuid4())
    ok(http.post(f"/api/conversations/{thread}/questions", json={"id": question_id, "text": question_text}))
    assert ask_one(folder, PASS_REPLY, [])
    return question_id


def permit_consolidation(http, lesson, limit=8):
    path = f"/api/lessons/{lesson}/consolidation"
    preview = ok(http.post(path + "/preview", json={"maxCalls": limit}))
    ok(http.post(path + "/authorize", json={"maxCalls": limit, "confirmation": preview["confirmation"]}))
    return preview


def traffic(result, prompts, status=200):
    def transport(address, headers, payload):
        prompts.append((payload["model"], payload["messages"][0]["content"]))
        wire = {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}
        return status, json.dumps(wire).encode()
    return transport


def consolidate_one(folder, reply, prompts):
    engine = open_db(folder)
    try:
        return process_one(engine, transport=traffic(reply, prompts))
    finally:
        engine.dispose()


def publish_consolidated(folder):
    engine = open_db(folder)
    try:
        return publish_one(engine, folder)
    finally:
        engine.dispose()


def test_learned_triggers_verified_addition_and_preserves_review_after_a_failed_check(tmp_path):
    prompts = []
    question = "为什么试印以前还要检查字行？"
    with session(tmp_path) as http:
        lesson, _ = completed_course(http, tmp_path)
        question_id = ask_grounded(http, tmp_path, lesson, question)
        endpoint = f"/api/lessons/{lesson}"
        workflow = endpoint + "/consolidation"
        original = ok(http.get(endpoint))["body"]
        permit(http, lesson)
        timer = ok(http.post("/api/timer", json={"action": "start", "lessonId": lesson}))["active"]
        ok(http.post("/api/timer", json={"action": "finish", "lessonId": lesson, "timerId": timer["id"]}))
        assert ok(http.get(workflow))["jobs"] == []
        learned_on = ok(http.put(endpoint + "/learned", json={"learned": True}))["learnedOn"]
        job, = ok(http.get(workflow))["jobs"]
        assert job["state"] == "blocked" and job["reason"] == "authorization_required"
        assert not consolidate_one(tmp_path, SUPPLEMENT, prompts) and not prompts
        scope = permit_consolidation(http, lesson)
        assert scope["main"]["model"] == "isolated-model" and scope["review"]["model"] == "independent-model"
        assert any(pair["question"] == question for pair in scope["pairs"])
        assert assess_one(tmp_path, COMPOSED, [])
        date_before = review(http, lesson)["dueOn"]
        assert consolidate_one(tmp_path, SUPPLEMENT, prompts)
        invalid = {**ACCEPT, "citations": [{"source": "S1", "quote": "没有出处的句子。"}]}
        assert consolidate_one(tmp_path, invalid, prompts)
        failed, = ok(http.get(workflow))["jobs"]
        assert failed["state"] == "blocked" and failed["reason"] == "not_verified" and failed["phase"] == "review"
        assert not publish_consolidated(tmp_path)
        assert ok(http.get(endpoint))["body"] == original and review(http, lesson)["dueOn"] == date_before
        assert ok(http.get(endpoint))["project"]["lessons"][0]["learnedOn"] == learned_on
    with session(tmp_path) as http:
        retry = workflow + f"/jobs/{job['id']}/retry"
        ok(http.post(retry))
        assert consolidate_one(tmp_path, ACCEPT, prompts) and publish_consolidated(tmp_path)
        assert [model for model, _ in prompts] == ["isolated-model", "independent-model", "independent-model"]
        assert question in prompts[0][1] and all(question not in prompt for _, prompt in prompts[1:])
        assert all(SOURCE in prompt and "私有附注" not in prompt for _, prompt in prompts)
        body = ok(http.get(endpoint))
        assert body["courseVersion"] == 2 and body["body"].startswith(original) and EXPLANATION in body["body"]
        assert ok(http.get(endpoint + "/versions/1"))["markdown"] == original
        addition = ok(http.get(endpoint + "/versions/2"))["sections"][-1]
        assert addition["consolidation"]["id"] == job["id"] and addition["consolidation"]["questions"] == [question_id]
        assert addition["citations"][0]["quote"] == SOURCE
        hits = ok(http.get("/api/files/search", params={"q": "再检查字行是否平整", "kind": "course"}))["hits"]
        assert any(item["id"] == f"course-{lesson}" for item in hits)
        complete, = ok(http.get(workflow))["jobs"]
        assert complete["state"] == "complete" and complete["publishedVersion"] == 2
        assert review(http, lesson)["dueOn"] == date_before
        ok(http.put(endpoint + "/learned", json={"learned": True}))
        assert len(ok(http.get(workflow))["jobs"]) == 1
        assert not consolidate_one(tmp_path, SUPPLEMENT, prompts)


def test_learning_day_waits_for_the_frozen_questions_and_excludes_an_older_day(tmp_path):
    prompts = []
    clock = datetime(2026, 6, 10, 15, 50, tzinfo=UTC)
    app = create_app(tmp_path, "learning-day-test", now=lambda: clock)
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer learning-day-test"}) as http:
        lesson, _ = completed_course(http, tmp_path)
        ask_grounded(http, tmp_path, lesson, "昨晚提出且含私有内容的问题")
        clock += timedelta(minutes=15)
        path = f"/api/lessons/{lesson}/consolidation"
        permit_consolidation(http, lesson)
        thread = open_thread(http, lesson)
        endpoint = f"/api/conversations/{thread}/questions"
        ok(http.post(endpoint, json={"id": str(uuid4()), "text": "今天勾选之前提出的问题"}))
        ok(http.put(f"/api/lessons/{lesson}/learned", json={"learned": True}))
        assert ok(http.get(path))["jobs"][0]["questionCount"] == 1
        assert not consolidate_one(tmp_path, SUPPLEMENT, prompts) and not prompts
        assert ask_one(tmp_path, PASS_REPLY, [])
        assert consolidate_one(tmp_path, SUPPLEMENT, prompts)
        assert "今天勾选之前提出的问题" in prompts[0][1]
        assert "昨晚提出且含私有内容的问题" not in prompts[0][1]


@pytest.mark.parametrize("sending", [False, True], ids=["before-send", "after-admission"])
def test_revocation_interruption_and_edited_file_keep_the_saved_draft_and_fence_publication(tmp_path, sending):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = completed_course(http, tmp_path)
        ask_grounded(http, tmp_path, lesson, "哪些检查适合放在试印之前？")
        endpoint = f"/api/lessons/{lesson}"
        path = endpoint + "/consolidation"
        permit_consolidation(http, lesson, limit=4)
        ok(http.put(endpoint + "/learned", json={"learned": True}))
        running, = ok(http.get(path))["jobs"]
        retry = path + f"/jobs/{running['id']}/retry"
        assert consolidate_one(tmp_path, SUPPLEMENT, prompts)
        engine = open_db(tmp_path)
        try:
            claim = reserve(engine, utc_now())
            assert claim and claim.phase == "review"
            ok(http.post(path + "/revoke"))
            dispatch(engine, claim, transport=traffic(ACCEPT, prompts))
            failed, = ok(http.get(path))["jobs"]
            assert failed["reason"] == "authorization_revoked" and failed["draft"] == SUPPLEMENT["section"]
            assert len(prompts) == 1 and http.post(retry).status_code == 409
            permit_consolidation(http, lesson, limit=4)
            claim = reserve(engine, utc_now())
            assert claim and claim.phase == "review"
            if sending:
                admit_for_interruption(engine, claim.id, claim.prompt, utc_now())
            settle_interrupted(engine, utc_now())
            assert_interrupted_use(engine, claim.id, sending)
            dispatch(engine, claim, transport=traffic(ACCEPT, prompts))
            assert len(prompts) == 1
            resumed, = ok(http.get(path))["jobs"]
            assert resumed["phase"] == "review"
            assert resumed["state"] == ("blocked" if sending else "queued")
            assert resumed["reason"] == ("uncertain" if sending else None)
        finally:
            engine.dispose()
        if sending:
            assert not consolidate_one(tmp_path, ACCEPT, prompts)
            ok(http.post(retry))
        assert consolidate_one(tmp_path, ACCEPT, prompts)
        course_file = ok(http.get(f"/api/files/course-{lesson}"))
        current = tmp_path / course_file["path"]
        previous = current.read_bytes()
        original_with_edit = previous + "\n\n手工修订内容必须保留。\n".encode()
        current.write_bytes(original_with_edit)
        assert publish_consolidated(tmp_path)
        blocked, = ok(http.get(path))["jobs"]
        assert blocked["phase"] == "publish" and blocked["reason"] == "course_changed"
        assert current.read_bytes() == original_with_edit and ok(http.get(endpoint))["courseVersion"] == 1
        current.write_bytes(previous)
        ok(http.post(retry))
        assert publish_consolidated(tmp_path) and ok(http.get(endpoint))["courseVersion"] == 2
        assert len(prompts) == 2


def test_automatic_retry_limit_is_shared_by_drafting_and_independent_review(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = completed_course(http, tmp_path)
        ask_grounded(http, tmp_path, lesson, "试印前怎样锁紧版框？")
        endpoint = f"/api/lessons/{lesson}"
        permit_consolidation(http, lesson)
        ok(http.put(endpoint + "/learned", json={"learned": True}))
        engine = open_db(tmp_path)
        clock = utc_now()
        try:
            for _ in range(2):
                assert process_one(engine, transport=traffic({}, prompts, status=503), now=lambda: clock)
                assert not process_one(engine, transport=traffic(SUPPLEMENT, prompts), now=lambda: clock)
                clock += timedelta(seconds=8)
            assert process_one(engine, transport=traffic(SUPPLEMENT, prompts), now=lambda: clock)
            assert process_one(engine, transport=traffic({}, prompts, status=503), now=lambda: clock)
            clock += timedelta(seconds=8)
            assert not process_one(engine, transport=traffic(ACCEPT, prompts), now=lambda: clock)
        finally:
            engine.dispose()
        job, = ok(http.get(endpoint + "/consolidation"))["jobs"]
        assert job["state"] == "blocked" and job["phase"] == "review" and job["reason"] == "service_unavailable"
        assert job["draft"] == SUPPLEMENT["section"] and len(prompts) == 4


def test_batches_preserve_all_eligible_questions_across_compatible_new_versions(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = completed_course(http, tmp_path)
        permit_assistant(http, lesson, limit=12)
        thread = open_thread(http, lesson)
        ids = {str(uuid4()) for _ in range(9)}
        for number, identity in enumerate(sorted(ids)):
            ok(http.post(f"/api/conversations/{thread}/questions", json={
                "id": identity, "text": f"请补充试印步骤的用途，问题 {number}。",
            }))
            assert ask_one(tmp_path, PASS_REPLY, [])
        endpoint = f"/api/lessons/{lesson}"
        permit_consolidation(http, lesson)
        ok(http.put(endpoint + "/learned", json={"learned": True}))
        assert sorted(job["questionCount"] for job in ok(http.get(endpoint + "/consolidation"))["jobs"]) == [1, 8]
        another = {"section": {"title": "字行复查",
                                "text": "试印前检查字行是否平整，拣字后用版框固定活字。", "checks": []}}
        for draft in (SUPPLEMENT, another):
            assert consolidate_one(tmp_path, draft, prompts)
            assert consolidate_one(tmp_path, ACCEPT, prompts)
            assert publish_consolidated(tmp_path)
        latest = ok(http.get(endpoint + "/versions/3"))
        summaries = latest["sections"][1:]
        assert {identity for item in summaries for identity in item["consolidation"]["questions"]} == ids
        assert all(job["state"] == "complete" for job in ok(http.get(endpoint + "/consolidation"))["jobs"])
        assert len(prompts) == 4
