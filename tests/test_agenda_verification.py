"""The Claude/Codex agenda check that runs between Canvas discovery and extraction.

No test starts a real agent. A scripted backend stands in for ``run_structured`` and replies
with a verdict, which is exactly the point where a real agent's claims enter the pipeline;
everything after it (evidence checks, re-capture, status, caching, replay) is real code.
"""

from __future__ import annotations

import contextlib
from datetime import date
from urllib.parse import urlparse

import pytest
import requests

from canvas_task_sync.agenda_verification import (
    AgendaStatus,
    AgendaVerdict,
    AgendaVerification,
    AgendaVerificationError,
    AgendaVerificationUnavailable,
    AgendaVerifier,
    VerdictEvidence,
    VerdictSource,
    build_verification_prompt,
    check_evidence,
    parse_verdict,
    verdict_output_schema,
)
from canvas_task_sync.configuration import CourseSettings
from canvas_task_sync.models import AcquisitionStrategy, AgendaBlock, SourceCapture
from canvas_task_sync.sources import CourseAgendaSource
from canvas_task_sync.sources.canvas import CanvasAgendaNotFound, CanvasAgendaSource
from canvas_task_sync.sources.canvas_tools import CanvasCourseReader
from canvas_task_sync.web_models import EventLevel

WEEK = date(2026, 8, 24)
COURSE_ID = "11126"
BASE = "https://canvas.example"
PREFIX = f"/api/v1/courses/{COURSE_ID}"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.links: dict[str, object] = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    """A Canvas course. Unknown paths answer 404, as Canvas does."""

    def __init__(self, routes):
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url, *, params=None, timeout=None, **_kwargs):
        del params, timeout
        parsed = urlparse(url)
        assert parsed.netloc == "canvas.example", f"Canvas session left Canvas: {url}"
        self.calls.append(parsed.path)
        payload = self.routes.get(parsed.path)
        if payload is None:
            raise requests.HTTPError(f"404 for {parsed.path}")
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)


def agenda_table(heading: str, monday: str, monday_work: str, tuesday: str = "Lab") -> str:
    return f"""
    <table>
      <tr><th>{heading}</th><th>Learning Activities</th><th>Assignments</th></tr>
      <tr><td>{monday.split("|")[0]}</td><td>{monday.split("|")[1]}</td>
          <td>{monday_work}</td></tr>
      <tr><td>Tuesday</td><td>{tuesday}</td><td>Bring calculator</td></tr>
    </table>
    """


def link(assignment_id: int, text: str) -> str:
    return f'<a href="{BASE}/courses/{COURSE_ID}/assignments/{assignment_id}">{text}</a>'


ASSIGNMENTS = [
    {
        "id": 501,
        "name": "Practice set 4",
        "due_at": "2026-08-26T03:59:00Z",  # Tuesday Aug 25, 11:59 PM in New York.
        "html_url": f"{BASE}/courses/{COURSE_ID}/assignments/501",
    },
    {
        "id": 500,
        "name": "Practice set 3",
        "due_at": "2026-08-19T03:59:00Z",
        "html_url": f"{BASE}/courses/{COURSE_ID}/assignments/500",
    },
]


def course_routes(body: str, *, extra_pages=()) -> dict[str, object]:
    page = {
        "url": "weekly-agenda",
        "title": "Weekly agenda",
        "html_url": f"{BASE}/courses/{COURSE_ID}/pages/weekly-agenda",
        "updated_at": "2026-08-23T15:00:00Z",
        "body": body,
    }
    routes: dict[str, object] = {
        f"{PREFIX}/front_page": {"url": "home", "title": "Home", "body": "<p>Welcome back.</p>"},
        PREFIX: {},
        f"{PREFIX}/modules": [
            {
                "id": 9,
                "name": "Unit 3",
                "items": [
                    {"id": 71, "type": "Assignment", "title": "Practice set 4", "content_id": 501}
                ],
            }
        ],
        f"{PREFIX}/pages": [page, *extra_pages],
        f"{PREFIX}/pages/weekly-agenda": page,
        f"{PREFIX}/assignments": ASSIGNMENTS,
    }
    for extra in extra_pages:
        routes[f"{PREFIX}/pages/{extra['url']}"] = extra
    return routes


def canvas_source(routes, *, week=WEEK, current=WEEK) -> tuple[CanvasAgendaSource, FakeSession]:
    session = FakeSession(routes)
    return (
        CanvasAgendaSource(
            course_id=COURSE_ID,
            target_week_start=week,
            current_week_start=current,
            base_url=BASE,
            token="test-token",
            session=session,
            timezone_name="America/New_York",
        ),
        session,
    )


