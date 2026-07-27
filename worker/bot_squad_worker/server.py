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
        from bot_squad_worker.deploy import boot_git_sha, effective_worker_git_sha
        return {
            "ok": True,
            "version": _pkg_version(),
            "uptime": time.monotonic() - started,
            "git_sha": effective_worker_git_sha(),
            "boot_git_sha": boot_git_sha(),
        }

    @app.post("/actions/{name}")
    def call_action(name: str, params: dict | None = None) -> dict:
        try:
            return dispatch(name, params or {})
        except ActionError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return app


# Module-level app for `uvicorn bot_squad_worker.server:app --uds …`
app = build_app()
