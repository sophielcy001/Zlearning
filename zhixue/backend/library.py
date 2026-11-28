from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from unicodedata import normalize
from tempfile import NamedTemporaryFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from .content import lesson_course
from .db import AssessmentRound, ImportedFile, Lesson, LibraryWebSnapshot, Note, NoteLesson, NoteVersion, PrerequisiteTask, Project, RemediationCard
from .documents import MAX_DOCUMENT_BYTES


@dataclass(frozen=True)
class LibraryDocument:
    id: str
    name: str
    kind: str
    path: Path
    format: str
    project_id: str | None = None
    project_topic: str | None = None
    location_status: str = "ready"
    location_message: str | None = None
    project_ids: tuple[str, ...] = ()
    snapshot_encoding: tuple[str, str] | None = None


@dataclass(frozen=True)
class FileSnapshot:
    signature: str
    content: bytes

    @property
    def revision(self) -> str:
        return sha256(self.content).hexdigest()


class FileTooLarge(ValueError):
    pass


def local_path(folder: Path, kind: str, identity: str, suffix: str = ".md") -> Path:
    return folder / "library" / kind / f"{identity}{suffix}"


def prerequisite_file_id(identity: str) -> str:
    return f"prerequisite-{identity}"


def atomic_write(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=".writing-", delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            temp.replace(path)
        finally:
            handle.close()
            temp.unlink(missing_ok=True)


def file_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


def read_snapshot(path: Path) -> FileSnapshot:
    before = file_signature(path)
    with path.open("rb") as handle:
        content = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise FileTooLarge
    if file_signature(path) != before:
        raise OSError("文件仍在修改，请刷新后重试。")
    return FileSnapshot(before, content)


def note_title(note: Note | None, lesson_title: str | None = None) -> str:
    if note and note.title:
        return note.title
    return f"{lesson_title} · 笔记" if lesson_title else "未命名笔记"


def wiki_target_key(value: str) -> str:
    return normalize("NFC", value.strip().replace("\\", "/")).casefold()


def documents(db: Session, folder: Path, known: set[str]) -> list[LibraryDocument]:
    result = []
    observed_notes = set(db.scalars(select(NoteVersion.note_id).where(NoteVersion.seen_on_disk.is_(True)).distinct()))
    notes = {note.id: note for note in db.scalars(select(Note))}
    note_projects: dict[str, dict[str, str]] = {}
    associations = select(NoteLesson.note_id, Project.id, Project.topic).join(
        Lesson, Lesson.id == NoteLesson.lesson_id,
    ).join(Project, Project.id == Lesson.project_id).distinct().order_by(NoteLesson.note_id, Project.id)
    for note_id, project_id, topic in db.execute(associations):
        note_projects.setdefault(note_id, {})[project_id] = topic

    titles = {}
    lessons = select(Lesson.id, Lesson.ready, Lesson.position, Lesson.outline, Project.id, Project.topic, Project.sample).join(
        Project, Project.id == Lesson.project_id
    )
    for identity, ready, position, outline, project_id, topic, sample in db.execute(lessons):
        name = lesson_course(sample, position, outline).title
        titles[identity] = name
        if ready:
            result.append(LibraryDocument(f"course-{identity}", name, "course", local_path(folder, "courses", identity), "md", project_id, topic))
        # An old note may exist solely on disk and can acquire a catalog entry when first edited.
        if identity not in notes:
            path = local_path(folder, "notes", identity)
            if f"note-{identity}" in known or path.exists():
                result.append(LibraryDocument(f"note-{identity}", note_title(None, name), "note", path, "md", project_id, topic))

    for identity, note in notes.items():
        file_id = f"note-{identity}"
        path = local_path(folder, "notes", identity)
        if identity not in observed_notes and file_id not in known and not path.exists():
            continue
        projects = note_projects.get(identity, {})
        ids = tuple(projects)
        topics = " / ".join(dict.fromkeys(projects.values())) or None
        result.append(LibraryDocument(file_id, note_title(note, titles.get(identity)), "note", path, "md",
                                      ids[0] if len(ids) == 1 else None, topics, project_ids=ids))
    imports = select(ImportedFile, LibraryWebSnapshot.revision, LibraryWebSnapshot.encoding).outerjoin(LibraryWebSnapshot)
    for item, revision, encoding in db.execute(imports):
        suffix = Path(item.name).suffix.lower()
        result.append(LibraryDocument(item.id, item.name, "import", local_path(folder, "imports", item.id, suffix), suffix[1:],
                                      snapshot_encoding=(revision, encoding) if revision is not None else None))
    cards = select(RemediationCard, AssessmentRound.number, Project.id, Project.topic).join(
        AssessmentRound, AssessmentRound.id == RemediationCard.round_id,
    ).join(Lesson, Lesson.id == RemediationCard.lesson_id).join(Project, Project.id == Lesson.project_id).where(
        RemediationCard.active.is_(True), RemediationCard.published_at.is_not(None),
    )
    for card, round_number, project, topic in db.execute(cards):
        name = f"{card.basis['title']} · 第 {round_number} 轮错因补学"
        result.append(LibraryDocument(f"supplement-{card.grade_id}", name, "supplement",
                                      local_path(folder, "supplements", card.grade_id), "md", project, topic))
    prerequisites = select(PrerequisiteTask.id, PrerequisiteTask.topic, Project.id, Project.topic).join(
        Lesson, Lesson.id == PrerequisiteTask.lesson_id,
    ).join(Project, Project.id == Lesson.project_id).where(PrerequisiteTask.published_at.is_not(None))
    for identity, concept, project, topic in db.execute(prerequisites):
        file_id = prerequisite_file_id(identity)
        result.append(LibraryDocument(file_id, f"{concept} · 前置知识", "supplement",
                                      local_path(folder, "supplements", file_id), "md", project, topic))
    return result
