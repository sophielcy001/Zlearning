from contextlib import contextmanager
from datetime import UTC, datetime
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend import chat_model_worker
from backend.chat_model_worker import dispatch_reply, reserve_reply
from backend.db import open_db, transaction
from backend.note_ai_worker import dispatch_note_ai, reserve_note_ai
from backend.note_check_worker import dispatch_note_check, reserve_note_check
from backend.note_checks import CHARS_PER_CALL, EDIT_DELAY, scan_checks
from backend.restored_tasks import hold_restored_tasks
from test_chat_model import confirmed, draft
from test_backups import inspect_backup, make_backup
from test_daily_budget import AFTER, BEFORE, RECOVERY, SETTINGS, budget_client, create_generation, pulse
from test_full_restore import app_client, request_restore, wait_restore
from test_model_assessments import assess_one
from test_model_costs import LEDGER, connection, reply, set_pricing
from test_model_failover import goal_request, roles, submit
from test_model_study import DRAFT, PLAN, SESSION, ok, run
from test_note_ai import new_note, preview as preview_note, record_transport
from test_note_checks import authorize, configure_reviewer, transport as check_transport
from test_review_remediation import CHECKED, DRAFT as REMEDIATION, complete_round


def limit(amount, currency="CNY"):
    return {"amount": amount, "currency": currency}


def set_limits(http, **amounts):
    return ok(http.put(SETTINGS, json={"concurrency": 4, "dailyLimit": None, **amounts}))


def priced_main(http):
    model = connection(http)
    set_pricing(http, model, "0", "100")
    ok(http.patch("/api/models/roles", json={"main": model["id"]}))
    return model


def route_result(output=90):
    return reply(json.dumps(PLAN), {"prompt_tokens": 30, "completion_tokens": output})


def resume(http, identity, now=BEFORE):
    day = "2027-02-01" if now == BEFORE else "2027-02-02"
    return ok(http.post(RECOVERY, json={"billingDay": day, "tasks": [{"kind": "generation", "id": identity}]}))


def test_daily_amount_reserves_an_in_flight_request_before_any_second_send(tmp_path):
    outputs = []
    with budget_client(tmp_path, lambda: BEFORE) as http:
        priced_main(http)
        set_limits(http, dailyCostLimit=limit("0.65"))
        first = create_generation(http)
        second = None

        def send(address, headers, payload):
            nonlocal second
            outputs.append(payload["max_tokens"])
            if len(outputs) == 1:
                row, = ok(http.get(LEDGER))["items"]
                assert row["estimatedCost"] is None and row["reservedCost"] == limit("0.6144")
                second = create_generation(http)
                assert run(tmp_path, send, now=lambda: BEFORE)
                blocked = ok(http.get(f"/api/projects/{second}/generation"))
                assert blocked["reason"] == "daily_cost" and blocked["usage"]["calls"] == 0
                assert resume(http, second)["resumed"] == []
            return route_result()

        assert run(tmp_path, send, now=lambda: BEFORE)
        assert outputs == [6144]
        row, = ok(http.get(LEDGER))["items"]
        assert row["scopeId"] == first and row["estimatedCost"] == limit("0.009")
        task, = ok(http.get(RECOVERY))["tasks"]
        assert task["id"] == second and task["canResume"] is True
        assert len(resume(http, second)["resumed"]) == 1
        assert run(tmp_path, send, now=lambda: BEFORE)
        assert outputs == [6144, 6144]


