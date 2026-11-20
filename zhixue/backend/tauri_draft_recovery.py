import argparse
from collections import defaultdict
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, ValidationError

from .backup_format import BackupError
from .backup_restore import exclusive_lease, restore_root
from .backup_snapshot import DATABASE
from .storage_files import sqlite_file_uri
from .workspace_setup import SERVICE_LOCK, WorkspaceSetup


class LegacyOrigin(BaseModel):
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    records: list[dict[str, JsonValue]]


class LegacyDraftExport(BaseModel):
    format: Literal["zhixue-tauri-drafts-v1"]
    origins: list[LegacyOrigin]


def canonical(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def draft_workspace(record: dict) -> str | None:
    kind = record.get("kind")
    snapshot = record.get("snapshot")
    if kind not in ("lesson", "review") or not isinstance(snapshot, dict):
        raise ValueError("导出文件含有无法识别的离线记录。")
    workspace = snapshot.get("workspace")
    identity = snapshot.get("id")
    if not all(isinstance(value, str) and value for value in (workspace, identity, snapshot.get("title"))):
        raise ValueError("导出记录缺少工作区、标题或编号。")
    if record.get("key") != f"{workspace}:{kind}:{identity}":
        raise ValueError("导出记录的编号与内容不符。")

    if kind == "lesson":
        note = record.get("note")
        if not isinstance(note, dict) or not note.get("editedAt") or note.get("pending") or note.get("conflict"):
            return None
        if not isinstance(note.get("content"), str) or not isinstance(snapshot.get("body"), str):
            raise ValueError("导出笔记或课程正文格式不正确。")
        return workspace

    if record.get("submission"):
        return None
    questions, answers = snapshot.get("questions"), record.get("draft")
    if not isinstance(questions, list) or not isinstance(answers, list) or len(questions) != len(answers):
        raise ValueError("导出题组与草稿答案长度不符。")
    for question, answer in zip(questions, answers, strict=True):
        if not isinstance(question, dict) or not isinstance(question.get("text"), str):
            raise ValueError("导出文件中的题目格式不正确。")
        options = question.get("options")
        if options is not None and (not isinstance(options, list) or not all(isinstance(option, str) for option in options)):
            raise ValueError("导出文件中的题目选项格式不正确。")
        if answer is not None and not (
            (options is None and isinstance(answer, str)) or
            (isinstance(options, list) and type(answer) is int and 0 <= answer < len(options))
        ):
            raise ValueError("导出文件中的答案格式不正确。")
    return workspace if any(answer is not None and answer != "" for answer in answers) else None


def variants_for(export: LegacyDraftExport, workspace: str) -> tuple[dict, dict]:
    variants = defaultdict(dict)
    skipped = {"otherWorkspace": 0, "notDraft": 0}
    ports = set()
    for origin in export.origins:
        if origin.port in ports:
            raise ValueError("导出文件含有重复的端口。")
        ports.add(origin.port)
        keys = set()
        for record in origin.records:
            source = draft_workspace(record)
            key = record["key"]
            if key in keys:
                raise ValueError("同一端口含有重复的记录编号。")
            keys.add(key)
            if source != workspace:
                reason = "notDraft" if source is None else "otherWorkspace"
                skipped[reason] += 1
                continue
            variants[key].setdefault(canonical(record), []).append(origin.port)
    return variants, skipped


def selected_versions(variants: dict, choices: dict[str, int]) -> dict:
    chosen = {}
    for key, port in choices.items():
        chosen[key] = next((value for value, origins in variants.get(key, {}).items() if port in origins), None)
        if chosen[key] is None:
            raise ValueError(f"所选端口不包含这份草稿（{key}）。")
    return chosen


def verified_file(path: Path) -> Path:
    if path.resolve() != path or not path.is_file():
        raise ValueError(f"无法读取已初始化的智学数据库（{path.name}）。")
    return path


def recover_drafts(data: Path, bundle: dict, *, apply: bool = False, choices: dict[str, int] | None = None) -> dict:
    export = LegacyDraftExport.model_validate(bundle)
    control = data.resolve(strict=True)
    if not control.is_dir():
        raise ValueError("请指定新版 Tauri 应用的数据目录。")

    with ExitStack() as stack:
        if apply:
            stack.enter_context(exclusive_lease(control / ".zhixue-launch.sqlite3"))
        selection = WorkspaceSetup(control).selection
        if selection is None or not selection.complete:
            raise ValueError("请先在新版 Tauri 完成知识库设置。")
        if apply:
            stack.enter_context(exclusive_lease(restore_root(selection.folder) / SERVICE_LOCK))
        knowledge = verified_file(selection.folder / DATABASE)
        offline = verified_file(control / "offline-study.sqlite3")
        with closing(sqlite3.connect(sqlite_file_uri(knowledge, mode="ro"), uri=True)) as source:
            row = source.execute("SELECT identity FROM mobile_workspace WHERE singleton = 1").fetchone()
        if row is None:
            raise ValueError("当前知识库缺少离线工作区编号。")

        variants, skipped = variants_for(export, row[0])
        chosen = selected_versions(variants, choices or {})
        report = {"workspace": row[0], "ready": [], "conflicts": [], "keptCurrent": [],
                  "alreadyPresent": [], "imported": [], "skipped": skipped}
        mode = "rw" if apply else "ro"
        with closing(sqlite3.connect(sqlite_file_uri(offline, mode=mode), uri=True)) as db, db:
            if apply:
                db.execute("PRAGMA synchronous = FULL")
                db.execute("BEGIN IMMEDIATE")
            for key, values in sorted(variants.items()):
                picked = chosen.get(key)
                existing = db.execute(
                    "SELECT payload FROM offline_study WHERE area = 'items' AND key = ?", (key,),
                ).fetchone()
                if existing is not None:
                    current = canonical(json.loads(existing[0])) if existing[0] is not None else None
                    same = current == picked if picked is not None else current in values
                    report["alreadyPresent" if same else "keptCurrent"].append(key)
                    continue
                if picked is None and len(values) > 1:
                    report["conflicts"].append({"key": key, "variants": sorted(sorted(ports) for ports in values.values())})
                    continue
                payload = picked if picked is not None else next(iter(values))
                if apply:
                    db.execute("INSERT INTO offline_study VALUES ('items', ?, 1, ?)", (key, payload))
                    report["imported"].append(key)
                else:
                    report["ready"].append(key)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="预览或迁回 Windows Tauri 旧版离线笔记和复习草稿")
    parser.add_argument("--data", type=Path, required=True, help="新版 Tauri 应用数据目录")
    parser.add_argument("--export", type=Path, required=True, help="导出的历史草稿 JSON")
    parser.add_argument("--choose", action="append", default=[], metavar="编号=端口", help="选择一个有冲突的旧版草稿")
    parser.add_argument("--apply", action="store_true", help="写入可恢复的草稿")
    args = parser.parse_args()

    try:
        selections = {}
        for choice in args.choose:
            key, port = choice.rsplit("=", 1)
            if key in selections:
                raise ValueError("每份草稿只能指定一个端口。")
            selections[key] = int(port)
        bundle = json.loads(args.export.read_text(encoding="utf-8-sig"))
        result = recover_drafts(args.data, bundle, apply=args.apply, choices=selections)
    except ValidationError:
        parser.exit(1, "历史草稿导出格式不正确。\n")
    except (OSError, ValueError, BackupError, sqlite3.Error) as error:
        parser.exit(1, f"无法迁回草稿。{error}\n")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
