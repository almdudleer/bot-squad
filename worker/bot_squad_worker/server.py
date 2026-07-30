"""FastAPI app the worker exposes over a Unix socket."""
from __future__ import annotations

import time
from importlib.metadata import version, PackageNotFoundError

from fastapi import FastAPI, HTTPException

from bot_squad_worker.actions import ActionError, dispatch


def _pkg_version() -> str:
    try:
        return version("bot-squad-worker")
    except PackageNotFoundError:
        return "0.0.0"


def build_app() -> FastAPI:
    """Build a fresh FastAPI app. Tests instantiate one per test for isolation."""
    app = FastAPI(title="bot-squad-worker", version=_pkg_version())
    started = time.monotonic()

    @app.get("/health")
    def health() -> dict:
        # T-0335 item-13: frozen-at-boot git_sha lets a deploy detect that the
        # running worker is on stale code (running_sha != deployed_sha).
        # T-0717: report the EFFECTIVE sha — the boot sha advanced to the deployed
        # sha when worker/ is byte-identical between them, so a deploy that
        # (correctly) skipped the restart doesn't read as permanent drift.
        # boot_sha stays exposed alongside it: it's what the restart gate compares,
        # so an operator debugging a skipped/needed restart can still see it.
        # T-0824: `install_git_sha` — the same missing term, on the twin surface.
        # Neither of the two shas above answers "is this process running the
        # DEPLOYED code": `boot_git_sha` is what it loaded and `git_sha` is that
        # advanced only when `worker/` is byte-identical, so both can be equal
        # AND both behind the tree. That is the state that read as healthy on
        # /api/health for the hours before a restart. Cheaper here than there —
        # this process runs FROM the install tree, so it just reads it, with no
        # marker in between.
        from bot_squad_worker.deploy import (
            boot_git_sha, effective_worker_git_sha, install_tree_git_sha,
            restart_pending_state,
        )
        body = {
            "ok": True,
            "version": _pkg_version(),
            "uptime": time.monotonic() - started,
            "git_sha": effective_worker_git_sha(),
            "boot_git_sha": boot_git_sha(),
            # "" (never a guess) when git cannot answer — a caller comparing
            # against it must treat empty as UNKNOWN, not as a match.
            "install_git_sha": install_tree_git_sha(),
        }
        # T-0739: same discrimination the API's /api/health makes, on the surface
        # an operator debugging from the worker side reaches for. Present only
        # when a restart is genuinely owed/in flight — absent is the healthy case.
        from bot_squad_worker.actions import _get_config
        try:
            pending = restart_pending_state(_get_config())
        except Exception:  # noqa: BLE001 — /health must never fail on a probe
            pending = None
        if pending:
            body["restart_pending"] = pending
        return body

    @app.post("/actions/{name}")
    def call_action(name: str, params: dict | None = None) -> dict:
        try:
            return dispatch(name, params or {})
        except ActionError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return app


# Module-level app for `uvicorn bot_squad_worker.server:app --uds …`
app = build_app()
