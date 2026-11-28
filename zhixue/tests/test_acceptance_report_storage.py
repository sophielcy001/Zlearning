from errno import EACCES
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from acceptance.server import RunStore, create_app
from acceptance.usage import read_usage_checkpoint, write_usage_checkpoint
from backend.db import open_db, upgrade


def access_error(winerror):
    error = PermissionError(EACCES, "injected replacement rejection")
    if winerror is not None:
        error.winerror = winerror
    return error


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_busy_report_replacement_keeps_completed_step_without_rerunning(tmp_path, monkeypatch, winerror):
    executed = []

    def scenario(run):
        with run.step("setup", {"模式": "本机"}) as output:
            executed.append(run.identity)
            output["结果"] = "已完成"

    original_replace = Path.replace
    rejected = False

    def replace(source, destination):
        nonlocal rejected
        if destination.parent == tmp_path and destination.suffix == ".json" and not rejected:
            saved = json.loads(source.read_text(encoding="utf-8"))
            if saved["records"] and saved["records"][0]["status"] == "passed":
                rejected = True
                raise access_error(winerror)
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace)
    store = RunStore(tmp_path, execute=scenario)
    token = "report-fixture-token"
    with TestClient(create_app(store, token), base_url="http://127.0.0.1") as http:
        started = http.post("/api/runs", json={"mode": "simulated"},
                            headers={"Authorization": f"Bearer {token}"})
        assert started.status_code == 202
        identity = started.json()["id"]
        store.wait()
    saved = RunStore(tmp_path).read(identity)
    assert rejected and executed == [identity]
    assert saved["state"] == "passed" and saved["finishedAt"]
    record, = saved["records"]
    assert record["status"] == "passed" and record["output"] == {"结果": "已完成"}
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("winerror", [5, None])
def test_denied_report_preserves_original_and_removes_temp(tmp_path, monkeypatch, winerror):
    store = RunStore(tmp_path)
    destination = store.path(uuid4())
    store.write(destination, {"state": "before"})
    original = destination.read_bytes()
    attempts = []
    error = access_error(winerror)

    def reject(source, target):
        assert target == destination and source.parent == destination.parent
        attempts.append(source.read_bytes())
        assert destination.read_bytes() == original
        raise error

    monkeypatch.setattr(Path, "replace", reject)
    with pytest.raises(PermissionError) as caught:
        store.write(destination, {"state": "after"})
    assert caught.value is error
    assert destination.read_bytes() == original and not list(tmp_path.glob("*.tmp"))
    if winerror is None:
        assert len(attempts) == 1
    else:
        assert 1 < len(attempts) < 10
        assert len(set(attempts)) == 1


def test_usage_checkpoint_survives_rejected_replacement(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = open_db(workspace)
    try:
        upgrade(engine)
    finally:
        engine.dispose()
    database = workspace / "zhixue.sqlite3"
    checkpoint = tmp_path / "sample.usage"
    write_usage_checkpoint(checkpoint, database)
    assert read_usage_checkpoint(checkpoint)["serviceStopped"] is False
    original_replace = Path.replace
    rejected = False

    def replace(source, destination):
        nonlocal rejected
        if destination == checkpoint and not rejected:
            rejected = True
            raise access_error(5)
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace)
    write_usage_checkpoint(checkpoint, database, final=True)
    saved = read_usage_checkpoint(checkpoint)
    assert rejected and saved["serviceStopped"] is True
    assert saved["run"] == "sample" and saved["output"]["调用记录"] == []
    assert not list(tmp_path.glob("*.tmp"))

