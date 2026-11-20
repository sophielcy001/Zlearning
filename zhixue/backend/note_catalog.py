from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .content import lesson_course
from .db import Lesson, LibraryLocation, Note, NoteLesson, Project
from .library import local_path, note_title


def require_lesson(db: Session, lesson_id: str) -> Lesson:
    lesson = db.get(Lesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "找不到这节课程。")
    if not lesson.ready:
        raise HTTPException(409, "课程准备完成后可以使用笔记。")
    return lesson


def legacy_note(db: Session, lesson: Lesson) -> Note:
    note = db.get(Note, lesson.id)
    if note is None:
        note = Note(id=lesson.id)
        db.add(note)
        db.flush()
        db.add(NoteLesson(note_id=note.id, lesson_id=lesson.id))
    return note


def find_note(db: Session, folder: Path, note_id: str, *, legacy: bool = False) -> Note:
    if legacy:
        return legacy_note(db, require_lesson(db, note_id))
    note = db.get(Note, note_id)
    if note is not None:
        return note
    lesson = db.get(Lesson, note_id)
    if lesson is not None and has_legacy_file(db, folder, note_id):
        return legacy_note(db, lesson)
    raise HTTPException(404, "找不到这篇笔记。")


def has_legacy_file(db: Session, folder: Path, lesson_id: str) -> bool:
    return db.get(LibraryLocation, f"note-{lesson_id}") is not None or local_path(folder, "notes", lesson_id).exists()


def lesson_choices(db: Session, identities: set[str] | None = None) -> list[dict]:
    if identities == set():
        return []
    lessons = select(Lesson.id, Lesson.position, Lesson.outline, Project.id.label("project_id"),
                     Project.topic, Project.sample).join(Project).order_by(Project.topic, Project.id, Lesson.position)
    if identities is not None:
        lessons = lessons.where(Lesson.id.in_(identities))
    return [
        {"id": row.id, "title": lesson_course(row.sample, row.position, row.outline).title,
         "projectId": row.project_id, "projectTopic": row.topic}
        for row in db.execute(lessons)
    ]


def note_view(db: Session, note: Note, titles: dict[str, str] | None = None) -> dict:
    if note.title is None and titles is None:
        titles = {item["id"]: item["title"] for item in lesson_choices(db, {note.id})}
    return {"id": note.id, "title": note_title(note, (titles or {}).get(note.id))}


def associated_ids(db: Session, note_id: str) -> set[str]:
    return set(db.scalars(select(NoteLesson.lesson_id).where(NoteLesson.note_id == note_id)))


def metadata_view(db: Session, note: Note) -> dict:
    return {**note_view(db, note), "metadataRevision": note.metadata_revision, "lessons": lesson_choices(db, associated_ids(db, note.id))}


def replace_associations(db: Session, note_id: str, identities: set[str]) -> bool:
    current = associated_ids(db, note_id)
    if current == identities:
        return False
    added = identities - current
    if added != set(db.scalars(select(Lesson.id).where(Lesson.id.in_(added)))):
        raise HTTPException(409, "有知识点已不存在，请重新打开关联列表。")
    for identity in current - identities:
        db.delete(db.get(NoteLesson, (note_id, identity)))
    db.add_all(NoteLesson(note_id=note_id, lesson_id=identity) for identity in sorted(added))
    return True


def lesson_notes(db: Session, folder: Path, lesson_id: str) -> list[dict]:
    lesson = require_lesson(db, lesson_id)
    notes = list(db.scalars(select(Note).join(NoteLesson).where(NoteLesson.lesson_id == lesson_id)))
    if db.get(Note, lesson_id) is None and (not notes or has_legacy_file(db, folder, lesson_id)):
        notes.insert(0, legacy_note(db, lesson))
    if titles := {note.id for note in notes if note.title is None}:
        names = {item["id"]: item["title"] for item in lesson_choices(db, titles)}
    else:
        names = {}
    return sorted((note_view(db, note, names) for note in notes), key=lambda row: (row["id"] != lesson_id, row["title"], row["id"]))
