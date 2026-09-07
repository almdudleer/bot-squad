"""T-1060 — a system wake must not reset the recycle clock.

THE DEFECT, as measured on the live install (2026-09-05/06). ``uc_redrive``
re-drives an attendant whose reply turn died, on the stakeholder's cadence:
~5 min, ~15 min, then every ~30 for as long as the message hangs (T-0794).
Every one of those nudges is a REAL turn — the Stop hook fires and
``idle_timeout._idle_age``'s primary signal resets. The steady cadence
(``uc_redrive.steady_ping_sec()`` = 1800s) is SHORTER than the recycle window
(``idle_timeout.idle_timeout_sec()`` = 3300s), so an attendant under an open
campaign is pinned permanently below its deadline — always, because those two
numbers alone decide it.

The stakeholder's watchrobot attendant took 79 consecutive turns spaced
1787-1796s apart across 39 hours, one 6-8s after each ``uc_redrive: re-woke
idle attendant`` journal line: a maximum idle age of 1796s against a 3300s
window. It could therefore never reach ``PLAN_COMPACT_EXIT``, so it never took
a DELIBERATE suspend, and all six SessionMds ever written for that gid carry
``suspend_source: gc_sessions`` — the forensic "no live pane" flip, which
preserves whatever stale ``claude_uuid`` was already on the record. That ghost
is exactly the input T-1053 had to work around, and T-1053's fix (prefer a
deliberate suspend over a ghost) can never engage for an attendant that has no
deliberate suspend to prefer.

THE FIX is the system-wake anchor: the waker preserves the idle moment it is
about to overwrite, once per campaign, and ``_idle_age`` returns the OLDER of
that and the live reading. Every test below is paired with the control that
fails without it — a cadence test whose control does not pin would be pinning
nothing.
"""
from __future__ import annotations

import os
import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import actions as A
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import lifecycle_events as LE
from bot_squad_worker import sessions as S
from bot_squad_worker import uc_redrive as U

SID = "S-almdudleer-gu_dc8262b6cea9098d98e04d7e-user-conversation-p727"
GID = "gu_dc8262b6cea9098d98e04d7e"
SLUG = "watchrobot"


def _attendant(tmp_path: Path, **extra) -> tuple[types.SimpleNamespace, Path, Path]:
    """A real SessionMd + hook-marker dir for the stakeholder's attendant."""
    data = tmp_path / "data"
    repo = tmp_path / "clone"
    (data / SLUG / "sessions").mkdir(parents=True)
    repo.mkdir()
    fm = {"sid": SID, "status": "active", "window": f"{GID}-user-conversation",
          "cwd": str(repo), "claude_uuid": "7ea64910", "role": "user-conversation",
          "global_user_id": GID}
    fm.update(extra)
    md = data / SLUG / "sessions" / f"{SID}.md"
    S._write_session_metadata(md, fm)
    cfg = types.SimpleNamespace(data_dir=data, projects={SLUG: object()})
    return cfg, md, repo


def _turn_ended(repo: Path, at: float) -> None:
    """The Stop hook firing: the marker's mtime IS the idle anchor."""
    LE.touch_marker(str(repo), SID, LE.MARKER_STOP)
    os.utime(LE.marker_path(str(repo), SID, LE.MARKER_STOP), (at, at))


def _idle(md: Path, now: float) -> float | None:
    return IT._idle_age({}, S._read_session_metadata(md), "/home/x", now)


# --- the reproduction, with its control ------------------------------------

def test_the_redrive_cadence_is_shorter_than_the_recycle_window():
    """The two numbers that decide it, asserted rather than assumed.

    If either default ever moves so that a campaign can no longer outpace the
    window, the pin this whole file is about stops existing — and the anchor
    tests below would be pinning a hazard that is gone. This is the assumption
    they rest on, stated where it fails loudly.
    """
    assert U.steady_ping_sec() < IT.idle_timeout_sec()


