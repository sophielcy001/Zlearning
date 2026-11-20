from collections import defaultdict
from datetime import date
import json
from pathlib import Path, PurePosixPath

from sqlalchemy import and_, select, text
from sqlalchemy.orm import Session

from .content import lesson_course
from .db import CourseVersion, Lesson, NoteLesson, PrerequisiteTask, Project, RemediationCard
from .review_queue import review_queue
from .study_report import review_events


FOLDER_NAMES = {
    "courses": "课程", "notes": "笔记", "imports": "导入资料", "supplements": "补充讲解",
}


def folder_name(folder: str) -> str:
    root, sep, rest = folder.partition("/")
    return FOLDER_NAMES.get(root, "知识库根目录" if root == "." else root) + (sep + rest if rest else "")


def edge_key(source: str, target: str) -> str:
    return "|".join(sorted((source, target)))


def lesson_links(db: Session, folder: Path, today: date) -> tuple[dict, set]:
    reviews = {}
    for _, lesson, _, review in review_queue(db, folder, today, through=today):
        if review["status"] == "due":
            reviews.setdefault(lesson.id, {key: review[key] for key in ("kind", "lessonId", "key", "title")})

    latest = {(event.lesson, event.key): event.outcome for event in review_events(db, today)
              if event.outcome != "ungraded"}
    weak = {lesson for (lesson, _), outcome in latest.items() if outcome in {"failed", "hinted"}}
    return reviews, weak


def graph_catalog(db: Session, folder: Path, today: date | None) -> tuple[dict[str, dict], dict[str, str], list[dict]]:
    nodes = {}
    projects = {}
    documents = db.execute(text("""
        SELECT d.id, d.kind, d.name, d.path, d.project_ids, d.project_topic, d.status, d.revision,
          (SELECT substr(p.body, 1, 180) FROM library_passages p
           WHERE p.file_id=d.id AND p.position>0 AND trim(p.body)!=''
           ORDER BY p.position LIMIT 1) AS excerpt
        FROM library_index_documents d
        WHERE NOT EXISTS (
          SELECT 1 FROM library_locations l WHERE l.id=d.id AND l.trash_id IS NOT NULL
        )
        ORDER BY d.name, d.id
    """)).mappings()
    revisions = {}
    for row in documents:
        file_id = row["id"]
        revisions[file_id] = row["revision"]
        relative = row["path"].removeprefix("library/")
        directory = PurePosixPath(relative).parent.as_posix()
        nodes[file_id] = {
            "id": file_id, "kind": row["kind"], "title": row["name"],
            "path": row["path"], "folder": directory, "projectIds": json.loads(row["project_ids"]),
            "projectTopic": row["project_topic"], "excerpt": row["excerpt"], "fileStatus": row["status"],
            "lessonId": None, "learned": False, "ready": True, "review": None, "weak": False,
        }

    reviews, weak = lesson_links(db, folder, today) if today is not None else ({}, set())
    lessons = (select(Lesson.id, Lesson.position, Lesson.outline, Lesson.ready, Lesson.learned_on,
                      Project.id, Project.topic, Project.sample)
               .join(Project, Project.id == Lesson.project_id)
               .order_by(Project.created_on, Project.id, Lesson.position))
    outline_links = []
    for lesson_id, position, outline, ready, learned, project_id, topic, sample in db.execute(lessons):
        identity = f"course-{lesson_id}"
        course = lesson_course(sample, position, outline)
        projects[project_id] = topic
        outline_links.extend((identity, f"course-{peer}") for peer in (outline or {}).get("prerequisites", []))
        file = nodes.get(identity, {})
        nodes[identity] = {
            **file, "id": identity, "kind": "course", "title": course.title, "lessonId": lesson_id,
            "projectIds": [project_id], "projectTopic": topic, "learned": learned is not None,
            "ready": ready, "review": reviews.get(lesson_id), "weak": lesson_id in weak,
            "excerpt": course.outcome, "path": file.get("path"), "folder": file.get("folder"),
            "fileStatus": file.get("fileStatus"),
        }

    directions = defaultdict(set)

    def connect(source: str, target: str) -> None:
        if source != target and source in nodes and target in nodes:
            directions[edge_key(source, target)].add("forward" if source < target else "reverse")

    for source, target in outline_links:
        connect(source, target)

    for note_id, lesson_id in db.execute(select(NoteLesson.note_id, NoteLesson.lesson_id)):
        connect(f"note-{note_id}", f"course-{lesson_id}")
    for lesson_id in db.scalars(text("""
        SELECT lessons.id FROM lessons
        JOIN library_index_documents d ON d.id='note-' || lessons.id
        LEFT JOIN notes n ON n.id=lessons.id
        WHERE n.id IS NULL
    """)):
        connect(f"note-{lesson_id}", f"course-{lesson_id}")

    # The file index already excludes escaped links and code. Resolve name links only when unambiguous.
    wiki = db.execute(text("""
        WITH active_files AS (
          SELECT d.id FROM library_index_documents d
          WHERE NOT EXISTS (
            SELECT 1 FROM library_locations l WHERE l.id=d.id AND l.trash_id IS NOT NULL
          )
        ), unique_names AS (
          SELECT n.key, min(n.file_id) AS id FROM library_wiki_names n
          JOIN active_files a ON a.id=n.file_id GROUP BY n.key HAVING count(*)=1
        )
        SELECT DISTINCT w.source_id, coalesce(w.target_id, n.id) AS target
        FROM library_wiki_links w
        JOIN active_files a ON a.id=w.source_id
        LEFT JOIN unique_names n ON w.target_id IS NULL AND n.key=w.key
        WHERE coalesce(w.target_id, n.id) IS NOT NULL
    """))
    active_docs = revisions.keys()
    for source, target in wiki:
        if target in active_docs:
            connect(source, target)

    versions = select(CourseVersion.lesson_id, CourseVersion.revision, CourseVersion.sections).join(
        Lesson, and_(Lesson.id == CourseVersion.lesson_id, Lesson.version == CourseVersion.version),
    ).where(Lesson.ready.is_(True))
    for lesson_id, revision, sections in db.execute(versions):
        course_id = f"course-{lesson_id}"
        if revisions.get(course_id) != revision:
            continue
        for section in sections:
            if section["state"] != "verified":
                continue
            for ref in section["citations"]:
                if revisions.get(ref["fileId"]) == ref["revision"]:
                    connect(course_id, ref["fileId"])

    cards = select(RemediationCard.grade_id, RemediationCard.lesson_id).where(
        RemediationCard.active.is_(True), RemediationCard.published_at.is_not(None),
    )
    for card_id, lesson_id in db.execute(cards):
        connect(f"supplement-{card_id}", f"course-{lesson_id}")
    prerequisites = select(PrerequisiteTask.id, PrerequisiteTask.lesson_id, PrerequisiteTask.target_lesson_id)
    for task_id, lesson_id, target in db.execute(prerequisites):
        if target:
            connect(f"course-{lesson_id}", f"course-{target}")
        connect(f"course-{lesson_id}", f"prerequisite-{task_id}")

    for key in directions:
        a, b = key.split("|")
        pair = (nodes[a], nodes[b])
        for entry, other in (pair, pair[::-1]):
            if entry["kind"] == "import" and other["kind"] == "course":
                entry["projectIds"] = sorted(set(entry["projectIds"]) | set(other["projectIds"]))

    edges = {key: "both" if len(modes) == 2 else next(iter(modes)) for key, modes in directions.items()}
    options = [{"id": project_id, "topic": topic} for project_id, topic in sorted(projects.items(), key=lambda item: (item[1], item[0]))]
    return nodes, edges, options
