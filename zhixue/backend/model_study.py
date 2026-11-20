from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select

from .content import is_practice
from .course_review import ReviewReply, assess, independent_reviewer, review_prompt, same_model
from .course_revision import (
    active_revision, pinned_revision_model, replacement_sections, review_draft, revision_issue, revision_prompt,
    revision_review_current,
)
from .course_sources import sources_current
from .db import (
    CourseFinding, CourseReviewPlan, GenerationSettings, GenerationStep, Lesson, ModelCharge, ModelStudy,
    ModelUse, Project, RouteVersion, Task, transaction,
)
from .model_credentials import CREDENTIAL_FAILURES, CredentialStore, NativeCredentialStore
from .model_failover import FAILOVER_FAILURES, TRANSIENT
from .model_ledger import record_call_usage, request_model, sum_usage
from .model_protocols import TextReply, Transport, post_json
from .model_study_input import (
    DEFAULT_CONCURRENCY, connection_view, generation_goal_fields, main_connection, parse_model_json, parse_result,
    pinned_connection, prompt_for, usable_connection,
)
from .model_usage import (
    active_calls, admit_reserved_call, auto_resume_enabled, billed_calls, billing_day, daily_capacity,
    settle_interrupted_use,
)
from .money_budget import DAILY_BUDGET_REASONS, MONEY_ERRORS, held_issue, request_issue, task_limit
from .project_activity import study_moment, touch_project


ERRORS = {
    "restored": "从备份恢复，等待继续。",
    "configuration_changed": "主模型连接已变更，请重新确认接收模型。",
    "goal_changed": "学习目标已更新，请重新确认发送范围后继续生成。",
    "review_configuration_changed": "复核模型不可用或已变更，请检查模型分工并重新确认复核发送范围。",
    "review_independence": "备用复核模型与实际草稿作者独立。",
    "review_sources_changed": "复核资料已经变化，请重新选择定位并确认发送范围。",
    "revision_base_changed": "正式课程已经变化，请保留新版内容并重新发起修订。",
    "publication_failed": "课程文件暂时无法发布，请检查知识库位置、冲突与存储空间。",
    "missing_key": "请在管理中心配置主模型密钥。",
    "credential_store": "系统凭据库暂不可用，请解锁后继续。",
    "task_budget": "已达到该任务的调用次数上限。",
    "daily_budget": "已达到今日生成调用次数上限。",
    "network": "暂时无法连接模型服务。",
    "timeout": "模型服务请求超时。",
    "rate_limited": "模型服务限制了请求频率。",
    "service_unavailable": "模型服务暂不可用。",
    "unauthorized": "模型服务拒绝了身份验证或访问权限。",
    "quota": "服务商余额或额度不足。",
    "redirect": "模型地址发生跳转，请核对接口地址。",
    "request_rejected": "模型服务拒绝了请求，请核对地址和模型名称。",
    "invalid_response": "模型返回的内容未通过路线或草稿格式检查。",
    "no_text": "模型未返回可用文本。",
    "dispatch_failed": "后台任务在请求发送前发生异常，请重试。",
    "uncertain": "上次请求已进入发送阶段，任务结果尚未确认，请手动决定是否继续。",
    "project_paused": "请先恢复学习项目。",
    **MONEY_ERRORS,
}
COUNTED = ModelUse.status != "not_sent"
GENERATION_USE = and_(
    ModelUse.assessment_job_id.is_(None), ModelUse.question_id.is_(None), ModelUse.consolidation_id.is_(None),
)


def step_rows(db, project_id: str) -> list[GenerationStep]:
    return list(db.scalars(select(GenerationStep).where(GenerationStep.project_id == project_id)))


def focus_window(study: ModelStudy, lessons: list[Lesson]) -> tuple[list[str], list[str]]:
    order = [lesson.id for lesson in lessons if not is_practice(lesson.outline)]
    if study.focus_id not in order:
        return order[:3], order[:3]
    center = order.index(study.focus_id)
    positions = range(max(0, center - 2), min(len(order), center + 3))
    return [order[index] for index in positions], [order[index] for index in sorted(positions, key=lambda i: (abs(i - center), i))]


def update_window(db, study: ModelStudy, lessons: list[Lesson]) -> None:
    _, priority = focus_window(study, lessons)
    ranks = {identity: rank for rank, identity in enumerate(priority, 1)}
    for step in step_rows(db, study.project_id):
        if step.lesson_id is not None:
            step.priority = ranks.get(step.lesson_id)


