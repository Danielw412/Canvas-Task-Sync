from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pytest
from pydantic import ValidationError

from canvas_task_sync.configuration import (
    BrowserSourceSettings,
    CourseSettings,
    GoogleSlidesSourceSettings,
)
from canvas_task_sync.gemini import build_prompt
from canvas_task_sync.models import (
    ActionKind,
    AgendaBlock,
    BlockRole,
    Confidence,
    DayCount,
    DueRelation,
    ExtractedTask,
    SourceCapture,
    TaskClassification,
    TaskType,
)
from canvas_task_sync.scheduling import build_draft_tasks, row_date_ranges
from canvas_task_sync.sources.canvas import CanvasAgendaNotFound, CanvasAgendaSource
from canvas_task_sync.sources.published_slides import (
    PublishedSlidesError,
    fetch_published_deck,
    parse_heading_dates,
    parse_viewer_html,
    published_deck_id,
    slide_heading,
    viewer_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "published_slides_viewer.html"
DECK_ID = "2PACX-FIXTUREDECK00000000000000"
EMBED_URL = f"https://docs.google.com/presentation/d/e/{DECK_ID}/pubembed?start=false&amp;loop=false"
PREFIX = "/api/v1/courses/12477"
REFERENCE = date(2026, 9, 21)


def viewer_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class FakeCanvasResponse:
    def __init__(self, payload):
        self.payload = payload
        self.links: dict = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeCanvasSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url, *, params=None, timeout=None):
        del params, timeout
        parsed = urlparse(url)
        assert parsed.hostname == "canvas.example", f"Canvas session left Canvas: {url}"
        self.calls.append(parsed.path)
        payload = self.routes.get(parsed.path)
        if payload is None:
            raise AssertionError(f"Unexpected Canvas request: {parsed.path}")
        return FakeCanvasResponse(payload)


class FakeDeckResponse:
    def __init__(self, body: str, status_code: int = 200, headers: dict | None = None):
        self.body = body.encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


class FakeDeckSession:
    def __init__(self, body: str | None = None, **response_options):
        self.body = viewer_html() if body is None else body
        self.response_options = response_options
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeDeckResponse(self.body, **self.response_options)

    def close(self):
        return None


def canvas_routes(front_body: str = "<p>Welcome to AP Psychology.</p>") -> dict:
    return {
        f"{PREFIX}/front_page": {
            "url": "home",
            "title": "00. Agenda. Week 4",
            "html_url": "https://canvas.example/courses/12477/pages/home",
            "body": front_body,
        },
        PREFIX: {},
        f"{PREFIX}/modules": [
            {
                "id": 1,
                "name": "Agendas",
                "items": [
                    {
                        "id": 5,
                        "type": "Page",
                        "title": "00. Agendas. 2026 [AP Psychology]",
                        "page_url": "00-agendas-2026",
                    }
                ],
            },
            {
                "id": 2,
                "name": "UNIT 3: Research",
                "items": [
                    {
                        "id": 930780,
                        "type": "Assignment",
                        "content_id": 184575,
                        "title": "03. Correlation Activity",
                    },
                    {
                        "id": 930198,
                        "type": "Assignment",
                        "content_id": 184374,
                        "title": "03. Loftus Reading Questions [Turn-in]",
                    },
                    {
                        "id": 859455,
                        "type": "Assignment",
                        "content_id": 170633,
                        "title": "02. Memory",
                    },
                    {"id": 859444, "type": "Page", "title": "02. H.M."},
                ],
            },
        ],
        f"{PREFIX}/pages": [],
        f"{PREFIX}/assignments": [
            {"id": 184575, "name": "03. Correlation Activity", "due_at": "2026-09-25T18:15:00Z"},
            {
                "id": 184374,
                "name": "03. Loftus Reading Questions [Turn-in]",
                "due_at": "2026-09-21T18:15:00Z",
            },
            # A test's Canvas due date is often a make-up window; the slide's day still rules.
            {"id": 170633, "name": "02. Memory", "due_at": "2026-09-18T18:15:00Z"},
        ],
        f"{PREFIX}/pages/00-agendas-2026": {
            "url": "00-agendas-2026",
            "title": "00. Agendas. 2026 [AP Psychology]",
            "html_url": "https://canvas.example/courses/12477/pages/00-agendas-2026",
            "body": f'<p><iframe title="embedded content" src="{EMBED_URL}"></iframe></p>',
        },
    }


