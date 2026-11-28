from uuid import uuid4

from test_chat_model import confirmed, draft, worker
from test_model_study import ok, session, setup_model
from test_professional_agent import AGENTS, create_agent
from test_skill_library import NEW, SOURCE, confirm_update, install, upload


def document(name, description):
    return f"---\nname: {name}\ndescription: {description}\n---\nGuidance for {name}.\n".encode()


def test_auto_matches_only_authorized_skills_from_current_question(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        first = install(http)
        hidden = install(http, document("private-print", "整理试印前检查事项和字行间距。"))
        legal = install(http, document("paper-storage", "Archival paper storage and humidity."))
        _, _, agent = create_agent(http, skills=[first["name"], legal["name"]])
        route = f"/api/chat-model/{uuid4()}"
        common = {"agentId": agent["id"], "autoSkill": True}
        for question, expected in [
            ("试印前检查怎么安排？", first),
            ("Explain paper storage", legal),
            ("Use PAPER-STORAGE", legal),
            ("字行间距怎么量？", None),
            ("今天几点？", None),
        ]:
            preview, _ = confirmed(http, route, draft(question, **common))
            assert preview["agent"]["skillMode"] == "automatic"
            pinned = preview["agent"]["skill"]
            assert (pinned["checksum"] if pinned else None) == (expected["checksum"] if expected else None)
            assert hidden["checksum"] not in str(preview)
        assert ok(http.get(route.replace("/chat-model/", "/conversations/").rsplit("/", 1)[0]))["items"] == []
        assert not worker(tmp_path, [])


def test_ambiguous_auto_match_falls_back_and_manual_modes_keep_working(tmp_path):
    with session(tmp_path) as http:
        setup_model(http)
        first = install(http)
        second = install(http, document("other-print", first["description"]))
        _, _, agent = create_agent(http, skills=[first["name"], second["name"]])
        route = f"/api/chat-model/{uuid4()}"
        message = draft("整理试印前检查事项", agentId=agent["id"])
        automatic, _ = confirmed(http, route, {**message, "autoSkill": True})
        manual, _ = confirmed(http, route, {**message, "skill": second["name"]})
        disabled, _ = confirmed(http, route, message)
        assert automatic["agent"]["skill"] is None
        assert manual["agent"]["skill"]["checksum"] == second["checksum"]
        assert disabled["agent"]["skill"] is None
        assert "skillMode" not in disabled["agent"]
        assert http.post(route + "/preview", json={**message, "autoSkill": True, "skill": first["name"]}).status_code == 422
        assert http.post(route + "/preview", json=draft("问题", autoSkill=True)).status_code == 409


def test_auto_selection_is_bound_to_confirmation_and_records_exact_sent_version(tmp_path):
    identity = str(uuid4())
    route = f"/api/chat-model/{identity}"
    calls = []
    with session(tmp_path) as http:
        setup_model(http)
        first = install(http)
        data, _, agent = create_agent(http)
        question = draft("怎么做试印前检查？", agentId=agent["id"], autoSkill=True)
        old, stale = confirmed(http, route, question)
        assert old["agent"]["skill"]["checksum"] == first["checksum"]
        new = ok(confirm_update(http, NEW, ok(upload(http, "/preview", NEW))))
        assert http.post(route + "/send", json=stale).status_code == 409
        fresh, payload = confirmed(http, route, question)
        assert fresh["agent"]["skill"]["checksum"] == new["checksum"]
        ok(http.post(route + "/send", json=payload))

        edit = {**data, "revision": agent["revision"], "skills": []}
        revision = ok(http.post(AGENTS + "/preview", json=edit))
        ok(http.post(AGENTS, json={**edit, "confirmation": revision["confirmation"]}))
        after, _ = confirmed(http, route, {**question, "id": str(uuid4())})
        assert after["agent"]["skill"] is None

    assert worker(tmp_path, calls)
    assert len(calls) == 1 and new["checksum"] in calls[0][1] and first["checksum"] not in calls[0][1]
    with session(tmp_path) as http:
        trace = ok(http.get(f"/api/conversations/{identity}"))["messages"][-1]["modelReply"]["agent"]
        assert trace["skillMode"] == "automatic"
        assert trace["skill"]["checksum"] == new["checksum"]
        assert "source" not in trace["skill"] and SOURCE.decode() not in str(trace)