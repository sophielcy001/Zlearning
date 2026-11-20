from hashlib import sha256
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import select

from .course_review import finding_sections
from .course_sources import sources_current
from .db import CourseFinding, CourseVersion, GenerationStep, Lesson, ModelStudy, Project, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path, identity_of
from .library import FileTooLarge, local_path, read_snapshot
from .model_study import sync_task, utc_now, when_utc
from .notes import create_stage, link_if_absent
from .project_activity import study_moment, touch_project


def plain_markdown(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>#+.!|~()-])", r"\\\1", value)


def section_markdown(item: dict) -> str:
    heading = plain_markdown(" ".join(item["title"].split()))
    parts = [f"## {heading}"]
    if item["state"] != "verified":
        parts.append("> 用户采纳，尚未核验通过")
    parts.append(plain_markdown(item["text"]))
    for reference in item["citations"]:
        label = plain_markdown(f'{reference["title"]} · {reference["label"]}')
        parts.append(f'来源 · {label}（版本 {reference["revision"][:12]}）')
    return "\n\n".join(parts)


def course_markdown(title: str, sections: list[dict]) -> str:
    heading = f"# {plain_markdown(' '.join(title.split()))}"
    return "\n\n".join([heading, *(section_markdown(item) for item in sections)]) + "\n"


def put_version(path: Path, content: bytes, revision: str):
    identity = identity_of(path)
    if identity is None:
        temporary = path.with_name(f".writing-course-{uuid4().hex}")
        try:
            create_stage(temporary, content)
            link_if_absent(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        identity = identity_of(path)
    if identity is None or read_snapshot(path).revision != revision:
        raise LocationConflict("课程版本路径已存在其他内容。")
    return identity


def original_matches(db, folder: Path, lesson: Lesson, version: CourseVersion) -> bool:
    try:
        tracker = LocationTracker(db, folder)
        current = tracker.resolve(f"course-{lesson.id}", local_path(folder, "courses", lesson.id))
        return current.status == "ready" and read_snapshot(current.path).revision == version.revision
    except (OSError, InvalidLocation, FileTooLarge, LocationConflict):
        return False


def save_course_version(db, folder: Path, lesson: Lesson, previous: CourseVersion | None,
                        finding_id: str, sections: list[dict], markdown: str, now) -> int:
    file_id = f"course-{lesson.id}"
    if previous and not original_matches(db, folder, lesson, previous):
        raise LocationConflict("正式课程已有外部改动，请保留当前文件并检查版本。")
    number = previous.version + 1 if previous else 1
    content = markdown.encode("utf-8")
    revision = sha256(content).hexdigest()
    filename = f"{lesson.id}-v{number}-{revision[:12]}"
    path = checked_path(folder, local_path(folder, "courses", filename).relative_to(folder).as_posix())
    identity = put_version(path, content, revision)
    LocationTracker(db, folder).bind(file_id, path, identity, revision)
    db.add(CourseVersion(lesson_id=lesson.id, version=number, finding_id=finding_id, published_at=when_utc(now),
                         sections=sections, markdown=markdown, revision=revision))
    lesson.ready = True
    lesson.version = number
    return number


def release_course(db, folder: Path, lesson: Lesson, finding: CourseFinding, step: GenerationStep, now,
                   *, verified: bool = True) -> None:
    previous = db.get(CourseVersion, (lesson.id, lesson.version)) if lesson.ready else None
    included = {item["index"]: item for item in previous.sections} if previous else {}
    for item in finding_sections(db, finding, step):
        index = item["index"]
        prior = included.get(index)
        if verified and item["state"] == "verified" and (prior is None or prior["state"] != "verified"):
            included[index] = item
        elif prior is None and item["state"] != "verified" and item["disposition"] == "adopted":
            included[index] = item
    sections = [included[index] for index in sorted(included)]
    if sections and (previous is None or previous.sections != sections):
        save_course_version(db, folder, lesson, previous, finding.id, sections,
                            course_markdown(lesson.outline["title"], sections), now)


def publish_one(engine, folder: Path, now=utc_now) -> bool:
    with transaction(engine) as db:
        step = db.scalar(select(GenerationStep).join(
            ModelStudy, ModelStudy.project_id == GenerationStep.project_id,
        ).join(Project, Project.id == GenerationStep.project_id).where(
            GenerationStep.publication_pending.is_(True), ModelStudy.stopped.is_(False),
            ModelStudy.deleted_at.is_(None), Project.paused.is_(False),
        ).order_by(GenerationStep.priority.asc().nulls_last(), GenerationStep.id).limit(1))
        if step is None:
            return False
        study = db.get(ModelStudy, step.project_id)
        project = db.get(Project, step.project_id)
        lesson = db.get(Lesson, step.lesson_id)
        finding = db.get(CourseFinding, step.finding_id)
        step.publication_pending = False
        clock = now()
        if not sources_current(db, folder, finding.sources):
            step.state = "review_failed"
            step.error = "review_sources_changed"
        else:
            try:
                release_course(db, folder, lesson, finding, step, clock)
            except (OSError, InvalidLocation, FileTooLarge, LocationConflict):
                step.error = "publication_failed"
        if step.error:
            study.reason = study.reason or step.error
        touch_project(project, study_moment(clock))
        sync_task(db, study, project)
        return True
