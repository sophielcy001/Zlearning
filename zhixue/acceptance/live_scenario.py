from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from backend.money_budget import MONEY_ERRORS

from .live_config import MAX_CALLS, SAMPLE_LIMITS, SAMPLES, describe_connection
from .live_runtime import WORKER_EXIT_REASONS
from .product import goal_project, prepare_product, start_product, until
from .steps import CheckFailed, check
from .usage import read_usage, read_usage_checkpoint, usage_after_stop


WAIT = 42


def load_course(run, api, sample):
    with run.step("real-route", {"主题": sample.topic, "预期成果": sample.outcome}) as output:
        identity, consent = goal_project(api, sample.topic, SAMPLE_LIMITS["course"] - 1, outcome=sample.outcome)
        project = f"/api/projects/{identity}"
        generation = f"{project}/generation"

        def installed():
            current = api.get(project)
            if current["lessons"]:
                return current
            if current["task"]["status"] == "blocked":
                raise CheckFailed(current["task"]["error"] or "真实模型路线生成失败。")
            return None

        course = until(installed, "没有在时限内收到样本学习路线。", WAIT)
        lesson = course["lessons"][0]
        output.update({"授权字段": [item["key"] for item in consent["inputs"]], "路线课程数": len(course["lessons"]),
                       "优先核验": lesson["title"], "首课目标": lesson["outcome"]})
    lesson_id = lesson["id"]
    target = f"/api/lessons/{lesson_id}"
    with run.step("real-draft", {"课程": lesson["title"]}) as output:
        def drafted():
            status = api.get(generation)
            step = next(item for item in status["steps"] if item["lessonId"] == lesson_id)
            if step["state"] == "awaiting_review":
                return step
            if status["state"] == "blocked":
                raise CheckFailed(status["error"] or "真实模型首课草稿生成失败。")
            return None

        step = until(drafted, "没有在时限内收到首课草稿。", WAIT)
        draft = api.get(f"{generation}/drafts/{lesson_id}")
        check(api.get(target)["body"] is None, "首课草稿在复核前出现在正文中。")
        output.update({"任务状态": step["state"], "生成小节": draft["sections"]})
    with run.step("real-verify", {"公开资料摘要": sample.source, "来源": sample.source_url,
                                 "定位": "第 1 行"}) as output:
        document = api.post("/api/files", {"name": sample.source_name, "content": sample.source})
        file = api.get(f"/api/files/{document['id']}?at=1")
        scope = api.authorize(f"{project}/course-review", {
            "sources": [{"fileId": file["id"], "revision": file["revision"], "at": 1}],
        })
        check(scope["sources"][0]["excerpt"] == sample.source, "独立复核的授权范围与公开资料摘要不一致。")
        api.post(f"{generation}/control", {"action": "resume", "maxCalls": SAMPLE_LIMITS["course"]})

        def verified():
            verdict = api.get(f"{target}/verification")
            if verdict["state"] == "review_failed":
                raise CheckFailed(verdict["error"] or "独立复核模型返回了不能使用的结果。")
            if verdict["sections"]:
                return verdict
            status = api.get(generation)
            if status["reason"] in MONEY_ERRORS:
                raise CheckFailed(status["error"] or MONEY_ERRORS[status["reason"]])
            return None

        audit = until(verified, "没有在时限内收到独立核验结果。", WAIT)
        output["片段核验"] = [
            {"标题": row["title"], "状态": row["state"], "原因": row["reason"],
             "引用": [citation["quote"] for citation in row["citations"]]}
            for row in audit["sections"]
        ]
    with run.step("real-publish", {"样本": sample.title, "来源": sample.source_url}) as output:
        check(any(item["state"] == "verified" for item in audit["sections"]), "独立复核未找到可以发布的有依据片段。")

        def published():
            view = api.get(target)
            return view if view["courseVersion"] else None

        course = until(published, "已核验的课程片段没有生成可读版本。", WAIT)
        saved = api.get(f"{target}/verification")
        citations = [citation for row in saved["published"] for citation in row["citations"]]
        check(course["courseMatchesVerification"] and course["body"] and citations
              and all(ref["fileId"] == document["id"] and ref["at"] == 1 and ref["quote"] in sample.source
                      for ref in citations),
              "已发布版本或原文引用未能与授权证据核对。")
        output.update({"课程版本": course["courseVersion"], "正文": course["body"],
                       "引用原句": list(dict.fromkeys(ref["quote"] for ref in citations))})
    return target, lesson_id


