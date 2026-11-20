from sqlalchemy import select, text

from .course_sources import MAX_EXCERPT, read_source_revisions, source_current
from .db import CourseVersion, Lesson, Project


def cited_files(sections):
    return {ref["fileId"] for section in sections for ref in section["citations"]}


def affected_sections(db, sections, revisions):
    stale = {
        section["index"]: [ref for ref in section["citations"] if not source_current(db, ref, revisions[ref["fileId"]])]
        for section in sections
    }
    affected = {index for index, citations in stale.items() if citations}
    if not affected:
        return []

    titles = {section["index"]: section["title"] for section in sections}
    while True:
        dependent = {section["index"] for section in sections if affected.intersection(section.get("dependsOn", []))}
        if dependent <= affected:
            break
        affected |= dependent

    return [{
        "index": section["index"], "title": section["title"], "text": section["text"],
        "citations": stale[section["index"]],
        "dependencyTitles": [titles[index] for index in section.get("dependsOn", []) if index in affected],
    } for section in sections if section["index"] in affected]


def pending_source_updates(db, folder):
    courses = db.execute(select(Lesson.id, Lesson.outline, Project.topic, CourseVersion.sections)
                         .join(CourseVersion, (CourseVersion.lesson_id == Lesson.id) & (CourseVersion.version == Lesson.version))
                         .join(Project, Project.id == Lesson.project_id)
                         .where(Lesson.ready)
                         .order_by(Project.updated_at.desc(), Lesson.id)).all()
    revisions = read_source_revisions(db, folder, {
        file_id for course in courses for file_id in cited_files(course.sections)
    })
    pending = []
    for course in courses:
        count = len(affected_sections(db, course.sections, revisions))
        if count:
            pending.append({
                "lessonId": course.id, "title": (course.outline or {}).get("title") or course.topic,
                "projectTopic": course.topic, "sectionCount": count,
            })
    return pending


def compare_source(db, previous, revision):
    if revision is None:
        return {"previous": previous, "current": None, "status": "unavailable"}
    bindings = {"file_id": previous["fileId"], "revision": revision, "at": previous["at"], "quote": previous["quote"]}
    sql = """
        SELECT d.name, COALESCE(p.text_revision, d.revision) AS revision, p.position, p.label, p.body
        FROM library_index_documents d JOIN library_passages p ON p.file_id=d.id
        LEFT JOIN library_ocr_pages o ON o.file_id=d.id AND o.page=p.position AND o.revision=d.revision
        WHERE d.id=:file_id AND d.kind='import' AND d.revision=:revision
          AND COALESCE(p.text_revision, d.revision)=COALESCE(o.text_revision, d.revision) AND
    """
    row = db.execute(text(sql + "p.position=:at"), bindings).mappings().first()
    status = "changed"
    if row and previous["quote"] in row["body"]:
        status = "retained"
    elif previous["quote"]:
        matches = db.execute(text(sql + "instr(p.body, :quote)>0 LIMIT 2"), bindings).mappings().all()
        if len(matches) == 1 and matches[0]["body"].count(previous["quote"]) == 1:
            row, status = matches[0], "moved"

    if not row or not row["body"].strip():
        return {"previous": previous, "current": None, "status": "no_text"}
    offset = max(0, row["body"].index(previous["quote"]) - 450) if status in {"retained", "moved"} else 0
    current = {
        "id": "", "fileId": previous["fileId"], "revision": row["revision"], "at": row["position"],
        "title": row["name"], "label": row["label"], "offset": offset, "excerpt": row["body"][offset:offset + MAX_EXCERPT],
    }
    return {"previous": previous, "current": current, "status": status}


def source_update_detail(db, course, impacted, revisions):
    statuses = set()
    for section in impacted:
        section["citations"] = [compare_source(db, ref, revisions[ref["fileId"]]) for ref in section["citations"]]
        statuses.update(entry["status"] for entry in section["citations"])
    advice = []
    if statuses & {"unavailable", "no_text"}:
        advice.append("为无法核对的引文找替代依据，或删改依赖这些引文的结论。")
    if "changed" in statuses:
        advice.append("用新资料逐条核对依赖已变更引文的结论，修订需要调整的表述并注明适用条件。")
    if statuses & {"moved", "retained"}:
        advice.append("核对仍能找到的引文和它在新版资料中的上下文，更新出处。")
    if any(section["dependencyTitles"] for section in impacted):
        advice.append("一并核对依赖这些结论的小节。")
    return {
        "version": course.version if course else None, "sections": impacted,
        "section": impacted[0]["index"] if len(impacted) == 1 else None,
        "instruction": "".join(advice),
    }
