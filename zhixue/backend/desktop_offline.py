import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, JsonValue


Area = Literal["items", "timers"]
Key = Annotated[str, Query(min_length=1)]


class OfflineWrite(BaseModel):
    revision: Annotated[int, Field(strict=True, ge=0, lt=2**53 - 1)]
    value: JsonValue = None


def desktop_offline_router(file: Path) -> APIRouter:
    router = APIRouter(prefix="/api/desktop-offline")
    with closing(sqlite3.connect(file)) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS offline_study (
                area TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT,
                PRIMARY KEY (area, key)
            ) STRICT
        """)

    def check_key(area: Area, key: str):
        if area == "timers" and key != "ledger":
            raise HTTPException(422, "本机离线记录标识无效。")

    @router.get("/items")
    def list_items():
        with closing(sqlite3.connect(file)) as db:
            rows = db.execute("""
                SELECT payload FROM offline_study
                WHERE area = 'items' AND payload IS NOT NULL ORDER BY key
            """)
            return [json.loads(payload) for (payload,) in rows]

    @router.get("/{area}/record")
    def read_record(area: Area, key: Key):
        check_key(area, key)
        with closing(sqlite3.connect(file)) as db:
            row = db.execute(
                "SELECT revision, payload FROM offline_study WHERE area = ? AND key = ?", (area, key),
            ).fetchone()
        if row is None:
            return {"revision": 0}
        revision, payload = row
        return {"revision": revision} if payload is None else {"revision": revision, "value": json.loads(payload)}

    @router.put("/{area}/record")
    def write_record(area: Area, key: Key, request: OfflineWrite):
        check_key(area, key)
        try:
            payload = None if request.value is None else json.dumps(request.value, ensure_ascii=False, allow_nan=False)
        except ValueError as error:
            raise HTTPException(422, "本机离线记录格式无效。") from error
        with closing(sqlite3.connect(file)) as db, db:
            db.execute("PRAGMA synchronous = FULL")
            if request.revision == 0:
                saved = db.execute("""
                    INSERT INTO offline_study (area, key, revision, payload) VALUES (?, ?, 1, ?)
                    ON CONFLICT (area, key) DO NOTHING
                """, (area, key, payload))
            else:
                saved = db.execute("""
                    UPDATE offline_study SET revision = revision + 1, payload = ?
                    WHERE area = ? AND key = ? AND revision = ?
                """, (payload, area, key, request.revision))
        return {"saved": saved.rowcount == 1}

    return router
