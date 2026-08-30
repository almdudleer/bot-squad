"""T-0333/T-0334: self-healing context compaction (closed loop).

When a session crosses the context ceiling, the SYSTEM /compacts it — but ONLY
when its pane is idle (Claude finished its turn, awaiting input) and the composer
is ready (❯), never mid-turn. The operator session is NOT exempt (T-0334).
"""
from __future__ import annotations

import time
import types

import pytest

from bot_squad_worker import autocompact as A
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import sessions as S
# T-0649: reuse idle_timeout's tmp cfg + session-md harness rather than
# duplicating it — the ceiling compact-and-stay path drives the exact same
# session-md fields idle_timeout's own compact-and-stay suite exercises.
from tests.test_idle_timeout import _make_cfg


# --- pure decision helpers --------------------------------------------------

def test_compact_safe_only_when_idle():
    assert A.compact_safe("idle") is True
    # never interrupt active work, a Ctrl-C'd pane, or a paneless session
    assert A.compact_safe("running") is False
    assert A.compact_safe("paused") is False
    assert A.compact_safe("suspended") is False
    assert A.compact_safe("") is False


def test_compact_due_requires_urgent_level_and_respects_cooldown():
    # below the ceiling → never auto-compact
    assert A.compact_due("none", {}, now=1000.0) is False
    assert A.compact_due("warn", {}, now=1000.0) is False
    # at the ceiling, never compacted before → due
    assert A.compact_due("urgent", {}, now=1000.0) is True
    # just compacted → cooldown suppresses a re-/compact
    fired = {"compact": 1000.0}
    assert A.compact_due("urgent", fired, now=1000.0 + 10, cooldown=600) is False
    # cooldown elapsed → due again
    assert A.compact_due("urgent", fired, now=1000.0 + 601, cooldown=600) is True


def test_composer_ready_pure():
    assert A.composer_ready("some output\n❯ \n") is True
    assert A.composer_ready("") is False
    assert A.composer_ready("just bash $ no rune") is False
    # mid-generation marker → not safe even if a stale ❯ lingers
    assert A.composer_ready("❯\n· Working… (esc to interrupt)") is False


# --- T-0864 DoD 3: composer_ready was as silent as recycle_gate.is_attached ---

def test_composer_ready_verdicts_unchanged_when_a_sid_is_passed():
    """The T-0864 logging is additive — sid/now must not move any verdict."""
    for buf in ("some output\n❯ \n", "", "just bash $ no rune",
                "❯\n· Working… (esc to interrupt)"):
        assert (A.composer_ready(buf, sid="S-alpha", now=1000.0)
                is A.composer_ready(buf))


def test_composer_ready_logs_a_debounced_skip_naming_the_reason(caplog):
    from bot_squad_worker import recycle_gate
    recycle_gate._last_skip_log.clear()
    caplog.set_level("INFO")
    # a permission dialog / stale render: no ❯ on screen
    assert A.composer_ready("[y/n]?", sid="S-alpha", now=1000.0) is False
    assert A.composer_ready("[y/n]?", sid="S-alpha", now=1000.1) is False  # same tick
    assert A.composer_ready("❯ · Working… (esc to interrupt)",
                            sid="S-alpha", now=1035.0) is False  # window elapsed
    lines = [r.getMessage() for r in caplog.records
             if "not composer-ready" in r.getMessage()]
    assert len(lines) == 2
    assert "S-alpha" in lines[0] and "❯" in lines[0]
    assert "mid-generation" in lines[1]


def test_composer_ready_logs_nothing_without_a_sid(caplog):
    """input_mux reuses this predicate per keystroke batch to decide whether a
    pane can be typed into — a "not ready" there is the normal case, not a
    deferred recycle, and must not fill the log."""
    from bot_squad_worker import recycle_gate
    recycle_gate._last_skip_log.clear()
    caplog.set_level("INFO")
    assert A.composer_ready("") is False
    assert A.composer_ready("❯ (esc to interrupt)") is False
    assert [r for r in caplog.records if "composer-ready" in r.getMessage()] == []
    assert recycle_gate._last_skip_log == {}


