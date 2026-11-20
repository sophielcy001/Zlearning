from hmac import compare_digest, digest
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field, StringConstraints
from sqlalchemy import select, update

from .assessment import basis_hash, suspend_revised_rounds
from .course_anchors import Anchor, SQLITE_MAX, passage, pending_anchors, resolve_anchor, target_anchor
from .course_publication import course_markdown, original_matches, save_course_version
from .course_review import Index, independent_reviewer, same_model
from .course_revision import active_revision, replacement_sections, revision_issue, revision_review_current, review_draft
from .course_sources import HexDigest, SourceLocator, locate_sources, observe_sources, sources_current
from .db import AssessmentObjective, CourseAnchorLink, CourseFinding, CourseReviewPlan, CourseVersion, GenerationStep, Lesson, ModelStudy, Note, NoteLesson, Project, transaction
from .file_locations import InvalidLocation, LocationConflict
from .library import FileTooLarge
from .library_index import LibraryIndex
from .model_study import ERRORS, main_backup, settings_view, sync_task
from .model_study_input import Input, Revision, connection_view, main_connection, usable_connection
from .money_budget import task_limit
from .note_catalog import has_legacy_file, note_view
from .project_activity import study_moment, touch_project


RevisionSources = Annotated[list[SourceLocator], Field(min_length=1, max_length=6)]


class RevisionInput(Input):
    baseVersion: Revision
    section: Index | None = None
    instruction: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
    sources: RevisionSources | None = None


class BeginRevision(RevisionInput):
    confirmation: HexDigest


class RevisionIdentity(Input):
    id: UUID


class AcceptRevision(RevisionIdentity):
    confirmation: HexDigest


class RenewReview(RevisionIdentity):
    sources: RevisionSources | None = None


class ConfirmReview(RenewReview):
    confirmation: HexDigest


class AnchorChoice(Input):
    source: Anchor
    target: Anchor


class ConfirmAnchor(AnchorChoice):
    confirmation: HexDigest


