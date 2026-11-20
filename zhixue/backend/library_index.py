from dataclasses import astuple, replace
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .db import LibraryOcrPage, transaction
from .documents import Extracted, MAX_DOCUMENT_BYTES, Section, extract
from .file_locations import InvalidLocation, LocationTracker, checked_path
from .library import FileTooLarge, LibraryDocument, documents, file_signature, read_snapshot
from .library_metadata import ACTIVE, METADATA_COLUMNS, METADATA_JOIN, Shelf
from .wiki_links import aliases, index_wiki


MAX_HITS = 80
HIDDEN = {"trashed", "purged"}


SEARCH_FILTERS = f"""(:kind IS NULL OR d.kind=:kind)
    AND {ACTIVE}
    AND (:format IS NULL OR d.format=:format)
    AND (:prefix IS NULL OR substr(d.path, 1, length(:prefix))=:prefix)
    AND (:project IS NULL OR EXISTS (SELECT 1 FROM json_each(d.project_ids) WHERE value=:project))
    AND (:tag IS NULL OR EXISTS (SELECT 1 FROM library_file_tags t WHERE t.file_id=d.id AND t.key=:tag))
    AND (:shelf!='favorites' OR a.favorite=1)
    AND (:shelf!='recent' OR a.last_viewed_at IS NOT NULL)"""


def search_scope(kind: str | None, file_format: str | None, project: str | None,
                 directory: str | None, tag: str | None, shelf: Shelf) -> dict:
    return {
        "kind": kind, "format": file_format, "project": project,
        "prefix": f"{directory}/" if directory else None, "tag": tag, "shelf": shelf,
    }


def apply_reviewed_pages(db: Session, item: LibraryDocument, revision: str | None, parsed: Extracted) -> Extracted:
    if revision is None or parsed.page_count is None:
        return parsed
    pages = db.scalars(select(LibraryOcrPage).where(
        LibraryOcrPage.file_id == item.id, LibraryOcrPage.revision == revision,
        LibraryOcrPage.page.between(1, parsed.page_count),
    )).all()
    if not pages:
        return parsed

    sections = {section.number: section for section in parsed.sections}
    for page in pages:
        label = f"第 {page.page} 页" if item.format == "pdf" else "图片"
        sections[page.page] = Section(page.page, f"{label} · 已校对 OCR", page.body, page.text_revision)
    covered = set(page.page for page in pages)
    unreadable = tuple(number for number in parsed.unreadable_pages if number not in covered)
    remaining = parsed.page_count - len(sections)
    messages = []
    if unreadable:
        messages.append("部分页面没有可提取文字，可逐页识别或查看原页。")
    if remaining:
        messages.append(f"另有 {remaining} 页超过自动提取范围，可逐页识别。")
    ordered = tuple(sections[number] for number in sorted(sections))
    return replace(
        parsed, sections=ordered, content="\n\n".join(section.text for section in ordered if section.text),
        status="partial" if messages else "ready", message="".join(messages) or None, unreadable_pages=unreadable,
    )


