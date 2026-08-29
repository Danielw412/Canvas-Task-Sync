from __future__ import annotations

from datetime import date

from canvas_task_sync.gemini import GeminiExtractor
from canvas_task_sync.models import (
    ActionKind,
    AgendaBlock,
    BlockRole,
    Confidence,
    DueRelation,
    ExtractedTask,
    SourceCapture,
    TaskClassification,
    TaskType,
)
from canvas_task_sync.scheduling import (
    build_draft_tasks,
    find_agenda_range,
    row_date_ranges,
)


class _Backend:
    def __init__(self, candidates):
        self.candidates = candidates

    def generate(self, **_):
        return self.candidates


def _extracted(spanish_capture, spanish_course, spanish_candidates):
    return GeminiExtractor(_Backend(spanish_candidates)).extract(
        spanish_capture, spanish_course
    ).tasks


def test_week_range_and_compound_row_end_are_deterministic(spanish_capture):
    assert find_agenda_range(spanish_capture) == (date(2026, 5, 25), date(2026, 5, 29))
    dates = row_date_ranges(spanish_capture)
    assert dates[("agenda_table", 3)] == (date(2026, 5, 27), date(2026, 5, 28))
    assert dates[("agenda_table", 5)] == (date(2026, 6, 1), date(2026, 6, 1))


def test_spanish_fixture_rebases_to_expected_deadlines(
    spanish_capture, spanish_course, spanish_candidates
):
    drafts, uncertain, ignored = build_draft_tasks(
        course_id="spanish",
        course=spanish_course,
        capture=spanish_capture,
        tasks=_extracted(spanish_capture, spanish_course, spanish_candidates),
        today=date(2026, 8, 11),
        rebase_week=date(2026, 8, 10),
    )

    assert not uncertain
    assert not ignored
    assert [(draft.title, draft.due_date) for draft in drafts] == [
        ("[SPANISH] Complete class activity", date(2026, 8, 12)),
        ("[SPANISH] VHL practice", date(2026, 8, 14)),
        ("[SPANISH] Present hypothetical conversations", date(2026, 8, 14)),
        ("[SPANISH] Submit class activity", date(2026, 8, 14)),
        ("[SPANISH] VHL practice", date(2026, 8, 18)),
        ("[SPANISH] Bring money", date(2026, 8, 17)),
    ]
    assert all(not draft.historical for draft in drafts)
    monday_tasks = [draft for draft in drafts if draft.source_anchor.endswith("r5:c2")]
    assert [draft.ordinal for draft in monday_tasks] == [0, 1]


def test_application_policy_overrides_gemini_for_same_day_actions(
    spanish_capture, spanish_course, spanish_candidates
):
    drafts, _, _ = build_draft_tasks(
        course_id="spanish",
        course=spanish_course,
        capture=spanish_capture,
        tasks=_extracted(spanish_capture, spanish_course, spanish_candidates),
        today=date(2026, 5, 1),
    )
    by_title = {draft.title: draft for draft in drafts}
    assert by_title["[SPANISH] Present hypothetical conversations"].due_date == date(
        2026, 5, 29
    )
    assert by_title["[SPANISH] Submit class activity"].due_date == date(2026, 5, 29)
    assert by_title["[SPANISH] Bring money"].due_date == date(2026, 6, 1)


def test_assessment_uses_explicit_date_then_dated_row_and_formats_title(
    spanish_capture, spanish_course
):
    explicit = ExtractedTask(
        source_anchor="table:agenda_table:r3:c2",
        source_text="Crime and Punishment Exam May 29, 2026",
        row_label="W/Th",
        classification=TaskClassification.HOMEWORK,
        task_type=TaskType.TEST,
        action_kind=ActionKind.OTHER,
        title_stem="Crime and Punishment Exam",
        due_relation=DueRelation.EXPLICIT_DATE,
        explicit_due_date="2026-05-29",
        confidence=Confidence.HIGH,
    )
    row_quiz = ExtractedTask(
        source_anchor="table:agenda_table:r3:c2",
        source_text="Vocabulary",
        row_label="W/Th",
        classification=TaskClassification.HOMEWORK,
        task_type=TaskType.QUIZ,
        action_kind=ActionKind.OTHER,
        title_stem="Vocabulary",
        confidence=Confidence.HIGH,
    )
    drafts, uncertain, _ = build_draft_tasks(
        course_id="spanish",
        course=spanish_course,
        capture=spanish_capture,
        tasks=[explicit, row_quiz],
        today=date(2026, 5, 1),
    )
    assert not uncertain
    assert [(task.title, task.due_date, task.task_type) for task in drafts] == [
        ("[SPANISH] Crime and Punishment Exam", date(2026, 5, 29), TaskType.TEST),
        ("[SPANISH] Vocabulary Quiz", date(2026, 5, 28), TaskType.QUIZ),
    ]


