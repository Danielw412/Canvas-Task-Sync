"""The laptop dashboard proxy must be indistinguishable from the backend it fronts.

Every test here drives the *real* backend app through the proxy rather than a stub, so a
route added to ``web_app`` is covered the moment it exists.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from canvas_task_sync.browser_capture import (
    AcquisitionMode,
    BrowserCaptureEnvelope,
    BrowserCaptureItem,
    BrowserCaptureScreenshot,
    CaptureMethod,
)
from canvas_task_sync.proxy_app import HOP_BY_HOP_HEADERS, create_proxy_app
from canvas_task_sync.web_app import create_web_app
from canvas_task_sync.web_models import RunMode, RunStage, RunStatus, RunTrigger

PROXY_TEST_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=None, pool=10.0)
FIXTURES = Path(__file__).parent / "fixtures"


def _png_data_url() -> str:
    encoded = (FIXTURES / "spanish_target_page.png.b64").read_text(encoding="ascii")
    return f"data:image/png;base64,{''.join(encoded.split())}"


@contextmanager
def _serving(app: FastAPI) -> Iterator[str]:
    """Run ``app`` on a real loopback socket.

    Starlette's TestClient and httpx's ASGITransport both buffer a whole response before
    handing it back, so neither can show whether bytes actually flow while the upstream is
    still producing them. Only a real socket can.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover - only on a wedged CI host
        raise RuntimeError("Test server did not start.")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def _write_project(root: Path) -> Path:
    config = root / "config" / "courses.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """version: 1
state_path: .canvas-task-sync/state.sqlite3
gemini_model: test-model
courses:
  spanish:
    enabled: true
    name: Test Spanish
    prefix: SPANISH
    task_list: School
    timezone: America/New_York
    meeting_days: [mon, tue, wed, thu, fri]
    source:
      type: google_slides
      url: https://docs.google.com/presentation/d/fixture/edit
      page_id: slide-1
      extraction:
        mode: text
