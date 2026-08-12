"""F7 / T-0233-extension: auto-GC throwaway QA tasks so they stop leaking onto
the board (a dangling loop). A throwaway task is detected by its TITLE prefix
(``QA-TEST-DELETEME``) or a ``throwaway: true`` flag — NEVER by body text, so a
real ticket that merely *mentions* the convention is left alone."""
from __future__ import annotations

import os
import time
import types
from pathlib import Path

from bot_squad_worker import task_gc


def _cfg(tmp_path: Path):
    return types.SimpleNamespace(data_dir=tmp_path)


def _write_task(tmp_path: Path, name: str, *, title: str, status: str = "open",
                body: str = "", throwaway=None, age_sec: float = 0.0, now: float = 1_000_000.0):
    backlog = tmp_path / "proj" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    fm = f"---\nid: {name}\ntitle: {title!r}\nstatus: {status}\n"
    if throwaway is not None:
        fm += f"throwaway: {str(throwaway).lower()}\n"
    fm += "---\n\n"
    p = backlog / f"{name}.md"
    p.write_text(fm + body, encoding="utf-8")
    if age_sec:
        os.utime(p, (now - age_sec, now - age_sec))
    return p


# --- pure detection ---------------------------------------------------------

def test_is_throwaway_detects_title_prefix_case_insensitive():
    assert task_gc.is_throwaway_task("QA-TEST-DELETEME-board dogfood", {}) is True
    assert task_gc.is_throwaway_task("qa-test-deleteme x", {}) is True
    assert task_gc.is_throwaway_task("Normal feature ticket", {}) is False


def test_is_throwaway_detects_flag():
    assert task_gc.is_throwaway_task("Anything", {"throwaway": True}) is True
    assert task_gc.is_throwaway_task("Anything", {"throwaway": "true"}) is True
    assert task_gc.is_throwaway_task("Anything", {"throwaway": False}) is False


def test_is_throwaway_ignores_body_mentions():
    # detection takes (title, meta) — a ticket whose BODY discusses the
    # convention is NOT throwaway.
    assert task_gc.is_throwaway_task(
        "Add auto-GC for QA-TEST-DELETEME tasks", {}) is False


# --- GC behavior ------------------------------------------------------------

def test_gc_archives_stale_throwaway(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "T-0262", title="QA-TEST-DELETEME-board dogfood",
                status="open", age_sec=7200, now=now)  # 2h old, grace 1h
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert "T-0262" in out["archived"]
    backlog = tmp_path / "proj" / "backlog"
    assert not (backlog / "T-0262.md").exists()          # off the board
    assert (backlog / "_gc" / "T-0262.md").exists()      # archived (reversible)


def test_gc_archives_closed_throwaway_immediately(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "T-0900", title="QA-TEST-DELETEME-x",
                status="closed", age_sec=0, now=now)  # fresh but closed
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert "T-0900" in out["archived"]


def test_gc_leaves_fresh_open_throwaway(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "T-0901", title="QA-TEST-DELETEME-in-progress",
                status="open", age_sec=60, now=now)  # 1 min old < grace
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert out["archived"] == []
    assert (tmp_path / "proj" / "backlog" / "T-0901.md").exists()


def test_gc_leaves_normal_tasks_even_if_old(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "T-0100", title="Real feature ticket",
                status="open", age_sec=999999, now=now)
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert out["archived"] == []
    assert (tmp_path / "proj" / "backlog" / "T-0100.md").exists()


def test_gc_ignores_a_normal_task_that_mentions_the_marker_in_body(tmp_path):
    now = 1_000_000.0
    _write_task(tmp_path, "F7-feat", title="Add auto-GC of throwaway tasks",
                status="open", body="We purge QA-TEST-DELETEME tasks.",
                age_sec=999999, now=now)
    out = task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj", now=now)
    assert out["archived"] == []


def test_gc_no_backlog_is_safe(tmp_path):
    assert task_gc.gc_throwaway_tasks(_cfg(tmp_path), "proj")["archived"] == []


# === T-0484: general task cleanup (stale + duplicate) =======================
# Generalizes the QA-throwaway GC into a real cleanup process: (a) age-based
# archival of stale/abandoned tasks, (b) duplicate detection + merge. Both are
# reversible (off-board archive to backlog/_gc/). The existing throwaway GC
# (tested above) is untouched.

