"""What we actually SENT — the outbound half of every conversation (T-0755).

Why this exists
---------------
``data/_mothership/conversations/<slug>/<gid>.jsonl`` looks like a transcript
and reads like one. On 2026-07-27 an operator read the window 04:47-05:10Z,
saw six stakeholder messages and no other author, and concluded — with false
confidence, in about ninety seconds — that he had gone unanswered for nearly
three hours. He had not: ``tg_reply_map.json`` records four sends from the
attending session at 04:58:34, 04:59:45, 05:01:02 and 05:02:16Z, tightly
interleaved with his messages. The map pins ``message_id -> sid`` and drops the
body on the floor, so the only recoverable fact was that we sent *something*.

A CORRECTION to the reported mechanism, measured on the live install before
building anything: the store is NOT structurally inbound-only. It holds 206
``session:``-authored records in watchrobot's thread and 27 in bot-squad's —
the API append endpoint has always accepted a session writeback. What happened
is narrower and worse: that path STOPPED being used. The attendant's last
append is 2026-07-18T12:57:26Z; every send since then (the whole topic-routing
era, T-0660/T-0676 onward) leaves through ``tg_notify`` -> ``_send_stakeholder_dm``
-> ``TgClient.send``, which records the routing key and nothing else. So the
defect is COVERAGE, not absence — one send path was recorded and five were not,
and the recorded one fell out of use. That is why this module hooks the
TRANSPORT rather than adding a sixth caller-side append: a chokepoint every
send must pass through cannot fall out of use.

Shape
-----
Two files, one durable record, in that order:

1. **The spool** — ``data/_worker/outbound/<YYYY-MM-DD>.jsonl``, written by the
   send path itself. One local ``O_APPEND`` line write, no network, no lock:
   this runs immediately after a message was *delivered*, so it must be as
   close to free as a write can be, and it must never raise (see
   :func:`record`). This file is the complete record — nothing here can be lost
   by a later failure.
2. **The transcript** — the spool is drained (``outbound_drain_tick``) into the
   conversation store itself via the API's token-gated append, so
   ``<slug>/<gid>[/tN].jsonl`` becomes a true interleaved transcript. It is
   mirrored INTO that file rather than kept beside it on purpose: the failure
   this ticket exists to prevent was a human reading the raw JSONL, and a side
   file is exactly what such a reader misses.

Splitting write from mirror is what lets the send path stay local-only while
the store keeps its single-writer invariant (the API owns
``conversation_store``; the worker never writes it directly). A drain failure
costs latency, never the record.

Authorship convention (T-0755 owns it; T-0746 consumes it)
----------------------------------------------------------
``author`` is ``<class>`` or ``<class>:<detail>``, and the class is one of
exactly three:

``user``
    A HUMAN wrote this content. No detail segment. Never used for anything the
    system composed.
``session:<sid>``
    An agent session authored this content.
``system:<kind>``
    The system composed it — ``system:task-lifecycle``, ``system:tg-listener``,
    ``system:deploy_monitor``, ``system:voice-intake``.

Orthogonal to author, every record carries ``direction``: ``"in"`` (it reached
us) or ``"out"`` (we sent it). The two are genuinely independent — a
``system:`` line can be either — and conflating them is what made the store
readable as an inbox. ``direction`` defaults to ``"in"`` on read, so every line
written before T-0755 keeps its exact meaning with no migration.

Retention (deliberate, per the operator's ask)
----------------------------------------------
* **Bodies are not truncated in practice.** Telegram's own 4096-char cap
  bounds a single send, and long pages are already split into parts by
  ``tg.split_for_tg`` *before* they reach the transport — so one record is at
  most ~4 KiB. :data:`BODY_CAP` (8 KiB) is a runaway guard for a non-TG
  transport, not routine trimming, and when it fires it says so in the record.
* **Spool files are deleted after :data:`SPOOL_RETENTION_DAYS` (14).** The
  spool is a delivery queue, not the archive: once drained, its content lives
  in the conversation store. 14 days is far past the drain interval and past
  any plausible API outage.
* **The archive — the conversation store — is not trimmed by this module.**
  Deleting the record of what we told the stakeholder is the exact failure this
  ticket exists to fix. At the live rate (~10 outbound/day, ~1 KiB each) a
  thread grows ~4 MB/year; when that needs bounding it needs a deliberate
  archival decision, not a silent cap here.

Best-effort, but LOUD (T-0586)
------------------------------
A logging failure must never break a send, so every entry point swallows its
exceptions — but never silently. Each drop is logged at ERROR and counted in
:data:`DROPS`, which :func:`stats` exposes, because T-0586's precedent is a
silent best-effort drop that left a real 13.5-minute failure undiagnosable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Per-record body cap. See "Retention" above — TG's 4096 cap plus pre-transport
#: splitting means this is a runaway guard, not routine trimming.
BODY_CAP = 8192

#: Days a drained spool file is kept before deletion.
SPOOL_RETENTION_DAYS = 14

#: Max spool lines drained per tick — bounds one tick's work when a long API
#: outage has backed the spool up.
DRAIN_BATCH = 200

#: Counters for the LOUD half of best-effort. Reset only by process restart.
DROPS: dict[str, int] = {"record": 0, "drain": 0, "mirror": 0}


# ---------------------------------------------------------------------------
# Authorship convention (module docstring, "Authorship convention")
# ---------------------------------------------------------------------------

#: The complete set of author classes. Closed on purpose: a writer that could
#: invent a class is how the store's vocabulary drifts, and T-0746 needs one
#: decision it can rely on rather than a convention held only in prose.
AUTHOR_CLASSES = ("user", "session", "system")


def is_valid_author(author: str) -> bool:
    """True when ``author`` obeys the convention.

    ``user`` takes NO detail segment — that is the load-bearing half. The class
    means "a human wrote this content", so ``user:something`` would be a
    qualified human, which is not a thing this store can represent.
    """
    a = str(author or "").strip()
    if a == "user":
        return True
    cls, sep, detail = a.partition(":")
    return bool(sep) and cls in ("session", "system") and bool(detail.strip())


def author_class(author: str) -> str:
    """The class half of ``author`` (``"user"`` / ``"session"`` / ``"system"``),
    or ``""`` when it does not obey the convention."""
    a = str(author or "").strip()
    if a == "user":
        return "user"
    cls = a.partition(":")[0]
    return cls if is_valid_author(a) else ""


def author_for_send(*, route_sid: str, sender_label: str) -> str:
    """The convention-valid author for one outbound send.

    ``route_sid`` is the RAW routing SID when the sender is a real session (the
    same value ``tg_reply_map`` requires before it will pin a reply route).
    Anything else — ``deploy_monitor``, ``voice_intake``, a display label like
    ``"bot-squad operator"``, or nothing at all — is the system speaking, and
    is recorded as ``system:<kind>`` rather than being dropped: what the deploy
    monitor told the stakeholder is as unauditable as what a session told him.
    """
    from bot_squad_worker import tg_reply_map

    sid = str(route_sid or "").strip()
    if tg_reply_map.is_routing_sid(sid):
        return f"session:{sid}"
    kind = _slugify_kind(sender_label) or "unattributed"
    return f"system:{kind}"


def _slugify_kind(label: str) -> str:
    """A ``system:`` detail segment from a free-form sender label.

    Kept to ``[a-z0-9-]`` so the detail can never smuggle a ``:`` and forge a
    different class, and never carries a path separator (these values reach
    filenames nowhere today, but the store's own segment validator rejects them
    and there is no reason to hand it something it must refuse).
    """
    s = re.sub(r"[^a-z0-9]+", "-", str(label or "").lower()).strip("-")
    return s[:48]


# ---------------------------------------------------------------------------
# Secret redaction — never write a token into a world-readable jsonl
# ---------------------------------------------------------------------------

# Generic shapes, matched regardless of what config holds. Deliberately narrow:
# a redactor that eats ordinary prose would quietly destroy the record this
# ticket exists to preserve, which is a worse failure than the one it prevents.
_SHAPES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # Telegram bot token: <8-10 digit bot id>:<35-char secret>.
    ("tg-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    # JWT (the install mints these for UI auth) — three base64url segments.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # Authorization headers quoted out of a curl recipe or a traceback.
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    # `token = "..."` / `api_key: ...` / `password=...` in a quoted config blob.
    ("assigned-secret", re.compile(
        r"(?i)\b(?:bot_token|api[_-]?key|secret[_-]?key|jwt[_-]?secret|"
        r"worker[_-]?api[_-]?token|password|passwd|token)\b\s*[:=]\s*"
        r"[\"']?([A-Za-z0-9._~+/=:-]{12,})[\"']?")),
    # `enc:` values from secrets.toml — ciphertext, but still not ours to copy.
    ("enc-secret", re.compile(r"\benc:[A-Za-z0-9+/=_-]{16,}\b")),
)

_PLACEHOLDER = "‹redacted:{kind}›"

# Live secrets are worth matching literally as well as by shape: an install's
# real token can be quoted in a form no generic pattern anticipates (split
# across a log line, embedded in a URL). Short values are skipped — redacting a
# 4-character "secret" would blank ordinary text everywhere it occurs.
_MIN_LITERAL_LEN = 12


def _literal_secrets(cfg: Any) -> list[str]:
    """This install's actual secret values, longest first.

    Longest-first matters: a bot token contains its own bot-id prefix, so
    replacing the short value first would leave the long one unmatched.
    """
    raw = [
        getattr(cfg, "tg_bot_token", ""),
        getattr(cfg, "max_bot_token", ""),
        os.environ.get("WORKER_API_TOKEN", ""),
        os.environ.get("JWT_SECRET", ""),
        os.environ.get("BOT_SQUAD_SECRETS_KEY", ""),
    ]
    vals = {str(v).strip() for v in raw if v and len(str(v).strip()) >= _MIN_LITERAL_LEN}
    return sorted(vals, key=len, reverse=True)


def redact(text: str, *, cfg: Any = None) -> tuple[str, list[str]]:
    """Return ``(clean_text, kinds_redacted)``.

    ``kinds_redacted`` is non-empty exactly when something was replaced, and it
    is stored on the record: a redaction that leaves no trace is indistinguishable
    from text that never contained a secret, and an auditor reading this log
    needs to know which of the two they are looking at.
    """
    out = str(text or "")
    kinds: list[str] = []
    for secret in _literal_secrets(cfg):
        if secret in out:
            out = out.replace(secret, _PLACEHOLDER.format(kind="live-secret"))
            if "live-secret" not in kinds:
                kinds.append("live-secret")
    for kind, pat in _SHAPES:
        # The assigned-secret pattern captures only the VALUE (group 1) so the
        # key name survives — "token = <redacted>" stays diagnosable, which is
        # the whole point of redacting rather than dropping the message.
        if pat.groups:
            new, n = pat.subn(
                lambda m: m.group(0).replace(
                    m.group(1), _PLACEHOLDER.format(kind=kind)), out)
        else:
            new, n = pat.subn(_PLACEHOLDER.format(kind=kind), out)
        if n:
            out = new
            kinds.append(kind)
    return out, kinds


def _cap_body(text: str) -> tuple[str, int]:
    """Apply :data:`BODY_CAP`. Returns ``(body, original_len_if_capped)``.

    Keeps the HEAD: an over-cap body is a composed page whose subject and
    commitments lead, and a tail-only record would be the least useful half.
    """
    t = str(text or "")
    if len(t) <= BODY_CAP:
        return t, 0
    return t[:BODY_CAP], len(t)


# ---------------------------------------------------------------------------
# The spool — written by the send path
# ---------------------------------------------------------------------------

def spool_dir(data_dir: Any) -> Path:
    return Path(data_dir) / "_worker" / "outbound"


def _spool_path(data_dir: Any, *, when: datetime | None = None) -> Path:
    day = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return spool_dir(data_dir) / f"{day}.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(
    data_dir: Any,
    *,
    channel: str,
    chat_id: Any,
    text: str,
    route_sid: str = "",
    sender_label: str = "",
    thread_id: Any = None,
    message_id: Any = None,
    reply_to_message_id: Any = None,
    cfg: Any = None,
    timestamp: str | None = None,
) -> dict | None:
    """Record one DELIVERED outbound message. Returns the stored record.

    Called from the transport immediately after a successful send, so it does
    exactly one thing that can block: a single line append to a local file.
    No lock (``O_APPEND`` of one line is atomic enough for the single worker
    process that writes here), no network, no read-modify-write — the store is
    read live by several sessions on every check and this must not be something
    they queue behind.

    NEVER raises. A logging failure that broke a send would be strictly worse
    than the gap it is fixing, so every failure is caught, counted in
    :data:`DROPS` and logged at ERROR (T-0586: the drop must be diagnosable).
    Returns ``None`` when nothing was recorded.
    """
    try:
        body, kinds = redact(text, cfg=cfg)
        body, orig_len = _cap_body(body)
        rec: dict[str, Any] = {
            "timestamp": timestamp or _now_iso(),
            "direction": "out",
            "author": author_for_send(route_sid=route_sid, sender_label=sender_label),
            "channel": str(channel or "tg"),
            "chat_id": str(chat_id),
            "text": body,
        }
        if thread_id is not None and str(thread_id).strip() != "":
            rec["thread_id"] = thread_id
        if message_id is not None:
            rec["message_id"] = message_id
        if reply_to_message_id is not None:
            # The inbound message this send ANSWERS, when the send was threaded
            # to one (T-0725 made replies real TG replies). This is the join
            # the operator could not make: "we sent something" vs "we sent THIS,
            # answering THAT".
            rec["reply_to_message_id"] = reply_to_message_id
        if kinds:
            rec["redacted"] = kinds
        if orig_len:
            rec["truncated"] = {"orig_len": orig_len, "cap": BODY_CAP}
        path = _spool_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception:  # noqa: BLE001 — a log failure must never break a SEND
        DROPS["record"] += 1
        log.exception(
            "outbound_log.record DROPPED an outbound message (chat=%s sid=%s) "
            "— it will NOT appear in the transcript; drops=%d",
            chat_id, route_sid or sender_label, DROPS["record"])
        return None


def stats() -> dict:
    """Drop counters, for the loud half of best-effort (surfaced by the tick's
    audit dict and readable from any session)."""
    return dict(DROPS)


# ---------------------------------------------------------------------------
# The drain — spool -> the conversation store, so the transcript interleaves
# ---------------------------------------------------------------------------

def _cursor_path(data_dir: Any) -> Path:
    """How far each spool file has been mirrored (``{filename: lines_done}``).

    A byte/line cursor rather than rewriting the spool: the spool file is
    append-only and may be being appended to WHILE the drain reads it, so
    consuming by rewrite would race the send path. Nothing here may make a send
    wait.
    """
    return spool_dir(data_dir) / ".drained.json"


def _load_cursor(data_dir: Any) -> dict[str, int]:
    try:
        raw = json.loads(_cursor_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _save_cursor(data_dir: Any, cursor: dict[str, int]) -> None:
    p = _cursor_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cursor, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def resolve_conversation(cfg: Any, *, chat_id: Any, thread_id: Any = None) -> dict | None:
    """``(chat_id, thread_id)`` -> ``{"slug", "gid", "thread_id"}``, or ``None``.

    The send path speaks addresses (chat + forum thread); the conversation store
    is keyed by identity (project + global user + thread). ``conversation_locus``
    already holds that correspondence — it records, per inbound message, which
    ``(chat_id, thread_id)`` a ``(slug, gid[, thread])`` conversation is
    happening on — so this is that map read BACKWARDS rather than a new source
    of truth that could disagree with the relay's own routing.

    Exact-match only: an entry for the same chat but a DIFFERENT thread is a
    different conversation, and mirroring into it would file our reply under a
    topic we never sent it to. ``None`` (a deploy notification to a project chat
    nobody has ever conversed in, a brand-new topic) is normal and not an error
    — the record still lives in full in the spool, which is why the spool is the
    archive and this is only the interleave.
    """
    from bot_squad_worker import conversation_locus

    want_chat = str(chat_id or "").strip()
    if not want_chat:
        return None
    want_thread = None if thread_id in (None, "") else str(thread_id)
    best: dict | None = None
    for key, entry in conversation_locus.load(cfg).items():
        if str(entry.get("chat_id") or "") != want_chat:
            continue
        got = entry.get("thread_id")
        got = None if got in (None, "") else str(got)
        if got != want_thread:
            continue
        slug, _, rest = str(key).partition(":")
        gid = rest.split(":", 1)[0]
        if not slug or not gid:
            continue
        cand = {"slug": slug, "gid": gid, "thread_id": entry.get("thread_id"),
                "at": entry.get("at") or ""}
        if best is None or cand["at"] > best["at"]:
            best = cand
    return best


def _mirror(cfg: Any, rec: dict) -> bool:
    """Append one spool record into the conversation store via the API.

    Goes through the token-gated worker->API append (the store is
    API-single-writer) exactly as ``tg_listener.append_conversation`` and
    ``task_chat._append_thread`` already do. ``direction="out"`` is what stops
    this from looping: the API relays a ``session:``-authored append back out to
    Telegram, and this record IS an already-delivered send, so a relay would
    send it a second time.
    """
    from bot_squad_worker import tg_listener as _tgl

    target = resolve_conversation(cfg, chat_id=rec.get("chat_id"),
                                  thread_id=rec.get("thread_id"))
    if target is None:
        return False
    base = _tgl._api_base_url()
    token = _tgl._worker_api_token()
    if not base or not token:
        return False
    import httpx

    payload: dict[str, Any] = {
        "author": rec.get("author"),
        "text": rec.get("text") or "",
        "timestamp": rec.get("timestamp"),
        "channel": rec.get("channel") or "tg",
        "direction": "out",
        # Already on the wire — see the endpoint's relay gate.
        "delivered": True,
    }
    if target.get("thread_id") not in (None, ""):
        payload["thread_id"] = target["thread_id"]
    r = httpx.post(
        f"{base}/api/m/worker/conversations/{target['slug']}/{target['gid']}/messages",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return True


def _prune_spool(data_dir: Any, cursor: dict[str, int]) -> int:
    """Delete spool files older than :data:`SPOOL_RETENTION_DAYS`. Returns the
    count removed. See "Retention" in the module docstring for why only the
    spool is pruned and the archive is not."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=SPOOL_RETENTION_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for p in sorted(spool_dir(data_dir).glob("*.jsonl")):
        if p.stem >= cutoff:
            continue
        try:
            p.unlink()
            cursor.pop(p.name, None)
            removed += 1
        except OSError:
            log.warning("outbound_log: could not prune spool file %s", p)
    return removed