def next_generation_step(db, study: ModelStudy, now: datetime) -> GenerationStep | None:
    clock = when_utc(now)
    pending = (step for step in step_rows(db, study.project_id)
               if step.state in ("pending", "review_pending") and (step.priority is not None or step.manual_review)
               and (step.available_at is None or step.available_at <= clock))
    return min(pending, key=lambda item: (-1 if item.manual_review else item.priority, item.id), default=None)


def settings_view(db) -> dict:
    saved = db.get(GenerationSettings, 1)
    return {"dailyLimit": saved.daily_limit if saved else None,
            "concurrency": saved.concurrency if saved else DEFAULT_CONCURRENCY,
            "autoContinueDailyBudget": bool(saved and saved.auto_continue_daily_budget),
            "dailyCostLimit": saved.daily_cost_limit if saved else None,
            "defaultTaskCostLimit": saved.default_task_cost_limit if saved else None}


def usage_view(db, project_id: str) -> dict:
    calls, input_, output, reported = db.execute(select(
        func.count(), func.sum(ModelUse.input_tokens), func.sum(ModelUse.output_tokens),
        func.count().filter(or_(ModelUse.input_tokens.is_not(None), ModelUse.output_tokens.is_not(None))),
    ).where(ModelUse.project_id == project_id, GENERATION_USE, COUNTED)).one()
    costs = sum_usage(db, ModelCharge.kind == "generation", ModelCharge.scope_id == project_id)
    return {"calls": calls, "inputTokens": input_, "outputTokens": output, "reportedCalls": reported, "costs": costs}


def sync_task(db, study: ModelStudy, project: Project) -> None:
    task = project.task
    task.error = ERRORS.get(study.reason)
    steps = step_rows(db, study.project_id)
    if study.deleted_at:
        task.status = "deleted"
        task.error = None
    elif study.stopped or project.paused:
        task.status = "restore_pending" if study.reason == "restored" else "stopped"
    elif any(step.publication_pending or step.state in {"running", "reviewing"} for step in steps):
        task.status = "queued"
    elif study.reason:
        task.status = "blocked"
    elif any(
        step.state in {"pending", "review_pending"} and (step.priority is not None or step.manual_review)
        for step in steps
    ):
        task.status = "queued"
    elif project.lessons and all(lesson.ready for lesson in project.lessons if not is_practice(lesson.outline)) and all(
        step.state not in {"disputed", "review_failed"} for step in steps
    ):
        task.status = "ready"
    else:
        task.status = "awaiting_review"


def study_view(db, study: ModelStudy, project: Project) -> dict:
    window, _ = focus_window(study, project.lessons)
    steps = {step.lesson_id: step for step in step_rows(db, project.id)}
    saved_connection = main_connection(db)
    plan = db.get(CourseReviewPlan, project.id)
    cost, inherited = task_limit(db, "generation", project.id)
    active = []
    for role, backup in (("main", study.backup), ("review", plan.backup if plan else None)):
        if backup and (failure := backup.get("failure")):
            active.append({"role": role, "connection": backup["connection"], "reason": failure, "message": ERRORS.get(failure)})
    return {
        "state": project.task.status, "reason": "project_paused" if project.paused else study.reason,
        "error": ERRORS.get("project_paused" if project.paused else study.reason),
        "focusId": study.focus_id, "window": window, "maxCalls": study.max_calls, "usage": usage_view(db, project.id),
        "maxCost": cost, "usesDefaultCost": inherited,
        "failovers": active, "backup": study.backup["connection"] if study.backup else None,
        "authorizationCurrent": bool(
            saved_connection and saved_connection.id == study.connection_id and saved_connection.revision == study.connection_revision
            and study.goal_fields == generation_goal_fields(db, study)
            and (not study.backup or not study.backup.get("failure") or main_backup(db, study) is not None)
        ),
        "steps": [
            {"id": step.id, "lessonId": identity, "kind": step.kind,
             "state": "outside_window" if step.state in {"pending", "review_pending"} and step.priority is None
             and not step.manual_review else step.state}
            for identity in [None, *(lesson.id for lesson in project.lessons)] if (step := steps.get(identity)) is not None
        ],
    }


