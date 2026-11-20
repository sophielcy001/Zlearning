from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import Integer, and_, case, cast, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .content import lesson_course
from .assessment import UNSETTLED
from .db import (
    AssessmentObjective, AssessmentRound, Lesson, ModelStudy, Project, ProjectTag, ProjectView, Review, RouteVersion, Task, transaction,
)
from .project_activity import touch_project
from .project_filters import BoardQuery, MetadataInput, STATUSES, ViewInput, match_projects, project_order


def eligible_lesson():
    return and_(Lesson.ready.is_(True), Lesson.learned_on.is_not(None))


def reading_order():
    return func.coalesce(Lesson.reading_order, Lesson.position)


def required_lesson():
    return func.coalesce(Lesson.outline["required"].as_boolean(), True)


def ordinary_lesson():
    return func.coalesce(Lesson.outline["kind"].as_string(), "") != "practice"


def scheduled_lesson():
    return ordinary_lesson() | required_lesson()


def lesson_counts():
    required = required_lesson()
    ordinary = ordinary_lesson()
    next_lesson = Lesson.learned_on.is_(None)
    return select(
        Lesson.project_id, func.count().filter(ordinary).label("total"),
        func.count(Lesson.learned_on).filter(ordinary).label("learned"),
        func.count().filter(required).label("required_total"),
        func.count(Lesson.learned_on).filter(required).label("required_learned"),
        func.min(reading_order()).filter(next_lesson & ordinary).label("next_position"),
        func.min(reading_order()).filter(next_lesson & required).label("next_required"),
        func.count().filter(ordinary & Lesson.ready.is_(True)).label("ready"),
    ).group_by(Lesson.project_id).subquery()


def project_metrics(day: date):
    lessons = lesson_counts()
    scheduled = Review.due_on.is_not(None)
    waiting = select(AssessmentRound.id).where(
        AssessmentRound.lesson_id == Review.lesson_id, AssessmentRound.key == Review.key,
        AssessmentRound.mode == "formal", AssessmentRound.state.in_(UNSETTLED),
    ).correlate(Review).exists()
    held = select(AssessmentObjective.key).where(
        AssessmentObjective.lesson_id == Review.lesson_id, AssessmentObjective.key == Review.key, AssessmentObjective.held.is_(True),
    ).correlate(Review).exists()
    available = scheduled & eligible_lesson() & ~waiting & ~held
    reviews = select(
        Lesson.project_id, func.count().label("total"),
        func.count().filter(scheduled).label("scheduled"),
        func.count().filter(available).label("available"),
        func.count().filter(available & (Review.due_on <= day)).label("due"),
    ).join(Review, Lesson.id == Review.lesson_id).group_by(Lesson.project_id).subquery()

    has_required = lessons.c.required_total > 0
    total = case((has_required, lessons.c.required_total), else_=func.coalesce(lessons.c.total, 0))
    learned = case((has_required, lessons.c.required_learned), else_=func.coalesce(lessons.c.learned, 0))
    next_position = case((has_required, lessons.c.next_required), else_=lessons.c.next_position)
    due = case((Project.paused.is_(True), 0), else_=func.coalesce(reviews.c.due, 0))
    return select(
        Project.id, Project.topic, Project.outcome, Project.sample, Project.created_on, Project.priority,
        func.coalesce(Project.created_at, Project.created_on).label("created_at"),
        func.coalesce(Project.updated_at, Project.created_on).label("updated_at"),
        Project.last_learned_at, total.label("total"), learned.label("learned"), due.label("due"),
        cast(func.round(100.0 * learned / func.nullif(total, 0)), Integer).label("progress"),
        case((Project.paused.is_(True), "paused"), (and_(total > 0, learned == total), "done"), else_="active").label("status"),
        case((reviews.c.total.is_(None), "none"), (reviews.c.scheduled == 0, "complete"),
             (and_(reviews.c.scheduled > 0, Project.paused.is_(True)), "paused"), (due > 0, "due"),
             (reviews.c.available > 0, "upcoming"), else_="paused").label("review_status"),
        case({"high": 3, "medium": 2, "low": 1}, value=Project.priority).label("priority_rank"),
        Lesson.id.label("next_id"), Lesson.ready.label("next_ready"), Lesson.position.label("next_position"),
        Lesson.outline.label("next_outline"),
    ).select_from(Project).outerjoin(lessons, lessons.c.project_id == Project.id).outerjoin(
        reviews, reviews.c.project_id == Project.id,
    ).outerjoin(Lesson, and_(Lesson.project_id == Project.id, reading_order() == next_position)).cte("project_metrics")


