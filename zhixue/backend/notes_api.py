from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from sqlalchemy import func, select

from .db import Lesson, LibraryLocation, Note, NoteConflict, NoteVersion, NoteWrite, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker
from .library import local_path
from .note_catalog import associated_ids, find_note, lesson_choices, lesson_notes, metadata_view, replace_associations
from .notes import (
    MAX_NOTE_CHARS, NoteFileError, check_detached_files, conflict_data, diff_lines, discard_stage,
    note_snapshot, observe, recover_writes, remember, remember_conflict, stage_write, version_data,
)


Revision = Annotated[str, StringConstraints(pattern="^[a-f0-9]{64}$")]
NoteText = Annotated[str, Field(max_length=MAX_NOTE_CHARS)]


class NoteInput(BaseModel):
    content: NoteText
    # Older clients can create a new file but must not overwrite an existing file without its revision.
    baseRevision: Revision | None = None


class NoteMetadata(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    lessonIds: list[UUID]

    @field_validator("title")
    @classmethod
    def single_line_title(cls, title: str) -> str:
        if any(ord(character) < 32 for character in title):
            raise ValueError("笔记标题需要为单行文字。")
        return title


class NewNote(NoteMetadata):
    id: UUID


class UpdateNote(NoteMetadata):
    metadataRevision: Annotated[int, Field(strict=True, ge=1)]


class ResolveInput(BaseModel):
    currentRevision: Revision | None
    draftRevision: Revision
    externalRevision: Revision | None
    action: Literal["keep", "draft", "external", "merge"]
    content: NoteText | None = None

    @model_validator(mode="after")
    def check_merge(self):
        if (self.action == "merge") != (self.content is not None):
            raise ValueError("手动合并时需要提交合并后的内容。")
        return self


def conflict_response(conflict_id: str):
    return JSONResponse({"detail": "笔记版本发生冲突。两份内容已保留。", "conflictId": conflict_id}, status_code=409)


def saved_note(db, note: Note, revision: str | None) -> dict:
    item = version_data(db, note.id, revision)
    return {"saved": True, "revision": revision, "content": item["content"] if item else ""}


def get_conflict(db, note: Note, identity: UUID) -> NoteConflict:
    conflict = db.get(NoteConflict, str(identity))
    if conflict is None or conflict.note_id != note.id:
        raise HTTPException(404, "找不到该笔记的冲突记录。")
    return conflict


@contextmanager
def open_note(engine, folder: Path, project_moment, note_id: str, *, legacy: bool = False):
    try:
        with transaction(engine) as db:
            note = find_note(db, folder, note_id, legacy=legacy)
            recovered = recover_writes(db, folder, note.id, project_moment)
            tracker = LocationTracker(db, folder)
            file_id = f"note-{note.id}"
            location = tracker.resolve(file_id, local_path(folder, "notes", note.id))
            if location.status in {"blocked", "unreadable", "trashed", "purged"}:
                raise NoteFileError(location.message or "请在知识库确认笔记位置。")
            current = observe(db, note.id, note_snapshot(location.path))
            if current and (registered := tracker.locations.get(file_id)) is not None:
                registered.revision = current
            check_detached_files(db, folder, note.id, current)
            yield db, note, current, tracker.relative(location.path)
        for write_id in recovered:
            discard_stage(folder, write_id)
    except (NoteFileError, LocationConflict, InvalidLocation) as error:
        raise HTTPException(409, str(error)) from error
    except (OSError, UnicodeError) as error:
        raise HTTPException(409, "笔记暂时无法读写。请检查文件权限、占用情况和剩余空间后重试。") from error


def file_router(engine, folder: Path, project_moment, prefix: str, *, legacy: bool = False) -> APIRouter:
    router = APIRouter(prefix=prefix)

    def open_file(identity: UUID):
        return open_note(engine, folder, project_moment, str(identity), legacy=legacy)

    def commit_write(identity: UUID, write_id: str):
        with open_file(identity) as (db, note, _current, _path):
            write = db.get(NoteWrite, write_id)
            result = conflict_response(write.result_conflict_id) if write.result_conflict_id else saved_note(db, note, write.desired_revision)
        return result

    @router.get("")
    def read_note(identity: UUID):
        with open_file(identity) as (db, note, current, _path):
            stored = version_data(db, note.id, current)
            conflicts = db.scalars(select(NoteConflict).where(
                NoteConflict.note_id == note.id, NoteConflict.resolution.is_(None),
            ).order_by(NoteConflict.created_at.desc(), NoteConflict.id))
            versions = db.execute(select(NoteVersion.revision, NoteVersion.source, NoteVersion.created_at,
                                         NoteVersion.seen_on_disk, func.substr(NoteVersion.content, 1, 240).label("excerpt"))
                                  .where(NoteVersion.note_id == note.id).order_by(NoteVersion.created_at.desc(), NoteVersion.revision)).all()
            return {
                "revision": current, "content": stored["content"] if stored else "",
                "missing": current is None and any(item.seen_on_disk for item in versions),
                "conflicts": [conflict_data(row) for row in conflicts],
                "versions": [{"revision": row.revision, "source": row.source, "createdAt": row.created_at.isoformat() + "Z",
                              "seenOnDisk": row.seen_on_disk, "excerpt": (row.excerpt or b"").decode("utf-8", errors="ignore")}
                             for row in versions],
            }

    @router.put("")
    def save_note(identity: UUID, payload: NoteInput):
        with open_file(identity) as (db, note, current, path):
            draft = remember(db, note.id, payload.content.encode("utf-8"), "draft")
            if draft == current:
                return saved_note(db, note, current)
            if payload.baseRevision != current:
                return conflict_response(remember_conflict(db, note.id, "changed", payload.baseRevision, current, draft))
            write_id = stage_write(db, note.id, path, current, draft)
        return commit_write(identity, write_id)

    @router.get("/conflicts/{conflict_id}")
    def preview_conflict(identity: UUID, conflict_id: UUID):
        with open_file(identity) as (db, note, current, _path):
            conflict = get_conflict(db, note, conflict_id)
            base = version_data(db, note.id, conflict.base_revision)
            external = version_data(db, note.id, conflict.external_revision)
            draft = version_data(db, note.id, conflict.draft_revision)
            live = version_data(db, note.id, current)
            comparison = external if conflict.kind == "detached" or live is None else live
            return {**conflict_data(conflict), "base": base, "current": live, "external": external,
                    "draft": draft, "diff": diff_lines(comparison, draft)}

    @router.post("/conflicts/{conflict_id}")
    def resolve_conflict(identity: UUID, conflict_id: UUID, payload: ResolveInput):
        with open_file(identity) as (db, note, current, path):
            conflict = get_conflict(db, note, conflict_id)
            if conflict.resolution is not None:
                return saved_note(db, note, current)
            matching_branches = (payload.draftRevision, payload.externalRevision) == (
                conflict.draft_revision, conflict.external_revision,
            )
            choices = {"keep": current, "draft": conflict.draft_revision, "external": conflict.external_revision}
            if payload.action == "merge":
                desired = remember(db, note.id, payload.content.encode("utf-8"), "merge")
                if matching_branches:
                    conflict.draft_revision = desired
            else:
                desired = choices[payload.action]
            if payload.currentRevision != current or not matching_branches:
                return conflict_response(conflict.id)
            if payload.action == "external" and desired is None:
                raise HTTPException(409, "该冲突没有可恢复的外部版本。")
            if desired == current:
                conflict.resolution = payload.action
                return saved_note(db, note, current)
            write_id = stage_write(db, note.id, path, current, desired, conflict, payload.action)
        return commit_write(identity, write_id)

    @router.get("/versions/{revision}")
    def preview_version(identity: UUID, revision: Revision):
        with transaction(engine, write=False) as db:
            location = db.get(LibraryLocation, f"note-{identity}")
            if location and location.trash_id:
                archived = version_data(db, str(identity), revision)
                if archived is None:
                    raise HTTPException(404, "该笔记版本不存在。")
                return {**archived, "current": None, "diff": diff_lines(None, archived)}
        with open_file(identity) as (db, note, current, _path):
            archived = version_data(db, note.id, revision)
            if archived is None:
                raise HTTPException(404, "该笔记版本不存在。")
            live = version_data(db, note.id, current)
            return {**archived, "current": live, "diff": diff_lines(live, archived)}

    return router


def note_router(engine, folder: Path, project_moment) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/lessons/{identity}/notes")
    def list_lesson_notes(identity: UUID):
        with transaction(engine) as db:
            return lesson_notes(db, folder, str(identity))

    @router.get("/notes/lesson-options")
    def available_lessons():
        with transaction(engine, write=False) as db:
            return lesson_choices(db)

    @router.post("/notes", status_code=201)
    def create_note(payload: NewNote):
        identity = str(payload.id)
        lessons = {str(lesson) for lesson in payload.lessonIds}
        with transaction(engine) as db:
            note = db.get(Note, identity)
            if note:
                if note.title != payload.title or associated_ids(db, identity) != lessons:
                    raise HTTPException(409, "该笔记标识已被使用。请刷新后重新新建。")
            else:
                path = local_path(folder, "notes", identity)
                if db.get(Lesson, identity) or db.get(LibraryLocation, f"note-{identity}") or path.exists():
                    raise HTTPException(409, "该笔记位置已被使用。请刷新后重新新建。")
                note = Note(id=identity, title=payload.title)
                db.add(note)
                db.flush()
                replace_associations(db, identity, lessons)
                initial = remember(db, identity, b"", "app")
                stage_write(db, identity, path.relative_to(folder).as_posix(), None, initial)
        # Use the same durable journal and conflict handling as subsequent edits.
        with open_note(engine, folder, project_moment, identity) as (db, note, _current, _path):
            return metadata_view(db, note)

    @router.get("/notes/{identity}/details")
    def read_details(identity: UUID):
        with transaction(engine) as db:
            return metadata_view(db, find_note(db, folder, str(identity)))

    @router.put("/notes/{identity}/details")
    def edit_details(identity: UUID, payload: UpdateNote):
        with transaction(engine) as db:
            note = find_note(db, folder, str(identity))
            if note.metadata_revision != payload.metadataRevision:
                raise HTTPException(409, "笔记标题或关联已在别处更新。请查看最新内容后重试。")
            location = db.get(LibraryLocation, f"note-{note.id}")
            if location and location.trash_id:
                raise HTTPException(409, "请从回收站恢复笔记后再修改标题或关联。")
            linked = replace_associations(db, note.id, {str(lesson) for lesson in payload.lessonIds})
            if note.title != payload.title or linked:
                note.title = payload.title
                note.metadata_revision += 1
            db.flush()
            return metadata_view(db, note)

    router.include_router(file_router(engine, folder, project_moment, "/notes/{identity}"))
    router.include_router(file_router(engine, folder, project_moment, "/lessons/{identity}/note", legacy=True))
    return router