def install_route(db, study: ModelStudy, project: Project, step: GenerationStep, now: datetime) -> None:
    if db.get(RouteVersion, (project.id, 1)) is not None:
        return
    identities = {node["key"]: str(uuid4()) for stage in step.result["stages"] for node in stage["lessons"]}
    nodes = [
        {"id": identities[node["key"]], "phase": stage["name"], "stageId": f"stage-{index}",
         "title": node["title"], "outcome": node["outcome"],
         "prerequisites": [identities[key] for key in node["prerequisites"]],
         "branch": node["branch"], "required": node["required"]}
        for index, stage in enumerate(step.result["stages"], 1) for node in stage["lessons"]
    ]
    project.lessons = [
        Lesson(id=node["id"], position=index, outline=node) for index, node in enumerate(nodes)
    ]
    db.flush()
    study.focus_id = nodes[0]["id"]
    db.add(RouteVersion(
        project_id=project.id, version=1, goal_version=1, created_at=study_moment(now),
        definition={"source": "model", "nodes": [{**node, "order": index} for index, node in enumerate(nodes, 1)]},
    ))
    for node in nodes:
        db.add(GenerationStep(id=str(uuid4()), project_id=project.id, lesson_id=node["id"], kind="draft", state="pending"))
    update_window(db, study, project.lessons)


def resume_study(db, study: ModelStudy, project: Project, now: datetime) -> None:
    study.stopped = False
    study.reason = study.daily_blocked_on = None
    for step in step_rows(db, study.project_id):
        if step.kind == "route" and step.state == "complete":
            install_route(db, study, project, step, now)
        elif step.state in {"failed", "review_failed"}:
            step.state = "review_pending" if step.state == "review_failed" else "pending"
            step.error = None
            step.available_at = None
        elif step.error == "publication_failed":
            step.error = None
            step.publication_pending = True
    sync_task(db, study, project)


def lesson_prompt_outline(project: Project, identity: str) -> dict:
    nodes = {lesson.id: lesson.outline for lesson in project.lessons}
    outline = nodes[identity]
    return {
        **{key: outline[key] for key in ("phase", "title", "outcome", "branch")},
        "prerequisites": [nodes[item]["title"] for item in outline["prerequisites"]],
    }


@dataclass(frozen=True)
class ReviewContext:
    plan_id: str
    sources: list[dict]
    draft: dict


@dataclass(frozen=True)
class Claim:
    identity: str
    step_id: str
    kind: str
    provider: str
    protocol: str
    address: str
    model: str
    credential: str | None
    prompt: str
    max_output: int
    review: ReviewContext | None = None
    single_section: bool = False


def utc_now() -> datetime:
    return datetime.now(UTC)


def when_utc(now: datetime) -> datetime:
    return (now.astimezone(UTC) if now.tzinfo else now).replace(tzinfo=None)


def generation_material(study: ModelStudy, project: Project, step: GenerationStep,
                        plan: CourseReviewPlan | None) -> tuple[str, int, ReviewContext | None]:
    request = active_revision(step)
    if step.state in {"review_pending", "reviewing"}:
        draft = review_draft(request, request["draft"]) if request else step.result
        review = (ReviewContext(request["id"], request["review"]["sources"], draft)
                  if request else ReviewContext(plan.id, plan.sources, draft))
        return review_prompt(review.draft, review.sources), 6144, review
    if request:
        prompt, maximum = revision_prompt(request)
        return prompt, maximum, None
    context = lesson_prompt_outline(project, step.lesson_id) if step.lesson_id else None
    prompt, maximum = prompt_for(step.kind, study.goal_fields, context)
    return prompt, maximum, None


def generation_amount_issue(db, study: ModelStudy, project: Project, now: datetime) -> str | None:
    day = billing_day(now)
    step = next_generation_step(db, study, now)
    if step is not None:
        connection, plan, issue = generation_recipient(db, study, step, reviewing=step.state == "review_pending")
        if issue is None:
            prompt, maximum, _ = generation_material(study, project, step, plan)
            return request_issue(db, "generation", project.id, day, connection, prompt, maximum)
    return held_issue(db, "generation", project.id, day)


def draft_author(db, step: GenerationStep) -> dict | None:
    return db.scalar(select(ModelUse.connection).where(
        ModelUse.step_id == step.id, ModelUse.status == "passed", GENERATION_USE,
        or_(ModelUse.phase == "draft", ModelUse.phase.is_(None)),
    ).order_by(ModelUse.finished_at.desc(), ModelUse.id.desc()).limit(1))


