from csv import DictReader
from io import StringIO
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from acceptance import product
from acceptance.server import RunStore, create_app
from acceptance.steps import CheckFailed


TOKEN = "test-only-acceptance-session"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_recording_replay_and_report_do_not_execute_the_case_again(tmp_path):
    calls = []

    def scenario(run):
        calls.append(run.identity)
        with run.step("setup", {"范围": "合成数据"}) as evidence:
            evidence["测试目录"] = "独立"
        with run.step("goal", {"操作": "故意阻止"}):
            raise CheckFailed("本地测试所用的固定失败原因。")

    store = RunStore(tmp_path, execute=scenario)
    with TestClient(create_app(store, TOKEN), base_url="http://127.0.0.1") as http:
        assert http.get("/api/runs").status_code == 401
        assert http.get("/api/runs", headers={**HEADERS, "Origin": "https://example.invalid"}).status_code == 403
        assert http.post("/api/runs", headers=HEADERS, json={"mode": "real"}).status_code == 422
        created = http.post("/api/runs", headers=HEADERS, json={"mode": "simulated"})
        assert created.status_code == 202
        identity = created.json()["id"]
        store.wait()
        original = store.path(identity).read_bytes()
        path = f"/api/runs/{identity}"
        saved = http.get(path, headers=HEADERS).json()
        assert saved["state"] == "failed" and saved["mode"] == "simulated"
        first, second = saved["records"]
        assert first["status"] == "passed" and first["output"] == {"测试目录": "独立"}
        assert first["elapsedMs"] >= 0 and second["status"] == "failed"
        assert second["reason"] == "本地测试所用的固定失败原因。"
        report = http.get(f"{path}/report", headers=HEADERS)
        assert report.status_code == 200
        assert "本地测试所用的固定失败原因。" in report.text
        assert "未验证" in report.text and TOKEN not in report.text
        assert "未验证 · 目标与路线调整" in report.text
        assert "未验证 · 重启后读回路线" in report.text
        assert "未验证 · 课内指令标记已会" in report.text
        assert "未验证 · 重启后读回阶段与测评" in report.text
        assert "未验证 · 知识库检索定位" in report.text
        assert "未验证 · 拒绝失效原文位置" in report.text
        assert "未验证 · 双链改名与反向定位" in report.text
        assert "未验证 · 原件归位与再次保存" in report.text
        assert "未验证 · 重启后原件归位读回" in report.text
        assert "未验证 · 回收站旧路径双链" in report.text
        assert "未验证 · 加密备份找回回收原件" in report.text
        assert "未验证 · 核对回收原件与重启读回" in report.text
        assert "未验证 · 延迟评分与单轮待办" in report.text
        assert "未验证 · 正式复习明细与提交日" in report.text
        assert "未验证 · 单页识别授权与范围" in report.text
        assert "未验证 · 校对冲突与索引" in report.text
        assert "未验证 · 重启恢复未发送预留" in report.text
        assert "未验证 · 重启核对已发送请求" in report.text
        assert http.get(f"{path}/billing", headers=HEADERS).status_code == 409
        assert http.get(path, headers=HEADERS).json() == saved
        assert store.path(identity).read_bytes() == original
        assert len(calls) == 1

    assert RunStore(tmp_path).read(identity) == saved


def test_billing_export_keeps_connection_scope_and_saved_checkpoint_without_writing(tmp_path):
    store = RunStore(tmp_path)
    identity = str(uuid4())
    instant = "2026-11-20T00:08:00+00:00"
    run = {
        "id": identity, "mode": "real", "sample": "sqlite", "state": "running", "startedAt": instant,
        "finishedAt": None, "reason": None, "records": [{
            "step": "real-model", "status": "passed", "input": {}, "startedAt": instant,
            "finishedAt": instant, "elapsedMs": 900, "reason": None,
            "output": {"主模型": {"主机": "127.0.0.1", "服务商": "自定义", "模型": "=1+1"},
                       "复核模型": {"主机": "review.example", "服务商": "自定义", "模型": "review-id"}},
        }],
    }
    store.write(store.path(identity), run)
    checkpoint = {
        "run": identity, "capturedAt": instant, "serviceStopped": False,
        "output": {"计入调用": 2, "调用记录": [
            {"阶段": "route", "模型分工": "主模型", "状态": "succeeded",
             "账本开始时间（UTC）": "2026-11-19T16:08:00Z",
             "服务商回报费用": {"currency": "USD", "amount": "0"}},
            {"阶段": "verify", "模型分工": "独立复核模型", "状态": "sending",
             "估算费用": {"currency": "CNY", "amount": "0.003"}},
        ]},
    }
    store.usage_path(identity).write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
    original_run = store.path(identity).read_bytes()
    original_usage = store.usage_path(identity).read_bytes()
    with TestClient(create_app(store, TOKEN), base_url="http://127.0.0.1") as http:
        path = f"/api/runs/{identity}"
        assert http.get(path + "/billing").status_code == 401
        exported = http.get(path + "/billing", headers=HEADERS)
        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"].startswith("text/csv")
        assert exported.content.startswith(b"\xef\xbb\xbf")
        first, second = list(DictReader(StringIO(exported.content.decode("utf-8-sig"))))
        assert first["运行 ID"] == second["运行 ID"] == identity
        assert first["接入范围"] == "含回环连接"
        assert first["用量采集时间"] == instant
        assert "最后一次落盘" in first["证据范围"]
        assert first["模型分工"] == "主模型" and first["模型"] == "'=1+1"
        assert (first["服务商回报币种"], first["服务商回报金额"]) == ("USD", "0")
        assert second["模型分工"] == "独立复核模型" and second["状态"] == "sending"
        assert second["服务商回报金额"] == second["结算记录时间（UTC）"] == ""
        assert (second["估算币种"], second["估算金额"]) == ("CNY", "0.003")
        for row in (first, second):
            assert row["账单金额"] == row["核对结论"] == ""
        assert TOKEN not in exported.text
        assert "connectionScope" not in http.get(path, headers=HEADERS).json()
        assert "- 接入范围 含回环连接" in http.get(path + "/report", headers=HEADERS).text
    assert store.path(identity).read_bytes() == original_run
    assert store.usage_path(identity).read_bytes() == original_usage



def test_prepared_runtime_keeps_large_resources_independent_of_source(tmp_path, monkeypatch):
    source = tmp_path / "source"
    model = source / "backend" / "voice_models" / "fixture.bin"
    model.parent.mkdir(parents=True)
    (source / "migrations").mkdir()
    (source / "dist").mkdir()
    (source / "backend" / "__pycache__").mkdir()
    with model.open("wb") as asset:
        asset.write(b"course model data")
        asset.truncate(17 * 1024 * 1024)
    monkeypatch.setattr(product, "ROOT", source)
    runtime = tmp_path / "runtime"
    product.prepare_product(runtime)
    staged = runtime / model.relative_to(source)
    assert staged.stat().st_size == model.stat().st_size
    assert not staged.samefile(model)
    assert not (runtime / "backend" / "__pycache__").exists()
    with staged.open("r+b") as snapshot:
        snapshot.write(b"copy")
    with model.open("rb") as original:
        assert original.read(4) == b"cour"
