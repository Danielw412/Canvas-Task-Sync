"""Runs one sync, wherever it happens to be hosted.

The web process spawns a worker process (`canvas_task_sync.worker`) that owns this code,
so the Gemini SDK, the Google API client, and the rest of the pipeline are resident only
while work is actually happening.  The same class runs in-process for a course whose
agenda comes from a Chrome capture, because those captures are memory-only by design and
must not be written somewhere a second process could read them.

Either way there is one implementation of what a run does, so the two hosts cannot drift.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any

from canvas_task_sync.control_store import ControlStore, utc_now
from canvas_task_sync.health import run_health_checks
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
    RunMode,
    RunStage,
    RunStatus,
)

TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.REVIEW_NEEDED,
    RunStatus.STALE,
    RunStatus.CANCELLED,
    RunStatus.FAILED,
    RunStatus.FAILED_PARTIAL,
    RunStatus.AWAITING_APPROVAL,
}


def _review_attention_count(counts: dict[str, int]) -> int:
    # Only items the sync could not act on because the remote task state is inconsistent
    # (duplicate managed IDs, malformed managed notes) need a person to review them.
    # Low-confidence extractions, missing remote tasks, and uncertain due dates stay
    # visible in the plan but are informational.
    return counts.get("conflict", 0)


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
        self.store.update_run(self.run_id, stage=stage)
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


class RunExecutor:
    """Executes runs against a control store, one run at a time per call.

    ``notifier`` only matters in-process, where the SSE endpoints wait on it. In the
    worker process it is a private condition nobody waits on: the web process picks the
    events up by polling the control database, which it already does every second.
    """

    def __init__(
        self,
        store: ControlStore,
        service: SyncService,
        *,
        notifier: threading.Condition | None = None,
    ) -> None:
        self.store = store
        self.service = service
        self._event_condition = notifier or threading.Condition()

    def _notify(self) -> None:
        with self._event_condition:
            self._event_condition.notify_all()

    def execute(self, run_id: int) -> None:
        self._execute(run_id)

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
            target_week_start=run.target_week_start,
            acquisition_strategy=run.acquisition_strategy,
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
        # Informational items (missing mappings, past-due tasks, low-confidence
        # extractions, uncertain due dates) remain visible in the plan. Only real
        # conflicts make an otherwise healthy auto-apply run report "review needed".
        attention_count = _review_attention_count(counts)
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
                else "Sync completed; items needing attention are recorded for review."
                if final_status == RunStatus.REVIEW_NEEDED
                else "Sync completed successfully."
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
