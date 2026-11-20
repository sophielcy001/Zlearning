from contextlib import closing
from pathlib import Path
from shutil import copyfile
import sqlite3
from subprocess import TimeoutExpired

from .product import goal_project, setup_workspace, start_product, until
from .simulation import MAIN, local_models
from .steps import CheckFailed, check


def register_main(api, address):
    setup_workspace(api)
    api.put("/api/generation/settings", {"dailyLimit": None, "concurrency": 1})
    model = api.post("/api/models/connections", {
        "name": "重启验收主模型", "provider": "custom", "protocol": "chat-completions",
        "endpoint": address, "model": MAIN,
    })
    api.put(f"/api/models/connections/{model['id']}/pricing", {
        "revision": model["revision"], "previousPricing": None,
        "pricing": {"currency": "CNY", "inputPerMillion": "1", "outputPerMillion": "2"},
    })
    api.request("PATCH", "/api/models/roles", {"main": model["id"]})


def draft_states(folder):
    path = (folder / "zhixue.sqlite3").resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return [status for (status,) in db.execute(
            "SELECT status FROM model_uses WHERE phase = 'draft' ORDER BY started_at, id",
        )]


def exercise(runtime, folder, boundary, output):
    was_sending = boundary == "sending"
    initial_phases = ["route", "draft"] if was_sending else ["route"]
    with local_models() as (models, address):
        with start_product(runtime, folder, entry=("acceptance_restart_runtime", boundary)) as api:
            register_main(api, address)
            identity, _ = goal_project(api, "活字印刷试印", 2, outcome="说明如何固定版框并检查字行")
            project = f"/api/projects/{identity}"
            generation = f"{project}/generation"
            until((folder / ".acceptance-draft-checkpoint").is_file, "Worker 未到达待中断检查点。", 25)
            original, = api.get(project)["lessons"]
            check(draft_states(folder) == [boundary], "中断检查点的调用状态与预期不同。")
            check(models.phases() == initial_phases, "中断前的回环请求与发送状态不一致。")
            (folder / ".acceptance-stop-worker").touch()
            try:
                code = api.child.wait(timeout=20)
            except TimeoutExpired as error:
                raise CheckFailed("Worker 中断后原服务仍在运行。") from error
            check(code == 0, f"Worker 中断后原服务退出码为 {code}。")
            output["旧服务退出码"] = code

        with start_product(runtime, folder) as api:
            if was_sending:
                def held():
                    current = api.get(generation)
                    costs = current["usage"]["costs"]
                    return current if current["reason"] == "uncertain" and costs["inFlight"] == 0 else None

                saved = until(held, "已发送的调用没有等待人工处理。", 25)
                check(draft_states(folder) == ["uncertain"] and models.phases() == initial_phases,
                      "已发送的草稿被自动重发，或旧调用结果丢失。")
                check(saved["usage"]["calls"] == 2 and saved["usage"]["costs"]["unknownCalls"] == 1,
                      "已发送的旧草稿没有按费用未知保留。")
                output["等待人工时费用未知的调用"] = 1
                api.post(f"{generation}/control", {"action": "resume", "maxCalls": 3})

            def drafted():
                current = api.get(generation)
                return current if any(step["state"] == "awaiting_review" and step["lessonId"] == original["id"]
                                      for step in current["steps"]) else None

            current = until(drafted, "重新启动后没有为原知识点续写草稿。", 25)
            phases = initial_phases + ["draft"]
            recovered_state = "uncertain" if was_sending else "not_sent"
            check(models.phases() == phases and current["usage"]["calls"] == len(phases),
                  "恢复后调用次数有偏差，或已保存的路线被重复执行。")
            check(draft_states(folder) == [recovered_state, "passed"],
                  "恢复后的旧调用和已完成草稿状态不正确。")
            check(api.get(f"/api/lessons/{original['id']}")["body"] is None,
                  "恢复时将未经独立核验的草稿暴露为正文。")
            output.update({
                "旧服务已停止": True, "旧调用记录": recovered_state, "需要人工继续": was_sending,
                "回环实收": phases, "计入调用": current["usage"]["calls"], "原首课": original["title"],
            })


def run_restart_scenario(run, runtime: Path, scratch: Path):
    copyfile(Path(__file__).with_name("restart_runtime.py"), runtime / "acceptance_restart_runtime.py")
    for step, boundary, label in (
        ("restart-unsent", "reserved", "草稿已预留，尚未发送"),
        ("restart-sent", "sending", "回环模型已收到，结果尚未入账"),
    ):
        with run.step(step, {"中断位置": label, "原任务调用上限": 2}) as output:
            exercise(runtime, scratch / f"worker-restart-{boundary}", boundary, output)