def test_a_redrive_campaign_no_longer_pins_the_attendant_below_its_deadline(tmp_path):
    """Nudged on uc_redrive's cadence, the attendant STILL reaches its window."""
    cfg, md, repo = _attendant(tmp_path)
    t0 = time.time() - 10_000          # his last real turn
    _turn_ended(repo, t0)
    window, ping = IT.idle_timeout_sec(), U.steady_ping_sec()

    now = t0
    for _ in range(10):
        now += ping
        IT.mark_system_wake(cfg, SLUG, SID, now=now)   # stamped BEFORE the nudge
        _turn_ended(repo, now + 8)                     # the nudge's own turn
        if IT.idle_due(_idle(md, now + 30), window):
            break
    else:                                              # pragma: no cover
        pytest.fail("the attendant never came due under the anchor")

    # It came due at the FIRST tick past the window, measured from HIS turn —
    # not from the nudge that happened seconds earlier.
    assert now - t0 >= window
    assert now - t0 < window + ping


def test_control_without_the_anchor_it_is_pinned_forever(tmp_path):
    """The same clock, same cadence, no anchor: never due — the live defect.

    Without this arm the test above would pass on a session that was never
    pinned in the first place.
    """
    cfg, md, repo = _attendant(tmp_path)
    t0 = time.time() - 10_000
    _turn_ended(repo, t0)
    window, ping = IT.idle_timeout_sec(), U.steady_ping_sec()

    now = t0
    ages = []
    for _ in range(10):
        now += ping
        _turn_ended(repo, now + 8)                     # nudge, but NO anchor
        ages.append(_idle(md, now + 30))
    assert max(ages) < window
    assert not any(IT.idle_due(a, window) for a in ages)


# --- the anchor's own properties -------------------------------------------

def test_idle_age_returns_the_anchor_over_a_reset_hook_marker(tmp_path):
    cfg, md, repo = _attendant(tmp_path)
    now = time.time()
    _turn_ended(repo, now - 4000)
    IT.mark_system_wake(cfg, SLUG, SID, now=now)
    _turn_ended(repo, now)                             # the wake's turn
    assert _idle(md, now) == pytest.approx(4000, abs=2)


def test_a_turn_in_progress_is_never_reported_as_idle(tmp_path):
    """The hook signal's 0.0 means A TURN IS RUNNING (an `.active` marker at or
    after the Stop). The anchor must not speak over it — otherwise a session
    mid-answer reads as idle for an hour and gets recycled out from under its
    own reply, which on THIS role is the stakeholder's live conversation."""
    cfg, md, repo = _attendant(tmp_path)
    now = time.time()
    _turn_ended(repo, now - 4000)
    IT.mark_system_wake(cfg, SLUG, SID, now=now)
    LE.touch_marker(str(repo), SID, LE.MARKER_ACTIVE)      # a turn started
    os.utime(LE.marker_path(str(repo), SID, LE.MARKER_ACTIVE), (now, now))
    assert _idle(md, now) == 0.0
    assert IT.idle_due(_idle(md, now), IT.idle_timeout_sec()) is False


def test_a_second_wake_does_not_move_the_anchor(tmp_path):
    """One-directional by construction: every later nudge in the same campaign
    is exactly what must not advance the clock."""
    cfg, md, repo = _attendant(tmp_path)
    now = time.time()
    _turn_ended(repo, now - 4000)
    assert IT.mark_system_wake(cfg, SLUG, SID, now=now) is True
    first = S._read_session_metadata(md)[IT.SYSTEM_WAKE_ANCHOR_FIELD]

    _turn_ended(repo, now)
    assert IT.mark_system_wake(cfg, SLUG, SID, now=now + 1800) is False
    assert S._read_session_metadata(md)[IT.SYSTEM_WAKE_ANCHOR_FIELD] == first
    assert _idle(md, now + 1800) == pytest.approx(5800, abs=2)


def test_the_anchor_can_never_make_a_session_look_busier(tmp_path):
    """A NEWER anchor than the live reading must not shorten the idle age —
    the combination is ``max``, not ``replace``."""
    cfg, md, repo = _attendant(tmp_path)
    now = time.time()
    _turn_ended(repo, now - 4000)                      # genuinely idle 4000s
    meta = S._read_session_metadata(md)
    meta[IT.SYSTEM_WAKE_ANCHOR_FIELD] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))   # a much younger anchor
    S._write_session_metadata(md, meta)
    assert _idle(md, now) == pytest.approx(4000, abs=2)


