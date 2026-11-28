from datetime import date
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import create_app
from test_model_study import SESSION, ok, session
from test_professional_agent import create_agent


API = "/api/memories"


def add(http, text="分步讲解", **options):
    data = {"id": str(uuid4()), "text": text, **options}
    return data, ok(http.post(API, json=data))


def entries(http, view):
    return ok(http.get(API, params={"view": view}))["items"]


def ref(entry):
    return {"id": entry["id"], "revision": entry["revision"]}


def review(http, action, *items):
    return http.post(API + "/review", json={"action": action, "items": [ref(item) for item in items]})


def test_shared_approval_edit_and_delay_use_current_revision_and_day(tmp_path):
    day = [date(2026, 4, 2)]
    with TestClient(create_app(tmp_path, SESSION, today=lambda: day[0]), base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer " + SESSION}) as http:
        _, one = add(http, "先给例子")
        _, two = add(http, "做一道练习")
        assert entries(http, "shared") == []
        assert ok(http.get(API + "/summary")) == {"waiting": 2}
        changed = ok(http.put(API + "/" + one["id"], json={"revision": one["revision"], "text": "先给一个例子"}))
        stale = review(http, "accept", one, two)
        assert stale.status_code == 409 and entries(http, "shared") == []
        ok(review(http, "defer", changed, two))
        assert ok(http.get(API + "/summary")) == {"waiting": 0}
        deferred = entries(http, "pending")
        assert len(deferred) == 2 and {item["reviewOn"] for item in deferred} == {"2026-04-03"}
        day[0] = date(2026, 4, 3)
        assert ok(http.get(API + "/summary")) == {"waiting": 2}
        ok(review(http, "accept", *entries(http, "pending")))
        assert {row["text"] for row in entries(http, "shared")} == {"先给一个例子", "做一道练习"}
        assert entries(http, "pending") == []


def test_agent_share_request_preserves_private_scope_and_reject_does_not_erase_it(tmp_path):
    with session(tmp_path) as http:
        _, _, agent = create_agent(http, skills=[])
        _, private = add(http, agentId=agent["id"])
        assert private["state"] == "active" and private["agentId"] == agent["id"]
        queued = ok(http.post(API + "/" + private["id"] + "/share", json={"revision": private["revision"]}))
        assert entries(http, "shared") == [] and entries(http, "private") == [queued]
        ok(review(http, "reject", queued))
        [restored] = entries(http, "private")
        assert restored["state"] == "active" and restored["text"] == private["text"]
        queued = ok(http.post(API + "/" + private["id"] + "/share", json={"revision": restored["revision"]}))
        ok(review(http, "accept", queued))
        assert entries(http, "private") == []
        [shared] = entries(http, "shared")
        assert shared["agentId"] is None and shared["state"] == "active"


def test_deleted_chat_source_stays_blocked_and_original_conversation_survives_restart(tmp_path):
    identity, source_id = str(uuid4()), str(uuid4())
    text = "我的学习偏好是先练习。"
    with session(tmp_path) as http:
        convo = ok(http.post("/api/conversations", json={"id": identity, "message": {"id": source_id, "text": text}}))
        assistant = convo["messages"][-1]["id"]
        assert http.post(API, json={"id": str(uuid4()), "text": text, "sourceMessageId": assistant}).status_code == 409
        data, memory = add(http, text, sourceMessageId=source_id)
        assert memory["source"] == {"kind": "chat", "messageId": source_id, "conversationId": identity}
        assert ok(http.post(API, json=data)) == memory
        source_path = API + "/" + memory["id"] + "/source"
        assert ok(http.get(source_path)) == {"text": text, "conversationId": identity}
        ok(http.post(API + "/" + memory["id"] + "/delete", json={"revision": memory["revision"]}))
        assert http.get(source_path).status_code == 404
    with session(tmp_path) as http:
        assert entries(http, "pending") == [] and entries(http, "shared") == []
        assert http.post(API, json={**data, "id": str(uuid4())}).status_code == 409
        assert ok(http.get("/api/conversations/" + identity))["messages"][0]["text"] == text


def test_backup_roundtrip_keeps_live_memories_and_deleted_chat_source(tmp_path):
    from test_backups import accepted, inspect_backup, make_backup
    from test_full_restore import app_client, request_restore, wait_restore

    source_id = str(uuid4())
    with app_client(tmp_path) as http:
        _, shared = add(http, "保留这条")
        ok(review(http, "accept", shared))
        ok(http.post("/api/conversations", json={
            "id": str(uuid4()), "message": {"id": source_id, "text": "已停用来源"},
        }))
        _, inactive = add(http, "准备停用", sourceMessageId=source_id)
        ok(review(http, "reject", inactive))
        backup = make_backup(http)
        add(http, "备份之后才创建")
        plan = accepted(inspect_backup(http, backup))
        assert plan["counts"]["backup"]["local_memories"] == 2
        request_restore(http, backup, plan)
        assert wait_restore(http)["state"] == "complete"
        assert [item["text"] for item in entries(http, "shared")] == ["保留这条"]
        assert entries(http, "pending") == []
        assert http.post(API, json={"id": str(uuid4()), "text": "再次创建", "sourceMessageId": source_id}).status_code == 409


def patch_review(http, *entries_and_text):
    return http.patch(API + "/review", json={
        "items": [{**ref(entry), "text": value} for entry, value in entries_and_text],
    })


def test_batch_pending_edit_preserves_scope_and_original_source_across_restart(tmp_path):
    source_id = str(uuid4())
    original = "先用一个例子讲概念"
    with session(tmp_path) as http:
        convo = ok(http.post("/api/conversations", json={
            "id": str(uuid4()), "message": {"id": source_id, "text": original},
        }))
        _, first = add(http, original, sourceMessageId=source_id)
        _, _, agent = create_agent(http, skills=[])
        _, private = add(http, "示例后给练习", agentId=agent["id"])
        second = ok(http.post(API + "/" + private["id"] + "/share", json={"revision": private["revision"]}))
        ok(review(http, "defer", second))
        _, unchanged = add(http, "保留原内容")
        pending = {item["id"]: item for item in entries(http, "pending")}
        second = pending[second["id"]]

        assert ok(patch_review(http, (first, "  先讲例子  "), (second, "再给两道练习"),
                               (unchanged, unchanged["text"]))) == {"updated": 2}
        assert entries(http, "shared") == []
        assert ok(http.get(API + "/summary")) == {"waiting": 2}
        changed = {item["id"]: item for item in entries(http, "pending")}
        new_first, new_second = changed[first["id"]], changed[second["id"]]
        assert (new_first["text"], new_first["revision"]) == ("先讲例子", first["revision"] + 1)
        assert (new_second["text"], new_second["revision"]) == ("再给两道练习", second["revision"] + 1)
        for before, after in ((first, new_first), (second, new_second)):
            assert all(after[field] == before[field] for field in ("agentId", "state", "origin", "source", "reviewOn"))
        assert changed[unchanged["id"]] == unchanged
        assert ok(http.get(f"{API}/{first['id']}/source")) == {"text": original, "conversationId": convo["id"]}

    with session(tmp_path) as http:
        assert {item["id"]: item for item in entries(http, "pending")} == changed
        assert review(http, "accept", first).status_code == 409
        ok(review(http, "accept", new_first))
        assert [row["text"] for row in entries(http, "shared")] == ["先讲例子"]
        assert [row["text"] for row in entries(http, "private")] == ["再给两道练习"]


def test_batch_pending_edit_rejects_stale_active_duplicate_and_blank_items_atomically(tmp_path):
    with session(tmp_path) as http:
        _, first = add(http, "第一条")
        _, second = add(http, "第二条")
        updated = ok(http.put(
            f"{API}/{second['id']}", json={"revision": second["revision"], "text": "另一处修改"},
        ))
        assert patch_review(http, (first, "这条不应改"), (second, "冲突修改")).status_code == 409
        assert patch_review(http, (first, "重选一条"), (first, "重复另一条")).status_code == 422
        assert patch_review(http, (first, "合规文本"), (updated, "   ")).status_code == 422
        assert patch_review(http, (first, "合规文本"), (updated, "汉" * 2001)).status_code == 422
        assert {item["id"]: item for item in entries(http, "pending")} == {first["id"]: first, updated["id"]: updated}
        ok(review(http, "accept", updated))
        [shared] = entries(http, "shared")
        assert patch_review(http, (first, "继续核对"), (shared, "跨范围修改")).status_code == 409
        assert entries(http, "pending") == [first]
        assert entries(http, "shared") == [shared]