def test_numeric_explicit_date_takes_precedence_over_weekday_in_evidence(
    spanish_capture, spanish_course
):
    task = ExtractedTask(
        source_anchor="table:agenda_table:r2:c2",
        source_text="Exam will be on Monday, 8/31.",
        row_label="T",
        classification=TaskClassification.CLASSWORK,
        task_type=TaskType.TEST,
        action_kind=ActionKind.COMPLETE,
        title_stem="Crime and Punishment Exam",
        due_relation=DueRelation.EXPLICIT_DATE,
        explicit_due_date="Monday, 8/31",
        confidence=Confidence.HIGH,
    )

    drafts, uncertain, _ = build_draft_tasks(
        course_id="english",
        course=spanish_course,
        capture=spanish_capture,
        tasks=[task],
        today=date(2026, 5, 1),
    )

    assert not uncertain
    assert drafts[0].due_date == date(2026, 8, 31)
    assert drafts[0].due_basis == "Explicit date stated in source evidence"


def test_header_numeric_date_uses_selected_agenda_week_for_year_context(
    spanish_capture, spanish_course
):
    header = spanish_capture.blocks[0]
    task = ExtractedTask(
        source_anchor=header.anchor,
        source_text="Exam will be on Monday, 8/31.",
        classification=TaskClassification.HOMEWORK,
        task_type=TaskType.TEST,
        action_kind=ActionKind.OTHER,
        title_stem="Crime and Punishment Exam",
        due_relation=DueRelation.EXPLICIT_DATE,
        explicit_due_date="2026-08-31",
        confidence=Confidence.HIGH,
    )

    drafts, uncertain, _ = build_draft_tasks(
        course_id="english",
        course=spanish_course,
        capture=spanish_capture,
        tasks=[task],
        today=date(2026, 5, 1),
    )

    assert not uncertain
    assert drafts[0].due_date == date(2026, 8, 31)
    assert drafts[0].due_uncertain is False


def test_multi_day_exam_is_split_into_one_dated_task_per_section(
    spanish_capture, spanish_course
):
    header = spanish_capture.blocks[0]
    task = ExtractedTask(
        source_anchor=header.anchor,
        source_text=(
            "Unit 1 Exam: Tuesday September 8: Free Response Question (FRQ) Section "
            "Wednesday September 9: Multiple Choice Question (MCQ) Section"
        ),
        classification=TaskClassification.HOMEWORK,
        task_type=TaskType.TEST,
        action_kind=ActionKind.OTHER,
        title_stem="Unit 1 Exam",
        due_relation=DueRelation.EXPLICIT_DATE,
        explicit_due_date="September 8 and September 9",
        confidence=Confidence.HIGH,
    )

    drafts, uncertain, _ = build_draft_tasks(
        course_id="physics",
        course=spanish_course,
        capture=spanish_capture,
        tasks=[task],
        today=date(2026, 5, 1),
    )

    assert not uncertain
    assert sorted((draft.title, draft.due_date) for draft in drafts) == [
        ("[SPANISH] Unit 1 FRQ Exam", date(2026, 9, 8)),
        ("[SPANISH] Unit 1 MCQ Exam", date(2026, 9, 9)),
    ]
    assert all(not draft.due_uncertain for draft in drafts)


