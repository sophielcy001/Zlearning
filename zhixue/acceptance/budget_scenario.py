from contextlib import ExitStack
from pathlib import Path
from secrets import token_urlsafe
from shutil import copyfile

from .product import goal_project, restore_backup, setup_workspace, start_product, until
from .simulation import MAIN, local_models
from .steps import check


SETTINGS = "/api/generation/settings"
RECOVERY = "/api/generation/daily-budget"
FIRST_DAY, SECOND_DAY, THIRD_DAY = "2027-02-01", "2027-02-02", "2027-02-03"
SETUP = {
    "dailyLimit": 9, "concurrency": 1, "dailyCostLimit": {"amount": "0.75", "currency": "CNY"},
    "defaultTaskCostLimit": {"amount": "0.65", "currency": "CNY"},
}
PRICING = {"currency": "CNY", "inputPerMillion": "0", "outputPerMillion": "100"}
RAISED_LIMIT = {"amount": "1", "currency": "CNY"}
OUTCOME = "说明版框和纸张各自的作用"


def create_project(api, topic):
    identity, _ = goal_project(api, topic, 3, outcome=OUTCOME)
    return f"/api/projects/{identity}", {"kind": "generation", "id": identity}


def wait_for_reason(api, project, reason):
    def paused():
        view = api.get(f"{project}/generation")
        return view if view["reason"] == reason else None

    return until(paused, f"任务未进入预期的额度暂停状态（{reason}）。")


def recovery_task(queue, target):
    return next((task for task in queue["tasks"] if all(task[key] == value for key, value in target.items())), {})


def confirm_resume(api, day, target):
    response = api.post(RECOVERY, {"billingDay": day, "tasks": [target]})
    check(response["resumed"] == [target] and response["skipped"] == 0, "任务没有在确认后进入继续队列。")


def raise_task_limit(api, target):
    return api.put(f"{RECOVERY}/tasks/generation/{target['id']}/amount", {"maxCost": RAISED_LIMIT})


def foreground_budget_notices(api):
    notices = api.post("/api/reminders/check", {"trigger": "foreground"})["reminders"]
    return [item for item in notices if item["kind"] == "budget"]


def verify_ledger(api, day, amount, phases):
    journal = api.get(f"/api/generation/usage?billingDay={day}")
    summary = journal["summary"]
    check(journal["billingDay"] == day and summary["calls"] == len(phases)
          and summary["currencies"] == [{"currency": "CNY", "actual": None, "estimated": amount}]
          and sorted(item["phase"] for item in journal["items"]) == sorted(phases),
          f"{day} 的逐次用量或估算费用与实际发送不符。")
    return journal


def check_daily_hold(api, project, target, day):
    blocked = wait_for_reason(api, project, "daily_cost")
    queue = api.get(RECOVERY)
    pending = recovery_task(queue, target)
    check(queue["billingDay"] == day and blocked["usage"]["calls"] == 0
          and pending.get("blockedOn") == day and pending.get("canResume") is False,
          "每日金额不足的任务未在发送前暂停。")
    return queue["dailyCost"]


