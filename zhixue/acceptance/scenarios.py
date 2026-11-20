from contextlib import ExitStack, closing
from datetime import date, timedelta
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
from tempfile import TemporaryDirectory
from time import sleep
from uuid import uuid4

from .budget_scenario import run_budget_scenario
from .library_scenario import run_library_scenario
from .ocr_scenario import run_ocr_scenario
from .product import goal_project, prepare_product, restore_backup, setup_workspace, start_product, until
from .restart_scenario import run_restart_scenario
from .revision_scenario import run_revision_scenario
from .route_scenario import run_route_scenario
from .simulation import ADDITION, ANSWER, BODY, MAIN, PRIVATE_LINE, QUESTION, REVIEWER, SOURCE, local_models
from .steps import check


TOPIC = "活字印刷试印"
OUTCOME = "说明试印时怎样固定活字并检查字行"
NOTE = "使用版框固定活字。"
SOURCE_NAME = "验收用合成资料.txt"
SOURCE_TEXT = f"验收样本\n{SOURCE}\n{PRIVATE_LINE}"
ANSWERS = ["用版框固定排好的活字。", "查看纸张的大小。"]


def learning_route(run, api):
    with run.step("goal", {"主题": TOPIC, "可检验成果": OUTCOME}) as output:
        identity, scope = goal_project(api, TOPIC, 3, outcome=OUTCOME)
        output.update({"项目 ID": identity, "授权字段": [item["key"] for item in scope["inputs"]],
                       "目标卡重复提交": "同一项目"})
    project = f"/api/projects/{identity}"
    with run.step("route", {"项目 ID": identity}) as output:
        def installed():
            current = api.get(project)
            return current if current["lessons"] else None

        current = until(installed, "后台路线没有生成。", timeout=55)
        lesson, = current["lessons"]
        check(current["generationMode"] == "model", "项目没有采用模型路线。")
        output.update({"课程": lesson["title"], "预期成果": lesson["outcome"], "阶段": lesson["phase"]})
    target = f"/api/lessons/{lesson['id']}"
    with run.step("draft", {"课程": lesson["title"]}) as output:
        generation = f"{project}/generation"

        def ready():
            view = api.get(generation)
            return next((step for step in view["steps"] if step["lessonId"] == lesson["id"]
                         and step["state"] == "awaiting_review"), None)

        step = until(ready, "课程草稿没有进入待独立核验。")
        draft = api.get(f"{generation}/drafts/{lesson['id']}")
        check(api.get(target)["body"] is None, "未核验草稿已出现在正文。")
        api.put(f"{target}/learned", {"learned": True}, expected=409)
        output.update({"草稿小节": [item["title"] for item in draft["sections"]], "任务状态": step["state"],
                       "已学写入": "HTTP 409"})
    return project, target


