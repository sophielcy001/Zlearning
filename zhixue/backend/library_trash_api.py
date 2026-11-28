from dataclasses import replace
from datetime import UTC, datetime
from hmac import compare_digest, digest
from json import dumps
from pathlib import Path
from secrets import token_bytes
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from .db import LibraryLocation, LibraryTrash, NoteWrite, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path, identity_of
from .library import documents
from .library_metadata import FileSelection
from .library_trash import VISIBLE, archive_path, describe, file_version, finish_operation, matches, recover_library_trash
from .wiki_links import referencing_sources, remember_trash_names


class RevisionInput(BaseModel):
    revision: str = Field(pattern="^[a-f0-9]{64}$")


class PathInput(RevisionInput):
    path: str = Field(min_length=1, max_length=1400)


class RestoreInput(PathInput):
    confirmation: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


def trash_router(engine, folder: Path, index) -> APIRouter:
    router = APIRouter()
    review_secret = token_bytes(32)

    def get_entry(db, identity: UUID) -> LibraryTrash:
        entry = db.get(LibraryTrash, str(identity))
        if entry is None:
            raise HTTPException(404, "找不到这条回收站记录。")
        return entry

    def candidates(db):
        tracker = LocationTracker(db, folder)
        known = set(db.scalars(text("SELECT id FROM library_index_documents"))) | tracker.locations.keys()
        return tracker, {item.id: item for item in documents(db, folder, known)}

    def source_file(tracker, entries, file_id: str):
        source = entries.get(file_id)
        if source is None:
            raise HTTPException(404, "找不到这条文件记录。")
        location = tracker.resolve(file_id, source.path)
        if location.status != "ready":
            raise HTTPException(409, location.message or "找不到原件，请先确认文件位置。")
        return source, location.path

    def inspect(path: Path):
        try:
            version = file_version(path)
        except (OSError, InvalidLocation, LocationConflict) as error:
            raise HTTPException(409, "暂时无法确认文件版本，请刷新后重试。") from error
        if version is None:
            raise HTTPException(409, "文件已移动或删除，请刷新。")
        return version

    def finish(identity: str, expected: str):
        with transaction(engine) as db:
            entry = db.get(LibraryTrash, identity)
            finish_operation(db, folder, entry)
            result = describe(folder, entry)
        if result["state"] != expected or expected == "trashed" and result["error"]:
            raise HTTPException(409, result["error"] or "操作尚未完成，请查看回收站状态。")
        return result

    @router.get("/trash")
    def trash_list():
        recover_library_trash(engine, folder)
        with transaction(engine, write=False) as db:
            entries = db.scalars(select(LibraryTrash).where(LibraryTrash.state.in_(VISIBLE))
                                 .order_by(LibraryTrash.deleted_at.desc(), LibraryTrash.id))
            return [describe(folder, entry) for entry in entries]

    def preview_file(db, tracker, entries, file_id: str):
        source, path = source_file(tracker, entries, file_id)
        version = inspect(path)
        name = path.name if source.kind == "import" and path.name != source.path.name else source.name
        return {"id": source.id, "name": name, "path": tracker.relative(path), "revision": version.revision,
                "kind": source.kind, "projectTopic": source.project_topic, "wikiImpact": referencing_sources(db, source.id)}

    @router.post("/trash/preview")
    def preview_batch(selection: FileSelection):
        index.refresh()
        with transaction(engine) as db:
            tracker, entries = candidates(db)
            results = []
            for file_id in selection.ids:
                try:
                    results.append({"id": file_id, "preview": preview_file(db, tracker, entries, file_id)})
                except HTTPException as error:
                    results.append({"id": file_id, "error": error.detail})
            return {"entries": results}

    @router.get("/{file_id}/trash")
    def preview_delete(file_id: str):
        index.refresh()
        with transaction(engine) as db:
            tracker, entries = candidates(db)
            return preview_file(db, tracker, entries, file_id)

    @router.post("/{file_id}/trash")
    def trash(file_id: str, payload: PathInput):
        with transaction(engine) as db:
            existing_location = db.get(LibraryLocation, file_id)
            existing = db.get(LibraryTrash, existing_location.trash_id) if existing_location and existing_location.trash_id else None
            if existing and existing.state in {"deleting", "trashed"} and (
                payload.path == existing.original_path and payload.revision == existing.revision
            ):
                identity = existing.id
            else:
                tracker, entries = candidates(db)
                source, path = source_file(tracker, entries, file_id)
                if source.kind == "note" and db.scalar(select(NoteWrite.id).where(
                    NoteWrite.note_id == file_id.removeprefix("note-"), NoteWrite.completed.is_(False),
                )):
                    raise HTTPException(409, "笔记还有未完成的保存，请先在课程页处理。")
                version = inspect(path)
                if tracker.relative(path) != payload.path or version.revision != payload.revision:
                    raise HTTPException(409, "原件路径或版本已变化，请重新查看后确认。")
                identity = str(uuid4())
                display_name = path.name if source.kind == "import" and path.name != source.path.name else source.name
                remember_trash_names(db, replace(source, name=display_name), payload.path)
                db.add(LibraryTrash(id=identity, file_id=file_id, name=display_name, kind=source.kind,
                                    format=source.format, project_id=source.project_id, project_topic=source.project_topic,
                                    original_path=payload.path, deleted_at=datetime.now(UTC).replace(tzinfo=None),
                                    revision=version.revision, file_key=version.identity.key, birth_time=version.identity.birth,
                                    state="deleting"))
                tracker.locations[file_id].trash_id = identity
        return finish(identity, "trashed")

    @router.post("/trash/{identity}/restore")
    def restore(identity: UUID, payload: RestoreInput):
        try:
            with transaction(engine) as db:
                entry = get_entry(db, identity)
                if entry.state == "restored" and entry.revision == payload.revision and entry.restore_path == payload.path:
                    return describe(folder, entry)
                if entry.state != "trashed" or entry.revision != payload.revision:
                    raise HTTPException(409, "回收站记录已变化，请刷新后重试。")
                tracker = LocationTracker(db, folder)
                destination = checked_path(folder, payload.path)
                if destination.suffix.lower() != f".{entry.format}":
                    raise InvalidLocation("恢复路径须保留原文件类型。")
                if tracker.owned_elsewhere(payload.path, entry.file_id) or identity_of(destination) is not None:
                    raise HTTPException(409, "恢复位置已占用，请填写其他路径。")
                version = inspect(archive_path(folder, entry))
                if version.revision != entry.revision:
                    raise HTTPException(409, "回收站原件内容已变化，请先检查。")
                if payload.confirmation is not None or not matches(entry, version):
                    if version.identity.key is None:
                        raise HTTPException(409, "暂时无法核对回收站原件的文件身份。")
                    review = dumps([
                        entry.id, entry.file_id, entry.original_path, entry.revision, entry.file_key,
                        entry.birth_time, payload.path, version.identity.key, version.identity.birth, version.signature,
                    ], separators=(",", ":")).encode()
                    confirmation = digest(review_secret, review, "sha256").hex()
                    if payload.confirmation is None:
                        return JSONResponse(status_code=409, content={
                            "code": "archive_identity",
                            "detail": "回收原件的文件身份已变化。当前内容与回收记录的版本一致。核对恢复位置后，可按此版本恢复。",
                            "confirmation": confirmation,
                        })
                    if not compare_digest(payload.confirmation, confirmation):
                        raise HTTPException(409, "回收站原件或恢复位置已变化，请重新核对。")
                    entry.file_key, entry.birth_time = version.identity.key, version.identity.birth
                entry.restore_path, entry.state, entry.error = payload.path, "restoring", None
            return finish(str(identity), "restored")
        except InvalidLocation as error:
            raise HTTPException(422, str(error)) from error
        except OSError as error:
            raise HTTPException(409, "暂时无法确认恢复位置。") from error

    @router.post("/trash/{identity}/purge")
    def purge(identity: UUID, payload: RevisionInput):
        with transaction(engine) as db:
            entry = get_entry(db, identity)
            if entry.state == "purged":
                return describe(folder, entry)
            if entry.state != "trashed" or entry.revision != payload.revision:
                raise HTTPException(409, "回收站记录已变化，请刷新后重试。")
            entry.state, entry.error = "purging", None
        return finish(str(identity), "purged")

    return router
