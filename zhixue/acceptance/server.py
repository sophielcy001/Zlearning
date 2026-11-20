from asyncio import to_thread
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
from threading import RLock, Thread
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.local_service import local_app
from backend.model_settings_api import ModelSettingsRoute
from .billing import billing_csv, billing_usage, connection_scope, scope_for_hosts
from .live_config import CHECK_LABELS, MAX_CALLS, LiveInput, QualityInput, SAMPLES, VERDICTS, live_definition
from .steps import BY_ID, STATE_LABELS, STEPS, CheckFailed
from .usage import read_usage_checkpoint


WEB = Path(__file__).with_name("web")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Run:
    def __init__(self, store, selection: LiveInput | None):
        self.store = store
        self.identity = str(uuid4())
        self.secrets = selection.secrets() if selection else ()
        self.data = {
            "id": self.identity, "mode": "simulated", "state": "running", "startedAt": now(),
            "finishedAt": None, "reason": None, "records": [],
        }
        if selection is not None:
            hosts = [urlsplit(profile.endpoint).hostname for _, profile in selection.connections()]
            self.data.update(mode="real", sample=selection.sample, connectionScope=scope_for_hosts(hosts))

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "〔已隐藏凭据〕")
            return value
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, dict):
            return {self.clean(key): self.clean(item) for key, item in value.items()}
        return value

    @contextmanager
    def step(self, step_id, inputs):
        if step_id not in BY_ID or any(item["step"] == step_id for item in self.data["records"]):
            raise ValueError("Unknown or already recorded acceptance step")
        start = monotonic()
        record = {
            "step": step_id, "status": "running", "input": self.clean(inputs), "output": {}, "reason": None,
            "startedAt": now(), "finishedAt": None, "elapsedMs": None,
        }
        self.data["records"].append(record)
        self.store.save(self)
        try:
            yield record["output"]
        except Exception as error:
            record["status"] = "failed"
            record["reason"] = self.clean(safe_reason(error))
            raise
        else:
            record["status"] = "passed"
        finally:
            record["output"] = self.clean(record["output"])
            record["finishedAt"] = now()
            record["elapsedMs"] = round(1000 * (monotonic() - start))
            self.store.save(self)


def safe_reason(error: Exception) -> str:
    return str(error) if isinstance(error, CheckFailed) else "验收程序遇到异常，当前节点未通过。"


def execute_simulation(run):
    from .scenarios import execute

    execute(run)


def execute_live_sample(run, selection):
    from .live_scenario import execute

    execute(run, selection)


