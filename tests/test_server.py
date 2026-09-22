"""The server hosts both dashboards, and a laptop reaches them through an SSH port forward.

The forward is plain TCP, so nothing rewrites the request: the backend sees exactly what
the laptop's browser sent. That browser types ``127.0.0.1:8890`` while the backend binds
``8790`` on the server, so the backend has to accept its configured public origin as its
own. It must not accept any other origin.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from canvas_task_sync.server import PortRouter, build_web_server
from canvas_task_sync.simple_web_app import create_simple_web_app
from canvas_task_sync.web_app import create_web_app
from canvas_task_sync.web_constants import PUBLIC_ORIGIN_ENV

WEB_DIST = Path(__file__).parents[1] / "src" / "canvas_task_sync" / "web_dist"
LAPTOP_ORIGIN = "http://127.0.0.1:8890"


def _write_project(root: Path) -> Path:
    config = root / "config" / "courses.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """version: 1
state_path: .canvas-task-sync/state.sqlite3
gemini_model: test-model
courses: {}
""",
        encoding="utf-8",
    )
    return config


@pytest.fixture
def tunneled(tmp_path, monkeypatch):
    """The backend as the server runs it: bound to 8790, browsed as 127.0.0.1:8890."""
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, LAPTOP_ORIGIN)
    app = create_web_app(_write_project(tmp_path), port=8790, simple_port=8891)
    with TestClient(app, base_url=LAPTOP_ORIGIN) as client:
        yield client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}


def test_the_laptop_browser_address_is_accepted_through_the_tunnel(tunneled):
    assert tunneled.get("/api/v1/overview").status_code == 200
    assert tunneled.get("/", headers={"Host": "localhost:8890"}).status_code == 200

    headers = {**_csrf(tunneled), "Origin": LAPTOP_ORIGIN}
    assert tunneled.post("/api/v1/settings/extension/rotate", headers=headers).status_code == 200
    simple = {**headers, "Origin": "http://127.0.0.1:8891"}
    assert tunneled.post("/api/v1/settings/extension/rotate", headers=simple).status_code == 200


def test_the_bound_port_still_works_for_companion_apps_on_the_server_side(tunneled):
    # School Dashboard's own tunnel forwards 8790 straight through.
    response = tunneled.get("/api/v1/tasks", headers={"Host": "127.0.0.1:8790"})
    assert response.status_code == 200


def test_only_the_configured_public_origin_is_added(tunneled):
    headers = _csrf(tunneled)
    for origin in ("http://127.0.0.1:9999", "http://evil.example", "https://127.0.0.1:8890"):
        rejected = tunneled.post(
            "/api/v1/settings/extension/rotate",
            headers={**headers, "Origin": origin},
        )
        assert rejected.json()["error"]["code"] == "origin_not_allowed", origin

    for host in ("127.0.0.1:9999", "evil.example:8890"):
        rejected = tunneled.get("/api/v1/overview", headers={"Host": host})
        assert rejected.json()["error"]["code"] == "host_not_allowed", host

    assert tunneled.post("/api/v1/settings/extension/rotate").json()["error"]["code"] == (
        "csrf_failed"
    )


def test_the_extension_is_told_to_pair_with_the_browser_address(tunneled):
    setup = tunneled.get("/api/v1/settings/extension").json()
    assert setup["server_url"] == LAPTOP_ORIGIN


def test_the_simple_dashboard_calls_the_api_at_the_browser_address(monkeypatch):
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, LAPTOP_ORIGIN)
    with TestClient(create_simple_web_app(backend_port=8790)) as client:
        assert client.get("/runtime-config.json").json() == {"api_base": LAPTOP_ORIGIN}


def test_without_a_tunnel_the_simple_dashboard_calls_the_bound_port(monkeypatch):
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    with TestClient(create_simple_web_app(backend_port=8890)) as client:
        assert client.get("/runtime-config.json").json() == {"api_base": LAPTOP_ORIGIN}


def test_port_router_sends_lifespan_to_the_backend_app():
    seen: list[str] = []

    def app(name: str):
        async def asgi(scope, receive, send) -> None:
            seen.append(f"{name}:{scope['type']}")

        return asgi

    router = PortRouter(app("backend"), {8891: app("simple")})

    async def scenario() -> None:
        await router({"type": "lifespan"}, None, None)
        await router({"type": "http", "server": ("127.0.0.1", 8891)}, None, None)
        await router({"type": "http", "server": ("127.0.0.1", 8790)}, None, None)
        await router({"type": "http", "server": None}, None, None)

    asyncio.run(scenario())

    assert seen == ["backend:lifespan", "simple:http", "backend:http", "backend:http"]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def _running(server: uvicorn.Server, sockets: list[socket.socket]) -> Iterator[None]:
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover - only on a wedged CI host
        raise RuntimeError("Test server did not start.")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def test_one_server_serves_both_dashboards_on_their_own_ports(tmp_path, monkeypatch):
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    port, simple_port = _free_port(), _free_port()
    server, sockets = build_web_server(
        _write_project(tmp_path), port=port, simple_port=simple_port
    )
    with _running(server, sockets), httpx.Client(timeout=10) as client:
        full = client.get(f"http://127.0.0.1:{port}/runs")
        simple = client.get(f"http://127.0.0.1:{simple_port}/")
        config = client.get(f"http://127.0.0.1:{simple_port}/runtime-config.json")
        api = client.get(f"http://127.0.0.1:{port}/api/v1/bootstrap")

    assert full.text == (WEB_DIST / "index.html").read_text(encoding="utf-8")
    assert simple.text == (WEB_DIST / "simple.html").read_text(encoding="utf-8")
    assert config.json() == {"api_base": f"http://127.0.0.1:{port}"}
    assert api.status_code == 200
    assert "csrf_token" in api.json()


def test_the_simple_dashboard_can_be_left_out(tmp_path):
    port, simple_port = _free_port(), _free_port()
    server, sockets = build_web_server(
        _write_project(tmp_path), port=port, simple_port=simple_port, serve_simple=False
    )
    try:
        assert [sock.getsockname()[1] for sock in sockets] == [port]
    finally:
        for sock in sockets:
            sock.close()


def test_a_busy_port_fails_fast_and_releases_the_other_one(tmp_path):
    port = _free_port()
    with socket.socket() as occupant:
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        busy = occupant.getsockname()[1]

        with pytest.raises(RuntimeError, match=f"127.0.0.1:{busy}"):
            build_web_server(_write_project(tmp_path), port=port, simple_port=busy)

    with socket.socket() as again:
        again.bind(("127.0.0.1", port))
