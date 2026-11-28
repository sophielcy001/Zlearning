from collections.abc import Callable
from datetime import UTC, date, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from subprocess import SubprocessError, TimeoutExpired
from threading import Lock
from typing import Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .db import ImportedFile, LibraryLocation, LibraryOcrPage, LibraryOcrVersion, LibraryWebSnapshot, transaction
from .recognition import Recognition
from .documents import FORMATS, MAX_DOCUMENT_BYTES, MAX_OCR_TEXT, MAX_TEXT_BYTES, RASTER_FORMATS, TEXT_FORMATS, run_parser
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path, identity_of
from .library import FileTooLarge, atomic_write_bytes, documents, file_signature, local_path, read_snapshot
from .library_batch import batch_router
from .library_folders import directory_path, folders_router
from .library_index import LibraryIndex, document_info, location_info, search_scope
from .library_metadata import Shelf, metadata_router, tag_key
from .library_semantic import INDEX_ERROR, SemanticIndex
from .library_trash import file_tombstone
from .library_trash_api import trash_router
from .notes import diff_lines
from .web_fetch import MAX_URL_LENGTH, Snapshot, WebPageError, download_page
from .wiki_links import MAX_WIKI_LENGTH, backlinks, resolve, suggest


Kind = Literal["course", "note", "import", "supplement"]
Format = Literal["txt", "md", "pdf", "docx", "png", "jpg", "jpeg", "html"]
Duplicate = Literal["ask", "reuse", "copy"]


def validate_filename(name: str, formats=FORMATS) -> str:
    if any(ord(char) < 32 or char in '/\\:<>|?*"' for char in name) or Path(name).suffix.lower()[1:] not in formats:
        raise ValueError("请选择 TXT、Markdown、PDF、DOCX、PNG、JPEG 或 HTML 文件。")
    return name


