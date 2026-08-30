"""T-0466 / M1-F1.3 — ~1h cache-window idle/waiting-session recycle + postpone.

Source: vision/initiatives/process-paradigm.md (SOURCE-VERBATIM Part A) — "it
should get recycled on timeout … cache invalidation timeout … 1 hour. On
timeout, stale waiting sessions should be asked to record their results and
exit. They should be able to postpone this until next timeout … indefinitely …
[and] should postpone if they are actively waiting for some long ongoing
process to finish (e.g. long build)".

The DoD wants worker coverage of three behaviours: TIMEOUT-FIRE (arm + finalize
the record-and-exit handoff), POSTPONE (per-window, repeatable), and
AUTO-POSTPONE (waiting on a tracked long bounded job).
"""
from __future__ import annotations

import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import autocompact as A
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import lifecycle_events as LE
from bot_squad_worker import sessions as S
# Captured at collection time (before any fixture monkeypatches recycle_gate)
# so the fail-closed subprocess-error test below can restore the REAL
# implementation regardless of what the `seams` fixture stubs it to.
from bot_squad_worker.recycle_gate import is_attached as _REAL_IS_ATTACHED


# --- env knobs --------------------------------------------------------------

def test_enabled_default_on_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    assert IT.idle_timeout_enabled() is True
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT", "0")
    assert IT.idle_timeout_enabled() is False


def test_window_default_and_override(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)
    assert IT.idle_timeout_sec() == IT.DEFAULT_IDLE_TIMEOUT_SEC == 3300
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "120")
    assert IT.idle_timeout_sec() == 120
    # garbage / non-positive falls back to the default (never collapse to 0)
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "-5")
    assert IT.idle_timeout_sec() == 3300
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "nope")
    assert IT.idle_timeout_sec() == 3300


# --- T-0930: the operator nudge cadence is DECOUPLED from idle_timeout_sec --

def test_operator_nudge_default_and_override(monkeypatch):
    monkeypatch.delenv("BOT_SQUAD_OPERATOR_NUDGE_SEC", raising=False)
    assert IT.operator_nudge_sec() == IT.DEFAULT_OPERATOR_NUDGE_SEC == 2400
    monkeypatch.setenv("BOT_SQUAD_OPERATOR_NUDGE_SEC", "60")
    assert IT.operator_nudge_sec() == 60
    # garbage / non-positive falls back to the default (never collapse to 0)
    monkeypatch.setenv("BOT_SQUAD_OPERATOR_NUDGE_SEC", "-5")
    assert IT.operator_nudge_sec() == 2400
    monkeypatch.setenv("BOT_SQUAD_OPERATOR_NUDGE_SEC", "nope")
    assert IT.operator_nudge_sec() == 2400


def test_operator_nudge_default_is_shorter_than_the_recycle_window():
    # The whole point of decoupling: 40min < 55min, so a drive=on operator
    # gets nudged well before its cache window would otherwise expire.
    assert IT.DEFAULT_OPERATOR_NUDGE_SEC < IT.DEFAULT_IDLE_TIMEOUT_SEC


# --- T-0856: the window must fire INSIDE the prompt-cache TTL ---------------

def test_window_fires_before_the_cache_ttl(monkeypatch, tmp_config_dir):
    """T-0856. The defect this pins: the window WAS 3600 — exactly the 1h
    prompt-cache TTL — so with a 60s tick a fire landed at 3600..3660s, always
    after expiry. 33 of 33 keep-alive wake-ups measured on the live install were
    a full cache MISS.

    The invariant is `window + tick + turn-margin <= cache TTL`. It is asserted
    against the tick the SCHEDULER actually registers, not a copy of `60` in
    this file, so re-crossing it by slowing the tick reds this test too — that
    is the half a hand-written constant would miss.
    """
    from bot_squad_worker.config import Config
    from bot_squad_worker.scheduler import build_scheduler

    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)

    sched = build_scheduler(Config.load(tmp_config_dir))
    job = next(j for j in sched.get_jobs() if j.id == "idle_timeout")
    tick = job.trigger.interval.total_seconds()
    # If this ever stops being an interval trigger the arithmetic below is
    # meaningless, so say so rather than reading a plausible-looking 0.
    assert tick > 0, "idle_timeout job carries no interval — cannot size the window"

    window = IT.idle_timeout_sec()
    assert IT.window_fits_cache_ttl(window, tick), (
        f"idle window {window}s + {tick:.0f}s tick + "
        f"{IT.CACHE_TURN_MARGIN_SEC}s turn-margin exceeds the "
        f"{IT.CACHE_TTL_SEC}s prompt-cache TTL — every keep-alive wake-up and "
        f"every recycle arm would land after the cache expired (T-0856)"
    )
    # The margin exists because `_idle_age` is measured from the jsonl mtime
    # (turn END) while the TTL clock starts at the request's START. A margin of
    # 0 would be the same bug wearing a different constant.
    assert IT.CACHE_TURN_MARGIN_SEC > 0


def test_window_fits_cache_ttl_rejects_the_old_default():
    """The green above proves nothing unless the predicate can go red. 3600 —
    this module's own default until T-0856 — is the input that must fail it."""
    assert IT.window_fits_cache_ttl(3300, 60) is True
    assert IT.window_fits_cache_ttl(3600, 60) is False   # the pre-T-0856 default
    assert IT.window_fits_cache_ttl(3540, 60) is False   # 59 min: still too late
    # exactly on the line is allowed; one second past it is not
    assert IT.window_fits_cache_ttl(
        IT.CACHE_TTL_SEC - 60 - IT.CACHE_TURN_MARGIN_SEC, 60) is True
    assert IT.window_fits_cache_ttl(
        IT.CACHE_TTL_SEC - 60 - IT.CACHE_TURN_MARGIN_SEC + 1, 60) is False


def test_ttl_crossing_env_override_is_honoured_but_warned(monkeypatch, caplog):
    """An operator may still set a longer window; what must not happen again is
    it being honoured SILENTLY, which is how 3600 ran cache-cold for a week."""
    import logging

    monkeypatch.setattr(IT, "_warned_windows", set())
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "3600")
    with caplog.at_level(logging.WARNING, logger="bot_squad_worker.idle_timeout"):
        assert IT.idle_timeout_sec() == 3600          # honoured, not clamped
        assert IT.idle_timeout_sec() == 3600          # second call: no new line
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, f"expected exactly one debounced warning, got {len(warns)}"
    assert "prompt-cache TTL" in warns[0].getMessage()

    # A conforming override says nothing at all.
    caplog.clear()
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "1800")
    with caplog.at_level(logging.WARNING, logger="bot_squad_worker.idle_timeout"):
        assert IT.idle_timeout_sec() == 1800
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# --- pure decision helpers --------------------------------------------------

def test_idle_due():
    assert IT.idle_due(3601, 3600) is True
    assert IT.idle_due(3600, 3600) is True
    assert IT.idle_due(3599, 3600) is False
    # unknowable age is conservative — never due
    assert IT.idle_due(None, 3600) is False


def test_postpone_active():
    now = 1000.0
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 500))
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 500))
    assert IT.postpone_active(future, now) is True
    assert IT.postpone_active(past, now) is False
    assert IT.postpone_active(None, now) is False
    assert IT.postpone_active("~", now) is False


