import json
from uuid import uuid4

import pytest

from backend.db import open_db
from backend.lesson_answer_worker import dispatch_answer, reserve_answer, settle_interrupted_answers
from backend.model_study import utc_now
from test_course_verification import DRAFT, SOURCE
from test_model_assessments import source_lesson
from test_model_study import admit_for_interruption, assert_interrupted_use, ok, session


PASS_REPLY = {
    "answer": "排好的活字要用版框固定。这样试印时字行才能保持在原来的位置。",
    "support": "grounded",
    "citations": [{"source": "S1", "quote": SOURCE}],
}


def assistant_transport(output, prompts):
    def transport(address, headers, request):
        prompts.append(request["messages"][0]["content"])
        body = {"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]}
        return 200, json.dumps(body).encode()
    return transport


def ask_one(folder, output, prompts):
    from backend.lesson_answer_worker import process_one

    engine = open_db(folder)
    try:
        return process_one(engine, transport=assistant_transport(output, prompts))
    finally:
        engine.dispose()


def open_thread(http, lesson):
    return ok(http.post("/api/conversations", json={"id": str(uuid4()), "lessonId": lesson}))["id"]


def permit_assistant(http, lesson, limit=7):
    path = f"/api/lessons/{lesson}/assistant"
    scope = ok(http.post(path + "/preview", json={"maxCalls": limit}))
    ok(http.post(path + "/authorize", json={"maxCalls": limit, "confirmation": scope["confirmation"]}))
    return scope


def selection(version=1):
    text = DRAFT["sections"][0]["text"]
    start = text.index("固定")
    return {"version": version, "sectionIndex": 0, "start": start, "end": len(text), "text": text[start:]}


def test_route_commands_in_model_course_stay_local_and_do_not_authorize_questions(tmp_path):
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        project = ok(http.get(f"/api/lessons/{lesson}"))["project"]["id"]
        thread = open_thread(http, lesson)
        route = f"/api/conversations/{thread}/questions"
        versions = f"/api/projects/{project}/versions"
        before = ok(http.get(versions))
        settings = ok(http.get(f"/api/lessons/{lesson}/assistant"))
        payload = {"id": str(uuid4()), "text": "这节我已经会了。"}
        assert http.post(route, json={**payload, "selection": selection()}).status_code == 409
        command = ok(http.post(route, json=payload))
        assert command["courseAction"] == {"kind": "known", "lessonId": lesson, "projectId": project}
        assert [(item["role"], "answer" in item) for item in command["messages"]] == [("user", False), ("assistant", False)]
        assert ok(http.post(route, json=payload)) == command
        assert http.post(route, json={**payload, "text": "修改我的学习目标"}).status_code == 409
        assert http.post(route, json={"id": str(uuid4()), "text": "这一步为什么重要？"}).status_code == 409
        assert http.post(route, json={"id": str(uuid4()), "text": "这节我已经会了？！"}).status_code == 409
        assert ok(http.get(versions)) == before
        assert ok(http.get(f"/api/lessons/{lesson}/assistant")) == settings
        assert not ask_one(tmp_path, PASS_REPLY, [])

        permit_assistant(http, lesson)
        selected = {"id": str(uuid4()), "text": "这节我已经会了。", "selection": selection()}
        answered = ok(http.post(route, json=selected))
        assert answered.get("courseAction") is None and answered["messages"][-1]["answer"]["state"] == "queued"
        replay = ok(http.post(f"/api/conversations/{thread}/messages", json={
            "id": selected["id"], "text": selected["text"],
        }))
        assert replay.get("courseAction") is None and replay["messages"] == answered["messages"]


def test_selected_question_requires_consent_and_persists_anchor_reply_and_favorite(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path, adopt=True)
        thread = open_thread(http, lesson)
        path = f"/api/conversations/{thread}/questions"
        request = {"id": str(uuid4()), "text": "这个步骤为什么要用版框？", "selection": selection(version=2)}
        assert http.post(path, json=request).status_code == 409
        scope = permit_assistant(http, lesson)
        assert scope["version"] == 2 and [entry["index"] for entry in scope["sections"]] == [0]
        queued = ok(http.post(path, json=request))
        assert queued["messages"][-1]["answer"]["state"] == "queued"
        assert queued["messages"][0]["question"]["selection"] == request["selection"]
        assert ok(http.post(path, json=request))["messages"] == queued["messages"]
        assert ask_one(tmp_path, PASS_REPLY, prompts)
        assert len(prompts) == 1 and SOURCE in prompts[0] and request["selection"]["text"] in prompts[0]
        assert "加热" not in prompts[0] and "私有附注" not in prompts[0]
        saved = ok(http.get(f"/api/conversations/{thread}"))["messages"]
        assert saved[-1]["role"] == "assistant" and saved[-1]["answer"]["support"] == "grounded"
        assert saved[-1]["answer"]["citations"][0]["quote"] == SOURCE
        ok(http.put(f"/api/lesson-questions/{request['id']}/favorite", json={"favorite": True}))
    with session(tmp_path) as http:
        favorites = ok(http.get(f"/api/lessons/{lesson}/assistant/favorites"))
        assert [(item["conversationId"], item["selection"]) for item in favorites["items"]] == [(thread, request["selection"])]
        assert ok(http.get(f"/api/conversations/{thread}"))["messages"][-1]["text"] == PASS_REPLY["answer"]
        assert not ask_one(tmp_path, PASS_REPLY, prompts)


def test_invalid_anchor_and_invented_citation_remain_outside_verified_context(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        thread = open_thread(http, lesson)
        permit_assistant(http, lesson)
        route = f"/api/conversations/{thread}/questions"
        message = {"id": str(uuid4()), "text": "帮我理解这里。", "selection": {**selection(), "text": "不在课程里的句子"}}
        assert http.post(route, json=message).status_code == 409
        assert ok(http.get(f"/api/conversations/{thread}"))["messages"] == []
        ok(http.post(route, json={**message, "selection": selection()}))
        invented = {**PASS_REPLY, "citations": [{"source": "S1", "quote": "资料中没有这个说法。"}]}
        assert ask_one(tmp_path, invented, prompts)
        answer = ok(http.get(f"/api/conversations/{thread}"))["messages"][-1]["answer"]
        assert answer["support"] == "unverified" and answer["citations"] == []


def test_followup_uses_only_the_latest_grounded_pair_from_its_thread(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path, adopt=True)
        thread, unrelated = open_thread(http, lesson), open_thread(http, lesson)
        permit_assistant(http, lesson)
        for identity, text in ((thread, "第一轮私下示例"), (thread, "怎么固定活字？"), (unrelated, "另一个对话的问题")):
            ok(http.post(f"/api/conversations/{identity}/questions", json={"id": str(uuid4()), "text": text}))
            assert ask_one(tmp_path, PASS_REPLY, prompts)
        adopted = DRAFT["sections"][1]["text"]
        picked = {"version": 2, "sectionIndex": 1, "start": 0, "end": len(adopted), "text": adopted}
        ok(http.post(f"/api/conversations/{thread}/questions", json={
            "id": str(uuid4()), "text": "这一步也要查证吗？", "selection": picked,
        }))
        assert ask_one(tmp_path, PASS_REPLY, prompts)
        context = json.loads(prompts[-1].split("\n答疑上下文 JSON\n")[1])
        assert context["selection"] == {**picked, "state": "disputed"}
        assert [part["index"] for part in context["sections"]] == [0]
        assert context["previous"] == {"question": "怎么固定活字？", "answer": PASS_REPLY["answer"]}
        assert "第一轮私下示例" not in prompts[-1] and "另一个对话的问题" not in prompts[-1]


def test_pause_revoke_and_shared_budget_preserve_one_question_without_a_hidden_call(tmp_path):
    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        thread = open_thread(http, lesson)
        permit_assistant(http, lesson)
        record = {"id": str(uuid4()), "text": "回答会等待项目恢复吗？", "selection": selection()}
        endpoint = f"/api/conversations/{thread}"
        retry = f"/api/lesson-questions/{record['id']}/retry"
        project = ok(http.get(f"/api/lessons/{lesson}"))["project"]["id"]
        pause = f"/api/projects/{project}/pause"
        ok(http.put(pause, json={"paused": True}))
        assert ok(http.post(endpoint + "/questions", json=record))["messages"][-1]["answer"]["state"] == "paused"
        assert not ask_one(tmp_path, PASS_REPLY, prompts)
        ok(http.put(pause, json={"paused": False}))
        engine = open_db(tmp_path)
        try:
            claim = reserve_answer(engine, utc_now())
            assert claim and claim.question_id == record["id"]
            ok(http.post(f"/api/lessons/{lesson}/assistant/revoke"))
            dispatch_answer(engine, claim, transport=assistant_transport(PASS_REPLY, prompts))
            failed = ok(http.get(endpoint))["messages"][-1]["answer"]
            assert failed["state"] == "blocked" and "撤销" in failed["error"]
            assert not prompts and http.post(retry).status_code == 409
        finally:
            engine.dispose()
        permit_assistant(http, lesson)
        ok(http.put("/api/generation/settings", json={"concurrency": 2, "dailyLimit": 1}))
        ok(http.post(retry))
        assert not ask_one(tmp_path, PASS_REPLY, prompts)
        assert "每日" in ok(http.get(endpoint))["messages"][-1]["answer"]["error"]
        ok(http.put("/api/generation/settings", json={"concurrency": 2, "dailyLimit": 7}))
        ok(http.post(retry))
        assert ask_one(tmp_path, PASS_REPLY, prompts)
        assert len(prompts) == 1 and len(ok(http.get(endpoint))["messages"]) == 2


@pytest.mark.parametrize("sending", [False, True], ids=["before-send", "after-admission"])
def test_source_changes_and_interrupted_dispatch_are_recoverable_from_the_saved_question(tmp_path, sending):
    prompts = []
    with session(tmp_path) as http:
        lesson, source = source_lesson(http, tmp_path)
        thread = open_thread(http, lesson)
        permit_assistant(http, lesson)
        record = {"id": str(uuid4()), "text": "这一段有什么出处？", "selection": selection()}
        endpoint = f"/api/conversations/{thread}"
        retry = f"/api/lesson-questions/{record['id']}/retry"
        ok(http.post(endpoint + "/questions", json=record))
        original = tmp_path / source["path"]
        saved = original.read_bytes()
        original.write_text("这份资料被编辑过。", encoding="utf-8")
        assert ask_one(tmp_path, PASS_REPLY, prompts) and not prompts
        assert "引用原件" in ok(http.get(endpoint))["messages"][-1]["answer"]["error"]
        original.write_bytes(saved)
        ok(http.post(retry))
        engine = open_db(tmp_path)
        try:
            claim = reserve_answer(engine, utc_now())
            assert claim and claim.question_id == record["id"]
            if sending:
                admit_for_interruption(engine, claim.id, claim.prompt, utc_now())
            settle_interrupted_answers(engine, utc_now())
            assert_interrupted_use(engine, claim.id, sending)
            dispatch_answer(engine, claim, transport=assistant_transport(PASS_REPLY, prompts))
            assert not prompts
            state = ok(http.get(endpoint))["messages"][-1]["answer"]["state"]
            assert state == ("blocked" if sending else "queued")
        finally:
            engine.dispose()
        if sending:
            assert not ask_one(tmp_path, PASS_REPLY, prompts)
            ok(http.post(retry))
        assert ask_one(tmp_path, PASS_REPLY, prompts)
        assert len(prompts) == 1 and ok(http.get(endpoint))["messages"][-1]["answer"]["state"] == "answered"
