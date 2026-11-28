import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import partial
from datetime import UTC, datetime
from errno import EINVAL, ENOTSUP
from hashlib import sha256
import os
import sqlite3
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
from time import monotonic

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .assessment_worker import (
    AssessmentClaim, complete_assessment, dispatch_assessment, reserve_assessment, settle_interrupted_assessments,
)
from .backup_automatic import CHECK_INTERVAL, AutomaticBackups
from .backup_catalog import BackupCatalog
from .agent_authoring import (
    AgentAuthoringClaim, complete_agent_authoring, dispatch_agent_authoring, reserve_agent_authoring, settle_agent_authoring,
)
from .memory_inference import (
    MemoryInferenceClaim, complete_memory_inference, dispatch_memory_inference, reserve_memory_inference, settle_memory_inference,
)
from .chat_model_worker import ChatReplyClaim, complete_reply, dispatch_reply, reserve_reply, settle_interrupted_replies
from .content import is_practice, lesson_course
from .course_publication import publish_one
from .db import (
    AgentAuthoringJob, ChatModelCall, GoalAuthoringJob, GraphRecommendation, Lesson, LibraryClassification, MemoryInferenceJob, ModelStudy, ModelUse,
    NoteCheckBatch, NoteSuggestion, PrerequisiteCall, Project, StageReviewCall, Task, open_db, transaction,
)
from .external_memory import ExternalMemoryCredentials, settle as settle_external_memory, step as sync_external_memory
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path, identity_of
from .library import atomic_write, local_path
from .library_folders import rename_without_replacing
from .goal_authoring_worker import (
    GoalAuthoringClaim, complete_goal_authoring, dispatch_goal_authoring, reserve_goal_authoring, settle_goal_authoring,
)
from .graph_recommendation_worker import (
    RecommendationClaim, complete_recommendation, dispatch_recommendation, reserve_recommendation, settle_recommendations,
)
from .library_classification_worker import (
    ClassificationClaim, complete_classification, dispatch_classification, reserve_classification, settle_classifications,
)
from .library_semantic import SemanticIndex
from .semantic_model import prepare_windows_semantic_runtime
from .lesson_answer_worker import AnswerClaim, complete_answer, dispatch_answer, reserve_answer, settle_interrupted_answers
from .model_credentials import CredentialStore
from .model_ledger import settle_open_charges
from .model_protocols import Transport, TransportNotSent, post_json
from .model_study import Claim as GenerationClaim, complete_call, dispatch, reserve_call, settle_interrupted, utc_now, when_utc
from .model_study_input import MAX_WORKERS
from .note_ai_worker import NoteAIClaim, complete_note_ai, dispatch_note_ai, reserve_note_ai, settle_interrupted_note_ai
from .note_check_worker import NoteCheckClaim, complete_note_check, dispatch_note_check, reserve_note_check, settle_note_checks
from .note_checks import SCAN_INTERVAL, scan_checks
from .process_io import watch_parent_disconnect
from .project_activity import study_day, study_moment, touch_project
from .prerequisite_worker import (
    PrerequisiteClaim, complete as complete_prerequisite, dispatch as dispatch_prerequisite,
    publish_one as publish_prerequisite, reserve as reserve_prerequisite, settle_interrupted as settle_prerequisites,
)
from .remediation import publish_remediation
from .stage_review_worker import (
    Claim as StageReviewClaim, complete as complete_stage_review, dispatch as dispatch_stage_review,
    reserve as reserve_stage_review, settle_interrupted as settle_stage_reviews,
)
from .study_consolidation_worker import (
    ConsolidationClaim, complete as complete_consolidation, dispatch as dispatch_consolidation,
    publish_one as publish_consolidation, reserve as reserve_consolidation, settle_interrupted as settle_consolidations,
)


def write_practice(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=".writing-", delete=False) as output:
        temporary = Path(output.name)
        try:
            content = body.encode("utf-8")
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            try:
                rename_without_replacing(temporary, path)
            except FileExistsError:
                if identity_of(path) is None:
                    raise
                with path.open("rb") as existing:
                    if existing.read(len(content) + 1) != content:
                        raise
        finally:
            output.close()
            temporary.unlink(missing_ok=True)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (EINVAL, ENOTSUP):
                raise
        finally:
            os.close(descriptor)