def verify_course(run, api, models, project, target):
    filename = api.post("/api/files", {"name": SOURCE_NAME, "content": SOURCE_TEXT})
    file = api.get(f"/api/files/{filename['id']}?at=2")
    evidence = {"sources": [{"fileId": file["id"], "revision": file["revision"], "at": 2}]}
    control = f"{project}/generation/control"
    with models.hold("verify") as request:
        with run.step("verify", {"复核资料": SOURCE_NAME, "位置": "第 2 行"}) as output:
            preview = api.authorize(f"{project}/course-review", evidence)
            check(preview["sources"][0]["excerpt"] == SOURCE, "发送预览没有定位到已选资料行。")
            check(request.arrived.wait(12), "独立复核请求没有到达模拟接口。")
            call, = models.calls("verify")
            check(call["model"] == REVIEWER and call["includesSource"] and not call["includesUnauthorizedLine"],
                  "独立复核请求超出了授权资料范围。")
            output.update({"接收模型": call["model"], "已发送资料行": preview["sources"][0]["excerpt"],
                           "包含未授权行": call["includesUnauthorizedLine"]})
        with run.step("pause", {"触发时机": "独立复核已发出，结果尚未返回"}) as output:
            stopped = api.post(control, {"action": "stop"})
            check(stopped["state"] == "stopped", "业务任务未进入停止状态。")
            request.release.set()

            def finding_saved():
                audit = api.get(f"{target}/verification")
                return audit if audit["sections"] else None

            held = until(finding_saved, "停止后的复核结果没有保存。")
            check(not held["published"] and api.get(target)["body"] is None, "停止期间发布了课程。")
            output.update({"任务状态": stopped["state"], "已保存核验小节": len(held["sections"]), "已发布": False})
    with run.step("resume", {"操作": "继续准备"}) as output:
        api.post(control, {"action": "resume"})

        def published():
            view = api.get(target)
            return view if view["courseVersion"] else None

        course = until(published, "恢复后没有发布课程。")
        check(len(models.calls("verify")) == 1, "恢复任务时重复调用了复核模型。")
        output.update({"发布版本": course["courseVersion"], "本次复核请求数": len(models.calls("verify"))})
    with run.step("dispute", {"已发布课程版本": course["courseVersion"]}) as output:
        audit = api.get(f"{target}/verification")
        states = [section["state"] for section in audit["sections"]]
        titles = [section["title"] for section in audit["sections"]]
        output["核验结果"] = dict(zip(titles, states))
        check(states == ["verified", "disputed", "dependency_blocked", "insufficient"],
              "争议、依赖与无效引用没有被分别标记。")
        citation = audit["sections"][0]["citations"][0]
        check(citation["fileId"] == file["id"] and citation["revision"] == file["revision"]
              and citation["at"] == 2 and citation["quote"] == SOURCE, "已发布的证据定位不一致。")
        check(BODY in course["body"] and all(title not in course["body"] for title in titles[1:]),
              "发布正文包含了被拦截的片段，或缺少已核验正文。")
        output.update({"有效引用": citation["quote"], "仅发布支持片段": True})
    return file


def study_course(run, api, models, target):
    lesson_id = target.rsplit("/", 1)[1]
    with run.step("read", {"课程 ID": lesson_id, "笔记": NOTE}) as output:
        view = api.get(target)
        check(view["courseMatchesVerification"], "实际正文与已核验版本不一致。")
        timer = api.post("/api/timer", {"action": "start", "lessonId": lesson_id})["active"]
        timer_id = timer["id"]
        sleep(1.05)
        paused = api.post("/api/timer", {"action": "pause", "timerId": timer_id})["active"]
        active = api.post("/api/timer", {"action": "resume", "timerId": timer_id})["active"]
        saved = api.post("/api/timer", {"action": "finish", "timerId": timer_id})["lastSaved"]
        note = api.put(f"{target}/note", {"content": NOTE})
        check(paused["status"] == "paused" and active["status"] == "running" and saved["id"] == timer_id
              and saved["elapsed"] > 0 and note["content"] == NOTE, "计时或笔记未保存。")
        output.update({"课程版本": view["courseVersion"], "学习计时秒数": saved["elapsed"], "笔记版本": note["revision"]})
    with run.step("ask", {"问题": QUESTION}) as output:
        api.authorize(f"{target}/assistant", {"maxCalls": 1})
        thread = api.post("/api/conversations", {"id": str(uuid4()), "lessonId": lesson_id})
        conversation = f"/api/conversations/{thread['id']}"
        api.post(f"{conversation}/questions", {"id": str(uuid4()), "text": QUESTION})

        def replied():
            return next((item for item in api.get(conversation)["messages"] if item.get("answer", {}).get("support")), None)

        message = until(replied, "课内答疑没有返回带依据的回答。")
        answer = message["answer"]
        check(message["text"] == ANSWER and answer["support"] == "grounded"
              and answer["citations"][0]["quote"] == SOURCE, "答复没有保存可定位的授权依据。")
        output.update({"答复": message["text"], "依据": answer["citations"][0]["quote"]})
    with run.step("save", {"操作": "允许后台答疑整理后标为已学"}) as output:
        api.authorize(f"{target}/consolidation", {"maxCalls": 2})
        learned = api.put(f"{target}/learned", {"learned": True})["learnedOn"]
        check(bool(learned), "已学状态没有保存。")

        def refreshed():
            view = api.get(target)
            return view if view["courseVersion"] > 1 else None

        saved = until(refreshed, "答疑整理没有生成新的已核验版本。")
        check(ADDITION in saved["body"] and saved["courseMatchesVerification"]
              and len(models.calls("consolidate")) == len(models.calls("consolidation-review")) == 1,
              "答疑补充未经过单独生成及复核。")
        output.update({"已学日期": learned, "课程版本": saved["courseVersion"], "新增小节": ADDITION})
    return learned


