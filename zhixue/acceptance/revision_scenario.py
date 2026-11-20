from contextlib import closing
from secrets import token_urlsafe
import sqlite3
from urllib.parse import urlencode
from uuid import uuid4

from .product import restore_backup, until
from .simulation import BODY, MAIN, PRIVATE_LINE, REVISED_BODY, REVISED_SOURCE, REVISION_NOTE_PRIVATE, REVIEWER, SOURCE
from .steps import check


FIRST_EDIT = "先固定版框，再检查墨色与字行。"
ORIGIN_TITLE = "隔离验收摘录"


def compare_source(api, target, identity):
    result = api.get(f"{target}/source-updates")
    for section in result["sections"]:
        for item in section["citations"]:
            if item["previous"]["fileId"] == identity and item["previous"]["quote"] == SOURCE:
                check(item["current"] is not None and item["status"] == "changed", "正式来源对照缺少更新后的文字。")
                return result, item
    check(False, "引用到已变更资料的旧引文未进入正式对照。")


def revision_input(update, source, expected):
    check(source["excerpt"] == expected, "修订建议的定位与磁盘实际改动不一致。")
    return {
        "baseVersion": update["version"], "section": update["section"], "instruction": update["instruction"],
        "sources": [{key: source[key] for key in ("fileId", "revision", "at", "offset")}],
    }


def signed_revision(api, endpoint, data, expected_excerpt, limit):
    preview = api.post(f"{endpoint}/preview", data)
    check(preview["main"]["model"] == MAIN and preview["reviewer"]["model"] == REVIEWER
          and preview["shareSourcesWithAuthor"] and preview["maxCalls"] == limit
          and [item["excerpt"] for item in preview["sources"]] == [expected_excerpt],
          "修订发送预览遗漏独立模型、额度或所选资料片段。")
    return {**data, "confirmation": preview["confirmation"]}


def reviewed_draft(api, models, target, choice, limit, base):
    endpoint = f"{target}/revision"
    first_call = len(models.phases())
    previous = api.get(endpoint)
    submission = signed_revision(api, endpoint, choice, REVISED_SOURCE, limit)
    check(api.get(endpoint) == previous and len(models.phases()) == first_call,
          "接受发送范围前已经排队或向模型发送。")
    queued = api.post(endpoint, submission, expected=202)

    def ready():
        current = api.get(endpoint)
        state = current["state"]
        check(state in {"pending", "running", "review_pending", "reviewing", "ready"},
              f"正式修订在核验前结束，状态为 {state}。")
        return current if state == "ready" else None

    result = until(ready, "正式 Worker 没有保存已核验的修订稿。")
    check(result["id"] == queued["id"] and bool(result["confirmation"])
          and any(change["after"] and change["after"]["text"] == REVISED_BODY for change in result["changes"]),
          "保存的修订稿未形成可确认的实际新版差异。")
    course = api.get(target)
    check((course["courseVersion"], course["body"]) == (base["courseVersion"], base["body"]),
          "核验通过后用户尚未接受，正式正文却发生变化。")
    check(models.phases()[first_call:] == ["revision-author", "revision-review"],
          "本次修订未恰好执行一次起草和一次独立复核。")
    calls = [models.calls(phase)[-1] for phase in ("revision-author", "revision-review")]
    check([item["model"] for item in calls] == [MAIN, REVIEWER]
          and all(item["includesUpdatedSource"] and not item["includesUnauthorizedLine"]
                  and not item["includesRevisionNote"] for item in calls), "修订模型收到的内容不符合确认范围。")
    return result


def match_anchor(api, lesson, old, new):
    query = urlencode({"fromVersion": old["version"], "version": new, **{key: old[key] for key in ("section", "start", "end")}})
    return api.get(f"{lesson}/revision-anchor?{query}")