def test_the_anchor_also_backs_the_jsonl_fallback(tmp_path, monkeypatch):
    """No hook marker yet (pre-hook / brand-new session) — the fallback path
    reads the anchor too, or a marker-less attendant stays pinned."""
    cfg, md, repo = _attendant(tmp_path)
    now = time.time()
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: now)  # fresh jsonl
    meta = S._read_session_metadata(md)
    meta[IT.SYSTEM_WAKE_ANCHOR_FIELD] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 4000))
    S._write_session_metadata(md, meta)
    assert _idle(md, now) == pytest.approx(4000, abs=2)
    # ...and with no readable activity at all, the anchor is the only reading.
    monkeypatch.setattr(S, "_pane_activity_at", lambda *a, **k: None)
    assert _idle(md, now) == pytest.approx(4000, abs=2)


@pytest.mark.parametrize("raw", ["", "~", "not-a-timestamp", "2026-13-45"])
def test_an_unreadable_anchor_simply_does_not_constrain_the_clock(tmp_path, raw):
    cfg, md, repo = _attendant(tmp_path, system_wake_anchor_at=raw)
    now = time.time()
    _turn_ended(repo, now - 10)
    assert IT.system_wake_anchor_age(S._read_session_metadata(md), now) is None
    assert _idle(md, now) == pytest.approx(10, abs=2)


def test_mark_is_a_no_op_when_the_md_is_gone(tmp_path):
    cfg = types.SimpleNamespace(data_dir=tmp_path / "data",
                                projects={SLUG: object()})
    assert IT.mark_system_wake(cfg, SLUG, SID, now=time.time()) is False


def test_clear_reports_whether_there_was_anything_to_clear():
    meta = {"system_wake_anchor_at": "2026-09-07T00:00:00Z"}
    assert IT.clear_system_wake_anchor(meta) is True
    assert IT.SYSTEM_WAKE_ANCHOR_FIELD not in meta
    assert IT.clear_system_wake_anchor(meta) is False


# --- who sets it, and who clears it ----------------------------------------

def _ensure_cfg(tmp_path, monkeypatch):
    cfg, md, repo = _attendant(tmp_path)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "live_user_conversation_sid",
                        lambda cfg, slug, gid: SID)
    monkeypatch.setattr(A, "_action_inject_input", lambda params: {"ok": True})
    return cfg, md, repo


def test_a_system_wake_ensure_stamps_the_anchor(tmp_path, monkeypatch):
    cfg, md, repo = _ensure_cfg(tmp_path, monkeypatch)
    _turn_ended(repo, time.time() - 4000)
    res = A.dispatch("ensure_user_conversation", {
        "slug": SLUG, "global_user_id": GID, "message_ref": "тут смотря что он значит",
        "system_wake": True})
    assert res["sid"] == SID and res["spawned"] is False
    assert S._read_session_metadata(md).get(IT.SYSTEM_WAKE_ANCHOR_FIELD)


def test_his_own_message_clears_the_anchor(tmp_path, monkeypatch):
    """The half that protects HIM: the moment a human messages the attendant,
    the clock is its own again and the deadline restarts from his turn."""
    cfg, md, repo = _ensure_cfg(tmp_path, monkeypatch)
    _turn_ended(repo, time.time() - 4000)
    A.dispatch("ensure_user_conversation", {
        "slug": SLUG, "global_user_id": GID, "message_ref": "x",
        "system_wake": True})
    assert S._read_session_metadata(md).get(IT.SYSTEM_WAKE_ANCHOR_FIELD)

    A.dispatch("ensure_user_conversation", {
        "slug": SLUG, "global_user_id": GID, "message_ref": "а теперь я пишу сам"})
    assert IT.SYSTEM_WAKE_ANCHOR_FIELD not in S._read_session_metadata(md)


