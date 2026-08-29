from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import uuid
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles

from canvas_task_sync.auth import load_google_credentials
from canvas_task_sync.browser_capture import (
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_TEXT_BYTES,
    AcquisitionMode,
    BrowserCaptureBroker,
    BrowserCaptureEnvelope,
    BrowserCaptureError,
    resource_id_from_url,
    source_type_from_url,
)
from canvas_task_sync.configuration import CourseSettings, ProjectSettings
from canvas_task_sync.configuration_service import (
    MAX_CREDENTIAL_FILE_BYTES,
    ConfigurationService,
)
from canvas_task_sync.control_store import ControlStore
from canvas_task_sync.health import connection_status, run_health_checks
from canvas_task_sync.models import AcquisitionStrategy
from canvas_task_sync.redaction import safe_exception_summary, sanitize
from canvas_task_sync.run_manager import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    RunManager,
    ScheduleManager,
    next_schedule_occurrence,
)
from canvas_task_sync.sources import create_course_source_adapter, create_source_adapter
from canvas_task_sync.sources.browser_connector import (
    automatic_acquisition_mode,
    extension_selection,
)
from canvas_task_sync.sync_service import SyncService
from canvas_task_sync.tracked_tasks import TrackedTaskReader
from canvas_task_sync.web_constants import DEFAULT_SIMPLE_WEB_PORT, DEFAULT_WEB_PORT
from canvas_task_sync.web_models import (
    ApiErrorDetail,
    BrowserResourceRead,
    CaptureFailure,
    CourseSave,
    CourseView,
    DiagnosticsResponse,
    GeminiKeyUpdate,
    GeneralSettings,
    HealthState,
    OverviewResponse,
    RunAllCreate,
    RunApply,
    RunCreate,
    RunStatus,
    Schedule,
    ScheduleCreate,
    ScheduleUpdate,
    TrackedTaskView,
)


class WebRuntime:
    def __init__(self, config_path: Path, *, port: int) -> None:
        self.port = port
        self.configuration = ConfigurationService(config_path)
        self.settings = self.configuration.load()
        self.store = ControlStore(self.settings.root_dir / ".canvas-task-sync" / "control.sqlite3")
        self.capture_broker = BrowserCaptureBroker()
        pairing_token = self.store.get_setting("extension_pairing_token")
        if not isinstance(pairing_token, str) or len(pairing_token) < 32:
            pairing_token = secrets.token_urlsafe(32)
            self.store.set_setting("extension_pairing_token", pairing_token)
        self.extension_pairing_token = pairing_token
        self.sync_service = SyncService(
            self.settings,
            source_factory=self.create_source_adapter,
        )
        self.tracked_tasks = TrackedTaskReader(
            self.settings,
            credentials_loader=self.sync_service.credentials_loader,
            tasks_client_factory=self.sync_service.tasks_client_factory,
        )
        self.runs = RunManager(self.store, self.sync_service)
        self.schedules = ScheduleManager(self.store, self.runs)
        self.csrf_token = secrets.token_urlsafe(32)
        self.retention_task: asyncio.Task[None] | None = None

    def reload_settings(self) -> ProjectSettings:
        self.settings = self.configuration.load()
        self.sync_service.settings = self.settings
        self.tracked_tasks.settings = self.settings
        return self.settings

    def create_source_adapter(self, settings: Any, credentials: Any, **kwargs: Any) -> Any:
        if isinstance(settings, CourseSettings):
            return create_course_source_adapter(
                settings,
                credentials,
                capture_broker=self.capture_broker,
                **kwargs,
            )
        return create_source_adapter(
            settings,
            credentials,
            capture_broker=self.capture_broker,
            **kwargs,
        )

    def rotate_extension_pairing_token(self) -> str:
        self.extension_pairing_token = secrets.token_urlsafe(32)
        self.store.set_setting("extension_pairing_token", self.extension_pairing_token)
        return self.extension_pairing_token

    async def start(self) -> None:
        self.store.prune_history()
        await self.runs.start()
        await self.schedules.start()
        self.retention_task = asyncio.create_task(self._retention_loop(), name="history-retention")

    async def stop(self) -> None:
        if self.retention_task is not None:
            self.retention_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.retention_task
            self.retention_task = None
        await self.schedules.stop()
        await self.runs.stop()
        self.capture_broker.clear()
        self.store.close()

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(24 * 60 * 60)
            self.store.prune_history()