""",
        encoding="utf-8",
    )
    return config


def _asgi_client_factory(app: FastAPI):
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            timeout=PROXY_TEST_TIMEOUT,
            follow_redirects=False,
        )

    return factory


def _proxy_for(backend: FastAPI, **kwargs) -> FastAPI:
    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend),
            timeout=PROXY_TEST_TIMEOUT,
            follow_redirects=False,
        )

    kwargs.setdefault("upstream", "http://127.0.0.1:8879")
    kwargs.setdefault("upstream_host", "127.0.0.1:8790")
    return create_proxy_app(client_factory=client_factory, **kwargs)


@pytest.fixture
def stack(tmp_path):
    """A backend and a proxy in front of it, both live."""
    backend = create_web_app(_write_project(tmp_path), port=8790, simple_port=8791)
    with TestClient(backend) as backend_client:
        proxy = _proxy_for(backend)
        with TestClient(proxy) as proxy_client:
            yield backend, backend_client, proxy, proxy_client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}


def test_read_only_api_surface_is_reachable_through_the_proxy(stack):
    _, _, _, client = stack
    paths = [
        "/api/v1/bootstrap",
        "/api/v1/overview",
        "/api/v1/courses",
        "/api/v1/courses-config",
        "/api/v1/tasks",
        "/api/v1/runs",
        "/api/v1/operations",
        "/api/v1/schedules",
        "/api/v1/settings/connections",
        "/api/v1/settings/extension",
        "/api/v1/diagnostics",
        "/api/v1/diagnostics/support-bundle",
    ]
    statuses = {path: client.get(path).status_code for path in paths}
    assert statuses == dict.fromkeys(paths, 200)


def test_proxy_rewrites_host_so_the_backend_loopback_guard_still_matches(stack):
    backend, _, _, client = stack
    # The browser addresses the laptop; the tunnel port would otherwise reach the backend
    # as an unknown host and be refused outright.
    assert client.get("/api/v1/bootstrap").json()["server"] == "127.0.0.1:8790"

    wrong_host = _proxy_for(backend, upstream_host="127.0.0.1:8879")
    with TestClient(wrong_host) as mismatched:
        rejected = mismatched.get("/api/v1/bootstrap")
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "host_not_allowed"


def test_csrf_and_origin_checks_survive_the_proxy(stack):
    _, _, _, client = stack
    assert client.post("/api/v1/health-runs").json()["error"]["code"] == "csrf_failed"

    headers = _csrf(client)
    assert client.post("/api/v1/health-runs", headers=headers).status_code == 202

    foreign = {**headers, "Origin": "http://evil.example"}
    assert client.post("/api/v1/health-runs", headers=foreign).json()["error"]["code"] == (
        "origin_not_allowed"
    )


def test_simple_dashboard_origin_is_still_allowed_through_the_proxy(stack):
    _, _, _, client = stack
    headers = {**_csrf(client), "Origin": "http://127.0.0.1:8791"}
    assert client.post("/api/v1/health-runs", headers=headers).status_code == 202


def test_cors_preflight_is_answered_by_the_backend_through_the_proxy(stack):
    _, _, _, client = stack
    response = client.options(
        "/api/v1/extension/captures",
        headers={
            "Origin": "chrome-extension://" + "a" * 32,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-extension-token",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "chrome-extension://" + "a" * 32


def test_extension_pairing_token_is_relayed_unchanged(stack):
    backend, _, _, client = stack
    token = backend.state.runtime.extension_pairing_token

    paired = client.get("/api/v1/extension/status", headers={"X-Extension-Token": token})
    assert paired.status_code == 200
    assert paired.json()["connected"] is True

    unpaired = client.get("/api/v1/extension/status", headers={"X-Extension-Token": "wrong"})
    assert unpaired.status_code == 401
    assert unpaired.json()["error"]["code"] == "extension_pairing_failed"


def test_extension_capture_upload_and_screenshot_round_trip_through_the_proxy(stack):
    backend, _, _, client = stack
    token = backend.state.runtime.extension_pairing_token
    envelope = BrowserCaptureEnvelope(
        capture_id="capture-proxy-fixture",
        source_type="google_slides",
        source_url="https://docs.google.com/presentation/d/fixture/edit",
        resource_id="fixture",
        title="Fixture deck",
        captured_at=datetime.now(UTC),
        requested_mode=AcquisitionMode.BOTH,
        methods_used=[CaptureMethod.TEXT, CaptureMethod.SCREENSHOT],
        items=[
            BrowserCaptureItem(
                id="slide-1",
                kind="slide",
                text="Homework: read chapter 4",
                slide_id="slide-1",
            )
        ],
        screenshots=[
            BrowserCaptureScreenshot(
                id="shot-1",
                item_id="slide-1",
                mime_type="image/png",
                data_url=_png_data_url(),
                width=1200,
                height=800,
            )
        ],
        metadata={"adapter_id": "google_slides-v1"},
    )
    accepted = client.post(
        "/api/v1/extension/captures",
        headers={"X-Extension-Token": token, "Origin": "chrome-extension://" + "a" * 32},
        json=envelope.model_dump(mode="json"),
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["accepted"] is True

    listed = client.get("/api/v1/settings/extension").json()["captures"]
    assert [(item["source_type"], item["screenshot_count"]) for item in listed] == [
        ("google_slides", 1)
    ]

    discarded = client.delete(
        "/api/v1/extension/captures",
        headers={"X-Extension-Token": token, "Origin": "chrome-extension://" + "a" * 32},
    )
    assert discarded.status_code == 204
    assert client.get("/api/v1/settings/extension").json()["captures"] == []


def test_capture_size_guard_still_sees_content_length_through_the_proxy(stack):
    backend, _, _, client = stack
    token = backend.state.runtime.extension_pairing_token
    # The guard reads Content-Length; a proxy that switched to chunked encoding would
    # silently disable it.
    oversized = client.post(
        "/api/v1/extension/captures",
        headers={
            "X-Extension-Token": token,
            "Content-Type": "application/json",
            "Content-Length": str(200 * 1024 * 1024),
        },
        content=b"{}",
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "capture_too_large"


def test_extension_capture_request_long_poll_returns_no_content(stack):
    backend, _, _, client = stack
    token = backend.state.runtime.extension_pairing_token
    response = client.get(
        "/api/v1/extension/capture-requests/next?wait_seconds=0",
        headers={"X-Extension-Token": token},
    )
    assert response.status_code == 204


def test_course_and_schedule_mutations_apply_on_the_backend(stack):
    backend, _, _, client = stack
    headers = _csrf(client)

    created = client.post(
        "/api/v1/schedules",
        headers=headers,
        json={
            "name": "Weekday preview",
            "course_id": "spanish",
            "weekdays": [0, 1, 2, 3, 4],
            "local_time": "19:00",
            "timezone": "America/New_York",
            "mode": "preview",
            "enabled": True,
        },
    )
    assert created.status_code == 201
    schedule_id = created.json()["id"]
    # The schedule must exist in the backend's own store, not merely in the response.
    assert [item.id for item in backend.state.runtime.store.list_schedules()] == [schedule_id]

    assert client.post(
        f"/api/v1/schedules/{schedule_id}/disable", headers=headers
    ).json()["enabled"] is False
    assert client.delete(f"/api/v1/schedules/{schedule_id}", headers=headers).status_code == 204

    disabled = client.post("/api/v1/courses/spanish/disable", headers=headers)
    assert disabled.status_code == 200
    assert backend.state.runtime.settings.course("spanish").enabled is False


def test_run_creation_through_the_proxy_queues_on_the_backend(stack):
    backend, _, _, client = stack
    response = client.post(
        "/api/v1/runs",
        headers=_csrf(client),
        json={"course_id": "spanish", "mode": "preview", "week_selection": "this_week"},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert backend.state.runtime.store.get_run(run_id, include_events=False) is not None


def test_server_sent_events_reach_the_browser_through_the_proxy(stack):
    backend, _, _, client = stack
    store = backend.state.runtime.store
    run_id = store.create_run(
        course_id="spanish",
        trigger=RunTrigger.MANUAL,
        requested_mode=RunMode.PREVIEW,
    )
    store.add_event(run_id, stage=RunStage.QUEUED, event_type="queued", message="Queued")
    store.add_event(
        run_id,
        stage=RunStage.VALIDATE_CONFIGURATION,
        event_type="stage_completed",
        message="Configuration validated",
    )
    store.update_run(run_id, status=RunStatus.SUCCEEDED, finished_at=datetime.now(UTC))

    response = client.get(f"/api/v1/runs/{run_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: stage_completed" in response.text
    assert "Configuration validated" in response.text


def test_multipart_upload_reaches_the_backend_through_the_proxy(stack, tmp_path):
    backend, _, _, client = stack
    client_document = {
        "installed": {
            "client_id": "fixture-client-id",
            "client_secret": "fixture-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    response = client.post(
        "/api/v1/settings/oauth-client",
        headers=_csrf(client),
        files={"file": ("credentials.json", json.dumps(client_document), "application/json")},
    )
    assert response.status_code == 204
    stored = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert stored["installed"]["client_id"] == "fixture-client-id"


def test_support_bundle_download_headers_survive_the_proxy(stack):
    _, _, _, client = stack
    response = client.get("/api/v1/diagnostics/support-bundle")
    assert response.headers["content-disposition"] == (
        'attachment; filename="canvas-task-sync-support.json"'
    )
    assert "runs" in response.json()


def test_unknown_api_route_returns_the_backend_error_shape(stack):
    _, _, _, client = stack
    response = client.get("/api/v1/definitely-not-a-route")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_route_not_found"


def test_proxy_serves_the_dashboard_shell_locally(stack):
    _, _, _, client = stack
    response = client.get("/runs/12")
    assert response.status_code in {200, 503}
    if response.status_code == 503:
        assert response.json()["error"]["code"] == "frontend_not_built"
    else:
        assert response.headers["content-type"].startswith("text/html")


def test_unreachable_backend_reports_a_retryable_error(tmp_path):
    def broken_client_factory() -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("tunnel down"))
        )
        return httpx.AsyncClient(transport=transport, timeout=PROXY_TEST_TIMEOUT)

    proxy = create_proxy_app(
        upstream="http://127.0.0.1:8879",
        client_factory=broken_client_factory,
    )
    with TestClient(proxy) as client:
        response = client.get("/api/v1/overview")
    assert response.status_code == 502
    body = response.json()["error"]
    assert body["code"] == "backend_unreachable"
    assert body["retryable"] is True
    assert "8879" in body["message"]


def test_proxy_app_creates_no_runtime_and_no_local_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    upstream = FastAPI()

    @upstream.get("/api/v1/overview")
    def overview() -> dict[str, str]:
        return {}

    proxy = create_proxy_app(client_factory=_asgi_client_factory(upstream))
    with TestClient(proxy) as client:
        client.get("/api/v1/overview")
        assert getattr(proxy.state, "runtime", None) is None
    assert list(tmp_path.rglob("*.sqlite3")) == []
    assert not (tmp_path / ".canvas-task-sync").exists()


def test_hop_by_hop_headers_are_not_relayed_in_either_direction():
    recorded: dict[str, dict[str, str]] = {}
    upstream = FastAPI()

    @upstream.get("/api/v1/overview")
    def overview(request: Request) -> Response:
        recorded["request"] = {
            name.decode().casefold(): value.decode() for name, value in request.headers.raw
        }
        return Response(
            content=b"{}",
            media_type="application/json",
            headers={"connection": "keep-alive", "x-kept": "yes"},
        )

    proxy = create_proxy_app(client_factory=_asgi_client_factory(upstream))
    with TestClient(proxy) as client:
        response = client.get("/api/v1/overview", headers={"X-Kept": "yes", "TE": "trailers"})

    assert "te" not in recorded["request"]
    assert recorded["request"]["x-kept"] == "yes"
    assert recorded["request"]["host"] == "127.0.0.1:8790"
    assert "connection" not in {name.casefold() for name in response.headers}
    assert response.headers["x-kept"] == "yes"
    assert "te" in HOP_BY_HOP_HEADERS


def test_response_body_streams_instead_of_buffering():
    # A buffering proxy would hold "first" back until the upstream generator finished, so
    # receiving it before the release proves bytes are relayed as they arrive.
    released = threading.Event()
    upstream = FastAPI()

    @upstream.get("/api/v1/slow-stream")
    async def slow_stream() -> StreamingResponse:
        async def chunks():
            yield b"first\n"
            await asyncio.to_thread(released.wait, 20)
            yield b"second\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    with _serving(upstream) as upstream_url:
        proxy = create_proxy_app(upstream=upstream_url, upstream_host="127.0.0.1:8790")
        with (
            _serving(proxy) as proxy_url,
            httpx.Client(timeout=30.0) as client,
            client.stream("GET", f"{proxy_url}/api/v1/slow-stream") as response,
        ):
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = response.iter_lines()
            assert next(lines) == "first"
            released.set()
            assert next(lines) == "second"


def test_real_socket_round_trip_preserves_host_rewrite_and_streaming_download():
    seen: dict[str, str] = {}
    upstream = FastAPI()

    @upstream.get("/api/v1/echo-host")
    def echo_host(request: Request) -> dict[str, str]:
        seen["host"] = request.headers["host"]
        return {"host": request.headers["host"]}

    @upstream.post("/api/v1/echo-body")
    async def echo_body(request: Request) -> dict[str, object]:
        body = await request.body()
        return {"length": len(body), "declared": request.headers.get("content-length")}

    with _serving(upstream) as upstream_url:
        proxy = create_proxy_app(upstream=upstream_url, upstream_host="127.0.0.1:8790")
        with _serving(proxy) as proxy_url, httpx.Client(timeout=30.0) as client:
            assert client.get(f"{proxy_url}/api/v1/echo-host").json() == {"host": "127.0.0.1:8790"}
            payload = b"x" * 3_000_000
            echoed = client.post(f"{proxy_url}/api/v1/echo-body", content=payload).json()
            # Content-Length must survive so the backend's capture size guard keeps working.
            assert echoed == {"length": len(payload), "declared": str(len(payload))}
    assert seen["host"] == "127.0.0.1:8790"


def test_backend_still_serves_its_own_dashboards_when_run_locally(tmp_path):
    """Running the backend directly must keep working exactly as before."""
    backend = create_web_app(_write_project(tmp_path), port=8790, simple_port=8791)
    with TestClient(backend) as client:
        assert client.get("/api/v1/bootstrap").status_code == 200
        assert client.get("/api/v1/overview").status_code == 200
    state_files = list((tmp_path / ".canvas-task-sync").glob("*.sqlite3"))
    assert [path.name for path in state_files] == ["control.sqlite3"]
    assert sqlite3.connect(state_files[0]).execute("PRAGMA user_version").fetchone() is not None
