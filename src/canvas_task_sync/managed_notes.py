from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from canvas_task_sync.models import DraftTask

MANAGED_START = "--- canvas-task-sync managed v1 ---"
MANAGED_END = "--- end canvas-task-sync managed ---"


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


def parse_notes(notes: str | None) -> ParsedNotes:
    value = notes or ""
    bounds = _managed_bounds(value)
    if bounds is None:
        return ParsedNotes(user_notes=value.strip(), managed_block=None, metadata=None)
    start, end = bounds
    managed = value[start:end]
    outside = f"{value[:start].rstrip()}\n{value[end:].lstrip()}".strip()

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
