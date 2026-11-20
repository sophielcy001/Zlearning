import json
import subprocess
import sys
from time import monotonic, sleep
from urllib.parse import urlsplit

import httpx2
import pytest


def test_worker_reports_its_original_failure_with_stdin_still_open(tmp_path):
    lease = tmp_path / ".worker-lease.sqlite3"
    original = b"invalid SQLite file for this isolated subprocess\n"
    lease.write_bytes(original)
    with subprocess.Popen(
        [sys.executable, "-m", "backend.worker", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    ) as child:
        try:
            code = child.wait(timeout=20)
            assert not child.stdin.closed
            diagnostics = child.stderr.read()
        finally:
            if child.poll() is None:
                child.kill()
    assert lease.read_bytes() == original
    assert "sqlite3.DatabaseError: file is not a database" in diagnostics
    assert code == 1, diagnostics


SINGLE_REQUEST_SERVER = """
import uvicorn

class SingleRequestServer(uvicorn.Server):
    def __init__(self, config):
        config.limit_max_requests = 1
        super().__init__(config)

uvicorn.Server = SingleRequestServer
from acceptance.__main__ import main
raise SystemExit(main())
"""


@pytest.mark.parametrize("stop_after_request", [True, False], ids=["server-shutdown", "parent-disconnect"])
def test_acceptance_cli_exits_cleanly(tmp_path, stop_after_request):
    entry = ["-c", SINGLE_REQUEST_SERVER] if stop_after_request else ["-m", "acceptance"]
    output = tmp_path / "address.txt"
    with output.open("wb") as stdout, subprocess.Popen(
        [sys.executable, *entry, "--stdio", "--reports", str(tmp_path / "history")],
        stdin=subprocess.PIPE, stdout=stdout, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    ) as child:
        try:
            deadline = monotonic() + 15
            announced = ""
            while monotonic() < deadline and child.poll() is None:
                announced = output.read_text(encoding="utf-8").strip()
                if announced:
                    break
                sleep(.05)
            assert announced.startswith("http://127.0.0.1:")
            url = urlsplit(announced)
            with httpx2.Client(trust_env=False, timeout=5) as http:
                assert http.get(f"http://127.0.0.1:{url.port}/").status_code == 200
            if not stop_after_request:
                child.stdin.close()
            code = child.wait(timeout=15)
            assert child.stdin.closed == (not stop_after_request)
            diagnostics = child.stderr.read()
        finally:
            if child.poll() is None:
                child.kill()
    assert code == 0, diagnostics


def test_model_sample_service_can_stop_after_worker_failure_with_stdin_open(tmp_path):
    checkpoint = tmp_path / "runtime.usage"
    lease = tmp_path / ".worker-lease.sqlite3"
    original = b"invalid SQLite file for this isolated subprocess\n"
    lease.write_bytes(original)
    with subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "acceptance.live_runtime", "--stdio",
         "--data", str(tmp_path), "--limit", "0", "--usage-checkpoint", str(checkpoint)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    ) as child:
        try:
            code = child.wait(timeout=20)
            assert not child.stdin.closed
            announced = child.stdout.read()
            diagnostics = child.stderr.read()
        finally:
            if child.poll() is None:
                child.kill()
    assert announced.startswith("http://127.0.0.1:")
    assert lease.read_bytes() == original
    assert code == 21, diagnostics
    assert "隔离模型 Worker 运行失败" in diagnostics
    assert "_enter_buffered_busy" not in diagnostics
    usage = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert usage["serviceStopped"] is True and usage["output"]["计入调用"] == 0


WORKER_OPEN_FAILURE = """
from acceptance import live_runtime

def fail_to_open(*args, **kwargs):
    raise RuntimeError("private-worker-exception-data")

live_runtime.open_db = fail_to_open
raise SystemExit(live_runtime.main())
"""


def test_model_sample_stops_without_announcing_when_worker_cannot_open_database(tmp_path):
    with subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", WORKER_OPEN_FAILURE, "--stdio",
         "--data", str(tmp_path), "--limit", "0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    ) as child:
        try:
            code = child.wait(timeout=10)
            assert not child.stdin.closed
            announced = child.stdout.read()
            diagnostics = child.stderr.read()
        finally:
            if child.poll() is None:
                child.kill()
    assert code == 20, diagnostics
    assert not announced
    assert "隔离模型 Worker 初始化失败" in diagnostics
    assert "private-worker-exception-data" not in diagnostics
