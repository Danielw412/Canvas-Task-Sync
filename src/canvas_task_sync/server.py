from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import uvicorn

from canvas_task_sync.simple_web_app import create_simple_web_app
from canvas_task_sync.web_app import create_web_app
from canvas_task_sync.web_constants import (
    DEFAULT_SIMPLE_WEB_PORT,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
)


def _run_simple_server(*, port: int, backend_port: int) -> None:
    uvicorn.run(
        create_simple_web_app(backend_port=backend_port),
        host=DEFAULT_WEB_HOST,
        port=port,
        log_level="info",
        access_log=False,
    )


def run_web_server(
    config_path: Path,
    *,
    port: int = DEFAULT_WEB_PORT,
    simple_port: int = DEFAULT_SIMPLE_WEB_PORT,
    open_browser: bool = True,
) -> None:
    url = f"http://{DEFAULT_WEB_HOST}:{port}"
    if open_browser:
        timer = threading.Timer(0.8, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    simple_thread = threading.Thread(
        target=_run_simple_server,
        kwargs={"port": simple_port, "backend_port": port},
        name="canvas-task-sync-simple-web",
        daemon=True,
    )
    simple_thread.start()
    uvicorn.run(
        create_web_app(config_path, port=port, simple_port=simple_port),
        host=DEFAULT_WEB_HOST,
        port=port,
        log_level="info",
        access_log=False,
    )
