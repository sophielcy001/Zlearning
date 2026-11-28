from datetime import UTC, datetime
from typing import Annotated, Literal
from unicodedata import category, normalize

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import bindparam, text

from .db import transaction


Shelf = Literal["all", "favorites", "recent"]
FileId = Annotated[str, Field(min_length=1, max_length=100)]
MAX_SELECTION = 100
ACTIVE = "NOT EXISTS (SELECT 1 FROM library_locations l WHERE l.id=d.id AND l.trash_id IS NOT NULL)"
METADATA_JOIN = "LEFT JOIN library_file_activity a ON a.file_id=d.id"
METADATA_COLUMNS = """coalesce(a.favorite, 0) AS favorite, a.last_viewed_at, a.last_viewed_order,
    (SELECT json_group_array(json_object('key', key, 'name', name)) FROM (
        SELECT key, name FROM library_file_tags WHERE file_id=d.id ORDER BY key
    )) AS tags"""


class FileSelection(BaseModel):
    ids: list[FileId] = Field(min_length=1, max_length=MAX_SELECTION)

    @field_validator("ids")
    @classmethod
    def distinct(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids):
            raise ValueError("一个批次中每份文件只能选择一次。")
        return ids


class TagChange(FileSelection):
    tag: str = Field(min_length=1, max_length=80)
    action: Literal["add", "remove"]


class FavoriteChange(BaseModel):
    favorite: bool = Field(strict=True)


def tag_name(value: str) -> str:
    result = normalize("NFC", value.strip())
    if not result or any(category(char) in {"Cc", "Cs"} for char in result):
        raise ValueError("标签需要是非空的单行文字。")
    return result


def tag_key(value: str) -> str:
    return tag_name(value).casefold()


def add_file_tags(db, values: list[dict]) -> None:
    if values:
        db.execute(text("""INSERT INTO library_file_tags (file_id, key, name) VALUES (:id, :key, :name)
            ON CONFLICT(file_id, key) DO NOTHING"""), values)


def selected_documents(db, ids: list[str]):
    statement = text(f"""SELECT d.id, d.name, d.format, d.kind, d.path, d.status, d.revision, d.fingerprint,
        {METADATA_COLUMNS} FROM library_index_documents d {METADATA_JOIN}
        WHERE d.id IN :ids AND {ACTIVE}""").bindparams(bindparam("ids", expanding=True))
    return {row.id: row for row in db.execute(statement, {"ids": ids})}


def metadata_router(engine, index) -> APIRouter:
    router = APIRouter()

    @router.post("/tags")
    def change_tags(data: TagChange):
        try:
            name = tag_name(data.tag)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        index.refresh()
        with transaction(engine) as db:
            current = selected_documents(db, data.ids)
            if len(current) != len(data.ids):
                raise HTTPException(409, "选中的文件已变化，请刷新文件列表后重新选择。")
            values = [{"id": file_id, "key": name.casefold(), "name": name} for file_id in data.ids]
            if data.action == "add":
                add_file_tags(db, values)
            else:
                db.execute(text("DELETE FROM library_file_tags WHERE file_id=:id AND key=:key"), values)
            return {"count": len(current)}

    @router.put("/{identity}/favorite")
    def favorite(identity: str, data: FavoriteChange):
        index.refresh(identity=identity)
        with transaction(engine) as db:
            if identity not in selected_documents(db, [identity]):
                raise HTTPException(404, "找不到可收藏的文件。")
            db.execute(text("""INSERT INTO library_file_activity (file_id, favorite) VALUES (:id, :favorite)
                ON CONFLICT(file_id) DO UPDATE SET favorite=excluded.favorite"""), {
                "id": identity, "favorite": data.favorite,
            })
        return {"favorite": data.favorite}

    @router.post("/{identity}/opened")
    def opened(identity: str):
        with transaction(engine) as db:
            row = selected_documents(db, [identity]).get(identity)
            if row is None or row.status == "missing":
                raise HTTPException(404, "找不到可访问的文件。")
            time = datetime.now(UTC).isoformat(timespec="microseconds")
            # The enclosing immediate transaction serializes visits across writers.
            db.execute(text("""INSERT INTO library_file_activity (file_id, favorite, last_viewed_at, last_viewed_order)
                VALUES (:id, 0, :time, (SELECT coalesce(max(last_viewed_order), 0) + 1 FROM library_file_activity))
                ON CONFLICT(file_id) DO UPDATE SET
                    last_viewed_at=excluded.last_viewed_at, last_viewed_order=excluded.last_viewed_order"""),
                {"id": identity, "time": time})
        return {"lastViewedAt": time}

    return router