def main_backup(db, study: ModelStudy):
    alternate = pinned_connection(db, "mainBackup", study.backup["connection"]) if study.backup else None
    return alternate if usable_connection(alternate) else None


def course_backup(db, study: ModelStudy, plan: CourseReviewPlan, author: dict):
    if not plan.backup:
        return None
    alternate = independent_reviewer(db, study, "reviewBackup")
    if alternate and connection_view(alternate) == plan.backup["connection"] and not same_model(author, plan.backup["connection"]):
        return alternate
    return None


def review_recipient(db, step: GenerationStep, study: ModelStudy, plan: CourseReviewPlan | None):
    if not plan or pinned_connection(db, "review", plan.reviewer) is None:
        return None
    author = draft_author(db, step)
    if author is None:
        return None
    if not (plan.backup and plan.backup.get("failure")):
        reviewer = independent_reviewer(db, study)
        if reviewer and not same_model(author, plan.reviewer):
            return reviewer
    return course_backup(db, study, plan, author)


def revision_recipient(db, request, *, reviewing: bool):
    if not reviewing:
        return pinned_revision_model(db, request, alternate=bool(request.get("mainFailure")))
    author = request.get("author")
    if not author:
        return None
    use_backup = bool(request.get("reviewFailure"))
    reference = request["review"]["backup"] if use_backup else request["review"]["recipient"]
    recipient = pinned_connection(db, "reviewBackup" if use_backup else "review", reference)
    return recipient if usable_connection(recipient) and not same_model(author, reference) else None


def generation_recipient(db, study: ModelStudy, step: GenerationStep, *, reviewing: bool, reserved: bool = False):
    request = active_revision(step)
    plan = db.get(CourseReviewPlan, study.project_id) if reviewing and not request else None
    if study.max_calls is not None:
        calls = db.scalar(select(func.count()).select_from(ModelUse).where(
            ModelUse.project_id == study.project_id, GENERATION_USE, COUNTED,
        ))
        if calls + int(not reserved) > study.max_calls:
            return None, plan, "task_budget"
    if request:
        if not revision_review_current(db, step, request):
            return None, None, "review_configuration_changed"
        connection = revision_recipient(db, request, reviewing=reviewing)
        problem = "review_configuration_changed" if reviewing else "configuration_changed"
        return connection, plan, None if connection else problem
    if reviewing:
        connection = review_recipient(db, step, study, plan)
        return connection, plan, None if connection else "review_configuration_changed"
    if study.goal_fields != generation_goal_fields(db, study):
        return None, None, "goal_changed"
    connection = main_connection(db)
    if connection is None or connection.id != study.connection_id or connection.revision != study.connection_revision:
        return None, plan, "configuration_changed"
    if study.backup and study.backup.get("failure"):
        connection = main_backup(db, study)
        if connection is None:
            return None, plan, "configuration_changed"
    if not usable_connection(connection):
        return None, plan, "missing_key"
    return connection, plan, None


def active_backup_reason(study: ModelStudy, plan: CourseReviewPlan | None, recipient: dict) -> str | None:
    backup = plan.backup if plan else study.backup
    if backup and backup["connection"] == recipient:
        return backup.get("failure") or "review_independence"
    return None


def switch_recipient(db, study: ModelStudy, step: GenerationStep, plan: CourseReviewPlan | None,
                     call: ModelUse, failure: str) -> bool:
    if failure not in FAILOVER_FAILURES or call.failover_reason is not None:
        return False
    if request := active_revision(step):
        reviewing = call.phase == "review"
        key = "reviewFailure" if reviewing else "mainFailure"
        alternate = {**request, key: failure}
        if revision_recipient(db, alternate, reviewing=reviewing) is not None:
            step.revision_request = alternate
            return True
        return False
    if call.phase == "review":
        if plan is None or call.connection != plan.reviewer or pinned_connection(db, "review", plan.reviewer) is None:
            return False
        author = draft_author(db, step)
        if author is None or course_backup(db, study, plan, author) is None:
            return False
        if not plan.backup.get("failure"):
            plan.backup = {**plan.backup, "failure": failure}
    else:
        primary = pinned_connection(db, "main", call.connection)
        if (primary is None or primary.id != study.connection_id or primary.revision != study.connection_revision
                or main_backup(db, study) is None):
            return False
        if not study.backup.get("failure"):
            study.backup = {**study.backup, "failure": failure}
    return True


