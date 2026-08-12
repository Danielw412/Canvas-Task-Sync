from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from time import perf_counter
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from canvas_task_sync.auth import load_google_credentials
from canvas_task_sync.configuration import CourseSettings, ProjectSettings
from canvas_task_sync.gemini import EXTRACTOR_VERSION, GeminiExtractor, GoogleGenAIBackend
from canvas_task_sync.google_tasks import GoogleTasksClient
from canvas_task_sync.models import (
    ExtractionMode,
    ExtractionOutcome,
    RemoteTask,
    SyncActionKind,
    SyncPlan,
)
from canvas_task_sync.planner import SyncPlanner, apply_sync_plan
from canvas_task_sync.redaction import sanitize
from canvas_task_sync.scheduling import build_draft_tasks
from canvas_task_sync.sources import create_source_adapter
from canvas_task_sync.state import StateStore
from canvas_task_sync.web_models import EventLevel, RunStage


class SyncCancelled(RuntimeError):
    pass


def _today(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()


class ProgressSink(Protocol):
    def emit(
        self,
        stage: RunStage,
        event_type: str,
        message: str,
        *,
        level: EventLevel = EventLevel.INFO,
        metadata: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None: ...


@dataclass
class CancellationToken:
    checker: Callable[[], bool]

    def raise_if_cancelled(self) -> None:
        if self.checker():
            raise SyncCancelled("The run was cancelled before the next stage.")


class NullProgressSink:
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
        del stage, event_type, message, level, metadata, duration_ms


class PreparedPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    version: int = 1
    course_id: str
    course: CourseSettings
    source_key: str
    tasklist_id: str
    tasklist_title: str
    configured_mode: ExtractionMode
    config_hash: str
    page_hash: str
    remote_hash: str
    plan_hash: str
    extraction_was_cached: bool
    extraction_outcome: ExtractionOutcome
    plan: SyncPlan


class AppliedPlanResult(BaseModel):
    applied_counts: dict[str, int]
    completed_action_indexes: list[int]


def action_counts(plan: SyncPlan) -> dict[str, int]:
    counts = Counter(action.kind.value for action in plan.actions)
    return {kind.value: counts.get(kind.value, 0) for kind in SyncActionKind}


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remote_hash(tasks: list[RemoteTask]) -> str:
    return _stable_hash(
        [
            {
                "id": task.id,
                "title": task.title,
                "notes": task.notes,
                "due": task.due,
                "status": task.status,
                "deleted": task.deleted,
                "hidden": task.hidden,
            }
            for task in sorted(tasks, key=lambda item: item.id)
        ]
    )


class SyncService:
    def __init__(
        self,
        settings: ProjectSettings,
        *,
        credentials_loader: Callable[..., Any] = load_google_credentials,
        source_factory: Callable[..., Any] = create_source_adapter,
        tasks_client_factory: Callable[[Any], GoogleTasksClient] = GoogleTasksClient,
        backend_factory: Callable[..., Any] = GoogleGenAIBackend,
    ) -> None:
        self.settings = settings
        self.credentials_loader = credentials_loader
        self.source_factory = source_factory
        self.tasks_client_factory = tasks_client_factory
        self.backend_factory = backend_factory
        self._apply_lock = threading.Lock()

    def prepare(
        self,
        *,
        course_id: str,
        include_past: bool,
        rebase_week: date | None,
        extraction_mode: ExtractionMode | None = None,
        progress: ProgressSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> PreparedPlan:
        sink = progress or NullProgressSink()
        token = cancellation or CancellationToken(lambda: False)
        token.raise_if_cancelled()

        started = perf_counter()
        course = self.settings.course(course_id).model_copy(deep=True)
        if not course.enabled:
            raise ValueError(f"Course '{course_id}' is disabled.")
        if rebase_week is not None and rebase_week.weekday() != 0:
            raise ValueError("The rebased week date must be a Monday.")
        if extraction_mode is not None:
            course.source.extraction.mode = extraction_mode
        load_dotenv(self.settings.root_dir / ".env")
        config_hash = _stable_hash(course.model_dump(mode="json"))
        sink.emit(
            RunStage.VALIDATE_CONFIGURATION,
            "stage_completed",
            "Configuration is valid.",
            metadata={
                "course_id": course_id,
                "source_type": course.source.type,
                "config_hash": config_hash,
            },
            duration_ms=int((perf_counter() - started) * 1000),
        )
        token.raise_if_cancelled()

        stage_started = perf_counter()
        credentials = self.credentials_loader(self.settings.root_dir, interactive=False)
        source = self.source_factory(course.source, credentials)
        tasks_client = self.tasks_client_factory(credentials)
        sink.emit(
            RunStage.AUTHENTICATE_SERVICES,
            "stage_completed",
            "Google credentials and service clients are ready.",
            metadata={"scopes": ["Google Tasks", "Google Slides readonly"]},
            duration_ms=int((perf_counter() - stage_started) * 1000),
        )
        token.raise_if_cancelled()

        stage_started = perf_counter()
        capture = source.capture(include_image=False)
        sink.emit(
            RunStage.CAPTURE_SOURCE,
            "stage_completed",
            "Captured the configured Google Slides page.",
            metadata={
                "page_id": capture.page_id,
                "block_count": len(capture.blocks),
                "page_hash": capture.page_hash,
            },
            duration_ms=int((perf_counter() - stage_started) * 1000),
        )
        token.raise_if_cancelled()

        stage_started = perf_counter()
        with StateStore(self.settings.resolved_state_path, writable=False) as state:
            outcome = state.cached_extraction(
                course_id=course_id,
                source_key=capture.source_key,
                page_hash=capture.page_hash,
                extractor_version=EXTRACTOR_VERSION,
                model_name=self.settings.gemini_model,
                configured_mode=course.source.extraction.mode,
            )
            state_records = state.records(course_id, capture.source_key)
        extraction_was_cached = outcome is not None
        if outcome is None:
            if course.source.extraction.mode in {
                ExtractionMode.IMAGE,
                ExtractionMode.HYBRID,
                ExtractionMode.AUTO,
            }:
                add_image = getattr(source, "add_image", None)
                if not callable(add_image):
                    raise RuntimeError(
                        f"Source adapter '{course.source.type}' cannot provide image extraction."
                    )
                capture = add_image(capture)
            backend = self.backend_factory(
                model=self.settings.gemini_model,
                api_key=os.getenv("GEMINI_API_KEY"),
            )
            outcome = GeminiExtractor(backend).extract(capture, course)
        sink.emit(
            RunStage.EXTRACT_ASSIGNMENTS,
            "stage_completed",
            (
                "Reused cached extraction."
                if extraction_was_cached
                else "Gemini extraction completed."
            ),
            metadata={
                "cache": "hit" if extraction_was_cached else "miss",
                "task_count": len(outcome.tasks),
                "uncertain_count": len(outcome.uncertain),
                "used_mode": outcome.used_mode.value,
                "fallback_reasons": outcome.fallback_reasons,
                "model": self.settings.gemini_model,
            },
            duration_ms=int((perf_counter() - stage_started) * 1000),
        )
        token.raise_if_cancelled()

        stage_started = perf_counter()
        drafts, policy_uncertain, ignored = build_draft_tasks(
            course_id=course_id,
            course=course,
            capture=capture,
            tasks=outcome.tasks,
            today=_today(course.timezone),
            rebase_week=rebase_week,
        )
        sink.emit(
            RunStage.CALCULATE_DEADLINES,
            "stage_completed",
            "Calculated deterministic task deadlines.",
            metadata={
                "draft_count": len(drafts),
                "policy_uncertain_count": len(policy_uncertain),
                "ignored_count": len(ignored),
            },
            duration_ms=int((perf_counter() - stage_started) * 1000),
        )
        token.raise_if_cancelled()

        stage_started = perf_counter()
        tasklist_id, tasklist_title = tasks_client.resolve_task_list(course.task_list)
        remote_tasks = tasks_client.list_tasks(tasklist_id)
        remote_hash = _remote_hash(remote_tasks)
        sink.emit(
            RunStage.COMPARE_GOOGLE_TASKS,
            "stage_completed",
            f"Read Google Tasks list '{tasklist_title}'.",
            metadata={
                "task_list": tasklist_title,
                "remote_task_count": len(remote_tasks),
                "remote_hash": remote_hash,
                "mapping_count": len(state_records),
            },
            duration_ms=int((perf_counter() - stage_started) * 1000),
        )
        token.raise_if_cancelled()

        stage_started = perf_counter()
        plan = SyncPlanner().plan(
            course_id=course_id,
            source_key=capture.source_key,
            task_list=tasklist_title,
            extraction_mode=outcome.used_mode,
            fallback_reasons=outcome.fallback_reasons,
            drafts=drafts,
            uncertain=[*outcome.uncertain, *policy_uncertain],
            ignored=ignored,
            state_records=state_records,
            remote_tasks=remote_tasks,
            include_past=include_past,
            dry_run=True,
        )
        plan_hash = _stable_hash(
            {
                "version": 1,
                "config_hash": config_hash,
                "page_hash": capture.page_hash,
                "remote_hash": remote_hash,
                "plan": plan.model_dump(mode="json"),
            }
        )
        counts = action_counts(plan)
        sink.emit(
            RunStage.BUILD_REVIEW_PLAN,
            "stage_completed",
            "Built an immutable review plan.",
            metadata={"counts": counts, "plan_hash": plan_hash},
            duration_ms=int((perf_counter() - stage_started) * 1000),
        )
        return PreparedPlan(
            course_id=course_id,
            course=course,
            source_key=capture.source_key,
            tasklist_id=tasklist_id,
            tasklist_title=tasklist_title,
            configured_mode=course.source.extraction.mode,
            config_hash=config_hash,
            page_hash=capture.page_hash,
            remote_hash=remote_hash,
            plan_hash=plan_hash,
            extraction_was_cached=extraction_was_cached,
            extraction_outcome=outcome,
            plan=plan,
        )

    def validate_prepared_plan(
        self,
        prepared: PreparedPlan,
        *,
        progress: ProgressSink | None = None,
    ) -> None:
        sink = progress or NullProgressSink()
        stage_started = perf_counter()
        current_course = self.settings.course(prepared.course_id).model_copy(deep=True)
        if prepared.configured_mode:
            current_course.source.extraction.mode = prepared.configured_mode
        current_config_hash = _stable_hash(current_course.model_dump(mode="json"))
        if current_config_hash != prepared.config_hash:
            raise ValueError("Course configuration changed after this preview.")
        credentials = self.credentials_loader(self.settings.root_dir, interactive=False)
        source = self.source_factory(current_course.source, credentials)
        current_capture = source.capture(include_image=False)
        tasks_client = self.tasks_client_factory(credentials)
        tasklist_id, _ = tasks_client.resolve_task_list(current_course.task_list)
        current_remote_hash = _remote_hash(tasks_client.list_tasks(tasklist_id))
        if current_capture.page_hash != prepared.page_hash:
            raise ValueError("The source page changed after this preview.")
        if current_remote_hash != prepared.remote_hash:
            raise ValueError("Google Tasks changed after this preview.")
        sink.emit(
            RunStage.REVALIDATE_PREVIEW,
            "stage_completed",
            "The source, configuration, and Google Tasks still match the preview.",
            metadata={
                "config_hash": current_config_hash,
                "page_hash": current_capture.page_hash,
                "remote_hash": current_remote_hash,
            },
            duration_ms=int((perf_counter() - stage_started) * 1000),
        )

    def apply(
        self,
        prepared: PreparedPlan,
        *,
        progress: ProgressSink | None = None,
        safe_subset: bool = False,
    ) -> AppliedPlanResult:
        sink = progress or NullProgressSink()
        with self._apply_lock:
            self.validate_prepared_plan(prepared, progress=sink)
            load_dotenv(self.settings.root_dir / ".env")
            credentials = self.credentials_loader(self.settings.root_dir, interactive=False)
            tasks_client = self.tasks_client_factory(credentials)
            writable_plan = prepared.plan.model_copy(deep=True)
            writable_plan.dry_run = False
            if safe_subset:
                writable_plan.actions = [
                    action
                    for action in writable_plan.actions
                    if action.kind in {
                        SyncActionKind.CREATE,
                        SyncActionKind.UPDATE,
                        SyncActionKind.UNCHANGED,
                    }
                ]
            completed_indexes: list[int] = []
            applied_counts: Counter[str] = Counter()
            state_path = self.settings.resolved_state_path
            with StateStore(state_path, writable=True) as state:
                remote_by_id = {
                    task.id: task
                    for task in tasks_client.list_tasks(prepared.tasklist_id)
                    if not task.deleted
                }
                for index, action in enumerate(writable_plan.actions):
                    if action.kind not in {
                        SyncActionKind.CREATE,
                        SyncActionKind.UPDATE,
                        SyncActionKind.UNCHANGED,
                    }:
                        continue
                    single = SyncPlan(
                        course_id=writable_plan.course_id,
                        task_list=writable_plan.task_list,
                        dry_run=False,
                        extraction_mode=writable_plan.extraction_mode,
                        fallback_reasons=writable_plan.fallback_reasons,
                        actions=[action],
                    )
                    stage_started = perf_counter()
                    apply_sync_plan(
                        single,
                        tasklist_id=prepared.tasklist_id,
                        tasks_client=tasks_client,
                        state=state,
                    )
                    completed_indexes.append(index)
                    applied_counts[action.kind.value] += 1
                    sink.emit(
                        RunStage.APPLY_CHANGES,
                        "action_applied",
                        f"{action.kind.value.title()} completed for '{action.title}'.",
                        metadata=sanitize(
                            {
                                "action": action.kind.value,
                                "title": action.title,
                                "remote_task_id": action.remote_task_id,
                                "action_index": index,
                            }
                        ),
                        duration_ms=int((perf_counter() - stage_started) * 1000),
                    )
                    if action.remote_task_id and action.remote_task_id not in remote_by_id:
                        remote_by_id[action.remote_task_id] = RemoteTask(
                            id=action.remote_task_id,
                            title=action.title,
                        )
                if not prepared.extraction_was_cached:
                    state.cache_extraction(
                        course_id=prepared.course_id,
                        source_key=prepared.source_key,
                        page_hash=prepared.page_hash,
                        extractor_version=EXTRACTOR_VERSION,
                        model_name=self.settings.gemini_model,
                        configured_mode=prepared.configured_mode,
                        outcome=prepared.extraction_outcome,
                    )
            sink.emit(
                RunStage.PERSIST_STATE,
                "stage_completed",
                "Persisted sync identity and extraction cache state.",
                metadata={"state_path": str(state_path), "applied_counts": dict(applied_counts)},
            )
            return AppliedPlanResult(
                applied_counts=dict(applied_counts),
                completed_action_indexes=completed_indexes,
            )


def prepared_plan_from_json(payload: str) -> PreparedPlan:
    return PreparedPlan.model_validate_json(payload)