def replace_document(db: Session, folder: Path, item: LibraryDocument, fingerprint: str, revision: str | None, parsed: Extracted) -> None:
    parsed = apply_reviewed_pages(db, item, revision, parsed)
    values = {
        "id": item.id, "fingerprint": fingerprint, "revision": revision, "name": item.name,
        "kind": item.kind, "format": item.format, "path": item.path.relative_to(folder).as_posix(),
        "project_id": item.project_id, "project_topic": item.project_topic,
        "project_ids": json.dumps(item.project_ids or ([item.project_id] if item.project_id else [])),
        "status": parsed.status,
        "message": parsed.message, "page_count": parsed.page_count,
        "unreadable_pages": json.dumps(parsed.unreadable_pages),
    }
    db.execute(text("DELETE FROM library_index_documents WHERE id=:id"), {"id": item.id})
    db.execute(text("""INSERT INTO library_index_documents
        (id, fingerprint, revision, name, kind, format, path, project_id, project_topic, project_ids, status, message, page_count, unreadable_pages, wiki_indexed)
        VALUES (:id, :fingerprint, :revision, :name, :kind, :format, :path, :project_id, :project_topic, :project_ids, :status, :message, :page_count, :unreadable_pages, 1)
    """), values)
    searchable_name = f"{item.name} {item.path.name}" if item.path.name != item.name else item.name
    passages = [{"file": item.id, "position": 0, "label": None, "title": searchable_name,
                 "topic": item.project_topic or "", "body": "", "text_revision": None}]
    passages.extend({"file": item.id, "position": section.number, "label": section.label, "title": "",
                     "topic": "", "body": section.text, "text_revision": section.text_revision}
                    for section in parsed.sections)
    for passage in passages:
        body = passage["body"]
        if passage["position"] and body.strip():
            basis = json.dumps([item.id, passage["position"], passage["text_revision"], body],
                               ensure_ascii=False, separators=(",", ":"))
            passage["semantic_key"] = sha256(basis.encode()).digest()
        else:
            passage["semantic_key"] = None
    db.execute(text("""INSERT INTO library_passages (file_id, position, label, title, topic, body, text_revision, semantic_key)
        VALUES (:file, :position, :label, :title, :topic, :body, :text_revision, :semantic_key)"""), passages)
    index_wiki(db, item, values["path"], revision, parsed)


def remember_previous_names(db: Session, folder: Path, items: list[LibraryDocument]) -> None:
    if not items:
        return
    locations = []
    for item in items:
        path = item.path.relative_to(folder).as_posix()
        locations.append({"id": item.id, "name": item.name, "path": path, "names": json.dumps(sorted(aliases(item, path)))})
    db.execute(text("""INSERT OR IGNORE INTO library_wiki_aliases (file_id, key)
        SELECT n.file_id, n.key FROM library_wiki_names n JOIN library_index_documents d ON d.id=n.file_id
        WHERE d.id=:id AND (d.name != :name OR d.path != :path)
          AND n.key NOT IN (SELECT value FROM json_each(:names))
          AND NOT EXISTS (
            SELECT 1 FROM library_wiki_names other
            WHERE other.key=n.key AND other.file_id!=n.file_id
              AND NOT EXISTS (SELECT 1 FROM library_locations l WHERE l.id=other.file_id AND l.trash_id IS NOT NULL)
          )"""), locations)


def prepare_document(folder: Path, item: LibraryDocument, previous: str | None):
    metadata = [value.as_posix() if isinstance(value, Path) else value for value in astuple(item)[:-1]]
    if item.snapshot_encoding is not None:
        metadata.append(item.snapshot_encoding)
    if item.location_status != "ready":
        status = "failed" if item.location_status == "unreadable" else "missing"
        message = item.location_message or "找不到原件。可检查原路径或在知识库修复文件位置。"
        fingerprint = json.dumps([metadata, status], ensure_ascii=False)
        return None if fingerprint == previous else (fingerprint, None, Extracted(status=status, message=message))
    try:
        path = checked_path(folder, item.path.relative_to(folder).as_posix())
        signature = file_signature(path)
        fingerprint = json.dumps([metadata, signature], ensure_ascii=False)
        if fingerprint == previous:
            return None
        snapshot = read_snapshot(path)
        fingerprint = json.dumps([metadata, snapshot.signature], ensure_ascii=False)
        revision = snapshot.revision
        encoding = item.snapshot_encoding[1] if item.snapshot_encoding and item.snapshot_encoding[0] == revision else None
        return fingerprint, revision, extract(snapshot.content, item.format, encoding=encoding)
    except FileNotFoundError:
        status, message = "missing", "找不到原件。可检查知识库中的原路径。"
    except FileTooLarge:
        status, message = "failed", f"文件超过 {MAX_DOCUMENT_BYTES // 1024**2} MiB 的读取上限。"
    except (OSError, InvalidLocation):
        status, message = "failed", "文件暂时无法读取。请检查路径和权限后刷新。"
    fingerprint = json.dumps([metadata, status], ensure_ascii=False)
    return None if fingerprint == previous else (fingerprint, None, Extracted(status=status, message=message))