# Teacher copied last week's table and forgot the heading: the new table still says Aug 17.
FORGOTTEN_HEADING = agenda_table(
    "Week of August 17", "Monday|Start Unit 3 lab", link(501, "Practice set 4")
) + agenda_table("Week of August 17", "Monday|Finish Unit 2 review", link(500, "Practice set 3"))
# The right table exists but carries no date at all.
UNDATED_CURRENT = agenda_table(
    "Unit 3", "Monday|Start Unit 3 lab", link(501, "Practice set 4")
) + agenda_table("Week of August 17", "Monday|Finish Unit 2 review", link(500, "Practice set 3"))
CORRECT = agenda_table(
    "Week of August 24", "Monday|Start Unit 3 lab", link(501, "Practice set 4")
) + agenda_table("Week of August 17", "Monday|Finish Unit 2 review", link(500, "Practice set 3"))
# The heading was updated, but the dated day rows still belong to the previous week.
STALE_DAYS = agenda_table(
    "Week of August 24", "Monday 8/17|Finish Unit 2 review", link(500, "Practice set 3")
)


@pytest.fixture
def course() -> CourseSettings:
    return CourseSettings.model_validate(
        {
            "name": "AP Physics",
            "prefix": "PHYS",
            "task_list": "School",
            "canvas_course_id": COURSE_ID,
            "canvas_base_url": BASE,
            "source": {"type": "none", "extraction": {"mode": "text"}},
        }
    )


def evidence(kind, quote, *, source=("page", "weekly-agenda"), stated=None, supports="neutral"):
    return {
        "kind": kind,
        "source": {"kind": source[0], "id": source[1]},
        "quote": quote,
        "stated_date": stated,
        "supports": supports,
        "note": "",
    }


def verdict(status, agenda=None, items=(), summary="Checked.") -> dict[str, object]:
    return {
        "status": status,
        "agenda": agenda,
        "summary": summary,
        "evidence": list(items),
        "concerns": [],
    }


def named(kind="page", identifier="weekly-agenda", table=None, distinctive=None):
    return {
        "kind": kind,
        "id": identifier,
        "table_number": table,
        "distinctive_text": distinctive,
    }


class ScriptedAgent:
    """Stands in for a Claude or Codex backend's run_structured."""

    def __init__(self, *replies, tool_calls=(), during=None) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.turns: list[object] = []
        self.used_model = "claude-sonnet-5-5"
        # Tool calls to make through the turn's real toolset before replying.
        self.tool_calls = list(tool_calls)
        self.results: list[tuple[str, bool]] = []
        self.during = during

    def run_structured(self, prompt, turn, parse):
        self.prompts.append(prompt)
        self.turns.append(turn)
        for name, arguments in self.tool_calls:
            self.results.append(turn.toolset.call(name, arguments))
        if self.during is not None:
            self.during()
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return parse(reply)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, EventLevel, dict]] = []

    def __call__(self, event_type, message, level, metadata) -> None:
        self.events.append((event_type, message, level, metadata))

    @property
    def types(self) -> list[str]:
        return [event[0] for event in self.events]


def verifier(course, agent=None, *, lookup=None, events=None, cancelled=lambda: False):
    return AgendaVerifier(
        course_id="physics",
        course=course,
        provider="claude",
        provider_label="Claude",
        model="claude-sonnet-5-5",
        agent_key="claude:claude-sonnet-5-5|effort:medium",
        backend_factory=(lambda: agent) if agent is not None else None,
        lookup=lookup,
        today=WEEK,
        emit=events if events is not None else Recorder(),
        cancelled=cancelled,
    )


def resolve(check: AgendaVerifier, source: CanvasAgendaSource) -> SourceCapture:
    """Run discovery, then the check, as CourseAgendaSource does with no fallback."""
    return CourseAgendaSource(lambda: source, None, AcquisitionStrategy.AUTO, check).capture(
        include_image=False
    )


# --- Verified agendas -----------------------------------------------------------------------


def test_a_verified_candidate_is_used_unchanged_and_annotated(course):
    source, _session = canvas_source(course_routes(CORRECT))
    plain = canvas_source(course_routes(CORRECT))[0].capture(include_image=False)
    agent = ScriptedAgent(
        verdict(
            "verified",
            named("candidate", None),
            [
                evidence(
                    "heading_date",
                    "Week of August 24",
                    source=("candidate", None),
                    stated="2026-08-24",
                    supports="requested_week",
                ),
                evidence(
                    "assignment_due",
                    "Practice set 4",
                    source=("assignment", "501"),
                    stated="2026-08-25",
                    supports="requested_week",
                ),
            ],
        )
    )
    events = Recorder()
    check = verifier(course, agent, events=events)
    capture = resolve(check, source)

    assert capture.page_hash == plain.page_hash
    assert capture.transcript == plain.transcript
    assert capture.source_metadata["agenda_verification"]["status"] == "verified"
    record = check.record
    assert record.status == AgendaStatus.VERIFIED and record.uses_candidate
    assert record.fingerprint == f"candidate:{plain.page_hash}"
    assert record.page_hash == plain.page_hash
    assert all(item.accepted for item in record.evidence)
    assert events.types == ["agenda_verification_started", "agenda_verified"]
    assert events.events[-1][2] == EventLevel.INFO


