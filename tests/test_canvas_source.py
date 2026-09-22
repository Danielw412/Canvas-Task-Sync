from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

import pytest

from canvas_task_sync.configuration import CourseSettings
from canvas_task_sync.models import (
    AcquisitionStrategy,
    ActionKind,
    AgendaBlock,
    BlockRole,
    Confidence,
    DueRelation,
    ExtractedTask,
    SourceCapture,
    TaskClassification,
    TaskType,
    WeekSelection,
)
from canvas_task_sync.scheduling import build_draft_tasks, row_date_ranges
from canvas_task_sync.sources import CourseAgendaSource
from canvas_task_sync.sources.canvas import (
    CanvasAgendaNotFound,
    CanvasAgendaSource,
    week_match_score,
)
from canvas_task_sync.week import selected_week_start

TARGET_WEEK = date(2026, 8, 17)
COURSE_IDS = ["12604", "11126", "11517", "12506"]


class FakeResponse:
    def __init__(self, payload, *, links=None):
        self.payload = payload
        self.links = links or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url, *, params=None, timeout=None):
        del params, timeout
        path = urlparse(url).path
        self.calls.append(path)
        payload = self.routes.get(path)
        if payload is None:
            raise AssertionError(f"Unexpected Canvas request: {path}")
        return FakeResponse(payload)


def agenda_html(heading: str) -> str:
    return f"""
    <div class="course-home"><p>Other course information</p>
      <section class="weekly-agenda">
        <h2>{heading}</h2>
        <p>Learning Targets for the Week: compare distributions.</p>
        <table>
          <tr><th>Day</th><th>Learning Activities</th><th>Assignments</th></tr>
          <tr><td>M</td><td>Review samples</td><td>Complete practice set 1</td></tr>
          <tr><td>T</td><td>Analyze results</td><td>Bring calculator</td></tr>
        </table>
      </section>
    </div>
    """


def physics_agenda_table(week_start: str, monday_text: str, friday_text: str) -> str:
    return f"""
    <table>
      <tr><td>Learning Targets for the Week: {week_start}. Apply kinematics equations.
        How to find the assignments in Canvas. All assignments are due on the date indicated.
      </td></tr>
      <tr><td></td><td></td><td>Assignments and learning activities</td></tr>
      <tr><td>M</td><td></td><td>Monday {monday_text}</td></tr>
      <tr><td>T</td><td></td><td>Tuesday Classwork: Unit 1 Lab</td></tr>
      <tr><td>W</td><td></td><td>Wednesday Classwork: Unit 1 Lab</td></tr>
      <tr><td>Th</td><td></td><td>Thursday Classwork: Unit 1 practice</td></tr>
      <tr><td>F</td><td></td><td>Friday {friday_text}</td></tr>
    </table>
    """


@pytest.mark.parametrize(
    "heading",
    [
        "Week of August 17",
        "August 17-21",
        "Week of August 17 to August 20",
        "Week of Aug. 17 through Aug. 21",
        "Week of 8/17-8/21",
    ],
)
def test_week_matching_accepts_date_variations_and_ignores_end_date_typos(heading):
    text = f"{heading} Learning Activities Assignments Monday Tuesday"
    assert week_match_score(text, TARGET_WEEK) >= 100


def test_week_selection_resolves_previous_current_and_next_mondays():
    today = date(2026, 8, 19)
    assert selected_week_start(today, WeekSelection.PREVIOUS) == date(2026, 8, 10)
    assert selected_week_start(today, WeekSelection.CURRENT) == TARGET_WEEK
    assert selected_week_start(today, WeekSelection.NEXT) == date(2026, 8, 24)