class RunStore:
    def __init__(self, directory: Path, execute=execute_simulation, execute_live=execute_live_sample):
        self.directory = directory
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.execute = execute
        self.execute_live = execute_live
        self.lock = RLock()
        self.active = None
        self.thread = None

    def path(self, identity) -> Path:
        return self.directory / (str(UUID(str(identity))) + ".json")

    def usage_path(self, identity) -> Path:
        return self.path(identity).with_suffix(".usage")

    def write(self, path: Path, data: dict) -> None:
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, run: Run) -> None:
        with self.lock:
            self.write(self.path(run.identity), run.data)

    def start(self, selection: LiveInput | None = None) -> str:
        with self.lock:
            if self.active is not None:
                raise HTTPException(409, "已有验收运行正在执行。")
            run = Run(self, selection)
            self.save(run)
            self.active = run.identity
            self.thread = Thread(target=self._finish, args=(run, selection), name="acceptance-run")
            self.thread.start()
            return run.identity

    def _finish(self, run: Run, selection: LiveInput | None) -> None:
        try:
            if selection is None:
                self.execute(run)
            else:
                self.execute_live(run, selection)
            run.data["state"] = "passed"
        except Exception as error:
            run.data.update(state="failed", reason=run.clean(safe_reason(error)))
        finally:
            run.secrets = ()
            with self.lock:
                try:
                    run.data["finishedAt"] = now()
                    self.save(run)
                    if any(record["step"] == "real-usage" and record["status"] == "passed"
                           for record in run.data["records"]):
                        with suppress(OSError):
                            self.usage_path(run.identity).unlink(missing_ok=True)
                finally:
                    self.active = None

    def wait(self) -> None:
        if self.thread is not None:
            self.thread.join()

    def read(self, identity, *, include_checkpoint=True) -> dict:
        with self.lock:
            path = self.path(identity)
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(saved, dict) or saved.get("id") != path.stem or saved.get("mode") not in {"simulated", "real"}
                    or saved.get("state") not in {"running", "passed", "failed", "interrupted"}
                    or not isinstance(saved.get("records"), list)):
                raise ValueError("Invalid acceptance record")
            if saved["state"] == "running" and saved["id"] != self.active:
                reason = "上一次验收程序已退出，保留退出前已经记录的阶段。"
                saved.update(state="interrupted", reason=reason)
                for record in saved["records"]:
                    if record["status"] == "running":
                        record.update(status="interrupted", reason=reason)
            if include_checkpoint and saved["mode"] == "real" and saved["state"] in {"interrupted", "failed"}:
                self._add_usage_checkpoint(saved)
            return saved

    def _add_usage_checkpoint(self, saved):
        record = next((item for item in saved["records"] if item["step"] == "real-usage"), None)
        if record is not None and (record["status"] == "passed" or record["output"]):
            return
        checkpoint = read_usage_checkpoint(self.usage_path(saved["id"]))
        if checkpoint is None:
            return
        if record is None:
            record = {
                "step": "real-usage", "status": "interrupted", "input": {"模型调用上限": MAX_CALLS},
                "startedAt": checkpoint["capturedAt"], "finishedAt": None, "elapsedMs": None,
                "reason": "此节点依据最近保存的用量快照。",
            }
            saved["records"].append(record)
        record["output"] = {
            "证据范围": "最后一次落盘的用量快照，可能早于中断时刻。",
            "采集时间": checkpoint["capturedAt"], "采集时隔离服务已停止": checkpoint["serviceStopped"],
            **checkpoint["output"],
        }

    def assess(self, identity, checks: dict) -> dict:
        with self.lock:
            saved = self.read(identity, include_checkpoint=False)
            publication = next((entry for entry in reversed(saved["records"])
                                if entry["step"] in {"real-publish", "real-save"} and entry["status"] == "passed"
                                and entry["output"].get("正文")), None)
            if saved["mode"] != "real" or saved["state"] == "running" or publication is None:
                raise HTTPException(409, "已发布样本结束运行后可记录人工抽查。")
            saved["quality"] = {"checkedAt": now(), "checks": checks,
                                "courseVersion": publication["output"].get("课程版本")}
            self.write(self.path(identity), saved)
            return saved["quality"]

    def list(self) -> list[dict]:
        runs = []
        for path in self.directory.glob("*.json"):
            try:
                run = self.read(path.stem, include_checkpoint=False)
                record = {key: run[key] for key in ("id", "mode", "state", "startedAt", "finishedAt")}
                if run["mode"] == "real":
                    record["sample"] = run["sample"]
            except (ValueError, OSError, KeyError, TypeError):
                continue
            runs.append(record)
        return sorted(runs, key=lambda run: (run["startedAt"], run["id"]), reverse=True)


