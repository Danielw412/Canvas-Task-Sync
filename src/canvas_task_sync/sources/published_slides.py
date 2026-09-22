"""Read a Google Slides deck that a teacher published to the web and embedded in Canvas.

A published deck ("File > Share > Publish to web") is addressed by an opaque ``2PACX-...`` ID
that the Slides API does not accept. Its public viewer page, however, carries every slide as an
SVG string in which each text box exposes its full text through an ``aria-label`` and each link
is an ``<a>`` element. This module fetches that page without any credentials and turns it into
slides, text boxes, and links. It performs acquisition only: slide dates come from the slide's
own heading, and deciding what is a task stays with Gemini and the scheduling policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import requests

PUBLISHED_DECK_URL_RE = re.compile(
    r"^https://docs\.google\.com/presentation/d/e/(?P<id>[A-Za-z0-9_-]{16,200})/"
    r"(?:pub|pubembed|embed)(?:[/?#]|$)"
)
VIEWER_URL_TEMPLATE = "https://docs.google.com/presentation/d/e/{published_id}/pub?start=false"
# The viewer page is about 100 KB per slide. A full school year of daily slides stays well
# below this, and a larger response is refused rather than read into memory.
MAX_VIEWER_BYTES = 40 * 1024 * 1024
VIEWER_TIMEOUT_SECONDS = 30
# JavaScript string escapes Google uses inside ``SK_svgData = '...'``.
SVG_DATA_RE = re.compile(r"SK_svgData\s*=\s*'([^'\\]*(?:\\.[^'\\]*)*)'", re.S)
JS_ESCAPE_RE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.S)
JS_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
TITLE_RE = re.compile(r"<title>(?P<title>[^<]*)</title>", re.I)
REVISION_RE = re.compile(r"\brevision:\s*(?P<revision>\d+(?:\.\d+)?)")
DOC_DATA_MARKER = "docData:"

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
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAY = r"(?:mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:[a-z]*)\.?"
_ORDINAL = r"(?:st|nd|rd|th)?"
_SEPARATOR = r"\s*(?:,\s*(?:and|&)?|and|&|-|–|—|to|through|thru)\s*"
_DAY_COUNTER = r"(?:days?|class|lesson)\s*#?\s*\d{1,3}(?:\s*(?:,|and|&|-|–|—)\s*\d{1,3})*"
# A slide heading is a whole line that names the class day(s) and nothing else:
# "Day 25: September 22", "Days 23 and 24: September 18 and 21", "Tuesday, Sept. 22",
# "Day 25 - 9/22". A date inside a sentence ("Test on September 16") is not a heading.
HEADING_RE = re.compile(
    rf"^\s*(?:{_DAY_COUNTER}\s*[:.\-–—]?\s*)?(?P<dates>(?:{_WEEKDAY},?\s*)?"
    rf"(?:(?:{_MONTH})\.?\s+\d{{1,2}}{_ORDINAL}|\d{{1,2}}/\d{{1,2}})"
    rf"(?:{_SEPARATOR}(?:{_WEEKDAY},?\s*)?(?:(?:{_MONTH})\.?\s+)?\d{{1,2}}(?:/\d{{1,2}})?"
    rf"{_ORDINAL})*)\s*[.:]?\s*$",
    re.IGNORECASE,
)
DATE_TOKEN_RE = re.compile(
    rf"(?P<separator>{_SEPARATOR})?(?:{_WEEKDAY},?\s*)?"
    rf"(?:(?P<month>{_MONTH})\.?\s+(?P<day>\d{{1,2}}){_ORDINAL}"
    rf"|(?P<numeric_month>\d{{1,2}})/(?P<numeric_day>\d{{1,2}})"
    rf"|(?P<bare_day>\d{{1,2}}){_ORDINAL})",
    re.IGNORECASE,
)
MAX_HEADING_CHARS = 80
MAX_HEADING_SPAN_DAYS = 14
MAX_YEAR_DISTANCE_DAYS = 190


class PublishedSlidesError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlideLink:
    text: str
    url: str


@dataclass(frozen=True)
class SlideText:
    shape_id: str
    text: str
    links: tuple[SlideLink, ...] = ()


@dataclass(frozen=True)
class PublishedSlide:
    slide_id: str
    position: int
    texts: tuple[SlideText, ...]


@dataclass(frozen=True)
class PublishedDeck:
    published_id: str
    title: str
    revision: str | None
    slides: tuple[PublishedSlide, ...]
    # Slide IDs the viewer listed without inlining their SVG, so their text is unknown.
    missing_slide_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlideHeading:
    shape_id: str
    text: str
    dates: tuple[date, ...]

    @property
    def start(self) -> date:
        return self.dates[0]

    @property
    def end(self) -> date:
        return self.dates[-1]


def published_deck_id(url: str) -> str | None:
    """Return the published deck ID of a viewer/embed URL, or None for any other URL."""
    match = PUBLISHED_DECK_URL_RE.match(url.strip())
    return match.group("id") if match else None


def viewer_url(published_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,200}", published_id):
        raise PublishedSlidesError("Refused to fetch a malformed published Slides ID.")
    return VIEWER_URL_TEMPLATE.format(published_id=published_id)


def _js_unescape(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        escape = match.group(1)
        if len(escape) > 1 and escape[0] in "xu":
            return chr(int(escape[1:], 16))
        return JS_SIMPLE_ESCAPES.get(escape, escape)

    return JS_ESCAPE_RE.sub(replace, value)


def _clean_link_url(value: str) -> str | None:
    """Unwrap Google's ``/url?q=`` redirect so volatile tracking parameters never reach hashes."""
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.hostname in {"www.google.com", "google.com"} and parsed.path == "/url":
        targets = parse_qs(parsed.query).get("q")
        if not targets:
            return None
        return _clean_link_url(targets[0])
    return value.strip()


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _clean_text(value: str) -> str:
    return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