# --- cfg + session-md harness (mirrors test_compact_handoff) ----------------

def _make_cfg(tmp_path: Path, *, sid: str, window: str, task_id: str | None,
              extra_md: dict | None = None, task_status: str = "closed"):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects.bot-squad]\n'
        'slug = "bot-squad"\n'
        'display_name = "Bot Squad"\n'
        f'repo_path = "{repo}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        'created_at = 2026-05-10\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    from bot_squad_worker.config import Config

    data_dir = tmp_path / "data"
    sess = data_dir / "bot-squad" / "sessions"
    sess.mkdir(parents=True)
    fm = {"sid": sid, "status": "active", "window": window,
          "cwd": str(repo), "claude_uuid": "uuid-" + sid}
    if task_id:
        fm["task_id"] = task_id
        # T-0863: the handoff destination for a task-bound session IS this md's
        # `## Context`, so it has to exist for the recycle to resolve one.
        backlog = data_dir / "bot-squad" / "backlog"
        backlog.mkdir(parents=True, exist_ok=True)
        (backlog / f"{task_id}-demo.md").write_text(
            f"---\nid: {task_id}\ntitle: Demo\nstatus: {task_status}\n---\n\n"
            "## Stakeholder notes\n\nDo the thing.\n\n"
            "## Context\n\nstate as of arm time\n")
    if extra_md:
        fm.update(extra_md)
    S._write_session_metadata(sess / f"{sid}.md", fm)

    cfg = Config.load(cfg_dir)
    patched = types.SimpleNamespace(projects=cfg.projects, data_dir=data_dir)
    return patched, data_dir


def _row(sid: str, *, window="demo", task_id="T-0042", cwd_repo: Path,
         status="active"):
    return {"sid": sid, "status": status, "window": window, "task_id": task_id,
            "role": "dev", "cwd": str(cwd_repo), "claude_uuid": "uuid-" + sid,
            "linux_user": ""}


@pytest.fixture
def seams(monkeypatch):
    """Stub every tmux/telemetry/suspend seam so no real session/tmux is
    touched.

    T-0566 made FINALIZE send ``/compact`` then terminate; T-0863 replaced that
    half — the recycle now ASKS the session to write its forward-state (into
    the ticket's ``## Context`` when it owns a task, into the role artifact when
    it doesn't), waits for that write, THEN terminates. Never a ``/compact``:
    the pane is suspended a tick later, so the squeeze would be discarded.
    ``calls['compact']`` stays wired so a regression that reintroduces it fails
    loudly instead of passing unnoticed."""
    calls = {"compact": [], "terminate": [], "ctx_handoff": [], "handoff": []}
    state = {"pane": "%9", "buf": "❯ \n", "idle_age": 5000.0,
             "tokens": 25000}  # default ABOVE the 20k threshold

    monkeypatch.setattr(A, "_pane_for", lambda sid, **kw: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane, **kw: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: calls["compact"].append(sid))
    monkeypatch.setattr(A, "_inject_context_handoff",
                        lambda sid, task_id, *, relaunch=True:
                        calls["ctx_handoff"].append((sid, task_id, relaunch)))
    monkeypatch.setattr(A, "_inject_handoff",
                        lambda sid, art, role=None, *, relaunch=True:
                        calls["handoff"].append((sid, art, role, relaunch)))
    monkeypatch.setattr(IT, "_context_tokens", lambda cfg, slug, sid: state["tokens"])

    def _fake_suspend(cfg, slug, sid, source=None, reason=None):
        calls["terminate"].append(sid)
        md = S._session_file(cfg.data_dir, slug, sid)
        existing = S._read_session_metadata(md) or {}
        meta = {"sid": sid, "status": "suspended",
                "claude_uuid": existing.get("claude_uuid", "~"),
                "task_id": existing.get("task_id", "~"),
                "window": existing.get("window", "~")}
        if source:
            meta["suspend_source"] = source
            meta["suspend_reason"] = reason or source
        S._write_session_metadata(md, meta)
        return {"ok": True, "suspended": True}
    monkeypatch.setattr(S, "suspend", _fake_suspend)

    # T-0563/T-0564: default slug "bot-squad" is already allowlisted; no human
    # is attached in these tests.
    monkeypatch.setattr(IT.recycle_gate, "is_attached", lambda target, **kw: False)

    # idle clock: jsonl mtime = now - idle_age
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: time.time() - state["idle_age"])
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)
    return {"calls": calls, "state": state}


def _fired(seams) -> bool:
    """Did the recycle take ANY action for this session this tick?

    T-0863 replaced the action itself — the normal path used to send a
    ``/compact`` and now injects a finalize handoff — so tests that only care
    THAT the recycle fired ask this instead of naming one mechanism. ``compact``
    stays in the tuple deliberately: the compact-and-stay path (exempt user
    sessions) still uses it, and a regression that reintroduces it on the
    terminate path should fail on a test that names it, not slip past one that
    merely asked "did something happen".
    """
    c = seams["calls"]
    return bool(c["compact"] or c["terminate"] or c["ctx_handoff"] or c["handoff"])


# --- A. TIMEOUT-FIRE: T-0863 ask-write-terminate ----------------------------