def run_budget_scenario(run, runtime: Path, folder: Path):
    with ExitStack() as sessions, local_models(output_tokens={"route": 3000, "draft": 90}) as (models, address):
        clock = runtime / "budget-clock"
        with run.step("budget", {"模型": "本机合成接口", "每日金额上限": SETUP["dailyCostLimit"],
                                 "默认单任务上限": SETUP["defaultTaskCostLimit"]}) as output:
            copyfile(Path(__file__).with_name("budget_runtime.py"), runtime / "acceptance_budget.py")
            clock.mkdir()
            (clock / "20270201T155900").touch()
            entry = ("acceptance_budget", "api", str(clock))
            api = sessions.enter_context(start_product(runtime, folder, entry=entry))
            setup_workspace(api)
            connection = api.post("/api/models/connections", {
                "name": "隔离金额验收", "provider": "custom", "protocol": "chat-completions",
                "model": MAIN, "endpoint": address,
            })
            priced = api.put(f"/api/models/connections/{connection['id']}/pricing", {
                "revision": connection["revision"], "previousPricing": None, "pricing": PRICING,
            })
            api.request("PATCH", "/api/models/roles", {"main": connection["id"]})
            settings = api.put(SETTINGS, SETUP)
            check(priced["pricing"] == PRICING and all(settings[key] == value for key, value in SETUP.items())
                  and settings["autoContinueDailyBudget"] is False, "单价、额度或默认继续设置未生效。")
            output.update({"单价": priced["pricing"], "默认跨日继续": "手动", "北京时间计费日": api.get(RECOVERY)["billingDay"]})

        with run.step("budget-task", {"默认限额": SETUP["defaultTaskCostLimit"]}) as output:
            project, task = create_project(api, "活字印刷与版框")
            blocked = wait_for_reason(api, project, "task_cost")
            lessons = api.get(project)["lessons"]
            check(len(lessons) == 1, "额度暂停前未保存路线。")
            lesson = lessons[0]["id"]
            draft = next((step for step in blocked["steps"] if step["lessonId"] == lesson), {})
            check(blocked["usage"]["calls"] == 1 and draft.get("state") == "pending"
                  and blocked["usesDefaultCost"] and models.phases() == ["route"],
                  "单任务额度暂停后出现了额外的模型请求或丢失了待续草稿。")
            verify_ledger(api, FIRST_DAY, "0.3", ["route"])
            password = token_urlsafe(24)
            api.put("/api/backups/destination", {"directory": str(folder / "budget-backups")})
            snapshot = api.post("/api/backups", {"password": password}, expected=201)
            output.update({"拦截原因": blocked["reason"], "课程编号": lesson, "草稿": draft["state"],
                           "接口收到的阶段": models.phases(), "归档时估算": "0.3 CNY"})

        with run.step("budget-resume", {"新上限": RAISED_LIMIT}) as output:
            with models.hold("draft") as gate:
                changed = raise_task_limit(api, task)
                check(changed["canResume"] is True and api.get(f"{project}/generation")["reason"] == "task_cost"
                      and not gate.arrived.wait(1), "调高额度后未等待用户确认就开始续跑。")
            confirm_resume(api, FIRST_DAY, task)

            def draft_ready():
                view = api.get(f"{project}/generation")
                return view if view["state"] == "awaiting_review" else None

            ready = until(draft_ready, "确认继续后原任务没有生成待核验草稿。")
            check(ready["usage"]["calls"] == 2 and models.phases() == ["route", "draft"]
                  and [item["id"] for item in api.get(project)["lessons"]] == [lesson]
                  and any(step["id"] == draft["id"] and step["state"] == "awaiting_review" for step in ready["steps"]),
                  "任务继续时重发了路线或没有沿用同一草稿。")
            journal = verify_ledger(api, FIRST_DAY, "0.309", ["route", "draft"])
            output.update({"调整后等待确认": True, "课程编号": lesson, "续跑阶段": models.phases()[1:],
                           "实际状态": ready["state"], "累计用量": journal["summary"]})

        with run.step("budget-daily", {"计费日": FIRST_DAY, "新目标": ["木版印刷与墨色", "活字校样与颜色"]}) as output:
            later, later_task = create_project(api, "木版印刷与墨色")
            daily_cost = check_daily_hold(api, later, later_task, FIRST_DAY)
            remaining, remaining_task = create_project(api, "活字校样与颜色")
            check_daily_hold(api, remaining, remaining_task, FIRST_DAY)
            check(models.phases() == ["route", "draft"] and daily_cost["settled"] == "0.309",
                  "同日被拦截的任务产生了额外请求或费用。")
            output.update({"拦截原因": "daily_cost", "拦截的新任务数": 2, "新任务发送数": 0, "每日额度": daily_cost})

        with run.step("budget-rollover", {"北京时间计费日": [FIRST_DAY, SECOND_DAY, THIRD_DAY]}) as output:
            with models.hold("route") as gate:
                (clock / "20270201T160100").touch()
                plan = api.get(RECOVERY)
                check(plan["billingDay"] == SECOND_DAY and all(
                    recovery_task(plan, target).get("canResume") is True for target in (later_task, remaining_task)
                ),
                      "跨过北京时间午夜后，昨日额度任务未进入可继续状态。")
                api.post(RECOVERY, {"billingDay": FIRST_DAY, "tasks": [later_task]}, expected=409)
                notices = foreground_budget_notices(api)
                repeated = foreground_budget_notices(api)
                check(len(notices) == 1 and notices[0]["count"] == 2 and not repeated
                      and not gate.arrived.wait(1) and all(
                          api.get(f"{target}/generation")["reason"] == "daily_cost" for target in (later, remaining)
                      ), "次日默认流程没有合并提醒或等待手动继续。")
            stopped = api.post(f"{RECOVERY}/tasks/generation/{remaining_task['id']}/stop")
            check(stopped["stopped"] and api.get(f"{remaining}/generation")["reason"] == "cost_stopped",
                  "未选择继续的额度任务无法停止。")
            confirm_resume(api, SECOND_DAY, later_task)
            manual = wait_for_reason(api, later, "task_cost")
            check(manual["usage"]["calls"] == 1 and models.phases() == ["route", "draft", "route"],
                  "次日手动继续没有只发送额度允许的路线。")
            verify_ledger(api, SECOND_DAY, "0.3", ["route"])

            automatic, automatic_task = create_project(api, "拓印与湿度")
            check_daily_hold(api, automatic, automatic_task, SECOND_DAY)
            with models.hold("draft") as gate:
                check(raise_task_limit(api, later_task)["canResume"] is True,
                      "无法让单任务暂停的对照任务具备续跑额度。")
                settings = api.put(SETTINGS, {**SETUP, "autoContinueDailyBudget": True})
                check(settings["autoContinueDailyBudget"] is True, "次日自动继续未启用。")
                (clock / "20270202T160100").touch()
                resumed = wait_for_reason(api, automatic, "task_cost")
                queue = api.get(RECOVERY)
                check(recovery_task(queue, later_task).get("canResume") is True and not gate.arrived.wait(1),
                      "跨日自动继续发送了仍需手动确认的单任务。")
            still_held = api.get(f"{later}/generation")
            check(resumed["usage"]["calls"] == 1 and still_held["reason"] == "task_cost" and still_held["usage"]["calls"] == 1
                  and models.phases() == ["route", "draft", "route", "route"],
                  "次日自动继续没有只恢复昨日的每日额度任务。")
            verify_ledger(api, THIRD_DAY, "0.3", ["route"])
            output.update({"次日合并提醒": notices[0]["count"], "再次前台检查新增提醒": len(repeated),
                           "旧计费日提交": "HTTP 409", "次日手动继续": ["route"], "次日自动继续": ["route"],
                           "额度足够的单任务仍待确认": True})

        with run.step("budget-restore", {"归档时估算": "0.3 CNY", "当前计费日": THIRD_DAY}) as output:
            days = ((FIRST_DAY, "0.309", ["route", "draft"]),
                    (SECOND_DAY, "0.3", ["route"]), (THIRD_DAY, "0.3", ["route"]))
            before = [verify_ledger(api, *expectation) for expectation in days]
            restored = restore_backup(api, f"/api/backups/{snapshot['id']}", password)
            api.get(later, expected=404)
            api.get(automatic, expected=404)
            after = [verify_ledger(api, *expectation) for expectation in days]
            restored_task = api.get(f"{project}/generation")
            check(before == after and restored_task["state"] == "restore_pending"
                  and [item["id"] for item in api.get(project)["lessons"]] == [lesson],
                  "恢复后逐次用量、原课程或待继续状态发生异常。")
            with models.hold("draft") as gate:
                settings = api.put(SETTINGS, {
                    **SETUP, "defaultTaskCostLimit": RAISED_LIMIT, "autoContinueDailyBudget": True,
                })
                check(settings["defaultTaskCostLimit"] == RAISED_LIMIT and settings["autoContinueDailyBudget"] is True,
                      "恢复后未成功调高限额及启用自动继续。")
                following, following_task = create_project(api, "恢复后检查印刷用纸")
                balance = check_daily_hold(api, following, following_task, THIRD_DAY)
                check(not gate.arrived.wait(1), "额度足够后自动发送了从备份恢复的草稿。")
            waiting = api.get(f"{project}/generation")
            check(balance["settled"] == "0.3" and waiting["state"] == "restore_pending"
                  and waiting["maxCost"] == RAISED_LIMIT and models.phases() == ["route", "draft", "route", "route"],
                  "恢复后出现了未确认的重发，或计费服务丢失了当日支出。")
            output.update({"整库恢复": restored["state"], "调高额度后恢复任务状态": waiting["state"],
                           "恢复后保留的逐日估算": {day: f"{amount} CNY" for day, amount, _ in days},
                           "恢复后的新任务": "daily_cost", "恢复后新增模型请求数": 0})