def test_single_task_amount_survives_midnight_and_higher_limit_continues_the_saved_route(tmp_path):
    now = BEFORE
    outputs = []

    def send(address, headers, payload):
        outputs.append(payload["max_tokens"])
        if "制定学习路线" in payload["messages"][-1]["content"]:
            return route_result(4000)
        return reply(json.dumps(DRAFT), {"prompt_tokens": 30, "completion_tokens": 90})

    with budget_client(tmp_path, lambda: now) as http:
        priced_main(http)
        set_limits(http, defaultTaskCostLimit=limit("0.65"), autoContinueDailyBudget=True)
        identity = create_generation(http)
        target = f"/api/projects/{identity}/generation"
        assert run(tmp_path, send, now=lambda: now)
        saved_lessons = ok(http.get(f"/api/projects/{identity}"))["lessons"]
        assert saved_lessons
        assert run(tmp_path, send, now=lambda: now)
        assert ok(http.get(target))["reason"] == "task_cost" and outputs == [6144]
        now = AFTER
        assert not run(tmp_path, send, now=lambda: now)
        assert pulse(http) == [] and resume(http, identity, AFTER)["resumed"] == []
        assert ok(http.post(target + "/control", json={"action": "resume", "maxCost": limit("1")}))["maxCost"] == limit("1")
        assert run(tmp_path, send, now=lambda: now)
        assert outputs == [6144, 4096]
        assert ok(http.get(f"/api/projects/{identity}"))["lessons"] == saved_lessons


def test_course_consent_binds_a_specific_amount_before_the_first_request(tmp_path):
    sent = []
    with budget_client(tmp_path, lambda: BEFORE) as http:
        priced_main(http)
        set_limits(http, defaultTaskCostLimit=limit("1"))
        path, data, _ = goal_request(http)
        preview = ok(http.post(path + "/model-preview", json={**data, "maxCost": limit("0.3")}))
        consent = preview["consent"]
        assert consent["maxCost"] == limit("0.3")
        assert http.post(path + "/submit", json={**data, "modelConsent": {
            **consent, "maxCost": limit("1"),
        }}).status_code == 409
        project = submit(http, path, data, preview)
        assert run(tmp_path, lambda *_: sent.append(True) or route_result(), now=lambda: BEFORE)
        task = ok(http.get(project + "/generation"))
        assert task["maxCost"] == limit("0.3") and task["reason"] == "task_cost"
        assert sent == []


@pytest.mark.parametrize("automatic", [False, True])
def test_only_a_daily_amount_hold_can_recover_at_the_next_shanghai_day(tmp_path, automatic):
    now = BEFORE
    calls = []

    def send(address, headers, payload):
        calls.append(payload["max_tokens"])
        return route_result(6000)

    with budget_client(tmp_path, lambda: now) as http:
        priced_main(http)
        set_limits(http, dailyCostLimit=limit("0.65"), autoContinueDailyBudget=automatic)
        first = create_generation(http)
        assert run(tmp_path, send, now=lambda: now)
        ok(http.post(f"/api/projects/{first}/generation/control", json={"action": "stop"}))
        second = create_generation(http)
        assert run(tmp_path, send, now=lambda: now)
        assert len(calls) == 1 and ok(http.get(f"/api/projects/{second}/generation"))["reason"] == "daily_cost"
        assert pulse(http) == []
        now = AFTER
        if automatic:
            assert pulse(http) == []
        else:
            notice, = pulse(http)
            assert notice["kind"] == "budget" and notice["count"] == 1
            assert pulse(http) == [] and not run(tmp_path, send, now=lambda: now)
            assert len(resume(http, second, AFTER)["resumed"]) == 1
        assert run(tmp_path, send, now=lambda: now)
        assert len(calls) == 2
        assert ok(http.get(f"/api/projects/{second}/generation"))["usage"]["calls"] == 1


def test_uncertain_main_charge_still_counts_when_the_same_task_switches_to_backup(tmp_path):
    calls = []

    def send(address, headers, payload):
        calls.append(payload["model"])
        return (503, b"{}") if len(calls) == 1 else route_result()

    with budget_client(tmp_path, lambda: BEFORE) as http:
        main, backup, _, _ = roles(http)
        for model in (main, backup):
            set_pricing(http, model, "0", "100")
        set_limits(http, defaultTaskCostLimit=limit("0.7"))
        path, data, preview = goal_request(http, allow=True)
        project = submit(http, path, data, preview)
        target = project + "/generation"
        assert run(tmp_path, send, now=lambda: BEFORE)
        assert run(tmp_path, send, now=lambda: BEFORE)
        assert calls == [main["model"]] and ok(http.get(target))["reason"] == "task_cost"
        row, = ok(http.get(LEDGER))["items"]
        assert row["actualCost"] is None and row["estimatedCost"] is None
        assert row["reservedCost"] == limit("0.6144")
        ok(http.post(target + "/control", json={"action": "resume", "maxCost": limit("1.23")}))
        assert run(tmp_path, send, now=lambda: BEFORE)
        assert calls == [main["model"], backup["model"]]


