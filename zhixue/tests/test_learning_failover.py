from uuid import uuid4

from sqlalchemy import select

from backend import assessment_worker, lesson_answer_worker, prerequisite_worker, study_consolidation_worker
from backend.db import ModelUse, PrerequisiteCall, open_db, transaction
from test_backups import create_project
from test_lesson_assistant import PASS_REPLY, open_thread
from test_model_assessments import COMPOSED, GRADE, review, source_lesson
from test_model_failover import connection
from test_model_study import ok, session
from test_prerequisites import DRAFT as QUICK_DRAFT, VERIFIED as QUICK_REVIEW, input_source, models as quick_models
from test_review_remediation import CHECKED, DRAFT as REMEDIATION, publish_card
from test_stage_model_review import reply_with
from test_study_consolidation import ACCEPT, SUPPLEMENT, ask_grounded, completed_course


def alternates(http):
    author, reviewer = connection(http, "learning-main-standby"), connection(http, "learning-review-standby")
    ok(http.patch("/api/models/roles", json={"mainBackup": author["id"], "reviewBackup": reviewer["id"]}))
    return author, reviewer


def consent(http, path, options):
    preview = ok(http.post(path + "/preview", json=options))
    assert preview["backups"]
    ok(http.post(path + "/authorize", json={**options, "confirmation": preview["confirmation"]}))
    return preview


def run(folder, worker, result, seen, status=200):
    engine = open_db(folder)
    try:
        return worker.process_one(engine, transport=reply_with(result, seen, status=status))
    finally:
        engine.dispose()


def assert_same_scope(calls, first, alternate):
    assert [value[1]["model"] for value in calls] == [first, alternate]
    assert calls[0][1]["messages"] == calls[1][1]["messages"]


def test_formal_review_grading_and_remediation_switch_in_place_with_both_roles(tmp_path):
    with session(tmp_path) as http:
        lesson, evidence = source_lesson(http, tmp_path)
        first, backup = alternates(http)
        path = f"/api/lessons/{lesson}/assessment"
        consent(http, path, {"allowMainBackup": True, "allowReviewBackup": True, "maxCalls": 10, "remediation": True})
        wire = []
        for output, kind in ((COMPOSED, "objectives"), (GRADE, "grade"), (REMEDIATION, "remediation"), (CHECKED, "remediation_check")):
            if kind == "grade":
                scheduled = review(http, lesson)
                ok(http.post("/api/demo/next-day"))
                group = ok(http.get(f"/api/lessons/{lesson}/assessments/{scheduled['key']}"))["group"]
                saved = f"/api/assessments/{group['id']}"
                answers = ["放进版框。正确小题私有注释", "检查纸张尺寸。"]
                ok(http.post(saved + "/submit", json={"answers": answers, "action": "submit"}))
            start = len(wire)
            assert run(tmp_path, assessment_worker, output, wire, status=503 if kind == "objectives" else 403)
            if kind == "grade":
                assert ok(http.get(saved))["state"] == "grading"
                assert review(http, lesson)["dueOn"] == scheduled["dueOn"]
            assert run(tmp_path, assessment_worker, output, wire)
            assert_same_scope(wire[start:], "independent-model" if kind == "remediation_check" else "isolated-model",
                              backup["model"] if kind == "remediation_check" else first["model"])
        assert publish_card(tmp_path)
        card, = ok(http.get(f"/api/lessons/{lesson}/remediation"))["cards"]
        assert card["citations"][0]["fileId"] == evidence["id"]
        assert "正确小题私有注释" not in wire[5][1]["messages"][0]["content"]
        assert ok(http.post(saved + "/recheck"))["state"] == "rechecking"
        assert run(tmp_path, assessment_worker, GRADE, wire, 403)
        assert run(tmp_path, assessment_worker, GRADE, wire)
        assert_same_scope(wire[8:], "independent-model", backup["model"])
        result = ok(http.get(saved))
        assert result["answers"] == answers and len(result["history"]) == 2
        assert ok(http.get(path))["calls"] == 10
        assert not run(tmp_path, assessment_worker, GRADE, wire)


def test_lesson_answer_backup_expires_on_replacement_and_retry_keeps_the_grounded_question(tmp_path):
    with session(tmp_path) as http:
        lesson, _ = completed_course(http, tmp_path)
        first, _ = alternates(http)
        path = f"/api/lessons/{lesson}/assistant"
        options = {"allowMainBackup": True, "maxCalls": 3}
        preview = ok(http.post(path + "/preview", json=options))
        assert preview["backups"]["main"]["id"] == first["id"]
        changed = connection(http, "new-answer-standby")
        ok(http.patch("/api/models/roles", json={"mainBackup": changed["id"]}))
        assert http.post(path + "/authorize", json={**options, "confirmation": preview["confirmation"]}).status_code == 409
        ok(http.patch("/api/models/roles", json={"mainBackup": first["id"]}))
        ok(http.post(path + "/authorize", json={**options, "confirmation": preview["confirmation"]}))
        thread = open_thread(http, lesson)
        question = {"id": str(uuid4()), "text": "为何要固定排完的活字？"}
        queued = ok(http.post(f"/api/conversations/{thread}/questions", json=question))
        assert queued["messages"][-1]["answer"]["state"] == "queued"
        seen = []
        assert run(tmp_path, lesson_answer_worker, PASS_REPLY, seen, 403)
    with session(tmp_path) as http:
        assert run(tmp_path, lesson_answer_worker, PASS_REPLY, seen)
        assert_same_scope(seen, "isolated-model", first["model"])
        answer = ok(http.get(f"/api/conversations/{thread}"))["messages"][-1]["answer"]
        assert answer["state"] == "answered" and answer["recipient"]["id"] == first["id"]
        assert ok(http.get(path))["calls"] == 2


