from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from canvas_task_sync.models import AgendaBlock, BlockRole, SourceCapture

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
DAY_RE = re.compile(
    r"^(?:m|t|w|th|f|mon|tue|wed|thu|fri|monday|tuesday|wednesday|thursday|friday)\.?$", re.I
)
SEMANTIC_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}
THIS_WEEK_RE = re.compile(
    r"\b(?:this\s+week(?:['’]?s)?(?:\s+agenda)?|agenda\s+(?:for\s+)?this\s+week)\b",
    re.IGNORECASE,
)
CANVAS_ASSIGNMENT_PATH_RE = re.compile(r"/courses/\d+/assignments/\d+(?:/|$)")


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


def find_week_matches(text: str, target_week_start: date) -> list[WeekTextMatch]:
    matches: list[WeekTextMatch] = []
    for pattern in (MONTH_DATE_RE, NUMERIC_DATE_RE):
        for found in pattern.finditer(text):
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
            start = (
                min(candidates, key=lambda value: abs((value - target_week_start).days))
                if candidates
                else None
            )
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
            if found.group("week"):
                score += 20
            if found.group("range"):
                score += 8
            surrounding = text[max(0, found.start() - 180) : found.end() + 240].casefold()
            score += min(24, 6 * sum(term in surrounding for term in AGENDA_TERMS))
            matches.append(WeekTextMatch(start, score, found.group(0), found.start()))
    return sorted(matches, key=lambda item: (-item.score, item.position))


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
    for node in [parser.root, *parser.root.descendants()]:
        text = node.text(" ")
        if len(text) < 25:
            continue
        matches = find_week_matches(text, target_week_start)
        if not matches:
            continue
        best = matches[0]
        lowered = text.casefold()
        agenda_signals = sum(term in lowered for term in AGENDA_TERMS)
        weekday_signals = sum(
            day in lowered for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
        )
        tables = sum(1 for _ in node.descendants({"table"}))
        score = (
            best.score + (agenda_signals * 13) + min(20, weekday_signals * 4) + min(15, tables * 8)
        )
        score -= min(35, max(0, len(text) - 2500) / 300)
        candidates.append((score, -len(text), node, best))
    if not candidates:
        raise CanvasAgendaNotFound(
            "Canvas content did not contain a recognizable heading for the week of "
            f"{target_week_start.strftime('%B')} {target_week_start.day}."
        )
    _, _, node, matched = max(candidates, key=lambda item: (item[0], item[1]))
    return node, matched


def _relative_agenda_node(
    parser: CanvasHtmlParser, target_week_start: date
) -> tuple[HtmlNode, WeekTextMatch]:
    candidates: list[tuple[float, int, HtmlNode, WeekTextMatch]] = []
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
        tables = sum(1 for _ in node.descendants({"table"}))
        score = 120 + (agenda_signals * 13) + min(20, weekday_signals * 4) + min(15, tables * 8)
        score -= min(35, max(0, len(text) - 2500) / 300)
        matched = WeekTextMatch(target_week_start, 120, found.group(0), found.start())
        candidates.append((score, -len(text), node, matched))
    if not candidates:
        raise CanvasAgendaNotFound("Canvas content did not contain a current-week agenda heading.")
    _, _, node, matched = max(candidates, key=lambda item: (item[0], item[1]))
    return node, matched


def _direct_children(node: HtmlNode, tags: set[str]) -> list[HtmlNode]:
    return [child for child in node.children if isinstance(child, HtmlNode) and child.tag in tags]


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


def _agenda_blocks(node: HtmlNode, document: CanvasDocument) -> list[AgendaBlock]:
    blocks: list[AgendaBlock] = []
    order = 0

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
        normalized = " ".join(text.split()).strip()
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

    table_nodes = list(node.descendants({"table"}))
    for table_index, table in enumerate(table_nodes):
        rows = list(table.descendants({"tr"}))
        headers: list[str] = []
        for row_index, row in enumerate(rows):
            cells = _direct_children(row, {"td", "th"})
            if not cells:
                continue
            values = [cell.text(" ") for cell in cells]
            if not headers and any(
                "assignment" in value.casefold() or "activit" in value.casefold()
                for value in values
            ):
                headers = values
            row_label = next(
                (value.rstrip(".") for value in values if DAY_RE.fullmatch(value.strip())), None
            )
            for column_index, value in enumerate(values):
                lowered_header = (
                    headers[column_index].casefold() if column_index < len(headers) else ""
                )
                if row_label and DAY_RE.fullmatch(value.strip()):
                    role = BlockRole.DAY
                elif "assignment" in lowered_header or "homework" in lowered_header:
                    role = BlockRole.ASSIGNMENTS
                elif "activit" in lowered_header or "lesson" in lowered_header:
                    role = BlockRole.LEARNING
                elif row_index == 0:
                    role = BlockRole.HEADER
                else:
                    role = BlockRole.UNKNOWN
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
        if id(semantic) in table_descendants:
            continue
        text = semantic.text(" ")
        role = BlockRole.HEADER if semantic.tag.startswith("h") else BlockRole.UNKNOWN
        append(
            text,
            semantic.tag,
            role,
            assignment_links=_assignment_links(semantic, document.html_url),
        )

    if not blocks:
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
    has_table = any(True for _ in node.descendants({"table"}))
    return len(text) >= 60 and (
        agenda_structure or weekday_count >= 2 or (has_table and weekday_count >= 1)
    )


def _transcript(blocks: list[AgendaBlock]) -> str:
    sections: list[str] = []
    for block in blocks:
        context = [f"anchor={block.anchor}", f"role={block.role.value}", f"order={block.order}"]
        if block.row_label:
            context.append(f"day={block.row_label}")
        sections.append(f"[{' '.join(context)}]\n{block.text}")
    return "\n\n".join(sections)


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
        if not ranked:
            raise CanvasAgendaNotFound(
                "No sufficiently specific Canvas agenda was found for the week of "
                f"{self.target_week_start.isoformat()} "
                f"after checking {len(documents)} content item(s)."
            )
        score, document, node, matched = max(ranked, key=lambda item: item[0])
        blocks = _agenda_blocks(node, document)
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