def read_and_learn(run, api, sample, target, lesson_id):
    with run.step("real-reading", {"操作": "打开核验版本并保存验收笔记"}) as output:
        note = "已阅读样本课程及依据。"
        api.put(f"{target}/note", {"content": note})
        current = api.get(target)
        check(current["note"] == note and current["courseMatchesVerification"], "阅读或笔记没有保留。")
        output.update({"课程版本": current["courseVersion"], "已保存笔记": current["note"]})

    with run.step("real-answer", {"问题": sample.question}) as output:
        api.authorize(f"{target}/assistant", {"maxCalls": SAMPLE_LIMITS["answer"]})
        thread = api.post("/api/conversations", {"id": str(uuid4()), "lessonId": lesson_id})
        conversation = f"/api/conversations/{thread['id']}"
        api.post(f"{conversation}/questions", {"id": str(uuid4()), "text": sample.question})

        def replied():
            for item in api.get(conversation)["messages"]:
                if answer := item.get("answer"):
                    if answer["state"] == "blocked":
                        raise CheckFailed(answer["error"] or "样本答疑已经暂停。")
                    if answer["support"]:
                        return item
            return None

        message = until(replied, "没有在时限内收到样本答疑。", WAIT)
        answer = message["answer"]
        output.update({"答复": message["text"], "依据状态": answer["support"],
                       "原文定位": [citation["quote"] for citation in answer["citations"]]})
        check(answer["support"] == "grounded" and answer["citations"], "实际答复没有可定位的依据。")

    with run.step("real-save", {"操作": "标为已学并核验答疑补充"}) as output:
        endpoint = f"{target}/consolidation"
        api.authorize(endpoint, {"maxCalls": SAMPLE_LIMITS["consolidation"]})
        learned = api.put(f"{target}/learned", {"learned": True})["learnedOn"]
        output["已学日期"] = learned

        def settled():
            return next((item for item in api.get(endpoint)["jobs"]
                         if item["state"] in {"complete", "no_changes", "blocked"}), None)

        job = until(settled, "答疑整理没有在时限内完成。", WAIT)
        output.update({"整理状态": job["state"], "更新版本": job["publishedVersion"]})
        check(job["state"] in {"complete", "no_changes"}, job["error"] or "实际答疑补充未核验通过。")
        final = api.get(target)
        verification = api.get(f"{target}/verification")
        citations = [citation for row in verification["published"] for citation in row["citations"]]
        check(final["courseMatchesVerification"] and final["body"] and citations
              and all(ref["at"] == 1 and ref["quote"] in sample.source for ref in citations),
              "学完后保存的版本或证据定位未能核对。")
        output.update({"课程版本": final["courseVersion"], "正文": final["body"],
                       "引用原句": list(dict.fromkeys(ref["quote"] for ref in citations))})
    return learned


def run_review(run, api, sample, target, lesson_id, learned):
    def reviews():
        return [item for item in api.get("/api/reviews")["reviews"] if item["lessonId"] == lesson_id]

    with run.step("real-review", {"作答材料": sample.answer}) as output:
        endpoint = f"{target}/assessment"
        cap = SAMPLE_LIMITS["assessment"]
        settings = {"maxCalls": cap}
        authorization = api.post(f"{endpoint}/preview", settings)
        sections = len({item["proof"] for item in authorization["sections"]})
        output["出题依据片段数"] = sections
        check(sections < cap, "当前已核验片段较多，本次复习额度不足以完成出题与评分。")
        api.post(f"{endpoint}/authorize", {**settings, "confirmation": authorization["confirmation"]})

        def prepared():
            jobs = [job for job in api.get(endpoint)["jobs"] if job["kind"] == "objectives"]
            for job in jobs:
                if job["state"] == "blocked":
                    raise CheckFailed(job["error"] or "模型复习题未能生成。")
            return None if jobs else reviews()

        objective = until(prepared, "已学课程没有在时限内生成复习题。", WAIT)[0]
        first_due = (date.fromisoformat(learned) + timedelta(days=1)).isoformat()
        check(objective["dueOn"] == first_due and objective["kind"] == "model", "首次复习没有排在已学的次日。")
        api.post("/api/demo/next-day")
        current = api.get(f"{target}/assessments/{objective['key']}")
        check(current["status"] == "due" and current["group"], "试用日期推进后没有到期题组。")
        group = current["group"]
        path = f"/api/assessments/{group['id']}"
        answers = [sample.answer] * len(group["questions"])
        saved = api.post(f"{path}/submit", {"action": "submit", "answers": answers})
        check(saved["state"] == "grading", "正式题组提交后没有进入评阅队列。")

        def graded():
            current = api.get(path)
            if current.get("jobState") == "blocked":
                raise CheckFailed(current.get("error") or "真实模型未返回可用的评价。")
            return current["result"]

        result = until(graded, "没有在时限内收到样本评阅结果。", WAIT)
        output.update({"题组": [item["text"] for item in group["questions"]], "评价": result["outcome"],
                       "说明": result["explanation"], "评分项": result["items"]})

    with run.step("real-schedule", {"首轮到期": first_due, "独立评价": result["outcome"]}) as output:
        following = next(item for item in reviews() if item["key"] == objective["key"])
        passed = result["outcome"] == "passed"
        expected = (date.fromisoformat(result["submittedOn"]) + timedelta(days=2 if passed else 1)).isoformat()
        check(result["outcome"] in {"passed", "failed"}
              and following["stage"] == result["stage"] == (1 if passed else 0)
              and following["dueOn"] == result["dueOn"] == expected,
              "下一阶段及复习日期未采用实际评阅结果。")
        check(api.get(path)["result"] == result, "分配下次日期后无法读取已保存的评阅。")
        output.update({"复习阶段": following["stage"], "下次日期": following["dueOn"]})


