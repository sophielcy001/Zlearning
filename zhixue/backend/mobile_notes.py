from dataclasses import dataclass
from difflib import SequenceMatcher

from .notes import MAX_NOTE_CHARS


@dataclass(frozen=True)
class Edit:
    start: int
    stop: int
    replacement: tuple[str, ...]


def edits(base: list[str], changed: list[str]) -> set[Edit]:
    return {
        Edit(start, stop, tuple(changed[left:right]))
        for tag, start, stop, left, right in SequenceMatcher(None, base, changed).get_opcodes()
        if tag != "equal"
    }


def apply_edits(base: list[str], desktop: list[str], phone: list[str]) -> str | None:
    changes = edits(base, desktop) | edits(base, phone)
    parts = []
    endpoint = 0
    inserted_at = None
    for edit in sorted(changes, key=lambda change: (change.start, change.stop)):
        if edit.start < endpoint or edit.start == edit.stop == inserted_at:
            return None
        parts.extend(base[endpoint:edit.start])
        parts.extend(edit.replacement)
        endpoint = edit.stop
        inserted_at = edit.start if edit.start == edit.stop else None
    parts.extend(base[endpoint:])
    result = "".join(parts)
    return result if len(result) <= MAX_NOTE_CHARS else None


def merge_note(base: str, desktop: str, phone: str) -> str | None:
    if desktop == base or phone == desktop:
        return phone
    if phone == base:
        return desktop
    return apply_edits(base.splitlines(keepends=True), desktop.splitlines(keepends=True), phone.splitlines(keepends=True))
