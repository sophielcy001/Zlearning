from contextlib import ExitStack, closing
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from .product import setup_workspace, start_product, until
from .steps import check


TOPIC = "SQLite 验收路线"
ORIGINAL_OUTCOME = "完成一次基本查询"
NEW_OUTCOME = "用分组查询汇总每周学习记录"
NOTE = "本机路线验收私有笔记"
PRACTICE_TITLE = "汇总本周的学习记录"
INSTRUCTIONS = "按主题汇总五条记录，并核对总时长。"
COLLISION_ERROR = "实战文件写入失败。检查知识库和同名文件后重试。"
ASSISTED_OUTCOME = "用查询汇总并核对一周的学习记录"
QUERY_STAGE = "查询训练"
FOUNDATION_BRANCH = "基础回顾"
REVIEW_BRANCH = "基础核对"
SELECTION_BRANCH = "筛选练习"
PRACTICE_BRANCH = "实战"
OPTIONAL_STAGE_PRACTICE = "核对新阶段的查询结果"
OPTIONAL_STAGE_INSTRUCTIONS = "把两个查询的行数与筛选条件逐一核对。"


def course_state(api, lesson_id):
    course_path = f"/api/lessons/{lesson_id}"
    course = api.get(course_path)
    lesson = next(item for item in course["project"]["lessons"] if item["id"] == lesson_id)
    note = api.get(f"{course_path}/note")
    schedule = [
        (item["key"], item["stage"], item["dueOn"])
        for item in api.get("/api/reviews")["reviews"] if item["lessonId"] == lesson_id
    ]
    return {"version": course["version"], "readingRevision": course["readingRevision"],
            "learnedOn": lesson["learnedOn"], "noteRevision": note["revision"], "schedule": schedule}


def markdown_revisions(files):
    return [sha256(file.read_bytes()).hexdigest() for file in files]


def route_contents(api, project):
    current = api.get(project)
    return {key: current[key] for key in ("outcome", "routeVersion", "lessons")}


def send_route_command(api, project, lesson, text, expected_kind):
    thread = api.post("/api/conversations", {"id": str(uuid4()), "lessonId": lesson})
    path = f"/api/conversations/{thread['id']}/messages"
    message = {"id": str(uuid4()), "text": text}
    response = api.post(path, message)
    expected = {"kind": expected_kind, "lessonId": lesson, "projectId": project.rsplit("/", 1)[-1]}
    check(response.get("courseAction") == expected and api.post(path, message) == response,
          "课内指令没有返回课程对应的操作，或重复提交改变了会话。")


def confirm_route(api, path, payload, preview):
    signed = {**payload, "confirmation": preview["confirmation"]}
    saved = api.post(f"{path}/confirm", signed)
    check(saved["version"] == preview["nextVersion"] and saved["nodes"] == preview["after"]
          and api.post(f"{path}/confirm", signed) == saved, "确认后的路线和预览不符，或重复确认创建了新版本。")
    return saved