def test_undated_assessment_without_a_dated_row_is_imported_as_due_uncertain(
    spanish_capture, spanish_course
):
    header = spanish_capture.blocks[0]
    assessment = ExtractedTask(
        source_anchor=header.anchor,
        source_text="Course final exam",
        classification=TaskClassification.HOMEWORK,
        task_type=TaskType.TEST,
        action_kind=ActionKind.OTHER,
        title_stem="Course Final",
        confidence=Confidence.HIGH,
    )
    drafts, uncertain, _ = build_draft_tasks(
        course_id="spanish",
        course=spanish_course,
        capture=spanish_capture,
        tasks=[assessment],
        today=date(2026, 5, 1),
    )
    assert len(drafts) == 1
    assert not uncertain
    assert drafts[0].due_date is None
    assert drafts[0].due_uncertain is True
    assert "dated agenda row" in drafts[0].due_uncertain_reason


def test_holidays_and_ordinary_in_class_activity_are_ignored(
    spanish_capture, spanish_course
):
    tasks = [
        ExtractedTask(
            source_anchor="table:agenda_table:r1:c1",
            source_text="Memorial Day",
            row_label="M",
            classification=TaskClassification.CLASSWORK,
            action_kind=ActionKind.OTHER,
            title_stem="Memorial Day",
            due_relation=DueRelation.NONE,
            confidence=Confidence.HIGH,
        ),
        ExtractedTask(
            source_anchor="table:agenda_table:r0:c1",
            source_text="Learning Activities / Learning Targets",
            classification=TaskClassification.CLASSWORK,
            action_kind=ActionKind.OTHER,
            title_stem="Learning targets",
            due_relation=DueRelation.NONE,
            confidence=Confidence.HIGH,
        ),
        ExtractedTask(
            source_anchor="table:agenda_table:r2:c1",
            source_text="La profesora explicará cómo se calificará la actividad",
            row_label="T",
            classification=TaskClassification.CLASSWORK,
            action_kind=ActionKind.OTHER,
            title_stem="Grading explanation",
            due_relation=DueRelation.NONE,
            confidence=Confidence.HIGH,
        ),
        ExtractedTask(
            source_anchor="table:agenda_table:r5:c1",
            source_text="Actividad de De Niño y repaso de gramática",
            row_label="M",
            classification=TaskClassification.CLASSWORK,
            action_kind=ActionKind.COMPLETE,
            title_stem="De Niño activity",
            due_relation=DueRelation.NONE,
            confidence=Confidence.HIGH,
        ),
    ]
    drafts, uncertain, ignored = build_draft_tasks(
        course_id="spanish",
        course=spanish_course,
        capture=spanish_capture,
        tasks=tasks,
        today=date(2026, 5, 1),
    )
    assert not drafts
    assert not uncertain
    assert {item.title for item in ignored} == {
        "Memorial Day",
        "De Niño activity",
        "Learning targets",
        "Grading explanation",
    }


def test_unsupported_explicit_date_is_imported_as_due_uncertain(
    spanish_capture, spanish_course
):
    task = ExtractedTask(
        source_anchor="table:agenda_table:r2:c2",
        source_text="Completar actividad de la clase",
        row_label="T",
        classification=TaskClassification.HOMEWORK,
        action_kind=ActionKind.COMPLETE,
        title_stem="Complete class activity",
        due_relation=DueRelation.EXPLICIT_DATE,
        explicit_due_date="2026-05-28",
        confidence=Confidence.HIGH,
    )
    drafts, uncertain, _ = build_draft_tasks(
        course_id="spanish",
        course=spanish_course,
        capture=spanish_capture,
        tasks=[task],
        today=date(2026, 5, 1),
    )
    assert len(drafts) == 1
    assert not uncertain
    assert drafts[0].due_date is None
    assert drafts[0].due_uncertain is True
    assert "not present" in drafts[0].due_uncertain_reason


def test_duplicate_gemini_candidates_are_collapsed_by_application_code(
    spanish_capture, spanish_course, spanish_candidates
):
    tasks = _extracted(spanish_capture, spanish_course, spanish_candidates)
    drafts, uncertain, ignored = build_draft_tasks(
        course_id="spanish",
        course=spanish_course,
        capture=spanish_capture,
        tasks=[tasks[0], tasks[0].model_copy(deep=True)],
        today=date(2026, 5, 1),
    )
    assert len(drafts) == 1
    assert not uncertain
    assert len(ignored) == 1
    assert "Duplicate extraction" in ignored[0].reason


