from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import uvicorn

from canvas_task_sync.simple_web_app import create_simple_web_app
from canvas_task_sync.web_constants import (
    DEFAULT_BACKEND_HOST_HEADER,
    DEFAULT_REMOTE_BACKEND_URL,
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


def _start_simple_server(*, port: int, backend_port: int) -> threading.Thread:
    thread = threading.Thread(
        target=_run_simple_server,
        kwargs={"port": port, "backend_port": backend_port},
        name="canvas-task-sync-simple-web",
        daemon=True,
    )
    thread.start()
    return thread


def _open_browser_later(url: str) -> None:
    timer = threading.Timer(0.8, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()


def run_web_server(
    config_path: Path,
    *,
    port: int = DEFAULT_WEB_PORT,
    simple_port: int = DEFAULT_SIMPLE_WEB_PORT,
    open_browser: bool = True,
    serve_simple: bool = True,
) -> None:
    """Run the authoritative backend, which also serves the dashboards it is local to."""
    from canvas_task_sync.web_app import create_web_app

    if open_browser:
        _open_browser_later(f"http://{DEFAULT_WEB_HOST}:{port}")
    if serve_simple:
        _start_simple_server(port=simple_port, backend_port=port)
    uvicorn.run(
        create_web_app(config_path, port=port, simple_port=simple_port),
        host=DEFAULT_WEB_HOST,
        port=port,
        log_level="info",
        access_log=False,
    )


def run_proxy_server(
    *,
    upstream: str = DEFAULT_REMOTE_BACKEND_URL,
    upstream_host: str = DEFAULT_BACKEND_HOST_HEADER,
    port: int = DEFAULT_WEB_PORT,
    simple_port: int = DEFAULT_SIMPLE_WEB_PORT,
    open_browser: bool = True,
) -> None:
    """Run both dashboards locally against a backend that lives somewhere else.

    Nothing here touches course configuration, Google credentials, or SQLite state: those
    belong to the backend ``upstream`` points at.
    """
    from canvas_task_sync.proxy_app import create_proxy_app

    if open_browser:
        _open_browser_later(f"http://{DEFAULT_WEB_HOST}:{port}")
    _start_simple_server(port=simple_port, backend_port=port)
    uvicorn.run(
        create_proxy_app(
            upstream=upstream,
            upstream_host=upstream_host,
            port=port,
            simple_port=simple_port,
        ),
        host=DEFAULT_WEB_HOST,
        port=port,
        log_level="info",
        access_log=False,
    )