def test_start_asks_for_the_ticket_context_and_never_compacts(tmp_path, seams):
    """T-0863: over threshold, a task-bound session is asked to write its
    forward-state into its ticket's `## Context` — `relaunch=False`, because
    this trigger ENDS the session rather than booting a successor."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["ctx_handoff"] == [(sid, "T-0042", False)]
    assert seams["calls"]["compact"] == []  # «никакого компакта»
    assert seams["calls"]["terminate"] == []  # not yet — awaiting the write
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_phase"] == "finalizing"
    assert "idle_recycle_armed_at" in meta
    # the ARM-time snapshot of the destination, so FINALIZE can tell a real
    # write from a timeout
    assert meta["idle_recycle_mark"].startswith("ctx:")


def test_start_asks_a_taskless_session_for_its_role_artifact(tmp_path, seams):
    """The operator has no ticket to write a Context onto, so its half of the
    handoff is unchanged — but it too is told it is being ENDED, not
    relaunched, and it too never gets a `/compact`."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None,
                          extra_md={"drive": "off"})
    row = _row(sid, cwd_repo=data.parent / "repo", window="operator", task_id=None)
    row["role"] = "operator"
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert len(seams["calls"]["handoff"]) == 1
    got_sid, art, role, relaunch = seams["calls"]["handoff"][0]
    assert got_sid == sid and role == "operator" and relaunch is False
    assert art.endswith("/artifacts/operator-state.md")
    assert seams["calls"]["compact"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_mark"].startswith("mtime:")


def test_start_terminates_immediately_below_threshold(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["tokens"] = 5000  # below the 20k default threshold
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == []  # nothing worth writing down
    assert seams["calls"]["ctx_handoff"] == []
    assert seams["calls"]["terminate"] == [sid]
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["resumable"] is True
    assert "recycled_at" in meta
    assert "no forward-state written" in meta["resume_hint"]


def test_start_with_no_destination_terminates_without_a_compact(tmp_path, seams):
    """The correction's core: the old code sent Claude's native `/compact` and
    then suspended the pane a tick later, paying for a squeeze nothing would
    ever read — «пустая трата токенов»."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    # bound to an id whose md does not exist → no Context, no artifact fallback
    (data / "bot-squad" / "backlog" / "T-0042-demo.md").unlink()
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == []
    assert seams["calls"]["terminate"] == [sid]


def test_not_due_when_jsonl_fresh(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["idle_age"] = 10.0  # just had a turn → cache warm
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == []
    assert seams["calls"]["terminate"] == []


def _armed(tmp_path, sid, *, mark=None, when=None, task_id="T-0042",
           window="demo", phase="finalizing"):
    """A session md mid-handoff: FINALIZE armed at ``when`` with ``mark`` as the
    ARM-time snapshot of its destination."""
    extra = {"idle_recycle_phase": phase,
             "idle_recycle_armed_at": when or time.strftime(
                 "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if mark is not None:
        extra["idle_recycle_mark"] = mark
    return _make_cfg(tmp_path, sid=sid, window=window, task_id=task_id,
                     extra_md=extra)


def _ticket_md(data: Path, task_id="T-0042") -> Path:
    return data / "bot-squad" / "backlog" / f"{task_id}-demo.md"


def test_finalize_terminates_and_records_once_the_context_is_written(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    # armed against the Context as it was, then the session actually wrote
    cfg, data = _armed(tmp_path, sid, mark="ctx:stale-arm-digest")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == [sid]
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["status"] == "suspended"
    assert meta["resumable"] is True
    assert "recycled_at" in meta
    assert "forward-state written to T-0042's ## Context" in meta["resume_hint"]
    assert "idle_recycle_phase" not in meta  # cleared
    assert "idle_recycle_mark" not in meta


def test_finalize_waits_while_the_context_is_unchanged(tmp_path, seams):
    """The write is the signal, not the clock: an unchanged `## Context` means
    the session has not handed off, and terminating it here loses everything —
    the exact failure T-0858 measured 20 times over."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _armed(tmp_path, sid, mark=None)
    # arm-mark == the ticket's CURRENT Context digest → nothing written yet
    mark = A.handoff_mark({"kind": "context", "task_md": str(_ticket_md(data))})
    md = data / "bot-squad" / "sessions" / f"{sid}.md"
    meta = S._read_session_metadata(md)
    meta["idle_recycle_mark"] = mark
    S._write_session_metadata(md, meta)

    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []
    assert S._read_session_metadata(md)["idle_recycle_phase"] == "finalizing"


def test_finalize_waits_while_pane_not_composer_ready(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _armed(tmp_path, sid, mark="ctx:stale-arm-digest")
    seams["state"]["buf"] = "· Writing… (esc to interrupt)"  # mid-turn
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_phase"] == "finalizing"  # still armed


def test_finalize_timeout_terminates_anyway(tmp_path, seams):
    """Never wedge: even if the session never writes, the bounded wait times
    out and we terminate + record regardless — the recycle must always converge
    to a resumable-suspended state. The record says so: `wrote_state` is False,
    so a later reader can tell this exit apart from a clean handoff."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - A.handoff_timeout_sec() - 60))
    cfg, data = _armed(tmp_path, sid, mark=None, when=old)
    mark = A.handoff_mark({"kind": "context", "task_md": str(_ticket_md(data))})
    md = data / "bot-squad" / "sessions" / f"{sid}.md"
    meta = S._read_session_metadata(md)
    meta["idle_recycle_mark"] = mark  # never moved
    S._write_session_metadata(md, meta)
    seams["state"]["buf"] = "· Writing… (esc to interrupt)"  # never came back

    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == [sid]
    meta = S._read_session_metadata(md)
    assert meta["resumable"] is True
    assert "no forward-state written" in meta["resume_hint"]


def test_finalize_does_not_claim_a_write_when_the_ticket_vanished(tmp_path, seams):
    """If the destination stops resolving mid-handoff, the mark goes empty —
    which DIFFERS from the armed mark. Reporting that as "forward-state
    written" would put a false success in the record a later reader trusts."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - A.handoff_timeout_sec() - 60))
    cfg, data = _armed(tmp_path, sid, mark="ctx:some-arm-digest", when=old)
    _ticket_md(data).unlink()  # ticket deleted while the handoff was in flight

    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "no forward-state written" in meta["resume_hint"]


def test_finalize_converges_on_a_pre_t0863_stamp(tmp_path, seams):
    """A worker restart mid-recycle leaves `idle_recycle_phase: compacting` and
    no mark. It must still finalize, or the session sits armed forever."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _armed(tmp_path, sid, mark=None, phase="compacting")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == [sid]


def test_finalize_drops_stamp_when_pane_already_gone(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "finalizing",
                                    "idle_recycle_armed_at": armed})
    seams["state"]["pane"] = None  # the session already exited on its own
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "idle_recycle_phase" not in meta


# --- T-0858: the WHOLE idle window in one drive, through the REAL writer ----

def _ticket_context(path: Path) -> str:
    """A ticket's ``## Context`` as a reader sees it.

    Extracted exactly the way :func:`autocompact.context_digest` extracts the
    bytes it hashes — same frontmatter regex (never ``split("---", 2)``, which
    breaks on a title containing a dash run, T-0286), same ``parse_body`` — so
    the test and the mechanism cannot disagree about which bytes count as the
    handoff.
    """
    from bot_squad_worker.task_body import parse_body
    text = path.read_text()
    m = A._TICKET_FM_RE.match(text)
    return parse_body(m.group(2) if m else text).get("context", "")


def test_idle_window_drive_writes_the_ticket_context_before_terminate(
        tmp_path, seams, monkeypatch):
    """T-0858's DoD: drive a task-bound session over the idle window and prove
    the ticket's ``## Context`` held its forward-state BEFORE it was terminated.

    Every other test in this section pins one half of the machine with a
    hand-made mark. This one runs all the ticks against ONE ticket md and lets
    the write happen the way the finalize prompt asks for it — through
    ``task_context_set``, the action ``bsq ticket context`` calls. What that
    catches and two half-tests cannot: an ARM whose snapshot is taken against a
    different destination than FINALIZE re-resolves, a mark the real writer
    does not actually move, and a terminate that races the write.

    T-0858 was opened on 20 consecutive idle recycles that reported success
    having recorded nothing, so "before terminate" is asserted as an ORDERING —
    the Context is read at the instant ``sessions.suspend`` is called, not
    after the drive is over, where a later tick's write would also pass.
    """
    from bot_squad_worker import actions

    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    ticket = _ticket_md(data)
    row = _row(sid, cwd_repo=data.parent / "repo")
    before = _ticket_context(ticket)
    assert before.strip() == "state as of arm time"

    # What the ticket's Context said at the instant the session was suspended.
    inner_suspend = S.suspend  # the seams fixture's recorder, not the real one

    def _recording_suspend(cfg_, slug_, sid_, **kw):
        seen["context"] = _ticket_context(ticket)
        return inner_suspend(cfg_, slug_, sid_, **kw)

    seen: dict[str, str] = {}
    monkeypatch.setattr(S, "suspend", _recording_suspend)

    # tick 1 — the window has elapsed: the session is ASKED, never terminated,
    # and the system does NOT author the Context on its behalf.
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["ctx_handoff"] == [(sid, "T-0042", False)]
    assert seams["calls"]["terminate"] == []
    assert _ticket_context(ticket) == before

    # tick 2 — nothing written yet: the wait holds instead of terminating.
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []

    # the session obeys the prompt, through the one verb the prompt names
    monkeypatch.setattr(actions, "_CONFIG", cfg)
    handoff = "Rewired the widget; the flaky import remains — start there."
    actions._action_task_context_set(
        {"slug": "bot-squad", "task_id": "T-0042", "text": handoff, "sid": sid})

    # tick 3 — the write landed: terminate + record.
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == [sid]

    # THE assertion the ticket was opened for: the forward-state was already on
    # the ticket when the session was cut, not merely present afterwards.
    assert handoff in seen["context"]
    assert seen["context"] != before
    assert handoff in _ticket_context(ticket)   # and it survived the terminate

    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["status"] == "suspended"
    assert meta["resumable"] is True
    assert "forward-state written to T-0042's ## Context" in meta["resume_hint"]
    # …and across the whole drive, not one `/compact`: «никакого компакта».
    assert seams["calls"]["compact"] == []


def test_finalize_writes_the_executive_summary_through_its_own_action(
        tmp_path, seams, monkeypatch):
    """T-0863: the finalize prompt asks for TWO writes, and the second one has
    to land on the same md without disturbing the first.

    Driven through ``task_summary_set`` — the action ``bsq ticket summary``
    calls — rather than through ``set_summary`` directly, because what is under
    test here is the whole write path: the lock, the frontmatter re-stamp, and
    the fact that the two writers share one file.
    """
    from bot_squad_worker import actions
    from bot_squad_worker.task_body import parse_body

    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    ticket = _ticket_md(data)
    monkeypatch.setattr(actions, "_CONFIG", cfg)

    actions._action_task_context_set(
        {"slug": "bot-squad", "task_id": "T-0042",
         "text": "Rewired the widget.\n\n### Next\n\nthe flaky import.", "sid": sid})
    actions._action_task_summary_set(
        {"slug": "bot-squad", "task_id": "T-0042",
         "text": "Writer and CLI shipped; the board render remains.", "sid": sid})

    parsed = parse_body(ticket.read_text().split("---\n", 2)[-1])
    assert parsed["summary"] == "Writer and CLI shipped; the board render remains."
    # the ask is untouched and the working area still holds ALL of its detail —
    # a summary write that clipped either would be the T-0729 shape returning.
    assert parsed["verbatim"] == "Do the thing."
    assert "the flaky import." in parsed["context"]
    assert "updated:" in ticket.read_text().split("---")[1]

    # …and a non-paragraph is refused by the ACTION too, not only by the writer:
    # the CLI/API callers never touch `set_summary`, so a refusal that stopped
    # at the module boundary would not exist for either of them.
    with pytest.raises(actions.ActionError) as e:
        actions._action_task_summary_set(
            {"slug": "bot-squad", "task_id": "T-0042", "text": "a\n\nb"})
    assert "ONE paragraph" in str(e.value)


def test_a_summary_only_write_does_NOT_release_the_finalize_wait(
        tmp_path, seams, monkeypatch):
    """The STATED BOUND of the digest design, pinned so it cannot drift.

    FINALIZE watches the `## Context` digest alone. A summary is one cheap
    paragraph, so if either-section-changed released the wait, a degraded
    session could satisfy finalize with a status line and lose the forward
    state — which is exactly the reported-success-stored-nothing failure
    T-0858 was opened on, arriving through the new section.

    So: write ONLY the summary, and the session must still be alive.
    """
    from bot_squad_worker import actions

    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    monkeypatch.setattr(actions, "_CONFIG", cfg)

    # tick 1 — asked, armed against the Context digest.
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == []

    actions._action_task_summary_set(
        {"slug": "bot-squad", "task_id": "T-0042",
         "text": "Nearly done, honest.", "sid": sid})

    # the ticket md CHANGED — mtime and bytes both — and that must not count.
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []

    # the Context write is what releases it.
    actions._action_task_context_set(
        {"slug": "bot-squad", "task_id": "T-0042", "text": "the real state", "sid": sid})
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == [sid]


def test_idle_window_drive_holds_when_the_context_is_never_written(
        tmp_path, seams, monkeypatch):
    """The falsifiable half of the drive above: identical, with the WRITE
    REMOVED.

    Same ticket, same seams, same ticks — the only difference is that nobody
    calls ``task_context_set``. The session must still be alive, because what
    releases the terminate is the write and not the clock. Without this arm the
    green above would also pass a build that terminated on tick 3 regardless,
    which is precisely the 20-recycles-recorded-nothing behaviour T-0858
    measured.

    The bounded timeout still applies and is deliberately NOT reached here —
    ``test_finalize_timeout_terminates_anyway`` owns that end.
    """
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    ticket = _ticket_md(data)
    row = _row(sid, cwd_repo=data.parent / "repo")
    before = _ticket_context(ticket)

    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True   # armed
    for _ in range(10):
        assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                                user_home="/home/x") is False

    assert seams["calls"]["terminate"] == []
    assert seams["calls"]["compact"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_phase"] == "finalizing"
    assert meta["status"] == "active"
    assert _ticket_context(ticket) == before


def test_terminate_failure_is_retried_next_tick(tmp_path, seams, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")

    def _boom(cfg, slug, sid, **kw):
        raise RuntimeError("tmux exploded")
    monkeypatch.setattr(S, "suspend", _boom)
    seams["state"]["tokens"] = 5000  # below threshold → terminate attempted this tick
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta.get("status") == "active"  # untouched — will retry next tick


def test_arm_skips_when_pane_not_composer_ready(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["buf"] = "working… esc to interrupt\n"  # mid-turn
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == []
    assert seams["calls"]["terminate"] == []


def test_kill_switch_disables_recycle(tmp_path, seams, monkeypatch):
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT", "0")
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


# --- A2. T-0616/T-0617: hand-launched user sessions never TERMINATE, but ----
# ---     DO get compact-and-stay (T-0617) --------------------------------

def test_hand_launched_user_session_compacts_but_never_terminates(tmp_path, seams):
    """p8's exact shape (D-0053 §4): window ``user-session`` derives role
    ``dev``, so the T-0564 role check alone let it ride the full recycle
    path. The window signal must keep it off the terminate-and-remember flow
    forever — but T-0617 gives it compact-and-stay instead of the old full
    no-op exemption: it still gets compacted in place, session left running."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid]
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["compact_stay_phase"] == "compacting"
    assert "compact_stay_armed_at" in meta
    assert "idle_recycle_phase" not in meta  # the terminate-flow field, untouched


def test_hand_launched_user_session_stale_terminate_phase_never_finalized(tmp_path, seams):
    """Even a stale terminate-flow in-flight phase stamp (a pre-fix leftover,
    or hand-edited md) must not route an exempt session into the
    finalize→terminate half — the exempt branch ignores ``idle_recycle_phase``
    entirely and drives its own ``compact_stay_phase`` machine instead."""
    sid = "S-almdudleer-user-session-p8"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": armed})
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == []
    assert seams["calls"]["compact"] == [sid]


def test_recycle_exempt_marker_blocks_terminate_but_allows_compact_stay(tmp_path, seams):
    """An ad-hoc-named hand-launched session is exempted from TERMINATE by
    the explicit ``recycle_exempt: true`` md stamp (the hook preserves it,
    T-0616) — but T-0617 still compacts it in place."""
    sid = "S-almdudleer-bot-squad-myadhoc-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="myadhoc", task_id=None,
                          extra_md={"recycle_exempt": True})
    row = _row(sid, window="myadhoc", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid] and seams["calls"]["terminate"] == []


