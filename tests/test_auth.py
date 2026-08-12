from canvas_task_sync.auth import SCOPES, SLIDES_READONLY_SCOPE, TASKS_SCOPE


def test_oauth_uses_only_tasks_and_target_slides_read_scope():
    assert SCOPES == [TASKS_SCOPE, SLIDES_READONLY_SCOPE]
    assert all("drive" not in scope for scope in SCOPES)
