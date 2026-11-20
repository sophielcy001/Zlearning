from contextlib import closing
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.background import BackgroundTask

from .backup_format import BackupError
from .backup_restore import RESTORE_FOLDER, exclusive_lease, restore_root, write_json
from .backup_snapshot import DATABASE
from .local_service import local_app, mount_frontend


SETTINGS_FILE = ".zhixue-workspace.json"
OWNER_FILE = ".zhixue-owner.json"
SERVICE_LOCK = "service-lease.sqlite3"


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class Selection:
    folder: Path
    complete: bool


def read_object(path: Path) -> dict:
    if path.resolve() != path or not path.is_file():
        raise ValueError("expected local setup file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid setup file")
    return value


def check_storage(folder: Path) -> None:
    with TemporaryDirectory(dir=folder, prefix=".zhixue-check-") as work:
        with closing(sqlite3.connect(Path(work) / "check.sqlite3")) as database, database:
            if database.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                raise WorkspaceError("这个文件夹无法使用 SQLite WAL，请选择支持本地数据库的目录。")
            database.execute("CREATE TABLE availability (value INTEGER)")


class WorkspaceSetup:
    def __init__(self, control: Path):
        self.control = control
        self.file = control / SETTINGS_FILE
        self.owner = sha256(os.fsencode(control)).hexdigest()
        self.lock = Lock()
        self.selection = self.load()

    def matches_owner(self, folder: Path) -> bool:
        try:
            return read_object(folder / OWNER_FILE).get("owner") == self.owner
        except (OSError, ValueError):
            return False

    def load(self) -> Selection | None:
        if not self.file.exists() and not self.file.is_symlink():
            known = (DATABASE, "library", "backups", "backup-catalog.sqlite3")
            if any((self.control / name).exists() for name in known) or any((self.control / RESTORE_FOLDER).glob("switch-*")):
                return Selection(self.control, complete=True)
            return None
        try:
            value = read_object(self.file)
            folder = Path(value["directory"])
            if not folder.is_absolute() or not isinstance(value["complete"], bool):
                raise ValueError("invalid setup data")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise WorkspaceError("无法读取上次选择的知识库位置，请检查智学启动目录中的设置文件。") from error
        if folder.resolve() != folder or not folder.is_dir() or not self.matches_owner(folder):
            raise WorkspaceError(f"上次选择的知识库位置暂不可用。请重新连接磁盘或确认目录仍在（{folder}）。")
        return Selection(folder, complete=value["complete"])

    def view(self, current: Path | None) -> dict:
        selected = self.selection
        if selected is None:
            folder, stage = self.control, "storage"
        elif current != selected.folder:
            folder, stage = selected.folder, "opening"
        else:
            folder, stage = selected.folder, "complete" if selected.complete else "models"
        return {"stage": stage, "directory": str(folder), "library": str(folder / "library")}

    def save(self, selection: Selection) -> None:
        write_json(self.file, {"directory": str(selection.folder), "complete": selection.complete})
        self.selection = selection

    def ensure_empty(self, folder: Path) -> None:
        if folder == self.control:
            return
        lock_names = {SERVICE_LOCK, f"{SERVICE_LOCK}-journal", f"{SERVICE_LOCK}-wal", f"{SERVICE_LOCK}-shm"}
        for entry in folder.iterdir():
            if entry.name == ".DS_Store" or entry.name == OWNER_FILE and self.matches_owner(folder):
                continue
            if (entry.name == RESTORE_FOLDER and entry.resolve() == entry and entry.is_dir()
                    and all(item.name in lock_names for item in entry.iterdir())):
                continue
            raise WorkspaceError("请选择一个新文件夹或空文件夹。已有数据的目录可以从原启动入口打开。")

    def choose(self, directory: str | None) -> None:
        with self.lock:
            if self.selection is not None:
                raise WorkspaceError("知识库位置已经选定，请稍后继续设置。")
            requested = Path(directory).expanduser() if directory is not None else self.control
            if not requested.is_absolute() or directory is not None and any(ord(char) < 32 for char in directory):
                raise WorkspaceError("请输入本机文件夹的绝对路径。")
            folder = requested.resolve()
            folder.mkdir(parents=True, exist_ok=True)
            self.ensure_empty(folder)
            work = restore_root(folder)
            with exclusive_lease(work / SERVICE_LOCK):
                self.ensure_empty(folder)
                check_storage(folder)
                marker = folder / OWNER_FILE
                if marker.exists() or marker.is_symlink():
                    if not self.matches_owner(folder):
                        raise WorkspaceError("这个文件夹已经分配给另一处安装，请选择其他位置。")
                else:
                    write_json(marker, {"owner": self.owner})
                self.save(Selection(folder, complete=False))

    def finish(self, current: Path | None) -> dict:
        with self.lock:
            if self.selection is None or current != self.selection.folder:
                raise WorkspaceError("请等知识库打开后再进入首页。")
            if not self.selection.complete:
                self.save(replace(self.selection, complete=True))
            return self.view(current)


class StorageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None


def setup_router() -> APIRouter:
    router = APIRouter(prefix="/api/setup")

    @router.get("")
    def setup_status(request: Request):
        setup = getattr(request.app.state, "workspace_setup", None)
        return setup.view(request.app.state.active_workspace) if setup else {"stage": "complete"}

    @router.put("/storage", status_code=202)
    def select_storage(payload: StorageInput, request: Request):
        opening = getattr(request.app.state, "open_workspace", None)
        if opening is None:
            raise HTTPException(409, "知识库已经打开。")
        setup = request.app.state.workspace_setup
        try:
            setup.choose(payload.directory)
        except (WorkspaceError, BackupError) as error:
            raise HTTPException(409, str(error)) from None
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            raise HTTPException(409, "无法在这个位置创建知识库，请检查目录权限和剩余空间。") from None
        return JSONResponse(setup.view(None), status_code=202, background=BackgroundTask(opening))

    @router.post("/complete")
    def finish_setup(request: Request):
        setup = getattr(request.app.state, "workspace_setup", None)
        if setup is None:
            return {"stage": "complete"}
        try:
            return setup.finish(request.app.state.active_workspace)
        except WorkspaceError as error:
            raise HTTPException(409, str(error)) from None
        except OSError:
            raise HTTPException(409, "无法保存引导进度，请检查启动目录的写入权限。") from None

    return router


def attach_workspace(app: FastAPI, setup: WorkspaceSetup, active: Path | None) -> None:
    app.state.workspace_setup = setup
    app.state.active_workspace = active


def setup_app(setup: WorkspaceSetup, session_token: str) -> FastAPI:
    app = local_app(session_token)
    attach_workspace(app, setup, None)
    app.include_router(setup_router())

    @app.get("/api/timer")
    def timer_before_workspace():
        return {"active": None, "stashed": [], "lastSaved": None}

    @app.api_route("/api/{route:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def workspace_required(route: str):
        raise HTTPException(409, "请先选择知识库位置。")

    mount_frontend(app)
    return app