def prepare_course(db, folder: Path, project: Project, lesson: Lesson) -> None:
    course = lesson_course(project.sample, lesson.position, lesson.outline)
    path = local_path(folder, "courses", lesson.id)
    content = f"# {course.title}\n\n{course.body.strip()}\n"
    if course.source:
        title, url = course.source
        content += f"\n## 参考资料\n\n[{title}]({url})\n"
    path = checked_path(folder, path.relative_to(folder).as_posix())
    if is_practice(lesson.outline):
        write_practice(path, content)
    elif path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise OSError("课程文件已有不同内容")
    else:
        atomic_write(path, content)
    identity = identity_of(path)
    if identity is None:
        raise OSError("课程文件已移走。")
    LocationTracker(db, folder).bind(f"course-{lesson.id}", path, identity, sha256(content.encode("utf-8")).hexdigest())
    lesson.ready = True
    now = study_moment(datetime.now(UTC))
    touch_project(project, datetime.combine(study_day(db, now.date()), now.time()))


def run_one(engine: Engine, folder: Path) -> bool:
    folder = folder.resolve()
    with transaction(engine) as db:
        practice = db.scalar(select(Lesson).where(
            Lesson.ready.is_(False), Lesson.outline["kind"].as_string() == "practice",
            Lesson.outline["preparationError"].as_string().is_(None),
        ).order_by(Lesson.id).limit(1))
        if practice is not None:
            try:
                prepare_course(db, folder, db.get(Project, practice.project_id), practice)
            except (OSError, UnicodeError, InvalidLocation, LocationConflict):
                practice.outline = {**practice.outline, "preparationError": "实战文件写入失败。检查知识库和同名文件后重试。"}
            return True

        task = db.scalars(select(Task).where(
            Task.status == "queued", Task.project_id.not_in(select(ModelStudy.project_id)),
        ).order_by(Task.project_id).limit(1)).first()
        if task is None:
            return False

        project = db.get(Project, task.project_id)
        ordinary_lessons = [item for item in project.lessons if not is_practice(item.outline)]
        lesson = next((item for item in ordinary_lessons if not item.ready), None)
        try:
            if lesson is not None:
                prepare_course(db, folder, project, lesson)
        except (OSError, UnicodeError, InvalidLocation, LocationConflict):
            task.status = "failed"
            task.error = "课程文件写入失败。检查本地目录和同名文件后重试。"
            return True

        if all(item.ready for item in ordinary_lessons):
            task.status = "ready"
        return True


def prepare_one(folder: Path) -> bool:
    engine = open_db(folder)
    try:
        return run_one(engine, folder)
    finally:
        engine.dispose()


def dispatch_claim(send, finish, engine, claim, vault, transport):
    try:
        send(engine, claim, vault=vault, transport=transport)
    except TransportNotSent:
        finish(engine, claim, "dispatch_failed", None, None, utc_now(), sent=False)