def test_autocompact_enabled_default_on_kill_switch_off(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_AUTOCOMPACT", raising=False)
    assert A.autocompact_enabled() is True
    monkeypatch.setenv("BOT_SQUAD_AUTOCOMPACT", "0")
    assert A.autocompact_enabled() is False


# --- executor (maybe_compact) -----------------------------------------------

@pytest.fixture
def harness(monkeypatch):
    """Stub pane resolution / capture / inject so no tmux is touched.

    These exercise the legacy Claude ``/compact`` path + the safety gates shared
    by both strategies, so we pin ``compact_mode`` to ``claude`` (the T-0467
    write-to-artifact handoff has its own suite in test_compact_handoff.py)."""
    sent: list[str] = []
    state = {"pane": "%9", "buf": "❯ \n"}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: sent.append(sid))
    monkeypatch.setattr(A, "autocompact_enabled", lambda: True)
    monkeypatch.setattr(A, "compact_mode", lambda: "claude")
    # T-0563/T-0564: these tests use slug "proj" with cfg=None and a fake pane
    # id — allowlist it and stub the tmux attach check so the new shared gate
    # (recycle_gate.recycle_allowed) doesn't touch real tmux or the default
    # bot-squad-only allowlist. The gate itself has its own test module.
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "proj")
    monkeypatch.setattr(A.recycle_gate, "is_attached", lambda target, **kw: False)
    return {"sent": sent, "state": state}


def _rec(sid="S-almdudleer-dev-p5", activity="idle", fired=None, role=None):
    return {"sid": sid, "activity": activity, "alert_fired_at": dict(fired or {}),
            "role": role}


def test_maybe_compact_sends_when_idle_urgent_and_composer_ready(harness):
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is True
    assert harness["sent"] == ["S-almdudleer-dev-p5"]
    assert rec["alert_fired_at"]["compact"] == 1000.0  # cooldown stamped


def test_maybe_compact_skips_when_not_idle(harness):
    rec = _rec(activity="running")
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_maybe_compact_skips_below_threshold(harness):
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "warn", now=1000.0) is False
    assert harness["sent"] == []


def test_maybe_compact_skips_when_composer_not_ready(harness):
    harness["state"]["buf"] = "· Working… (esc to interrupt)"
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_maybe_compact_skips_within_cooldown(harness, monkeypatch):
    monkeypatch.setattr(A, "compact_cooldown_sec", lambda: 600)
    rec = _rec(fired={"compact": 1000.0})
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0 + 5) is False
    assert harness["sent"] == []


def test_maybe_compact_skips_when_no_live_pane(harness):
    harness["state"]["pane"] = None
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_maybe_compact_kill_switch(harness, monkeypatch):
    monkeypatch.setattr(A, "autocompact_enabled", lambda: False)
    rec = _rec()
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_operator_session_is_not_exempt(harness):
    """T-0334: the operator session is a first-class member of the loop."""
    rec = _rec(sid="S-almdudleer-bot-squad-operator-p5", activity="idle")
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is True
    assert harness["sent"] == ["S-almdudleer-bot-squad-operator-p5"]


# --- T-0563/T-0564: recycle-v2 gates ----------------------------------------