def report_text(run: dict) -> str:
    live = run["mode"] == "real"
    mode = ("模型接口联调。实际服务商及费用以账单核验。人工抽查单独记录。"
            if live else "本机模拟接口。通过表示产品流程满足本次合成用例的断言。")
    lines = [
        "# 智学独立验收报告", "", f"- 运行 ID {run['id']}",
        f"- 模型模式为{mode}",
        f"- 结论 {STATE_LABELS[run['state']]}",
        f"- 开始时间 {run['startedAt']}",
        f"- 结束时间 {run['finishedAt'] or '尚未记录'}",
    ]
    if live:
        sample = SAMPLES[run["sample"]]
        lines.append(f"- 样本 {sample.title}")
        lines.append(f"- 接入范围 {connection_scope(run)}")
    if run["reason"]:
        lines.append(f"- 原因 {run['reason']}")
    lines.extend(("", "## 实际运行", ""))
    executed = set()
    for record in run["records"]:
        step = BY_ID.get(record["step"])
        executed.add(record["step"])
        duration = "未记录" if record["elapsedMs"] is None else f"{record['elapsedMs']} ms"
        lines.extend((
            f"### {step.title if step else record['step']}", "",
            f"- 状态 {STATE_LABELS[record['status']]}",
            f"- 开始时间 {record['startedAt']}",
            f"- 结束时间 {record['finishedAt'] or '尚未记录'}",
            f"- 耗时 {duration}",
        ))
        if record["reason"]:
            lines.append(f"- 原因 {record['reason']}")
        evidence = {"输入摘要": record["input"], "输出": record["output"]}
        lines.extend(("", "```json", json.dumps(evidence, ensure_ascii=False, indent=2), "```", ""))
    if live:
        lines.extend(("## 人工抽查", ""))
        if quality := run.get("quality"):
            lines.append(f"- 记录时间 {quality['checkedAt']}")
            if version := quality.get("courseVersion"):
                lines.append(f"- 抽查课程版本 {version}")
            for key, label in CHECK_LABELS.items():
                lines.append(f"- {label} · {VERDICTS[quality['checks'][key]]}。{sample.criteria[key]}")
        else:
            lines.append("人工抽查未完成。")
        lines.extend(("", f"[原始资料]({sample.source_url})", ""))
    lines.extend(("## 此次未验证", ""))
    lines.extend(f"- 未验证 · {step.title}。{step.design}" for step in STEPS if step.id not in executed)
    return "\n".join(lines) + "\n"


class RunInput(BaseModel):
    mode: Literal["simulated"]


def create_app(store: RunStore, token: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        yield
        await to_thread(store.wait)

    app = local_app(token, lifespan=lifespan)

    @app.get("/api/definition")
    def definition():
        return {"steps": [asdict(step) for step in STEPS], "live": live_definition()}

    @app.get("/api/runs")
    def list_runs():
        return store.list()

    @app.post("/api/runs", status_code=202)
    def begin_run(_payload: RunInput):
        return {"id": store.start()}

    def load(identity: UUID):
        try:
            return store.read(identity)
        except FileNotFoundError as error:
            raise HTTPException(404, "找不到这次运行。") from error
        except (ValueError, OSError) as error:
            raise HTTPException(409, "这次运行的记录无法读取。") from error

    @app.get("/api/runs/{identity}")
    def get_run(identity: UUID):
        return load(identity)

    @app.get("/api/runs/{identity}/billing")
    def export_billing(identity: UUID):
        run = load(identity)
        usage = billing_usage(run)
        if usage is None:
            raise HTTPException(409, "已结束的接口联调需保存调用记录后才能导出账单核对表。")
        return Response(
            billing_csv(run, usage), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="zhixue-acceptance-{identity}-billing.csv"'},
        )

    @app.get("/api/runs/{identity}/report")
    def export_report(identity: UUID):
        return Response(
            report_text(load(identity)), media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="zhixue-acceptance-{identity}.md"'},
        )

    live_routes = APIRouter(prefix="/api", route_class=ModelSettingsRoute)

    @live_routes.post("/real-runs", status_code=202)
    def begin_live_run(selection: LiveInput):
        return {"id": store.start(selection)}

    @live_routes.put("/runs/{identity}/quality")
    def record_quality(identity: UUID, payload: QualityInput):
        try:
            return store.assess(identity, payload.checks)
        except FileNotFoundError as error:
            raise HTTPException(404, "找不到这次运行。") from error
        except (ValueError, OSError) as error:
            raise HTTPException(409, "这次运行的记录无法保存。") from error

    app.include_router(live_routes)
    app.mount("/", StaticFiles(directory=WEB, html=True), name="acceptance")
    return app
