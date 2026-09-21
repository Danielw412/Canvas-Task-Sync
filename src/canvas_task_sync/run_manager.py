"""Queues runs and hands them to a worker process.

Nothing in this module imports the sync pipeline.  That is the point: the web process
stays a web process, and `canvas_task_sync.worker` carries the Gemini SDK and the Google
clients for as long as there is work, then exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from canvas_task_sync.configuration import ProjectSettings
from canvas_task_sync.control_store import ControlStore, utc_now
from canvas_task_sync.memory import release_memory
from canvas_task_sync.web_models import (
    EventLevel,
    RunCreate,
    RunMode,
    RunStage,
    RunStatus,
    RunTrigger,
    ScheduleMode,
)
from canvas_task_sync.week import selected_week_start

ACTIVE_STATUSES = {
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.APPLYING,
}
TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.REVIEW_NEEDED,
    RunStatus.STALE,
    RunStatus.CANCELLED,
    RunStatus.FAILED,
    RunStatus.FAILED_PARTIAL,
    RunStatus.AWAITING_APPROVAL,
}

# How long the worker process is kept alive after the queue drains. Long enough that a
# sync-all followed by an apply does not pay to start twice, short enough that an idle
# evening costs nothing.
WORKER_IDLE_SECONDS = 60.0
WORKER_START_TIMEOUT = 60.0


class SyncWorkerClient:
    """Owns the lifetime of the worker process.

    The parent decides when the worker exits rather than letting it time out on its own,
    so there is no window where a job is dispatched to a process that has already decided
    to stop.
    """

    def __init__(
        self,
        config_path: Path,
        *,
        max_workers: int,
        python: str | None = None,
    ) -> None:
        self.config_path = config_path
        self.max_workers = max_workers
        self.python = python or sys.executable
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[None]] = {}
        self._spawn_lock = asyncio.Lock()
        self._shutdown_handle: asyncio.TimerHandle | None = None
        self._ready = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def run(self, run_id: int) -> None:
        """Execute one run in the worker and wait for it to finish."""
        self.cancel_idle_shutdown()
        await self._ensure_started()
        process = self._process
        assert process is not None and process.stdin is not None
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending[run_id] = future
        try:
            process.stdin.write(json.dumps({"run_id": run_id}).encode() + b"\n")
            await process.stdin.drain()
        except (ConnectionResetError, BrokenPipeError) as error:
            self._pending.pop(run_id, None)
            raise RuntimeError("The sync worker stopped before the run started.") from error
        await future

    async def _ensure_started(self) -> None:
        async with self._spawn_lock:
            if self.running:
                return
            self._process = await asyncio.create_subprocess_exec(
                self.python,
                "-m",
                "canvas_task_sync.worker",
                "--config",
                str(self.config_path),
                "--max-workers",
                str(self.max_workers),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
            )
            self._reader = asyncio.create_task(self._read_replies(), name="sync-worker-reader")
            await asyncio.wait_for(self._await_ready(), timeout=WORKER_START_TIMEOUT)

    async def _await_ready(self) -> None:
        while not self._ready.is_set():
            process = self._process
            if process is not None and process.returncode is not None:
                # It died on the way up -- a bad config or a broken install. Say so now
                # rather than letting every run wait out the start timeout.
                raise RuntimeError(
                    f"The sync worker exited during startup (code {process.returncode}). "
                    "Check the service log."
                )
            await asyncio.sleep(0.02)

    async def _read_replies(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            async for raw in process.stdout:
                try:
                    payload = json.loads(raw.decode().strip() or "{}")
                except ValueError:
                    continue
                if payload.get("ready"):
                    self._ready.set()
                    continue
                run_id = payload.get("run_id")
                future = self._pending.pop(run_id, None) if run_id is not None else None
                if future is not None and not future.done():
                    future.set_result(None)
        finally:
            # The worker died or was shut down: nobody else will answer these.
            self._ready.clear()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("The sync worker exited unexpectedly."))
            self._pending.clear()

    def schedule_idle_shutdown(self, delay: float = WORKER_IDLE_SECONDS) -> None:
        self.cancel_idle_shutdown()
        if not self.running:
            return
        loop = asyncio.get_running_loop()
        self._shutdown_handle = loop.call_later(
            delay, lambda: asyncio.ensure_future(self._idle_stop())
        )

    def cancel_idle_shutdown(self) -> None:
        if self._shutdown_handle is not None:
            self._shutdown_handle.cancel()
            self._shutdown_handle = None

    async def _idle_stop(self) -> None:
        if self._pending:
            return
        await self.stop()
        release_memory()

    async def stop(self) -> None:
        self.cancel_idle_shutdown()
        process, self._process = self._process, None
        reader, self._reader = self._reader, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ConnectionResetError, BrokenPipeError, AttributeError):
                process.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=20)
            if process.returncode is None:
                process.kill()
                await process.wait()
        if reader is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(reader, timeout=5)
        # The pipe transports outlive the process on Windows' proactor loop and warn at
        # collection time unless they are closed here.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


class RunManager:
    def __init__(
        self,
        store: ControlStore,
        settings: ProjectSettings,
        *,
        config_path: Path,
        source_factory: Any | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.config_path = config_path
        # Only the in-process fallback needs this; the worker builds its own adapters.
        self._source_factory = source_factory
        self._queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._worker_count = max(2, min(8, len(settings.courses) or 1))
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._event_condition = threading.Condition()
        self._queued: set[int] = set()
        self._client = SyncWorkerClient(config_path, max_workers=self._worker_count)
        self._local: Any | None = None

    async def start(self) -> None:
        if not self._worker_tasks:
            self._worker_tasks = [
                asyncio.create_task(self._worker(), name=f"canvas-sync-worker-{index + 1}")
                for index in range(self._worker_count)
            ]

    async def stop(self) -> None:
        if self._worker_tasks:
            for _ in self._worker_tasks:
                await self._queue.put(None)
            await asyncio.gather(*self._worker_tasks)
            self._worker_tasks = []
        await self._client.stop()

    def create_preview(
        self,
        request: RunCreate,
        *,
        trigger: RunTrigger = RunTrigger.MANUAL,
        requested_mode: RunMode | None = None,
        schedule_id: int | None = None,
        operation_id: str | None = None,
    ) -> int:
        effective_mode = requested_mode or request.mode
        operation_id = operation_id or str(uuid.uuid4())
        course = self.settings.course(request.course_id)
        target_week_start = selected_week_start(
            datetime.now(ZoneInfo(course.timezone)).date(),
            request.week_selection,
        )
        run_id = self.store.create_run(
            course_id=request.course_id,
            operation_id=operation_id,
            trigger=trigger,
            requested_mode=effective_mode,
            extraction_mode=request.extraction_mode,
            week_selection=request.week_selection,
            target_week_start=target_week_start,
            acquisition_strategy=request.acquisition_strategy,
            include_past=request.include_past,
            test_rebase_week=request.test_rebase_week,
            schedule_id=schedule_id,
        )
        self.store.add_event(
            run_id,
            stage=RunStage.QUEUED,
            event_type="run_queued",
            message="Run added to the local FIFO queue.",
            metadata={
                "trigger": trigger.value,
                "mode": effective_mode.value,
                "week_selection": request.week_selection.value,
                "target_week_start": target_week_start.isoformat(),
                "acquisition_strategy": request.acquisition_strategy.value,
            },
        )
        self._queued.add(run_id)
        self._queue.put_nowait(run_id)
        self._notify()
        return run_id

    def create_health(self, *, course_id: str | None = None) -> int:
        selected_course = course_id or next(iter(sorted(self.settings.courses)), "all")
        run_id = self.store.create_run(
            operation_id=str(uuid.uuid4()),
            course_id=selected_course,
            trigger=RunTrigger.MANUAL,
            requested_mode=RunMode.HEALTH,
        )
        self.store.add_event(
            run_id,
            stage=RunStage.QUEUED,
            event_type="health_queued",
            message="Health check added to the local FIFO queue.",
            metadata={"course_id": course_id},
        )
        self._queued.add(run_id)
        self._queue.put_nowait(run_id)
        self._notify()
        return run_id

    def queue_apply(self, run_id: int, *, plan_hash: str) -> None:
        run = self.store.get_run(run_id, include_events=False)
        if run is None:
            raise KeyError(run_id)
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError("Only a completed preview awaiting approval can be applied.")
        if not run.plan_hash or run.plan_hash != plan_hash:
            raise ValueError("The submitted plan hash does not match this preview.")
        if run.test_rebase_week is not None:
            raise ValueError("A rebased diagnostic preview cannot be applied.")
        self.store.update_run(
            run_id,
            status=RunStatus.QUEUED,
            stage=RunStage.QUEUED,
            cancel_requested=False,
            finished_at=None,
            error_code=None,
            error_summary=None,
        )
        self.store.add_event(
            run_id,
            stage=RunStage.QUEUED,
            event_type="apply_queued",
            message="Approved plan added to the write queue.",
            metadata={"plan_hash": plan_hash},
        )
        self._queued.add(run_id)
        self._queue.put_nowait(run_id)
        self._notify()

    def cancel(self, run_id: int) -> bool:
        changed = self.store.request_cancel(run_id)
        if changed:
            self.store.add_event(
                run_id,
                stage=RunStage.QUEUED,
                event_type="cancellation_requested",
                message="Cancellation requested; it will take effect between stages.",
                level=EventLevel.WARNING,
            )
            self._notify()
        return changed

    async def wait_for_events(self, timeout: float = 1.0) -> None:
        loop = asyncio.get_running_loop()

        def wait() -> None:
            with self._event_condition:
                self._event_condition.wait(timeout=timeout)

        await loop.run_in_executor(None, wait)

    def _notify(self) -> None:
        with self._event_condition:
            self._event_condition.notify_all()

    @property
    def busy(self) -> bool:
        return bool(self._queued) or not self._queue.empty()

    def _needs_in_process_run(self, run_id: int) -> bool:
        """True when this run depends on a capture only this process is holding.

        Browser captures are memory-only by design, so there is nowhere a separate
        process could read them from. Those runs stay here; everything else -- which is
        every Canvas, Slides, and Docs course -- goes to the worker.
        """
        run = self.store.get_run(run_id, include_events=False)
        if run is None:
            return False
        course = self.settings.courses.get(run.course_id)
        return bool(course is not None and course.source.type == "browser")

    def _in_process_executor(self) -> Any:
        if self._local is None:
            # Imported here so a backend that never runs a browser-sourced course never
            # pays for the pipeline at all.
            from canvas_task_sync.run_executor import RunExecutor
            from canvas_task_sync.sync_service import SyncService

            self._local = RunExecutor(
                self.store,
                SyncService(self.settings, source_factory=self._source_factory),
                notifier=self._event_condition,
            )
        return self._local

    async def _dispatch(self, run_id: int) -> None:
        if self._needs_in_process_run(run_id):
            await asyncio.to_thread(self._in_process_executor().execute, run_id)
            return
        await self._client.run(run_id)

    async def _worker(self) -> None:
        while True:
            run_id = await self._queue.get()
            if run_id is None:
                self._queue.task_done()
                break
            self._queued.discard(run_id)
            try:
                await self._dispatch(run_id)
            except Exception:
                # The worker process records the failure against the run itself; losing
                # the pipe must not take down this queue.
                self.store.update_run(
                    run_id,
                    status=RunStatus.FAILED,
                    stage=RunStage.COMPLETE,
                    finished_at=datetime.now(UTC),
                    error_code="worker_unavailable",
                    error_summary="The sync worker stopped before the run finished.",
                )
            finally:
                self._queue.task_done()
                self._notify()
                if not self.busy:
                    # Let the worker go, and give back whatever this process accumulated
                    # serving the run's progress.
                    self._client.schedule_idle_shutdown()
                    release_memory()

class ScheduleManager:
    def __init__(self, store: ControlStore, runs: RunManager) -> None:
        self.store = store
        self.runs = runs
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="canvas-schedule-loop")

    async def stop(self) -> None:
        if self._task is not None:
            self._stop.set()
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick(utc_now())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15)
            except TimeoutError:
                continue

    def tick(self, now: datetime) -> None:
        for schedule in self.store.due_schedules(now):
            scheduled_for = schedule.next_run_at
            if scheduled_for is None:
                continue
            while now - scheduled_for.astimezone(UTC) > timedelta(seconds=90):
                next_run = next_schedule_occurrence(
                    weekdays=schedule.weekdays,
                    local_time=schedule.local_time,
                    timezone_name=schedule.timezone,
                    after=scheduled_for,
                )
                self.store.record_schedule_occurrence(
                    schedule_id=schedule.id,
                    scheduled_for=scheduled_for,
                    status="missed",
                    details="Local server was offline or unavailable during the grace window.",
                )
                self.store.advance_schedule(
                    schedule.id,
                    next_run_at=next_run,
                    last_run_at=scheduled_for,
                    last_result="missed",
                )
                scheduled_for = next_run
                if scheduled_for > now:
                    break
            if scheduled_for > now:
                continue
            next_run = next_schedule_occurrence(
                weekdays=schedule.weekdays,
                local_time=schedule.local_time,
                timezone_name=schedule.timezone,
                after=scheduled_for,
            )
            mode = (
                RunMode.AUTO_APPLY
                if schedule.mode == ScheduleMode.AUTO_APPLY
                else RunMode.PREVIEW
            )
            run_id = self.runs.create_preview(
                RunCreate(course_id=schedule.course_id),
                trigger=RunTrigger.SCHEDULE,
                requested_mode=mode,
                schedule_id=schedule.id,
            )
            self.store.record_schedule_occurrence(
                schedule_id=schedule.id,
                scheduled_for=scheduled_for,
                status="queued",
                run_id=run_id,
                details="Scheduled occurrence queued.",
            )
            self.store.advance_schedule(
                schedule.id,
                next_run_at=next_run,
                last_run_at=scheduled_for,
                last_result="queued",
            )


def next_schedule_occurrence(
    *,
    weekdays: list[int],
    local_time: Any,
    timezone_name: str,
    after: datetime | None = None,
) -> datetime:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(timezone_name)
    cursor = (after or utc_now()).astimezone(zone)
    for offset in range(0, 15):
        candidate_date = (cursor + timedelta(days=offset)).date()
        if candidate_date.weekday() not in weekdays:
            continue
        candidate = datetime.combine(candidate_date, local_time, tzinfo=zone)
        normalized = candidate.astimezone(UTC).astimezone(zone)
        if normalized.replace(tzinfo=None) != candidate.replace(tzinfo=None):
            candidate = normalized
        if candidate > cursor:
            return candidate.astimezone(UTC)
    raise ValueError("Could not calculate the next scheduled occurrence.")
