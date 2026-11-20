from hashlib import sha256
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select

from .assessment import round_for, round_view
from .assessment_api import SubmitInput as ModelSubmit, editable, submit_round, target_round
from .assessment_input import MAX_QUESTIONS
from .content import lesson_course
from .course_reading import course_document
from .db import AssessmentRound, Attempt, Lesson, MobileChange, MobileWorkspace, NoteConflict, NoteWrite, Project, Review, transaction
from .file_locations import LocationTracker
from .local_reviews import find_review, require_current_round, submit_local_review
from .mobile_notes import merge_note
from .note_catalog import lesson_notes
from .notes import remember, remember_conflict, stage_write, version_data
from .notes_api import NoteText, Revision, open_note, saved_note
from .review_queue import review_queue


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    workspace: UUID
    capturedAt: AwareDatetime


class NoteChange(Change):
    kind: Literal["note"]
    noteId: UUID
    baseRevision: Revision | None
    content: NoteText


Answer = Annotated[int, Field(strict=True, ge=0, le=64)] | Annotated[str, Field(max_length=3000)]


class ReviewChange(Change):
    kind: Literal["review"]
    lessonId: UUID
    key: str = Field(min_length=1, max_length=128)
    round: int = Field(ge=1)
    groupId: UUID | None = None
    basis: Revision
    answers: list[Answer] = Field(max_length=MAX_QUESTIONS)
    action: Literal["submit", "unknown"]


MobileInput = Annotated[NoteChange | ReviewChange, Field(discriminator="kind")]


def workspace_id(db) -> str:
    return db.get(MobileWorkspace, 1).identity


