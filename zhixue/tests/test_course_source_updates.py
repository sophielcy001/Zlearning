from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO
import sqlite3
from threading import Event

import pymupdf
from PIL import Image

from backend import course_sources
from backend.library_index import LibraryIndex
from test_course_revision import published_course, queue_revision, revision_traffic
from test_course_verification import FINDINGS, SOURCE, authorize, prepare, publish
from test_library import upload
from test_model_study import ok, run, session


NEW_QUOTE = "版框固定后，先检查字行和墨色，再开始试印。"
TODOS = "/api/study-plan/source-updates"


def test_source_update_compares_saved_quote_and_preserves_course_until_explicit_revision(tmp_path):
    with session(tmp_path) as http:
        project, lesson, source = published_course(http, tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        update_url = lesson_url + "/source-updates"
        endpoint = lesson_url + "/revision"
        original = ok(http.get(lesson_url))
        source_file = tmp_path / source["path"]
        calls = []
        assert ok(http.get(TODOS)) == []
        source_file.write_text(f"本机资料备注\n{NEW_QUOTE}\n私有附注", encoding="utf-8")

        todos = ok(http.get(TODOS))
        assert [(todo["lessonId"], todo["sectionCount"]) for todo in todos] == [(lesson, 1)]
        assert ok(http.get(TODOS)) == todos
        update = ok(http.get(update_url))
        assert update["version"] == original["courseVersion"]
        assert update["section"] == 0 and update["instruction"]
        [section] = update["sections"]
        [citation] = section["citations"]
        assert citation["previous"]["quote"] == SOURCE
        assert citation["status"] == "changed"
        assert citation["current"]["excerpt"] == NEW_QUOTE
        assert ok(http.get(lesson_url))["courseVersion"] == original["courseVersion"]
        assert not run(tmp_path, revision_traffic(calls))
        assert not calls

        locator = {key: citation["current"][key] for key in ("fileId", "at", "offset", "revision")}
        request = {
            "baseVersion": update["version"], "section": update["section"],
            "instruction": update["instruction"], "sources": [locator],
        }
        preview = ok(http.post(endpoint + "/preview", json=request))
        assert preview["shareSourcesWithAuthor"] is True
        assert [entry["excerpt"] for entry in preview["sources"]] == [NEW_QUOTE]
        assert not run(tmp_path, revision_traffic(calls))
        source_file.write_text(f"本机资料备注\n{NEW_QUOTE}\n已变更的私有附注", encoding="utf-8")
        response = http.post(endpoint, json={**request, "confirmation": preview["confirmation"]})
        assert response.status_code == 409
        assert not run(tmp_path, revision_traffic(calls))
        assert not calls

        current = ok(http.get(update_url))["sections"][0]["citations"][0]["current"]
        request["sources"][0]["revision"] = current["revision"]
        queued, _ = queue_revision(http, endpoint, request)
        transport = revision_traffic(calls, {"sections": [{
            "index": 0, "verdict": "supported", "reason": "经核对的新版资料。",
            "citations": [{"source": "S1", "quote": NEW_QUOTE}], "dependsOn": [],
        }]})
        assert run(tmp_path, transport) and run(tmp_path, transport)
        assert len(calls) == 2 and all(NEW_QUOTE in prompt and "私有附注" not in prompt for _, prompt in calls)
        assert ok(http.get(lesson_url))["courseVersion"] == original["courseVersion"]
        assert ok(http.get(TODOS)) == todos
        ready = ok(http.get(endpoint))
        assert ready["state"] == "ready"
        ok(http.post(endpoint + "/accept", json={"id": queued["id"], "confirmation": ready["confirmation"]}))
        assert ok(http.get(lesson_url))["courseVersion"] == original["courseVersion"] + 1
        assert ok(http.get(TODOS)) == []
        assert source["revision"] == ok(http.get(f"/api/projects/{project}/course-review"))["sources"][0]["revision"]


def test_source_update_distinguishes_a_moved_quote_from_a_missing_original(tmp_path):
    with session(tmp_path) as http:
        _, lesson, source = published_course(http, tmp_path)
        source_file = tmp_path / source["path"]
        update_url = f"/api/lessons/{lesson}/source-updates"

        original = source_file.read_bytes()
        source_file.write_text(f"新增备注\n本机资料备注\n{SOURCE}\n私有附注", encoding="utf-8")
        moved = ok(http.get(update_url))["sections"][0]["citations"][0]
        assert moved["status"] == "moved"
        assert moved["previous"]["at"] == 2 and moved["current"]["at"] == 3
        assert moved["current"]["excerpt"] == SOURCE

        parked = tmp_path.with_name(tmp_path.name + "-parked.txt")
        source_file.rename(parked)
        missing = ok(http.get(update_url))
        assert missing["sections"][0]["citations"][0]["status"] == "unavailable"
        assert missing["sections"][0]["citations"][0]["current"] is None
        assert "替代依据" in missing["instruction"]
        assert len(ok(http.get(TODOS))) == 1

        parked.write_bytes(original)
        parked.rename(source_file)
        assert ok(http.get(TODOS)) == []
        assert ok(http.get(update_url))["sections"] == []


def test_source_update_includes_sections_that_depend_on_the_changed_evidence(tmp_path):
    with session(tmp_path) as http:
        project, lesson, source, scope = prepare(http, tmp_path, [])
        supplemental_id = ok(http.post("/api/files", json={"name": "补充.txt", "content": "此处说明相关印刷步骤。"}))["id"]
        supplemental = ok(http.get(f"/api/files/{supplemental_id}?at=1"))
        scope["sources"].append({"fileId": supplemental_id, "at": 1, "revision": supplemental["revision"]})
        authorize(http, project, scope)
        assert run(tmp_path, revision_traffic([], {"sections": [{
            "index": i, "verdict": "supported", "reason": "由试印资料支持。",
            "citations": [{"source": "S2" if i else "S1",
                           "quote": "此处说明相关印刷步骤。" if i else SOURCE}],
            "dependsOn": [i - 1] if i in {1, 2} else [],
        } for i in range(4)]})) and publish(tmp_path)

        (tmp_path / source["path"]).write_text(f"本机资料备注\n{NEW_QUOTE}", encoding="utf-8")
        [todo] = ok(http.get(TODOS))
        assert todo["sectionCount"] == 3
        update = ok(http.get(f"/api/lessons/{lesson}/source-updates"))
        assert update["section"] is None
        assert [section["index"] for section in update["sections"]] == [0, 1, 2]
        assert [section["dependencyTitles"] for section in update["sections"]] == [[], ["固定活字"], ["温度"]]
        assert [len(section["citations"]) for section in update["sections"]] == [1, 0, 0]


def test_source_update_can_renew_only_the_reviewer_scope_after_authoring(tmp_path):
    with session(tmp_path) as http:
        _, lesson, source = published_course(http, tmp_path)
        lesson_url = f"/api/lessons/{lesson}"
        endpoint = lesson_url + "/revision"
        source_file = tmp_path / source["path"]
        source_file.write_text(f"本机资料备注\n{NEW_QUOTE}\n私有附注", encoding="utf-8")
        current = ok(http.get(lesson_url + "/source-updates"))["sections"][0]["citations"][0]["current"]
        source_keys = ("fileId", "at", "offset", "revision")
        queued, _ = queue_revision(http, endpoint, {
            "baseVersion": 1, "section": 0, "instruction": "使用新的试印资料。",
            "sources": [{key: current[key] for key in source_keys}],
        })
        calls = []
        transport = revision_traffic(calls, {"sections": [{
            "index": 0, "verdict": "supported", "reason": "经新版资料核对。",
            "citations": [{"source": "S1", "quote": NEW_QUOTE}], "dependsOn": [],
        }]})
        assert run(tmp_path, transport)
        assert [model for model, _ in calls] == ["isolated-model"]
        source_file.write_text(f"新增内容\n本机资料备注\n{NEW_QUOTE}\n私有附注", encoding="utf-8")
        assert not run(tmp_path, transport)
        assert ok(http.get(endpoint))["canRenewReview"]
        refreshed = ok(http.get(f"/api/files/{source['id']}?at=3"))
        renewal = {"id": queued["id"], "sources": [{"fileId": source["id"], "at": 3, "revision": refreshed["revision"]}]}
        preview = ok(http.post(endpoint + "/review-preview", json=renewal))
        assert preview["sources"][0]["excerpt"] == NEW_QUOTE
        assert len(calls) == 1
        ok(http.post(endpoint + "/review-authorize", json={**renewal, "confirmation": preview["confirmation"]}))
        assert run(tmp_path, transport)
        assert [model for model, _ in calls] == ["isolated-model", "independent-model"]
        assert ok(http.get(endpoint))["state"] == "ready"
        assert ok(http.get(lesson_url))["courseVersion"] == 1


def test_correcting_reviewed_ocr_marks_the_source_even_when_original_bytes_are_unchanged(tmp_path):
    with session(tmp_path) as http:
        project, lesson, _, _ = prepare(http, tmp_path, [])
        image = BytesIO()
        Image.new("RGB", (16, 16), "white").save(image, "PNG")
        item_id = ok(upload(http, "试印扫描.png", image.getvalue()))["id"]
        file_url = f"/api/files/{item_id}"
        original = ok(http.get(file_url))["revision"]
        ocr_url = file_url + "/pages/1/ocr"
        reviewed = ok(http.put(ocr_url, json={"revision": original, "textRevision": None, "text": SOURCE}))
        authorize(http, project, {"sources": [{"fileId": item_id, "at": 1, "revision": reviewed["textRevision"]}]})
        assert run(tmp_path, revision_traffic([], FINDINGS)) and publish(tmp_path)
        assert ok(http.get(TODOS)) == []

        corrected = ok(http.put(ocr_url, json={"revision": original, "textRevision": reviewed["textRevision"], "text": NEW_QUOTE}))
        assert ok(http.get(file_url))["revision"] == original
        assert len(ok(http.get(TODOS))) == 1
        update = ok(http.get(f"/api/lessons/{lesson}/source-updates"))
        comparison = update["sections"][0]["citations"][0]
        assert comparison["status"] == "changed"
        assert comparison["previous"]["revision"] == reviewed["textRevision"]
        assert comparison["current"]["revision"] == corrected["textRevision"]
        assert comparison["current"]["excerpt"] == NEW_QUOTE


def test_first_ocr_override_of_a_text_page_invalidates_old_locator_and_queued_revision(tmp_path, monkeypatch):
    with session(tmp_path) as http:
        project, lesson, _, _ = prepare(http, tmp_path, [])
        original_quote = "Check the type before proof printing."
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), original_quote)
            data = pdf.tobytes()
        file_id = ok(upload(http, "试印.pdf", data))["id"]
        file_url = f"/api/files/{file_id}"
        original = ok(http.get(file_url + "?at=1"))["revision"]
        authorize(http, project, {"sources": [{"fileId": file_id, "at": 1, "revision": original}]})
        result = {"sections": [
            {**FINDINGS["sections"][0], "citations": [{"source": "S1", "quote": original_quote}]},
            *FINDINGS["sections"][1:],
        ]}
        assert run(tmp_path, revision_traffic([], result)) and publish(tmp_path)
        endpoint = f"/api/lessons/{lesson}/revision"
        queue_revision(http, endpoint, {"baseVersion": 1, "section": 0, "instruction": "检查试印步骤。"})
        assert ok(http.get(TODOS)) == []

        update_url = f"/api/lessons/{lesson}/source-updates"
        with monkeypatch.context() as lagging_index:
            lagging_index.setattr(LibraryIndex, "refresh", lambda self, **options: 0)
            corrected = ok(http.put(file_url + "/pages/1/ocr", json={
                "revision": original, "textRevision": None, "text": NEW_QUOTE,
            }))
            assert ok(http.get(file_url))["revision"] == original
            assert len(ok(http.get(TODOS))) == 1
            pending = ok(http.get(update_url))["sections"][0]["citations"][0]
            assert pending["current"] is None
        comparison = ok(http.get(update_url))["sections"][0]["citations"][0]
        assert comparison["current"]["revision"] == corrected["textRevision"]
        assert comparison["previous"]["quote"] == original_quote
        assert ok(http.get(endpoint))["state"] == "blocked"
        calls = []
        assert not run(tmp_path, revision_traffic(calls, result))
        assert not calls


def test_source_todo_poll_does_not_hold_the_database_writer_lock_while_reading_files(tmp_path, monkeypatch):
    with session(tmp_path) as http:
        published_course(http, tmp_path)
        scanning, resume = Event(), Event()
        snapshot = course_sources.read_snapshot

        def slow_snapshot(path):
            scanning.set()
            assert resume.wait(10)
            return snapshot(path)

        monkeypatch.setattr(course_sources, "read_snapshot", slow_snapshot)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(http.get, TODOS)
            try:
                assert scanning.wait(5)
                with closing(sqlite3.connect(tmp_path / "zhixue.sqlite3", timeout=0.3)) as writer, writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute("UPDATE lessons SET version=version")
            finally:
                resume.set()
            assert ok(pending.result(timeout=5)) == []