def deck_source(
    week: date,
    *,
    routes: dict | None = None,
    deck_session: FakeDeckSession | None = None,
) -> tuple[CanvasAgendaSource, FakeCanvasSession, FakeDeckSession]:
    canvas_session = FakeCanvasSession(routes or canvas_routes())
    published = deck_session or FakeDeckSession()
    source = CanvasAgendaSource(
        course_id="12477",
        target_week_start=week,
        base_url="https://canvas.example",
        token="canvas-secret",
        session=canvas_session,
        timezone_name="America/New_York",
        published_session=published,
    )
    return source, canvas_session, published


def psych_course() -> CourseSettings:
    return CourseSettings.model_validate(
        {
            "name": "AP Psychology",
            "prefix": "PSYCH",
            "task_list": "School",
            "canvas_course_id": "12477",
            "timezone": "America/New_York",
            "source": {
                "type": "none",
                "extraction": {
                    "mode": "text",
                    "same_day_action_kinds": ["submit", "present", "bring"],
                },
            },
        }
    )


def block_containing(capture: SourceCapture, text: str) -> AgendaBlock:
    return next(block for block in capture.blocks if text in block.text)


def extracted(block: AgendaBlock, source_text: str, **overrides) -> ExtractedTask:
    fields = {
        "source_anchor": block.anchor,
        "source_text": source_text,
        "row_label": block.row_label,
        "classification": TaskClassification.HOMEWORK,
        "action_kind": ActionKind.COMPLETE,
        "title_stem": source_text,
        "due_relation": DueRelation.NEXT_CLASS,
        "confidence": Confidence.HIGH,
    }
    return ExtractedTask(**{**fields, **overrides})


def test_viewer_page_yields_slides_text_boxes_and_clean_links():
    deck = parse_viewer_html(viewer_html(), DECK_ID)

    assert deck.title == "00. APP Agendas. 2026"
    assert deck.revision == "967.0"
    # The fixture lists 21 slides but inlines only 20, like a viewer that omits a slide.
    assert len(deck.slides) == 20
    assert deck.missing_slide_ids == ("g3f7911e2679_0_0",)

    newest = deck.slides[0]
    assert newest.texts[0].text.splitlines()[:2] == [
        "Day 25: September 22",
        "Research: Correlation Studies",
    ]
    # Google's /url?q= redirect (with its rotating ust/usg parameters) is unwrapped.
    assert [link.url for link in newest.texts[0].links] == [
        "https://docs.google.com/spreadsheets/d/FAKE015c761059cdd40e0de34077/copy",
        "https://docs.google.com/document/d/FAKEf3b77ca882a9b6854be208c1/copy",
        "https://canvas.example/courses/12477/modules/items/930780",
    ]
    # Navigation images (Course Info.png, Help.png, ...) are not text and carry no links.
    assert all(".png" not in text.text for slide in deck.slides for text in slide.texts)

    friday = next(slide for slide in deck.slides if "September 11" in slide.texts[0].text)
    side_note = friday.texts[1]
    assert side_note.text == "For Friday: H.M. form: Refer to your notes from p. 26"
    assert [link.text for link in side_note.links] == ["H.M. form"]