# --- stale detection (pure) -------------------------------------------------

def test_is_stale_open_task_past_grace():
    grace = task_gc.stale_task_gc_sec()
    assert task_gc.is_stale_task({"status": "open"}, grace + 1) is True
    assert task_gc.is_stale_task({"status": "planned"}, grace + 1) is True
    assert task_gc.is_stale_task({"status": "reopened"}, grace + 1) is True


def test_is_stale_active_task_never_stale():
    # in-progress / totest tasks are being worked — age must not reap them.
    grace = task_gc.stale_task_gc_sec()
    assert task_gc.is_stale_task({"status": "in_progress"}, grace * 10) is False
    assert task_gc.is_stale_task({"status": "totest"}, grace * 10) is False


def test_is_stale_closed_task_never_stale():
    grace = task_gc.stale_task_gc_sec()
    assert task_gc.is_stale_task({"status": "closed"}, grace * 10) is False


def test_is_stale_fresh_open_task_not_stale():
    assert task_gc.is_stale_task({"status": "open"}, 60) is False


def test_is_stale_skips_throwaway():
    # throwaway tasks have their own faster GC path — don't double-handle.
    grace = task_gc.stale_task_gc_sec()
    assert task_gc.is_stale_task({"status": "open", "throwaway": True}, grace + 1) is False


# --- stale find + age-based archive -----------------------------------------

def test_find_stale_tasks_reports_only_aged_inactive(tmp_path):
    now = 1_000_000.0
    grace = task_gc.stale_task_gc_sec()
    _write_task(tmp_path, "T-0500", title="Old abandoned task",
                status="open", age_sec=grace + 100, now=now)
    _write_task(tmp_path, "T-0501", title="Fresh task",
                status="open", age_sec=60, now=now)
    _write_task(tmp_path, "T-0502", title="Active task",
                status="in_progress", age_sec=grace + 100, now=now)
    out = task_gc.find_stale_tasks(_cfg(tmp_path), "proj", now=now)
    ids = {s["id"] for s in out["stale"]}
    assert ids == {"T-0500"}


def test_gc_stale_tasks_archives_reversibly(tmp_path):
    now = 1_000_000.0
    grace = task_gc.stale_task_gc_sec()
    _write_task(tmp_path, "T-0500", title="Old abandoned task",
                status="open", age_sec=grace + 100, now=now)
    out = task_gc.gc_stale_tasks(_cfg(tmp_path), "proj", now=now)
    assert "T-0500" in out["archived"]
    backlog = tmp_path / "proj" / "backlog"
    assert not (backlog / "T-0500.md").exists()        # off the board
    assert (backlog / "_gc" / "T-0500.md").exists()    # archived (reversible)


def test_gc_stale_tasks_leaves_active_and_fresh(tmp_path):
    now = 1_000_000.0
    grace = task_gc.stale_task_gc_sec()
    _write_task(tmp_path, "T-0502", title="Active task",
                status="in_progress", age_sec=grace + 100, now=now)
    _write_task(tmp_path, "T-0501", title="Fresh task",
                status="open", age_sec=60, now=now)
    out = task_gc.gc_stale_tasks(_cfg(tmp_path), "proj", now=now)
    assert out["archived"] == []
    backlog = tmp_path / "proj" / "backlog"
    assert (backlog / "T-0502.md").exists()
    assert (backlog / "T-0501.md").exists()


# --- duplicate detection (suggestion) + merge -------------------------------

def test_find_duplicate_tasks_groups_by_normalized_title(tmp_path):
    _write_task(tmp_path, "T-0600", title="Fix the login bug", status="open")
    _write_task(tmp_path, "T-0601", title="fix the   LOGIN  bug!", status="open")
    _write_task(tmp_path, "T-0602", title="Unrelated work", status="open")
    out = task_gc.find_duplicate_tasks(_cfg(tmp_path), "proj")
    groups = out["duplicates"]
    assert len(groups) == 1
    g = groups[0]
    assert set(g["ids"]) == {"T-0600", "T-0601"}
    assert g["suggested_keep"] == "T-0600"  # lowest numeric id = the original


def test_find_duplicate_tasks_ignores_throwaway(tmp_path):
    _write_task(tmp_path, "T-0610", title="QA-TEST-DELETEME-x", status="open")
    _write_task(tmp_path, "T-0611", title="QA-TEST-DELETEME-x", status="open")
    out = task_gc.find_duplicate_tasks(_cfg(tmp_path), "proj")
    assert out["duplicates"] == []


