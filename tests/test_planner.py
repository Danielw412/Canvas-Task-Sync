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
    UncertainItem,
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


def test_manual_override_is_not_reverted_by_a_later_source_sync():
    source_draft = _draft()
    record = _record(source_draft, "durable-id").model_copy(update={
        "title": "[SPANISH] User-edited activity",
        "details": "Keep the user's revised notes.",
        "due_date": "2026-08-15",
        "classification": TaskClassification.CLASSWORK,
        "task_type": TaskType.ASSIGNMENT,
        "action_kind": ActionKind.SUBMIT,
        "due_basis": "manual",
        "manually_managed": True,
    })
    remote = RemoteTask(
        id="remote-1",
        title=record.title,
        notes=record.details,
        due="2026-08-15T00:00:00.000Z",
        tasklist_id="list-1",
        tasklist_title="School",
    )

    plan = _plan([source_draft], records=[record], remotes=[remote])

    assert plan.actions[0].kind == SyncActionKind.UNCHANGED
    assert plan.actions[0].desired is not None
    assert plan.actions[0].desired.title == "[SPANISH] User-edited activity"
    assert plan.actions[0].desired.details == "Keep the user's revised notes."
    assert plan.actions[0].desired.due_date == date(2026, 8, 15)


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
    assert plan.actions[0].conflict is True


def test_duplicate_managed_logical_id_is_a_conflict():
    draft = _draft()
    marker = build_managed_block(draft, "durable-id")
    remotes = [
        RemoteTask(
            id=remote_id,
            title=draft.title,
            notes=marker,
            due="2026-08-12T00:00:00.000Z",
            tasklist_id="list-1",
            tasklist_title="School",
        )
        for remote_id in ("remote-1", "remote-2")
    ]

    plan = _plan([draft], remotes=remotes, include_past=True)

    uncertain = [action for action in plan.actions if action.kind == SyncActionKind.UNCERTAIN]
    assert len(uncertain) == 1
    assert "More than one remote task" in uncertain[0].reason
    assert uncertain[0].conflict is True


THIS_WEEK = "canvas:11126:week:2026-09-21"
LAST_WEEK = "canvas:11126:week:2026-09-14"


def _exam_draft(source_key, due, source_text="October 6 FRQ section"):
    return _draft(
        due=due, title="[PHYSICS] Unit 2 FRQ Exam", task_type=TaskType.TEST
    ).model_copy(update={"source_key": source_key, "source_text": source_text})


def _week_record(draft, logical_id, remote_id, source_key):
    return _record(draft, logical_id, remote_id=remote_id).model_copy(
        update={"source_key": source_key, "task_type": draft.task_type}
    )


def _remote(remote_id, draft, *, status="needsAction"):
    return RemoteTask(
        id=remote_id,
        title=draft.title,
        notes=compose_task_notes("", draft),
        due=f"{draft.due_date.isoformat()}T00:00:00.000Z" if draft.due_date else None,
        status=status,
        tasklist_id="list-1",
        tasklist_title="School",
    )


def _week_plan(drafts, *, carryover, remotes, source_key=THIS_WEEK, week=date(2026, 9, 21)):
    return SyncPlanner().plan(
        course_id="spanish",
        source_key=source_key,
        task_list="School",
        extraction_mode=ExtractionMode.TEXT,
        fallback_reasons=[],
        drafts=drafts,
        uncertain=[],
        ignored=[],
        state_records=[],
        remote_tasks=remotes,
        carryover_records=carryover,
        week_start=week,
        today=week,
        include_past=False,
        dry_run=True,
    )


def test_rescheduled_item_from_last_weeks_agenda_updates_the_existing_task():
    old = _exam_draft(LAST_WEEK, date(2026, 10, 5), "October 5 FRQ section")
    record = _week_record(old, "exam-id", "exam-remote", LAST_WEEK)

    plan = _week_plan(
        [_exam_draft(THIS_WEEK, date(2026, 10, 6))],
        carryover=[record],
        remotes=[_remote("exam-remote", old)],
    )

    assert [action.kind for action in plan.actions] == [SyncActionKind.UPDATE]
    assert plan.actions[0].logical_id == "exam-id"
    assert plan.actions[0].remote_task_id == "exam-remote"
    assert plan.actions[0].due_date == date(2026, 10, 6)
    assert "due date" in plan.actions[0].reason


@pytest.mark.parametrize(
    ("old_due", "status"),
    [
        (date(2026, 10, 5), "completed"),  # Finished work is never re-opened or moved.
        (date(2026, 9, 18), "needsAction"),  # Due before this week: a new occurrence.
    ],
)
def test_finished_or_past_tasks_from_older_agendas_are_not_adopted(old_due, status):
    old = _exam_draft(LAST_WEEK, old_due, "October 5 FRQ section")
    record = _week_record(old, "exam-id", "exam-remote", LAST_WEEK)

    plan = _week_plan(
        [_exam_draft(THIS_WEEK, date(2026, 10, 6))],
        carryover=[record],
        remotes=[_remote("exam-remote", old, status=status)],
    )

    assert [action.kind for action in plan.actions] == [SyncActionKind.CREATE]
    assert plan.actions[0].logical_id != "exam-id"


