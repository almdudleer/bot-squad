"""Team entity — persisted, tmux-session-keyed roster (T-0142).

A ``Team`` is a first-class, on-disk bot-squad concept keyed by **tmux
session name** (e.g. ``bot-squad``, ``bot-squad-operator-ux-and-session-mgmt``).
It records the TL slot, the live teammates, and the archived members, and it
**survives a worker reload** because it is persisted under
``data/<slug>/teams/<name>.md`` and rebuilt from the SessionMd registry on
every dispatch tick.

Teams are a *projection* of the SessionMd registry, not an independent source
of truth: ``reconcile_teams`` groups every session by its ``tmux_session``
field, derives each session's role (via :func:`sessions._derive_role`), and
writes one Team md per tmux session. The durable md is what the UI/CLI read
and what lets a team resume after a server restart, but the projection is
always re-derivable from the sessions on disk, so a deleted/corrupt team md
self-heals on the next tick.

Schema (frontmatter under ``data/<slug>/teams/<name>.md``)::

    name: <tmux session name>          # the key
    tl: <SID or ~>                     # the lead slot (teamlead/operator role)
    teammates: [<SID>, ...]            # live/suspended dev members
    archived_members: [<SID>, ...]     # members archived (archived: true)
    archived: false                    # whole-team archive flag
    created_at: <ISO8601>
    updated_at: <ISO8601>
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from bot_squad_worker import sessions as _sessions

# tmux session names are conservative, but sanitise anyway so a team md path
# can never escape the teams dir or collide with frontmatter delimiters.
_NAME_SANITISE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sanitise_name(name: str) -> str:
    return _NAME_SANITISE_RE.sub("_", (name or "").strip()) or "_"


def _teams_dir(data_dir: Path, slug: str) -> Path:
    return data_dir / slug / "teams"


def _team_file(data_dir: Path, slug: str, name: str) -> Path:
    return _teams_dir(data_dir, slug) / f"{_sanitise_name(name)}.md"


def _read_team(path: Path) -> dict | None:
    """Parse a Team md's frontmatter (reuses the SessionMd parser)."""
    return _sessions._read_session_metadata(path)


def _write_team(path: Path, meta: dict, *, atomic: bool = True) -> None:
    """Persist a Team md (atomic by default, mirroring the gc reconcilers)."""
    _sessions._write_session_metadata(path, meta, atomic=atomic)


def _is_truthy(value: Any) -> bool:
    return str(value).lower() == "true"


