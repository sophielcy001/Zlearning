import re
from pathlib import PurePosixPath
from unicodedata import normalize

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .course_publication import section_markdown
from .db import CourseVersion
from .documents import Extracted, TEXT_FORMATS
from .library import LibraryDocument
from .library_trash import file_tombstone


MAX_WIKI_LENGTH = 512
MAX_SUGGESTIONS = 16
STABLE_ID = re.compile(
    r"(?:note-|course-|supplement-|prerequisite-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
    re.IGNORECASE | re.ASCII,
)
REFERENCE = re.compile(r"\[\[([^\[\]\r\n]{1,512})\]\]")
AVAILABLE = "NOT EXISTS (SELECT 1 FROM library_locations l WHERE l.id=d.id AND l.trash_id IS NOT NULL)"


def target_key(value: str) -> str:
    return normalize("NFC", value.strip().replace("\\", "/")).casefold()


def wiki_rule(state: StateInline, silent: bool) -> bool:
    start = state.pos
    if (silent or state.linkLevel or not state.src.startswith("[[", start)
            or start > 0 and state.src[start - 1] == "!"):
        return False
    match = REFERENCE.match(state.src, start, state.posMax)
    if match is None or state.src[match.end():match.end() + 1] == "(":
        return False
    target = match[1].partition("|")[0].strip()
    if not target:
        return False
    token = state.push("wiki_reference", "", 0)
    token.meta = {"target": target, "offset": state.src.count("\n", 0, start)}
    state.pos = match.end()
    return True


markdown = MarkdownIt("commonmark").use(front_matter_plugin).use(dollarmath_plugin)
markdown.inline.ruler.after("link", "wiki_reference", wiki_rule)


def occurrences(source: str, first_line: int = 1):
    if "[[" not in source:
        return
    for token in markdown.parse(source.removeprefix("\ufeff")):
        if token.type == "inline" and token.map:
            for child in token.children or ():
                if child.type == "wiki_reference":
                    yield token.map[0] + child.meta["offset"] + first_line, child.meta["target"]


def course_occurrences(db: Session, identity: str, revision: str, content: str):
    version = db.scalar(select(CourseVersion).where(
        CourseVersion.lesson_id == identity.removeprefix("course-"), CourseVersion.revision == revision,
    ).order_by(CourseVersion.version.desc()).limit(1))
    if version is None or version.markdown != content:
        return
    # Published Markdown escapes section prose. Use only the archival sections for these exact bytes.
    parts = [section_markdown(section) for section in version.sections]
    if content.partition("\n\n")[2] != "\n\n".join(parts) + "\n":
        return
    line = 3
    for part, section in zip(parts, version.sections):
        yield from occurrences(section["text"], line + (2 if section["state"] == "verified" else 4))
        line += part.count("\n") + 2


def aliases(item: LibraryDocument, path: str) -> set[str]:
    filename = PurePosixPath(path)
    name = PurePosixPath(item.name)
    keys = {item.name, path, path.removeprefix("library/"), filename.name}
    if name.suffix.lower() == f".{item.format}":
        keys.add(name.stem)
    return {target_key(key) for key in keys if key.strip()}


def index_wiki(db: Session, item: LibraryDocument, path: str, revision: str | None, parsed: Extracted) -> None:
    db.execute(text("INSERT INTO library_wiki_names(key, file_id) VALUES (:key, :id)"),
               [{"key": key, "id": item.id} for key in aliases(item, path)])
    db.execute(text("""INSERT OR IGNORE INTO library_wiki_names (key, file_id)
        SELECT key, file_id FROM library_wiki_aliases WHERE file_id=:id"""), {"id": item.id})
    if revision is None or item.format not in TEXT_FORMATS:
        return

    matches = set(occurrences(parsed.content))
    if item.kind == "course" and r"\[\[" in parsed.content:
        matches.update(course_occurrences(db, item.id, revision, parsed.content))

    rows = set()
    last_position = len(parsed.sections)
    for position, target in matches:
        if position <= last_position:
            key = target_key(target)
            rows.add((position, key, key if STABLE_ID.fullmatch(target) else None))
    if rows:
        db.execute(text("""INSERT INTO library_wiki_links(source_id, position, key, target_id)
            VALUES (:id, :position, :key, :target)"""), [
            {"id": item.id, "position": position, "key": key, "target": target}
            for position, key, target in rows
        ])


def target_info(row) -> dict:
    return {key: row[key] for key in ("id", "name", "path", "kind", "status")} | {"projectTopic": row["project_topic"]}


def suggest(db: Session, term: str) -> list[dict]:
    key = target_key(term)
    rows = db.execute(text(f"""
        SELECT d.id, d.name, d.path, d.kind, d.status, d.project_topic
        FROM library_index_documents d
        WHERE {AVAILABLE} AND d.status != 'missing'
          AND (:key = '' OR instr(unicode_fold(d.id), :key) = 1 OR EXISTS (
            SELECT 1 FROM library_wiki_names n WHERE n.file_id=d.id AND instr(n.key, :key) > 0
          ))
        ORDER BY (unicode_fold(d.name) = :key) DESC, (instr(unicode_fold(d.name), :key) = 1) DESC,
          d.name, d.path, d.id LIMIT :limit
    """), {"key": key, "limit": MAX_SUGGESTIONS}).mappings()
    return [target_info(row) for row in rows]


def resolve(db: Session, target: str) -> dict:
    key = target_key(target)
    explicit = STABLE_ID.fullmatch(target.strip()) is not None
    rows = db.execute(text(f"""
        SELECT d.id, d.name, d.path, d.kind, d.status, d.project_topic
        FROM library_index_documents d
        WHERE {AVAILABLE} AND ((:explicit AND d.id=:key) OR (NOT :explicit AND EXISTS (
            SELECT 1 FROM library_wiki_names n WHERE n.key=:key AND n.file_id=d.id
        )))
        ORDER BY d.name, d.path, d.id
    """), {"key": key, "explicit": explicit}).mappings()
    choices = [target_info(row) for row in rows]
    if not choices and explicit and (removed := file_tombstone(db, key)):
        choices = [{field: removed[field] for field in ("id", "name", "path", "kind", "status", "projectTopic")}]
    state = "missing" if not choices else "resolved" if len(choices) == 1 else "ambiguous"
    return {"state": state, "choices": choices}


def backlinks(db: Session, identity: str) -> list[dict]:
    rows = db.execute(text(f"""
        WITH unique_names AS (
          SELECT n.key FROM library_wiki_names n
          WHERE n.file_id=:id AND NOT EXISTS (
            SELECT 1 FROM library_wiki_names other
            JOIN library_index_documents d ON d.id=other.file_id
            WHERE other.key=n.key AND other.file_id!=:id AND {AVAILABLE}
          )
        ), matching AS (
          SELECT source_id, position FROM library_wiki_links WHERE target_id=:id
          UNION
          SELECT source_id, position FROM library_wiki_links WHERE target_id IS NULL
            AND key IN (SELECT key FROM unique_names)
        )
        SELECT d.id, d.name, d.path, d.kind, d.status, d.project_topic, d.revision, m.position, p.label, p.body
        FROM matching m
        JOIN library_index_documents d ON d.id=m.source_id
        JOIN library_passages p ON p.file_id=m.source_id AND p.position=m.position
        WHERE {AVAILABLE}
        ORDER BY d.name, d.id, m.position
    """), {"id": identity}).mappings()
    return [{
        **target_info(row), "revision": row["revision"], "at": row["position"],
        "label": row["label"], "snippet": row["body"][:300] + ("…" if len(row["body"]) > 300 else ""),
    } for row in rows]
