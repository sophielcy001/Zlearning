from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException

from .course_review import same_model
from .db import ModelConnection
from .model_study_input import Input, backup_candidate, connection_view, main_connection, pinned_connection, usable_connection


TRANSIENT = {"network", "timeout", "rate_limited", "service_unavailable"}
FAILOVER_FAILURES = TRANSIENT | {
    "missing_key", "credential_store", "unauthorized", "quota", "redirect", "request_rejected",
}
Role = Literal["main", "review"]
BACKUP_ROLES = {"main": "mainBackup", "review": "reviewBackup"}
ROLE_LABELS = {"main": "主模型", "review": "复核模型"}


class BackupChoices(Input):
    allowMainBackup: bool = False
    allowReviewBackup: bool = False


@dataclass(frozen=True)
class RoleScope:
    role: Role
    primary: dict | None
    backup: dict | None
    author: dict | None = None


@dataclass(frozen=True)
class Recipient:
    connection: ModelConnection
    failover_reason: str | None = None


def backup_options(db, main: ModelConnection | None, review: ModelConnection | None, *,
                   allow_main: bool, allow_review: bool = False) -> dict:
    backups = {}
    for role, primary, allowed in (("main", main, allow_main), ("review", review, allow_review)):
        if not allowed:
            continue
        alternate = backup_candidate(db, BACKUP_ROLES[role], primary)
        if alternate is None or (role == "review" and main and same_model(connection_view(main), connection_view(alternate))):
            label = ROLE_LABELS[role]
            raise HTTPException(409, f"请在管理中心配置另一条可用于{label}的备用连接。")
        backups[role] = connection_view(alternate)
    return {"backups": backups} if backups else {}


def distinct_reviewer(db, scope: RoleScope, connection: ModelConnection) -> bool:
    if scope.role != "review":
        return True
    model = connection_view(connection)
    main = main_connection(db)
    return ((main is None or not same_model(connection_view(main), model))
            and (scope.author is None or not same_model(scope.author, model)))


def approved_backup(db, scope: RoleScope) -> ModelConnection | None:
    alternate = pinned_connection(db, BACKUP_ROLES[scope.role], scope.backup)
    if (not usable_connection(alternate) or scope.primary is None or alternate.id == scope.primary["id"]
            or not distinct_reviewer(db, scope, alternate)):
        return None
    return alternate


def active_failure(scope: RoleScope, failovers: dict | None) -> str | None:
    saved = (failovers or {}).get(scope.role)
    if saved and saved["primary"] == scope.primary and saved["backup"] == scope.backup:
        return saved["reason"]
    return None


def choose_recipient(db, scope: RoleScope, failovers: dict | None) -> Recipient | None:
    primary = pinned_connection(db, scope.role, scope.primary)
    if not usable_connection(primary):
        return None
    failure = active_failure(scope, failovers)
    if failure is None and distinct_reviewer(db, scope, primary):
        return Recipient(primary)
    alternate = approved_backup(db, scope)
    return Recipient(alternate, failure or "review_independence") if alternate else None


def switch_after_failure(db, scope: RoleScope, job, call, status: str, retries: int) -> bool:
    if (retries >= 2 or status not in FAILOVER_FAILURES or call.failover_reason is not None or call.connection != scope.primary
            or pinned_connection(db, scope.role, scope.primary) is None or approved_backup(db, scope) is None):
        return False
    job.failovers = {**(job.failovers or {}), scope.role: {"primary": scope.primary, "backup": scope.backup, "reason": status}}
    return True


def public_failovers(failovers: dict | None, errors: dict) -> list[dict]:
    return [{"role": role, "connection": value["backup"], "reason": value["reason"], "message": errors.get(value["reason"])}
            for role, value in (failovers or {}).items()]