def reconcile_teams(cfg: Any, slug: str) -> dict:
    """Rebuild Team mds from the SessionMd registry for ``slug``.

    Groups every SessionMd (current linux user) by its ``tmux_session`` field
    (defaulting to the project ``slug`` when absent), derives each session's
    role, and writes one Team md per group:

      - ``tl``: the lead slot — the operator/teamlead-role session, preferring
        a live/active one, breaking ties by latest ``started_at``.
      - ``teammates``: non-archived dev-role sessions.
      - ``archived_members``: sessions with ``archived: true``.

    ``created_at`` is preserved across reconciles; ``updated_at`` is bumped.
    A team whose membership is unchanged is still re-stamped (cheap, and keeps
    ``updated_at`` honest as a liveness signal).

    Returns ``{"ok": True, "teams": [<name>, ...], "reconciled": N}``.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"reconcile_teams: unknown project slug {slug!r}")

    sessions_dir = cfg.data_dir / slug / "sessions"
    if not sessions_dir.exists():
        return {"ok": True, "teams": [], "reconciled": 0}

    user = _sessions._get_current_user()
    user_prefix = f"S-{user}-"
    live_sids = {
        _sessions.compute_sid(user, p.window, p.pane_id)
        for p in _sessions.list_panes()
    }

    # T-0177: teams are keyed by the project (slug), not by tmux session — the
    # project TL and its initiative devs (spread across `<slug>-<initiative>`
    # tmux sessions by the T-0001 per-initiative routing) fold into ONE roster.
    # The exception is a *constant* team (prod-support, user-feedback — an
    # initiative flagged `constant_team: true`): it keeps its own identity,
    # keyed by its tmux session `<slug>-<stem>`. The sessions LIST still
    # sub-groups by live tmux for [[T-0176]] reality; team identity is the
    # project.
    from bot_squad_worker.constant_teams import constant_team_stems

    const_stems = constant_team_stems(cfg, slug)
    const_sessions = {f"{slug}-{stem}" for stem in const_stems}

    def _group_for(meta: dict) -> str:
        ts = meta.get("tmux_session")
        init_stem = Path(str(meta.get("initiative") or "")).stem
        if ts in const_sessions:
            return ts
        if init_stem and init_stem in const_stems:
            return f"{slug}-{init_stem}"
        return slug

    # group name -> list of (sid, meta)
    groups: dict[str, list[tuple[str, dict]]] = {}
    for md in sorted(sessions_dir.glob("*.md")):
        if not md.stem.startswith(user_prefix):
            continue
        meta = _sessions._read_session_metadata(md)
        if meta is None:
            continue
        sid = meta.get("sid", md.stem)
        groups.setdefault(_group_for(meta), []).append((sid, meta))

    teams_dir = _teams_dir(cfg.data_dir, slug)
    teams_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for name, members in groups.items():
        tl_candidates: list[tuple[str, dict]] = []
        teammates: list[str] = []
        archived_members: list[str] = []
        for sid, meta in members:
            role = _sessions._derive_role(
                meta.get("window"),
                meta.get("task_id"),
                meta.get("initiative"),
                extra_task_ids=[
                    t for t in (meta.get("extra_task_ids") or []) if t and t != "~"
                ],
                extra_initiatives=[
                    i for i in (meta.get("extra_initiatives") or []) if i and i != "~"
                ],
            )
            if _is_truthy(meta.get("archived")):
                archived_members.append(sid)
                continue
            if role in ("teamlead", "operator"):
                tl_candidates.append((sid, meta))
            else:
                teammates.append(sid)

        # Lead slot: prefer a live session, then the latest started_at.
        def _tl_rank(item: tuple[str, dict]) -> tuple[int, tuple[int, str]]:
            sid, meta = item
            live = 1 if sid in live_sids else 0
            return (live, _sessions._started_at_key(meta.get("started_at")))

        tl = "~"
        if tl_candidates:
            ranked = sorted(tl_candidates, key=_tl_rank, reverse=True)
            tl = ranked[0][0]
            # T-0177: folding by project can put several teamlead/operator-role
            # sessions in one group. Only one holds the lead slot; the rest stay
            # visible as teammates rather than being silently dropped.
            teammates.extend(sid for sid, _ in ranked[1:])

        path = _team_file(cfg.data_dir, slug, name)
        prev = _read_team(path) or {}
        meta_out = {
            "name": name,
            "tl": tl,
            "teammates": sorted(teammates),
            "archived_members": sorted(archived_members),
            "archived": prev.get("archived", "false") if prev else "false",
            "created_at": prev.get("created_at") or _now(),
            "updated_at": _now(),
        }
        _write_team(path, meta_out)
        written.append(name)

    return {"ok": True, "teams": sorted(written), "reconciled": len(written)}


def prune_orphan_teams(cfg: Any, slug: str, *, dry_run: bool = True) -> dict:
    """T-0177 (gated): remove stale team md files left by the tmux-session →
    project regroup. After ``reconcile_teams`` rebuilds the project team, the old
    per-initiative team files (e.g. ``<slug>-operator-ux-and-session-mgmt.md``)
    are orphans. Valid teams = the project team (``slug``) + each constant team
    (``<slug>-<stem>`` for a ``constant_team: true`` initiative). Any other team
    file is pruned, UNLESS it is ``archived: true`` (operator intent preserved).

    ``dry_run=True`` (default) reports what it *would* prune without deleting.
    Returns ``{ok, dry_run, pruned: [<name>, ...]}``.
    """
    from bot_squad_worker.actions import ActionError
    from bot_squad_worker.constant_teams import constant_team_stems

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"prune_orphan_teams: unknown project slug {slug!r}")

    teams_dir = _teams_dir(cfg.data_dir, slug)
    if not teams_dir.exists():
        return {"ok": True, "dry_run": dry_run, "pruned": []}

    valid = {slug} | {f"{slug}-{stem}" for stem in constant_team_stems(cfg, slug)}
    pruned: list[str] = []
    for md in sorted(teams_dir.glob("*.md")):
        meta = _read_team(md)
        if meta is None:
            continue
        name = meta.get("name", md.stem)
        if name in valid or _is_truthy(meta.get("archived")):
            continue
        pruned.append(name)
        if not dry_run:
            md.unlink()
    return {"ok": True, "dry_run": dry_run, "pruned": sorted(pruned)}


def list_teams(cfg: Any, slug: str) -> dict:
    """Return every persisted Team md for ``slug`` (read-only)."""
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"list_teams: unknown project slug {slug!r}")

    teams_dir = _teams_dir(cfg.data_dir, slug)
    teams: list[dict] = []
    if teams_dir.exists():
        for md in sorted(teams_dir.glob("*.md")):
            meta = _read_team(md)
            if meta is None:
                continue
            teams.append(meta)
    return {"ok": True, "teams": teams}


def load_team(cfg: Any, slug: str, name: str) -> dict | None:
    """Load a single Team md by tmux session name, or None if absent."""
    return _read_team(_team_file(cfg.data_dir, slug, name))


def archive_team(cfg: Any, slug: str, name: str) -> dict:
    """Archive a whole team: suspend every live member, mark the team archived.

    This is the "true Team archive" the bsq ``team archive`` seam (T-0146)
    was holding a place for: it retires the entire tmux-session-keyed team in
    one call, rather than the interim per-session ``suspend_session``.

    Live members (TL + teammates) are suspended (pane closed, md preserved);
    the team md gets ``archived: true``. Idempotent.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"archive_team: unknown project slug {slug!r}")

    path = _team_file(cfg.data_dir, slug, name)
    meta = _read_team(path)
    if meta is None:
        raise ActionError(f"archive_team: no team named {name!r}")

    user = _sessions._get_current_user()
    live_sids = {
        _sessions.compute_sid(user, p.window, p.pane_id)
        for p in _sessions.list_panes()
    }

    members = []
    tl = meta.get("tl")
    if tl and tl != "~":
        members.append(tl)
    members.extend(t for t in (meta.get("teammates") or []) if t and t != "~")

    suspended: list[str] = []
    for sid in members:
        if sid in live_sids:
            try:
                _sessions.suspend(cfg, slug, sid)
                suspended.append(sid)
            except Exception:
                # Best-effort: a member that won't suspend shouldn't block the
                # rest of the team from being archived.
                pass

    meta["archived"] = "true"
    meta["updated_at"] = _now()
    _write_team(path, meta)
    return {"ok": True, "name": name, "suspended": suspended, "archived": True}


def resurrect_team(cfg: Any, slug: str, name: str) -> dict:
    """Bring an archived team back: clear the archive flag, resume the TL.

    Resumes only the TL slot (the lead re-spawns its teammates on demand via
    the working-set policy) and clears ``archived`` so the team rejoins the
    default roster. Returns the resumed TL's (possibly rotated) SID.
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"resurrect_team: unknown project slug {slug!r}")

    path = _team_file(cfg.data_dir, slug, name)
    meta = _read_team(path)
    if meta is None:
        raise ActionError(f"resurrect_team: no team named {name!r}")

    resumed = None
    tl = meta.get("tl")
    if tl and tl != "~":
        try:
            res = _sessions.resume(cfg, slug, tl)
            resumed = res.get("sid", tl)
        except Exception as exc:  # surface but don't strand the team archived
            meta["archived"] = "false"
            meta["updated_at"] = _now()
            _write_team(path, meta)
            raise ActionError(f"resurrect_team: TL resume failed: {exc}")

    meta["archived"] = "false"
    meta["updated_at"] = _now()
    _write_team(path, meta)
    return {"ok": True, "name": name, "tl": resumed}