def test_the_agent_locates_an_undated_agenda_discovery_missed(course):
    source, _session = canvas_source(course_routes(UNDATED_CURRENT))
    with pytest.raises(CanvasAgendaNotFound):
        canvas_source(course_routes(UNDATED_CURRENT))[0].capture(include_image=False)
    agent = ScriptedAgent(
        verdict(
            "verified",
            named(table=1),
            [
                evidence(
                    "assignment_due",
                    "Practice set 4",
                    source=("assignment", "501"),
                    stated="2026-08-25",
                    supports="requested_week",
                )
            ],
        )
    )
    check = verifier(course, agent)
    capture = resolve(check, source)

    assert "Start Unit 3 lab" in capture.transcript
    assert "Finish Unit 2 review" not in capture.transcript
    assert capture.source_key == f"canvas:{COURSE_ID}:week:2026-08-24"
    record = check.record
    assert record.status == AgendaStatus.VERIFIED and not record.uses_candidate
    assert record.candidate is None and record.fingerprint.startswith("inventory:")
    assert record.agenda.kind == "page" and record.agenda.table_number == 1
    assert record.page_hash == capture.page_hash
    # Deterministic: capturing the same table again gives the same hash.
    again = canvas_source(course_routes(UNDATED_CURRENT))[0]
    again.ensure_discovered()
    document = again.find_document("page", "weekly-agenda")
    assert again.capture_document(document, table_number=1).page_hash == capture.page_hash
    assert "Claude located the Canvas agenda" in record.describe()


# --- Mislabeled and unresolved agendas never reach extraction --------------------------------


def test_a_forgotten_heading_is_reported_as_mislabeled_with_an_override_to_confirm(course):
    source, _session = canvas_source(course_routes(FORGOTTEN_HEADING))
    agent = ScriptedAgent(
        verdict(
            "suspected_mislabeled",
            named(table=1, distinctive="Start Unit 3 lab"),
            [
                evidence(
                    "heading_date", "Week of August 17", stated="2026-08-17", supports="other_week"
                ),
                evidence(
                    "assignment_due",
                    "Practice set 4",
                    source=("assignment", "501"),
                    stated="2026-08-25",
                    supports="requested_week",
                ),
                evidence(
                    "neighbor_week",
                    "Finish Unit 2 review",
                    supports="neutral",
                ),
            ],
            summary="Table 1 is new Unit 3 work but still headed August 17.",
        )
    )
    events = Recorder()
    check = verifier(course, agent, events=events)
    with pytest.raises(AgendaVerificationError) as raised:
        resolve(check, source)

    record = raised.value.verification
    assert record is check.record
    assert record.status == AgendaStatus.SUSPECTED_MISLABELED
    assert record.page_hash is None
    suggestion = record.override_suggestion
    assert suggestion is not None
    assert (suggestion.page_slug, suggestion.table_number) == ("weekly-agenda", 1)
    assert suggestion.expected_heading_date == date(2026, 8, 17)
    assert suggestion.target_week_start == WEEK
    assert suggestion.required_text == "Start Unit 3 lab"
    message = str(raised.value)
    assert "mislabeled" in message and "no dates were reinterpreted" in message
    assert "temporary agenda override" in message
    assert events.types[-1] == "agenda_unverified"
    assert events.events[-1][2] == EventLevel.WARNING


def test_an_agent_cannot_verify_a_table_whose_own_heading_names_another_week(course):
    source, _session = canvas_source(course_routes(FORGOTTEN_HEADING))
    agent = ScriptedAgent(
        verdict(
            "verified",
            named(table=1),
            [
                evidence(
                    "assignment_due",
                    "Practice set 4",
                    source=("assignment", "501"),
                    stated="2026-08-25",
                    supports="requested_week",
                )
            ],
        )
    )
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationError):
        resolve(check, source)
    record = check.record
    assert record.agent_status == AgendaStatus.VERIFIED
    assert record.status == AgendaStatus.SUSPECTED_MISLABELED
    assert "heading names the week of 2026-08-17" in record.reasons[0]


def test_a_current_heading_over_last_weeks_dated_days_is_not_verified(course):
    source, _session = canvas_source(course_routes(STALE_DAYS))
    agent = ScriptedAgent(verdict("verified", named("candidate", None)))
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationError):
        resolve(check, source)
    assert check.record.status == AgendaStatus.SUSPECTED_MISLABELED
    assert "dated days" in check.record.reasons[0]


def test_verified_needs_dated_evidence_for_an_agenda_without_a_date(course):
    body = agenda_table("Unit 3", "Monday|Start Unit 3 lab", "Read chapter 4")
    source, _session = canvas_source(course_routes(body))
    agent = ScriptedAgent(
        verdict(
            "verified",
            named(table=1),
            [evidence("content", "Start Unit 3 lab", supports="requested_week")],
        )
    )
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationError):
        resolve(check, source)
    assert check.record.status == AgendaStatus.UNRESOLVED
    assert "Nothing dated in Canvas" in check.record.reasons[0]


