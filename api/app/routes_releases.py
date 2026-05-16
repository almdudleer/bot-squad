"""Public release-feed endpoints (T-0082).

Exposes the release manifest produced by T-0081's ``prod.sh`` so attached
servers (T-0083 consumer poller) can pull the latest tarball.

Three endpoints under ``/api/releases``:

- ``GET /api/releases/latest`` — manifest entry for ``current``.
- ``GET /api/releases/{version}`` — manifest entry for a specific version.
- ``GET /api/releases/_files/{version}.tar.gz`` — streams the tarball.

All three are PUBLIC in v0 (no auth header). HTTPS-only is enforced
upstream by traefik. Per-consumer signed access is deferred.

The manifest path is fixed: ``<data_dir>/bot-squad/releases/index.json``
(matches what ``prod.sh`` writes; T-0081 spec is the SSOT for the entry
schema). On-disk entries carry ``tarball_path``; the API adds a
``tarball_url`` computed against the incoming request's base URL so it
works on staging.botsquad.dev and any detached-server URL without a
config change.

T-0087 (UI) and T-0088 (telemetry) extend this router with additional
endpoints — keep this module focused on the public feed surface.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

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


@router.get("/{version}")
def get_version(request: Request, version: str) -> dict:
    manifest = _load_manifest(_releases_dir(request))
    for entry in manifest.get("releases") or []:
        if entry.get("version") == version:
            return _with_tarball_url(entry, request)
    raise HTTPException(status_code=404, detail=f"unknown version: {version}")
