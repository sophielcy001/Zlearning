from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from time import sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from acceptance.live_config import LiveInput, SAMPLES
from acceptance.server import RunStore, create_app
from acceptance.simulation import ASSESSMENT, GRADE


TOKEN = "acceptance-live-test-session"
KEY = "a-test-key-that-must-not-appear"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def profiles():
    return {
        "sample": "sqlite", "confirmed": True,
        "main": {"name": "主模型", "provider": "custom", "protocol": "chat-completions",
                 "endpoint": "http://127.0.0.1:9110/v1", "model": "main-test", "apiKey": KEY},
        "review": {"name": "独立复核模型", "provider": "custom", "protocol": "chat-completions",
                   "endpoint": "http://127.0.0.1:9111/v1", "model": "review-test"},
    }


def test_live_mode_requires_confirmation_and_keeps_credentials_out_of_history(tmp_path):
    handed_off = []

    def execute(run, selection):
        handed_off.append(selection.sample)
        with run.step("real-publish", {"样本": "SQLite"}) as result:
            key = selection.main.apiKey.get_secret_value()
            result["正文"] = f"模拟服务异常回显 {key}"

    store = RunStore(tmp_path, execute_live=execute)
    with TestClient(create_app(store, TOKEN), base_url="http://127.0.0.1") as http:
        definition = http.get("/api/definition", headers=HEADERS).json()
        assert {sample["id"] for sample in definition["live"]["samples"]} == {"sqlite", "si"}
        assert definition["live"]["maxCalls"] == 12
        no_consent = http.post("/api/real-runs", headers=HEADERS, json={**profiles(), "confirmed": False})
        assert no_consent.status_code == 422 and KEY not in no_consent.text
        invalid = profiles()
        invalid["main"]["apiKey"] = f" {KEY}"
        rejected = http.post("/api/real-runs", headers=HEADERS, json=invalid)
        assert rejected.status_code == 422 and KEY not in rejected.text
        duplicate = profiles()
        duplicate["review"]["model"] = "main-test"
        assert http.post("/api/real-runs", headers=HEADERS, json=duplicate).status_code == 422
        assert http.get("/api/runs", headers=HEADERS).json() == []
        assert handed_off == []

        fresh = http.post("/api/real-runs", headers=HEADERS, json=profiles())
        assert fresh.status_code == 202, fresh.text
        store.wait()
        identity = fresh.json()["id"]
        url = f"/api/runs/{identity}"
        raw = http.get(url, headers=HEADERS)
        run = raw.json()
        assert run["mode"] == "real" and run["state"] == "passed"
        assert KEY not in raw.text and "已隐藏凭据" in raw.text
        assert KEY not in (tmp_path / f"{identity}.json").read_text(encoding="utf-8")
        report = http.get(f"{url}/report", headers=HEADERS)
        assert "真实模型" in report.text and "人工抽查未完成" in report.text
        assert KEY not in report.text

        checks = {"facts": "confirmed", "evidence": "confirmed", "boundary": "disputed"}
        assessed = http.put(f"{url}/quality", json={"checks": checks}, headers=HEADERS)
        assert assessed.status_code == 200
        updated = http.get(url, headers=HEADERS).json()
        assert updated["records"] == run["records"]
        assert updated["quality"]["checks"] == checks
        assert "有偏差" in http.get(f"{url}/report", headers=HEADERS).text
        assert handed_off == ["sqlite"]
        assert json.loads((tmp_path / f"{identity}.json").read_text(encoding="utf-8")) == updated