def reserve_call(engine, now: datetime) -> Claim | None:
    clock = when_utc(now)
    day = billing_day(clock)
    with transaction(engine) as db:
        settings = settings_view(db)
        active = active_calls(db)
        if active >= settings["concurrency"]:
            return None
        daily = billed_calls(db, day)
        pending = ModelStudy.reason.is_(None) & (Task.status == "queued")
        pending |= (ModelStudy.reason == "goal_changed") & Task.status.in_(("queued", "blocked"))
        if auto_resume_enabled(settings, daily):
            pending |= (ModelStudy.reason.in_(DAILY_BUDGET_REASONS) & (Task.status == "blocked") & (ModelStudy.daily_blocked_on < day))
        candidates = list(db.scalars(select(GenerationStep).join(
            ModelStudy, ModelStudy.project_id == GenerationStep.project_id,
        ).join(Task, Task.project_id == GenerationStep.project_id).join(Project, Project.id == GenerationStep.project_id)
            .where(or_(GenerationStep.priority.is_not(None), GenerationStep.manual_review.is_(True)),
                   GenerationStep.state.in_(("pending", "review_pending")), ModelStudy.stopped.is_(False),
                   pending, Project.paused.is_(False),
                   ModelStudy.deleted_at.is_(None),
                   or_(GenerationStep.available_at.is_(None), GenerationStep.available_at <= clock))
            .order_by(case((GenerationStep.manual_review.is_(True), -1), else_=GenerationStep.priority),
                      case({"high": 0, "medium": 1}, value=Project.priority, else_=2),
                      Project.created_on, GenerationStep.id)))
        folder = Path(engine.url.database).resolve().parent
        for step in candidates:
            study = db.get(ModelStudy, step.project_id)
            waiting_for_goal = study.reason == "goal_changed"
            if study.reason is not None and not waiting_for_goal and not (
                study.reason in DAILY_BUDGET_REASONS and study.daily_blocked_on is not None and study.daily_blocked_on < day
                and auto_resume_enabled(settings, daily)
            ):
                continue
            project = db.get(Project, step.project_id)
            reviewing = step.state == "review_pending"
            connection, plan, issue = generation_recipient(db, study, step, reviewing=reviewing)
            request = active_revision(step)
            if not issue and request:
                issue = revision_issue(db, folder, db.get(Lesson, step.lesson_id), request)
            recovering = study.reason in DAILY_BUDGET_REASONS
            if not issue and not request and recovering and reviewing and not sources_current(db, folder, plan.sources):
                issue = "review_sources_changed"
            if not issue and not daily_capacity(settings["dailyLimit"], daily):
                issue = "daily_budget"
            if issue:
                if request:
                    step.error = issue
                study.reason = issue
                if issue == "daily_budget":
                    study.daily_blocked_on = day
                sync_task(db, study, project)
                continue
            if not (waiting_for_goal and (reviewing or request)):
                study.reason, study.daily_blocked_on = None, None
            identity = str(uuid4())
            kind = "review" if reviewing else "revision_draft" if request else step.kind
            prompt, max_output, review = generation_material(study, project, step, plan)
            reference = connection_view(connection)
            if request:
                step.error = None
            step.state = "reviewing" if reviewing else "running"
            step.active_call = identity
            db.add(ModelUse(id=identity, project_id=project.id, step_id=step.id, connection=reference,
                            started_at=clock, billing_day=day, status="reserved", auto_retry=False, phase=kind,
                            failover_reason=(request.get("reviewFailure" if reviewing else "mainFailure")
                                             if request else active_backup_reason(study, plan, reference))))
            if recovering:
                sync_task(db, study, project)
            return Claim(identity, step.id, kind, connection.provider, connection.protocol, reference["address"],
                         connection.model, connection.credential_ref, prompt, max_output, review,
                         bool(request and request["section"] is not None))
    return None


