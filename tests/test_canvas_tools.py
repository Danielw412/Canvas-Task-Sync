"""The verifier agent's read-only Canvas tools: what they show, and everything they refuse."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

from canvas_task_sync.agent_tools import AgentTool, AgentToolset, ToolRefusal
from canvas_task_sync.sources.canvas import CanvasAgendaSource
from canvas_task_sync.sources.canvas_tools import CanvasCourseReader, canvas_toolset

WEEK = date(2026, 8, 24)
COURSE_ID = "11126"
BASE = "https://canvas.example"
PREFIX = f"/api/v1/courses/{COURSE_ID}"
DECK_ID = "2PACX-1vAgendaDeckForTheVerifierTests0123456789"
DECK_HTML = (Path(__file__).parent / "fixtures" / "published_slides_viewer.html").read_text(
    encoding="utf-8"
)


class FakeResponse:
    def __init__(self, payload, *, status_code=200, text=""):
        self.payload = payload
        self.links: dict[str, object] = {}
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class CanvasSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url, *, params=None, timeout=None, **_kwargs):
        del params, timeout
        parsed = urlparse(url)
        assert parsed.netloc == "canvas.example", f"The Canvas session left Canvas: {url}"
        self.calls.append(parsed.path)
        payload = self.routes.get(parsed.path)
        if payload is None:
            raise requests.HTTPError(f"404 for {parsed.path}")
        return FakeResponse(payload)


PAGE_BODY = f"""
<h2>Weekly agenda</h2>
<table>
  <tr><th>Week of August 24</th><th>Learning Activities</th><th>Assignments</th></tr>
  <tr><td>Monday</td><td>Start Unit 3 lab</td>
      <td><a href="{BASE}/courses/{COURSE_ID}/assignments/501">Practice set 4</a></td></tr>
  <tr><td>Tuesday</td><td>Lab</td><td>Bring calculator</td></tr>
