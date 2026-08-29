from __future__ import annotations

from datetime import date

from canvas_task_sync.identity import initial_logical_id, resolve_logical_ids
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


def test_ambiguous_duplicate_row_move_does_not_guess():
    one = _draft(anchor="table:agenda_table:r2:c2", source_text="VHL practice")
    two = _draft(anchor="table:agenda_table:r5:c2", source_text="VHL practice")
    moved = _draft(anchor="table:agenda_table:r7:c2", source_text="VHL practice")
    resolved = resolve_logical_ids(
        [moved],
        [_record(one, "first-vhl"), _record(two, "second-vhl")],
    )
    assert resolved[0] not in {"first-vhl", "second-vhl"}
