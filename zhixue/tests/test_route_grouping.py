from hmac import digest
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.db import Review, StageReviewCall, open_db, transaction
from backend.model_usage import billed_calls, billing_day
from backend.stage_review_worker import dispatch, reserve
from test_demo import GOAL, client, first_lesson  # noqa: F401
from test_stage_assessments import complete, quiz_submission, stage_path, tasks
from test_stage_model_review import BEFORE, SELECTION, VALID, approved, evidence_attempt, open_client, reply_with, setup_models


PROJECT = f"/api/projects/{GOAL['id']}"
ORDER = f"{PROJECT}/route-order"


def ok(response):
    assert response.is_success, response.text
    return response.json()


def project_lessons(http):
    return ok(http.get(PROJECT))["lessons"]


def place(current, lesson, stage, branch):
    return {
        "baseVersion": current,
        "order": [item["id"] for item in lesson],
        "placements": {lesson[0]["id"]: {"stage": stage, "branch": branch}},
    }


def confirm(http, change, preview=None):
    if preview is None:
        preview = ok(http.post(f"{ORDER}/preview", json=change))
    return ok(http.post(f"{ORDER}/confirm", json={**change, "confirmation": preview["confirmation"]}))


def review_schedule(folder):
    engine = open_db(folder)
    try:
        with transaction(engine, write=False) as db:
            return [
                (row.lesson_id, row.key, row.stage, row.due_on, row.round_number, row.answers, row.hinted)
                for row in db.scalars(select(Review).order_by(Review.lesson_id, Review.key))
            ]
    finally:
        engine.dispose()


def test_stage_change_keeps_reading_note_and_old_attempt_and_creates_new_scope(client):
    http, folder = client
    lesson_url = first_lesson(http, folder)
    first, second, third = project_lessons(http)
    complete(http, lesson_url)
    note = "# 我的记录\n\n解释表、行和列。"
    ok(http.put(f"{lesson_url}/note", json={"content": note}))
    reading = ok(http.get(lesson_url))
    schedule = review_schedule(folder)
    initial, = tasks(http)
    old_url = f"{stage_path()}/{initial['id']}"
    old_detail = ok(http.post(f"{old_url}/attempts", json=quiz_submission(initial, [2, 0])))

    change = place(1, [first, second, third], {"anchorId": second["id"]}, "重点练习")
    preview = ok(http.post(f"{ORDER}/preview", json=change))
    assert preview["stageImpact"] == {
        "before": [
            {"phase": first["phase"], "lessons": [first["title"]]},
            {"phase": second["phase"], "lessons": [second["title"]]},
        ],
        "after": [{"phase": second["phase"], "lessons": [first["title"], second["title"]]}],
    }
    assert [node["phase"] for node in project_lessons(http)] == [first["phase"], second["phase"], third["phase"]]
    assert tasks(http)[0]["eligible"]
    tampered = place(1, [first, second, third], {"anchorId": second["id"]}, "替换后的文本")
    assert http.post(f"{ORDER}/confirm", json={**tampered, "confirmation": preview["confirmation"]}).status_code == 409

    saved = confirm(http, change, preview)
    assert saved["version"] == 2
    assert confirm(http, change, preview) == saved
    assert http.post(f"{ORDER}/preview", json=change).status_code == 409
    assert [node["id"] for node in saved["nodes"]] == change["order"]
    refreshed = ok(http.get(PROJECT))
    moved = refreshed["lessons"][0]
    assert refreshed["routeVersion"] == 2
    assert moved["phase"] == second["phase"]
    assert moved["branch"] == "重点练习" and moved["ready"] and moved["learnedOn"]
    preserved = ok(http.get(lesson_url))
    assert preserved.pop("project") == refreshed
    reading.pop("project")
    assert preserved == reading
    assert review_schedule(folder) == schedule
    historical, = tasks(http)
    assert historical["id"] == initial["id"] and historical["currentScope"] is False
    detail = ok(http.get(old_url))
    assert detail["eligible"] is False and detail["attempts"] == old_detail["attempts"]
    assert http.post(f"{old_url}/attempts", json=quiz_submission(detail, [1, 2])).status_code == 409

    complete(http, f"/api/lessons/{second['id']}")
    current, = [item for item in tasks(http) if item["currentScope"]]
    assert current["routeVersion"] == 2 and current["eligible"]
    current_scope = ok(http.get(f"{stage_path()}/{current['id']}"))
    assert {item["lessonId"] for item in current_scope["criteria"]} == {first["id"], second["id"]}

    branch_only = place(2, project_lessons(http), {"anchorId": second["id"]}, None)
    preview = ok(http.post(f"{ORDER}/preview", json=branch_only))
    assert "stageImpact" not in preview
    confirm(http, branch_only, preview)
    assert project_lessons(http)[0]["branch"] is None
    assert [item["id"] for item in tasks(http) if item["currentScope"]] == [current["id"]]