def test_pinned_marker_blocks_compact_stay_too(tmp_path, seams):
    """T-0926 follow-up: unlike ``recycle_exempt`` (blocks terminate only,
    still permits compact-and-stay by design), an explicit ``pinned: true``
    stamp blocks compact-and-stay as well — no automatic action at all."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"pinned": True})
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


# --- A3. T-0617: compact-and-stay — arm, finalize, anti-loop ----------------

def test_compact_stay_finalize_never_terminates_and_stamps_last_at(tmp_path, seams):
    sid = "S-almdudleer-user-session-p8"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_phase": "compacting",
                                    "compact_stay_armed_at": armed})
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "compact_stay_phase" not in meta
    assert "compact_stay_armed_at" not in meta
    assert "compact_stay_last_at" in meta
    assert meta["status"] == "active"  # session untouched — never suspended


def test_compact_stay_finalize_waits_while_pane_not_composer_ready(tmp_path, seams):
    sid = "S-almdudleer-user-session-p8"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_phase": "compacting",
                                    "compact_stay_armed_at": armed})
    seams["state"]["buf"] = "· Compacting… (esc to interrupt)"
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["compact_stay_phase"] == "compacting"  # still armed


def test_compact_stay_finalize_timeout_never_terminates(tmp_path, seams):
    """Never wedge — even if /compact never seems to finish, the bounded wait
    times out and finalize converges. Unlike the terminate flow, converging
    here means clearing the phase and leaving the session running, NOT
    calling sessions.suspend."""
    sid = "S-almdudleer-user-session-p8"
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - A.handoff_timeout_sec() - 60))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_phase": "compacting",
                                    "compact_stay_armed_at": old})
    seams["state"]["buf"] = "· Compacting… (esc to interrupt)"
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "compact_stay_phase" not in meta
    assert "compact_stay_last_at" in meta


def test_compact_stay_finalize_drops_stamp_when_pane_already_gone(tmp_path, seams):
    sid = "S-almdudleer-user-session-p8"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_phase": "compacting",
                                    "compact_stay_armed_at": armed})
    seams["state"]["pane"] = None
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "compact_stay_phase" not in meta


def test_compact_stay_skips_when_nothing_worth_compacting(tmp_path, seams):
    """Below the context-token threshold, compact-and-stay is a no-op (not a
    terminate — unlike the non-exempt flow's below-threshold branch)."""
    sid = "S-almdudleer-user-session-p8"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None)
    seams["state"]["tokens"] = 5000  # below the 20k default threshold
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_compact_stay_anti_loop_blocks_rearm_within_same_window(tmp_path, seams):
    """T-0617's core ask: a compact-and-stay fires at most once per cache
    window. ``compact_stay_last_at`` is the guard — set it fresh (as
    FINALIZE would just have) and prove a second ARM does not fire even
    though the idle-age signal still reads well past the window (the exact
    T-0616 double-compact shape: a stale/misbehaving idle clock must not be
    trusted to have reset)."""
    sid = "S-almdudleer-user-session-p8"
    just_finalized = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_last_at": just_finalized})
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_compact_stay_rearms_once_the_window_has_elapsed(tmp_path, seams):
    """The anti-loop guard is per-window, not permanent: once a full
    ``idle_timeout_sec()`` has passed since the last compact-and-stay, the
    next idle-due tick may arm again."""
    sid = "S-almdudleer-user-session-p8"
    long_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() - IT.idle_timeout_sec() - 10))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="user-session", task_id=None,
                          extra_md={"compact_stay_last_at": long_ago})
    row = _row(sid, window="user-session", task_id=None,
               cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid]


