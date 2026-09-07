"""The operator SEAT (T-0937) — who is DRIVING this board, whatever their role.

Stakeholder (2026-08-31, live tmux, interrupting a ``bsq bud operator`` call):

    "от меня сейчас не так много запросов, но запрос на параллелизм, так что ты
    должен стать оператором одновременно с юзер-сессией по идее"

    "ну вот первая на интроспекцию что ты подумал что тебе надо поставить
    отдельного оператора (и он появился уже)"

WHAT WENT WRONG, MEASURED. The root user-conversation session set a drive mode
and resumed the pace; 25 seconds later the 60s :mod:`operator_redrive` tick
minted a separate operator session, because "who holds the operator seat" was
derived from ONE thing only — ``role == operator`` on a live session
(``dispatch.live_operator_sids``). The root was actively driving the board and
the stakeholder had just refused a separate operator, and neither fact was
legible to the gate: the moment the load crossed the T-0855 ceilings the tier
flipped and the spawn fired.

THE FIX IS A SEAT, NOT A ROLE. The root cannot simply *become* the operator:
:func:`sessions._role_of` is single-valued, and morphing the root would break
its user-conversation routing (``ensure_user_conversation``, ``role_exempt``,
TG attendance) — the T-0932 ladder treats the two as exclusive rungs. So
"operator" becomes a **HAT** the root can wear while keeping its own role:

    the SEAT is held  ⟺  a LIVE ROOT session has claimed the drive

and every spawn path — the 60s re-drive, ``bsq bud operator``,
``dispatch.decide_topology`` — asks THIS module instead of asking only "is
there a session whose role is operator". One SSOT, so the scheduler and a
session can never disagree about whether the board already has a driver.

TWO WAYS TO HOLD IT, and the implicit one is what closes the measured gap:

* **explicit** — ``bsq operator seat claim`` writes ``claim.json``. The
  deliberate "I am driving this board myself" move, and the one that survives a
  project with no drive mode configured at all.
* **implicit (drive)** — the standing drive block's ``set_by`` names a LIVE
  root session. ``bsq pace drive`` already stamps the setter's SID there
  (``require_sid``), so the session that SET the drive is by construction the
  session that is driving it. No new user action was needed to fix the
  incident: the root that ran ``bsq pace drive --scope all`` holds the seat
  from that moment.

An explicit claim (or an explicit RELEASE, see :func:`release`) always beats the
implicit one, resolved by recency when both exist.

LIVENESS IS THE WHOLE LEASE. There is no expiry and no heartbeat: the seat is
held for exactly as long as the holder's session md reads live (active or
paused — a paused root is a parked process that comes back, and treating that
brief window as "vacant" is the very race this module exists to remove).
A dead / archived / recycled holder vacates the seat by itself, so «no spawn
while it lives, spawn resumes if it dies with drive on» needs no reaper.

FAIL-OPEN, DELIBERATELY. :func:`seat_holder` never raises: any unreadable state
resolves to "vacant", i.e. to pre-T-0937 behaviour, because a broken seat read
must never strand the backlog. The surface function :func:`seat_status` carries
the ``error`` explicitly so a fault is visible rather than reported as an
ordinary empty seat.

Kill switch: ``BOT_SQUAD_OPERATOR_SEAT=0`` — no session can hold the seat and
every gate behaves exactly as it did before this module existed.

Interlocks: T-0932 (the budding ladder — this is what makes L2 a CHOICE rather
than something the tick does behind the root's back), T-0929 (drive states),
T-0855 (the tier ceilings), T-0523 / T-0472 (operator identity + singleton).
The full role registry is NOT here — that is T-0943.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

def _enabled() -> bool:
    """False iff ``BOT_SQUAD_OPERATOR_SEAT`` disables the seat entirely."""
    raw = os.environ.get("BOT_SQUAD_OPERATOR_SEAT")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_dir(cfg: Any, slug: str) -> Path:
    return cfg.data_dir / slug / "_worker" / "operator_seat"


def _claim_path(cfg: Any, slug: str) -> Path:
    """The explicit claim record. Present-with-``held:true`` = claimed;
    present-with-``held:false`` = an explicit RELEASE tombstone (see
    :func:`release`); absent = nothing explicit was ever said."""
    return _state_dir(cfg, slug) / "claim.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _drive_fingerprint(drive: dict) -> str:
    """A stable digest of a normalised drive block — the identity of one drive
    STATEMENT. Used only to tell "the drive I released" from "a drive that has
    been re-stated since"; see the tombstone rule in :func:`_resolve`."""
    return hashlib.sha1(
        json.dumps(drive, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _read_record(cfg: Any, slug: str) -> Optional[dict]:
    """The raw claim/release record as stored, or None. No liveness applied."""
    try:
        data = json.loads(_claim_path(cfg, slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(cfg: Any, slug: str, rec: dict) -> None:
    d = _state_dir(cfg, slug)
    d.mkdir(parents=True, exist_ok=True)
    p = _claim_path(cfg, slug)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True))
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Eligibility — WHO may wear the hat
# ---------------------------------------------------------------------------

def _live_meta(cfg: Any, slug: str, sid: str) -> Optional[dict]:
    """Session metadata for ``sid`` iff it is a LIVE session of ``slug``.

    Live = ``sessions._is_live_holder`` (active or paused, not archived) — the
    same roster ``dispatch.live_role_sids`` and ``budding._live_sessions`` read,
    so the seat can never see a session the topology gate does not.
    """
    from bot_squad_worker import sessions as S

    if not sid:
        return None
    sess_dir = cfg.data_dir / slug / "sessions"
    if not sess_dir.exists():
        return None
    p = sess_dir / f"{sid}.md"
    meta = S._read_session_metadata(p) if p.exists() else None
    if meta is None:
        # The filename IS the SID by convention; scan only when it is not.
        for md in sorted(sess_dir.glob("*.md")):
            m = S._read_session_metadata(md)
            if m is not None and str(m.get("sid") or md.stem) == sid:
                meta = m
                break
    if meta is None or not S._is_live_holder(meta):
        return None
    return meta


def eligibility(cfg: Any, slug: str, sid: str) -> dict:
    """May ``sid`` hold the operator seat? ``{ok, reason, meta}``.

    Only the ROOT session qualifies — the one whose IMMUTABLE SID carries the
    ``user-conversation`` window (``budding.is_root_session``, the same identity
    rule ``live_user_conversation_sid`` uses). Two reasons it is that test and
    not "role == user-conversation":

    * the root's stored role oscillates by design (it reads ``dev`` while it
      holds a task — that IS the L0 rung), so a role test would drop the seat at
      exactly the moment the root is doing the thing this module is about;
    * a DEV or team-lead wearing the operator hat is not the ask and would let
      any leaf worker suppress the board's driver.

    An actual ``operator``-role session does not need a seat claim: it holds the
    board by role, through the T-0472 singleton. A DEDICATED one is detected by
    ``dispatch.live_operator_sids``; a session holding operator ALONGSIDE other
    roles is not, which is why the claim gate below asks
    ``dispatch.operator_exclusivity_sids`` (T-0943) — it was asking the
    singleton, and a root could claim the seat beside a live talking-operator.
    """
    from bot_squad_worker import budding as _budding

    meta = _live_meta(cfg, slug, sid)
    if meta is None:
        return {"ok": False, "meta": None,
                "reason": f"{sid or '(none)'} is not a live session of {slug!r} "
                          f"— the seat is held by a living session or not at all"}
    if not _budding.is_root_session(sid, meta):
        return {"ok": False, "meta": meta,
                "reason": f"{sid} is not the root user-conversation session — "
                          f"only the root may wear the operator hat while "
                          f"keeping its own role (T-0937)"}
    return {"ok": True, "meta": meta, "reason": ""}


# ---------------------------------------------------------------------------
# The read every gate makes
# ---------------------------------------------------------------------------

def _holder_record(cfg: Any, slug: str, sid: str, kind: str, *,
                   since: Any, source: str, meta: dict) -> dict:
    from bot_squad_worker import sessions as S

    return {
        "sid": sid,
        "kind": kind,                       # "claim" | "drive"
        "since": since,                     # ISO-8601 Z, or None
        "source": source or "",             # the words that set it, if any
        "role": S._role_of(meta),           # its OWN role — the hat is extra
        "status": str(meta.get("status") or ""),
    }


def _resolve(cfg: Any, slug: str) -> dict:
    """The full resolution, including WHY a candidate was rejected.

    ``{holder, explicit, implicit, rejected: [...]}`` — ``holder`` is the
    effective seat record or None. Raises; :func:`seat_holder` is the tolerant
    wrapper every gate uses.
    """
    rejected: list[str] = []
    explicit: Optional[dict] = None
    implicit: Optional[dict] = None

    rec = _read_record(cfg, slug)
    rec_sid = str((rec or {}).get("sid") or "") if rec else ""
    rec_held = bool((rec or {}).get("held")) if rec else False

    if rec and rec_held:
        el = eligibility(cfg, slug, rec_sid)
        if el["ok"]:
            explicit = _holder_record(
                cfg, slug, rec_sid, "claim",
                since=rec.get("at_iso"), source=str(rec.get("source") or ""),
                meta=el["meta"])
        else:
            rejected.append(f"claim({rec_sid}): {el['reason']}")

    from bot_squad_worker import pace as _pace
    drive = _pace.read_drive(cfg, slug)
    if drive.get("configured"):
        d_sid = str(drive.get("set_by") or "")
        # `set_by` is free text (the worker write path defaults it to "user");
        # only a real SID can name a session, and only a live root can hold the
        # seat. Anything else is provenance, not a claim.
        if d_sid.startswith("S-"):
            el = eligibility(cfg, slug, d_sid)
            if el["ok"]:
                implicit = _holder_record(
                    cfg, slug, d_sid, "drive",
                    since=drive.get("set_at"),
                    source=str(drive.get("source_text") or ""),
                    meta=el["meta"])
            else:
                rejected.append(f"drive.set_by({d_sid}): {el['reason']}")

    holder = explicit
    if holder is None and implicit is not None:
        # An explicit RELEASE tombstone suppresses the implicit claim it was
        # meant to drop — otherwise "release" would silently not release, since
        # the drive block still names the same live root. A drive RE-SET after
        # the release is a fresher statement and wins.
        #
        # "Fresher" is decided by the STATEMENT, not by comparing clocks: the
        # tombstone fingerprints the drive block as it stood when the seat was
        # dropped, and the implicit claim revives the moment that block changes.
        # Ordering two stamps instead LOSES the case — pace writes ``set_at`` at
        # SECOND resolution while the tombstone carries a fractional epoch, so a
        # re-drive-set in the same second reads as OLDER than the release that
        # preceded it and the seat silently stays vacant. (Measured in the
        # T-0937 walkthrough, step 7, before this was written.)
        #
        # Residual, stated rather than hidden: a re-set that is byte-identical
        # to the released one AND lands inside the same second is not a change
        # this can see, so the tombstone keeps holding. It fails OPEN — the seat
        # stays vacant and the re-drive mints an operator — which is the safe
        # direction, and one more `bsq pace drive` (a new second) revives it.
        if rec is not None and not rec_held and rec_sid == implicit["sid"]:
            if str(rec.get("drive_fingerprint") or "") == _drive_fingerprint(drive):
                rejected.append(
                    f"drive.set_by({implicit['sid']}): superseded by an "
                    f"explicit release at {rec.get('at_iso') or '?'}")
                implicit = None
        holder = implicit

    return {"holder": holder, "explicit": explicit, "implicit": implicit,
            "rejected": rejected}


def seat_holder(cfg: Any, slug: str) -> Optional[dict]:
    """The session holding the operator seat, or None when it is VACANT.

    ``{sid, kind, since, source, role, status}``. **Never raises**: the kill
    switch, an unreadable claim file and an unreadable pace config all resolve
    to None — i.e. to pre-T-0937 behaviour, where the re-drive mints an
    operator. Failing the other way (holding a seat nobody can read) would
    strand the backlog, which is the one outcome worse than the bug this fixes.
    """
    if not _enabled():
        return None
    try:
        return _resolve(cfg, slug)["holder"]
    except Exception:  # noqa: BLE001 — a broken seat read must not stop the operator
        log.exception("operator_seat: seat read failed for %s (treating as vacant)", slug)
        return None


def seat_status(cfg: Any, slug: str) -> dict:
    """The seat for a SURFACE to print: ``{ok, enabled, held, holder, explicit,
    implicit, rejected, error}``.

    Unlike :func:`seat_holder` this reports a fault as ``error`` instead of as
    an ordinary empty seat — an unreadable state that renders identically to a
    real "nobody is driving" is the silent-None failure the drive-mode design
    (D-0069) names as its most dangerous line.
    """
    if not _enabled():
        return {"ok": True, "enabled": False, "held": False, "holder": None,
                "explicit": None, "implicit": None, "rejected": [],
                "error": None}
    try:
        res = _resolve(cfg, slug)
    except Exception as exc:  # noqa: BLE001
        log.exception("operator_seat: status read failed for %s", slug)
        return {"ok": True, "enabled": True, "held": False, "holder": None,
                "explicit": None, "implicit": None, "rejected": [],
                "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "enabled": True, "held": res["holder"] is not None,
            "holder": res["holder"], "explicit": res["explicit"],
            "implicit": res["implicit"], "rejected": res["rejected"],
            "error": None}


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------

def claim(cfg: Any, slug: str, sid: str, *, source: str = "",
          force: bool = False) -> dict:
    """``sid`` takes the operator seat. Returns ``{ok, seat, replaced}``.

    Refuses — with ``ActionError``, because a claim that silently did nothing is
    worse than one that failed — when ``sid`` is not an eligible live root, when
    a live OPERATOR session already drives the board (the T-0472 singleton owns
    that case; a seat claim beside it would be the second dispatcher), or when
    another live root already holds the seat (``force=True`` takes it over).
    Idempotent for the current holder.
    """
    from bot_squad_worker import dispatch as _dispatch
    from bot_squad_worker.actions import ActionError

    if cfg.projects.get(slug) is None:
        raise ActionError(f"operator_seat.claim: unknown project slug {slug!r}")
    if not _enabled():
        raise ActionError(
            "the operator seat is disabled (BOT_SQUAD_OPERATOR_SEAT=0) — "
            "nothing would honour the claim")

    el = eligibility(cfg, slug, sid)
    if not el["ok"]:
        raise ActionError(f"operator_seat.claim: {el['reason']}")

    # T-0943 (second pass): EXCLUSIVITY — "is anyone already driving this
    # board", which a session holding operator among other roles satisfies.
    # `live_operator_sids` is the dedicated-operator singleton and answered a
    # different question here, so a root could claim the seat beside a live
    # talking-operator. The claimant is excluded: claiming the seat for the
    # board you are already driving is not a second dispatcher.
    live = _dispatch.operator_exclusivity_sids(cfg, slug, exclude_sid=sid)
    if live:
        raise ActionError(
            f"operator_seat.claim: operator {live[0]} is already driving this "
            f"board — exactly one driver per project (T-0472). Route through "
            f"it, or let it recycle first")

    prev = seat_holder(cfg, slug)
    if prev is not None and prev["sid"] != sid and not force:
        raise ActionError(
            f"operator_seat.claim: {prev['sid']} already holds the seat "
            f"(via {prev['kind']}) — pass force to take it over")

    now = time.time()
    _write_record(cfg, slug, {
        "sid": sid, "held": True, "at": now, "at_iso": _utc_now_iso(),
        "source": str(source or ""),
    })
    log.info("operator_seat[%s]: %s claimed the operator seat%s", slug, sid,
             f" (taking over from {prev['sid']})" if prev and prev["sid"] != sid else "")
    return {"ok": True, "seat": seat_holder(cfg, slug),
            "replaced": prev["sid"] if prev and prev["sid"] != sid else None}


def release(cfg: Any, slug: str, sid: str = "", *, force: bool = False) -> dict:
    """Give the seat up. Returns ``{ok, released, was}``.

    Writes a RELEASE tombstone rather than deleting the claim, because the
    implicit (drive-``set_by``) claim would otherwise re-assert itself the
    instant the explicit one was removed — the drive block still names the same
    live root. The tombstone is superseded by a later ``bsq pace drive`` or by a
    fresh :func:`claim`, so it holds the seat open without freezing it.

    ``released`` is False when the seat was already vacant (idempotent no-op).
    Refuses to release ANOTHER session's seat unless ``force``.
    """
    from bot_squad_worker.actions import ActionError

    if cfg.projects.get(slug) is None:
        raise ActionError(f"operator_seat.release: unknown project slug {slug!r}")

    prev = seat_holder(cfg, slug)
    if prev is None:
        return {"ok": True, "released": False, "was": None}
    if sid and prev["sid"] != sid and not force:
        raise ActionError(
            f"operator_seat.release: the seat is held by {prev['sid']}, not by "
            f"{sid} — pass force to release someone else's claim")

    # The drive stamp AS IT STANDS NOW — the tombstone's whole job is to say
    # "this particular drive statement no longer means the seat is held", so it
    # has to name which statement (see the identity rule in :func:`_resolve`).
    from bot_squad_worker import pace as _pace
    try:
        fp = _drive_fingerprint(_pace.read_drive(cfg, slug))
    except Exception:  # noqa: BLE001 — an unreadable drive block just means the
        fp = ""  # tombstone cannot suppress an implicit claim; that fails open

    _write_record(cfg, slug, {
        "sid": prev["sid"], "held": False, "at": time.time(),
        "at_iso": _utc_now_iso(), "released_by": str(sid or ""),
        "drive_fingerprint": fp,
    })
    log.info("operator_seat[%s]: %s released the operator seat", slug, prev["sid"])
    return {"ok": True, "released": True, "was": prev}
