from contextlib import closing
import json
import sqlite3

from test_course_verification import DRAFT, SOURCE, authorize, model_traffic, prepare, publish
from test_model_assessments import COMPOSED, GRADE, assess_one, permit, review
from test_model_study import ok, run, session
from test_full_restore import app_client, request_restore, wait_restore
from test_backups import inspect_backup, make_backup
from test_daily_budget import AFTER, BEFORE, RECOVERY, SETTINGS, budget_client


REVISED = "排好活字后，用版框固定字块，试印前检查字行是否平整。"
REVISION = {"sections": [{"title": "固定活字并检查字行", "text": REVISED, "checks": ["版框和字行"]}]}
CHECKED = {"sections": [{"index": 0, "verdict": "supported", "reason": "与授权原文一致。",
                        "citations": [{"source": "S1", "quote": SOURCE}], "dependsOn": []}]}


def revision_traffic(calls, findings=CHECKED):
    def transport(address, headers, payload):
        prompt = payload["messages"][0]["content"]
        calls.append((payload["model"], prompt))
        value = REVISION if payload["model"] == "isolated-model" else findings
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(value)}}],
                               "usage": {"prompt_tokens": 40, "completion_tokens": 80}}).encode()
    return transport


def published_course(http, folder):
    initial = []
    project, lesson, source, scope = prepare(http, folder, initial, limit=12)
    authorize(http, project, scope)
    assert run(folder, model_traffic(initial)) and publish(folder)
    return project, lesson, source


def queue_revision(http, endpoint, request):
    preview = ok(http.post(f"{endpoint}/preview", json=request))
    submission = {**request, "confirmation": preview["confirmation"]}
    reply = ok(http.post(endpoint, json=submission))
    assert ok(http.post(endpoint, json=submission))["id"] == reply["id"]
    return reply, preview


def test_local_revision_reviews_diff_before_accepting_and_preserves_the_original(tmp_path):
    with session(tmp_path) as http:
        project, lesson, source = published_course(http, tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        endpoint = lesson_url + "/revision"
        old = ok(http.get(lesson_url))
        ok(http.put(lesson_url + "/learned", json={"learned": True}))
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, [])
        original = review(http, lesson)
        ok(http.post("/api/demo/next-day"))
        objective_path = lesson_url + f"/assessments/{original['key']}"
        first_round = ok(http.get(objective_path))["group"]
        answers = ["把它们固定在版框里。", "检查字行是否平整。"]
        ok(http.post(f"/api/assessments/{first_round['id']}/submit", json={"action": "submit", "answers": answers}))
        assert assess_one(tmp_path, GRADE, [])
        progressed = review(http, lesson)
        assert progressed["stage"] == 1
        note = ok(http.get(lesson_url + "/note"))
        ok(http.put(lesson_url + "/note", json={"content": "保留我的笔记。", "baseRevision": note["revision"]}))
        request = {"baseVersion": 1, "section": 0, "instruction": "把试印前的检查说清楚"}
        pending, preview = queue_revision(http, endpoint, request)
        assert pending["state"] == "pending"
        assert preview["main"]["model"] == "isolated-model" and preview["reviewer"]["model"] == "independent-model"
        assert preview["content"][0]["text"] in old["body"]
        assert preview["sources"][0]["excerpt"] == SOURCE

        calls = []
        assert run(tmp_path, revision_traffic(calls))
        assert run(tmp_path, revision_traffic(calls))
        candidate = ok(http.get(endpoint))
        assert candidate["state"] == "ready" and candidate["changes"][0]["after"]["text"] == REVISED
        original_text = candidate["changes"][0]["before"]["text"]
        assert original_text in old["body"] and len(candidate["notes"]) == 1
        assert candidate["objectives"] == [{"id": original["key"], "title": original["title"]}]
        assert ok(http.get(lesson_url))["body"] == old["body"]
        assert not publish(tmp_path)
        assert [model for model, _ in calls] == ["isolated-model", "independent-model"]
        assert "私有附注" not in "".join(prompt for _, prompt in calls)
        assert "保留我的笔记" not in "".join(prompt for _, prompt in calls)
        accept = {"id": pending["id"], "confirmation": candidate["confirmation"]}
        result = ok(http.post(endpoint + "/accept", json=accept))
        assert result["publishedVersion"] == 2
        assert ok(http.post(endpoint + "/accept", json=accept))["publishedVersion"] == 2
        assert ok(http.get("/api/study-plan"))["courseUpdates"] == [{
            "id": pending["id"], "lessonId": lesson, "title": "活字排版", "projectTopic": old["project"]["topic"], "version": 2,
        }]
        assert ok(http.post(endpoint + "/seen", json={"id": pending["id"]}))["seen"] is True
        assert ok(http.get("/api/study-plan"))["courseUpdates"] == []
        current = ok(http.get(lesson_url))
        assert REVISED in current["body"] and current["project"]["lessons"][0]["learnedOn"]
        assert ok(http.get(lesson_url + "/versions/1"))["markdown"] == old["body"]
        anchor = f"{lesson_url}/revision-anchor?fromVersion=1&version=2&section=0&start=0&end={len(original_text)}"
        assert ok(http.get(anchor)) == {"target": None, "revision": True}
        assert ok(http.get(lesson_url + "/note"))["content"] == "保留我的笔记。"
        usage = ok(http.get(f"/api/projects/{project}/generation"))["usage"]
        assert usage["calls"] == 5
        assert ok(http.get(f"/api/files/{source['id']}?at=2"))["anchor"]["text"] == SOURCE
        held = ok(http.get(objective_path))
        assert (held["status"], held["stage"], held["dueOn"]) == ("held", 1, progressed["dueOn"])
        assert held["latest"]["answers"] == answers
        permit(http, lesson)
        renewed_prompts = []
        assert assess_one(tmp_path, COMPOSED, renewed_prompts)
        assert REVISED in renewed_prompts[0][1]
        resumed = ok(http.get(objective_path))
        assert (resumed["status"], resumed["stage"], resumed["dueOn"]) == (progressed["status"], 1, progressed["dueOn"])
        assert resumed["latest"]["answers"] == answers
        assert http.post(f"/api/assessments/{first_round['id']}/recheck").status_code == 409


