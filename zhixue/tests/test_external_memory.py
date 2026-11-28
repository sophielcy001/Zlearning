from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import create_app
from test_local_memory import API as MEMORY, entries, review
from test_model_study import SESSION, ok
from test_professional_agent import create_agent


API = "/api/external-memory"
ADD = "/v3/memories/add/"
LIST = "/v3/memories/?page=1&page_size=100"
KEY = "fixture-external-token"
TEXT = "每次先独立尝试一步，再看解答。"


class Credentials:
    def __init__(self):
        self.items = {}

    def get(self, reference):
        return self.items.get(reference)

    def put(self, reference, value):
        self.items[reference] = value


class Mem0:
    def __init__(self):
        self.calls = []
        self.records = {}
        self.fail_delete = False
        self.pretend_delete = False
        self.show_unknown = True
        self.lose_receipt = False
        self.during_upload = lambda: None

    def request(self, method, path, key, payload=None):
        assert key == KEY
        self.calls.append((method, path, payload))
        if method == "POST" and path == LIST:
            user = payload["filters"]["user_id"]
            matching = [{"id": key, "user_id": user} for key, record in self.records.items()
                        if record["user_id"] == user and self.show_unknown]
            return 200, {"count": len(matching), "next": None, "results": matching}
        if method == "POST" and path == ADD:
            memory = str(uuid4())
            self.records[memory] = payload
            self.during_upload()
            if self.lose_receipt:
                raise OSError("response lost")
            return 200, {"status": "SUCCEEDED", "results": [{"id": memory}]}
        memory = path.split("/")[-2]
        if method == "DELETE":
            if self.fail_delete:
                return 503, {"error": "unavailable"}
            if self.pretend_delete:
                return 200, {"message": "Memory deleted successfully!"}
            if self.records.pop(memory, None) is not None:
                return 200, {"message": "Memory deleted successfully!"}
        elif method == "GET" and memory in self.records:
            return 200, {"id": memory}
        return 404, {"error": "Memory not found!"}


