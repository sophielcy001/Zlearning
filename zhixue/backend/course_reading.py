from pathlib import Path
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .db import Lesson, LessonReadingPosition
from .file_locations import LocationTracker
from .library import local_path


class ReadingPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block: Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    offset: Annotated[int, Field(strict=True, ge=0, le=10_000_000)]
    excerpt: Annotated[str, StringConstraints(min_length=1, max_length=72)]
    excerptOffset: Annotated[int, Field(strict=True, ge=0, le=48)]

    @model_validator(mode="after")
    def valid_position(self):
        length = len(self.excerpt.encode("utf-16-le")) // 2
        if self.excerptOffset >= length or self.excerptOffset > self.offset:
            raise ValueError("阅读位置必须在原文片段内。")
        return self


class ReadingPositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    point: ReadingPoint | None


def course_document(tracker: LocationTracker, folder: Path, lesson: Lesson) -> tuple[str | None, str | None]:
    if not lesson.ready:
        return None, None
    source = tracker.resolve(f"course-{lesson.id}", local_path(folder, "courses", lesson.id))
    if source.status != "ready":
        return None, source.message or "找不到本节课程的原文件。"
    try:
        with source.path.open(encoding="utf-8", newline="") as handle:
            return handle.read(), None
    except (OSError, UnicodeError) as error:
        raise HTTPException(409, "本地文件无法读取，请检查文件后重试。") from error


def position_view(saved: LessonReadingPosition | None) -> dict | None:
    return {"revision": saved.revision, "point": saved.point} if saved is not None else None
