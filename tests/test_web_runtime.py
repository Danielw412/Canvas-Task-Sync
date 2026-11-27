from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from canvas_task_sync.configuration import (
    CanvasAgendaOverride,
    NoFallbackSourceSettings,
    ProjectSettings,
)
from canvas_task_sync.configuration_service import ConfigurationService
from canvas_task_sync.control_store import ControlStore
from canvas_task_sync.models import (
    ExtractionMode,
    ExtractionOutcome,
    SyncAction,
    SyncActionKind,
    SyncPlan,
)
from canvas_task_sync.redaction import REDACTED, redact_text, sanitize
from canvas_task_sync.run_executor import (
    RunExecutor,
    StoreProgressSink,
    _review_attention_count,
)
from canvas_task_sync.run_manager import (
    ScheduleManager,
    next_schedule_occurrence,
)
from canvas_task_sync.sync_service import AppliedPlanResult, PreparedPlan
from canvas_task_sync.web_app import create_web_app
from canvas_task_sync.web_models import (
    CourseSave,
    EventLevel,
    HealthState,
    RunMode,
    RunStage,
    RunStatus,
    RunTrigger,
    ScheduleCreate,
    ScheduleMode,
)


def test_switching_to_no_fallback_removes_old_source_fields(tmp_path):
    config_path = _write_project(tmp_path)
    service = ConfigurationService(config_path)
    course = service.load().course("spanish").model_copy(deep=True)
    course.canvas_course_id = "12604"
    course.source = NoFallbackSourceSettings(extraction=course.source.extraction)

    service.save_course(CourseSave(id="spanish", settings=course), creating=False)

    source = service.sanitized_document()["courses"]["spanish"]["source"]
    assert source["type"] == "none"
    assert "url" not in source
    assert "page_id" not in source


def test_course_api_saves_and_removes_an_override_without_changing_another_course(tmp_path):
    config = _write_project(tmp_path)
    service = ConfigurationService(config)
    other = service.load().course("spanish").model_copy(deep=True)
    service.save_course(CourseSave(id="other", settings=other), creating=True)
    app = create_web_app(config)
    with TestClient(app) as client:
        before = client.get("/api/v1/courses").json()
        course = next(item for item in before if item["id"] == "spanish")
        other_before = next(item for item in before if item["id"] == "other")
        settings = course["settings"]
        settings["canvas_course_id"] = "12604"
        settings["canvas_agenda_override"] = {
            "page_slug": "weekly-agenda",
            "table_number": 1,
            "expected_heading_date": "2026-08-17",
            "target_week_start": "2026-08-24",
            "required_text": "Distinctive current worksheet",
        }
        response = client.put(
            "/api/v1/courses/spanish",
            headers=_csrf(client),
            json={"id": "spanish", "settings": settings},
        )
        assert response.status_code == 200
        saved = client.get("/api/v1/courses").json()
        assert next(item for item in saved if item["id"] == "other") == other_before
        stored = service.load().course("spanish").canvas_agenda_override
        assert stored.target_week_start == date(2026, 8, 24)
        assert stored.expires_on == date(2026, 8, 30)
        assert "keep this course comment" in config.read_text()
        settings["canvas_agenda_override"] = None
        removed = client.put(
            "/api/v1/courses/spanish",
            headers=_csrf(client),
            json={"id": "spanish", "settings": settings},
        )
        assert removed.status_code == 200
        assert service.load().course("spanish").canvas_agenda_override is None


@pytest.mark.parametrize(
    "changes",
    [
        {"target_week_start": "2026-08-25"},
        {"page_slug": "../other-course"},
        {"page_slug": "https://other.example/agenda"},
        {"table_number": 0},
        {"table_number": 101},
        {"required_text": "  "},
    ],
)
def test_temporary_override_rejects_invalid_settings(changes):
    with pytest.raises(ValueError):
        CanvasAgendaOverride.model_validate(
            {
                "page_slug": "weekly-agenda",
                "table_number": 1,
                "expected_heading_date": "2026-08-17",
                "target_week_start": "2026-08-24",
                "required_text": "Distinctive current worksheet",
                **changes,
            }
        )


def test_temporary_override_requires_a_canvas_course(tmp_path):
    course = ConfigurationService(_write_project(tmp_path)).load().course("spanish")
    payload = course.model_dump()
    payload["canvas_agenda_override"] = {
        "page_slug": "weekly-agenda",
        "expected_heading_date": "2026-08-17",
        "target_week_start": "2026-08-24",
        "required_text": "Distinctive current worksheet",
    }
    with pytest.raises(ValueError, match="canvas_course_id is required"):
        type(course).model_validate(payload)