@pytest.mark.parametrize("course_id", COURSE_IDS)
def test_example_canvas_course_ids_produce_week_scoped_captures(course_id):
    prefix = f"/api/v1/courses/{course_id}"
    session = FakeSession(
        {
            f"{prefix}/front_page": {
                "url": "home",
                "title": "Home",
                "html_url": f"https://canvas.example/courses/{course_id}",
                "body": agenda_html("August 17-21"),
            },
            prefix: {},
            f"{prefix}/modules": [],
            f"{prefix}/pages": [],
            f"{prefix}/assignments": [],
        }
    )
    capture = CanvasAgendaSource(
        course_id=course_id,
        target_week_start=TARGET_WEEK,
        base_url="https://canvas.example",
        token="test-token",
        session=session,
    ).capture(include_image=False)

    assert capture.source_key == f"canvas:{course_id}:week:2026-08-17"
    assert capture.resource_id == course_id
    assert capture.selection["week_start"] == "2026-08-17"
    assert "Complete practice set 1" in capture.transcript
    assert capture.source_metadata["screenshot_available"] is False
    monday_assignment = next(
        block
        for block in capture.blocks
        if block.role.value == "assignments" and block.row_label == "M"
    )
    assert monday_assignment.row_index == 1
    assert monday_assignment.column_index == 2
    assert row_date_ranges(capture)[
        (monday_assignment.element_id, monday_assignment.row_index)
    ] == (TARGET_WEEK, TARGET_WEEK)
    course = CourseSettings.model_validate(
        {
            "name": "Canvas course",
            "prefix": "CANVAS",
            "task_list": "School",
            "canvas_course_id": course_id,
            "source": {"type": "none", "extraction": {"mode": "text"}},
        }
    )
    drafts, uncertain, _ = build_draft_tasks(
        course_id=course_id,
        course=course,
        capture=capture,
        tasks=[
            ExtractedTask(
                source_anchor=monday_assignment.anchor,
                source_text="Complete practice set 1",
                row_label="M",
                classification=TaskClassification.HOMEWORK,
                action_kind=ActionKind.COMPLETE,
                title_stem="Practice set 1",
                due_relation=DueRelation.NEXT_CLASS,
                confidence=Confidence.HIGH,
            )
        ],
        today=TARGET_WEEK,
    )
    assert not uncertain
    assert drafts[0].due_date == date(2026, 8, 18)


def test_exact_canvas_assignment_link_is_retained_and_ambiguous_links_are_omitted():
    course_id = "11126"
    prefix = f"/api/v1/courses/{course_id}"
    linked_html = agenda_html("August 17-21").replace(
        "Complete practice set 1",
        f'<a href="/courses/{course_id}/assignments/42">Complete practice set 1</a>'
        f'<a href="https://evil.example/courses/{course_id}/assignments/99">Fake</a>',
    )
    session = FakeSession(
        {
            f"{prefix}/front_page": {
                "url": "home",
                "title": "Home",
                "html_url": f"https://canvas.example/courses/{course_id}",
                "body": linked_html,
            },
            prefix: {},
            f"{prefix}/modules": [],
            f"{prefix}/pages": [],
            f"{prefix}/assignments": [],
            f"{prefix}/assignments/42": {
                "id": 42,
                "name": "Practice set 1",
                "html_url": f"https://canvas.example/courses/{course_id}/assignments/42",
                "description": "Complete practice set 1.",
            },
        }
    )
    capture = CanvasAgendaSource(
        course_id=course_id,
        target_week_start=TARGET_WEEK,
        base_url="https://canvas.example",
        token="test-token",
        session=session,
    ).capture(include_image=False)
    assignment_block = next(
        block
        for block in capture.blocks
        if block.role.value == "assignments" and "practice set 1" in block.text
    )
    assert assignment_block.metadata["assignment_links"] == [
        {
            "url": f"https://canvas.example/courses/{course_id}/assignments/42",
            "text": "Complete practice set 1",
        }
    ]
    course = CourseSettings.model_validate(
        {
            "name": "AP Physics C",
            "prefix": "PHYSICS",
            "task_list": "School",
            "canvas_course_id": course_id,
            "source": {"type": "none", "extraction": {"mode": "text"}},
        }
    )
    task = ExtractedTask(
        source_anchor=assignment_block.anchor,
        source_text="Complete practice set 1",
        row_label="M",
        classification=TaskClassification.HOMEWORK,
        action_kind=ActionKind.COMPLETE,
        title_stem="Practice set 1",
        due_relation=DueRelation.NEXT_CLASS,
        confidence=Confidence.HIGH,
    )

    drafts, _, _ = build_draft_tasks(
        course_id=course_id,
        course=course,
        capture=capture,
        tasks=[task],
        today=TARGET_WEEK,
    )
    assert drafts[0].assignment_url == (
        f"https://canvas.example/courses/{course_id}/assignments/42"
    )

    ambiguous = capture.model_copy(deep=True)
    ambiguous_block = next(
        block for block in ambiguous.blocks if block.anchor == task.source_anchor
    )
    ambiguous_block.metadata["assignment_links"] = [
        {
            "url": f"https://canvas.example/courses/{course_id}/assignments/42",
            "text": "Practice set",
        },
        {
            "url": f"https://canvas.example/courses/{course_id}/assignments/43",
            "text": "Practice set",
        },
    ]
    ambiguous_drafts, _, _ = build_draft_tasks(
        course_id=course_id,
        course=course,
        capture=ambiguous,
        tasks=[task],
        today=TARGET_WEEK,
    )
    assert ambiguous_drafts[0].assignment_url is None

    unrelated = capture.model_copy(deep=True)
    unrelated_block = next(
        block for block in unrelated.blocks if block.anchor == task.source_anchor
    )
    unrelated_block.metadata["assignment_links"] = [
        {
            "url": f"https://canvas.example/courses/{course_id}/assignments/44",
            "text": "Different lab report",
        }
    ]
    unrelated_drafts, _, _ = build_draft_tasks(
        course_id=course_id,
        course=course,
        capture=unrelated,
        tasks=[task],
        today=TARGET_WEEK,
    )
    assert unrelated_drafts[0].assignment_url is None


