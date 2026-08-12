from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from canvas_task_sync.auth import load_google_credentials
from canvas_task_sync.configuration import ProjectSettings
from canvas_task_sync.google_tasks import GoogleTasksClient
from canvas_task_sync.models import ExtractionMode, SyncPlan
from canvas_task_sync.sources import create_source_adapter
from canvas_task_sync.sync_service import SyncService


def _today(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()


def run_sync(
    settings: ProjectSettings,
    *,
    course_id: str,
    apply: bool,
    include_past: bool,
    rebase_week: date | None,
    extraction_mode: ExtractionMode | None = None,
) -> SyncPlan:
    if apply and rebase_week is not None:
        raise ValueError("--test-rebase-week cannot be combined with --apply.")
    prepared = SyncService(settings).prepare(
        course_id=course_id,
        include_past=include_past,
        rebase_week=rebase_week,
        extraction_mode=extraction_mode,
    )
    plan = prepared.plan.model_copy(deep=True)
    if apply:
        SyncService(settings).apply(prepared)
        plan.dry_run = False
    return plan


def run_doctor(settings: ProjectSettings, course_id: str | None = None) -> list[str]:
    load_dotenv(settings.root_dir / ".env")
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is missing from .env.")

    credentials = load_google_credentials(settings.root_dir, interactive=False)
    tasks_client = GoogleTasksClient(credentials)
    course_ids = [course_id] if course_id else sorted(settings.courses)
    checks = ["Gemini API key is configured.", "Google OAuth scopes are valid."]
    for selected_id in course_ids:
        course = settings.course(selected_id)
        capture = create_source_adapter(course.source, credentials).capture(
            include_image=False
        )
        tasklist_id, tasklist_title = tasks_client.resolve_task_list(course.task_list)
        task_count = len(tasks_client.list_tasks(tasklist_id))
        checks.append(
            f"{selected_id}: target page {capture.page_id} is readable "
            f"({len(capture.blocks)} blocks)."
        )
        checks.append(
            f"{selected_id}: Google Tasks list '{tasklist_title}' is readable ({task_count} tasks)."
        )
    return checks


def authorize(settings: ProjectSettings) -> None:
    load_google_credentials(settings.root_dir, interactive=True)


def default_config_path() -> Path:
    return Path("config/courses.yaml")
