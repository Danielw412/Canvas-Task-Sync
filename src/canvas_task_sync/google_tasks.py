from __future__ import annotations

from datetime import date
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from canvas_task_sync.models import RemoteTask


class GoogleTasksError(RuntimeError):
    pass


def date_from_google_due(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def google_due(value: date | None) -> str | None:
    if value is None:
        return None
    return f"{value.isoformat()}T00:00:00.000Z"


class GoogleTasksClient:
    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        service: Any | None = None,
    ) -> None:
        self.service = service or build(
            "tasks",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )

    def resolve_task_list(self, title: str) -> tuple[str, str]:
        matches: list[dict[str, Any]] = []
        page_token: str | None = None
        try:
            while True:
                response = (
                    self.service.tasklists()
                    .list(maxResults=100, pageToken=page_token)
                    .execute()
                )
                matches.extend(
                    item
                    for item in response.get("items", [])
                    if str(item.get("title", "")).casefold() == title.casefold()
                )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except Exception as error:
            raise GoogleTasksError("Could not list Google Tasks lists.") from error

        if not matches:
            raise GoogleTasksError(
                f"Google Tasks list '{title}' was not found; it will not be created automatically."
            )
        if len(matches) > 1:
            raise GoogleTasksError(
                f"More than one Google Tasks list is named '{title}'. Rename one before syncing."
            )
        return str(matches[0]["id"]), str(matches[0].get("title", title))

    def list_tasks(self, tasklist_id: str) -> list[RemoteTask]:
        items: list[RemoteTask] = []
        page_token: str | None = None
        try:
            while True:
                response = (
                    self.service.tasks()
                    .list(
                        tasklist=tasklist_id,
                        maxResults=100,
                        pageToken=page_token,
                        showCompleted=True,
                        showDeleted=True,
                        showHidden=True,
                    )
                    .execute()
                )
                for item in response.get("items", []):
                    items.append(
                        RemoteTask(
                            id=str(item["id"]),
                            title=str(item.get("title", "")),
                            notes=str(item.get("notes", "")),
                            due=item.get("due"),
                            status=str(item.get("status", "needsAction")),
                            completed=item.get("completed"),
                            deleted=bool(item.get("deleted", False)),
                            hidden=bool(item.get("hidden", False)),
                        )
                    )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except Exception as error:
            raise GoogleTasksError("Could not read tasks from the configured list.") from error
        return items

    def create_task(
        self,
        tasklist_id: str,
        *,
        title: str,
        notes: str = "",
        due_date: date | None,
    ) -> RemoteTask:
        body: dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due_date:
            body["due"] = google_due(due_date)
        try:
            item = (
                self.service.tasks()
                .insert(tasklist=tasklist_id, body=body)
                .execute()
            )
        except Exception as error:
            raise GoogleTasksError(f"Could not create Google Task '{title}'.") from error
        return RemoteTask(
            id=str(item["id"]),
            title=str(item.get("title", title)),
            notes=str(item.get("notes", notes)),
            due=item.get("due"),
            status=str(item.get("status", "needsAction")),
            completed=item.get("completed"),
        )

    def get_task(self, tasklist_id: str, task_id: str) -> RemoteTask:
        try:
            item = self.service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
        except Exception as error:
            raise GoogleTasksError(f"Could not verify Google Task '{task_id}'.") from error
        return RemoteTask(
            id=str(item.get("id", task_id)),
            title=str(item.get("title", "")),
            notes=str(item.get("notes", "")),
            due=item.get("due"),
            status=str(item.get("status", "needsAction")),
            completed=item.get("completed"),
            deleted=bool(item.get("deleted", False)),
            hidden=bool(item.get("hidden", False)),
        )

    def patch_due(self, tasklist_id: str, task_id: str, due_date: date | None) -> None:
        try:
            self.service.tasks().patch(
                tasklist=tasklist_id,
                task=task_id,
                body={"due": google_due(due_date)},
            ).execute()
        except Exception as error:
            raise GoogleTasksError(
                f"Could not repair the due date for task '{task_id}'."
            ) from error

    def verify_due(self, tasklist_id: str, task_id: str, due_date: date | None) -> RemoteTask:
        verified = self.get_task(tasklist_id, task_id)
        if date_from_google_due(verified.due) == due_date:
            return verified
        self.patch_due(tasklist_id, task_id, due_date)
        verified = self.get_task(tasklist_id, task_id)
        if date_from_google_due(verified.due) != due_date:
            expected = due_date.isoformat() if due_date else "no due date"
            actual = date_from_google_due(verified.due)
            raise GoogleTasksError(
                f"Google Task '{task_id}' did not retain due date {expected}; "
                f"server returned {actual.isoformat() if actual else 'no due date'}."
            )
        return verified

    def update_notes(self, tasklist_id: str, task_id: str, notes: str) -> RemoteTask:
        try:
            item = self.service.tasks().patch(
                tasklist=tasklist_id,
                task=task_id,
                body={"notes": notes},
            ).execute()
        except Exception as error:
            raise GoogleTasksError(f"Could not clean notes for Google Task '{task_id}'.") from error
        return RemoteTask(
            id=str(item.get("id", task_id)),
            title=str(item.get("title", "")),
            notes=str(item.get("notes", notes)),
            due=item.get("due"),
            status=str(item.get("status", "needsAction")),
            completed=item.get("completed"),
        )

    def update_task(
        self,
        tasklist_id: str,
        task_id: str,
        *,
        title: str,
        notes: str,
        due_date: date | None,
    ) -> RemoteTask:
        # PATCH deliberately omits status and every user-controlled field except title/notes/due.
        body: dict[str, Any] = {
            "title": title,
            "notes": notes,
            "due": google_due(due_date),
        }
        try:
            item = (
                self.service.tasks()
                .patch(tasklist=tasklist_id, task=task_id, body=body)
                .execute()
            )
        except Exception as error:
            raise GoogleTasksError(f"Could not update Google Task '{title}'.") from error
        return RemoteTask(
            id=str(item.get("id", task_id)),
            title=str(item.get("title", title)),
            notes=str(item.get("notes", notes)),
            due=item.get("due"),
            status=str(item.get("status", "needsAction")),
            completed=item.get("completed"),
        )

    def set_task_completed(
        self,
        tasklist_id: str,
        task_id: str,
        *,
        completed: bool,
    ) -> RemoteTask:
        body: dict[str, Any] = {"status": "completed" if completed else "needsAction"}
        if not completed:
            body["completed"] = None
        try:
            item = self.service.tasks().patch(
                tasklist=tasklist_id,
                task=task_id,
                body=body,
            ).execute()
        except Exception as error:
            raise GoogleTasksError(f"Could not update Google Task status '{task_id}'.") from error
        return RemoteTask(
            id=str(item.get("id", task_id)),
            title=str(item.get("title", "")),
            notes=str(item.get("notes", "")),
            due=item.get("due"),
            status=str(item.get("status", body["status"])),
            completed=item.get("completed"),
        )