def test_context_about_other_weeks_does_not_block_a_verified_agenda(course):
    # Live agents label the neighboring week's agenda "other_week" while verifying this one.
    source, _session = canvas_source(course_routes(CORRECT))
    agent = ScriptedAgent(
        verdict(
            "verified",
            named("candidate", None),
            [
                evidence(
                    "heading_date",
                    "Week of August 24",
                    source=("candidate", None),
                    stated="2026-08-24",
                    supports="requested_week",
                ),
                evidence(
                    "neighbor_week", "Week of August 17", stated="2026-08-17", supports="other_week"
                ),
                evidence(
                    "assignment_due",
                    "Practice set 3",
                    source=("assignment", "500"),
                    stated="2026-08-18",
                    supports="other_week",
                ),
            ],
        )
    )
    check = verifier(course, agent)
    resolve(check, source)
    assert check.record.status == AgendaStatus.VERIFIED
    assert all(item.accepted for item in check.record.evidence)


def test_an_unresolved_verdict_is_never_upgraded(course):
    source, _session = canvas_source(course_routes(CORRECT))
    agent = ScriptedAgent(
        verdict(
            "unresolved",
            named("candidate", None),
            [
                evidence(
                    "heading_date",
                    "Week of August 24",
                    source=("candidate", None),
                    stated="2026-08-24",
                    supports="requested_week",
                )
            ],
        )
    )
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationError) as raised:
        resolve(check, source)
    assert check.record.status == AgendaStatus.UNRESOLVED
    assert "could not verify" in str(raised.value)


def test_an_unconfirmed_mislabel_suspicion_becomes_unresolved(course):
    source, _session = canvas_source(course_routes(CORRECT))
    agent = ScriptedAgent(verdict("suspected_mislabeled", named("candidate", None)))
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationError):
        resolve(check, source)
    assert check.record.status == AgendaStatus.UNRESOLVED
    assert check.record.override_suggestion is None


@pytest.mark.parametrize(
    "agenda",
    [
        None,
        named(identifier="no-such-page"),
        named(identifier="../../users/self"),
        named(table=7),
        named("assignment", "999"),
    ],
)
def test_a_verdict_naming_nothing_real_is_unresolved(course, agenda):
    source, session = canvas_source(course_routes(UNDATED_CURRENT))
    agent = ScriptedAgent(verdict("verified", agenda))
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationError):
        resolve(check, source)
    assert check.record.status == AgendaStatus.UNRESOLVED
    assert all(call.startswith(PREFIX) for call in session.calls)


# --- Evidence is checked against Canvas -----------------------------------------------------


def _check(source, item, candidate=None):
    reader = CanvasCourseReader(source, candidate=candidate)
    return check_evidence(
        VerdictEvidence.model_validate(item), source=source, reader=reader, candidate=candidate
    )


def test_evidence_must_quote_canvas_and_state_dates_that_canvas_states(course):
    source, _session = canvas_source(course_routes(CORRECT))
    source.ensure_discovered()
    # Case, spacing, and typographic quotes or dashes do not matter; wording does.
    assert _check(
        source, evidence("heading_date", "week of  AUGUST 24", stated="2026-08-24")
    ).accepted
    accepted = _check(source, evidence("heading_date", "Week of August 24", stated="2026-08-24"))
    assert accepted.accepted and accepted.stated_date == date(2026, 8, 24)
    cases = {
        "the quote does not appear": evidence(
            "heading_date", "Week of August 31", stated="2026-08-31"
        ),
        "does not state that date": evidence(
            "heading_date", "Week of August 24", stated="2026-08-25"
        ),
        "different due date": evidence(
            "assignment_due", "Practice set 4", source=("assignment", "501"), stated="2026-08-24"
        ),
        "different last-edited date": evidence(
            "edit_date", "Weekly agenda", stated="2026-08-20"
        ),
        "could not be found": evidence(
            "content", "Start Unit 3 lab", source=("page", "missing-page")
        ),
        "not a YYYY-MM-DD": evidence("day_date", "Monday", stated="next Monday"),
        "needs the date": evidence("heading_date", "Week of August 24"),
        "no quote": evidence("content", " "),
    }
    for problem, item in cases.items():
        checked = _check(source, item)
        assert not checked.accepted, problem
        assert problem in checked.problem
    edit = _check(source, evidence("edit_date", "Weekly agenda", stated="2026-08-23"))
    assert edit.accepted


def test_rejected_evidence_is_kept_on_the_record_but_set_aside(course):
    source, _session = canvas_source(course_routes(CORRECT))
    agent = ScriptedAgent(
        verdict(
            "verified",
            named("candidate", None),
            [
                evidence(
                    "heading_date",
                    "Week of August 24",
                    source=("candidate", None),
                    stated="2026-08-24",
                    supports="requested_week",
                ),
                evidence("content", "Invented sentence that Canvas never said"),
            ],
        )
    )
    check = verifier(course, agent)
    resolve(check, source)
    assert check.record.status == AgendaStatus.VERIFIED
    assert [item.accepted for item in check.record.evidence] == [True, False]
    assert "set aside" in check.record.reasons[-1]


