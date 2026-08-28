from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from canvas_task_sync.configuration import CourseSettings
from canvas_task_sync.models import (
    AcquisitionStrategy,
    ActionKind,
    ExtractionMode,
    SyncPlan,
    TaskClassification,
    TaskType,
    WeekSelection,
)
from canvas_task_sync.web_constants import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    APPLYING = "applying"
    SUCCEEDED = "succeeded"
    REVIEW_NEEDED = "review_needed"
    STALE = "stale"
    CANCELLED = "cancelled"
    FAILED = "failed"
    FAILED_PARTIAL = "failed_partial"


class RunStage(StrEnum):
    QUEUED = "queued"
    VALIDATE_CONFIGURATION = "validate_configuration"
    AUTHENTICATE_SERVICES = "authenticate_services"
    CAPTURE_SOURCE = "capture_source"
    EXTRACT_ASSIGNMENTS = "extract_assignments"
    CALCULATE_DEADLINES = "calculate_deadlines"
    COMPARE_GOOGLE_TASKS = "compare_google_tasks"
    BUILD_REVIEW_PLAN = "build_review_plan"
    REVALIDATE_PREVIEW = "revalidate_preview"
    APPLY_CHANGES = "apply_changes"
    PERSIST_STATE = "persist_state"
    HEALTH_CHECK = "health_check"
    COMPLETE = "complete"


class RunTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"


class RunMode(StrEnum):
    PREVIEW = "preview"
    AUTO_APPLY = "auto_apply"
    HEALTH = "health"


class EventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ScheduleMode(StrEnum):
    PREVIEW = "preview"
    AUTO_APPLY = "auto_apply"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    ERROR = "error"
    MISSING = "missing"


class RunCreate(BaseModel):
    course_id: str
    mode: RunMode = RunMode.AUTO_APPLY
    week_selection: WeekSelection = WeekSelection.CURRENT
    acquisition_strategy: AcquisitionStrategy = AcquisitionStrategy.AUTO
    extraction_mode: ExtractionMode | None = None
    include_past: bool = False
    test_rebase_week: date | None = None

    @field_validator("test_rebase_week")
    @classmethod
    def validate_rebase_week(cls, value: date | None) -> date | None:
        if value is not None and value.weekday() != 0:
            raise ValueError("The rebased week date must be a Monday.")
        return value

    @field_validator("mode")
    @classmethod
    def validate_manual_mode(cls, value: RunMode) -> RunMode:
        if value == RunMode.HEALTH:
            raise ValueError("Manual sync mode must be preview or auto_apply.")
        return value


class RunAllCreate(BaseModel):
    mode: RunMode = RunMode.AUTO_APPLY
    include_past: bool = False
    week_selection: WeekSelection = WeekSelection.CURRENT
    acquisition_strategy: AcquisitionStrategy = AcquisitionStrategy.AUTO

    @field_validator("mode")
    @classmethod
    def validate_manual_mode(cls, value: RunMode) -> RunMode:
        if value == RunMode.HEALTH:
            raise ValueError("Manual sync mode must be preview or auto_apply.")
        return value


class CaptureFailure(BaseModel):
    code: str = Field(default="automatic_capture_failed", min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)


class RunApply(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)


class RunEvent(BaseModel):
    id: int
    run_id: int
    sequence: int
    created_at: datetime
    stage: RunStage
    event_type: str
    level: EventLevel
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None


class RunSummary(BaseModel):
    id: int
    operation_id: str = ""
    course_id: str
    course_name: str | None = None
    trigger: RunTrigger
    requested_mode: RunMode
    status: RunStatus
    stage: RunStage
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    extraction_mode: ExtractionMode | None = None
    week_selection: WeekSelection = WeekSelection.CURRENT
    target_week_start: date | None = None
    acquisition_strategy: AcquisitionStrategy = AcquisitionStrategy.AUTO
    counts: dict[str, int] = Field(default_factory=dict)
    applied_counts: dict[str, int] = Field(default_factory=dict)
    plan_hash: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    schedule_id: int | None = None


