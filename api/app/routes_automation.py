"""The project's drive state + the ultimate off-switch (T-0929).

> "And there should be explicit UI state where I could easily turn them off for
> a project, just stop any automatic activity all at once. And explicitly see if
> it's happening in the first place. Now that's all very unclear there and
> uncontrollable, and there are many ways in which the system operates around
> this mechanism. But that should be the ultimate switch."
> — the stakeholder, 2026-08-28.

Two endpoints, both PROXIES to the worker (`automation_status` /
`automation_set_state`) rather than file reads/writes like the sibling
transparency surface. That difference is deliberate and is the ticket:

* **the off switch must also END running autopilots**, which a file write cannot
  do — the root cause found on this ticket was precisely a pause flag that
  changed a file and left the driven sessions working;
* **the mechanism registry — what the switch actually covers — must have exactly
  ONE copy.** The drive block's on-disk *shape* is mirrored here (see
  ``routes_transparency``) because the api cannot import worker code and the UI
  must still render a mode when the worker is down. A second, hand-maintained
  list of *which subsystems are automatic* is a different animal: it would drift
  the first time someone adds a tick, and a switch that is documented to stop
  something it does not is the reported defect wearing a new face.

So: "which mode is set" survives a dead worker (transparency, file-read); "what
is running right now and stop it" requires a live worker, which is honest —
with no worker, nothing is running.

⚠ **T-0674 is superseded HERE, and only here.** That ticket's 2026-07-25 verdict
cut every web-side operator control ("pause/resume now live in TG/CLI only", one
surviving web lever). This ticket, 2026-08-28, asks for a project off-switch in
the UI in his own words. The later ask wins for this one control; the rest of
T-0674's cut stands.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.project_authz import require_project_member
from app.routes_auth import require_auth
from app.worker_client import WorkerClient, WorkerError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{slug}/automation",
    tags=["automation"],
    dependencies=[Depends(require_auth)],
)

#: Mirrors ``pace.DRIVE_STATES``. Validated here as well as in the worker so a
#: typo comes back a 400 naming the closed set, not a 500 from the socket.
_STATES = ("one_task", "finish_up", "all_tasks", "off")


def _worker(request: Request) -> WorkerClient:
    return request.app.state.worker_router.coordinator()


def _check_project(request: Request, slug: str) -> None:
    cfg = request.app.state.api_config
    if cfg.project(slug) is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {slug}")


class StateBody(BaseModel):
    state: str
    #: The words that set it, stored verbatim on the pace config so every surface
    #: can print WHY the state is what it is rather than leaving him to infer it.
    source_text: str | None = None


@router.get("")
async def get_automation(slug: str, request: Request,
                         user: dict = Depends(require_auth)) -> dict:
    """Is anything automatic running for this project, and in what mode.

    Returns the worker's ``automation.snapshot``: the effective drive state,
    whether the switch is engaged, every mechanism the switch covers (and every
    one it deliberately does not, with the reason), and the autopilot runs live
    right now.
    """
    _check_project(request, slug)
    try:
        return await _worker(request).call_action("automation_status", {"slug": slug})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/state", dependencies=[Depends(require_project_member)])
async def set_automation_state(slug: str, body: StateBody, request: Request,
                               user: dict = Depends(require_auth)) -> dict:
    """Set the project's named drive state — including ``off``, the switch.

    Project-WRITE, so it carries ``require_project_member`` (T-0381's SSOT gate,
    admin-only until per-project roles land). Changing a project's drive state is
    orchestration control in the same class as ``autopilot/start`` beside it —
    the enumeration guard in ``test_project_write_authz.py`` enforces this, and
    the read above stays broad like every other read.

    Returns the resulting snapshot, so the UI renders what the system now IS
    rather than assuming the write did what it asked. That is not ceremony: "the
    operator drive mechanism either changes state without my confirmation or is
    not covering the whole system" is a report about surfaces that claim a state
    they do not have.
    """
    _check_project(request, slug)
    state = (body.state or "").strip().replace("-", "_")
    if state not in _STATES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid drive state {body.state!r} — must be one of: "
                   f"{', '.join(_STATES)}",
        )
    params: dict = {
        "slug": slug,
        "state": state,
        "requested_by": str(user.get("email") or user.get("sub") or "web"),
    }
    if body.source_text:
        params["source_text"] = body.source_text
    try:
        return await _worker(request).call_action("automation_set_state", params)
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