def review_course(run, api, models, target, learned):
    lesson_id = target.rsplit("/", 1)[1]

    def scheduled(key=None):
        return [item for item in api.get("/api/reviews")["reviews"]
                if item["lessonId"] == lesson_id and (key is None or item["key"] == key)]

    def planned_review(key):
        plan = api.get("/api/study-plan")
        return [task for day in plan["days"] for task in day["tasks"]
                if task.get("lessonId") == lesson_id and task.get("key") == key]

    with run.step("review", {"来源": "已核验课程片段", "已学日期": learned}) as output:
        preview = api.authorize(f"{target}/assessment", {"maxCalls": 3})
        check(all(item["index"] != 1 for item in preview["sections"]), "复习授权范围包含争议小节。")
        due = until(scheduled, "未生成核心能力的复习任务。")[0]
        initial = (date.fromisoformat(learned) + timedelta(days=1)).isoformat()
        check(due["kind"] == "model" and due["dueOn"] == initial, "首次复习未排在已学的次日。")
        api.post("/api/demo/next-day")
        submitted = api.post("/api/demo/next-day")["today"]
        objective = api.get(f"{target}/assessments/{due['key']}")
        check(objective["status"] == "due", "推进试用日期后，首次复习未到期。")
        group = objective["group"]
        path = f"/api/assessments/{group['id']}"
        api.put(f"{path}/draft", {"answers": [ANSWERS[0], ""]})
        check(api.get(path)["draft"] == [ANSWERS[0], ""], "复习草稿未保存。")
        with models.hold("grade") as pending:
            posted = api.post(f"{path}/submit", {"action": "submit", "answers": ANSWERS})
            check(posted["state"] == "grading" and pending.arrived.wait(12), "作答未进入后台评价。")
            waiting = scheduled(due["key"])
            check(len(waiting) == 1 and waiting[0]["status"] == "waiting" and waiting[0]["dueOn"] == initial
                  and not planned_review(due["key"]), "评分等待期间重复生成了待办或改变了首次到期日。")
            for _ in range(3):
                returned_on = api.post("/api/demo/next-day")["today"]

        def graded():
            view = api.get(path)
            return view["result"] if view["result"] else None

        result = until(graded, "后台复习评价未保存。")
        check(result["outcome"] == "passed", "核心能力答案满足要求时，独立评价未通过。")
        output.update({"初次到期": initial, "题目": [item["text"] for item in group["questions"]],
                       "提交的答案": ANSWERS, "评阅结果": result["outcome"]})
    with run.step("schedule", {"独立评价": result["outcome"], "评价前阶段": due["stage"]}) as output:
        queue = scheduled(due["key"])
        check(len(queue) == 1, "没有保留一轮原目标的正式待办。")
        following = queue[0]
        expected = (date.fromisoformat(submitted) + timedelta(days=2)).isoformat()
        check(following["stage"] == 1 and following["dueOn"] == result["dueOn"] == expected,
              "评价后的复习阶段或到期日期不正确。")
        planned = planned_review(due["key"])
        check(len(planned) == 1 and planned[0]["dueOn"] == expected, "复习计划没有采用新的排期。")
        output.update({"复习阶段": following["stage"], "下次到期": following["dueOn"], "计划中的同项数量": len(planned)})
    with run.step("schedule-delay", {"初次到期日": initial, "实际提交日": submitted}) as output:
        check(initial < submitted < expected < returned_on
              and result["submittedOn"] == submitted and result["scoredOn"] == returned_on
              and result["gradingDelayed"] and following["status"] == "due" and following.get("gradingDelayed")
              and len(models.calls("grade")) == 1 and api.get(path)["answers"] == ANSWERS,
              "迟到的评价未按实际提交日结算，或当前待办缺少评分延迟标记。")
        output.update({"评分延迟": True, "实际提交日": submitted, "实际评分日": returned_on, "真实到期日": expected,
                       "当前待办数量": len(queue), "学习计划待办数量": len(planned), "作答仍可读": True})
    return path, result, f"{target}/assessments/{due['key']}"