def test_repeated_homework_is_due_after_latest_consecutive_occurrence(spanish_course):
    rows = [
        (1, "Monday", "No School"),
        (
            2,
            "Tuesday",
            "Read chapter 3 in the text book, and sign up for AP Classroom Using the Join Codes.",
        ),
        (
            3,
            "Wednesday",
            "Read chapter 3 in the text book, and sign up for AP Classroom Using the Join Codes",
        ),
        (
            4,
            "Thursday",
            "Read chapter 2 in the text book, and sign up for AP Classroom Using the Join Codes",
        ),
        (
            5,
            "Friday",
            "Read chapter 2 in the text book, and sign up for AP Classroom Using the Join Codes",
        ),
    ]
    blocks = [
        AgendaBlock(
            anchor="header",
            element_id="agenda",
            kind="heading",
            role=BlockRole.HEADER,
            text="August 17-21, 2026",
        )
    ]
    blocks.extend(
        AgendaBlock(
            anchor=f"table:agenda:r{row}:c2",
            element_id="agenda",
            kind="table_cell",
            role=BlockRole.ASSIGNMENTS,
            row_index=row,
            column_index=2,
            row_label=label,
            text=text,
            order=row,
        )
        for row, label, text in rows
    )
    capture = SourceCapture(
        source_key="canvas:physics:week:2026-08-17",
        source_url="https://canvas.example/physics",
        page_hash="fixture",
        transcript="\n".join(block.text for block in blocks),
        blocks=blocks,
    )
    tasks = [
        ExtractedTask(
            source_anchor="table:agenda:r2:c2",
            source_text="Read chapter 3 in the text book",
            row_label="Tuesday",
            classification=TaskClassification.HOMEWORK,
            action_kind=ActionKind.READ,
            title_stem="Read chapter 3",
            due_relation=DueRelation.NEXT_CLASS,
            confidence=Confidence.HIGH,
        ),
        ExtractedTask(
            source_anchor="table:agenda:r2:c2",
            source_text="sign up for AP Classroom Using the Join Codes",
            row_label="Tuesday",
            classification=TaskClassification.HOMEWORK,
            action_kind=ActionKind.OTHER,
            title_stem="Sign up for AP Classroom",
            due_relation=DueRelation.NEXT_CLASS,
            confidence=Confidence.HIGH,
        ),
        ExtractedTask(
            source_anchor="table:agenda:r4:c2",
            source_text="Read chapter 2 in the text book",
            row_label="Thursday",
            classification=TaskClassification.HOMEWORK,
            action_kind=ActionKind.READ,
            title_stem="Read chapter 2",
            due_relation=DueRelation.NEXT_CLASS,
            confidence=Confidence.HIGH,
        ),
    ]

    drafts, uncertain, ignored = build_draft_tasks(
        course_id="physics",
        course=spanish_course,
        capture=capture,
        tasks=tasks,
        today=date(2026, 8, 17),
    )

    assert not uncertain
    assert not ignored
    assert [(draft.title, draft.source_date, draft.due_date) for draft in drafts] == [
        ("[SPANISH] Read chapter 3", date(2026, 8, 19), date(2026, 8, 20)),
        ("[SPANISH] Sign up for AP Classroom", date(2026, 8, 21), date(2026, 8, 24)),
        ("[SPANISH] Read chapter 2", date(2026, 8, 21), date(2026, 8, 24)),
    ]


def test_repeated_candidates_collapse_to_latest_scheduled_occurrence(spanish_course):
    blocks = [
        AgendaBlock(
            anchor="header",
            element_id="agenda",
            kind="heading",
            role=BlockRole.HEADER,
            text="August 17-21, 2026",
        ),
        AgendaBlock(
            anchor="table:agenda:r2:c2",
            element_id="agenda",
            kind="table_cell",
            role=BlockRole.ASSIGNMENTS,
            row_index=2,
            column_index=2,
            row_label="Tuesday",
            text="Read chapter 3 in the text book",
        ),
        AgendaBlock(
            anchor="table:agenda:r3:c2",
            element_id="agenda",
            kind="table_cell",
            role=BlockRole.ASSIGNMENTS,
            row_index=3,
            column_index=2,
            row_label="Wednesday",
            text="Read chapter 3 in the text book",
        ),
    ]
    capture = SourceCapture(
        source_key="canvas:physics:week:2026-08-17",
        source_url="https://canvas.example/physics",
        page_hash="fixture",
        transcript="\n".join(block.text for block in blocks),
        blocks=blocks,
    )
    tasks = [
        ExtractedTask(
            source_anchor=anchor,
            source_text="Read chapter 3 in the text book",
            row_label=label,
            classification=TaskClassification.HOMEWORK,
            action_kind=ActionKind.READ,
            title_stem="Read chapter 3",
            due_relation=DueRelation.NEXT_CLASS,
            confidence=Confidence.HIGH,
        )
        for anchor, label in (
            ("table:agenda:r2:c2", "Tuesday"),
            ("table:agenda:r3:c2", "Wednesday"),
        )
    ]

    drafts, uncertain, ignored = build_draft_tasks(
        course_id="physics",
        course=spanish_course,
        capture=capture,
        tasks=tasks,
        today=date(2026, 8, 17),
    )

    assert not uncertain
    assert len(drafts) == 1
    assert drafts[0].source_date == date(2026, 8, 19)
    assert drafts[0].due_date == date(2026, 8, 20)
    assert len(ignored) == 1
    assert "latest consecutive agenda row" in ignored[0].reason