def signature(secret, scope, lesson_id, *fields):
    encoded = json.dumps([scope, lesson_id, *fields], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return digest(secret, encoded.encode("utf-8"), "sha256").hex()


def course_target(db, identity):
    lesson = db.get(Lesson, str(identity))
    if lesson is None:
        raise HTTPException(404, "找不到这节课程。")
    step = db.scalar(select(GenerationStep).where(GenerationStep.lesson_id == lesson.id))
    if step is None or not lesson.ready:
        raise HTTPException(404, "课程发布后可以发起模型修订。")
    return lesson, step, db.get(ModelStudy, step.project_id), db.get(Project, step.project_id)


def available(study, project):
    if study.deleted_at or study.stopped or project.paused:
        raise HTTPException(409, "请先恢复学习项目及课程生成任务。")


def model_scope(db, study):
    main = main_connection(db)
    if not (usable_connection(main) and main.id == study.connection_id and main.revision == study.connection_revision):
        raise HTTPException(409, "请在项目中重新确认课程主模型。")
    review = reviewer_scope(db, study)
    alternate = main_backup(db, study)
    backup = connection_view(alternate) if alternate else None
    if backup and same_model(backup, review["recipient"]) and (not review["backup"] or same_model(backup, review["backup"])):
        backup = None
    return connection_view(main), backup, review


def reviewer_scope(db, study):
    plan = db.get(CourseReviewPlan, study.project_id)
    primary_review = independent_reviewer(db, study)
    if plan is None or primary_review is None or connection_view(primary_review) != plan.reviewer:
        raise HTTPException(409, "请在项目中确认独立复核模型和资料。")
    alternate_review = independent_reviewer(db, study, "reviewBackup")
    review_backup = (connection_view(alternate_review)
                     if alternate_review and plan.backup and connection_view(alternate_review) == plan.backup["connection"]
                     else None)
    return {
        "planId": plan.id, "recipient": plan.reviewer, "backup": review_backup, "sources": plan.sources,
    }


def select_review_sources(db, folder, review, locators):
    if locators is not None:
        review["sources"] = locate_sources(db, folder, locators)
    elif not sources_current(db, folder, review["sources"]):
        raise HTTPException(409, ERRORS["review_sources_changed"])


def renewal_scope(db, folder, lesson, step, study, project, data):
    available(study, project)
    request = active_revision(step)
    if not request or request["id"] != str(data.id) or not request.get("draft") or step.active_call:
        raise HTTPException(409, "请等当前模型调用结束或刷新已保存的修订草稿。")
    if revision_issue(db, folder, lesson, request) == "revision_base_changed":
        raise HTTPException(409, ERRORS["revision_base_changed"])
    review = reviewer_scope(db, study)
    select_review_sources(db, folder, review, data.sources)
    if same_model(review["recipient"], request["author"]) and (not review["backup"] or same_model(review["backup"], request["author"])):
        raise HTTPException(409, "请为这份草稿选择与实际作者独立的复核模型。")
    return request, review


def snapshot_scope(db, folder, lesson, step, study, project, data):
    available(study, project)
    current = db.get(CourseVersion, (lesson.id, lesson.version))
    if (current is None or current.version != data.baseVersion
            or not original_matches(db, folder, lesson, current)):
        raise HTTPException(409, "本机正文或课程版本已有改动，请刷新并保留手工编辑。")
    if step.active_call or step.publication_pending or step.state not in {"reviewed", "disputed"}:
        raise HTTPException(409, "请等本节当前处理完成后再修订。")
    shown = current.sections if data.section is None else [item for item in current.sections if item["index"] == data.section]
    if not shown:
        raise HTTPException(422, "请选择本版中已经发布的小节。")
    main, backup, review = model_scope(db, study)
    select_review_sources(db, folder, review, data.sources)
    limit, _ = task_limit(db, "generation", project.id)
    scope = {
        **data.model_dump(include={"baseVersion", "section", "instruction"}),
        "baseRevision": current.revision, "priorRequest": (step.revision_request or {}).get("id"),
        "goal": {field["label"]: field["value"] for field in study.goal_fields if field["value"]},
        "content": [{key: item[key] for key in ("index", "title", "text", "checks")} for item in shown],
        "context": current.sections, "main": main, "backup": backup, "review": review,
        "maxCalls": study.max_calls, "maxCost": limit, "dailyCost": settings_view(db)["dailyCostLimit"],
    }
    if data.sources is not None:
        scope["shareSourcesWithAuthor"] = True
    return scope


def note_impact(db, folder, lesson):
    notes = list(db.scalars(select(Note).join(NoteLesson).where(NoteLesson.lesson_id == lesson.id).order_by(Note.id)))
    items = [note_view(db, note) for note in notes]
    if not any(note.id == lesson.id for note in notes) and has_legacy_file(db, folder, lesson.id):
        items.append({"id": lesson.id, "title": "本节笔记"})
    return items


def revision_view(db, folder, secret, lesson, step, study, project):
    request = step.revision_request
    if request is None:
        return {"state": "idle", "baseVersion": lesson.version}
    status = request.get("status")
    summary = {
        "id": request["id"], "baseVersion": request["baseVersion"], "section": request["section"],
        "instruction": request["instruction"], "publishedVersion": request.get("publishedVersion"),
    }
    if status:
        result = {**summary, "state": status}
        if status == "accepted":
            result.update(seen=request.get("seen", False), changes=stored_diff(db, lesson, request))
        return result
    issue = revision_issue(db, folder, lesson, request)
    if not issue and not revision_review_current(db, step, request):
        issue = "review_configuration_changed"
    state = {"revision_ready": "ready", "revision_unverified": "not_verified"}.get(step.state, step.state)
    reason = issue or ("project_paused" if project.paused else study.reason or step.error)
    if study.stopped or project.paused:
        state = "stopped"
    elif issue or (step.error and study.reason == step.error):
        state = "blocked"
    result = {**summary, "state": state, "reason": reason, "error": ERRORS.get(reason)}
    if request.get("draft") and not step.active_call and reason in {"review_configuration_changed", "review_sources_changed"}:
        result["canRenewReview"] = True
    if study.stopped and study.reason == "restored" and not project.paused and step.state in {"revision_ready", "revision_unverified"}:
        result["canContinueRestore"] = True
    if request.get("finding"):
        finding = db.get(CourseFinding, request["finding"])
        result["findings"] = finding.sections
        candidate = replacement_sections(request, finding)
        if state == "ready" and candidate is not None and not candidate_sources_current(db, folder, candidate):
            result.update(state="blocked", reason="review_sources_changed", error=ERRORS["review_sources_changed"])
            return result
        if state == "ready" and candidate is not None:
            result["changes"] = section_difference(request["context"], candidate)
            result["notes"] = note_impact(db, folder, lesson)
            proofs = {basis_hash(item) for item in candidate if item["state"] == "verified"}
            result["objectives"] = [
                {"id": item.key, "title": item.title}
                for item in db.scalars(select(AssessmentObjective).where(AssessmentObjective.lesson_id == lesson.id)
                                      .order_by(AssessmentObjective.key)) if item.basis["proof"] not in proofs
            ]
            result["confirmation"] = signature(secret, "accept-course-revision", lesson.id, request["id"], finding.id,
                                               request["baseRevision"], result["notes"], result["objectives"])
    return result


def candidate_sources_current(db, folder, sections):
    return sources_current(db, folder, [ref for item in sections if item["state"] == "verified" for ref in item["citations"]])


def section_difference(original, replacement):
    before = {item["index"]: item for item in original}
    after = {item["index"]: item for item in replacement}
    before_order = {index: position for position, index in enumerate(before, 1)}
    after_order = {index: position for position, index in enumerate(after, 1)}
    audit_fields = {"revisionId", "revisionAuthor", "previousProof"}
    def content(item):
        return {key: value for key, value in item.items() if key not in audit_fields} if item else None
    order = list(after) + [index for index in before if index not in after]
    return [
        {"index": index, "before": before.get(index), "after": after.get(index),
         "beforeOrder": before_order.get(index), "afterOrder": after_order.get(index)}
        for index in order
        if content(before.get(index)) != content(after.get(index)) or before_order.get(index) != after_order.get(index)
    ]


def stored_diff(db, lesson, request):
    before = db.get(CourseVersion, (lesson.id, request["baseVersion"]))
    after = db.get(CourseVersion, (lesson.id, request["publishedVersion"]))
    return section_difference(before.sections, after.sections) if before and after else []


def restore_step(step, request, status, **fields):
    step.state = request["previousState"]
    step.manual_review = False
    step.error = step.available_at = None
    step.revision_request = {
        **{key: request[key] for key in ("id", "baseVersion", "section", "instruction", "confirmation")},
        "status": status, **fields,
    }


def current_course(db, identity):
    lesson = db.get(Lesson, str(identity))
    if lesson is None:
        raise HTTPException(404, "找不到这节课程。")
    return lesson, db.get(CourseVersion, (lesson.id, lesson.version)) if lesson.ready else None


def anchor_scope(db, folder, secret, identity, data):
    lesson, current = current_course(db, identity)
    if (current is None or data.target.version != current.version or data.source.version >= current.version
            or not original_matches(db, folder, lesson, current)):
        raise HTTPException(409, "当前课程版本或本机文件已有变化，请刷新后重新选择。")
    old = db.get(CourseVersion, (lesson.id, data.source.version))
    original, target = passage(old, data.source), passage(current, data.target)
    if original is None or target is None:
        raise HTTPException(422, "找不到原引文或所选段落。")
    prior = db.get(CourseAnchorLink, data.source.key(lesson.id))
    match = resolve_anchor(db, old, current, data.source, original)
    if prior is None and (not match["revision"] or match["target"] is not None):
        raise HTTPException(409, "这条引用暂不需要重新关联。")
    quote = target["text"][data.target.start:data.target.end]
    if not quote.strip():
        raise HTTPException(422, "请选择有内容的新版原文。")
    confirmation = signature(secret, "associate-course-anchor", lesson.id, data.source.model_dump(),
                             data.target.model_dump(), old.revision, current.revision, prior.mapping_revision if prior else None)
    return prior, {
        "source": {**data.source.model_dump(), "title": original["title"], "quote": original["text"][data.source.start:data.source.end]},
        "target": {**data.target.model_dump(), "title": target["title"], "quote": quote},
        "confirmation": confirmation,
    }


def course_revision_router(engine, folder: Path, session_token: str, now):
    router = APIRouter(prefix="/api/lessons")
    secret = session_token.encode("utf-8")
    index = LibraryIndex(engine, folder)

    @router.get("/{identity}/revision")
    def view(identity: UUID):
        with transaction(engine) as db:
            return revision_view(db, folder, secret, *course_target(db, identity))

    @router.get("/{identity}/revision-anchor")
    def match_anchor(identity: UUID, fromVersion: Annotated[int, Field(ge=1, le=SQLITE_MAX)],
                     version: Annotated[int, Field(ge=1, le=SQLITE_MAX)],
                     section: Annotated[int, Field(ge=0, le=SQLITE_MAX)],
                     start: Annotated[int, Field(ge=0, le=SQLITE_MAX)], end: Annotated[int, Field(ge=1, le=SQLITE_MAX)]):
        source = Anchor(version=fromVersion, section=section, start=start, end=end)
        with transaction(engine, write=False) as db:
            old = db.get(CourseVersion, (str(identity), fromVersion))
            new = db.get(CourseVersion, (str(identity), version))
            origin = passage(old, source)
            if new is None or origin is None:
                raise HTTPException(404, "找不到引用的课程版本或原文。")
            return resolve_anchor(db, old, new, source, origin)

    @router.get("/{identity}/revision/anchor-impact")
    def anchor_impact(identity: UUID):
        with observe_sources(engine) as db:
            lesson, step, _, _ = course_target(db, identity)
            request = active_revision(step)
            if step.state != "revision_ready" or request is None or not request.get("finding"):
                raise HTTPException(409, "等待新版通过独立核验。")
            finding = db.get(CourseFinding, request["finding"])
            candidate = replacement_sections(request, finding) if finding else None
            if candidate is None:
                raise HTTPException(409, "当前没有可预览的已核验修订。")
            preview = CourseVersion(lesson_id=lesson.id, version=lesson.version + 1, sections=candidate)
            return pending_anchors(db, folder, preview)

    @router.get("/{identity}/anchor-links")
    def anchor_links(identity: UUID):
        with observe_sources(engine) as db:
            _, current = current_course(db, identity)
            return pending_anchors(db, folder, current)

    @router.post("/{identity}/anchor-links/preview")
    def preview_anchor_link(identity: UUID, data: AnchorChoice):
        with observe_sources(engine) as db:
            _, scope = anchor_scope(db, folder, secret, identity, data)
            return scope

    @router.put("/{identity}/anchor-links")
    def save_anchor_link(identity: UUID, data: ConfirmAnchor):
        with transaction(engine) as db:
            prior, scope = anchor_scope(db, folder, secret, identity, data)
            result = {"target": data.target.model_dump()}
            if prior is not None and target_anchor(prior) == data.target:
                return result
            if not compare_digest(data.confirmation, scope["confirmation"]):
                raise HTTPException(409, "关联位置已变化，请重新预览确认。")
            if prior is None:
                prior = CourseAnchorLink(lesson_id=str(identity), source_version=data.source.version,
                                         source_section=data.source.section, source_start=data.source.start,
                                         source_end=data.source.end, mapping_revision=0)
                db.add(prior)
            prior.target_version, prior.target_section = data.target.version, data.target.section
            prior.target_start, prior.target_end = data.target.start, data.target.end
            prior.mapping_revision += 1
            return result

    @router.post("/{identity}/revision/seen")
    def seen(identity: UUID, data: RevisionIdentity):
        with transaction(engine) as db:
            lesson, step, study, project = course_target(db, identity)
            request = step.revision_request
            if request is None or request["id"] != str(data.id) or request.get("status") != "accepted":
                raise HTTPException(409, "课程更新已变化，请刷新。")
            step.revision_request = {**request, "seen": True}
            return revision_view(db, folder, secret, lesson, step, study, project)

    @router.post("/{identity}/revision/preview")
    def preview(identity: UUID, data: RevisionInput):
        for file_id in {str(locator.fileId) for locator in data.sources or []}:
            index.refresh(identity=file_id)
        with transaction(engine) as db:
            lesson, step, study, project = course_target(db, identity)
            saved = step.revision_request
            if saved and not saved.get("status"):
                raise HTTPException(409, "请先处理当前修订稿。")
            scope = snapshot_scope(db, folder, lesson, step, study, project, data)
            return {
                "main": scope["main"], "backup": scope["backup"], "reviewer": scope["review"]["recipient"],
                "reviewBackup": scope["review"]["backup"], "sources": scope["review"]["sources"],
                "shareSourcesWithAuthor": scope.get("shareSourcesWithAuthor", False),
                "content": scope["content"], "context": [
                    {key: item[key] for key in ("index", "title", "text", "checks")}
                    for item in scope["context"] if data.section is not None and item["index"] != data.section
                ],
                "goal": scope["goal"], "maxCalls": scope["maxCalls"], "maxCost": scope["maxCost"], "dailyCost": scope["dailyCost"],
                "confirmation": signature(secret, "start-course-revision", lesson.id, scope),
            }

    @router.post("/{identity}/revision", status_code=202)
    def begin(identity: UUID, data: BeginRevision):
        with transaction(engine) as db:
            lesson, step, study, project = course_target(db, identity)
            previous = step.revision_request
            if previous and compare_digest(previous["confirmation"], data.confirmation):
                return revision_view(db, folder, secret, lesson, step, study, project)
            if active_revision(step):
                raise HTTPException(409, "请先处理当前修订稿。")
            if previous and previous.get("status") == "accepted" and not previous.get("seen"):
                raise HTTPException(409, "请先查看上次课程更新。")
            scope = snapshot_scope(db, folder, lesson, step, study, project, data)
            if not compare_digest(data.confirmation, signature(secret, "start-course-revision", lesson.id, scope)):
                raise HTTPException(409, "课程、修订要求、模型或来源已变化，请重新预览发送范围。")
            step.revision_request = {
                **scope, "confirmation": data.confirmation, "id": str(uuid4()), "previousState": step.state,
            }
            step.state, step.manual_review = "pending", True
            sync_task(db, study, project)
            return revision_view(db, folder, secret, lesson, step, study, project)

    @router.post("/{identity}/revision/accept")
    def accept(identity: UUID, data: AcceptRevision):
        with transaction(engine) as db:
            lesson, step, study, project = course_target(db, identity)
            request = step.revision_request
            if request is None or request["id"] != str(data.id):
                raise HTTPException(409, "修订记录已变化，请刷新查看。")
            if request.get("status") == "accepted" and compare_digest(request["acceptConfirmation"], data.confirmation):
                return revision_view(db, folder, secret, lesson, step, study, project)
            available(study, project)
            summary = revision_view(db, folder, secret, lesson, step, study, project)
            if summary["state"] != "ready" or not compare_digest(summary["confirmation"], data.confirmation):
                raise HTTPException(409, "需要查看仍然有效的独立核验和差异才能接受新版。")
            finding = db.get(CourseFinding, request["finding"])
            sections = replacement_sections(request, finding)
            if not candidate_sources_current(db, folder, sections):
                raise HTTPException(409, ERRORS["review_sources_changed"])
            original = db.get(CourseVersion, (lesson.id, request["baseVersion"]))
            if request["section"] is not None:
                original_sections = {item["index"]: item for item in original.sections}
                for item in sections:
                    previous = original_sections[item["index"]]
                    if previous["state"] == "verified" and basis_hash(item) != basis_hash(previous):
                        item["previousProof"] = basis_hash(previous)
            clock = now()
            try:
                number = save_course_version(db, folder, lesson, original, finding.id, sections,
                                             course_markdown(lesson.outline["title"], sections), clock)
            except (OSError, InvalidLocation, FileTooLarge, LocationConflict) as error:
                raise HTTPException(409, ERRORS["publication_failed"]) from error
            restore_step(step, request, "accepted", publishedVersion=number, acceptConfirmation=data.confirmation)
            affected = [item["id"] for item in summary["objectives"]]
            db.execute(update(AssessmentObjective).where(
                AssessmentObjective.lesson_id == lesson.id, AssessmentObjective.key.in_(affected),
            ).values(held=True))
            suspend_revised_rounds(db, lesson, affected)
            if request["section"] is None:
                step.state, step.finding_id, step.result = "reviewed", None, None
            touch_project(project, study_moment(clock))
            sync_task(db, study, project)
            return revision_view(db, folder, secret, lesson, step, study, project)

    @router.post("/{identity}/revision/review-preview")
    def review_preview(identity: UUID, data: RenewReview):
        for file_id in {str(locator.fileId) for locator in data.sources or []}:
            index.refresh(identity=file_id)
        with transaction(engine) as db:
            lesson, step, study, project = course_target(db, identity)
            request, review = renewal_scope(db, folder, lesson, step, study, project, data)
            draft = review_draft(request, request["draft"])["sections"]
            return {
                "reviewer": review["recipient"], "reviewBackup": review["backup"], "sources": review["sources"],
                "content": draft,
                "confirmation": signature(secret, "renew-revision-review", lesson.id, request["id"],
                                          request["review"], request["draft"], review),
            }

    @router.post("/{identity}/revision/review-authorize")
    def review_authorize(identity: UUID, data: ConfirmReview):
        with transaction(engine) as db:
            lesson, step, study, project = course_target(db, identity)
            previous = active_revision(step)
            if previous and previous["id"] == str(data.id) and previous.get("reviewConfirmation") == data.confirmation:
                return revision_view(db, folder, secret, lesson, step, study, project)
            request, review = renewal_scope(db, folder, lesson, step, study, project, data)
            token = signature(secret, "renew-revision-review", lesson.id, request["id"], request["review"], request["draft"], review)
            if not compare_digest(token, data.confirmation):
                raise HTTPException(409, "复核模型、资料或草稿已经变化，请重新预览。")
            step.revision_request = {
                **{key: value for key, value in request.items() if key not in {"finding", "reviewFailure"}},
                "review": review, "reviewConfirmation": token,
            }
            if same_model(review["recipient"], request["author"]):
                step.revision_request = {**step.revision_request, "reviewFailure": "review_independence"}
            if study.reason in {"review_configuration_changed", "review_sources_changed"}:
                study.reason = study.daily_blocked_on = None
            step.state, step.manual_review, step.error, step.available_at = "review_pending", True, None, None
            sync_task(db, study, project)
            return revision_view(db, folder, secret, lesson, step, study, project)

    @router.post("/{identity}/revision/discard")
    def discard(identity: UUID, data: RevisionIdentity):
        with transaction(engine) as db:
            lesson, step, study, project = course_target(db, identity)
            request = step.revision_request
            if request is None or request["id"] != str(data.id):
                raise HTTPException(409, "修订记录已变化，请刷新查看。")
            if request.get("status") == "discarded":
                return revision_view(db, folder, secret, lesson, step, study, project)
            if request.get("status") or study.deleted_at:
                raise HTTPException(409, "修订正在处理或已结束，暂不可丢弃。")
            if step.active_call:
                raise HTTPException(409, "请等正在执行的这次调用结束后再保留旧版。")
            if study.reason and study.reason == step.error:
                study.reason = study.daily_blocked_on = None
            restore_step(step, request, "discarded")
            sync_task(db, study, project)
            return revision_view(db, folder, secret, lesson, step, study, project)

    return router