def test_restoring_a_daily_amount_hold_requires_fresh_continuation(tmp_path):
    now = BEFORE
    calls = []

    def send(address, headers, payload):
        calls.append(payload["max_tokens"])
        return route_result(6000)

    with budget_client(tmp_path, lambda: now) as http:
        priced_main(http)
        set_limits(http, dailyCostLimit=limit("0.65"), autoContinueDailyBudget=True)
        first = create_generation(http)
        assert run(tmp_path, send, now=lambda: now)
        ok(http.post(f"/api/projects/{first}/generation/control", json={"action": "stop"}))
        second = create_generation(http)
        target = f"/api/projects/{second}/generation"
        assert run(tmp_path, send, now=lambda: now)
        assert ok(http.get(target))["reason"] == "daily_cost"
        engine = open_db(tmp_path)
        try:
            hold_restored_tasks(engine, now.replace(tzinfo=UTC))
        finally:
            engine.dispose()
        now = AFTER
        assert not run(tmp_path, send, now=lambda: now)
        assert calls == [6144] and ok(http.get(target))["state"] == "restore_pending"


def test_encrypted_restore_does_not_reset_same_day_model_spending(tmp_path):
    calls = []

    def send(_, __, payload):
        calls.append(payload["max_tokens"])
        return route_result(6000)

    with app_client(tmp_path, now=lambda: BEFORE) as http:
        priced_main(http)
        set_limits(http, dailyCostLimit=limit("0.65"))
        identity = create_generation(http)
        target = f"/api/projects/{identity}/generation"
        archived = make_backup(http)
        assert run(tmp_path, send, now=lambda: BEFORE)
        request_restore(http, archived, ok(inspect_backup(http, archived)))
        assert wait_restore(http)["state"] == "complete"
        assert ok(http.get(target))["state"] == "restore_pending"
        ok(http.post(target + "/control", json={"action": "resume"}))
        assert run(tmp_path, send, now=lambda: BEFORE)
        assert calls == [6144] and ok(http.get(target))["reason"] == "daily_cost"
        assert ok(http.get(RECOVERY))["dailyCost"]["settled"] == "0.6"


def test_remediation_draft_and_independent_review_use_the_same_task_amount(tmp_path):
    prompts = []
    with budget_client(tmp_path, lambda: datetime.now(UTC)) as http:
        _, target, _ = complete_round(http, tmp_path, prompts)
        for model in ok(http.get("/api/models"))["connections"]:
            set_pricing(http, model, "0", "100")
        set_limits(http, defaultTaskCostLimit=limit("0.7"))
        assert assess_one(tmp_path, REMEDIATION, prompts)
        sent = len(prompts)
        assert assess_one(tmp_path, CHECKED, prompts)
        assert len(prompts) == sent
        task, = ok(http.get(RECOVERY))["tasks"]
        assert task["kind"] == "assessment" and task["phase"] == "remediation_check"
        assert not task["canResume"] and ok(http.get(target))["remediation"]["state"] != "verified"
        path = f"{RECOVERY}/tasks/assessment/{task['id']}/amount"
        assert ok(http.put(path, json={"maxCost": limit("1")}))["canResume"]
        date = ok(http.get(RECOVERY))["billingDay"]
        assert len(ok(http.post(RECOVERY, json={"billingDay": date,
            "tasks": [{"kind": "assessment", "id": task["id"]}],
        }))["resumed"]) == 1
        assert assess_one(tmp_path, CHECKED, prompts)
        assert len(prompts) == sent + 1


