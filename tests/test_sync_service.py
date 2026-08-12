from __future__ import annotations

from pathlib import Path

import pytest

from canvas_task_sync.configuration import ProjectSettings
from canvas_task_sync.models import ExtractionMode, GeminiTaskCandidate, RemoteTask
from canvas_task_sync.state import StateStore
from canvas_task_sync.sync_service import (
    CancellationToken,
    NullProgressSink,
    SyncCancelled,
    SyncService,
)
from canvas_task_sync.web_models import EventLevel, RunStage


class RecordingSink(NullProgressSink):
    def __init__(self) -> None:
        self.events: list[tuple[RunStage, str, str, dict[str, object]]] = []

    def emit(
        self,
        stage: RunStage,
        event_type: str,
        message: str,
        *,
        level: EventLevel = EventLevel.INFO,
        metadata: dict[str, object] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        del level, duration_ms
        self.events.append((stage, event_type, message, metadata or {}))


class FakeBackend:
    def __init__(self, candidates: list[GeminiTaskCandidate]) -> None:
        self.candidates = candidates
        self.calls = 0

    def generate(self, **_kwargs):
        self.calls += 1
        return self.candidates


class FakeSource:
    def __init__(self, capture) -> None:
        self.capture_value = capture

    def capture(self, *, include_image: bool):
        del include_image
        return self.capture_value

    def add_image(self, capture):
        return capture


class FakeTasks:
    def __init__(self) -> None:
        self.tasks: list[RemoteTask] = []
        self.created: list[str] = []
        self.fail_on_create: int | None = None

    def resolve_task_list(self, _title: str):
        return "list-1", "School"

    def list_tasks(self, _tasklist_id: str):
        return [task.model_copy(deep=True) for task in self.tasks]

    def create_task(self, _tasklist_id: str, *, title: str, notes: str, due_date):
        if self.fail_on_create is not None and len(self.created) + 1 == self.fail_on_create:
            raise RuntimeError("provider rejected the second write")
        task = RemoteTask(
            id=f"remote-{len(self.created) + 1}",
            title=title,
            notes=notes,
            due=due_date.isoformat() if due_date else None,
        )
        self.created.append(title)
        self.tasks.append(task)
        return task

    def update_task(self, *_args, **_kwargs):
        raise AssertionError("This fixture should not produce updates.")


def _service(
    tmp_path: Path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
) -> tuple[SyncService, FakeSource, FakeTasks, FakeBackend]:
    settings = ProjectSettings(
        root_dir=tmp_path,
        state_path=Path(".canvas-task-sync/state.sqlite3"),
        gemini_model="test-model",
        courses={"spanish": spanish_course.model_copy(deep=True)},
    )
    source = FakeSource(spanish_capture)
    tasks = FakeTasks()
    backend = FakeBackend(spanish_candidates)
    service = SyncService(
        settings,
        credentials_loader=lambda *_args, **_kwargs: object(),
        source_factory=lambda *_args, **_kwargs: source,
        tasks_client_factory=lambda _credentials: tasks,
        backend_factory=lambda **_kwargs: backend,
    )
    return service, source, tasks, backend


def test_prepare_emits_stages_in_order_without_writing_state(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    service, _source, _tasks, backend = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    sink = RecordingSink()
    prepared = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
        progress=sink,
    )

    assert [event[0] for event in sink.events] == [
        RunStage.VALIDATE_CONFIGURATION,
        RunStage.AUTHENTICATE_SERVICES,
        RunStage.CAPTURE_SOURCE,
        RunStage.EXTRACT_ASSIGNMENTS,
        RunStage.CALCULATE_DEADLINES,
        RunStage.COMPARE_GOOGLE_TASKS,
        RunStage.BUILD_REVIEW_PLAN,
    ]
    assert prepared.plan.dry_run is True
    assert len(prepared.plan_hash) == 64
    assert backend.calls == 1
    assert not service.settings.resolved_state_path.exists()


def test_cancellation_takes_effect_between_stages(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    service, _source, _tasks, _backend = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    sink = RecordingSink()
    with pytest.raises(SyncCancelled):
        service.prepare(
            course_id="spanish",
            include_past=True,
            rebase_week=None,
            extraction_mode=ExtractionMode.TEXT,
            progress=sink,
            cancellation=CancellationToken(cancelled),
        )
    assert [event[0] for event in sink.events] == [
        RunStage.VALIDATE_CONFIGURATION,
        RunStage.AUTHENTICATE_SERVICES,
    ]


def test_apply_rejects_stale_source_before_writes(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    service, source, tasks, _backend = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    prepared = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
    )
    source.capture_value = spanish_capture.model_copy(update={"page_hash": "changed"})

    with pytest.raises(ValueError, match="source page changed"):
        service.apply(prepared)
    assert tasks.created == []
    assert not service.settings.resolved_state_path.exists()


def test_apply_exact_snapshot_persists_completed_mappings(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    service, _source, tasks, _backend = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    prepared = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
    )
    expected_writes = sum(
        action.kind.value in {"create", "update"} for action in prepared.plan.actions
    )
    result = service.apply(prepared)

    assert len(tasks.created) == expected_writes
    assert result.applied_counts.get("create", 0) == expected_writes
    with StateStore(service.settings.resolved_state_path, writable=False) as state:
        assert len(state.records("spanish", prepared.source_key)) == expected_writes


def test_partial_failure_keeps_completed_identity_mapping(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    service, _source, tasks, _backend = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    prepared = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
    )
    tasks.fail_on_create = 2
    with pytest.raises(RuntimeError, match="second write"):
        service.apply(prepared)

    assert len(tasks.created) == 1
    with StateStore(service.settings.resolved_state_path, writable=False) as state:
        records = state.records("spanish", prepared.source_key)
    assert len(records) == 1
    assert records[0].google_task_id == "remote-1"