def test_every_listed_slide_is_dated_by_its_heading():
    deck = parse_viewer_html(viewer_html(), DECK_ID)
    headings = [slide_heading(slide, REFERENCE) for slide in deck.slides]

    assert all(headings)
    assert headings[0].dates == (date(2026, 9, 22),)
    assert headings[1].text == "Days 23 and 24: September 18 and 21"
    assert headings[1].dates == (date(2026, 9, 18), date(2026, 9, 21))
    assert headings[-1].dates == (date(2026, 8, 24),)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Day 25: September 22", (date(2026, 9, 22),)),
        ("Days 23 and 24: September 18 and 21", (date(2026, 9, 18), date(2026, 9, 21))),
        ("Tuesday, Sept. 22", (date(2026, 9, 22),)),
        ("Day 25 - 9/22", (date(2026, 9, 22),)),
        ("Day 5: August 24th", (date(2026, 8, 24),)),
        ("September 8-11", (date(2026, 9, 8), date(2026, 9, 11))),
        ("Aug 31 - Sept 1", (date(2026, 8, 31), date(2026, 9, 1))),
        (
            "Friday, September 18 and Monday, September 21",
            (date(2026, 9, 18), date(2026, 9, 21)),
        ),
        ("January 4", (date(2027, 1, 4),)),
        # A date inside a sentence is not a heading, and neither is a bare day counter.
        ("Test on September 16", None),
        ("We will test on WEDNESDAY.", None),
        ("Day 30", None),
        ("Unit 3: 2/4 Research Methods", None),
        # A heading spanning weeks of the calendar is a misread, not a class day.
        ("September 1 - October 30", None),
    ],
)
def test_slide_headings_name_class_days(line, expected):
    assert parse_heading_dates(line, REFERENCE) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (EMBED_URL.replace("&amp;", "&"), DECK_ID),
        (f"https://docs.google.com/presentation/d/e/{DECK_ID}/pub?start=false", DECK_ID),
        (f"https://docs.google.com/presentation/d/e/{DECK_ID}/embed", DECK_ID),
        ("https://docs.google.com/presentation/d/1AbCdEfGhIjKlMnOpQrStUv/edit", None),
        (f"https://evil.example/presentation/d/e/{DECK_ID}/pub", None),
        (f"http://docs.google.com/presentation/d/e/{DECK_ID}/pub", None),
        (f"https://docs.google.com/document/d/e/{DECK_ID}/pub", None),
    ],
)
def test_only_published_slides_viewer_urls_are_recognized(url, expected):
    assert published_deck_id(url) == expected


def test_fetch_builds_the_viewer_url_itself_and_refuses_redirects():
    session = FakeDeckSession()
    deck = fetch_published_deck(DECK_ID, session=session)

    assert len(deck.slides) == 20
    [(url, options)] = session.calls
    assert url == viewer_url(DECK_ID)
    assert options["allow_redirects"] is False
    assert options["stream"] is True
    assert "headers" not in options

    with pytest.raises(PublishedSlidesError, match="HTTP 302"):
        fetch_published_deck(DECK_ID, session=FakeDeckSession(status_code=302))
    with pytest.raises(PublishedSlidesError, match="malformed"):
        viewer_url("../../evil")


def test_fetch_refuses_an_oversized_viewer_page():
    with pytest.raises(PublishedSlidesError, match="size limit"):
        fetch_published_deck(DECK_ID, session=FakeDeckSession(), max_bytes=10_000)
    declared = FakeDeckSession(headers={"Content-Length": str(10**9)})
    with pytest.raises(PublishedSlidesError, match="size limit"):
        fetch_published_deck(DECK_ID, session=declared)


def test_viewer_without_slides_is_reported_as_unreadable():
    with pytest.raises(PublishedSlidesError, match="did not contain readable slides"):
        parse_viewer_html("<html><title>Sign in</title></html>", DECK_ID)


def test_canvas_capture_reads_the_embedded_deck_for_the_target_week():
    source, canvas_session, deck_session = deck_source(date(2026, 9, 14))
    capture = source.capture(include_image=False)

    assert capture.source_key == "canvas:12477:week:2026-09-14"
    assert capture.source_url == "https://canvas.example/courses/12477/pages/00-agendas-2026"
    assert capture.source_metadata["canvas_kind"] == "published_slides"
    assert capture.source_metadata["agenda_format"] == "daily_slides"
    assert capture.source_metadata["embedded_in"] == "00. Agendas. 2026 [AP Psychology]"
    assert capture.selection["matched_text"] == "Day 19: September 14"
    assert len(capture.selection["slide_ids"]) == 5

    headings = [block for block in capture.blocks if block.role == BlockRole.DAY]
    assert [(block.text, block.row_label) for block in headings] == [
        ("Day 19: September 14", "Monday"),
        ("Day 20: September 15", "Tuesday"),
        ("Day 21: September 16", "Wednesday"),
        ("Day 22: September 17", "Thursday"),
        ("Days 23 and 24: September 18 and 21", "Friday/Monday"),
    ]
    two_day = headings[-1]
    assert row_date_ranges(capture)[(two_day.element_id, two_day.row_index)] == (
        date(2026, 9, 18),
        date(2026, 9, 21),
    )

    test_day = block_containing(capture, "Here is the test")
    assert test_day.anchor == "canvas:slides:g3fb72421960_0_3"
    assert headings[2].anchor == "canvas:slides:g3fb72421960_0_0"
    assert test_day.metadata["assignment_links"] == [
        {
            "url": "https://canvas.example/courses/12477/assignments/170633",
            "text": "Here is the test",
            "title": "02. Memory",
            "due_at": "2026-09-18T18:15:00Z",
        }
    ]
    assert '- "Here is the test" -> Canvas assignment "02. Memory"' in capture.transcript
    notes = block_containing(capture, "Independent note-taking")
    assert notes.metadata["assignment_links"] == []
    assert "Google Docs document" in capture.transcript

    # The Canvas token never leaves Canvas, and the deck is read without any credentials.
    assert all(call.startswith(PREFIX) for call in canvas_session.calls)
    assert deck_session.headers == {}
    assert len(deck_session.calls) == 1


