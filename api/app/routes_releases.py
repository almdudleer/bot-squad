"""Public release-feed endpoints (T-0082) + consumer telemetry (T-0088).

Exposes the release manifest produced by T-0081's ``prod.sh`` so attached
servers (T-0083 consumer poller) can pull the latest tarball, plus a
telemetry surface for those same consumers to report installed_version
back to the mothership.

Public release-feed (T-0082) — all three are PUBLIC in v0 (no auth header).
HTTPS-only is enforced upstream by traefik:

- ``GET /api/releases/latest`` — manifest entry for ``current``.
- ``GET /api/releases/{version}`` — manifest entry for a specific version.
- ``GET /api/releases/_files/{version}.tar.gz`` — streams the tarball.

Consumer telemetry (T-0088):

- ``POST /api/releases/_telemetry`` — consumer→mothership. Body carries
  ``install_id`` (the ``srv_<id>`` from Chapter I's install-token flow),
  validated against ``MothershipStore``. Unknown ids → 403. The POST is
  unauthenticated by install-flag (the install_id itself is the gate)
  so detached single-installs can still receive a stray POST and 403 it
  cleanly (their registry is empty, so every id is unknown).
- ``GET /api/releases/_telemetry`` — mothership UI fan-out for T-0087.
  Cookie-authed + mothership-only (404 unless ``MOTHERSHIP=1``); returns
  the full per-install snapshot list joined with the registry's
  ``display_name`` so the UI can label rows without a second round-trip.

The manifest path is fixed: ``<data_dir>/bot-squad/releases/index.json``
(matches what ``prod.sh`` writes; T-0081 spec is the SSOT for the entry
schema). On-disk entries carry ``tarball_path``; the API adds a
``tarball_url`` computed against the incoming request's base URL so it
works on staging.botsquad.dev and any detached-server URL without a
config change.

T-0087 (UI) extends this router with additional endpoints — keep this
module focused on the public feed + telemetry surface.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.mothership_store import MothershipStore
from app.routes_auth import require_auth

router = APIRouter(prefix="/releases", tags=["releases"])


def _releases_dir(request: Request) -> Path:
    cfg = request.app.state.api_config
    return cfg.data_dir / "bot-squad" / "releases"


def _load_manifest(releases_dir: Path) -> dict:
    index = releases_dir / "index.json"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="no releases manifest")
    try:
        return json.loads(index.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"manifest unreadable: {e}")


def _with_tarball_url(entry: dict, request: Request) -> dict:
    """Return a copy of ``entry`` with ``tarball_url`` populated.

    URL is reversed off the named ``releases_file`` route so a prefix
    change in ``main.py`` doesn't desync the link.
    """
    version = entry.get("version")
    if not version:
        return dict(entry)
    url = str(request.url_for("releases_file", filename=f"{version}.tar.gz"))
    return {**entry, "tarball_url": url}


@router.get("/latest")
def get_latest(request: Request) -> dict:
    manifest = _load_manifest(_releases_dir(request))
    current = manifest.get("current")
    releases = manifest.get("releases") or []
    if not current or not releases:
        raise HTTPException(status_code=404, detail="no releases available")
    for entry in releases:
        if entry.get("version") == current:
            return _with_tarball_url(entry, request)
    # `current` points at a version not in the list — manifest is broken,
    # but to the caller it's still a "not found" answer.
    raise HTTPException(
        status_code=404, detail=f"current version not in manifest: {current}"
    )


# Register the tarball route BEFORE the catch-all ``/{version}`` so
# ``_files`` is never swallowed by the version param.
@router.get("/_files/{filename}", name="releases_file")
def get_tarball(request: Request, filename: str) -> FileResponse:
    # Defensive: refuse path traversal and non-tar.gz names. Manifest
    # entries always end in ``.tar.gz`` (per T-0081); anything else is
    # either a typo or a probe.
    if "/" in filename or ".." in filename or not filename.endswith(".tar.gz"):
        raise HTTPException(
            status_code=404, detail=f"unknown release tarball: {filename}"
        )
    path = _releases_dir(request) / filename
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail=f"unknown release tarball: {filename}"
        )
    # FileResponse handles Content-Length from stat() + sets the
    # configured media_type. Content-Disposition with the basename is
    # nice-to-have for humans saving via curl -O.
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Telemetry (T-0088)
# ---------------------------------------------------------------------------
#
# Registered BEFORE ``/{version}`` so the underscore-prefixed paths aren't
# swallowed by the version param (same reason ``_files/{filename}`` lives
# above ``/{version}``).

# Fields accepted on the POST body (in addition to ``install_id``). Anything
# else the consumer ships is silently dropped at the store layer, but we
# enumerate here too so the route docstring + tests stay in sync with the
# spec without a second cross-reference.
_TELEMETRY_FIELDS = (
    "installed_version",
    "last_check_at",
    "last_apply_at",
    "last_apply_outcome",
    "current_git_sha",
)


def _telemetry_store(request: Request) -> MothershipStore:
    """``MothershipStore`` rooted at the canonical mothership data path.

    Kept distinct from ``routes_mothership._store`` to avoid a cross-module
    import that would tie the detach-deletable mothership module into the
    always-mounted releases router.
    """
    cfg = request.app.state.api_config
    return MothershipStore(cfg.data_dir / "_mothership")


@router.post("/_telemetry", status_code=204)
def post_telemetry(request: Request, payload: dict) -> None:
    """Consumer→mothership telemetry POST.

    Body shape (spec)::

        {
          "install_id": "srv_<hex>",
          "installed_version": "v...",
          "last_check_at": "<iso8601>",
          "last_apply_at": "<iso8601>" | null,
          "last_apply_outcome": "success" | "failed:<step>" | "never",
          "current_git_sha": "<sha40>" | null
        }

    Returns 204 on success, 400 on missing ``install_id``, 403 if the
    ``install_id`` is unknown to the registry (or this isn't a mothership
    at all — detached single-installs have an empty registry so every id
    is "unknown").

    The POST is intentionally unauthenticated by install-flag because the
    consumer-side caller (the autoupdate poller) hasn't got a session
    cookie. Gating is by ``install_id`` membership in ``MothershipStore``.
    """
    install_id = (payload.get("install_id") or "").strip()
    if not install_id:
        raise HTTPException(status_code=400, detail="install_id required")
    store = _telemetry_store(request)
    if store.get_server(install_id) is None:
        # Unknown install_id (or non-mothership). 403 keeps "unknown" and
        # "you're not the mothership" indistinguishable to a probe, which
        # is the same posture ``/connect`` takes (mothership-seam.md).
        raise HTTPException(status_code=403, detail="unknown install_id")
    telemetry = {k: payload.get(k) for k in _TELEMETRY_FIELDS}
    store.set_release_telemetry(install_id, telemetry)


@router.get("/_telemetry")
def get_telemetry(
    request: Request, _user: dict = Depends(require_auth)
) -> list[dict]:
    """Mothership-only GET: the per-install release-telemetry roll-up.

    404 unless ``MOTHERSHIP=1`` (so the detached single-install build
    silently doesn't expose this surface even though the router is
    mounted everywhere). Cookie-authed otherwise — same posture as the
    other ``/api/m`` cookie-surface endpoints.

    Each row joins the telemetry snapshot with the registry's
    ``display_name`` so the T-0087 UI can render the installs grid
    without a second round-trip. Servers that never reported telemetry
    appear with ``None`` for all release fields (rather than being
    omitted) so the UI can flag "registered but never checked in".
    """
    if os.environ.get("MOTHERSHIP", "0") != "1":
        raise HTTPException(status_code=404, detail="telemetry not available")
    rows: list[dict] = []
    for s in _telemetry_store(request).list_servers():
        release = s.release or {}
        rows.append(
            {
                "install_id": s.id,
                "install_name": s.display_name,
                "installed_version": release.get("installed_version"),
                "last_check_at": release.get("last_check_at"),
                "last_apply_at": release.get("last_apply_at"),
                "last_apply_outcome": release.get("last_apply_outcome"),
                "current_git_sha": release.get("current_git_sha"),
            }
        )
    return rows


# Catch-all version lookup — MUST stay last because ``/{version}`` swallows
# any unmatched single-segment path under ``/api/releases/`` (e.g.
# ``/api/releases/_telemetry`` would otherwise hit this handler with
# ``version="_telemetry"`` and 404 on the manifest lookup).
@router.get("/{version}")
def get_version(request: Request, version: str) -> dict:
    manifest = _load_manifest(_releases_dir(request))
    for entry in manifest.get("releases") or []:
        if entry.get("version") == version:
            return _with_tarball_url(entry, request)
    raise HTTPException(status_code=404, detail=f"unknown version: {version}")
