from __future__ import annotations

from datetime import date

from canvas_task_sync.identity import (
    CarryoverMatch,
    carryover_matches,
    initial_logical_id,
    resolve_logical_ids,
)
from canvas_task_sync.models import (
    ActionKind,
    DraftTask,
    StateRecord,
    TaskClassification,
    TaskType,
)


def _draft(
    *,
    anchor="table:agenda_table:r2:c2",
    ordinal=0,
    source_text="Completar actividad de la clase",
    title="[SPANISH] Complete class activity",
    fingerprint="fingerprint-a",
    due=date(2026, 8, 12),
):
    return DraftTask(
        course_id="spanish",
        source_key="google_slides:fixture:page",
        source_url="https://example.invalid/source",
        source_anchor=anchor,
        source_text=source_text,
        ordinal=ordinal,
        title=title,
        details=source_text,
        classification=TaskClassification.HOMEWORK,
        action_kind=ActionKind.COMPLETE,
        due_date=due,
        due_basis="next class",
        fingerprint=fingerprint,
    )


def _record(draft, logical_id):
    return StateRecord(
        logical_id=logical_id,
        course_id=draft.course_id,
        source_key=draft.source_key,
        anchor=draft.source_anchor,
        ordinal=draft.ordinal,
        fingerprint=draft.fingerprint,
        source_text=draft.source_text,
        title=draft.title,
        due_date=draft.due_date.isoformat() if draft.due_date else None,
    )


def test_initial_identity_is_coordinate_based_not_wording_or_deadline():
    original = _draft()
    edited = _draft(source_text="Completely rewritten", due=date(2026, 8, 13))
    assert initial_logical_id(original) == initial_logical_id(edited)


def test_wording_and_deadline_edits_retain_stored_identity():
    original = _draft()
    record = _record(original, "durable-id")
    edited = _draft(
        source_text="Terminar toda la actividad de clase",
        title="[SPANISH] Finish class activity",
        fingerprint="new-fingerprint",
        due=date(2026, 8, 13),
    )
    assert resolve_logical_ids([edited], [record]) == {0: "durable-id"}


def test_reordered_actions_match_unique_fingerprints():
    first = _draft(ordinal=0, source_text="Practice VHL", fingerprint="one")
    second = _draft(ordinal=1, source_text="Bring money", fingerprint="two")
    records = [_record(first, "id-one"), _record(second, "id-two")]
    reordered = [
        _draft(ordinal=0, source_text="Bring money", fingerprint="two"),
        _draft(ordinal=1, source_text="Practice VHL", fingerprint="one"),
    ]
    assert resolve_logical_ids(reordered, records) == {0: "id-two", 1: "id-one"}


def test_inserted_action_does_not_alias_a_matched_record_at_its_old_ordinal():
    original = _draft(
        ordinal=0,
        source_text="Sign up for AP Classroom",
        fingerprint="signup",
    )
    original_id = initial_logical_id(original)
    inserted = _draft(
        ordinal=0,
        source_text="Unit 1 Assignment 3 check-in",
        fingerprint="assignment-3",
    )
    moved_original = _draft(
        ordinal=1,
        source_text="Sign up for AP Classroom",
        fingerprint="signup",
    )

    resolved = resolve_logical_ids(
        [inserted, moved_original],
        [_record(original, original_id)],
    )

    assert resolved[1] == original_id
    assert resolved[0] != original_id
    assert len(set(resolved.values())) == 2


def test_split_assessment_reuses_legacy_combined_task_for_first_section():
    combined = _draft(
        source_text="Unit 1 Exam on September 8 and September 9",
        title="[PHYSICS] Unit 1 Exam",
        fingerprint="combined",
    )
    combined_id = initial_logical_id(combined)
    frq = _draft(
        ordinal=0,
        source_text="Tuesday September 8: FRQ Section",
        title="[PHYSICS] Unit 1 FRQ Exam",
        fingerprint="frq",
        due=date(2026, 9, 8),
    ).model_copy(update={"task_type": TaskType.TEST})
    mcq = _draft(
        ordinal=1,
        source_text="Wednesday September 9: MCQ Section",
        title="[PHYSICS] Unit 1 MCQ Exam",
        fingerprint="mcq",
        due=date(2026, 9, 9),
    ).model_copy(update={"task_type": TaskType.TEST})

    resolved = resolve_logical_ids(
        [frq, mcq],
        [_record(combined, combined_id)],
    )

    assert resolved[0] == combined_id
    assert resolved[1] != combined_id


def test_unique_table_row_move_retains_identity():
    original = _draft(anchor="table:agenda_table:r2:c2")
    moved = _draft(anchor="table:agenda_table:r7:c2")
    assert resolve_logical_ids([moved], [_record(original, "moved-id")]) == {0: "moved-id"}


