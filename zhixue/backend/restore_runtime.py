from collections.abc import Callable
from errno import EDQUOT, ENOSPC
import sqlite3
from threading import Condition, Thread
from uuid import uuid4

from fastapi.responses import JSONResponse

from .backup_format import BackupError


STATUS_PATH = "/api/backups/restore-status"


class RestoreRuntime:
    def __init__(self, pause_worker: Callable, resume_worker: Callable, initial: dict | None = None):
        self.condition = Condition()
        self.pause_worker = pause_worker
        self.resume_worker = resume_worker
        self.receipt = initial or {"state": "idle"}
        self.blocked = False
        self.requests = 0
        self.thread: Thread | None = None
        self.closing = False

    def status(self) -> dict:
        with self.condition:
            return self.receipt.copy()

    def enter(self) -> bool:
        with self.condition:
            if self.blocked:
                return False
            self.requests += 1
            return True

    def leave(self) -> None:
        with self.condition:
            self.requests -= 1
            if not self.requests:
                self.condition.notify_all()

    def submit(self, backup_id: str, apply: Callable) -> dict:
        with self.condition:
            if self.blocked or self.closing:
                raise BackupError("busy", "整体恢复正在处理，请等待状态更新。")
            identity = uuid4().hex
            self.receipt = {"id": identity, "backupId": backup_id, "state": "running", "step": "quiescing"}
            self.blocked = True
            self.thread = Thread(target=self.run, args=(identity, backup_id, apply), name="full-restore")
            self.thread.start()
            return self.receipt.copy()

    def progress(self, step: str) -> None:
        with self.condition:
            self.receipt["step"] = step

    def run(self, identity: str, backup_id: str, apply: Callable) -> None:
        paused = False
        result = {"id": identity, "backupId": backup_id}
        try:
            with self.condition:
                if not self.condition.wait_for(lambda: self.requests == 0, timeout=30):
                    raise BackupError("busy", "仍有学习数据正在读取或保存。请重新预览后再恢复。")
            self.pause_worker()
            paused = True
            result.update(apply(identity, self.progress))
        except BackupError as failure:
            state = "recovery_required" if failure.code == "recovery_required" else "failed"
            result.update(state=state, code=failure.code, detail=str(failure))
        except Exception as failure:
            origin = getattr(failure, "orig", failure)
            space = (getattr(origin, "errno", None) in {ENOSPC, EDQUOT}
                     or getattr(origin, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL)
            detail = "保存空间不足。当前学习数据已保留。" if space else "恢复未完成。请检查本机目录和备份文件后重新预览。"
            result.update(state="failed", code="space" if space else "restore_write", detail=detail)
        finally:
            if paused and result.get("state") != "recovery_required" and not self.closing:
                try:
                    self.resume_worker()
                except OSError:
                    result["workerIssue"] = "后台进程暂未启动，请重新打开智学。"
            with self.condition:
                self.receipt = result
                self.blocked = result.get("state") == "recovery_required"
                self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closing = True
            worker = self.thread
        if worker:
            worker.join()


class RestoreGate:
    def __init__(self, app, runtime: RestoreRuntime):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        always_readable = scope.get("method") == "GET" and path in (STATUS_PATH, "/api/setup")
        if scope["type"] != "http" or not path.startswith("/api/") or always_readable:
            await self.app(scope, receive, send)
            return
        if not self.runtime.enter():
            response = JSONResponse({"detail": "整体恢复期间学习数据暂不可访问。", "code": "restore_in_progress"},
                                    status_code=503, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.runtime.leave()


def attach_restore(app, runtime: RestoreRuntime) -> None:
    app.state.restore_runtime = runtime
    app.add_middleware(RestoreGate, runtime=runtime)