def test_canvas_discovery_follows_internal_page_links_without_following_external_links():
    course_id = "12604"
    prefix = f"/api/v1/courses/{course_id}"
    linked_page = f"{prefix}/pages/week-august-17"
    session = FakeSession(
        {
            f"{prefix}/front_page": {
                "url": "home",
                "title": "Course home",
                "html_url": f"https://canvas.example/courses/{course_id}",
                "body": (
                    "<p>Open the weekly materials.</p>"
                    f'<a href="/courses/{course_id}/pages/week-august-17">Agenda</a>'
                    '<a href="https://docs.google.com/document/d/external/edit">External</a>'
                ),
            },
            prefix: {},
            f"{prefix}/modules": [],
            f"{prefix}/pages": [],
            f"{prefix}/assignments": [],
            linked_page: {
                "url": "week-august-17",
                "title": "Weekly agenda",
                "html_url": f"https://canvas.example/courses/{course_id}/pages/week-august-17",
                "body": agenda_html("Week of August 17 to August 20"),
            },
        }
    )

    capture = CanvasAgendaSource(
        course_id=course_id,
        target_week_start=TARGET_WEEK,
        base_url="https://canvas.example",
        token="test-token",
        session=session,
    ).capture(include_image=False)

    assert capture.page_id == "week-august-17"
    assert linked_page in session.calls
    assert all("docs.google.com" not in call for call in session.calls)
    assignment_blocks = [block for block in capture.blocks if block.role.value == "assignments"]
    assert any(block.row_label == "M" for block in assignment_blocks)


def test_canvas_discovery_uses_module_week_context_for_a_linked_undated_agenda():
    course_id = "11517"
    prefix = f"/api/v1/courses/{course_id}"
    page_path = f"{prefix}/pages/current-agenda"
    session = FakeSession(
        {
            f"{prefix}/front_page": {},
            prefix: {},
            f"{prefix}/modules": [
                {
                    "id": 9,
                    "name": "Week of August 17",
                    "items": [
                        {
                            "type": "Page",
                            "title": "Agenda",
                            "url": f"https://canvas.example{page_path}",
                        }
                    ],
                }
            ],
            f"{prefix}/pages": [],
            f"{prefix}/assignments": [],
            page_path: {
                "url": "current-agenda",
                "title": "Agenda",
                "html_url": f"https://canvas.example/courses/{course_id}/pages/current-agenda",
                "body": agenda_html("Weekly agenda"),
            },
        }
    )

    capture = CanvasAgendaSource(
        course_id=course_id,
        target_week_start=TARGET_WEEK,
        base_url="https://canvas.example",
        token="test-token",
        session=session,
    ).capture(include_image=False)

    assert capture.page_id == "current-agenda"
    assert capture.selection["matched_text"] == "Week of August 17"


def test_canvas_capture_scopes_a_multi_week_page_to_the_target_table():
    course_id = "11126"
    prefix = f"/api/v1/courses/{course_id}"
    body = (
        "<div class='agenda-history'>"
        + physics_agenda_table(
            "August 24",
            "Due on Monday August 24: Unit 1 Assignment 1",
            "Classwork: Unit 1 Assignment 3",
        )
        + physics_agenda_table(
            "August 17",
            "No School",
            "Due on Monday August 24: stale Unit 1 Assignment 1; Read chapter 3",
        )
        + "</div>"
    )
    session = FakeSession(
        {
            f"{prefix}/front_page": {
                "url": "physics-agenda",
                "title": "AP Physics C agenda",
                "html_url": f"https://canvas.example/courses/{course_id}/pages/physics-agenda",
                "body": body,
            },
            prefix: {},
            f"{prefix}/modules": [],
            f"{prefix}/pages": [],
            f"{prefix}/assignments": [],
        }
    )

    capture = CanvasAgendaSource(
        course_id=course_id,
        target_week_start=date(2026, 8, 24),
        base_url="https://canvas.example",
        token="test-token",
        session=session,
    ).capture(include_image=False)

    assert "Learning Targets for the Week: August 24" in capture.transcript
    assert "Learning Targets for the Week: August 17" not in capture.transcript
    assert "Read chapter 3" not in capture.transcript
    assert len(capture.blocks) == 12
    assert {block.metadata["table_index"] for block in capture.blocks} == {0}


