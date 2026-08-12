from __future__ import annotations

import asyncio
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from canvas_task_sync.control_store import ControlStore, utc_now
from canvas_task_sync.health import run_health_checks
from canvas_task_sync.models import SyncActionKind
from canvas_task_sync.redaction import safe_exception_summary, sanitize
from canvas_task_sync.sync_service import (
    CancellationToken,
    PreparedPlan,
    ProgressSink,
    SyncCancelled,
    SyncService,
    action_counts,
    prepared_plan_from_json,
)
from canvas_task_sync.web_models import (
    EventLevel,
    RunCreate,
    RunMode,
    RunStage,
    RunStatus,
    RunTrigger,
    ScheduleMode,
)

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


class StoreProgressSink(ProgressSink):
    def __init__(self, store: ControlStore, run_id: int, notifier: threading.Condition) -> None:
        self.store = store
        self.run_id = run_id
        self.notifier = notifier

    def emit(
        self,
        stage: RunStage,
        event_type: str,
        message: str,
        *,
        level: EventLevel = EventLevel.INFO,
        metadata: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if event_type == "action_applied" and metadata and metadata.get("action"):
            run = self.store.get_run(self.run_id, include_events=False)
            counts = dict(run.applied_counts) if run else {}
            action = str(metadata["action"])
            counts[action] = counts.get(action, 0) + 1
            self.store.update_run(self.run_id, applied_counts_json=counts)
        self.store.add_event(
            self.run_id,
            stage=stage,
            event_type=event_type,
            message=message,
            level=level,
            metadata=sanitize(metadata or {}),
            duration_ms=duration_ms,
        )
        with self.notifier:
            self.notifier.notify_all()


class RunManager:
    def __init__(self, store: ControlStore, service: SyncService) -> None:
        self.store = store
        self.service = service
        self._queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="canvas-sync")
        self._event_condition = threading.Condition()
        self._queued: set[int] = set()

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker(), name="canvas-sync-worker")

    async def stop(self) -> None:
        if self._worker_task is not None:
            await self._queue.put(None)
            await self._worker_task
            self._worker_task = None
        self._executor.shutdown(wait=True, cancel_futures=False)

    def create_preview(
        self,
        request: RunCreate,
        *,
        trigger: RunTrigger = RunTrigger.MANUAL,
        requested_mode: RunMode = RunMode.PREVIEW,
        schedule_id: int | None = None,
    ) -> int:
        run_id = self.store.create_run(
            course_id=request.course_id,
            trigger=trigger,
            requested_mode=requested_mode,
            extraction_mode=request.extraction_mode,
            include_past=request.include_past,
            test_rebase_week=request.test_rebase_week,
            schedule_id=schedule_id,
        )
        self.store.add_event(
            run_id,
            stage=RunStage.QUEUED,
            event_type="run_queued",
            message="Run added to the local FIFO queue.",
            metadata={"trigger": trigger.value, "mode": requested_mode.value},
        )
        self._queued.add(run_id)
        self._queue.put_nowait(run_id)
        self._notify()
        return run_id

    def create_health(self, *, course_id: str | None = None) -> int:
        selected_course = course_id or next(iter(sorted(self.service.settings.courses)), "all")
        run_id = self.store.create_run(
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

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            run_id = await self._queue.get()
            if run_id is None:
                self._queue.task_done()
                break
            self._queued.discard(run_id)
            try:
                await loop.run_in_executor(self._executor, self._execute, run_id)
            finally:
                self._queue.task_done()

    def _execute(self, run_id: int) -> None:
        run = self.store.get_run(run_id, include_events=False)
        if run is None:
            return
        sink = StoreProgressSink(self.store, run_id, self._event_condition)
        if run.cancel_requested:
            self._finish_cancelled(run_id, sink)
            return
        try:
            if run.requested_mode == RunMode.HEALTH:
                self._execute_health(run_id, sink)
            elif run.plan_hash and run.plan is not None and run.status == RunStatus.QUEUED:
                self._execute_apply(run_id, sink)
            else:
                self._execute_preview(run_id, sink)
        except SyncCancelled:
            self._finish_cancelled(run_id, sink)
        except Exception as error:  # Boundaries turn provider-specific errors into safe run state.
            current = self.store.get_run(run_id, include_events=False)
            partial = bool(current and any(current.applied_counts.values()))
            status = RunStatus.FAILED_PARTIAL if partial else RunStatus.FAILED
            summary = safe_exception_summary(error)
            self.store.update_run(
                run_id,
                status=status,
                stage=RunStage.COMPLETE,
                finished_at=utc_now(),
                error_code=type(error).__name__.lower(),
                error_summary=summary,
            )
            sink.emit(
                RunStage.COMPLETE,
                "run_failed",
                summary,
                level=EventLevel.ERROR,
                metadata={"error_type": type(error).__name__, "partial": partial},
            )
        finally:
            self._finish_scheduled_run(run_id)
            self._notify()

    def _finish_scheduled_run(self, run_id: int) -> None:
        run = self.store.get_run(run_id, include_events=False)
        if run is None or run.schedule_id is None or run.status not in TERMINAL_STATUSES:
            return
        occurrence_status = {
            RunStatus.AWAITING_APPROVAL: "preview_ready",
            RunStatus.SUCCEEDED: "succeeded",
            RunStatus.REVIEW_NEEDED: "review_needed",
            RunStatus.STALE: "stale",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.FAILED: "failed",
            RunStatus.FAILED_PARTIAL: "failed_partial",
        }[run.status]
        details = run.error_summary or {
            "preview_ready": "Scheduled preview is ready for review.",
            "succeeded": "Scheduled run completed successfully.",
            "review_needed": "Safe changes completed; remaining items need review.",
            "cancelled": "Scheduled run was cancelled before completion.",
        }.get(occurrence_status, "Scheduled run finished.")
        self.store.finish_schedule_occurrence(
            run_id=run_id,
            status=occurrence_status,
            details=details,
        )

    def _execute_preview(self, run_id: int, sink: StoreProgressSink) -> None:
        run = self.store.get_run(run_id, include_events=False)
        if run is None:
            return
        self.store.mark_run_started(run_id, stage=RunStage.VALIDATE_CONFIGURATION)
        token = CancellationToken(lambda: self.store.cancellation_requested(run_id))
        prepared = self.service.prepare(
            course_id=run.course_id,
            include_past=run.include_past,
            rebase_week=run.test_rebase_week,
            extraction_mode=run.extraction_mode,
            progress=sink,
            cancellation=token,
        )
        counts = action_counts(prepared.plan)
        self.store.set_prepared_plan(
            run_id,
            plan=prepared.plan,
            prepared_json=prepared.model_dump_json(),
            plan_hash=prepared.plan_hash,
            config_hash=prepared.config_hash,
            page_hash=prepared.page_hash,
            remote_hash=prepared.remote_hash,
            counts=counts,
        )
        attention_count = sum(
            counts.get(kind.value, 0)
            for kind in {
                SyncActionKind.UNCERTAIN,
                SyncActionKind.REMOTE_MISSING,
                SyncActionKind.SOURCE_MISSING,
                SyncActionKind.HISTORICAL_BLOCKED,
            }
        )
        if run.requested_mode == RunMode.AUTO_APPLY:
            self.store.update_run(
                run_id,
                status=RunStatus.APPLYING,
                stage=RunStage.REVALIDATE_PREVIEW,
            )
            self._apply_prepared(run_id, prepared, sink, safe_subset=True)
            final_status = RunStatus.REVIEW_NEEDED if attention_count else RunStatus.SUCCEEDED
        else:
            final_status = RunStatus.AWAITING_APPROVAL
        self.store.update_run(
            run_id,
            status=final_status,
            stage=RunStage.COMPLETE,
            finished_at=utc_now(),
        )
        sink.emit(
            RunStage.COMPLETE,
            "run_completed",
            (
                "Preview is ready for review."
                if final_status == RunStatus.AWAITING_APPROVAL
                else "Scheduled run completed; items needing attention remain untouched."
                if final_status == RunStatus.REVIEW_NEEDED
                else "Scheduled run completed successfully."
            ),
            metadata={"status": final_status.value, "counts": counts},
        )

    def _execute_health(self, run_id: int, sink: StoreProgressSink) -> None:
        run = self.store.get_run(run_id, include_events=False)
        if run is None:
            return
        self.store.mark_run_started(run_id, stage=RunStage.HEALTH_CHECK)
        selected = None if run.course_id == "all" else run.course_id
        checks = run_health_checks(self.service.settings, selected)
        counts: Counter[str] = Counter(check.state.value for check in checks)
        for check in checks:
            sink.emit(
                RunStage.HEALTH_CHECK,
                "health_check",
                f"{check.label}: {check.summary}",
                level=(
                    EventLevel.ERROR
                    if check.state.value == "error"
                    else EventLevel.WARNING
                    if check.state.value in {"warning", "missing"}
                    else EventLevel.INFO
                ),
                metadata={"key": check.key, "state": check.state.value, **check.details},
                duration_ms=check.duration_ms,
            )
        failed = counts.get("error", 0) + counts.get("missing", 0)
        status = RunStatus.REVIEW_NEEDED if failed else RunStatus.SUCCEEDED
        self.store.update_run(
            run_id,
            status=status,
            stage=RunStage.COMPLETE,
            finished_at=utc_now(),
            counts_json=dict(counts),
        )
        sink.emit(
            RunStage.COMPLETE,
            "run_completed",
            "Health check completed with issues." if failed else "All health checks passed.",
            level=EventLevel.WARNING if failed else EventLevel.INFO,
            metadata={"status": status.value, "counts": dict(counts)},
        )

    def _execute_apply(self, run_id: int, sink: StoreProgressSink) -> None:
        payload = self.store.prepared_json(run_id)
        if not payload:
            raise RuntimeError("The immutable preview snapshot is missing.")
        prepared = prepared_plan_from_json(payload)
        self.store.update_run(
            run_id,
            status=RunStatus.APPLYING,
            stage=RunStage.REVALIDATE_PREVIEW,
            started_at=utc_now(),
        )
        self._apply_prepared(run_id, prepared, sink, safe_subset=False)
        self.store.update_run(
            run_id,
            status=RunStatus.SUCCEEDED,
            stage=RunStage.COMPLETE,
            finished_at=utc_now(),
        )
        sink.emit(
            RunStage.COMPLETE,
            "run_completed",
            "Approved changes were applied successfully.",
            metadata={"status": RunStatus.SUCCEEDED.value},
        )

    def _apply_prepared(
        self,
        run_id: int,
        prepared: PreparedPlan,
        sink: StoreProgressSink,
        *,
        safe_subset: bool,
    ) -> None:
        try:
            result = self.service.apply(prepared, progress=sink, safe_subset=safe_subset)
        except ValueError as error:
            message = str(error)
            if "changed after this preview" in message:
                self.store.update_run(
                    run_id,
                    status=RunStatus.STALE,
                    stage=RunStage.COMPLETE,
                    finished_at=utc_now(),
                    error_code="stale_preview",
                    error_summary=message,
                )
                sink.emit(
                    RunStage.COMPLETE,
                    "preview_stale",
                    message,
                    level=EventLevel.WARNING,
                )
                raise SyncCancelled("Stale preview was not applied.") from None
            raise
        self.store.update_run(run_id, applied_counts_json=result.applied_counts)

    def _finish_cancelled(self, run_id: int, sink: StoreProgressSink) -> None:
        current = self.store.get_run(run_id, include_events=False)
        if current and current.status == RunStatus.STALE:
            return
        self.store.update_run(
            run_id,
            status=RunStatus.CANCELLED,
            stage=RunStage.COMPLETE,
            finished_at=utc_now(),
        )
        sink.emit(
            RunStage.COMPLETE,
            "run_cancelled",
            "Run cancelled before the next stage.",
            level=EventLevel.WARNING,
        )


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
