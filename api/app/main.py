"""FastAPI app entrypoint — wires routers and reads config from env."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from app.config import ApiConfig, AuthConfig
from app.routes_health import router as health_router


def build_app() -> FastAPI:
    app = FastAPI(title="bot-squad-api")

    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    sock_path = Path(os.environ.get("WORKER_SOCK", str(data_dir / "_sock" / "worker.sock")))
    jwt_secret = os.environ.get("JWT_SECRET", "")
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET env var is required")

    app.state.api_config = ApiConfig.load(config_dir)
    app.state.auth_config = AuthConfig.load(config_dir)
    app.state.sock_path = sock_path
    app.state.heartbeat_path = data_dir / "_worker" / "heartbeat"
    app.state.jwt_secret = jwt_secret
    app.state.cookie_secure = os.environ.get("COOKIE_SECURE", "1") == "1"

    app.include_router(health_router, prefix="/api")

    from app.routes_auth import router as auth_router
    app.include_router(auth_router, prefix="/api")

    from app.routes_projects import router as projects_router
    app.include_router(projects_router, prefix="/api")

    from app.routes_backlog import router as backlog_router
    app.include_router(backlog_router, prefix="/api")

    from app.routes_vision import router as vision_router
    from app.routes_feedback import router as feedback_router
    from app.routes_sessions import router as sessions_router
    from app.routes_runs import router as runs_router
    from app.routes_messages import router as messages_router
    app.include_router(vision_router, prefix="/api")
    app.include_router(feedback_router, prefix="/api")
    app.include_router(sessions_router, prefix="/api")
    app.include_router(runs_router, prefix="/api")
    app.include_router(messages_router, prefix="/api")

    from app.routes_auth import require_auth
    from app.worker_client import WorkerClient, WorkerError
    from fastapi import Depends, HTTPException

    @app.post("/api/worker/noop")
    async def worker_noop(_user: dict = Depends(require_auth)) -> dict:
        client = WorkerClient(app.state.sock_path)
        try:
            return await client.call_action("noop", {})
        except WorkerError as e:
            raise HTTPException(status_code=502, detail=str(e))

    # Serve the React bundle from /app/web/dist (set by Dockerfile).
    web_dist = Path(os.environ.get("WEB_DIST", "/app/web/dist"))
    if web_dist.exists():
        # Mount last so /api/* takes precedence; fall through to index.html via SPA route.
        from fastapi.responses import FileResponse
        from fastapi import HTTPException as _HE

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> FileResponse:
            # Don't intercept /api routes (already routed above).
            if full_path.startswith("api"):
                raise _HE(status_code=404)
            file = web_dist / full_path
            if file.is_file():
                return FileResponse(file)
            return FileResponse(web_dist / "index.html")

    return app


app = build_app() if os.environ.get("BUILD_AT_IMPORT", "0") == "1" else None
