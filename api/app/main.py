"""FastAPI app entrypoint — wires routers and reads config from env."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from urllib.parse import urlparse

from app.config import ApiConfig, AuthConfig
from app.routes_health import router as health_router


def _hostname_from_url(url: str) -> str:
    """Best-effort FQDN extraction for the mothership self-register label.

    Returns ``""`` if the URL is unparseable so the caller can fall back to
    its hard-coded default.
    """
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


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

    # Phase 2 multi-user: build the WorkerRouter once so request handlers
    # don't have to re-parse config on every call.
    from app.worker_client import WorkerRouter
    coordinator_user = os.environ.get("BOT_SQUAD_COORDINATOR_USER", "almdudleer")
    known_users = {
        meta.linux_user for meta in app.state.auth_config.user_meta.values()
    }
    app.state.worker_router = WorkerRouter(
        coordinator_sock=sock_path,
        coordinator_user=coordinator_user,
        known_users=known_users,
    )

    app.include_router(health_router, prefix="/api")

    from app.routes_auth import router as auth_router
    app.include_router(auth_router, prefix="/api")

    from app.routes_projects import router as projects_router
    app.include_router(projects_router, prefix="/api")

    from app.routes_backlog import router as backlog_router
    app.include_router(backlog_router, prefix="/api")

    # T-0147: product-analytics (internal-usage) dashboard.
    from app.routes_analytics import router as analytics_router
    app.include_router(analytics_router, prefix="/api")

    from app.routes_vision import router as vision_router
    from app.routes_feedback import router as feedback_router
    from app.routes_usecases import router as usecases_router
    from app.routes_docs import router as docs_router
    from app.routes_sessions import router as sessions_router, dev_spawn_router
    from app.routes_runs import router as runs_router
    from app.routes_messages import router as messages_router
    from app.routes_scheduler import router as scheduler_router
    app.include_router(vision_router, prefix="/api")
    app.include_router(feedback_router, prefix="/api")
    app.include_router(usecases_router, prefix="/api")
    app.include_router(docs_router, prefix="/api")
    app.include_router(sessions_router, prefix="/api")
    app.include_router(dev_spawn_router, prefix="/api")
    app.include_router(runs_router, prefix="/api")
    app.include_router(messages_router, prefix="/api")
    app.include_router(scheduler_router, prefix="/api")

    # T-0153: autopilot — prompt-driven, time-boxed autonomous runs surfaced
    # via the kebab popover on teams / sessions / the project header.
    from app.routes_autopilot import router as autopilot_router
    app.include_router(autopilot_router, prefix="/api")

    from app.routes_intersession import router as intersession_router
    app.include_router(intersession_router, prefix="/api")

    from app.routes_users import router as users_router
    from app.routes_settings import router as settings_router
    from app.routes_me import router as me_router
    app.include_router(users_router, prefix="/api")
    app.include_router(settings_router, prefix="/api")
    app.include_router(me_router, prefix="/api")

    from app.routes_welcome import router as welcome_router
    app.include_router(welcome_router, prefix="/api")

    # T-0082: public release-feed endpoints. Mounted on every install
    # (not gated behind MOTHERSHIP) — detached single-installs simply
    # have an empty `data/bot-squad/releases/` so all three endpoints
    # return 404. The mothership populates the manifest via prod.sh.
    from app.routes_releases import router as releases_router
    app.include_router(releases_router, prefix="/api")

    # T-0089: consumer-side autoupdate status/pause/check_now. Mounted on
    # every install but each route 404s when MOTHERSHIP=1 (the producer
    # never consumes its own releases, so there's no state to surface).
    from app.routes_autoupdate import router as autoupdate_router
    app.include_router(autoupdate_router, prefix="/api")

    # Centralization-layer routes — mounted only on botsquad.dev installs.
    # Single-install servers run with MOTHERSHIP unset (or "0") and never
    # expose /api/m/* or /i/*. Detach build = MOTHERSHIP=0 (or delete the module).
    # Three routers because the install flow has three distinct auth surfaces;
    # see routes_mothership.py for the rationale.
    if os.environ.get("MOTHERSHIP", "0") == "1":
        from app.routes_mothership import (
            router as mothership_router,
            installer_router as mothership_installer_router,
            bundle_router as mothership_bundle_router,
        )
        app.include_router(mothership_router, prefix="/api/m")
        app.include_router(mothership_installer_router, prefix="/api/m")
        # Bundle GETs sit at root: /i/<token>/install.sh + instructions.md.
        # They must register BEFORE the SPA catch-all below.
        app.include_router(mothership_bundle_router)

        # T-0489: TG conversation history (per project+user). Mothership-only —
        # the user-communication module is centralized here (voice-04) and keys
        # on the mothership global_user_id (T-0488). Worker-token write surface
        # + session-auth read surface, both under /api/m.
        from app.routes_conversations import (
            router as conversations_router,
            worker_router as conversations_worker_router,
        )
        app.include_router(conversations_router, prefix="/api/m")
        app.include_router(conversations_worker_router, prefix="/api/m")

        # T-0055: self-register this mothership in its own registry on boot so
        # the unified all-projects view at `/` has a row for "this server"
        # without waiting for an admin to manually add it. Idempotent — dedup
        # by base_url; subsequent boots no-op.
        #
        # MOTHERSHIP_BASE_URL is the canonical config knob (already used by
        # routes_mothership._mothership_base_url for install-bundle URLs).
        # If unset, fall back to the staging hostname per T-0055 brief; a
        # follow-up ticket should make this configurable cleanly.
        from app.mothership_store import MothershipStore

        self_url = os.environ.get(
            "MOTHERSHIP_BASE_URL", "https://staging.botsquad.dev"
        )
        self_name = os.environ.get("MOTHERSHIP_SELF_NAME") or _hostname_from_url(
            self_url
        ) or "this server"
        try:
            MothershipStore(data_dir / "_mothership").register_self_if_missing(
                base_url=self_url,
                display_name=self_name,
            )
        except OSError:
            # A read-only DATA_DIR shouldn't crash app boot; log-and-continue.
            # The detach build never enters this branch (MOTHERSHIP=0).
            import logging
            logging.getLogger(__name__).warning(
                "mothership self-register failed (continuing without it)",
                exc_info=True,
            )

    from app.routes_auth import require_auth
    from app.worker_client import WorkerError
    from fastapi import Depends, HTTPException

    @app.post("/api/worker/noop")
    async def worker_noop(_user: dict = Depends(require_auth)) -> dict:
        client = app.state.worker_router.coordinator()
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

        # T-0369: response_model=None — the `-> FileResponse` return annotation is
        # a ForwardRef under `from __future__ import annotations`; without this
        # FastAPI tries to build a response-model schema for it and /openapi.json
        # 500s. FileResponse is a Response class, not a model.
        @app.get("/{full_path:path}", response_class=FileResponse, response_model=None)
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