def test_compact_stay_due_helper():
    now = 10_000.0
    assert IT.compact_stay_due(None, now, 3600) is True
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 100))
    assert IT.compact_stay_due(fresh, now, 3600) is False
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3700))
    assert IT.compact_stay_due(stale, now, 3600) is True


# --- B. POSTPONE: per-window, repeatable ------------------------------------

def test_postpone_skips_the_recycle(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_postpone_until": future})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_expired_postpone_lets_recycle_fire_again(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_postpone_until": past})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert _fired(seams)  # due again — postpone is per-window


def test_set_idle_postpone_default_one_window(tmp_path, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    monkeypatch.setenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", "3600")
    before = time.time()
    out = S.set_idle_postpone(cfg, "bot-squad", sid)
    assert out["ok"] is True and out["seconds"] == 3600
    until = S._parse_ts_epoch(out["postpone_until"])
    assert before + 3600 - 5 <= until <= before + 3600 + 5
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_postpone_until"] == out["postpone_until"]


def test_set_idle_postpone_custom_seconds_is_repeatable(tmp_path):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    out1 = S.set_idle_postpone(cfg, "bot-squad", sid, seconds=120, reason="long build")
    assert out1["seconds"] == 120
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_postpone_reason"] == "long build"
    # repeat — pushes the deadline forward again (unbounded)
    time.sleep(0.01)
    out2 = S.set_idle_postpone(cfg, "bot-squad", sid, seconds=300)
    assert S._parse_ts_epoch(out2["postpone_until"]) >= S._parse_ts_epoch(out1["postpone_until"])


def test_idle_postpone_action_dispatch(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    monkeypatch.setattr(ACT, "_get_config", lambda: cfg)
    out = ACT.dispatch("idle_postpone", {"slug": "bot-squad", "sid": sid})
    assert out["ok"] is True
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "idle_postpone_until" in meta


def test_idle_postpone_action_rejects_extra_and_bad_seconds(tmp_path, monkeypatch):
    import bot_squad_worker.actions as ACT
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, _ = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    monkeypatch.setattr(ACT, "_get_config", lambda: cfg)
    with pytest.raises(ACT.ActionError, match="unexpected"):
        ACT.dispatch("idle_postpone", {"slug": "bot-squad", "sid": sid, "bogus": 1})
    with pytest.raises(ACT.ActionError, match="integer"):
        ACT.dispatch("idle_postpone", {"slug": "bot-squad", "sid": sid, "seconds": "soon"})


def test_idle_postpone_registered_with_mode():
    from bot_squad_worker.actions import ACTION_MODES, ACTION_REGISTRY
    assert "idle_postpone" in ACTION_REGISTRY
    assert ACTION_MODES["idle_postpone"] == "tmux_only"


# --- C. AUTO-POSTPONE: waiting on a tracked long bounded job -----------------

def _enqueue_deploy(data_dir: Path, sid: str, *, phase="processing"):
    import json
    d = data_dir / "bot-squad" / "_jobs" / "deploy" / phase
    d.mkdir(parents=True, exist_ok=True)
    (d / "20260627-deploy.json").write_text(json.dumps(
        {"slug": "bot-squad", "target": "staging", "requested_by": sid}))


def test_inflight_deploy_detected(tmp_path):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    assert IT.tracking_long_job(cfg, "bot-squad", sid) is False
    _enqueue_deploy(data, sid, phase="processing")
    assert IT.tracking_long_job(cfg, "bot-squad", sid) is True
    # a deploy requested by SOMEONE ELSE does not auto-postpone us
    assert IT.tracking_long_job(cfg, "bot-squad", "S-other-p1") is False


def test_auto_postpone_skips_recycle_for_inflight_build(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    _enqueue_deploy(data, sid, phase="queue")  # build queued, not yet running
    row = _row(sid, cwd_repo=data.parent / "repo")
    # idle past the window, but waiting on a tracked build → auto-postpone
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_recycle_fires_once_build_completes(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    # no deploy in flight → the normal idle window applies and the recycle arms
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert _fired(seams)


# --- tick: per-project sweep, non-active rows skipped ------------------------

def test_tick_recycles_active_skips_suspended(tmp_path, seams, monkeypatch):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    active = _row(sid, cwd_repo=data.parent / "repo", status="active")
    dead = _row("S-almdudleer-bot-squad-old-p9", cwd_repo=data.parent / "repo",
                status="suspended")
    monkeypatch.setattr(S, "list_sessions", lambda cfg, slug: [active, dead])
    monkeypatch.setattr(S, "_get_user_home", lambda: "/home/x")
    monkeypatch.setattr(S, "_get_current_user", lambda: "almdudleer")
    IT.tick(cfg)
    assert seams["calls"]["ctx_handoff"] == [(sid, "T-0042", False)]


# --- T-0470: hook-driven idle clock + emitted lifecycle events ---------------

def test_idle_age_reads_hook_signal_over_jsonl(tmp_path, seams):
    """The timeout decision reads the HOOK Stop-marker, not the jsonl mtime."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    repo = data.parent / "repo"
    # jsonl says FRESH (10s — not due); hook Stop-marker says idle 4000s (> 1h).
    seams["state"]["idle_age"] = 10.0
    LE.touch_marker(str(repo), sid, LE.MARKER_STOP)
    import os
    anchor = time.time() - 4000
    os.utime(LE.marker_path(str(repo), sid, LE.MARKER_STOP), (anchor, anchor))
    row = _row(sid, cwd_repo=repo)
    # Despite the fresh jsonl, the hook signal drives the decision → it ARMS.
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["ctx_handoff"] == [(sid, "T-0042", False)]


def test_active_marker_keeps_session_busy(tmp_path, seams):
    """A .active marker newer than .stop = turn in progress → NOT due."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    repo = data.parent / "repo"
    seams["state"]["idle_age"] = 10.0  # jsonl irrelevant once a hook signal exists
    import os
    LE.touch_marker(str(repo), sid, LE.MARKER_STOP)
    anchor = time.time() - 4000
    os.utime(LE.marker_path(str(repo), sid, LE.MARKER_STOP), (anchor, anchor))
    LE.touch_marker(str(repo), sid, LE.MARKER_ACTIVE)  # newer → busy
    row = _row(sid, cwd_repo=repo)
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_arm_emits_session_timeout_event(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    doc = LE.read_events(cfg, "bot-squad", sid)
    assert doc.get("counts", {}).get(LE.SESSION_TIMEOUT) == 1
    assert doc["last"][LE.SESSION_TIMEOUT]["reason"] == "idle_window"


def test_finalize_emits_session_recycled_event(tmp_path, seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": armed})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    doc = LE.read_events(cfg, "bot-squad", sid)
    assert doc.get("counts", {}).get(LE.SESSION_RECYCLED) == 1
    assert doc["last"][LE.SESSION_RECYCLED]["cause"] == "idle_timeout"


# --- D. T-0563/T-0564: recycle-v2 gates, exercised through maybe_recycle -----

def test_non_allowlisted_project_never_touched_by_default_allowlist(tmp_path, seams):
    """T-0563: the 2026-06-29 incident's fix — a session in a non-allowlisted
    project is NEVER touched by idle_timeout (watchrobot itself joined the
    default allowlist in T-0613, so the example is another slug)."""
    sid = "S-almdudleer-lim-finance-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    # seed the session under a DIFFERENT (non-allowlisted) project slug
    sess = data / "lim-finance" / "sessions"
    sess.mkdir(parents=True)
    S._write_session_metadata(sess / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "demo",
        "cwd": str(data.parent / "repo"), "claude_uuid": "uuid-" + sid,
        "task_id": "T-0042"})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "lim-finance", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def test_env_override_allowlists_watchrobot(tmp_path, seams, monkeypatch):
    """T-0563: BOT_SQUAD_RECYCLE_PROJECTS opts a project in explicitly."""
    monkeypatch.setenv("BOT_SQUAD_RECYCLE_PROJECTS", "watchrobot")
    sid = "S-almdudleer-watchrobot-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    sess = data / "watchrobot" / "sessions"
    sess.mkdir(parents=True)
    S._write_session_metadata(sess / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "demo",
        "cwd": str(data.parent / "repo"), "claude_uuid": "uuid-" + sid,
        "task_id": "T-0042"})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "watchrobot", row, now=time.time(),
                            user_home="/home/x") is True
    assert _fired(seams)


def test_config_recycle_projects_fallback_allows_watchrobot(tmp_path, seams):
    """T-0563: system_settings.toml [recycle].projects extends the allowlist
    when no env override is set."""
    sid = "S-almdudleer-watchrobot-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    cfg.recycle_projects = ("bot-squad", "watchrobot")
    sess = data / "watchrobot" / "sessions"
    sess.mkdir(parents=True)
    S._write_session_metadata(sess / f"{sid}.md", {
        "sid": sid, "status": "active", "window": "demo",
        "cwd": str(data.parent / "repo"), "claude_uuid": "uuid-" + sid,
        "task_id": "T-0042"})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "watchrobot", row, now=time.time(),
                            user_home="/home/x") is True
    assert _fired(seams)


def test_user_conversation_role_never_terminated_but_gets_compact_stay(tmp_path, seams):
    """T-0564: the human's own live chat is never auto-TERMINATED, even when
    idle past the window and in an allowlisted project. T-0617: it DOES get
    compact-and-stay — this is in fact the session the T-0566 soft ask
    ("compact user sessions before cache expiry, in place") most concretely
    describes, since it is the longest-lived session in the system."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          extra_md={"role": "user-conversation"})
    row = _row(sid, cwd_repo=data.parent / "repo")
    row["role"] = "user-conversation"
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert seams["calls"]["compact"] == [sid] and seams["calls"]["terminate"] == []


def test_attached_session_never_recycled(tmp_path, seams, monkeypatch):
    """T-0564: a human tmux client attached to the pane blocks the recycle even
    in an allowlisted project with a non-exempt role."""
    monkeypatch.setattr(IT.recycle_gate, "is_attached", lambda target, **kw: True)
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []


def _operator_row(sid: str, *, cwd_repo: Path, status="active"):
    return {"sid": sid, "status": status, "window": "operator", "task_id": None,
            "role": "operator", "cwd": str(cwd_repo), "claude_uuid": "uuid-" + sid,
            "linux_user": ""}


@pytest.fixture
def keepalive_seams(seams, monkeypatch):
    """Extend `seams` with a spy on the keep-alive nudge send."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(IT, "_send_keepalive_nudge",
                        lambda sid, text: calls.append((sid, text)))
    seams["calls"]["keepalive"] = calls
    return seams


# --- T-0655: drive=on operator gets a keep-alive nudge instead of recycle ----

def test_drive_on_operator_gets_keepalive_nudge_not_recycled(tmp_path, keepalive_seams):
    """The stakeholder's core ask: an idle drive=on operator past its cache
    window is nudged ("continue"), never terminated/compacted."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert len(keepalive_seams["calls"]["keepalive"]) == 1
    assert keepalive_seams["calls"]["keepalive"][0][0] == sid
    assert keepalive_seams["calls"]["compact"] == []
    assert keepalive_seams["calls"]["terminate"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "operator_keepalive_last_at" in meta
    assert meta["status"] == "active"  # never suspended


def test_drive_on_operator_default_when_field_absent(tmp_path, keepalive_seams):
    """`drive` unset on an operator session still reads as on (default)."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "drive" not in meta
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert keepalive_seams["calls"]["terminate"] == []


def test_drive_on_operator_nudged_at_40min_before_the_55min_recycle_window(
        tmp_path, keepalive_seams):
    """T-0930: the nudge must fire on its OWN 40min cadence, not wait for the
    55min recycle window — the entire point of decoupling it."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    keepalive_seams["state"]["idle_age"] = 2500.0  # > 2400 (40min), < 3300 (55min)
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert len(keepalive_seams["calls"]["keepalive"]) == 1


def test_drive_on_operator_not_due_when_jsonl_fresh(tmp_path, keepalive_seams):
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    keepalive_seams["state"]["idle_age"] = 10.0
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert keepalive_seams["calls"]["keepalive"] == []


def test_drive_on_operator_nudge_anti_loop_blocks_within_same_window(tmp_path, keepalive_seams):
    """A second tick within the same cache window must not re-nudge — this is
    what protects against every ~60s tick re-injecting 'continue' if the
    operator never responds (idle clock doesn't reset without a new turn)."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    just_sent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None,
                          extra_md={"operator_keepalive_last_at": just_sent})
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert keepalive_seams["calls"]["keepalive"] == []


def test_drive_on_operator_nudge_rearms_after_window_elapses(tmp_path, keepalive_seams):
    sid = "S-almdudleer-bot-squad-operator-p1"
    long_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() - IT.idle_timeout_sec() - 10))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None,
                          extra_md={"operator_keepalive_last_at": long_ago})
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert len(keepalive_seams["calls"]["keepalive"]) == 1