def test_agenda_deck_is_read_before_lecture_decks_and_hosted_by_its_own_page():
    notes_deck = "2PACX-NOTESDECK000000000000000"
    routes = canvas_routes(
        # The front page repeats the agenda deck below an old weekly table.
        "<table>"
        + "<tr><td>MONDAY</td><td>Memory, continued</td><td></td></tr>" * 5
        + f'</table><iframe src="{EMBED_URL}"></iframe>'
    )
    # Lecture notes pages, each embedding its own deck, are discovered before the agenda page.
    routes[f"{PREFIX}/modules"].insert(
        0,
        {
            "id": 3,
            "name": "UNIT 3: Research",
            "items": [
                {
                    "id": 929359,
                    "type": "Page",
                    "title": "03. Student Notes: Descriptive Studies, 1",
                    "page_url": "03-student-notes",
                }
            ],
        },
    )
    routes[f"{PREFIX}/pages/03-student-notes"] = {
        "url": "03-student-notes",
        "title": "03. Student Notes: Descriptive Studies, 1",
        "html_url": "https://canvas.example/courses/12477/pages/03-student-notes",
        "body": (
            '<iframe src="https://docs.google.com/presentation/d/e/'
            f'{notes_deck}/pubembed?start=false"></iframe>'
        ),
    }
    source, _, deck_session = deck_source(date(2026, 9, 21), routes=routes)

    capture = source.capture(include_image=False)

    assert [url for url, _ in deck_session.calls] == [viewer_url(DECK_ID)]
    assert capture.source_url == "https://canvas.example/courses/12477/pages/00-agendas-2026"
    assert capture.source_metadata["embedded_in"] == "00. Agendas. 2026 [AP Psychology]"


def test_deck_capture_hash_ignores_googles_rotating_redirect_parameters():
    first = deck_source(date(2026, 9, 21))[0].capture(include_image=False)
    rotated = FakeDeckSession(viewer_html().replace("ust=1", "ust=987654321"))
    second = deck_source(date(2026, 9, 21), deck_session=rotated)[0].capture(include_image=False)

    assert first.page_hash == second.page_hash
    headings = [block.text for block in first.blocks if block.role == BlockRole.DAY]
    assert headings == ["Days 23 and 24: September 18 and 21", "Day 25: September 22"]


def test_edits_to_other_weeks_slides_do_not_change_the_week_capture():
    first = deck_source(date(2026, 9, 21))[0].capture(include_image=False)
    edited = FakeDeckSession(viewer_html().replace("Why we remember", "Why do we remember"))
    second = deck_source(date(2026, 9, 21), deck_session=edited)[0].capture(include_image=False)

    assert first.page_hash == second.page_hash


def test_week_without_dated_slides_reports_the_deck_state():
    source, _, _ = deck_source(date(2026, 9, 28))

    with pytest.raises(CanvasAgendaNotFound, match="has no slide dated in that week yet"):
        source.capture(include_image=False)


