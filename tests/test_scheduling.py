from __future__ import annotations

from datetime import date

from canvas_task_sync.gemini import GeminiExtractor
from canvas_task_sync.models import (
    ActionKind,
    Confidence,
    DueRelation,
    ExtractedTask,
    TaskClassification,
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


def test_explicit_date_is_rejected_without_exact_source_evidence(
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
    assert not drafts
    assert len(uncertain) == 1
    assert "not present" in uncertain[0].reason


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
