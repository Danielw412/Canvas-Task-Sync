from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from canvas_task_sync.configuration import CourseSettings
from canvas_task_sync.gemini import normalized_text, token_similarity
from canvas_task_sync.models import (
    ActionKind,
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
    # Teachers abbreviate freely ("Oct. 8", "Sept 21"); an unparsed month silently drops
    # an explicit deadline.
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))
ORDINAL = r"(?:st|nd|rd|th)?"
DATE_RANGE_PATTERN = re.compile(
    rf"\b(?P<month>{MONTH_PATTERN})\.?\s+(?P<start>\d{{1,2}}){ORDINAL}\s*"
    rf"(?:[-\u2013\u2014]\s*(?:(?P<end_month>{MONTH_PATTERN})\.?\s+)?"
    rf"(?P<end>\d{{1,2}}){ORDINAL})?\s*,\s*(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
# Gemini sometimes returns a timestamp ("2026-10-08T00:00:00"), so no trailing word boundary.
ISO_DATE_PATTERN = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
MONTH_DAY_PATTERN = re.compile(
    rf"\b(?P<month>{MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}}){ORDINAL}"
    rf"(?:\s*,\s*(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
NUMERIC_MONTH_DAY_PATTERN = re.compile(
    r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2}|\d{4}))?\b"
)
# "Complete 1/2 of the packet" is a fraction, not January 2.
FRACTION_SUFFIX_PATTERN = re.compile(r"\s*of\b", re.IGNORECASE)
TIME_OF_DAY_PATTERN = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\b\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)
LIST_ENUMERATOR_PATTERN = re.compile(r"^\s*\d{1,2}[.)]\s+", re.MULTILINE)
# Timing too vague to place on a calendar. An assessment "next week" is not on its row.
VAGUE_TIMING_PATTERN = re.compile(
    r"\b(?:next\s+week|later\s+this\s+week|upcoming|coming\s+up|tb[ad]|"
    r"to\s+be\s+(?:announced|determined)|(?:date\s+)?(?:is\s+)?unclear|"
    r"not\s+yet\s+(?:scheduled|announced)|will\s+be\s+announced)\b",
    re.IGNORECASE,
)
SAME_DAY_TIMING_PATTERN = re.compile(
    r"\b(?:today|tonight|end\s+of\s+(?:the\s+)?(?:class|period|day)|"
    r"(?:at|by)\s+midnight|closes?\s+at|due\s+(?:at|by)\s+\d)",
    re.IGNORECASE,
)
NEXT_CLASS_TIMING_PATTERN = re.compile(r"\b(?:tomorrow|next\s+class)\b", re.IGNORECASE)

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
    "weds": 2,
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
SECTION_ACRONYM_PATTERN = re.compile(
    r"\((?P<acronym>[A-Z][A-Z0-9]{1,7})\)\s*(?:section|part)?",
)
SECTION_NAME_PATTERN = re.compile(
    r"\b(?P<name>free\s+response|multiple\s+choice)\b",
    re.IGNORECASE,
)
BARE_SECTION_ACRONYM_PATTERN = re.compile(
    r"\b(?P<acronym>[A-Z]{2,5})\s+(?i:section|part)\b"
)
ASSESSMENT_WORD_PATTERN = re.compile(r"\b(?:quiz|test|exam|midterm|final)\b", re.IGNORECASE)
# "Section"/"Part" is filler unless it names the section ("Part 2", "Section A").
SECTION_FILLER_PATTERN = re.compile(
    r"\b(?:section|part)s?\b(?!\s+(?:\d|[A-Za-z]\b))", re.IGNORECASE
)
ASSESSMENT_LEAD_PATTERN = re.compile(r"^(?:on|over|for|about|covering)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class CalendarDateMention:
    value: date
    start: int
    end: int
    text: str


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


def _stated_row_date(day_texts: list[str], weekday: int, agenda_start: date) -> date | None:
    """Return the date a day cell states ("Monday September 21st"), when it is unambiguous.

    A stated date is more reliable than counting weekdays from the week heading, which
    drifts when a table skips a day or its heading is off by one. It is used only when
    it agrees with the row's weekday and falls near the selected agenda week.
    """
    values = {
        mention.value
        for text in day_texts
        for mention in _calendar_date_mentions(text, agenda_start)
    }
    if len(values) != 1:
        return None
    (value,) = values
    if value.weekday() != weekday or not -7 <= (value - agenda_start).days <= 13:
        return None
    return value


def _declared_row_dates(block: AgendaBlock) -> tuple[date, date] | None:
    """Return the dates the source itself states for a block's row.

    A daily slide's heading ("Days 23 and 24: September 18 and 21") names its class days, so
    its row needs no weekday walk from the start of the week.
    """
    values = block.metadata.get("row_dates")
    if not isinstance(values, list) or not values:
        return None
    try:
        parsed = sorted(date.fromisoformat(str(value)) for value in values)
    except ValueError:
        return None
    return parsed[0], parsed[-1]


def row_date_ranges(capture: SourceCapture) -> dict[tuple[str, int], tuple[date, date]]:
    stated: dict[tuple[str, int], tuple[date, date]] = {}
    for block in capture.blocks:
        if block.row_index is None:
            continue
        row_dates = _declared_row_dates(block)
        if row_dates is not None:
            stated.setdefault((block.element_id, block.row_index), row_dates)

    agenda_range = find_agenda_range(capture)
    if agenda_range is None:
        return stated
    agenda_start, _ = agenda_range

    rows: dict[tuple[str, int], str] = {}
    day_texts: dict[tuple[str, int], list[str]] = defaultdict(list)
    for block in capture.blocks:
        if block.row_index is None or not block.row_label:
            continue
        key = (block.element_id, block.row_index)
        if key in stated:
            continue
        rows.setdefault(key, block.row_label)
        if block.role == BlockRole.DAY:
            day_texts[key].append(block.text)

    result: dict[tuple[str, int], tuple[date, date]] = dict(stated)
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
            row_start = _stated_row_date(
                day_texts.get((element_id, row_index), []), start_weekday, agenda_start
            ) or _on_or_after(cursor, start_weekday)
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


def _calendar_date_mentions(
    text: str, source_date: date | None
) -> list[CalendarDateMention]:
    mentions: list[CalendarDateMention] = []

    def append(match: re.Match[str], value: date | None) -> None:
        if value is None:
            return
        mentions.append(
            CalendarDateMention(
                value=value,
                start=match.start(),
                end=match.end(),
                text=match.group(0),
            )
        )

    for match in ISO_DATE_PATTERN.finditer(text):
        try:
            parsed = date(*(int(value) for value in match.groups()))
        except ValueError:
            continue
        append(match, parsed)
    for match in MONTH_DAY_PATTERN.finditer(text):
        append(
            match,
            _date_for_month_day(
                MONTHS[match.group("month").casefold()],
                int(match.group("day")),
                year=int(match.group("year")) if match.group("year") else None,
                source_date=source_date,
            ),
        )
    for match in NUMERIC_MONTH_DAY_PATTERN.finditer(text):
        if FRACTION_SUFFIX_PATTERN.match(text, match.end()):
            continue
        append(
            match,
            _date_for_month_day(
                int(match.group("month")),
                int(match.group("day")),
                year=int(match.group("year")) if match.group("year") else None,
                source_date=source_date,
            ),
        )
    return sorted(mentions, key=lambda item: (item.start, item.end, item.value))


def _calendar_dates_in_text(text: str, source_date: date | None) -> set[date]:
    return {mention.value for mention in _calendar_date_mentions(text, source_date)}


def _named_weekdays(text: str) -> set[int]:
    return {DAY_ALIASES[match.group(1).casefold()] for match in WEEKDAY_PATTERN.finditer(text)}


def _stated_timing(text: str) -> DueRelation | None:
    """Return the relation the evidence itself states ("closes at 9 pm", "for tomorrow")."""
    same_day = SAME_DAY_TIMING_PATTERN.search(text) is not None
    next_class = NEXT_CLASS_TIMING_PATTERN.search(text) is not None
    if same_day == next_class:
        return None
    return DueRelation.SAME_DAY if same_day else DueRelation.NEXT_CLASS


def strip_date_references(text: str) -> str:
    """Return evidence without dates, weekdays, times, or list numbering.

    A rescheduled item keeps its wording but changes its dates ("September 28 FRQ
    section" becomes "October 5 FRQ section"), so identity must compare what is left.
    """
    value = LIST_ENUMERATOR_PATTERN.sub(" ", text)
    for pattern in (
        ISO_DATE_PATTERN,
        MONTH_DAY_PATTERN,
        NUMERIC_MONTH_DAY_PATTERN,
        TIME_OF_DAY_PATTERN,
        WEEKDAY_PATTERN,
    ):
        value = pattern.sub(" ", value)
    return " ".join(value.split())


def has_calendar_date(text: str) -> bool:
    # Only presence matters; a leap-year reference accepts every month/day, even Feb 29.
    return bool(_calendar_date_mentions(text, date(2024, 1, 1)))


def identity_numbers(text: str) -> set[str]:
    """Numbers that name an item ("Unit 2", "chapter 3", "1.4"), excluding dates and times."""
    return set(re.findall(r"\d+", strip_date_references(text)))


@dataclass(frozen=True)
class _ExplicitResolution:
    due_date: date | None = None
    basis: str = "Due date uncertain"
    uncertain_reason: str | None = None
    # Set when the evidence carries no date at all, so the ordinary row policy applies.
    fallback: DueRelation | None = None


UNSUPPORTED_EXPLICIT_DATE = _ExplicitResolution(
    uncertain_reason="The proposed explicit date is not present in the exact source evidence."
)


def _resolve_explicit_due(
    task: ExtractedTask,
    *,
    reference_date: date | None,
    supporting_text: str,
    row_date: date | None,
    explicit_weekday: date | None,
    default_same_day: bool,
) -> _ExplicitResolution:
    """Resolve an explicit deadline from the exact evidence, using Gemini only as a pointer.

    Gemini's ``explicit_due_date`` is free text: it may be missing, carry the wrong year,
    or name a weekday. The evidence decides. A concrete proposal must agree with a date
    in the evidence (year aside); a proposal without a date defers to the evidence.
    """
    stated = "Explicit date stated in source evidence"
    proposal = task.explicit_due_date or ""
    evidence_dates = _calendar_dates_in_text(task.source_text, reference_date)
    proposed = _calendar_date_mentions(proposal, reference_date)
    if proposed:
        support = evidence_dates | _calendar_dates_in_text(supporting_text, reference_date)
        if row_date is not None:
            support.add(row_date)
        wanted = {(mention.value.month, mention.value.day) for mention in proposed}
        matched = {value for value in support if (value.month, value.day) in wanted}
        if len(matched) == 1:
            return _ExplicitResolution(due_date=matched.pop(), basis=stated)
        return UNSUPPORTED_EXPLICIT_DATE

    if len(evidence_dates) == 1:
        (value,) = evidence_dates
        named = _named_weekdays(task.source_text)
        if named and value.weekday() not in named:
            return _ExplicitResolution(
                uncertain_reason="The weekday and calendar date in the source evidence disagree."
            )
        return _ExplicitResolution(due_date=value, basis=stated)
    if evidence_dates:
        named = _named_weekdays(proposal)
        candidates = {value for value in evidence_dates if value.weekday() in named}
        if len(candidates) == 1:
            return _ExplicitResolution(due_date=candidates.pop(), basis=stated)
        return _ExplicitResolution(
            uncertain_reason="The source evidence names more than one date."
        )
    if explicit_weekday is not None:
        # Evidence such as "Due Thursday at midnight" names only a weekday.
        return _ExplicitResolution(
            due_date=explicit_weekday,
            basis="Weekday explicitly stated in source evidence",
        )
    if VAGUE_TIMING_PATTERN.search(task.source_text) or VAGUE_TIMING_PATTERN.search(proposal):
        return _ExplicitResolution(
            uncertain_reason="The source gives only vague timing for this item."
        )
    stated = _stated_timing(task.source_text)
    if stated is not None:
        return _ExplicitResolution(fallback=stated)
    if default_same_day:
        # Assessments and row-bound actions without a date belong on their agenda row.
        return _ExplicitResolution(fallback=DueRelation.SAME_DAY)
    return UNSUPPORTED_EXPLICIT_DATE


def _explicit_weekday_date(
    source_text: str,
    row_range: tuple[date, date] | None,
    evidence_dates: set[date] | None = None,
) -> date | None:
    if row_range is None:
        return None
    matches = list(WEEKDAY_PATTERN.finditer(source_text))
    if not matches:
        return None
    weekday = DAY_ALIASES[matches[-1].group(1).casefold()]
    # "Exam will be on Monday, 8/31" names a calendar Monday, not the next Monday.
    stated = sorted(value for value in evidence_dates or () if value.weekday() == weekday)
    if len(stated) == 1:
        return stated[0]
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


def _assessment_part_label(segment: str, index: int) -> str:
    acronym = SECTION_ACRONYM_PATTERN.search(segment) or BARE_SECTION_ACRONYM_PATTERN.search(
        segment
    )
    if acronym is not None:
        return acronym.group("acronym").upper()
    named = SECTION_NAME_PATTERN.search(segment)
    if named is not None:
        return "FRQ" if named.group("name").casefold().startswith("free") else "MCQ"
    section = re.search(r"\b(?:section|part)\s+([A-Za-z0-9]+)\b", segment, re.I)
    if section is not None:
        return f"Section {section.group(1).upper()}"
    return f"Day {index + 1}"


def _assessment_part_title(title: str, label: str, task_type: TaskType) -> str:
    cleaned = " ".join(title.split()).strip(" -:[]")
    terminal = "Quiz" if task_type == TaskType.QUIZ else "Test"
    match = re.search(r"\b(quiz|test|exam|midterm|final)\s*$", cleaned, re.I)
    if match is not None:
        terminal = match.group(1).title()
        cleaned = cleaned[: match.start()].rstrip(" -:")
    return " ".join(part for part in (cleaned, label, terminal) if part)


def _canonical_assessment_title(title: str, task_type: TaskType) -> str:
    """Put the assessment word last and drop filler "section" words.

    Gemini names the same exam "Unit 2 Exam FRQ Section" one run and "Unit 2 FRQ Section
    Exam" the next; one canonical form ("Unit 2 FRQ Exam") keeps titles and matching stable.
    Titles that already read naturally are returned unchanged.
    """
    words = list(ASSESSMENT_WORD_PATTERN.finditer(title))
    if not words:
        return f"{title} {'Quiz' if task_type == TaskType.QUIZ else 'Test'}"
    if (
        len(words) == 1
        and words[0].end() == len(title)
        and not SECTION_FILLER_PATTERN.search(title)
    ):
        return title
    body = SECTION_FILLER_PATTERN.sub(" ", ASSESSMENT_WORD_PATTERN.sub(" ", title))
    body = ASSESSMENT_LEAD_PATTERN.sub("", " ".join(body.split()).strip(" -:"))
    if not body:
        return title
    return f"{body} {words[-1].group(0).title()}"


def _normalized_assessment_title(task: ExtractedTask) -> str:
    title = " ".join(task.title_stem.split()).strip(" -:[]")
    if task.task_type == TaskType.QUIZ:
        if re.search(r"\bquiz$", title, re.I):
            return title
        return _canonical_assessment_title(title, task.task_type)

    acronym_match = SECTION_ACRONYM_PATTERN.search(task.source_text)
    if acronym_match is not None:
        label = acronym_match.group("acronym").upper()
        terminal = "Exam" if re.search(r"\bexam\b", title, re.I) else "Test"
        base = re.sub(
            r"\b(?:free\s+response\s+question|multiple\s+choice\s+question|"
            r"FRQ|MCQ|section|part|quiz|test|exam|midterm|final)\b",
            " ",
            title,
            flags=re.IGNORECASE,
        )
        base = " ".join(base.split()).strip(" -:[]")
        return " ".join(part for part in (base, label, terminal) if part)

    return _canonical_assessment_title(title, task.task_type)


def _split_multi_date_assessment(
    task: ExtractedTask,
    source_date: date | None,
) -> list[ExtractedTask]:
    if task.task_type not in {TaskType.QUIZ, TaskType.TEST}:
        return [task]
    mentions = _calendar_date_mentions(task.source_text, source_date)
    if len({mention.value for mention in mentions}) < 2:
        return [task]

    parts: list[ExtractedTask] = []
    for index, mention in enumerate(mentions):
        segment_start = 0 if index == 0 else mention.start
        segment_end = mentions[index + 1].start if index + 1 < len(mentions) else len(
            task.source_text
        )
        evidence = task.source_text[segment_start:segment_end].strip(" ,;:-")
        label = _assessment_part_label(evidence, index)
        parts.append(
            task.model_copy(
                update={
                    "source_text": evidence,
                    "title_stem": _assessment_part_title(
                        task.title_stem, label, task.task_type
                    ),
                    "due_relation": DueRelation.EXPLICIT_DATE,
                    "explicit_due_date": mention.text,
                }
            )
        )
    return parts


def _is_canvas_assignment_url(value: str) -> bool:
    return bool(CANVAS_ASSIGNMENT_PATH_PATTERN.search(value))


def _linked_assignment(
    task: ExtractedTask, block: AgendaBlock
) -> tuple[dict[str, Any] | None, str]:
    """Return the one Canvas assignment linked from the task's phrase, and the text tying them.

    Slide links are phrased as instructions ("Submit here"), so their text rarely resembles
    the task title. Containment is the association instead: the link text lies inside the exact
    evidence or, failing that, inside the one source line that holds the evidence. Two links at
    the tightest scope are ambiguous and never widen to the line.
    """
    evidence = normalized_text(task.source_text)
    links = [
        (str(link.get("url", "")), normalized_text(str(link.get("text", ""))), link)
        for link in block.metadata.get("assignment_links", [])
        if isinstance(link, dict)
    ]
    links = [item for item in links if item[1] and _is_canvas_assignment_url(item[0])]
    if not evidence or not links:
        return None, ""
    scopes = [task.source_text] + [
        line for line in block.text.splitlines() if evidence in normalized_text(line)
    ]
    for scope in scopes:
        padded = f" {normalized_text(scope)} "
        found = {url: link for url, text, link in links if f" {text} " in padded}
        if len(found) == 1:
            return next(iter(found.values())), scope
        if found:
            return None, ""
    return None, ""


def _mentions_timing(text: str, reference: date | None) -> bool:
    """Return whether text states a day, a date, or any timing of its own."""
    return bool(
        WEEKDAY_PATTERN.search(text)
        or SAME_DAY_TIMING_PATTERN.search(text)
        or NEXT_CLASS_TIMING_PATTERN.search(text)
        or VAGUE_TIMING_PATTERN.search(text)
        or _calendar_date_mentions(text, reference)
    )


def _canvas_due_date(
    task: ExtractedTask,
    block: AgendaBlock,
    *,
    timezone_name: str,
    reference: date | None,
) -> date | None:
    """Return the due date of the Canvas assignment linked from the task, in local time.

    It only fills in timing the source leaves unstated: when the evidence or its line names a
    day or date, the stated-day policy decides instead, so the two can never disagree silently.
    """
    link, scope = _linked_assignment(task, block)
    value = link.get("due_at") if link else None
    if not value or _mentions_timing(scope, reference):
        return None
    try:
        due = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return due.astimezone(ZoneInfo(timezone_name)).date()


def _assignment_url(
    task: ExtractedTask,
    block: AgendaBlock,
    capture: SourceCapture,
) -> str | None:
    linked, _ = _linked_assignment(task, block)
    if linked is not None:
        return str(linked["url"])
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


def _collapse_work_before_submission(
    drafts: list[DraftTask],
) -> tuple[list[DraftTask], list[IgnoredItem]]:
    """Track an assignment once when the agenda lists work days before its submission day."""

    submitted: dict[str, date] = {}
    for draft in drafts:
        if (
            draft.action_kind == ActionKind.SUBMIT
            and draft.task_type == TaskType.ASSIGNMENT
            and draft.due_date is not None
        ):
            key = normalized_text(draft.title)
            submitted[key] = max(submitted.get(key, draft.due_date), draft.due_date)

    kept: list[DraftTask] = []
    ignored: list[IgnoredItem] = []
    for draft in drafts:
        submission_date = submitted.get(normalized_text(draft.title))
        # Compare the day the work appears on the agenda, not its next-class deadline.
        work_date = draft.source_date or draft.due_date
        if (
            submission_date is not None
            and draft.action_kind != ActionKind.SUBMIT
            and draft.task_type == TaskType.ASSIGNMENT
            and work_date is not None
            and work_date <= submission_date
        ):
            ignored.append(
                IgnoredItem(
                    title=draft.title,
                    evidence=draft.source_text,
                    reason="Work on this assignment is tracked by its later submission task.",
                    source_anchor=draft.source_anchor,
                )
            )
            continue
        kept.append(draft)
    return kept, ignored


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
    agenda_reference = agenda_range[0] if agenda_range is not None else None
    expanded_tasks = [
        part
        for task in tasks
        for part in _split_multi_date_assessment(task, agenda_reference)
    ]
    for task in expanded_tasks:
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
            source_date = row_range[1] if row_range else None
            calendar_reference = source_date or agenda_reference
            evidence_dates = _calendar_dates_in_text(task.source_text, calendar_reference)
            # A weekday outside a dated row still belongs to the captured agenda week.
            agenda_week = (agenda_reference, agenda_reference) if agenda_reference else None
            explicit_weekday = _explicit_weekday_date(
                task.source_text, row_range or agenda_week, evidence_dates
            )
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
            # A Canvas assignment linked from the task's own phrase carries the deadline the
            # teacher set. It replaces inferred timing only: a date or day the source states
            # still wins, and an assessment stays on the day it is given.
            canvas_due: date | None = None
            if not is_assessment:
                canvas_due = _canvas_due_date(
                    task,
                    block,
                    timezone_name=course.timezone,
                    reference=row_range[1] if row_range else agenda_reference,
                )
            deadline_bearing_classwork = (
                relation == DueRelation.EXPLICIT_DATE
                or is_same_day_action
                or is_assessment
                or explicit_weekday is not None
                or canvas_due is not None
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

            explicit: _ExplicitResolution | None = None
            if relation == DueRelation.EXPLICIT_DATE:
                explicit = _resolve_explicit_due(
                    task,
                    reference_date=calendar_reference,
                    supporting_text=block.text,
                    row_date=source_date,
                    explicit_weekday=explicit_weekday,
                    default_same_day=is_assessment or is_same_day_action,
                )
                if explicit.fallback is not None:
                    relation = explicit.fallback
                    explicit = None
            # Timing the evidence states outranks Gemini's relation and the row defaults:
            # "Closes at 9 pm" is due that day, and "bring it to class tomorrow" is not.
            stated_timing = None
            if explicit is None and explicit_weekday is None and not evidence_dates:
                stated_timing = _stated_timing(task.source_text)
                if stated_timing is not None and relation in {
                    DueRelation.SAME_DAY,
                    DueRelation.NEXT_CLASS,
                }:
                    relation = stated_timing
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
            if explicit is not None:
                due_date = explicit.due_date
                due_basis = explicit.basis
                due_uncertain = explicit.due_date is None
                due_uncertain_reason = explicit.uncertain_reason
            elif explicit_weekday is not None:
                due_date = explicit_weekday
                due_basis = "Weekday explicitly stated in source evidence"
            elif canvas_due is not None:
                due_date = canvas_due
                due_basis = "Due date of the Canvas assignment linked in source evidence"
            elif relation == DueRelation.SAME_DAY:
                if VAGUE_TIMING_PATTERN.search(task.source_text):
                    # "Exam will be next week" announces an assessment; it is not on this row.
                    due_uncertain = True
                    due_uncertain_reason = "The source gives only vague timing for this item."
                    due_basis = "Due date uncertain"
                elif source_date is None:
                    due_uncertain = True
                    due_uncertain_reason = (
                        "Same-day action could not be tied to a dated agenda row."
                    )
                    due_basis = "Due date uncertain"
                else:
                    due_date = source_date
                    due_basis = (
                        "Same-day deadline stated in source evidence"
                        if stated_timing == DueRelation.SAME_DAY
                        else "Assessment scheduled on its agenda row"
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
                    due_basis = (
                        "Next class stated in source evidence"
                        if stated_timing == DueRelation.NEXT_CLASS
                        else "Work with no stated date due next configured school day"
                    )
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
            if task.task_type in {TaskType.QUIZ, TaskType.TEST}:
                title_stem = _normalized_assessment_title(task)

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
    drafts, submission_ignored = _collapse_work_before_submission(drafts)
    ignored.extend(submission_ignored)
    return drafts, uncertain, ignored
