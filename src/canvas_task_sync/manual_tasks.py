from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from typing import Any

from canvas_task_sync.auth import load_google_credentials
from canvas_task_sync.configuration import ProjectSettings
from canvas_task_sync.google_tasks import GoogleTasksClient, GoogleTasksError
from canvas_task_sync.models import StateRecord, TaskType
from canvas_task_sync.state import StateStore
from canvas_task_sync.web_models import ManualTaskSave


class ManualTaskError(ValueError):
    pass


class ManualTaskService:
    """Apply explicit user task edits to Google Tasks and the canonical local mapping."""

    def __init__(
        self,
        settings: ProjectSettings,
        *,
        credentials_loader: Callable[..., Any] = load_google_credentials,
        tasks_client_factory: Callable[[Any], GoogleTasksClient] = GoogleTasksClient,
    ) -> None:
        self.settings = settings
        self.credentials_loader = credentials_loader
        self.tasks_client_factory = tasks_client_factory

    def create(self, payload: ManualTaskSave) -> str:
        course = self._course(payload.course_id)
        client = self._client()
        tasklist_title = self._destination(course, payload.task_type)
        tasklist_id, resolved_title = client.resolve_task_list(tasklist_title)
        title = _prefixed_title(payload.title, course.prefix)
        remote = client.create_task(
            tasklist_id,
            title=title,
            notes=payload.details,
            due_date=payload.due_date,
        )
        client.verify_due(tasklist_id, remote.id, payload.due_date)
        if payload.completed:
            client.set_task_completed(tasklist_id, remote.id, completed=True)
        logical_id = str(uuid.uuid4())
        source_key = f"manual:{logical_id}"
        record = StateRecord(
            logical_id=logical_id,
            course_id=payload.course_id,
            source_key=source_key,
            source_url=payload.source_url,
            assignment_url=payload.assignment_url,
            anchor=source_key,
            ordinal=0,
            fingerprint=_fingerprint(payload),
            source_text=payload.details,
            title=title,
            details=payload.details,
            classification=payload.classification,
            task_type=payload.task_type,
            action_kind=payload.action_kind,
            due_date=payload.due_date.isoformat() if payload.due_date else None,
            due_basis="manual",
            google_task_id=remote.id,
            tasklist_id=tasklist_id,
            tasklist_title=resolved_title,
            manually_managed=True,
        )
        with StateStore(self.settings.resolved_state_path, writable=True) as state:
            state.upsert_record(record)
        return logical_id

    def update(self, logical_id: str, payload: ManualTaskSave) -> None:
        with StateStore(self.settings.resolved_state_path, writable=False) as state:
            record = state.get_record(logical_id)
        if record is None:
            raise ManualTaskError("Tracked task was not found.")
        if payload.course_id != record.course_id:
            raise ManualTaskError("A task's course cannot be changed after creation.")
        if not record.tasklist_id or not record.google_task_id:
            raise ManualTaskError("This task is not linked to a writable Google Task.")
        course = self._course(record.course_id)
        client = self._client()
        existing = client.get_task(record.tasklist_id, record.google_task_id)
        if existing.deleted:
            raise ManualTaskError("The linked Google Task was deleted and cannot be edited.")
        title = _prefixed_title(payload.title, course.prefix)
        client.update_task(
            record.tasklist_id,
            record.google_task_id,
            title=title,
            notes=payload.details,
            due_date=payload.due_date,
        )
        client.verify_due(record.tasklist_id, record.google_task_id, payload.due_date)
        if (existing.status == "completed") != payload.completed:
            client.set_task_completed(
                record.tasklist_id,
                record.google_task_id,
                completed=payload.completed,
            )
        updated = record.model_copy(
            update={
                "source_url": payload.source_url,
                "assignment_url": payload.assignment_url,
                "source_text": payload.details,
                "title": title,
                "details": payload.details,
                "classification": payload.classification,
                "task_type": payload.task_type,
                "action_kind": payload.action_kind,
                "due_date": payload.due_date.isoformat() if payload.due_date else None,
                "due_basis": "manual",
                "due_uncertain": False,
                "due_uncertain_reason": None,
                "fingerprint": _fingerprint(payload),
                "manually_managed": True,
            }
        )
        with StateStore(self.settings.resolved_state_path, writable=True) as state:
            state.upsert_record(updated)

    def _client(self) -> GoogleTasksClient:
        try:
            credentials = self.credentials_loader(self.settings.root_dir, interactive=False)
            return self.tasks_client_factory(credentials)
        except (GoogleTasksError, RuntimeError) as error:
            raise ManualTaskError(str(error)) from error

    def _course(self, course_id: str) -> Any:
        course = self.settings.courses.get(course_id)
        if course is None:
            raise ManualTaskError("Choose a configured course.")
        return course

    @staticmethod
    def _destination(course: Any, task_type: TaskType) -> str:
        if task_type in {TaskType.QUIZ, TaskType.TEST}:
            return course.assessment_task_list
        return course.task_list


def _prefixed_title(title: str, prefix: str) -> str:
    cleaned = title.strip()
    marker = f"[{prefix}]"
    if cleaned.casefold() == marker.casefold() or cleaned.casefold().startswith(
        f"{marker} ".casefold()
    ):
        return cleaned
    return f"{marker} {cleaned}"


def _fingerprint(payload: ManualTaskSave) -> str:
    value = "|".join(
        [
            payload.course_id,
            payload.title.strip(),
            payload.details.strip(),
            payload.due_date.isoformat() if payload.due_date else "",
            payload.task_type.value,
        ]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
