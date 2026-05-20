"""Public release-feed endpoints (T-0082) + consumer telemetry (T-0088)
+ mothership UI helpers (T-0087).

Exposes the release manifest produced by T-0081's ``prod.sh`` so attached
servers (T-0083 consumer poller) can pull the latest tarball, plus a
telemetry surface for those same consumers to report installed_version
back to the mothership, plus a couple of mothership-only endpoints that
back the T-0087 UI (the full manifest list + a release-notes draft writer).

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

Mothership UI surface (T-0087):

- ``GET /api/releases/_all`` — full manifest list (same per-entry shape
  as ``/latest`` but every entry, newest-first). Cookie-authed +
  mothership-only — the detached-install /latest feed is public, but
  the operator-facing roll-up is gated like the rest of /api/m/*.
- ``POST /api/releases/_notes_draft`` ``{notes: str}`` — write the
  release-notes body to ``data/bot-squad/releases/<next-version>.md``
  using the same vYYYY.MM.DD.N counter ``prod.sh`` derives. The deploy
  worker then picks the file up on cut (T-0081 step 5). Mothership-only.

The manifest path is fixed: ``<data_dir>/bot-squad/releases/index.json``
(matches what ``prod.sh`` writes; T-0081 spec is the SSOT for the entry
schema). On-disk entries carry ``tarball_path``; the API adds a
``tarball_url`` computed against the incoming request's base URL so it
works on staging.botsquad.dev and any detached-server URL without a
config change.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.mothership_store import MothershipStore
from app.routes_auth import require_auth

router = APIRouter(prefix="/releases", tags=["releases"])


def _refuse_unless_mothership() -> None:
    """Raise 404 unless this install is the mothership.

    Mirrors the gate on ``GET /_telemetry`` so the T-0087 UI endpoints
    behave identically on detached single-installs: the route stays
    mounted (the router itself is always wired in ``main.py``) but
    every operator-facing surface returns 404, identical to "not built
    with that feature".
    """
    if os.environ.get("MOTHERSHIP", "0") != "1":
        raise HTTPException(status_code=404, detail="not available")


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


# ---------------------------------------------------------------------------
# Mothership UI surface (T-0087)
# ---------------------------------------------------------------------------
#
# Both endpoints sit between the telemetry GET (also underscore-prefixed
# so it dodges the ``/{version}`` catch-all) and the final ``/{version}``
# fallback. They MUST stay above ``/{version}`` for the same reason as
# the other underscore routes.


@router.get("/_all")
def get_all(
    request: Request, _user: dict = Depends(require_auth)
) -> list[dict]:
    """Full manifest list for the T-0087 release-history table.

    Returns every entry from ``index.json``'s ``releases`` array with
    ``tarball_url`` filled in (same per-entry shape as ``/latest``),
    newest-first. The manifest is the canonical history (T-0081 spec);
    the manifest writer appends, so an order-by-creation_at sort is
    equivalent to "reverse the file order" without re-parsing
    timestamps.

    Mothership-only: detached single-installs don't run prod.sh and
    don't have a manifest at all, so this 404s twice over (gate + empty
    file). Auth + 404 match the GET /_telemetry posture so a single
    ``Suspense`` boundary on the UI side handles both.
    """
    _refuse_unless_mothership()
    releases_dir = _releases_dir(request)
    index = releases_dir / "index.json"
    if not index.is_file():
        # Empty list (not 404) — the UI table renders an empty-state
        # banner instead of an error envelope. Saves a happy-path branch
        # on the FE for fresh mothership installs before the first cut.
        return []
    try:
        manifest = json.loads(index.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"manifest unreadable: {e}")
    entries = manifest.get("releases") or []
    # Newest-first. prod.sh appends, so the on-disk array is oldest-first
    # — reverse here rather than baking the order into the FE.
    return [_with_tarball_url(e, request) for e in reversed(entries)]


# Filename schema mirrors prod.sh step 5: ``<RELEASES_DIR>/<version>.md``.
# Validating the computed version against this regex is belt-and-braces —
# the inputs are all internal (UTC date + manifest counter) but the file
# write is the one surface we don't want a malformed pattern reaching.
_VERSION_RE = re.compile(r"^v\d{4}\.\d{2}\.\d{2}\.\d+$")


def _next_version_for_today(releases_dir: Path) -> str:
    """Compute the same vYYYY.MM.DD.N tag prod.sh would compute next.

    Mirror of the inline ``python3 -`` block in
    ``data/bot-squad/deploy/prod.sh`` (step 2). Re-deriving it here lets
    the operator pre-stage notes BEFORE the deploy worker runs — when
    prod.sh executes it picks the same file up via the ``NOTES_FILE``
    branch.

    Edge case: if the operator drafts at 23:59 UTC and the deploy runs
    at 00:01 UTC, the file lands under "today" and prod.sh's "tomorrow"
    file lookup finds nothing. Spec explicitly accepts this — the
    operator just types the notes again, or the cut button is the same
    HTTP roundtrip + queue so the window is sub-second in practice.
    """
    date_tag = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    prefix = f"v{date_tag}."
    n = 0
    index = releases_dir / "index.json"
    if index.is_file():
        try:
            data = json.loads(index.read_text())
        except (OSError, json.JSONDecodeError):
            # If the manifest is unreadable, prod.sh would crash before
            # cutting — we still want the draft to be writeable so the
            # operator can stage notes for the post-fix re-run. Fall
            # through with n=0 (counter starts at 1).
            data = {}
        for entry in data.get("releases", []) or []:
            v = entry.get("version", "")
            if not v.startswith(prefix):
                continue
            try:
                n = max(n, int(v[len(prefix):]))
            except ValueError:
                # Malformed entry in the manifest — skip, don't crash.
                continue
    return f"v{date_tag}.{n + 1}"


@router.post("/_notes_draft", status_code=200)
def post_notes_draft(
    request: Request, payload: dict, _user: dict = Depends(require_auth)
) -> dict:
    """Stage release notes for the next cut.

    Body: ``{"notes": "<markdown>"}``. Writes ``<releases_dir>/<v>.md``
    where ``<v>`` is the next vYYYY.MM.DD.N — computed identically to
    prod.sh step 2 so the running deploy picks the file up via the
    ``NOTES_FILE`` lookup in step 5.

    Returns ``{ok: true, version: "<v>", path: "<rel-path>"}`` so the
    UI can echo the staged version back to the operator without a
    second round-trip.

    Idempotent: re-POSTing replaces the staged file (same name) so the
    operator can iterate on copy before hitting "cut". The same-day
    counter only advances after prod.sh actually cuts (which appends to
    the manifest), so re-drafting before cut keeps the same target.

    Mothership-only — see ``/_all`` for the same rationale.
    """
    _refuse_unless_mothership()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object required")
    notes = payload.get("notes")
    if not isinstance(notes, str):
        raise HTTPException(status_code=400, detail="notes must be a string")
    # Whitespace-only notes are almost certainly an accidental submit;
    # 400 is friendlier than silently writing an empty file that prod.sh
    # would gladly embed as a blank notes body.
    if not notes.strip():
        raise HTTPException(status_code=400, detail="notes must not be empty")

    releases_dir = _releases_dir(request)
    releases_dir.mkdir(parents=True, exist_ok=True)
    version = _next_version_for_today(releases_dir)
    if not _VERSION_RE.match(version):
        # Defence-in-depth: every input is internal, but if a future
        # change to the computation breaks the format we'd rather 500
        # here than write a malformed path.
        raise HTTPException(
            status_code=500, detail=f"computed bad version: {version!r}"
        )
    rel_path = f"data/bot-squad/releases/{version}.md"
    notes_path = releases_dir / f"{version}.md"
    # Atomic-ish write — same .tmp + replace dance the rest of the
    # codebase uses for config edits. prod.sh's notes lookup is a plain
    # ``[ -f ]`` so a half-written file would be picked up if the API
    # crashed mid-write without this.
    tmp = notes_path.with_suffix(".md.tmp")
    tmp.write_text(notes)
    os.replace(tmp, notes_path)
    return {"ok": True, "version": version, "path": rel_path}


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
