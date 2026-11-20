from datetime import UTC, datetime
from hashlib import sha256
from itertools import islice
import json
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select

from .chat_goals import EXAMPLE_MESSAGE, RECENT_GOAL_MESSAGES, add_goal_message, edit_goal, evidence, make_goal
from .chat_model import model_reply_metadata
from .content import is_model_lesson, lesson_course
from .db import ChatAttachment, ChatMessage, Conversation, GoalVersion, ImportedFile, Lesson, Project, transaction
from .lesson_assistant import thread_metadata
from .model_costs import MoneyLimit
from .model_study_input import CallLimit, GenerationConsent, fields_to_send, preview_consent, verify_consent
from .money_budget import set_task_limit
from .project_versions import core_fields, create_local_project, create_model_project, goal_values
from .teaching_style import teaching_style


MESSAGE_PAGE = 60
HISTORY_PAGE = 24
ChatText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
COURSE_ACTIONS = {
    "这节我已经会了": "known", "这节我会了": "known",
    "把这个知识点提前": "earlier", "把这节提前": "earlier",
    "修改我的学习目标": "goal", "修改学习目标": "goal",
}
COURSE_ACTION_REPLIES = {
    "known": "可以预览本节的「已会」标记。已学日期和复习记录会继续保留。",
    "earlier": "可以预览把本节向前移动一位，再确认新顺序。",
    "goal": "可以编辑当前项目的学习目标和日期，预览后确认。",
}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageInput(Input):
    id: UUID
    text: ChatText


class NewConversation(Input):
    id: UUID
    message: MessageInput | None = None
    lessonId: UUID | None = None
    sample: bool = False
    attachments: list[UUID] = Field(default_factory=list, max_length=100)


class DraftRevision(Input):
    goalId: UUID
    revision: Annotated[int, Field(strict=True, ge=1)]


class DraftInput(DraftRevision):
    values: dict[str, Annotated[str, Field(max_length=500)]] = Field(max_length=12)
    removeEvidence: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=120)


class ModelPreview(DraftInput):
    maxCalls: CallLimit | None = None
    maxCost: MoneyLimit | None = None
    allowBackup: bool = False


class SubmitGoal(DraftInput):
    modelConsent: GenerationConsent | None = None


class LegacyMessage(Input):
    role: Literal["user", "demo"]
    text: Annotated[str, Field(min_length=1, max_length=2000)]


class LegacyGoal(Input):
    id: UUID
    topic: Annotated[str, Field(max_length=180)]
    outcome: Annotated[str, Field(max_length=500)]
    foundation: Annotated[str, Field(max_length=300)] = ""


class LegacyConversation(Input):
    messages: list[LegacyMessage] = Field(max_length=20_000)
    goal: LegacyGoal | None = None


def digest(values: dict) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(raw).hexdigest()


def timestamp(moment: datetime) -> str:
    return moment.replace(tzinfo=UTC).isoformat()


def conversation_view(thread: Conversation) -> dict:
    return {
        "id": thread.id, "title": thread.title, "lessonId": thread.lesson_id,
        "createdAt": timestamp(thread.created_at), "updatedAt": timestamp(thread.updated_at),
    }