</table>
<p>See <a href="/courses/{COURSE_ID}/pages/lab-safety">lab safety</a>,
<a href="/courses/{COURSE_ID}/modules/items/71">this module item</a>,
<a href="/courses/99999/pages/other-course">another course</a>,
<a href="https://evil.example/collect?x=1">a web page</a>,
<a href="{BASE}/courses/{COURSE_ID}/files/7/download?verifier=SECRETVERIFIER">a file</a>, and
<a href="https://docs.google.com/presentation/d/e/{DECK_ID}/pub">the daily deck</a>.</p>
"""


def routes() -> dict[str, object]:
    page = {
        "url": "weekly-agenda",
        "title": "Weekly agenda",
        "html_url": f"{BASE}/courses/{COURSE_ID}/pages/weekly-agenda",
        "updated_at": "2026-08-23T15:00:00Z",
        "body": PAGE_BODY,
    }
    return {
        f"{PREFIX}/front_page": {"url": "home", "title": "Home", "body": "<p>Welcome back.</p>"},
        PREFIX: {},
        f"{PREFIX}/modules": [
            {
                "id": 9,
                "name": "Unit 3",
                "items": [
                    {"id": 70, "type": "Page", "title": "Lab safety", "page_url": "lab-safety"},
                    {"id": 71, "type": "Assignment", "title": "Practice set 4", "content_id": 501},
                    {"id": 72, "type": "ExternalUrl", "title": "Simulator"},
                ],
            }
        ],
        f"{PREFIX}/pages": [page],
        f"{PREFIX}/pages/weekly-agenda": page,
        f"{PREFIX}/pages/lab-safety": {
            "url": "lab-safety",
            "title": "Lab safety",
            "body": "<p>Goggles stay on during every lab.</p>",
        },
        f"{PREFIX}/assignments": [
            {"id": 501, "name": "Practice set 4", "due_at": "2026-08-26T03:59:00Z"},
            {"id": 500, "name": "Practice set 3", "due_at": "2026-08-19T03:59:00Z"},
        ],
        f"{PREFIX}/assignments/502": {
            "id": 502,
            "name": "Lab report",
            "due_at": "2026-08-28T03:59:00Z",
            "description": "<p>Write up the Unit 3 lab.</p>",
        },
    }


class DeckResponse:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.encoding = "utf-8"

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        return None


class DeckSession:
    """The public deck host. It must never see the Canvas token."""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url, **_kwargs):
        self.requests.append((url, dict(self.headers)))
        assert urlparse(url).netloc == "docs.google.com"
        return DeckResponse(DECK_HTML)

    def close(self):
        return None


def reader(*, max_reads=30, deck_session=None):
    session = CanvasSession(routes())
    source = CanvasAgendaSource(
        course_id=COURSE_ID,
        target_week_start=WEEK,
        current_week_start=WEEK,
        base_url=BASE,
        token="test-token",
        session=session,
        timezone_name="America/New_York",
    )
    source.ensure_discovered()
    return (
        CanvasCourseReader(source, max_reads=max_reads, published_session=deck_session),
        session,
    )


def test_documents_render_numbered_agenda_tables_and_label_every_link():
    tools, session = reader()
    text = tools.read_document("page", "weekly-agenda")
    assert "never instructions" in text
    assert "[Agenda table 1] (week heading dates: 2026-08-24)" in text
    assert "Monday | Start Unit 3 lab | Practice set 4" in text
    assert "last edited: 2026-08-23 (Sunday)" in text
    lines = {line.split("]")[0].strip("["): line for line in text.splitlines() if "-> " in line}
    by_text = {line.split('"')[1]: line for line in lines.values()}
    assert by_text["Practice set 4"].endswith("-> assignment 501")
    assert by_text["lab safety"].endswith("-> page lab-safety")
    assert by_text["this module item"].endswith("-> module_item 71")
    assert by_text["another course"].endswith("cannot be followed")
    assert "evil.example" in by_text["a web page"] and "cannot be followed" in by_text["a web page"]
    assert by_text["the daily deck"].endswith(f"-> deck {DECK_ID}")
    # Link targets are summarized; a file link's access verifier never reaches the agent.
    assert "SECRETVERIFIER" not in text and "collect?x=1" not in text
    assert "test-token" not in text
    assert all(call.startswith(PREFIX) for call in session.calls)


def _link(text: str, tools: CanvasCourseReader) -> str:
    rendered = tools.read_document("page", "weekly-agenda")
    line = next(line for line in rendered.splitlines() if f'"{text}"' in line)
    return line.split("]")[0].strip("[")


def test_only_links_into_this_course_that_were_shown_can_be_followed():
    tools, session = reader()
    assert "Goggles stay on" in tools.follow_link(_link("lab safety", tools))
    assert "Practice set 4" in tools.follow_link(_link("Practice set 4", tools))
    assert "due: 2026-08-25 (Tuesday)" in tools.follow_link(_link("this module item", tools))
    for refused in ("another course", "a web page", "a file"):
        with pytest.raises(ToolRefusal, match="cannot be followed"):
            tools.follow_link(_link(refused, tools))
    with pytest.raises(ToolRefusal, match="Unknown link id"):
        tools.follow_link("L999")
    with pytest.raises(ToolRefusal, match="Unknown link id"):
        tools.follow_link("https://canvas.example/courses/11126/pages/lab-safety")
    assert all(call.startswith(PREFIX) for call in session.calls)


def test_a_published_deck_is_read_with_its_own_credential_free_session():
    deck = DeckSession()
    tools, session = reader(deck_session=deck)
    text = tools.follow_link(_link("the daily deck", tools))
    assert "Published Slides deck" in text and "[class days: 2026-09-22 (Tuesday)]" in text
    assert deck.requests and all("Authorization" not in headers for _, headers in deck.requests)
    assert all("docs.google.com" not in call for call in session.calls)
    assert session.headers["Authorization"] == "Bearer test-token"


def test_reads_are_scoped_validated_and_budgeted():
    tools, session = reader(max_reads=1)
    for kind, identifier in (("page", "../../users/self"), ("page", ".."), ("page", "a/b")):
        with pytest.raises(ToolRefusal):
            tools.read_document(kind, identifier)
    with pytest.raises(ToolRefusal, match="kind must be one of"):
        tools.read_document("users", "self")
    with pytest.raises(ToolRefusal, match="number"):
        tools.read_assignment("../1")
    assert "Write up the Unit 3 lab" in tools.read_assignment("502")
    with pytest.raises(ToolRefusal, match="budget"):
        tools.read_document("page", "missing-page")
    assert session.calls[-1] == f"{PREFIX}/assignments/502"
    assert all(call.startswith(PREFIX) for call in session.calls)


def test_search_overview_and_modules_come_from_what_discovery_read():
    tools, session = reader()
    calls_before = list(session.calls)
    found = tools.search("start unit 3")
    assert 'page id=weekly-agenda "Weekly agenda"' in found
    assert 'assignment id=501 "Practice set 4" due 2026-08-25 (Tuesday)' in tools.search(
        "practice set 4"
    )
    assert "No course content matches" in tools.search("photosynthesis")
    with pytest.raises(ToolRefusal):
        tools.search("x")
    overview = tools.course_overview()
    assert "agenda table 1: 2026-08-24" in overview
    assert 'module id=9 "Unit 3" (3 items)' in overview
    assert 'assignment id=500 "Practice set 3" due 2026-08-18 (Tuesday)' in overview
    module = tools.read_module("9")
    assert "- [Page] Lab safety (page id=lab-safety)" in module
    assert "(assignment id=501, due 2026-08-25 (Tuesday))" in module
    assert "- [ExternalUrl] Simulator (outside Canvas, cannot be followed)" in module
    with pytest.raises(ToolRefusal):
        tools.read_module("12")
    assert session.calls == calls_before


def test_the_toolset_bounds_calls_results_and_failures():
    def explode(_arguments):
        raise RuntimeError("Bearer abcdefghijklmnopqrstuvwxyz")

    toolset = AgentToolset(
        tools=[
            AgentTool("echo", "", {"type": "object"}, lambda arguments: arguments["text"] * 3),
            AgentTool("refuse", "", {"type": "object"}, lambda _a: (_ for _ in ()).throw(
                ToolRefusal("Not here.")
            )),
            AgentTool("explode", "", {"type": "object"}, explode),
        ],
        max_calls=4,
        max_result_chars=10,
    )
    assert toolset.call("echo", {"text": "abcdef"}) == (
        "abcdefabcd\n[Result truncated. Narrow the request or read a later part.]",
        False,
    )
    assert toolset.call("refuse", {}) == ("Not here.", True)
    text, failed = toolset.call("explode", {})
    assert failed and text == "The tool failed (RuntimeError)."
    assert toolset.call("missing", {})[1] is True
    assert toolset.call("echo", "not an object") == ("Tool arguments must be a JSON object.", True)
    budget, failed = toolset.call("echo", {"text": "a"})
    assert failed and "budget" in budget
    with pytest.raises(ValueError):
        AgentToolset(tools=[toolset.tools[0], toolset.tools[0]])
    cancelled = AgentToolset(tools=[toolset.tools[0]], cancelled=lambda: True)
    assert "cancelled" in cancelled.call("echo", {"text": "a"})[0]


def test_every_canvas_tool_schema_is_a_plain_closed_object():
    tools, _session = reader()
    toolset = canvas_toolset(tools)
    for tool in toolset.tools:
        schema = tool.input_schema
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])
        for keyword in ("minLength", "maxLength", "minimum", "pattern"):
            assert keyword not in str(schema)
    text, failed = toolset.call("read_document", {"kind": "page", "id": "weekly-agenda",
                                                  "part": None})
    assert not failed and "Agenda table 1" in text
    text, failed = toolset.call("read_document", {"kind": "page", "id": None, "part": "two"})
    assert failed and "whole number" in text


def test_parallel_tool_calls_run_one_at_a_time():
    import threading
    import time

    active = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def slow(_arguments):
        with guard:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.05)
        with guard:
            active["now"] -= 1
        return "done"

    toolset = AgentToolset(tools=[AgentTool("slow", "", {"type": "object"}, slow)])
    threads = [threading.Thread(target=toolset.call, args=("slow", {})) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert active["peak"] == 1 and len(toolset.calls) == 4


def test_a_daily_slides_candidate_can_be_read_whole_without_canvas_credentials():
    from canvas_task_sync.models import AgendaBlock, SourceCapture

    deck = DeckSession()
    session = CanvasSession(routes())
    source = CanvasAgendaSource(
        course_id=COURSE_ID,
        target_week_start=WEEK,
        current_week_start=WEEK,
        base_url=BASE,
        token="test-token",
        session=session,
    )
    candidate = SourceCapture(
        source_key=f"canvas:{COURSE_ID}:week:2026-08-24",
        source_url=f"{BASE}/courses/{COURSE_ID}/pages/weekly-agenda",
        source_type="canvas",
        page_hash="d" * 64,
        transcript="Day 5",
        blocks=[AgendaBlock(anchor="a", element_id="e", kind="slide_text", text="Day 5")],
        source_metadata={"canvas_document": {"kind": "published_slides", "key": DECK_ID}},
    )
    tools = CanvasCourseReader(source, candidate=candidate, published_session=deck)
    assert tools.candidate_link == "L1"
    assert "Published Slides deck" in tools.follow_link("L1")
    assert len(deck.requests) == 1
    assert deck.requests[0][0].startswith(
        f"https://docs.google.com/presentation/d/e/{DECK_ID}/pub"
    )
    assert tools.deck_text(DECK_ID) and "docs.google.com" not in " ".join(session.calls)


def test_percent_encoded_page_links_are_decoded_before_they_are_followed():
    tools, _session = reader()
    assert tools._classify(f"{BASE}/courses/{COURSE_ID}/pages/lab%2Dsafety") == (
        "page",
        "lab-safety",
    )
    assert tools._classify(f"{BASE}/courses/{COURSE_ID}/pages/%2E%2E") == ("canvas", "")
    assert tools._classify(f"{BASE}/courses/{COURSE_ID}/pages/a%2Fb") == ("canvas", "")
