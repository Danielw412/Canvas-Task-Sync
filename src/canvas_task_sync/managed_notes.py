from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from canvas_task_sync.models import DraftTask

MANAGED_START = "--- canvas-task-sync managed v1 ---"
MANAGED_END = "--- end canvas-task-sync managed ---"
GENERATED_DESCRIPTION_START = "--- canvas-task-sync generated description v1 ---"
GENERATED_DESCRIPTION_END = "--- end canvas-task-sync generated description ---"
DUE_DATE_UNCERTAIN_NOTE = "Due date uncertain"


@dataclass(frozen=True)
class ParsedNotes:
    user_notes: str
    managed_block: str | None
    metadata: dict[str, Any] | None


def _managed_bounds(value: str) -> tuple[int, int] | None:
    start = value.find(MANAGED_START)
    if start < 0:
        return None
    end = value.find(MANAGED_END, start + len(MANAGED_START))
    if end < 0:
        return None
    return start, end + len(MANAGED_END)


def _generated_description_bounds(value: str) -> tuple[int, int] | None:
    start = value.find(GENERATED_DESCRIPTION_START)
    if start < 0:
        return None
    end = value.find(
        GENERATED_DESCRIPTION_END,
        start + len(GENERATED_DESCRIPTION_START),
    )
    if end < 0:
        return None
    return start, end + len(GENERATED_DESCRIPTION_END)


def parse_notes(notes: str | None) -> ParsedNotes:
    value = notes or ""
    bounds = _managed_bounds(value)
    if bounds is None:
        return ParsedNotes(user_notes=value.strip(), managed_block=None, metadata=None)
    start, end = bounds
    managed = value[start:end]
    outside = strip_managed_block(value)

    metadata: dict[str, Any] | None = None
    lines = managed.splitlines()
    if len(lines) >= 2 and lines[1].startswith("metadata: "):
        try:
            parsed = json.loads(lines[1].removeprefix("metadata: "))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metadata = parsed
    return ParsedNotes(user_notes=outside, managed_block=managed, metadata=metadata)


def has_managed_delimiter(notes: str | None) -> bool:
    value = notes or ""
    return MANAGED_START in value or MANAGED_END in value


def strip_managed_block(notes: str | None) -> str:
    """Remove one valid legacy block without rewriting the user's note text."""
    value = notes or ""
    bounds = _managed_bounds(value)
    if bounds is None:
        return value
    start, end = bounds
    before = value[:start]
    after = value[end:]
    if before.endswith("\n\n") and not after:
        before = before[:-2]
    elif not before and after.startswith("\n\n"):
        after = after[2:]
    return f"{before}{after}"


def _safe_managed_text(value: str) -> str:
    return value.replace(MANAGED_START, "[managed marker]").replace(
        MANAGED_END, "[end managed marker]"
    )


def build_managed_block(draft: DraftTask, logical_id: str) -> str:
    metadata = {
        "logical_id": logical_id,
        "course_id": draft.course_id,
        "source_key": draft.source_key,
        "source_url": draft.source_url,
        "source_anchor": draft.source_anchor,
        "ordinal": draft.ordinal,
        "fingerprint": draft.fingerprint,
        "source_text": draft.source_text,
        "title": draft.title,
        "due_date": draft.due_date.isoformat() if draft.due_date else None,
    }
    due = draft.due_date.isoformat() if draft.due_date else "none"
    details = _safe_managed_text(draft.details.strip()) or "(none)"
    evidence = _safe_managed_text(draft.source_text.strip()) or "(none)"
    encoded_metadata = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "\n".join(
        [
            MANAGED_START,
            f"metadata: {encoded_metadata}",
            f"Due: {due}",
            f"Deadline basis: {_safe_managed_text(draft.due_basis)}",
            f"Details: {details}",
            "Exact source evidence:",
            evidence,
            MANAGED_END,
        ]
    )


def merge_notes(user_notes: str | None, managed_block: str) -> str:
    value = user_notes or ""
    bounds = _managed_bounds(value)
    if bounds is not None:
        start, end = bounds
        return f"{value[:start]}{managed_block}{value[end:]}"
    if not value:
        return managed_block
    separator = "" if value.endswith("\n\n") else "\n\n"
    return f"{value}{separator}{managed_block}"


def strip_generated_description(notes: str | None) -> str:
    value = notes or ""
    bounds = _generated_description_bounds(value)
    if bounds is None:
        return value
    start, end = bounds
    before = value[:start]
    after = value[end:]
    if before.endswith("\n\n") and not after:
        before = before[:-2]
    elif not before and after.startswith("\n\n"):
        after = after[2:]
    return f"{before}{after}"


def build_generated_description(details: str, assignment_url: str | None) -> str:
    """Return only the human-readable task description.

    ``assignment_url`` remains in the signature for callers that already provide it,
    but links and sync ownership metadata do not belong in the Google Task notes.
    """

    del assignment_url
    return details.strip()


def merge_generated_description(notes: str | None, generated_block: str) -> str:
    value = notes or ""
    bounds = _generated_description_bounds(value)
    if bounds is not None:
        start, end = bounds
        if generated_block:
            return f"{value[:start]}{generated_block}{value[end:]}"
        return strip_generated_description(value)
    if not generated_block:
        return value
    if not value:
        return generated_block
    separator = "" if value.endswith("\n\n") else "\n\n"
    return f"{value}{separator}{generated_block}"


def compose_task_notes(notes: str | None, draft: DraftTask) -> str:
    """Return the current description without exposing internal sync markers."""

    del notes
    description = build_generated_description(draft.details, draft.assignment_url)
    return set_due_date_uncertain_note(description, uncertain=draft.due_uncertain)


def set_due_date_uncertain_note(notes: str | None, *, uncertain: bool) -> str:
    """Add or remove the reserved human-readable uncertainty line."""

    value = notes or ""
    for separator in ("\r\n\r\n", "\n\n"):
        suffix = f"{separator}{DUE_DATE_UNCERTAIN_NOTE}"
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    if value == DUE_DATE_UNCERTAIN_NOTE:
        value = ""
    elif DUE_DATE_UNCERTAIN_NOTE in value.splitlines():
        lines = value.splitlines(keepends=True)
        value = "".join(
            line for line in lines if line.rstrip("\r\n") != DUE_DATE_UNCERTAIN_NOTE
        )

    if not uncertain:
        return value
    if not value:
        return DUE_DATE_UNCERTAIN_NOTE
    return f"{value}\n\n{DUE_DATE_UNCERTAIN_NOTE}"
