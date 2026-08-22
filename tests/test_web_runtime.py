from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from canvas_task_sync.configuration import NoFallbackSourceSettings
from canvas_task_sync.configuration_service import ConfigurationService
from canvas_task_sync.control_store import ControlStore
from canvas_task_sync.redaction import REDACTED, redact_text, sanitize
from canvas_task_sync.run_manager import (
    ScheduleManager,
    StoreProgressSink,
    next_schedule_occurrence,
)
from canvas_task_sync.web_app import create_web_app
from canvas_task_sync.web_models import (
    CourseSave,
    EventLevel,
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


def test_configuration_updates_preserve_comments_and_create_backup(tmp_path):
    config = _write_project(tmp_path)
    service = ConfigurationService(config)
    course = service.load().course("spanish")
    course.name = "Updated Spanish"
    course.ai_instructions = "Skip optional reading tasks."
    from canvas_task_sync.web_models import CourseSave

    service.save_course(CourseSave(id="spanish", settings=course), creating=False)
    text = config.read_text(encoding="utf-8")
    assert "# keep this header comment" in text
    assert "# keep this course comment" in text
    assert "name: Updated Spanish" in text
    assert "ai_instructions: Skip optional reading tasks." in text
    assert service.load().course("spanish").ai_instructions == "Skip optional reading tasks."
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
