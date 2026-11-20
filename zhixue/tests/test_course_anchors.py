from contextlib import closing
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config

from backend.db import ROOT, open_db
from backend.library import local_path
from test_backups import inspect_backup, make_backup
from test_full_restore import app_client, request_restore, wait_restore
from test_course_revision import CHECKED, REVISED, published_course, queue_revision, revision_traffic
from test_course_verification import DRAFT
from test_model_study import ok, run, session


ORIGINAL = DRAFT["sections"][0]["text"]


def anchor(version, text, *, start=0):
    return {"version": version, "section": 0, "start": start, "end": start + len(text)}


def original_link(lesson, source):
    return (f"#/lessons/{lesson}?readingVersion={source['version']}&section={source['section']}"
            f"&selectionStart={source['start']}&selectionEnd={source['end']}")


def locate(http, lesson, source, version):
    return ok(http.get(f"/api/lessons/{lesson}/revision-anchor", params={
        "fromVersion": source["version"], "version": version,
        **{key: source[key] for key in ("section", "start", "end")},
    }))


def rewrite(http, folder, lesson, base_version, text=REVISED, *, sections=None):
    endpoint = f"/api/lessons/{lesson}/revision"
    job, _ = queue_revision(http, endpoint, {
        "baseVersion": base_version, "section": 0 if sections is None else None, "instruction": "更新排版步骤。",
    })
    shown = sections or [{"title": "核对试印步骤", "text": text, "checks": ["版面"]}]
    review = revision_traffic([], {"sections": [
        {**CHECKED["sections"][0], "index": index} for index in range(len(shown))
    ]})

    def transport(address, headers, payload):
        if payload["model"] != "isolated-model":
            return review(address, headers, payload)
        content = json.dumps({"sections": shown})
        return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    assert run(folder, transport) and run(folder, transport)
    ready = ok(http.get(endpoint))
    assert ready["state"] == "ready"
    predicted = ok(http.get(endpoint + "/anchor-impact"))
    assert predicted["version"] == base_version + 1
    assert ok(http.get(f"/api/lessons/{lesson}"))["courseVersion"] == base_version
    ok(http.post(endpoint + "/accept", json={"id": job["id"], "confirmation": ready["confirmation"]}))
    ok(http.post(endpoint + "/seen", json={"id": job["id"]}))
    return predicted


def save_excerpt(http, endpoint, body):
    revision = ok(http.get(endpoint))["revision"]
    ok(http.put(endpoint, json={"content": body, "baseRevision": revision}))
    return ok(http.get(endpoint))


def test_saved_note_links_can_be_reassociated_without_changing_original_notes_or_versions(tmp_path):
    with session(tmp_path) as http:
        _, lesson, _ = published_course(http, tmp_path)
        endpoint = f"/api/lessons/{lesson}/anchor-links"
        source = anchor(1, ORIGINAL)
        href = original_link(lesson, source)
        link = f"[查看原文 · 第 1 版]({href})"
        note_endpoint = f"/api/lessons/{lesson}/note"
        note_body = f"> {ORIGINAL}\n\n{link}\n"
        legacy = local_path(tmp_path, "notes", lesson)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(note_body.encode("utf-8"))
        escaped = link.replace("#", "&#35;", 1)
        sibling, code_only = str(uuid4()), str(uuid4())
        for identity, name, content in (
            (sibling, "项目摘录", f"{escaped}\n\n{escaped}"),
            (code_only, "Markdown 用法", f"\x60\x60\x60md\n{link}\n\x60\x60\x60"),
        ):
            ok(http.post("/api/notes", json={"id": identity, "title": name, "lessonIds": []}))
            save_excerpt(http, f"/api/notes/{identity}", content)

        before_acceptance = rewrite(http, tmp_path, lesson, 1, "😀" + REVISED)
        assert {item["id"] for item in before_acceptance["items"][0]["notes"]} == {lesson, sibling}
        [pending] = ok(http.get(endpoint))["items"]
        assert pending["source"] == source and pending["quote"] == ORIGINAL
        assert {item["id"] for item in pending["notes"]} == {lesson, sibling}

        selection = {"source": source, "target": anchor(2, REVISED, start=1)}
        preview = ok(http.post(endpoint + "/preview", json=selection))
        assert preview["source"]["quote"] == ORIGINAL and preview["target"]["quote"] == REVISED
        stale_selection = {"source": source, "target": anchor(2, "😀")}
        stale = ok(http.post(endpoint + "/preview", json=stale_selection))
        assert locate(http, lesson, source, 2) == {"target": None, "revision": True}

        submission = {**selection, "confirmation": preview["confirmation"]}
        course_document = ok(http.get(f"/api/files/course-{lesson}"))
        original_file = tmp_path / course_document["path"]
        frozen = original_file.read_bytes()
        original_file.write_bytes(frozen + b"\nAn external edit.\n")
        assert http.put(endpoint, json=submission).status_code == 409
        assert original_file.read_bytes().endswith(b"An external edit.\n")
        original_file.write_bytes(frozen)
        ok(http.put(endpoint, json=submission))
        ok(http.put(endpoint, json=submission))
        assert http.put(endpoint, json={**stale_selection, "confirmation": stale["confirmation"]}).status_code == 409
        assert ok(http.get(endpoint))["items"] == []
        saved = ok(http.get(note_endpoint))
        assert (saved["content"], saved["revision"]) == (note_body, sha256(note_body.encode("utf-8")).hexdigest())
        assert ok(http.get(f"/api/lessons/{lesson}/versions/1"))["sections"][0]["text"] == ORIGINAL

    with session(tmp_path) as http:
        result = locate(http, lesson, source, 2)
        assert result["manual"] and result["target"]["start"] == 1
        assert result["target"]["end"] == 1 + len(REVISED)