def create_web_app(
    config_path: Path,
    *,
    port: int = DEFAULT_WEB_PORT,
    simple_port: int = DEFAULT_SIMPLE_WEB_PORT,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = WebRuntime(config_path, port=port)
        app.state.runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="Canvas Task Sync Control Center",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            f"http://127.0.0.1:{simple_port}",
            f"http://localhost:{simple_port}",
        ],
        allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Accept",
            "Content-Type",
            "X-CSRF-Token",
            "X-Extension-Token",
            "Last-Event-ID",
        ],
    )

    @app.middleware("http")
    async def local_mutation_guard(request: Request, call_next):
        host = request.headers.get("host", "").casefold()
        allowed_hosts = {
            "127.0.0.1",
            f"127.0.0.1:{port}",
            "localhost",
            f"localhost:{port}",
            "testserver",
        }
        if host not in allowed_hosts:
            return _error_response(
                status_code=400,
                code="host_not_allowed",
                message="This control center accepts loopback requests only.",
            )
        is_mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        is_extension_api = request.url.path.startswith("/api/v1/extension/")
        if is_extension_api:
            runtime = _runtime(request)
            origin = request.headers.get("origin")
            if origin and not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
                return _error_response(
                    status_code=403,
                    code="extension_origin_not_allowed",
                    message="The extension bridge accepts Chrome extension requests only.",
                )
            if request.method != "OPTIONS" and not hmac.compare_digest(
                request.headers.get("x-extension-token", ""),
                runtime.extension_pairing_token,
            ):
                return _error_response(
                    status_code=401,
                    code="extension_pairing_failed",
                    message="The extension pairing token is missing or no longer valid.",
                )
            content_length = request.headers.get("content-length")
            maximum_request_bytes = MAX_CAPTURE_TEXT_BYTES + (MAX_CAPTURE_BYTES * 2)
            if (
                request.method == "POST"
                and content_length
                and content_length.isdigit()
                and int(content_length) > maximum_request_bytes
            ):
                return _error_response(
                    status_code=413,
                    code="capture_too_large",
                    message="The browser capture exceeds the local bridge size limit.",
                )
        elif request.url.path.startswith("/api/v1/") and is_mutation:
            runtime = _runtime(request)
            origin = request.headers.get("origin")
            allowed_origins = {
                f"http://127.0.0.1:{runtime.port}",
                f"http://localhost:{runtime.port}",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                f"http://127.0.0.1:{simple_port}",
                f"http://localhost:{simple_port}",
            }
            if origin and origin not in allowed_origins:
                return _error_response(
                    status_code=403,
                    code="origin_not_allowed",
                    message="This control center only accepts local same-origin mutations.",
                )
            if request.headers.get("x-csrf-token") != runtime.csrf_token:
                return _error_response(
                    status_code=403,
                    code="csrf_failed",
                    message="Refresh the control center and try the action again.",
                )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, error: RequestValidationError):
        field_errors: dict[str, list[str]] = {}
        # Only loc/msg are read so secret-bearing request input and context never echo.
        for item in error.errors():
            location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            field_errors.setdefault(location or "request", []).append(
                str(item.get("msg", "Invalid value"))
            )
        return _error_response(
            status_code=422,
            code="validation_error",
            message="One or more fields need attention.",
            field_errors=field_errors,
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(_request: Request, error: HTTPException):
        if isinstance(error.detail, dict) and "code" in error.detail:
            detail = error.detail
            return _error_response(
                status_code=error.status_code,
                code=str(detail["code"]),
                message=str(detail.get("message", "Request failed.")),
                retryable=bool(detail.get("retryable", False)),
                run_id=detail.get("run_id"),
            )
        return _error_response(
            status_code=error.status_code,
            code="request_failed",
            message=str(error.detail),
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, error: ValueError):
        return _error_response(
            status_code=400,
            code="invalid_request",
            message=safe_exception_summary(error),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, error: Exception):
        return _error_response(
            status_code=500,
            code="internal_error",
            message=safe_exception_summary(error),
            retryable=False,
        )

    api = APIRouter(prefix="/api/v1")

    @api.get("/bootstrap")
    def bootstrap(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        return {
            "csrf_token": runtime.csrf_token,
            "server": f"127.0.0.1:{runtime.port}",
            "api_version": 1,
        }

    @api.get("/overview", response_model=OverviewResponse)
    def overview(request: Request, course_id: str | None = None) -> OverviewResponse:
        runtime = _runtime(request)
        courses = _course_views(runtime)
        selected = (
            course_id
            if course_id in runtime.settings.courses
            else _default_course(runtime.settings)
        )
        recent = _annotate_runs(runtime, runtime.store.list_runs(limit=5))
        latest = _annotate_run(runtime, runtime.store.latest_run(selected))
        next_schedule = next(
            (
                schedule
                for schedule in sorted(
                    runtime.store.list_schedules(enabled_only=True),
                    key=lambda item: (
                        item.next_run_at is None,
                        item.next_run_at.isoformat() if item.next_run_at else "",
                    ),
                )
                if selected is None or schedule.course_id == selected
            ),
            None,
        )
        return OverviewResponse(
            selected_course_id=selected,
            courses=courses,
            connections=connection_status(runtime.settings, port=runtime.port),
            latest_run=latest,
            recent_runs=recent,
            next_schedule=next_schedule,
        )

    @api.get("/courses", response_model=list[CourseView])
    def list_courses(request: Request) -> list[CourseView]:
        return _course_views(_runtime(request))

    @api.get("/tasks", response_model=list[TrackedTaskView])
    def list_tracked_tasks(
        request: Request,
        completed: bool | None = None,
        course_id: str | None = None,
    ) -> list[TrackedTaskView]:
        return _runtime(request).tracked_tasks.list(completed=completed, course_id=course_id)

    @api.get("/tasks/{logical_id}", response_model=TrackedTaskView)
    def get_tracked_task(request: Request, logical_id: str) -> TrackedTaskView:
        task = _runtime(request).tracked_tasks.get(logical_id)
        if task is None:
            raise _http_error(404, "task_not_found", "Tracked task was not found.")
        return task

    @api.post("/courses", response_model=list[CourseView], status_code=201)
    def create_course(request: Request, payload: CourseSave) -> list[CourseView]:
        runtime = _runtime(request)
        runtime.configuration.save_course(payload, creating=True)
        runtime.reload_settings()
        return _course_views(runtime)

    @api.put("/courses/{course_id}", response_model=list[CourseView])
    def update_course(request: Request, course_id: str, payload: CourseSave) -> list[CourseView]:
        if payload.id != course_id:
            raise _http_error(
                409,
                "immutable_course_id",
                "Course IDs cannot be changed after creation.",
            )
        runtime = _runtime(request)
        runtime.configuration.save_course(payload, creating=False)
        runtime.reload_settings()
        return _course_views(runtime)

    @api.delete("/courses/{course_id}", response_model=list[CourseView])
    def delete_course(request: Request, course_id: str) -> list[CourseView]:
        runtime = _runtime(request)
        if runtime.store.list_runs(
            limit=1,
            course_id=course_id,
            statuses=ACTIVE_STATUSES,
        ):
            raise _http_error(
                409,
                "course_run_active",
                "This course has an active run. Cancel or wait for it to finish before "
                "deleting the course.",
            )
        runtime.configuration.delete_course(course_id)
        runtime.store.delete_schedules_for_course(course_id)
        runtime.reload_settings()
        return _course_views(runtime)

    @api.post("/courses/{course_id}/disable", response_model=list[CourseView])
    def disable_course(request: Request, course_id: str) -> list[CourseView]:
        runtime = _runtime(request)
        runtime.configuration.set_course_enabled(course_id, False)
        runtime.store.disable_schedules_for_course(course_id)
        runtime.reload_settings()
        return _course_views(runtime)

    @api.post("/courses/{course_id}/enable", response_model=list[CourseView])
    def enable_course(request: Request, course_id: str) -> list[CourseView]:
        runtime = _runtime(request)
        runtime.configuration.set_course_enabled(course_id, True)
        runtime.reload_settings()
        return _course_views(runtime)

    @api.post("/courses/{course_id}/test")
    async def test_course(request: Request, course_id: str) -> dict[str, Any]:
        runtime = _runtime(request)
        runtime.settings.course(course_id)
        checks = await asyncio.to_thread(
            run_health_checks,
            runtime.settings,
            course_id,
            capture_broker=runtime.capture_broker,
        )
        return {"checks": checks}

    @api.get("/courses-config")
    def sanitized_course_config(request: Request) -> dict[str, Any]:
        return sanitize(_runtime(request).configuration.sanitized_document())

    @api.get("/runs")
    def list_runs(
        request: Request,
        course_id: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        runtime = _runtime(request)
        safe_limit = min(max(limit, 1), 200)
        statuses = [status] if status else None
        return [run.model_dump(mode="json") for run in _annotate_runs(
            runtime,
            runtime.store.list_runs(limit=safe_limit, course_id=course_id, statuses=statuses),
        )]

    @api.get("/runs/{run_id}")
    def get_run(request: Request, run_id: int) -> dict[str, Any]:
        runtime = _runtime(request)
        run = runtime.store.get_run(run_id)
        if run is None:
            raise _http_error(404, "run_not_found", "Run was not found.")
        return _annotate_run(runtime, run).model_dump(mode="json")

    @api.post("/runs", status_code=202)
    def create_run(request: Request, payload: RunCreate) -> dict[str, Any]:
        runtime = _runtime(request)
        course = runtime.settings.course(payload.course_id)
        if not course.enabled:
            raise _http_error(409, "course_disabled", "Enable this course before running it.")
        capture_request = _queue_automatic_capture(
            runtime,
            course,
            extraction_mode=payload.extraction_mode,
            acquisition_strategy=payload.acquisition_strategy,
        )
        operation_id = str(uuid.uuid4())
        run_id = runtime.runs.create_preview(payload, operation_id=operation_id)
        return {
            "run_id": run_id,
            "operation_id": operation_id,
            "status": "queued",
            "capture_request_id": (
                capture_request["request_id"] if capture_request is not None else None
            ),
        }

    @api.post("/runs/all", status_code=202)
    def create_all_runs(request: Request, payload: RunAllCreate) -> dict[str, Any]:
        runtime = _runtime(request)
        operation_id = str(uuid.uuid4())
        run_ids: list[int] = []
        capture_request_ids: list[str] = []
        for course_id, course in sorted(runtime.settings.courses.items()):
            if not course.enabled:
                continue
            capture_request = _queue_automatic_capture(
                runtime,
                course,
                acquisition_strategy=payload.acquisition_strategy,
            )
            if capture_request is not None:
                capture_request_ids.append(str(capture_request["request_id"]))
            run_ids.append(
                runtime.runs.create_preview(
                    RunCreate(
                        course_id=course_id,
                        mode=payload.mode,
                        include_past=payload.include_past,
                        week_selection=payload.week_selection,
                        acquisition_strategy=payload.acquisition_strategy,
                    ),
                    operation_id=operation_id,
                )
            )
        if not run_ids:
            raise _http_error(409, "no_enabled_courses", "Enable at least one course first.")
        return {
            "run_ids": run_ids,
            "operation_id": operation_id,
            "status": "queued",
            "capture_request_ids": capture_request_ids,
        }

    @api.get("/operations")
    def list_operations(request: Request, limit: int = 50) -> list[dict[str, Any]]:
        runtime = _runtime(request)
        runs = _annotate_runs(runtime, runtime.store.list_runs(limit=500))
        grouped: dict[str, list[Any]] = {}
        for run in runs:
            grouped.setdefault(run.operation_id, []).append(run)
        result: list[dict[str, Any]] = []
        for operation_id, operation_runs in list(grouped.items())[: min(max(limit, 1), 100)]:
            statuses = {run.status for run in operation_runs}
            if statuses & ACTIVE_STATUSES:
                status = RunStatus.RUNNING
            elif RunStatus.FAILED_PARTIAL in statuses:
                status = RunStatus.FAILED_PARTIAL
            elif RunStatus.FAILED in statuses:
                status = RunStatus.FAILED
            elif statuses & {RunStatus.REVIEW_NEEDED, RunStatus.STALE}:
                status = RunStatus.REVIEW_NEEDED
            elif RunStatus.AWAITING_APPROVAL in statuses:
                status = RunStatus.AWAITING_APPROVAL
            elif RunStatus.CANCELLED in statuses:
                status = RunStatus.CANCELLED
            else:
                status = RunStatus.SUCCEEDED
            result.append(
                {
                    "id": operation_id,
                    "run_ids": [run.id for run in operation_runs],
                    "course_ids": [run.course_id for run in operation_runs],
                    "course_names": [run.course_name or run.course_id for run in operation_runs],
                    "status": status.value,
                    "created_at": min(run.created_at for run in operation_runs),
                    "finished_at": max(
                        (run.finished_at for run in operation_runs if run.finished_at),
                        default=None,
                    ),
                }
            )
        return result

    @api.get("/operations/{operation_id}/events", response_class=EventSourceResponse)
    async def operation_events(
        request: Request,
        operation_id: str,
        last_event_id: int | None = Header(default=None),
    ) -> AsyncIterable[ServerSentEvent]:
        runtime = _runtime(request)
        if not runtime.store.runs_for_operation(operation_id):
            raise _http_error(404, "operation_not_found", "Sync operation was not found.")
        cursor = last_event_id or 0
        while True:
            events = runtime.store.operation_events_after(operation_id, cursor)
            for event in events:
                cursor = int(event["id"])
                course = runtime.settings.courses.get(str(event["course_id"]))
                event["operation_id"] = operation_id
                event["course_name"] = course.name if course else event["course_id"]
                yield ServerSentEvent(
                    data=event,
                    event="log",
                    id=str(cursor),
                    retry=1000,
                )
            operation_runs = runtime.store.runs_for_operation(operation_id)
            if all(run.status in TERMINAL_STATUSES for run in operation_runs) and not events:
                yield ServerSentEvent(
                    data={"operation_id": operation_id},
                    event="complete",
                    id=str(cursor),
                )
                break
            if await request.is_disconnected():
                break
            await runtime.runs.wait_for_events(timeout=1.0)

    @api.post("/runs/{run_id}/apply", status_code=202)
    def apply_run(request: Request, run_id: int, payload: RunApply) -> dict[str, Any]:
        runtime = _runtime(request)
        try:
            runtime.runs.queue_apply(run_id, plan_hash=payload.plan_hash)
        except KeyError:
            raise _http_error(404, "run_not_found", "Run was not found.") from None
        except ValueError as error:
            raise _http_error(409, "apply_not_allowed", str(error), run_id=run_id) from None
        return {"run_id": run_id, "status": "queued"}

    @api.post("/runs/{run_id}/cancel", status_code=202)
    def cancel_run(request: Request, run_id: int) -> dict[str, Any]:
        runtime = _runtime(request)
        if runtime.store.get_run(run_id, include_events=False) is None:
            raise _http_error(404, "run_not_found", "Run was not found.")
        if not runtime.runs.cancel(run_id):
            raise _http_error(
                409,
                "cancel_not_allowed",
                "This run has finished or already started writing changes.",
                run_id=run_id,
            )
        return {"run_id": run_id, "status": "cancellation_requested"}

    def require_existing_run(request: Request, run_id: int) -> None:
        runtime = _runtime(request)
        if runtime.store.get_run(run_id, include_events=False) is None:
            raise _http_error(404, "run_not_found", "Run was not found.")

    @api.get("/runs/{run_id}/events", response_class=EventSourceResponse)
    async def run_events(
        request: Request,
        run_id: int,
        last_event_id: int | None = Header(default=None),
        _existing_run: None = Depends(require_existing_run),
    ) -> AsyncIterable[ServerSentEvent]:
        runtime = _runtime(request)
        cursor = last_event_id or 0
        while True:
            events = runtime.store.events_after(run_id, cursor)
            for event in events:
                cursor = event.sequence
                yield ServerSentEvent(
                    data=event.model_dump(mode="json"),
                    event=event.event_type,
                    id=str(event.sequence),
                    retry=1000,
                )
            run = runtime.store.get_run(run_id, include_events=False)
            if run is None or (run.status in TERMINAL_STATUSES and not events):
                break
            if await request.is_disconnected():
                break
            await runtime.runs.wait_for_events(timeout=1.0)

    @api.post("/health-runs", status_code=202)
    def create_health_run(request: Request, course_id: str | None = None) -> dict[str, Any]:
        runtime = _runtime(request)
        if course_id:
            runtime.settings.course(course_id)
        run_id = runtime.runs.create_health(course_id=course_id)
        return {"run_id": run_id, "status": "queued"}

    @api.get("/schedules")
    def list_schedules(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        return {
            "items": runtime.store.list_schedules(),
            "occurrences": runtime.store.list_schedule_occurrences(limit=50),
        }

    @api.post("/schedules", response_model=Schedule, status_code=201)
    def create_schedule(request: Request, payload: ScheduleCreate) -> Schedule:
        runtime = _runtime(request)
        course = runtime.settings.course(payload.course_id)
        if payload.enabled and not course.enabled:
            raise _http_error(409, "course_disabled", "Enable this course first.")
        next_run = (
            next_schedule_occurrence(
                weekdays=payload.weekdays,
                local_time=payload.local_time,
                timezone_name=payload.timezone,
            )
            if payload.enabled
            else None
        )
        return runtime.store.create_schedule(payload, next_run_at=next_run)

    @api.put("/schedules/{schedule_id}", response_model=Schedule)
    def update_schedule(
        request: Request,
        schedule_id: int,
        payload: ScheduleUpdate,
    ) -> Schedule:
        runtime = _runtime(request)
        course = runtime.settings.course(payload.course_id)
        if payload.enabled and not course.enabled:
            raise _http_error(409, "course_disabled", "Enable this course first.")
        next_run = (
            next_schedule_occurrence(
                weekdays=payload.weekdays,
                local_time=payload.local_time,
                timezone_name=payload.timezone,
            )
            if payload.enabled
            else None
        )
        schedule = runtime.store.update_schedule(schedule_id, payload, next_run_at=next_run)
        if schedule is None:
            raise _http_error(404, "schedule_not_found", "Schedule was not found.")
        return schedule

    @api.delete("/schedules/{schedule_id}", status_code=204)
    def delete_schedule(request: Request, schedule_id: int) -> Response:
        if not _runtime(request).store.delete_schedule(schedule_id):
            raise _http_error(404, "schedule_not_found", "Schedule was not found.")
        return Response(status_code=204)

    @api.post("/schedules/{schedule_id}/enable", response_model=Schedule)
    def enable_schedule(request: Request, schedule_id: int) -> Schedule:
        runtime = _runtime(request)
        schedule = runtime.store.get_schedule(schedule_id)
        if schedule is None:
            raise _http_error(404, "schedule_not_found", "Schedule was not found.")
        course = runtime.settings.course(schedule.course_id)
        if not course.enabled:
            raise _http_error(409, "course_disabled", "Enable this course first.")
        next_run = next_schedule_occurrence(
            weekdays=schedule.weekdays,
            local_time=schedule.local_time,
            timezone_name=schedule.timezone,
        )
        enabled = runtime.store.set_schedule_enabled(
            schedule_id,
            enabled=True,
            next_run_at=next_run,
        )
        if enabled is None:
            raise _http_error(404, "schedule_not_found", "Schedule was not found.")
        return enabled

    @api.post("/schedules/{schedule_id}/disable", response_model=Schedule)
    def disable_schedule(request: Request, schedule_id: int) -> Schedule:
        disabled = _runtime(request).store.set_schedule_enabled(
            schedule_id,
            enabled=False,
            next_run_at=None,
        )
        if disabled is None:
            raise _http_error(404, "schedule_not_found", "Schedule was not found.")
        return disabled

    @api.get("/settings/connections")
    def get_connections(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        return {
            "connections": connection_status(runtime.settings, port=runtime.port),
            "general": {
                "history_retention_days": runtime.store.get_setting(
                    "history_retention_days", 90
                )
            },
            "paths": {
                "control_database": str(runtime.store.path),
                "state_database": str(runtime.settings.resolved_state_path),
                "config": str(runtime.configuration.config_path),
            },
        }

    @api.get("/settings/extension")
    def get_extension_setup(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        return {
            "server_url": f"http://127.0.0.1:{runtime.port}",
            "pairing_token": runtime.extension_pairing_token,
            "capture_ttl_seconds": runtime.capture_broker.ttl_seconds,
            "supported_sources": ["google_slides", "google_docs", "google_sheets", "web_page"],
            "load_unpacked_path": str(runtime.settings.root_dir / "extension" / "dist"),
            "captures": [
                status.as_dict() for status in runtime.capture_broker.list_statuses()
            ],
            "capture_requests": runtime.capture_broker.list_capture_requests(),
        }

    @api.post("/settings/extension/rotate")
    def rotate_extension_pairing(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        cleared = runtime.capture_broker.clear()
        return {
            "pairing_token": runtime.rotate_extension_pairing_token(),
            "cleared_captures": cleared,
        }

    @api.delete("/settings/extension/captures", status_code=204)
    def clear_extension_captures(request: Request) -> Response:
        _runtime(request).capture_broker.clear()
        return Response(status_code=204)

    @api.get("/extension/status")
    def extension_status(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        return {
            "connected": True,
            "api_version": 1,
            "capture_ttl_seconds": runtime.capture_broker.ttl_seconds,
            "supported_sources": ["google_slides", "google_docs", "google_sheets", "web_page"],
            "captures": [
                status.as_dict() for status in runtime.capture_broker.list_statuses()
            ],
            "capture_requests": runtime.capture_broker.list_capture_requests(),
        }

    @api.post("/agent/browser-resources/read")
    def read_browser_resource(
        request: Request,
        payload: BrowserResourceRead,
    ) -> dict[str, Any]:
        runtime = _runtime(request)
        try:
            source_type = source_type_from_url(payload.url)
            resource_id = resource_id_from_url(payload.url)
            try:
                capture = runtime.capture_broker.get(source_type, resource_id)
                capture_status = "cached"
            except BrowserCaptureError as cache_error:
                if cache_error.code not in {"capture_missing", "capture_stale"}:
                    raise
                capture_request = runtime.capture_broker.request_capture(
                    payload.url,
                    AcquisitionMode.TEXT,
                    {},
                )
                capture = runtime.capture_broker.wait_for_capture(
                    source_type,
                    resource_id,
                    request_id=capture_request["request_id"],
                    timeout_seconds=payload.timeout_seconds,
                )
                capture_status = "captured"
        except ValueError as error:
            raise _http_error(422, "browser_resource_url_invalid", str(error)) from None
        except BrowserCaptureError as error:
            status = 504 if error.code == "automatic_capture_timeout" else 409
            raise _http_error(status, error.code, str(error)) from None

        readable_items = [
            item.model_dump(mode="json", exclude_none=True)
            for item in capture.items
            if item.text.strip() or item.structured_data is not None
        ]
        content = "\n\n".join(
            item.text.strip() for item in capture.items if item.text.strip()
        )
        return {
            "ok": True,
            "source_type": capture.source_type,
            "source_url": capture.source_url,
            "resource_id": capture.resource_id,
            "title": capture.title,
            "captured_at": capture.captured_at.isoformat(),
            "content": content[:500_000],
            "content_truncated": len(content) > 500_000,
            "items": readable_items[:2_000],
            "items_truncated": len(readable_items) > 2_000,
            "metadata": capture.metadata,
            "warnings": capture.warnings,
            "capture_status": capture_status,
        }

    @api.get("/extension/capture-requests/next", response_model=None)
    def next_extension_capture_request(
        request: Request,
        wait_seconds: int = 0,
    ) -> Response | dict[str, Any]:
        capture_request = _runtime(request).capture_broker.claim_capture_request(
            wait_seconds=max(0, min(wait_seconds, 25))
        )
        if capture_request is None:
            return Response(status_code=204)
        return capture_request

    @api.post("/extension/capture-requests/{request_id}/failed", status_code=204)
    def fail_extension_capture_request(
        request: Request,
        request_id: str,
        payload: CaptureFailure,
    ) -> Response:
        if not _runtime(request).capture_broker.fail_capture_request(
            request_id,
            error_code=payload.code,
            error_message=payload.message,
        ):
            raise _http_error(404, "capture_request_not_found", "Capture request was not found.")
        return Response(status_code=204)

    @api.post("/extension/captures", status_code=202)
    def accept_extension_capture(
        request: Request,
        payload: BrowserCaptureEnvelope,
    ) -> dict[str, Any]:
        status = _runtime(request).capture_broker.put(payload)
        return {
            "accepted": True,
            "capture": status.as_dict(),
        }

    @api.delete("/extension/captures", status_code=204)
    def discard_extension_captures(request: Request) -> Response:
        _runtime(request).capture_broker.clear()
        return Response(status_code=204)

    @api.put("/settings/general")
    def update_general_settings(
        request: Request, payload: GeneralSettings
    ) -> GeneralSettings:
        runtime = _runtime(request)
        runtime.store.set_setting("history_retention_days", payload.history_retention_days)
        runtime.store.prune_history(retention_days=payload.history_retention_days)
        return payload

    @api.post("/settings/gemini-key", status_code=204)
    def save_gemini_key(request: Request, payload: GeminiKeyUpdate) -> Response:
        runtime = _runtime(request)
        runtime.configuration.save_gemini_key(payload.api_key)
        runtime.reload_settings()
        return Response(status_code=204)

    @api.post("/settings/gemini/test")
    async def test_gemini(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        checks = await asyncio.to_thread(
            run_health_checks,
            runtime.settings,
            None,
            capture_broker=runtime.capture_broker,
        )
        check = next((item for item in checks if item.key == "gemini_api"), None)
        return {"check": check}

    @api.post("/settings/oauth-client", status_code=204)
    async def upload_oauth_client(
        request: Request,
        file: Annotated[UploadFile, File()],
    ) -> Response:
        runtime = _runtime(request)
        payload = await file.read(MAX_CREDENTIAL_FILE_BYTES + 1)
        runtime.configuration.save_oauth_client(payload)
        return Response(status_code=204)

    @api.post("/settings/google/authorize")
    async def authorize_google(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        await asyncio.to_thread(
            load_google_credentials,
            runtime.settings.root_dir,
            interactive=True,
        )
        return {"connections": connection_status(runtime.settings, port=runtime.port)}

    @api.post("/settings/google/disconnect")
    def disconnect_google(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        disconnected = runtime.configuration.disconnect_google()
        return {
            "disconnected": disconnected,
            "connections": connection_status(runtime.settings, port=runtime.port),
        }

    @api.delete("/history", status_code=204)
    def clear_history(request: Request) -> Response:
        _runtime(request).store.clear_history()
        return Response(status_code=204)

    @api.get("/diagnostics", response_model=DiagnosticsResponse)
    def diagnostics(request: Request) -> DiagnosticsResponse:
        runtime = _runtime(request)
        connections = connection_status(runtime.settings, port=runtime.port)
        return DiagnosticsResponse(
            checks=connections.checks,
            recent_events=runtime.store.recent_events(limit=100),
            error_runs=_annotate_runs(
                runtime,
                runtime.store.list_runs(
                    limit=50,
                    statuses=[RunStatus.FAILED, RunStatus.FAILED_PARTIAL, RunStatus.STALE],
                ),
            ),
            control_database=str(runtime.store.path),
            state_database=str(runtime.settings.resolved_state_path),
        )

    @api.get("/diagnostics/support-bundle")
    def support_bundle(request: Request) -> Response:
        runtime = _runtime(request)
        payload = sanitize(
            {
                "generated_by": "Canvas Task Sync",
                "config": runtime.configuration.sanitized_document(),
                "connections": connection_status(runtime.settings, port=runtime.port).model_dump(
                    mode="json"
                ),
                "runs": [
                    run.model_dump(mode="json") for run in runtime.store.list_runs(limit=100)
                ],
                "events": [
                    event.model_dump(mode="json")
                    for event in runtime.store.recent_events(limit=500)
                ],
                "schedules": [
                    schedule.model_dump(mode="json")
                    for schedule in runtime.store.list_schedules()
                ],
            }
        )
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="canvas-task-sync-support.json"'},
        )

    app.include_router(api)
    _mount_frontend(app)
    return app


def _runtime(request: Request) -> WebRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise RuntimeError("Web runtime is not initialized.")
    return runtime


def _default_course(settings: ProjectSettings) -> str | None:
    enabled = sorted(key for key, course in settings.courses.items() if course.enabled)
    return enabled[0] if enabled else next(iter(sorted(settings.courses)), None)


def _queue_automatic_capture(
    runtime: WebRuntime,
    course: Any,
    *,
    extraction_mode: Any | None = None,
    acquisition_strategy: AcquisitionStrategy = AcquisitionStrategy.AUTO,
) -> dict[str, Any] | None:
    if course.source.type != "browser":
        return None
    if acquisition_strategy == AcquisitionStrategy.CANVAS_API:
        return None
    if course.canvas_course_id and acquisition_strategy == AcquisitionStrategy.AUTO:
        return None
    source = course.source.model_copy(deep=True)
    if extraction_mode is not None:
        source.extraction.mode = extraction_mode
    return runtime.capture_broker.request_capture(
        source.url,
        automatic_acquisition_mode(source),
        extension_selection(source),
    )


def _course_views(runtime: WebRuntime) -> list[CourseView]:
    connections = connection_status(runtime.settings, port=runtime.port)
    views: list[CourseView] = []
    for course_id, course in sorted(runtime.settings.courses.items()):
        source_ready = True
        if course.source.type == "browser":
            try:
                runtime.capture_broker.get(
                    source_type_from_url(course.source.url),
                    resource_id_from_url(course.source.url),
                    max_age_seconds=course.source.freshness_seconds,
                )
            except Exception:
                source_ready = False
        ready = (
            connections.google_authorized
            and connections.gemini_configured
            and source_ready
        )
        if not course.enabled:
            readiness = HealthState.WARNING
            message = "Disabled"
        elif course.source.type == "browser" and not source_ready:
            readiness = HealthState.WARNING
            message = "Browser capture needed"
        elif ready:
            readiness = HealthState.HEALTHY
            message = "Ready"
        else:
            readiness = HealthState.MISSING
            message = "Setup needed"
        views.append(
            CourseView(
                id=course_id,
                settings=course,
                readiness=readiness,
                readiness_message=message,
            )
        )
    return views


def _annotate_run(runtime: WebRuntime, run: Any) -> Any:
    if run is None:
        return None
    course = runtime.settings.courses.get(run.course_id)
    run.course_name = course.name if course else ("All courses" if run.course_id == "all" else None)
    return run


def _annotate_runs(runtime: WebRuntime, runs: list[Any]) -> list[Any]:
    return [_annotate_run(runtime, run) for run in runs]


def _http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    run_id: int | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
            "run_id": run_id,
        },
    )


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    field_errors: dict[str, list[str]] | None = None,
    run_id: int | None = None,
) -> JSONResponse:
    detail = ApiErrorDetail(
        code=code,
        message=message,
        retryable=retryable,
        field_errors=field_errors,
        run_id=run_id,
    )
    return JSONResponse(status_code=status_code, content={"error": detail.model_dump(mode="json")})


def _mount_frontend(app: FastAPI) -> None:
    web_dist = Path(__file__).with_name("web_dist")
    assets = web_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    @app.api_route(
        "/api/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def api_not_found(full_path: str):
        del full_path
        return _error_response(
            status_code=404,
            code="api_route_not_found",
            message=(
                "This API route is not available. Restart Canvas Task Sync if the web interface "
                "was updated while the local server was running."
            ),
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend(full_path: str):
        del full_path
        index = web_dist / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "frontend_not_built",
                    "message": (
                        "Run the frontend build before starting the packaged control center."
                    ),
                    "retryable": False,
                }
            },
        )
