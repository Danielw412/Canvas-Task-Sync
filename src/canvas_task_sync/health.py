from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from canvas_task_sync.auth import SCOPES, load_google_credentials
from canvas_task_sync.browser_capture import BrowserCaptureBroker
from canvas_task_sync.configuration import ProjectSettings
from canvas_task_sync.google_tasks import GoogleTasksClient
from canvas_task_sync.redaction import safe_exception_summary
from canvas_task_sync.sources import create_course_source_adapter
from canvas_task_sync.web_constants import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT
from canvas_task_sync.web_models import (
    ConnectionItem,
    ConnectionStatus,
    HealthCheck,
    HealthState,
)
from canvas_task_sync.week import monday_for


def _now() -> datetime:
    return datetime.now(UTC)


def _oauth_client_valid(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(value, dict) and isinstance(value.get("installed"), dict)


def connection_status(
    settings: ProjectSettings, *, port: int = DEFAULT_WEB_PORT
) -> ConnectionStatus:
    load_dotenv(settings.root_dir / ".env")
    client_configured = _oauth_client_valid(settings.root_dir / "credentials.json")
    token_path = settings.root_dir / "token.json"
    authorized = False
    scope_summary = "Authorization required"
    if token_path.exists():
        try:
            payload = json.loads(token_path.read_text(encoding="utf-8"))
            token_scopes = set(payload.get("scopes") or [])
            authorized = set(SCOPES).issubset(token_scopes)
            scope_summary = (
                "Required Tasks and Slides scopes are present"
                if authorized
                else ("Required Google scopes are missing")
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            scope_summary = "token.json is not valid JSON"
    gemini = bool(os.getenv("GEMINI_API_KEY"))
    checks = [
        ConnectionItem(
            key="oauth_client",
            label="OAuth client",
            state=HealthState.HEALTHY if client_configured else HealthState.MISSING,
            summary="Valid desktop client" if client_configured else "Upload credentials.json",
        ),
        ConnectionItem(
            key="google_authorization",
            label="Google authorization",
            state=HealthState.HEALTHY if authorized else HealthState.MISSING,
            summary=scope_summary,
        ),
        ConnectionItem(
            key="gemini_api",
            label="Gemini API",
            state=HealthState.HEALTHY if gemini else HealthState.MISSING,
            summary=(
                f"Configured · {' → '.join(settings.gemini_model_chain)} · per-course reasoning"
                if gemini
                else "Add a Gemini API key"
            ),
        ),
        ConnectionItem(
            key="local_database",
            label="Local database",
            state=HealthState.HEALTHY,
            summary="Operational storage is available",
        ),
    ]
    return ConnectionStatus(
        google_client_configured=client_configured,
        google_authorized=authorized,
        gemini_configured=gemini,
        local_server=f"{DEFAULT_WEB_HOST}:{port}",
        checks=checks,
    )


def run_health_checks(
    settings: ProjectSettings,
    course_id: str | None = None,
    *,
    capture_broker: BrowserCaptureBroker | None = None,
) -> list[HealthCheck]:
    load_dotenv(settings.root_dir / ".env")
    checks: list[HealthCheck] = []
    selected_course = settings.course(course_id) if course_id else None
    reasoning_level = selected_course.gemini_reasoning if selected_course else "medium"
    model_chain = (
        settings.gemini_model_chain_for(selected_course)
        if selected_course is not None
        else settings.gemini_model_chain
    )
    api_key = os.getenv("GEMINI_API_KEY")
    started = perf_counter()
    if not api_key:
        checks.append(
            HealthCheck(
                key="gemini_api",
                label="Gemini API",
                state=HealthState.MISSING,
                summary="GEMINI_API_KEY is missing.",
                duration_ms=int((perf_counter() - started) * 1000),
            )
        )
    else:
        try:
            from google import genai

            with genai.Client(api_key=api_key) as client:
                model = None
                selected_model = None
                model_errors: list[Exception] = []
                for candidate in model_chain:
                    try:
                        model = client.models.get(model=candidate)
                        selected_model = candidate
                        break
                    except Exception as error:
                        model_errors.append(error)
                if model is None or selected_model is None:
                    raise model_errors[-1]
            checks.append(
                HealthCheck(
                    key="gemini_api",
                    label="Gemini API",
                    state=HealthState.HEALTHY,
                    summary=(
                        f"Model {selected_model} is available with {reasoning_level} reasoning."
                        if selected_model == model_chain[0]
                        else (
                            f"Fallback model {selected_model} is available; primary is unavailable."
                        )
                    ),
                    duration_ms=int((perf_counter() - started) * 1000),
                    details={
                        "model": getattr(model, "name", selected_model),
                        "configured_chain": model_chain,
                        "thinking_level": reasoning_level,
                    },
                )
            )
        except Exception as error:
            checks.append(
                HealthCheck(
                    key="gemini_api",
                    label="Gemini API",
                    state=HealthState.ERROR,
                    summary=safe_exception_summary(error, known_secrets=[api_key]),
                    duration_ms=int((perf_counter() - started) * 1000),
                )
            )

    started = perf_counter()
    try:
        credentials = load_google_credentials(settings.root_dir, interactive=False)
        checks.append(
            HealthCheck(
                key="google_authorization",
                label="Google authorization",
                state=HealthState.HEALTHY,
                summary="Required Google OAuth scopes are valid.",
                duration_ms=int((perf_counter() - started) * 1000),
            )
        )
    except Exception as error:
        checks.append(
            HealthCheck(
                key="google_authorization",
                label="Google authorization",
                state=HealthState.ERROR,
                summary=safe_exception_summary(error),
                duration_ms=int((perf_counter() - started) * 1000),
            )
        )
        return checks

    tasks_client = GoogleTasksClient(credentials)
    course_ids = [course_id] if course_id else sorted(settings.courses)
    for selected_id in course_ids:
        course = settings.course(selected_id)
        started = perf_counter()
        try:
            local_today = datetime.now(ZoneInfo(course.timezone)).date()
            capture = create_course_source_adapter(
                course,
                credentials,
                target_week_start=monday_for(local_today),
                capture_broker=capture_broker,
            ).capture(include_image=False)
            checks.append(
                HealthCheck(
                    key=f"source:{selected_id}",
                    label=f"{course.name} source",
                    state=HealthState.HEALTHY,
                    summary=(
                        f"{capture.source_type.replace('_', ' ').title()} capture is readable · "
                        f"{len(capture.blocks)} blocks."
                    ),
                    duration_ms=int((perf_counter() - started) * 1000),
                    details={
                        "course_id": selected_id,
                        "source_type": capture.source_type,
                        "resource_id": capture.resource_id,
                        "page_id": capture.page_id,
                    },
                )
            )
        except Exception as error:
            checks.append(
                HealthCheck(
                    key=f"source:{selected_id}",
                    label=f"{course.name} source",
                    state=HealthState.ERROR,
                    summary=safe_exception_summary(error),
                    duration_ms=int((perf_counter() - started) * 1000),
                )
            )
        started = perf_counter()
        try:
            list_details: list[dict[str, object]] = []
            for configured_title in dict.fromkeys(
                [course.task_list, course.assessment_task_list]
            ):
                tasklist_id, tasklist_title = tasks_client.resolve_task_list(configured_title)
                list_details.append(
                    {
                        "title": tasklist_title,
                        "task_count": len(tasks_client.list_tasks(tasklist_id)),
                    }
                )
            checks.append(
                HealthCheck(
                    key=f"tasks:{selected_id}",
                    label=f"{course.name} task lists",
                    state=HealthState.HEALTHY,
                    summary=" · ".join(
                        f"'{item['title']}' readable ({item['task_count']} tasks)"
                        for item in list_details
                    ),
                    duration_ms=int((perf_counter() - started) * 1000),
                    details={"course_id": selected_id, "task_lists": list_details},
                )
            )
        except Exception as error:
            checks.append(
                HealthCheck(
                    key=f"tasks:{selected_id}",
                    label=f"{course.name} task list",
                    state=HealthState.ERROR,
                    summary=safe_exception_summary(error),
                    duration_ms=int((perf_counter() - started) * 1000),
                )
            )
    checked_at = _now()
    for check in checks:
        check.details.setdefault("checked_at", checked_at.isoformat())
    return checks
