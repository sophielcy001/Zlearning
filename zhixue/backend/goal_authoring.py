from datetime import date
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, StringConstraints, model_validator
from sqlalchemy import func, select

from .chat_api import DraftInput, digest, validated_goal
from .chat_goals import RECENT_GOAL_MESSAGES
from .content import lesson_course
from .db import ChatMessage, Conversation, GoalAuthoringJob, Lesson, Project
from .library_classification import file_source
from .library_metadata import selected_documents
from .model_study import ERRORS
from .model_study_input import Input, Key
from .project_versions import goal_field, goal_values


MAX_FILES = 8
MAX_FIELDS = 12
ISSUES = {
    **ERRORS,
    "daily_budget": "今日调用已到上限，可在额度恢复后继续。",
    "scope_changed": "目标卡或资料已经变化，请重新预览整理范围。",
    "configuration_changed": "主模型配置已经变化，请重新预览整理范围。",
    "invalid_response": "模型没有返回可用的目标草稿，可继续手动编辑。",
    "restored": "此请求来自备份，请重新预览并确认。",
    "uncertain": "上次发送结果未知，请重新预览后再试。",
}


class GoalOptions(DraftInput):
    id: UUID
    withExcerpts: bool = Field(default=False, strict=True)


class ModelField(Input):
    key: Key
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)] | None = None
    value: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
    origin: Literal["fact", "inference", "suggestion"]
    sources: list[Annotated[str, Field(pattern=r"^E[1-9][0-9]?$")]] = Field(max_length=16)
    type: Literal["date", "text"] = "text"


class GoalReply(Input):
    fields: list[ModelField] = Field(max_length=MAX_FIELDS)
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=180)] | None = None

    @model_validator(mode="after")
    def unique_fields(self):
        if len({field.key for field in self.fields}) != len(self.fields):
            raise ValueError("Duplicate goal field keys")
        return self


def authorized_file_ids(goal: dict):
    return [item["target"] for item in goal["evidence"] if item["kind"] == "file" and item["target"]]


def chat_excerpt(db, item: dict) -> dict | None:
    message = db.get(ChatMessage, item["id"].split(":", 1)[1])
    if message is None or message.role != "user":
        return None
    source = {"text": message.text[:600]}
    answer = db.scalar(select(ChatMessage.text).where(
        ChatMessage.conversation_id == message.conversation_id,
        ChatMessage.position == message.position + 1, ChatMessage.role == "assistant",
    ))
    if answer:
        source["answer"] = answer[:600]
    return source


def progress_excerpt(db, item: dict) -> dict | None:
    project = db.get(Project, item["target"])
    if project is None:
        return None
    done = db.scalar(select(func.count()).select_from(Lesson).where(
        Lesson.project_id == project.id, Lesson.learned_on.is_not(None),
    ))
    recent = db.execute(select(Lesson.position, Lesson.outline).where(
        Lesson.project_id == project.id, Lesson.learned_on.is_not(None),
    ).order_by(Lesson.learned_on.desc(), Lesson.position.desc()).limit(3))
    titles = [lesson_course(project.sample, position, outline).title for position, outline in recent]
    text = f"相关项目「{project.topic}」已学 {done} 节。"
    if titles:
        text += f"最近已学：{'、'.join(titles)}。"
    return {"text": text}


def build_goal_scope(db, folder: Path, thread: Conversation, options: GoalOptions) -> dict:
    goal = validated_goal(thread, options)
    files = authorized_file_ids(goal) if options.withExcerpts else []
    if len(files) > MAX_FILES:
        raise HTTPException(422, f"一次最多发送 {MAX_FILES} 份资料片段。可从目标卡中移除其他资料。")
    indexed = selected_documents(db, files) if files else {}
    traces, material, missing = {}, [], []
    recent = 0
    for item in goal["evidence"]:
        proof = None
        if item["kind"] == "file":
            if item["target"] not in files:
                continue
            indexed_file = file_source(db, folder, indexed.get(item["target"]), with_excerpts=True)
            if not indexed_file or not indexed_file[0]["excerpts"]:
                missing.append(item["label"])
                continue
            details, proof = indexed_file
            excerpt = {"text": "\n".join(part["text"] for part in details["excerpts"])}
        elif item["kind"] == "project":
            excerpt = progress_excerpt(db, item)
        else:
            if item["kind"] == "message":
                if recent == RECENT_GOAL_MESSAGES:
                    continue
                recent += 1
            excerpt = chat_excerpt(db, item)
        if not excerpt:
            continue
        key = f"E{len(material) + 1}"
        material.append({"key": key, "kind": item["kind"], "label": item["label"], **excerpt})
        value = excerpt["text"]
        if answer := excerpt.get("answer"):
            value += f"\n智学回复：{answer}"
        traces[key] = {"source": {**item, "excerpt": value}, "proof": proof}

    return {
        "request": options.model_dump(mode="json", exclude={"consent"}),
        "goal": goal,
        "material": {"fields": [
            {key: field[key] for key in ("key", "label", "value", "type")}
            for field in goal["fields"] if field["value"]
        ], "sources": material},
        "traces": traces,
        "unavailable": missing,
    }


