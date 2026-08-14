from __future__ import annotations

from canvas_task_sync.cli import build_parser, render_plan
from canvas_task_sync.models import ExtractionMode, SyncPlan
from canvas_task_sync.web_constants import DEFAULT_WEB_PORT


def test_cli_supports_required_sync_flags():
    args = build_parser().parse_args(
        [
            "sync",
            "--course",
            "spanish",
            "--json",
            "--include-past",
            "--test-rebase-week",
            "2026-08-10",
            "--extraction-mode",
            "image",
        ]
    )
    assert args.course == "spanish"
    assert args.json is True
    assert args.include_past is True
    assert args.extraction_mode == "image"


def test_web_uses_the_dedicated_local_port_by_default():
    args = build_parser().parse_args(["web"])

    assert args.port == DEFAULT_WEB_PORT == 8790


def test_dry_run_always_prints_all_reconciliation_sections():
    plan = SyncPlan(
        course_id="spanish",
        task_list="School",
        dry_run=True,
        extraction_mode=ExtractionMode.HYBRID,
    )
    output = render_plan(plan)
    assert "DRY RUN (no writes)" in output
    for heading in (
        "CREATE (0)",
        "UPDATE (0)",
        "UNCHANGED (0)",
        "UNCERTAIN (0)",
        "IGNORED (0)",
        "REMOTE_MISSING (0)",
        "SOURCE_MISSING (0)",
        "HISTORICAL_BLOCKED (0)",
    ):
        assert heading in output