def test_merge_tasks_archives_dups_and_annotates_keeper(tmp_path):
    _write_task(tmp_path, "T-0600", title="Fix the login bug", status="open")
    _write_task(tmp_path, "T-0601", title="fix the login bug", status="open")
    out = task_gc.merge_tasks(_cfg(tmp_path), "proj", "T-0600", ["T-0601"])
    assert out["kept"] == "T-0600"
    assert "T-0601" in out["merged"]
    backlog = tmp_path / "proj" / "backlog"
    # dup off the board, preserved in _gc (reversible)
    assert not (backlog / "T-0601.md").exists()
    archived = backlog / "_gc" / "T-0601.md"
    assert archived.exists()
    assert "merged_into" in archived.read_text(encoding="utf-8")
    # keeper carries a back-reference so the merge is discoverable + reversible
    assert "T-0601" in (backlog / "T-0600.md").read_text(encoding="utf-8")


def test_merge_tasks_reports_missing_ids(tmp_path):
    _write_task(tmp_path, "T-0600", title="Fix the login bug", status="open")
    out = task_gc.merge_tasks(_cfg(tmp_path), "proj", "T-0600", ["T-9999"])
    assert out["merged"] == []
    assert "T-9999" in out["missing"]


# ---------------------------------------------------------------------------
# T-0889 — auto-pause: a ticket at in_progress that no live session holds
# ---------------------------------------------------------------------------
# His ask, 2026-08-04: "When all sessions working on a ticket are terminated,
# the ticket is automatically passed to another 'started' / 'paused' status".
# Two conditions gate it and each has its own test below: NOBODY HOLDS IT (the
# binding) and NOBODY HAS TOUCHED IT (the grace) — the second because a session
# that recycles loses its task binding while its successor keeps working, which
# is not hypothetical: it was true of T-0889 itself on the day this was built.

NOW = 1_000_000.0
FAR = 5 * 3600  # older than the 4h default grace


def _write_session(tmp_path: Path, sid: str, *, status: str = "active",
                   role: str = "dev", task_id: str = "~",
                   extra_task_ids: str = "[]", archived: str | None = None):
    sess = tmp_path / "proj" / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    fm = (f"---\nsid: {sid}\nstatus: {status}\nwindow: w-{sid}\n"
          f"role: {role}\ntask_id: {task_id}\nextra_task_ids: {extra_task_ids}\n")
    if archived is not None:
        fm += f"archived: '{archived}'\n"
    fm += "---\n\nbody\n"
    p = sess / f"{sid}.md"
    p.write_text(fm, encoding="utf-8")
    return p


def _status_of(p: Path) -> str:
    from bot_squad_worker import frontmatter as fm
    return str((fm.parse_or_none(p.read_text(encoding="utf-8"))[0] or {}).get("status"))


def test_auto_pause_moves_an_unheld_in_progress_ticket(tmp_path):
    """The whole feature in one case: in_progress + no live holder + past the
    grace -> paused, with the reason left in the Progress feed."""
    p = _write_task(tmp_path, "T-0551", title="Real work", status="in_progress",
                    age_sec=FAR, now=NOW)
    out = task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)
    assert out["paused"] == ["T-0551"]
    assert _status_of(p) == "paused"
    body = p.read_text(encoding="utf-8")
    assert "## Progress" in body
    assert "auto-paused" in body


def test_auto_pause_stamps_updated(tmp_path):
    """The board sorts and reports on ``updated``; a status change nobody
    stamped would read as untouched since its last human edit."""
    p = _write_task(tmp_path, "T-0551", title="Real work", status="in_progress",
                    age_sec=FAR, now=NOW)
    task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)
    from bot_squad_worker import frontmatter as fm
    meta = fm.parse_or_none(p.read_text(encoding="utf-8"))[0]
    assert meta["updated"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW))


def test_auto_pause_spares_a_ticket_a_live_session_holds(tmp_path):
    p = _write_task(tmp_path, "T-0883", title="Held", status="in_progress",
                    age_sec=FAR, now=NOW)
    _write_session(tmp_path, "S-dev-1", task_id="T-0883")
    out = task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)
    assert out["paused"] == []
    assert _status_of(p) == "in_progress"


