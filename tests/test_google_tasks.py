from __future__ import annotations

from datetime import date

from canvas_task_sync.google_tasks import GoogleTasksClient, date_from_google_due, google_due


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _PagedListResource:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        token = kwargs.get("pageToken")
        return _Request(self.pages[token])


class _TasksResource(_PagedListResource):
    def __init__(self, pages):
        super().__init__(pages)
        self.patch_calls = []

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        return _Request({"id": kwargs["task"], **kwargs["body"]})


class _Service:
    def __init__(self, tasklists, tasks):
        self.tasklists_resource = _PagedListResource(tasklists)
        self.tasks_resource = _TasksResource(tasks)

    def tasklists(self):
        return self.tasklists_resource

    def tasks(self):
        return self.tasks_resource


def test_task_list_and_tasks_are_fully_paginated():
    service = _Service(
        tasklists={
            None: {"items": [{"id": "other", "title": "Other"}], "nextPageToken": "p2"},
            "p2": {"items": [{"id": "school", "title": "School"}]},
        },
        tasks={
            None: {"items": [{"id": "one", "title": "One"}], "nextPageToken": "p2"},
            "p2": {"items": [{"id": "two", "title": "Two", "status": "completed"}]},
        },
    )
    client = GoogleTasksClient(service=service)
    tasklist_id, title = client.resolve_task_list("school")
    tasks = client.list_tasks(tasklist_id)

    assert (tasklist_id, title) == ("school", "School")
    assert [task.id for task in tasks] == ["one", "two"]
    assert service.tasks_resource.calls[0]["showCompleted"] is True
    assert service.tasks_resource.calls[0]["showDeleted"] is True
    assert service.tasks_resource.calls[0]["showHidden"] is True
    assert [call["pageToken"] for call in service.tasks_resource.calls] == [None, "p2"]


def test_patch_preserves_completion_by_omitting_status():
    service = _Service(
        tasklists={None: {"items": []}},
        tasks={None: {"items": []}},
    )
    client = GoogleTasksClient(service=service)
    client.update_task(
        "school",
        "task-id",
        title="[SPANISH] Task",
        notes="notes",
        due_date=date(2026, 8, 12),
    )
    body = service.tasks_resource.patch_calls[0]["body"]
    assert body == {
        "title": "[SPANISH] Task",
        "notes": "notes",
        "due": "2026-08-12T00:00:00.000Z",
    }
    assert "status" not in body


def test_google_due_is_date_only_semantics():
    assert google_due(date(2026, 8, 12)) == "2026-08-12T00:00:00.000Z"
    assert date_from_google_due("2026-08-12T00:00:00.000Z") == date(2026, 8, 12)