def test_unreadable_deck_is_reported_in_the_not_found_error():
    source, _, _ = deck_source(
        date(2026, 9, 21), deck_session=FakeDeckSession(status_code=404)
    )

    with pytest.raises(CanvasAgendaNotFound, match="could not be read: .*HTTP 404"):
        source.capture(include_image=False)


def test_labeled_weekly_canvas_agenda_outranks_the_deck():
    front = """
    <h2>Week of September 14</h2>
    <p>Learning Targets for the Week: memory.</p>
    <table>
      <tr><th>Day</th><th>Learning Activities</th><th>Assignments</th></tr>
      <tr><td>Monday</td><td>Memory review</td><td>Complete review worksheet</td></tr>
      <tr><td>Tuesday</td><td>Jeopardy review</td><td>Study for the test</td></tr>
    </table>
    """
    capture = deck_source(date(2026, 9, 14), routes=canvas_routes(front))[0].capture(
        include_image=False
    )

    assert capture.source_metadata["canvas_kind"] == "front_page"


def test_deck_outranks_a_stale_weekly_page_that_only_mentions_the_week():
    stale = """
    <table><tr><td>AP PSYCHOLOGY September 8-11</td></tr>
      <tr><td>MONDAY</td><td>LABOR DAY</td><td></td></tr>
      <tr><td>TUESDAY</td><td>Memory, continued</td><td>Tasks</td></tr>
    </table>
    """
    capture = deck_source(date(2026, 9, 7), routes=canvas_routes(stale))[0].capture(
        include_image=False
    )

    assert capture.source_metadata["canvas_kind"] == "published_slides"
    assert capture.selection["matched_text"] == "Day 15: September 8"


def test_slide_deadlines_use_slide_dates_stated_weekdays_and_linked_canvas_due_dates():
    course = psych_course()
    week_21 = deck_source(date(2026, 9, 21))[0].capture(include_image=False)
    correlation = block_containing(week_21, "A correlation worksheet")
    two_day = block_containing(week_21, "two-day plan")
    week_14 = deck_source(date(2026, 9, 14))[0].capture(include_image=False)
    test_day = block_containing(week_14, "Here is the test")
    week_07 = deck_source(date(2026, 9, 7))[0].capture(include_image=False)
    hm_form = block_containing(week_07, "For Friday: H.M. form")

    drafts_21, uncertain, _ = build_draft_tasks(
        course_id="psych",
        course=course,
        capture=week_21,
        tasks=[
            extracted(
                correlation,
                "A correlation worksheet; Submit here.",
                action_kind=ActionKind.SUBMIT,
                due_relation=DueRelation.SAME_DAY,
                title_stem="Correlation activity",
            ),
            extracted(
                two_day,
                "If you are submitting the questions electronically, follow the "
                "instructions at this link.",
                action_kind=ActionKind.SUBMIT,
                title_stem="Loftus reading questions",
            ),
            extracted(two_day, "The two-day plan is here.", title_stem="Two-day plan"),
        ],
        today=date(2026, 9, 22),
    )
    assert not uncertain
    by_title = {draft.title: draft for draft in drafts_21}
    activity = by_title["[PSYCH] Correlation activity"]
    # "Submit here" is a same-day submit on its own, but Canvas says the activity is due Friday.
    assert (activity.due_date, activity.assignment_url) == (
        date(2026, 9, 25),
        "https://canvas.example/courses/12477/assignments/184575",
    )
    assert activity.due_basis == "Due date of the Canvas assignment linked in source evidence"
    loftus = by_title["[PSYCH] Loftus reading questions"]
    # Canvas' 18:15 UTC deadline is Monday afternoon in New York, not next class Tuesday.
    assert (loftus.due_date, loftus.historical) == (date(2026, 9, 21), True)
    plan = by_title["[PSYCH] Two-day plan"]
    # The slide covers Friday and Monday, so next-class work is due after Monday.
    assert (plan.due_date, plan.source_date, plan.assignment_url) == (
        date(2026, 9, 22),
        date(2026, 9, 21),
        None,
    )

    drafts_14, _, _ = build_draft_tasks(
        course_id="psych",
        course=course,
        capture=week_14,
        tasks=[
            extracted(
                test_day,
                "Here is the test: 46 multiple-choice questions.",
                task_type=TaskType.TEST,
                action_kind=ActionKind.OTHER,
                due_relation=DueRelation.SAME_DAY,
                title_stem="Memory Test",
            )
        ],
        today=date(2026, 9, 14),
    )
    # An assessment stays on the day it is given, whatever its Canvas due date says.
    assert [(draft.due_date, draft.assignment_url) for draft in drafts_14] == [
        (date(2026, 9, 16), "https://canvas.example/courses/12477/assignments/170633")
    ]

    drafts_07, _, _ = build_draft_tasks(
        course_id="psych",
        course=course,
        capture=week_07,
        tasks=[
            extracted(
                hm_form,
                "For Friday: H.M. form: Refer to your notes from p. 26",
                title_stem="H.M. form",
            )
        ],
        today=date(2026, 9, 7),
    )
    assert [(draft.due_date, draft.due_basis) for draft in drafts_07] == [
        (date(2026, 9, 11), "Weekday explicitly stated in source evidence")
    ]


