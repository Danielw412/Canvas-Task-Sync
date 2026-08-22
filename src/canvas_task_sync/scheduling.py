from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date, timedelta

from canvas_task_sync.configuration import CourseSettings
from canvas_task_sync.gemini import normalized_text, token_similarity
from canvas_task_sync.models import (
    AgendaBlock,
    BlockRole,
    DraftTask,
    DueRelation,
    ExtractedTask,
    IgnoredItem,
    SourceCapture,
    TaskClassification,
    TaskType,
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
NUMERIC_MONTH_DAY_PATTERN = re.compile(
    r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2}|\d{4}))?\b"
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
CLASSWORK_ONLY_PATTERNS = (
    "practice identifying hypotheses",
    "work through released ap frq",
    "work through released ap free response",
)
WEEKDAY_PATTERN = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
CANVAS_ASSIGNMENT_PATH_PATTERN = re.compile(r"/courses/\d+/assignments/\d+(?:/|$)")


class AgendaDateError(ValueError):
    pass


def _is_classwork_only(evidence_normalized: str) -> bool:
    return bool(
        any(pattern in evidence_normalized for pattern in CLASSWORK_ONLY_PATTERNS)
        or (
            "practice identifying" in evidence_normalized
            and "hypotheses" in evidence_normalized
        )
        or (
            "work through released ap" in evidence_normalized
            and ("frq" in evidence_normalized or "free response" in evidence_normalized)
        )
    )


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
    parsed = parse_agenda_range(capture.transcript)
    if parsed:
        return parsed

    selected_week = capture.selection.get("week_start")
    if isinstance(selected_week, str):
        try:
            start = date.fromisoformat(selected_week)
        except ValueError:
            pass
        else:
            return start, start + timedelta(days=6)
    return None


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


def _date_for_month_day(
    month: int,
    day: int,
    *,
    year: int | None,
    source_date: date | None,
) -> date | None:
    if year is not None:
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    if source_date is None:
        return None

    candidates: list[date] = []
    for candidate_year in (source_date.year - 1, source_date.year, source_date.year + 1):
        try:
            candidates.append(date(candidate_year, month, day))
        except ValueError:
            continue
    return min(candidates, key=lambda value: abs((value - source_date).days), default=None)


def _calendar_dates_in_text(text: str, source_date: date | None) -> set[date]:
    dates: set[date] = set()
    for match in ISO_DATE_PATTERN.finditer(text):
        try:
            dates.add(date(*(int(value) for value in match.groups())))
        except ValueError:
            continue
    for match in MONTH_DAY_PATTERN.finditer(text):
        parsed = _date_for_month_day(
            MONTHS[match.group("month").casefold()],
            int(match.group("day")),
            year=int(match.group("year")) if match.group("year") else None,
            source_date=source_date,
        )
        if parsed is not None:
            dates.add(parsed)
    for match in NUMERIC_MONTH_DAY_PATTERN.finditer(text):
        parsed = _date_for_month_day(
            int(match.group("month")),
            int(match.group("day")),
            year=int(match.group("year")) if match.group("year") else None,
            source_date=source_date,
        )
        if parsed is not None:
            dates.add(parsed)
    return dates


def _explicit_date(task: ExtractedTask, source_date: date | None) -> date | None:
    if not task.explicit_due_date:
        return None
    proposed_dates = _calendar_dates_in_text(task.explicit_due_date, source_date)
    evidence_dates = _calendar_dates_in_text(task.source_text, source_date)
    supported = proposed_dates & evidence_dates
    return min(supported) if len(supported) == 1 else None


def _explicit_weekday_date(
    source_text: str,
    row_range: tuple[date, date] | None,
) -> date | None:
    if row_range is None:
        return None
    matches = list(WEEKDAY_PATTERN.finditer(source_text))
    if not matches:
        return None
    weekday = DAY_ALIASES[matches[-1].group(1).casefold()]
    return _on_or_after(row_range[0], weekday)


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
            task.task_type.value,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_canvas_assignment_url(value: str) -> bool:
    return bool(CANVAS_ASSIGNMENT_PATH_PATTERN.search(value))