def current_phases(db: Session, rows) -> dict[str, str]:
    targets = {row.id: row.next_id for row in rows if row.next_id is not None}
    if not targets:
        return {}
    versions = select(RouteVersion.project_id, func.max(RouteVersion.version).label("version")).where(
        RouteVersion.project_id.in_(targets),
    ).group_by(RouteVersion.project_id).subquery()
    routes = db.execute(select(RouteVersion.project_id, RouteVersion.definition).join(
        versions, and_(versions.c.project_id == RouteVersion.project_id, versions.c.version == RouteVersion.version),
    ))
    phases = {}
    for identity, definition in routes:
        node = next((item for item in definition["nodes"] if item["id"] == targets[identity]), None)
        if node is not None:
            phases[identity] = node["phase"]
    return phases


def next_lesson(row, phase: str | None) -> dict | None:
    if row.next_id is None:
        return None
    course = lesson_course(row.sample, row.next_position, row.next_outline)
    return {"id": row.next_id, "title": course.title, "phase": phase if phase is not None else course.phase,
            "ready": row.next_ready}


def tags_for(db: Session, project_ids: list[str]) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = defaultdict(list)
    if project_ids:
        for identity, name in db.execute(
            select(ProjectTag.project_id, ProjectTag.name).where(ProjectTag.project_id.in_(project_ids)).order_by(ProjectTag.key),
        ):
            tags[identity].append(name)
    return tags


def row_date(value: datetime | str | None) -> str | None:
    return str(value)[:10] if value is not None else None


def describe_project(row, tags: list[str], phase: str | None) -> dict:
    return {
        "id": row.id, "topic": row.topic, "outcome": row.outcome, "status": row.status, "priority": row.priority,
        "progress": row.progress, "learned": row.learned, "lessonCount": row.total, "nextLesson": next_lesson(row, phase),
        "due": row.due, "reviewStatus": row.review_status, "tags": tags, "createdOn": row.created_on,
        "updatedOn": row_date(row.updated_at), "lastLearnedOn": row_date(row.last_learned_at),
    }


def get_board_stamp(db: Session, day: date) -> tuple[int, str]:
    count, updated, learned = db.execute(select(func.count(), func.max(Project.updated_at), func.max(Project.last_learned_at))).one()
    return count, f"{day}/{count}/{updated}/{learned}"


def page_bounds(total: int, size: int, requested: int) -> tuple[int, int, int]:
    last = (total + size - 1) // size
    page = min(requested, max(1, last))
    return page, last, (page - 1) * size


def board_page(db: Session, day: date, payload: BoardQuery) -> dict:
    view = payload.view
    matches = match_projects(project_metrics(day), view, day)
    counts = dict.fromkeys(STATUSES, 0)
    counts.update(dict(db.execute(select(matches.c.status, func.count()).group_by(matches.c.status)).tuples().all()))
    total = sum(counts.values())
    project_total, stamp = get_board_stamp(db, day)
    ordering = project_order(matches.c, view.sort)
    pages = {}
    rows = {}
    if view.layout == "board":
        displayed = {column.key: counts[column.key] for column in view.columns if column.visible}
        size = view.boardSize
    else:
        displayed = {"all": total}
        size = view.pageSize
    for key, count in displayed.items():
        page, last, offset = page_bounds(count, size, payload.pages.get(key, 1))
        query = select(matches) if key == "all" else select(matches).where(matches.c.status == key)
        rows[key] = db.execute(query.order_by(*ordering).limit(size).offset(offset)).all() if count else []
        pages[key] = {"page": page, "lastPage": last, "total": count,
                      "start": offset + 1 if count else 0, "end": offset + len(rows[key])}
    displayed_rows = [row for group in rows.values() for row in group]
    tags = tags_for(db, [row.id for row in displayed_rows])
    phases = current_phases(db, displayed_rows)
    for key, entries in rows.items():
        pages[key]["items"] = [describe_project(row, tags.get(row.id, []), phases.get(row.id)) for row in entries]
    return {"total": total, "hasProjects": project_total > 0, "counts": counts, "pages": pages, "stamp": stamp}


def app_project_summary(db: Session, day: date) -> dict:
    projects = select(Project.id, Project.topic).order_by(Project.created_at.desc().nulls_last(), Project.created_on.desc(), Project.id)
    sidebar = [dict(row) for row in db.execute(projects.limit(4)).mappings()]
    metrics = project_metrics(day)
    continuation = db.execute(
        select(metrics).where(metrics.c.status == "active").order_by(metrics.c.created_at.desc(), metrics.c.id).limit(1),
    ).first()
    total_due = db.scalar(select(func.coalesce(func.sum(metrics.c.due), 0)))
    task_count, queued = db.execute(select(func.count(), func.count().filter(Task.status == "queued")).where(
        Task.status.not_in(("ready", "deleted")),
    )).one()
    _, stamp = get_board_stamp(db, day)
    phases = current_phases(db, [continuation]) if continuation else {}
    return {
        "recentProjects": sidebar,
        "continuation": describe_project(continuation, [], phases.get(continuation.id)) if continuation else None,
        "dueCount": total_due, "taskCount": task_count, "queuedCount": queued, "boardStamp": stamp,
    }