def test_link_on_the_evidence_line_ties_a_partial_phrase_to_its_canvas_assignment():
    capture = deck_source(date(2026, 9, 21))[0].capture(include_image=False)
    two_day = block_containing(capture, "two-day plan")

    drafts, _, _ = build_draft_tasks(
        course_id="psych",
        course=psych_course(),
        capture=capture,
        tasks=[
            extracted(
                two_day,
                "submitting the questions electronically",
                action_kind=ActionKind.SUBMIT,
                title_stem="Loftus reading questions",
            ),
            # The plan's own line links only a Google Doc, so it keeps next-class timing.
            extracted(two_day, "Do good work.", title_stem="Two-day plan"),
        ],
        today=date(2026, 9, 21),
    )

    by_title = {draft.title: draft for draft in drafts}
    loftus = by_title["[PSYCH] Loftus reading questions"]
    assert (loftus.due_date, loftus.assignment_url) == (
        date(2026, 9, 21),
        "https://canvas.example/courses/12477/assignments/184374",
    )
    plan = by_title["[PSYCH] Two-day plan"]
    assert (plan.due_date, plan.assignment_url) == (date(2026, 9, 22), None)


def test_a_day_stated_in_the_evidence_or_its_line_keeps_the_canvas_due_date_out():
    block = AgendaBlock(
        anchor="canvas:slides:s1:t1",
        element_id="published_slides:deck",
        kind="slide_text",
        role=BlockRole.UNKNOWN,
        row_index=0,
        row_label="Monday",
        text="Lab report; submit it here by Wednesday.\nReading notes; turn in here.",
        metadata={
            "row_dates": ["2026-09-21"],
            "assignment_links": [
                {
                    "url": "https://canvas.example/courses/1/assignments/9",
                    "text": "submit it here",
                    "title": "Lab report",
                    "due_at": "2026-09-25T18:15:00Z",
                },
                {
                    "url": "https://canvas.example/courses/1/assignments/10",
                    "text": "turn in here",
                    "title": "Reading notes",
                    "due_at": "2026-09-24T18:15:00Z",
                },
            ],
        },
    )
    capture = SourceCapture(
        source_key="canvas:1:week:2026-09-21",
        source_url="https://canvas.example/courses/1",
        source_type="canvas",
        page_hash="b" * 64,
        transcript=block.text,
        blocks=[block],
        selection={"week_start": "2026-09-21"},
    )

    drafts, _, _ = build_draft_tasks(
        course_id="1",
        course=psych_course(),
        capture=capture,
        tasks=[
            extracted(
                block,
                "submit it here by Wednesday",
                action_kind=ActionKind.SUBMIT,
                title_stem="Submit lab report",
            ),
            extracted(block, "Lab report", title_stem="Lab report"),
            extracted(block, "Reading notes", title_stem="Reading notes"),
        ],
        today=date(2026, 9, 21),
    )

    by_title = {draft.title: (draft.due_date, draft.assignment_url) for draft in drafts}
    assert by_title == {
        "[PSYCH] Submit lab report": (
            date(2026, 9, 23),
            "https://canvas.example/courses/1/assignments/9",
        ),
        # Tied to the lab report by its line, but that line states Wednesday, so Canvas' Friday
        # is not silently substituted; the phrase itself names no day, so it is next class.
        "[PSYCH] Lab report": (
            date(2026, 9, 22),
            "https://canvas.example/courses/1/assignments/9",
        ),
        "[PSYCH] Reading notes": (
            date(2026, 9, 24),
            "https://canvas.example/courses/1/assignments/10",
        ),
    }


