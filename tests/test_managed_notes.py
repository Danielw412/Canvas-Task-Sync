from __future__ import annotations

from datetime import date

from canvas_task_sync.managed_notes import (
    GENERATED_DESCRIPTION_END,
    GENERATED_DESCRIPTION_START,
    build_managed_block,
    compose_task_notes,
    merge_notes,
    parse_notes,
    set_due_date_uncertain_note,
    strip_managed_block,
)
from canvas_task_sync.models import ActionKind, DraftTask, TaskClassification


def _draft(due=date(2026, 8, 12), *, assignment_url=None):
    return DraftTask(
        course_id="spanish",
        source_key="google_slides:fixture:page",
        source_url="https://example.invalid/source",
        assignment_url=assignment_url,
        source_anchor="table:agenda:r2:c2",
        source_text="Completar actividad de la clase",
        title="[SPANISH] Complete class activity",
        details="Pages 10-12",
        classification=TaskClassification.HOMEWORK,
        action_kind=ActionKind.COMPLETE,
        due_date=due,
        due_basis="Assignments-column work due next configured class",
        fingerprint="fingerprint",
    )


def test_marker_contains_recovery_identity_and_exact_evidence():
    block = build_managed_block(_draft(), "logical-id")
    parsed = parse_notes(block)
    assert parsed.metadata["logical_id"] == "logical-id"
    assert parsed.metadata["source_anchor"] == "table:agenda:r2:c2"
    assert parsed.metadata["source_text"] == "Completar actividad de la clase"
    assert "Pages 10-12" in block
    assert "Exact source evidence:\nCompletar actividad de la clase" in block


def test_replacing_marker_preserves_user_notes_outside_block():
    old = build_managed_block(_draft(), "logical-id")
    new = build_managed_block(_draft(date(2026, 8, 13)), "logical-id")
    existing = f"Remember to ask the teacher.\n\n{old}\n\nBring a blue pen."

    merged = merge_notes(existing, new)

    assert merged.count("canvas-task-sync managed v1") == 1
    assert "Remember to ask the teacher." in merged
    assert "Bring a blue pen." in merged
    assert "Due: 2026-08-13" in merged
    assert "Due: 2026-08-12" not in merged


def test_replacing_marker_preserves_outside_bytes_and_position():
    old = build_managed_block(_draft(), "logical-id")
    new = build_managed_block(_draft(date(2026, 8, 13)), "logical-id")
    prefix = "  exact prefix spacing\n\n"
    suffix = "\n\nexact suffix spacing  "
    existing = f"{prefix}{old}{suffix}"
    assert merge_notes(existing, new) == f"{prefix}{new}{suffix}"


def test_legacy_block_can_be_removed_without_changing_user_note_text():
    block = build_managed_block(_draft(), "logical-id")
    assert strip_managed_block(merge_notes("My exact note", block)) == "My exact note"
    assert strip_managed_block(block) == ""


def test_due_date_uncertain_note_is_exact_and_preserves_user_notes():
    marked = set_due_date_uncertain_note("Keep my note exactly.", uncertain=True)
    assert marked == "Keep my note exactly.\n\nDue date uncertain"
    assert set_due_date_uncertain_note(marked, uncertain=False) == "Keep my note exactly."
    assert set_due_date_uncertain_note(marked, uncertain=True) == marked


def test_generated_description_contains_only_current_details():
    original = _draft(assignment_url="https://canvas.example/courses/7/assignments/10")
    notes = compose_task_notes("Ask about question 4.", original)
    assert notes == "Pages 10-12"

    changed = original.model_copy(
        update={
            "details": "Complete only the odd-numbered problems.",
            "assignment_url": "https://canvas.example/courses/7/assignments/11",
        }
    )
    refreshed = compose_task_notes(notes, changed)
    assert refreshed == "Complete only the odd-numbered problems."


def test_generated_description_replaces_legacy_notes_and_keeps_due_warning():
    draft = _draft(assignment_url="https://canvas.example/courses/7/assignments/10")
    draft.due_uncertain = True
    legacy = merge_notes("Keep exactly.", build_managed_block(draft, "logical-id"))

    notes = compose_task_notes(legacy, draft)

    assert notes == "Pages 10-12\n\nDue date uncertain"
    assert compose_task_notes(notes, draft) == notes


def test_existing_generated_wrapper_is_replaced_by_plain_description():
    wrapped = "\n".join(
        [
            GENERATED_DESCRIPTION_START,
            "Old description",
            GENERATED_DESCRIPTION_END,
        ]
    )

    assert compose_task_notes(wrapped, _draft()) == "Pages 10-12"
