"""T-0616 (D-0053 root cause): the SessionStart hook's registry write must
preserve frontmatter fields it doesn't manage, instead of rebuilding the md
from its fixed template.

Incident (2026-07-04/05 evidence run, D-0053 §4): SessionStart fires with
``source=compact`` the moment a ``/compact`` completes; the hook's template
rewrite dropped ``idle_recycle_phase``/``idle_recycle_armed_at``, so the next
idle tick saw no in-flight recycle and ARMED AGAIN — every recycle
double-compacted (p11/p23/p29) and the stakeholder's own user-session-p8
looped on re-compacts. The same drop silently ate ``role`` (bsq morph),
``idle_postpone_until`` (bsq postpone) and any future stamp.

Executed against the REAL hook script with a fake tmux on PATH and a
throwaway $BOT_SQUAD (hermetic — never touches the live registry; cf the
session_start-hook-clobbers-live-md lesson). The end-to-end regression drives
the actual ``idle_timeout.maybe_recycle`` against the hook-rewritten md:
arm → hook rewrite (source=compact) → NO second arm.
"""
from __future__ import annotations

import os
import stat
import subprocess
import time
import types
from pathlib import Path

import pytest

from bot_squad_worker import autocompact as A
from bot_squad_worker import idle_timeout as IT
from bot_squad_worker import recycle_gate as G
from bot_squad_worker import sessions as S
from tests.test_session_start_marker_guard import FAKE_TMUX, _find_hook, _user

WINDOW = "t0616-work"
PANE = "p7"  # FAKE_TMUX answers #{pane_id} with %7


@pytest.fixture()
def hook_env(tmp_path):
    """Throwaway $BOT_SQUAD (slug ``bot-squad`` so the recycle allowlist
    default applies) + repo + fake tmux; returns (run_hook, repo, sessions_dir).
    ``run_hook(source=…)`` varies the SessionStart source."""
    real_hook = _find_hook("session_start.sh")
    derive = _find_hook("derive_role.sh")
    my_sid = _find_hook("hook_my_sid.sh")
    if not (real_hook and derive and my_sid):
        pytest.skip("hook scripts not reachable from test cwd")

    bs = tmp_path / "bs"
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (bs / "scripts" / "hooks").mkdir(parents=True)
    (bs / "config").mkdir(parents=True)
    os.symlink(derive, bs / "scripts" / "hooks" / "derive_role.sh")
    os.symlink(my_sid, bs / "scripts" / "hooks" / "hook_my_sid.sh")
    (bs / "config" / "projects.toml").write_text(
        '[projects.bot-squad]\nrepo_path = "%s"\n' % repo)
    sessions_dir = bs / "data" / "bot-squad" / "sessions"
    sessions_dir.mkdir(parents=True)
    (bs / "data" / "bot-squad" / "vision").mkdir(parents=True)

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(FAKE_TMUX)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    def run_hook(*, source="compact", window=WINDOW,
                 claude_sid="11111111-2222-3333-4444-555555555555"):
        env = dict(os.environ)
        env["BOT_SQUAD"] = str(bs)
        env["PATH"] = f"{shim_dir}:{env['PATH']}"
        env["TMUX"] = "/tmp/fake-tmux-sock,123,0"
        env["TMUX_PANE"] = "%7"
        env["FAKE_WINDOW"] = window
        for k in ("BOT_SQUAD_TASK_ID", "BOT_SQUAD_INITIATIVE",
                  "BOT_SQUAD_OWNER", "BOT_SQUAD_OWNER_USER"):
            env.pop(k, None)
        return subprocess.run(
            ["bash", str(real_hook)],
            input='{"session_id":"%s","source":"%s"}' % (claude_sid, source),
            capture_output=True, text=True, cwd=str(repo), env=env, timeout=30,
        )

    return run_hook, repo, sessions_dir


def _sid() -> str:
    return f"S-{_user()}-{WINDOW}-{PANE}"


