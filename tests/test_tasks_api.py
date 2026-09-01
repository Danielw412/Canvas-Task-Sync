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


class MutableTasksClient:
    def __init__(self) -> None:
        self.tasks: dict[str, RemoteTask] = {}
        self.updates: list[tuple[str, str, str]] = []

    def resolve_task_list(self, title: str) -> tuple[str, str]:
        return ("list-tests", title) if title == "Tests" else ("list-1", title)

    def list_tasks(self, tasklist_id: str) -> list[RemoteTask]:
        return [
            task.model_copy(update={"tasklist_id": tasklist_id, "tasklist_title": "School"})
            for task in self.tasks.values()
        ]

    def create_task(self, tasklist_id: str, *, title: str, notes: str, due_date) -> RemoteTask:
        task = RemoteTask(
            id="manual-google-1",
            title=title,
            notes=notes,
            due=f"{due_date.isoformat()}T00:00:00.000Z" if due_date else None,
        )
        self.tasks[task.id] = task
        self.updates.append(("create", tasklist_id, title))
        return task

    def get_task(self, tasklist_id: str, task_id: str) -> RemoteTask:
        return self.tasks[task_id]

    def verify_due(self, tasklist_id: str, task_id: str, due_date) -> RemoteTask:
        return self.tasks[task_id]

    def update_task(
        self,
        tasklist_id: str,
        task_id: str,
        *,
        title: str,
        notes: str,
        due_date,
    ) -> RemoteTask:
        current = self.tasks[task_id]
        updated = current.model_copy(update={
            "title": title,
            "notes": notes,
            "due": f"{due_date.isoformat()}T00:00:00.000Z" if due_date else None,
        })
        self.tasks[task_id] = updated
        self.updates.append(("update", tasklist_id, title))
        return updated

    def set_task_completed(
        self,
        tasklist_id: str,
        task_id: str,
        *,
        completed: bool,
    ) -> RemoteTask:
        updated = self.tasks[task_id].model_copy(update={
            "status": "completed" if completed else "needsAction",
            "completed": "2026-08-31T20:00:00.000Z" if completed else None,
        })
        self.tasks[task_id] = updated
        return updated


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


def test_manual_task_create_and_edit_write_google_and_persist_override(tmp_path: Path) -> None:
    config_path = _write_project(tmp_path)
    app = create_web_app(config_path)
    tasks = MutableTasksClient()
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.manual_tasks.credentials_loader = lambda *_args, **_kwargs: object()
        runtime.manual_tasks.tasks_client_factory = lambda _credentials: tasks
        runtime.tracked_tasks.credentials_loader = lambda *_args, **_kwargs: object()
        runtime.tracked_tasks.tasks_client_factory = lambda _credentials: tasks
        csrf = client.get("/api/v1/bootstrap").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        created = client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "course_id": "physics",
                "title": "Lab report",
                "details": "Finish the analysis and conclusion.",
                "due_date": "2026-09-03",
                "completed": False,
                "classification": "homework",
                "task_type": "assignment",
                "action_kind": "write",
                "source_url": "https://school.instructure.com/courses/11126",
            },
        )

        assert created.status_code == 201
        logical_id = created.json()["logical_id"]
        assert created.json()["display_title"] == "Lab report"
        assert created.json()["manually_managed"] is True
        assert created.json()["source"]["type"] == "manual"
        assert tasks.tasks["manual-google-1"].title == "[PHYSICS] Lab report"

        edited = client.put(
            f"/api/v1/tasks/{logical_id}",
            headers=headers,
            json={
                "course_id": "physics",
                "title": "Revised lab report",
                "details": "Add error analysis and submit the final PDF.",
                "due_date": "2026-09-04",
                "completed": True,
                "classification": "classwork",
                "task_type": "assignment",
                "action_kind": "submit",
                "assignment_url": "https://school.instructure.com/courses/11126/assignments/99",
            },
        )

    assert edited.status_code == 200
    assert edited.json()["display_title"] == "Revised lab report"
    assert edited.json()["details"] == "Add error analysis and submit the final PDF."
    assert edited.json()["due_date"] == "2026-09-04"
    assert edited.json()["completed"] is True
    assert tasks.tasks["manual-google-1"].status == "completed"
    with StateStore(tmp_path / ".canvas-task-sync" / "state.sqlite3", writable=False) as state:
        record = state.get_record(logical_id)
    assert record is not None
    assert record.manually_managed is True
    assert record.title == "[PHYSICS] Revised lab report"
