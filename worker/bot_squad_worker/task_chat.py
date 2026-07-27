"""Backlog visibility in the TG chat (T-0589, direction #1b of T-0587).

Two surfaces, one frontmatter scan:

- ``compose_digest`` — the on-demand SHORT backlog digest ("что по задачам"):
  one status-counts line + up to :data:`DIGEST_HEADLINES` P1/P2 headlines with
  T-ids, in-flight work first. Exposed as the ``task_digest`` worker action /
  ``bsq task digest``; the ATTENDANT (user-conversation session) triggers it
  from the existing conversation flow and pastes the text into its thread
  reply — the digest is conversational CONTENT relayed verbatim (T-0569 relay,
  never page-slimmed), so the shortness is enforced HERE by construction
  (conscious single-message cap per the T-0610 slim rule, not a page cut).

- ``lifecycle_tick`` — one-line lifecycle notifications for STAKEHOLDER-
  AUTHORED tasks (any ``stakeholder:YYYY-MM-DD`` provenance token, exactly what
  attendants stamp on dictated tasks): a 60s sweep diffs backlog frontmatter
  against a sidecar (``data/_worker/task_lifecycle/<slug>.json``) and posts one
  line per transition INTO ``in_progress`` / ``totest`` / ``closed``. Sweeping
  the mds catches EVERY mutation surface (``bsq ticket update``, the web API,
  direct edits) without per-callsite hooks. Digest-style dedupe: all of a
  sweep's transitions batch into ONE message, and an unchanged status never
  re-fires. Quiet-hours aware: sends ride ``urgent=False`` through the
  ``_send_stakeholder_dm`` SSOT — a quiet-hours drop (``sent: False``) leaves
  the sidecar UNSTAMPED, so the notification retries each tick and lands on
  the first post-quiet one (deferred, never lost). First sight of a task only
  baselines the sidecar (no send) — deploying this must not blast the history
  of every existing stakeholder task into the chat.

Kill switch: ``BOT_SQUAD_TASK_NOTIFY=0`` disables the lifecycle sweep (the
digest action stays available — it only speaks when asked).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: headline cap — the digest must stay one comfortable TG message.
DIGEST_HEADLINES = 6
#: headline title cut, so six lines can't balloon past digest size.
DIGEST_TITLE_MAX = 60

#: canonical statuses (T-0479), in the order the digest counts/ranks them:
#: in-flight first, then queued, then not-started.
_STATUS_ORDER = ("in_progress", "reopened", "totest", "open", "planned")

#: transitions the stakeholder is told about (T-0589 verbatim scope:
#: "open->in_progress, ->totest, ->closed"). Anything else (planned->open,
#: closed->reopened, …) updates the sidecar silently, so the NEXT interesting
#: transition still fires exactly once.
NOTIFY_STATUSES = frozenset({"in_progress", "totest", "closed"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Shared backlog frontmatter scan
# ---------------------------------------------------------------------------


def _scan_backlog(cfg: Any, slug: str) -> list[dict]:
    """Light frontmatter rows for every task md in ``data/<slug>/backlog``.

    Top-level ``*.md`` only — the ``_gc/`` archive subdir is naturally
    excluded. Per-file errors are skipped (one corrupt md must break neither
    the digest nor the sweep)."""
    from bot_squad_worker import frontmatter as fm

    backlog = Path(cfg.data_dir) / slug / "backlog"
    rows: list[dict] = []
    if not backlog.is_dir():
        return rows
    for p in sorted(backlog.glob("*.md")):
        try:
            parsed = fm.parse_or_none(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not parsed:
            continue
        meta = parsed[0]
        task_id = str(meta.get("id") or "").strip()
        if not task_id:
            continue
        rows.append({
            "id": task_id,
            "title": str(meta.get("title") or "").strip(),
            "status": str(meta.get("status") or "").strip() or "planned",
            "priority": str(meta.get("priority") or "").strip().upper(),
            "provenance": str(meta.get("provenance") or "").strip(),
        })
    return rows


def _is_stakeholder_authored(provenance: str) -> bool:
    """True when ANY provenance token is a ``stakeholder:YYYY-MM-DD`` cite —
    the exact form attendants stamp on dictated tasks (user-conversation role
    doc §2; the grammar has no user-id form, T-0519)."""
    return any(
        tok.strip().startswith("stakeholder:")
        for tok in (provenance or "").split(",")
    )


def _cut_title(title: str, cap: int = DIGEST_TITLE_MAX) -> str:
    t = (title or "").strip()
    return t if len(t) <= cap else t[: cap - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# On-demand digest
# ---------------------------------------------------------------------------


def compose_digest(cfg: Any, slug: str) -> dict:
    """The short backlog digest: ``{text, counts}``.

    Counts line: every non-closed status with a non-zero count (canonical
    order) + the closed total. Headlines: P1/P2 tasks, in-flight statuses
    first, P1 before P2, at most :data:`DIGEST_HEADLINES` lines with an
    explicit ``+N ещё`` tail — never silently truncated coverage."""
    rows = _scan_backlog(cfg, slug)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    parts = [f"{counts[s]} {s}" for s in _STATUS_ORDER if counts.get(s)]
    for s in sorted(counts):  # non-canonical statuses still show up
        if s not in _STATUS_ORDER and s != "closed":
            parts.append(f"{counts[s]} {s}")
    head = f"Бэклог {slug}: " + (" · ".join(parts) if parts else "пусто")
    if counts.get("closed"):
        head += f" ({counts['closed']} closed)"

    def _rank(r: dict) -> tuple:
        status_rank = (
            _STATUS_ORDER.index(r["status"])
            if r["status"] in _STATUS_ORDER else len(_STATUS_ORDER)
        )
        return (status_rank, r["priority"], r["id"])

    headliners = sorted(
        (r for r in rows
         if r["priority"] in ("P1", "P2") and r["status"] != "closed"),
        key=_rank,
    )
    lines = [
        f"• {r['id']} {r['priority']} {r['status']} — {_cut_title(r['title'])}"
        for r in headliners[:DIGEST_HEADLINES]
    ]
    overflow = len(headliners) - DIGEST_HEADLINES
    if overflow > 0:
        lines.append(f"+{overflow} ещё P1/P2 — весь список в UI или спроси")

    return {"text": "\n".join([head, *lines]), "counts": counts}


# ---------------------------------------------------------------------------
# Lifecycle notifications (scheduler sweep)
# ---------------------------------------------------------------------------


def _sidecar_path(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / "_worker" / "task_lifecycle" / f"{slug}.json"


def _read_sidecar(cfg: Any, slug: str) -> dict[str, str]:
    """``{task_id: last_announced_status}``; missing/corrupt file = empty
    (worst case tasks re-baseline silently — never a crash, never a blast)."""
    try:
        raw = json.loads(_sidecar_path(cfg, slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, dict):
        return {}
    return {str(k): str(v) for k, v in tasks.items()}


def _write_sidecar(cfg: Any, slug: str, tasks: dict[str, str]) -> None:
    p = _sidecar_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"tasks": tasks, "updated_at": _now_iso()}, indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def _notify(cfg: Any, slug: str, text: str) -> bool:
    """Deliver one lifecycle message through the page SSOT. ``urgent=False``
    ON PURPOSE: a status change is exactly the non-urgent class the quiet-hours
    gate exists for. Returns True only when actually delivered — a quiet-hours
    drop comes back ``{ok: True, sent: False}`` and must NOT stamp the sidecar
    (the _monitor_notify retry idiom, inverted for non-urgent)."""
    try:
        from bot_squad_worker.actions import _send_stakeholder_dm

        project = cfg.projects.get(slug)
        chat_id = getattr(project, "tg_chat", "") if project else ""
        res = _send_stakeholder_dm(
            cfg, message=text, urgent=False, tg_chat_id=chat_id,
            # T-0755: `_append_thread` below already records this exact text in
            # the very thread the outbound log would mirror it into, with a
            # better author (`system:task-lifecycle`, which names WHAT spoke
            # rather than the transport's display label). Two near-identical
            # lines per notice would make the transcript harder to read, which
            # is the thing this is all for.
            record_outbound=False,
        )
        return bool(res.get("ok")) and bool(res.get("sent"))
    except Exception:  # noqa: BLE001 — a channel outage never kills the sweep
        log.exception("task_chat: lifecycle notify failed [%s]", slug)
        return False


def _thread_gid(cfg: Any, slug: str) -> str:
    """The global-user id whose TG DM IS this project's chat (tg_chat ==
    tg_user_id) — the stakeholder thread the delivered line should also be
    recorded in. Read-only peek at the mothership users store (the worker
    reads ``_mothership``, never writes it); empty when unresolvable."""
    project = cfg.projects.get(slug)
    chat = str(getattr(project, "tg_chat", "") or "") if project else ""
    if not chat:
        return ""
    try:
        raw = json.loads(
            (Path(cfg.data_dir) / "_mothership" / "users.json")
            .read_text(encoding="utf-8"))
        users = raw.get("users") or []
    except (OSError, ValueError):
        return ""
    for u in users:
        if isinstance(u, dict) and str(u.get("tg_user_id") or "") == chat:
            return str(u.get("id") or "")
    return ""


def _append_thread(cfg: Any, slug: str, text: str) -> bool:
    """Best-effort durable record of a DELIVERED notification in the
    stakeholder's conversation thread, via the worker→API token path (the
    store is API-single-writer — same route as tg_listener's appends). Author
    ``system:task-lifecycle`` deliberately does NOT match the ``session:*``
    relay prefix: the TG send already happened, the append is the attendant's
    memory, not a second delivery."""
    from bot_squad_worker import tg_listener as _tgl

    gid = _thread_gid(cfg, slug)
    base = _tgl._api_base_url()
    token = _tgl._worker_api_token()
    if not gid or not base or not token:
        return False
    import httpx

    try:
        r = httpx.post(
            f"{base}/api/m/worker/conversations/{slug}/{gid}/messages",
            json={"author": "system:task-lifecycle", "text": text,
                  "timestamp": _now_iso(),
                  # T-0755: the TG send already happened above, so this is a
                  # record of a DELIVERED message, not one to deliver.
                  "delivered": True},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return False
    return True


def lifecycle_tick_one(cfg: Any, slug: str) -> dict:
    """One sweep for one project. Returns a small audit dict."""
    rows = [r for r in _scan_backlog(cfg, slug)
            if _is_stakeholder_authored(r["provenance"])]
    state = _read_sidecar(cfg, slug)
    new_state = dict(state)
    changes: list[dict] = []

    seen: set[str] = set()
    for r in rows:
        seen.add(r["id"])
        prev = state.get(r["id"])
        if prev is None:
            # First sight — baseline silently (no history blast on deploy).
            new_state[r["id"]] = r["status"]
        elif prev != r["status"]:
            if r["status"] in NOTIFY_STATUSES:
                changes.append(r)  # sidecar stamped only after delivery
            else:
                new_state[r["id"]] = r["status"]
    # Vanished (gc-archived / renamed) tasks drop out of the sidecar quietly.
    for gone in set(new_state) - seen:
        del new_state[gone]

    delivered = False
    if changes:
        text = "\n".join(
            f"📋 {r['id']} → {r['status']} — {_cut_title(r['title'])}"
            for r in changes
        )
        delivered = _notify(cfg, slug, text)
        if delivered:
            for r in changes:
                new_state[r["id"]] = r["status"]
            _append_thread(cfg, slug, text)

    if new_state != state:
        _write_sidecar(cfg, slug, new_state)
    return {"ok": True, "slug": slug, "transitions": len(changes),
            "delivered": delivered}


def lifecycle_tick(cfg: Any) -> dict:
    """All-projects sweep, scheduler entrypoint. Kill switch:
    ``BOT_SQUAD_TASK_NOTIFY=0``."""
    if os.environ.get("BOT_SQUAD_TASK_NOTIFY", "").strip() == "0":
        return {"ok": True, "disabled": True}
    out: dict[str, Any] = {"ok": True, "projects": {}}
    for slug in cfg.projects:
        try:
            out["projects"][slug] = lifecycle_tick_one(cfg, slug)
        except Exception:  # noqa: BLE001 — one bad project never kills the sweep
            log.exception("task_chat: lifecycle tick failed for %s", slug)
    return out
