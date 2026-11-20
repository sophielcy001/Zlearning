from datetime import date
from io import BytesIO
from uuid import uuid4

from docx import Document
import pymupdf
from sqlalchemy import select

from backend.db import Lesson, Review, RouteVersion, StageAssessment, open_db, transaction
from backend.worker import prepare_one
from test_demo import GOAL, client, first_lesson  # noqa: F401


def stage_path():
    return f"/api/projects/{GOAL['id']}/stage-assessments"


def tasks(http):
    response = http.get(stage_path())
    assert response.status_code == 200, response.text
    return response.json()["items"]


def complete(http, lesson):
    assert http.put(f"{lesson}/learned", json={"learned": True}).status_code == 200


def quiz_submission(stage, answers):
    return {
        "id": str(uuid4()), "kind": "quiz", "expectedAttempt": stage["attemptCount"], "answers": answers,
    }


def test_stage_is_created_by_course_completion_and_can_be_skipped_and_retaken(client):
    http, folder = client
    lesson = first_lesson(http, folder)
    assert tasks(http) == []
    complete(http, lesson)
    identity = lesson.rsplit("/", 1)[-1]
    with transaction(open_db(folder), write=False) as db:
        assert db.scalar(select(StageAssessment)).criteria[0]["lessonId"] == identity
        review_before = [(item.stage, item.due_on) for item in db.scalars(select(Review)).all()]

    stage, = tasks(http)
    assert stage["status"] == "awaiting"
    assert stage["routeVersion"] == 1
    path = f"{stage_path()}/{stage['id']}"
    detail = http.get(path).json()
    assert len(detail["quiz"]) == 2
    assert all("correct" not in question for question in detail["quiz"])
    skipped = http.put(f"{path}/skip", json={"skipped": True, "expectedRevision": detail["revision"]}).json()
    assert skipped["status"] == "skipped"

    saved = http.get(f"/api/projects/{GOAL['id']}").json()
    assert saved["status"] == "active" and saved["lessons"][1]["ready"]
    assert http.put(f"{path}/skip", json={"skipped": False, "expectedRevision": skipped["revision"]}).json()["status"] == "awaiting"
    submitted = quiz_submission(stage, [2, 0])
    receipt = http.post(f"{path}/attempts", json=submitted)
    assert receipt.status_code == 201, receipt.text
    failed = receipt.json()
    assert failed["status"] == "needs_work"
    assert failed["attempts"][0]["findings"][0]["lessonId"] == identity
    assert http.post(f"{path}/attempts", json=submitted).json()["attemptCount"] == 1
    assert http.post(f"{path}/attempts", json=quiz_submission(stage, [0, 1])).status_code == 409
    with transaction(open_db(folder), write=False) as db:
        assert [(item.stage, item.due_on) for item in db.scalars(select(Review)).all()] == review_before

    assert http.put(f"{lesson}/learned", json={"learned": False}).status_code == 200
    assert http.get(path).json()["eligible"] is False
    assert http.post(f"{path}/attempts", json=quiz_submission(failed, [1, 2])).status_code == 409
    complete(http, lesson)
    second = http.get(path).json()
    assert second["quiz"][0]["prompt"] != detail["quiz"][0]["prompt"]
    retake = http.post(f"{path}/attempts", json=quiz_submission(second, [1, 2]))
    assert retake.status_code == 201, retake.text
    assert retake.json()["status"] == "passed"
    assert [attempt["outcome"] for attempt in retake.json()["attempts"]] == ["passed", "needs_work"]
    assert len(tasks(http)) == 1


