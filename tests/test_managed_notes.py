from __future__ import annotations

from datetime import date

from canvas_task_sync.managed_notes import build_managed_block, merge_notes, parse_notes
from canvas_task_sync.models import ActionKind, DraftTask, TaskClassification


def _draft(due=date(2026, 8, 12)):
    return DraftTask(
        course_id="spanish",
        source_key="google_slides:fixture:page",
        source_url="https://example.invalid/source",
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