def fingerprint(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def previous(db, data: Change) -> tuple[MobileChange | None, str]:
    if workspace_id(db) != str(data.workspace):
        raise HTTPException(409, "原工作区已更换。请保留手机记录并核对电脑上的工作区。")
    marker = fingerprint(data.model_dump(mode="json"))
    saved = db.get(MobileChange, str(data.id))
    if saved is not None and saved.fingerprint != marker:
        raise HTTPException(409, "此同步编号对应另一份操作。")
    return saved, marker


def note_result(receipt: MobileChange, *, revision=None, content="", conflict=None) -> dict:
    reply = {"id": receipt.id, "submittedRevision": receipt.details["submittedRevision"]}
    if conflict:
        return {**reply, "state": "conflict", "conflictId": conflict}
    return {**reply, "state": "saved", "revision": revision, "content": content,
            "merged": revision != reply["submittedRevision"]}


def save_mobile_note(engine, folder, moment, now, data: NoteChange) -> dict:
    with transaction(engine, write=False) as db:
        saved, _ = previous(db, data)
        if saved is not None and saved.reply:
            return saved.reply
    identity = str(data.noteId)
    with open_note(engine, folder, moment, identity) as (db, note, current, path):
        receipt, marker = previous(db, data)
        if receipt is None:
            draft = remember(db, identity, data.content.encode("utf-8"), "mobile")
            receipt = MobileChange(id=str(data.id), fingerprint=marker, kind=data.kind, received_at=now(),
                                   details={"noteId": identity, "submittedRevision": draft})
            db.add(receipt)
            if draft == current:
                receipt.reply = note_result(receipt, revision=current, content=data.content)
            elif data.baseRevision == current:
                receipt.details = {**receipt.details, "writeId": stage_write(db, identity, path, current, draft)}
            else:
                base = version_data(db, identity, data.baseRevision)
                desktop = version_data(db, identity, current)
                combined = merge_note(base["content"], desktop["content"], data.content) if base and desktop else None
                if combined is None:
                    conflict = remember_conflict(db, identity, "changed", data.baseRevision, current, draft)
                    receipt.reply = note_result(receipt, conflict=conflict)
                elif combined == desktop["content"]:
                    receipt.reply = note_result(receipt, revision=current, content=combined)
                else:
                    merged = remember(db, identity, combined.encode("utf-8"), "merge")
                    receipt.details = {**receipt.details, "writeId": stage_write(db, identity, path, current, merged)}
        if receipt.reply:
            return receipt.reply

    # open_note recovers a staged write before reading the receipt, including after a lost response or a restart.
    with open_note(engine, folder, moment, identity) as (db, note, _, _):
        receipt, _ = previous(db, data)
        if receipt.reply is None:
            write = db.get(NoteWrite, receipt.details["writeId"])
            if write.result_conflict_id:
                receipt.reply = note_result(receipt, conflict=write.result_conflict_id)
            else:
                stored = saved_note(db, note, write.desired_revision)
                receipt.reply = note_result(receipt, revision=stored["revision"], content=stored["content"])
        return receipt.reply


def model_paper(lesson: Lesson, group: AssessmentRound) -> dict:
    questions = [{"text": item["text"]} for item in group.group["questions"]]
    return {"kind": "model", "groupId": group.id, "round": group.number, "questions": questions,
            "basis": fingerprint([lesson.id, lesson.version, group.id, group.number, group.course_version, questions])}


def local_paper(lesson: Lesson, review: Review, objective) -> dict:
    questions = [{"text": item.text, "options": list(item.options)} for item in objective.questions(review.round_number)]
    return {"kind": "local", "groupId": None, "round": review.round_number, "questions": questions,
            "basis": fingerprint([lesson.id, lesson.version, review.key, review.round_number, questions])}


def grade_mobile_review(db, folder, data: ReviewChange, now, day, moment) -> dict:
    if data.groupId is not None:
        target = target_round(db, data.groupId)
        group, lesson, project, objective, review = target
        if lesson.id != str(data.lessonId) or review.key != data.key or group.mode != "formal" or group.number != data.round:
            raise HTTPException(409, "题组对应的复习轮次已变化。")
        if group.submission or group.hinted:
            raise HTTPException(409, "电脑已有作答或提示。手机记录待核对作答顺序。")
        editable(db, folder, group, lesson, project, objective, review, day(db))
        if data.basis != model_paper(lesson, group)["basis"]:
            raise HTTPException(409, "题组版本已变化。")
        if any(not isinstance(answer, str) for answer in data.answers):
            raise HTTPException(422, "请填写题组答案。")
        result = submit_round(db, folder, data.groupId, ModelSubmit(answers=data.answers, action=data.action), now, day)
        return {"state": "settled" if result["result"] else "grading", "result": result["result"]}

    target = find_review(db, data.lessonId, data.key)
    project, lesson, review, objective = target
    if db.get(Attempt, (lesson.id, data.key, data.round)) or review.hinted:
        raise HTTPException(409, "电脑已有作答或提示。手机记录待核对作答顺序。")
    today = day(db)
    require_current_round(project, lesson, review, data.round, today)
    paper = local_paper(lesson, review, objective)
    if data.basis != paper["basis"]:
        raise HTTPException(409, "题组版本已变化。")
    if len(data.answers) > len(paper["questions"]) or any(
        type(answer) is not int or answer >= len(question["options"])
        for answer, question in zip(data.answers, paper["questions"])
    ):
        raise HTTPException(422, "答案与题组不符。")
    result = submit_local_review(db, folder, target, data.round, data.answers, data.action, today, moment)
    return {"state": "settled", "result": result}


def review_result(db, item: MobileChange) -> dict:
    reply = item.reply
    if reply["state"] != "grading":
        return reply
    group = db.get(AssessmentRound, item.details["groupId"])
    if group is None:
        return {**reply, "state": "held", "reason": "电脑找不到原题组。"}
    result = round_view(db, group)
    if result["result"]:
        return {**reply, "state": "settled", "result": result["result"]}
    return {**reply, "reason": result["error"]}


def mobile_review_ready(db, review: Review, info: dict) -> bool:
    if info["status"] != "due" or review.hinted:
        return False
    if info["kind"] != "model":
        return True
    group = round_for(db, review, "formal")
    return group is not None and group.state == "ready" and not group.hinted


def mobile_endpoints(engine, folder: Path, day, now, moment):
    router = APIRouter(prefix="/api/mobile")

    @router.get("/catalog")
    def catalog():
        with transaction(engine) as db:
            lessons = [
                {"id": lesson.id, "title": lesson_course(project.sample, lesson.position, lesson.outline).title,
                 "projectTopic": project.topic}
                for lesson, project in db.execute(
                    select(Lesson, Project).join(Project).where(Lesson.ready.is_(True))
                    .order_by(Project.topic, Project.id, Lesson.position)
                )
            ]
            reviews = []
            today = day(db)
            for _, lesson, review, info in review_queue(db, folder, today, through=today):
                if not mobile_review_ready(db, review, info):
                    continue
                reviews.append({"id": f"{lesson.id}/{review.key}/{review.round_number}", "kind": info["kind"],
                                "lessonId": lesson.id, "key": review.key, "title": info["title"],
                                "round": review.round_number, "projectTopic": info["projectTopic"], "dueOn": info["dueOn"]})
            return {"workspace": workspace_id(db), "lessons": lessons, "reviews": reviews}

    @router.get("/lessons/{identity}")
    def download_lesson(identity: UUID):
        lesson_id = str(identity)
        with transaction(engine) as db:
            lesson = db.get(Lesson, lesson_id)
            if lesson is None:
                raise HTTPException(404, "找不到课程。")
            body, issue = course_document(LocationTracker(db, folder), folder, lesson)
            if body is None or not lesson.ready:
                raise HTTPException(409, issue or "课程正文暂未就绪。")
            project = db.get(Project, lesson.project_id)
            course = lesson_course(project.sample, lesson.position, lesson.outline)
            notes = lesson_notes(db, folder, lesson_id)
            data = {"workspace": workspace_id(db), "id": lesson_id, "title": course.title,
                    "projectTopic": project.topic, "revision": sha256(body.encode("utf-8")).hexdigest(), "body": body}
        if not notes:
            return {**data, "note": None}
        primary = notes[0]
        with open_note(engine, folder, moment, primary["id"]) as (db, note, revision, _):
            stored = saved_note(db, note, revision)
            return {**data, "note": {**primary, "revision": stored["revision"], "content": stored["content"]}}

    @router.get("/lessons/{identity}/reviews/{key}")
    def download_review(identity: UUID, key: str):
        with transaction(engine) as db:
            review = db.get(Review, (str(identity), key))
            if review is None:
                raise HTTPException(404, "找不到此复习目标。")
            lesson = db.get(Lesson, review.lesson_id)
            if lesson.outline:
                group = round_for(db, review, "formal")
                if group is None or group.hinted:
                    raise HTTPException(409, "当前题组尚未准备或已查看提示。")
                _, _, project, objective, _ = target_round(db, UUID(group.id))
                editable(db, folder, group, lesson, project, objective, review, day(db))
                paper = model_paper(lesson, group)
                title = objective.title
            else:
                project, lesson, review, objective = find_review(db, identity, key)
                require_current_round(project, lesson, review, review.round_number, day(db))
                if review.hinted:
                    raise HTTPException(409, "当前题组已查看提示。请在电脑继续。")
                paper = local_paper(lesson, review, objective)
                title = objective.title
            return {**paper, "workspace": workspace_id(db), "id": f"{lesson.id}/{key}/{review.round_number}",
                    "lessonId": lesson.id, "key": key, "title": title,
                    "projectTopic": project.topic, "dueOn": review.due_on}

    @router.post("/changes")
    def receive(data: MobileInput):
        if isinstance(data, NoteChange):
            reply = save_mobile_note(engine, folder, moment, now, data)
            # The desktop may have resolved a conflict whose original reply never reached the phone.
            return status(data.id, data.workspace) if reply["state"] == "conflict" else reply
        with transaction(engine) as db:
            saved, marker = previous(db, data)
            if saved is not None:
                current = review_result(db, saved)
                return saved.reply if current["state"] == "grading" else current
            try:
                reply = grade_mobile_review(db, folder, data, now, day, moment)
            except HTTPException as error:
                if error.status_code not in (404, 409):
                    raise
                reply = {"state": "held", "reason": error.detail}
            reply["id"] = str(data.id)
            details = data.model_dump(mode="json", exclude={"id", "workspace", "kind"})
            db.add(MobileChange(id=reply["id"], fingerprint=marker, kind=data.kind, received_at=now(),
                                details=details, reply=reply))
            return reply

    @router.get("/changes")
    def history():
        with transaction(engine, write=False) as db:
            records = db.scalars(select(MobileChange).where(MobileChange.kind == "review")
                                 .order_by(MobileChange.received_at.desc(), MobileChange.id).limit(200))
            return {"workspace": workspace_id(db), "reviews": [
                {**record.details, **review_result(db, record), "receivedAt": record.received_at.isoformat()}
                for record in records
            ]}

    @router.get("/changes/{identity}")
    def status(identity: UUID, workspace: UUID):
        with transaction(engine, write=False) as db:
            if workspace_id(db) != str(workspace):
                raise HTTPException(409, "原工作区已更换。")
            record = db.get(MobileChange, str(identity))
            if record is None or record.reply is None:
                raise HTTPException(404, "找不到这份同步回执。")
            if record.kind == "review":
                return review_result(db, record)
            conflict_id = record.reply.get("conflictId")
            conflict = db.get(NoteConflict, conflict_id) if conflict_id else None
            if conflict is None or conflict.resolution is None:
                return record.reply
            note_id = record.details["noteId"]
        with open_note(engine, folder, moment, note_id) as (db, note, current, _):
            record = db.get(MobileChange, str(identity))
            value = saved_note(db, note, current)
            return note_result(record, revision=value["revision"], content=value["content"])

    change = TypeAdapter(MobileInput)

    def dispatch(command: dict):
        match command:
            case {"op": "catalog"}:
                return catalog()
            case {"op": "course", "lesson": str(identity)}:
                return download_lesson(UUID(identity))
            case {"op": "paper", "lesson": str(identity), "key": str(key)} if 0 < len(key) <= 128:
                return download_review(UUID(identity), key)
            case {"op": "receive", "change": dict(data)}:
                return receive(change.validate_python(data))
            case {"op": "receipt", "id": str(identity), "workspace": str(workspace)}:
                return status(UUID(identity), UUID(workspace))
            case _:
                raise HTTPException(422, "手机请求的操作不受支持。")

    return router, dispatch