def run_queue(
    engine: Engine, folder: Path, stopped: Event, *, vault: CredentialStore | None = None, transport: Transport = post_json,
) -> None:
    with closing(sqlite3.connect(folder / ".worker-lease.sqlite3", timeout=0)) as lease:
        while not stopped.is_set():
            try:
                lease.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                stopped.wait(0.35)
        if stopped.is_set():
            return
        settle_interrupted(engine, utc_now())
        settle_interrupted_assessments(engine, utc_now())
        settle_interrupted_answers(engine, utc_now())
        settle_interrupted_replies(engine, utc_now())
        settle_goal_authoring(engine, utc_now())
        settle_agent_authoring(engine, utc_now())
        settle_memory_inference(engine, utc_now())
        settle_external_memory(engine)
        settle_interrupted_note_ai(engine, utc_now())
        settle_note_checks(engine, utc_now())
        settle_recommendations(engine, utc_now())
        settle_classifications(engine, utc_now())
        settle_consolidations(engine, utc_now())
        with transaction(engine) as db:
            settle_open_charges(db, when_utc(utc_now()), direct=False)
        settle_prerequisites(engine, utc_now())
        settle_stage_reviews(engine, utc_now())
        backups = AutomaticBackups(BackupCatalog(folder))
        semantic = SemanticIndex(folder)
        handlers = {
            AnswerClaim: (dispatch_answer, complete_answer, ModelUse),
            ChatReplyClaim: (partial(dispatch_reply, workspace=folder), complete_reply, ChatModelCall),
            GoalAuthoringClaim: (dispatch_goal_authoring, complete_goal_authoring, GoalAuthoringJob),
            AgentAuthoringClaim: (dispatch_agent_authoring, complete_agent_authoring, AgentAuthoringJob),
            MemoryInferenceClaim: (dispatch_memory_inference, complete_memory_inference, MemoryInferenceJob),
            NoteAIClaim: (dispatch_note_ai, complete_note_ai, NoteSuggestion),
            RecommendationClaim: (dispatch_recommendation, complete_recommendation, GraphRecommendation),
            ClassificationClaim: (dispatch_classification, complete_classification, LibraryClassification),
            NoteCheckClaim: (dispatch_note_check, complete_note_check, NoteCheckBatch),
            ConsolidationClaim: (dispatch_consolidation, complete_consolidation, ModelUse),
            PrerequisiteClaim: (dispatch_prerequisite, complete_prerequisite, PrerequisiteCall),
            StageReviewClaim: (dispatch_stage_review, complete_stage_review, StageReviewCall),
            AssessmentClaim: (dispatch_assessment, complete_assessment, ModelUse),
            GenerationClaim: (dispatch, complete_call, ModelUse),
        }
        external_vault = ExternalMemoryCredentials()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS + 3) as pool:
            flights = {}
            external_sync = None
            next_external_sync = 0.0
            backup_check = None
            next_backup_check = 0.0
            next_note_scan = 0.0
            next_semantic_check = 0.0
            semantic_check = None
            while not stopped.is_set():
                instant = monotonic()
                if instant >= next_external_sync and (external_sync is None or external_sync.done()):
                    if external_sync is not None and external_sync.exception() is not None:
                        settle_external_memory(engine)
                    external_sync = pool.submit(sync_external_memory, engine, external_vault)
                    next_external_sync = instant + 2
                if instant >= next_semantic_check and (semantic_check is None or semantic_check.done()):
                    working = semantic_check is not None and semantic_check.result()
                    semantic_check = pool.submit(semantic.step, stopped)
                    next_semantic_check = instant + (0.35 if working else 2)
                if instant >= next_backup_check and (backup_check is None or backup_check.done()):
                    backup_check = pool.submit(backups.check, engine)
                    next_backup_check = instant + CHECK_INTERVAL
                if instant >= next_note_scan:
                    scan_checks(engine, folder, utc_now())
                    next_note_scan = instant + SCAN_INTERVAL
                for future, claim in list(flights.items()):
                    if not future.done():
                        continue
                    del flights[future]
                    if future.exception() is None:
                        continue
                    _, finish, model = handlers[type(claim)]
                    identity = claim.identity if isinstance(claim, GenerationClaim) else claim.id
                    with transaction(engine, write=False) as db:
                        call = db.get(model, identity)
                        status = call.status if call is not None and call.finished_at is None else None
                    if status in {"reserved", "sending"}:
                        sent = status == "sending"
                        reason = "uncertain" if sent else "dispatch_failed"
                        finish(engine, claim, reason, None, None, utc_now(), sent=sent)
                if (publish_prerequisite(engine, folder) or publish_one(engine, folder)
                        or publish_remediation(engine, folder) or publish_consolidation(engine, folder)):
                    continue
                clock = utc_now()
                claim = None
                if len(flights) < MAX_WORKERS:
                    claim = (reserve_answer(engine, clock) or reserve_reply(engine, clock)
                             or reserve_goal_authoring(engine, clock) or reserve_agent_authoring(engine, clock)
                             or reserve_memory_inference(engine, clock) or reserve_note_ai(engine, clock)
                             or reserve_prerequisite(engine, clock)
                             or reserve_assessment(engine, clock, remediation_jobs=False)
                             or reserve_stage_review(engine, clock) or reserve_call(engine, clock) or reserve_assessment(engine, clock, remediation_jobs=True)
                             or reserve_consolidation(engine, clock) or reserve_recommendation(engine, clock)
                             or reserve_classification(engine, clock)
                             or reserve_note_check(engine, clock))
                if claim:
                    send, finish, _ = handlers[type(claim)]
                    flights[pool.submit(dispatch_claim, send, finish, engine, claim, vault, transport)] = claim
                elif not run_one(engine, folder):
                    stopped.wait(0.35)


def main(arguments: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    args = parser.parse_args(arguments)
    folder = args.folder.resolve()
    prepare_windows_semantic_runtime()
    engine = open_db(folder)
    stopped = Event()

    watch_parent_disconnect(stopped.set)
    try:
        run_queue(engine, folder, stopped)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