# --- Caching, replay, and fallbacks ---------------------------------------------------------


def located() -> dict[str, object]:
    """The agent's verdict that table 1 of the weekly agenda page is this week's."""
    return verdict(
        "verified",
        named(table=1),
        [
            evidence(
                "assignment_due",
                "Practice set 4",
                source=("assignment", "501"),
                stated="2026-08-25",
                supports="requested_week",
            )
        ],
    )


def test_a_cached_verification_of_the_same_agenda_needs_no_agent_turn(course):
    first = verifier(course, ScriptedAgent(located()))
    expected = resolve(first, canvas_source(course_routes(UNDATED_CURRENT))[0])
    stored = AgendaVerification.model_validate_json(first.record.model_dump_json())
    keys: list[tuple[str, str]] = []

    def lookup(source_key, fingerprint):
        keys.append((source_key, fingerprint))
        return stored if fingerprint == stored.fingerprint else None

    agent = ScriptedAgent()
    events = Recorder()
    check = verifier(course, agent, lookup=lookup, events=events)
    capture = resolve(check, canvas_source(course_routes(UNDATED_CURRENT))[0])
    assert capture.page_hash == expected.page_hash
    assert agent.prompts == []
    assert check.record.cached
    assert keys == [(f"canvas:{COURSE_ID}:week:2026-08-24", stored.fingerprint)]
    assert "Reused the verification" in events.events[-1][1]


def test_a_cached_alternative_that_changed_is_verified_again(course):
    first = verifier(course, ScriptedAgent(located()))
    resolve(first, canvas_source(course_routes(UNDATED_CURRENT))[0])
    stored = first.record
    # The inventory fingerprint is unchanged only if the page is unchanged, so fake a stale
    # entry: same fingerprint, a page hash the table no longer captures to.
    stale = stored.model_copy(update={"page_hash": "0" * 64})
    agent = ScriptedAgent(verdict("unresolved", None))
    check = verifier(course, agent, lookup=lambda *_: stale)
    with pytest.raises(AgendaVerificationError):
        resolve(check, canvas_source(course_routes(UNDATED_CURRENT))[0])
    assert len(agent.prompts) == 1
    assert not check.record.cached


def test_a_cached_rejection_is_replayed_without_an_agent(course):
    first = verifier(course, ScriptedAgent(verdict("unresolved", None)))
    with pytest.raises(AgendaVerificationError):
        resolve(first, canvas_source(course_routes(CORRECT))[0])
    stored = first.record
    agent = ScriptedAgent()
    check = verifier(course, agent, lookup=lambda *_: stored)
    with pytest.raises(AgendaVerificationError):
        resolve(check, canvas_source(course_routes(CORRECT))[0])
    assert agent.prompts == [] and check.record.cached


def test_replay_reapplies_the_decision_and_detects_changes(course):
    first = verifier(course, ScriptedAgent(located()))
    expected = resolve(first, canvas_source(course_routes(UNDATED_CURRENT))[0])
    record = AgendaVerification.model_validate_json(first.record.model_dump_json())

    replay = AgendaVerifier.replay(record, course=course)
    assert replay.discovers(fallback_available=True)
    capture = resolve(replay, canvas_source(course_routes(UNDATED_CURRENT))[0])
    assert capture.page_hash == expected.page_hash

    changed = UNDATED_CURRENT.replace("Start Unit 3 lab", "Start Unit 3 lab and quiz")
    # The page changed, and so does discovery's starting point when it now finds a dated one.
    for body in (changed, CORRECT):
        with pytest.raises(ValueError, match="changed after this preview"):
            resolve(
                AgendaVerifier.replay(record, course=course),
                canvas_source(course_routes(body))[0],
            )


def test_with_a_fallback_a_missing_canvas_agenda_uses_it_without_an_agent(course):
    source, _session = canvas_source(course_routes(FORGOTTEN_HEADING))
    agent = ScriptedAgent()
    check = verifier(course, agent)
    assert not check.discovers(fallback_available=True)

    class Fallback:
        def capture(self, *, include_image):
            del include_image
            return SourceCapture(
                source_key="google_slides:deck:page",
                source_url="https://docs.google.com/presentation/d/x/edit",
                page_hash="f" * 64,
                transcript="Fallback agenda",
                blocks=[AgendaBlock(anchor="a", element_id="e", kind="text", text="Agenda")],
            )

    capture = CourseAgendaSource(
        lambda: source, lambda: Fallback(), AcquisitionStrategy.AUTO, check
    ).capture(include_image=False)
    assert capture.source_type == "google_slides"
    assert agent.prompts == [] and check.record is None


