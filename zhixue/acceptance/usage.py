from collections import Counter
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile

from sqlalchemy import and_, create_engine, select

from backend.db import ModelCharge, ModelRole, ModelUse, transaction
from backend.model_ledger import charge_view, sum_usage, utc_clock

from .report_files import replace_report


ROLES = {"main": "主模型", "review": "独立复核模型"}


def usage_after_stop(usage: dict) -> dict:
    result = {**usage}
    unknown, pending = usage.get("费用未知的调用"), usage.get("待结算调用")
    if type(unknown) is int and type(pending) is int:
        result.update({"费用未知的调用": unknown + pending, "待结算调用": 0})
    result["调用记录"] = [
        {**call, "状态": "uncertain"} if isinstance(call, dict) and call.get("状态") == "sending" else call
        for call in usage["调用记录"]
    ]
    return result


def read_usage(database: Path, *, final=False) -> dict:
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(database.as_uri() + "?mode=ro", uri=True))
    try:
        with transaction(engine, write=False) as db:
            roles = {identity: ROLES[role] for role, identity in db.execute(
                select(ModelRole.role, ModelRole.connection_id).where(ModelRole.role.in_(ROLES))
            )}
            unsent = Counter()
            rows = []
            charges = ModelCharge.source == ModelUse.__tablename__
            for use, charge in db.execute(select(ModelUse, ModelCharge).outerjoin(
                ModelCharge, and_(charges, ModelCharge.call_id == ModelUse.id),
            ).order_by(ModelUse.started_at, ModelUse.id)):
                # Keep the transport's durable decision even if the Worker has not finished the task.
                status = "not_sent" if charge is not None and charge.status == "not_sent" else use.status
                if status in {"not_sent", "reserved"}:
                    unsent[status] += 1
                else:
                    rows.append((use, charge))
            summary = sum_usage(db, charges, ModelCharge.call_id.in_([use.id for use, _ in rows]))
            calls = []
            for use, charge in rows:
                amount = charge_view(charge) if charge is not None else {}
                input_tokens = use.input_tokens if use.input_tokens is not None else amount.get("inputTokens")
                output_tokens = use.output_tokens if use.output_tokens is not None else amount.get("outputTokens")
                calls.append({
                    "阶段": use.phase, "模型分工": roles.get(use.connection.get("id")), "状态": use.status,
                    "账本开始时间（UTC）": amount["startedAt"] if charge is not None else utc_clock(use.started_at).isoformat() + "Z",
                    "结算记录时间（UTC）": amount.get("finishedAt"),
                    "输入用量": input_tokens, "输出用量": output_tokens,
                    "服务商回报费用": amount.get("actualCost"), "估算费用": amount.get("estimatedCost"),
                })
            unknown = summary["unknownCalls"] + len(rows) - summary["calls"]
            result = {
                "计入调用": len(rows),
                "发送前取消或拦截": unsent.get("not_sent", 0),
                "尚未发送的预留": unsent.get("reserved", 0),
                "收到用量的调用": sum(call["输入用量"] is not None or call["输出用量"] is not None for call in calls),
                "按币种汇总": summary["currencies"],
                "费用未知的调用": unknown,
                "待结算调用": summary["inFlight"],
                "调用记录": calls,
            }
            return usage_after_stop(result) if final else result
    finally:
        engine.dispose()


def write_usage_checkpoint(path: Path, database: Path, *, final=False) -> None:
    output = read_usage(database, final=final)
    checkpoint = {
        "run": path.stem, "capturedAt": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "serviceStopped": final, "output": output,
    }
    with NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                            mode="w", encoding="utf-8", delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(checkpoint, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            replace_report(temporary, path)
        finally:
            file.close()
            temporary.unlink(missing_ok=True)


def read_usage_checkpoint(path: Path) -> dict | None:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(checkpoint, dict) or checkpoint.get("run") != path.stem
            or not isinstance(checkpoint.get("capturedAt"), str)
            or type(checkpoint.get("serviceStopped")) is not bool
            or not isinstance(checkpoint.get("output"), dict)):
        return None
    output = checkpoint["output"]
    count, calls = output.get("计入调用"), output.get("调用记录")
    if type(count) is not int or not isinstance(calls, list) or count != len(calls):
        return None
    return checkpoint