def save_pair(api, endpoint, source, target, expected):
    data = {"source": source, "target": target}
    preview = api.post(f"{endpoint}/preview", data)
    check(preview["source"]["quote"] == BODY and preview["target"]["quote"] == expected,
          "关联预览未同时显示保存的原摘录和所选新版文字。")
    saved = api.put(endpoint, {**data, "confirmation": preview["confirmation"]})
    check(saved["target"] == target, "确认后关联到的新版选区有误。")


def wait_for_worker(api, label):
    identity = str(uuid4())
    probe = api.post("/api/projects", {"id": identity, "topic": label, "outcome": "观察本机课程准备"}, expected=201)
    check(probe["generationMode"] == "local", "Worker 检查意外创建了模型生成任务。")

    def ready():
        project = api.get(f"/api/projects/{identity}")
        return project["task"]["status"] == "ready" and all(item["ready"] for item in project["lessons"])

    until(ready, "重启或恢复后的 Worker 未处理新建的本地课程。")


def run_revision_scenario(run, api, models, folder, project, target, reference, review, result, objective, answers, reopen):
    lesson_id = target.rsplit("/", 1)[1]
    source_path = folder / reference["path"]
    endpoint = f"{target}/revision"
    link_endpoint = f"{target}/anchor-links"
    tasks_endpoint = "/api/study-plan/source-updates"
    with run.step("revision-update", {"操作": "外部修改已核验课程引用的合成资料"}) as output:
        base = api.get(target)
        base_version = base["courseVersion"]
        archive_path = f"{target}/versions/{base_version}"
        original_version = api.get(archive_path)
        section = next((item for item in original_version["sections"] if item["text"] == BODY), None)
        check(section is not None and len(original_version["sections"]) >= 2 and base["courseMatchesVerification"],
              "课程换版用例缺少原有的正文与答疑整理版本。")
        old_anchor = {"version": base_version, "section": section["index"], "start": 0, "end": len(BODY)}
        query = urlencode({"readingVersion": base_version, "section": section["index"], "selectionStart": 0,
                           "selectionEnd": len(BODY)})
        note_text = f"> {BODY}\n\n[查看原文 · 第 {base_version} 版](#{target.removeprefix('/api')}?{query})\n\n{REVISION_NOTE_PRIVATE}\n"
        note_id = str(uuid4())
        api.post("/api/notes", {"id": note_id, "title": ORIGIN_TITLE, "lessonIds": [lesson_id]}, expected=201)
        note_path = f"/api/notes/{note_id}"
        blank = api.get(note_path)
        saved_note = api.put(note_path, {"content": note_text, "baseRevision": blank["revision"]})
        original_note = api.get(f"{target}/note")
        old_schedule = api.get(objective)
        original_assessment = api.get(review)
        check(old_schedule["stage"] == 1 and old_schedule["dueOn"] == result["dueOn"]
              and original_assessment["result"] == result and original_assessment["answers"] == answers
              and bool(original_assessment["history"]), "缺少已完成的原作答、复习评价与排期。")
        initial_calls = len(models.phases())
        check(not any(item["lessonId"] == lesson_id for item in api.get(tasks_endpoint)), "变更前已存在来源更新待办。")
        source_path.write_text(f"验收样本\n{FIRST_EDIT}\n{PRIVATE_LINE}", encoding="utf-8")
        todo = next((item for item in api.get(tasks_endpoint) if item["lessonId"] == lesson_id), None)
        update, comparison = compare_source(api, target, reference["id"])
        check(todo is not None and todo["sectionCount"] == len(update["sections"])
              and update["version"] == base_version and update["section"] is None
              and len(update["sections"]) >= 2 and comparison["current"]["excerpt"] == FIRST_EDIT
              and len(models.phases()) == initial_calls, "资料更新未正确产生多小节待办，或未经确认发出了请求。")
        output.update({"旧版": base_version, "受影响小节": [item["title"] for item in update["sections"]],
                       "原引文": comparison["previous"]["quote"], "当前资料": comparison["current"]["excerpt"],
                       "新增模型请求数": len(models.phases()) - initial_calls})

    with run.step("revision-preview", {"操作": "预览所选资料后再次修改原件"}) as output:
        study = api.get(f"{project}/generation")
        initial_usage = study["usage"]
        # Leave capacity for another revision so the budget cannot hide a duplicate send after recovery.
        limit = initial_usage["calls"] + 6
        changed = api.post(f"{project}/generation/control", {"action": "resume", "maxCalls": limit})
        check(changed["maxCalls"] == limit, "项目没有为两次独立修订保留足够的调用额度。")
        choice = revision_input(update, comparison["current"], FIRST_EDIT)
        idle = api.get(endpoint)
        old_request = signed_revision(api, endpoint, choice, FIRST_EDIT, limit)
        source_path.write_text(f"验收样本\n{REVISED_SOURCE}\n{PRIVATE_LINE}", encoding="utf-8")
        api.post(endpoint, old_request, expected=409)
        check(len(models.phases()) == initial_calls and api.get(target)["courseVersion"] == base_version
              and api.get(endpoint) == idle and api.get(f"{project}/generation")["usage"] == initial_usage,
              "过期预览仍改变了修订状态、用量或正式课程。")
        update, comparison = compare_source(api, target, reference["id"])
        choice = revision_input(update, comparison["current"], REVISED_SOURCE)
        output.update({"提交旧预览": "HTTP 409", "原预览资料": FIRST_EDIT,
                       "重新选择资料": comparison["current"]["excerpt"], "新版正式发布": False,
                       "新增模型请求数": len(models.phases()) - initial_calls, "本任务调用上限": changed["maxCalls"]})

    with run.step("revision-keep", {"操作": "确认发送后核验，选择保留旧版"}) as output:
        first = reviewed_draft(api, models, target, choice, limit, base)
        retained = api.post(f"{endpoint}/discard", {"id": first["id"]})
        check(retained["state"] == "discarded" and api.get(target)["body"] == base["body"]
              and any(item["lessonId"] == lesson_id for item in api.get(tasks_endpoint)),
              "选择保留旧版后正文或来源待办被提前改变。")
        output.update({"核验稿状态": first["state"], "修订结果": retained["state"], "旧版仍有效": True,
                       "本次模型请求数": len(models.phases()) - initial_calls})

    with run.step("revision-accept", {"操作": "再次核验并确认接受新版"}) as output:
        second = reviewed_draft(api, models, target, choice, limit, base)
        pending_links = api.get(f"{endpoint}/anchor-impact")
        pending = next((item for item in pending_links["items"] if item["source"] == old_anchor), None)
        check(pending is not None and pending["quote"] == BODY and any(note["id"] == note_id for note in pending["notes"]),
              "接受前的影响预览未列出真实的原摘录。")
        objectives = second["objectives"]
        check(any(item["id"] == objective.rsplit("/", 1)[1] for item in objectives),
              "接受前未展示因换版而受影响的既有复习目标。")
        before = next((item["before"] for item in second["changes"] if item["before"] and item["before"]["text"] == BODY), None)
        after = next((item["after"] for item in second["changes"] if item["after"] and item["after"]["text"] == REVISED_BODY), None)
        check(before is not None and after is not None, "接受前差异未同时显示旧正文与已核验的新段落。")
        viewed = api.post(f"{endpoint}/accept", {"id": second["id"], "confirmation": second["confirmation"]})
        version = viewed["publishedVersion"]
        current = api.get(target)
        new_archive = api.get(f"{target}/versions/{version}")
        source = choice["sources"][0]
        verified_sections = new_archive["sections"]
        check(bool(verified_sections) and all(item["state"] == "verified" and any(
            ref["quote"] == REVISED_SOURCE and all(ref[key] == source[key] for key in ("fileId", "revision", "at"))
            for ref in item["citations"]
        ) for item in verified_sections), "正式新版的已核验小节未绑定本次选中的新引文。")
        check(version == base_version + 1 == current["courseVersion"] and current["courseMatchesVerification"]
              and REVISED_BODY in current["body"] and api.get(archive_path) == original_version,
              "接受新版后正式课文或旧版历史不完整。")

        def records_kept(product):
            latest = product.get(target)
            latest_review = product.get(objective)
            assessment = product.get(review)
            check(latest["courseVersion"] == version and latest["courseMatchesVerification"]
                  and latest["project"]["lessons"][0]["learnedOn"] == base["project"]["lessons"][0]["learnedOn"]
                  and product.get(f"{target}/versions/{version}") == new_archive
                  and product.get(archive_path) == original_version
                  and all(assessment[key] == original_assessment[key] for key in ("answers", "result", "history"))
                  and (latest_review["stage"], latest_review["dueOn"]) == (old_schedule["stage"], old_schedule["dueOn"])
                  and latest_review["status"] == "held",
                  "换版后旧版、已学状态或原作答与复习阶段发生异常。")
            kept = product.get(f"{target}/note")
            note = product.get(note_path)
            check((kept["revision"], kept["content"]) == (original_note["revision"], original_note["content"])
                  and (note["revision"], note["content"]) == (saved_note["revision"], note_text), "换版或关联修改了笔记原文。")

        records_kept(api)
        usage = api.get(f"{project}/generation")["usage"]
        check((usage["calls"], usage["reportedCalls"]) ==
              (initial_usage["calls"] + 4, initial_usage["reportedCalls"] + 4),
              "两次已完成的修订没有分别落账起草与独立复核。")
        output.update({"接受前正式版本": base_version, "确认后版本": version, "确认前待关联摘录": pending["quote"],
                       "新版差异": {"旧文": before["text"], "新文": after["text"]},
                       "新版核验小节数": len(verified_sections), "已发布引用": REVISED_SOURCE,
                       "受影响复习目标": [item["title"] for item in objectives],
                       "复习记录保留": True, "旧复习组状态": api.get(objective)["status"],
                       "原到期日": old_schedule["dueOn"], "接受前仍需确认": second["state"],
                       "此次发送角色": [models.calls(phase)[-1]["model"] for phase in ("revision-author", "revision-review")],
                       "两次修订已结算请求": usage["reportedCalls"] - initial_usage["reportedCalls"]})

    with run.step("revision-seen", {"操作": "单独标为已知悉"}) as output:
        waiting = api.get("/api/study-plan")["courseUpdates"]
        notice = next((item for item in waiting if item["lessonId"] == lesson_id), None)
        check(notice is not None and notice["version"] == version and notice["id"] == second["id"]
              and not any(item["lessonId"] == lesson_id for item in api.get(tasks_endpoint)),
              "已接受的新版本没有进入查看待办，或旧来源待办未处理。")
        known = api.post(f"{endpoint}/seen", {"id": notice["id"]})
        check(known["seen"] and not any(item["lessonId"] == lesson_id for item in api.get("/api/study-plan")["courseUpdates"])
              and api.get(target)["courseVersion"] == version, "标为已知悉没有独立清除待查看任务。")
        output.update({"接受新版后待查看": notice["version"], "知悉后仍生效版本": version,
                       "待查看项已解除": True})

    with run.step("revision-anchor", {"操作": "从原摘录选择新版文字后确认关联"}) as output:
        pending = next((item for item in api.get(link_endpoint)["items"] if item["source"] == old_anchor), None)
        match = match_anchor(api, target, old_anchor, version)
        check(pending is not None and pending["quote"] == BODY and match["target"] is None and match["revision"],
              "无法定位的旧摘录未进入待关联，或自动跳到了未经选择的文字。")
        selected = next(item for item in new_archive["sections"] if item["text"] == REVISED_BODY)
        new_anchor = {"version": version, "section": selected["index"], "start": 0, "end": len(REVISED_BODY)}
        save_pair(api, link_endpoint, old_anchor, new_anchor, REVISED_BODY)
        expected_target = {key: new_anchor[key] for key in ("section", "start", "end")}

        def links_kept(product):
            found = match_anchor(product, target, old_anchor, version)
            check(found["manual"] and found["target"] == expected_target
                  and not any(item["source"] == old_anchor for item in product.get(link_endpoint)["items"]),
                  "原链接没有定位到已确认的新版选区，或待关联项未关闭。")
            records_kept(product)
            accepted = product.get(endpoint)
            generation = product.get(f"{project}/generation")
            check(accepted["state"] == "accepted" and accepted["id"] == second["id"] and accepted["seen"]
                  and generation["usage"] == usage and generation["maxCalls"] >= usage["calls"] + 2
                  and not any(step["state"] in {"pending", "running", "review_pending", "reviewing", "revision_ready"}
                              for step in generation["steps"])
                  and not any(item["lessonId"] == lesson_id for item in product.get("/api/study-plan")["courseUpdates"])
                  and not any(item["lessonId"] == lesson_id for item in product.get(tasks_endpoint)),
                  "关联、恢复或重启影响了已知悉状态、原用量或已确认的任务。")
            return found["target"]

        linked = links_kept(api)
        output.update({"待关联原摘录": pending["quote"], "确认的新版文字": selected["text"],
                       "原笔记逐字一致": True, "原链接当前位置": {"版本": version, **linked}})
        observed_calls = models.phases()
        check(observed_calls[initial_calls:] == ["revision-author", "revision-review"] * 2,
              "两次明确确认以外出现了额外的模型请求。")

    with run.step("revision-restore", {"操作": "保存加密归档，修改关联，随后恢复"}) as output:
        password = token_urlsafe(24)
        api.put("/api/backups/destination", {"directory": str(folder / "revision-backups")})
        backup = api.post("/api/backups", {"password": password}, expected=201)
        backup_path = f"/api/backups/{backup['id']}"
        preview = api.post(f"{backup_path}/preview", {"password": password})
        check(preview["counts"]["backup"]["course_anchor_links"] == 1, "归档预览没有包含已确认的摘录关联。")
        alternate = {**new_anchor, "end": 4}
        save_pair(api, link_endpoint, old_anchor, alternate, REVISED_BODY[:4])
        altered = match_anchor(api, target, old_anchor, version)["target"]
        check(altered["end"] == 4, "恢复前没有成功更改关联，无法区分恢复结果。")
        restored = restore_backup(api, backup_path, password)
        wait_for_worker(api, "恢复后本地课程准备")
        linked = links_kept(api)
        check(models.phases() == observed_calls, "整库恢复后重复发送了模型请求。")
        output.update({"整库恢复": restored["state"], "备份内关联数": preview["counts"]["backup"]["course_anchor_links"],
                       "恢复前临时位置": altered, "恢复后原链接": {"版本": version, **linked},
                       "生成用量保持一致": usage, "Worker 新建本地课程": "已准备", "新增模型请求数": 0})

    with run.step("revision-restart", {"操作": "关闭隔离服务并重新打开同一工作区"}) as output:
        api = reopen()
        wait_for_worker(api, "重启后本地课程准备")
        linked = links_kept(api)
        with closing(sqlite3.connect((folder / "zhixue.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
            check(db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                  and db.execute("PRAGMA foreign_key_check").fetchall() == [],
                  "重启后的已换版工作区未通过 SQLite 完整性检查。")
        check(models.phases() == observed_calls, "重启后出现了未确认的模型重发。")
        output.update({"重新打开的版本": version, "旧版仍可读": base_version, "原笔记逐字一致": True,
                       "原链接当前位置": linked, "旧作答与原到期日保留": True,
                       "生成用量保持一致": usage, "Worker 新建本地课程": "已准备",
                       "SQLite 完整性": "ok", "新增模型请求数": 0})
