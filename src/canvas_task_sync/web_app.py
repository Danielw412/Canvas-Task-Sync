from __future__ import annotations

import asyncio
import json
import secrets
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
from canvas_task_sync.configuration import ProjectSettings
from canvas_task_sync.configuration_service import (
    MAX_CREDENTIAL_FILE_BYTES,
    ConfigurationService,
)
from canvas_task_sync.control_store import ControlStore
from canvas_task_sync.health import connection_status, run_health_checks
from canvas_task_sync.redaction import safe_exception_summary, sanitize
from canvas_task_sync.run_manager import (
    TERMINAL_STATUSES,
    RunManager,
    ScheduleManager,
    next_schedule_occurrence,
)
from canvas_task_sync.sync_service import SyncService
from canvas_task_sync.web_models import (
    ApiErrorDetail,
    CourseSave,
    CourseView,
    DiagnosticsResponse,
    GeminiKeyUpdate,
    GeneralSettings,
    HealthState,
    OverviewResponse,
    RunApply,
    RunCreate,
    RunStatus,
    Schedule,
    ScheduleCreate,
    ScheduleUpdate,
)


class WebRuntime:
    def __init__(self, config_path: Path, *, port: int) -> None:
        self.port = port
        self.configuration = ConfigurationService(config_path)
        self.settings = self.configuration.load()
        self.store = ControlStore(self.settings.root_dir / ".canvas-task-sync" / "control.sqlite3")
        self.sync_service = SyncService(self.settings)
        self.runs = RunManager(self.store, self.sync_service)
        self.schedules = ScheduleManager(self.store, self.runs)
        self.csrf_token = secrets.token_urlsafe(32)
        self.retention_task: asyncio.Task[None] | None = None

    def reload_settings(self) -> ProjectSettings:
        self.settings = self.configuration.load()
        self.sync_service.settings = self.settings
        return self.settings

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
        self.store.close()

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(24 * 60 * 60)
            self.store.prune_history()


def create_web_app(config_path: Path, *, port: int = 8787) -> FastAPI:
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
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Last-Event-ID"],
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
        if request.url.path.startswith("/api/v1/") and is_mutation:
            runtime = _runtime(request)
            origin = request.headers.get("origin")
            allowed_origins = {
                f"http://127.0.0.1:{runtime.port}",
                f"http://localhost:{runtime.port}",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
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
        checks = await asyncio.to_thread(run_health_checks, runtime.settings, course_id)
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
        run_id = runtime.runs.create_preview(payload)
        return {"run_id": run_id, "status": "queued"}

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
        checks = await asyncio.to_thread(run_health_checks, runtime.settings, None)
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


def _course_views(runtime: WebRuntime) -> list[CourseView]:
    connections = connection_status(runtime.settings, port=runtime.port)
    ready = connections.google_authorized and connections.gemini_configured
    views: list[CourseView] = []
    for course_id, course in sorted(runtime.settings.courses.items()):
        if not course.enabled:
            readiness = HealthState.WARNING
            message = "Disabled"
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