class _SlideSvgParser(HTMLParser):
    """Collect one slide's ID, text boxes, and text links in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.slide_id: str | None = None
        self.shapes: list[tuple[str, str]] = []
        self.links: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        element_id = values.get("id", "")
        if tag == "g" and element_id:
            if element_id.startswith("a11y-"):
                label = _clean_text(values.get("aria-label", ""))
                if values.get("role") == "img" and label:
                    self.shapes.append((element_id.removeprefix("a11y-"), label))
            elif self.slide_id is None:
                self.slide_id = element_id
        elif tag == "a":
            label = " ".join(values.get("aria-label", "").split())
            url = _clean_link_url(values.get("xlink:href") or values.get("href") or "")
            # Links without a label wrap images such as navigation buttons, not agenda text.
            if label and url:
                self.links.append((len(self.shapes) - 1, label, url))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _slide_from_svg(svg: str, position: int) -> PublishedSlide | None:
    parser = _SlideSvgParser()
    parser.feed(svg)
    parser.close()
    if parser.slide_id is None:
        return None
    links: dict[int, list[SlideLink]] = {}
    for preceding, label, url in parser.links:
        needle = _normalized(label)
        owners = [
            index
            for index, (_, text) in enumerate(parser.shapes)
            if needle and needle in _normalized(text)
        ]
        # A link overlay follows the text box it belongs to; fall back to any text box that
        # contains the link text when the renderer orders them differently.
        owner = preceding if preceding in owners else (owners[0] if owners else None)
        if owner is None:
            continue
        link = SlideLink(label, url)
        if link not in links.setdefault(owner, []):
            links[owner].append(link)
    texts = tuple(
        SlideText(shape_id, text, tuple(links.get(index, [])))
        for index, (shape_id, text) in enumerate(parser.shapes)
    )
    return PublishedSlide(parser.slide_id, position, texts)


def _doc_data_slide_ids(html: str) -> list[str] | None:
    """Return the viewer's slide list, or None when Google changed the page structure."""
    start = html.find(DOC_DATA_MARKER)
    if start < 0:
        return None
    index = start + len(DOC_DATA_MARKER)
    while index < len(html) and html[index].isspace():
        index += 1
    if index >= len(html) or html[index] != "[":
        return None
    depth = 0
    in_string = False
    escaped = False
    for end in range(index, len(html)):
        character = html[end]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(html[index : end + 1])
                    return [
                        str(slide[0])
                        for slide in payload[1]
                        if isinstance(slide, list) and slide and isinstance(slide[0], str)
                    ]
                except (ValueError, IndexError, TypeError):
                    return None
    return None


def parse_viewer_html(html: str, published_id: str) -> PublishedDeck:
    slides: list[PublishedSlide] = []
    seen: set[str] = set()
    for match in SVG_DATA_RE.finditer(html):
        slide = _slide_from_svg(_js_unescape(match.group(1)), len(slides))
        if slide is not None and slide.slide_id not in seen:
            seen.add(slide.slide_id)
            slides.append(slide)
    if not slides:
        raise PublishedSlidesError(
            "The published Slides viewer did not contain readable slides; the deck may be "
            "unpublished or Google may have changed the viewer format."
        )
    listed = _doc_data_slide_ids(html) or []
    missing = tuple(slide_id for slide_id in listed if slide_id not in seen)
    title_match = TITLE_RE.search(html)
    title = _js_unescape(title_match.group("title")) if title_match else ""
    title = re.sub(r"\s*-\s*Google (?:Slides|Docs)\s*$", "", " ".join(title.split()))
    revision = REVISION_RE.search(html)
    return PublishedDeck(
        published_id=published_id,
        title=title or "Published Google Slides deck",
        revision=revision.group("revision") if revision else None,
        slides=tuple(slides),
        missing_slide_ids=missing,
    )


