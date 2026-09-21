"""Sync worker process: does the work, then goes away.

The web process is long-lived, so anything it imports it keeps.  The sync pipeline is
the expensive part of that -- the Gemini SDK alone is about 23 MB and never unloads --
and it is idle almost all day.  Running it here instead means the web process never
imports it, and the moment the queue drains this process exits and the operating system
takes all of it back.

Protocol, one JSON object per line in each direction:

    parent -> stdin    {"run_id": 12}
    worker -> stdout   {"ready": true}                      once, at startup
                       {"run_id": 12, "ok": true}
                       {"run_id": 12, "ok": false, "error": "..."}

The parent closes stdin to shut the worker down, so the worker never has to guess
whether more work is coming.  Run state, progress events, and failures all travel
through the control database rather than this pipe; the pipe only says "this one is
finished", which is what lets the parent keep its queue semantics.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from canvas_task_sync.configuration import load_settings
from canvas_task_sync.control_store import ControlStore
from canvas_task_sync.redaction import safe_exception_summary
from canvas_task_sync.run_executor import RunExecutor
from canvas_task_sync.sync_service import SyncService

DEFAULT_MAX_WORKERS = 6


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canvas-task-sync-worker",
        description="Execute queued Canvas Task Sync runs on behalf of the web process.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help="Runs to execute concurrently, matching the web process's queue width.",
    )
    return parser


class _Writer:
    """Serializes replies so concurrent runs cannot interleave a line."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Claim the real stdout for the protocol before anything else can write to it, then
    # point sys.stdout at stderr. A stray print from any dependency would otherwise land
    # in the middle of a JSON line and desynchronise the parent.
    protocol_stream = sys.stdout
    sys.stdout = sys.stderr
    writer = _Writer(protocol_stream)

    settings = load_settings(args.config)
    store = ControlStore(settings.root_dir / ".canvas-task-sync" / "control.sqlite3")
    executor = RunExecutor(store, SyncService(settings))
    writer.send({"ready": True})

    def handle(run_id: int) -> None:
        try:
            executor.execute(run_id)
        except Exception as error:  # The run's own failure handling already ran.
            writer.send({"run_id": run_id, "ok": False, "error": safe_exception_summary(error)})
        else:
            writer.send({"run_id": run_id, "ok": True})

    try:
        with ThreadPoolExecutor(
            max_workers=max(1, args.max_workers),
            thread_name_prefix="canvas-run",
        ) as pool:
            # Ends on EOF, which is how the parent asks this process to exit.
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    job = json.loads(line)
                    run_id = int(job["run_id"])
                except (ValueError, KeyError, TypeError):
                    writer.send({"ok": False, "error": "Malformed job."})
                    continue
                pool.submit(handle, run_id)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