class LibraryIndex:
    def __init__(self, engine: Engine, folder: Path):
        self.engine = engine
        self.folder = folder.resolve()
        self.lock = Lock()

    def refresh(self, *, rebuild: bool = False, identity: str | None = None) -> int:
        with self.lock:
            return self._refresh(rebuild, identity)

    def _refresh(self, rebuild: bool, identity: str | None) -> int:
        # Recheck each file's original revision under the writer lock before publishing its passages.
        with transaction(self.engine) as db:
            previous = {row.id: (row.fingerprint, row.wiki_indexed) for row in db.execute(text(
                "SELECT id, fingerprint, wiki_indexed FROM library_index_documents"
            ))}
            tracker = LocationTracker(db, self.folder)
            items = [item for item in tracker.documents(documents(db, self.folder, set(previous) | tracker.locations.keys()))
                     if item.location_status not in HIDDEN]
        changes = []
        for item in items:
            if identity is not None and item.id != identity:
                continue
            fingerprint, wiki_indexed = previous.get(item.id, (None, False))
            # Preserve the old fingerprint for location recovery during a link-index migration.
            prepared = prepare_document(self.folder, item, fingerprint if wiki_indexed and not rebuild else None)
            if prepared is not None:
                changes.append((item, *prepared))
        stale = set(previous).difference(item.id for item in items)
        if not (changes or stale or rebuild):
            return 0

        with transaction(self.engine) as db:
            latest = {row.id: (row.fingerprint, row.wiki_indexed) for row in db.execute(text(
                "SELECT id, fingerprint, wiki_indexed FROM library_index_documents"
            ))}
            tracker = LocationTracker(db, self.folder)
            current = {item.id: item for item in tracker.documents(documents(db, self.folder, set(latest) | tracker.locations.keys()))
                       if item.location_status not in HIDDEN}
            removed = stale - current.keys()
            if removed:
                db.execute(text("DELETE FROM library_index_documents WHERE id=:id"), [{"id": item_id} for item_id in removed])
            ready = []
            for item, fingerprint, revision, parsed in changes:
                if current.get(item.id) != item or latest.get(item.id) != previous.get(item.id):
                    continue
                if revision is not None:
                    try:
                        signature = file_signature(checked_path(self.folder, item.path.relative_to(self.folder).as_posix()))
                    except (OSError, InvalidLocation):
                        continue
                    if signature != json.loads(fingerprint)[1]:
                        continue
                ready.append((item, fingerprint, revision, parsed))
            # Resolve old names against the same index snapshot for every file in a moved directory.
            remember_previous_names(db, self.folder, [item for item, *_ in ready])
            for item, fingerprint, revision, parsed in ready:
                if revision is not None and (location := tracker.locations.get(item.id)) is not None:
                    location.revision = revision
                replace_document(db, self.folder, item, fingerprint, revision, parsed)
            if rebuild:
                db.execute(text("INSERT INTO library_fts(library_fts) VALUES ('rebuild')"))
                db.execute(text("INSERT INTO library_fts(library_fts, rank) VALUES ('integrity-check', 1)"))
            return len(ready)

    def list(self) -> list[dict]:
        with transaction(self.engine, write=False) as db:
            rows = db.execute(text(f"""SELECT d.*, {METADATA_COLUMNS} FROM library_index_documents d {METADATA_JOIN}
                WHERE {ACTIVE}
                ORDER BY d.name, d.id""")).mappings()
            return [document_info(row) for row in rows]

    def search(self, query: str, kind: str | None, file_format: str | None, project: str | None,
               directory: str | None = None, tag: str | None = None, shelf: Shelf = "all") -> dict:
        with transaction(self.engine, write=False) as db:
            return keyword_search(query, search_scope(kind, file_format, project, directory, tag, shelf),
                                  lambda sql, params: db.execute(text(sql), params).mappings())

    def indexed_document(self, identity: str, *, with_passages: bool):
        with transaction(self.engine, write=False) as db:
            row = db.execute(text(f"""SELECT d.*, {METADATA_COLUMNS} FROM library_index_documents d {METADATA_JOIN}
                WHERE d.id=:id"""), {"id": identity}).mappings().first()
            if row is None or not with_passages:
                return row, ()
            passages = db.execute(text("""SELECT position, label, body, text_revision FROM library_passages
                WHERE file_id=:id AND position>0 ORDER BY position"""), {"id": identity})
            return row, tuple(Section(*passage) for passage in passages)


