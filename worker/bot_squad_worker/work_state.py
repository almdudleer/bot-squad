"""T-0942 — the project's ONE work-state doc, writable by any role-holder.

WHAT WAS BROKEN, MEASURED AT THE LINE THAT DID IT
-------------------------------------------------
:func:`assignment.role_artifact` routes a task-LESS session's compact
destination by role: ``operator`` → ``artifacts/operator-state.md``, and *every
other role* → ``artifacts/role-<role>-<window-base>.md``. On the live install
those two files sat side by side and their mtimes name the defect exactly::

    artifacts/operator-state.md                                 2026-07-31
    artifacts/role-user-conversation-S-…-user-conversation.md    2026-09-04

The user-conversation session that was HOLDING the operator seat (under the
no-separate-operator ruling) had been writing its handoff faithfully — into a
per-ROLE file. The seat's own state-doc got nothing for five weeks, and 38
operator sessions were minted in that window, each told the July document was
its only memory. Stakeholder, 2026-08-31, watching it happen:

  «мб operator state doc должен быть work state doc, и в него должна иметь
   право и юзер-сессия писать. Правда понадобится concurrency lock.»

So: ONE doc per project, keyed to the PROJECT and not to a role lineage, which
whichever session currently holds a project-level role writes, arbitrated by a
lock. That is this module.

THE LOCK IS NECESSARY BUT IT IS NOT THE PART THAT WAS BROKEN
------------------------------------------------------------
The failure that actually happened is **nobody writing at all** (38 of 38), not
two writers racing. Two consequences shape the API here:

* :func:`staleness` is a first-class READ result, not a frontmatter date a
  reader has to notice. A ``updated:`` line did not stop an operator booting on
  2026-09-06 from reading a 2026-07-31 body as current fact, because a date is
  not a warning. Callers that PRESENT this doc (the successor boot prompt, the
  CLI, the API) must change what they CLAIM when it is stale — see
  :func:`staleness_banner`.
* :func:`stale_holders` exists so a scheduler tick can nudge the live
  role-holder, rather than the doc's freshness depending on a session choosing
  to write it.

CONCURRENCY
-----------
:func:`write` holds an exclusive ``fcntl`` flock over ``work-state.md.lock``
across the WHOLE read→modify→write, and CAS-refuses on a stale ``base_rev``
instead of overwriting. The lock alone would serialize the two writers and
still lose the first one's content — the lock says "one at a time", the CAS
says "you edited an older copy, merge and retry". Both are needed; the flock is
kernel-released, so a writer that dies never wedges the doc.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The one work-state doc, per project. Replaces ``operator-state.md``.
WORK_STATE_ARTIFACT = "work-state.md"
#: The name it is migrated FROM. Kept resolvable for one release so a rollback
#: (or an old worker still running) does not strand the content.
LEGACY_OPERATOR_STATE_ARTIFACT = "operator-state.md"

_ARTIFACTS_SUBDIR = "artifacts"
_LOCK_SUFFIX = ".lock"
_VERSIONS_KEEP = 20

#: Roles whose forward-state IS project work-state, so their compact handoff
#: lands in this doc rather than a per-role file.
#:
#: The stakeholder named exactly these two — the operator and the user-session —
#: and the pairing is the whole point: «мб это как раз потому что жесткое
#: разделение на юзер-сессию и оператора». A task-less ``dev``/``qa``/
#: ``teamlead`` keeps its own per-assignment artifact: its forward-state is
#: about ITS assignment, and full-replacing the project's work-state with it
#: would be the same conflation in the other direction.
#:
#: This is the routing set. It is NOT the permission set — see
#: :func:`may_write`, which is deliberately wider because the verbatim grants
#: the right to write, not the obligation to be the only writer.
PROJECT_ROLES: tuple[str, ...] = ("operator", "user-conversation")

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


# --- env knobs --------------------------------------------------------------

def _int_env(name: str, default: int) -> int:
    try:
        v = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def stale_after_hours() -> int:
    """Age past which this doc is presented as HISTORY, not current state.

    24h by default: the doc's contract is "update it on every MAJOR change",
    so a day of silence on a project with live sessions already means the body
    describes a state nobody has confirmed. The measured failure was 5 weeks,
    which any sane threshold catches; the threshold matters for the nudge, not
    for that case."""
    return _int_env("BOT_SQUAD_WORK_STATE_STALE_HOURS", 24)


# --- paths ------------------------------------------------------------------

def artifacts_dir(data_dir: Path | str, slug: str) -> Path:
    return Path(data_dir) / slug / _ARTIFACTS_SUBDIR


def doc_path(data_dir: Path | str, slug: str) -> Path:
    return artifacts_dir(data_dir, slug) / WORK_STATE_ARTIFACT


def legacy_path(data_dir: Path | str, slug: str) -> Path:
    return artifacts_dir(data_dir, slug) / LEGACY_OPERATOR_STATE_ARTIFACT


def lock_path(data_dir: Path | str, slug: str) -> Path:
    p = doc_path(data_dir, slug)
    return p.parent / (p.name + _LOCK_SUFFIX)


def versions_dir(data_dir: Path | str, slug: str) -> Path:
    return artifacts_dir(data_dir, slug) / ".versions" / Path(WORK_STATE_ARTIFACT).stem


# --- frontmatter ------------------------------------------------------------

def parse_header(text: str) -> dict[str, str]:
    """The doc's frontmatter as a flat ``{key: value}`` map (empty if none)."""
    m = _FM_RE.match(text or "")
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def body_of(text: str) -> str:
    """The doc without its frontmatter."""
    m = _FM_RE.match(text or "")
    return (text or "")[m.end():] if m else (text or "")


