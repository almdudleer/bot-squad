"""Mothership centralization-layer routes.

Mounted only when the ``MOTHERSHIP`` env var is ``"1"`` (see ``main.py``).
Detach build: delete this file + ``mothership_store.py`` + ``web/src/mothership/``,
rebuild — no other code references the centralization layer.

This file ships the SCAFFOLD only. Sibling tasks land the surface:

- T-0024 — install-token issuance, ``GET /i/<token>/install.sh``,
  ``GET /i/<token>/instructions.md``, ``POST /installer/connect`` handshake,
  install-checkpoint stream
- T-0023 — per-server backend client (proxy or direct), used by
  ``GET /servers/{id}/projects`` aggregate
- T-0025 — cross-server ``/projects`` aggregate consumed by the all-projects-
  by-server UI page

The contract these tasks consume is fixed in
``vision/architecture/mothership-seam.md`` — read that before extending
this surface.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.mothership_store import MothershipStore
from app.routes_auth import require_auth


router = APIRouter(tags=["mothership"], dependencies=[Depends(require_auth)])


def _store(request: Request) -> MothershipStore:
    cfg = request.app.state.api_config
    return MothershipStore(cfg.data_dir / "_mothership")


@router.get("/servers")
def list_servers(request: Request) -> list[dict]:
    return [s.to_public() for s in _store(request).list_servers()]