def drain(cfg: Any, *, limit: int = DRAIN_BATCH) -> dict:
    """Mirror new spool records into the conversation store. Returns an audit
    dict: ``{mirrored, unresolved, failed, pruned, drops}``.

    ``unresolved`` counts records whose destination is not a known conversation
    (a deploy chat, a topic nobody has written in yet). Those are NOT retried
    forever — the cursor advances past them — because their content is already
    durable in the spool and re-resolving them on every tick would be a
    permanent no-op that hides real failures behind it. ``failed`` is the count
    that resolved but could not be appended; those DO hold the cursor, so an
    API outage delays the interleave rather than losing it.
    """
    data_dir = Path(cfg.data_dir)
    out = {"mirrored": 0, "unresolved": 0, "failed": 0, "pruned": 0}
    try:
        cursor = _load_cursor(data_dir)
        budget = int(limit)
        for path in sorted(spool_dir(data_dir).glob("*.jsonl")):
            if budget <= 0:
                break
            done = cursor.get(path.name, 0)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                log.warning("outbound_log.drain: unreadable spool %s", path)
                continue
            for idx in range(done, len(lines)):
                if budget <= 0:
                    break
                raw = lines[idx].strip()
                if not raw:
                    cursor[path.name] = idx + 1
                    continue
                budget -= 1
                try:
                    rec = json.loads(raw)
                except ValueError:
                    # A torn line can never be mirrored; skipping it and saying
                    # so beats stalling every later record behind it forever.
                    log.warning("outbound_log.drain: skipping torn line %s:%d",
                                path.name, idx + 1)
                    cursor[path.name] = idx + 1
                    continue
                try:
                    mirrored = _mirror(cfg, rec)
                except Exception:  # noqa: BLE001 — retried next tick
                    DROPS["mirror"] += 1
                    log.exception(
                        "outbound_log.drain: mirror FAILED for %s:%d (chat=%s) "
                        "— the send is recorded in the spool but NOT yet in the "
                        "transcript; will retry; failures=%d",
                        path.name, idx + 1, rec.get("chat_id"), DROPS["mirror"])
                    out["failed"] += 1
                    break
                cursor[path.name] = idx + 1
                if mirrored:
                    out["mirrored"] += 1
                else:
                    out["unresolved"] += 1
            else:
                continue
            if out["failed"]:
                break
        out["pruned"] = _prune_spool(data_dir, cursor)
        _save_cursor(data_dir, cursor)
    except Exception:  # noqa: BLE001 — a tick failure must not kill the scheduler
        DROPS["drain"] += 1
        log.exception("outbound_log.drain failed (drops=%d)", DROPS["drain"])
    out["drops"] = stats()
    return out


