from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import ImportedFile, LibraryOcrPage
from .file_locations import InvalidLocation, LocationTracker, checked_path
from .library import FileTooLarge, local_path, read_snapshot
from .model_study_input import Input


MAX_EXCERPT = 2200
HexDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SourceLocator(Input):
    fileId: UUID
    revision: HexDigest
    at: Annotated[int, Field(strict=True, ge=1)]
    offset: Annotated[int, Field(strict=True, ge=0)] = 0


@contextmanager
def observe_sources(engine):
    # Location resolution can stage repairs. Observation discards them and does not reserve the writer lock.
    with Session(engine, autoflush=False) as db:
        db.execute(text("BEGIN"))
        yield db


def read_source_revisions(db, folder: Path, file_ids: set[str]) -> dict[str, str | None]:
    revisions: dict[str, str | None] = dict.fromkeys(file_ids)
    if not revisions:
        return revisions
    tracker = LocationTracker(db, folder)
    for file_id in file_ids:
        item = db.get(ImportedFile, file_id)
        if item is None:
            continue
        try:
            location = tracker.resolve(file_id, local_path(folder, "imports", file_id, Path(item.name).suffix.lower()))
            if location.status == "ready":
                safe = checked_path(folder, tracker.relative(location.path))
                revisions[file_id] = read_snapshot(safe).revision
        except (OSError, InvalidLocation, FileTooLarge):
            continue
    return revisions


def source_current(db, source: dict, revision: str | None) -> bool:
    if revision is None:
        return False
    number = source.get("at")
    if number is not None:
        page = db.get(LibraryOcrPage, (source["fileId"], number))
        if page is not None and page.revision == revision:
            return page.text_revision == source["revision"]
    return source["revision"] == revision


def sources_current(db, folder: Path, sources: list[dict]) -> bool:
    revisions = read_source_revisions(db, folder, {source["fileId"] for source in sources})
    return all(source_current(db, source, revisions[source["fileId"]]) for source in sources)


def locate_sources(db, folder: Path, locators: list[SourceLocator]) -> list[dict]:
    items = [(str(item.fileId), item.at, item.offset) for item in locators]
    if len(items) != len(set(items)):
        raise HTTPException(422, "请选择不同的资料定位。")
    sources = []
    for number, locator in enumerate(locators, 1):
        row = db.execute(text("""
            SELECT d.name, COALESCE(p.text_revision, d.revision) AS revision, p.label, p.body
            FROM library_index_documents d JOIN library_passages p ON p.file_id=d.id
            WHERE d.id=:identity AND d.kind='import' AND p.position=:position
        """), {"identity": str(locator.fileId), "position": locator.at}).mappings().first()
        if not row or row["revision"] != locator.revision:
            raise HTTPException(409, "资料或所选定位已经变化，请重新选择。")
        excerpt = row["body"][locator.offset:locator.offset + MAX_EXCERPT]
        if not excerpt.strip():
            raise HTTPException(422, "所选定位没有可用于复核的文字。")
        sources.append({
            "id": f"S{number}", "fileId": str(locator.fileId), "revision": locator.revision, "at": locator.at,
            "title": row["name"], "label": row["label"], "offset": locator.offset, "excerpt": excerpt,
        })
    if not sources_current(db, folder, sources):
        raise HTTPException(409, "资料已经修改或移除，请重新预览发送范围。")
    return sources
