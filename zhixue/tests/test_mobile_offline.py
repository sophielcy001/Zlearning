import json
import os
from uuid import uuid4

import pytest

from test_demo import client, first_lesson


PATH = "/api/mobile"


def change(workspace, kind, **values):
    return {
        "id": str(uuid4()), "workspace": workspace, "kind": kind,
        "capturedAt": "2027-02-01T09:21:00+08:00", **values,
    }


def test_mobile_selects_a_lesson_and_preserves_both_conflicting_notes(client):
    http, folder = client
    url = first_lesson(http, folder)
    identity = url.rsplit("/", 1)[-1]
    initial = "# 测试\r\n电脑旧段\r\n手机旧段\r\n"
    saved = http.put(f"{url}/note", json={"content": initial})
    saved.raise_for_status()

    response = http.get(f"{PATH}/catalog")
    response.raise_for_status()
    catalog = response.json()
    assert len(catalog["lessons"]) == 3
    assert all("body" not in row and "note" not in row for row in catalog["lessons"])
    snapshot = http.get(f"{PATH}/lessons/{identity}").json()
    assert snapshot["body"] and snapshot["note"]["content"] == initial
    base = snapshot["note"]["revision"]
    http.put(f"{url}/note", json={"content": initial.replace("电脑旧段", "电脑新段"), "baseRevision": base}).raise_for_status()

    queued = change(catalog["workspace"], "note", noteId=identity, baseRevision=base,
                    content=initial.replace("手机旧段", "手机新段"))
    merged = http.post(f"{PATH}/changes", json=queued)
    merged.raise_for_status()
    merged = merged.json()
    expected = initial.replace("手机旧段", "手机新段").replace("电脑旧段", "电脑新段")
    assert merged["state"] == "saved" and merged["content"] == expected and merged["merged"]
    assert http.post(f"{PATH}/changes", json=queued).json() == merged
    assert http.post(f"{PATH}/changes", json={**queued, "content": "编号相同但内容不同"}).status_code == 409
    assert (folder / "library" / "notes" / f"{identity}.md").read_bytes() == expected.encode()

    conflict = change(catalog["workspace"], "note", noteId=identity, baseRevision=base,
                      content=initial.replace("电脑旧段", "手机同段改稿"))
    accepted = http.post(f"{PATH}/changes", json=conflict).json()
    assert accepted["state"] == "conflict"
    assert http.post(f"{PATH}/changes", json=conflict).json() == accepted
    result = http.get(f"{url}/note/conflicts/{accepted['conflictId']}").json()
    assert result["current"]["content"] == expected
    assert "手机同段改稿" in result["draft"]["content"]
    assert http.get(f"{url}/note").json()["content"] == expected
    resolution = http.post(f"{url}/note/conflicts/{accepted['conflictId']}", json={
        "action": "keep", "currentRevision": result["current"]["revision"],
        "draftRevision": result["draft"]["revision"], "externalRevision": result["external"]["revision"],
    })
    resolution.raise_for_status()
    confirmed = http.get(f"{PATH}/changes/{conflict['id']}", params={"workspace": catalog["workspace"]}).json()
    assert confirmed["state"] == "saved" and confirmed["content"] == expected
    assert http.post(f"{PATH}/changes", json=conflict).json() == confirmed
    latest = expected + "电脑核对后继续写入\r\n"
    http.put(f"{url}/note", json={"content": latest, "baseRevision": confirmed["revision"]}).raise_for_status()
    refreshed = http.get(f"{PATH}/changes/{conflict['id']}", params={"workspace": catalog["workspace"]}).json()
    assert refreshed["content"] == latest and refreshed["submittedRevision"] == confirmed["submittedRevision"]
    assert http.post(f"{PATH}/changes", json=conflict).json() == refreshed
    assert http.post(f"{PATH}/changes", json={**conflict, "content": latest}).status_code == 409
    assert http.post(f"{PATH}/changes", json={**conflict, "id": str(uuid4()), "workspace": str(uuid4())}).status_code == 409