def thread_page(db, thread: Conversation, before: int | None = None, *, course_action: dict | None = None) -> dict:
    query = select(ChatMessage).where(ChatMessage.conversation_id == thread.id)
    if before is not None:
        query = query.where(ChatMessage.position < before)
    messages = db.scalars(query.order_by(ChatMessage.position.desc()).limit(MESSAGE_PAGE + 1)).all()
    selected = messages[:MESSAGE_PAGE]
    lesson = db.get(Lesson, thread.lesson_id) if thread.lesson_id else None
    model_lesson = lesson is not None and is_model_lesson(lesson.outline)
    metadata = model_reply_metadata(db, [item.id for item in selected]) if selected and not lesson else {}
    if model_lesson:
        project = db.get(Project, lesson.project_id)
        metadata = thread_metadata(db, [item.id for item in selected], project.paused)
    attachments = db.execute(select(ImportedFile.id, ImportedFile.name).join(
        ChatAttachment, ChatAttachment.file_id == ImportedFile.id,
    ).where(ChatAttachment.conversation_id == thread.id).order_by(ChatAttachment.added_at, ImportedFile.id))
    projects = db.execute(select(Project.id, Project.topic).join(
        GoalVersion, GoalVersion.project_id == Project.id,
    ).where(GoalVersion.version == 1, GoalVersion.conversation_id == thread.id).order_by(Project.created_at.desc(), Project.id))
    return {
        **conversation_view(thread), "goal": thread.goal,
        "assistantMode": "model" if model_lesson else "local" if lesson else None,
        "messages": [
            {"id": item.id, "position": item.position, "role": item.role, "text": item.text,
             "createdAt": timestamp(item.created_at), **metadata.get(item.id, {}),
             **({"courseCommand": True} if saved_course_action(db, thread, item) else {})}
            for item in reversed(selected)
        ],
        "olderBefore": selected[-1].position if len(messages) > MESSAGE_PAGE else None,
        "attachments": [{"id": identity, "name": name} for identity, name in attachments],
        "projects": [{"id": identity, "topic": title} for identity, title in projects],
        **({"courseAction": course_action} if course_action else {}),
    }


def get_thread(db, identity: UUID | str) -> Conversation:
    thread = db.get(Conversation, str(identity))
    if thread is None:
        raise HTTPException(404, "找不到这段对话。")
    return thread


def title_for(text: str) -> str:
    line = " ".join(text.split())[:64]
    return line or "新对话"


def linked_course_action(db, thread: Conversation, text: str) -> dict | None:
    command = "".join(text.split()).rstrip("。！.!").removeprefix("请")
    kind = COURSE_ACTIONS.get(command)
    if kind and thread.lesson_id:
        lesson = db.get(Lesson, thread.lesson_id)
        if lesson is not None:
            return {"kind": kind, "lessonId": lesson.id, "projectId": lesson.project_id}
    return None


def course_reply_id(message_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"zhixue:course-command:{message_id}"))


def saved_course_action(db, thread: Conversation, message: ChatMessage | None) -> dict | None:
    if message is None or message.role != "user" or message.conversation_id != thread.id:
        return None
    action = linked_course_action(db, thread, message.text)
    if action is None:
        return None
    reply = db.get(ChatMessage, course_reply_id(message.id))
    if reply and reply.conversation_id == thread.id and reply.role == "assistant" and reply.position == message.position + 1:
        return action
    return None


def reply(db, thread: Conversation, text: str) -> str:
    guided = teaching_style(db) == "guided"
    if text == EXAMPLE_MESSAGE:
        if guided:
            return "如果用一张表存学习记录，一行会代表什么？先挑一条记录试着写下来。"
        return "可以从理解表、行和列开始，再练习筛选与排序。"
    if thread.lesson_id:
        lesson = db.get(Lesson, thread.lesson_id)
        project = db.get(Project, lesson.project_id)
        objective = lesson_course(project.sample, lesson.position, lesson.outline).outcome
        if guided:
            return f"本节的目标是「{objective}」。你能先举一个与这项目标有关的例子，再指出最不确定的一步吗？"
        return f"本节的目标是「{objective}」。可以对照这项目标，说明你卡在哪一步。"
    if guided:
        return "先想想自己已经能解释或完成什么。哪一步卡住了？可以从一个具体例子开始。"
    return "可以先确定一个能检查的成果，例如说明一个概念，或独立完成一个小练习。需要安排路线时，可以把问题整理成学习目标。"


