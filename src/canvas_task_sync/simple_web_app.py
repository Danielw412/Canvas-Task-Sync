from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from canvas_task_sync.web_constants import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT


def create_simple_web_app(*, backend_port: int = DEFAULT_WEB_PORT) -> FastAPI:
    app = FastAPI(
        title="Canvas Task Sync Simple",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    web_dist = Path(__file__).with_name("web_dist")
    assets = web_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="simple-web-assets")

    @app.get("/runtime-config.json", include_in_schema=False)
    def runtime_config() -> dict[str, str]:
        return {"api_base": f"http://{DEFAULT_WEB_HOST}:{backend_port}"}

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        del full_path
        index = web_dist / "simple.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "frontend_not_built",
                    "message": "Run the frontend build before starting the simple UI.",
                    "retryable": False,
                }
            },
        )

    return app