def test_auto_pause_spares_a_ticket_held_only_via_extra_task_ids(tmp_path):
    """A dev's bundled tickets live in ``extra_task_ids``, not ``task_id`` —
    reading the primary alone would pause every bundled ticket under a live dev."""
    p = _write_task(tmp_path, "T-0885", title="Bundled", status="in_progress",
                    age_sec=FAR, now=NOW)
    _write_session(tmp_path, "S-dev-1", task_id="T-0001",
                   extra_task_ids="[T-0885, T-0887]")
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == []
    assert _status_of(p) == "in_progress"


def test_auto_pause_spares_a_ticket_held_by_a_NON_dev_session(tmp_path):
    """Role-agnostic by design: his words are "all sessions working on a
    ticket", so a TL or user-conversation holder counts. The dev-only set the
    routing gate uses would have paused this one."""
    p = _write_task(tmp_path, "T-0612", title="TL-held", status="in_progress",
                    age_sec=FAR, now=NOW)
    _write_session(tmp_path, "S-tl-1", role="teamlead", task_id="T-0612")
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == []


def test_auto_pause_counts_a_PAUSED_session_as_a_holder(tmp_path):
    """``_is_live_holder`` counts active AND paused sessions — a parked process
    still holds its task. (Session ``paused`` and ticket ``paused`` are two
    different vocabularies; this is the session one.)"""
    _write_task(tmp_path, "T-0612", title="x", status="in_progress", age_sec=FAR, now=NOW)
    _write_session(tmp_path, "S-dev-1", status="paused", task_id="T-0612")
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == []


def test_auto_pause_ignores_a_suspended_holder(tmp_path):
    """The point of the feature: the session that held it is GONE. A suspended
    md is the reaped session's historical record, not a holder."""
    p = _write_task(tmp_path, "T-0551", title="x", status="in_progress",
                    age_sec=FAR, now=NOW)
    _write_session(tmp_path, "S-dev-dead", status="suspended", task_id="T-0551")
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == ["T-0551"]
    assert _status_of(p) == "paused"


def test_auto_pause_ignores_an_archived_holder(tmp_path):
    _write_task(tmp_path, "T-0551", title="x", status="in_progress", age_sec=FAR, now=NOW)
    _write_session(tmp_path, "S-dev-old", status="active", task_id="T-0551",
                   archived="true")
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == ["T-0551"]


def test_auto_pause_withholds_inside_the_grace(tmp_path):
    """The measured case: T-0889 itself was in_progress, actively being built,
    and held by NO live session (its holder had recycled and the successor
    inherited no binding). The recent WRITE is what spares it."""
    p = _write_task(tmp_path, "T-0889", title="Being worked right now",
                    status="in_progress", age_sec=60, now=NOW)
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == []
    assert _status_of(p) == "in_progress"


def test_auto_pause_grace_is_env_tunable(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_AUTO_PAUSE_GRACE_SEC", "30")
    _write_task(tmp_path, "T-0889", title="x", status="in_progress", age_sec=60, now=NOW)
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == ["T-0889"]


def test_auto_pause_grace_ignores_garbage_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_AUTO_PAUSE_GRACE_SEC", "not-a-number")
    assert task_gc.auto_pause_grace_sec() == task_gc.DEFAULT_AUTO_PAUSE_GRACE_SEC


def test_auto_pause_moves_ONLY_in_progress(tmp_path):
    """``totest`` is the one that matters: a session ending there is a HANDOFF
    to a verifier, not an abandonment, and pausing it would recall shipped work
    into the doer band."""
    for status in ("open", "planned", "reopened", "totest", "closed", "paused"):
        _write_task(tmp_path, f"T-{status}", title="x", status=status,
                    age_sec=FAR, now=NOW)
    out = task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)
    assert out["paused"] == []
    backlog = tmp_path / "proj" / "backlog"
    for status in ("open", "planned", "reopened", "totest", "closed", "paused"):
        assert _status_of(backlog / f"T-{status}.md") == status


def test_auto_pause_is_idempotent(tmp_path):
    """Second tick must be a no-op — a ticket that keeps re-announcing itself
    would spam the Progress feed and re-stamp ``updated`` every 60s."""
    p = _write_task(tmp_path, "T-0551", title="x", status="in_progress",
                    age_sec=FAR, now=NOW)
    first = task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)
    after_first = p.read_text(encoding="utf-8")
    second = task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW + 600)
    assert first["paused"] == ["T-0551"] and second["paused"] == []
    assert p.read_text(encoding="utf-8") == after_first


