from datetime import datetime

from test_backups import accepted, inspect_backup, make_backup
from test_demo import first_lesson
from test_model_assessments import COMPOSED, GRADE, GROUP, assess_one, permit, review, source_lesson
from test_model_study import ok, session
from test_study_report import local_app, report


def open_weak(http, folder):
    lesson = first_lesson(http, folder)
    accepted(http.put(lesson + "/learned", json={"learned": True}))
    accepted(http.post("/api/demo/next-day"))
    target = lesson + "/reviews/rows-columns"
    accepted(http.post(target + "/submit", json={"round": 1, "answers": [], "action": "unknown"}))
    return target


def test_local_weak_practice_uses_a_separate_bank_and_preserves_formal_progress(tmp_path):
    clock = lambda: datetime(2027, 2, 1, 8)
    with local_app(tmp_path, clock) as http:
        target = open_weak(http, tmp_path)
        before = ok(http.get(target))
        history = report(http, period="week")
        [weak] = history["weak"]
        assert weak["sourceRound"] == 1 and weak["groupId"] is None
        path = target + "/practice/1"
        exercise = ok(http.get(path))
        assert exercise["state"] == "ready" and exercise["result"] is None
        assert exercise["explanation"] == before["latest"]["explanation"]
        assert all("correct" not in question for question in exercise["questions"])
        assert not {q["text"] for q in exercise["questions"]} & {q["text"] for q in before["questions"]}
        submitted = {"basis": exercise["basis"], "answers": [1, 2]}
        assert http.post(path, json={**submitted, "basis": "0" * 64}).status_code == 409
        assert http.post(path, json={**submitted, "answers": [3, 2]}).status_code == 422
        completed = ok(http.post(path, json=submitted))
        assert completed["state"] == "completed" and completed["result"]["correctCount"] == 2
        assert ok(http.post(path, json=submitted)) == completed
        assert http.post(path, json={**submitted, "answers": [0, 0]}).status_code == 409
        assert ok(http.get(target)) == before
        assert report(http, period="week")["totals"]["review"] == history["totals"]["review"]
        assert report(http, period="week")["weak"] == history["weak"]
        backup = make_backup(http)
        assert accepted(inspect_backup(http, backup))["counts"]["backup"]["local_practice_attempts"] == 1
    with local_app(tmp_path, clock) as http:
        assert ok(http.get(path)) == completed
        assert ok(http.get(target)) == before


def test_model_weak_entry_reads_formal_evidence_and_queues_only_practice(tmp_path):
    calls = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, calls)
        item = review(http, lesson)
        path = f"/api/lessons/{lesson}/assessments/{item['key']}"
        ok(http.post("/api/demo/next-day"))
        formal = ok(http.get(path))["group"]
        failed = ok(http.post(f"/api/assessments/{formal['id']}/submit", json={"action": "unknown", "answers": []}))
        assert failed["result"]["outcome"] == "failed"
        before = review(http, lesson)
        before_report = report(http, "week")
        [weak] = before_report["weak"]
        assert weak["groupId"] == formal["id"] and weak["sourceRound"] == 1
        detail = ok(http.get(path, params={"focus": formal["id"]}))
        assert detail["focus"]["id"] == formal["id"] and detail["group"] is None
        assert http.post(path + "/prepare", json={"focus": formal["id"], "mode": "formal"}).status_code == 422
        request = {"focus": formal["id"], "mode": "practice"}
        practical = ok(http.post(path + "/prepare", json=request))
        assert practical["mode"] == "practice"
        assert ok(http.post(path + "/prepare", json=request))["id"] == practical["id"]
        assert ok(http.get(path, params={"focus": formal["id"]}))["group"]["id"] == practical["id"]
        group = {**GROUP, "questions": [{**GROUP["questions"][0], "text": "如何检查一次压印的固定效果？"}]}
        assert assess_one(tmp_path, group, calls)
        ok(http.post(f"/api/assessments/{practical['id']}/submit", json={"action": "submit", "answers": ["用版框固定。"]}))
        grade = {**GRADE, "items": GRADE["items"][:1]}
        assert assess_one(tmp_path, grade, calls)
        assert ok(http.get(f"/api/assessments/{practical['id']}"))["result"]["outcome"] == "passed"
        assert review(http, lesson) == before
        assert report(http, "week")["totals"]["review"] == before_report["totals"]["review"]

def test_local_focus_can_review_old_explanation_when_project_pauses(tmp_path):
    from test_demo import GOAL

    with local_app(tmp_path, lambda: datetime(2027, 2, 1, 8)) as http:
        path = open_weak(http, tmp_path) + "/practice/1"
        exercise = ok(http.get(path))
        payload = {"basis": exercise["basis"], "answers": [1, 2]}
        paused = f"/api/projects/{GOAL['id']}/pause"
        ok(http.put(paused, json={"paused": True}))
        read_only = ok(http.get(path))
        assert read_only["state"] == "read_only" and read_only["explanation"] == exercise["explanation"]
        assert read_only["questions"] == [] and read_only["basis"] is None
        assert http.post(path, json=payload).status_code == 409
        ok(http.put(paused, json={"paused": False}))
        assert ok(http.post(path, json=payload))["state"] == "completed"


def test_model_focus_rejects_unrelated_source_and_respects_revoked_authorization(tmp_path):
    from uuid import uuid4

    calls = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path)
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, calls)
        target = review(http, lesson)
        path = f"/api/lessons/{lesson}/assessments/{target['key']}"
        ok(http.post("/api/demo/next-day"))
        source = ok(http.get(path))["group"]
        ok(http.post(f"/api/assessments/{source['id']}/submit", json={"action": "unknown", "answers": []}))
        assert http.get(path, params={"focus": str(uuid4())}).status_code == 409
        ok(http.post(f"/api/lessons/{lesson}/assessment/revoke"))
        preview = ok(http.get(path, params={"focus": source["id"]}))
        assert preview["focus"]["id"] == source["id"]
        assert http.post(path + "/prepare", json={"focus": source["id"], "mode": "practice"}).status_code == 409
        assert not assess_one(tmp_path, GROUP, calls)