def current_rev(text: str) -> int:
    """The doc's revision counter. A doc with no ``rev:`` reads as 0.

    0 is the right answer for the pre-migration ``operator-state.md`` too: the
    first write through this module bumps it to 1, and a caller that passes
    ``base_rev=0`` is asserting "I read a doc that had no rev", which is true."""
    try:
        return int(parse_header(text).get("rev", "0"))
    except (TypeError, ValueError):
        return 0


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts).strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def compose(content: str, *, rev: int, sid: str | None, role: str | None,
            ts: str | None = None) -> str:
    """Frontmatter + body, in the shape :func:`parse_header` reads back.

    ``updated_by``/``roles``/``rev`` are the provenance the design calls for:
    the doc says WHO last wrote it and from which role, so a reader can tell a
    seat-holder's write from a drive-by one without leaving the file."""
    ts = ts or _utcnow_iso()
    lines = [
        "---",
        "assignment: work-state",
        "kind: work-state",
    ]
    if sid:
        lines.append(f"updated_by: {sid}")
        # Kept for one release: `compose_result_body` wrote `sid:` and the API +
        # older readers look for it. Dropping it would blank the provenance
        # column on a surface this ticket is not touching.
        lines.append(f"sid: {sid}")
    if role:
        lines.append(f"role: {role}")
    lines.append(f"rev: {rev}")
    lines.append(f"updated: {ts}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (content or "").rstrip("\n") + "\n"


# --- staleness --------------------------------------------------------------

def moved_sessions_bar() -> int:
    """Sessions minted since the doc was written that make it stale ahead of the
    clock — a slow counter, one tick per real project event.

    CALIBRATED AGAINST THE LIVE FLEET, not chosen. The first cut also counted
    backlog mds touched since, at a bar of 10, and on this install that verdict
    came back STALE for a doc written ELEVEN MINUTES earlier: a ticket md is
    rewritten by every note, so it measures chatter, not movement. A freshness
    warning that fires on an 11-minute-old doc is the drift-nagger failure with
    a new name — it teaches its reader to skip the banner, and then the banner
    is not there on the day the doc is five weeks old. Tickets-touched is still
    REPORTED (it is useful context in the banner); it no longer votes."""
    return _int_env("BOT_SQUAD_WORK_STATE_MOVED_SESSIONS", 8)


def movement_floor_hours() -> int:
    """Movement cannot make a doc stale before this age.

    The floor is what keeps the relative check honest on a fast fleet: sessions
    recycle roughly hourly here, so "8 sessions minted" alone can be twenty
    minutes' worth. Age-OR-movement without a floor is age-OR-noise."""
    return _int_env("BOT_SQUAD_WORK_STATE_MOVEMENT_FLOOR_HOURS", 2)


def project_movement(data_dir: Path | str, slug: str,
                     since: float | None) -> dict[str, int]:
    """How far the project has moved since ``since`` (epoch seconds).

    Tickets are counted by mtime (a backlog md is rewritten when the ticket
    actually moves). Sessions are counted by their md's ``started_at``, NOT by
    mtime: a session md is rewritten on every status change, so an mtime scan
    counts every live pane as "new" within seconds of a write and the doc would
    read stale the moment it was written — a freshness signal that fires
    immediately is the drift-nagger failure with a new name.

    ``sessions/*.md`` is deleted on reap, so this UNDER-reports for a long-dead
    window. That is the harmless direction: by the time the mds are gone the
    age check has fired on its own, and movement only ever adds staleness.

    Returns zeros (never raises) when ``since`` is unknown or a dir is absent."""
    out = {"sessions_since": 0, "tickets_since": 0}
    if since is None:
        return out
    root = Path(data_dir) / slug
    try:
        n = 0
        for f in (root / "backlog").glob("*.md"):
            with contextlib.suppress(OSError):
                if f.stat().st_mtime > since:
                    n += 1
        out["tickets_since"] = n
    except OSError:
        log.exception("work_state.project_movement: cannot scan backlog of %s", slug)
    try:
        n = 0
        for f in (root / "sessions").glob("*.md"):
            with contextlib.suppress(OSError):
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    head = fh.read(1024)
                m = _STARTED_AT_RE.search(head)
                if m and (_parse_started_at(m.group(1)) or 0) > since:
                    n += 1
        out["sessions_since"] = n
    except OSError:
        log.exception("work_state.project_movement: cannot scan sessions of %s", slug)
    return out


_STARTED_AT_RE = re.compile(r"^started_at:\s*(.+)$", re.MULTILINE)


def _parse_started_at(value: str) -> float | None:
    """``started_at`` is written in two shapes across the fleet — the ISO-Z form
    the worker stamps and python's ``isoformat()`` with a ``+00:00`` offset —
    so parse both rather than picking one and silently counting zero."""
    v = (value or "").strip()
    if not v or v == "~":
        return None
    ts = _parse_iso(v)
    if ts is not None:
        return ts
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None



def staleness(text: str, path: Path | str | None = None,
              now: float | None = None,
              movement: dict[str, int] | None = None) -> dict[str, Any]:
    """How old this doc is, and whether it may still be presented as current.

    ``updated`` (frontmatter) is authoritative and mtime is the fallback: a
    ``cp``/rsync of the data dir rewrites every mtime, and a doc that looks
    fresh because it was COPIED is exactly the lie this returns a verdict to
    prevent.

    Returns ``{exists, updated, updated_by, rev, age_seconds, age_human,
    stale, stale_after_hours}``. ``age_seconds`` is ``None`` when the age
    cannot be established at all — which is treated as STALE, not as fresh: an
    unknown age is the state the July doc was in.

    ``movement`` (from :func:`project_movement`) makes the verdict RELATIVE as
    well as absolute. A fixed hour count is the wrong instrument on its own: it
    would have caught the five-week doc, which any threshold catches, and it
    says nothing about the interesting case — a doc hours old against a board
    that moved fifty times underneath it. So the doc is ALSO stale when the
    project has demonstrably moved since it was written, and the counts go into
    the banner because "38 sessions have been minted since this was written" is
    the sentence that stops a reader; an hour count is not."""
    now = time.time() if now is None else now
    hdr = parse_header(text or "")
    exists = bool((text or "").strip())
    updated = hdr.get("updated") or None
    at = _parse_iso(updated)
    if at is None and path is not None:
        with contextlib.suppress(OSError):
            st = Path(path).stat()
            at = st.st_mtime
    age = None if at is None else max(0.0, now - at)
    limit = stale_after_hours()
    by_age = True if age is None else age > limit * 3600
    mv = movement or {}
    old_enough = age is not None and age > movement_floor_hours() * 3600
    by_move = old_enough and mv.get("sessions_since", 0) >= moved_sessions_bar()
    return {
        "exists": exists,
        "updated": updated,
        "updated_by": hdr.get("updated_by") or hdr.get("sid") or None,
        "role": hdr.get("role") or None,
        "rev": current_rev(text or ""),
        "age_seconds": None if age is None else int(age),
        "age_human": age_human(age),
        "stale": bool(exists and (by_age or by_move)),
        "stale_reason": ("age" if by_age else "movement") if (by_age or by_move) else None,
        "stale_after_hours": limit,
        "sessions_since": mv.get("sessions_since"),
        "tickets_since": mv.get("tickets_since"),
    }


def age_human(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "unknown age"
    m = int(age_seconds // 60)
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 48:
        return f"{h}h"
    return f"{h // 24}d"


def staleness_banner(st: dict[str, Any], *, rewrite_cmd: str | None = None) -> str:
    """The warning a READER must be shown, or ``""`` when the doc is current.

    This exists because a frontmatter date is not a warning. On 2026-09-06 an
    operator booted from a doc whose header said ``updated: 2026-07-30`` and
    read the body as current fact — the date was right there and it changed
    nothing, because the brief around it said "this is your ONLY memory,
    continue from there". A reader has to be TOLD the two sentences disagree."""
    if not st.get("stale"):
        return ""
    if not st.get("exists"):
        return ("⚠️ THE WORK-STATE DOC IS EMPTY. There is no recorded project "
                "state to continue from — establish it by measuring, and write "
                "the doc when you have.")
    # `rewrite_cmd` exists because this banner is also shown for a per-ROLE
    # artifact (a task-less dev/TL/qa boot, and every crash recovery), where
    # `bsq work-state write` is the wrong verb — that session's artifact is its
    # own file and it writes it with `bsq compact-save`. A warning that ends in
    # a command the reader cannot use teaches them to stop reading the warning.
    who = st.get("updated_by") or "an unrecorded session"
    when = st.get("updated") or "an unrecorded time"
    # The "each was told" clause belongs to SESSIONS only — a ticket was not told
    # anything, and hanging it off a joined list produced "1 ticket has moved
    # since — each was told this doc is the project's state."
    moved = []
    n = st.get("sessions_since") or 0
    if n:
        moved.append(f"{n} session{'s have' if n != 1 else ' has'} been minted "
                     f"since, each told this doc is the project's state")
    n = st.get("tickets_since") or 0
    if n:
        moved.append(f"{n} ticket{'s have' if n != 1 else ' has'} moved since")
    # "; " not " and ": the sessions clause already contains a comma clause, and
    # joining with "and" ran it into the ticket count as one long sentence.
    moved_s = (" " + "; ".join(moved) + ".") if moved else ""
    return (
        f"⚠️ THIS DOC IS STALE — last written {st['age_human']} ago ({when}) by "
        f"{who}"
        + (f", past the {st['stale_after_hours']}h freshness bar."
           if st.get("stale_reason") == "age" else ".")
        + f"{moved_s} READ IT AS "
        "HISTORY, NOT AS CURRENT FACT. Its priorities, its 'what is happening "
        "now' and its standing directives may all name work that has since "
        "shipped, been superseded or been abandoned. RE-MEASURE before you act "
        "on any of it, and rewrite the doc from what you measure "
        f"({rewrite_cmd or '`bsq work-state write --file <f> --base-rev <rev>`'})."
    )


# --- read -------------------------------------------------------------------

def read(data_dir: Path | str, slug: str) -> dict[str, Any]:
    """Read the work-state doc + its staleness verdict.

    Falls back to the legacy ``operator-state.md`` when the new name does not
    exist yet, so a read never goes blind between the code landing and the
    migration running."""
    p = doc_path(data_dir, slug)
    src = p
    text = ""
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        lp = legacy_path(data_dir, slug)
        with contextlib.suppress(FileNotFoundError):
            text = lp.read_text(encoding="utf-8")
            src = lp
    except OSError:
        log.exception("work_state.read: unreadable %s", p)
    st = staleness(text, src)
    # The movement probe stats every backlog md and reads the head of every
    # session md — cheap, but not free, and `read` is on the session-start path.
    # It can only CHANGE the verdict once the doc is past the movement floor, so
    # a doc younger than that (the healthy case) skips it entirely.
    age = st.get("age_seconds")
    if age is None or age > movement_floor_hours() * 3600:
        st = staleness(text, src, movement=project_movement(
            data_dir, slug, _parse_iso(st.get("updated"))))
    return {
        "path": str(p),
        "read_from": str(src),
        "content": text,
        "rev": st["rev"],
        "staleness": st,
        "banner": staleness_banner(st),
    }


# --- write ------------------------------------------------------------------

class WorkStateConflict(Exception):
    """A CAS refusal: the caller edited a revision that is no longer current."""

    def __init__(self, expected: int, actual: int, content: str):
        super().__init__(
            f"work-state doc moved on: you based your edit on rev {expected}, "
            f"the doc is now at rev {actual}. Re-read it, merge your changes "
            f"into the current text, and write again with --base-rev {actual}.")
        self.expected = expected
        self.actual = actual
        self.content = content


@contextlib.contextmanager
def _flock(data_dir: Path | str, slug: str):
    lp = lock_path(data_dir, slug)
    lp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _snapshot(data_dir: Path | str, slug: str, prev_text: str, rev: int) -> Path | None:
    """Keep the WHOLE previous file, so a bad full-replace is recoverable.

    Same reasoning as the ticket snapshots (T-0891): ``data/`` is outside git,
    and a full-replace with no undo anywhere is how a month of state goes in
    one command. Restoring is ``cp`` — no parser in the recovery path."""
    if not (prev_text or "").strip():
        return None
    vdir = versions_dir(data_dir, slug)
    try:
        vdir.mkdir(parents=True, exist_ok=True)
        stamp = _utcnow_iso().replace("-", "").replace(":", "")
        seq = 1
        dest = vdir / f"{stamp}-{seq:04d}-rev{rev:05d}.md"
        while dest.exists():
            seq += 1
            dest = vdir / f"{stamp}-{seq:04d}-rev{rev:05d}.md"
        dest.write_text(prev_text, encoding="utf-8")
        for old in sorted(vdir.glob("*.md"))[:-_VERSIONS_KEEP]:
            with contextlib.suppress(OSError):
                old.unlink()
        return dest
    except OSError:
        log.exception("work_state: snapshot failed for %s", slug)
        return None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write(data_dir: Path | str, slug: str, content: str, *,
          sid: str | None = None, role: str | None = None,
          base_rev: int | None = None,
          allow_blind: bool = False) -> dict[str, Any]:
    """Full-replace the work-state doc under the lock, with CAS on ``base_rev``.

    ``base_rev=None`` means "I did not read first" and is refused over a
    non-empty doc: a blind full-replace of someone else's live state is the
    lost write the lock exists to stop, and a refusal the caller sees beats a
    merge nobody notices was needed.

    ``allow_blind`` is the ONE exception, and it exists for the compact seam.
    A session being finalized has no earlier read to CAS against, and its
    context is cleared the moment it answers — a refusal there does not protect
    the doc, it drops the forward-state on the floor at the only moment it can
    still be written, which is the exact defect class this ticket is about. So
    the compact write is accepted, the previous file is snapshotted (recovery is
    a ``cp``), and the result flags ``blind`` with the sid it overwrote so the
    overwrite is visible rather than silent.

    Raises :class:`WorkStateConflict` on a stale ``base_rev``; the exception
    carries the current text so the caller can merge without a second read.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("work_state.write: empty content")
    data_dir = Path(data_dir)
    p = doc_path(data_dir, slug)
    with _flock(data_dir, slug):
        prev = ""
        try:
            prev = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Pre-migration: the legacy doc IS the current state, and its rev is
            # what a reader saw. Adopt it inside the lock so the first write
            # after the rename cannot silently discard a month of content.
            with contextlib.suppress(FileNotFoundError, OSError):
                prev = legacy_path(data_dir, slug).read_text(encoding="utf-8")
        except OSError:
            log.exception("work_state.write: unreadable %s", p)
            raise
        cur = current_rev(prev)
        prev_sid = parse_header(prev).get("updated_by") or parse_header(prev).get("sid")
        blind_over = None
        if base_rev is None:
            if prev.strip() and not allow_blind:
                raise WorkStateConflict(0, cur, prev)
            if prev.strip() and prev_sid and prev_sid != sid:
                blind_over = prev_sid
                log.warning(
                    "work_state: %s blind-overwrote rev %d written by %s "
                    "(compact seam has no base_rev; previous file snapshotted)",
                    sid, cur, prev_sid)
        elif int(base_rev) != cur:
            raise WorkStateConflict(int(base_rev), cur, prev)
        snap = _snapshot(data_dir, slug, prev, cur)
        new_rev = cur + 1
        text = compose(content, rev=new_rev, sid=sid, role=role)
        _atomic_write(p, text)
        # Write-through to the legacy name for one release (design objection 9):
        # an old worker or a rolled-back install reads `operator-state.md`, and
        # leaving it frozen at July is the same defect with a new filename.
        with contextlib.suppress(OSError):
            _atomic_write(legacy_path(data_dir, slug), text)
    return {
        "ok": True,
        "path": str(p),
        "rev": new_rev,
        "base_rev": cur,
        "bytes_written": len(text.encode("utf-8")),
        "snapshot": str(snap) if snap else None,
        "blind": bool(blind_over),
        "blind_over": blind_over,
    }


# --- migration --------------------------------------------------------------

def migrate(data_dir: Path | str, slug: str) -> dict[str, Any]:
    """Adopt an existing ``operator-state.md`` as ``work-state.md``, once.

    Idempotent and non-destructive: it only acts when the new name is absent
    and the old one has content, and it never deletes the old file (the
    write-through in :func:`write` keeps both in step for this release)."""
    data_dir = Path(data_dir)
    new = doc_path(data_dir, slug)
    old = legacy_path(data_dir, slug)
    if new.exists():
        return {"ok": True, "migrated": False, "reason": "work-state.md exists"}
    try:
        text = old.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {"ok": True, "migrated": False, "reason": "no operator-state.md"}
    if not text.strip():
        return {"ok": True, "migrated": False, "reason": "operator-state.md empty"}
    with _flock(data_dir, slug):
        if new.exists():
            return {"ok": True, "migrated": False, "reason": "work-state.md exists"}
        _atomic_write(new, text)
    log.info("work_state: migrated %s operator-state.md -> work-state.md (%d bytes)",
             slug, len(text.encode("utf-8")))
    return {"ok": True, "migrated": True, "path": str(new),
            "from": str(old), "bytes": len(text.encode("utf-8"))}


# --- who may write ----------------------------------------------------------

def may_write(meta: dict | None) -> bool:
    """Whether a session may write the work-state doc.

    ANY live session of the project may — which is the explicit widening the
    verbatim asks for («в него должна иметь право и юзер-сессия писать») and
    which the v2 design left as a decision rather than a fallback: "if T-0942's
    intent is truly 'every role writes', relax the gate then, explicitly".

    So this is not a role check at all — it is a liveness check, and the gate
    that matters is CAS + snapshots, not an allow-list. The one thing refused
    is a caller with no session md: an unknown sid leaves no provenance in
    ``updated_by``, and an unattributable full-replace of the project's state
    is worse than a refused one."""
    return meta is not None


# --- freshness sweep --------------------------------------------------------

def nudge_text(slug: str, st: dict[str, Any]) -> str:
    """The reminder injected into a stale doc's role-holder."""
    who = st.get("updated_by") or "nobody on record"
    return (
        f"[system] The {slug} WORK-STATE DOC is {st['age_human']} stale — last "
        f"written by {who} at {st.get('updated') or 'an unrecorded time'}. You "
        "hold a project-level role, so it is yours to refresh, and the next "
        "session to boot is told this doc is its memory. Read it with `bsq "
        "work-state`, then REPLACE it with what is true now: `bsq work-state "
        "write --file <f> --base-rev <rev>`. Priorities · What's happening now · "
        "Delivered · Next · Tracked issues — forward state, not a log. "
        "(T-0942: 38 consecutive operator sessions skipped this and the 39th "
        "booted on a five-week-old document.)"
    )


# --- freshness sweep (the TOP-UP, explicitly not the load-bearing half) ------
#
# WHY THIS IS RANKED LAST AND KEPT SMALL. The non-volitional half of T-0942 is
# the routing (`assignment.role_artifact` sends operator AND user-conversation
# compacts here, and a compact happens whether or not anyone chooses) plus the
# read-time claim change (`autocompact.boot_prompt_from_artifact` stops calling
# a stale doc "your ONLY memory"). Neither nags anyone, so neither has an off
# switch — which is the test this sweep has to survive and the drift-checker
# did not.
#
# THE PRECEDENT, on this fleet, this week: drift_check is exactly "ask
# repeatedly on a cooldown". On 2026-09-06 it nagged 2-minute-old devs claiming
# 8581 minutes of silence, delivered into busy panes where the text parked
# unsubmitted, re-fired every 2 minutes, and the fleet's answer was to switch it
# off on all eight lanes. Repeated asking is not merely weaker than a mechanism;
# its failure mode is that operators disable it, and then it protects nothing.
#
# So this sweep is constrained against each of those three causes:
#   * ONE clock — the doc's own frontmatter. No per-session arithmetic to get
#     wrong, and it is the same number the reader is shown.
#   * ONE nudge per stale EPISODE per session, not one per cooldown. The
#     episode is keyed by the doc's `updated` stamp: a session is told once,
#     and told again only after somebody has actually written the doc.
#   * Never into a running pane — an injection into a busy composer parks
#     unsubmitted and is pure noise.
# If it still gets switched off, the routing and the read-time claim stand
# without it.

def freshness_sweep(cfg: Any, slug: str) -> dict[str, Any]:
    """Nudge the live holder of a project-level role when this doc is stale."""
    from bot_squad_worker import sessions as S

    if not _int_env("BOT_SQUAD_WORK_STATE_NUDGE", 1):
        return {"ok": True, "disabled": True, "nudged": []}
    try:
        from bot_squad_worker import automation as _automation
        if not _automation.gate(cfg, slug, "work_state_freshness"):
            return {"ok": True, "paused": True, "nudged": []}
    except Exception:
        log.exception("work_state.freshness_sweep: automation gate failed")
        return {"ok": False, "nudged": []}

    res = read(cfg.data_dir, slug)
    st = res["staleness"]
    if not st["stale"]:
        return {"ok": True, "stale": False, "nudged": []}
    # The episode key. `updated` alone is enough and is deliberately NOT the
    # rev: a rev bump without a new timestamp cannot happen, and keying off the
    # timestamp means the marker is comparable to what the session was shown.
    episode = str(st.get("updated") or "unknown")

    try:
        rows = S.list_sessions(cfg, slug)
    except Exception:
        log.exception("work_state.freshness_sweep: list_sessions failed for %s", slug)
        return {"ok": False, "nudged": []}
    user = S._get_current_user()
    panes = {S.compute_sid(user, p.window, p.pane_id): p for p in S.list_panes()}

    text = nudge_text(slug, st)
    nudged: list[dict] = []
    for row in rows:
        sid = row.get("sid")
        if not sid or row.get("status") != "active":
            continue
        if (row.get("role") or "") not in PROJECT_ROLES:
            continue
        # A running pane is mid-turn: the injection parks in the composer
        # unsubmitted, which is how drift_check turned into noise.
        if row.get("activity") != "idle":
            continue
        pane = panes.get(sid)
        if pane is None:
            continue
        md_path = S._session_file(cfg.data_dir, slug, sid)
        meta = S._read_session_metadata(md_path)
        if meta is None:
            continue
        if str(meta.get("work_state_nudged_for") or "") == episode:
            continue
        try:
            S._deliver_prompt(pane.pane_id, text, data_dir=cfg.data_dir, sid=sid)
        except Exception:
            log.exception("work_state.freshness_sweep: inject failed for %s", sid)
            continue
        nudged.append({"sid": sid, "role": row.get("role"), "episode": episode})
        meta["work_state_nudged_for"] = episode
        try:
            S._write_session_metadata(md_path, meta, atomic=True)
        except OSError:
            log.exception("work_state.freshness_sweep: could not persist marker for %s", sid)
    if nudged:
        log.info("work_state.freshness_sweep: %s nudged %s", slug, nudged)
    return {"ok": True, "stale": True, "episode": episode, "nudged": nudged}
