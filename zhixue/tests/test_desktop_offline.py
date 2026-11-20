from fastapi.testclient import TestClient

from backend.app import create_app


BASE = "/api/desktop-offline"
ITEM = f"{BASE}/items/record"
TIMER = f"{BASE}/timers/record"
AUTH = {"Authorization": "Bearer demo-test-session"}
KEY = {"key": "workspace:review:saved"}
LEDGER = {"key": "ledger"}


def test_desktop_offline_records_survive_app_and_loopback_port_restart(tmp_path):
    workspace = tmp_path / "knowledge"
    store = tmp_path / "offline-study.sqlite3"
    draft = {"key": KEY["key"], "draft": [1, "中文答案"], "kind": "review"}
    ledger = {"actions": [{"id": "local-timer"}]}
    app = create_app(workspace, "demo-test-session", desktop_offline=store)
    with TestClient(app, base_url="http://127.0.0.1:34081", headers=AUTH) as http:
        assert http.get(ITEM, params=KEY).json() == {"revision": 0}
        assert http.put(ITEM, params=KEY, json={"revision": 0, "value": draft}).json() == {"saved": True}
        assert http.put(TIMER, params=LEDGER, json={"revision": 0, "value": ledger}).json() == {"saved": True}

    restarted = create_app(workspace, "demo-test-session", desktop_offline=store)
    with TestClient(restarted, base_url="http://127.0.0.1:34082", headers=AUTH) as http:
        assert http.get(f"{BASE}/items").json() == [draft]
        assert http.get(ITEM, params=KEY).json() == {"revision": 1, "value": draft}
        assert http.get(TIMER, params=LEDGER).json() == {"revision": 1, "value": ledger}
        assert http.put(ITEM, params=KEY, json={"revision": 1}).json() == {"saved": True}
        assert http.put(ITEM, params=KEY, json={"revision": 1, "value": draft}).json() == {"saved": False}
        assert http.get(ITEM, params=KEY).json() == {"revision": 2}
        assert http.get(f"{BASE}/items").json() == []


def test_desktop_offline_records_require_local_session_and_opt_in(tmp_path):
    workspace = tmp_path / "knowledge"
    endpoint = f"{BASE}/items"
    app = create_app(workspace, "demo-test-session")
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as http:
        assert http.get(endpoint).status_code == 404

    opt_in = create_app(workspace, "demo-test-session", desktop_offline=tmp_path / "offline-study.sqlite3")
    with TestClient(opt_in, base_url="http://127.0.0.1") as http:
        assert http.get(endpoint).status_code == 401
        assert http.get(endpoint, headers={**AUTH, "Origin": "https://outside.example"}).status_code == 403
        assert http.get(endpoint, headers=AUTH).json() == []
        assert http.get(TIMER, params={"key": "other"}, headers=AUTH).status_code == 422
        assert http.put(ITEM, params=KEY, headers=AUTH, json={"revision": -1}).status_code == 422