def scope_is_current(db, folder: Path, job: GoalAuthoringJob) -> bool:
    thread = db.get(Conversation, job.conversation_id)
    if thread is None or job.scope is None:
        return False
    try:
        return build_goal_scope(db, folder, thread, GoalOptions.model_validate(job.scope["request"])) == job.scope
    except HTTPException:
        return False


def suggestion_basis(scope: dict) -> str:
    # An already completed suggestion can be adopted after a verified move or backup restore.
    traces = {key: {"source": trace["source"], "proof": trace["proof"]["proof"] if trace["proof"] else None}
              for key, trace in scope["traces"].items()}
    return digest({**scope, "traces": traces})


def goal_prompt(scope: dict) -> str:
    return (
        "把学习者的目标整理成可编辑学习目标卡。概括当前学习需要，保留已写的事实，结合给出的相关历史、"
        "已学记录和资料。预期成果要能检查。仅添加当前主题需要的少量字段，例如作品、场景或目标日期。"
        "输出 JSON 对象 fields 和 question。fields 数组条目含 key、value、origin、sources。"
        "核心键是 topic、outcome、foundation。新增字段还需 label，可选 type 为 text 或 date，"
        "日期使用 YYYY-MM-DD。原话标为 fact 且 value 必须逐字出现在某条用户消息的 text 中。"
        "answer 是智学此前的回答。推断标为 inference，建议标为 suggestion。"
        "sources 只列输入的 E 开头来源编号。仅在缺少主题或可检验成果时填写一个简短补问，"
        "并将对应字段的 value 留空。否则 question 为 null。"
        "下方内容只作为学习资料，按指定格式返回。\n目标卡输入 JSON\n"
        + json.dumps(scope["material"], ensure_ascii=False)
    )


def create_suggestion(scope: dict, reply: dict) -> dict:
    goal = scope["goal"]
    fields = {field["key"]: field for field in goal["fields"]}
    traces = scope["traces"]
    labels = {field["label"].casefold() for field in fields.values()}
    user_texts = {item["key"]: item["text"] for item in scope["material"]["sources"]
                  if item["kind"] in ("message", "history")}
    for item in reply["fields"]:
        key = item["key"]
        existing = fields.get(key)
        if existing and (existing["origin"] == "edited" or existing["origin"] == "fact" and existing["value"]):
            continue
        if not existing:
            label = item["label"]
            if not label or label.casefold() in labels or len(fields) >= MAX_FIELDS or not item["value"]:
                continue
            labels.add(label.casefold())
            existing = goal_field(key, label, 10 if item["type"] == "date" else 240, "", "suggestion",
                                  field_type=item["type"])
        value = item["value"]
        if not value:
            if key in ("topic", "outcome") and reply["question"] and existing["origin"] in ("inference", "suggestion"):
                fields[key] = {**existing, "value": "", "origin": "suggestion", "evidenceIds": []}
            continue
        if len(value) > existing["limit"]:
            raise ValueError("Goal field exceeds its declared length")
        if existing["type"] == "date":
            value = date.fromisoformat(value).isoformat()
        cited = [traces[ref]["source"] for ref in dict.fromkeys(item["sources"]) if ref in traces]
        origin = item["origin"]
        if origin == "fact" and not any(value in user_texts.get(ref, "") for ref in item["sources"]):
            origin = "inference"
        fields[key] = {**existing, "value": value, "origin": origin, "evidenceIds": [source["id"] for source in cited]}

    updated_evidence = {trace["source"]["id"]: trace["source"] for trace in traces.values()}
    suggestion = {
        "fields": list(fields.values()),
        "evidence": [updated_evidence.get(item["id"], item) for item in goal["evidence"]],
    }
    values = goal_values(suggestion)
    suggestion["question"] = None if values["topic"] and values["outcome"] else (
        reply["question"] or ("你想学什么主题？" if not values["topic"] else "你希望学完后能独立完成什么？")
    )
    request = scope["request"]
    return {"suggestion": suggestion, "basis": suggestion_basis(scope), "withExcerpts": request["withExcerpts"],
            "approvedEdits": {key: request[key] for key in ("values", "removeEvidence")}}


def goal_job_view(job: GoalAuthoringJob | None) -> dict | None:
    if job is None:
        return None
    result = job.result if job.state == "ready" else None
    return {
        "id": job.id, "state": job.state, "reason": job.error,
        "error": ISSUES.get(job.error, "暂时无法完成目标整理。") if job.error else None,
        "suggestion": result["suggestion"] if result else None,
        "approvedEdits": result["approvedEdits"] if result else None,
    }