def add_exchange(db, thread: Conversation, data: MessageInput, now: datetime,
                 *, role: str, reply_text: str, course_reply: str | None = None) -> tuple[ChatMessage, ChatMessage]:
    last = db.scalar(select(func.max(ChatMessage.position)).where(ChatMessage.conversation_id == thread.id))
    position = (last or 0) + 1
    item = ChatMessage(id=str(data.id), conversation_id=thread.id, position=position, role="user",
                       text=data.text, created_at=now)
    answer = ChatMessage(id=course_reply or str(uuid4()), conversation_id=thread.id, position=position + 1,
                         role=role, text=reply_text, created_at=now)
    db.add_all([item, answer])
    if course_reply is None:
        add_goal_message(thread, item.id, item.text)
    if position == 1 and not thread.lesson_id:
        thread.title = title_for(data.text)
    thread.updated_at = now
    return item, answer


def append_message(db, thread: Conversation, data: MessageInput, now: datetime,
                   *, course_action: dict | None = None) -> tuple[ChatMessage, dict | None]:
    saved = db.get(ChatMessage, str(data.id))
    if saved is not None:
        if saved.conversation_id != thread.id or saved.role != "user" or saved.text != data.text:
            raise HTTPException(409, "这条消息已用于另一项内容，请重新发送。")
        return saved, saved_course_action(db, thread, saved)
    if course_action:
        reply_id = course_reply_id(str(data.id))
        if reply_id == str(data.id) or db.get(ChatMessage, reply_id) is not None:
            raise HTTPException(409, "这条消息编号已占用，请重新发送。")
        item, _ = add_exchange(db, thread, data, now, role="assistant",
                               reply_text=COURSE_ACTION_REPLIES[course_action["kind"]], course_reply=reply_id)
    else:
        item, _ = add_exchange(db, thread, data, now, role="demo", reply_text=reply(db, thread, data.text))
    return item, course_action


def attach_file(db, thread: Conversation, file_id: UUID, now: datetime) -> None:
    identity = str(file_id)
    file = db.get(ImportedFile, identity)
    if file is None:
        raise HTTPException(404, "找不到这份资料。")
    if db.get(ChatAttachment, (thread.id, identity)) is not None:
        return
    db.add(ChatAttachment(conversation_id=thread.id, file_id=identity, added_at=now))
    if thread.goal is not None:
        info = evidence("file", identity, file.name, "", target=identity)
        thread.goal = {**thread.goal, "revision": thread.goal["revision"] + 1,
                       "evidence": [*thread.goal["evidence"], info]}
    thread.updated_at = now


def current_goal(thread: Conversation, payload: DraftRevision) -> dict:
    active = thread.goal
    if active is None or active["id"] != str(payload.goalId) or active["revision"] != payload.revision:
        raise HTTPException(409, "这张目标卡已变更，请打开最新版本。")
    return active


def validated_goal(thread: Conversation, payload: DraftInput) -> dict:
    return edit_goal(current_goal(thread, payload), payload.values, payload.removeEvidence)


def submit_goal(db, thread: Conversation, payload: SubmitGoal, now: datetime, project_moment, secret: bytes) -> dict:
    identity = str(payload.goalId)
    fingerprint = digest({**payload.model_dump(mode="json", exclude_none=True), "removeEvidence": sorted(set(payload.removeEvidence))})
    saved = db.get(GoalVersion, (identity, 1))
    result = {"projectId": identity, "goalVersion": 1, "routeVersion": None if payload.modelConsent else 1}
    if saved:
        if saved.conversation_id == thread.id and saved.submission_digest == fingerprint:
            return result
        raise HTTPException(409, "这张目标卡已经提交。请从对话中打开已创建的项目。")

    finalized = validated_goal(thread, payload)
    values = goal_values(finalized)
    if not values["topic"] or not values["outcome"]:
        raise HTTPException(422, "请补充学习主题和预期成果。")
    definition = {key: finalized[key] for key in ("fields", "evidence", "revision")}
    if payload.modelConsent:
        inputs = fields_to_send(finalized)
        connection, backup = verify_consent(db, identity, inputs, payload.modelConsent, secret)
        create_model_project(db, identity, definition, project_moment(db), conversation_id=thread.id,
                             submission_digest=fingerprint, connection=connection, backup=backup, inputs=inputs,
                             limit=payload.modelConsent.maxCalls)
        if payload.modelConsent.maxCost is not None:
            set_task_limit(db, "generation", identity, payload.modelConsent.maxCost, False)
    else:
        create_local_project(db, identity, definition, project_moment(db), conversation_id=thread.id,
                             submission_digest=fingerprint)
    thread.goal = None
    thread.updated_at = now
    return result


