from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from canvas_task_sync.app import authorize, default_config_path, run_doctor, run_sync
from canvas_task_sync.configuration import load_settings
from canvas_task_sync.models import ExtractionMode, SyncActionKind, SyncPlan

DISPLAY_ORDER = [
    SyncActionKind.CREATE,
    SyncActionKind.UPDATE,
    SyncActionKind.UNCHANGED,
    SyncActionKind.UNCERTAIN,
    SyncActionKind.IGNORED,
    SyncActionKind.REMOTE_MISSING,
    SyncActionKind.SOURCE_MISSING,
    SyncActionKind.HISTORICAL_BLOCKED,
]


def _date_argument(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected a date in YYYY-MM-DD format.") from error
    if parsed.weekday() != 0:
        raise argparse.ArgumentTypeError("The rebased week date must be a Monday.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canvas-task-sync",
        description="Extract visual course agendas and reconcile them with Google Tasks.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Course configuration YAML (default: config/courses.yaml).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth", help="Authorize Google Tasks and target-slide read access.")

    doctor = subparsers.add_parser("doctor", help="Validate credentials and configured sources.")
    doctor.add_argument("--course", help="Check only one configured course.")

    sync = subparsers.add_parser("sync", help="Build a dry-run plan or apply it.")
    sync.add_argument("--course", required=True, help="Configured course ID.")
    sync.add_argument("--apply", action="store_true", help="Create and patch planned tasks.")
    sync.add_argument(
        "--include-past",
        action="store_true",
        help="Permit creates/updates whose supported due date is already past.",
    )
    sync.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sync.add_argument(
        "--test-rebase-week",
        type=_date_argument,
        metavar="YYYY-MM-DD",
        help="Dry-run only: shift the fixture week to this Monday.",
    )
    sync.add_argument(
        "--extraction-mode",
        choices=[mode.value for mode in ExtractionMode],
        help="Diagnostic override; normal runs use the per-course configuration.",
    )
    web = subparsers.add_parser("web", help="Start the local web control center.")
    web.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Loopback port for the web control center (default: 8787).",
    )
    web.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the control center in the default browser.",
    )
    return parser


def plan_as_json(plan: SyncPlan) -> str:
    counts = Counter(action.kind.value for action in plan.actions)
    payload = plan.model_dump(mode="json")
    payload["counts"] = {kind.value: counts.get(kind.value, 0) for kind in DISPLAY_ORDER}
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def render_plan(plan: SyncPlan) -> str:
    operation = "DRY RUN (no writes)" if plan.dry_run else "APPLY"
    lines = [
        f"{operation} — course={plan.course_id} list={plan.task_list} "
        f"extraction={plan.extraction_mode.value}"
    ]
    if plan.fallback_reasons:
        lines.append("Auto fallback: " + "; ".join(plan.fallback_reasons))
    grouped = plan.grouped()
    for kind in DISPLAY_ORDER:
        actions = grouped[kind]
        lines.extend(["", f"{kind.value.upper()} ({len(actions)})"])
        if not actions:
            lines.append("  (none)")
            continue
        for action in actions:
            due = action.due_date.isoformat() if action.due_date else "no due date"
            lines.append(f"  - {action.title} — {due}")
            lines.append(f"    {action.reason}")
            if action.evidence and kind in {SyncActionKind.UNCERTAIN, SyncActionKind.IGNORED}:
                evidence = " ".join(action.evidence.split())
                lines.append(f"    evidence: {evidence}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "sync" and args.apply and args.test_rebase_week:
            parser.error("--test-rebase-week cannot be combined with --apply")
        settings = load_settings(args.config)
        if args.command == "auth":
            authorize(settings)
            print("Google authorization is ready for Tasks and target-slide read access.")
            return 0
        if args.command == "doctor":
            for check in run_doctor(settings, args.course):
                print(f"OK  {check}")
            return 0
        if args.command == "sync":
            plan = run_sync(
                settings,
                course_id=args.course,
                apply=args.apply,
                include_past=args.include_past,
                rebase_week=args.test_rebase_week,
                extraction_mode=(
                    ExtractionMode(args.extraction_mode) if args.extraction_mode else None
                ),
            )
            print(plan_as_json(plan) if args.json else render_plan(plan))
            return 0
        if args.command == "web":
            if not 1 <= args.port <= 65535:
                parser.error("--port must be between 1 and 65535")
            from canvas_task_sync.server import run_web_server

            run_web_server(args.config, port=args.port, open_browser=not args.no_open)
            return 0
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
