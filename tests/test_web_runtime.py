from __future__ import annotations

import json
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from canvas_task_sync.configuration_service import ConfigurationService
from canvas_task_sync.control_store import ControlStore
from canvas_task_sync.redaction import REDACTED, redact_text, sanitize
from canvas_task_sync.run_manager import ScheduleManager, next_schedule_occurrence
from canvas_task_sync.web_app import create_web_app
from canvas_task_sync.web_models import (
    EventLevel,
    RunMode,
    RunStage,
    RunStatus,
    RunTrigger,
    ScheduleCreate,
    ScheduleMode,
)


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
            metadata={"api_key": "should-never-persist", "count": 3},
        )

        replay = store.events_after(run_id, sequence=1)
        assert [event.sequence for event in replay] == [2]
        assert replay[0].metadata == {"api_key": REDACTED, "count": 3}
        store.update_run(run_id, status=RunStatus.SUCCEEDED, finished_at=datetime.now(UTC))
        assert store.get_run(run_id).status == RunStatus.SUCCEEDED
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
    from canvas_task_sync.web_models import CourseSave

    service.save_course(CourseSave(id="spanish", settings=course), creating=False)
    text = config.read_text(encoding="utf-8")
    assert "# keep this header comment" in text
    assert "# keep this course comment" in text
    assert "name: Updated Spanish" in text
    assert config.with_suffix(".yaml.bak").exists()


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