def keyword_search(query: str, scope: dict, execute) -> dict:
    if not query:
        return {"hits": [], "hasMore": False}
    matches = "library_fts MATCH :phrase" if len(query) >= 3 else (
        "instr(unicode_fold(p.body), :folded) > 0 OR instr(unicode_fold(p.title), :folded) > 0 OR instr(unicode_fold(p.topic), :folded) > 0"
    )
    source = "library_fts JOIN library_passages p ON p.id=library_fts.rowid" if len(query) >= 3 else "library_passages p"
    params = {
        "phrase": '"' + query.replace('"', '""') + '"', "folded": query.casefold(),
        **scope, "limit": MAX_HITS + 1,
    }
    statement = f"""SELECT d.*, {METADATA_COLUMNS}, p.position, p.label, p.body, p.text_revision FROM {source}
        JOIN library_index_documents d ON p.file_id=d.id
        {METADATA_JOIN}
        WHERE ({matches}) AND {SEARCH_FILTERS}
        ORDER BY CASE WHEN :shelf='recent' THEN a.last_viewed_at END DESC,
            (p.position=0) DESC, d.name, d.id, p.position LIMIT :limit
    """
    rows = list(execute(statement, params))
    hits = [{**document_info(row),
             "location": location_info(row["position"], row["label"], row["text_revision"]) if row["position"] else None,
             "snippet": excerpt(row["body"] or row["name"], query)} for row in rows[:MAX_HITS]]
    return {"hits": hits, "hasMore": len(rows) > MAX_HITS}


def location_info(number: int, label: str, text_revision: str | None) -> dict:
    location = {"number": number, "label": label}
    if text_revision is not None:
        location["textRevision"] = text_revision
    return location


def document_info(row) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "format": row["format"], "name": row["name"], "path": row["path"],
        "projectId": row["project_id"], "projectTopic": row["project_topic"],
        "projectIds": json.loads(row["project_ids"]), "revision": row["revision"],
        "status": row["status"], "message": row["message"],
        "pageCount": row["page_count"], "unreadablePages": json.loads(row["unreadable_pages"]),
        "tags": json.loads(row["tags"]), "favorite": bool(row["favorite"]), "lastViewedAt": row["last_viewed_at"],
    }


def excerpt(body: str, query: str) -> str:
    folded_body, folded_query = body.casefold(), query.casefold()
    match = folded_body.find(folded_query)
    first = max(0, match)
    last = first + (len(folded_query) if match >= 0 else len(query))
    if match >= 0 and len(folded_body) != len(body):
        # A folded character such as ß can occupy multiple positions.
        first, folded_end = 0, 0
        for position, char in enumerate(body, 1):
            folded_end += len(char.casefold())
            if folded_end <= match:
                first = position
            if folded_end >= last:
                last = position
                break
    start = max(0, first - 30)
    end = min(len(body), last + 70)
    return ("…" if start else "") + body[start:end].replace("\n", " ") + ("…" if end < len(body) else "")