def fetch_published_deck(
    published_id: str,
    *,
    session: requests.Session | None = None,
    max_bytes: int = MAX_VIEWER_BYTES,
) -> PublishedDeck:
    """Fetch the public viewer page. The session must never carry school credentials."""
    url = viewer_url(published_id)
    client = session or requests.Session()
    try:
        # A published deck answers directly. A redirect means it is unpublished or needs a
        # sign-in, and following it would only lead to an account page.
        response = client.get(
            url,
            timeout=VIEWER_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as error:
        raise PublishedSlidesError(
            f"Could not reach the published Slides deck ({type(error).__name__})."
        ) from error
    try:
        if response.status_code != 200:
            raise PublishedSlidesError(
                f"The published Slides deck returned HTTP {response.status_code}."
            )
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise PublishedSlidesError("The published Slides deck exceeded the size limit.")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=256 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise PublishedSlidesError("The published Slides deck exceeded the size limit.")
            chunks.append(chunk)
    except requests.RequestException as error:
        raise PublishedSlidesError(
            f"Could not read the published Slides deck ({type(error).__name__})."
        ) from error
    finally:
        response.close()
    html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    return parse_viewer_html(html, published_id)


def _nearest_year_date(month: int, day: int, reference: date) -> date | None:
    candidates: list[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    best = min(candidates, key=lambda value: abs((value - reference).days), default=None)
    if best is None or abs((best - reference).days) > MAX_YEAR_DISTANCE_DAYS:
        return None
    return best


def parse_heading_dates(line: str, reference: date) -> tuple[date, ...] | None:
    """Return the class dates a slide heading names, or None when the line is not a heading.

    "Days 23 and 24: September 18 and 21" names two class days; "September 8-11" names a span,
    returned as its first and last day. A later day number without a month continues the
    previous month, and a year is inferred as the one nearest ``reference``.
    """
    stripped = " ".join(line.split())
    if not stripped or len(stripped) > MAX_HEADING_CHARS:
        return None
    heading = HEADING_RE.match(stripped)
    if heading is None:
        return None
    dates: list[date] = []
    month: int | None = None
    for token in DATE_TOKEN_RE.finditer(heading.group("dates")):
        if token.group("month"):
            month = MONTHS[token.group("month").casefold()]
            day = int(token.group("day"))
        elif token.group("numeric_month"):
            month = int(token.group("numeric_month"))
            day = int(token.group("numeric_day"))
        elif month is not None:
            day = int(token.group("bare_day"))
        else:
            return None
        if dates:
            anchor = dates[-1]
            try:
                value = date(anchor.year, month, day)
            except ValueError:
                return None
            if value < anchor:
                try:
                    value = date(anchor.year + 1, month, day)
                except ValueError:
                    return None
        else:
            found = _nearest_year_date(month, day, reference)
            if found is None:
                return None
            value = found
        # A span ("September 8-11") keeps only its endpoints: scheduling needs the row's
        # first and last class day, not every day between them.
        dates.append(value)
    if not dates or dates != sorted(dates) or (dates[-1] - dates[0]).days > MAX_HEADING_SPAN_DAYS:
        return None
    return tuple(dict.fromkeys(dates))


def slide_heading(slide: PublishedSlide, reference: date) -> SlideHeading | None:
    """Return the first text box line that is a dated class-day heading."""
    for text in slide.texts:
        first_line = text.text.split("\n", 1)[0]
        dates = parse_heading_dates(first_line, reference)
        if dates:
            return SlideHeading(text.shape_id, first_line, dates)
    return None


def weekday_label(dates: tuple[date, ...]) -> str:
    first = WEEKDAYS[dates[0].weekday()].title()
    last = WEEKDAYS[dates[-1].weekday()].title()
    return first if first == last else f"{first}/{last}"


def overlaps_week(heading: SlideHeading, week_start: date) -> bool:
    return heading.start <= week_start + timedelta(days=6) and heading.end >= week_start


__all__ = [
    "MAX_VIEWER_BYTES",
    "PublishedDeck",
    "PublishedSlide",
    "PublishedSlidesError",
    "SlideHeading",
    "SlideLink",
    "SlideText",
    "fetch_published_deck",
    "overlaps_week",
    "parse_heading_dates",
    "parse_viewer_html",
    "published_deck_id",
    "slide_heading",
    "viewer_url",
    "weekday_label",
]
