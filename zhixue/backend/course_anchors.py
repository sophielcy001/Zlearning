from collections import defaultdict
import re
from typing import Annotated

from pydantic import ConfigDict, Field
from sqlalchemy import select, text

from .course_revision import section_identity
from .db import CourseAnchorLink, CourseVersion
from .file_locations import InvalidLocation, LocationConflict, LocationTracker
from .library import documents
from .model_study_input import Input
from .notes import NoteFileError, note_snapshot
from .wiki_links import markdown


SQLITE_MAX = 2**63 - 1
EXCERPT = re.compile(r"readingVersion=([1-9][0-9]{0,18})&section=([0-9]{1,19})"
                     r"&selectionStart=([0-9]{1,19})&selectionEnd=([1-9][0-9]{0,18})")


class Anchor(Input):
    model_config = ConfigDict(frozen=True)
    version: Annotated[int, Field(strict=True, ge=1, le=SQLITE_MAX)]
    section: Annotated[int, Field(strict=True, ge=0, le=SQLITE_MAX)]
    start: Annotated[int, Field(strict=True, ge=0, le=SQLITE_MAX)]
    end: Annotated[int, Field(strict=True, ge=1, le=SQLITE_MAX)]

    def key(self, lesson):
        return lesson, self.version, self.section, self.start, self.end


def passage(course, anchor):
    if course is None or not 0 <= anchor.start < anchor.end:
        return None
    for section in course.sections:
        if section["index"] == anchor.section and anchor.end <= len(section["text"]):
            return section
    return None


def relocated_anchor(old, new, anchor):
    identity = section_identity(old)
    unchanged = [section for section in new.sections if section["state"] == old["state"] and section_identity(section) == identity]
    if len(unchanged) == 1 and unchanged[0]["index"] == anchor.section:
        return anchor.model_dump(exclude={"version"})
    quote = old["text"][anchor.start:anchor.end]
    matches = []
    for section in new.sections:
        text = section["text"]
        if section["state"] != old["state"] or (len(quote) < 8 and text != quote):
            continue
        start = text.find(quote)
        if start < 0:
            continue
        if text.find(quote, start + 1) >= 0:
            return None
        matches.append({"section": section["index"], "start": start, "end": start + len(quote)})
    return matches[0] if len(matches) == 1 else None


def target_anchor(link):
    return Anchor(version=link.target_version, section=link.target_section, start=link.target_start, end=link.target_end)


def resolve_anchor(db, old, current, source, section):
    saved = db.get(CourseAnchorLink, source.key(old.lesson_id))
    if saved and saved.target_version <= current.version:
        target = target_anchor(saved)
        if target.version == current.version:
            result = target.model_dump(exclude={"version"}) if passage(current, target) is not None else None
        else:
            chosen = db.get(CourseVersion, (old.lesson_id, target.version))
            chosen_section = passage(chosen, target)
            result = relocated_anchor(chosen_section, current, target) if chosen_section is not None else None
        return {"target": result, "revision": True, "manual": True}
    # Keep links to ordinary generated and consolidated versions on their saved versions.
    if not any(item.get("revisionId") for item in current.sections):
        return {"target": None, "revision": False}
    return {"target": relocated_anchor(section, current, source), "revision": True}


def excerpt_anchors(content, lesson):
    prefix = f"#/lessons/{lesson}?"
    for token in markdown.parse(content.removeprefix("\ufeff")):
        for child in token.children or ():
            href = child.attrGet("href") if child.type == "link_open" else None
            match = EXCERPT.fullmatch(href[len(prefix):]) if href and href.startswith(prefix) else None
            if match:
                version, section, start, end = map(int, match.groups())
                if start < end and max(version, section, end) <= SQLITE_MAX:
                    yield Anchor(version=version, section=section, start=start, end=end)


def pending_anchors(db, folder, latest):
    if latest is None:
        return {"version": None, "items": [], "unavailable": 0}
    if latest.version == 1:
        return {"version": latest.version, "items": [], "unavailable": 0}
    versions = {item.version: item for item in db.scalars(select(CourseVersion).where(
        CourseVersion.lesson_id == latest.lesson_id, CourseVersion.version < latest.version,
    ))}
    mappings = defaultdict(list)
    unavailable = 0
    tracker = LocationTracker(db, folder)
    known = set(db.scalars(text("SELECT id FROM library_index_documents WHERE kind='note'"))) | tracker.locations.keys()
    for item in documents(db, folder, known):
        if item.kind != "note":
            continue
        try:
            location = tracker.resolve(item.id, item.path)
            if location.status != "ready":
                if location.status not in {"trashed", "purged"}:
                    unavailable += 1
                continue
            snapshot = note_snapshot(location.path)
        except (NoteFileError, OSError, InvalidLocation, LocationConflict):
            unavailable += 1
            continue
        if snapshot:
            for anchor in set(excerpt_anchors(snapshot.content.decode("utf-8"), latest.lesson_id)):
                if anchor.version in versions:
                    mappings[anchor].append({"id": item.id.removeprefix("note-"), "title": item.name})
    pending = []
    ordered = sorted(mappings, key=lambda point: (point.version, point.section, point.start, point.end))
    for source in ordered:
        start = versions[source.version]
        section = passage(start, source)
        if section is None:
            continue
        result = resolve_anchor(db, start, latest, source, section)
        if result["revision"] and result["target"] is None:
            pending.append({
                "source": source.model_dump(), "title": section["title"], "quote": section["text"][source.start:source.end],
                "notes": sorted(mappings[source], key=lambda item: (item["title"], item["id"])),
            })
    return {"version": latest.version, "items": pending, "unavailable": unavailable}