def test_mobile_archives_repeated_attempts_and_settles_at_most_once(client):
    http, folder = client
    url = first_lesson(http, folder)
    identity = url.rsplit("/", 1)[-1]
    http.put(f"{url}/learned", json={"learned": True}).raise_for_status()
    http.post("/api/demo/next-day").raise_for_status()
    response = http.get(f"{PATH}/catalog")
    response.raise_for_status()
    catalog = response.json()
    choice = next(row for row in catalog["reviews"] if row["key"] == "rows-columns")
    paper = http.get(f"{PATH}/lessons/{identity}/reviews/{choice['key']}").json()
    assert paper["kind"] == "local" and len(paper["questions"]) == 2
    assert all(set(row) == {"text", "options"} for row in paper["questions"])
    sent = change(catalog["workspace"], "review", lessonId=identity, key=choice["key"],
                  basis=paper["basis"], round=paper["round"], answers=[0, 1], action="submit")
    result = http.post(f"{PATH}/changes", json=sent)
    result.raise_for_status()
    completed = result.json()
    assert completed["state"] == "settled" and completed["result"]["outcome"] == "passed"
    assert http.post(f"{PATH}/changes", json=sent).json() == completed
    duplicate = {**sent, "id": str(uuid4()), "answers": [1, 1], "capturedAt": "2027-02-01T08:15:00+08:00"}
    received = http.post(f"{PATH}/changes", json=duplicate)
    received.raise_for_status()
    assert received.json()["state"] == "held"
    assert http.get(f"{url}/reviews/{choice['key']}").json()["round"] == 2
    history = http.get(f"{PATH}/changes").json()["reviews"]
    assert {row["id"]: row["answers"] for row in history} == {sent["id"]: [0, 1], duplicate["id"]: [1, 1]}


def test_downloaded_model_answer_waits_for_authorized_worker_and_survives_resend(tmp_path):
    from test_model_assessments import COMPOSED, GRADE, assess_one, permit, source_lesson
    from test_model_study import session

    prompts = []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path, adopt=True)
        permit(http, lesson, max_calls=6)
        assert assess_one(tmp_path, COMPOSED, prompts)
        http.post("/api/demo/next-day").raise_for_status()
        catalog = http.get(f"{PATH}/catalog").json()
        choice = next(row for row in catalog["reviews"] if row["lessonId"] == lesson)
        endpoint = f"{PATH}/lessons/{lesson}/reviews/{choice['key']}"
        paper = http.get(endpoint).json()
        assert paper["kind"] == "model" and all(set(question) == {"text"} for question in paper["questions"])
        answers = ["把活字固定在版框内。", "字行是否平整。"]
        queued = change(catalog["workspace"], "review", lessonId=lesson, key=choice["key"], basis=paper["basis"],
                        round=paper["round"], groupId=paper["groupId"], answers=answers, action="submit")
        receipt = http.post(f"{PATH}/changes", json=queued)
        receipt.raise_for_status()
        assert receipt.json()["state"] == "grading"
        assert http.post(f"{PATH}/changes", json=queued).json() == receipt.json()
        assert http.post(f"{PATH}/changes", json={**queued, "id": str(uuid4())}).json()["state"] == "held"
        assert len(prompts) == 1
        assert assess_one(tmp_path, GRADE, prompts)
        assert not assess_one(tmp_path, GRADE, prompts)
        state = http.get(f"{PATH}/changes/{queued['id']}", params={"workspace": catalog["workspace"]}).json()
        assert state["state"] == "settled" and state["result"]["outcome"] == "passed"
        retry = http.post(f"{PATH}/changes", json=queued)
        retry.raise_for_status()
        assert retry.json() == state
        assert len(prompts) == 2 and answers[0] in prompts[1][1]