def read_spool(
    data_dir: Any, *, since: str = "", chat_id: str = "", limit: int = 200,
) -> list[dict]:
    """Chronological spool records, newest-last — the raw "what did we send"
    lookup that does not depend on the mirror having resolved anything.

    This is what an operator asking "did we answer him between 04:47 and 05:10?"
    reads when the answer must not depend on routing having worked.
    """
    out: list[dict] = []
    for path in sorted(spool_dir(data_dir).glob("*.jsonl")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if since and str(rec.get("timestamp") or "") < since:
                continue
            if chat_id and str(rec.get("chat_id") or "") != str(chat_id):
                continue
            out.append(rec)
    out.sort(key=lambda r: str(r.get("timestamp") or ""))
    return out[-int(limit):] if limit and len(out) > int(limit) else out


def _epoch_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def spool_health(data_dir: Any) -> dict:
    """``{files, undrained, oldest_undrained_age_s, drops}`` — the one call a
    monitor or a session needs to see the log is actually working."""
    cursor = _load_cursor(data_dir)
    files = sorted(spool_dir(data_dir).glob("*.jsonl"))
    undrained = 0
    oldest = 0.0
    now = time.time()
    for p in files:
        try:
            total = sum(1 for line in p.read_text(encoding="utf-8").splitlines()
                        if line.strip())
        except OSError:
            continue
        pending = max(0, total - cursor.get(p.name, 0))
        if pending:
            undrained += pending
            oldest = max(oldest, now - p.stat().st_mtime)
    return {"files": len(files), "undrained": undrained,
            "oldest_undrained_age_s": int(oldest), "drops": stats()}
