"""Laptop-side dashboard host and transparent reverse proxy to the real backend.

This app deliberately imports nothing from the sync pipeline.  It serves the built
frontend from ``web_dist`` and forwards every ``/api/`` request, byte for byte, to the
authoritative backend reached through an SSH tunnel.  No ``WebRuntime``, ``SyncService``,
scheduler, or SQLite file is created on the machine running it.

Two details make the forwarding transparent rather than merely functional:

* The ``Host`` header is rewritten to the backend's own dashboard origin, so the
  backend's loopback host guard, CSRF origin checks, and extension pairing checks all see
  exactly what they would see without a tunnel.
* Responses stream.  Server-sent events, the extension's long-poll for capture requests,
  and the support-bundle download all pass through without buffering.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from canvas_task_sync.web_constants import (
    DEFAULT_BACKEND_HOST_HEADER,
    DEFAULT_REMOTE_BACKEND_URL,
    DEFAULT_SIMPLE_WEB_PORT,
    DEFAULT_WEB_PORT,
)

PROXIED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Connection-level headers belong to a single hop and must not be relayed.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# No read timeout: sync previews, health checks, SSE streams, and the extension's
# capture long-poll all hold a response open far longer than a default timeout allows.
# The connect timeout still fails fast when the SSH tunnel is down.
PROXY_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=None, pool=10.0)

ClientFactory = Callable[[], httpx.AsyncClient]


def _forwarded_request_headers(request: Request, upstream_host: str) -> list[tuple[str, str]]:
    forwarded: list[tuple[str, str]] = [("host", upstream_host)]
    for raw_name, raw_value in request.headers.raw:
        name = raw_name.decode("latin-1")
        if name.casefold() in HOP_BY_HOP_HEADERS or name.casefold() == "host":
            continue
        # Content-Length is kept so the backend's own capture size guard still sees it;
        # httpx honours an explicit length alongside a streamed body instead of chunking.
        forwarded.append((name, raw_value.decode("latin-1")))
    return forwarded


def _forwarded_response_headers(response: httpx.Response) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in response.headers.multi_items()
        if name.casefold() not in HOP_BY_HOP_HEADERS
    ]


def _unreachable_response(upstream: str) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "code": "backend_unreachable",
                "message": (
                    f"Could not reach the Canvas Task Sync backend at {upstream}. "
                    "Check that the SSH tunnel to the server is running."
                ),
                "retryable": True,
                "field_errors": None,
                "run_id": None,
            }
        },
    )


def create_proxy_app(
    *,
    upstream: str = DEFAULT_REMOTE_BACKEND_URL,
    upstream_host: str = DEFAULT_BACKEND_HOST_HEADER,
    port: int = DEFAULT_WEB_PORT,
    simple_port: int = DEFAULT_SIMPLE_WEB_PORT,
    client_factory: ClientFactory | None = None,
) -> FastAPI:
    del simple_port  # Accepted for symmetry with create_web_app; the backend owns CORS.
    upstream_base = upstream.rstrip("/")

    def _default_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=PROXY_TIMEOUT, follow_redirects=False)

    build_client = client_factory or _default_client_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = build_client()
        app.state.upstream = upstream_base
        app.state.upstream_host = upstream_host
        app.state.port = port
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(
        title="Canvas Task Sync Dashboard Proxy",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @app.api_route("/api/{full_path:path}", methods=PROXIED_METHODS, include_in_schema=False)
    async def proxy_api(request: Request, full_path: str):
        return await _proxy(request, f"/api/{full_path}")

    async def _proxy(request: Request, path: str):
        client: httpx.AsyncClient = request.app.state.client
        query = request.url.query
        url = f"{upstream_base}{path}{'?' + query if query else ''}"
        upstream_request = client.build_request(
            request.method,
            url,
            headers=_forwarded_request_headers(request, upstream_host),
            content=request.stream(),
        )
        try:
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            return _unreachable_response(upstream_base)

        async def body() -> AsyncIterator[bytes]:
            try:
                # aiter_raw leaves any Content-Encoding intact, so the forwarded
                # Content-Length and Content-Encoding headers stay truthful.
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        streamed = StreamingResponse(body(), status_code=response.status_code)
        # Replacing raw_headers wholesale keeps repeated headers (Vary, Set-Cookie) intact
        # and stops Starlette from inventing a Content-Type the backend did not send.
        streamed.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in _forwarded_response_headers(response)
        ]
        return streamed

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    web_dist = Path(__file__).with_name("web_dist")
    assets = web_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="proxy-web-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend(full_path: str):
        del full_path
        index = web_dist / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "frontend_not_built",
                    "message": "Run the frontend build before starting the dashboard proxy.",
                    "retryable": False,
                }
            },
        )
