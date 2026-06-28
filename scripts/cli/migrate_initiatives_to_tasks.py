#!/usr/bin/env python3
"""T-0480 / D-0038: migrate legacy initiatives into the task store as tasks
marked ``kind: initiative``.

Initiatives live today as ``vision/initiatives/*.md`` (a separate entity type)
with lifecycle in the ``active_initiatives`` / ``finished_initiatives`` sidecars.
This converts each into a real task (Fork D1=A: renumber to a ``T-NNNN`` id) so
the unification the stakeholder asked for collapses to ONE relation
(``parent_task``). Old refs keep resolving via an alias index
(``vision/initiative_aliases.json``) that the byte-identical resolver
(``initiative_resolver.py``, worker+api) reads.

Principles (operator-gated capstone):
  * DRY-RUN BY DEFAULT — pass --apply to write. Dry-run produces the manifest
    the operator reviews BEFORE authorizing the data move.
  * IDEMPOTENT — a migrated original is archived under ``_migrated/`` so a second
    run enumerates nothing and is a no-op.
  * REVERSIBLE — originals are MOVED (not deleted) to ``_migrated/``; a manifest
    records every action; --rollback replays it in reverse.

Self-contained (no bot_squad_worker import — scripts/cli has no package path);
the counter protocol mirrors idalloc.allocate_id exactly.

Usage:
  migrate_initiatives_to_tasks.py --slug bot-squad                 # dry-run + manifest
  migrate_initiatives_to_tasks.py --slug bot-squad --manifest-out /tmp/m.json
  migrate_initiatives_to_tasks.py --slug bot-squad --apply         # execute (gated)
  migrate_initiatives_to_tasks.py --slug bot-squad --apply --backfill-children
  migrate_initiatives_to_tasks.py --rollback /path/to/manifest.json
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA = "/home/www/bot-squad/data"
DEFAULT_SLUG = "bot-squad"

# process-paradigm binds 50+ live tickets — migrate it LAST (D-0038 §5).
_MIGRATE_LAST = "process-paradigm"

# T-0480 D-B (operator 2026-06-28): a small number of REAL shipped initiatives
# live at the vision/ ROOT instead of vision/initiatives/. Enumerate them too —
# but EXPLICITLY by exact name, so the neighbouring source docs
# (INI-XX-process-paradigm-SOURCE-VERBATIM.md, INI-process-paradigm-STRUCTURED.md)
# are NOT swept in.
_EXTRA_ROOT_INITIATIVES = (
    "INI-04-structured-stakeholder-comms-2026-06-21.md",
)

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
_INI_PREFIX_RE = re.compile(r"^(INI-\d+)")
_TASK_FILE_RE = re.compile(r"^T-(\d{4,})-")
_TASK_ID_RE = re.compile(r"^T-\d{4,}$")
_INITIATIVE_FIELD_RE = re.compile(r"^initiative:\s*(.*)$", re.M)


# --- parsing helpers --------------------------------------------------------

def _unquote(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val.strip()


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (meta, body). Tolerant: a file with no ``---`` block yields
    ({}, whole-text). Scalars only — enough for initiative + task frontmatter."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = _unquote(v.strip())
    return meta, m.group(2)


def normalize_ref(value: str) -> str:
    """Strip exactly one trailing literal ``.md`` (mirror of normalize_id)."""
    return value[:-3] if value.endswith(".md") else value


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return (s[:60].rstrip("-") or "initiative")


# --- single-initiative derivation -------------------------------------------

def derive_initiative(path: Path) -> dict:
    """Parse one initiative file into the fields the migration needs. Robust to
    both shapes: full frontmatter (INI-01/03) and NO frontmatter (8 of 15 live
    files, incl the bare one-line ui-polish.md)."""
    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    stem = path.stem  # filename without .md

    # old INI id: from frontmatter `id:` if it's an INI-NN, else the filename
    # prefix (no-frontmatter files like INI-02 carry the id only in the name).
    old_id = None
    fm_id = (meta.get("id") or "").strip()
    if fm_id.startswith("INI-"):
        old_id = fm_id
    else:
        pm = _INI_PREFIX_RE.match(stem)
        if pm:
            old_id = pm.group(1)

    # title: frontmatter name > first H1 in the doc > filename stem.
    title = (meta.get("name") or "").strip()
    if not title:
        hm = _H1_RE.search(text)
        title = hm.group(1).strip() if hm else stem

    created = (meta.get("created") or "").strip()
    if not created:
        created = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    provenance = (meta.get("provenance") or "").strip() or None

    # aka / alias keys: the short INI id (if any) + the filename stem. Both map
    # to the new T-id; the resolver normalizes (.md-stripped) on lookup.
    aka: list[str] = []
    if old_id:
        aka.append(old_id)
    if stem not in aka:
        aka.append(stem)

    return {
        "file": path.name,
        "stem": stem,
        "old_id": old_id,
        "title": title,
        "created": created,
        "provenance": provenance,
        "body": body,
        "aka": aka,
        "alias_keys": list(aka),
    }


def derive_status(basename_md: str, active: set, finished: set) -> str:
    """Sidecar membership → task status (D-0038 §5.3). active → in_progress,
    finished → closed, neither → open."""
    if basename_md in finished:
        return "closed"
    if basename_md in active:
        return "in_progress"
    return "open"


# --- store helpers ----------------------------------------------------------

def _root(data_dir, slug: str) -> Path:
    return Path(data_dir) / slug


def _read_sidecar(root: Path, name: str) -> set:
    p = root / "vision" / name
    try:
        return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}
    except (FileNotFoundError, OSError):
        return set()


def _read_counter(root: Path) -> int:
    p = root / "_counters" / "task.txt"
    try:
        raw = p.read_text(encoding="utf-8").strip()
        return int(raw) if raw.isdigit() else 0
    except (FileNotFoundError, OSError, ValueError):
        return 0


def _scan_max_task_id(backlog_dir: Path) -> int:
    mx = 0
    if backlog_dir.is_dir():
        for f in backlog_dir.glob("T-*.md"):
            m = _TASK_FILE_RE.match(f.name)
            if m:
                mx = max(mx, int(m.group(1)))
    return mx


def _collect_initiative_files(root: Path) -> list[Path]:
    """All migratable initiative files: vision/initiatives/*.md PLUS the
    explicit vision/-root initiatives (D-B). process-paradigm LAST; everything
    else alphabetical by stem."""
    initiatives_dir = root / "vision" / "initiatives"
    files = [
        p for p in initiatives_dir.glob("*.md")
        if p.is_file() and p.parent.name != "_migrated"
    ]
    for name in _EXTRA_ROOT_INITIATIVES:
        extra = root / "vision" / name
        if extra.is_file():
            files.append(extra)
    return sorted(files, key=lambda p: (1 if p.stem == _MIGRATE_LAST else 0, p.stem))


# --- plan (no writes) -------------------------------------------------------

def build_plan(data_dir, slug: str) -> dict:
    """Compute the full migration plan + manifest WITHOUT writing anything or
    allocating ids (ids are PROJECTED from the counter). This is the artifact the
    operator reviews at the Phase-2 gate."""
    root = _root(data_dir, slug)
    backlog_dir = root / "backlog"

    active = _read_sidecar(root, "active_initiatives")
    finished = _read_sidecar(root, "finished_initiatives")

    counter_before = _read_counter(root)
    base = max(counter_before, _scan_max_task_id(backlog_dir))

    ordered = _collect_initiative_files(root)
    initiatives: list[dict] = []
    alias_index: dict[str, str] = {}
    for i, path in enumerate(ordered):
        d = derive_initiative(path)
        # record the original location relative to the project root so apply +
        # rollback move it back correctly (vision/initiatives/ OR vision/ root).
        d["src_relpath"] = str(path.relative_to(root))
        new_id = f"T-{base + 1 + i:04d}"
        d["new_id"] = new_id
        d["status"] = derive_status(d["file"], active, finished)
        for key in d["alias_keys"]:
            alias_index[normalize_ref(key)] = new_id
        initiatives.append(d)

    children = _child_reparent_plan(backlog_dir, alias_index)

    return {
        "ticket": "T-0480",
        "slug": slug,
        "data_dir": str(data_dir),
        "counter_before": counter_before,
        "count": len(initiatives),
        "migrate_last": _MIGRATE_LAST,
        "initiatives": [
            {k: v for k, v in d.items() if k != "body"} for d in initiatives
        ],
        "alias_index": alias_index,
        "children": children,
        "_full": initiatives,  # bodies retained for apply; stripped before json dump
    }


def _child_reparent_plan(backlog_dir: Path, alias_index: dict) -> dict:
    matched = 0
    unmatched = 0
    unmatched_refs: dict[str, int] = {}
    matched_by_initiative: dict[str, int] = {}
    if backlog_dir.is_dir():
        for f in backlog_dir.glob("T-*.md"):
            try:
                meta, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            ref = (meta.get("initiative") or "").strip()
            if not ref or ref in ("~", "null", "None"):
                continue
            tid = alias_index.get(normalize_ref(ref))
            if tid:
                matched += 1
                matched_by_initiative[tid] = matched_by_initiative.get(tid, 0) + 1
            else:
                unmatched += 1
                unmatched_refs[ref] = unmatched_refs.get(ref, 0) + 1
    return {
        "matched": matched,
        "unmatched": unmatched,
        "unmatched_refs": unmatched_refs,
        "matched_by_initiative": matched_by_initiative,
    }


# --- task file emission -----------------------------------------------------

def _dump_initiative_task(d: dict) -> str:
    """Serialize one initiative-task md. Provenance is PRESERVED from the
    original or stamped ``T-0480`` (the migration ticket) so post-cutoff ids
    pass the provenance lint. Lists emitted inline (T-0075 convention)."""
    prov = d.get("provenance") or "T-0480"
    lines = ["---"]
    lines.append(f"id: {d['new_id']}")
    lines.append(f"title: {json.dumps(d['title'], ensure_ascii=False)}")
    lines.append(f"status: {d['status']}")
    lines.append("kind: initiative")
    lines.append("priority: 0")
    lines.append(f"created: {d['created']}")
    lines.append(f"provenance: {json.dumps(prov, ensure_ascii=False)}")
    if d["aka"]:
        lines.append("aka: [" + ", ".join(d["aka"]) + "]")
    lines.append("---")
    body = (d.get("body") or "").lstrip("\n")
    return "\n".join(lines) + "\n\n" + body + ("\n" if not body.endswith("\n") else "")


def _write_counter(root: Path, value: int) -> None:
    counters_dir = root / "_counters"
    counters_dir.mkdir(parents=True, exist_ok=True)
    p = counters_dir / "task.txt"
    with open(p, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(value))
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# --- run / apply / rollback -------------------------------------------------

def run(data_dir, slug: str, *, apply: bool = False, manifest_out=None,
        backfill_children: bool = False) -> dict:
    """Build the plan; with apply=True execute it (write tasks, archive
    originals, write alias index, optionally reparent children). Always writes
    the manifest (the dry-run manifest IS the operator-review artifact)."""
    root = _root(data_dir, slug)
    plan = build_plan(data_dir, slug)
    full = plan.pop("_full")

    actions: list[dict] = []
    if apply and full:
        backlog_dir = root / "backlog"
        backlog_dir.mkdir(parents=True, exist_ok=True)
        migrated_dir = root / "vision" / "initiatives" / "_migrated"
        migrated_dir.mkdir(parents=True, exist_ok=True)

        for d in full:
            filename = f"{d['new_id']}-{_slugify(d['title'])}.md"
            dest = backlog_dir / filename
            dest.write_text(_dump_initiative_task(d), encoding="utf-8")
            orig = root / d["src_relpath"]
            archived = migrated_dir / d["file"]
            if orig.exists():
                shutil.move(str(orig), str(archived))
            actions.append({
                "new_id": d["new_id"], "task_file": filename,
                "archived_from": d["file"], "src_relpath": d["src_relpath"],
                "old_id": d["old_id"],
            })

        # advance the counter to the highest id we just allocated
        highest = max(int(d["new_id"][2:]) for d in full)
        _write_counter(root, highest)

        # write the alias index the resolver reads
        alias_path = root / "vision" / "initiative_aliases.json"
        alias_path.write_text(json.dumps(plan["alias_index"], indent=2, ensure_ascii=False),
                              encoding="utf-8")

        if backfill_children:
            actions.append({"backfill_children": _backfill_children(
                backlog_dir, plan["alias_index"])})

    plan["applied"] = apply
    plan["actions"] = actions

    # default manifest location
    if manifest_out is None and apply:
        manifest_out = root / "vision" / "initiatives" / "_migrated" / "manifest.json"
    if manifest_out is not None:
        mp = Path(manifest_out)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        plan["manifest_path"] = str(mp)

    return plan


def _backfill_children(backlog_dir: Path, alias_index: dict) -> dict:
    """Stamp parent_task from the resolved initiative ref. KEEP the legacy
    initiative: field (belt-and-suspenders; a later pass can drop it)."""
    done = 0
    for f in backlog_dir.glob("T-*.md"):
        text = f.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(text)
        ref = (meta.get("initiative") or "").strip()
        if not ref or ref in ("~", "null", "None"):
            continue
        tid = alias_index.get(normalize_ref(ref))
        if not tid or meta.get("parent_task"):
            continue
        # insert/replace parent_task in the frontmatter block
        m = _FM_RE.match(text)
        if not m:
            continue
        block = m.group(1)
        if re.search(r"^parent_task:", block, re.M):
            block = re.sub(r"^parent_task:.*$", f"parent_task: {tid}", block, flags=re.M)
        else:
            block = block + f"\nparent_task: {tid}"
        f.write_text(f"---\n{block}\n---\n{m.group(2)}", encoding="utf-8")
        done += 1
    return {"reparented": done}


def rollback(manifest_path) -> dict:
    """Reverse an --apply run from its manifest: move archived originals back,
    delete the created initiative-task files, drop the alias index."""
    mp = Path(manifest_path)
    plan = json.loads(mp.read_text(encoding="utf-8"))
    root = _root(plan["data_dir"], plan["slug"])
    backlog_dir = root / "backlog"
    migrated_dir = root / "vision" / "initiatives" / "_migrated"
    restored = 0
    for a in plan.get("actions", []):
        if "task_file" not in a:
            continue
        archived = migrated_dir / a["archived_from"]
        # restore to the ORIGINAL location (vision/initiatives/ or vision/ root)
        orig = root / a.get("src_relpath", f"vision/initiatives/{a['archived_from']}")
        orig.parent.mkdir(parents=True, exist_ok=True)
        if archived.exists():
            shutil.move(str(archived), str(orig))
        tf = backlog_dir / a["task_file"]
        if tf.exists():
            tf.unlink()
        restored += 1
    alias_path = root / "vision" / "initiative_aliases.json"
    if alias_path.exists():
        alias_path.unlink()
    _write_counter(root, plan["counter_before"])
    return {"restored": restored}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Migrate initiatives → kind:initiative tasks (T-0480).")
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--slug", default=DEFAULT_SLUG)
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--backfill-children", action="store_true",
                    help="also stamp parent_task on child tasks (staged)")
    ap.add_argument("--manifest-out", default=None, help="write the manifest here")
    ap.add_argument("--rollback", default=None, help="reverse a prior apply from its manifest.json")
    args = ap.parse_args(argv)

    if args.rollback:
        res = rollback(args.rollback)
        print(json.dumps(res, indent=2))
        return 0

    plan = run(args.data_dir, args.slug, apply=args.apply,
               manifest_out=args.manifest_out, backfill_children=args.backfill_children)
    mode = "APPLIED" if args.apply else "DRY-RUN (no data moved)"
    print(f"=== migrate_initiatives_to_tasks: {mode} ===")
    print(f"initiatives: {plan['count']}  counter_before: {plan['counter_before']}  "
          f"(process-paradigm migrated last)")
    for d in plan["initiatives"]:
        print(f"  {d['new_id']}  status={d['status']:<12} aka={d['aka']}  <- {d['file']}")
    ch = plan["children"]
    print(f"child reparent: matched={ch['matched']} unmatched={ch['unmatched']}")
    if ch["unmatched_refs"]:
        print(f"  PHANTOM/unmatched refs (no initiative file): {ch['unmatched_refs']}")
    if plan.get("manifest_path"):
        print(f"manifest: {plan['manifest_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