def test_consolidation_switch_preserves_draft_and_uses_an_independent_backup_after_author_collision(tmp_path):
    with session(tmp_path) as http:
        lesson, _ = completed_course(http, tmp_path)
        ask_grounded(http, tmp_path, lesson, "为什么试印前先检查字行？")
        author, verifier = alternates(http)
        ok(http.patch("/api/models/roles", json={"review": author["id"]}))
        path = f"/api/lessons/{lesson}/consolidation"
        consent(http, path, {"allowMainBackup": True, "allowReviewBackup": True, "maxCalls": 4})
        ok(http.put(f"/api/lessons/{lesson}/learned", json={"learned": True}))
        seen = []
        assert run(tmp_path, study_consolidation_worker, SUPPLEMENT, seen, 503)
        assert run(tmp_path, study_consolidation_worker, SUPPLEMENT, seen)
        assert_same_scope(seen, "isolated-model", author["model"])
        engine = open_db(tmp_path)
        try:
            assert not study_consolidation_worker.publish_one(engine, tmp_path)
            assert run(tmp_path, study_consolidation_worker, ACCEPT, seen)
            assert seen[-1][1]["model"] == verifier["model"]
            assert study_consolidation_worker.publish_one(engine, tmp_path)
            job, = ok(http.get(path))["jobs"]
            assert job["reviewer"]["id"] == verifier["id"] and job["author"]["id"] == author["id"]
            with transaction(engine, write=False) as db:
                record = db.scalar(select(ModelUse).where(ModelUse.failover_reason == "review_independence"))
                assert record.connection["model"] == verifier["model"]
        finally:
            engine.dispose()


def test_prerequisite_stops_at_call_limit_and_reuses_the_saved_draft_when_budget_is_increased(tmp_path):
    with session(tmp_path) as http:
        quick_models(http)
        author, verifier = alternates(http)
        data = create_project(http, tmp_path)
        lesson = data["lessons"][0]["id"]
        path = f"/api/lessons/{lesson}/prerequisites"
        options = {"topic": "准备语句绑定", "sources": [input_source(http)], "allowMainBackup": True,
                   "allowReviewBackup": True, "maxCalls": 3}
        scope = ok(http.post(path + "/preview", json=options))
        job = ok(http.post(path + "/generate", json={**options, "confirmation": scope["confirmation"]}))["job"]
        seen = []
        assert run(tmp_path, prerequisite_worker, QUICK_DRAFT, seen, 403)
        assert run(tmp_path, prerequisite_worker, QUICK_DRAFT, seen)
        assert run(tmp_path, prerequisite_worker, QUICK_REVIEW, seen, 503)
        assert not run(tmp_path, prerequisite_worker, QUICK_REVIEW, seen)
        current = ok(http.get(path, params={"topic": options["topic"]}))["job"]
        assert current["reason"] == "task_budget" and current["draft"] == QUICK_DRAFT["sections"]
        resume = {**options, "maxCalls": 4, "resumeJobId": job["id"]}
        preview = ok(http.post(path + "/preview", json=resume))
        ok(http.post(path + "/generate", json={**resume, "confirmation": preview["confirmation"]}))
        assert run(tmp_path, prerequisite_worker, QUICK_REVIEW, seen)
        assert_same_scope(seen[:2], "isolated-model", author["model"])
        assert_same_scope(seen[2:], "prerequisite-review", verifier["model"])
        assert all("从未授权的私有附注" not in req[1]["messages"][0]["content"] for req in seen)
        engine = open_db(tmp_path)
        try:
            assert prerequisite_worker.publish_one(engine, tmp_path)
            assert ok(http.get(path, params={"topic": options["topic"]}))["kind"] == "explanation"
            with transaction(engine, write=False) as db:
                calls = list(db.scalars(select(PrerequisiteCall)))
                assert len(calls) == 4 and sum(call.auto_retry for call in calls) == 2
        finally:
            engine.dispose()


def test_saved_answer_never_sends_a_reserved_backup_after_the_backup_role_changes(tmp_path):
    with session(tmp_path) as http:
        lesson, _ = completed_course(http, tmp_path)
        first, _ = alternates(http)
        path = f"/api/lessons/{lesson}/assistant"
        consent(http, path, {"allowMainBackup": True})
        thread = open_thread(http, lesson)
        identity = str(uuid4())
        ok(http.post(f"/api/conversations/{thread}/questions", json={"id": identity, "text": "如何固定活字？"}))
        seen = []
        assert run(tmp_path, lesson_answer_worker, PASS_REPLY, seen, 403)
        engine = open_db(tmp_path)
        try:
            from backend.model_study import utc_now
            claim = lesson_answer_worker.reserve_answer(engine, utc_now())
            assert claim.model == first["model"]
            replacement = connection(http, "late-standby")
            ok(http.patch("/api/models/roles", json={"mainBackup": replacement["id"]}))
            lesson_answer_worker.dispatch_answer(engine, claim, transport=reply_with(PASS_REPLY, seen))
            assert len(seen) == 1
            answer = ok(http.get(f"/api/conversations/{thread}"))["messages"][-1]["answer"]
            assert answer["state"] == "blocked"
            with transaction(engine, write=False) as db:
                assert db.get(ModelUse, claim.id).status == "not_sent"
            ok(http.patch("/api/models/roles", json={"mainBackup": first["id"]}))
            ok(http.post(f"/api/lesson-questions/{identity}/retry"))
            assert run(tmp_path, lesson_answer_worker, PASS_REPLY, seen)
            assert_same_scope(seen, "isolated-model", first["model"])
        finally:
            engine.dispose()
