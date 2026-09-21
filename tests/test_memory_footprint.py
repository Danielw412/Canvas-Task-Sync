"""Keep the backend's idle footprint from creeping back up.

Two things hold it down: heavy Google libraries that load only when something actually
uses them, and freed pages handed back to the OS once work finishes.  Both are easy to
undo by accident with an innocuous-looking import or an early return, so both are
pinned here.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from canvas_task_sync.memory import TRIM_SUPPORTED, release_memory

# Each costs real resident memory and is needed only on a specific code path:
# oauthlib when someone authorizes, googleapiclient once a client is built, genai
# during extraction.
DEFERRED_MODULES = [
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery",
    "google.genai",
    # The whole pipeline belongs to the worker process now.
    "canvas_task_sync.sync_service",
    "canvas_task_sync.gemini",
    "canvas_task_sync.sources",
    "canvas_task_sync.run_executor",
]


def test_importing_the_web_app_does_not_pull_in_the_heavy_google_libraries():
    # A subprocess is the only honest check: the test session has already imported
    # these for other tests.
    probe = (
        "import sys;"
        "import canvas_task_sync.web_app;"
        f"print(','.join(m for m in {DEFERRED_MODULES!r} if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).parent.parent,
    )
    eagerly_loaded = [name for name in result.stdout.strip().split(",") if name]
    assert eagerly_loaded == [], (
        f"{eagerly_loaded} now load at import time. Move the import into the function "
        "that needs it; each one costs several MB in every idle backend."
    )


def test_serving_the_dashboard_does_not_pull_in_the_heavy_google_libraries(tmp_path):
    config = tmp_path / "config" / "courses.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """version: 1
state_path: .canvas-task-sync/state.sqlite3
gemini_model: test-model
courses: {}
""",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.py"
    probe.write_text(
        f"""import sys
from pathlib import Path

from fastapi.testclient import TestClient

from canvas_task_sync.web_app import create_web_app

with TestClient(create_web_app(Path({str(config)!r}))) as client:
    client.get("/api/v1/overview")
    client.get("/api/v1/settings/connections")
print(",".join(m for m in {DEFERRED_MODULES!r} if m in sys.modules))
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    assert result.returncode == 0, result.stderr
    eagerly_loaded = [name for name in result.stdout.strip().split(",") if name]
    assert eagerly_loaded == [], f"{eagerly_loaded} load just from serving the dashboard."


def test_release_memory_is_safe_to_call_anywhere():
    # It is advisory: unsupported platforms get the collection and a False, never a raise.
    assert release_memory() in {True, False}
    assert release_memory() in {True, False}


@pytest.mark.skipif(not TRIM_SUPPORTED, reason="malloc_trim is glibc-only")
def test_release_memory_reports_pages_returned_after_a_large_allocation():
    ballast = [bytearray(1024 * 1024) for _ in range(64)]
    del ballast
    assert release_memory() is True


def _manager(tmp_path):
    from canvas_task_sync.configuration import ProjectSettings
    from canvas_task_sync.control_store import ControlStore
    from canvas_task_sync.run_manager import RunManager

    settings = ProjectSettings(
        root_dir=tmp_path,
        state_path=Path(".canvas-task-sync/state.sqlite3"),
        courses={},
    )
    store = ControlStore(tmp_path / "control.sqlite3")
    manager = RunManager(
        store,
        settings,
        config_path=tmp_path / "config" / "courses.yaml",
    )
    return manager, store


def test_draining_the_queue_reclaims_and_releases_the_worker_process(tmp_path, monkeypatch):
    from canvas_task_sync import run_manager as run_manager_module

    reclaims: list[int] = []
    monkeypatch.setattr(run_manager_module, "release_memory", lambda: reclaims.append(1))
    manager, store = _manager(tmp_path)
    dispatched: list[int] = []
    idle_shutdowns: list[int] = []

    async def scenario() -> None:
        async def fake_dispatch(run_id: int) -> None:
            dispatched.append(run_id)

        manager._dispatch = fake_dispatch
        manager._client.schedule_idle_shutdown = lambda *_a, **_k: idle_shutdowns.append(1)

        worker = asyncio.create_task(manager._worker())
        for run_id in (1, 2):
            manager._queued.add(run_id)
            manager._queue.put_nowait(run_id)

        async def settled() -> None:
            while not reclaims:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(settled(), timeout=10)
        await manager._queue.put(None)
        await asyncio.wait_for(worker, timeout=10)

    try:
        asyncio.run(scenario())
    finally:
        store.close()

    assert dispatched == [1, 2]
    # Once per drain, not once per run: a sync-all must not stop and restart the worker
    # between courses.
    assert reclaims == [1]
    assert idle_shutdowns == [1]


def test_busy_reports_queued_and_pending_work(tmp_path):
    manager, store = _manager(tmp_path)

    async def scenario() -> None:
        assert manager.busy is False

        manager._queued.add(7)
        assert manager.busy is True

        manager._queued.clear()
        manager._queue.put_nowait(7)
        assert manager.busy is True

    try:
        asyncio.run(scenario())
    finally:
        store.close()