def _seed_md(sessions_dir: Path, repo: Path, extra: str = "") -> Path:
    # tmux_session / linux_user match what the fake tmux + SID resolve to, so
    # the hook's (pre-existing) refresh of those managed fields is a no-op and
    # the roundtrip test below can compare whole files.
    md = sessions_dir / f"{_sid()}.md"
    md.write_text(
        "---\n"
        f"sid: {_sid()}\n"
        "status: active\n"
        f"window: {WINDOW}\n"
        f"cwd: {repo}\n"
        "claude_uuid: old-uuid\n"
        "task_id: ~\n"
        "initiative: ~\n"
        "extra_task_ids: []\n"
        "extra_initiatives: []\n"
        "started_at: 2026-07-05T10:00:00Z\n"
        "owner: ~\n"
        "owner_user: ~\n"
        "tmux_session: fake-tmux-session\n"
        f"linux_user: {_user()}\n"
        + extra +
        "---\n"
    )
    return md


INFLIGHT = ("idle_recycle_phase: compacting\n"
            "idle_recycle_armed_at: 2026-07-05T14:00:00Z\n")
DURABLE = ("role: operator\n"
           "idle_postpone_until: 2026-07-05T09:00:00Z\n"
           "recycle_exempt: true\n")


def test_compact_fire_preserves_unknown_fields(hook_env):
    """The D-0053 clobber: a source=compact rewrite must keep the in-flight
    recycle phase AND every other unmanaged field, verbatim."""
    run_hook, repo, sessions_dir = hook_env
    md = _seed_md(sessions_dir, repo, INFLIGHT + DURABLE)
    res = run_hook(source="compact")
    assert res.returncode == 0, res.stderr
    text = md.read_text()
    assert "idle_recycle_phase: compacting" in text
    assert "idle_recycle_armed_at: 2026-07-05T14:00:00Z" in text
    assert "role: operator" in text
    assert "idle_postpone_until: 2026-07-05T09:00:00Z" in text
    assert "recycle_exempt: true" in text
    # managed fields still refreshed / preserved as before
    assert "claude_uuid: 11111111-2222-3333-4444-555555555555" in text
    assert "started_at: 2026-07-05T10:00:00Z" in text


def test_double_fire_does_not_duplicate_passthrough_lines(hook_env):
    run_hook, repo, sessions_dir = hook_env
    md = _seed_md(sessions_dir, repo, INFLIGHT + DURABLE)
    assert run_hook(source="compact").returncode == 0
    assert run_hook(source="compact").returncode == 0
    text = md.read_text()
    for needle in ("idle_recycle_phase:", "role:", "recycle_exempt:"):
        assert text.count(needle) == 1, (needle, text)


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_non_compact_fire_clears_inflight_phase_keeps_durable(hook_env, source):
    """The in-flight recycle pair is only meaningful across the source=compact
    fire (the /compact the recycle itself sent). Any other source means a NEW
    life for the session — a surviving stamp would hand the next idle tick a
    timed-out finalize that terminates the fresh session. Durable stamps
    (morph role / postpone / exempt marker) survive every source."""
    run_hook, repo, sessions_dir = hook_env
    md = _seed_md(sessions_dir, repo, INFLIGHT + DURABLE)
    res = run_hook(source=source)
    assert res.returncode == 0, res.stderr
    text = md.read_text()
    assert "idle_recycle_phase" not in text
    assert "idle_recycle_armed_at" not in text
    assert "role: operator" in text
    assert "idle_postpone_until: 2026-07-05T09:00:00Z" in text
    assert "recycle_exempt: true" in text


# --- the T-0616 DoD regression: arm → hook rewrite → NO second arm ----------