def save_usage(run, database, *, final=False):
    with run.step("real-usage", {"模型调用上限": MAX_CALLS}) as output:
        try:
            output.update(read_usage(database, final=final))
        except (OSError, SQLAlchemyError) as error:
            checkpoint = read_usage_checkpoint(run.store.usage_path(run.identity)) if final else None
            if checkpoint is not None:
                output.update({
                    "证据范围": "模型账本末次读取失败，以下为最后一次保存的用量快照，可能早于中断时刻。",
                    "采集时间": checkpoint["capturedAt"],
                    "采集时隔离服务已停止": checkpoint["serviceStopped"],
                    "当前隔离服务已停止": True,
                    **usage_after_stop(checkpoint["output"]),
                })
            raise CheckFailed("无法读取隔离工作区的模型账本。") from error
        check(output["计入调用"] <= MAX_CALLS, "样本账本超出授权的调用范围。")


def execute(run, selection):
    sample = SAMPLES[selection.sample]
    with TemporaryDirectory(prefix="zhixue-live-sample-") as scratch:
        runtime, folder = Path(scratch) / "app", Path(scratch) / "workspace"
        database = folder / "zhixue.sqlite3"
        sample_failed = False
        try:
            with closing_session(run, selection, sample, runtime, folder) as api:
                target, lesson_id = load_course(run, api, sample)
                learned = read_and_learn(run, api, sample, target, lesson_id)
                run_review(run, api, sample, target, lesson_id, learned)
        except Exception:
            sample_failed = True
            raise
        finally:
            if not sample_failed or database.is_file() or run.store.usage_path(run.identity).is_file():
                try:
                    save_usage(run, database, final=True)
                except CheckFailed:
                    # The ledger step has its own failure record. Keep the failure that stopped the sample.
                    if not sample_failed:
                        raise


@contextmanager
def closing_session(run, selection, sample, runtime, folder):
    with ExitStack() as sessions:
        with run.step("real-model", {"样本": sample.title, "授权调用上限": MAX_CALLS}) as output:
            prepare_product(runtime)
            package = runtime / "acceptance"
            package.mkdir()
            for filename in ("__init__.py", "live_runtime.py", "usage.py"):
                copyfile(Path(__file__).with_name(filename), package / filename)
            entry = ("acceptance.live_runtime", "--limit", str(MAX_CALLS),
                     "--usage-checkpoint", str(run.store.usage_path(run.identity).resolve()))
            pricing = {}
            if selection.budget:
                entry += ("--cost-limit", selection.budget.limit.model_dump_json())
                pricing = {"main": selection.budget.mainPricing, "review": selection.budget.reviewPricing}
                output["金额控制"] = selection.budget.model_dump()
            api = sessions.enter_context(start_product(runtime, folder, entry=entry, exit_reasons=WORKER_EXIT_REASONS))
            check(not api.get("/api/state")["recentProjects"], "联调没有进入空白隔离工作区。")
            if pricing:
                # Keep later lessons from competing with the sample's review for a reserved call.
                api.put("/api/generation/settings", {"dailyLimit": None, "concurrency": 1})
            roles = {}
            for role, profile in selection.connections():
                data = profile.model_dump(exclude={"apiKey"})
                if profile.apiKey is not None:
                    data["apiKey"] = profile.apiKey.get_secret_value()
                connection = api.post("/api/models/connections", data)
                roles[role] = connection["id"]
                if role in pricing:
                    api.put(f"/api/models/connections/{connection['id']}/pricing", {
                        "revision": connection["revision"], "previousPricing": None,
                        "pricing": pricing[role].model_dump(),
                    })
            api.request("PATCH", "/api/models/roles", roles)
            output.update({"主模型": describe_connection(selection.main), "复核模型": describe_connection(selection.review),
                           "工作区": "临时 SQLite 和知识库", "凭据": "运行期间保存在进程内存"})
        yield api