def test_auto_pause_skips_throwaway_and_archived_tickets(tmp_path):
    _write_task(tmp_path, "T-0900", title="QA-TEST-DELETEME-x", status="in_progress",
                age_sec=FAR, now=NOW)
    backlog = tmp_path / "proj" / "backlog"
    (backlog / "T-0901.md").write_text(
        "---\nid: T-0901\ntitle: x\nstatus: in_progress\narchived: 'true'\n---\n\n",
        encoding="utf-8")
    os.utime(backlog / "T-0901.md", (NOW - FAR, NOW - FAR))
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == []


def test_auto_pause_preserves_non_canonical_body_sections(tmp_path):
    """T-0729's lesson: a parse->compose round-trip would delete ``## DoD`` and
    every other non-canonical section. ``append_progress`` splices."""
    body = "## Verbatim request\n\nhis words\n\n## DoD\n\n- [ ] a thing\n"
    p = _write_task(tmp_path, "T-0551", title="x", status="in_progress",
                    body=body, age_sec=FAR, now=NOW)
    task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)
    after = p.read_text(encoding="utf-8")
    assert "## DoD" in after and "- [ ] a thing" in after and "his words" in after


def test_auto_pause_survives_an_unparseable_ticket(tmp_path):
    """One bad file must not cost the rest of the sweep — the tick runs every
    60s over every project."""
    backlog = tmp_path / "proj" / "backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / "T-bad.md").write_text("no frontmatter here\n", encoding="utf-8")
    _write_task(tmp_path, "T-0551", title="x", status="in_progress", age_sec=FAR, now=NOW)
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)["paused"] == ["T-0551"]


def test_auto_pause_no_backlog_dir_is_a_noop(tmp_path):
    assert task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "nosuch", now=NOW) == {"paused": []}


def test_live_held_task_ids_unions_primary_and_extras_across_sessions(tmp_path):
    _write_session(tmp_path, "S-a", task_id="T-1", extra_task_ids="[T-2]")
    _write_session(tmp_path, "S-b", task_id="T-3", extra_task_ids="[]")
    _write_session(tmp_path, "S-c", status="suspended", task_id="T-4")
    assert task_gc.live_held_task_ids(_cfg(tmp_path), "proj") == {"T-1", "T-2", "T-3"}


def test_live_held_task_ids_drops_the_unset_sentinel(tmp_path):
    """``task_id: ~`` is the UNSET sentinel — a session holding nothing. Left in,
    it would be a task id no ticket has, which is harmless, but it also masks
    the real question this set answers."""
    _write_session(tmp_path, "S-a", task_id="~")
    assert task_gc.live_held_task_ids(_cfg(tmp_path), "proj") == set()


def test_auto_pause_kill_switch_stops_the_pass(tmp_path, monkeypatch):
    """The one pass in the tick that changes what HE sees needs an off switch
    that is not a revert. Off means "stop moving them" — nothing is moved back."""
    monkeypatch.setenv("BOT_SQUAD_AUTO_PAUSE", "0")
    p = _write_task(tmp_path, "T-0551", title="x", status="in_progress",
                    age_sec=FAR, now=NOW)
    out = task_gc.auto_pause_unheld_tasks(_cfg(tmp_path), "proj", now=NOW)
    assert out == {"paused": [], "disabled": True}
    assert _status_of(p) == "in_progress"
    assert "auto-paused" not in p.read_text(encoding="utf-8")


def test_auto_pause_is_on_by_default_and_only_0_disables_it(tmp_path, monkeypatch):
    """Default ON (the feature he asked for must not need an env var to work),
    and a garbage value must not silently disable it — same shape as
    ``recovery.boot_reconcile_enabled``."""
    monkeypatch.delenv("BOT_SQUAD_AUTO_PAUSE", raising=False)
    assert task_gc.auto_pause_enabled() is True
    for value in ("1", "yes", "", "  "):
        monkeypatch.setenv("BOT_SQUAD_AUTO_PAUSE", value)
        assert task_gc.auto_pause_enabled() is True, value
    for value in ("0", " 0 "):
        monkeypatch.setenv("BOT_SQUAD_AUTO_PAUSE", value)
        assert task_gc.auto_pause_enabled() is False, value