def _assignment_url(
    task: ExtractedTask,
    block: AgendaBlock,
    capture: SourceCapture,
) -> str | None:
    raw_links = block.metadata.get("assignment_links", [])
    links = [
        (str(link.get("url", "")), str(link.get("text", "")))
        for link in raw_links
        if isinstance(link, dict) and _is_canvas_assignment_url(str(link.get("url", "")))
    ]
    links = list(dict.fromkeys(links))
    if len(links) == 1:
        url, link_text = links[0]
        score = max(
            token_similarity(task.source_text, link_text),
            token_similarity(task.title_stem, link_text),
        )
        if score >= 0.45:
            return url
    if len(links) > 1:
        ranked = sorted(
            (
                (
                    max(
                        token_similarity(task.source_text, link_text),
                        token_similarity(task.title_stem, link_text),
                    ),
                    url,
                )
                for url, link_text in links
                if link_text.strip()
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if ranked:
            runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
            if ranked[0][0] >= 0.55 and ranked[0][0] - runner_up >= 0.10:
                return ranked[0][1]

    if (
        capture.source_metadata.get("canvas_kind") == "assignment"
        and _is_canvas_assignment_url(capture.source_url)
    ):
        return capture.source_url
    return None


def _clean_title(prefix: str, title_stem: str) -> str:
    stem = " ".join(title_stem.split()).strip(" -:[]")
    existing_prefix = re.compile(rf"^\[{re.escape(prefix)}\]\s*", re.IGNORECASE)
    stem = existing_prefix.sub("", stem)
    return f"[{prefix}] {stem}"


def _latest_contiguous_occurrence_date(
    task: ExtractedTask,
    block: AgendaBlock,
    *,
    blocks: list[AgendaBlock],
    dates: dict[tuple[str, int], tuple[date, date]],
    meeting_weekdays: list[int],
) -> date | None:
    """Return the end of the consecutive row run containing the extracted task.

    Gemini may collapse a repeated homework instruction to its first row. Exact atomic
    evidence lets application code recover all consecutive occurrences from the source.
    A missing configured class day breaks the run, so a later recurrence remains a
    separate assignment.
    """

    if block.row_index is None or block.role != BlockRole.ASSIGNMENTS:
        return None
    current_range = dates.get((block.element_id, block.row_index))
    needle = normalized_text(task.source_text)
    if current_range is None or len(needle.split()) < 3:
        return current_range[1] if current_range else None

    occurrences: set[tuple[date, date]] = set()
    for candidate in blocks:
        if (
            candidate.role != BlockRole.ASSIGNMENTS
            or candidate.element_id != block.element_id
            or candidate.row_index is None
            or (
                block.column_index is not None
                and candidate.column_index is not None
                and candidate.column_index != block.column_index
            )
        ):
            continue
        candidate_range = dates.get((candidate.element_id, candidate.row_index))
        if candidate_range and needle in normalized_text(candidate.text):
            occurrences.add(candidate_range)

    ordered = sorted(occurrences)
    try:
        current_index = ordered.index(current_range)
    except ValueError:
        return current_range[1]

    run_end = current_index
    while run_end + 1 < len(ordered):
        previous = ordered[run_end]
        following = ordered[run_end + 1]
        if next_class_day(previous[1], meeting_weekdays) != following[0]:
            break
        run_end += 1
    return ordered[run_end][1]


def _collapse_continuing_drafts(
    drafts: list[DraftTask],
) -> tuple[list[DraftTask], list[IgnoredItem]]:
    """Collapse duplicate candidates that resolve to one title and deadline."""

    selected: dict[tuple[str, date | None, str, str], DraftTask] = {}
    order: list[tuple[str, date | None, str, str]] = []
    ignored: list[IgnoredItem] = []
    for draft in drafts:
        key = (
            normalized_text(draft.title),
            draft.due_date,
            draft.task_type.value,
            draft.action_kind.value,
        )
        previous = selected.get(key)
        if previous is None:
            selected[key] = draft
            order.append(key)
            continue
        ranked = sorted(
            (previous, draft),
            key=lambda item: (item.source_date or date.min, item.source_anchor, item.ordinal),
        )
        discard, keep = ranked
        selected[key] = keep
        ignored.append(
            IgnoredItem(
                title=discard.title,
                evidence=discard.source_text,
                reason=(
                    "Repeated homework occurrence collapsed after scheduling from the latest "
                    "consecutive agenda row."
                ),
                source_anchor=discard.source_anchor,
            )
        )
    return [selected[key] for key in order], ignored


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
            f"{task.action_kind.value}:{task.task_type.value}",
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
            explicit_weekday = _explicit_weekday_date(task.source_text, row_range)
            relation = task.due_relation
            is_assessment = task.task_type in {TaskType.QUIZ, TaskType.TEST}
            if explicit_weekday is None and relation != DueRelation.EXPLICIT_DATE:
                if is_assessment or is_same_day_action:
                    relation = DueRelation.SAME_DAY
                elif is_assignment and relation in {DueRelation.NONE, DueRelation.SAME_DAY}:
                    relation = course.source.extraction.assignments_default_due

            effective_classification = (
                TaskClassification.HOMEWORK if is_assignment else task.classification
            )
            if block.role == BlockRole.LEARNING or _is_classwork_only(evidence_normalized):
                effective_classification = TaskClassification.CLASSWORK
            if (
                effective_classification == TaskClassification.HOMEWORK
                and relation == DueRelation.NONE
            ):
                relation = DueRelation.NEXT_CLASS
            deadline_bearing_classwork = (
                relation == DueRelation.EXPLICIT_DATE
                or is_same_day_action
                or is_assessment
                or explicit_weekday is not None
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
            if relation == DueRelation.NEXT_CLASS:
                latest_occurrence = _latest_contiguous_occurrence_date(
                    task,
                    block,
                    blocks=capture.blocks,
                    dates=dates,
                    meeting_weekdays=course.meeting_weekdays,
                )
                if latest_occurrence is not None and (
                    source_date is None or latest_occurrence > source_date
                ):
                    source_date = latest_occurrence
            due_date: date | None = None
            due_basis = "No supported due date"
            due_uncertain = False
            due_uncertain_reason: str | None = None
            if relation == DueRelation.EXPLICIT_DATE:
                due_date = _explicit_date(task, source_date)
                if due_date is None:
                    due_uncertain = True
                    due_uncertain_reason = (
                        "The proposed explicit date is not present in the exact source evidence."
                    )
                    due_basis = "Due date uncertain"
                else:
                    due_basis = "Explicit date stated in source evidence"
            elif explicit_weekday is not None:
                due_date = explicit_weekday
                due_basis = "Weekday explicitly stated in source evidence"
            elif relation == DueRelation.SAME_DAY:
                if source_date is None:
                    due_uncertain = True
                    due_uncertain_reason = (
                        "Same-day action could not be tied to a dated agenda row."
                    )
                    due_basis = "Due date uncertain"
                else:
                    due_date = source_date
                    due_basis = (
                        "Assessment scheduled on its agenda row"
                        if is_assessment
                        else f"{task.action_kind.value} action due on its agenda row"
                    )
            elif relation == DueRelation.NEXT_CLASS:
                if source_date is None:
                    due_uncertain = True
                    due_uncertain_reason = (
                        "Next-class work could not be tied to a dated agenda row."
                    )
                    due_basis = "Due date uncertain"
                else:
                    due_date = next_class_day(source_date, course.meeting_weekdays)
                    due_basis = "Work with no stated date due next configured school day"
                    if row_range and source_date > row_range[1]:
                        due_basis = (
                            "Repeated work due after its latest consecutive agenda occurrence"
                        )
            elif is_assessment:
                due_uncertain = True
                due_uncertain_reason = (
                    "Assessment could not be tied to an explicit date or dated agenda row."
                )
                due_basis = "Due date uncertain"
            elif effective_classification == TaskClassification.HOMEWORK:
                due_uncertain = True
                due_uncertain_reason = "Homework has no supported due-date relation."
                due_basis = "Due date uncertain"

            if rebase_delta is not None:
                if source_date is not None:
                    source_date += rebase_delta
                if due_date is not None:
                    due_date += rebase_delta
                due_basis = f"{due_basis}; test week rebased"

            title_stem = task.title_stem.strip()
            if task.task_type == TaskType.QUIZ and not normalized_text(title_stem).endswith(
                " quiz"
            ):
                title_stem = f"{title_stem} Quiz"
            elif task.task_type == TaskType.TEST and not re.search(
                r"\b(test|exam|midterm|final)$", normalized_text(title_stem)
            ):
                title_stem = f"{title_stem} Test"

            drafts.append(
                DraftTask(
                    course_id=course_id,
                    source_key=capture.source_key,
                    source_url=capture.source_url,
                    assignment_url=_assignment_url(task, block, capture),
                    source_anchor=anchor,
                    source_text=task.source_text,
                    ordinal=ordinal,
                    title=_clean_title(course.prefix, title_stem),
                    details=task.details,
                    classification=effective_classification,
                    task_type=task.task_type,
                    action_kind=task.action_kind,
                    due_date=due_date,
                    due_basis=due_basis,
                    due_uncertain=due_uncertain,
                    due_uncertain_reason=due_uncertain_reason,
                    source_date=source_date,
                    historical=bool(due_date and due_date < today),
                    fingerprint=_fingerprint(task),
                )
            )

    drafts, continuing_ignored = _collapse_continuing_drafts(drafts)
    ignored.extend(continuing_ignored)
    return drafts, uncertain, ignored
