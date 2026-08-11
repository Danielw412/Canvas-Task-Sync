#!/usr/bin/env python3

"""
Find Google Sheets links embedded in a Canvas course.

Environment variables:
    CANVAS_BASE_URL=https://your-school.instructure.com
    CANVAS_TOKEN=your_canvas_access_token
    CANVAS_COURSE_ID=12345

Install:
    pip install requests

Run:
    python extract_canvas_sheets.py
"""

from __future__ import annotations

import html
import os
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from dotenv import load_dotenv


# Load .env from the same directory as this script.
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIRECTORY / ".env")

CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL", "").rstrip("/")
CANVAS_TOKEN = os.getenv("CANVAS_TOKEN", "")
COURSE_ID = os.getenv("CANVAS_COURSE_ID", "")

SHEETS_URL_PATTERN = re.compile(
    r"https?://docs\.google\.com/spreadsheets/"
    r"(?:u/\d+/)?"
    r"d/(?:e/)?[A-Za-z0-9_-]+"
    r"(?:[^\s\"'<>]*)?",
    re.IGNORECASE,
)

SPREADSHEET_ID_PATTERN = re.compile(
    r"docs\.google\.com/spreadsheets/"
    r"(?:u/\d+/)?"
    r"d/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SheetMatch:
    course_id: str
    source_type: str
    source_name: str
    source_id: str
    url: str
    spreadsheet_id: str | None


class LinkExtractor(HTMLParser):
    """Extract href and src attributes from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src", "data-src"} and value:
                self.urls.append(value)


class CanvasClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )

    def get(self, path: str, params: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_all(self, path: str, params: Any = None) -> list[dict[str, Any]]:
        """Follow Canvas pagination links."""

        url: str | None = f"{self.base_url}{path}"
        results: list[dict[str, Any]] = []
        first_request = True

        while url:
            response = self.session.get(
                url,
                params=params if first_request else None,
                timeout=30,
            )
            response.raise_for_status()

            data = response.json()
            if not isinstance(data, list):
                raise TypeError(f"Expected a list from {url}")

            results.extend(data)
            url = response.links.get("next", {}).get("url")
            first_request = False

        return results


def repeatedly_decode(value: str, rounds: int = 3) -> str:
    """Decode HTML entities and percent-encoding several times."""

    decoded = value

    for _ in range(rounds):
        new_value = unquote(html.unescape(decoded))
        if new_value == decoded:
            break
        decoded = new_value

    return decoded


def extract_urls_from_redirect(url: str) -> list[str]:
    """
    Some Canvas links wrap the real URL in query parameters such as:
    ?url=https%3A%2F%2Fdocs.google.com%2F...
    """

    results = [url]

    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        for parameter in ("url", "target", "redirect", "redirect_uri", "href"):
            for value in query.get(parameter, []):
                results.append(repeatedly_decode(value))
    except ValueError:
        pass

    return results


def clean_url(url: str) -> str:
    return url.rstrip(".,);]}")


def spreadsheet_id_from_url(url: str) -> str | None:
    match = SPREADSHEET_ID_PATTERN.search(url)
    return match.group(1) if match else None


def find_google_sheet_urls(content: str | None) -> set[str]:
    if not content:
        return set()

    decoded_content = repeatedly_decode(content)
    candidates: list[str] = [decoded_content]

    parser = LinkExtractor()

    try:
        parser.feed(decoded_content)
        candidates.extend(parser.urls)
    except Exception:
        # The regex scan below can still find URLs in malformed HTML.
        pass

    found: set[str] = set()

    for candidate in candidates:
        for expanded_candidate in extract_urls_from_redirect(candidate):
            decoded_candidate = repeatedly_decode(expanded_candidate)

            for match in SHEETS_URL_PATTERN.finditer(decoded_candidate):
                found.add(clean_url(match.group(0)))

    return found


def add_matches(
    matches: set[SheetMatch],
    *,
    course_id: str,
    source_type: str,
    source_name: str,
    source_id: str,
    content: str | None,
) -> None:
    for url in find_google_sheet_urls(content):
        matches.add(
            SheetMatch(
                course_id=course_id,
                source_type=source_type,
                source_name=source_name,
                source_id=source_id,
                url=url,
                spreadsheet_id=spreadsheet_id_from_url(url),
            )
        )


def scan_course(client: CanvasClient, course_id: str) -> list[SheetMatch]:
    matches: set[SheetMatch] = set()

    print(f"Scanning course {course_id}...")

    # Course name and syllabus
    course = client.get(
        f"/api/v1/courses/{course_id}",
        params={"include[]": "syllabus_body"},
    )

    course_name = course.get("name", f"Course {course_id}")

    add_matches(
        matches,
        course_id=course_id,
        source_type="syllabus",
        source_name=course_name,
        source_id=course_id,
        content=course.get("syllabus_body"),
    )

    # Pages: the page-list response may not contain the full body,
    # so retrieve each individual page.
    # Pages may be disabled for some courses.
    try:
        pages = client.get_all(
            f"/api/v1/courses/{course_id}/pages",
            params={"per_page": 100},
        )
    except requests.HTTPError as error:
        response = error.response

        if (
            response is not None
            and response.status_code == 404
            and "disabled for this course" in response.text.lower()
        ):
            print("  Pages: disabled; skipping")
            pages = []
        else:
            raise

    print(f"  Pages: {len(pages)}")

    for page_summary in pages:
        page_url = page_summary.get("url")
        if not page_url:
            continue

        try:
            page = client.get(
                f"/api/v1/courses/{course_id}/pages/{page_url}"
            )
        except requests.HTTPError as error:
            print(
                f"  Warning: could not read page {page_url}: {error}",
                file=sys.stderr,
            )
            continue

        add_matches(
            matches,
            course_id=course_id,
            source_type="page",
            source_name=page.get("title", page_url),
            source_id=str(page.get("page_id", page_url)),
            content=page.get("body"),
        )

    # Assignments
    assignments = client.get_all(
        f"/api/v1/courses/{course_id}/assignments",
        params={"per_page": 100},
    )

    print(f"  Assignments: {len(assignments)}")

    for assignment in assignments:
        add_matches(
            matches,
            course_id=course_id,
            source_type="assignment",
            source_name=assignment.get("name", "Untitled assignment"),
            source_id=str(assignment.get("id", "")),
            content=assignment.get("description"),
        )

    # Discussions and announcements
    discussions = client.get_all(
        f"/api/v1/courses/{course_id}/discussion_topics",
        params={"per_page": 100},
    )

    print(f"  Discussions/announcements: {len(discussions)}")

    for discussion in discussions:
        source_type = (
            "announcement"
            if discussion.get("is_announcement")
            else "discussion"
        )

        add_matches(
            matches,
            course_id=course_id,
            source_type=source_type,
            source_name=discussion.get("title", "Untitled discussion"),
            source_id=str(discussion.get("id", "")),
            content=discussion.get("message"),
        )

    return sorted(
        matches,
        key=lambda item: (
            item.source_type,
            item.source_name.lower(),
            item.url,
        ),
    )


def validate_configuration() -> None:
    missing = []

    if not CANVAS_BASE_URL:
        missing.append("CANVAS_BASE_URL")

    if not CANVAS_TOKEN:
        missing.append("CANVAS_TOKEN")

    if not COURSE_ID:
        missing.append("CANVAS_COURSE_ID")

    if missing:
        print(
            "Missing environment variables: " + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            "\nExample PowerShell setup:\n"
            '$env:CANVAS_BASE_URL="https://school.instructure.com"\n'
            '$env:CANVAS_TOKEN="your-token"\n'
            '$env:CANVAS_COURSE_ID="12345"\n'
            "python extract_canvas_sheets.py",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> None:
    validate_configuration()
    client = CanvasClient(CANVAS_BASE_URL, CANVAS_TOKEN)

    try:
        matches = scan_course(client, COURSE_ID)
    except requests.HTTPError as error:
        response = error.response

        print(f"\nCanvas API error: {error}", file=sys.stderr)

        if response is not None:
            print(response.text[:1_000], file=sys.stderr)

        raise SystemExit(1)

    print()

    if not matches:
        print("No direct Google Sheets links found.")
        print(
            "The sheet may be inside an external-tool/LTI launch, "
            "which can hide the original Google URL from the API."
        )
        return

    print(f"Found {len(matches)} Google Sheets link(s):\n")

    for index, match in enumerate(matches, start=1):
        print(f"{index}. {match.source_type}: {match.source_name}")
        print(f"   Source ID: {match.source_id}")
        print(f"   Spreadsheet ID: {match.spreadsheet_id or 'published/unknown'}")
        print(f"   URL: {match.url}")
        print()


if __name__ == "__main__":
    main()