def test_drive_on_operator_postpone_active_skips_nudge(tmp_path, keepalive_seams):
    sid = "S-almdudleer-bot-squad-operator-p1"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None,
                          extra_md={"idle_postpone_until": future})
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert keepalive_seams["calls"]["keepalive"] == []


def test_drive_on_operator_tracked_job_skips_nudge(tmp_path, keepalive_seams):
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    _enqueue_deploy(data, sid, phase="processing")
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert keepalive_seams["calls"]["keepalive"] == []


def test_drive_on_operator_nudge_waits_for_composer_ready(tmp_path, keepalive_seams):
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None)
    keepalive_seams["state"]["buf"] = "working… esc to interrupt\n"  # mid-turn
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert keepalive_seams["calls"]["keepalive"] == []


def test_keepalive_due_helper():
    now = 10_000.0
    assert IT.keepalive_due(None, now, 3600) is True
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 100))
    assert IT.keepalive_due(fresh, now, 3600) is False
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3700))
    assert IT.keepalive_due(stale, now, 3600) is True


def test_keepalive_nudge_text_no_target_steers_to_drive_off(monkeypatch):
    from bot_squad_worker import operator_redrive
    monkeypatch.setattr(operator_redrive, "weekly_quota_target_pct", lambda cfg: None)
    text = IT._keepalive_nudge_text(cfg=None, slug="bot-squad")
    assert "drive=off" in text or "drive off" in text
    assert "maintenance" not in text.lower()  # only the WITH-target case steers there


