from contextlib import contextmanager
from http.client import HTTPException
import json
import os
from pathlib import Path
from queue import Empty, Queue
from shutil import copy2, copytree, ignore_patterns
import subprocess
import sys
from threading import Thread
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from backend.db import ROOT
from .steps import CheckFailed, check


def until(operation, reason: str, timeout: float = 16):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if result := operation():
            return result
        sleep(.08)
    raise CheckFailed(reason)


class ProductConnectionError(CheckFailed):
    """The isolated service did not establish or maintain its local connection."""


class Product:
    def __init__(self, base_url: str, token: str, *, child=None, check_running=None):
        self.base_url = base_url
        self.token = token
        self.child = child
        self.check_running = check_running
        self.opener = build_opener(ProxyHandler({}))

    def request(self, method: str, path: str, data=None, *, expected=None, raw=False):
        if self.check_running is not None:
            self.check_running()
        if isinstance(data, bytes):
            payload, content_type = data, "application/octet-stream"
        else:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
            content_type = "application/json"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": content_type}
        request = Request(self.base_url + path, payload, headers, method=method)
        try:
            with self.opener.open(request, timeout=9) as response:
                status, body = response.status, response.read()
        except HTTPError as error:
            with error:
                status, body = error.code, error.read()
        except (OSError, HTTPException) as error:
            if self.check_running is not None:
                self.check_running()
            raise ProductConnectionError("与隔离学习服务的本机连接中断或超时。") from error
        if self.check_running is not None:
            self.check_running()
        check(status == expected if expected is not None else 200 <= status < 300,
              f"{method} {urlsplit(path).path} 返回 HTTP {status}。")
        return body if raw else json.loads(body)

    def get(self, path, *, expected=None, raw=False):
        return self.request("GET", path, expected=expected, raw=raw)

    def post(self, path, data=None, *, expected=None):
        return self.request("POST", path, data, expected=expected)

    def put(self, path, data, *, expected=None):
        return self.request("PUT", path, data, expected=expected)

    def authorize(self, path, data):
        preview = self.post(f"{path}/preview", data)
        self.post(f"{path}/authorize", {**data, "confirmation": preview["confirmation"]})
        return preview


def copy_runtime_file(source, target):
    # A clone keeps large bundled models isolated without consuming their full size again.
    if sys.platform == "darwin" and Path(source).stat().st_size >= 16 * 1024 * 1024:
        result = subprocess.run(["/bin/cp", "-c", "-p", source, target], capture_output=True, text=True)
        if result.returncode:
            raise OSError(result.stderr.strip())
        return target
    return copy2(source, target)


def prepare_product(runtime: Path) -> None:
    # Keep repeated imports from the API and Worker on the host's temporary filesystem.
    try:
        for directory in ("backend", "migrations"):
            copytree(ROOT / directory, runtime / directory, ignore=ignore_patterns("__pycache__"),
                     copy_function=copy_runtime_file)
        build = ROOT / "dist"
        try:
            (runtime / "dist").symlink_to(build, target_is_directory=True)
        except OSError:
            # Windows can restrict creation of directory symlinks.
            copytree(build, runtime / "dist")
    except OSError as error:
        raise CheckFailed("无法在本机临时目录准备正式服务文件。") from error


@contextmanager
def start_product(
    runtime: Path, folder: Path, *, entry: tuple[str, ...] = ("backend.launch",),
    exit_reasons: dict[int, str] | None = None, check_running=None,
):
    # Force the synthetic worker's HTTP calls to stay local even with proxy settings on the host.
    environment = {**os.environ, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    with subprocess.Popen(
        [sys.executable, "-m", *entry, "--stdio", "--port", "0", "--data", str(folder)],
        cwd=runtime, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=environment,
    ) as child:
        completed = connection_failed = False
        reasons = exit_reasons or {}
        try:
            ready = Queue(maxsize=1)
            reader = Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
            reader.start()
            try:
                address = urlsplit(ready.get(timeout=60).strip())
            except Empty as error:
                raise ProductConnectionError("正式学习服务未在时限内启动。") from error
            tokens = parse_qs(urlsplit(address.fragment).query).get("session", [])
            if address.scheme != "http" or address.hostname != "127.0.0.1" or len(tokens) != 1:
                raise ProductConnectionError("正式学习服务没有返回有效的本机地址。")
            yield Product(f"{address.scheme}://{address.netloc}", tokens[0], child=child, check_running=check_running)
            completed = True
        except ProductConnectionError:
            connection_failed = True
            raise
        finally:
            child.stdin.close()
            try:
                status = child.wait(timeout=12)
            except subprocess.TimeoutExpired as error:
                child.kill()
                child.wait()
                if completed:
                    raise CheckFailed("结束隔离学习服务时超时。") from error
            else:
                if status and (completed or (connection_failed and status in reasons)):
                    raise CheckFailed(reasons.get(status, "隔离学习服务异常结束。"))


def setup_workspace(api):
    state = api.get("/api/setup")
    check(state["stage"] == "storage", "隔离服务没有进入空白环境引导。")
    api.put("/api/setup/storage", {"directory": None}, expected=202)

    def models_ready():
        try:
            return api.get("/api/setup")["stage"] == "models"
        except CheckFailed:
            # The launcher opens the workspace on the same bound port after closing the setup app.
            return False

    until(models_ready, "正式服务没有切换到模型配置步骤。")
    check(api.post("/api/setup/complete")["stage"] == "complete", "空白环境引导未完成。")


def goal_project(api, topic: str, call_limit: int, *, outcome: str):
    thread_id = str(uuid4())
    api.post("/api/conversations", {
        "id": thread_id, "message": {"id": str(uuid4()), "text": f"我想学习{topic}"},
    })
    path = f"/api/conversations/{thread_id}/goal"
    draft = api.post(path)
    fields = {"goalId": draft["id"], "revision": draft["revision"], "values": {"topic": topic, "outcome": outcome}}
    preview = api.post(f"{path}/model-preview", {**fields, "maxCalls": call_limit})
    submission = {**fields, "modelConsent": preview["consent"]}
    result = api.post(f"{path}/submit", submission)
    repeated = api.post(f"{path}/submit", submission)
    check(result["projectId"] == repeated["projectId"], "重复提交生成了不同项目。")
    return result["projectId"], preview


def restore_backup(api, archive, password):
    preview = api.post(f"{archive}/preview", {"password": password})
    api.post(f"{archive}/restore", {
        "password": password, "confirmation": preview["restore"]["confirmation"],
    }, expected=202)

    def restored():
        status = api.get("/api/backups/restore-status")
        return status if status["state"] != "running" else None

    result = until(restored, "整库恢复没有在时限内完成。", 30)
    check(result["state"] == "complete", "整库恢复未完成。")
    return result