def test_relative_this_week_heading_requires_current_target_and_current_canvas_update():
    course_id = "11126"
    prefix = f"/api/v1/courses/{course_id}"

    def source(target, updated_at):
        session = FakeSession(
            {
                f"{prefix}/front_page": {
                    "url": "home",
                    "title": "This week's agenda",
                    "updated_at": updated_at,
                    "body": agenda_html("This week's agenda"),
                },
                prefix: {},
                f"{prefix}/modules": [],
                f"{prefix}/pages": [],
                f"{prefix}/assignments": [],
            }
        )
        return CanvasAgendaSource(
            course_id=course_id,
            target_week_start=target,
            current_week_start=TARGET_WEEK,
            timezone_name="America/New_York",
            base_url="https://canvas.example",
            token="test-token",
            session=session,
        )

    capture = source(TARGET_WEEK, "2026-08-17T09:00:00-04:00").capture(include_image=False)
    assert capture.selection["matched_text"].casefold() == "this week's agenda"
    assert capture.source_metadata["canvas_updated_on"] == "2026-08-17"

    with pytest.raises(CanvasAgendaNotFound, match="No sufficiently specific Canvas agenda"):
        source(date(2026, 8, 10), "2026-08-17T09:00:00-04:00").capture(include_image=False)
    with pytest.raises(CanvasAgendaNotFound, match="No sufficiently specific Canvas agenda"):
        source(TARGET_WEEK, "2026-08-10T09:00:00-04:00").capture(include_image=False)


def front_page_source(course_id: str, body: str, target: date) -> CanvasAgendaSource:
    prefix = f"/api/v1/courses/{course_id}"
    session = FakeSession(
        {
            f"{prefix}/front_page": {
                "url": "agenda",
                "title": "Agenda",
                "html_url": f"https://canvas.example/courses/{course_id}/pages/agenda",
                "body": body,
            },
            prefix: {},
            f"{prefix}/modules": [],
            f"{prefix}/pages": [],
            f"{prefix}/assignments": [],
        }
    )
    return CanvasAgendaSource(
        course_id=course_id,
        target_week_start=target,
        base_url="https://canvas.example",
        token="test-token",
        session=session,
    )