def test_connection_test_respects_daily_amount_and_requires_matching_pricing(tmp_path):
    sent = []

    def send(address, headers, payload):
        sent.append(payload["max_tokens"])
        return reply("OK", {"prompt_tokens": 8, "completion_tokens": 512})

    with TestClient(create_app(tmp_path, SESSION, now=lambda: BEFORE, model_transport=send),
                    base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {SESSION}"}) as http:
        model = connection(http)
        path = f"/api/models/connections/{model['id']}/test"
        request = {"revision": model["revision"]}
        set_limits(http, dailyCostLimit=limit("0.06"))
        assert http.post(path, json=request).status_code == 409 and sent == []
        set_pricing(http, model, "0", "100", currency="USD")
        assert http.post(path, json=request).status_code == 409 and sent == []
        set_pricing(http, model, "0", "100")
        assert ok(http.post(path, json=request))["status"] == "passed"
        assert http.post(path, json=request).status_code == 409 and sent == [512]
        assert ok(http.get(LEDGER))["total"] == 1


def test_provider_actual_cost_supersedes_the_reservation_and_token_estimate(tmp_path):
    calls = []

    def send(address, headers, payload):
        calls.append(payload["max_tokens"])
        return reply("OK", {"prompt_tokens": 8, "completion_tokens": 512, "cost": 0.012,
                            "is_byok": False, "total_tokens": 520})

    with TestClient(create_app(tmp_path, SESSION, now=lambda: BEFORE, model_transport=send),
                    base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {SESSION}"}) as http:
        model = connection(http, "https://openrouter.ai/api/v1")
        path = f"/api/models/connections/{model['id']}/test"
        set_pricing(http, model, "0", "100", currency="USD")
        set_limits(http, dailyCostLimit=limit("0.065", "USD"))
        for _ in range(2):
            assert ok(http.post(path, json={"revision": model["revision"]}))["status"] == "passed"
        assert calls == [512, 512]
        totals = ok(http.get(RECOVERY))["dailyCost"]
        assert totals["settled"] == "0.024" and totals["reserved"] == "0" and totals["unknownCalls"] == 0


def test_same_chat_question_can_continue_after_raising_its_amount(tmp_path):
    sent = []

    def send(address, headers, payload):
        sent.append(payload["messages"][-1]["content"])
        return reply("模型回复", {"prompt_tokens": 12, "completion_tokens": 30})

    with budget_client(tmp_path, lambda: BEFORE) as http:
        priced_main(http)
        set_limits(http, defaultTaskCostLimit=limit("0.15"))
        thread = str(uuid4())
        route = f"/api/chat-model/{thread}"
        question = draft("怎样验证单任务金额？")
        _, request = confirmed(http, route, question)
        ok(http.post(route + "/send", json=request))
        engine = open_db(tmp_path)
        try:
            dispatch_reply(engine, reserve_reply(engine, BEFORE), transport=send, now=lambda: BEFORE)
            blocked, = ok(http.get(RECOVERY))["tasks"]
            assert blocked["kind"] == "chat" and blocked["id"] == question["id"] and not blocked["canResume"]
            assert sent == []
            edited = ok(http.put(f"{RECOVERY}/tasks/chat/{question['id']}/amount", json={"maxCost": limit("0.2")}))
            assert edited["canResume"] and sent == []
            data = {"billingDay": "2027-02-01", "tasks": [{"kind": "chat", "id": question["id"]}]}
            assert len(ok(http.post(RECOVERY, json=data))["resumed"]) == 1
            dispatch_reply(engine, reserve_reply(engine, BEFORE), transport=send, now=lambda: BEFORE)
            assert len(sent) == 1 and question["text"] in sent[0]
            messages = ok(http.get(f"/api/conversations/{thread}"))["messages"]
            assert [message["text"] for message in messages] == [question["text"], "模型回复"]
        finally:
            engine.dispose()


def test_chat_admission_rechecks_the_day_after_waiting_for_sqlite_write_access(tmp_path, monkeypatch):
    now = BEFORE
    received = []

    def send(address, headers, payload):
        received.append(payload["max_tokens"])
        return reply("回复", {"prompt_tokens": 30, "completion_tokens": 1600})

    with budget_client(tmp_path, lambda: now) as http:
        priced_main(http)
        set_limits(http, dailyCostLimit=limit("0.2"))
        engine = open_db(tmp_path)
        try:
            keys = []
            for prompt in ("昨日缓慢获取锁", "今日请求"):
                route = f"/api/chat-model/{uuid4()}"
                _, request = confirmed(http, route, draft(prompt))
                ok(http.post(route + "/send", json=request))
                keys.append(reserve_reply(engine, BEFORE if not keys else AFTER))
            dispatch_reply(engine, keys[1], transport=send, now=lambda: AFTER)

            @contextmanager
            def write_transaction(engine):
                nonlocal now
                with transaction(engine) as db:
                    now = AFTER
                    yield db

            monkeypatch.setattr(chat_model_worker, "transaction", write_transaction)
            dispatch_reply(engine, keys[0], transport=send, now=lambda: now)
            assert received == [1800]
            task, = ok(http.get(RECOVERY))["tasks"]
            assert task["id"] == keys[0].reply_id and task["reason"] == "daily_cost"
        finally:
            engine.dispose()


def test_stopped_note_amount_hold_stays_stopped_across_midnight(tmp_path):
    now = BEFORE
    calls = []
    with budget_client(tmp_path, lambda: now) as http:
        priced_main(http)
        set_limits(http, dailyCostLimit=limit("0.3"), autoContinueDailyBudget=True)
        _, note = new_note(http, text="用于验证额度的选段。")
        request, _ = preview_note(http, note, "explain", "用于验证额度的选段。")
        ok(http.post(note + "/assistant", json=request))
        engine = open_db(tmp_path)
        try:
            dispatch_note_ai(engine, reserve_note_ai(engine, now), transport=record_transport(calls), now=lambda: now)
            task, = ok(http.get(RECOVERY))["tasks"]
            assert task["id"] == request["id"] and task["reason"] == "daily_cost" and calls == []
            assert ok(http.post(f"{RECOVERY}/tasks/note_ai/{task['id']}/stop"))["stopped"]
            now = AFTER
            set_limits(http, dailyCostLimit=limit("1"))
            assert reserve_note_ai(engine, now) is None and pulse(http) == []
            stopped, = ok(http.get(RECOVERY))["tasks"]
            assert stopped["reason"] == "cost_stopped" and stopped["canResume"]
            assert len(ok(http.post(RECOVERY, json={"billingDay": "2027-02-02",
                "tasks": [{"kind": "note_ai", "id": task["id"]}],
            }))["resumed"]) == 1
            dispatch_note_ai(engine, reserve_note_ai(engine, now), transport=record_transport(calls), now=lambda: now)
            assert len(calls) == 1
            assert ok(http.get(note + "/assistant"))["state"] == "ready"
            assert ok(http.get(note))["content"] == "用于验证额度的选段。"
        finally:
            engine.dispose()


def test_note_check_batches_share_a_task_cap_across_separate_sends(tmp_path):
    now = BEFORE
    calls = []
    with budget_client(tmp_path, lambda: now) as http:
        model = configure_reviewer(http)
        set_pricing(http, model, "0", "100")
        set_limits(http, defaultTaskCostLimit=limit("0.401"))
        _, note = new_note(http, text="段" * (CHARS_PER_CALL + 200))
        route = note + "/checks"
        _, initial = authorize(http, route)
        now += EDIT_DELAY
        engine = open_db(tmp_path)
        try:
            for _ in range(2):
                dispatch_note_check(engine, reserve_note_check(engine, now), transport=check_transport(calls, []),
                                    now=lambda: now)
            assert len(calls) == 1
            paused = ok(http.get(route))
            assert paused["id"] == initial["id"] and paused["completed"] == 1 and paused["costBlocked"]
            task, = ok(http.get(RECOVERY))["tasks"]
            assert task["kind"] == "note_check" and not task["canResume"]
            assert http.post(f"{route}/{initial['id']}/resume").status_code == 409
            set_path = f"{RECOVERY}/tasks/note_check/{initial['id']}/amount"
            assert ok(http.put(set_path, json={"maxCost": limit("0.5")}))["canResume"]
            ok(http.post(f"{route}/{initial['id']}/resume"))
            dispatch_note_check(engine, reserve_note_check(engine, now), transport=check_transport(calls, []), now=lambda: now)
            assert len(calls) == 2 and ok(http.get(route))["state"] == "reviewed"
        finally:
            engine.dispose()


def test_note_check_reauthorization_carries_the_existing_amount_scope(tmp_path):
    now = BEFORE
    calls = []
    with budget_client(tmp_path, lambda: now) as http:
        model = configure_reviewer(http)
        set_pricing(http, model, "0", "100")
        set_limits(http, defaultTaskCostLimit=limit("0.401"))
        _, note = new_note(http, text="重新确认内容。")
        route = note + "/checks"
        _, initial = authorize(http, route)
        now += EDIT_DELAY
        engine = open_db(tmp_path)
        try:
            send = check_transport(calls, [{"quote": "找不到的内容", "reason": "无效位置"}])
            dispatch_note_check(engine, reserve_note_check(engine, now), transport=send, now=lambda: now)
            assert len(calls) == 1 and ok(http.get(route))["state"] == "partial"
            _, fresh = authorize(http, route)
            assert fresh["id"] != initial["id"]
            now += EDIT_DELAY
            dispatch_note_check(engine, reserve_note_check(engine, now), transport=send, now=lambda: now)
            assert len(calls) == 1 and ok(http.get(route))["costBlocked"]
        finally:
            engine.dispose()


def test_shorter_authorized_note_can_continue_when_its_new_quote_fits(tmp_path):
    now = BEFORE
    calls = []
    with budget_client(tmp_path, lambda: now) as http:
        model = configure_reviewer(http)
        set_pricing(http, model, "100", "0")
        set_limits(http, defaultTaskCostLimit=limit("0.2"))
        _, note = new_note(http, text="段" * CHARS_PER_CALL)
        route = note + "/checks"
        _, saved = authorize(http, route)
        engine = open_db(tmp_path)
        try:
            send = check_transport(calls, [])
            now += EDIT_DELAY
            dispatch_note_check(engine, reserve_note_check(engine, now), transport=send, now=lambda: now)
            assert not calls and ok(http.get(route))["costBlocked"]
            current = ok(http.get(note))
            ok(http.put(note, json={"baseRevision": current["revision"], "content": "精简后需核对的段落。"}))
            scan_checks(engine, tmp_path, now)
            now += EDIT_DELAY
            task, = ok(http.get(RECOVERY))["tasks"]
            assert task["id"] == saved["id"] and task["canResume"]
            ok(http.post(f"{route}/{saved['id']}/resume"))
            dispatch_note_check(engine, reserve_note_check(engine, now), transport=send, now=lambda: now)
            assert len(calls) == 1 and calls[0][1]["note"] == "精简后需核对的段落。"
        finally:
            engine.dispose()


def test_sample_budget_carries_spending_between_phases_at_billing_midnight(tmp_path):
    from backend.db import ModelCharge, upgrade
    from backend.model_costs import MoneyLimit
    from backend.model_usage import billing_day
    from backend.money_budget import amount_issue

    before, after = billing_day(BEFORE), billing_day(AFTER)
    assert before < after
    budget = MoneyLimit(currency='CNY', amount='0.5')
    pricing = {'currency': 'CNY', 'inputPerMillion': '2', 'outputPerMillion': '4'}
    engine = open_db(tmp_path, workspace_cost_limit=budget)
    try:
        upgrade(engine)
        with transaction(engine) as db:
            db.add(ModelCharge(source='model_uses', call_id=str(uuid4()), kind='generation',
                               scope_id=str(uuid4()), phase='route', billing_day=before,
                               started_at=BEFORE, finished_at=BEFORE, status='reported',
                               actual_currency='CNY', actual_amount='0.3'))
        with transaction(engine, write=False) as db:
            target = str(uuid4())
            assert amount_issue(db, 'answer', target, after, pricing, '0.2') is None
            assert amount_issue(db, 'answer', target, after, pricing, '0.21') == 'workspace_cost'
    finally:
        engine.dispose()

    default = open_db(tmp_path)
    try:
        with transaction(default, write=False) as db:
            assert amount_issue(db, 'answer', target, after, pricing, '0.21') is None
    finally:
        default.dispose()