def recover_files(run, api, folder, target):
    note = f"{target}/note"
    with run.step("conflict", {"操作": "外部更改后从旧版本保存"}) as output:
        base = api.get(note)
        on_disk = folder / "library" / "notes" / f"{target.rsplit('/', 1)[1]}.md"
        disk_text = NOTE + "\n这是外部添加的内容。"
        draft = NOTE + "\n这是编辑器中的草稿。"
        on_disk.write_text(disk_text, encoding="utf-8", newline="\n")
        conflict = api.put(note, {"content": draft, "baseRevision": base["revision"]}, expected=409)
        path = f"{note}/conflicts/{conflict['conflictId']}"
        preview = api.get(path)
        check(preview["current"]["content"] == disk_text and preview["draft"]["content"] == draft,
              "冲突预览没有同时保留外部版本与草稿。")
        merged = f"{disk_text}\n这是编辑器中的草稿。"
        saved = api.post(path, {
            "action": "merge", "content": merged, "draftRevision": preview["draft"]["revision"],
            "externalRevision": preview["external"]["revision"] if preview["external"] else None,
            "currentRevision": preview["current"]["revision"],
        })
        check(saved["content"] == on_disk.read_text(encoding="utf-8") == merged, "确认后没有保存合并版本。")
        output.update({"旧稿提交": "HTTP 409", "磁盘版本": preview["current"]["content"],
                       "待提交草稿": preview["draft"]["content"], "合并内容": saved["content"]})
    return merged


def retry_and_delete(run, api, models):
    previous = len(models.calls("route"))
    with models.hold("draft") as receiving:
        with run.step("retry", {"模拟网络响应": "首次路线请求 HTTP 503"}) as output:
            models.fail_next("route", 503)
            identity, _ = goal_project(api, "接口暂不可用后的恢复", 3, outcome=OUTCOME)
            project = f"/api/projects/{identity}"
            check(receiving.arrived.wait(12), "路线请求暂时失败后，Worker 没有继续生成草稿。")
            attempts = [call["status"] for call in models.calls("route")[previous:]]
            route = api.get(project)["lessons"]
            check(attempts == [503, 200] and len(route) == 1, "暂时失败未恰好重试一次，或生成了重复路线。")
            output.update({"实际 HTTP 响应": attempts, "生成课程数": len(route)})
        with run.step("delete", {"场景": "草稿请求在途时确认删除"}) as output:
            task = f"{project}/generation"
            scope = api.post(f"{task}/delete-preview")
            check(scope["running"] == 1 and scope["routeNodes"] == 1, "删除预览没有显示在途请求及路线范围。")
            deleted = api.post(f"{task}/delete", {"confirmation": scope["confirmation"]})
            check(deleted["state"] == "deleted", "确认后未删除准备任务。")
            receiving.release.set()

            def acknowledged():
                view = api.get(task)
                return view if view["usage"]["reportedCalls"] == 2 else None

            result = until(acknowledged, "未观察到迟到请求的用量结算。")
            api.get(f"{task}/drafts/{route[0]['id']}", expected=404)
            api.post(f"{task}/control", {"action": "resume"}, expected=409)
            check(api.get(project)["lessons"] == route and result["usage"]["calls"] == 3
                  and all(item["state"] == "discarded" for item in result["steps"]),
                  "删除后路线丢失、迟到结果被接收或用量重复结算。")
            output.update({"删除状态": result["state"], "保留路线课程数": len(route),
                           "迟到草稿": "HTTP 404", "账本调用数": result["usage"]["calls"]})


