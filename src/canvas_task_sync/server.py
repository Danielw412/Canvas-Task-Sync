from __future__ import annotations

import logging
import os
import socket
import threading
import webbrowser
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

from canvas_task_sync.simple_web_app import create_simple_web_app
from canvas_task_sync.web_constants import (
    DEFAULT_SIMPLE_WEB_PORT,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
)

logger = logging.getLogger("uvicorn.error")


class PortRouter:
    """Serve a different ASGI app on each listening port from a single event loop.

    The simple dashboard used to run a second uvicorn server on its own thread. Routing on
    the socket a connection arrived through keeps its separate origin without the second
    server. Lifespan events carry no socket, so they go to ``default``, the only app with
    startup work.
    """

    def __init__(self, default: ASGIApp, by_port: dict[int, ASGIApp]) -> None:
        self._default = default
        self._by_port = by_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        server = scope.get("server")
        app = self._by_port.get(server[1]) if server else None
        await (app or self._default)(scope, receive, send)


def bind_loopback(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            # Lets a restarted service rebind while its old connections sit in TIME_WAIT.
            # Windows reads the same flag as permission to share a port another process is
            # still listening on, so it stays off there.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((DEFAULT_WEB_HOST, port))
    except OSError as error:
        sock.close()
        raise RuntimeError(f"Could not listen on {DEFAULT_WEB_HOST}:{port}: {error}") from None
    return sock


def build_web_server(
    config_path: Path,
    *,
    port: int = DEFAULT_WEB_PORT,
    simple_port: int = DEFAULT_SIMPLE_WEB_PORT,
    serve_simple: bool = True,
) -> tuple[uvicorn.Server, list[socket.socket]]:
    """The backend and, optionally, the simple dashboard, bound but not yet serving."""
    from canvas_task_sync.web_app import create_web_app

    sockets = [bind_loopback(port)]
    by_port: dict[int, ASGIApp] = {}
    if serve_simple:
        try:
            sockets.append(bind_loopback(simple_port))
        except RuntimeError:
            sockets[0].close()
            raise
        by_port[simple_port] = create_simple_web_app(backend_port=port)
    app = PortRouter(create_web_app(config_path, port=port, simple_port=simple_port), by_port)
    # No reverse proxy ever sits in front of this server: remote browsers arrive through an
    # SSH port forward, which is plain TCP. So forwarded headers are never trusted.
    config = uvicorn.Config(app, log_level="info", access_log=False, proxy_headers=False)
    return uvicorn.Server(config), sockets


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
    """Run the authoritative backend, which also serves both dashboards."""
    server, sockets = build_web_server(
        config_path,
        port=port,
        simple_port=simple_port,
        serve_simple=serve_simple,
    )
    logger.info("Control center and API on http://%s:%d", DEFAULT_WEB_HOST, port)
    if serve_simple:
        logger.info("Simple dashboard on http://%s:%d", DEFAULT_WEB_HOST, simple_port)
    if open_browser:
        _open_browser_later(f"http://{DEFAULT_WEB_HOST}:{port}")
    server.run(sockets=sockets)