def test_statistics_agenda_classwork_default_due_and_explicit_thursday(
    spanish_capture, spanish_course
):
    replacements = {
        "table:agenda_table:r2:c1": (
            "Practice identifying null and alternative hypotheses",
            "T",
        ),
        "table:agenda_table:r2:c2": (
            "Complete Unit 6 AP Classroom practice problems",
            "T",
        ),
        "table:agenda_table:r3:c0": ("W", "W"),
        "table:agenda_table:r3:c1": ("Work through released AP FRQs", "W"),
        "table:agenda_table:r3:c2": (
            "Complete 2022 AP FRQ #4\nBring completed FRQ to class Thursday",
            "W",
        ),
    }
    blocks = [
        block.model_copy(
            update={
                "text": replacements[block.anchor][0],
                "row_label": replacements[block.anchor][1],
            }
        )
        if block.anchor in replacements
        else block
        for block in spanish_capture.blocks
    ]
    capture = spanish_capture.model_copy(update={"blocks": blocks})
    tasks = [
        ExtractedTask(
            source_anchor="table:agenda_table:r2:c1",
            source_text="Practice identifying null and alternative hypotheses",
            row_label="T",
            classification=TaskClassification.HOMEWORK,
            action_kind=ActionKind.COMPLETE,
            title_stem="Practice identifying hypotheses",
            due_relation=DueRelation.NEXT_CLASS,
            confidence=Confidence.HIGH,
        ),
        ExtractedTask(
            source_anchor="table:agenda_table:r2:c2",
            source_text="Complete Unit 6 AP Classroom practice problems",
            row_label="T",
            classification=TaskClassification.HOMEWORK,
            action_kind=ActionKind.COMPLETE,
            title_stem="Complete Unit 6 practice",
            due_relation=DueRelation.NONE,
            confidence=Confidence.HIGH,
        ),
        ExtractedTask(
            source_anchor="table:agenda_table:r3:c1",
            source_text="Work through released AP FRQs",
            row_label="W",
            classification=TaskClassification.HOMEWORK,
            action_kind=ActionKind.COMPLETE,
            title_stem="Work through released AP FRQs",
            due_relation=DueRelation.NEXT_CLASS,
            confidence=Confidence.HIGH,
        ),
        ExtractedTask(
            source_anchor="table:agenda_table:r3:c2",
            source_text="Bring completed FRQ to class Thursday",
            row_label="W",
            classification=TaskClassification.HOMEWORK,
            action_kind=ActionKind.BRING,
            title_stem="Bring completed FRQ",
            due_relation=DueRelation.SAME_DAY,
            confidence=Confidence.HIGH,
        ),
    ]

    drafts, uncertain, ignored = build_draft_tasks(
        course_id="statistics",
        course=spanish_course,
        capture=capture,
        tasks=tasks,
        today=date(2026, 5, 1),
    )

    assert not uncertain
    assert [(item.title, item.due_date) for item in drafts] == [
        ("[SPANISH] Complete Unit 6 practice", date(2026, 5, 27)),
        ("[SPANISH] Bring completed FRQ", date(2026, 5, 28)),
    ]
    assert {item.title for item in ignored} == {
        "Practice identifying hypotheses",
        "Work through released AP FRQs",
    }