def import_legacy(db, data: LegacyConversation, now: datetime) -> dict:
    fingerprint = digest(data.model_dump(mode="json"))
    identity = str(uuid5(NAMESPACE_URL, f"zhixue:legacy-chat:{fingerprint}"))
    existing = db.get(Conversation, identity)
    if existing:
        return thread_page(db, existing)
    if not data.messages and data.goal is None:
        raise HTTPException(422, "没有需要导入的对话。")
    title = next((title_for(item.text) for item in data.messages if item.role == "user"), "已导入的对话")
    thread = Conversation(id=identity, title=title, created_at=now, updated_at=now, creation_digest=fingerprint)
    db.add(thread)
    db.flush()
    for position, message in enumerate(data.messages, 1):
        db.add(ChatMessage(id=str(uuid5(UUID(identity), str(position))), conversation_id=identity, position=position,
                           role=message.role, text=message.text, created_at=now))
    if data.goal:
        goal_id = str(data.goal.id)
        supplied = data.goal.model_dump(exclude={"id"})
        previous = db.get(GoalVersion, (goal_id, 1))
        if previous and previous.conversation_id is None and goal_values(previous.definition) == supplied:
            previous.conversation_id = identity
        else:
            if previous:
                goal_id = str(uuid5(UUID(identity), "legacy-draft"))
            thread.goal = {"id": goal_id, "revision": 1, "fields": core_fields(supplied, "edited"), "evidence": []}
    return thread_page(db, thread)


