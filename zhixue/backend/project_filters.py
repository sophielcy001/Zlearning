from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import and_, func, or_, select

from .db import ProjectTag


Status = Literal["active", "paused", "done"]
Priority = Literal["high", "medium", "low"]
SortKey = Literal["learned", "updated", "created", "name", "progress", "due", "priority"]
STATUSES = ("active", "paused", "done")
TagName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
Percent = Annotated[int, Field(strict=True, ge=0, le=100)]
Page = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StatusFilter(Input):
    field: Literal["status"]
    op: Literal["in", "not_in"]
    values: list[Status] = Field(min_length=1, max_length=3)


class TagFilter(Input):
    field: Literal["tag"]
    op: Literal["in", "not_in"]
    values: list[TagName] = Field(min_length=1, max_length=30)


class ReviewFilter(Input):
    field: Literal["review"]
    op: Literal["due", "not_due"]


class PriorityFilter(Input):
    field: Literal["priority"]
    op: Literal["is"]
    value: Priority | None


class ProgressFilter(Input):
    field: Literal["progress"]
    op: Literal["lt", "gte"]
    value: Percent


class ProgressRange(Input):
    field: Literal["progress"]
    op: Literal["between"]
    low: Percent
    high: Percent

    @model_validator(mode="after")
    def validate_order(self):
        if self.low > self.high:
            raise ValueError("进度下限不能超过上限。")
        return self


class RecentFilter(Input):
    field: Literal["learned", "updated"]
    op: Literal["recent"]
    days: Annotated[int, Field(strict=True, ge=1, le=36500)]


class DateRange(Input):
    field: Literal["learned", "updated"]
    op: Literal["between"]
    fromDate: date
    toDate: date

    @model_validator(mode="after")
    def validate_order(self):
        if self.fromDate > self.toDate:
            raise ValueError("开始日期不能晚于结束日期。")
        return self


class NoTag(Input):
    field: Literal["tag"]
    op: Literal["none"]


class NoProgress(Input):
    field: Literal["progress"]
    op: Literal["none"]


class NeverLearned(Input):
    field: Literal["learned"]
    op: Literal["never"]


Condition = StatusFilter | TagFilter | ReviewFilter | PriorityFilter | ProgressFilter | ProgressRange | RecentFilter | DateRange | NoTag | NoProgress | NeverLearned


class Order(Input):
    key: SortKey = "learned"
    direction: Literal["asc", "desc"] = "desc"


class Column(Input):
    key: Status
    visible: bool = True


class ViewDefinition(Input):
    layout: Literal["board", "table", "cards"] = "board"
    search: str = Field(default="", max_length=500)
    combinator: Literal["and", "or"] = "and"
    filters: list[Condition] = Field(default_factory=list, max_length=30)
    sort: Order = Field(default_factory=Order)
    columns: list[Column] = Field(default_factory=lambda: [Column(key=key) for key in STATUSES], min_length=3, max_length=3)
    boardSize: Literal[3, 5, 10] = 3
    pageSize: Literal[12, 24, 48] = 12

    @model_validator(mode="after")
    def validate_columns(self):
        if {column.key for column in self.columns} != set(STATUSES) or not any(column.visible for column in self.columns):
            raise ValueError("请保留每个状态的分组设置，且至少显示一列。")
        return self


class BoardQuery(Input):
    view: ViewDefinition = Field(default_factory=ViewDefinition)
    pages: dict[Status | Literal["all"], Page] = Field(default_factory=dict)


class ViewInput(Input):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    definition: ViewDefinition


class MetadataInput(Input):
    tags: list[TagName] = Field(max_length=30)
    priority: Priority | None


def filter_expression(columns, condition: Condition, day: date):
    if isinstance(condition, StatusFilter):
        includes = columns.status.in_(condition.values)
        return includes if condition.op == "in" else ~includes
    if isinstance(condition, (TagFilter, NoTag)):
        tags = select(ProjectTag.key).where(ProjectTag.project_id == columns.id)
        if isinstance(condition, NoTag):
            return ~tags.exists()
        includes = tags.where(ProjectTag.key.in_([value.casefold() for value in condition.values])).exists()
        return includes if condition.op == "in" else ~includes
    if isinstance(condition, ReviewFilter):
        return columns.due > 0 if condition.op == "due" else columns.due == 0
    if isinstance(condition, PriorityFilter):
        return columns.priority == condition.value
    if isinstance(condition, NoProgress):
        return columns.progress.is_(None)
    if isinstance(condition, ProgressRange):
        return columns.progress.between(condition.low, condition.high)
    if isinstance(condition, ProgressFilter):
        return columns.progress < condition.value if condition.op == "lt" else columns.progress >= condition.value
    if isinstance(condition, NeverLearned):
        return columns.last_learned_at.is_(None)

    date_column = func.date(columns.last_learned_at if condition.field == "learned" else columns.updated_at)
    if isinstance(condition, RecentFilter):
        start = day.fromordinal(max(1, day.toordinal() - condition.days + 1))
        return date_column.between(start, day)
    return date_column.between(condition.fromDate, condition.toDate)


def match_projects(metrics, view: ViewDefinition, day: date):
    columns = metrics.c
    query = select(metrics)
    search = view.search.strip().casefold()
    if search:
        query = query.where(or_(func.instr(func.unicode_fold(columns.topic), search) > 0,
                                func.instr(func.unicode_fold(columns.outcome), search) > 0))
    if view.filters:
        combine = and_ if view.combinator == "and" else or_
        query = query.where(combine(*(filter_expression(columns, condition, day) for condition in view.filters)))
    return query.cte("matches")


def project_order(columns, sort: Order):
    values = {
        "learned": columns.last_learned_at, "updated": columns.updated_at,
        "created": columns.created_at, "name": func.unicode_fold(columns.topic),
        "progress": columns.progress, "due": columns.due, "priority": columns.priority_rank,
    }
    value = values[sort.key]
    direction = value.desc() if sort.direction == "desc" else value.asc()
    return direction.nulls_last(), columns.id
