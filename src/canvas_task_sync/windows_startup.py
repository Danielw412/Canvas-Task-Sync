"""Background entry point used by the Windows scheduled startup task."""

from __future__ import annotations

import argparse
import codecs
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from canvas_task_sync import cli
from canvas_task_sync.web_constants import DEFAULT_WEB_PORT


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canvas-task-sync-windows-startup",
        description="Start Canvas Task Sync without opening a browser or console window.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)
    return parser


def _archive_legacy_log(log_path: Path) -> Path | None:
    if not log_path.exists() or log_path.stat().st_size < 2:
        return None

    with log_path.open("rb") as log_file:
        prefix = log_file.read(2)
    if prefix not in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return None

    archive_path = log_path.with_name(f"{log_path.name}.legacy")
    if archive_path.exists():
        archive_path = log_path.with_name(
            f"{log_path.name}.{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')}.legacy"
        )
    log_path.replace(archive_path)
    return archive_path


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    archived_log_path = _archive_legacy_log(args.log_path)

    with (
        args.log_path.open("a", encoding="utf-8", errors="replace", buffering=1) as log_file,
        redirect_stdout(log_file),
        redirect_stderr(log_file),
    ):
        if archived_log_path is not None:
            print(f"Archived legacy startup log: {archived_log_path}")
        print(
            f"[{datetime.now().astimezone().isoformat()}] "
            "Starting Canvas Task Sync Windows background server."
        )
        try:
            return cli.main(
                [
                    "--config",
                    str(args.config),
                    "web",
                    "--port",
                    str(args.port),
                    "--no-open",
                ]
            )
        except BaseException:
            traceback.print_exc()
            return 1
        finally:
            print(
                f"[{datetime.now().astimezone().isoformat()}] "
                "Canvas Task Sync Windows background server stopped."
            )


if __name__ == "__main__":
    raise SystemExit(main())
