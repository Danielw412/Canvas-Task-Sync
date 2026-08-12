from canvas_task_sync.app import _today


def test_configured_iana_timezone_is_available_on_windows():
    assert _today("America/New_York").year >= 2026
