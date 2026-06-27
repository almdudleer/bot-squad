"""T-0198: the spawn assembler reads the role contract from the git-tracked SSOT
(``api/app/resources/roles``), NOT the per-project live copy
(``data/<slug>/vision/roles``) which is seeded once at scaffold and drifts.

This pins the role-doc-drift fix (D-0043): editing the git SSOT in one place is
what a spawned session receives. The guard puts a DIFFERENT sentinel in each copy
and asserts the assembled brief carries the SSOT text, never the live text — so
the live copy can't silently become the effective source again.

``bsq`` is an extensionless script loaded via SourceFileLoader.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BSQ_PATH = Path(__file__).resolve().parent / "bsq"
_loader = SourceFileLoader("bsq_mod_rolessot", str(_BSQ_PATH))
_spec = importlib.util.spec_from_loader("bsq_mod_rolessot", _loader)
bsq = importlib.util.module_from_spec(_spec)
_loader.exec_module(bsq)

SLUG = "bot-squad"
TICKET = "T-9998"
SSOT_SENTINEL = "SSOT_ROLE_CONTRACT_4f1a — the git-tracked authoritative dev role."
LIVE_SENTINEL = "LIVE_ROLE_CONTRACT_9c3b — the STALE per-project live copy."


def _write_ticket(tmp_path: Path) -> dict:
    md = tmp_path / f"{TICKET}-fixture.md"
    md.write_text(
        "---\n"
        f"id: {TICKET}\n"
        "title: role ssot fixture\n"
        "---\n\n"
        "## Verbatim request\n\n> do the thing\n\n## DoD\n\n- ship it\n",
        encoding="utf-8",
    )
    return {"paths": {TICKET: md}, "fms": {TICKET: {"id": TICKET, "title": "role ssot fixture"}}}


def _seed_role_copies(tmp_path: Path) -> None:
    """Plant a SSOT dev.md and a DIFFERENT live dev.md under a fake BOT_SQUAD."""
    ssot = tmp_path / "api" / "app" / "resources" / "roles"
    ssot.mkdir(parents=True, exist_ok=True)
    (ssot / "dev.md").write_text(SSOT_SENTINEL + "\n", encoding="utf-8")
    live = tmp_path / "data" / SLUG / "vision" / "roles"
    live.mkdir(parents=True, exist_ok=True)
    (live / "dev.md").write_text(LIVE_SENTINEL + "\n", encoding="utf-8")


def test_assembler_reads_ssot_not_live_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(bsq, "BOT_SQUAD", str(tmp_path))
    monkeypatch.setattr(bsq, "_collect_guidance", lambda *a, **k: ([], 0, None))
    _seed_role_copies(tmp_path)
    f = _write_ticket(tmp_path)

    brief = bsq._assemble_prompt(SLUG, [TICKET], f["paths"], f["fms"], "dev", "S-tl")

    assert SSOT_SENTINEL in brief, "assembler must read the git-tracked SSOT role contract"
    assert LIVE_SENTINEL not in brief, "assembler must NOT read the drifting live copy"


def test_roles_ssot_dir_points_at_resources(monkeypatch):
    monkeypatch.setattr(bsq, "BOT_SQUAD", "/fake/install")
    assert bsq._roles_ssot_dir() == Path("/fake/install/api/app/resources/roles")
