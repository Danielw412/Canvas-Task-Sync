"""Background entry point used by the Windows scheduled startup task."""

from __future__ import annotations

import argparse
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.log_path.parent.mkdir(parents=True, exist_ok=True)

    with (
        args.log_path.open("a", encoding="utf-8", errors="replace", buffering=1) as log_file,
        redirect_stdout(log_file),
        redirect_stderr(log_file),
    ):
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