def test_unverified_rewrite_can_be_discarded_without_switching_or_resetting_usage(tmp_path):
    with session(tmp_path) as http:
        project, lesson, _ = published_course(http, tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        endpoint = lesson_url + "/revision"
        old = ok(http.get(lesson_url))["body"]
        request = {"baseVersion": 1, "instruction": "重写整节，补充检查步骤"}
        pending, _ = queue_revision(http, endpoint, request)
        calls = []
        uncertain = {"sections": [{"index": 0, "verdict": "insufficient", "reason": "缺少依据。",
                                   "citations": [], "dependsOn": []}]}
        assert run(tmp_path, revision_traffic(calls, uncertain))
        assert run(tmp_path, revision_traffic(calls, uncertain))
        result = ok(http.get(endpoint))
        assert result["state"] == "not_verified" and "confirmation" not in result
        assert http.post(endpoint + "/accept", json={"id": pending["id"], "confirmation": "0" * 64}).status_code == 409
        ok(http.post(endpoint + "/discard", json={"id": pending["id"]}))
        assert ok(http.get(endpoint))["state"] == "discarded"
        assert ok(http.get(lesson_url))["body"] == old
        assert ok(http.get(f"/api/projects/{project}/generation"))["usage"]["calls"] == 5
        with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3")) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_changed_sources_fence_accepting_the_candidate(tmp_path):
    with session(tmp_path) as http:
        _, lesson, source = published_course(http, tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        endpoint = lesson_url + "/revision"
        request = {"baseVersion": 1, "instruction": "补充试印检查"}
        pending, _ = queue_revision(http, endpoint, request)
        calls = []
        assert run(tmp_path, revision_traffic(calls)) and run(tmp_path, revision_traffic(calls))
        candidate = ok(http.get(endpoint))
        assert candidate["state"] == "ready"
        (tmp_path / source["path"]).write_text("原文已变更。", encoding="utf-8")
        assert http.post(endpoint + "/accept", json={"id": pending["id"], "confirmation": candidate["confirmation"]}).status_code == 409
        assert ok(http.get(lesson_url))["courseVersion"] == 1
        assert ok(http.get(endpoint))["reason"] == "review_sources_changed"
        assert len(calls) == 2


def test_restore_waits_for_continue_and_can_accept_saved_verification_without_new_model_calls(tmp_path):
    with app_client(tmp_path) as http:
        project, lesson, _ = published_course(http, tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        endpoint = lesson_url + "/revision"
        pending, _ = queue_revision(http, endpoint, {"baseVersion": 1, "instruction": "重写试印步骤"})
        traffic = []
        assert run(tmp_path, revision_traffic(traffic)) and run(tmp_path, revision_traffic(traffic))
        before = ok(http.get(endpoint))
        assert before["state"] == "ready"
        archived = make_backup(http)
        ok(http.post(endpoint + "/discard", json={"id": pending["id"]}))
        request_restore(http, archived, ok(inspect_backup(http, archived)))
        assert wait_restore(http)["state"] == "complete"
        stopped = ok(http.get(endpoint))
        assert stopped["state"] == "stopped" and stopped["reason"] == "restored"
        assert http.post(endpoint + "/accept", json={"id": pending["id"], "confirmation": before["confirmation"]}).status_code == 409
        ok(http.post(f"/api/projects/{project}/generation/control", json={"action": "resume"}))
        ready = ok(http.get(endpoint))
        assert ready["state"] == "ready" and not run(tmp_path, revision_traffic(traffic))
        accepted = ok(http.post(endpoint + "/accept", json={"id": pending["id"], "confirmation": ready["confirmation"]}))
        assert accepted["publishedVersion"] == 2 and len(traffic) == 2


def test_next_day_automatic_recovery_finishes_revision_review_once(tmp_path):
    now = BEFORE
    calls = []
    with budget_client(tmp_path, lambda: now) as http:
        project, lesson, _ = published_course(http, tmp_path)
        endpoint = f"/api/lessons/{lesson}/revision"
        ok(http.put(SETTINGS, json={"concurrency": 1, "dailyLimit": 1, "autoContinueDailyBudget": True}))
        queue_revision(http, endpoint, {"baseVersion": 1, "instruction": "补充试印检查"})
        assert run(tmp_path, revision_traffic(calls), now=lambda: now)
        assert not run(tmp_path, revision_traffic(calls), now=lambda: now)
        assert ok(http.get(endpoint))["reason"] == "daily_budget"
        now = AFTER
        assert ok(http.get(RECOVERY))["tasks"][0]["id"] == project
        assert run(tmp_path, revision_traffic(calls), now=lambda: now)
        assert not run(tmp_path, revision_traffic(calls), now=lambda: now)
        assert ok(http.get(endpoint))["state"] == "ready"
        assert [model for model, _ in calls] == ["isolated-model", "independent-model"]


def test_reauthorization_uses_saved_draft_and_rechecks_current_scope(tmp_path):
    with session(tmp_path) as http:
        project, lesson, original = published_course(http, tmp_path)
        endpoint = f"/api/lessons/{lesson}/revision"
        pending, _ = queue_revision(http, endpoint, {"baseVersion": 1, "instruction": "重写试印步骤"})
        traffic = []
        transport = revision_traffic(traffic)
        scope = {"sources": [{"fileId": original["id"], "revision": original["revision"], "at": 2}]}

        def replace_review_during_draft(*args):
            authorize(http, project, scope)
            return transport(*args)

        assert run(tmp_path, replace_review_during_draft)
        assert not run(tmp_path, transport)
        assert ok(http.get(endpoint))["canRenewReview"] is True and len(traffic) == 1
        identity = {"id": pending["id"]}
        preview = ok(http.post(endpoint + "/review-preview", json=identity))
        assert preview["content"][0]["text"] == REVISED
        authorize(http, project, scope)
        assert http.post(endpoint + "/review-authorize", json={**identity, "confirmation": preview["confirmation"]}).status_code == 409
        ready = ok(http.post(endpoint + "/review-preview", json=identity))
        ok(http.post(endpoint + "/review-authorize", json={**identity, "confirmation": ready["confirmation"]}))
        assert run(tmp_path, transport)
        assert [name for name, _ in traffic] == ["isolated-model", "independent-model"]
        assert ok(http.get(endpoint))["state"] == "ready"


def test_replacing_a_course_preserves_an_in_flight_assessment_without_advancing_it(tmp_path):
    with session(tmp_path) as http:
        _, lesson, _ = published_course(http, tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        endpoint = lesson_url + "/revision"
        ok(http.put(lesson_url + "/learned", json={"learned": True}))
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, [])
        original = review(http, lesson)
        objective_path = lesson_url + f"/assessments/{original['key']}"
        ok(http.post("/api/demo/next-day"))
        saved = ok(http.get(objective_path))["group"]
        round_url = f"/api/assessments/{saved['id']}"
        answers = ["放在版框中固定", "检查字行"]
        ok(http.post(round_url + "/submit", json={"action": "submit", "answers": answers}))
        pending, _ = queue_revision(http, endpoint, {"baseVersion": 1, "section": 0, "instruction": "重写本节第一段"})
        assert run(tmp_path, revision_traffic([])) and run(tmp_path, revision_traffic([]))
        candidate = ok(http.get(endpoint))
        accepted = {"id": pending["id"], "confirmation": candidate["confirmation"]}
        assert assess_one(tmp_path, GRADE, [], on_request=lambda: ok(http.post(endpoint + "/accept", json=accepted)))
        retained = ok(http.get(round_url))
        assert retained["state"] == "superseded" and retained["answers"] == answers and retained["result"] is None
        assert ok(http.get(objective_path))["status"] == "held"
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, [])
        refreshed = ok(http.get(objective_path))
        assert refreshed["status"] == "due" and refreshed["stage"] == 0 and refreshed["dueOn"] == original["dueOn"]
        assert refreshed["group"]["courseVersion"] == 2 and refreshed["group"]["id"] != saved["id"]
        assert ok(http.get(round_url))["answers"] == answers


def test_whole_revision_prevents_old_disputes_from_changing_the_new_edition(tmp_path):
    with session(tmp_path) as http:
        _, lesson, _ = published_course(http, tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        previous = ok(http.get(lesson_url + "/verification"))["findingId"]
        endpoint = lesson_url + "/revision"
        pending, _ = queue_revision(http, endpoint, {"baseVersion": 1, "instruction": "重写整节课程"})
        assert run(tmp_path, revision_traffic([])) and run(tmp_path, revision_traffic([]))
        candidate = ok(http.get(endpoint))
        assert ok(http.post(endpoint + "/accept", json={
            "id": pending["id"], "confirmation": candidate["confirmation"],
        }))["publishedVersion"] == 2
        batch = {"action": "recheck", "items": [{"lessonId": lesson, "index": 1, "findingId": previous}]}
        prediction = ok(http.post("/api/disputes/preview", json=batch))
        assert prediction["items"][0]["status"] == "unavailable"
        assert ok(http.get(lesson_url))["courseVersion"] == 2


def test_whole_rewrite_retains_identical_sections_and_old_anchors_when_order_changes(tmp_path):
    def checked(indices):
        return {"sections": [{**CHECKED["sections"][0], "index": index,
                              **({"verdict": "insufficient", "citations": []} if index > 1 else {})}
                             for index in indices]}

    with session(tmp_path) as http:
        project, lesson, _, scope = prepare(http, tmp_path, [], limit=10)
        authorize(http, project, scope)
        assert run(tmp_path, revision_traffic([], checked(range(4)))) and publish(tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        endpoint = lesson_url + "/revision"
        ok(http.put(lesson_url + "/learned", json={"learned": True}))
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, []) and assess_one(tmp_path, COMPOSED, [])
        previous = ok(http.get("/api/reviews"))["reviews"]
        pending, _ = queue_revision(http, endpoint, {"baseVersion": 1, "instruction": "调换这两个小节的顺序"})

        def transport(address, headers, payload):
            if payload["model"] != "isolated-model":
                return revision_traffic([], checked(range(2)))(address, headers, payload)
            content = {"sections": list(reversed(DRAFT["sections"][:2]))}
            return 200, json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()

        assert run(tmp_path, transport) and run(tmp_path, transport)
        preview = ok(http.get(endpoint))
        assert not preview["objectives"] and {item["index"] for item in preview["changes"]} == {0, 1}
        ok(http.post(endpoint + "/accept", json={"id": pending["id"], "confirmation": preview["confirmation"]}))
        published = ok(http.get(lesson_url + "/versions/2"))["sections"]
        assert [item["index"] for item in published] == [1, 0]
        assert ok(http.get("/api/reviews"))["reviews"] == previous
        anchor = lesson_url + "/revision-anchor?fromVersion=1&version=2&section=0&start=0&end=3"
        assert ok(http.get(anchor))["target"] == {"section": 0, "start": 0, "end": 3}


def test_partial_rewrite_with_unknown_dependency_remains_unverified(tmp_path):
    with session(tmp_path) as http:
        _, lesson, _ = published_course(http, tmp_path)
        endpoint = f"/api/lessons/{lesson}/revision"
        queue_revision(http, endpoint, {"baseVersion": 1, "section": 0, "instruction": "补充检查"})
        dependency = {"sections": [{**CHECKED["sections"][0], "dependsOn": [9]}]}
        assert run(tmp_path, revision_traffic([])) and run(tmp_path, revision_traffic([], dependency))
        assert ok(http.get(endpoint))["state"] == "not_verified"