def test_non_allowlisted_project_is_never_compacted(harness, monkeypatch):
    """T-0563: the 2026-06-29 incident's fix — a non-allowlisted project is
    NEVER auto-/compact-ed (watchrobot itself joined the default allowlist in
    T-0613, so the example is any OTHER project)."""
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)  # undo harness override
    rec = _rec()
    assert A.maybe_compact(None, "other-project", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_user_conversation_role_never_gets_the_handoff_path(harness):
    """T-0564/T-0649: the human's own live chat never rides the
    handoff/terminate machinery — with no resolvable session md (cfg=None
    here) the T-0649 compact-in-place branch also fails closed, so nothing is
    sent. See test_exempt_session_ceiling_compacts_in_place below for the
    real (cfg-backed) compact-in-place behavior this session now gets
    instead of "never touched"."""
    rec = _rec(role="user-conversation")
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is False
    assert harness["sent"] == []


def test_attached_pane_compacts_in_place_but_never_without_stay(harness, monkeypatch):
    """T-0564 -> T-0930: attachment used to block EVERYTHING; the stakeholder's
    2026-08-30 ruling splits it — an attended session may be compacted (he
    wants the context down even while he works), it must never be relaunched
    or typed-over. With the stay mode killed, the old hands-off rule returns
    wholesale, because the only remaining response would be the relaunch."""
    monkeypatch.setattr(A.recycle_gate, "is_attached", lambda target, **kw: True)
    rec = _rec()
    # stay ON (default): the attended pane IS compacted (typing-free composer)
    assert A.maybe_compact(None, "proj", rec, "urgent", now=1000.0) is True
    assert harness["sent"] == [rec["sid"]]
    # ...but never over his half-typed draft
    harness["sent"].clear()
    rec2 = _rec(sid="S-almdudleer-dev-p6")
    harness["state"]["buf"] = "❯ his draft\n"
    assert A.maybe_compact(None, "proj", rec2, "urgent", now=1000.0) is False
    assert harness["sent"] == []
    # stay OFF: attached blocks everything, exactly the pre-T-0930 behaviour
    harness["state"]["buf"] = "❯ \n"
    monkeypatch.setenv("BOT_SQUAD_CEILING_COMPACT_STAY", "0")
    rec3 = _rec(sid="S-almdudleer-dev-p7")
    assert A.maybe_compact(None, "proj", rec3, "urgent", now=1000.0) is False
    assert harness["sent"] == []


# --- T-0649: ceiling trigger for exempt sessions → compact-in-place ---------
#
# The human's own exempt sessions (recycle_gate.user_session_exempt) never
# ride the handoff/terminate machinery above. Their context-ceiling trigger
# instead reuses idle_timeout's T-0617 compact-and-stay state machine
# (compact_stay_phase/_armed_at/_last_at on the session md) — same fields,
# same anti-loop guard, so the ceiling and idle-window triggers can never
# double-compact one session. Needs a REAL cfg (not the cfg=None harness
# above) since it reads/writes the session md.

@pytest.fixture
def stay_harness(monkeypatch):
    """Mirrors ``harness`` but leaves cfg/session-md resolution real — the
    T-0649 ceiling compact-and-stay path needs an actual md_path to arm/
    finalize against."""
    sent: list[str] = []
    state = {"pane": "%9", "buf": "❯ \n"}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: sent.append(sid))
    monkeypatch.setattr(A, "autocompact_enabled", lambda: True)
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "bot-squad")
    monkeypatch.setattr(A.recycle_gate, "is_attached", lambda target, **kw: False)
    return {"sent": sent, "state": state}