def test_an_unverified_canvas_agenda_falls_back_to_the_configured_source(course):
    source, _session = canvas_source(course_routes(CORRECT))
    check = verifier(course, ScriptedAgent(verdict("unresolved", None)))

    class Fallback:
        def capture(self, *, include_image):
            del include_image
            return SourceCapture(
                source_key="google_slides:deck:page",
                source_url="https://docs.google.com/presentation/d/x/edit",
                page_hash="f" * 64,
                transcript="Fallback agenda",
                blocks=[AgendaBlock(anchor="a", element_id="e", kind="text", text="Agenda")],
            )

    capture = CourseAgendaSource(
        lambda: source, lambda: Fallback(), AcquisitionStrategy.AUTO, check
    ).capture(include_image=False)
    fallback = capture.source_metadata["acquisition_fallback"]
    assert fallback["agenda_verification"] == "unresolved"
    assert "could not verify" in fallback["reason"]

    strict = verifier(course, ScriptedAgent(verdict("unresolved", None)))
    with pytest.raises(AgendaVerificationError):
        CourseAgendaSource(
            lambda: canvas_source(course_routes(CORRECT))[0],
            lambda: Fallback(),
            AcquisitionStrategy.CANVAS_API,
            strict,
        ).capture(include_image=False)


def test_an_explicit_override_is_never_second_guessed(course):
    from canvas_task_sync.configuration import CanvasAgendaOverride

    routes = course_routes(FORGOTTEN_HEADING)
    session = FakeSession(routes)
    source = CanvasAgendaSource(
        course_id=COURSE_ID,
        target_week_start=WEEK,
        current_week_start=WEEK,
        base_url=BASE,
        token="test-token",
        session=session,
        agenda_override=CanvasAgendaOverride(
            page_slug="weekly-agenda",
            table_number=1,
            expected_heading_date=date(2026, 8, 17),
            target_week_start=WEEK,
            required_text="Start Unit 3 lab",
        ),
    )
    agent = ScriptedAgent()
    check = verifier(course, agent)
    capture = CourseAgendaSource(lambda: source, None, AcquisitionStrategy.AUTO, check).capture(
        include_image=False
    )
    assert capture.source_metadata["agenda_override"]
    assert agent.prompts == [] and check.record is None


# --- The agent turn -------------------------------------------------------------------------


def test_agent_failures_stop_the_run_instead_of_falling_back(course):
    from canvas_task_sync.agent_backends import AgentExtractionError

    source, _session = canvas_source(course_routes(CORRECT))
    agent = ScriptedAgent(AgentExtractionError("The Claude plan's usage limit was reached."))
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationUnavailable, match="could not verify the Canvas agenda"):
        CourseAgendaSource(
            lambda: source, lambda: pytest.fail("no fallback"), AcquisitionStrategy.AUTO, check
        ).capture(include_image=False)


def test_a_cancelled_turn_raises_its_own_error_for_the_run_to_report(course):
    from canvas_task_sync.agent_backends import AgentExtractionError

    source, _session = canvas_source(course_routes(CORRECT))
    agent = ScriptedAgent(AgentExtractionError("The run was cancelled."))
    check = verifier(course, agent, cancelled=lambda: True)
    with pytest.raises(AgentExtractionError, match="cancelled"):
        resolve(check, source)


def test_the_turn_gets_the_verifier_prompt_schema_and_read_only_tools(course):
    source, _session = canvas_source(course_routes(CORRECT))
    agent = ScriptedAgent(verdict("unresolved", None))
    with pytest.raises(AgendaVerificationError):
        resolve(verifier(course, agent), source)
    turn = agent.turns[0]
    assert "untrusted data" in turn.system_prompt
    assert turn.output_schema == verdict_output_schema()
    assert turn.max_turns and turn.max_turns <= 40
    assert turn.toolset.names == [
        "course_overview",
        "search_course",
        "read_document",
        "read_assignment",
        "read_module",
        "follow_link",
    ]
    prompt = agent.prompts[0]
    assert "2026-08-24 (Monday) through 2026-08-30 (Sunday)" in prompt
    assert "<candidate-agenda>" in prompt and "Start Unit 3 lab" in prompt
    assert "COURSE OVERVIEW" in prompt and "table 1: 2026-08-24" in prompt
    assert "test-token" not in prompt


def test_the_prompt_for_a_missing_agenda_says_discovery_found_none(course):
    source, _session = canvas_source(course_routes(FORGOTTEN_HEADING))
    with pytest.raises(CanvasAgendaNotFound) as missing:
        source.capture(include_image=False)
    reader = CanvasCourseReader(source)
    prompt = build_verification_prompt(
        course=course, source=source, reader=reader, candidate=None, missing=missing.value,
        today=WEEK,
    )
    assert "NO CANDIDATE" in prompt and "No sufficiently specific Canvas agenda" in prompt
    assert "table 1: 2026-08-17" in prompt and "table 2: 2026-08-17" in prompt


