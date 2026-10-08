from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

from canvas_task_sync.configuration import (
    CanvasAgendaOverride,
    NoFallbackSourceSettings,
    ProjectSettings,
)
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


def test_override_change_invalidates_a_preview_before_any_task_writes(
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
    course = service.settings.course("spanish")
    course.canvas_course_id = "12604"
    prepared = service.prepare(course_id="spanish", include_past=True, rebase_week=None)
    course.canvas_agenda_override = CanvasAgendaOverride(
        page_slug="weekly-agenda",
        expected_heading_date=date(2026, 8, 17),
        target_week_start=date(2026, 8, 24),
        required_text="Distinctive current worksheet",
    )
    with pytest.raises(ValueError, match="Course configuration changed after this preview"):
        service.apply(prepared)
    assert tasks.created == []
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


def test_unchanged_page_reuses_its_extraction_after_the_sync_writes_tasks(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    # The recent-task context sent to Gemini changes after every write. Keying the cache
    # on it re-extracted unchanged pages, and due dates flipped between runs.
    service, _source, tasks, backend = _service(
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
    service.apply(first)
    assert tasks.created

    second = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
    )

    assert backend.calls == 1
    assert second.extraction_was_cached is True
    assert second.extraction_cache_key == first.extraction_cache_key


def test_next_weeks_agenda_adopts_tasks_created_from_last_weeks_agenda(
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
    week = date(2026, 5, 25)
    first = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        target_week_start=week,
        extraction_mode=ExtractionMode.TEXT,
    )
    service.apply(first)
    created = len(tasks.created)
    # The same items listed again on the next agenda, which is a separate source.
    source.capture_value = spanish_capture.model_copy(
        update={"source_key": "canvas:spanish:week:2026-05-25"}
    )

    second = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        target_week_start=week,
        extraction_mode=ExtractionMode.TEXT,
    )

    kinds = [action.kind.value for action in second.plan.actions if action.desired]
    assert kinds and set(kinds) == {"unchanged"}
    service.apply(second)
    assert len(tasks.created) == created
    with StateStore(service.settings.resolved_state_path, writable=False) as state:
        assert len(state.records("spanish", "canvas:spanish:week:2026-05-25")) == created


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
    course.gemini_reasoning = "high"

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
    assert captured["thinking_level"] == "high"
    assert prepared.extraction_cache_key.startswith(
        "gemini-3.5-flash -> gemini-3.5-flash-lite -> "
        "gemini-3.7-flash -> gemini-3.6-flash|reasoning:high|"
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


def test_a_global_claude_agent_replaces_gemini_for_extraction(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    from canvas_task_sync.configuration import ExtractionAgentSettings

    service, _source, _tasks, gemini = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    service.settings.extraction_agent = ExtractionAgentSettings(
        provider="claude", model="claude-opus-5-5", effort="high"
    )
    agent_backend = FakeBackend(spanish_candidates)
    agent_backend.provider_label = "Claude"
    agent_backend.used_model = "claude-opus-5-5"
    resolved: list[object] = []

    def agent_factory(agent):
        resolved.append(agent)
        return agent_backend

    service.agent_backend_factory = agent_factory
    sink = RecordingSink()
    prepared = service.prepare(
        course_id="spanish",
        include_past=True,
        rebase_week=None,
        extraction_mode=ExtractionMode.TEXT,
        progress=sink,
    )

    assert gemini.calls == 0 and agent_backend.calls == 1
    assert resolved[0].models == ["claude-opus-5-5"] and resolved[0].effort == "high"
    assert callable(agent_backend.cancelled) and callable(agent_backend.on_slot_wait)
    assert prepared.extraction_cache_key.startswith("claude:claude-opus-5-5|effort:high|")
    extracted = next(event for event in sink.events if event[0] == RunStage.EXTRACT_ASSIGNMENTS)
    assert extracted[2] == "Claude extraction completed."
    assert extracted[3]["provider"] == "claude"
    assert extracted[3]["model"] == "claude-opus-5-5"
    assert extracted[3]["reasoning_level"] == "high"


def test_switching_agents_does_not_reuse_another_agents_cached_extraction(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    from canvas_task_sync.configuration import ExtractionAgentSettings

    service, _source, _tasks, _backend = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    gemini_key = service.prepare(
        course_id="spanish", include_past=True, rebase_week=None
    ).extraction_cache_key
    service.settings.extraction_agent = ExtractionAgentSettings(provider="codex")
    service.agent_backend_factory = lambda _agent: FakeBackend(spanish_candidates)
    codex_key = service.prepare(
        course_id="spanish", include_past=True, rebase_week=None
    ).extraction_cache_key
    assert gemini_key.startswith("test-model -> gemini-3.6-flash")
    assert codex_key.startswith("codex:gpt-6-luna|effort:medium|")


def test_an_agent_turn_stopped_by_cancellation_reports_the_cancellation(
    tmp_path,
    spanish_course,
    spanish_capture,
    spanish_candidates,
):
    from canvas_task_sync.configuration import ExtractionAgentSettings

    service, _source, _tasks, _backend = _service(
        tmp_path,
        spanish_course,
        spanish_capture,
        spanish_candidates,
    )
    service.settings.extraction_agent = ExtractionAgentSettings(provider="claude")
    cancelled = {"value": False}

    class InterruptedBackend:
        def generate(self, **_kwargs):
            cancelled["value"] = True
            raise RuntimeError("The run was cancelled.")

    service.agent_backend_factory = lambda _agent: InterruptedBackend()
    with pytest.raises(SyncCancelled):
        service.prepare(
            course_id="spanish",
            include_past=True,
            rebase_week=None,
            cancellation=CancellationToken(lambda: cancelled["value"]),
        )


# --- Agenda verification (Claude and Codex only) ------------------------------------------

VERIFY_WEEK = date(2026, 8, 24)
CANVAS = "https://canvas.example"
CANVAS_PREFIX = "/api/v1/courses/11126"


class CanvasCourse:
    """A Canvas course whose pages a test can edit between runs."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url, *, params=None, timeout=None, **_kwargs):
        del params, timeout
        path = urlparse(url).path
        self.calls.append(path)
        page = {
            "url": "weekly-agenda",
            "title": "Weekly agenda",
            "html_url": f"{CANVAS}/courses/11126/pages/weekly-agenda",
            "body": self.body,
        }
        routes = {
            f"{CANVAS_PREFIX}/front_page": {"url": "home", "title": "Home", "body": "<p>Hi</p>"},
            CANVAS_PREFIX: {},
            f"{CANVAS_PREFIX}/modules": [],
            f"{CANVAS_PREFIX}/pages": [page],
            f"{CANVAS_PREFIX}/pages/weekly-agenda": page,
            f"{CANVAS_PREFIX}/assignments": [
                {"id": 501, "name": "Practice set 4", "due_at": "2026-08-26T03:59:00Z"}
            ],
        }
        if path not in routes:
            raise requests.HTTPError(f"404 for {path}")

        class Response:
            links: dict[str, object] = {}

            def raise_for_status(self):
                return None

            def json(self):
                return routes[path]

        return Response()


def _table(heading: str, work: str) -> str:
    return (
        f"<table><tr><th>{heading}</th><th>Learning Activities</th><th>Assignments</th></tr>"
        f"<tr><td>Monday</td><td>{work}</td><td><a href='{CANVAS}/courses/11126/assignments/"
        "501'>Practice set 4</a></td></tr><tr><td>Tuesday</td><td>Lab</td>"
        "<td>Bring calculator</td></tr></table>"
    )


class VerifyingAgent:
    """One fake Claude backend: it verifies agendas and extracts nothing."""

    def __init__(self, verdicts, counter) -> None:
        self.verdicts = verdicts
        self.counter = counter
        self.used_model = "claude-opus-5-5"
        self.provider_label = "Claude"
        self.fallback_reasons: list[str] = []

    def run_structured(self, prompt, turn, parse):
        self.counter["verify"] += 1
        reply = self.verdicts.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return parse(reply)

    def generate(self, **_kwargs):
        self.counter["extract"] += 1
        return []


def _verifying_service(
    tmp_path, spanish_course, body, verdicts, *, provider="claude", fallback=None
):
    from canvas_task_sync.configuration import ExtractionAgentSettings
    from canvas_task_sync.sources import CourseAgendaSource
    from canvas_task_sync.sources.canvas import CanvasAgendaSource

    course = spanish_course.model_copy(deep=True)
    course.canvas_course_id = "11126"
    course.canvas_base_url = CANVAS
    course.source = NoFallbackSourceSettings(extraction={"mode": "text"})
    settings = ProjectSettings(
        root_dir=tmp_path,
        state_path=Path(".canvas-task-sync/state.sqlite3"),
        gemini_model="test-model",
        extraction_agent=ExtractionAgentSettings(provider=provider),
        courses={"physics": course},
    )
    canvas = CanvasCourse(body)
    counter = {"verify": 0, "extract": 0}
    factory_options: list[dict[str, object]] = []

    def source_factory(course, credentials, *, target_week_start, acquisition_strategy, **kw):
        del credentials
        factory_options.append(kw)
        return CourseAgendaSource(
            lambda: CanvasAgendaSource(
                course_id=course.canvas_course_id,
                target_week_start=target_week_start,
                current_week_start=VERIFY_WEEK,
                base_url=CANVAS,
                token="test-token",
                session=canvas,
                timezone_name=course.timezone,
            ),
            (lambda: fallback) if fallback is not None else None,
            acquisition_strategy,
            kw.get("agenda_resolver"),
        )

    gemini = FakeBackend([])
    service = SyncService(
        settings,
        credentials_loader=lambda *_args, **_kwargs: object(),
        source_factory=source_factory,
        tasks_client_factory=lambda _credentials: FakeTasks(),
        backend_factory=lambda **_kwargs: gemini,
        agent_backend_factory=lambda _agent: VerifyingAgent(verdicts, counter),
    )
    return service, canvas, counter, factory_options, gemini


def _verified_located() -> dict[str, object]:
    return {
        "status": "verified",
        "agenda": {"kind": "page", "id": "weekly-agenda", "table_number": 1,
                   "distinctive_text": None},
        "summary": "Table 1 links this week's practice set.",
        "evidence": [
            {
                "kind": "assignment_due",
                "source": {"kind": "assignment", "id": "501"},
                "quote": "Practice set 4",
                "stated_date": "2026-08-25",
                "supports": "requested_week",
                "note": "",
            }
        ],
        "concerns": [],
    }


def _prepare(service, sink=None, **kwargs):
    return service.prepare(
        course_id="physics",
        include_past=True,
        rebase_week=None,
        target_week_start=VERIFY_WEEK,
        progress=sink,
        **kwargs,
    )


def test_a_verified_agenda_is_previewed_applied_by_replay_and_then_reused(
    tmp_path, spanish_course
):
    body = _table("Unit 3", "Start Unit 3 lab") + _table("Week of August 17", "Old review")
    verdicts = [_verified_located()]
    service, _canvas, counter, options, _gemini = _verifying_service(
        tmp_path, spanish_course, body, verdicts
    )
    sink = RecordingSink()
    prepared = _prepare(service, sink)

    assert counter == {"verify": 1, "extract": 1}
    assert "agenda_resolver" in options[0]
    verification = prepared.agenda_verification
    assert verification.status.value == "verified" and not verification.uses_candidate
    assert verification.agent_key == "claude:claude-sonnet-5-5|effort:medium"
    capture_events = [event for event in sink.events if event[0] == RunStage.CAPTURE_SOURCE]
    assert [event[1] for event in capture_events] == [
        "agenda_verification_started",
        "agenda_verified",
        "stage_completed",
    ]
    assert capture_events[-1][2] == "Captured the Canvas agenda Claude located for this week."
    assert capture_events[-1][3]["agenda_verification"]["status"] == "verified"
    assert not service.settings.resolved_state_path.exists()

    # The stored preview revalidates by replaying the decision: no second agent turn.
    from canvas_task_sync.sync_service import prepared_plan_from_json

    service.apply(prepared_plan_from_json(prepared.model_dump_json()))
    assert counter["verify"] == 1
    with StateStore(service.settings.resolved_state_path, writable=False) as state:
        stored = state.cached_agenda_verification(
            course_id="physics",
            source_key="canvas:11126:week:2026-08-24",
            fingerprint=verification.fingerprint,
            verifier_version=verification.version,
            agent_key=verification.agent_key,
        )
    assert stored is not None and '"cached"' not in stored

    sink = RecordingSink()
    again = _prepare(service, sink)
    assert counter["verify"] == 1
    assert again.agenda_verification.cached and again.page_hash == prepared.page_hash
    reused = next(event for event in sink.events if event[1] == "agenda_verified")
    assert "Reused the verification" in reused[2]


def test_a_mislabeled_agenda_stops_the_run_before_extraction(tmp_path, spanish_course):
    from canvas_task_sync.agenda_verification import AgendaVerificationError

    body = _table("Week of August 17", "Start Unit 3 lab") + _table(
        "Week of August 17", "Old review"
    )
    verdict = _verified_located()
    verdict["status"] = "suspected_mislabeled"
    verdict["agenda"]["distinctive_text"] = "Start Unit 3 lab"
    service, _canvas, counter, _options, _gemini = _verifying_service(
        tmp_path, spanish_course, body, [verdict]
    )
    sink = RecordingSink()
    with pytest.raises(AgendaVerificationError, match="temporary agenda override") as raised:
        _prepare(service, sink)
    assert raised.value.verification.override_suggestion.required_text == "Start Unit 3 lab"
    assert counter == {"verify": 1, "extract": 0}
    assert sink.events[-1][1] == "agenda_unverified"
    assert not service.settings.resolved_state_path.exists()


def test_gemini_never_verifies_or_discovers_agendas(tmp_path, spanish_course):
    from canvas_task_sync.sources.canvas import CanvasAgendaNotFound

    body = _table("Week of August 24", "Start Unit 3 lab")
    service, _canvas, counter, options, gemini = _verifying_service(
        tmp_path, spanish_course, body, [], provider="gemini"
    )
    prepared = _prepare(service)
    assert options == [{}] and prepared.agenda_verification is None
    assert counter == {"verify": 0, "extract": 0} and gemini.calls == 1
    missing, _canvas, counter, _options, _gemini = _verifying_service(
        tmp_path, spanish_course, _table("Unit 3", "Start Unit 3 lab"), [], provider="gemini"
    )
    with pytest.raises(CanvasAgendaNotFound):
        _prepare(missing)
    assert counter["verify"] == 0


def test_a_changed_alternative_agenda_makes_the_preview_stale(tmp_path, spanish_course):
    body = _table("Unit 3", "Start Unit 3 lab") + _table("Week of August 17", "Old review")
    service, canvas, counter, _options, _gemini = _verifying_service(
        tmp_path, spanish_course, body, [_verified_located()]
    )
    prepared = _prepare(service)
    canvas.body = body.replace("Start Unit 3 lab", "Start Unit 3 lab report")
    with pytest.raises(ValueError, match="changed after this preview"):
        service.apply(prepared)
    assert counter["verify"] == 1
    assert not service.settings.resolved_state_path.exists()


def test_an_agent_that_cannot_verify_fails_the_run_and_cancellation_is_reported(
    tmp_path, spanish_course
):
    from canvas_task_sync.agenda_verification import AgendaVerificationUnavailable
    from canvas_task_sync.agent_backends import AgentExtractionError

    body = _table("Week of August 24", "Start Unit 3 lab")
    service, _canvas, counter, _options, _gemini = _verifying_service(
        tmp_path,
        spanish_course,
        body,
        [AgentExtractionError("Claude Code is not signed in.")],
    )
    with pytest.raises(AgendaVerificationUnavailable, match="not signed in"):
        _prepare(service)
    assert counter["extract"] == 0

    cancelled = {"value": False}

    def interrupted(*_args):
        cancelled["value"] = True
        raise AgentExtractionError("The run was cancelled.")

    service, _canvas, counter, _options, _gemini = _verifying_service(
        tmp_path, spanish_course, body, []
    )
    service.agent_backend_factory = lambda _agent: type(
        "Interrupted", (), {"run_structured": interrupted}
    )()
    with pytest.raises(SyncCancelled):
        _prepare(service, cancellation=CancellationToken(lambda: cancelled["value"]))


def test_an_unverified_canvas_agenda_falls_back_and_the_rejection_is_replayed_and_reused(
    tmp_path, spanish_course, spanish_capture
):
    unresolved = {
        "status": "unresolved",
        "agenda": None,
        "summary": "Nothing confirms this week.",
        "evidence": [],
        "concerns": [],
    }
    fallback = FakeSource(spanish_capture)
    body = _table("Week of August 24", "Start Unit 3 lab")
    service, canvas, counter, _options, _gemini = _verifying_service(
        tmp_path, spanish_course, body, [unresolved, dict(unresolved)], fallback=fallback
    )
    sink = RecordingSink()
    prepared = _prepare(service, sink)
    assert prepared.page_hash == spanish_capture.page_hash
    assert prepared.agenda_verification.status.value == "unresolved"
    captured = next(
        event
        for event in sink.events
        if event[0] == RunStage.CAPTURE_SOURCE and event[1] == "stage_completed"
    )
    assert captured[3]["acquisition_fallback"]["agenda_verification"] == "unresolved"
    assert counter == {"verify": 1, "extract": 1}

    # Apply replays the rejection through the same fallback, then caches it.
    service.apply(prepared)
    assert counter["verify"] == 1
    again = _prepare(service)
    assert counter["verify"] == 1 and again.agenda_verification.cached
    assert again.page_hash == spanish_capture.page_hash

    # A changed Canvas agenda is a new question for the agent.
    canvas.body = _table("Week of August 24", "Start Unit 3 lab, part 2")
    _prepare(service)
    assert counter["verify"] == 2
