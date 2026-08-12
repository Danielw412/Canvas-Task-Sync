from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date, timedelta

from canvas_task_sync.configuration import CourseSettings
from canvas_task_sync.gemini import normalized_text
from canvas_task_sync.models import (
    AgendaBlock,
    BlockRole,
    DraftTask,
    DueRelation,
    ExtractedTask,
    IgnoredItem,
    SourceCapture,
    TaskClassification,
    UncertainItem,
)

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
MONTH_PATTERN = "|".join(name.title() for name in MONTHS)
DATE_RANGE_PATTERN = re.compile(
    rf"\b(?P<month>{MONTH_PATTERN})\s+(?P<start>\d{{1,2}})\s*"
    rf"(?:[-\u2013\u2014]\s*(?:(?P<end_month>{MONTH_PATTERN})\s+)?"
    rf"(?P<end>\d{{1,2}}))?\s*,\s*(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
ISO_DATE_PATTERN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTH_DAY_PATTERN = re.compile(
    rf"\b(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:\s*,\s*(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)

DAY_ALIASES = {
    "m": 0,
    "mon": 0,
    "monday": 0,
    "t": 1,
    "tu": 1,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "w": 2,
    "wed": 2,
    "wednesday": 2,
    "th": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "f": 4,
    "fri": 4,
    "friday": 4,
    "sa": 5,
    "sat": 5,
    "saturday": 5,
    "su": 6,
    "sun": 6,
    "sunday": 6,
}

NON_ACTION_PATTERNS = (
    "memorial day",
    "holiday",
    "learning target",
    "objetivo de aprendizaje",
    "how it will be graded",
    "como se calificara",
    "grading narration",
)


class AgendaDateError(ValueError):
    pass


def parse_agenda_range(text: str) -> tuple[date, date] | None:
    match = DATE_RANGE_PATTERN.search(text)
    if not match:
        return None
    start_month = MONTHS[match.group("month").casefold()]
    year = int(match.group("year"))
    start = date(year, start_month, int(match.group("start")))
    end_month = MONTHS[(match.group("end_month") or match.group("month")).casefold()]
    end_day = int(match.group("end") or match.group("start"))
    end = date(year, end_month, end_day)
    if end < start:
        end = date(year + 1, end_month, end_day)
    return start, end


def find_agenda_range(capture: SourceCapture) -> tuple[date, date] | None:
    for block in capture.blocks:
        parsed = parse_agenda_range(block.text)
        if parsed:
            return parsed
    return parse_agenda_range(capture.transcript)


def _parse_day_label(label: str) -> tuple[int, int] | None:
    pieces = [piece for piece in re.split(r"\s*[-\u2013\u2014/]\s*", label) if piece]
    if not pieces:
        return None
    weekdays: list[int] = []
    for piece in pieces:
        normalized = re.sub(r"[^a-z]", "", piece.casefold())
        weekday = DAY_ALIASES.get(normalized)
        if weekday is None:
            return None
        weekdays.append(weekday)
    return weekdays[0], weekdays[-1]


def _on_or_after(value: date, weekday: int) -> date:
    return value + timedelta(days=(weekday - value.weekday()) % 7)


def row_date_ranges(capture: SourceCapture) -> dict[tuple[str, int], tuple[date, date]]:
    agenda_range = find_agenda_range(capture)
    if agenda_range is None:
        return {}
    agenda_start, _ = agenda_range

    rows: dict[tuple[str, int], str] = {}
    for block in capture.blocks:
        if block.row_index is None or not block.row_label:
            continue
        rows.setdefault((block.element_id, block.row_index), block.row_label)

    result: dict[tuple[str, int], tuple[date, date]] = {}
    element_ids = sorted({element_id for element_id, _ in rows})
    for element_id in element_ids:
        cursor = agenda_start
        element_rows = sorted(
            (
                (row_index, label)
                for (candidate_id, row_index), label in rows.items()
                if candidate_id == element_id
            ),
            key=lambda item: item[0],
        )
        for row_index, label in element_rows:
            day_range = _parse_day_label(label)
            if day_range is None:
                continue
            start_weekday, end_weekday = day_range
            row_start = _on_or_after(cursor, start_weekday)
            row_end = _on_or_after(row_start, end_weekday)
            if row_end < row_start:
                row_end += timedelta(days=7)
            result[(element_id, row_index)] = (row_start, row_end)
            cursor = row_end + timedelta(days=1)
    return result


def next_class_day(after: date, meeting_weekdays: list[int]) -> date:
    if not meeting_weekdays:
        raise AgendaDateError("The course has no configured meeting days.")
    candidate = after + timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in meeting_weekdays:
            return candidate
        candidate += timedelta(days=1)
    raise AgendaDateError("Could not find the next configured class day.")


def _explicit_date(task: ExtractedTask, source_date: date | None) -> date | None:
    if not task.explicit_due_date:
        return None
    try:
        parsed = date.fromisoformat(task.explicit_due_date)
    except ValueError:
        return None

    iso_match = ISO_DATE_PATTERN.search(task.source_text)
    if iso_match and date(*(int(value) for value in iso_match.groups())) == parsed:
        return parsed

    for match in MONTH_DAY_PATTERN.finditer(task.source_text):
        year = int(match.group("year")) if match.group("year") else None
        if year is None:
            if source_date is None:
                continue
            year = source_date.year
        evidence_date = date(
            year,
            MONTHS[match.group("month").casefold()],
            int(match.group("day")),
        )
        if evidence_date == parsed:
            return parsed
    return None


def _task_position(task: ExtractedTask, block: AgendaBlock) -> tuple[int, str, str]:
    haystack = normalized_text(block.text)
    needle = normalized_text(task.source_text)
    position = haystack.find(needle)
    if position < 0:
        position = len(haystack)
    return position, normalized_text(task.source_text), normalized_text(task.title_stem)


def _fingerprint(task: ExtractedTask) -> str:
    material = "|".join(
        [
            normalized_text(task.source_text),
            task.action_kind.value,
            normalized_text(task.title_stem),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _clean_title(prefix: str, title_stem: str) -> str:
    stem = " ".join(title_stem.split()).strip(" -:[]")
    existing_prefix = re.compile(rf"^\[{re.escape(prefix)}\]\s*", re.IGNORECASE)
    stem = existing_prefix.sub("", stem)
    return f"[{prefix}] {stem}"


def build_draft_tasks(
    *,
    course_id: str,
    course: CourseSettings,
    capture: SourceCapture,
    tasks: list[ExtractedTask],
    today: date,
    rebase_week: date | None = None,
) -> tuple[list[DraftTask], list[UncertainItem], list[IgnoredItem]]:
    blocks = {block.anchor: block for block in capture.blocks}
    dates = row_date_ranges(capture)
    agenda_range = find_agenda_range(capture)
    rebase_delta: timedelta | None = None
    if rebase_week is not None:
        if rebase_week.weekday() != 0:
            raise AgendaDateError("--test-rebase-week must be a Monday.")
        if agenda_range is None:
            raise AgendaDateError("Cannot rebase an agenda without a supported week/date heading.")
        rebase_delta = rebase_week - agenda_range[0]

    grouped: dict[str, list[ExtractedTask]] = defaultdict(list)
    seen_candidates: set[tuple[str, str, str]] = set()
    duplicate_ignored: list[IgnoredItem] = []
    for task in tasks:
        candidate_key = (
            task.source_anchor,
            normalized_text(task.source_text),
            task.action_kind.value,
        )
        if candidate_key in seen_candidates:
            duplicate_ignored.append(
                IgnoredItem(
                    title=task.title_stem,
                    evidence=task.source_text,
                    reason="Duplicate extraction candidate collapsed deterministically.",
                    source_anchor=task.source_anchor,
                )
            )
            continue
        seen_candidates.add(candidate_key)
        grouped[task.source_anchor].append(task)

    drafts: list[DraftTask] = []
    uncertain: list[UncertainItem] = []
    ignored: list[IgnoredItem] = duplicate_ignored

    for anchor in sorted(grouped):
        block = blocks.get(anchor)
        if block is None:
            for task in grouped[anchor]:
                uncertain.append(
                    UncertainItem(
                        title=task.title_stem,
                        evidence=task.source_text,
                        reason="The extracted source anchor no longer exists on the page.",
                        source_anchor=anchor,
                    )
                )
            continue

        ordered = sorted(grouped[anchor], key=lambda task: _task_position(task, block))
        row_range = None
        if block.row_index is not None:
            row_range = dates.get((block.element_id, block.row_index))

        for ordinal, task in enumerate(ordered):
            evidence_normalized = normalized_text(task.source_text)
            if any(pattern in evidence_normalized for pattern in NON_ACTION_PATTERNS):
                ignored.append(
                    IgnoredItem(
                        title=task.title_stem,
                        evidence=task.source_text,
                        reason="Holiday or non-action agenda content.",
                        source_anchor=anchor,
                    )
                )
                continue

            is_assignment = block.role == BlockRole.ASSIGNMENTS
            is_same_day_action = task.action_kind in course.source.extraction.same_day_action_kinds
            relation = task.due_relation
            if is_same_day_action:
                relation = DueRelation.SAME_DAY
            elif is_assignment and relation in {DueRelation.NONE, DueRelation.SAME_DAY}:
                relation = course.source.extraction.assignments_default_due

            effective_classification = (
                TaskClassification.HOMEWORK if is_assignment else task.classification
            )
            deadline_bearing_classwork = (
                relation == DueRelation.EXPLICIT_DATE or is_same_day_action
            )
            if (
                effective_classification == TaskClassification.CLASSWORK
                and not deadline_bearing_classwork
            ):
                ignored.append(
                    IgnoredItem(
                        title=task.title_stem,
                        evidence=task.source_text,
                        reason="Ordinary classwork has no supported deadline.",
                        source_anchor=anchor,
                    )
                )
                continue

            source_date = row_range[1] if row_range else None
            due_date: date | None = None
            due_basis = "No supported due date"
            if relation == DueRelation.EXPLICIT_DATE:
                due_date = _explicit_date(task, source_date)
                if due_date is None:
                    uncertain.append(
                        UncertainItem(
                            title=task.title_stem,
                            evidence=task.source_text,
                            reason=(
                                "The proposed explicit date is not present in the exact "
                                "source evidence."
                            ),
                            source_anchor=anchor,
                        )
                    )
                    continue
                due_basis = "Explicit date stated in source evidence"
            elif relation == DueRelation.SAME_DAY:
                if source_date is None:
                    uncertain.append(
                        UncertainItem(
                            title=task.title_stem,
                            evidence=task.source_text,
                            reason="Same-day action could not be tied to a dated agenda row.",
                            source_anchor=anchor,
                        )
                    )
                    continue
                due_date = source_date
                due_basis = f"{task.action_kind.value} action due on its agenda row"
            elif relation == DueRelation.NEXT_CLASS:
                if source_date is None:
                    uncertain.append(
                        UncertainItem(
                            title=task.title_stem,
                            evidence=task.source_text,
                            reason="Next-class work could not be tied to a dated agenda row.",
                            source_anchor=anchor,
                        )
                    )
                    continue
                due_date = next_class_day(source_date, course.meeting_weekdays)
                due_basis = "Assignments-column work due next configured class"
            elif effective_classification == TaskClassification.HOMEWORK:
                uncertain.append(
                    UncertainItem(
                        title=task.title_stem,
                        evidence=task.source_text,
                        reason="Homework has no supported due-date relation.",
                        source_anchor=anchor,
                    )
                )
                continue

            if rebase_delta is not None:
                if source_date is not None:
                    source_date += rebase_delta
                if due_date is not None:
                    due_date += rebase_delta
                due_basis = f"{due_basis}; test week rebased"

            drafts.append(
                DraftTask(
                    course_id=course_id,
                    source_key=capture.source_key,
                    source_url=capture.source_url,
                    source_anchor=anchor,
                    source_text=task.source_text,
                    ordinal=ordinal,
                    title=_clean_title(course.prefix, task.title_stem),
                    details=task.details,
                    classification=effective_classification,
                    action_kind=task.action_kind,
                    due_date=due_date,
                    due_basis=due_basis,
                    source_date=source_date,
                    historical=bool(due_date and due_date < today),
                    fingerprint=_fingerprint(task),
                )
            )

    return drafts, uncertain, ignored
