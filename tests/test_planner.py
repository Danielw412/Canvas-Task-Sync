from __future__ import annotations

from datetime import date

import pytest

from canvas_task_sync.identity import initial_logical_id
from canvas_task_sync.managed_notes import (
    build_managed_block,
    compose_task_notes,
    merge_notes,
)
from canvas_task_sync.models import (
    ActionKind,
    DraftTask,
    ExtractionMode,
    RemoteTask,
    StateRecord,
    SyncActionKind,
    TaskClassification,
    TaskType,
)
from canvas_task_sync.planner import SyncPlanner, apply_sync_plan
from canvas_task_sync.state import StateStore

SOURCE_KEY = "google_slides:fixture:page"


def _draft(
    *,
    due=date(2026, 8, 12),
    title="[SPANISH] Complete class activity",
    task_type=TaskType.ASSIGNMENT,
    due_uncertain=False,
    due_uncertain_reason=None,
    assignment_url=None,
):
    return DraftTask(
        course_id="spanish",
        source_key=SOURCE_KEY,
        source_url="https://example.invalid/source",
        assignment_url=assignment_url,
        source_anchor="table:agenda:r2:c2",
        source_text="Completar actividad de la clase",
        title=title,
        details="Completar actividad de la clase",
        classification=TaskClassification.HOMEWORK,
        task_type=task_type,
        action_kind=ActionKind.COMPLETE,
        due_date=due,
        due_basis="next class",
        due_uncertain=due_uncertain,
        due_uncertain_reason=due_uncertain_reason,
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
        due_date=draft.due_date.isoformat() if draft.due_date else None,
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


def test_due_uncertain_task_is_created_without_date_and_with_exact_note(tmp_path):
    draft = _draft(
        due=None,
        due_uncertain=True,
        due_uncertain_reason="No dated agenda row.",
    )
    plan = _plan([draft]).model_copy(deep=True)
    plan.dry_run = False

    class Tasks:
        def __init__(self):
            self.created = None

        def list_tasks(self, _tasklist_id):
            return []

        def create_task(self, _tasklist_id, **payload):
            self.created = payload
            return RemoteTask(id="created", title=payload["title"], notes=payload["notes"])

        def verify_due(self, _tasklist_id, _task_id, due_date):
            assert due_date is None
            return RemoteTask(id="created", title=draft.title, notes="Due date uncertain")

    tasks = Tasks()
    with StateStore(tmp_path / "state.sqlite3", writable=True) as state:
        apply_sync_plan(plan, tasklist_id="list-1", tasks_client=tasks, state=state)

    assert tasks.created["due_date"] is None
    assert tasks.created["notes"] == (
        "Completar actividad de la clase\n\nDue date uncertain"
    )
    assert plan.actions[0].due_uncertain is True


def test_later_supported_date_updates_same_task_and_removes_uncertainty_note():
    original = _draft(
        due=None,
        due_uncertain=True,
        due_uncertain_reason="No dated agenda row.",
    )
    record = _record(original, "durable-id")
    remote = RemoteTask(
        id="remote-1",
        title=original.title,
        notes="Keep this note.\n\nDue date uncertain",
        due=None,
        tasklist_id="list-1",
        tasklist_title="School",
    )
    dated = _draft(due=date(2026, 8, 13))

    plan = _plan([dated], records=[record], remotes=[remote])

    assert plan.actions[0].kind == SyncActionKind.UPDATE
    assert plan.actions[0].logical_id == "durable-id"
    assert plan.actions[0].due_date == date(2026, 8, 13)
    assert plan.actions[0].due_uncertain is False
    assert plan.actions[0].replacement_notes == "Completar actividad de la clase"
    assert "Due date uncertain" not in plan.actions[0].replacement_notes


def test_marker_recovery_without_sqlite_plans_note_cleanup_and_preserves_completion():
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
    assert [action.kind for action in plan.actions] == [SyncActionKind.UPDATE]
    assert plan.actions[0].remote_task_id == "remote-1"
    assert plan.actions[0].replacement_notes == "Completar actividad de la clase"


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


def test_unmanaged_title_due_collision_is_ignored_without_duplicate_or_review():
    draft = _draft()
    remote = RemoteTask(
        id="unmanaged",
        title=draft.title,
        notes="User-created task",
        due="2026-08-12T00:00:00.000Z",
    )
    plan = _plan([draft], remotes=[remote])
    assert [action.kind for action in plan.actions] == [SyncActionKind.IGNORED]
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


def test_new_assessment_routes_to_tests_but_existing_mapping_stays_in_school():
    assessment = _draft(
        title="[SPANISH] Preterite Quiz",
        task_type=TaskType.QUIZ,
    )
    new_plan = SyncPlanner().plan(
        course_id="spanish",
        source_key=SOURCE_KEY,
        task_list="School",
        assessment_task_list="Tests",
        tasklist_ids={"School": "list-1", "Tests": "list-2"},
        extraction_mode=ExtractionMode.TEXT,
        fallback_reasons=[],
        drafts=[assessment],
        uncertain=[],
        ignored=[],
        state_records=[],
        remote_tasks=[],
        include_past=True,
        dry_run=True,
    )
    assert new_plan.actions[0].task_list == "Tests"

    logical_id = initial_logical_id(assessment)
    record = _record(assessment, logical_id)
    remote = RemoteTask(
        id="remote-1",
        title=assessment.title,
        notes=compose_task_notes("", assessment),
        due="2026-08-12T00:00:00.000Z",
        tasklist_id="list-1",
        tasklist_title="School",
    )
    existing_plan = SyncPlanner().plan(
        course_id="spanish",
        source_key=SOURCE_KEY,
        task_list="School",
        assessment_task_list="Tests",
        tasklist_ids={"School": "list-1", "Tests": "list-2"},
        extraction_mode=ExtractionMode.TEXT,
        fallback_reasons=[],
        drafts=[assessment],
        uncertain=[],
        ignored=[],
        state_records=[record],
        remote_tasks=[remote],
        include_past=True,
        dry_run=True,
    )
    assert existing_plan.actions[0].task_list == "School"
    assert existing_plan.actions[0].kind == SyncActionKind.UNCHANGED


def test_description_update_replaces_notes_without_internal_text():
    original = _draft(assignment_url="https://canvas.example/courses/7/assignments/10")
    record = _record(original, "durable-id")
    remote = RemoteTask(
        id="remote-1",
        title=original.title,
        notes=compose_task_notes("My manual note.", original),
        due="2026-08-12T00:00:00.000Z",
        tasklist_id="list-1",
        tasklist_title="School",
    )
    changed = original.model_copy(
        update={
            "details": "Complete the revised activity.",
            "assignment_url": "https://canvas.example/courses/7/assignments/11",
        }
    )

    plan = _plan([changed], records=[record], remotes=[remote])

    assert plan.actions[0].kind == SyncActionKind.UPDATE
    assert plan.actions[0].replacement_notes == "Complete the revised activity."


def test_same_class_collision_in_other_list_prevents_duplicate():
    draft = _draft(title="[SPANISH] Preterite Quiz", task_type=TaskType.QUIZ)
    collision = RemoteTask(
        id="existing-test",
        title=draft.title,
        due="2026-08-12T00:00:00.000Z",
        tasklist_id="list-2",
        tasklist_title="Tests",
    )
    plan = SyncPlanner().plan(
        course_id="spanish",
        source_key=SOURCE_KEY,
        task_list="School",
        assessment_task_list="Tests",
        tasklist_ids={"School": "list-1", "Tests": "list-2"},
        extraction_mode=ExtractionMode.TEXT,
        fallback_reasons=[],
        drafts=[draft],
        uncertain=[],
        ignored=[],
        state_records=[],
        remote_tasks=[collision],
        collision_remote_tasks=[collision],
        include_past=True,
        dry_run=True,
    )
    assert plan.actions[0].kind == SyncActionKind.IGNORED
    assert plan.actions[0].task_list == "Tests"


def test_read_only_state_does_not_create_sqlite_file(tmp_path):
    path = tmp_path / "missing" / "state.sqlite3"
    with StateStore(path, writable=False) as state:
        assert state.records("spanish", SOURCE_KEY) == []
    assert not path.exists()


def test_stale_legacy_marker_becomes_safe_notes_cleanup_with_local_recovery():
    draft = _draft()
    logical_id = initial_logical_id(draft)
    remote = RemoteTask(
        id="legacy",
        title=draft.title,
        notes=merge_notes("Keep this note", build_managed_block(draft, logical_id)),
        due="2026-08-12T00:00:00.000Z",
        tasklist_id="list-1",
        tasklist_title="School",
    )
    plan = SyncPlanner().plan(
        course_id="spanish",
        source_key=SOURCE_KEY,
        task_list="School",
        assessment_task_list="Tests",
        tasklist_ids={"School": "list-1", "Tests": "list-2"},
        extraction_mode=ExtractionMode.TEXT,
        fallback_reasons=[],
        drafts=[],
        uncertain=[],
        ignored=[],
        state_records=[],
        remote_tasks=[remote],
        include_past=True,
        dry_run=True,
        course_prefix="SPANISH",
    )
    cleanup = next(action for action in plan.actions if action.kind == SyncActionKind.NOTES_CLEANUP)
    assert cleanup.replacement_notes == "Keep this note"
    assert cleanup.recovered_state is not None
    assert cleanup.recovered_state.google_task_id == "legacy"


def test_malformed_legacy_marker_is_flagged_without_cleanup():
    remote = RemoteTask(
        id="broken",
        title="[SPANISH] Broken",
        notes="--- canvas-task-sync managed v1 ---\nnot valid",
        tasklist_id="list-1",
        tasklist_title="School",
    )
    plan = SyncPlanner().plan(
        course_id="spanish",
        source_key=SOURCE_KEY,
        task_list="School",
        extraction_mode=ExtractionMode.TEXT,
        fallback_reasons=[],
        drafts=[],
        uncertain=[],
        ignored=[],
        state_records=[],
        remote_tasks=[remote],
        include_past=True,
        dry_run=True,
        course_prefix="SPANISH",
    )
    assert [action.kind for action in plan.actions] == [SyncActionKind.UNCERTAIN]
    assert "malformed" in plan.actions[0].reason