def test_note_retry_recovers_exact_staged_write_when_reply_is_interrupted(client, monkeypatch):
    from contextlib import contextmanager

    from fastapi import HTTPException

    from backend import mobile_api

    http, folder = client
    url = first_lesson(http, folder)
    identity = url.rsplit("/", 1)[-1]
    workspace = http.get(f"{PATH}/catalog").json()["workspace"]
    note = http.get(f"{PATH}/lessons/{identity}").json()["note"]
    queued = change(workspace, "note", noteId=identity, baseRevision=note["revision"], content="从手机保存的正文")
    real_open = mobile_api.open_note
    calls = 0

    @contextmanager
    def lose_connection(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(503, "模拟电脑在提交回执前断开")
        with real_open(*args, **kwargs) as opened:
            yield opened

    monkeypatch.setattr(mobile_api, "open_note", lose_connection)
    assert http.post(f"{PATH}/changes", json=queued).status_code == 503
    monkeypatch.setattr(mobile_api, "open_note", real_open)
    saved = http.post(f"{PATH}/changes", json=queued)
    saved.raise_for_status()
    assert saved.json()["content"] == queued["content"] and saved.json()["state"] == "saved"
    assert http.post(f"{PATH}/changes", json=queued).json() == saved.json()
    assert (folder / "library" / "notes" / f"{identity}.md").read_bytes() == queued["content"].encode()


@pytest.mark.skipif(os.name != "nt", reason="Requires a Windows sharing violation")
def test_offline_note_recovers_a_lost_sharing_conflict_receipt_and_preserves_later_phone_edits(tmp_path):
    from test_notes import app as note_workspace

    initial = "# 学习笔记\n电脑原段\n分隔段\n手机原段\n"
    external = initial.replace("电脑原段", "外部编辑器改稿")
    phone = initial.replace("手机原段", "手机第一次草稿")
    later = phone.replace("手机第一次草稿", "手机后续草稿")
    merged = external.replace("手机原段", "手机第一次草稿")
    expected = external.replace("手机原段", "手机后续草稿")
    with note_workspace(tmp_path) as http:
        url = first_lesson(http, tmp_path)
        identity = url.rsplit("/", 1)[-1]
        note_url = f"{url}/note"
        http.put(note_url, json={"content": initial}).raise_for_status()
        workspace = http.get(f"{PATH}/catalog").json()["workspace"]
        snapshot = http.get(f"{PATH}/lessons/{identity}").json()["note"]
        queued = change(workspace, "note", noteId=identity, baseRevision=snapshot["revision"], content=phone)
        path = tmp_path / "library" / "notes" / f"{identity}.md"
        with path.open("r+b") as editor:
            response = http.post(f"{PATH}/changes", json=queued)
            response.raise_for_status()
            first = response.json()
            assert first["state"] == "conflict"
            assert path.read_bytes() == initial.encode()
            editor.seek(0)
            editor.write(external.encode())
            editor.truncate()

    with note_workspace(tmp_path) as http:
        replay = http.post(f"{PATH}/changes", json=queued)
        replay.raise_for_status()
        assert replay.json() == first
        receipt_url = f"{PATH}/changes/{queued['id']}"
        assert http.get(receipt_url, params={"workspace": workspace}).json() == first
        [conflict] = http.get(note_url).json()["conflicts"]
        assert conflict["id"] == first["conflictId"] and conflict["kind"] == "interrupted"
        conflict_url = f"{note_url}/conflicts/{conflict['id']}"
        preview = http.get(conflict_url).json()
        assert preview["current"]["content"] == external and preview["draft"]["content"] == phone
        result = http.post(conflict_url, json={
            "action": "merge", "currentRevision": preview["current"]["revision"], "content": merged,
            "draftRevision": preview["draft"]["revision"], "externalRevision": preview["external"]["revision"],
        })
        result.raise_for_status()
        resolved = http.get(receipt_url, params={"workspace": workspace}).json()
        assert (resolved["state"], resolved["content"], resolved["revision"]) == ("saved", merged, result.json()["revision"])
        assert resolved["submittedRevision"] == first["submittedRevision"] == preview["draft"]["revision"]
        resolved_replay = http.post(f"{PATH}/changes", json=queued)
        resolved_replay.raise_for_status()
        assert resolved_replay.json() == resolved
        follow_up = change(workspace, "note", noteId=identity, baseRevision=resolved["submittedRevision"], content=later)
        latest = http.post(f"{PATH}/changes", json=follow_up)
        latest.raise_for_status()
        latest = latest.json()
        assert latest["state"] == "saved" and latest["content"] == expected and latest["merged"]
        assert http.post(f"{PATH}/changes", json=follow_up).json() == latest
        assert http.get(note_url).json()["conflicts"] == []
        assert path.read_bytes() == expected.encode()
    (tmp_path / "手机笔记占用同步.json").write_text(json.dumps({
        "first": first, "replayAfterRestart": replay.json(), "desktopMerged": resolved,
        "replayAfterResolution": resolved_replay.json(),
        "laterPhoneEdit": latest, "finalMarkdown": path.read_bytes().decode("utf-8"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
