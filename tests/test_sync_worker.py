"""The worker process boundary.

These drive the real subprocess and the real protocol, because the interesting failures
here are the ones a mock cannot have: a run that finishes in another process but whose
events never reach the web process, a worker that outlives its usefulness, or a queue
that wedges when the worker dies mid-run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from canvas_task_sync.configuration import ProjectSettings
from canvas_task_sync.control_store import ControlStore
from canvas_task_sync.run_manager import RunManager, SyncWorkerClient
from canvas_task_sync.web_models import RunMode, RunStatus, RunTrigger

REPO_ROOT = Path(__file__).parent.parent

PROJECT = """version: 1
state_path: .canvas-task-sync/state.sqlite3
gemini_model: test-model
courses:
  spanish:
    enabled: true
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
"""

BROWSER_PROJECT = PROJECT.replace(
    """    source:
      type: google_slides
      url: https://docs.google.com/presentation/d/fixture/edit
      page_id: slide-1""",
    """    source:
      type: browser
      url: https://docs.google.com/presentation/d/fixture/edit
      page_id: slide-1""",
)


def _write_project(root: Path, document: str = PROJECT) -> Path:
    config = root / "config" / "courses.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(document, encoding="utf-8")
    return config


def _settings(root: Path) -> ProjectSettings:
    from canvas_task_sync.configuration import load_settings

    return load_settings(_write_project(root))


def test_worker_announces_readiness_and_exits_when_stdin_closes(tmp_path):
    config = _write_project(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "canvas_task_sync.worker", "--config", str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
    )
    try:
        assert json.loads(process.stdout.readline()) == {"ready": True}
        # Closing stdin is how the web process asks the worker to go away; it must not
        # need a signal or a timeout to notice.
        process.stdin.close()
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:  # pragma: no cover - only if the assert above failed
            process.kill()


def test_worker_executes_a_run_and_records_it_in_the_shared_database(tmp_path):
    config = _write_project(tmp_path)
    store = ControlStore(tmp_path / ".canvas-task-sync" / "control.sqlite3")
    run_id = store.create_run(
        course_id="spanish",
        trigger=RunTrigger.MANUAL,
        requested_mode=RunMode.PREVIEW,
    )
    store.close()

    process = subprocess.Popen(
        [sys.executable, "-m", "canvas_task_sync.worker", "--config", str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
    )
    try:
        assert json.loads(process.stdout.readline()) == {"ready": True}
        process.stdin.write(json.dumps({"run_id": run_id}) + "\n")
        process.stdin.flush()
        reply = json.loads(process.stdout.readline())
        process.stdin.close()
        process.wait(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover
            process.kill()

    assert reply["run_id"] == run_id

    # The run has no credentials, so it fails -- but it must fail *as a recorded run* in
    # the database the web process reads, not as a silent subprocess crash.
    reopened = ControlStore(tmp_path / ".canvas-task-sync" / "control.sqlite3")
    try:
        run = reopened.get_run(run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert run.finished_at is not None
        assert [event.event_type for event in run.events][-1] == "run_failed"
    finally:
        reopened.close()


def test_malformed_job_is_reported_without_killing_the_worker(tmp_path):
    config = _write_project(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "canvas_task_sync.worker", "--config", str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
    )
    try:
        assert json.loads(process.stdout.readline()) == {"ready": True}
        process.stdin.write("not json\n")
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["ok"] is False
        process.stdin.close()
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:  # pragma: no cover
            process.kill()


def test_client_starts_one_worker_and_stops_it_on_demand(tmp_path):
    config = _write_project(tmp_path)
    store = ControlStore(tmp_path / ".canvas-task-sync" / "control.sqlite3")
    run_id = store.create_run(
        course_id="spanish",
        trigger=RunTrigger.MANUAL,
        requested_mode=RunMode.PREVIEW,
    )
    store.close()
    client = SyncWorkerClient(config, max_workers=2)

    async def scenario() -> None:
        assert client.running is False
        await asyncio.wait_for(client.run(run_id), timeout=120)
        assert client.running is True
        await client.stop()
        assert client.running is False

    asyncio.run(scenario())


def test_idle_shutdown_releases_the_worker_process(tmp_path):
    config = _write_project(tmp_path)
    client = SyncWorkerClient(config, max_workers=1)

    async def scenario() -> None:
        await client._ensure_started()
        assert client.running is True
        client.schedule_idle_shutdown(delay=0.05)
        for _ in range(400):
            await asyncio.sleep(0.05)
            if not client.running:
                break
        assert client.running is False, "the worker should exit once the queue is idle"

    asyncio.run(scenario())


def test_new_work_cancels_a_pending_idle_shutdown(tmp_path):
    config = _write_project(tmp_path)
    client = SyncWorkerClient(config, max_workers=1)

    async def scenario() -> None:
        await client._ensure_started()
        client.schedule_idle_shutdown(delay=30)
        assert client._shutdown_handle is not None
        client.cancel_idle_shutdown()
        assert client._shutdown_handle is None
        await client.stop()

    asyncio.run(scenario())


def test_a_dead_worker_fails_the_run_instead_of_wedging_the_queue(tmp_path):
    config = _write_project(tmp_path)
    store = ControlStore(tmp_path / ".canvas-task-sync" / "control.sqlite3")
    settings = _settings(tmp_path)
    manager = RunManager(store, settings, config_path=config)
    run_id = store.create_run(
        course_id="spanish",
        trigger=RunTrigger.MANUAL,
        requested_mode=RunMode.PREVIEW,
    )

    async def scenario() -> None:
        async def broken_dispatch(_run_id: int) -> None:
            raise RuntimeError("The sync worker exited unexpectedly.")

        manager._dispatch = broken_dispatch
        worker = asyncio.create_task(manager._worker())
        manager._queued.add(run_id)
        manager._queue.put_nowait(run_id)
        await asyncio.wait_for(manager._queue.join(), timeout=15)
        await manager._queue.put(None)
        await asyncio.wait_for(worker, timeout=15)

    try:
        asyncio.run(scenario())
        run = store.get_run(run_id, include_events=False)
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert run.error_code == "worker_unavailable"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("document", "expected_in_process"),
    [(PROJECT, False), (BROWSER_PROJECT, True)],
)
def test_only_chrome_capture_runs_stay_in_the_web_process(tmp_path, document, expected_in_process):
    # A browser capture lives in this process's memory and is deliberately never written
    # anywhere a second process could read, so those runs cannot be handed off.
    config = _write_project(tmp_path, document)
    store = ControlStore(tmp_path / ".canvas-task-sync" / "control.sqlite3")
    settings = _settings(tmp_path) if document is PROJECT else None
    if settings is None:
        from canvas_task_sync.configuration import load_settings

        settings = load_settings(config)
    manager = RunManager(store, settings, config_path=config)
    try:
        run_id = store.create_run(
            course_id="spanish",
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.PREVIEW,
        )
        assert manager._needs_in_process_run(run_id) is expected_in_process
    finally:
        store.close()


def test_a_worker_that_cannot_start_fails_fast(tmp_path):
    # A missing config makes the worker exit immediately; the client must surface that
    # instead of waiting out its start timeout on every run.
    client = SyncWorkerClient(tmp_path / "config" / "missing.yaml", max_workers=1)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="exited during startup"):
            await asyncio.wait_for(client._ensure_started(), timeout=30)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(client.stop())