def test_keepalive_nudge_text_with_target_steers_to_maintenance(monkeypatch):
    """Addendum 1: a live quota target must steer toward maintenance backlog,
    not toward setting drive=off."""
    from bot_squad_worker import operator_redrive
    monkeypatch.setattr(operator_redrive, "weekly_quota_target_pct", lambda cfg: 20.0)
    text = IT._keepalive_nudge_text(cfg=None, slug="bot-squad")
    assert "20%" in text
    assert "maintenance" in text.lower()
    assert "NOT" in text  # "NOT by itself a reason to set drive=off"


# --- T-0930: dev drive-unmet nudge (5min, independent of the operator's) ---

@pytest.fixture
def dev_nudge_seams(seams, monkeypatch):
    """Extend `seams` with a spy on the dev nudge send."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(IT, "_send_dev_nudge",
                        lambda sid, text: calls.append((sid, text)))
    seams["calls"]["dev_nudge"] = calls
    return seams


def test_dev_with_open_task_gets_nudged_not_recycled(tmp_path, dev_nudge_seams):
    """The stakeholder's core dev ask: an idle dev whose task is not yet
    totest/closed is nudged ('продолжай'), never terminated/compacted."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="open")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert len(dev_nudge_seams["calls"]["dev_nudge"]) == 1
    assert dev_nudge_seams["calls"]["dev_nudge"][0][0] == sid
    assert dev_nudge_seams["calls"]["terminate"] == []
    assert dev_nudge_seams["calls"]["ctx_handoff"] == []
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "dev_nudge_last_at" in meta
    assert meta["status"] == "active"  # never suspended


def test_dev_with_in_progress_task_gets_nudged_too(tmp_path, dev_nudge_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="in_progress")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert len(dev_nudge_seams["calls"]["dev_nudge"]) == 1


def test_dev_with_totest_task_falls_through_to_normal_recycle(tmp_path, dev_nudge_seams):
    """DONE — not drive-unmet, so it must NOT be nudged (falls through to the
    same terminate-and-remember mechanism the pre-T-0930 tests exercise)."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="totest")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert dev_nudge_seams["calls"]["dev_nudge"] == []
    assert _fired(dev_nudge_seams)  # the normal terminate/ctx-handoff path fired instead


def test_dev_with_blocked_on_user_task_falls_through_not_nudged(tmp_path, dev_nudge_seams):
    """WAITING — the literal failure T-0931's blocked_on_user exists to stop:
    a blocked dev must NOT be nudged 'продолжай'. (It still falls through to
    the generic terminate path here — T-0930's dedicated WAITING durable
    wait-state is separate, not-yet-built follow-on work; the DoD for THIS
    piece is only that it is never nudged.)"""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="blocked_on_user")
    row = _row(sid, cwd_repo=data.parent / "repo")
    IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(), user_home="/home/x")
    assert dev_nudge_seams["calls"]["dev_nudge"] == []


def test_dev_with_no_task_id_never_nudged(tmp_path, dev_nudge_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id=None)
    row = _row(sid, cwd_repo=data.parent / "repo", task_id=None)
    IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(), user_home="/home/x")
    assert dev_nudge_seams["calls"]["dev_nudge"] == []


def test_dev_nudged_at_5min_before_the_55min_recycle_window(tmp_path, dev_nudge_seams):
    """T-0930: the whole point of decoupling — 5min, not 55min."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="open")
    dev_nudge_seams["state"]["idle_age"] = 310.0  # > 300 (5min), << 3300 (55min)
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert len(dev_nudge_seams["calls"]["dev_nudge"]) == 1