def client(folder, vault, remote):
    return TestClient(create_app(folder, SESSION, external_memory_credentials=vault,
                                 external_memory_transport=remote.request),
                      base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {SESSION}"})


def settings(http, revision=0, scopes=None, key=KEY):
    return http.put(API + "/settings", json={"revision": revision, "scopes": scopes or [], "apiKey": key})


def memory(http, agent=None, text=TEXT, source=None):
    return ok(http.post(MEMORY, json={"id": str(uuid4()), "text": text, "agentId": agent, "sourceMessageId": source}))


def share(http, entry):
    ok(review(http, "accept", entry))
    return next(row for row in entries(http, "shared") if row["id"] == entry["id"])


def prepare(http, entry):
    request = {"id": str(uuid4()), "memoryId": entry["id"], "revision": entry["revision"]}
    return request, http.post(API + "/preview", json=request)


def confirm(http, entry):
    request, response = prepare(http, entry)
    preview = ok(response)
    return request, preview, {**request, "confirmation": preview["confirmation"]}


def worker(folder, vault, remote):
    from backend.db import open_db
    from backend.external_memory import step

    engine = open_db(folder)
    try:
        return step(engine, vault, remote.request)
    finally:
        engine.dispose()


def test_external_export_checks_scope_and_sends_only_confirmed_memory(tmp_path):
    vault, remote = Credentials(), Mem0()
    with client(tmp_path, vault, remote) as http:
        assert ok(http.get(API))["scopes"] == []
        pending = memory(http)
        status = ok(settings(http, scopes=["shared"]))
        assert status["scopes"] == ["shared"] and status["connected"]
        assert KEY not in str(status)
        assert prepare(http, pending)[1].status_code == 409
        _, _, agent = create_agent(http, skills=[])
        assert prepare(http, memory(http, agent["id"]))[1].status_code == 409
        entry = share(http, pending)
        request, preview, submitted = confirm(http, entry)
        assert preview["text"] == TEXT and preview["recipient"] == "https://api.mem0.ai"
        assert preview["scope"] == "shared"
        assert not worker(tmp_path, vault, remote)
        queued = ok(http.post(API + "/copies", json=submitted))
        assert ok(http.post(API + "/copies", json=submitted)) == queued
        assert worker(tmp_path, vault, remote)
        copies = ok(http.get(API))["copies"]
        assert len(copies) == 1 and copies[0]["state"] == "synced"
        assert not worker(tmp_path, vault, remote)
        sent = [payload for method, path, payload in remote.calls if method == "POST" and path == ADD]
        assert sent == [{"messages": [{"role": "user", "content": TEXT}], "infer": False,
                         "immutable": True, "user_id": "zhixue-" + request["id"]}]
        assert request["memoryId"] not in str(sent)
        assert KEY.encode() not in (tmp_path / "zhixue.sqlite3").read_bytes()

def test_local_delete_blocks_old_source_before_verified_external_deletion(tmp_path):
    vault, remote = Credentials(), Mem0()
    with client(tmp_path, vault, remote) as http:
        ok(settings(http, scopes=["shared"]))
        source, conv = str(uuid4()), str(uuid4())
        ok(http.post("/api/conversations", json={"id": conv, "message": {"id": source, "text": "独立的原话"}}))
        entry = share(http, memory(http, source=source))
        _, _, submitted = confirm(http, entry)
        queued = ok(http.post(API + "/copies", json=submitted))
        assert worker(tmp_path, vault, remote)
        remote_id = next(iter(remote.records))
        ok(http.post(MEMORY + "/" + entry["id"] + "/delete", json={"revision": entry["revision"]}))
        assert not entries(http, "shared")
        assert http.get(MEMORY + "/" + entry["id"] + "/source").status_code == 404
        assert ok(http.get("/api/conversations/" + conv))["messages"][0]["text"] == "独立的原话"
        assert http.post(MEMORY, json={"id": str(uuid4()), "text": TEXT, "sourceMessageId": source}).status_code == 409
        assert ok(http.get(API))["copies"][0]["state"] == "pending_delete"
        remote.fail_delete = True
        assert worker(tmp_path, vault, remote)
        copy = ok(http.get(API))["copies"][0]
        assert copy["state"] == "pending_delete" and not copy["working"] and copy["error"]
        assert remote_id in remote.records and settings(http, revision=1, scopes=[], key="different-token").status_code == 409

    with client(tmp_path, vault, remote) as http:
        remote.fail_delete = False
        ok(http.post(API + "/copies/" + queued["id"] + "/retry"))
        assert worker(tmp_path, vault, remote)
        assert ("DELETE", f"/v1/memories/{remote_id}/", None) in remote.calls
        assert ("GET", f"/v1/memories/{remote_id}/", None) in remote.calls
        assert not remote.records and not entries(http, "shared")
        assert ok(http.get(API))["copies"][0]["state"] == "removed"


def test_scope_change_rejects_stale_preview_stops_queued_upload_and_retires_copies(tmp_path):
    vault, remote = Credentials(), Mem0()
    with client(tmp_path, vault, remote) as http:
        initial = ok(settings(http, scopes=["private", "shared"]))
        _, _, agent = create_agent(http, skills=[])
        private = memory(http, agent["id"])
        _, _, payload = confirm(http, private)
        ok(http.post(API + "/copies", json=payload))
        assert worker(tmp_path, vault, remote)
        shared = share(http, memory(http, text="准备分享的记忆"))
        _, _, stale = confirm(http, shared)
        queued = share(http, memory(http, text="排队等待的记忆"))
        _, _, to_send = confirm(http, queued)
        ok(http.post(API + "/copies", json=to_send))
        ok(settings(http, revision=initial["revision"], scopes=[], key=None))
        assert http.post(API + "/copies", json=stale).status_code == 409
        observed = {row["memoryId"]: row["state"] for row in ok(http.get(API))["copies"]}
        assert observed == {private["id"]: "pending_delete", queued["id"]: "cancelled"}
        assert {row["id"] for row in entries(http, "shared")} == {shared["id"], queued["id"]}
        assert {row["id"] for row in entries(http, "private")} == {private["id"]}
        assert worker(tmp_path, vault, remote)
        assert not remote.records
        assert sum(path == ADD for _, path, _ in remote.calls) == 1


def test_lost_upload_receipt_during_local_delete_stays_pending_until_visible(tmp_path):
    vault, remote = Credentials(), Mem0()
    with client(tmp_path, vault, remote) as http:
        ok(settings(http, scopes=["shared"]))
        entry = share(http, memory(http))
        _, _, submitted = confirm(http, entry)
        queued = ok(http.post(API + "/copies", json=submitted))
        remote.lose_receipt = True
        remote.show_unknown = False
        remote.during_upload = lambda: ok(http.post(MEMORY + "/" + entry["id"] + "/delete",
                                                    json={"revision": entry["revision"]}))
        assert worker(tmp_path, vault, remote)
        assert ok(http.get(API))["copies"][0]["state"] == "pending_delete"
        ok(http.post(API + "/copies/" + queued["id"] + "/retry"))
        assert worker(tmp_path, vault, remote)
        copy = ok(http.get(API))["copies"][0]
        assert copy["state"] == "pending_delete" and not copy["working"] and copy["error"]
        assert len(remote.records) == 1
        remote.show_unknown = True
        ok(http.post(API + "/copies/" + queued["id"] + "/retry"))
        assert worker(tmp_path, vault, remote)
        assert not remote.records
        assert ok(http.get(API))["copies"][0]["state"] == "removed"
        assert sum(path == ADD for _, path, _ in remote.calls) == 1


def test_local_edit_retires_old_remote_copy_and_delete_needs_confirmed_absence(tmp_path):
    vault, remote = Credentials(), Mem0()
    with client(tmp_path, vault, remote) as http:
        ok(settings(http, scopes=["shared"]))
        entry = share(http, memory(http))
        _, _, submitted = confirm(http, entry)
        queued = ok(http.post(API + "/copies", json=submitted))
        assert worker(tmp_path, vault, remote)
        edited = ok(http.put(MEMORY + "/" + entry["id"], json={"revision": entry["revision"], "text": "改后的本地记忆"}))
        assert prepare(http, edited)[1].status_code == 409
        remote.pretend_delete = True
        assert worker(tmp_path, vault, remote)
        assert ok(http.get(API))["copies"][0]["state"] == "pending_delete"
        remote.pretend_delete = False
        ok(http.post(API + "/copies/" + queued["id"] + "/retry"))
        assert worker(tmp_path, vault, remote)
        assert ok(http.get(API))["copies"][0]["state"] == "removed"
        assert entries(http, "shared")[0]["text"] == edited["text"]
        assert prepare(http, edited)[1].is_success


def test_same_install_restore_retains_live_deletion_and_holds_new_exports(tmp_path):
    from backend.restore_runtime import RestoreRuntime, attach_restore
    from test_backups import PASSWORD, accepted, inspect_backup, make_backup
    from test_full_restore import request_restore, wait_restore

    vault, remote = Credentials(), Mem0()
    runtime = RestoreRuntime(lambda: None, lambda: None)
    http = client(tmp_path, vault, remote)
    attach_restore(http.app, runtime)
    with http:
        try:
            ok(settings(http, scopes=["shared"]))
            entry = share(http, memory(http))
            _, _, payload = confirm(http, entry)
            ok(http.post(API + "/copies", json=payload))
            assert worker(tmp_path, vault, remote)
            backup = make_backup(http)
            from backend.backup_snapshot import read_backup
            from contextlib import closing
            import sqlite3

            unpacked = tmp_path / "fixture-unpacked"
            unpacked.mkdir()
            from backend.backup_catalog import BackupCatalog
            from uuid import UUID

            read_backup(BackupCatalog(tmp_path).get(UUID(backup["id"])).path, PASSWORD, destination=unpacked)
            with closing(sqlite3.connect(unpacked / "zhixue.sqlite3")) as db:
                assert not db.execute("SELECT * FROM external_memory_connection").fetchall()
                assert not db.execute("SELECT * FROM external_memory_copies").fetchall()
            ok(http.post(MEMORY + "/" + entry["id"] + "/delete", json={"revision": entry["revision"]}))
            remote.fail_delete = True
            assert worker(tmp_path, vault, remote)
            other = share(http, memory(http, text="恢复前排队的新副本"))
            _, _, next_payload = confirm(http, other)
            ok(http.post(API + "/copies", json=next_payload))
            plan = accepted(inspect_backup(http, backup))
            request_restore(http, backup, plan)
            result = wait_restore(http)
            assert result["state"] == "complete", result
            status = ok(http.get(API))
            assert status["connected"] and status["scopes"] == []
            copies = {item["memoryId"]: item for item in status["copies"]}
            assert copies[other["id"]]["state"] == "cancelled"
            assert copies[entry["id"]]["state"] == "pending_delete"
            assert not prepare(http, entry)[1].is_success
            remote.fail_delete = False
            ok(http.post(API + "/copies/" + payload["id"] + "/retry"))
            assert worker(tmp_path, vault, remote)
            assert not remote.records
            assert sum(path == ADD for _, path, _ in remote.calls) == 1
        finally:
            runtime.close()