class TextImport(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    content: str = Field(min_length=1, max_length=50_000)

    @field_validator("name")
    @classmethod
    def safe_name(cls, name: str) -> str:
        return validate_filename(name.strip(), TEXT_FORMATS)


class WebImport(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)


class WebCopy(BaseModel):
    fileId: UUID
    revision: str = Field(pattern="^[0-9a-f]{64}$")


def web_filename(title: str) -> str:
    cleaned = "".join(" " if ord(char) < 32 or char in '/\\:<>|?*"' else char for char in title)
    label = " ".join(cleaned.split())[:174].strip(" .")
    return f"{label or '网页快照'}.html"


class RepairPath(BaseModel):
    path: str = Field(min_length=1, max_length=1400)


class RepairConfirmation(RepairPath):
    revision: str = Field(pattern="^[0-9a-f]{64}$")


class OcrDraft(BaseModel):
    revision: str = Field(pattern="^[0-9a-f]{64}$")


class ExternalOcrDraft(OcrDraft):
    authorizationId: UUID


class OcrBase(OcrDraft):
    textRevision: str | None = Field(pattern="^[0-9a-f]{64}$")


class OcrConfirmation(OcrBase):
    text: str = Field(min_length=1, max_length=MAX_OCR_TEXT)

    @field_validator("text")
    @classmethod
    def searchable(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("请输入可搜索的校对文字。")
        return value


def document_path(folder: Path, info: dict) -> Path:
    return checked_path(folder, info["path"])


def validate_revision(info: dict, expected: str | None) -> None:
    if expected and expected != info["revision"]:
        raise HTTPException(409, "文件已更新，原定位可能已变化。请重新搜索。")


def read_current(folder: Path, info: dict):
    if info["status"] in {"trashed", "purged"}:
        raise HTTPException(409, "此文件当前不可打开，请查看回收站记录。")
    if info["status"] == "missing":
        raise HTTPException(409, "请先修复原文件的位置。")
    try:
        snapshot = read_snapshot(document_path(folder, info))
    except (OSError, InvalidLocation, FileTooLarge) as failure:
        raise HTTPException(409, "本地文件暂时无法读取，请检查后重试。") from failure
    if info["revision"] and snapshot.revision != info["revision"]:
        raise HTTPException(409, "文件正在更新，请刷新后重试。")
    return snapshot


def find_duplicate(db: Session, folder: Path, suffix: str, content: bytes, revision: str, webpage: Snapshot | None):
    tracker = LocationTracker(db, folder)
    matches = select(ImportedFile).order_by(ImportedFile.created_on, ImportedFile.id)
    if webpage is not None:
        matches = matches.join(LibraryWebSnapshot).where(
            LibraryWebSnapshot.url == webpage.url, LibraryWebSnapshot.revision == revision,
            LibraryWebSnapshot.encoding == webpage.encoding,
        )
    for item in db.scalars(matches):
        if Path(item.name).suffix.lower() != suffix:
            continue
        try:
            location = tracker.resolve(item.id, local_path(folder, "imports", item.id, suffix))
            if location.status != "ready":
                continue
            path = location.path
            if path.stat().st_size == len(content) and read_snapshot(path).revision == revision:
                return item
        except (OSError, InvalidLocation, FileTooLarge):
            continue
    return None


def ocr_version_data(source: str, revision: str, saved: datetime | None) -> dict:
    return {"sourceRevision": source, "textRevision": revision,
            "savedAt": saved.replace(tzinfo=UTC).isoformat() if saved is not None else None}


def remember_ocr(db: Session, identity: str, number: int, revision: str,
                 source: str, body: str, saved_at: datetime | None = None):
    key = (identity, number, revision)
    if db.get(LibraryOcrVersion, key) is None:
        db.add(LibraryOcrVersion(file_id=identity, page=number, text_revision=revision,
                                 source_revision=source, body=body, saved_at=saved_at))


def library_router(
    engine: Engine, folder: Path, day: Callable[[Session], date], index: LibraryIndex, recognition: Recognition,
    now: Callable[[], datetime],
) -> APIRouter:
    router = APIRouter(prefix="/api/files")
    folder = folder.resolve()
    semantic = SemanticIndex(folder)
    recognizing = Lock()
    downloading = Lock()

    def store(name: str, content: bytes, duplicate: Duplicate, webpage: Snapshot | None = None) -> tuple[int, dict]:
        revision = sha256(content).hexdigest()
        path = None
        written = None
        try:
            with transaction(engine) as db:
                suffix = Path(name).suffix.lower()
                if duplicate != "copy":
                    existing = find_duplicate(db, folder, suffix, content, revision, webpage)
                    if existing:
                        result = {"status": "duplicate" if duplicate == "ask" else "reused", "id": existing.id, "name": existing.name}
                        if webpage is not None:
                            result["revision"] = revision
                        return 200, result
                item = ImportedFile(id=str(uuid4()), name=name, created_on=day(db))
                db.add(item)
                if webpage is not None:
                    db.flush()
                    db.add(LibraryWebSnapshot(
                        file_id=item.id, requested_url=webpage.requested_url, url=webpage.url,
                        fetched_at=webpage.fetched_at, encoding=webpage.encoding, revision=revision,
                    ))
                path = checked_path(folder, local_path(folder, "imports", item.id, suffix).relative_to(folder).as_posix())
                atomic_write_bytes(path, content)
                written = file_signature(path)
                if (identity := identity_of(path)) is not None:
                    LocationTracker(db, folder).bind(item.id, path, identity, revision)
                result = {"status": "created", "id": item.id}
            return 201, result
        except (OSError, InvalidLocation, LocationConflict, SQLAlchemyError) as failure:
            if path is not None and written is not None:
                try:
                    if file_signature(path) == written:
                        path.unlink()
                except OSError:
                    pass
            raise HTTPException(409, "本地文件保存失败，请检查目录权限和空间。") from failure

    @router.get("")
    def list_files():
        index.refresh()
        return index.list()

    @router.post("", status_code=201)
    def import_text(payload: TextImport):
        _, result = store(payload.name, payload.content.encode("utf-8"), "copy")
        return {"id": result["id"]}

    @router.post("/upload", status_code=201)
    async def upload(request: Request, response: Response, name: str = Query(min_length=1, max_length=180), duplicate: Duplicate = "ask"):
        try:
            name = validate_filename(name.strip())
        except ValueError as failure:
            raise HTTPException(422, str(failure)) from failure
        limit = MAX_TEXT_BYTES if Path(name).suffix.lower()[1:] in TEXT_FORMATS else MAX_DOCUMENT_BYTES
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > limit:
                raise HTTPException(413, f"文件大小超过 {limit // 1024} KiB。")
            content.extend(chunk)
        if not content:
            raise HTTPException(422, "请选择有内容的文件。")
        status, result = await run_in_threadpool(store, name, bytes(content), duplicate)
        response.status_code = status
        return result

    @router.post("/web", status_code=201)
    def import_web(payload: WebImport, response: Response):
        if not downloading.acquire(blocking=False):
            raise HTTPException(429, "正在导入另一份网页，请稍后重试。")
        try:
            webpage = download_page(payload.url)
        except WebPageError as failure:
            raise HTTPException(failure.status, str(failure)) from failure
        except TimeoutExpired as failure:
            raise HTTPException(504, "读取网页超时，请检查网络或选择较短的网页。") from failure
        except (OSError, SubprocessError, ValueError) as failure:
            raise HTTPException(502, "暂时无法获取网页快照，请重试。") from failure
        finally:
            downloading.release()
        name = web_filename(webpage.title)
        status, result = store(name, webpage.body, "ask", webpage)
        response.status_code = status
        return {"name": name, **result}

    @router.get("/semantic-index")
    def semantic_status():
        return semantic.status()

    @router.post("/semantic-index", status_code=202)
    def prepare_semantic():
        index.refresh()
        try:
            return semantic.request()
        except (OSError, sqlite3.Error) as error:
            raise HTTPException(503, INDEX_ERROR) from error

    @router.get("/search")
    def search(q: str = Query(min_length=1, max_length=120), kind: Kind | None = None, format: Format | None = None,
               project: UUID | None = None, directory: str | None = Query(default=None, min_length=1, max_length=1400),
               tag: str | None = Query(default=None, min_length=1, max_length=300), shelf: Shelf = "all",
               mode: Literal["keyword", "hybrid"] = "keyword"):
        if directory is not None:
            try:
                directory_path(folder, directory, allow_missing_root=True)
            except InvalidLocation as error:
                raise HTTPException(422, str(error)) from error
            except (LocationConflict, OSError) as error:
                raise HTTPException(409, "当前文件夹已移动或无法读取，请重新选择。") from error
        try:
            tag = tag_key(tag) if tag is not None else None
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        index.refresh()
        query = q.strip()
        scope = (kind, format, str(project) if project else None, directory, tag, shelf)
        if mode == "hybrid":
            return semantic.hybrid(query, search_scope(*scope), lambda: index.search(query, *scope))
        return index.search(query, *scope)

    @router.post("/reindex")
    def reindex():
        documents = index.refresh(rebuild=True)
        try:
            semantic.rebuild()
        except (OSError, sqlite3.Error) as error:
            raise HTTPException(503, INDEX_ERROR) from error
        return {"documents": documents}

    @router.get("/wiki/suggestions")
    def wiki_suggestions(q: str = Query(default="", max_length=MAX_WIKI_LENGTH)):
        index.refresh()
        with transaction(engine, write=False) as db:
            return suggest(db, q)

    @router.get("/wiki/resolve")
    def wiki_resolve(target: str = Query(min_length=1, max_length=MAX_WIKI_LENGTH)):
        index.refresh()
        with transaction(engine, write=False) as db:
            return resolve(db, target)

    @router.get("/{identity}/backlinks")
    def wiki_backlinks(identity: str):
        index.refresh()
        with transaction(engine, write=False) as db:
            return backlinks(db, identity)

    def repair_item(db, identity: str):
        tracker = LocationTracker(db, folder)
        known = set(db.scalars(text("SELECT id FROM library_index_documents"))) | tracker.locations.keys()
        entries = documents(db, folder, known)
        item = next((item for item in entries if item.id == identity), None)
        if item is None:
            raise HTTPException(404, "找不到这条文件记录。")
        tracker.documents([entry for entry in entries if entry.id != identity])
        return tracker, item, tracker.repairable(identity, item.path)

    @router.get("/{identity}/repair")
    def repair_options(identity: str):
        try:
            with transaction(engine) as db:
                tracker, item, previous = repair_item(db, identity)
                paths = [tracker.relative(path) for path in tracker.inventory()
                         if path.suffix.lower() == item.path.suffix.lower()
                         and not tracker.owned_elsewhere(tracker.relative(path), identity)]
                return {"previousPath": tracker.relative(previous.path), "candidates": sorted(paths)[:60],
                        "hasMore": len(paths) > 60}
        except LocationConflict as error:
            raise HTTPException(409, str(error)) from error

    def apply_repair(identity: str, payload: RepairPath, requested_revision: str | None = None):
        try:
            with transaction(engine) as db:
                tracker, item, previous = repair_item(db, identity)
                path, _ = tracker.candidate(identity, payload.path, item.path.suffix.lower())
                snapshot = read_snapshot(path)
                last = tracker.locations.get(identity)
                same = snapshot.revision == last.revision if last and last.revision else None
                if requested_revision is None:
                    return {"path": payload.path, "previousPath": tracker.relative(previous.path), "revision": snapshot.revision,
                            "sameContent": same, "size": len(snapshot.content),
                            "excerpt": snapshot.content[:420].decode("utf-8", errors="replace") if item.format in TEXT_FORMATS else None}
                current = identity_of(path)
                if snapshot.revision != requested_revision or current is None or file_signature(path) != snapshot.signature:
                    raise LocationConflict("候选文件在预览后发生变化，请重新预览。")
                tracker.bind(identity, path, current, snapshot.revision)
            index.refresh(identity=identity)
            return {"id": identity, "path": payload.path, "revision": snapshot.revision}
        except InvalidLocation as error:
            raise HTTPException(422, str(error)) from error
        except LocationConflict as error:
            raise HTTPException(409, str(error)) from error
        except (OSError, FileTooLarge) as error:
            raise HTTPException(409, "候选文件无法读取或大小超过限制。请重新选择。") from error

    @router.post("/{identity}/repair/preview")
    def preview_repair(identity: str, payload: RepairPath):
        return apply_repair(identity, payload)

    @router.put("/{identity}/repair")
    def confirm_repair(identity: str, payload: RepairConfirmation):
        return apply_repair(identity, payload, payload.revision)

    router.include_router(trash_router(engine, folder, index))
    router.include_router(folders_router(engine, folder, index))
    router.include_router(metadata_router(engine, index))
    router.include_router(batch_router(engine, folder, index, day))

    def find(identity: str, *, with_passages: bool = True):
        index.refresh(identity=identity)
        with transaction(engine, write=False) as db:
            if removed := file_tombstone(db, identity):
                return removed, ()
        row, sections = index.indexed_document(identity, with_passages=with_passages)
        if row is None:
            raise HTTPException(404, "找不到这条文件记录。")
        return document_info(row), sections

    @router.post("/web/copy", status_code=201)
    def copy_web(payload: WebCopy):
        identity = str(payload.fileId)
        info, _ = find(identity, with_passages=False)
        validate_revision(info, payload.revision)
        with transaction(engine, write=False) as db:
            source = db.get(LibraryWebSnapshot, identity)
            if source is None or source.revision != payload.revision:
                raise HTTPException(409, "原网页快照已变化，请重新导入或查看当前文件。")
            fields = (source.requested_url, source.url, source.fetched_at, source.encoding)
        body = read_current(folder, info).content
        webpage = Snapshot(*fields, info["name"], body)
        _, result = store(info["name"], body, "copy", webpage)
        return result

    @router.get("/{identity}")
    def preview(identity: str, at: int | None = Query(default=None, ge=1), revision: str | None = Query(default=None, max_length=64)):
        info, sections = find(identity)
        if info["status"] in {"trashed", "purged"}:
            return {**info, "content": "", "markdown": False, "anchor": None}
        file_format = info["format"]
        raster = file_format in RASTER_FORMATS and info["pageCount"]
        position = (at or 1) if raster else at
        section = next((entry for entry in sections if entry.number == position), None)
        if not section or not section.text_revision or revision != section.text_revision:
            validate_revision(info, revision)
        anchor = {**location_info(section.number, section.label, section.text_revision), "text": section.text} if section else None
        if raster:
            if position > info["pageCount"]:
                raise HTTPException(404, "找不到这一页。")
            if anchor is None:
                label = f"第 {position} 页" if file_format == "pdf" else "图片"
                anchor = {"number": position, "label": label, "text": ""}
            return {**info, "content": anchor["text"], "markdown": False, "anchor": anchor}

        if at is not None and anchor is None:
            message = "找不到网页快照中的这一节。" if file_format == "html" else "找不到文件中的这一行或段落。"
            raise HTTPException(404, message)
        content = "\n\n".join(section.text for section in sections if section.text.strip())
        if file_format == "html":
            with transaction(engine, write=False) as db:
                source = db.get(LibraryWebSnapshot, identity)
                web_source = {
                    "requestedUrl": source.requested_url, "url": source.url, "fetchedAt": source.fetched_at,
                    "matchesSnapshot": source.revision == info["revision"],
                } if source is not None else None
            return {
                **info, "content": anchor["text"] if anchor else content, "markdown": False, "anchor": anchor,
                "webSource": web_source, "sections": [location_info(entry.number, entry.label, None) for entry in sections],
            }
        if file_format in TEXT_FORMATS and info["status"] in {"ready", "partial", "empty"}:
            content = read_current(folder, info).content.decode("utf-8")
        return {**info, "content": content, "markdown": file_format == "md", "anchor": anchor}

    @router.get("/{identity}/original")
    def original(identity: str):
        info, _ = find(identity, with_passages=False)
        snapshot = read_current(folder, info)
        name = quote(info["name"], safe="")
        return Response(snapshot.content, media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})

    def raster_info(identity: str, revision: str | None):
        info, _ = find(identity, with_passages=False)
        validate_revision(info, revision)
        if info["format"] not in RASTER_FORMATS or not info["pageCount"]:
            raise HTTPException(404, "找不到这一页。")
        return info

    def page_source(identity: str, number: int, revision: str | None):
        info = raster_info(identity, revision)
        if not 1 <= number <= info["pageCount"]:
            raise HTTPException(404, "找不到这一页。")
        return info, read_current(folder, info)

    def history_page_count(identity: str, number: int, revision: str) -> int:
        if number < 1:
            raise HTTPException(404, "找不到这一页。")
        info = raster_info(identity, revision)
        read_current(folder, info)
        return info["pageCount"]

    @router.get("/{identity}/pages/{number}/image")
    def page_image(identity: str, number: int, revision: str | None = Query(default=None, max_length=64)):
        info, snapshot = page_source(identity, number, revision)
        try:
            image = run_parser(snapshot.content, "page", info["format"], str(number))
        except (OSError, SubprocessError) as failure:
            raise HTTPException(422, "无法渲染这一页，可下载原件。") from failure
        return Response(image, media_type="image/png")

    @router.post("/{identity}/pages/{number}/ocr")
    def draft_ocr(identity: str, number: int, payload: OcrDraft):
        if not recognizing.acquire(blocking=False):
            raise HTTPException(429, "正在识别另一页，请稍后重试。")
        try:
            info, snapshot = page_source(identity, number, payload.revision)
            draft = json.loads(run_parser(snapshot.content, "ocr", info["format"], str(number)))
        except TimeoutExpired as failure:
            raise HTTPException(422, "本页识别超时，可对照原件手动录入。") from failure
        except (OSError, SubprocessError, ValueError) as failure:
            raise HTTPException(422, "本机无法识别这一页，可对照原件手动录入。") from failure
        finally:
            recognizing.release()
        if not draft["text"]:
            raise HTTPException(422, "本页未识别到文字，可对照原件手动录入。")
        return draft

    @router.post("/{identity}/pages/{number}/ocr/external")
    def draft_external_ocr(identity: str, number: int, payload: ExternalOcrDraft):
        info, snapshot = page_source(identity, number, payload.revision)
        try:
            image = run_parser(snapshot.content, "page", info["format"], str(number))
        except (OSError, SubprocessError, ValueError) as failure:
            raise HTTPException(422, "无法渲染所选页图，可对照原件手动录入。") from failure
        result = recognition.ocr(str(payload.authorizationId), image)
        return {"text": result.text[:MAX_OCR_TEXT], "truncated": result.truncated or len(result.text) > MAX_OCR_TEXT}

    def save_ocr(identity: str, number: int, payload: OcrBase, body: str):
        info, snapshot = page_source(identity, number, payload.revision)
        text_revision = sha256(f"{payload.revision}\0{number}\0{body}".encode()).hexdigest()
        with transaction(engine) as db:
            location = db.get(LibraryLocation, identity)
            if (location is None or location.trash_id or location.missing
                    or location.path != info["path"] or location.revision != payload.revision):
                raise HTTPException(409, "原件位置或版本已变化，请刷新预览后重新校对。")
            try:
                same_file = file_signature(document_path(folder, info)) == snapshot.signature
            except (OSError, InvalidLocation):
                same_file = False
            if not same_file:
                raise HTTPException(409, "原件已变化，请刷新预览后重新校对。")

            page = db.get(LibraryOcrPage, (identity, number))
            previous = page.text_revision if page is not None and page.revision == payload.revision else None
            if previous == text_revision:
                return {"textRevision": text_revision}
            if previous != payload.textRevision:
                raise HTTPException(409, "识别文本已在其他窗口保存，请刷新后重新校对。")
            if page is not None:
                remember_ocr(db, identity, number, page.text_revision, page.revision, page.body)
            remember_ocr(db, identity, number, text_revision, payload.revision, body, now())
            if page is None:
                page = LibraryOcrPage(file_id=identity, page=number)
                db.add(page)
            page.revision, page.text_revision, page.body = payload.revision, text_revision, body
            db.execute(text("UPDATE library_index_documents SET fingerprint='' WHERE id=:id"), {"id": identity})
        index.refresh(identity=identity)
        return {"textRevision": text_revision}

    @router.put("/{identity}/pages/{number}/ocr")
    def confirm_ocr(identity: str, number: int, payload: OcrConfirmation):
        return save_ocr(identity, number, payload, payload.text)

    @router.get("/{identity}/pages/{number}/ocr/versions")
    def ocr_versions(identity: str, number: int, revision: str = Query(pattern="^[0-9a-f]{64}$"), includePages: bool = False):
        count = history_page_count(identity, number, revision)
        with transaction(engine, write=False) as db:
            versions = [ocr_version_data(*row) for row in db.execute(
                select(LibraryOcrVersion.source_revision, LibraryOcrVersion.text_revision, LibraryOcrVersion.saved_at)
                .where(LibraryOcrVersion.file_id == identity, LibraryOcrVersion.page == number)
                .order_by(LibraryOcrVersion.saved_at.desc(), LibraryOcrVersion.text_revision)
            )]
            page = db.get(LibraryOcrPage, (identity, number))
            if page is not None and all(item["textRevision"] != page.text_revision for item in versions):
                versions.append(ocr_version_data(page.revision, page.text_revision, None))
            if number > count and not versions:
                raise HTTPException(404, "找不到这一页的校对历史。")
            current = page.text_revision if number <= count and page is not None and page.revision == revision else None
            result = {"currentTextRevision": current, "versions": versions}
            if includePages:
                archived = select(LibraryOcrVersion.page).where(LibraryOcrVersion.file_id == identity)
                existing = select(LibraryOcrPage.page).where(LibraryOcrPage.file_id == identity)
                result.update(pageCount=count, pages=list(db.scalars(archived.union(existing).order_by("page"))))
            return result

    @router.get("/{identity}/pages/{number}/ocr/versions/{text_revision}")
    def inspect_ocr_version(identity: str, number: int, text_revision: str, revision: str = Query(pattern="^[0-9a-f]{64}$")):
        count = history_page_count(identity, number, revision)
        with transaction(engine, write=False) as db:
            row = db.get(LibraryOcrVersion, (identity, number, text_revision))
            page = db.get(LibraryOcrPage, (identity, number))
            if row is not None:
                selected = {**ocr_version_data(row.source_revision, row.text_revision, row.saved_at), "text": row.body}
            elif page is not None and page.text_revision == text_revision:
                selected = {**ocr_version_data(page.revision, page.text_revision, None), "text": page.body}
            else:
                raise HTTPException(404, "找不到所选校对版本。")
            current = {"textRevision": page.text_revision, "text": page.body} if number <= count and page and page.revision == revision else None
            can_restore = (number <= count and selected["sourceRevision"] == revision
                           and (not current or current["textRevision"] != text_revision))
            return {"selected": selected, "current": current,
                    "diff": diff_lines({"content": current["text"]} if current else None, {"content": selected["text"]}),
                    "canRestore": can_restore}

    @router.post("/{identity}/pages/{number}/ocr/versions/{text_revision}/restore")
    def restore_ocr_version(identity: str, number: int, text_revision: str, payload: OcrBase):
        with transaction(engine, write=False) as db:
            row = db.get(LibraryOcrVersion, (identity, number, text_revision))
            if row is None:
                raise HTTPException(404, "找不到所选校对版本。")
            if row.source_revision != payload.revision:
                raise HTTPException(409, "这个校对版本对应旧原件，只供查阅。")
            body = row.body
        return save_ocr(identity, number, payload, body)

    return router