def finish_revision(db, step, request, call, claim, result, clock) -> None:
    if claim.review:
        finding = CourseFinding(
            id=call.id, lesson_id=step.lesson_id, plan_id=request["review"]["planId"], reviewer=call.connection,
            created_at=clock, sources=[{key: item[key] for key in ("fileId", "revision", "at")} for item in claim.review.sources],
            sections=result["sections"],
        )
        db.add(finding)
        state = "revision_ready" if replacement_sections(request, finding) is not None else "revision_unverified"
        step.revision_request = {**request, "finding": finding.id}
        step.state, step.manual_review = state, False
    else:
        step.revision_request = {**request, "draft": result, "author": call.connection}
        step.state = "review_pending"
        backup = request["review"]["backup"]
        if same_model(call.connection, request["review"]["recipient"]) and backup and not same_model(call.connection, backup):
            step.revision_request = {**step.revision_request, "reviewFailure": "review_independence"}
    step.error = None


def complete_call(engine, claim: Claim, outcome: str, result: dict | None, usage: dict | None,
                  now: datetime, *, sent: bool) -> None:
    clock = when_utc(now)
    with transaction(engine) as db:
        call = db.get(ModelUse, claim.identity)
        step = db.get(GenerationStep, claim.step_id)
        if call is None or call.finished_at is not None or step is None or step.active_call != claim.identity:
            return
        study = db.get(ModelStudy, step.project_id)
        project = db.get(Project, step.project_id)
        plan = db.get(CourseReviewPlan, project.id)
        step.available_at = None
        request = active_revision(step)
        if request and claim.review and result is not None:
            issue = revision_issue(db, Path(engine.url.database).resolve().parent, db.get(Lesson, step.lesson_id), request)
            if not issue and not revision_review_current(db, step, request):
                issue = "review_configuration_changed"
            if issue:
                outcome, result = issue, None
        call.finished_at = clock
        call.status = outcome if sent else "not_sent"
        record_call_usage(call, usage)
        step.active_call = None
        if study.deleted_at is not None:
            call.status = "discarded" if sent else "not_sent"
        elif request and result is not None:
            finish_revision(db, step, request, call, claim, result, clock)
        elif not request and claim.review and (plan is None or claim.review.plan_id != plan.id):
            call.status = "discarded" if sent else "not_sent"
            step.state = "review_pending" if plan else "awaiting_review"
        elif claim.review and result is not None:
            finding = CourseFinding(
                id=call.id, lesson_id=step.lesson_id, plan_id=plan.id, reviewer=call.connection,
                created_at=clock, sources=[{key: item[key] for key in ("fileId", "revision", "at")} for item in claim.review.sources],
                sections=result["sections"],
            )
            db.add(finding)
            step.finding_id = finding.id
            step.manual_review = False
            step.error = None
            step.state = "reviewed" if all(item["state"] == "verified" for item in finding.sections) else "disputed"
            step.publication_pending = any(item["state"] == "verified" for item in finding.sections)
        elif result is not None:
            step.error = None
            step.result = result
            if step.kind == "route":
                step.state = "complete"
            else:
                step.state = "review_pending" if plan else "awaiting_review"
            if step.kind == "route" and not study.stopped and not project.paused:
                install_route(db, study, project, step, now)
        else:
            step.error = outcome
            retries = db.scalar(select(func.count()).select_from(ModelUse).where(
                ModelUse.project_id == project.id, GENERATION_USE,
                ModelUse.auto_retry.is_(True),
            ))
            retry = retries < 2 and not study.stopped and not project.paused
            switched = retry and switch_recipient(db, study, step, plan, call, outcome)
            if switched or (retry and outcome in TRANSIENT):
                call.auto_retry = True
                step.state = "review_pending" if claim.review else "pending"
                step.available_at = None if switched else clock + timedelta(seconds=2 + 4 * retries)
            else:
                step.state = "review_failed" if claim.review else "failed"
                study.reason = study.reason or outcome
        touch_project(project, study_moment(now))
        sync_task(db, study, project)