def apply_assistant_changes(run, api, project, first, second, baseline):
    versions = f"{project}/versions"
    route_path = f"{project}/route-order"
    before = api.get(versions)
    original = route_contents(api, project)
    route = before["routes"][0]
    order = [node["id"] for node in route["nodes"]]

    command = "这节我已经会了。"
    with run.step("route-assistant-known", {"课内指令": command}) as output:
        send_route_command(api, project, first, command, "known")
        change = {"baseVersion": route["version"], "order": order, "marks": {first: "known"}}
        preview = api.post(f"{route_path}/preview", change)
        expected = [{**node, "learningMark": "known"} if node["id"] == first else node for node in route["nodes"]]
        check(preview["before"] == route["nodes"] and preview["after"] == expected and api.get(versions) == before
              and route_contents(api, project) == original and course_state(api, first) == baseline,
              "已会预览变更了其他内容，或确认前改动了路线与学习状态。")
        marked = confirm_route(api, route_path, change, preview)
        check(api.get(project)["lessons"][0]["learningMark"] == "known" and course_state(api, first) == baseline,
              "已会标记没有保存，或原课程状态与复习排期发生变化。")
        output.update({"标记": "已会", "当前路线版本": marked["version"], "确认前路线不变": True,
                       "原已学状态保留": True, "原复习排期保留": True})

    command = "把这个知识点提前。"
    with run.step("route-assistant-earlier", {"课内指令": command}) as output:
        before = api.get(versions)
        original = route_contents(api, project)
        send_route_command(api, project, second, command, "earlier")
        previous = order.index(second)
        check(previous > 0, "样例课程已在第一位，无法验证提前操作。")
        ordered = list(order)
        ordered[previous - 1], ordered[previous] = ordered[previous], ordered[previous - 1]
        change = {"baseVersion": marked["version"], "order": ordered}
        preview = api.post(f"{route_path}/preview", change)
        nodes = {node["id"]: node for node in marked["nodes"]}
        expected = [{**nodes[identity], "order": position} for position, identity in enumerate(ordered, 1)]
        check(preview["before"] == marked["nodes"] and preview["after"] == expected and api.get(versions) == before
              and route_contents(api, project) == original and course_state(api, first) == baseline,
              "提前预览未保留其余节点，或确认前改动了阅读顺序与学习状态。")
        advanced = confirm_route(api, route_path, change, preview)
        check([item["id"] for item in api.get(project)["lessons"]] == ordered, "项目阅读顺序未保存提前结果。")
        output.update({"课程": nodes[second]["title"], "原位置": previous + 1, "新位置": previous,
                       "当前路线版本": advanced["version"], "调整后": [node["title"] for node in advanced["nodes"]]})

    command = "修改我的学习目标"
    with run.step("route-assistant-goal", {"课内指令": command, "拟修改的预期成果": ASSISTED_OUTCOME}) as output:
        before = api.get(versions)
        original = route_contents(api, project)
        send_route_command(api, project, second, command, "goal")
        goal_path = f"{project}/goal-revision"
        goal = before["goals"][0]
        change = {"baseGoalVersion": goal["version"], "baseRouteVersion": advanced["version"],
                  "values": {"outcome": ASSISTED_OUTCOME}}
        preview = api.post(f"{goal_path}/preview", change)
        check([(item["key"], item["before"], item["after"]) for item in preview["changes"]]
              == [("outcome", NEW_OUTCOME, ASSISTED_OUTCOME)] and preview["requiresModelConsent"] is False
              and api.get(versions) == before and route_contents(api, project) == original
              and course_state(api, first) == baseline, "目标预览缺少原值或新值，或确认前改动了目标与学习状态。")
        signed = {**change, "confirmation": preview["confirmation"]}
        updated = api.post(f"{goal_path}/confirm", signed)
        check(updated == {"goalVersion": goal["version"] + 1, "routeVersion": advanced["version"] + 1}
              and api.post(f"{goal_path}/confirm", signed) == updated, "目标确认未建立一个新版本。")
        after = api.get(versions)
        prior_fields = {field["key"]: field["value"] for field in goal["fields"]}
        fields = {field["key"]: field["value"] for field in after["goals"][0]["fields"]}
        check(after["goals"][1:] == before["goals"] and after["routes"][1:] == before["routes"]
              and after["routes"][0]["nodes"] == advanced["nodes"] and after["routes"][0]["goalVersion"] == updated["goalVersion"]
              and fields[after["goalDateKey"]] == prior_fields[before["goalDateKey"]]
              and api.get(project)["outcome"] == ASSISTED_OUTCOME, "目标确认后的日期、预期成果或原路线不符。")
        output.update({"目标版本": updated["goalVersion"], "当前路线版本": updated["routeVersion"],
                       "保存的预期成果": ASSISTED_OUTCOME, "原目标日期保留": True, "课程顺序保留": True})
        return after