def test_dev_nudge_not_due_when_jsonl_fresh(tmp_path, dev_nudge_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="open")
    dev_nudge_seams["state"]["idle_age"] = 10.0
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert dev_nudge_seams["calls"]["dev_nudge"] == []


def test_dev_nudge_anti_loop_blocks_within_same_cadence_window(tmp_path, dev_nudge_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    just_sent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="open",
                          extra_md={"dev_nudge_last_at": just_sent})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert dev_nudge_seams["calls"]["dev_nudge"] == []


def test_dev_nudge_rearms_after_cadence_window_elapses(tmp_path, dev_nudge_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    long_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() - IT.dev_nudge_sec() - 10))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="open",
                          extra_md={"dev_nudge_last_at": long_ago})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert len(dev_nudge_seams["calls"]["dev_nudge"]) == 1


def test_dev_nudge_tracked_job_skips_nudge(tmp_path, dev_nudge_seams):
    """'если он не ждет build' — the SAME tracked-job auto-postpone the
    terminate path already uses."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="open")
    _enqueue_deploy(data, sid, phase="processing")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert dev_nudge_seams["calls"]["dev_nudge"] == []


def test_dev_nudge_postpone_active_skips_nudge(tmp_path, dev_nudge_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800))
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="open",
                          extra_md={"idle_postpone_until": future})
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert dev_nudge_seams["calls"]["dev_nudge"] == []


def test_dev_nudge_waits_for_composer_ready(tmp_path, dev_nudge_seams):
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042",
                          task_status="open")
    dev_nudge_seams["state"]["buf"] = "working… esc to interrupt\n"  # mid-turn
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert dev_nudge_seams["calls"]["dev_nudge"] == []


def test_dev_drive_unmet_helper(tmp_path, seams):
    cfg, data = _make_cfg(tmp_path, sid="S-almdudleer-bot-squad-demo-p5",
                          window="demo", task_id="T-0042", task_status="open")
    assert IT.dev_drive_unmet(cfg, "bot-squad", "T-0042") is True
    assert IT.dev_drive_unmet(cfg, "bot-squad", None) is False
    assert IT.dev_drive_unmet(cfg, "bot-squad", "") is False
    assert IT.dev_drive_unmet(cfg, "bot-squad", "T-9999-missing") is False


def test_dev_drive_unmet_false_for_done_and_waiting(tmp_path):
    for status, expected in (("totest", False), ("closed", False),
                             ("blocked_on_user", False), ("open", True),
                             ("in_progress", True), ("paused", True)):
        tp = tmp_path / status
        tp.mkdir()
        cfg, data = _make_cfg(tp, sid="S-almdudleer-bot-squad-demo-p5",
                              window="demo", task_id="T-0042", task_status=status)
        assert IT.dev_drive_unmet(cfg, "bot-squad", "T-0042") is expected, status


# --- T-0655: drive=off is the ONLY thing that permits an operator to recycle -

def test_drive_off_operator_falls_through_to_normal_recycle(tmp_path, keepalive_seams):
    """Once the operator itself sets drive=off, the normal terminate-and-
    remember machinery applies — but WITHOUT the resumable/resume_hint bait
    (self-terminate, per the stakeholder's explicit preference)."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None,
                          extra_md={"drive": "off"})
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert keepalive_seams["calls"]["keepalive"] == []
    # default seams tokens > threshold → it is ASKED to write its state (an
    # operator is task-less, so that is its role artifact), never /compact'd
    assert keepalive_seams["calls"]["compact"] == []
    assert len(keepalive_seams["calls"]["handoff"]) == 1
    assert keepalive_seams["calls"]["handoff"][0][0] == sid
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["idle_recycle_phase"] == "finalizing"


def test_drive_off_operator_below_threshold_self_terminates_without_resume_bait(tmp_path, keepalive_seams):
    sid = "S-almdudleer-bot-squad-operator-p1"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None,
                          extra_md={"drive": "off"})
    keepalive_seams["state"]["tokens"] = 5000  # below threshold — terminate immediately
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert keepalive_seams["calls"]["terminate"] == [sid]
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["status"] == "suspended"
    assert "recycled_at" in meta
    # T-0655: self_terminate — no resume bait for a deliberate operator stop
    assert "resumable" not in meta
    assert "resume_hint" not in meta


def test_drive_off_operator_finalize_compact_also_self_terminates(tmp_path, keepalive_seams):
    """The finalize half of an in-flight compact must ALSO self-terminate for
    an operator — role is threaded through, not just the arm half."""
    sid = "S-almdudleer-bot-squad-operator-p1"
    armed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg, data = _make_cfg(tmp_path, sid=sid, window="operator", task_id=None,
                          extra_md={"drive": "off",
                                    "idle_recycle_phase": "compacting",
                                    "idle_recycle_armed_at": armed})
    row = _operator_row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    assert keepalive_seams["calls"]["terminate"] == [sid]
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert "resumable" not in meta
    assert "resume_hint" not in meta


# --- T-0655 regression: dev/TL terminate-and-remember flow is UNCHANGED -----

def test_dev_role_recycle_still_stamps_resumable_unaffected_by_drive(tmp_path, seams):
    """Confirms the drive=on-operator exception never leaks onto a plain dev
    session — the existing 'exit when task is done' / resumable-recycle
    behaviour for dev/TL sessions is untouched by this ticket."""
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    seams["state"]["tokens"] = 5000  # below threshold — terminate immediately
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is True
    meta = S._read_session_metadata(data / "bot-squad" / "sessions" / f"{sid}.md")
    assert meta["resumable"] is True
    assert "resume_hint" in meta


def test_attached_check_failure_skips_recycle(tmp_path, seams, monkeypatch):
    """T-0564: a tmux list-clients error fails CLOSED (treated as attached)."""
    # exercise the REAL is_attached (undoing the seams fixture's stub) to prove
    # the fail-closed subprocess-error path, routed through the gate.
    monkeypatch.setattr(IT.recycle_gate, "is_attached", _REAL_IS_ATTACHED)
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("tmux not found")))
    sid = "S-almdudleer-bot-squad-demo-p5"
    cfg, data = _make_cfg(tmp_path, sid=sid, window="demo", task_id="T-0042")
    row = _row(sid, cwd_repo=data.parent / "repo")
    assert IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x") is False
    assert seams["calls"]["compact"] == [] and seams["calls"]["terminate"] == []