@pytest.fixture
def seams(monkeypatch):
    """Same seam set as test_idle_timeout: no real tmux/telemetry/suspend."""
    calls = {"compact": [], "terminate": []}
    state = {"pane": "%7", "buf": "❯ ready\n", "idle_age": 7200.0,
             "tokens": 60000}
    monkeypatch.setattr(A, "_pane_for", lambda sid: state["pane"])
    monkeypatch.setattr(A, "_capture_pane", lambda pane: state["buf"])
    monkeypatch.setattr(A, "_send_compact", lambda sid: calls["compact"].append(sid))
    monkeypatch.setattr(IT, "_context_tokens", lambda cfg, slug, sid: state["tokens"])
    monkeypatch.setattr(G, "is_attached", lambda target: False)
    monkeypatch.setattr(S, "_pane_activity_at",
                        lambda cwd, uuid, home: time.time() - state["idle_age"])

    def _fake_suspend(cfg, slug, sid, source=None, reason=None):
        calls["terminate"].append(sid)
        md = S._session_file(cfg.data_dir, slug, sid)
        existing = S._read_session_metadata(md) or {}
        # Mirror the real live-pane suspend: rebuild from its fixed template
        # (unmanaged fields incl. the in-flight phase do NOT survive it).
        meta = {"sid": sid, "status": "suspended",
                "claude_uuid": existing.get("claude_uuid", "~"),
                "task_id": existing.get("task_id", "~"),
                "window": existing.get("window", "~")}
        if source:
            meta["suspend_source"] = source
        S._write_session_metadata(md, meta)
        return {"ok": True}
    monkeypatch.setattr(S, "suspend", _fake_suspend)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("BOT_SQUAD_IDLE_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("BOT_SQUAD_RECYCLE_PROJECTS", raising=False)
    return {"calls": calls, "state": state}


def _tick(data_dir: Path, md: Path) -> bool:
    cfg = types.SimpleNamespace(projects={"bot-squad": {}}, data_dir=data_dir)
    meta = S._read_session_metadata(md) or {}
    row = {"sid": _sid(), "status": "active", "window": meta.get("window"),
           "task_id": meta.get("task_id"), "role": None,
           "cwd": meta.get("cwd"), "claude_uuid": meta.get("claude_uuid"),
           "linux_user": ""}
    return IT.maybe_recycle(cfg, "bot-squad", row, now=time.time(),
                            user_home="/home/x")


def test_clobber_sequence_no_second_arm(hook_env, seams):
    """The exact p11/p23/p29 double-compact shape, made impossible:
    tick #1 ARMS (sends /compact, stamps the phase) → the compact completes
    and fires the REAL hook with source=compact (this used to clobber the
    stamp) → tick #2 must FINALIZE (terminate-and-remember), never re-arm.
    Exactly ONE /compact per recycle episode."""
    run_hook, repo, sessions_dir = hook_env
    md = _seed_md(sessions_dir, repo)
    data_dir = sessions_dir.parent.parent  # $BOT_SQUAD/data

    # tick #1: over-threshold idle session → ARM
    assert _tick(data_dir, md) is True
    assert seams["calls"]["compact"] == [_sid()]
    assert (S._read_session_metadata(md) or {}).get("idle_recycle_phase") == "compacting"

    # the /compact completes → Claude Code fires SessionStart(source=compact)
    res = run_hook(source="compact")
    assert res.returncode == 0, res.stderr
    assert (S._read_session_metadata(md) or {}).get("idle_recycle_phase") == \
        "compacting", "hook rewrite must not clobber the in-flight phase"

    # tick #2: composer ready again → FINALIZE, not a second arm
    assert _tick(data_dir, md) is True
    assert seams["calls"]["compact"] == [_sid()], "no second /compact — ever"
    assert seams["calls"]["terminate"] == [_sid()]
    after = S._read_session_metadata(md) or {}
    assert after.get("status") == "suspended"
    assert after.get("resumable") is True


def test_pre_hook_md_without_extras_roundtrips(hook_env):
    """A vanilla md (no unmanaged fields) is byte-stable apart from the
    refreshed claude_uuid — the passthrough adds nothing."""
    run_hook, repo, sessions_dir = hook_env
    md = _seed_md(sessions_dir, repo)
    before = md.read_text()
    assert run_hook(source="compact").returncode == 0
    after = md.read_text()
    assert after == before.replace("claude_uuid: old-uuid",
                                   "claude_uuid: 11111111-2222-3333-4444-555555555555")