def test_rescheduled_exam_sections_keep_their_identity():
    anchor = "canvas:physics-agenda:1"
    frq = _draft(anchor=anchor, ordinal=0, source_text="September 28 FRQ section",
                 title="[PHYSICS] Unit 2 FRQ Exam", fingerprint="frq-28")
    mcq = _draft(anchor=anchor, ordinal=1, source_text="September 29 MCQ section",
                 title="[PHYSICS] Unit 2 MCQ Exam", fingerprint="mcq-29")
    moved = [
        _draft(anchor=anchor, ordinal=0, source_text="October 5 FRQ section",
               title="[PHYSICS] Unit 2 FRQ Exam", fingerprint="frq-5"),
        _draft(anchor=anchor, ordinal=1, source_text="October 6 MCQ section",
               title="[PHYSICS] Unit 2 MCQ Exam", fingerprint="mcq-6"),
    ]

    resolved = resolve_logical_ids(moved, [_record(frq, "frq-id"), _record(mcq, "mcq-id")])

    assert resolved == {0: "frq-id", 1: "mcq-id"}


def test_replaced_row_content_is_not_paired_by_position():
    # Pairing these would rename, and pass the completion of, an unrelated task.
    original = _draft(source_text="Unit 1 Assignment 3 (U1A3)",
                      title="[PHYSICS] Unit 1 Assignment 3", fingerprint="u1a3")
    renumbered = _draft(source_text="Submit Unit 1 Assignment 4",
                        title="[PHYSICS] Unit 1 Assignment 4", fingerprint="u1a4")
    unrelated = _draft(source_text="Make sure you bring a book to class tomorrow",
                       title="[ENGLISH] Bring a book", fingerprint="book")
    survey = _draft(source_text="Complete Book Love reading survey for start of class",
                    title="[ENGLISH] Book Love survey", fingerprint="survey")

    assert resolve_logical_ids([renumbered], [_record(original, "u1a3-id")])[0] != "u1a3-id"
    assert resolve_logical_ids([unrelated], [_record(survey, "survey-id")])[0] != "survey-id"


def test_item_keeps_identity_when_order_based_anchors_shift():
    original = _draft(anchor="canvas:week:12",
                      source_text="Finish annotating prompt and outlining response for tomorrow.",
                      title="[ENGLISH] Annotate prompt", fingerprint="annotate")
    shifted = original.model_copy(update={"source_anchor": "canvas:week:13"})

    assert resolve_logical_ids([shifted], [_record(original, "annotate-id")]) == {
        0: "annotate-id"
    }


def test_carryover_matches_the_same_item_despite_wording_but_not_other_numbers():
    last_week = _record(
        _draft(source_text="Unit 1 Assignment 2 (U1A2)",
               title="[PHYSICS] Unit 1 Assignment 2 (U1A2)"),
        "u1a2",
    )
    submitted = _draft(source_text="Submit Unit 1 Assignment 2 (U1A2)",
                       title="[PHYSICS] Submit Unit 1 Assignment 2 (U1A2)")
    exam = _record(
        _draft(source_text="October 5 FRQ section", title="[PHYSICS] Unit 2 FRQ Exam")
        .model_copy(update={"task_type": TaskType.TEST}),
        "exam",
    )
    moved_exam = _draft(
        source_text="October 6 FRQ section", title="[PHYSICS] Unit 2 FRQ Section Exam"
    ).model_copy(update={"task_type": TaskType.TEST})
    pearson = _record(_draft(source_text="1.4 Pearson", title="[LINALG] Pearson"), "p14")
    next_pearson = _draft(source_text="1.5 Pearson", title="[LINALG] Pearson")
    paragraph = _record(
        _draft(source_text="Complete paragraph for Monday", title="[ENGLISH] Paragraph"),
        "para",
    )
    other_paragraph = _draft(source_text="Outline a new prompt response",
                             title="[ENGLISH] Paragraph")

    assert carryover_matches(submitted, [last_week]) == [
        CarryoverMatch(last_week, reschedulable=True)
    ]
    assert carryover_matches(moved_exam, [exam]) == [CarryoverMatch(exam, reschedulable=True)]
    assert carryover_matches(next_pearson, [pearson]) == []
    assert carryover_matches(other_paragraph, [paragraph]) == []


def test_only_restated_dates_let_a_generic_title_follow_a_reschedule():
    novel = _record(
        _draft(source_text="Frankenstein due Oct. 8.", title="[ENGLISH] Read Frankenstein"),
        "novel",
    )
    moved_novel = _draft(source_text="Frankenstein due Oct. 13.",
                         title="[ENGLISH] Read Frankenstein")
    practice = _record(
        _draft(source_text="Completar actividades de práctica - VHL",
               title="[SPANISH] VHL practice"),
        "vhl",
    )
    next_practice = _draft(source_text="Completar actividades de práctica - VHL",
                           title="[SPANISH] VHL practice")

    assert carryover_matches(moved_novel, [novel]) == [
        CarryoverMatch(novel, reschedulable=True)
    ]
    # Weekly practice recurs: it matches only a task on the same date (planner's rule).
    assert carryover_matches(next_practice, [practice]) == [
        CarryoverMatch(practice, reschedulable=False)
    ]


def test_ambiguous_duplicate_row_move_does_not_guess():
    one = _draft(anchor="table:agenda_table:r2:c2", source_text="VHL practice")
    two = _draft(anchor="table:agenda_table:r5:c2", source_text="VHL practice")
    moved = _draft(anchor="table:agenda_table:r7:c2", source_text="VHL practice")
    resolved = resolve_logical_ids(
        [moved],
        [_record(one, "first-vhl"), _record(two, "second-vhl")],
    )
    assert resolved[0] not in {"first-vhl", "second-vhl"}
