from __future__ import annotations

from datetime import date

import pytest

from canvas_task_sync.identity import initial_logical_id
from canvas_task_sync.managed_notes import build_managed_block, merge_notes
from canvas_task_sync.models import (
    ActionKind,
    DraftTask,
    ExtractionMode,
    RemoteTask,
    StateRecord,
    SyncActionKind,
    TaskClassification,
)
from canvas_task_sync.planner import SyncPlanner, apply_sync_plan
from canvas_task_sync.state import StateStore

SOURCE_KEY = "google_slides:fixture:page"


def _draft(*, due=date(2026, 8, 12), title="[SPANISH] Complete class activity"):
    return DraftTask(
        course_id="spanish",
        source_key=SOURCE_KEY,
        source_url="https://example.invalid/source",
        source_anchor="table:agenda:r2:c2",
        source_text="Completar actividad de la clase",
        title=title,
        details="Completar actividad de la clase",
        classification=TaskClassification.HOMEWORK,
        action_kind=ActionKind.COMPLETE,
        due_date=due,
        due_basis="next class",
        fingerprint="fingerprint",
    )


def _record(draft, logical_id, remote_id="remote-1"):
    return StateRecord(
        logical_id=logical_id,
        course_id="spanish",
        source_key=SOURCE_KEY,
        anchor=draft.source_anchor,
        ordinal=draft.ordinal,
        fingerprint=draft.fingerprint,
        source_text=draft.source_text,
        title=draft.title,
        due_date=draft.due_date.isoformat(),
        google_task_id=remote_id,
        tasklist_id="list-1",
    )


def _plan(drafts, *, records=None, remotes=None, include_past=False):
    return SyncPlanner().plan(
        course_id="spanish",
        source_key=SOURCE_KEY,
        task_list="School",
        extraction_mode=ExtractionMode.HYBRID,
        fallback_reasons=[],
        drafts=drafts,
        uncertain=[],
        ignored=[],
        state_records=records or [],
        remote_tasks=remotes or [],
        include_past=include_past,
        dry_run=True,
    )


def test_new_task_is_create_and_dry_run_does_not_call_writer():
    plan = _plan([_draft()])
    assert [action.kind for action in plan.actions] == [SyncActionKind.CREATE]
    with pytest.raises(RuntimeError, match="dry-run"):
        apply_sync_plan(
            plan,
            tasklist_id="list-1",
            tasks_client=object(),
            state=object(),
        )


def test_marker_recovery_without_sqlite_is_unchanged_and_preserves_completion():
    draft = _draft()
    logical_id = initial_logical_id(draft)
    marker = build_managed_block(draft, logical_id)
    remote = RemoteTask(
        id="remote-1",
        title=draft.title,
        notes=merge_notes("My own note", marker),
        due="2026-08-12T00:00:00.000Z",
        status="completed",
    )
    plan = _plan([draft], remotes=[remote])
    assert [action.kind for action in plan.actions] == [SyncActionKind.UNCHANGED]
    assert plan.actions[0].remote_task_id == "remote-1"


def test_deadline_edit_is_update_with_same_identity():
    original = _draft()
    record = _record(original, "durable-id")
    marker = build_managed_block(original, "durable-id")
    remote = RemoteTask(
        id="remote-1",
        title=original.title,
        notes=marker,
        due="2026-08-12T00:00:00.000Z",
    )
    edited = _draft(due=date(2026, 8, 13))
    plan = _plan([edited], records=[record], remotes=[remote])
    assert plan.actions[0].kind == SyncActionKind.UPDATE
    assert plan.actions[0].logical_id == "durable-id"
    assert "due date" in plan.actions[0].reason


def test_unmanaged_title_due_collision_is_uncertain_not_duplicate():
    draft = _draft()
    remote = RemoteTask(
        id="unmanaged",
        title=draft.title,
        notes="User-created task",
        due="2026-08-12T00:00:00.000Z",
    )
    plan = _plan([draft], remotes=[remote])
    assert [action.kind for action in plan.actions] == [SyncActionKind.UNCERTAIN]
    assert plan.actions[0].evidence == "Completar actividad de la clase"


def test_remote_deletion_and_source_disappearance_are_reported_without_recreation():
    draft = _draft()
    record = _record(draft, "durable-id", remote_id="deleted-id")
    deleted = RemoteTask(id="deleted-id", title=draft.title, deleted=True)
    missing_remote_plan = _plan([draft], records=[record], remotes=[deleted])
    assert missing_remote_plan.actions[0].kind == SyncActionKind.REMOTE_MISSING

    source_missing_plan = _plan([], records=[record], remotes=[])
    assert source_missing_plan.actions[0].kind == SyncActionKind.SOURCE_MISSING


def test_past_task_is_blocked_without_explicit_flag():
    historical = _draft(due=date(2020, 1, 1))
    historical.historical = True
    blocked = _plan([historical])
    allowed = _plan([historical], include_past=True)
    assert blocked.actions[0].kind == SyncActionKind.HISTORICAL_BLOCKED
    assert allowed.actions[0].kind == SyncActionKind.CREATE


def test_read_only_state_does_not_create_sqlite_file(tmp_path):
    path = tmp_path / "missing" / "state.sqlite3"
    with StateStore(path, writable=False) as state:
        assert state.records("spanish", SOURCE_KEY) == []
    assert not path.exists()
