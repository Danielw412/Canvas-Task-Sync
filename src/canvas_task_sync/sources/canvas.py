from __future__ import annotations

import hashlib
import json
import os
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from canvas_task_sync.models import AgendaBlock, BlockRole, SourceCapture
from canvas_task_sync.sources.published_slides import (
    PublishedDeck,
    PublishedSlide,
    PublishedSlidesError,
    SlideHeading,
    SlideLink,
    fetch_published_deck,
    overlaps_week,
    published_deck_id,
    slide_heading,
    weekday_label,
)

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))
MONTH_DATE_RE = re.compile(
    rf"(?P<week>week\s+of\s+)?(?P<month>{MONTH_PATTERN})\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<year>20\d{2}))?"
    rf"(?P<range>\s*(?:-|–|—|to|through)\s*(?:(?:{MONTH_PATTERN})\.?\s+)?\d{{1,2}}(?:st|nd|rd|th)?)?",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(
    r"(?P<week>week\s+of\s+)?(?P<month>\d{1,2})[/-](?P<day>\d{1,2})"
    r"(?:[/-](?P<year>20\d{2}|\d{2}))?"
    r"(?P<range>\s*(?:-|–|—|to|through)\s*\d{1,2}(?:[/-]\d{1,2})?)?",
    re.IGNORECASE,
)
AGENDA_TERMS = (
    "agenda",
    "learning activities",
    "learning targets",
    "assignments",
    "homework",
)
DAY_NAME_PATTERN = (
    r"(?:m|t|w|th|f|mon|tue|tues|wed|weds|thu|thur|thurs|fri|"
    r"monday|tuesday|wednesday|thursday|friday)"
)
DAY_RE = re.compile(rf"^{DAY_NAME_PATTERN}\.?$", re.I)
DATED_DAY_RE = re.compile(rf"^(?P<day>{DAY_NAME_PATTERN})\.?,?\s+(?P<date>.+)$", re.I | re.S)
# Schedule notes a school adds under a day name: "B Day", "(A)", "Day 3", "Early dismissal".
# Deliberately a whitelist, so "Monday / Quiz" stays content instead of becoming a label.
DAY_ANNOTATION_RE = re.compile(
    r"^\(?(?:[a-h]|[a-h]\s*day|day\s*[a-h0-9]{1,2}|[a-h]/[a-h]|block\s*[a-z0-9]{1,2}|"
    r"odd|even|half\s+day|early\s+(?:dismissal|release)|late\s+start|"
    r"delayed\s+opening|no\s+school)\)?$",
    re.I,
)
DAY_CELL_MAX_CHARS = 60
SEMANTIC_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}
LINE_BREAK_TAGS = {
    "address",
    "blockquote",
    "br",
    "caption",
    "dd",
    "div",
    "dl",
    "dt",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
HEADER_CELL_MAX_CHARS = 80
WEEK_HEADING_MAX_CHARS = 120
# Words and symbols a week heading may carry besides its date ("🗓️ Week of ...").
WEEK_HEADING_FILLER_RE = re.compile(
    r"\b(?:week|of|for|the|learning|targets?|agenda|and|to|through)\b|[^a-z0-9]+"
)
WEEK_LABEL_PREFIX_RE = re.compile(r"(?:learning\s+targets\s+)?for\s+the\s+week\s*:?\s*$", re.I)
ROW_CELL_ROLE_PRIORITY = {BlockRole.ASSIGNMENTS: 3, BlockRole.LEARNING: 2, BlockRole.UNKNOWN: 1}
THIS_WEEK_RE = re.compile(
    r"\b(?:this\s+week(?:['’]?s)?(?:\s+agenda)?|agenda\s+(?:for\s+)?this\s+week)\b",
    re.IGNORECASE,
)
CANVAS_ASSIGNMENT_PATH_RE = re.compile(r"/courses/\d+/assignments/\d+(?:/|$)")
# A course may embed a few published decks; each viewer page is fetched in full.
MAX_EMBEDDED_DECKS = 3
# A deck is a daily agenda only when several slides carry class-day headings.
MIN_DATED_DECK_SLIDES = 2
# A deck competes with Canvas pages on their scale: as strong as an exact week match, plus
# coverage. A labeled "Week of" agenda table still outranks it; a stale weekly page does not.
DECK_BASE_SCORE = 120
DECK_SCORE_PER_DAY = 10
DECK_MAX_COVERAGE_SCORE = 40
GOOGLE_FILE_KINDS = {
    "document": "Google Docs document",
    "spreadsheets": "Google Sheets spreadsheet",
    "presentation": "Google Slides presentation",
    "forms": "Google Form",
    "drawings": "Google Drawing",
}


class CanvasSourceError(RuntimeError):
    pass


class CanvasAgendaNotFound(CanvasSourceError):
    pass


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    parent: HtmlNode | None = None
    children: list[HtmlNode | str] = field(default_factory=list)

    def text(self, separator: str = " ") -> str:
        parts: list[str] = []

        def visit(node: HtmlNode | str) -> None:
            if isinstance(node, str):
                normalized = " ".join(node.split())
                if normalized:
                    parts.append(normalized)
                return
            for child in node.children:
                visit(child)

        visit(self)
        return separator.join(parts).strip()

    def descendants(self, tags: set[str] | None = None) -> Iterable[HtmlNode]:
        for child in self.children:
            if not isinstance(child, HtmlNode):
                continue
            if tags is None or child.tag in tags:
                yield child
            yield from child.descendants(tags)


class CanvasHtmlParser(HTMLParser):
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document")
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(
            tag.casefold(),
            {key.casefold(): value or "" for key, value in attrs},
            self.current,
        )
        self.current.children.append(node)
        if node.tag not in self.VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.current.tag == tag.casefold() and self.current.parent is not None:
            self.current = self.current.parent

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()
        cursor = self.current
        while cursor.parent is not None:
            if cursor.tag == wanted:
                self.current = cursor.parent
                return
            cursor = cursor.parent

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.current.children.append(data)


@dataclass(frozen=True)
class WeekTextMatch:
    start: date
    score: int
    matched_text: str
    position: int
    heading: bool = False


@dataclass
class CanvasDocument:
    key: str
    kind: str
    title: str
    body: str
    html_url: str
    context: str = ""
    updated_on: date | None = None


def _candidate_date(month: int, day: int, year: int, target: date) -> date | None:
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    if abs((value - target).days) > 190:
        return None
    return value


def _matched_date(found: re.Match[str], target_week_start: date) -> date | None:
    month_value = found.group("month").casefold().rstrip(".")
    month = MONTHS.get(month_value, int(month_value) if month_value.isdigit() else 0)
    year_text = found.group("year")
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
        years = [year]
    else:
        years = [
            target_week_start.year - 1,
            target_week_start.year,
            target_week_start.year + 1,
        ]
    candidates = [
        value
        for candidate_year in years
        if (
            value := _candidate_date(
                month,
                int(found.group("day")),
                candidate_year,
                target_week_start,
            )
        )
        is not None
    ]
    return (
        min(candidates, key=lambda value: abs((value - target_week_start).days))
        if candidates
        else None
    )


def _has_week_label(text: str, found: re.Match[str]) -> bool:
    # Canvas agenda tables often label their first cell as
    # "Learning Targets for the Week: August 24" instead of the more
    # conventional "Week of August 24".
    prefix = text[max(0, found.start() - 80) : found.start()]
    return bool(found.group("week") or WEEK_LABEL_PREFIX_RE.search(prefix))


def find_week_matches(text: str, target_week_start: date) -> list[WeekTextMatch]:
    matches: list[WeekTextMatch] = []
    for pattern in (MONTH_DATE_RE, NUMERIC_DATE_RE):
        for found in pattern.finditer(text):
            start = _matched_date(found, target_week_start)
            if start is None:
                continue
            delta = (start - target_week_start).days
            if delta == 0:
                score = 120
            elif 0 <= delta <= 4:
                score = 94 - (delta * 3)
            elif -2 <= delta < 0:
                score = 72 - abs(delta * 4)
            else:
                continue
            labeled = _has_week_label(text, found)
            if labeled:
                # A labeled week heading outranks incidental due dates for the same
                # Monday in an older table on the page.
                score += 20
            if found.group("range"):
                score += 8
            surrounding = text[max(0, found.start() - 180) : found.end() + 240].casefold()
            score += min(24, 6 * sum(term in surrounding for term in AGENDA_TERMS))
            matches.append(
                WeekTextMatch(
                    start,
                    score,
                    found.group(0),
                    found.start(),
                    heading=labeled or bool(found.group("range")),
                )
            )
    return sorted(matches, key=lambda item: (-item.score, item.position))


def _other_week_heading(text: str, target_week_start: date) -> bool:
    """Return whether text carries a week heading for a different agenda week."""
    for pattern in (MONTH_DATE_RE, NUMERIC_DATE_RE):
        for found in pattern.finditer(text):
            # Bare numeric ranges such as "2-3" are too ambiguous to count as headings.
            ranged_month = pattern is MONTH_DATE_RE and found.group("range")
            if not (_has_week_label(text, found) or ranged_month):
                continue
            start = _matched_date(found, target_week_start)
            if start is not None and not -2 <= (start - target_week_start).days <= 4:
                return True
    return False


def _belongs_to_other_week(text: str, target_week_start: date) -> bool:
    matches = find_week_matches(text, target_week_start)
    return not any(match.heading for match in matches) and _other_week_heading(
        text, target_week_start
    )


def _inside_any(node: HtmlNode, node_ids: set[int]) -> bool:
    cursor: HtmlNode | None = node
    while cursor is not None:
        if id(cursor) in node_ids:
            return True
        cursor = cursor.parent
    return False


def week_match_score(text: str, target_week_start: date) -> int:
    matches = find_week_matches(text, target_week_start)
    return matches[0].score if matches else 0


def _parse_html(body: str) -> CanvasHtmlParser:
    parser = CanvasHtmlParser()
    parser.feed(body or "")
    parser.close()
    return parser


def _agenda_node(
    parser: CanvasHtmlParser, target_week_start: date
) -> tuple[HtmlNode, WeekTextMatch]:
    candidates: list[tuple[float, int, HtmlNode, WeekTextMatch]] = []
    table_candidates: list[tuple[float, int, HtmlNode, WeekTextMatch]] = []
    other_week_tables = {
        id(table)
        for table in parser.root.descendants({"table"})
        if _belongs_to_other_week(table.text(" "), target_week_start)
    }
    for node in [parser.root, *parser.root.descendants()]:
        text = node.text(" ")
        if len(text) < 25:
            continue
        matches = find_week_matches(text, target_week_start)
        if not matches:
            continue
        if not any(match.heading for match in matches) and (
            _inside_any(node, other_week_tables) or _other_week_heading(text, target_week_start)
        ):
            # An incidental date, such as a make-up exam deadline, inside another week's
            # agenda must not claim that agenda for the target week.
            continue
        best = matches[0]
        lowered = text.casefold()
        agenda_signals = sum(term in lowered for term in AGENDA_TERMS)
        weekday_signals = sum(
            day in lowered for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        )
        tables = int(node.tag == "table") + sum(1 for _ in node.descendants({"table"}))
        score = (
            best.score + (agenda_signals * 13) + min(20, weekday_signals * 4) + min(15, tables * 8)
        )
        score -= min(35, max(0, len(text) - 2500) / 300)
        candidate = (score, -len(text), node, best)
        candidates.append(candidate)
        if node.tag == "table" and _sufficient_agenda_content(node):
            table_candidates.append(candidate)
    if not candidates:
        raise CanvasAgendaNotFound(
            "Canvas content did not contain a recognizable heading for the week of "
            f"{target_week_start.strftime('%B')} {target_week_start.day}."
        )
    # A Canvas page may retain several weekly agenda tables.  When the target
    # date appears inside one table, that table is the authoritative scope; a
    # shared ancestor would mix the target week with older weeks before Gemini
    # ever sees the transcript.
    scoped_candidates = table_candidates or candidates
    _, _, node, matched = max(scoped_candidates, key=lambda item: (item[0], item[1]))
    return node, matched


def _relative_agenda_node(
    parser: CanvasHtmlParser, target_week_start: date
) -> tuple[HtmlNode, WeekTextMatch]:
    candidates: list[tuple[float, int, HtmlNode, WeekTextMatch]] = []
    table_candidates: list[tuple[float, int, HtmlNode, WeekTextMatch]] = []
    for node in [parser.root, *parser.root.descendants()]:
        text = node.text(" ")
        found = THIS_WEEK_RE.search(text)
        if found is None or len(text) < 25:
            continue
        lowered = text.casefold()
        agenda_signals = sum(term in lowered for term in AGENDA_TERMS)
        weekday_signals = sum(
            day in lowered for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        )
        tables = int(node.tag == "table") + sum(1 for _ in node.descendants({"table"}))
        score = 120 + (agenda_signals * 13) + min(20, weekday_signals * 4) + min(15, tables * 8)
        score -= min(35, max(0, len(text) - 2500) / 300)
        matched = WeekTextMatch(target_week_start, 120, found.group(0), found.start())
        candidate = (score, -len(text), node, matched)
        candidates.append(candidate)
        if node.tag == "table" and _sufficient_agenda_content(node):
            table_candidates.append(candidate)
    if not candidates:
        raise CanvasAgendaNotFound("Canvas content did not contain a current-week agenda heading.")
    scoped_candidates = table_candidates or candidates
    _, _, node, matched = max(scoped_candidates, key=lambda item: (item[0], item[1]))
    return node, matched


def _direct_children(node: HtmlNode, tags: set[str]) -> list[HtmlNode]:
    return [child for child in node.children if isinstance(child, HtmlNode) and child.tag in tags]


def _nearest_table(node: HtmlNode) -> HtmlNode | None:
    cursor = node.parent
    while cursor is not None and cursor.tag != "table":
        cursor = cursor.parent
    return cursor


def _week_heading_start(text: str, target_week_start: date) -> date | None:
    """Return the week announced by heading-like text such as "Week of September 14".

    Only short text that is essentially the heading counts, so a note such as
    "Unit 2 Exam: September 28-29" inside an agenda does not split it into sections.
    """
    normalized = " ".join(text.split())
    if not normalized or len(normalized) > WEEK_HEADING_MAX_CHARS:
        return None
    for pattern in (MONTH_DATE_RE, NUMERIC_DATE_RE):
        for found in pattern.finditer(normalized):
            ranged = pattern is MONTH_DATE_RE and found.group("range")
            if not (_has_week_label(normalized, found) or ranged):
                continue
            rest = f"{normalized[: found.start()]} {normalized[found.end() :]}".casefold()
            if len(WEEK_HEADING_FILLER_RE.sub("", rest)) > 4:
                continue
            start = _matched_date(found, target_week_start)
            if start is not None:
                return start
    return None


def _week_section_filter(
    node: HtmlNode, target_week_start: date
) -> Callable[[HtmlNode], bool] | None:
    """Scope a node that holds several weeks' agendas to the target week's section.

    Pages such as "Past Agendas" put each week's heading beside, not inside, its table,
    so the only node that contains the target heading also contains every older week.
    Each element belongs to the last week heading at or before its end in document
    order. Returns None when the node does not mix weeks, so it is used unchanged.
    """
    spans: dict[int, tuple[int, int]] = {}
    markers: list[tuple[int, bool]] = []
    counter = 0

    def visit(element: HtmlNode) -> None:
        nonlocal counter
        start = counter
        counter += 1
        for child in element.children:
            if isinstance(child, HtmlNode):
                visit(child)
        spans[id(element)] = (start, counter - 1)
        if element.tag not in {"table", "thead", "tbody", "tfoot", "tr"}:
            week = _week_heading_start(element.text(" "), target_week_start)
            if week is not None:
                markers.append((start, -2 <= (week - target_week_start).days <= 4))

    visit(node)
    if not any(is_target for _, is_target in markers) or all(
        is_target for _, is_target in markers
    ):
        return None
    markers.sort()
    positions = [position for position, _ in markers]

    def keep(element: HtmlNode) -> bool:
        _, end = spans.get(id(element), (0, 0))
        index = bisect_right(positions, end) - 1
        return index >= 0 and markers[index][1]

    return keep


def _line_text(node: HtmlNode, *, skip_nested_tables: bool = False) -> str:
    """Return node text with one line per paragraph/list item so list entries stay distinct."""
    lines: list[str] = []
    current: list[str] = []

    def flush() -> None:
        line = " ".join(" ".join(current).split())
        if line:
            lines.append(line)
        current.clear()

    def visit(item: HtmlNode | str) -> None:
        if isinstance(item, str):
            current.append(item)
            return
        if skip_nested_tables and item is not node and item.tag == "table":
            return  # A nested table is captured as its own rows.
        breaks_line = item.tag in LINE_BREAK_TAGS
        if breaks_line:
            flush()
        for child in item.children:
            visit(child)
        if breaks_line:
            flush()

    visit(node)
    flush()
    return "\n".join(lines)


def _day_label(value: str) -> str | None:
    """Return the weekday label for a day cell such as "Th" or "Monday September 14th".

    Day cells often carry a date and a short schedule note on further lines
    ("Monday / September 21st / B Day"). Anything longer, or with instructions after
    the day name, is content rather than a label.
    """
    stripped = value.strip()
    if DAY_RE.fullmatch(stripped):
        return stripped.rstrip(".")
    if len(stripped) > DAY_CELL_MAX_CHARS:
        return None
    first, _, rest = stripped.partition("\n")
    first = first.strip()
    if DAY_RE.fullmatch(first):
        label = first.rstrip(".")
        dated_line = False
    else:
        dated = DATED_DAY_RE.fullmatch(first)
        if dated is None:
            return None
        label = dated.group("day")
        rest = f"{dated.group('date')}\n{rest}"
        dated_line = True
    # Joining lines also rejoins a date split over lines ("September" / "10th").
    remainder = " ".join(rest.split())
    without_dates = remainder
    for pattern in (MONTH_DATE_RE, NUMERIC_DATE_RE):
        without_dates = " ".join(pattern.sub(" ", without_dates).split())
    if dated_line and without_dates == remainder:
        return None  # "Monday Quiz": text after the day name must start with a date.
    if not without_dates or DAY_ANNOTATION_RE.fullmatch(without_dates):
        return label
    return None


def _is_header_row(cells: list[HtmlNode], values: list[str]) -> bool:
    labels = [value for value in values if value.strip()]
    if not any(
        "assignment" in value.casefold() or "activit" in value.casefold() for value in labels
    ):
        return False
    # Narrative cells such as "How to find the assignments in Canvas ..." mention the same
    # words but are not column headings.
    return all(cell.tag == "th" for cell in cells) or all(
        len(value) <= HEADER_CELL_MAX_CHARS for value in labels
    )


def _distinct_row_cells(values: list[str], roles: list[BlockRole]) -> set[int]:
    """Keep one copy of cells repeated across a row, preferring the most specific column."""
    kept: dict[str, int] = {}
    for index, value in enumerate(values):
        key = " ".join(value.casefold().split())
        if not key:
            continue
        current = kept.get(key)
        if current is None or ROW_CELL_ROLE_PRIORITY.get(
            roles[index], 0
        ) > ROW_CELL_ROLE_PRIORITY.get(roles[current], 0):
            kept[key] = index
    return set(kept.values())


def _safe_anchor(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", value).strip("_")[:180] or "content"


def _assignment_links(node: HtmlNode, base_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    source_host = urlparse(base_url).hostname
    for anchor in node.descendants({"a"}):
        href = anchor.attrs.get("href", "").strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname != source_host
            or not CANVAS_ASSIGNMENT_PATH_RE.search(parsed.path)
            or absolute in seen
        ):
            continue
        seen.add(absolute)
        links.append({"url": absolute, "text": anchor.text(" ")})
    return links


def _agenda_blocks(
    node: HtmlNode,
    document: CanvasDocument,
    target_week_start: date | None = None,
) -> list[AgendaBlock]:
    blocks: list[AgendaBlock] = []
    order = 0
    in_target_week = (
        _week_section_filter(node, target_week_start) if target_week_start else None
    )

    def append(
        text: str,
        kind: str,
        role: BlockRole,
        row_label: str | None = None,
        element_id: str | None = None,
        row_index: int | None = None,
        column_index: int | None = None,
        **metadata: Any,
    ) -> None:
        nonlocal order
        normalized = "\n".join(
            " ".join(line.split()) for line in text.splitlines() if line.strip()
        )
        if not normalized:
            return
        order += 1
        blocks.append(
            AgendaBlock(
                anchor=f"canvas:{_safe_anchor(document.key)}:{order}",
                element_id=element_id or f"{document.key}:{order}",
                kind=kind,
                role=role,
                row_index=row_index,
                column_index=column_index,
                row_label=row_label,
                text=normalized,
                order=order,
                metadata={"canvas_kind": document.kind, **metadata},
            )
        )

    table_nodes = ([node] if node.tag == "table" else []) + list(node.descendants({"table"}))
    for table_index, table in enumerate(table_nodes):
        # A nested table's rows belong to it alone; counting them for the outer table too
        # captured every nested row twice.
        rows = [row for row in table.descendants({"tr"}) if _nearest_table(row) is table]
        headers: list[str] = []
        for row_index, row in enumerate(rows):
            cells = _direct_children(row, {"td", "th"})
            if not cells or (in_target_week is not None and not in_target_week(row)):
                continue
            values = [_line_text(cell, skip_nested_tables=True) for cell in cells]
            if not headers and _is_header_row(cells, values):
                headers = values
            day_labels = [_day_label(value) for value in values]
            row_label = next((label for label in day_labels if label), None)
            roles: list[BlockRole] = []
            for column_index in range(len(values)):
                lowered_header = (
                    headers[column_index].casefold() if column_index < len(headers) else ""
                )
                if day_labels[column_index]:
                    role = BlockRole.DAY
                elif "assignment" in lowered_header or "homework" in lowered_header:
                    role = BlockRole.ASSIGNMENTS
                elif "activit" in lowered_header or "lesson" in lowered_header:
                    role = BlockRole.LEARNING
                elif row_index == 0:
                    role = BlockRole.HEADER
                else:
                    role = BlockRole.UNKNOWN
                roles.append(role)
            kept_columns = _distinct_row_cells(values, roles)
            for column_index, value in enumerate(values):
                role = roles[column_index]
                if column_index not in kept_columns:
                    if value.strip():
                        # Consume the duplicate's slot so later anchors stay stable.
                        order += 1
                    continue
                assignment_links = _assignment_links(cells[column_index], document.html_url)
                append(
                    value,
                    "table_cell",
                    role,
                    row_label=row_label,
                    element_id=f"{document.key}:table:{table_index}",
                    row_index=row_index,
                    column_index=column_index,
                    table_index=table_index,
                    assignment_links=assignment_links,
                )

    table_descendants = {
        id(descendant) for table in table_nodes for descendant in [table, *table.descendants()]
    }
    for semantic in node.descendants(SEMANTIC_TAGS):
        if id(semantic) in table_descendants or (
            in_target_week is not None and not in_target_week(semantic)
        ):
            continue
        text = semantic.text(" ")
        role = BlockRole.HEADER if semantic.tag.startswith("h") else BlockRole.UNKNOWN
        append(
            text,
            semantic.tag,
            role,
            assignment_links=_assignment_links(semantic, document.html_url),
        )

    if not blocks and in_target_week is None:
        append(node.text(" "), "canvas_html", BlockRole.UNKNOWN)
    return blocks


def _sufficient_agenda_content(node: HtmlNode) -> bool:
    text = node.text(" ")
    lowered = text.casefold()
    weekday_count = sum(
        day in lowered for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
    )
    agenda_structure = any(
        term in lowered for term in ("learning activities", "assignments", "homework", "classwork")
    )
    has_table = node.tag == "table" or any(True for _ in node.descendants({"table"}))
    return len(text) >= 60 and (
        agenda_structure or weekday_count >= 2 or (has_table and weekday_count >= 1)
    )


def _link_description(link: dict[str, Any]) -> str:
    title = str(link.get("title") or "").strip()
    labels = {
        "canvas_assignment": "Canvas assignment",
        "canvas_quiz": "Canvas quiz",
        "canvas_page": "Canvas page",
        "canvas": "Canvas",
    }
    label = labels.get(str(link.get("kind")))
    if label is None:
        return title or str(link.get("host") or "web page")
    return f'{label} "{title}"' if title else label


def _transcript(blocks: list[AgendaBlock]) -> str:
    sections: list[str] = []
    for block in blocks:
        context = [f"anchor={block.anchor}", f"role={block.role.value}", f"order={block.order}"]
        if block.row_label:
            context.append(f"day={block.row_label}")
        row_dates = block.metadata.get("row_dates")
        if row_dates:
            context.append(f"date={','.join(row_dates)}")
        section = f"[{' '.join(context)}]\n{block.text}"
        links = block.metadata.get("links") or []
        if links:
            # Link targets are context for Gemini, not evidence, so they stay out of block text.
            described = "\n".join(
                f'- "{link["text"]}" -> {_link_description(link)}' for link in links
            )
            section += f"\n(links in this text; context only, never evidence)\n{described}"
        sections.append(section)
    return "\n\n".join(sections)


def _embedded_deck_ids(body: str) -> list[str]:
    """Return published Slides decks embedded in Canvas HTML, in document order."""
    found: list[str] = []
    for frame in _parse_html(body).root.descendants({"iframe"}):
        deck_id = published_deck_id(frame.attrs.get("src", ""))
        if deck_id and deck_id not in found:
            found.append(deck_id)
    return found


def _describe_web_link(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    if host == "docs.google.com":
        kind = next(
            (
                label
                for segment, label in GOOGLE_FILE_KINDS.items()
                if parsed.path.startswith(f"/{segment}/")
            ),
            None,
        )
        if kind:
            if parsed.path.rstrip("/").endswith("/copy"):
                kind = f"{kind} (make a copy)"
            return {"kind": "google_file", "title": kind, "host": host}
    return {"kind": "web", "host": host}


@dataclass(frozen=True)
class _DeckAgenda:
    score: int
    deck: PublishedDeck
    document: CanvasDocument
    slides: list[tuple[PublishedSlide, SlideHeading]]


def _internal_api_url(value: str, base_url: str, course_id: str) -> str | None:
    absolute = urljoin(f"{base_url}/", value)
    parsed = urlparse(absolute)
    base = urlparse(base_url)
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
        return None
    path = parsed.path
    api_prefix = f"/api/v1/courses/{course_id}/"
    course_prefix = f"/courses/{course_id}/"
    if path.startswith(api_prefix):
        return path + (f"?{parsed.query}" if parsed.query else "")
    if not path.startswith(course_prefix):
        return None
    suffix = path[len(course_prefix) :]
    if suffix.startswith("pages/"):
        slug = quote(suffix.removeprefix("pages/").strip("/"), safe="")
        return f"{api_prefix}pages/{slug}"
    if suffix.startswith("assignments/"):
        assignment_id = suffix.removeprefix("assignments/").split("/", 1)[0]
        if assignment_id.isdigit():
            return f"{api_prefix}assignments/{assignment_id}"
    return None


def _document_links(body: str, base_url: str, course_id: str) -> list[str]:
    parser = _parse_html(body)
    links: list[str] = []
    for node in parser.root.descendants({"a"}):
        for key in ("data-api-endpoint", "href"):
            endpoint = _internal_api_url(node.attrs.get(key, ""), base_url, course_id)
            if endpoint and endpoint not in links:
                links.append(endpoint)
    return links


class CanvasApiClient:
    def __init__(
        self, base_url: str, token: str, *, session: requests.Session | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    def _url(self, path_or_url: str) -> str:
        url = urljoin(f"{self.base_url}/", path_or_url)
        parsed = urlparse(url)
        base = urlparse(self.base_url)
        if (parsed.scheme, parsed.netloc) != (
            base.scheme,
            base.netloc,
        ) or not parsed.path.startswith("/api/v1/"):
            raise CanvasSourceError(
                "Refused to send the Canvas token outside this Canvas API origin."
            )
        return url

    def get(self, path_or_url: str, *, params: Any = None) -> Any:
        response = self.session.get(self._url(path_or_url), params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def list(self, path_or_url: str, *, params: Any = None) -> list[dict[str, Any]]:
        url = self._url(path_or_url)
        values: list[dict[str, Any]] = []
        current_params = params
        while url:
            response = self.session.get(url, params=current_params, timeout=30)
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                raise CanvasSourceError("Canvas returned an unexpected non-list response.")
            values.extend(item for item in page if isinstance(item, dict))
            next_link = getattr(response, "links", {}).get("next", {}).get("url")
            url = self._url(next_link) if next_link else ""
            current_params = None
        return values


class CanvasAgendaSource:
    def __init__(
        self,
        *,
        course_id: str,
        target_week_start: date,
        base_url: str | None = None,
        token: str | None = None,
        session: requests.Session | None = None,
        max_documents: int = 250,
        timezone_name: str = "UTC",
        current_week_start: date | None = None,
        published_session: requests.Session | None = None,
    ) -> None:
        resolved_base = (base_url or os.getenv("CANVAS_BASE_URL") or "").strip().rstrip("/")
        resolved_token = (
            token
            or os.getenv("CANVAS_TOKEN")
            or os.getenv("CANVAS_API_TOKEN")
            or os.getenv("CANVAS_API_KEY")
        )
        if not resolved_base:
            raise CanvasSourceError("CANVAS_BASE_URL is required for Canvas agenda discovery.")
        if not resolved_token:
            raise CanvasSourceError("CANVAS_TOKEN is required for Canvas agenda discovery.")
        self.course_id = course_id
        self.target_week_start = target_week_start
        self.timezone_name = timezone_name
        local_today = datetime.now(ZoneInfo(timezone_name)).date()
        self.current_week_start = current_week_start or (
            local_today - timedelta(days=local_today.weekday())
        )
        self.base_url = resolved_base
        self.client = CanvasApiClient(resolved_base, resolved_token, session=session)
        self.max_documents = max_documents
        # Published decks are public pages on docs.google.com. They are fetched with a separate,
        # credential-free session so the Canvas bearer token can never reach them.
        self._published_session = published_session
        self._module_items: dict[str, dict[str, Any]] = {}
        self._assignments: dict[str, dict[str, Any]] = {}
        self._quiz_assignments: dict[str, dict[str, Any]] = {}

    def _document(
        self, payload: dict[str, Any], kind: str, context: str = ""
    ) -> CanvasDocument | None:
        body = str(
            payload.get("body") or payload.get("description") or payload.get("syllabus_body") or ""
        )
        if not body.strip():
            return None
        key = str(payload.get("url") or payload.get("page_id") or payload.get("id") or kind)
        title = str(payload.get("title") or payload.get("name") or kind.replace("_", " ").title())
        html_url = str(payload.get("html_url") or f"{self.base_url}/courses/{self.course_id}")
        updated_on = None
        updated_at = payload.get("updated_at") or payload.get("last_edited_at")
        if updated_at:
            try:
                updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                updated_on = updated.astimezone(ZoneInfo(self.timezone_name)).date()
            except ValueError:
                pass
        return CanvasDocument(key, kind, title, body, html_url, context, updated_on)

    def _discover(self) -> tuple[list[CanvasDocument], list[str]]:
        prefix = f"/api/v1/courses/{self.course_id}"
        documents: list[CanvasDocument] = []
        warnings: list[str] = []
        queued: list[tuple[str, str]] = []
        seen_endpoints: set[str] = set()

        def add(payload: dict[str, Any], kind: str, context: str = "") -> None:
            document = self._document(payload, kind, context)
            if document and all(
                existing.key != document.key or existing.kind != document.kind
                for existing in documents
            ):
                documents.append(document)
                for link in _document_links(document.body, self.base_url, self.course_id):
                    queued.append((link, f"linked from {document.title}"))

        def optional(label: str, callback: Any) -> Any:
            try:
                return callback()
            except (requests.RequestException, ValueError, CanvasSourceError) as error:
                warnings.append(f"{label}: {type(error).__name__}")
                return None

        front = optional("front page", lambda: self.client.get(f"{prefix}/front_page"))
        if isinstance(front, dict):
            add(front, "front_page")

        course = optional(
            "syllabus",
            lambda: self.client.get(prefix, params=[("include[]", "syllabus_body")]),
        )
        if isinstance(course, dict) and course.get("syllabus_body"):
            add(course, "syllabus")

        modules = (
            optional(
                "modules",
                lambda: self.client.list(
                    f"{prefix}/modules",
                    params=[("include[]", "items"), ("per_page", 100)],
                ),
            )
            or []
        )
        for module in modules:
            module_context = str(module.get("name") or "")
            items = module.get("items")
            if not isinstance(items, list):
                module_id = module.get("id")
                items = (
                    optional(
                        f"module {module.get('id')} items",
                        lambda module_id=module_id: self.client.list(
                            f"{prefix}/modules/{module_id}/items", params={"per_page": 100}
                        ),
                    )
                    or []
                )
            for item in items:
                if item.get("id") is not None:
                    self._module_items[str(item["id"])] = item
                endpoint = item.get("url")
                if endpoint:
                    queued.append(
                        (str(endpoint), f"module {module_context}: {item.get('title', '')}")
                    )
                elif item.get("type") == "Page" and item.get("page_url"):
                    queued.append(
                        (f"{prefix}/pages/{quote(str(item['page_url']), safe='')}", module_context)
                    )

        pages = (
            optional(
                "pages",
                lambda: self.client.list(f"{prefix}/pages", params={"per_page": 100}),
            )
            or []
        )
        for page in pages:
            if page.get("body"):
                add(page, "page")
            elif page.get("url"):
                queued.append(
                    (
                        f"{prefix}/pages/{quote(str(page['url']), safe='')}",
                        str(page.get("title") or ""),
                    )
                )

        assignments = (
            optional(
                "assignments",
                lambda: self.client.list(f"{prefix}/assignments", params={"per_page": 100}),
            )
            or []
        )
        for assignment in assignments:
            if assignment.get("id") is not None:
                self._assignments[str(assignment["id"])] = assignment
            if assignment.get("quiz_id") is not None:
                self._quiz_assignments[str(assignment["quiz_id"])] = assignment
            if assignment.get("description"):
                add(assignment, "assignment")

        while queued and len(documents) < self.max_documents:
            endpoint, context = queued.pop(0)
            normalized = _internal_api_url(endpoint, self.base_url, self.course_id)
            if not normalized or normalized in seen_endpoints:
                continue
            seen_endpoints.add(normalized)
            payload = optional(
                "linked Canvas content", lambda endpoint=normalized: self.client.get(endpoint)
            )
            if isinstance(payload, dict):
                kind = "assignment" if "/assignments/" in normalized else "page"
                add(payload, kind, context)
        return documents, warnings

    def _assignment_link(
        self,
        assignment_id: str,
        title: str,
        *,
        kind: str = "canvas_assignment",
    ) -> dict[str, Any]:
        assignment = self._assignments.get(assignment_id) or {}
        link: dict[str, Any] = {
            "kind": kind,
            "title": str(assignment.get("name") or title),
            "assignment_url": (
                f"{self.base_url}/courses/{self.course_id}/assignments/{assignment_id}"
            ),
        }
        if assignment.get("due_at"):
            link["due_at"] = str(assignment["due_at"])
        return link

    def _canvas_link(self, url: str) -> dict[str, Any] | None:
        """Describe a link to this Canvas origin, resolving module items to their content.

        Slides link to module items ("/modules/items/930780"). The module listing gathered
        during discovery says which assignment or page each item is, without another request.
        """
        parsed = urlparse(url)
        base = urlparse(self.base_url)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            return None
        match = re.match(r"^/courses/(?P<course>\d+)(?:/(?P<rest>.*))?$", parsed.path)
        if match is None or match.group("course") != self.course_id:
            return {"kind": "canvas"}
        rest = (match.group("rest") or "").strip("/")
        item_match = re.fullmatch(r"modules/items/(\d+)", rest)
        if item_match:
            item = self._module_items.get(item_match.group(1))
            if item is None:
                return {"kind": "canvas"}
            title = str(item.get("title") or "")
            content_id = str(item.get("content_id") or "")
            if item.get("type") == "Assignment" and content_id.isdigit():
                return self._assignment_link(content_id, title)
            if item.get("type") == "Quiz":
                quiz_assignment = self._quiz_assignments.get(content_id)
                if quiz_assignment is not None:
                    return self._assignment_link(
                        str(quiz_assignment["id"]), title, kind="canvas_quiz"
                    )
                return {"kind": "canvas_quiz", "title": title}
            if item.get("type") == "Page":
                return {"kind": "canvas_page", "title": title}
            return {"kind": "canvas", "title": title}
        assignment_match = re.match(r"assignments/(\d+)(?:/|$)", rest)
        if assignment_match:
            return self._assignment_link(assignment_match.group(1), "")
        if rest.startswith("pages/"):
            return {"kind": "canvas_page"}
        return {"kind": "canvas"}

    def _embedded_decks(self, documents: list[CanvasDocument]) -> list[tuple[str, CanvasDocument]]:
        """Return embedded decks, most agenda-like first, each with the page that best hosts it.

        Courses also embed published lecture decks on every notes page, so fetching in
        discovery order could spend every fetch on notes. A deck on a page titled as an agenda
        comes first, then one on the front page or syllabus. Its host is the smallest such
        page, which is the page dedicated to the deck rather than one that merely repeats it.
        """
        hosts: dict[str, tuple[tuple[int, int, int], CanvasDocument]] = {}
        for index, document in enumerate(documents):
            if "/presentation/d/e/" not in document.body:
                continue
            labels = f"{document.title} {document.context}".casefold()
            priority = (
                2
                if "agenda" in labels
                else 1
                if document.kind in {"front_page", "syllabus"}
                else 0
            )
            rank = (-priority, len(document.body), index)
            for deck_id in _embedded_deck_ids(document.body):
                if deck_id not in hosts or rank < hosts[deck_id][0]:
                    hosts[deck_id] = (rank, document)
        ordered = sorted(hosts.items(), key=lambda item: (item[1][0][0], item[1][0][2]))
        return [(deck_id, document) for deck_id, (_, document) in ordered]

    def _deck_agenda(
        self, documents: list[CanvasDocument], warnings: list[str]
    ) -> tuple[_DeckAgenda | None, list[str]]:
        """Read the course's daily-agenda deck, if it embeds one, for the target week.

        Decks are read in agenda-likeness order until one proves to be a daily agenda (several
        slides with class-day headings); later decks are never fetched.
        """
        embedded = self._embedded_decks(documents)
        if not embedded:
            return None, []

        notes: list[str] = []
        best: _DeckAgenda | None = None
        week_end = self.target_week_start + timedelta(days=6)
        session = self._published_session or requests.Session()
        try:
            for deck_id, document in embedded[:MAX_EMBEDDED_DECKS]:
                try:
                    deck = fetch_published_deck(deck_id, session=session)
                except PublishedSlidesError as error:
                    warnings.append(f"published Slides deck in {document.title}: {error}")
                    notes.append(
                        f'The published Slides deck in "{document.title}" could not be read: '
                        f"{error}"
                    )
                    continue
                if deck.missing_slide_ids:
                    warnings.append(
                        f"published Slides deck in {document.title}: "
                        f"{len(deck.missing_slide_ids)} slide(s) were not in the viewer page"
                    )
                dated = [
                    (slide, heading)
                    for slide in deck.slides
                    if (heading := slide_heading(slide, self.target_week_start)) is not None
                ]
                if len(dated) < MIN_DATED_DECK_SLIDES:
                    continue
                in_week = sorted(
                    (item for item in dated if overlaps_week(item[1], self.target_week_start)),
                    key=lambda item: (item[1].start, item[1].end, item[0].position),
                )
                if not in_week:
                    notes.append(
                        f'The published Slides agenda in "{document.title}" has no slide dated '
                        "in that week yet."
                    )
                    break
                days = {
                    value
                    for _, heading in in_week
                    for value in heading.dates
                    if self.target_week_start <= value <= week_end
                }
                score = DECK_BASE_SCORE + min(
                    DECK_MAX_COVERAGE_SCORE, DECK_SCORE_PER_DAY * len(days)
                )
                best = _DeckAgenda(score, deck, document, in_week)
                break
        finally:
            if self._published_session is None:
                session.close()
        return best, notes

    def _slide_links(
        self, links: tuple[SlideLink, ...]
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        records: list[dict[str, Any]] = []
        assignment_links: list[dict[str, str]] = []
        for link in links:
            described = self._canvas_link(link.url) or _describe_web_link(link.url)
            assignment_url = described.pop("assignment_url", None)
            due_at = described.pop("due_at", None)
            records.append({"text": link.text, "url": link.url, **described})
            if assignment_url:
                linked = {
                    "url": assignment_url,
                    "text": link.text,
                    "title": str(described.get("title") or ""),
                }
                if due_at:
                    linked["due_at"] = due_at
                assignment_links.append(linked)
        return records, assignment_links

    def _deck_capture(
        self,
        agenda: _DeckAgenda,
        documents: list[CanvasDocument],
        warnings: list[str],
    ) -> SourceCapture:
        deck = agenda.deck
        element_id = f"published_slides:{deck.published_id}"
        blocks: list[AgendaBlock] = []
        for row_index, (slide, heading) in enumerate(agenda.slides):
            row_label = weekday_label(heading.dates)
            row_dates = [value.isoformat() for value in heading.dates]
            # Google object IDs are unique within a deck, so the slide ID anchors its heading
            # and each text box ID anchors its text. One short ID per anchor is also what
            # Gemini copies back reliably; slide-plus-shape anchors came back truncated.
            parts: list[tuple[str, str, BlockRole, str, tuple[SlideLink, ...]]] = [
                (slide.slide_id, "slide_heading", BlockRole.DAY, heading.text, ())
            ]
            for text in slide.texts:
                body = text.text
                if text.shape_id == heading.shape_id:
                    body = body.partition("\n")[2]
                if body.strip():
                    parts.append((text.shape_id, "slide_text", BlockRole.UNKNOWN, body, text.links))
            for object_id, kind, role, body, links in parts:
                records, assignment_links = self._slide_links(links)
                blocks.append(
                    AgendaBlock(
                        anchor=f"canvas:slides:{_safe_anchor(object_id)}",
                        element_id=element_id,
                        kind=kind,
                        role=role,
                        row_index=row_index,
                        row_label=row_label,
                        text=body.strip(),
                        order=len(blocks) + 1,
                        slide_id=slide.slide_id,
                        metadata={
                            "canvas_kind": "published_slides",
                            "row_dates": row_dates,
                            "links": records,
                            "assignment_links": assignment_links,
                        },
                    )
                )
        document_key = f"published_slides:{deck.published_id}"
        canonical = {
            "course_id": self.course_id,
            "week_start": self.target_week_start.isoformat(),
            "document": {"key": document_key, "kind": "published_slides", "title": deck.title},
            "blocks": [block.model_dump(mode="json") for block in blocks],
        }
        page_hash = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        first_heading = agenda.slides[0][1]
        return SourceCapture(
            source_key=f"canvas:{self.course_id}:week:{self.target_week_start.isoformat()}",
            source_url=agenda.document.html_url,
            source_type="canvas",
            resource_id=self.course_id,
            page_id=document_key,
            page_hash=page_hash,
            transcript=_transcript(blocks),
            blocks=blocks,
            captured_at=datetime.now(UTC),
            selection={
                "week_start": self.target_week_start.isoformat(),
                "matched_text": first_heading.text,
                "slide_ids": [slide.slide_id for slide, _ in agenda.slides],
            },
            source_metadata={
                "title": deck.title,
                "canvas_kind": "published_slides",
                "agenda_format": "daily_slides",
                "embedded_in": agenda.document.title,
                "match_score": agenda.score,
                "matched_start": first_heading.start.isoformat(),
                "canvas_updated_on": None,
                "deck_revision": deck.revision,
                "deck_slide_count": len(deck.slides),
                "slides_in_week": len(agenda.slides),
                "documents_checked": len(documents),
                "warnings": warnings,
                "screenshot_available": False,
            },
        )

    def capture(self, *, include_image: bool) -> SourceCapture:
        if include_image:
            raise CanvasSourceError("Canvas API agenda captures provide text, not screenshots.")
        documents, warnings = self._discover()
        ranked: list[tuple[int, CanvasDocument, HtmlNode, WeekTextMatch]] = []
        for document in documents:
            parser = _parse_html(document.body)
            combined = " ".join((document.title, document.context, parser.root.text(" ")))
            score = week_match_score(combined, self.target_week_start)
            if score:
                try:
                    node, matched = _agenda_node(parser, self.target_week_start)
                except CanvasAgendaNotFound:
                    context_matches = find_week_matches(
                        " ".join((document.title, document.context)), self.target_week_start
                    )
                    if not context_matches:
                        continue
                    node, matched = parser.root, context_matches[0]
            else:
                relative_context = THIS_WEEK_RE.search(combined)
                updated_for_target = (
                    document.updated_on is not None
                    and self.target_week_start
                    <= document.updated_on
                    <= self.target_week_start + timedelta(days=6)
                )
                if (
                    relative_context is None
                    or self.target_week_start != self.current_week_start
                    or not updated_for_target
                ):
                    continue
                score = 120
                try:
                    node, matched = _relative_agenda_node(parser, self.target_week_start)
                except CanvasAgendaNotFound:
                    node = parser.root
                    matched = WeekTextMatch(
                        self.target_week_start,
                        120,
                        relative_context.group(0),
                        relative_context.start(),
                    )
            if not _sufficient_agenda_content(node):
                continue
            lowered = node.text(" ").casefold()
            sufficiency = sum(term in lowered for term in AGENDA_TERMS) + sum(
                day in lowered for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
            )
            ranked.append((score + min(40, sufficiency * 5), document, node, matched))
        best_page = max(ranked, key=lambda item: item[0]) if ranked else None
        deck_agenda, deck_notes = self._deck_agenda(documents, warnings)
        if deck_agenda is not None and (best_page is None or deck_agenda.score >= best_page[0]):
            return self._deck_capture(deck_agenda, documents, warnings)
        if best_page is None:
            raise CanvasAgendaNotFound(
                " ".join(
                    [
                        "No sufficiently specific Canvas agenda was found for the week of "
                        f"{self.target_week_start.isoformat()} "
                        f"after checking {len(documents)} content item(s).",
                        *deck_notes,
                    ]
                )
            )
        score, document, node, matched = best_page
        blocks = _agenda_blocks(node, document, self.target_week_start)
        if not blocks:
            raise CanvasAgendaNotFound(
                "The Canvas agenda page mixes several weeks and none of its content "
                f"belongs to the week of {self.target_week_start.isoformat()}."
            )
        transcript = _transcript(blocks)
        canonical = {
            "course_id": self.course_id,
            "week_start": self.target_week_start.isoformat(),
            "document": {"key": document.key, "kind": document.kind, "title": document.title},
            "blocks": [block.model_dump(mode="json") for block in blocks],
        }
        page_hash = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return SourceCapture(
            source_key=f"canvas:{self.course_id}:week:{self.target_week_start.isoformat()}",
            source_url=document.html_url,
            source_type="canvas",
            resource_id=self.course_id,
            page_id=document.key,
            page_hash=page_hash,
            transcript=transcript,
            blocks=blocks,
            captured_at=datetime.now(UTC),
            selection={
                "week_start": self.target_week_start.isoformat(),
                "matched_text": matched.matched_text,
            },
            source_metadata={
                "title": document.title,
                "canvas_kind": document.kind,
                "match_score": score,
                "matched_start": matched.start.isoformat(),
                "canvas_updated_on": (
                    document.updated_on.isoformat() if document.updated_on else None
                ),
                "documents_checked": len(documents),
                "warnings": warnings,
                "screenshot_available": False,
            },
        )
