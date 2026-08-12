from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import uvicorn

from canvas_task_sync.web_app import create_web_app


def run_web_server(
    config_path: Path,
    *,
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        timer = threading.Timer(0.8, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    uvicorn.run(
        create_web_app(config_path, port=port),
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
