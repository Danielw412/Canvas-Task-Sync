from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from canvas_task_sync.auth import load_google_credentials
from canvas_task_sync.configuration import ProjectSettings
from canvas_task_sync.google_tasks import GoogleTasksClient, GoogleTasksError
from canvas_task_sync.models import RemoteTask, StateRecord, TaskType
from canvas_task_sync.state import StateStore
from canvas_task_sync.web_models import (
    TrackedTaskCanvasIdentity,
    TrackedTaskCourse,
    TrackedTaskGoogleIdentity,
    TrackedTaskSource,
    TrackedTaskView,
)

CANVAS_ASSIGNMENT_PATH = re.compile(r"/courses/(?P<course_id>\d+)/assignments/(?P<id>\d+)")


class TrackedTaskReader:
    """Read canonical Task Sync mappings and merge their live Google Task status."""

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

    def list(
        self,
        *,
        completed: bool | None = None,
        course_id: str | None = None,
    ) -> list[TrackedTaskView]:
        with StateStore(self.settings.resolved_state_path, writable=False) as state:
            records = state.all_records()
        if course_id is not None:
            records = [record for record in records if record.course_id == course_id]
        views = self._views(records)
        if completed is not None:
            if completed:
                views = [view for view in views if view.completed is True]
            else:
                # The unfinished-work feed is intentionally strict: only a live Google Task
                # status of needsAction is proof that an item is still unfinished. Unknown,
                # missing, or unavailable statuses must never resurrect a checked-off task.
                views = [view for view in views if view.completed is False]
        return sorted(
            views,
            key=lambda view: (
                view.due_date is None,
                view.due_date.isoformat() if view.due_date else "",
                view.course.name.casefold(),
                view.display_title.casefold(),
                view.logical_id,
            ),
        )

    def get(self, logical_id: str) -> TrackedTaskView | None:
        with StateStore(self.settings.resolved_state_path, writable=False) as state:
            record = state.get_record(logical_id)
        if record is None:
            return None
        return self._views([record])[0]

    def _views(self, records: list[StateRecord]) -> list[TrackedTaskView]:
        if not records:
            return []
        tasklist_ids = sorted({record.tasklist_id for record in records if record.tasklist_id})
        remote_by_key: dict[tuple[str, str], RemoteTask] = {}
        remote_status_available = True
        try:
            credentials = self.credentials_loader(self.settings.root_dir, interactive=False)
            client = self.tasks_client_factory(credentials)
            for tasklist_id in tasklist_ids:
                for remote in client.list_tasks(tasklist_id):
                    remote_by_key[(tasklist_id, remote.id)] = remote
        except (GoogleTasksError, RuntimeError):
            remote_status_available = False
        return [
            self._view(
                record,
                remote_by_key.get((record.tasklist_id or "", record.google_task_id or "")),
                remote_status_available=remote_status_available,
            )
            for record in records
        ]

    def _view(
        self,
        record: StateRecord,
        remote: RemoteTask | None,
        *,
        remote_status_available: bool,
    ) -> TrackedTaskView:
        course = self.settings.courses.get(record.course_id)
        course_name = course.name if course else record.course_id
        prefix = course.prefix if course else record.course_id
        canvas = _canvas_identity(record, course)
        tasklist_title = record.tasklist_title or _tasklist_title(record, course)
        completed = None
        completion_status = "unknown"
        if remote is not None and not remote.deleted:
            completed = remote.status == "completed"
            completion_status = "completed" if completed else "incomplete"
        elif not remote_status_available:
            completion_status = "unavailable"
        elif record.google_task_id:
            completion_status = "missing"

        source_url = record.source_url or record.assignment_url
        return TrackedTaskView(
            logical_id=record.logical_id,
            course=TrackedTaskCourse(
                id=record.course_id,
                name=course_name,
                prefix=prefix,
                canvas_course_id=canvas.course_id,
                canvas_base_url=course.canvas_base_url if course else None,
                canvas_url=canvas.course_url,
            ),
            title=record.title,
            display_title=_display_title(record.title, prefix),
            details=record.details,
            due_date=record.due_date,
            completed=completed,
            completion_status=completion_status,
            classification=record.classification,
            task_type=record.task_type,
            action_kind=record.action_kind,
            due_basis=record.due_basis,
            due_uncertain=record.due_uncertain,
            due_uncertain_reason=record.due_uncertain_reason,
            source_date=record.source_date,
            historical=record.historical,
            manually_managed=record.manually_managed,
            google_task=TrackedTaskGoogleIdentity(
                task_id=record.google_task_id,
                tasklist_id=record.tasklist_id,
                tasklist_title=tasklist_title,
                status=remote.status if remote else completion_status,
                completed_at=remote.completed if remote else None,
                deleted=remote.deleted if remote else False,
                hidden=remote.hidden if remote else False,
            ),
            source=TrackedTaskSource(
                key=record.source_key,
                type=_source_type(record.source_key, source_url),
                url=source_url,
                anchor=record.anchor,
                text=record.source_text,
                assignment_url=record.assignment_url,
            ),
            canvas=canvas,
        )


def _display_title(title: str, prefix: str) -> str:
    return re.sub(rf"^\[{re.escape(prefix)}\]\s*", "", title, flags=re.IGNORECASE).strip()


def _tasklist_title(record: StateRecord, course: Any | None) -> str | None:
    if course is None:
        return None
    if record.task_type in {TaskType.QUIZ, TaskType.TEST}:
        return course.assessment_task_list
    return course.task_list


def _source_type(source_key: str, source_url: str | None) -> str:
    lowered = source_key.casefold()
    if lowered.startswith("manual:"):
        return "manual"
    for candidate in ("canvas", "google_slides", "google_docs", "google_sheets", "browser"):
        if candidate in lowered:
            return candidate
    if source_url and "instructure.com" in urlparse(source_url).netloc.casefold():
        return "canvas"
    return "unknown"


def _canvas_identity(record: StateRecord, course: Any | None) -> TrackedTaskCanvasIdentity:
    assignment_url = record.assignment_url
    if (
        assignment_url is None
        and record.source_url
        and CANVAS_ASSIGNMENT_PATH.search(urlparse(record.source_url).path)
    ):
        assignment_url = record.source_url
    match = CANVAS_ASSIGNMENT_PATH.search(urlparse(assignment_url).path) if assignment_url else None
    canvas_course_id = (
        match.group("course_id")
        if match
        else (course.canvas_course_id if course is not None else None)
    )
    assignment_id = match.group("id") if match else None
    base_url = course.canvas_base_url if course is not None else None
    if base_url is None and assignment_url:
        parsed = urlparse(assignment_url)
        if parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
    course_url = (
        f"{base_url.rstrip('/')}/courses/{canvas_course_id}"
        if base_url and canvas_course_id
        else None
    )
    return TrackedTaskCanvasIdentity(
        course_id=canvas_course_id,
        assignment_id=assignment_id,
        course_url=course_url,
        assignment_url=assignment_url,
    )
