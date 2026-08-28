from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
        self.kwargs: list[dict[str, object]] = []

    def generate(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
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
        self.test_tasks: list[RemoteTask] = []
        self.created: list[str] = []
        self.fail_on_create: int | None = None

    def resolve_task_list(self, title: str):
        return ("list-2", "Tests") if title.casefold() == "tests" else ("list-1", "School")

    def list_tasks(self, tasklist_id: str):
        source = self.test_tasks if tasklist_id == "list-2" else self.tasks
        return [task.model_copy(deep=True) for task in source]

    def create_task(self, tasklist_id: str, *, title: str, notes: str, due_date):
        if self.fail_on_create is not None and len(self.created) + 1 == self.fail_on_create:
            raise RuntimeError("provider rejected the second write")
        task = RemoteTask(
            id=f"remote-{len(self.created) + 1}",
            title=title,
            notes=notes,
            due=due_date.isoformat() if due_date else None,
        )
        self.created.append(title)
        (self.test_tasks if tasklist_id == "list-2" else self.tasks).append(task)
        return task

    def verify_due(self, tasklist_id: str, task_id: str, due_date):
        task = next(task for task in self.list_tasks(tasklist_id) if task.id == task_id)
        assert task.due[:10] == due_date.isoformat() if due_date else task.due is None
        return task

    def update_notes(self, tasklist_id: str, task_id: str, notes: str):
        source = self.test_tasks if tasklist_id == "list-2" else self.tasks
        task = next(task for task in source if task.id == task_id)
        task.notes = notes
        return task.model_copy(deep=True)

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


def test_prepare_status_reports_when_a_gemini_fallback_model_was_used(
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
    backend.used_model = "fallback-model"
    backend.fallback_reasons = ["primary model failed"]
    sink = RecordingSink()

    service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
        progress=sink,
    )

    extraction_event = next(
        event
        for event in sink.events
        if event[0] == RunStage.EXTRACT_ASSIGNMENTS and event[1] == "stage_completed"
    )
    assert "fallback model fallback-model" in extraction_event[2]
    assert extraction_event[3]["fallback_used"] is True


def test_course_ai_instructions_change_prompt_and_extraction_cache_key(
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
    first = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
    )
    service.settings.courses["spanish"].ai_instructions = (
        "Do not create homework tasks for reading assignments."
    )
    second = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
    )

    assert first.extraction_cache_key != second.extraction_cache_key
    assert "Do not create homework tasks for reading assignments." not in str(
        backend.kwargs[0]["prompt"]
    )
    assert "Do not create homework tasks for reading assignments." in str(
        backend.kwargs[1]["prompt"]
    )


def test_course_model_preferences_override_project_chain_and_cache_key(
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
    captured: dict[str, object] = {}

    def backend_factory(**kwargs):
        captured.update(kwargs)
        return backend

    service.backend_factory = backend_factory
    course = service.settings.courses["spanish"]
    course.gemini_model = "gemini-3.5-flash"
    course.gemini_fallback_models = [
        "gemini-3.5-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]

    prepared = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
    )

    assert captured["model"] == "gemini-3.5-flash"
    assert captured["fallback_models"] == [
        "gemini-3.5-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]
    assert prepared.extraction_cache_key.startswith(
        "gemini-3.5-flash -> gemini-3.5-flash-lite -> "
        "gemini-3.7-flash -> gemini-3.6-flash|"
    )


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


def test_apply_allows_unrelated_course_changes_in_shared_task_list(
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
    tasks.tasks.append(
        RemoteTask(id="english-write", title="[ENGLISH] Revise paragraph")
    )

    result = service.apply(prepared)

    assert result.applied_counts["create"] == len(tasks.created)
    assert tasks.created


def test_apply_rejects_same_course_change_in_shared_task_list(
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
    tasks.tasks.append(
        RemoteTask(id="spanish-write", title="[SPANISH] Newly added task")
    )

    with pytest.raises(ValueError, match="Google Tasks changed"):
        service.apply(prepared)

    assert tasks.created == []


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


def test_failed_due_verification_keeps_new_remote_identity_mapping(
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

    def fail_verification(*_args, **_kwargs):
        raise RuntimeError("server omitted due date")

    tasks.verify_due = fail_verification
    with pytest.raises(RuntimeError, match="server omitted due date"):
        service.apply(prepared)

    with StateStore(service.settings.resolved_state_path, writable=False) as state:
        records = state.records("spanish", prepared.source_key)
    assert len(records) == 1
    assert records[0].google_task_id == "remote-1"


def test_prepare_feeds_unfinished_and_recent_completed_class_tasks_to_gemini(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    service, _source, tasks, backend = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    now = datetime.now(UTC)
    tasks.tasks = [
        RemoteTask(id="open", title="[SPANISH] VHL practice", status="needsAction"),
        RemoteTask(
            id="recent",
            title="[SPANISH] Submit class activity",
            status="completed",
            completed=(now - timedelta(days=14) + timedelta(minutes=1)).isoformat(),
        ),
        RemoteTask(
            id="old",
            title="[SPANISH] Old worksheet",
            status="completed",
            completed=(now - timedelta(days=15)).isoformat(),
        ),
        RemoteTask(id="other", title="[MATH] VHL practice", status="needsAction"),
        RemoteTask(id="near-prefix", title="[SPANISHISH] Wrong class", status="needsAction"),
        RemoteTask(
            id="hidden-recent",
            title="[SPANISH] Hidden recent completion",
            status="completed",
            hidden=True,
            completed=(now - timedelta(days=2)).isoformat(),
        ),
    ]
    tasks.test_tasks = [
        RemoteTask(
            id="assessment",
            title="[SPANISH] Preterite Quiz",
            due="2026-08-21T00:00:00.000Z",
        )
    ]

    service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
    )
    prompt = str(backend.kwargs[0]["prompt"])
    assert "[SPANISH] VHL practice" in prompt
    assert "[SPANISH] Submit class activity" in prompt
    assert "[SPANISH] Preterite Quiz" in prompt
    assert "[SPANISH] Hidden recent completion" in prompt
    assert "list Tests" in prompt
    assert "due 2026-08-21" in prompt
    assert "Old worksheet" not in prompt
    assert "[MATH]" not in prompt
    assert "[SPANISHISH]" not in prompt