def encoded_reply(reply):
    return {"choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 21, "completion_tokens": 12}}


@contextmanager
def model_fixture(case, echo=False, *, abandon_reply=None, draft_sections=1, objective_delay=0):
    sample = SAMPLES[case]
    traffic = []
    quote = {"source": "S1", "quote": sample.source}
    route = {"stages": [{"name": "基础", "lessons": [
        {"key": "example", "title": sample.title, "outcome": sample.outcome},
        {"key": "more", "title": "延伸示例", "outcome": "独立完成例题", "prerequisites": ["example"]},
    ]}]}
    draft = {"sections": [
        {"title": "规则和例题" if index == 0 else f"规则和例题 {index + 1}",
         "text": sample.source, "checks": ["例题计算"]}
        for index in range(draft_sections)
    ]}
    finding = {"sections": [
        {"index": index, "verdict": "supported", "reason": "样本资料摘要包含所需内容。",
         "citations": [quote], "dependsOn": []}
        for index in range(draft_sections)
    ]}
    answer = {"answer": sample.answer, "support": "grounded", "citations": [quote]}
    addition = {"section": {"title": "答疑补充", "text": sample.answer, "checks": ["例题计算"]}}
    reviewed_addition = {"verdict": "supported", "reason": "问答可定位到摘要。", "citations": [quote]}
    assessment = json.loads(json.dumps(ASSESSMENT))
    for question in assessment["objectives"][0]["group"]["questions"]:
        question["quote"] = sample.source
    grade = {**GRADE, "explanation": "本机接口测试返回的评价。" + (KEY if echo else "")}
    if case == "si":
        grade["items"] = [dict(item, met=False) for item in GRADE["items"]]
    phases = {
        "制定学习路线": ("main-test", route),
        "起草一个知识点": ("main-test", draft),
        "复核材料 JSON": ("review-test", finding),
        "答疑上下文 JSON": ("main-test", answer),
        "整理同一节课的答疑": ("main-test", addition),
        "独立核验学完后的答疑补充": ("review-test", reviewed_addition),
        "拟定可独立检验": ("main-test", assessment),
        "评价整组答案": ("main-test", grade),
    }

    class ModelServer(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = payload["messages"][0]["content"]
            marker = next((text for text in phases if text in prompt), None)
            traffic.append((marker, payload["model"], self.headers["Authorization"], KEY in prompt))
            expected, result = phases.get(marker, (None, None))
            if payload["model"] != expected:
                self.send_response(400)
                self.end_headers()
                return
            if abandon_reply is not None and abandon_reply(marker):
                return
            if objective_delay and marker == "拟定可独立检验":
                sleep(objective_delay)
            wire = json.dumps(encoded_reply(result), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, _format, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), ModelServer) as models:
        model_thread = Thread(target=models.serve_forever, daemon=True)
        model_thread.start()
        try:
            yield f"http://127.0.0.1:{models.server_port}/v1", traffic, list(phases)
        finally:
            models.shutdown()
            model_thread.join()


@pytest.mark.parametrize("case,echo", [("sqlite", False), ("si", False), ("sqlite", True)])
def test_live_environment_uses_the_real_worker_and_http_credential_flow(tmp_path, case, echo):
    sample = SAMPLES[case]
    with model_fixture(case, echo) as (endpoint, traffic, markers):
        data = profiles()
        data["sample"] = case
        for role in ("main", "review"):
            data[role]["endpoint"] = endpoint
            data[role]["apiKey"] = KEY + ("-review" if role == "review" else "")
        store = RunStore(tmp_path)
        with TestClient(create_app(store, TOKEN), base_url="http://127.0.0.1") as http:
            fresh = http.post("/api/real-runs", headers=HEADERS, json=data)
            assert fresh.status_code == 202, fresh.text
            store.wait()
            identity = fresh.json()["id"]
            run = store.read(identity)
            assert run["state"] == ("failed" if echo else "passed"), [(entry["step"], entry["status"], entry["reason"])
                                              for entry in run["records"]]
            assert not list(tmp_path.glob("*.usage"))
            records = {record["step"]: record["output"] for record in run["records"]}
            assert records["real-usage"]["计入调用"] == 9
            assert records["real-usage"]["收到用量的调用"] == 9
            assert records["real-usage"]["费用未知的调用"] == 9
            assert records["real-usage"]["按币种汇总"] == []
            details = records["real-usage"]["调用记录"]
            assert [row["模型分工"] for row in details] == [
                "独立复核模型" if model == "review-test" else "主模型" for _, model, _, _ in traffic
            ]
            for row in details:
                registered = datetime.fromisoformat(row["账本开始时间（UTC）"])
                settled = datetime.fromisoformat(row["结算记录时间（UTC）"])
                assert registered.utcoffset().total_seconds() == 0 and settled >= registered
            if echo:
                assert records["real-usage"]["调用记录"][-1]["状态"] == "invalid_response"
                assert "real-schedule" not in records
            else:
                assert records["real-review"]["评价"] == ("passed" if case == "sqlite" else "failed")
                assert records["real-schedule"]["复习阶段"] == (1 if case == "sqlite" else 0)
            assert sample.source in records["real-publish"]["引用原句"]
            report = http.get(f"/api/runs/{identity}/report", headers=HEADERS).text
            assert "账本开始时间（UTC）" in report and "独立复核模型" in report and KEY not in report
            assert [marker for marker, *_ in traffic] == [*markers[:-1], "拟定可独立检验", "评价整组答案"]
            assert all(model == ("review-test" if marker in ("复核材料 JSON", "独立核验学完后的答疑补充")
                            else "main-test") and "Bearer " + KEY + (
                "-review" if model == "review-test" else ""
            ) == authorization and not echoed for marker, model, authorization, echoed in traffic)


def test_live_budget_waits_for_all_serial_assessment_jobs(tmp_path):
    # Five successful responses take over 42 seconds in aggregate at concurrency one.
    with model_fixture("sqlite", draft_sections=4, objective_delay=9) as (endpoint, traffic, markers):
        data = profiles()
        data["main"]["endpoint"] = data["review"]["endpoint"] = endpoint
        price = {"currency": "CNY", "inputPerMillion": "0", "outputPerMillion": "0"}
        data["budget"] = {"limit": {"currency": "CNY", "amount": "1"},
                          "mainPricing": price, "reviewPricing": price}
        store = RunStore(tmp_path)
        with TestClient(create_app(store, TOKEN), base_url="http://127.0.0.1") as http:
            accepted = http.post("/api/real-runs", headers=HEADERS, json=data)
            assert accepted.status_code == 202, accepted.text
            store.wait()
            run = store.read(accepted.json()["id"])
            records = {item["step"]: item for item in run["records"]}
            review = records["real-review"]
            assert review["output"]["出题依据片段数"] == 5
            assert run["state"] == "passed", (run["reason"], review, [phase for phase, *_ in traffic])
            assert review["status"] == "passed" and review["elapsedMs"] >= 45000
            assert records["real-schedule"]["output"]["复习阶段"] == 1
            usage = records["real-usage"]["output"]
            assert usage["计入调用"] == usage["收到用量的调用"] == 12
            assert usage["待结算调用"] == 0
            assert [phase for phase, *_ in traffic] == [*markers[:-2], *([markers[-2]] * 5), markers[-1]]


def test_live_transport_caps_concurrent_outbound_attempts(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from acceptance.live_runtime import capped_transport

    attempts = []

    def deliver(address, headers, payload):
        attempts.append(payload["request"])
        return 200, b"{}"

    monkeypatch.setattr("acceptance.live_runtime.post_json", deliver)
    send = capped_transport(2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda number: send("http://127.0.0.1:1", {}, {"request": number})[0], range(6)))
    assert sorted(statuses) == [200, 200, 429, 429, 429, 429]
    assert len(attempts) == 2


def test_live_transport_rechecks_shutdown_after_the_usage_checkpoint(monkeypatch):
    from threading import Event

    from acceptance.live_runtime import capped_transport
    from backend.model_protocols import TransportNotSent

    shutdown = Event()
    outbound = []
    monkeypatch.setattr("acceptance.live_runtime.post_json", lambda *args: outbound.append(args))
    send = capped_transport(2, shutdown.set, stopped=shutdown)

    with pytest.raises(TransportNotSent):
        send("http://127.0.0.1:1", {}, {})
    with pytest.raises(TransportNotSent):
        send("http://127.0.0.1:1", {}, {})
    assert outbound == []


def test_live_report_preserves_ledger_costs_and_leaves_missing_charges_unknown(tmp_path):
    from acceptance.live_config import LiveInput
    from acceptance.live_scenario import save_usage
    from acceptance.server import report_text
    from test_model_costs import WHEN, connection, ledger_client, reply, set_pricing
    from test_model_study import PLAN, consent_request, ok, run

    folder = tmp_path / 'workspace'
    with ledger_client(folder) as http:
        native = connection(http, 'https://openrouter.ai/api/v1')
        local = connection(http)
        set_pricing(http, local, '2', '4')
        for model, usage in (
            (native, {'prompt_tokens': 120, 'completion_tokens': 90, 'cost': 0.012, 'is_byok': False}),
            (native, {'prompt_tokens': 120, 'completion_tokens': 90, 'cost': 0, 'is_byok': False}),
            (local, {'prompt_tokens': 120, 'completion_tokens': 90}),
            (local, {'prompt_tokens': 120, 'completion_tokens': 90, 'prompt_cache_hit_tokens': 12}),
        ):
            ok(http.patch('/api/models/roles', json={'main': model['id']}))
            endpoint, goal, preview = consent_request(http, 1)
            ok(http.post(endpoint, json={**goal, 'modelConsent': preview['consent']}))
            assert run(folder, lambda *_: reply(json.dumps(PLAN), usage), now=lambda: WHEN)
        ledger = ok(http.get('/api/generation/usage'))['summary']

    store = RunStore(tmp_path / 'reports', execute_live=lambda run, _: save_usage(run, folder / 'zhixue.sqlite3'))
    identity = store.start(LiveInput.model_validate(profiles()))
    store.wait()
    saved = store.read(identity)
    assert saved['state'] == 'passed', saved['records']
    output = saved['records'][0]['output']
    assert output['计入调用'] == 4 and output['收到用量的调用'] == 4
    assert output['按币种汇总'] == ledger['currencies'] == [
        {'currency': 'CNY', 'actual': None, 'estimated': '0.0006'},
        {'currency': 'USD', 'actual': '0.012', 'estimated': None},
    ]
    assert output['费用未知的调用'] == ledger['unknownCalls'] == 1
    assert output['待结算调用'] == 0
    actual = [entry['服务商回报费用'] for entry in output['调用记录'] if entry['服务商回报费用'] is not None]
    assert sorted((entry['currency'], entry['amount']) for entry in actual) == [('USD', '0'), ('USD', '0.012')]
    estimated = [entry['估算费用'] for entry in output['调用记录'] if entry['估算费用'] is not None]
    assert estimated == [{'currency': 'CNY', 'amount': '0.0006'}]
    text = report_text(saved)
    assert '0.012' in text and '0.0006' in text and '费用未知的调用' in text and KEY not in text


@pytest.mark.parametrize("maximum,review_rate,expected_calls", [("0", "4", 0), ("0.001", "100000", 2)])
def test_live_run_checks_confirmed_amount_before_sending(tmp_path, monkeypatch, maximum, review_rate, expected_calls):
    monkeypatch.setattr("acceptance.live_scenario.WAIT", 25)
    with model_fixture('sqlite') as (endpoint, traffic, _):
        data = profiles()
        data['main']['endpoint'] = data['review']['endpoint'] = endpoint
        pricing = {'currency': 'CNY', 'inputPerMillion': '0', 'outputPerMillion': '0'}
        review_pricing = {**pricing, 'outputPerMillion': review_rate}
        data['budget'] = {'limit': {'currency': 'CNY', 'amount': maximum},
                          'mainPricing': pricing, 'reviewPricing': review_pricing}
        store = RunStore(tmp_path)
        with TestClient(create_app(store, TOKEN), base_url='http://127.0.0.1') as http:
            mismatch = {**data, 'budget': {**data['budget'], 'reviewPricing': {**pricing, 'currency': 'USD'}}}
            assert http.post('/api/real-runs', headers=HEADERS, json=mismatch).status_code == 422
            assert store.list() == []
            fresh = http.post('/api/real-runs', headers=HEADERS, json=data)
            assert fresh.status_code == 202, fresh.text
            store.wait()
            report = store.read(fresh.json()['id'])
            first = report['records'][0]['output']
            assert first['金额控制'] == data['budget']
            assert report['state'] == 'failed' and '联调金额' in report['reason']
            usage = report["records"][-1]["output"]
            assert usage["计入调用"] == expected_calls
            assert usage["发送前取消或拦截"] >= 1
            assert usage["尚未发送的预留"] == 0
            assert [phase for phase, *_ in traffic] == ['制定学习路线', '起草一个知识点'][:expected_calls]


@pytest.mark.parametrize("stored_state", ["running", "interrupted"])
def test_interrupted_sample_can_save_quality_and_reopen_with_its_publication(tmp_path, stored_state):
    identity = str(uuid4())
    started = "2026-11-19T06:00:00+00:00"
    course = {"step": "real-publish", "status": "passed", "input": {}, "output": {"课程版本": 1, "正文": "已发布的样本课。"},
              "reason": None, "startedAt": started, "finishedAt": started, "elapsedMs": 8}
    pending = {"step": "real-answer", "status": stored_state, "input": {}, "output": {}, "reason": None,
               "startedAt": started, "finishedAt": None, "elapsedMs": None}
    original = {"id": identity, "mode": "real", "sample": "sqlite", "state": stored_state, "startedAt": started,
                "finishedAt": None, "reason": None, "records": [course, pending]}
    path = tmp_path / f"{identity}.json"
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    untouched = path.read_bytes()
    if stored_state == "interrupted":
        path.with_suffix(".usage").write_text("{damaged checkpoint", encoding="utf-8")
    endpoint = f"/api/runs/{identity}"
    checks = {"facts": "confirmed", "evidence": "insufficient", "boundary": "disputed"}
    with TestClient(create_app(RunStore(tmp_path), TOKEN), base_url="http://127.0.0.1") as http:
        before = http.get(endpoint, headers=HEADERS)
        assert before.is_success, before.text
        interrupted = before.json()
        assert interrupted["state"] == interrupted["records"][1]["status"] == "interrupted"
        assert path.read_bytes() == untouched
        assessed = http.put(endpoint + "/quality", headers=HEADERS, json={"checks": checks})
        assert assessed.is_success, assessed.text
        quality = assessed.json()
        assert quality["checks"] == checks and quality["courseVersion"] == 1
        retained = http.get(endpoint, headers=HEADERS)
        assert retained.is_success, retained.text
        assert retained.json() == {**interrupted, "quality": quality}

    with TestClient(create_app(RunStore(tmp_path), TOKEN), base_url="http://127.0.0.1") as http:
        listing = http.get("/api/runs", headers=HEADERS).json()
        assert [(item["id"], item["state"]) for item in listing] == [(identity, "interrupted")]
        assert http.get(endpoint, headers=HEADERS).json() == retained.json()
        report = http.get(endpoint + "/report", headers=HEADERS)
        assert report.is_success
        assert "结论 已中断" in report.text and "已发布的样本课" in report.text
        assert "适用条件与争议片段 · 有偏差" in report.text and "抽查课程版本 1" in report.text
        assert "未验证 · 模型用量记录" in report.text
    assert json.loads(path.read_bytes()) == retained.json()


def test_usage_report_distinguishes_reservations_and_failed_provider_calls(tmp_path):
    from acceptance.live_config import LiveInput
    from acceptance.live_scenario import save_usage
    from backend.db import open_db
    from backend.model_study import dispatch, reserve_call
    from test_model_costs import WHEN
    from test_model_study import consent_request, ok, session, setup_model

    workspace = tmp_path / "workspace"
    history = tmp_path / "reports"
    database = workspace / "zhixue.sqlite3"
    selection = LiveInput.model_validate(profiles())
    store = RunStore(history, execute_live=lambda run, _: save_usage(run, database))

    def usage():
        identity = store.start(selection)
        store.wait()
        saved = store.read(identity)
        assert saved["state"] == "passed", saved["records"]
        return saved["records"][0]["output"]

    sending = []

    def provider_unavailable(*_):
        sending.append(usage())
        return 503, b""

    with session(workspace) as http:
        setup_model(http)
        for _ in range(2):
            endpoint, goal, preview = consent_request(http, 3)
            ok(http.post(endpoint, json={**goal, "modelConsent": preview["consent"]}))
        engine = open_db(workspace)
        try:
            active = reserve_call(engine, WHEN)
            reserved = reserve_call(engine, WHEN)
            assert active is not None and reserved is not None
            dispatch(engine, active, transport=provider_unavailable, now=lambda: WHEN)
        finally:
            engine.dispose()
    in_flight, = sending
    assert in_flight["计入调用"] == in_flight["待结算调用"] == 1
    assert in_flight["尚未发送的预留"] == 1
    assert in_flight["发送前取消或拦截"] == in_flight["费用未知的调用"] == 0

    finished = usage()
    assert finished["计入调用"] == finished["尚未发送的预留"] == finished["费用未知的调用"] == 1
    assert finished["发送前取消或拦截"] == finished["待结算调用"] == 0
    assert finished["按币种汇总"] == []
    call, = finished["调用记录"]
    assert call["状态"] == "service_unavailable"
    assert call["服务商回报费用"] is call["估算费用"] is None


@pytest.mark.parametrize("ledger_unavailable", [False, True])
def test_live_disconnect_records_unknown_cost_for_a_sent_answer(tmp_path, monkeypatch, ledger_unavailable):
    from acceptance import live_scenario
    from acceptance.product import start_product

    child = None
    exits = []

    @contextmanager
    def observe(*args, **kwargs):
        nonlocal child
        with start_product(*args, **kwargs) as api:
            child = api.child
            yield api

    def interrupt(marker):
        if marker != "答疑上下文 JSON":
            return False
        child.kill()
        exits.append(child.wait(timeout=10))
        return True

    monkeypatch.setattr(live_scenario, "start_product", observe)
    if ledger_unavailable:
        def unreadable(_database, *, final=False):
            assert final and child.poll() is not None
            raise OSError("simulated final ledger read failure")
        monkeypatch.setattr(live_scenario, "read_usage", unreadable)
    with model_fixture("sqlite", abandon_reply=interrupt) as (address, traffic, _):
        data = profiles()
        data["main"]["endpoint"] = data["review"]["endpoint"] = address
        store = RunStore(tmp_path)
        with TestClient(create_app(store, TOKEN), base_url="http://127.0.0.1") as http:
            created = http.post("/api/real-runs", headers=HEADERS, json=data)
            assert created.status_code == 202
            identity = created.json()["id"]
            store.wait()
        url = f"/api/runs/{identity}"
        with TestClient(create_app(RunStore(tmp_path), TOKEN), base_url="http://127.0.0.1") as replay:
            reopened = replay.get(url, headers=HEADERS)
            assert reopened.is_success, reopened.text
            opened = reopened.json()
            report = replay.get(url + "/report", headers=HEADERS)
            assert report.is_success and KEY not in report.text
            assert "费用未知的调用" in report.text and "uncertain" in report.text
            assert SAMPLES["sqlite"].source in report.text
            (tmp_path / "interrupted-sample-report.md").write_bytes(report.content)
        nodes = {item["step"]: item for item in opened["records"]}

        assert len(exits) == 1 and exits[0] != 0
        assert opened["state"] == "failed" and "连接中断" in opened["reason"]
        assert nodes["real-publish"]["status"] == "passed"
        assert nodes["real-answer"]["status"] == "failed"
        output = nodes["real-usage"]["output"]
        if ledger_unavailable or nodes["real-usage"]["status"] != "passed":
            assert nodes["real-usage"]["status"] == "failed"
            assert "无法读取隔离工作区的模型账本" in nodes["real-usage"]["reason"]
            assert output["采集时间"] and output["当前隔离服务已停止"] is True
            assert output["采集时隔离服务已停止"] is False
        assert output["计入调用"] == len(traffic)
        assert [marker for marker, *_ in traffic].count("答疑上下文 JSON") == 1
        answer, = [row for row in output["调用记录"] if row["阶段"] == "lesson_answer"]
        assert answer["状态"] == "uncertain"
        assert answer["估算费用"] is None and answer["服务商回报费用"] is None
        assert output["待结算调用"] == 0
        assert output["费用未知的调用"] == len(traffic)
        assert RunStore(tmp_path).read(identity) == opened


def test_sample_report_reads_provider_usage_saved_before_job_update(tmp_path):
    from acceptance.live_scenario import save_usage
    from backend.db import ModelUse, open_db
    from backend.model_ledger import save_charge_reply
    from backend.model_study import dispatch, reserve_call
    from test_model_costs import WHEN
    from test_model_study import PLAN, consent_request, ok, session, setup_model

    workspace = tmp_path / "workspace"
    database = workspace / "zhixue.sqlite3"
    store = RunStore(tmp_path / "reports", execute_live=lambda run, _: save_usage(run, database))
    selection = LiveInput.model_validate(profiles())
    snapshots = []

    with session(workspace) as http:
        setup_model(http)
        endpoint, goal, preview = consent_request(http, 2)
        ok(http.post(endpoint, json={**goal, "modelConsent": preview["consent"]}))
        engine = open_db(workspace)
        try:
            call = reserve_call(engine, WHEN)
            assert call is not None

            def reply(*_):
                save_charge_reply(engine, ModelUse.__tablename__, call.identity, "passed",
                                  {"inputTokens": 21, "outputTokens": 12}, WHEN)
                identity = store.start(selection)
                store.wait()
                snapshots.append(store.read(identity)["records"][0]["output"])
                return 200, json.dumps(encoded_reply(PLAN)).encode()

            dispatch(engine, call, transport=reply, now=lambda: WHEN)
        finally:
            engine.dispose()
    snapshot, = snapshots
    assert snapshot["收到用量的调用"] == 1
    row, = snapshot["调用记录"]
    assert row["输入用量"] == 21 and row["输出用量"] == 12
    assert row["状态"] == "sending"
    assert datetime.fromisoformat(row["结算记录时间（UTC）"]) == WHEN


def test_sample_keeps_first_failure_when_usage_database_cannot_be_read(tmp_path, monkeypatch):
    from acceptance import live_scenario
    from acceptance.steps import CheckFailed

    @contextmanager
    def corrupt_database(_run, _selection, _sample, _runtime, folder):
        folder.mkdir()
        (folder / "zhixue.sqlite3").write_bytes(b"invalid isolated SQLite data")
        yield object()

    def fail_course(run, _api, _sample):
        with run.step("real-route", {}):
            raise CheckFailed("样本服务无法处理路线。")

    monkeypatch.setattr(live_scenario, "closing_session", corrupt_database)
    monkeypatch.setattr(live_scenario, "load_course", fail_course)
    store = RunStore(tmp_path)
    identity = store.start(LiveInput.model_validate(profiles()))
    store.wait()
    saved = store.read(identity)
    assert saved["state"] == "failed"
    assert saved["reason"] == "样本服务无法处理路线。"
    route, usage = saved["records"]
    assert route["step"] == "real-route" and route["reason"] == saved["reason"]
    assert usage["step"] == "real-usage" and usage["status"] == "failed"
    assert "模型账本" in usage["reason"]


@pytest.mark.parametrize("case,echo", [("si", False), ("sqlite", True)])
def test_live_run_preserves_billing_when_ledger_file_disappears_after_shutdown(tmp_path, monkeypatch, case, echo):
    from csv import DictReader
    from io import StringIO

    from acceptance import live_scenario
    from acceptance.usage import read_usage_checkpoint

    closing = live_scenario.closing_session
    retained = []

    @contextmanager
    def lose_ledger_after_stop(run, selection, sample, runtime, folder):
        try:
            with closing(run, selection, sample, runtime, folder) as api:
                yield api
        finally:
            checkpoint = read_usage_checkpoint(run.store.usage_path(run.identity))
            assert checkpoint is not None and checkpoint["serviceStopped"]
            retained.append(checkpoint)
            (folder / "zhixue.sqlite3").unlink()

    monkeypatch.setattr(live_scenario, "closing_session", lose_ledger_after_stop)
    store = RunStore(tmp_path)
    with model_fixture(case, echo) as (endpoint, traffic, _):
        selection = profiles()
        selection["sample"] = case
        selection["main"]["endpoint"] = selection["review"]["endpoint"] = endpoint
        identity = store.start(LiveInput.model_validate(selection))
        store.wait()
    checkpoint, = retained
    assert checkpoint["output"]["计入调用"] == len(traffic) == 9
    archive = store.read(identity, include_checkpoint=False)
    assert archive["state"] == "failed", "an unreadable final ledger must prevent a passed run"
    records = {node["step"]: node for node in archive["records"]}
    assert records["real-publish"]["status"] == "passed"
    ledger = records["real-usage"]
    assert ledger["status"] == "failed" and ledger["reason"] == "无法读取隔离工作区的模型账本。"
    if echo:
        assert archive["reason"] == records["real-review"]["reason"] != ledger["reason"]
    else:
        assert records["real-schedule"]["status"] == "passed" and archive["reason"] == ledger["reason"]
    output = ledger["output"]
    assert output["采集时间"] == checkpoint["capturedAt"] and output["当前隔离服务已停止"] is True
    assert output["调用记录"] == checkpoint["output"]["调用记录"]
    assert output["计入调用"] == output["费用未知的调用"] == 9 and output["待结算调用"] == 0
    archive_bytes = store.path(identity).read_bytes()
    with TestClient(create_app(RunStore(tmp_path), TOKEN), base_url="http://127.0.0.1") as reopened:
        run = f"/api/runs/{identity}"
        assert reopened.get(run, headers=HEADERS).json() == archive
        report = reopened.get(run + "/report", headers=HEADERS)
        csv = reopened.get(run + "/billing", headers=HEADERS)
        assert report.is_success and csv.is_success
        assert KEY not in report.text and KEY.encode() not in csv.content
        assert "最后一次保存的用量快照" in report.text
        rows = list(DictReader(StringIO(csv.content.decode("utf-8-sig"))))
        assert len(rows) == 9 and all(row["运行 ID"] == identity and row["样本"] == SAMPLES[case].title for row in rows)
        assert all(row["用量采集时间"] == checkpoint["capturedAt"] for row in rows)
        assert all(row["证据范围"] == output["证据范围"] for row in rows)
        assert [row["阶段"] for row in rows] == [row["阶段"] for row in output["调用记录"]]
    assert store.path(identity).read_bytes() == archive_bytes
    (tmp_path / "末次账本缺失报告.md").write_bytes(report.content)
    (tmp_path / "末次账本缺失账单.csv").write_bytes(csv.content)


def test_product_handles_local_connection_closing_during_an_http_request():
    from acceptance.product import Product
    from acceptance.steps import CheckFailed

    class Disconnected(BaseHTTPRequestHandler):
        def do_GET(self):
            # Close the loopback connection without a status line.
            self.close_connection = True

    with ThreadingHTTPServer(("127.0.0.1", 0), Disconnected) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = Product(f"http://127.0.0.1:{server.server_port}", TOKEN)
            with pytest.raises(CheckFailed, match="与隔离学习服务的本机连接中断或超时"):
                api.get("/api/state")
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize(
    "checkpoint,known_reason",
    [("initialization", "隔离模型 Worker 初始化失败"), ("queue", "隔离模型 Worker 运行失败")],
)
def test_live_report_identifies_local_worker_failure(tmp_path, monkeypatch, checkpoint, known_reason):
    from acceptance import live_scenario

    def prepare_faulted_sample(runtime):
        live_product(runtime)
        if checkpoint == "queue":
            workspace = runtime.parent / "workspace"
            workspace.mkdir()
            (workspace / ".worker-lease.sqlite3").write_bytes(b"invalid isolated SQLite lease")

    def copy_worker_with_fault(source, target):
        live_copy(source, target)
        if checkpoint != "initialization" or target.name != "live_runtime.py":
            return
        code = target.read_text(encoding="utf-8")
        entry = 'if __name__ == "__main__":'
        injection = """
def fail_open(*args, **kwargs):
    raise RuntimeError("injected-private-worker-diagnostic")

open_db = fail_open


"""
        assert code.count(entry) == 1
        target.write_text(code.replace(entry, injection + entry), encoding="utf-8")

    live_product = live_scenario.prepare_product
    live_copy = live_scenario.copyfile
    monkeypatch.setattr(live_scenario, "prepare_product", prepare_faulted_sample)
    monkeypatch.setattr(live_scenario, "copyfile", copy_worker_with_fault)
    with model_fixture("sqlite") as (address, traffic, _):
        data = profiles()
        data["main"]["endpoint"] = data["review"]["endpoint"] = address
        store = RunStore(tmp_path)
        identity = store.start(LiveInput.model_validate(data))
        store.wait()
        record = store.read(identity)
        assert record["state"] == "failed" and known_reason in record["reason"], record
        assert traffic == []
        if checkpoint == "initialization":
            assert record["records"][0]["step"] == "real-model"
            assert known_reason in record["records"][0]["reason"]
        ledger = record["records"][-1]
        assert ledger["step"] == "real-usage" and ledger["output"]["计入调用"] == 0
        with TestClient(create_app(RunStore(tmp_path), TOKEN), base_url="http://127.0.0.1") as http:
            report = http.get(f"/api/runs/{identity}/report", headers=HEADERS)
            assert report.status_code == 200 and known_reason in report.text
            assert KEY not in report.text and "injected-private-worker-diagnostic" not in report.text
            (tmp_path / "worker-failure-report.md").write_bytes(report.content)


@pytest.mark.parametrize("graceful", [False, True], ids=["process-kill", "graceful-disconnect"])
def test_parent_termination_preserves_observed_model_usage_without_replaying_calls(tmp_path, graceful):
    import os
    import subprocess
    import sys
    from queue import Queue
    from threading import Event
    from time import monotonic, sleep
    from urllib.parse import urlsplit

    import httpx2

    answer_received, release = Event(), Event()

    def hold_answer(marker):
        if marker != "答疑上下文 JSON":
            return False
        answer_received.set()
        release.wait(timeout=35)
        return not graceful

    history = tmp_path / "history"
    scratch = tmp_path / "runtime"
    scratch.mkdir()
    environment = {**os.environ, "TMPDIR": str(scratch), "TEMP": str(scratch), "TMP": str(scratch)}
    with model_fixture("sqlite", abandon_reply=hold_answer) as (address, traffic, _):
        with subprocess.Popen(
            [sys.executable, "-m", "acceptance", "--stdio", "--reports", str(history)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env=environment, text=True, encoding="utf-8",
        ) as process:
            try:
                announced = Queue(maxsize=1)
                Thread(target=lambda: announced.put(process.stdout.readline().strip()), daemon=True).start()
                url = urlsplit(announced.get(timeout=20))
                auth = {"Authorization": "Bearer " + url.fragment.removeprefix("session=")}
                data = profiles()
                data["main"]["endpoint"] = data["review"]["endpoint"] = address
                with httpx2.Client(base_url=f"http://{url.netloc}", trust_env=False) as api:
                    response = api.post("/api/real-runs", headers=auth, json=data, timeout=10)
                    assert response.status_code == 202, response.text
                    identity = response.json()["id"]
                    assert answer_received.wait(timeout=35), traffic
                    original = (history / f"{identity}.json").read_bytes()
                    pending = json.loads(original)
                    assert pending["state"] == "running"
                    assert all(step["step"] != "real-usage" for step in pending["records"])
                    if graceful:
                        process.stdin.close()
                        deadline = monotonic() + 10
                        while monotonic() < deadline:
                            try:
                                api.get("/api/runs", headers=auth, timeout=1)
                            except httpx2.TransportError:
                                break
                            sleep(.05)
                        else:
                            pytest.fail("Acceptance listener did not close.")
                        release.set()
                        assert process.wait(timeout=55) == 0
                        assert len(traffic) == 4, [marker for marker, *_ in traffic]
                        original = (history / f"{identity}.json").read_bytes()
                    else:
                        process.kill()
                        assert process.wait(timeout=10) != 0

                with TestClient(create_app(RunStore(history), TOKEN), base_url="http://127.0.0.1") as reopened:
                    url = f"/api/runs/{identity}"
                    saved = reopened.get(url, headers=HEADERS).json()
                    assert saved["state"] == "interrupted"
                    nodes = {row["step"]: row for row in saved["records"]}
                    assert nodes["real-publish"]["status"] == "passed"
                    assert nodes["real-answer"]["status"] == "interrupted"
                    usage = nodes["real-usage"]
                    assert usage["status"] == ("passed" if graceful else "interrupted")
                    assert usage["output"]["计入调用"] == len(traffic) == 4
                    assert usage["output"]["收到用量的调用"] == (4 if graceful else 3)
                    if not graceful:
                        assert usage["output"]["证据范围"]
                    assert usage["output"]["调用记录"][-1]["阶段"] == "lesson_answer"
                    last = usage["output"]["调用记录"][-1]
                    assert last["模型分工"] == "主模型" and last["账本开始时间（UTC）"].endswith("Z")
                    assert (last["结算记录时间（UTC）"] is not None) == graceful
                    assert "独立复核模型" in [call["模型分工"] for call in usage["output"]["调用记录"]]
                    report = reopened.get(url + "/report", headers=HEADERS)
                    assert report.is_success and KEY not in report.text
                    assert ("用量快照" in report.text) != graceful
                    billing = reopened.get(url + "/billing", headers=HEADERS)
                    assert billing.is_success and "lesson_answer" in billing.text and KEY not in billing.text
                    assert (history / f"{identity}.json").read_bytes() == original
                    checks = {"facts": "confirmed", "evidence": "insufficient", "boundary": "disputed"}
                    quality = reopened.put(url + "/quality", headers=HEADERS, json={"checks": checks})
                    assert quality.is_success and quality.json()["courseVersion"] == 1
                    stored = json.loads((history / f"{identity}.json").read_bytes())
                    assert any(node["step"] == "real-usage" for node in stored["records"]) == graceful
                    amended = reopened.get(url, headers=HEADERS).json()
                    assert amended["quality"]["checks"] == checks
                    assert amended["records"][-1]["output"]["计入调用"] == 4
                    (tmp_path / "parent-interruption.md").write_bytes(report.content)
                    (tmp_path / "parent-interruption.json").write_text(
                        json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                    )
                    assert len(traffic) == 4
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
