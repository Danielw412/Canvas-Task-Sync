from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import perf_counter, sleep
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from canvas_task_sync.auth import load_google_credentials
from canvas_task_sync.configuration import CourseSettings, ProjectSettings
from canvas_task_sync.gemini import EXTRACTOR_VERSION, GeminiExtractor, GoogleGenAIBackend
from canvas_task_sync.google_tasks import GoogleTasksClient
from canvas_task_sync.managed_notes import parse_notes
from canvas_task_sync.models import (
    AcquisitionStrategy,
    ExtractionMode,
    ExtractionOutcome,
    RemoteTask,
    SyncActionKind,
    SyncPlan,
)
from canvas_task_sync.planner import SyncPlanner, apply_sync_plan
from canvas_task_sync.redaction import sanitize
from canvas_task_sync.scheduling import build_draft_tasks
from canvas_task_sync.sources import create_course_source_adapter
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


class TaskListSnapshot(BaseModel):
    id: str
    title: str
    remote_hash: str
    task_count: int


class PreparedPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    version: int = 3
    course_id: str
    course: CourseSettings
    source_key: str
    tasklist_id: str = ""
    tasklist_title: str = ""
    tasklists: dict[str, TaskListSnapshot] = Field(default_factory=dict)
    configured_mode: ExtractionMode
    target_week_start: date
    acquisition_strategy: AcquisitionStrategy = AcquisitionStrategy.AUTO
    extraction_cache_key: str = ""
    config_hash: str
    page_hash: str
    remote_hash: str
    relevant_remote_task_ids: dict[str, list[str]] = Field(default_factory=dict)
    plan_hash: str
    extraction_was_cached: bool
    extraction_outcome: ExtractionOutcome
    plan: SyncPlan


class AppliedPlanResult(BaseModel):
    applied_counts: dict[str, int]
    completed_action_indexes: list[int]


def action_counts(plan: SyncPlan) -> dict[str, int]:
    counts = Counter(action.kind.value for action in plan.actions)
    result = {kind.value: counts.get(kind.value, 0) for kind in SyncActionKind}
    result["due_uncertain"] = sum(action.due_uncertain for action in plan.actions)
    return result


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
                "completed": task.completed,
                "deleted": task.deleted,
                "hidden": task.hidden,
                "tasklist_id": task.tasklist_id,
            }
            for task in sorted(tasks, key=lambda item: (item.tasklist_id or "", item.id))
        ]
    )


def _read_task_lists(
    tasks_client: GoogleTasksClient,
    course: CourseSettings,
) -> tuple[dict[str, TaskListSnapshot], list[RemoteTask]]:
    snapshots: dict[str, TaskListSnapshot] = {}
    remote_tasks: list[RemoteTask] = []
    for configured_title in dict.fromkeys([course.task_list, course.assessment_task_list]):
        tasklist_id, tasklist_title = tasks_client.resolve_task_list(configured_title)
        tasks = [
            task.model_copy(
                update={"tasklist_id": tasklist_id, "tasklist_title": tasklist_title}
            )
            for task in tasks_client.list_tasks(tasklist_id)
        ]
        snapshots[configured_title] = TaskListSnapshot(
            id=tasklist_id,
            title=tasklist_title,
            remote_hash="",
            task_count=len(tasks),
        )
        remote_tasks.extend(tasks)
    return snapshots, remote_tasks


def _relevant_remote_tasks(
    remote_tasks: list[RemoteTask],
    course: CourseSettings,
    *,
    course_id: str,
    known_task_ids: dict[str, list[str]] | None = None,
) -> list[RemoteTask]:
    known_keys = {
        (tasklist_id, task_id)
        for tasklist_id, task_ids in (known_task_ids or {}).items()
        for task_id in task_ids
    }
    selected: list[RemoteTask] = []
    for task in remote_tasks:
        metadata = parse_notes(task.notes).metadata
        managed_for_course = bool(metadata and metadata.get("course_id") == course_id)
        if (
            (task.tasklist_id, task.id) in known_keys
            or _is_course_task(task.title, course.prefix)
            or managed_for_course
        ):
            selected.append(task)
    return selected