def conversation_router(engine, now, project_moment, session_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/conversations")
    secret = session_token.encode("utf-8")

    @router.get("")
    def history(
        page: Annotated[int, Query(ge=1)] = 1, q: Annotated[str, Query(max_length=120)] = "", lessonId: UUID | None = None,
    ):
        with transaction(engine, write=False) as db:
            filters = []
            if q.strip():
                filters.append(func.instr(func.unicode_fold(Conversation.title), q.strip().casefold()) > 0)
            if lessonId is not None:
                filters.append(Conversation.lesson_id == str(lessonId))
            total = db.scalar(select(func.count()).select_from(Conversation).where(*filters)) or 0
            last = max(1, (total + HISTORY_PAGE - 1) // HISTORY_PAGE)
            page = min(page, last)
            records = db.scalars(select(Conversation).where(*filters).order_by(
                Conversation.updated_at.desc(), Conversation.id.desc(),
            ).offset((page - 1) * HISTORY_PAGE).limit(HISTORY_PAGE))
            return {"items": [conversation_view(item) for item in records], "page": page, "lastPage": last, "total": total}

    @router.post("", status_code=201)
    def create(data: NewConversation):
        identity = str(data.id)
        fingerprint = digest(data.model_dump(mode="json"))
        with transaction(engine) as db:
            existing = db.get(Conversation, identity)
            if existing:
                if existing.creation_digest != fingerprint:
                    raise HTTPException(409, "这段对话已有初始内容，请重新建立对话。")
                message = db.get(ChatMessage, str(data.message.id)) if data.message else None
                course_action = saved_course_action(db, existing, message)
                return thread_page(db, existing, course_action=course_action)

            title = "新对话"
            if data.lessonId:
                lesson = db.get(Lesson, str(data.lessonId))
                if lesson is None:
                    raise HTTPException(404, "找不到这节课程。")
                project = db.get(Project, lesson.project_id)
                title = f"{lesson_course(project.sample, lesson.position, lesson.outline).title} · 答疑"
            if data.sample and (not data.message or data.message.text != EXAMPLE_MESSAGE):
                raise HTTPException(422, "请从首页打开 SQLite 样例。")
            created = now()
            thread = Conversation(id=identity, title=title, created_at=created, updated_at=created,
                                  lesson_id=str(data.lessonId) if data.lessonId else None, creation_digest=fingerprint)
            db.add(thread)
            db.flush()
            for file in dict.fromkeys(data.attachments):
                attach_file(db, thread, file, created)
            course_action = None
            if data.message:
                intent = linked_course_action(db, thread, data.message.text)
                message, course_action = append_message(db, thread, data.message, created, course_action=intent)
                if data.sample:
                    thread.goal = make_goal(db, thread, message, example=True)
            return thread_page(db, thread, course_action=course_action)

    @router.post("/legacy", status_code=201)
    def import_tab(data: LegacyConversation):
        with transaction(engine) as db:
            return import_legacy(db, data, now())

    @router.get("/{identity}")
    def read(identity: UUID, before: Annotated[int, Query(ge=1)] | None = None):
        with transaction(engine, write=False) as db:
            return thread_page(db, get_thread(db, identity), before)

    @router.post("/{identity}/messages", status_code=201)
    def send(identity: UUID, data: MessageInput):
        with transaction(engine) as db:
            thread = get_thread(db, identity)
            intent = linked_course_action(db, thread, data.text)
            _, course_action = append_message(db, thread, data, now(), course_action=intent)
            return thread_page(db, thread, course_action=course_action)

    @router.put("/{identity}/attachments/{file_id}")
    def add_attachment(identity: UUID, file_id: UUID):
        with transaction(engine) as db:
            thread = get_thread(db, identity)
            attach_file(db, thread, file_id, now())
            return thread_page(db, thread)

    @router.post("/{identity}/goal")
    def prepare_goal(identity: UUID):
        with transaction(engine) as db:
            thread = get_thread(db, identity)
            if thread.goal is None:
                messages = select(ChatMessage).where(
                    ChatMessage.conversation_id == thread.id, ChatMessage.role == "user",
                ).order_by(ChatMessage.position.desc())
                previous = None
                if thread.lesson_id:
                    learning_messages = (item for item in db.scalars(messages.execution_options(yield_per=MESSAGE_PAGE))
                                         if not saved_course_action(db, thread, item))
                    recent = list(islice(learning_messages, RECENT_GOAL_MESSAGES))
                    latest, previous = (recent[0] if recent else None), recent[1:]
                else:
                    latest = db.scalar(messages.limit(1))
                if latest is None:
                    raise HTTPException(409, "请先在对话中说明想学什么。")
                thread.goal = make_goal(db, thread, latest, previous_messages=previous)
                thread.updated_at = now()
            return thread.goal

    @router.put("/{identity}/goal")
    def save_goal(identity: UUID, data: DraftInput):
        with transaction(engine) as db:
            thread = get_thread(db, identity)
            updated = validated_goal(thread, data)
            if updated != thread.goal:
                thread.goal = updated
                thread.updated_at = now()
            return updated

    @router.post("/{identity}/goal/submit", status_code=201)
    def publish_goal(identity: UUID, data: SubmitGoal):
        with transaction(engine) as db:
            return submit_goal(db, get_thread(db, identity), data, now(), project_moment, secret)

    @router.post("/{identity}/goal/model-preview")
    def preview_model_inputs(identity: UUID, data: ModelPreview):
        with transaction(engine, write=False) as db:
            finalized = validated_goal(get_thread(db, identity), data)
            values = goal_values(finalized)
            if not values["topic"] or not values["outcome"]:
                raise HTTPException(422, "请补充学习主题和预期成果。")
            return preview_consent(db, str(data.goalId), fields_to_send(finalized), data.maxCalls, secret,
                                   allow_backup=data.allowBackup, max_cost=data.maxCost)

    return router