def test_verdict_schema_is_strict_and_malformed_verdicts_are_retryable():
    from canvas_task_sync.agent_backends import AgentExtractionError

    def walk(node):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    schema = verdict_output_schema()
    for node in walk(schema):
        assert "$ref" not in node and "default" not in node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
    assert parse_verdict('{"status": "unresolved", "agenda": null, "summary": "x", '
                         '"evidence": [], "concerns": []}').status == AgendaStatus.UNRESOLVED
    for broken in ("nope", {"status": "maybe", "summary": ""}, {"summary": "x"}):
        with pytest.raises(AgentExtractionError) as raised:
            parse_verdict(broken)
        assert raised.value.retryable
    assert AgendaVerdict.model_validate(verdict("verified", named())).agenda.kind == "page"
    assert VerdictSource(kind="candidate").id is None


def test_an_alternative_on_another_page_replaces_discoverys_choice_and_is_replayed(course):
    unit_page = {
        "url": "unit-3-agenda",
        "title": "Unit 3 agenda",
        "html_url": f"{BASE}/courses/{COURSE_ID}/pages/unit-3-agenda",
        "body": agenda_table("Unit 3", "Monday|Start Unit 3 lab", link(501, "Practice set 4")),
    }
    stale_front = agenda_table(
        "Week of August 24", "Monday|Holiday schedule draft", "Nothing assigned"
    )
    agent = ScriptedAgent(
        verdict(
            "verified",
            named(identifier="unit-3-agenda", table=1),
            [
                evidence(
                    "assignment_due",
                    "Practice set 4",
                    source=("assignment", "501"),
                    stated="2026-08-25",
                    supports="requested_week",
                )
            ],
            summary="The weekly page is a draft; the Unit 3 page links this week's work.",
        )
    )
    check = verifier(course, agent)
    capture = resolve(
        check, canvas_source(course_routes(stale_front, extra_pages=[unit_page]))[0]
    )
    record = AgendaVerification.model_validate_json(check.record.model_dump_json())
    assert capture.page_id == "unit-3-agenda"
    assert record.candidate.id == "weekly-agenda" and record.agenda.id == "unit-3-agenda"
    assert record.fingerprint.startswith("candidate:") and not record.uses_candidate
    assert "discovery had selected" in record.describe()

    replayed = resolve(
        AgendaVerifier.replay(record, course=course),
        canvas_source(course_routes(stale_front, extra_pages=[unit_page]))[0],
    )
    assert replayed.page_hash == capture.page_hash

    edited = {**unit_page, "body": unit_page["body"].replace("lab", "lab report")}
    with pytest.raises(ValueError, match="verified Canvas agenda changed after this preview"):
        resolve(
            AgendaVerifier.replay(record, course=course),
            canvas_source(course_routes(stale_front, extra_pages=[edited]))[0],
        )


# --- Regressions from review ----------------------------------------------------------------


def test_evidence_about_anything_but_the_named_agenda_never_supports_it(course):
    undated = agenda_table("Unit 3", "Monday|Start Unit 3 lab", "Read chapter 4")
    unlinked_assignment = evidence(
        "assignment_due",
        "Practice set 4",
        source=("assignment", "501"),
        stated="2026-08-25",
        supports="requested_week",
    )
    other_table_heading = evidence(
        "heading_date", "Week of August 24", stated="2026-08-24", supports="requested_week"
    )
    week_module = evidence(
        "module_placement",
        "Week of August 24",
        source=("module", "12"),
        stated="2026-08-24",
        supports="requested_week",
    )

    def run(body, modules, item):
        routes = course_routes(body)
        routes[f"{PREFIX}/modules"] = modules
        check = verifier(course, ScriptedAgent(verdict("verified", named(table=1), [item])))
        with contextlib.suppress(AgendaVerificationError):
            resolve(check, canvas_source(routes)[0])
        assert check.record.evidence[0].accepted
        return check.record

    empty_week = [{"id": 12, "name": "Week of August 24", "items": []}]
    assert run(undated, empty_week, unlinked_assignment).status == AgendaStatus.UNRESOLVED
    assert run(undated, empty_week, week_module).status == AgendaStatus.UNRESOLVED
    # A heading quoted from the page's other table says nothing about table 1.
    two_tables = undated + agenda_table("Week of August 24", "Monday|Other", "Other work")
    assert run(two_tables, empty_week, other_table_heading).status == AgendaStatus.UNRESOLVED
    holding = [
        {
            "id": 12,
            "name": "Week of August 24",
            "items": [{"id": 80, "type": "Page", "title": "Agenda", "page_url": "weekly-agenda"}],
        }
    ]
    assert run(undated, holding, week_module).status == AgendaStatus.VERIFIED


PAST_AGENDAS = (
    "<h3>Week of August 24</h3>"
    + agenda_table("Day", "Monday|Start Unit 3 lab", "Practice set 4 problems")
    + "<h3>Week of August 17</h3>"
    + agenda_table("Day", "Monday|Finish Unit 2 review", "Unit 2 test moved to 8/25")
)