def _task_ids_by_list(
    tasks: list[RemoteTask],
    *,
    additional: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    ids_by_list = {
        tasklist_id: set(task_ids)
        for tasklist_id, task_ids in (additional or {}).items()
    }
    for task in tasks:
        if task.tasklist_id:
            ids_by_list.setdefault(task.tasklist_id, set()).add(task.id)
    return {
        tasklist_id: sorted(task_ids)
        for tasklist_id, task_ids in sorted(ids_by_list.items())
    }


def _scope_tasklist_snapshots(
    snapshots: dict[str, TaskListSnapshot],
    relevant_tasks: list[RemoteTask],
) -> dict[str, TaskListSnapshot]:
    return {
        configured_title: snapshot.model_copy(
            update={
                "remote_hash": _remote_hash(
                    [
                        task
                        for task in relevant_tasks
                        if task.tasklist_id == snapshot.id
                    ]
                )
            }
        )
        for configured_title, snapshot in snapshots.items()
    }


def _is_course_task(title: str, prefix: str) -> bool:
    header = f"[{prefix}]".casefold()
    folded = title.casefold()
    return folded == header or folded.startswith(f"{header} ")


def _recent_course_tasks(
    remote_tasks: list[RemoteTask],
    course: CourseSettings,
    *,
    today: date,
) -> list[RemoteTask]:
    cutoff = (
        datetime.combine(today, time.min, tzinfo=ZoneInfo(course.timezone))
        - timedelta(days=14)
    ).astimezone(UTC)
    selected: list[RemoteTask] = []
    for task in remote_tasks:
        if task.deleted or not _is_course_task(task.title, course.prefix):
            continue
        if task.status == "completed":
            try:
                completed_at = datetime.fromisoformat((task.completed or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=UTC)
            if completed_at.astimezone(UTC) < cutoff:
                continue
        selected.append(task)
    return sorted(selected, key=lambda task: (task.title.casefold(), task.id))


def _assignment_context(tasks: list[RemoteTask]) -> list[str]:
    return [
        f"{task.title} | {task.status} | list {task.tasklist_title or 'unknown'} | "
        f"due {task.due[:10] if task.due else 'no due date'}"
        for task in tasks
    ]


class SyncService:
    def __init__(
        self,
        settings: ProjectSettings,
        *,
        credentials_loader: Callable[..., Any] = load_google_credentials,
        source_factory: Callable[..., Any] = create_course_source_adapter,
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
        target_week_start: date | None = None,
        acquisition_strategy: AcquisitionStrategy = AcquisitionStrategy.AUTO,
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
        target_week_start = target_week_start or (
            _today(course.timezone) - timedelta(days=_today(course.timezone).weekday())
        )
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
        source = self.source_factory(
            course,
            credentials,
            target_week_start=target_week_start,
            acquisition_strategy=acquisition_strategy,
        )
        tasks_client = self.tasks_client_factory(credentials)
        tasklists, remote_tasks = _read_task_lists(tasks_client, course)
        primary_snapshot = tasklists[course.task_list]
        recent_course_tasks = _recent_course_tasks(
            remote_tasks,
            course,
            today=_today(course.timezone),
        )
        existing_assignments = _assignment_context(recent_course_tasks)
        gemini_model_chain = self.settings.gemini_model_chain_for(course)
        extraction_cache_key = (
            f"{self.settings.gemini_cache_key_for(course)}"
            f"|instructions:{_stable_hash(course.ai_instructions)[:16]}"
            f"|context:{_stable_hash(existing_assignments)[:16]}"
        )
        sink.emit(
            RunStage.AUTHENTICATE_SERVICES,
            "stage_completed",
            "Google credentials and service clients are ready.",
            metadata={
                "scopes": ["Google Tasks", "Google Slides readonly"],
                "source_acquisition": course.source.type,
                "acquisition_strategy": acquisition_strategy.value,
                "target_week_start": target_week_start.isoformat(),
            },
            duration_ms=int((perf_counter() - stage_started) * 1000),
        )
        token.raise_if_cancelled()

        stage_started = perf_counter()
        capture = source.capture(include_image=False)
        sink.emit(
            RunStage.CAPTURE_SOURCE,
            "stage_completed",
            f"Captured the configured {capture.source_type.replace('_', ' ')} source.",
            metadata={
                "source_type": capture.source_type,
                "resource_id": capture.resource_id,
                "page_id": capture.page_id,
                "block_count": len(capture.blocks),
                "page_hash": capture.page_hash,
                "selection": capture.selection,
                "capture_warnings": capture.source_metadata.get("warnings", []),
                "acquisition_fallback": capture.source_metadata.get("acquisition_fallback"),
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
                model_name=extraction_cache_key,
                configured_mode=course.source.extraction.mode,
            )
            state_records = state.records(course_id, capture.source_key)
        extraction_was_cached = outcome is not None
        if outcome is None:
            effective_mode = course.source.extraction.mode
            if capture.source_metadata.get("screenshot_available") is False and effective_mode in {
                ExtractionMode.AUTO,
                ExtractionMode.HYBRID,
            }:
                effective_mode = ExtractionMode.TEXT
            needs_image = effective_mode in {
                ExtractionMode.IMAGE,
                ExtractionMode.HYBRID,
                ExtractionMode.AUTO,
            }
            if (
                effective_mode == ExtractionMode.AUTO
                and capture.source_metadata.get("screenshot_available") is False
            ):
                needs_image = False
            if needs_image:
                add_image = getattr(source, "add_image", None)
                if not callable(add_image):
                    raise RuntimeError(
                        f"Source adapter '{course.source.type}' cannot provide image extraction."
                    )
                capture = add_image(capture)
            backend = self.backend_factory(
                model=gemini_model_chain[0],
                fallback_models=gemini_model_chain[1:],
                api_key=os.getenv("GEMINI_API_KEY"),
            )
            if isinstance(backend, GoogleGenAIBackend):
                def wait_for_gemini_retry(seconds: float, attempts: list[str]) -> None:
                    sink.emit(
                        RunStage.EXTRACT_ASSIGNMENTS,
                        "gemini_retry_wait",
                        (
                            f"Waiting {seconds:g} seconds before the next Gemini model attempt."
                        ),
                        level=EventLevel.WARNING,
                        metadata={"wait_seconds": seconds, "attempts": attempts},
                    )
                    deadline = perf_counter() + seconds
                    while True:
                        token.raise_if_cancelled()
                        remaining = deadline - perf_counter()
                        if remaining <= 0:
                            return
                        sleep(min(1.0, remaining))

                backend.retry_waiter = wait_for_gemini_retry
            extraction_course = course.model_copy(deep=True)
            extraction_course.source.extraction.mode = effective_mode
            outcome = GeminiExtractor(backend).extract(
                capture,
                extraction_course,
                existing_assignments=existing_assignments,
            )
            if effective_mode != course.source.extraction.mode:
                outcome.fallback_reasons.insert(
                    0,
                    "Canvas supplied sufficient agenda text; image acquisition was not required.",
                )
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
                "model": outcome.model_name or gemini_model_chain[0],
                "configured_model_chain": gemini_model_chain,
                "model_fallback_reasons": outcome.model_fallback_reasons,
                "existing_assignment_context_count": len(existing_assignments),
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
        mapped_task_ids: dict[str, list[str]] = {}
        for record in state_records:
            if record.tasklist_id and record.google_task_id:
                mapped_task_ids.setdefault(record.tasklist_id, []).append(
                    record.google_task_id
                )
        relevant_remote_tasks = _relevant_remote_tasks(
            remote_tasks,
            course,
            course_id=course_id,
            known_task_ids=mapped_task_ids,
        )
        relevant_remote_task_ids = _task_ids_by_list(
            relevant_remote_tasks,
            additional=mapped_task_ids,
        )
        tasklists = _scope_tasklist_snapshots(tasklists, relevant_remote_tasks)
        remote_hash = _remote_hash(relevant_remote_tasks)
        tasklist_titles = [snapshot.title for snapshot in tasklists.values()]
        sink.emit(
            RunStage.COMPARE_GOOGLE_TASKS,
            "stage_completed",
            f"Read Google Tasks lists {', '.join(repr(title) for title in tasklist_titles)}.",
            metadata={
                "task_lists": tasklist_titles,
                "remote_task_count": len(remote_tasks),
                "relevant_remote_task_count": len(relevant_remote_tasks),
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
            task_list=primary_snapshot.title,
            assessment_task_list=tasklists[course.assessment_task_list].title,
            tasklist_ids={snapshot.title: snapshot.id for snapshot in tasklists.values()},
            extraction_mode=outcome.used_mode,
            fallback_reasons=outcome.fallback_reasons,
            drafts=drafts,
            uncertain=[*outcome.uncertain, *policy_uncertain],
            ignored=ignored,
            state_records=state_records,
            remote_tasks=remote_tasks,
            collision_remote_tasks=recent_course_tasks,
            course_prefix=course.prefix,
            include_past=include_past,
            dry_run=True,
        )
        plan_hash = _stable_hash(
            {
                "version": 3,
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
            tasklist_id=primary_snapshot.id,
            tasklist_title=primary_snapshot.title,
            tasklists=tasklists,
            configured_mode=course.source.extraction.mode,
            target_week_start=target_week_start,
            acquisition_strategy=acquisition_strategy,
            extraction_cache_key=extraction_cache_key,
            config_hash=config_hash,
            page_hash=capture.page_hash,
            remote_hash=remote_hash,
            relevant_remote_task_ids=relevant_remote_task_ids,
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
        if prepared.version != 3 or not prepared.tasklists:
            raise ValueError(
                "This preview predates course-scoped Google Tasks checks; create a new preview."
            )
        current_course = self.settings.course(prepared.course_id).model_copy(deep=True)
        if prepared.configured_mode:
            current_course.source.extraction.mode = prepared.configured_mode
        current_config_hash = _stable_hash(current_course.model_dump(mode="json"))
        if current_config_hash != prepared.config_hash:
            raise ValueError("Course configuration changed after this preview.")
        credentials = self.credentials_loader(self.settings.root_dir, interactive=False)
        source = self.source_factory(
            current_course,
            credentials,
            target_week_start=prepared.target_week_start,
            acquisition_strategy=prepared.acquisition_strategy,
        )
        current_capture = source.capture(include_image=False)
        tasks_client = self.tasks_client_factory(credentials)
        current_tasklists, current_remote_tasks = _read_task_lists(tasks_client, current_course)
        current_relevant_tasks = _relevant_remote_tasks(
            current_remote_tasks,
            current_course,
            course_id=prepared.course_id,
            known_task_ids=prepared.relevant_remote_task_ids,
        )
        current_tasklists = _scope_tasklist_snapshots(
            current_tasklists,
            current_relevant_tasks,
        )
        current_remote_hash = _remote_hash(current_relevant_tasks)
        if current_capture.page_hash != prepared.page_hash:
            raise ValueError("The source page changed after this preview.")
        if current_remote_hash != prepared.remote_hash:
            raise ValueError("Google Tasks changed after this preview.")
        if {
            key: (snapshot.id, snapshot.remote_hash)
            for key, snapshot in current_tasklists.items()
        } != {
            key: (snapshot.id, snapshot.remote_hash)
            for key, snapshot in prepared.tasklists.items()
        }:
            raise ValueError("A configured Google Tasks list changed after this preview.")
        sink.emit(
            RunStage.REVALIDATE_PREVIEW,
            "stage_completed",
            "The source, configuration, and Google Tasks still match the preview.",
            metadata={
                "config_hash": current_config_hash,
                "page_hash": current_capture.page_hash,
                "remote_hash": current_remote_hash,
                "task_lists": [snapshot.title for snapshot in current_tasklists.values()],
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
                        SyncActionKind.NOTES_CLEANUP,
                    }
                ]
            completed_indexes: list[int] = []
            applied_counts: Counter[str] = Counter()
            state_path = self.settings.resolved_state_path
            snapshots_by_title = {
                snapshot.title: snapshot for snapshot in prepared.tasklists.values()
            }
            with StateStore(state_path, writable=True) as state:
                for index, action in enumerate(writable_plan.actions):
                    if action.kind not in {
                        SyncActionKind.CREATE,
                        SyncActionKind.UPDATE,
                        SyncActionKind.UNCHANGED,
                        SyncActionKind.NOTES_CLEANUP,
                    }:
                        continue
                    if not action.task_list or action.task_list not in snapshots_by_title:
                        raise RuntimeError(
                            f"Action for '{action.title}' has no resolved destination list."
                        )
                    destination = snapshots_by_title[action.task_list]
                    single = SyncPlan(
                        course_id=writable_plan.course_id,
                        task_list=writable_plan.task_list,
                        task_lists=writable_plan.task_lists,
                        dry_run=False,
                        extraction_mode=writable_plan.extraction_mode,
                        fallback_reasons=writable_plan.fallback_reasons,
                        actions=[action],
                    )
                    stage_started = perf_counter()
                    apply_sync_plan(
                        single,
                        tasklist_id=destination.id,
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
                                "task_list": action.task_list,
                                "due_date": (
                                    action.due_date.isoformat() if action.due_date else None
                                ),
                                "due_uncertain": action.due_uncertain,
                                "due_uncertain_reason": action.due_uncertain_reason,
                                "due_verified": action.due_verified,
                            }
                        ),
                        duration_ms=int((perf_counter() - stage_started) * 1000),
                    )
                if not prepared.extraction_was_cached:
                    state.cache_extraction(
                        course_id=prepared.course_id,
                        source_key=prepared.source_key,
                        page_hash=prepared.page_hash,
                        extractor_version=EXTRACTOR_VERSION,
                        model_name=(
                            prepared.extraction_cache_key or self.settings.gemini_cache_key
                        ),
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
