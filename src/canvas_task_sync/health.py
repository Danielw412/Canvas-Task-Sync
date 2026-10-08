from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from canvas_task_sync.agent_status import check_agent_sign_in, quick_agent_status
from canvas_task_sync.auth import SCOPES, load_google_credentials
from canvas_task_sync.configuration import CourseSettings, ProjectSettings
from canvas_task_sync.redaction import safe_exception_summary
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
    agent = settings.extraction_agent
    if agent.provider == "gemini":
        extraction_ready = gemini
        agent_summary = agent.describe() if gemini else "Add a Gemini API key"
    else:
        status = quick_agent_status(agent.provider)
        extraction_ready = bool(status.ready)
        agent_summary = f"{agent.describe()} · {status.detail}"
    if agent.provider == "gemini" or not gemini:
        gemini_state = HealthState.HEALTHY if gemini else HealthState.MISSING
        gemini_summary = (
            f"Configured · {' → '.join(settings.gemini_model_chain)} · per-course reasoning"
            if gemini
            else "Add a Gemini API key"
        )
        if agent.provider != "gemini":
            gemini_state = HealthState.WARNING
            gemini_summary = f"Not configured · not needed while {agent.provider_label} extracts"
    else:
        gemini_state = HealthState.HEALTHY
        gemini_summary = f"Configured · not in use while {agent.provider_label} extracts"
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
            key="extraction_agent",
            label="Extraction agent",
            state=HealthState.HEALTHY if extraction_ready else HealthState.MISSING,
            summary=agent_summary,
        ),
        ConnectionItem(
            key="gemini_api",
            label="Gemini API",
            state=gemini_state,
            summary=gemini_summary,
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
        extraction_provider=agent.provider,
        extraction_label=agent.describe(),
        extraction_ready=extraction_ready,
        local_server=f"{DEFAULT_WEB_HOST}:{port}",
        checks=checks,
    )


def _pipeline():
    """Import the acquisition and Google clients only when a check actually runs.

    ``connection_status`` is called on almost every dashboard request and needs none of
    this; pulling it in at module scope would put the whole pipeline back into the web
    process it was just taken out of.
    """
    from canvas_task_sync.google_tasks import GoogleTasksClient
    from canvas_task_sync.sources import create_course_source_adapter

    return GoogleTasksClient, create_course_source_adapter


def _gemini_chain(
    settings: ProjectSettings, course: CourseSettings | None
) -> tuple[list[str], str]:
    if course is not None:
        resolved = settings.extraction_agent_for(course)
        if resolved.provider == "gemini":
            return resolved.models, resolved.effort or "medium"
        return settings.gemini_model_chain_for(course), course.gemini_reasoning
    agent = settings.extraction_agent
    if agent.provider == "gemini" and agent.model is not None:
        fallbacks = [model for model in settings.gemini_model_chain if model != agent.model]
        return [agent.model, *fallbacks], agent.effort
    return settings.gemini_model_chain, "medium"


def gemini_health_check(
    settings: ProjectSettings, course: CourseSettings | None = None
) -> HealthCheck:
    load_dotenv(settings.root_dir / ".env")
    model_chain, reasoning_level = _gemini_chain(settings, course)
    api_key = os.getenv("GEMINI_API_KEY")
    started = perf_counter()
    if not api_key:
        return HealthCheck(
            key="gemini_api",
            label="Gemini API",
            state=HealthState.MISSING,
            summary="GEMINI_API_KEY is missing.",
            duration_ms=int((perf_counter() - started) * 1000),
        )
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
    except Exception as error:
        return HealthCheck(
            key="gemini_api",
            label="Gemini API",
            state=HealthState.ERROR,
            summary=safe_exception_summary(error, known_secrets=[api_key]),
            duration_ms=int((perf_counter() - started) * 1000),
        )
    return HealthCheck(
        key="gemini_api",
        label="Gemini API",
        state=HealthState.HEALTHY,
        summary=(
            f"Model {selected_model} is available with {reasoning_level} reasoning."
            if selected_model == model_chain[0]
            else f"Fallback model {selected_model} is available; primary is unavailable."
        ),
        duration_ms=int((perf_counter() - started) * 1000),
        details={
            "model": getattr(model, "name", selected_model),
            "configured_chain": model_chain,
            "thinking_level": reasoning_level,
        },
    )


def agent_health_check(provider: str) -> HealthCheck:
    """Whether Claude Code or Codex is signed in here. Starts no turn and uses no usage."""
    started = perf_counter()
    status = check_agent_sign_in(provider)
    label = provider.title()
    return HealthCheck(
        key="extraction_agent",
        label=f"{label} sign-in",
        state=(
            HealthState.HEALTHY
            if status.ready
            else HealthState.WARNING
            if status.ready is None
            else HealthState.MISSING
        ),
        summary=status.detail,
        duration_ms=int((perf_counter() - started) * 1000),
        details={"provider": provider},
    )


def run_health_checks(
    settings: ProjectSettings,
    course_id: str | None = None,
    *,
    capture_broker: Any | None = None,
) -> list[HealthCheck]:
    load_dotenv(settings.root_dir / ".env")
    checks: list[HealthCheck] = []
    selected_course = settings.course(course_id) if course_id else None
    if settings.extraction_agent.provider == "gemini":
        checks.append(gemini_health_check(settings, selected_course))
    else:
        checks.append(agent_health_check(settings.extraction_agent.provider))

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

    GoogleTasksClient, create_course_source_adapter = _pipeline()
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