def dispatch(engine, claim: Claim, *, vault: CredentialStore | None = None,
             transport: Transport = post_json, now=utc_now) -> None:
    vault = vault if vault is not None else NativeCredentialStore()
    try:
        key = vault.get(claim.credential) if claim.credential else None
    except CREDENTIAL_FAILURES:
        complete_call(engine, claim, "credential_store", None, None, now(), sent=False)
        return
    if not key and (claim.credential or claim.provider != "custom"):
        complete_call(engine, claim, "missing_key", None, None, now(), sent=False)
        return
    folder = Path(engine.url.database).resolve().parent
    with transaction(engine) as db:
        call = db.get(ModelUse, claim.identity)
        step = db.get(GenerationStep, claim.step_id)
        if call is None or call.status != "reserved" or call.finished_at is not None or step is None or step.active_call != call.id:
            return
        study = db.get(ModelStudy, step.project_id)
        project = db.get(Project, step.project_id)
        clock = when_utc(now())
        if study.stopped or study.deleted_at is not None or project.paused:
            call.status, call.finished_at = "not_sent", clock
            step.active_call = None
            if study.deleted_at is None:
                step.state = "review_pending" if claim.review else "pending"
            sync_task(db, study, project)
            return
        connection, plan, issue = generation_recipient(db, study, step, reviewing=claim.review is not None, reserved=True)
        request = active_revision(step)
        outdated_prompt = (
            not issue and not request and claim.review is None
            and claim.prompt != generation_material(study, project, step, plan)[0]
        )
        if issue == "goal_changed" or outdated_prompt:
            call.status, call.finished_at = "not_sent", clock
            step.state, step.active_call = "pending", None
            study.reason = study.reason or issue
            sync_task(db, study, project)
            return
        if not issue and claim.review and not request and claim.review.plan_id != plan.id:
            issue = "review_configuration_changed"
        if not issue and request:
            issue = revision_issue(db, folder, db.get(Lesson, step.lesson_id), request)
        if not issue and (reference := connection_view(connection)) != call.connection:
            if not request and call.failover_reason is None and active_backup_reason(study, plan, reference):
                call.status, call.finished_at = "not_sent", clock
                step.active_call = None
                step.state = "review_pending" if claim.review else "pending"
                sync_task(db, study, project)
                return
            issue = "review_configuration_changed" if claim.review else "configuration_changed"
        if not issue and claim.review and not sources_current(db, folder, claim.review.sources):
            issue = "review_sources_changed"
        budget_issue = None
        if not issue and study.reason in MONEY_ERRORS:
            budget_issue = study.reason
            call.status, call.finished_at = "not_sent", clock
        elif not issue:
            budget_issue = admit_reserved_call(db, call, now, claim.prompt, claim.max_output)
        if budget_issue:
            step.state = "review_pending" if claim.review else "pending"
            step.active_call = step.available_at = None
            if study.reason is None:
                study.reason, study.daily_blocked_on = budget_issue, call.billing_day
                if request:
                    step.error = budget_issue
            sync_task(db, study, project)
            return
    if issue:
        complete_call(engine, claim, issue, None, None, clock, sent=False)
        return
    reply = request_model(engine, ModelUse, claim.identity, claim, key, claim.prompt, claim.max_output, transport, now)
    if key and key in reply.text:
        reply = TextReply("invalid_response", reply.usage)
    result = None
    outcome = reply.status
    if outcome == "passed":
        try:
            if claim.review:
                document = parse_model_json(ReviewReply, reply.text)
                result = {"sections": assess(document, claim.review.draft, claim.review.sources)}
            else:
                result = parse_result(claim.kind, reply.text)
                if claim.single_section and len(result["sections"]) != 1:
                    raise ValueError("局部修订应只返回一个小节。")
        except ValueError:
            result = None
            outcome = "invalid_response"
    complete_call(engine, claim, outcome, result, reply.usage, now(), sent=True)


def process_one(engine, *, vault: CredentialStore | None = None, transport: Transport = post_json, now=utc_now) -> bool:
    claim = reserve_call(engine, now())
    if claim is None:
        return False
    dispatch(engine, claim, vault=vault, transport=transport, now=now)
    return True


def settle_interrupted(engine, now: datetime) -> None:
    """Call after acquiring the exclusive worker lease for this data directory."""
    clock = when_utc(now)
    with transaction(engine) as db:
        for call in db.scalars(select(ModelUse).where(
            ModelUse.finished_at.is_(None), GENERATION_USE,
        )):
            unsent = settle_interrupted_use(call, clock)
            step = db.get(GenerationStep, call.step_id)
            if step.active_call == call.id:
                step.active_call = None
                study = db.get(ModelStudy, step.project_id)
                if study.deleted_at is None:
                    reviewing = call.phase == "review"
                    if unsent:
                        step.state = "review_pending" if reviewing else "pending"
                    else:
                        step.state = "review_failed" if reviewing else "failed"
                        step.error = "uncertain"
                        study.reason = study.reason or "uncertain"
                sync_task(db, study, db.get(Project, step.project_id))