def recovery_backup(run, api, folder, reference, target, note):
    password = token_urlsafe(24)
    with run.step("backup", {"范围": "临时工作区及合成资料"}) as output:
        api.put("/api/backups/destination", {"directory": str(folder / "acceptance-backup")})
        snapshot = api.post("/api/backups", {"password": password}, expected=201)
        archive = f"/api/backups/{snapshot['id']}"
        data = (folder / "acceptance-backup" / snapshot["name"]).read_bytes()
        check(SOURCE.encode() not in data and password.encode() not in data, "归档出现了原始资料或口令明文。")
        api.post(f"{archive}/preview", {"password": token_urlsafe(24)}, expected=422)
        preview = api.post(f"{archive}/preview", {"password": password})
        source = next(item for item in preview["files"] if item["path"] == reference["path"])
        copy = api.post(f"{archive}/files/restore", {
            "password": password, "path": source["path"], "confirmation": source["confirmation"],
        }, expected=201)
        check(copy["id"] != reference["id"] and api.get(f"/api/files/{copy['id']}")["content"] == SOURCE_TEXT,
              "单文件恢复没有产生保留来源内容的新副本。")
        result = restore_backup(api, archive, password)
        api.get(f"/api/files/{copy['id']}", expected=404)
        kept = api.get(f"/api/files/{reference['id']}")
        rollbacks = api.get("/api/backups")["items"]
        check(kept["content"] == SOURCE_TEXT and api.get(f"{target}/note")["content"] == note
              and any(item["id"] == result["rollbackBackupId"] and item["pinned"] for item in rollbacks),
              "整库恢复后的资料、笔记或回退点不完整。")
        output.update({"归档大小（字节）": len(data), "错误口令": "HTTP 422", "单文件找回": "新副本",
                       "整库恢复": result["state"], "已固定回退点": True})


def execute(run):
    with TemporaryDirectory(prefix="zhixue-acceptance-") as scratch, local_models() as (models, address):
        runtime, folder = Path(scratch) / "app", Path(scratch) / "workspace"
        with ExitStack() as sessions, ExitStack() as original:
            with run.step("setup", {"模型模式": "本机合成接口", "学习数据": "新建临时工作区"}) as output:
                prepare_product(runtime)
                api = original.enter_context(start_product(runtime, folder))
                setup_workspace(api)
                roles = {}
                for role, model in (("main", MAIN), ("review", REVIEWER)):
                    connection = api.post("/api/models/connections", {
                        "name": f"隔离验收 {role}", "provider": "custom", "protocol": "chat-completions",
                        "model": model, "endpoint": address,
                    })
                    roles[role] = connection["id"]
                api.request("PATCH", "/api/models/roles", roles)
                check(not api.get("/api/state")["recentProjects"], "验收工作区包含已有项目。")
                output.update({"正式入口": "backend.launch", "运行文件": "本机临时副本", "数据库": "新建 SQLite", "模型角色": [MAIN, REVIEWER]})
            run_library_scenario(run, api, folder)
            run_ocr_scenario(run, api, models, address)
            project, target = learning_route(run, api)
            reference = verify_course(run, api, models, project, target)
            learned = study_course(run, api, models, target)
            review, result, objective = review_course(run, api, models, target, learned)

            def reopen():
                sessions.close()
                return sessions.enter_context(start_product(runtime, folder))

            with run.step("restart", {"操作": "关闭并重新打开临时工作区"}) as output:
                original.close()
                api = reopen()
                course = api.get(target)
                check(course["courseMatchesVerification"] and ADDITION in course["body"] and course["note"] == NOTE
                      and api.get(review)["result"] == result, "重启后课程、笔记或复习评价不能完整读取。")
                reviews = api.get("/api/reviews")["reviews"]
                pending = [item for item in reviews if item["lessonId"] == target.rsplit("/", 1)[1]
                           and item["key"] == objective.rsplit("/", 1)[1]]
                check(len(pending) == 1 and pending[0]["dueOn"] == result["dueOn"]
                      and pending[0].get("gradingDelayed"), "重启后延迟待办或真实到期日丢失。")
                database = folder / "zhixue.sqlite3"
                check(database.is_file(), "重启后找不到隔离数据库。")
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                    check(db.execute("PRAGMA integrity_check").fetchone() == ("ok",), "重启后数据库完整性检查失败。")
                output.update({"课程版本": course["courseVersion"], "笔记": course["note"],
                               "下次复习": result["dueOn"], "SQLite 完整性": "ok"})
            merged = recover_files(run, api, folder, target)
            retry_and_delete(run, api, models)
            recovery_backup(run, api, folder, reference, target, merged)
            run_revision_scenario(run, api, models, folder, project, target, reference, review, result, objective, ANSWERS, reopen)
        run_budget_scenario(run, runtime, Path(scratch) / "budget-workspace")
        run_route_scenario(run, runtime, Path(scratch) / "route-workspace")
        run_restart_scenario(run, runtime, Path(scratch))