def test_reuse_new_stage_and_inherit_edited_practice_location(client):
    http, _ = client
    ok(http.post("/api/projects", json=GOAL))
    first, second, third = project_lessons(http)
    change = place(1, [first, second, third], {"name": "  联合训练  "}, "动手应用")
    change["placements"][third["id"]] = {"stage": {"name": "联合训练"}, "branch": None}
    change["placements"][second["id"]] = {"stage": {"anchorId": first["id"]}, "branch": None}
    change["practice"] = {
        "afterId": first["id"], "title": "交付实践", "outcome": "交付一张汇总表",
        "instructions": "按主题核对并汇总五条记录。", "required": False,
    }
    preview = ok(http.post(f"{ORDER}/preview", json=change))
    moved, practice, reused, moved_later = preview["after"]
    assert [node["id"] for node in (moved, reused, moved_later)] == change["order"]
    assert moved["stageId"] == moved_later["stageId"] == practice["stageId"]
    assert practice["branch"] == moved["branch"] == "动手应用"
    assert practice["phase"] == moved["phase"] == moved_later["phase"] == "联合训练"
    assert reused["phase"] == first["phase"]
    assert confirm(http, change, preview)["nodes"] == preview["after"]

    lessons = project_lessons(http)
    for invalid_stage in ({"anchorId": str(uuid4())}, {"name": "联合训练"}, {"name": "阶段\nB"}):
        invalid = place(2, lessons, invalid_stage, None)
        assert http.post(f"{ORDER}/preview", json=invalid).status_code == 422
    noop = place(2, lessons, {"anchorId": moved["id"]}, "动手应用")
    assert http.post(f"{ORDER}/preview", json=noop).status_code == 422


def test_board_and_continuation_display_unlearned_course_in_its_current_stage(client):
    http, folder = client
    first_lesson(http, folder)
    first, second, third = project_lessons(http)
    change = place(1, [first, second, third], {"name": "组合训练"}, "应用")
    confirm(http, change)

    board = ok(http.post("/api/project-board/query", json={"view": {}}))
    displayed, = board["pages"]["active"]["items"]
    expected = {"id": first["id"], "title": first["title"], "phase": "组合训练", "ready": True}
    assert displayed["nextLesson"] == expected
    assert ok(http.get("/api/state"))["continuation"]["nextLesson"] == expected


def test_order_confirmation_remains_compatible_with_existing_requests(client):
    http, _ = client
    ok(http.post("/api/projects", json=GOAL))
    change = {"baseVersion": 1, "order": [lesson["id"] for lesson in reversed(project_lessons(http))]}
    old_material = json.dumps(
        ["route-edit", GOAL["id"], {**change, "marks": {}, "practice": None}],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    preview = ok(http.post(f"{ORDER}/preview", json=change))
    assert preview["confirmation"] == digest(b"demo-test-session", old_material, "sha256").hex()
    assert "stageImpact" not in preview
    assert confirm(http, change, preview)["version"] == 2


@pytest.mark.parametrize("sent", [False, True])
def test_stage_change_releases_unsent_reviews_and_holds_in_flight_results(tmp_path, sent):
    clock = lambda: BEFORE
    with open_client(tmp_path, clock) as http:
        setup_models(http)
        stage, review_url = evidence_attempt(http, tmp_path)
        _, auth = approved(http, review_url)
        ok(http.post(review_url, json=auth))
        engine = open_db(tmp_path)
        try:
            claim = reserve(engine, BEFORE)
            assert claim
            lessons = project_lessons(http)
            branch = place(1, lessons, {"anchorId": lessons[0]["id"]}, "已保存分支")
            assert "stageImpact" not in ok(http.post(f"{ORDER}/preview", json=branch))
            assert confirm(http, branch)["version"] == 2
            with transaction(engine, write=False) as db:
                assert db.get(StageReviewCall, claim.id).status == "reserved"

            shifted = place(2, lessons, {"anchorId": lessons[1]["id"]}, None)
            requests = []

            def regroup():
                assert confirm(http, shifted)["version"] == 3

            if not sent:
                regroup()
            dispatch(
                engine, claim,
                transport=reply_with(VALID, requests, on_send=regroup if sent else None), now=clock,
            )
            history = ok(http.get(stage))
            assert history["currentScope"] is False and history["eligible"] is False
            attempt = history["attempts"][0]
            job = attempt["modelReview"]
            assert job["state"] == ("held" if sent else "stopped")
            assert attempt["outcome"] == "awaiting_review" and not attempt["findings"]
            if sent:
                assert job["heldReport"]["findings"][0]["citations"][0]["quote"] == "id INTEGER PRIMARY KEY"
                assert job["scope"]["excerpts"][0]["firstLine"] == 1
                assert http.post(f"{review_url}/apply").status_code == 409
            else:
                assert job["heldReport"] is None
            assert http.post(f"{review_url}/preview", json=SELECTION).status_code == 409
            with transaction(engine, write=False) as db:
                assert billed_calls(db, billing_day(BEFORE)) == int(sent)
                call = db.get(StageReviewCall, claim.id)
                assert (call.status == "not_sent") == (not sent)
            assert len(requests) == int(sent)
        finally:
            engine.dispose()