def test_linked_canvas_due_date_outranks_ungrounded_claims_and_course_day_rules():
    block = AgendaBlock(
        anchor="canvas:slides:s1:t1",
        element_id="published_slides:deck",
        kind="slide_text",
        role=BlockRole.UNKNOWN,
        row_index=0,
        row_label="Monday",
        text="Reading notes; turn in here.",
        metadata={
            "row_dates": ["2026-09-21"],
            "assignment_links": [
                {
                    "url": "https://canvas.example/courses/1/assignments/10",
                    "text": "turn in here",
                    "title": "Reading notes",
                    "due_at": "2026-09-24T18:15:00Z",
                },
            ],
        },
    )
    capture = SourceCapture(
        source_key="canvas:1:week:2026-09-21",
        source_url="https://canvas.example/courses/1",
        source_type="canvas",
        page_hash="c" * 64,
        transcript=block.text,
        blocks=[block],
        selection={"week_start": "2026-09-21"},
    )

    for overrides in (
        # Gemini claimed an explicit date the evidence does not state.
        {"due_relation": DueRelation.EXPLICIT_DATE, "explicit_due_date": None},
        # A course rule ("due 2 days after") gives way to the deadline set in Canvas.
        {
            "due_relation": DueRelation.DAYS_AFTER,
            "due_offset_days": 2,
            "due_offset_unit": DayCount.CALENDAR_DAYS,
        },
    ):
        drafts, uncertain, _ = build_draft_tasks(
            course_id="1",
            course=psych_course(),
            capture=capture,
            tasks=[extracted(block, "Reading notes; turn in here", **overrides)],
            today=date(2026, 9, 21),
        )

        assert not uncertain
        assert [(draft.due_date, draft.due_uncertain, draft.due_basis) for draft in drafts] == [
            (
                date(2026, 9, 24),
                False,
                "Due date of the Canvas assignment linked in source evidence",
            )
        ]


def test_tomorrow_on_a_dated_slide_is_the_next_class_after_it():
    capture = deck_source(date(2026, 9, 7))[0].capture(include_image=False)
    thursday_note = block_containing(capture, "For tomorrow: H.M. form")
    assert thursday_note.row_label == "Thursday"

    drafts, uncertain, _ = build_draft_tasks(
        course_id="psych",
        course=psych_course(),
        capture=capture,
        tasks=[
            # Gemini sometimes labels a relative day as an explicit date without a date.
            extracted(
                thursday_note,
                "For tomorrow: H.M. form: Refer to your notes from p. 26",
                due_relation=DueRelation.EXPLICIT_DATE,
                title_stem="H.M. form",
            )
        ],
        today=date(2026, 9, 7),
    )

    assert not uncertain
    assert [(draft.due_date, draft.due_basis) for draft in drafts] == [
        (date(2026, 9, 11), "Next class stated in source evidence")
    ]


def test_daily_slide_guidance_is_added_only_for_slide_decks():
    course = psych_course()
    deck_capture = deck_source(date(2026, 9, 21))[0].capture(include_image=False)
    page_capture = deck_capture.model_copy(update={"source_metadata": {}})

    deck_prompt = build_prompt(deck_capture, course, include_text=True)
    page_prompt = build_prompt(page_capture, course, include_text=True)

    assert "SOURCE FORMAT: a daily slide deck." in deck_prompt
    assert "SOURCE FORMAT" not in page_prompt
    assert "COURSE-SPECIFIC INSTRUCTIONS: None.\n\nRules:" in page_prompt


@pytest.mark.parametrize("settings_type", [GoogleSlidesSourceSettings, BrowserSourceSettings])
def test_publish_to_web_links_are_rejected_for_file_id_sources(settings_type):
    url = f"https://docs.google.com/presentation/d/e/{DECK_ID}/pub?start=false"
    with pytest.raises(ValidationError, match="Publish to web"):
        settings_type.model_validate({"url": url, "page_id": "p1"})