def test_exempt_session_ceiling_compacts_in_place_no_suspend(tmp_path, stay_harness, monkeypatch):
    """T-0649 core behavior: an exempt session over the context ceiling gets
    a native /compact sent AND stays running — never sessions.suspend()."""
    sid = "S-almdudleer-operator-p9"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    suspended = []
    monkeypatch.setattr(S, "suspend", lambda *a, **k: suspended.append(a))

    rec = {"sid": sid, "activity": "idle", "role": "user-conversation"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is True
    assert stay_harness["sent"] == [sid]
    assert suspended == []

    meta = S._read_session_metadata(S._session_file(data, "bot-squad", sid))
    assert meta["compact_stay_phase"] == "compacting"
    assert "compact_stay_armed_at" in meta
    # T-0617's terminate-flow fields must never be touched by this path.
    assert "idle_recycle_phase" not in meta


def test_pinned_session_ceiling_never_compacts(tmp_path, stay_harness, monkeypatch):
    """T-0926 follow-up: a ``pinned: true`` session gets no ceiling action at
    all — unlike the plain exempt case above (still compacts in place), pin
    is a stronger "do not touch" override."""
    sid = "S-almdudleer-operator-p9"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"pinned": True})
    suspended = []
    monkeypatch.setattr(S, "suspend", lambda *a, **k: suspended.append(a))

    rec = {"sid": sid, "activity": "idle", "role": "user-conversation"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is False
    assert stay_harness["sent"] == []
    assert suspended == []

    meta = S._read_session_metadata(S._session_file(data, "bot-squad", sid))
    assert "compact_stay_phase" not in meta


def test_exempt_session_ceiling_skips_below_urgent(tmp_path, stay_harness):
    sid = "S-almdudleer-operator-p9"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    rec = {"sid": sid, "activity": "idle", "role": "user-conversation"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "warn", now=1000.0) is False
    assert stay_harness["sent"] == []


def test_exempt_session_ceiling_finalize_never_suspends(tmp_path, stay_harness, monkeypatch):
    """A ceiling-armed compact-and-stay finalizes like idle_timeout's own —
    clears the phase, stamps compact_stay_last_at, never terminates."""
    sid = "S-almdudleer-operator-p9"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1000.0 - 30))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_phase": "compacting",
                                    "compact_stay_armed_at": armed})
    suspended = []
    monkeypatch.setattr(S, "suspend", lambda *a, **k: suspended.append(a))

    rec = {"sid": sid, "activity": "idle", "role": "user-conversation"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is True
    assert stay_harness["sent"] == []  # nothing re-sent — just finalizing
    assert suspended == []

    meta = S._read_session_metadata(S._session_file(data, "bot-squad", sid))
    assert "compact_stay_phase" not in meta
    assert "compact_stay_armed_at" not in meta
    assert "compact_stay_last_at" in meta


def test_exempt_session_ceiling_respects_shared_anti_loop_guard(tmp_path, stay_harness):
    """T-0649's whole point: an idle-window compact-and-stay that already
    finalized THIS cache window blocks the ceiling trigger from re-arming —
    and vice versa, since both read/write compact_stay_last_at."""
    sid = "S-almdudleer-operator-p9"
    just_finalized = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1000.0 - 10))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_last_at": just_finalized})
    rec = {"sid": sid, "activity": "idle", "role": "user-conversation"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is False
    assert stay_harness["sent"] == []


def test_exempt_session_ceiling_rearms_once_window_elapsed(tmp_path, stay_harness):
    sid = "S-almdudleer-operator-p9"
    long_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(1000.0 - IT.idle_timeout_sec() - 1))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_last_at": long_ago})
    rec = {"sid": sid, "activity": "idle", "role": "user-conversation"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is True
    assert stay_harness["sent"] == [sid]


def test_hand_launched_user_session_ceiling_also_compacts_in_place(tmp_path, stay_harness):
    """T-0616's window-segment exemption (not just role) is honored on the
    ceiling path too."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    rec = {"sid": sid, "activity": "idle", "role": "dev"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is True
    assert stay_harness["sent"] == [sid]


def test_worker_session_ceiling_unaffected_by_exempt_path(tmp_path, stay_harness, monkeypatch):
    """T-0649 explicitly scopes worker (dev/TL/operator) sessions OUT — a
    non-exempt session over the ceiling still tries the handoff/legacy
    /compact path, never the compact-and-stay one (no role artifact resolves
    here, so it falls to the legacy claude /compact — but critically it
    never touches compact_stay_* fields)."""
    sid = "S-almdudleer-dev-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id=None)
    monkeypatch.setattr(A, "compact_mode", lambda: "claude")
    rec = {"sid": sid, "activity": "idle", "role": "dev"}
    assert A.maybe_compact(cfg, "bot-squad", rec, "urgent", now=1000.0) is True
    assert stay_harness["sent"] == [sid]

    meta = S._read_session_metadata(S._session_file(data, "bot-squad", sid))
    assert "compact_stay_phase" not in meta
    assert "compact_stay_last_at" not in meta