def describe_view(view: ProjectView) -> dict:
    return {"id": view.id, "name": view.name, "definition": view.definition}


def project_board_router(engine: Engine, day: Callable[[Session], date], moment: Callable[[Session], datetime]) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/project-board/query")
    def query_projects(payload: BoardQuery):
        with transaction(engine, write=False) as db:
            return board_page(db, day(db), payload)

    @router.get("/projects/options")
    def project_options():
        with transaction(engine, write=False) as db:
            return [dict(row) for row in db.execute(
                select(Project.id, Project.topic).order_by(func.unicode_fold(Project.topic), Project.id),
            ).mappings()]

    @router.get("/tasks")
    def task_list(page: int = Query(default=1, ge=1, le=2**31 - 1)):
        size = 20
        active = Task.status.not_in(("ready", "deleted"))
        with transaction(engine, write=False) as db:
            total = db.scalar(select(func.count()).select_from(Task).where(active))
            page, last, offset = page_bounds(total, size, page)
            lessons = lesson_counts()
            rows = db.execute(select(
                Project.id, Project.topic, Project.paused, Task.status, Task.error,
                case((ModelStudy.project_id.is_not(None), "model"), else_="local").label("generationMode"),
                func.coalesce(lessons.c.ready, 0).label("completed"), func.coalesce(lessons.c.total, 0).label("total"),
            ).join(Task, Task.project_id == Project.id).outerjoin(ModelStudy, ModelStudy.project_id == Project.id)
                .outerjoin(lessons, lessons.c.project_id == Project.id).where(active)
                .order_by(Project.created_at.desc().nulls_last(), Project.created_on.desc(), Project.id).limit(size).offset(offset)).mappings()
            return {"total": total, "page": page, "lastPage": last, "items": [dict(row) for row in rows]}

    @router.get("/project-tags")
    def tag_options():
        with transaction(engine, write=False) as db:
            return db.scalars(select(func.min(ProjectTag.name)).group_by(ProjectTag.key).order_by(ProjectTag.key)).all()

    @router.put("/projects/{identity}/metadata")
    def update_metadata(identity: UUID, payload: MetadataInput):
        with transaction(engine) as db:
            project = db.get(Project, str(identity))
            if project is None:
                raise HTTPException(404, "找不到这个项目。")
            tags = {}
            for name in payload.tags:
                tags.setdefault(name.casefold(), name)
            existing = tags_for(db, [project.id]).get(project.id, [])
            ordered = [tags[key] for key in sorted(tags)]
            if existing != ordered:
                db.execute(delete(ProjectTag).where(ProjectTag.project_id == project.id))
                db.add_all(ProjectTag(project_id=project.id, key=key, name=name) for key, name in tags.items())
            if project.priority != payload.priority or existing != ordered:
                project.priority = payload.priority
                touch_project(project, moment(db))
            return {"tags": ordered, "priority": project.priority}

    @router.get("/project-views")
    def saved_views():
        with transaction(engine, write=False) as db:
            return [describe_view(view) for view in db.scalars(select(ProjectView).order_by(func.unicode_fold(ProjectView.name), ProjectView.id))]

    def write_view(payload: ViewInput, identity: UUID | None = None) -> dict:
        with transaction(engine) as db:
            view = db.get(ProjectView, str(identity)) if identity else ProjectView(id=str(uuid4()))
            if view is None:
                raise HTTPException(404, "找不到这个视图。")
            duplicate = select(ProjectView.id).where(
                func.unicode_fold(ProjectView.name) == payload.name.casefold(), ProjectView.id != view.id,
            )
            if db.scalar(duplicate):
                raise HTTPException(409, "视图名称已存在，请换一个名称。")
            view.name = payload.name
            view.definition = payload.definition.model_dump(mode="json")
            db.add(view)
            return describe_view(view)

    @router.post("/project-views", status_code=201)
    def save_view(payload: ViewInput):
        return write_view(payload)

    @router.put("/project-views/{identity}")
    def update_view(identity: UUID, payload: ViewInput):
        return write_view(payload, identity)

    @router.delete("/project-views/{identity}", status_code=204)
    def delete_view(identity: UUID):
        with transaction(engine) as db:
            view = db.get(ProjectView, str(identity))
            if view is not None:
                db.delete(view)
        return Response(status_code=204)

    return router