def run_route_scenario(run, runtime: Path, folder: Path):
    with ExitStack() as session:
        with run.step("route-updates", {"项目": TOPIC, "原目标": ORIGINAL_OUTCOME, "工作区": "新建本地隔离工作区"}) as output:
            api = session.enter_context(start_product(runtime, folder))
            setup_workspace(api)
            today = date.fromisoformat(api.get("/api/state")["today"])
            original_date = (today + timedelta(days=14)).isoformat()
            deadline = (today + timedelta(days=30)).isoformat()
            thread = str(uuid4())
            api.post("/api/conversations", {"id": thread, "message": {"id": str(uuid4()), "text": "我想备考数据库。"}})
            intake = f"/api/conversations/{thread}/goal"
            goal = api.post(intake)
            submitted = api.post(f"{intake}/submit", {
                "goalId": goal["id"], "revision": goal["revision"],
                "values": {"topic": TOPIC, "outcome": ORIGINAL_OUTCOME, "deadline": original_date},
            })
            project = f"/api/projects/{submitted['projectId']}"
            versions = f"{project}/versions"

            def prepared_project():
                current = api.get(project)
                return current if current["task"]["status"] == "ready" else None

            current = until(prepared_project, "本机 Worker 未准备好原课程。")
            initial_order = [lesson["id"] for lesson in current["lessons"]]
            check(current["generationMode"] == "local" and len(initial_order) == 3
                  and all(lesson["ready"] for lesson in current["lessons"]), "隔离项目的三节本地课程未就绪。")
            first, second, third = initial_order
            courses = folder / "library" / "courses"
            original_files = [courses / f"{lesson}.md" for lesson in initial_order]
            original_revisions = markdown_revisions(original_files)
            lesson_path = f"/api/lessons/{first}"
            note = api.get(f"{lesson_path}/note")
            api.put(f"{lesson_path}/note", {"content": NOTE, "baseRevision": note["revision"]})
            api.put(f"{lesson_path}/learned", {"learned": True})
            baseline = course_state(api, first)
            check(all(baseline.values()) and api.get(f"{lesson_path}/note")["content"] == NOTE,
                  "原课正文、笔记、已学日期或复习排期未保存。")
            initial = api.get(versions)
            check([row["version"] for row in initial["goals"]] == [1]
                  and [row["version"] for row in initial["routes"]] == [1], "原目标和路线版本不符。")
            output.update({"课程数": len(initial_order), "原目标日期": original_date,
                           "已学日期": baseline["learnedOn"], "复习目标数": len(baseline["schedule"]),
                           "原目标及路线版本": [1, 1]})

        goal_path = f"{project}/goal-revision"
        goal_payload = {"baseGoalVersion": 1, "baseRouteVersion": 1, "values": {"outcome": NEW_OUTCOME, "deadline": deadline}}
        with run.step("route-goal-preview", {"新目标": NEW_OUTCOME, "目标日期": deadline}) as output:
            proposal = api.post(f"{goal_path}/preview", goal_payload)
            expected = {"outcome": (ORIGINAL_OUTCOME, NEW_OUTCOME), "deadline": (original_date, deadline)}
            changes = {row["key"]: (row["before"], row["after"]) for row in proposal["changes"]}
            check(changes == expected and len(proposal["changes"]) == len(expected)
                  and proposal["requiresModelConsent"] is False, "目标预览缺失旧值、新值或日期。")
            confirmed_goal = {**goal_payload, "confirmation": proposal["confirmation"]}
            other_values = {**goal_payload["values"], "outcome": "未经预览的变动"}
            api.post(f"{goal_path}/confirm", {**confirmed_goal, "values": other_values}, expected=409)
            check(api.get(versions) == initial and api.get(project)["outcome"] == ORIGINAL_OUTCOME,
                  "预览或拒绝确认时改动了目标或路线。")
            labels = {"outcome": "预期成果", "deadline": "目标日期"}
            output.update({"变更": [{"字段": labels[key], "原内容": before, "新内容": after}
                                    for key, (before, after) in changes.items()],
                           "不同内容的确认": "HTTP 409", "预览后仍是原版本": True})

        with run.step("route-goal-confirm", {"操作": "确认预览中的目标与日期"}) as output:
            updated = api.post(f"{goal_path}/confirm", confirmed_goal)
            check(updated == {"goalVersion": 2, "routeVersion": 2}
                  and api.post(f"{goal_path}/confirm", confirmed_goal) == updated, "重复确认产生了不同的目标版本。")
            after_goal = api.get(versions)
            check([row["version"] for row in after_goal["goals"]] == [2, 1]
                  and [(row["version"], row["goalVersion"]) for row in after_goal["routes"]] == [(2, 2), (1, 1)]
                  and after_goal["routes"][0]["nodes"] == initial["routes"][0]["nodes"]
                  and after_goal["routes"][1] == initial["routes"][0]
                  and after_goal["goals"][1] == initial["goals"][0]
                  and api.get(project)["outcome"] == NEW_OUTCOME and course_state(api, first) == baseline
                  and markdown_revisions(original_files) == original_revisions,
                  "确认目标后旧课程、笔记、已学状态或复习排期发生变化。")
            output.update({"目标版本": updated["goalVersion"], "路线版本": updated["routeVersion"],
                           "原课程顺序保留": True, "原复习排期保留": True})

        route_path = f"{project}/route-order"
        practice = {"afterId": first, "title": PRACTICE_TITLE, "outcome": "生成一张按主题汇总的表",
                    "instructions": INSTRUCTIONS, "required": False}
        route_payload = {"baseVersion": 2, "order": [first, third, second], "marks": {first: "to_reinforce"},
                         "practice": practice}
        with run.step("route-edit-preview", {"操作": "交换后两课的顺序，在第一课后插入选做实战并设置待巩固标记"}) as output:
            api.post(f"{route_path}/preview", {**route_payload, "baseVersion": 1}, expected=409)
            preview = api.post(f"{route_path}/preview", route_payload)
            after = preview["after"]
            check(len(after) == 4 and after[1].get("kind") == "practice" and after[1]["required"] is False,
                  "路线预览没有在指定位置加入选做实战。")
            practice_id = str(UUID(after[1]["id"]))
            expected_order = [first, practice_id, third, second]
            practice_path = f"/api/lessons/{practice_id}"
            practice_file = courses / f"{practice_id}.md"
            check([node["id"] for node in after] == expected_order and after[0].get("learningMark") == "to_reinforce"
                  and preview["before"] == after_goal["routes"][0]["nodes"]
                  and api.get(versions) == after_goal and not practice_file.exists(),
                  "预览的顺序、标记或原路线状态不符合选择。")
            output.update({"旧版预览": "HTTP 409", "调整前": [item["title"] for item in preview["before"]],
                           "调整后": [item["title"] for item in after], "个人标记": "待巩固", "实战类型": "选做",
                           "确认前未建文件": True})

        with run.step("route-practice-conflict", {"操作": "确认预览后，让本机 Worker 遇到预先写入的同名文件"}) as output:
            collision = "测试用同名外部文件。".encode("utf-8")
            with practice_file.open("xb") as file:
                file.write(collision)
            confirmed_route = {**route_payload, "confirmation": preview["confirmation"]}
            route = api.post(f"{route_path}/confirm", confirmed_route)
            check(route["version"] == 3 and route["goalVersion"] == 2 and route["nodes"] == after
                  and api.post(f"{route_path}/confirm", confirmed_route) == route, "路线确认遗漏节点或重复创建实战。")
            error = until(lambda: api.get(project)["lessons"][1]["preparationError"], "Worker 未报告同名文件冲突。")
            check(error == COLLISION_ERROR and not api.get(project)["lessons"][1]["ready"]
                  and practice_file.read_bytes() == collision, "Worker 未保留原文件或未返回预期冲突。")
            output.update({"路线版本": route["version"], "实际准备结果": error, "同名文件原样保留": True})

        with run.step("route-practice-retry", {"操作": "移走合成冲突文件，使用正式接口重试"}) as output:
            practice_file.unlink()
            api.post(f"{practice_path}/practice/retry")
            until(lambda: api.get(project)["lessons"][1]["ready"], "重试后实战仍未就绪。")
            content = practice_file.read_bytes()
            digest = sha256(content).hexdigest()
            database = (folder / "zhixue.sqlite3").as_uri() + "?mode=ro"
            with closing(sqlite3.connect(database, uri=True)) as db:
                binding = db.execute("SELECT path, revision, missing FROM library_locations WHERE id=?",
                                     (f"course-{practice_id}",)).fetchone()
            check(binding == (practice_file.relative_to(folder).as_posix(), digest, 0), "Worker 未登记实战文件。")
            file = api.get(f"/api/files/course-{practice_id}")
            detail = api.get(practice_path)
            learned = api.put(f"{practice_path}/learned", {"learned": True})["learnedOn"]
            check(file["name"] == PRACTICE_TITLE and file["revision"] == digest
                  and INSTRUCTIONS in content.decode("utf-8") and INSTRUCTIONS in detail["body"]
                  and not detail["objectives"] and learned, "实战 Markdown、知识库登记或已学状态不完整。")
            output.update({"知识库登记": file["name"], "Markdown SHA-256": digest, "已学日期": learned})

        with run.step("route-edit-restart", {"操作": "结束正式服务并重新打开同一隔离工作区"}) as output:
            session.close()
            api = session.enter_context(start_product(runtime, folder))
            saved = api.get(versions)
            check(saved["goals"] == after_goal["goals"] and saved["routes"] == [route, *after_goal["routes"]]
                  and api.get(project)["outcome"] == NEW_OUTCOME, "重启后目标与路线历史无法完整读回。")
            date_field = next(field for field in saved["goals"][0]["fields"] if field["key"] == saved["goalDateKey"])
            lessons = api.get(project)["lessons"]
            persisted_practice = api.get(practice_path)
            persisted_file = api.get(f"/api/files/course-{practice_id}")
            check([item["id"] for item in lessons] == expected_order and lessons[0]["learningMark"] == "to_reinforce"
                  and lessons[1]["learnedOn"] == learned and date_field["value"] == deadline
                  and INSTRUCTIONS in (persisted_practice["body"] or "") and persisted_file["revision"] == digest
                  and sha256(practice_file.read_bytes()).hexdigest() == digest,
                  "重启后的实战文件、顺序、标记或学习日期发生变化。")
            check(course_state(api, first) == baseline and api.get(f"{lesson_path}/note")["content"] == NOTE
                  and markdown_revisions(original_files) == original_revisions
                  and not any(item["lessonId"] == practice_id for item in api.get("/api/reviews")["reviews"]),
                  "旧课、笔记、排期变化，或选做实战产生了复习任务。")
            output.update({"当前目标版本": saved["goals"][0]["version"], "当前路线版本": saved["routes"][0]["version"],
                           "目标日期": date_field["value"], "实战 ID": lessons[1]["id"], "实战已学日期": learned,
                           "原课程文件一致": len(original_files), "原笔记修订一致": True,
                           "原复习排期保留": True, "实战未新增复习任务": True})

        before_groups = apply_assistant_changes(run, api, project, first, second, baseline)
        original = route_contents(api, project)
        order = [node["id"] for node in before_groups["routes"][0]["nodes"]]
        current_nodes = {node["id"]: node for node in before_groups["routes"][0]["nodes"]}
        assessment_path = f"{project}/stage-assessments"
        placements = {
            first: {"stage": {"name": QUERY_STAGE}, "branch": FOUNDATION_BRANCH},
            second: {"stage": {"name": QUERY_STAGE}, "branch": SELECTION_BRANCH},
            practice_id: {"stage": {"name": QUERY_STAGE}, "branch": PRACTICE_BRANCH},
        }
        change = {"baseVersion": before_groups["routes"][0]["version"], "order": order, "placements": placements,
                  "practice": {"afterId": practice_id, "title": OPTIONAL_STAGE_PRACTICE,
                               "outcome": "核对并记录每个筛选条件的结果", "instructions": OPTIONAL_STAGE_INSTRUCTIONS,
                               "required": False}}
        with run.step("route-group-preview", {"阶段": QUERY_STAGE, "分支": [FOUNDATION_BRANCH, SELECTION_BRANCH, PRACTICE_BRANCH],
                                             "操作": "先提交旧阶段答题，再预览合并范围", "新增选做实战": OPTIONAL_STAGE_PRACTICE}) as output:
            existing = api.get(assessment_path)["items"]
            check(len(existing) == 1 and existing[0]["currentScope"] and existing[0]["eligible"]
                  and existing[0]["attemptCount"] == 0, "样例的原阶段测评未就绪。")
            old_path = f"{assessment_path}/{existing[0]['id']}"
            answers = [2, 0]
            old = api.post(f"{old_path}/attempts", {
                "id": str(uuid4()), "kind": "quiz", "expectedAttempt": 0, "answers": answers,
            })
            attempt = old["attempts"][0]
            check(old["attemptCount"] == 1 and attempt["kind"] == "quiz" and old["status"] == "needs_work"
                  and [question["answer"] for question in attempt["questions"]] == answers,
                  "旧阶段答题和所保存的评阅记录不符。")
            preview = api.post(f"{route_path}/preview", change)
            before_scopes = [{"phase": current_nodes[identity]["phase"], "lessons": [current_nodes[identity]["title"]]}
                             for identity in (first, second)]
            title_pair = [current_nodes[identity]["title"] for identity in (first, second)]
            after_scopes = [{"phase": QUERY_STAGE, "lessons": title_pair}]
            check(preview["stageImpact"] == {"before": before_scopes, "after": after_scopes},
                  "阶段预览的旧范围或合并后必学范围不符。")
            proposed = {node["id"]: node for node in preview["after"]}
            stage_id = proposed[first].get("stageId")
            position = order.index(practice_id) + 1
            inserted = preview["after"][position]
            optional_id = str(UUID(inserted["id"]))
            optional_file = courses / f"{optional_id}.md"
            grouped_order = [*order[:position], optional_id, *order[position:]]
            check(stage_id and [node["id"] for node in preview["after"]] == grouped_order
                  and all(proposed[identity]["stageId"] == stage_id and proposed[identity]["phase"] == QUERY_STAGE
                          and proposed[identity]["branch"] == placement["branch"] for identity, placement in placements.items())
                  and proposed[third] == {**current_nodes[third], "order": grouped_order.index(third) + 1},
                  "阶段预览改变了既有课的阅读顺序或遗漏了新的归属。")
            check(optional_id not in current_nodes and inserted.get("kind") == "practice" and inserted["required"] is False
                  and inserted["phase"] == QUERY_STAGE and inserted["stageId"] == stage_id and inserted["branch"] == PRACTICE_BRANCH
                  and inserted["title"] == OPTIONAL_STAGE_PRACTICE and not optional_file.exists(),
                  "新增选做实战未跟随移动后的锚点，或预览时已写入文件。")
            changed_branch = {**placements, first: {**placements[first], "branch": "未经预览的分支"}}
            api.post(f"{route_path}/confirm", {**change, "placements": changed_branch,
                                              "confirmation": preview["confirmation"]}, expected=409)
            check(api.get(versions) == before_groups and api.get(old_path) == old
                  and route_contents(api, project) == original and course_state(api, first) == baseline,
                  "阶段预览或拒绝确认时写入了路线或影响了原测评。")
            output.update({"旧测评范围": [{"阶段": scope["phase"], "课程": scope["lessons"]} for scope in before_scopes],
                           "新测评范围": [{"阶段": scope["phase"], "课程": scope["lessons"]} for scope in after_scopes],
                           "旧测评已有答题": old["attemptCount"], "不同内容的确认": "HTTP 409", "确认前路线不变": True})

        with run.step("route-group-confirm", {"操作": "保留新实战未学，完成第二节必学课，再调整必学课和原实战的分支"}) as output:
            grouped = confirm_route(api, route_path, change, preview)
            historical = api.get(old_path)
            historical_items = api.get(assessment_path)["items"]
            check(historical == {**old, "eligible": False, "currentScope": False}
                  and [item["id"] for item in historical_items] == [old["id"]]
                  and not historical_items[0]["currentScope"] and not historical_items[0]["eligible"],
                  "历史测评不可从列表打开，或合并阶段在课程未完成时出现。")
            api.post(f"{old_path}/attempts", {"id": str(uuid4()), "kind": "quiz", "expectedAttempt": 1, "answers": [1, 2]},
                     expected=409)
            optional = until(lambda: next((item for item in api.get(project)["lessons"]
                                           if item["id"] == optional_id and item["ready"]), None), "新增选做实战未就绪。")
            optional_content = optional_file.read_bytes()
            check(not optional["required"] and optional["learnedOn"] is None
                  and OPTIONAL_STAGE_INSTRUCTIONS in optional_content.decode("utf-8"), "新增选做实战未以未学状态就绪。")
            learned_second = api.put(f"/api/lessons/{second}/learned", {"learned": True})["learnedOn"]
            assessment_items = api.get(assessment_path)["items"]
            active = [item for item in assessment_items if item["currentScope"]]
            check(learned_second and len(active) == 1 and active[0]["eligible"]
                  and active[0]["id"] != old["id"] and active[0]["routeVersion"] == grouped["version"],
                  "完成合并后的必学课程时未建立唯一的新测评。")
            current_path = f"{assessment_path}/{active[0]['id']}"
            assessment = api.get(current_path)
            check([criterion["lessonId"] for criterion in assessment["criteria"]] == [first, second],
                  "新测评未覆盖合并后的两节必学课。")
            branch_change = {"baseVersion": grouped["version"], "order": grouped_order, "placements": {
                first: {"stage": {"anchorId": first}, "branch": REVIEW_BRANCH},
                practice_id: {"stage": {"anchorId": first}, "branch": None},
            }}
            branch_preview = api.post(f"{route_path}/preview", branch_change)
            check("stageImpact" not in branch_preview, "只调整分支时改变了测评范围。")
            branch_version = confirm_route(api, route_path, branch_change, branch_preview)
            lessons = {item["id"]: item for item in api.get(project)["lessons"]}
            check(api.get(current_path) == assessment and api.get(old_path) == historical
                  and api.get(assessment_path)["items"] == assessment_items
                  and lessons[first]["branch"] == REVIEW_BRANCH and lessons[practice_id]["branch"] is None
                  and lessons[optional_id]["learnedOn"] is None and lessons[practice_id]["stageId"] == stage_id,
                  "分支调整改变了阶段测评，或调整后的分支与未学实战状态不完整。")
            output.update({"合并路线版本": grouped["version"], "当前路线版本": branch_version["version"],
                           "历史测评尝试数": historical["attemptCount"], "向旧范围重新答题": "HTTP 409",
                           "新测评课程": title_pair, "分支调整沿用阶段测评": True, "已清空实战分支": True,
                           "必学课分支": REVIEW_BRANCH, "未学选做实战": OPTIONAL_STAGE_PRACTICE})

        with run.step("route-group-restart", {"操作": "退出并重启正式服务，在同一工作区读回旧答题与新测评"}) as output:
            session.close()
            api = session.enter_context(start_product(runtime, folder))
            saved = api.get(versions)
            check(saved["routes"] == [branch_version, grouped, *before_groups["routes"]]
                  and saved["goals"] == before_groups["goals"], "重启后原路线或目标版本发生变化。")
            refreshed = api.get(project)
            check(refreshed["routeVersion"] == branch_version["version"] and refreshed["outcome"] == ASSISTED_OUTCOME
                  and [node["id"] for node in refreshed["lessons"]] == grouped_order
                  and [(node["phase"], node["stageId"], node["branch"]) for node in refreshed["lessons"][:4]]
                  == [(QUERY_STAGE, stage_id, REVIEW_BRANCH), (QUERY_STAGE, stage_id, None),
                      (QUERY_STAGE, stage_id, PRACTICE_BRANCH), (QUERY_STAGE, stage_id, SELECTION_BRANCH)]
                  and refreshed["lessons"][0]["learningMark"] == "known"
                  and refreshed["lessons"][1]["learnedOn"] == learned and refreshed["lessons"][2]["learnedOn"] is None
                  and refreshed["lessons"][3]["learnedOn"] == learned_second,
                  "重启后分支、顺序、个人标记或已学日期未读回。")
            check(api.get(old_path) == historical and api.get(current_path) == assessment
                  and api.get(assessment_path)["items"] == assessment_items,
                  "重启后未读回测评列表、旧答题和新测评范围。")
            check(course_state(api, first) == baseline and api.get(f"{lesson_path}/note")["content"] == NOTE
                  and markdown_revisions(original_files) == original_revisions
                  and api.get(f"/api/files/course-{practice_id}")["revision"] == digest
                  and sha256(practice_file.read_bytes()).hexdigest() == digest and optional_file.read_bytes() == optional_content,
                  "重启后原笔记、课程 Markdown、复习排期或实战文件发生变化。")
            database = (folder / "zhixue.sqlite3").as_uri() + "?mode=ro"
            with closing(sqlite3.connect(database, uri=True)) as db:
                check(db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                      and not db.execute("PRAGMA foreign_key_check").fetchall(), "重启后 SQLite 完整性检查失败。")
            output.update({"当前路线版本": branch_version["version"], "目标版本": saved["goals"][0]["version"],
                           "阶段": QUERY_STAGE, "分支": [REVIEW_BRANCH, PRACTICE_BRANCH, SELECTION_BRANCH],
                           "原课程文件一致": len(original_files), "原笔记修订一致": True, "原复习排期保留": True,
                           "旧测评尝试保留": True, "新测评范围": title_pair, "实战文件保留": True,
                           "SQLite 完整性": "ok"})