def _write_project(root: Path, *, enabled: bool = True) -> Path:
    config = root / "config" / "courses.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""# keep this header comment
version: 1
state_path: .canvas-task-sync/state.sqlite3
gemini_model: test-model
courses:
  spanish:
    # keep this course comment
    enabled: {str(enabled).lower()}
    name: Test Spanish
    prefix: SPANISH
    task_list: School
    timezone: America/New_York
    meeting_days: [mon, tue, wed, thu, fri]
    source:
      type: google_slides
      url: https://docs.google.com/presentation/d/fixture/edit
      page_id: slide-1
      extraction:
        mode: text
        thumbnail_size: large
        assignments_default_due: next_class
        same_day_action_kinds: [bring, present, submit]
""",
        encoding="utf-8",
    )
    return config


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    return {"x-csrf-token": token}


def test_redaction_covers_secret_fields_bearers_keys_and_binary():
    api_key = "AIzaabcdefghijklmnopqrstuvwxyz123456"
    value = sanitize(
        {
            "authorization": f"Bearer {api_key}",
            "nested": {"client_secret": "top-secret"},
            "message": f"request failed for {api_key}",
            "source": b"not-an-image-we-should-log",
        }
    )
    assert value["authorization"] == REDACTED
    assert value["nested"]["client_secret"] == REDACTED
    assert api_key not in value["message"]
    assert value["source"].startswith("[BINARY ")
    assert redact_text(f"Bearer {api_key}") == f"Bearer {REDACTED}"


def test_redaction_normalizes_dates_for_json_event_metadata():
    value = sanitize(
        {
            "due_date": date(2026, 8, 19),
            "captured_at": datetime(2026, 8, 17, 21, 30, tzinfo=UTC),
        }
    )

    assert value == {
        "due_date": "2026-08-19",
        "captured_at": "2026-08-17T21:30:00+00:00",
    }
    json.dumps(value)


def test_control_store_replays_events_and_sanitizes_at_write_boundary(tmp_path):
    store = ControlStore(tmp_path / "control.sqlite3")
    try:
        run_id = store.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.PREVIEW,
        )
        store.add_event(
            run_id,
            stage=RunStage.QUEUED,
            event_type="first",
            message="Queued",
        )
        store.add_event(
            run_id,
            stage=RunStage.VALIDATE_CONFIGURATION,
            event_type="second",
            message="Validated",
            level=EventLevel.INFO,
            metadata={
                "api_key": "should-never-persist",
                "count": 3,
                "due_date": date(2026, 8, 19),
            },
        )

        replay = store.events_after(run_id, sequence=1)
        assert [event.sequence for event in replay] == [2]
        assert replay[0].metadata == {
            "api_key": REDACTED,
            "count": 3,
            "due_date": "2026-08-19",
        }
        store.update_run(run_id, status=RunStatus.SUCCEEDED, finished_at=datetime.now(UTC))
        assert store.get_run(run_id).status == RunStatus.SUCCEEDED
    finally:
        store.close()


def test_progress_events_persist_the_current_run_stage(tmp_path):
    store = ControlStore(tmp_path / "control.sqlite3")
    try:
        run_id = store.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.AUTO_APPLY,
        )
        sink = StoreProgressSink(store, run_id, threading.Condition())

        sink.emit(
            RunStage.EXTRACT_ASSIGNMENTS,
            "gemini_retry_wait",
            "Waiting before retrying Gemini.",
            level=EventLevel.WARNING,
        )

        assert store.get_run(run_id, include_events=False).stage == RunStage.EXTRACT_ASSIGNMENTS
    finally:
        store.close()


def test_only_conflicts_require_review():
    assert _review_attention_count(
        {
            "source_missing": 4,
            "historical_blocked": 3,
            "uncertain": 2,
            "remote_missing": 1,
            "due_uncertain": 1,
            "conflict": 0,
        }
    ) == 0
    assert _review_attention_count({"conflict": 1}) == 1
    assert _review_attention_count({}) == 0


class _FakePreviewService:
    def __init__(self, settings: ProjectSettings, prepared: PreparedPlan) -> None:
        self.settings = settings
        self.prepared = prepared

    def prepare(self, **_kwargs) -> PreparedPlan:
        return self.prepared

    def apply(self, prepared: PreparedPlan, *, progress, safe_subset: bool) -> AppliedPlanResult:
        del prepared, progress, safe_subset
        return AppliedPlanResult(applied_counts={}, completed_action_indexes=[])


def _prepared_plan(course, actions: list[SyncAction]) -> PreparedPlan:
    plan = SyncPlan(
        course_id="spanish",
        task_list=course.task_list,
        dry_run=True,
        extraction_mode=ExtractionMode.TEXT,
        actions=actions,
    )
    return PreparedPlan(
        course_id="spanish",
        course=course,
        source_key="fixture",
        configured_mode=course.source.extraction.mode,
        target_week_start=date(2026, 9, 7),
        config_hash="config",
        page_hash="page",
        remote_hash="remote",
        plan_hash="plan",
        extraction_was_cached=False,
        extraction_outcome=ExtractionOutcome(used_mode=ExtractionMode.TEXT),
        plan=plan,
    )


def _run_auto_apply(tmp_path: Path, course, actions: list[SyncAction]):
    settings = ProjectSettings(
        root_dir=tmp_path,
        state_path=Path(".canvas-task-sync/state.sqlite3"),
        courses={"spanish": course},
    )
    store = ControlStore(tmp_path / "control.sqlite3")
    executor = RunExecutor(store, _FakePreviewService(settings, _prepared_plan(course, actions)))
    try:
        run_id = store.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.AUTO_APPLY,
        )
        executor._execute_preview(run_id, StoreProgressSink(store, run_id, threading.Condition()))
        return store.get_run(run_id)
    finally:
        store.close()


def test_auto_apply_succeeds_when_only_informational_items_remain(tmp_path, spanish_course):
    run = _run_auto_apply(
        tmp_path,
        spanish_course,
        [
            SyncAction(
                kind=SyncActionKind.UNCERTAIN,
                title="Warm-up discussion",
                reason="Gemini did not provide a high-enough-confidence actionable title.",
            ),
            SyncAction(
                kind=SyncActionKind.REMOTE_MISSING,
                title="Deleted by the student",
                reason="The mapped Google Task is missing or deleted.",
            ),
            SyncAction(
                kind=SyncActionKind.CREATE,
                title="Essay draft",
                reason="New task",
                due_uncertain=True,
                due_uncertain_reason="Next-class work could not be tied to a dated agenda row.",
                task_list="School",
            ),
        ],
    )

    assert run.status == RunStatus.SUCCEEDED
    assert run.counts["uncertain"] == 1
    assert run.counts["remote_missing"] == 1
    assert run.counts["due_uncertain"] == 1
    assert run.counts["conflict"] == 0
    completed = next(event for event in run.events if event.event_type == "run_completed")
    assert completed.message == "Sync completed successfully."


def test_auto_apply_needs_review_when_remote_state_conflicts(tmp_path, spanish_course):
    run = _run_auto_apply(
        tmp_path,
        spanish_course,
        [
            SyncAction(
                kind=SyncActionKind.UNCERTAIN,
                title="Vocabulary quiz",
                reason="More than one remote task contains this managed logical ID.",
                conflict=True,
            ),
        ],
    )

    assert run.status == RunStatus.REVIEW_NEEDED
    assert run.counts["conflict"] == 1


def test_next_occurrence_normalizes_spring_dst_gap_and_uses_first_fall_fold():
    spring = next_schedule_occurrence(
        weekdays=[6],
        local_time=time(2, 30),
        timezone_name="America/New_York",
        after=datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
    )
    spring_local = spring.astimezone(ZoneInfo("America/New_York"))
    assert (spring_local.hour, spring_local.minute) == (3, 30)

    fall = next_schedule_occurrence(
        weekdays=[6],
        local_time=time(1, 30),
        timezone_name="America/New_York",
        after=datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
    )
    fall_local = fall.astimezone(ZoneInfo("America/New_York"))
    assert (fall_local.hour, fall_local.minute, fall_local.fold) == (1, 30, 0)


class _LocalRunQueue:
    def __init__(self, store: ControlStore) -> None:
        self.store = store
        self.created: list[int] = []

    def create_preview(self, request, *, trigger, requested_mode, schedule_id):
        run_id = self.store.create_run(
            course_id=request.course_id,
            trigger=trigger,
            requested_mode=requested_mode,
            schedule_id=schedule_id,
        )
        self.created.append(run_id)
        return run_id


def test_scheduler_records_all_offline_occurrences_then_queues_grace_window(tmp_path):
    store = ControlStore(tmp_path / "control.sqlite3")
    try:
        payload = ScheduleCreate(
            name="Daily",
            course_id="spanish",
            weekdays=list(range(7)),
            local_time=time(12, 0),
            timezone="UTC",
            mode=ScheduleMode.PREVIEW,
            enabled=True,
        )
        first = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        schedule = store.create_schedule(payload, next_run_at=first)
        queue = _LocalRunQueue(store)
        manager = ScheduleManager(store, queue)  # type: ignore[arg-type]
        manager.tick(datetime(2026, 8, 11, 12, 0, 30, tzinfo=UTC))

        occurrences = list(reversed(store.list_schedule_occurrences()))
        assert [item.status for item in occurrences] == [
            "missed",
            "missed",
            "missed",
            "queued",
        ]
        assert len(queue.created) == 1
        run_id = queue.created[0]
        store.update_run(run_id, status=RunStatus.SUCCEEDED, finished_at=datetime.now(UTC))
        assert store.finish_schedule_occurrence(
            run_id=run_id,
            status="succeeded",
            details="Completed.",
        )
        assert store.get_schedule(schedule.id).last_result == "succeeded"
    finally:
        store.close()


class _FailingRunQueue(_LocalRunQueue):
    def create_preview(self, request, **kwargs):
        if request.course_id == "gone":
            raise ValueError("Unknown course 'gone'.")
        return super().create_preview(request, **kwargs)


def test_a_schedule_that_cannot_start_does_not_stop_the_others(tmp_path):
    store = ControlStore(tmp_path / "control.sqlite3")
    try:
        due = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        schedules = {
            course: store.create_schedule(
                ScheduleCreate(
                    name=course,
                    course_id=course,
                    weekdays=list(range(7)),
                    local_time=time(12, 0),
                    timezone="UTC",
                    mode=ScheduleMode.PREVIEW,
                    enabled=True,
                ),
                next_run_at=due,
            )
            for course in ("gone", "spanish")
        }
        queue = _FailingRunQueue(store)
        manager = ScheduleManager(store, queue)  # type: ignore[arg-type]
        manager.tick(datetime(2026, 8, 8, 12, 0, 30, tzinfo=UTC))

        by_schedule = {item.schedule_id: item for item in store.list_schedule_occurrences()}
        assert by_schedule[schedules["gone"].id].status == "failed"
        assert "Unknown course" in by_schedule[schedules["gone"].id].details
        assert by_schedule[schedules["spanish"].id].status == "queued"
        assert len(queue.created) == 1
        # The failed schedule moved on to its next date instead of retrying every tick.
        assert store.get_schedule(schedules["gone"].id).next_run_at > due
    finally:
        store.close()


def test_a_restart_fails_runs_it_left_active_and_keeps_the_rest(tmp_path):
    config = _write_project(tmp_path)
    seed = ControlStore(tmp_path / ".canvas-task-sync" / "control.sqlite3")
    expected: dict[int, RunStatus] = {}
    for left, after in (
        (RunStatus.QUEUED, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.APPLYING, RunStatus.FAILED_PARTIAL),
        (RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_APPROVAL),
    ):
        run_id = seed.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.PREVIEW,
        )
        seed.update_run(run_id, status=left)
        expected[run_id] = after
    seed.close()

    with TestClient(create_web_app(config)) as client:
        for run_id, after in expected.items():
            run = client.get(f"/api/v1/runs/{run_id}").json()
            assert run["status"] == after.value
            interrupted = after != RunStatus.AWAITING_APPROVAL
            assert run["error_code"] == ("interrupted" if interrupted else None)
        # Course deletion is no longer blocked by a run nothing is working on.
        csrf = _csrf(client)
        assert client.delete("/api/v1/courses/spanish", headers=csrf).status_code == 200


def test_clearing_history_is_refused_while_a_run_is_active(tmp_path):
    app = create_web_app(_write_project(tmp_path))
    with TestClient(app) as client:
        store = app.state.runtime.store
        run_id = store.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.PREVIEW,
        )
        store.update_run(run_id, status=RunStatus.RUNNING)
        csrf = _csrf(client)

        refused = client.delete("/api/v1/history", headers=csrf)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "run_active"
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 200

        store.update_run(run_id, status=RunStatus.SUCCEEDED)
        assert client.delete("/api/v1/history", headers=csrf).status_code == 204
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 404


@pytest.mark.parametrize(
    ("dev_mode", "status", "cors_origin"),
    [("", 403, None), ("1", 204, "http://127.0.0.1:5173")],
)
def test_the_vite_dev_origin_is_trusted_only_in_dev_mode(
    tmp_path, monkeypatch, dev_mode, status, cors_origin
):
    monkeypatch.setenv("CANVAS_TASK_SYNC_DEV", dev_mode)
    origin = "http://127.0.0.1:5173"
    with TestClient(create_web_app(_write_project(tmp_path))) as client:
        read = client.get("/api/v1/bootstrap", headers={"origin": origin})
        sent = client.delete("/api/v1/history", headers={**_csrf(client), "origin": origin})
    assert read.headers.get("access-control-allow-origin") == cors_origin
    assert sent.status_code == status


def test_configuration_updates_preserve_comments_and_create_backup(tmp_path):
    config = _write_project(tmp_path)
    service = ConfigurationService(config)
    course = service.load().course("spanish")
    course.name = "Updated Spanish"
    course.ai_instructions = "Skip optional reading tasks."
    course.gemini_model = "gemini-3.5-flash"
    course.gemini_fallback_models = [
        "gemini-3.5-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]
    course.gemini_reasoning = "high"
    from canvas_task_sync.web_models import CourseSave

    service.save_course(CourseSave(id="spanish", settings=course), creating=False)
    text = config.read_text(encoding="utf-8")
    assert "# keep this header comment" in text
    assert "# keep this course comment" in text
    assert "name: Updated Spanish" in text
    assert "ai_instructions: Skip optional reading tasks." in text
    assert "gemini_model: gemini-3.5-flash" in text
    assert "gemini_reasoning: high" in text
    assert service.load().course("spanish").gemini_fallback_models == [
        "gemini-3.5-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]
    assert service.load().course("spanish").ai_instructions == "Skip optional reading tasks."
    assert service.load().course("spanish").gemini_reasoning == "high"
    assert config.with_suffix(".yaml.bak").exists()


def test_configuration_deletes_course_and_preserves_backup(tmp_path):
    config = _write_project(tmp_path)
    service = ConfigurationService(config)

    settings = service.delete_course("spanish")

    assert settings.courses == {}
    assert "spanish:" not in config.read_text(encoding="utf-8")
    assert "spanish:" in config.with_suffix(".yaml.bak").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="does not exist"):
        service.delete_course("spanish")


def test_delete_course_removes_schedules_but_keeps_run_history(tmp_path):
    app = create_web_app(_write_project(tmp_path))
    with TestClient(app) as client:
        headers = _csrf(client)
        schedule = client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "Weekday preview",
                "course_id": "spanish",
                "weekdays": [0, 1, 2, 3, 4],
                "local_time": "19:00",
                "timezone": "America/New_York",
                "mode": "preview",
                "enabled": True,
            },
        )
        assert schedule.status_code == 201
        run_id = app.state.runtime.store.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.PREVIEW,
        )
        blocked = client.delete("/api/v1/courses/spanish", headers=headers)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "course_run_active"

        app.state.runtime.store.update_run(
            run_id,
            status=RunStatus.CANCELLED,
            finished_at=datetime.now(UTC),
        )

        deleted = client.delete("/api/v1/courses/spanish", headers=headers)

        assert deleted.status_code == 200
        assert deleted.json() == []
        assert client.get("/api/v1/schedules").json()["items"] == []
        assert app.state.runtime.store.get_run(run_id, include_events=False) is not None
        assert app.state.runtime.settings.courses == {}


def test_oauth_upload_requires_desktop_client_and_localhost_redirect(tmp_path):
    config = _write_project(tmp_path)
    service = ConfigurationService(config)
    with pytest.raises(ValueError, match="installed"):
        service.save_oauth_client(json.dumps({"web": {}}).encode())
    with pytest.raises(ValueError, match="localhost"):
        service.save_oauth_client(
            json.dumps(
                {
                    "installed": {
                        "client_id": "id",
                        "client_secret": "secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["https://example.com/callback"],
                    }
                }
            ).encode()
        )


def test_api_enforces_host_origin_csrf_and_never_echoes_secret_input(tmp_path):
    app = create_web_app(_write_project(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/v1/overview").status_code == 200
        missing_api = client.get("/api/v1/not-a-real-route")
        assert missing_api.status_code == 404
        assert missing_api.headers["content-type"].startswith("application/json")
        assert missing_api.json()["error"]["code"] == "api_route_not_found"
        rejected = client.post("/api/v1/health-runs")
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "csrf_failed"

        headers = _csrf(client)
        origin_rejected = client.post(
            "/api/v1/health-runs",
            headers={**headers, "origin": "https://attacker.invalid"},
        )
        assert origin_rejected.status_code == 403
        assert origin_rejected.json()["error"]["code"] == "origin_not_allowed"

        host_rejected = client.get("/api/v1/overview", headers={"host": "attacker.invalid"})
        assert host_rejected.status_code == 400
        assert host_rejected.json()["error"]["code"] == "host_not_allowed"

        secret = "not-valid-but-private"
        invalid_secret = client.post(
            "/api/v1/settings/gemini-key",
            headers=headers,
            json={"api_key": secret * 300},
        )
        assert invalid_secret.status_code == 422
        assert secret not in invalid_secret.text
        assert set(invalid_secret.json()) == {"error"}


def test_a_canvas_token_saved_in_settings_replaces_the_old_one_at_once(tmp_path, monkeypatch):
    for name in ("CANVAS_TOKEN", "CANVAS_API_TOKEN", "CANVAS_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config = _write_project(tmp_path)
    env = tmp_path / ".env"
    env.write_text("CANVAS_BASE_URL=https://canvas.example\nCANVAS_TOKEN=old-token-value\n")
    app = create_web_app(config)
    with TestClient(app) as client:
        headers = _csrf(client)
        connections = client.get("/api/v1/settings/connections").json()["connections"]
        assert connections["canvas_configured"] is True

        rejected = client.post(
            "/api/v1/settings/canvas-token",
            headers=headers,
            json={"token": "has a space in it"},
        )
        assert rejected.status_code == 400
        assert "has a space" not in rejected.text

        saved = client.post(
            "/api/v1/settings/canvas-token",
            headers=headers,
            json={"token": "  1234~new-token-value  "},
        )
        assert saved.status_code == 204, saved.text
        assert "new-token-value" not in client.get("/api/v1/settings/connections").text

    from dotenv import dotenv_values

    assert dotenv_values(env) == {
        "CANVAS_BASE_URL": "https://canvas.example",
        "CANVAS_TOKEN": "1234~new-token-value",
    }
    assert (tmp_path / ".env.bak").read_text().endswith("CANVAS_TOKEN=old-token-value\n")
    # The running backend uses the new token without a restart.
    import os

    assert os.environ["CANVAS_TOKEN"] == "1234~new-token-value"


def test_the_canvas_token_check_sends_it_only_to_the_canvas_origin(tmp_path, monkeypatch):
    import requests

    from canvas_task_sync.configuration import load_settings
    from canvas_task_sync.health import canvas_token_health_check

    monkeypatch.setenv("CANVAS_BASE_URL", "https://canvas.example/")
    monkeypatch.setenv("CANVAS_TOKEN", "1234~secret-token")
    calls: list[tuple[str, dict]] = []

    class Answer:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    statuses = iter([200, 401])

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Answer(next(statuses))

    monkeypatch.setattr(requests, "get", fake_get)
    settings = load_settings(_write_project(tmp_path))
    assert canvas_token_health_check(settings).state == HealthState.HEALTHY
    rejected = canvas_token_health_check(settings)
    assert rejected.state == HealthState.ERROR
    assert "rejected" in rejected.summary
    url, kwargs = calls[0]
    assert url == "https://canvas.example/api/v1/users/self"
    assert kwargs["headers"] == {"Authorization": "Bearer 1234~secret-token"}
    assert kwargs["allow_redirects"] is False


def test_the_canvas_token_check_uses_the_address_sync_uses(tmp_path, monkeypatch):
    import requests

    from canvas_task_sync.configuration import load_settings
    from canvas_task_sync.health import canvas_token_health_check

    monkeypatch.setenv("CANVAS_BASE_URL", "https://env.example")
    monkeypatch.setenv("CANVAS_TOKEN", "1234~secret-token")
    urls: list[str] = []

    class Answer:
        status_code = 200

    def fake_get(url, **kwargs):
        urls.append(url)
        return Answer()

    monkeypatch.setattr(requests, "get", fake_get)
    settings = load_settings(_write_project(tmp_path))
    course = settings.courses["spanish"]
    for update in (
        {},  # no course address: .env's
        {"canvas_base_url": "https://course.example"},  # sync ignores it without a course ID
        {"canvas_base_url": "https://course.example", "canvas_course_id": "12345"},
    ):
        settings.courses["spanish"] = course.model_copy(update=update)
        canvas_token_health_check(settings)
    assert urls == [
        "https://env.example/api/v1/users/self",
        "https://env.example/api/v1/users/self",
        "https://course.example/api/v1/users/self",
    ]


def test_a_canvas_redirect_tells_you_to_check_the_address(tmp_path, monkeypatch):
    import requests

    from canvas_task_sync.configuration import load_settings
    from canvas_task_sync.health import canvas_token_health_check

    monkeypatch.setenv("CANVAS_BASE_URL", "http://canvas.example")
    monkeypatch.setenv("CANVAS_TOKEN", "1234~secret-token")

    class Answer:
        status_code = 301

    monkeypatch.setattr(requests, "get", lambda url, **kwargs: Answer())
    check = canvas_token_health_check(load_settings(_write_project(tmp_path)))

    assert check.state == HealthState.ERROR
    assert "CANVAS_BASE_URL" in check.summary


def test_the_canvas_token_is_read_from_the_first_name_that_is_set(monkeypatch):
    from canvas_task_sync.configuration import canvas_token_from_env

    monkeypatch.setenv("CANVAS_TOKEN", "")
    monkeypatch.setenv("CANVAS_API_TOKEN", "second")
    monkeypatch.setenv("CANVAS_API_KEY", "third")
    assert canvas_token_from_env() == "second"
    monkeypatch.setenv("CANVAS_TOKEN", "first")
    assert canvas_token_from_env() == "first"


def test_saving_a_secret_keeps_env_comments_and_stays_owner_only(tmp_path, monkeypatch):
    import os

    from dotenv import dotenv_values

    monkeypatch.setenv("CANVAS_TOKEN", "restored-after-the-test")
    service = ConfigurationService(_write_project(tmp_path))
    env = tmp_path / ".env"
    env.write_text("# Canvas notes\n\nCANVAS_BASE_URL=https://canvas.example\nCANVAS_TOKEN=old\n")
    env.chmod(0o600)

    service.save_canvas_token("1234~new-token-value")

    assert env.read_text().startswith("# Canvas notes\n\nCANVAS_BASE_URL=https://canvas.example\n")
    assert dotenv_values(env)["CANVAS_TOKEN"] == "1234~new-token-value"
    assert (tmp_path / ".env.bak").read_text().endswith("CANVAS_TOKEN=old\n")
    env.unlink()
    service.save_canvas_token("1234~another-token-value")  # a missing .env is created too
    assert dotenv_values(env) == {"CANVAS_TOKEN": "1234~another-token-value"}
    if os.name != "nt":  # Windows has no owner-only mode bits
        assert env.stat().st_mode & 0o777 == 0o600


def test_course_flags_written_the_old_yaml_way_still_load(tmp_path):
    from canvas_task_sync.configuration import load_settings

    config = _write_project(tmp_path)
    config.write_text(config.read_text().replace("enabled: true", "enabled: no"))

    assert load_settings(config).course("spanish").enabled is False


def test_run_event_stream_replays_after_last_event_id(tmp_path):
    app = create_web_app(_write_project(tmp_path))
    with TestClient(app) as client:
        missing = client.get("/api/v1/runs/999/events")
        assert missing.status_code == 404, missing.text

        runtime = app.state.runtime
        run_id = runtime.store.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.PREVIEW,
        )
        runtime.store.add_event(
            run_id,
            stage=RunStage.QUEUED,
            event_type="queued",
            message="Queued",
        )
        runtime.store.add_event(
            run_id,
            stage=RunStage.VALIDATE_CONFIGURATION,
            event_type="stage_completed",
            message="Configuration validated",
        )
        runtime.store.update_run(
            run_id,
            status=RunStatus.SUCCEEDED,
            finished_at=datetime.now(UTC),
        )

        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "id: 2" in response.text
        assert "event: stage_completed" in response.text
        assert "id: 1" not in response.text


def test_manual_web_runs_default_to_auto_apply_and_share_all_operation_id(tmp_path):
    app = create_web_app(_write_project(tmp_path))
    with TestClient(app) as client:
        runtime = app.state.runtime
        captured: list[tuple[object, str]] = []

        def create_without_worker(request, **kwargs):
            operation_id = kwargs["operation_id"]
            captured.append((request, operation_id))
            return runtime.store.create_run(
                operation_id=operation_id,
                course_id=request.course_id,
                trigger=RunTrigger.MANUAL,
                requested_mode=request.mode,
            )

        runtime.runs.create_preview = create_without_worker
        headers = _csrf(client)

        single = client.post("/api/v1/runs", headers=headers, json={"course_id": "spanish"})
        all_courses = client.post("/api/v1/runs/all", headers=headers, json={})

        assert single.status_code == 202
        assert all_courses.status_code == 202
        assert captured[0][0].mode == RunMode.AUTO_APPLY
        assert captured[1][0].mode == RunMode.AUTO_APPLY
        assert captured[0][1] == single.json()["operation_id"]
        assert captured[1][1] == all_courses.json()["operation_id"]


def test_operation_history_and_event_stream_merge_course_runs(tmp_path):
    app = create_web_app(_write_project(tmp_path))
    with TestClient(app) as client:
        runtime = app.state.runtime
        operation_id = "operation-test"
        run_ids = [
            runtime.store.create_run(
                operation_id=operation_id,
                course_id="spanish",
                trigger=RunTrigger.MANUAL,
                requested_mode=RunMode.AUTO_APPLY,
            )
            for _ in range(2)
        ]
        for index, run_id in enumerate(run_ids, start=1):
            runtime.store.add_event(
                run_id,
                stage=RunStage.APPLY_CHANGES,
                event_type="action_applied",
                message=f"Created task {index}",
                metadata={"action": "create", "title": f"Task {index}"},
            )
            runtime.store.update_run(
                run_id,
                status=RunStatus.SUCCEEDED,
                finished_at=datetime.now(UTC),
            )

        operations = client.get("/api/v1/operations").json()
        stream = client.get(f"/api/v1/operations/{operation_id}/events")

        assert operations[0]["id"] == operation_id
        assert operations[0]["run_ids"] == list(reversed(run_ids))
        assert stream.status_code == 200
        assert stream.text.count("event: log") == 2
        assert '"operation_id": "operation-test"' in stream.text
        assert '"event_type": "action_applied"' in stream.text
        assert "event: complete" in stream.text


def test_schedule_crud_disable_course_and_support_bundle_are_safe(tmp_path):
    app = create_web_app(_write_project(tmp_path))
    with TestClient(app) as client:
        headers = _csrf(client)
        created = client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "Weekday preview",
                "course_id": "spanish",
                "weekdays": [0, 1, 2, 3, 4],
                "local_time": "19:00",
                "timezone": "America/New_York",
                "mode": "preview",
                "enabled": True,
            },
        )
        assert created.status_code == 201
        schedule_id = created.json()["id"]

        disabled = client.post(f"/api/v1/schedules/{schedule_id}/disable", headers=headers)
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        enabled = client.post(f"/api/v1/schedules/{schedule_id}/enable", headers=headers)
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True

        courses = client.post("/api/v1/courses/spanish/disable", headers=headers)
        assert courses.status_code == 200
        schedule = client.get("/api/v1/schedules").json()["items"][0]
        assert schedule["enabled"] is False
        assert schedule["next_run_at"] is None

        runtime = app.state.runtime
        run_id = runtime.store.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.PREVIEW,
        )
        runtime.store.add_event(
            run_id,
            stage=RunStage.QUEUED,
            event_type="diagnostic",
            message="Bearer abcdefghijklmnopqrstuvwxyz",
            metadata={"client_secret": "do-not-export"},
        )
        bundle = client.get("/api/v1/diagnostics/support-bundle")
        assert bundle.status_code == 200
        assert "do-not-export" not in bundle.text
        assert "abcdefghijklmnopqrstuvwxyz" not in bundle.text

        assert client.delete(f"/api/v1/schedules/{schedule_id}", headers=headers).status_code == 204


def test_extraction_agent_settings_round_trip_through_the_api(tmp_path):
    config = _write_project(tmp_path)
    app = create_web_app(config)
    with TestClient(app) as client:
        initial = client.get("/api/v1/settings/extraction-agent").json()
        assert initial["settings"] == {"provider": "gemini", "model": None, "effort": "medium"}
        providers = {item["id"]: item for item in initial["providers"]}
        assert [model["id"] for model in providers["claude"]["models"]] == [
            "claude-sonnet-5-5",
            "claude-haiku-4-5-20251001",
            "claude-opus-5-5",
        ]
        assert [model["id"] for model in providers["codex"]["models"]] == [
            "gpt-6-luna",
            "gpt-6.1-sol",
        ]

        headers = _csrf(client)
        assert client.put(
            "/api/v1/settings/extraction-agent",
            json={"provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
        ).status_code == 403
        saved = client.put(
            "/api/v1/settings/extraction-agent",
            headers=headers,
            json={"provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
        )
        assert saved.status_code == 200
        assert saved.json()["label"] == "Claude · Opus 5.5 · high effort"

        rejected = client.put(
            "/api/v1/settings/extraction-agent",
            headers=headers,
            json={"provider": "codex", "model": "claude-opus-5-5", "effort": "high"},
        )
        assert rejected.status_code == 422

        connections = client.get("/api/v1/settings/connections").json()["connections"]
        assert connections["extraction_provider"] == "claude"
        assert connections["extraction_label"] == "Claude · Opus 5.5 · high effort"
        assert any(check["key"] == "extraction_agent" for check in connections["checks"])

    text = config.read_text(encoding="utf-8")
    assert "# keep this header comment" in text
    assert text.index("extraction_agent:") < text.index("courses:")
    agent = ConfigurationService(config).load().extraction_agent
    assert (agent.provider, agent.model, agent.effort) == ("claude", "claude-opus-5-5", "high")
    assert config.with_suffix(".yaml.bak").exists()


def test_agent_sign_in_test_asks_the_agent_without_starting_a_turn(tmp_path, monkeypatch):
    from canvas_task_sync import health
    from canvas_task_sync.agent_status import AgentSignIn

    asked: list[str] = []

    def fake_sign_in(provider: str, **_kwargs) -> AgentSignIn:
        asked.append(provider)
        return AgentSignIn(True, "Signed in · Claude Max")

    monkeypatch.setattr(health, "check_agent_sign_in", fake_sign_in)
    app = create_web_app(_write_project(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/settings/extraction-agent/test",
            headers=_csrf(client),
            json={"provider": "claude"},
        )
    assert response.status_code == 200
    assert response.json()["check"]["state"] == "healthy"
    assert response.json()["check"]["summary"] == "Signed in · Claude Max"
    assert asked == ["claude"]