def test_older_agenda_does_not_change_a_task_a_newer_agenda_tracks():
    newer = _exam_draft(THIS_WEEK, date(2026, 10, 6))
    record = _week_record(newer, "exam-id", "exam-remote", THIS_WEEK)

    plan = _week_plan(
        [_exam_draft(LAST_WEEK, date(2026, 9, 28), "September 28 FRQ section")],
        carryover=[record],
        remotes=[_remote("exam-remote", newer)],
        source_key=LAST_WEEK,
        week=date(2026, 9, 14),
    )

    assert [action.kind for action in plan.actions] == [SyncActionKind.IGNORED]
    assert "newer agenda" in plan.actions[0].reason
    assert plan.actions[0].remote_task_id == "exam-remote"


def test_duplicate_open_tasks_are_reported_instead_of_adding_another():
    first = _exam_draft(LAST_WEEK, date(2026, 10, 5), "October 5 FRQ section")
    second = _exam_draft(LAST_WEEK, date(2026, 10, 6), "October 6 FRQ section")
    records = [
        _week_record(first, "first-id", "first-remote", LAST_WEEK),
        _week_record(second, "second-id", "second-remote", LAST_WEEK),
    ]
    remotes = [_remote("first-remote", first), _remote("second-remote", second)]

    plan = _week_plan(
        [_exam_draft("canvas:11126:week:2026-09-28", date(2026, 10, 7))],
        carryover=records,
        remotes=remotes,
        source_key="canvas:11126:week:2026-09-28",
        week=date(2026, 9, 28),
    )

    assert [action.kind for action in plan.actions] == [SyncActionKind.UNCERTAIN]
    assert plan.actions[0].conflict is True
    assert "resolve the duplicates" in plan.actions[0].reason

    # When the copies come from different weeks, the latest agenda's task is the live one.
    records[1] = _week_record(second, "second-id", "second-remote", THIS_WEEK)
    plan = _week_plan(
        [_exam_draft("canvas:11126:week:2026-09-28", date(2026, 10, 7))],
        carryover=records,
        remotes=remotes,
        source_key="canvas:11126:week:2026-09-28",
        week=date(2026, 9, 28),
    )

    assert [action.kind for action in plan.actions] == [SyncActionKind.UPDATE]
    assert plan.actions[0].logical_id == "second-id"


def test_recurring_generic_work_on_a_new_date_is_a_new_task():
    last = _draft(due=date(2026, 9, 22), title="[SPANISH] VHL practice").model_copy(
        update={"source_key": LAST_WEEK}
    )
    record = _week_record(last, "vhl-id", "vhl-remote", LAST_WEEK)
    remotes = [_remote("vhl-remote", last)]

    new_date = _week_plan(
        [_draft(due=date(2026, 9, 24), title="[SPANISH] VHL practice").model_copy(
            update={"source_key": THIS_WEEK}
        )],
        carryover=[record],
        remotes=remotes,
    )
    same_date = _week_plan(
        [_draft(due=date(2026, 9, 22), title="[SPANISH] VHL practice").model_copy(
            update={"source_key": THIS_WEEK}
        )],
        carryover=[record],
        remotes=remotes,
    )

    assert [action.kind for action in new_date.actions] == [SyncActionKind.CREATE]
    assert [action.kind for action in same_date.actions] == [SyncActionKind.UNCHANGED]
    assert same_date.actions[0].logical_id == "vhl-id"


def test_unchanged_evidence_keeps_the_known_date_title_and_details():
    original = _draft(due=date(2026, 8, 13), title="[SPANISH] Complete Class Activity")
    record = _record(original, "durable-id").model_copy(
        update={"details": "Finish the class activity.", "due_basis": "next class"}
    )
    remote = RemoteTask(
        id="remote-1",
        title=original.title,
        notes="Finish the class activity.",
        due="2026-08-13T00:00:00.000Z",
        tasklist_id="list-1",
        tasklist_title="School",
    )
    # A re-extraction of the same evidence lost the date and reworded the rest.
    flaky = _draft(
        due=None,
        title="[SPANISH] Complete class activity",
        due_uncertain=True,
        due_uncertain_reason="Next-class work could not be tied to a dated agenda row.",
    ).model_copy(update={"details": "Complete and finish the class activity."})

    plan = _plan([flaky], records=[record], remotes=[remote])

    assert plan.actions[0].kind == SyncActionKind.UNCHANGED
    assert plan.actions[0].due_date == date(2026, 8, 13)
    assert plan.actions[0].due_uncertain is False
    assert plan.actions[0].desired.title == "[SPANISH] Complete Class Activity"

    # When the source itself changed, the new reading is followed.
    edited = flaky.model_copy(update={"source_text": "Completar la actividad (sin fecha)"})
    plan = _plan([edited], records=[record], remotes=[remote])

    assert plan.actions[0].kind == SyncActionKind.UPDATE
    assert plan.actions[0].due_date is None


def test_extraction_uncertainty_is_informational_not_a_conflict():
    plan = SyncPlanner().plan(
        course_id="spanish",
        source_key=SOURCE_KEY,
        task_list="School",
        extraction_mode=ExtractionMode.TEXT,
        fallback_reasons=[],
        drafts=[],
        uncertain=[
            UncertainItem(
                title="Warm-up",
                evidence="Warm-up: discuss the reading",
                reason="Gemini did not provide a high-enough-confidence actionable title.",
                source_anchor="table:agenda:r1:c2",
            )
        ],
        ignored=[],
        state_records=[],
        remote_tasks=[],
        include_past=True,
        dry_run=True,
        course_prefix="SPANISH",
    )

    assert [action.kind for action in plan.actions] == [SyncActionKind.UNCERTAIN]
    assert plan.actions[0].conflict is False