def test_reassociation_follows_the_selected_passage_across_later_editions_and_rejects_stale_preview(tmp_path):
    with session(tmp_path) as http:
        _, lesson, _ = published_course(http, tmp_path)
        endpoint = f"/api/lessons/{lesson}/anchor-links"
        source = anchor(1, ORIGINAL)
        href = original_link(lesson, source)
        save_excerpt(http, f"/api/lessons/{lesson}/note", f"[原摘录]({href})\n")
        rewrite(http, tmp_path, lesson, 1)
        input = {"source": source, "target": anchor(2, REVISED)}
        preview = ok(http.post(endpoint + "/preview", json=input))
        ok(http.put(endpoint, json={**input, "confirmation": preview["confirmation"]}))

        later = "新版说明。\r\n" + REVISED
        rewrite(http, tmp_path, lesson, 2, later)
        shifted = locate(http, lesson, source, 3)
        assert shifted["manual"] and shifted["target"]["start"] == len("新版说明。\r\n")
        assert ok(http.get(endpoint))["items"] == []
        input = {"source": source, "target": anchor(3, later)}
        stale = ok(http.post(endpoint + "/preview", json=input))

        rewrite(http, tmp_path, lesson, 3, "先练习试印，再处理版框。")
        assert http.put(endpoint, json={**input, "confirmation": stale["confirmation"]}).status_code == 409
        assert locate(http, lesson, source, 4) == {"target": None, "revision": True, "manual": True}
        [pending] = ok(http.get(endpoint))["items"]
        assert pending["quote"] == ORIGINAL


def test_upgrade_from_0050_and_encrypted_restore_preserve_original_and_reassociated_links(tmp_path):
    with session(tmp_path) as http:
        _, lesson, _ = published_course(http, tmp_path)
        source = anchor(1, ORIGINAL)
        href = original_link(lesson, source)
        note_path = f"/api/lessons/{lesson}/note"
        note = save_excerpt(http, note_path, f"> {ORIGINAL}\n\n[原文]({href})\n")
        rewrite(http, tmp_path, lesson, 1)

    engine = open_db(tmp_path)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0050_course_revision")
    finally:
        engine.dispose()

    with app_client(tmp_path) as http:
        version_backups = list((tmp_path / "backups" / "database-upgrades").glob("*.sqlite3"))
        assert len(version_backups) == 1
        with closing(sqlite3.connect(version_backups[0])) as backup_db:
            assert backup_db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0050_course_revision",)
        endpoint = f"/api/lessons/{lesson}/anchor-links"
        assert ok(http.get(endpoint))["items"][0]["source"] == source
        original_target = {"source": source, "target": anchor(2, REVISED)}
        preview = ok(http.post(endpoint + "/preview", json=original_target))
        ok(http.put(endpoint, json={**original_target, "confirmation": preview["confirmation"]}))

        backup = make_backup(http)
        archived = ok(inspect_backup(http, backup))
        assert archived["counts"]["backup"]["course_anchor_links"] == 1
        newer_target = {"source": source, "target": anchor(2, REVISED[:8])}
        preview = ok(http.post(endpoint + "/preview", json=newer_target))
        ok(http.put(endpoint, json={**newer_target, "confirmation": preview["confirmation"]}))
        assert locate(http, lesson, source, 2)["target"]["end"] == 8
        request_restore(http, backup, ok(inspect_backup(http, backup)))
        assert wait_restore(http)["state"] == "complete"
        assert locate(http, lesson, source, 2)["target"]["end"] == len(REVISED)
        after = ok(http.get(note_path))
        assert (after["content"], after["revision"]) == (note["content"], note["revision"])


def test_recycled_section_index_cannot_resolve_an_ambiguous_later_edition(tmp_path):
    with session(tmp_path) as http:
        _, lesson, _ = published_course(http, tmp_path)
        endpoint = f"/api/lessons/{lesson}/anchor-links"
        source = anchor(1, ORIGINAL)
        save_excerpt(http, f"/api/lessons/{lesson}/note", f"[原摘录]({original_link(lesson, source)})\n")
        rewrite(http, tmp_path, lesson, 1)
        choice = {"source": source, "target": anchor(2, REVISED)}
        preview = ok(http.post(endpoint + "/preview", json=choice))
        ok(http.put(endpoint, json={**choice, "confirmation": preview["confirmation"]}))

        rewrite(http, tmp_path, lesson, 2, sections=[
            {"title": title, "text": REVISED, "checks": ["版面"]} for title in ("先检查版框", "版框操作回顾")
        ])
        assert locate(http, lesson, source, 3) == {"target": None, "revision": True, "manual": True}
        assert ok(http.get(endpoint))["items"][0]["source"] == source