class RunDetail(RunSummary):
    include_past: bool = False
    test_rebase_week: date | None = None
    config_hash: str | None = None
    page_hash: str | None = None
    remote_hash: str | None = None
    cancel_requested: bool = False
    plan: SyncPlan | None = None
    events: list[RunEvent] = Field(default_factory=list)


class CourseView(BaseModel):
    id: str
    settings: CourseSettings
    readiness: HealthState = HealthState.WARNING
    readiness_message: str = "Not checked"


class CourseSave(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    settings: CourseSettings


class ScheduleBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    course_id: str
    weekdays: list[int] = Field(min_length=1)
    local_time: time
    timezone: str
    mode: ScheduleMode = ScheduleMode.PREVIEW
    enabled: bool = True

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, values: list[int]) -> list[int]:
        normalized = sorted(set(values))
        if any(value < 0 or value > 6 for value in normalized):
            raise ValueError("Weekdays must be integers from 0 through 6.")
        return normalized

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Timezone must be a valid IANA timezone name.") from error
        return value


class ScheduleCreate(ScheduleBase):
    pass


class ScheduleUpdate(ScheduleBase):
    pass


class Schedule(ScheduleBase):
    id: int
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_result: str | None = None
    created_at: datetime
    updated_at: datetime


class ScheduleOccurrence(BaseModel):
    id: int
    schedule_id: int
    scheduled_for: datetime
    status: str
    run_id: int | None = None
    details: str = ""
    created_at: datetime


class ConnectionItem(BaseModel):
    key: str
    label: str
    state: HealthState
    summary: str
    details: str | None = None
    checked_at: datetime | None = None


class ConnectionStatus(BaseModel):
    google_client_configured: bool
    google_authorized: bool
    gemini_configured: bool
    local_server: str = f"{DEFAULT_WEB_HOST}:{DEFAULT_WEB_PORT}"
    checks: list[ConnectionItem] = Field(default_factory=list)


class HealthCheck(BaseModel):
    key: str
    label: str
    state: HealthState
    summary: str
    duration_ms: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class OverviewResponse(BaseModel):
    selected_course_id: str | None
    courses: list[CourseView]
    connections: ConnectionStatus
    latest_run: RunSummary | None = None
    recent_runs: list[RunSummary] = Field(default_factory=list)
    next_schedule: Schedule | None = None


class TrackedTaskCourse(BaseModel):
    id: str
    name: str
    prefix: str
    canvas_course_id: str | None = None
    canvas_base_url: str | None = None
    canvas_url: str | None = None


class TrackedTaskSource(BaseModel):
    key: str
    type: str
    url: str | None = None
    anchor: str
    text: str
    assignment_url: str | None = None


class TrackedTaskGoogleIdentity(BaseModel):
    task_id: str | None = None
    tasklist_id: str | None = None
    tasklist_title: str | None = None
    status: str = "unknown"
    completed_at: str | None = None
    deleted: bool = False
    hidden: bool = False


class TrackedTaskCanvasIdentity(BaseModel):
    course_id: str | None = None
    assignment_id: str | None = None
    course_url: str | None = None
    assignment_url: str | None = None


class TrackedTaskView(BaseModel):
    logical_id: str
    course: TrackedTaskCourse
    title: str
    display_title: str
    details: str = ""
    due_date: date | None = None
    completed: bool | None = None
    completion_status: str
    classification: TaskClassification | None = None
    task_type: TaskType | None = None
    action_kind: ActionKind | None = None
    due_basis: str | None = None
    due_uncertain: bool = False
    due_uncertain_reason: str | None = None
    source_date: date | None = None
    historical: bool = False
    google_task: TrackedTaskGoogleIdentity
    source: TrackedTaskSource
    canvas: TrackedTaskCanvasIdentity


class DiagnosticsResponse(BaseModel):
    checks: list[ConnectionItem]
    recent_events: list[RunEvent]
    error_runs: list[RunSummary]
    control_database: str
    state_database: str


class GeminiKeyUpdate(BaseModel):
    api_key: str = Field(min_length=8, max_length=4096)


class GeneralSettings(BaseModel):
    history_retention_days: int = Field(default=90, ge=1, le=3650)


class ApiErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    field_errors: dict[str, list[str]] | None = None
    run_id: int | None = None


class ApiError(BaseModel):
    error: ApiErrorDetail