def test_dated_day_cells_schedule_weekday_deadlines_and_duplicate_columns_collapse():
    body = """
    <table>
      <caption><h4>WEEK OF AUGUST 17-21, 2026</h4></caption>
      <thead><tr><th>Days</th><th>Learning Goals</th><th>Learning Activities</th>
        <th>Assignments / Homework</th></tr></thead>
      <tbody>
        <tr><td><p><strong>Monday</strong></p><p><strong>August 17th</strong></p></td>
          <td><span>Explain photoelectron spectroscopy.</span></td>
          <td><span>Explain photoelectron spectroscopy.</span></td>
          <td><ul><li><strong>Unit 2 Packet HW Due Tuesday in class</strong></li>
            <li><strong>Unit 2 AP Classroom MC/FR Due Thursday at midnight</strong></li>
          </ul></td></tr>
        <tr><td><p>Tuesday</p><p>August 18th</p></td><td>Work on AP Classroom</td>
          <td>Work on AP Classroom</td><td>Watch AP Classroom Daily Video 1.5</td></tr>
        <tr><td><p>Friday</p><p>August 21st</p></td>
          <td><ul><li>Take the Unit 2 Free Response Test In Class</li></ul></td>
          <td><ul><li>Take the Unit 2 Free Response Test In Class</li></ul></td>
          <td>Watch AP Classroom Daily Videos 1.3</td></tr>
      </tbody>
    </table>
    """
    capture = front_page_source("12506", body, TARGET_WEEK).capture(include_image=False)

    monday = next(block for block in capture.blocks if block.text == "Monday\nAugust 17th")
    assert (monday.role, monday.row_label) == (BlockRole.DAY, "Monday")
    homework = next(
        block
        for block in capture.blocks
        if block.role == BlockRole.ASSIGNMENTS and block.row_label == "Monday"
    )
    assert homework.text == (
        "Unit 2 Packet HW Due Tuesday in class\n"
        "Unit 2 AP Classroom MC/FR Due Thursday at midnight"
    )
    test_blocks = [block for block in capture.blocks if "Free Response Test" in block.text]
    assert [(block.role, block.row_label) for block in test_blocks] == [
        (BlockRole.LEARNING, "Friday")
    ]
    # The skipped Learning Goals duplicate still consumes its slot, keeping anchors stable.
    assert test_blocks[0].anchor == "canvas:agenda:15"
    assert row_date_ranges(capture)[(test_blocks[0].element_id, test_blocks[0].row_index)] == (
        date(2026, 8, 21),
        date(2026, 8, 21),
    )

    course = CourseSettings.model_validate(
        {
            "name": "AP Chemistry",
            "prefix": "CHEM",
            "task_list": "School",
            "canvas_course_id": "12506",
            "source": {"type": "none", "extraction": {"mode": "text"}},
        }
    )

    def extracted(block, source_text, due_relation, explicit_due_date=None, **overrides):
        fields = {
            "source_anchor": block.anchor,
            "source_text": source_text,
            "row_label": block.row_label,
            "classification": TaskClassification.HOMEWORK,
            "action_kind": ActionKind.COMPLETE,
            "title_stem": source_text,
            "due_relation": due_relation,
            "explicit_due_date": explicit_due_date,
            "confidence": Confidence.HIGH,
        }
        return ExtractedTask(**{**fields, **overrides})

    drafts, uncertain, _ = build_draft_tasks(
        course_id="12506",
        course=course,
        capture=capture,
        tasks=[
            extracted(
                homework,
                "Unit 2 Packet HW Due Tuesday in class",
                DueRelation.EXPLICIT_DATE,
                "Tuesday in class",
            ),
            extracted(
                homework,
                "Unit 2 AP Classroom MC/FR Due Thursday at midnight",
                DueRelation.EXPLICIT_DATE,
                "Thursday at midnight",
            ),
            extracted(
                test_blocks[0],
                "Take the Unit 2 Free Response Test In Class",
                DueRelation.SAME_DAY,
                task_type=TaskType.TEST,
                action_kind=ActionKind.OTHER,
            ),
        ],
        today=TARGET_WEEK,
    )

    assert not uncertain
    assert sorted((draft.due_date, draft.due_uncertain) for draft in drafts) == [
        (date(2026, 8, 18), False),
        (date(2026, 8, 20), False),
        (date(2026, 8, 21), False),
    ]


def test_day_cells_with_schedule_notes_are_still_dated_day_labels():
    body = """
    <table>
      <caption><h4>WEEK OF SEPTEMBER 21-25</h4></caption>
      <tr><th>Days</th><th>Learning Activities</th><th>Assignments / Homework</th></tr>
      <tr><td><p>Monday</p><p>September 21st</p><p>B Day</p></td>
        <td>Stoichiometry</td><td>Access Unit 3 packet here.</td></tr>
      <tr><td><p>Tuesday</p><p>September</p><p>22nd</p><p>(A)</p></td>
        <td>Solutions</td><td>Friday Quiz</td></tr>
    </table>
    """
    capture = front_page_source("12506", body, date(2026, 9, 21)).capture(
        include_image=False
    )
    dates = row_date_ranges(capture)

    day_cells = [block for block in capture.blocks if block.role == BlockRole.DAY]
    assert [block.row_label for block in day_cells] == ["Monday", "Tuesday"]
    assert [dates[(block.element_id, block.row_index)][0] for block in day_cells] == [
        date(2026, 9, 21),
        date(2026, 9, 22),
    ]
    # A weekday followed by content is content, not a day label.
    quiz = next(block for block in capture.blocks if block.text == "Friday Quiz")
    assert quiz.role == BlockRole.ASSIGNMENTS
    assert quiz.row_label == "Tuesday"


def linear_algebra_week(heading: str, monday_assignment: str) -> str:
    return f"""
    <div>
      <div><h1>🗓️ {heading}</h1></div>
      <div><p><strong>Learning Targets for the Week:</strong> Ax=b</p></div>
      <table>
        <tr><td></td><td>Learning Activities</td><td>Assignments</td></tr>
        <tr><td>M</td><td>Lesson</td><td>{monday_assignment}</td></tr>
        <tr><td>T</td><td>Lesson</td><td>Pearson</td></tr>
      </table>
      <div><p>📚 See previous weeks</p></div>
    </div>
    """