def test_rubric_evidence_is_retained_and_requires_matching_citations(client):
    http, folder = client
    data = {**GOAL, "topic": "观察方法"}
    response = http.post("/api/projects", json=data)
    assert response.status_code == 201, response.text
    while prepare_one(folder):
        pass
    lesson = response.json()["lessons"][0]["id"]
    complete(http, f"/api/lessons/{lesson}")
    stage, = tasks(http)
    assert not stage["hasQuiz"]
    path = f"{stage_path()}/{stage['id']}"
    payload = {
        "id": str(uuid4()), "kind": "evidence", "expectedAttempt": 0,
        "evidence": [
            {"name": "lab/run.py", "kind": "project", "text": "print('sample result')"},
            {"name": "报告.md", "kind": "report", "text": "我预测结果为 5，并用三次记录比对。"},
        ],
    }
    response = http.post(f"{path}/attempts", json=payload)
    assert response.status_code == 201, response.text
    stage = response.json()
    assert stage["status"] == "awaiting_review"
    attempt, = stage["attempts"]
    assert attempt["scope"] == "static"
    assert attempt["evidence"][0]["text"] == "print('sample result')"
    review_url = f"{path}/attempts/{attempt['id']}/review"
    finding = {"lessonId": lesson, "verdict": "met", "note": "原文有明确的预测和比对说明。",
               "citation": {"evidenceIndex": 1, "quote": "模型实际执行并通过"}}
    assert http.put(review_url, json={"findings": [finding]}).status_code == 422
    finding["citation"]["quote"] = "我预测结果为 5"
    receipt = http.put(review_url, json={"findings": [finding]})
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["status"] == "passed"
    assert receipt.json()["attempts"][0]["reviewer"] == "self"
    finding["verdict"] = "needs_work"
    assert http.put(review_url, json={"findings": [finding]}).status_code == 409


def test_existing_progress_backfills_and_reorder_does_not_duplicate_an_assessment(client):
    http, folder = client
    lesson = first_lesson(http, folder)
    identity = lesson.rsplit("/", 1)[-1]
    with transaction(open_db(folder)) as db:
        db.get(Lesson, identity).learned_on = date(2027, 1, 30)
    first, = tasks(http)
    with transaction(open_db(folder)) as db:
        route = db.get(RouteVersion, (GOAL["id"], 1))
        db.add(RouteVersion(project_id=route.project_id, version=2, goal_version=1, created_at=route.created_at,
                            definition={**route.definition, "nodes": list(reversed(route.definition["nodes"]))}))
    reloaded, = tasks(http)
    assert reloaded["id"] == first["id"]
    assert reloaded["routeVersion"] == 1


def test_stage_waits_for_every_required_lesson_but_handles_optional_branches(client):
    http, folder = client
    first = first_lesson(http, folder)
    with transaction(open_db(folder)) as db:
        route = db.get(RouteVersion, (GOAL["id"], 1))
        original = route.definition["nodes"]
        core = [{**node, "phase": "共同阶段", "stageId": "stage-1"} for node in original[:2]]
        optional = {**original[2], "phase": "共同阶段", "stageId": "stage-1", "required": False}
        route.definition = {**route.definition, "nodes": [*core, optional]}
        second = core[1]["id"]
        optional_id = optional["id"]
    complete(http, first)
    assert tasks(http) == []
    complete(http, f"/api/lessons/{second}")
    stage, = tasks(http)
    detail = http.get(f"{stage_path()}/{stage['id']}").json()
    assert [item["lessonId"] for item in detail["criteria"]] == [first.rsplit("/", 1)[-1], second]
    assert len(detail["quiz"]) == 4
    complete(http, f"/api/lessons/{optional_id}")
    assert [item["id"] for item in tasks(http)] == [stage["id"]]


def test_preview_preserves_readable_report_scope_and_rejects_invalid_material(client):
    http, folder = client
    complete(http, first_lesson(http, folder))
    stage, = tasks(http)
    url = f"{stage_path()}/{stage['id']}/evidence-preview"

    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 70), "Measured 15 minutes.")
        pdf.new_page()
        response = http.post(url, params={"name": "Experiment.pdf"}, content=pdf.tobytes())
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "Measured 15 minutes."
    assert "部分页面" in response.json()["coverage"]

    word = Document()
    word.add_paragraph("观察记录")
    contents = BytesIO()
    word.save(contents)
    response = http.post(url, params={"name": "实验报告.docx"}, content=contents.getvalue())
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "观察记录"
    assert "段落和表格" in response.json()["coverage"]

    assert http.post(url, params={"name": "binary.bin"}, content=b"\xff\x00").status_code == 422
    assert http.post(url, params={"name": "../main.py"}, content=b"print(1)").status_code == 422
    assert http.post(url, params={"name": "large.txt"}, content=b"x" * 250_001).status_code == 413
    assert http.post(url, params={"name": "false.pdf"}, content=b"not a PDF").status_code == 422
