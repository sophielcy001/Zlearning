from uuid import uuid4

from test_library_folders import create
from test_model_study import ok, session
from test_professional_agent import AGENTS, create_agent
from test_skill_library import DETAIL, NEW, confirm_update, install, upload


CATALOG = "/api/capabilities"


def listed(http, kind):
    return ok(http.get(CATALOG, params={"kind": kind}))["items"]


def authorize(http, data, agent, **changes):
    update = {**data, "revision": agent["revision"], **changes}
    preview = ok(http.post(AGENTS + "/preview", json=update))
    return ok(http.post(AGENTS, json={**update, "confirmation": preview["confirmation"]}))


def test_catalog_reports_current_tool_grants_and_real_folder_availability(tmp_path):
    with session(tmp_path) as http:
        [tool] = listed(http, "tool")
        assert tool["id"] == "library-search" and tool["kind"] == "tool"
        assert tool["status"] == "installed" and tool["agents"] == []
        assert tool["source"] and tool["version"] and tool["permission"] and tool["dependencies"]
        assert listed(http, "mcp") == [] and listed(http, "skill") == []

        allowed = create(http, "有效目录")
        stale = create(http, "待移除")
        data, _, agent = create_agent(http, skills=[], knowledgeFolders=[allowed])
        _, _, other = create_agent(http, name="过期授权", skills=[], knowledgeFolders=[stale])
        (tmp_path / stale).rmdir()
        [tool] = listed(http, "tool")
        assert tool["status"] == "enabled"
        assert {grant["id"]: (grant["available"], grant["folders"]) for grant in tool["agents"]} == {
            agent["id"]: (True, [allowed]), other["id"]: (False, [stale]),
        }
        authorize(http, data, agent, knowledgeFolders=[])
        [tool] = listed(http, "tool")
        assert tool["status"] == "unavailable"
        assert [grant["id"] for grant in tool["agents"]] == [other["id"]]


def test_skill_catalog_tracks_grants_and_available_newer_version_after_rollback(tmp_path):
    with session(tmp_path) as http:
        first = install(http)
        [entry] = listed(http, "skill")
        assert entry["status"] == "installed" and entry["canUpdate"] is False
        assert entry["agents"] == []
        assert entry["name"] == first["name"] and "source" in entry

        _, _, agent = create_agent(http, id=str(uuid4()))
        [entry] = listed(http, "skill")
        assert entry["status"] == "enabled"
        assert [(row["id"], row["name"]) for row in entry["agents"]] == [(agent["id"], agent["name"])]
        latest = ok(confirm_update(http, NEW, ok(upload(http, "/preview", NEW))))
        assert listed(http, "skill")[0]["version"] == "2.0"
        target = ok(http.get(DETAIL + "/versions/" + first["checksum"]))
        ok(http.post(DETAIL + "/rollback", json={"checksum": first["checksum"], "confirmation": target["comparison"]["confirmation"]}))
        [entry] = listed(http, "skill")
        assert entry["version"] == "1.0" and entry["canUpdate"] is True
        assert entry["checksum"] == first["checksum"] and entry["checksum"] != latest["checksum"]
        assert entry["agents"][0]["id"] == agent["id"]