def test_a_table_headed_just_before_it_for_another_week_is_not_verified(course):
    # Discovery picks last week's table for its incidental 8/25; its heading is outside it.
    plain = canvas_source(course_routes(PAST_AGENDAS))[0].capture(include_image=False)
    assert "Finish Unit 2 review" in plain.transcript
    assert plain.source_metadata["heading_dates"] == ["2026-08-17"]
    check = verifier(course, ScriptedAgent(verdict("verified", named("candidate", None))))
    with pytest.raises(AgendaVerificationError):
        resolve(check, canvas_source(course_routes(PAST_AGENDAS))[0])
    assert check.record.status == AgendaStatus.SUSPECTED_MISLABELED
    assert "heading names the week of 2026-08-17" in check.record.reasons[0]

    located = verifier(course, ScriptedAgent(verdict("verified", named(table=1))))
    capture = resolve(located, canvas_source(course_routes(PAST_AGENDAS))[0])
    assert "Start Unit 3 lab" in capture.transcript and "Unit 2" not in capture.transcript
    assert located.record.status == AgendaStatus.VERIFIED
    assert capture.source_metadata["heading_dates"] == ["2026-08-24"]


def test_a_negative_verdict_is_reused_only_while_the_course_is_unchanged(course):
    first = verifier(course, ScriptedAgent(verdict("unresolved", None)))
    with pytest.raises(AgendaVerificationError):
        resolve(first, canvas_source(course_routes(CORRECT))[0])
    stored = AgendaVerification.model_validate_json(first.record.model_dump_json())
    assert stored.inventory and stored.cacheable

    extra = {"url": "new-page", "title": "New page", "body": "<p>Week 4 plans</p>"}
    agent = ScriptedAgent(verdict("unresolved", None))
    check = verifier(course, agent, lookup=lambda *_: stored)
    with pytest.raises(AgendaVerificationError):
        resolve(check, canvas_source(course_routes(CORRECT, extra_pages=[extra]))[0])
    assert len(agent.prompts) == 1 and not check.record.cached

    with pytest.raises(ValueError, match="Canvas course changed after this preview"):
        resolve(
            AgendaVerifier.replay(stored, course=course),
            canvas_source(course_routes(CORRECT, extra_pages=[extra]))[0],
        )


def test_a_negative_verdict_reached_while_canvas_failed_is_not_cacheable(course):
    routes = course_routes(CORRECT)
    routes[f"{PREFIX}/pages/unit-4"] = requests.ConnectionError("Canvas is down")
    agent = ScriptedAgent(
        verdict("unresolved", None),
        tool_calls=[("read_document", {"kind": "page", "id": "unit-4", "part": None})],
    )
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationError):
        resolve(check, canvas_source(routes)[0])
    assert agent.results[0][1] is True and "could not be read" in agent.results[0][0]
    assert check.record.tool_failures == 1 and not check.record.cacheable
    assert check.record.event_metadata()["tool_failures"] == 1


def test_naming_discoverys_agenda_keeps_discoverys_extent(course):
    check = verifier(
        course,
        ScriptedAgent(
            verdict(
                "verified",
                named("candidate", None, table=2),
                [
                    evidence(
                        "heading_date",
                        "Week of August 24",
                        source=("candidate", None),
                        stated="2026-08-24",
                        supports="requested_week",
                    )
                ],
            )
        ),
    )
    resolve(check, canvas_source(course_routes(CORRECT))[0])
    assert check.record.uses_candidate and check.record.agenda.table_number is None
    assert "table" not in check.record.describe()


def test_a_reply_that_arrives_after_cancellation_never_selects_the_fallback(course):
    cancelled = {"value": False}
    agent = ScriptedAgent(
        verdict("unresolved", None), during=lambda: cancelled.__setitem__("value", True)
    )
    check = verifier(course, agent, cancelled=lambda: cancelled["value"])
    with pytest.raises(AgendaVerificationUnavailable, match="cancelled"):
        CourseAgendaSource(
            lambda: canvas_source(course_routes(CORRECT))[0],
            lambda: pytest.fail("a cancelled run must not capture its fallback"),
            AcquisitionStrategy.AUTO,
            check,
        ).capture(include_image=False)
    assert check.record is None


def test_an_agenda_with_no_week_label_cannot_be_called_mislabeled(course):
    # Live Codex at low effort called an undated table "mislabeled"; it can only be unresolved.
    agent = ScriptedAgent(
        verdict(
            "suspected_mislabeled",
            named(table=1),
            [
                evidence(
                    "assignment_due",
                    "Practice set 4",
                    source=("assignment", "501"),
                    stated="2026-08-25",
                    supports="requested_week",
                ),
                evidence("content", "Start Unit 3 lab", source=("candidate", None)),
            ],
        )
    )
    check = verifier(course, agent)
    with pytest.raises(AgendaVerificationError, match="could not verify"):
        resolve(check, canvas_source(course_routes(UNDATED_CURRENT))[0])
    assert check.record.status == AgendaStatus.UNRESOLVED
    assert "cannot be mislabeled" in check.record.reasons[0]
    # With no candidate, citing one is set aside rather than matched against anything.
    assert check.record.evidence[1].problem == "its source could not be found in this course"
    assert 'never use kind "candidate"' in agent.prompts[0]