def test_page_of_past_weeks_is_scoped_to_the_target_week_section():
    # Each week's heading sits beside its table, so only the page as a whole contains
    # the target heading. The last week is also wrapped in an outer table.
    body = (
        linear_algebra_week("Week of September 14 to September 18", "Open Pearson")
        + linear_algebra_week("Week of September 7 to September 11", "Chapter 0 homework")
        + "<table><tr><td>"
        + linear_algebra_week("Week of August 31 to September 4", "1.2 Pearson")
        + "</td></tr></table>"
    )

    current = front_page_source("11517", body, date(2026, 9, 14)).capture(include_image=False)
    assert "Open Pearson" in current.transcript
    assert "Chapter 0 homework" not in current.transcript
    assert "1.2 Pearson" not in current.transcript
    assert "September 7" not in current.transcript

    nested = front_page_source("11517", body, date(2026, 8, 31)).capture(include_image=False)
    assert "Open Pearson" not in nested.transcript
    # The nested table's rows are captured once, not again as rows of the outer table.
    assert [block.text for block in nested.blocks].count("1.2 Pearson") == 1


def test_narrative_first_row_is_not_a_column_header_and_list_items_stay_separate():
    body = physics_agenda_table(
        "August 17",
        "<ul><li>Submit : Unit 1 Assignment 1</li><li>Unit 1 Assignment 2</li></ul>",
        "Classwork: Unit 1 Assignment 2",
    )
    capture = front_page_source("11126", body, TARGET_WEEK).capture(include_image=False)

    assert capture.blocks[0].role == BlockRole.HEADER
    monday = next(
        block
        for block in capture.blocks
        if block.row_label == "M" and block.role != BlockRole.DAY
    )
    assert monday.role == BlockRole.ASSIGNMENTS
    assert monday.text == "Monday\nSubmit : Unit 1 Assignment 1\nUnit 1 Assignment 2"


def test_incidental_date_in_an_older_week_table_does_not_claim_the_next_week():
    body = physics_agenda_table(
        "August 17",
        "Classwork: Unit 1 FRQ Exam",
        "Make up exams must be completed by Wednesday August 26",
    )

    with pytest.raises(CanvasAgendaNotFound, match="No sufficiently specific Canvas agenda"):
        front_page_source("11126", body, date(2026, 8, 24)).capture(include_image=False)


def test_course_can_use_canvas_without_any_fallback_source():
    course = CourseSettings.model_validate(
        {
            "name": "Canvas only",
            "prefix": "CANVAS",
            "task_list": "School",
            "canvas_course_id": "12506",
            "source": {"type": "none", "extraction": {"mode": "text"}},
        }
    )
    assert course.source.type == "none"
    assert course.source.extraction.mode.value == "text"


def _stub_capture(source_type: str) -> SourceCapture:
    return SourceCapture(
        source_key=f"{source_type}:fixture",
        source_url="https://example.test/agenda",
        source_type=source_type,
        page_hash="a" * 64,
        transcript="Agenda",
        blocks=[AgendaBlock(anchor="fixture:1", element_id="1", kind="text", text="Agenda")],
    )


def test_api_first_source_does_not_initialize_fallback_when_canvas_is_sufficient():
    created: list[str] = []

    class Primary:
        def capture(self, *, include_image):
            del include_image
            return _stub_capture("canvas")

    source = CourseAgendaSource(
        lambda: Primary(),
        lambda: created.append("fallback") or None,
        AcquisitionStrategy.AUTO,
    )

    assert source.capture(include_image=False).source_type == "canvas"
    assert created == []


def test_api_first_source_uses_configured_fallback_only_after_canvas_is_insufficient():
    class Primary:
        def capture(self, *, include_image):
            del include_image
            raise CanvasAgendaNotFound("No matching week")

    class Fallback:
        def capture(self, *, include_image):
            del include_image
            return _stub_capture("google_slides")

    capture = CourseAgendaSource(
        lambda: Primary(),
        lambda: Fallback(),
        AcquisitionStrategy.AUTO,
    ).capture(include_image=False)

    assert capture.source_type == "google_slides"
    assert capture.source_metadata["acquisition_fallback"]["from"] == "canvas_api"