def test_an_ensure_without_a_message_ref_still_clears_it(tmp_path, monkeypatch):
    """The clear is decided by WHO is waking, not by whether a nudge is sent —
    an ensure with no message_ref is still not the system re-driving."""
    cfg, md, repo = _ensure_cfg(tmp_path, monkeypatch)
    meta = S._read_session_metadata(md)
    meta[IT.SYSTEM_WAKE_ANCHOR_FIELD] = "2026-09-07T00:00:00Z"
    S._write_session_metadata(md, meta)
    A.dispatch("ensure_user_conversation", {"slug": SLUG, "global_user_id": GID})
    assert IT.SYSTEM_WAKE_ANCHOR_FIELD not in S._read_session_metadata(md)


def test_uc_redrive_marks_its_own_dispatch_as_a_system_wake(tmp_path, monkeypatch):
    """Prove the path is REACHED: the module that pins the attendant is the one
    that has to send the marker, so assert it off uc_redrive's real sweep."""
    data = tmp_path / "data"
    conv = data / "_mothership" / "conversations" / SLUG / GID
    conv.mkdir(parents=True)
    (data / SLUG / "sessions").mkdir(parents=True)
    now = time.time()
    hung = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 6 * 3600))
    (conv.parent / f"{GID}.jsonl").write_text(
        '{"timestamp": "%s", "author": "user", "text": "hi"}\n' % hung)
    cfg = types.SimpleNamespace(data_dir=data, projects={SLUG: object()})

    monkeypatch.setattr(U, "_notify_operator_stuck", lambda *a, **k: ["op"])
    monkeypatch.setattr(S, "live_user_conversation_sid", lambda cfg, slug, gid: SID)
    monkeypatch.setattr(S, "list_sessions",
                        lambda cfg, slug: [{"sid": SID, "activity": "idle"}])
    from bot_squad_worker import detector as D
    monkeypatch.setattr(D, "session_pressure",
                        lambda cfg: {"rate_limited_sids": [], "limit_blocked_sids": []})
    sent = []
    monkeypatch.setattr(A, "dispatch", lambda action, params: sent.append((action, params)))

    U.check_project(cfg, SLUG, now=now)
    assert [a for a, _ in sent] == ["ensure_user_conversation"]
    assert sent[0][1]["system_wake"] is True


def test_resume_drops_the_anchor(tmp_path, monkeypatch):
    """A resurrect is a new lifetime — an inherited anchor would have the
    resumed session born already past its deadline, and the fresh pane
    recycled on its very first tick.

    Driven through the real ``sessions.resume`` (tmux stubbed at ``_run``, the
    way the other resume tests do it) rather than by reading the source: the
    field has to be dropped by the code path, not merely named near it.
    """
    import subprocess
    from bot_squad_worker.config import Config

    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    repo = tmp_path / "repo"; repo.mkdir()
    data = tmp_path / "data"
    (data / SLUG / "sessions").mkdir(parents=True)
    (cfg_dir / "projects.toml").write_text(
        f'[projects.{SLUG}]\nslug = "{SLUG}"\ndisplay_name = "W"\n'
        f'repo_path = "{repo}"\ndeploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\nprod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\ntg_chat = "0"\ncreated_at = 2026-05-10\n')
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    loaded = Config.load(cfg_dir)
    cfg = types.SimpleNamespace(projects=loaded.projects, data_dir=data,
                                tg_bot_token="")

    win = f"{GID}-user-conversation"
    S._write_session_metadata(data / SLUG / "sessions" / f"{SID}.md", {
        "sid": SID, "status": "suspended", "window": win, "cwd": str(repo),
        "claude_uuid": "7ea64910", "task_id": "~", "role": "user-conversation",
        IT.SYSTEM_WAKE_ANCHOR_FIELD: "2026-09-05T00:00:00Z"})

    opened = [False]

    def fake_run(args, **kw):
        if "capture-pane" in args:
            return subprocess.CompletedProcess(args, 0, "\u276f \n", "")
        if "new-window" in args:
            opened[0] = True
        if "list-panes" in args and opened[0]:
            return subprocess.CompletedProcess(
                args, 0, f"%20|{win}|4250|{repo}|claude\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)

    res = S.resume(cfg, SLUG, SID)
    assert res.get("ok") is True
    new_md = data / SLUG / "sessions" / f"{res['sid']}.md"
    assert IT.SYSTEM_WAKE_ANCHOR_FIELD not in S._read_session_metadata(new_md)
