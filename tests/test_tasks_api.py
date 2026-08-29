from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from canvas_task_sync.models import (
    ActionKind,
    RemoteTask,
    StateRecord,
    TaskClassification,
    TaskType,
)
from canvas_task_sync.state import StateStore
from canvas_task_sync.web_app import create_web_app


def _write_project(root: Path) -> Path:
    config = root / "config" / "courses.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """version: 1
state_path: .canvas-task-sync/state.sqlite3
gemini_model: test-model
courses:
  physics:
    enabled: true
    name: AP Physics C
    prefix: PHYSICS
    task_list: School
    assessment_task_list: Tests
    timezone: America/New_York
    meeting_days: [mon, tue, wed, thu, fri]
    canvas_course_id: "11126"
    canvas_base_url: https://school.instructure.com
    source:
      type: google_slides
      url: https://docs.google.com/presentation/d/fixture/edit
      page_id: slide-1
      extraction:
        mode: text
""",
        encoding="utf-8",
    )
    return config


class FakeTasksClient:
    def list_tasks(self, tasklist_id: str) -> list[RemoteTask]:
        assert tasklist_id == "list-1"
        return [
            RemoteTask(id="google-1", title="Unit 1 HW", status="needsAction"),
            RemoteTask(
                id="google-2",
                title="Quiz",
                status="completed",
                completed="2026-08-27T13:00:00.000Z",
            ),
        ]


def _record(logical_id: str, google_task_id: str, *, task_type: TaskType) -> StateRecord:
    return StateRecord(
        logical_id=logical_id,
        course_id="physics",
        source_key="google_slides:fixture:slide-1",
        source_url="https://docs.google.com/presentation/d/fixture/edit#slide=id.slide-1",
        assignment_url=(
            f"https://school.instructure.com/courses/11126/assignments/{logical_id[-1]}"
        ),
        anchor="slide-1:block-2",
        ordinal=0,
        fingerprint=f"fingerprint-{logical_id}",
        source_text="Complete the assigned problems.",
        title="[PHYSICS] Unit 1 HW" if task_type == TaskType.ASSIGNMENT else "[PHYSICS] Quiz",
        details="Problems 15, 17, and 21.",
        classification=TaskClassification.HOMEWORK,
        task_type=task_type,
        action_kind=ActionKind.COMPLETE,
        due_date="2026-08-28",
        due_basis="explicit date",
        google_task_id=google_task_id,
        tasklist_id="list-1",
        tasklist_title="School" if task_type == TaskType.ASSIGNMENT else "Tests",
    )


def test_tasks_endpoints_merge_state_with_google_completion(tmp_path: Path) -> None:
    config_path = _write_project(tmp_path)
    state_path = tmp_path / ".canvas-task-sync" / "state.sqlite3"
    with StateStore(state_path, writable=True) as state:
        state.upsert_record(_record("logical-1", "google-1", task_type=TaskType.ASSIGNMENT))
        state.upsert_record(_record("logical-2", "google-2", task_type=TaskType.QUIZ))

    app = create_web_app(config_path)
    with TestClient(app) as client:
        app.state.runtime.tracked_tasks.credentials_loader = lambda *_args, **_kwargs: object()
        app.state.runtime.tracked_tasks.tasks_client_factory = (
            lambda _credentials: FakeTasksClient()
        )

        unfinished = client.get("/api/v1/tasks?completed=false")
        completed = client.get("/api/v1/tasks?completed=true")
        detail = client.get("/api/v1/tasks/logical-1")
        missing = client.get("/api/v1/tasks/not-real")

    assert unfinished.status_code == 200
    assert [item["logical_id"] for item in unfinished.json()] == ["logical-1"]
    assert completed.status_code == 200
    assert [item["logical_id"] for item in completed.json()] == ["logical-2"]
    assert detail.status_code == 200
    assert detail.json()["display_title"] == "Unit 1 HW"
    assert detail.json()["completed"] is False
    assert detail.json()["google_task"] == {
        "task_id": "google-1",
        "tasklist_id": "list-1",
        "tasklist_title": "School",
        "status": "needsAction",
        "completed_at": None,
        "deleted": False,
        "hidden": False,
    }
    assert detail.json()["canvas"]["assignment_id"] == "1"
    assert detail.json()["canvas"]["course_id"] == "11126"
    assert detail.json()["source"]["text"] == "Complete the assigned problems."
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "task_not_found"


def test_unfinished_tasks_exclude_unknown_google_status(tmp_path: Path) -> None:
    config_path = _write_project(tmp_path)
    state_path = tmp_path / ".canvas-task-sync" / "state.sqlite3"
    with StateStore(state_path, writable=True) as state:
        state.upsert_record(_record("logical-1", "google-1", task_type=TaskType.ASSIGNMENT))

    app = create_web_app(config_path)
    with TestClient(app) as client:
        app.state.runtime.tracked_tasks.credentials_loader = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        )
        unfinished = client.get("/api/v1/tasks?completed=false")
        detail = client.get("/api/v1/tasks/logical-1")

    assert unfinished.status_code == 200
    assert unfinished.json() == []
    assert detail.status_code == 200
    assert detail.json()["completed"] is None
    assert detail.json()["completion_status"] == "unavailable"
