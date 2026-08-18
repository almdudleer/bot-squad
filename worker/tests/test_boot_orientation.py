"""T-0904 — a provider whose CLI runs no SessionStart hook still gets oriented.

THE INCIDENT THIS PINS. A codex-provider operator was peer-messaged three
times. Each send injects the literal words ``check mail`` into the recipient's
pane — a convention that only means "run ``bsq inbox check``" to a session that
was TOLD so at boot. Codex had not been told by anything: the SessionStart hook
is wired in ``.claude/settings.json`` and Codex has no hooks, the bsq skill is
reached through Claude Code's project-skill discovery, ``AGENTS.md`` does not
exist in this repo, and the operator re-drive's standing-task text mentions
neither ``bsq`` nor the bus. So it searched the host for a mail client, found
none, and answered "I can't access mail yet: no Gmail/Outlook connector is
connected". Three times. ``bsq inbox check`` was never run.

Two fixes, and each test below belongs to one of them:

- BOOT: ``spawn``/``resume`` prepend the orientation to the delivered prompt
  for a hook-less provider — and deliver it even when the caller supplied no
  brief at all, which is the state the re-driven operator was actually in.
- NUDGE: ``inject_input`` makes the nudge self-describing for such a provider,
  because a prompt preamble is delivered once and does not survive a compaction
  while the nudge arrives every time.

EVERY assertion here has a CLAUDE-ARM NEGATIVE CONTROL. The claude path must
stay byte-identical: the bare string ``check mail`` is load-bearing (the
``input_mux`` verbatim contract, the harvest filters that drop it from
stakeholder guidance), and a "fix" that quietly rewrote it for the whole fleet
would be a regression wearing a green suite.
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from bot_squad_worker import agent_provider, boot_orientation as B
from bot_squad_worker.sessions import resume, spawn, _write_session_metadata


# ---------------------------------------------------------------------------
# Harness — a deliberately LOCAL copy (same reason test_explicit_model_effort
# gives): a peer's edits to test_sessions.py must not be able to move this
# file's ground.
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path, repo_path: Path) -> Any:
    from bot_squad_worker.config import Config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "test-project" / "backlog").mkdir(parents=True, exist_ok=True)
    (data_dir / "test-project" / "sessions").mkdir(parents=True, exist_ok=True)

    (cfg_dir / "projects.toml").write_text(
        "[projects.test-project]\n"
        'slug = "test-project"\n'
        'display_name = "Test Project"\n'
        f'repo_path = "{repo_path}"\n'
        'deploy_branch = "bot_squad/dev"\n'
        'master_branch = "master"\n'
        'prod_url = ""\n'
        'staging_url = ""\n'
        'dev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        'tg_chat = "0"\n'
        "created_at = 2026-05-10\n"
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    cfg = Config.load(cfg_dir)
    return types.SimpleNamespace(
        projects=cfg.projects,
        data_dir=data_dir,
        config_dir=cfg_dir,
        tg_bot_token=cfg.tg_bot_token,
    )


def _stub_tmux(monkeypatch, repo: Path, window: str, delivered: list[str],
               pane_cmd: str = "claude"):
    def fake_run(args, **kwargs):
        if "list-panes" in args:
            return subprocess.CompletedProcess(
                args, 0, f"%6|{window}|123|{repo}|{pane_cmd}\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    import bot_squad_worker.sessions as S

    monkeypatch.setattr(S, "_run", fake_run)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(repo.parent))
    monkeypatch.setattr(S.time, "sleep", lambda x: None)
    monkeypatch.setattr(S, "_wait_for_agent_composer_ready", lambda *_: True)
    monkeypatch.setattr(
        S, "_deliver_prompt",
        lambda pane_id, text, **kw: delivered.append(text))


def _spawn_delivered(tmp_path, monkeypatch, **spawn_kw) -> list[str]:
    """Everything ``spawn`` handed to the composer. Empty list = nothing sent."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)
    delivered: list[str] = []
    _stub_tmux(monkeypatch, repo, "w", delivered)
    spawn(cfg, "test-project", "w", **spawn_kw)
    return delivered


def _resume_delivered(tmp_path, monkeypatch, provider: str,
                      initial_prompt: str | None) -> list[str]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)
    sessions_dir = cfg.data_dir / "test-project" / "sessions"
    meta = {
        "sid": "S-u-w-p2", "status": "suspended", "window": "w",
        "cwd": str(repo), "claude_uuid": "u-1", "task_id": "T-0001",
        "provider": provider, "role": "operator",
    }
    _write_session_metadata(sessions_dir / f"{meta['sid']}.md", meta)
    delivered: list[str] = []
    _stub_tmux(monkeypatch, repo, "w", delivered)
    resume(cfg, "test-project", meta["sid"], initial_prompt=initial_prompt)
    return delivered


# ---------------------------------------------------------------------------
# The provider capability itself
# ---------------------------------------------------------------------------

def test_claude_runs_the_session_start_hook():
    assert agent_provider.get("claude").runs_session_start_hook is True


def test_codex_runs_no_session_start_hook():
    """The measured fact the whole ticket rests on: ``~/.codex/config.toml``
    has no hooks section, so nothing ever calls session_start.sh for codex."""
    assert agent_provider.get("codex").runs_session_start_hook is False


def test_needs_prompt_orientation_tracks_the_capability_for_every_provider():
    """Generic over the registry, not a hand-listed pair — a provider added
    later is covered by this file without anyone remembering to edit it."""
    for name in agent_provider.PROVIDERS:
        expected = not agent_provider.get(name).runs_session_start_hook
        assert B.needs_prompt_orientation(name) is expected, name


def test_a_provider_that_forgets_the_flag_gets_the_orientation():
    """FAIL-SAFE DEFAULT, and the direction matters. Firing bot-squad's
    SessionStart hook is a property of exactly one CLI, so every provider added
    later is by definition not that one. A provider whose author omits the
    field must land on "needs orientation" — the cost of being wrong that way
    is a preamble a hooked CLI would have duplicated; the cost of being wrong
    the other way is this ticket."""

    class NewCli(agent_provider.AgentProvider):
        def __init__(self):
            super().__init__("newcli", "newcli", ("$",))

    assert NewCli().runs_session_start_hook is False
    # ...and once it is registered like any real provider, the orientation
    # actually follows from that default rather than needing a second edit.
    registry = dict(agent_provider._REGISTRY)
    registry["newcli"] = NewCli()
    saved, agent_provider._REGISTRY = agent_provider._REGISTRY, registry
    try:
        assert B.needs_prompt_orientation("newcli") is True
        assert "bsq inbox check" in B.mail_nudge("newcli")
    finally:
        agent_provider._REGISTRY = saved


def test_claude_states_the_flag_rather_than_inheriting_it():
    """The one provider that DOES fire the hook says so explicitly, so the
    fail-safe default above can never silently switch claude's behaviour."""
    import inspect

    src = inspect.getsource(agent_provider.ClaudeProvider.__init__)
    assert "runs_session_start_hook=True" in src


def test_unknown_provider_falls_back_to_the_default_not_to_orientation():
    """Fail-SAFE, not fail-open: an unresolvable provider is treated as the
    default (claude), so a lookup miss can never rewrite a claude session's
    nudge or bolt a preamble onto its brief."""
    assert B.needs_prompt_orientation(None) is False
    assert B.needs_prompt_orientation("") is False
    assert B.needs_prompt_orientation("no-such-cli") is False


# ---------------------------------------------------------------------------
# The nudge
# ---------------------------------------------------------------------------

def test_claude_nudge_is_byte_identical_to_the_legacy_signal():
    """NEGATIVE CONTROL for every nudge assertion below."""
    assert B.mail_nudge("claude") == "check mail"
    assert B.MAIL_SIGNAL == "check mail"


def test_codex_nudge_names_the_command_to_run():
    text = B.mail_nudge("codex")
    assert text != B.MAIL_SIGNAL
    assert "bsq inbox check" in text


def test_codex_nudge_says_it_is_not_email():
    """The literal thing that went wrong: it read `check mail` as e-mail and
    went hunting for a connector. The nudge has to close that reading itself."""
    assert "not email" in B.mail_nudge("codex")


def test_every_hookless_provider_nudge_is_a_single_line():
    """``inject_input``'s transport sends one send-keys + Enter PER LINE, so a
    two-line nudge is TWO composer submissions — the session starts answering
    line 1 while line 2 is still arriving."""
    for name in agent_provider.PROVIDERS:
        assert "\n" not in B.mail_nudge(name), name


def test_every_nudge_stays_inside_the_measured_budget():
    """MEASURED, not tidiness (arms on T-0904, live codex pane %381):

        10 chars  submitted        87 chars benign  submitted
        63 chars  submitted        83 chars nudge   NOT submitted, 2/2,
        66 chars  submitted                         via BOTH transports

    Length, wrapping, the em-dash, the apostrophe and the semicolon were each
    ruled out by an arm that passed while carrying them, so the mechanism
    behind the 83-char failure is NOT known. What is known is that a silent
    non-submit is possible on this transport and that short nudges have never
    shown it — which is a reason to hold the budget, and a reason this test
    carries the measurement instead of asserting a cause it cannot support."""
    for name in agent_provider.PROVIDERS:
        assert len(B.mail_nudge(name)) <= B.MAIL_NUDGE_MAX_LEN, name


def test_hookless_nudge_still_starts_with_the_legacy_signal():
    """Keeps the expanded form recognisable to anything matching on the
    original prefix (and to a human reading a pane)."""
    assert B.mail_nudge("codex").startswith(B.MAIL_SIGNAL)


# ---------------------------------------------------------------------------
# The orientation block
# ---------------------------------------------------------------------------

def test_orientation_block_carries_the_check_mail_mapping():
    block = B.orientation_block(sid="S-u-w-p9", slug="test-project")
    assert "check mail" in block
    assert "bsq inbox check" in block


def test_orientation_block_names_the_cli_and_the_full_briefing():
    block = B.orientation_block(sid="S-u-w-p9", slug="test-project")
    assert "bsq --help" in block
    assert "bsq brief" in block
    assert "bsq peer send" in block


def test_orientation_block_tells_the_session_its_own_sid():
    """The hook's MESSAGE BUS block ends with "Your SID is: …"; without it a
    hook-less session cannot address itself in `bsq` at all."""
    assert "S-u-w-p9" in B.orientation_block(sid="S-u-w-p9")


def test_orientation_block_points_at_the_project_instructions(tmp_path):
    block = B.orientation_block(slug="test-project", data_dir=tmp_path)
    assert str(tmp_path / "test-project" / "AGENT_INSTRUCTIONS.md") in block


def test_orientation_block_omits_fields_it_was_not_given(tmp_path):
    """An unresolved field is left OUT rather than rendered as a placeholder
    that reads like a real value."""
    block = B.orientation_block()
    assert "Your SID:" not in block
    assert "Project:" not in block
    assert "AGENT_INSTRUCTIONS.md" not in block


def test_orientation_names_the_same_command_the_claude_hook_names():
    """Anti-drift: the hook and this block are two renderings of one mapping.
    Scoped to the hook's BUS_EOF heredoc, not the whole 900-line script, so it
    reports on the block under test and nothing else (T-0777)."""
    hook = (Path(__file__).resolve().parents[2]
            / "scripts" / "hooks" / "session_start.sh").read_text()
    open_tag = "cat <<BUS_EOF\n"
    start = hook.index(open_tag) + len(open_tag)
    bus_block = hook[start:hook.index("\nBUS_EOF", start)]
    assert "bsq inbox check" in bus_block, "hook block moved — re-scope this test"
    assert "bsq inbox check" in B.orientation_block()


# ---------------------------------------------------------------------------
# with_orientation — composition
# ---------------------------------------------------------------------------

def test_with_orientation_keeps_the_task_text_intact():
    out = B.with_orientation("Drive T-0904 to green.", sid="S-u-w-p9")
    assert "Drive T-0904 to green." in out
    assert "bsq inbox check" in out


def test_with_orientation_puts_the_orientation_first():
    out = B.with_orientation("Drive T-0904 to green.", sid="S-u-w-p9")
    assert out.index("bsq inbox check") < out.index("Drive T-0904 to green.")


def test_with_orientation_alone_when_there_is_no_brief():
    """The re-driven operator's actual state on some paths: a session that was
    handed no brief still has to be told what it is sitting in."""
    for empty in (None, "", "   "):
        out = B.with_orientation(empty, sid="S-u-w-p9")
        assert "bsq inbox check" in out
        assert "Your task follows" not in out


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------

def test_spawn_codex_prepends_orientation_to_the_brief(tmp_path, monkeypatch):
    delivered = _spawn_delivered(
        tmp_path, monkeypatch, provider="codex",
        initial_prompt="Your standing task: clear the backlog.")
    assert len(delivered) == 1
    assert "bsq inbox check" in delivered[0]
    assert "Your standing task: clear the backlog." in delivered[0]


def test_spawn_codex_delivers_orientation_even_with_no_brief(tmp_path, monkeypatch):
    """Previously ``spawn`` delivered NOTHING without an initial_prompt, which
    is exactly how a codex session reached a live pane knowing nothing."""
    delivered = _spawn_delivered(tmp_path, monkeypatch, provider="codex")
    assert len(delivered) == 1
    assert "bsq inbox check" in delivered[0]


def test_spawn_codex_orientation_names_the_new_sid(tmp_path, monkeypatch):
    delivered = _spawn_delivered(tmp_path, monkeypatch, provider="codex")
    assert "S-u-w-p6" in delivered[0]


def test_spawn_claude_brief_is_delivered_byte_identical(tmp_path, monkeypatch):
    """NEGATIVE CONTROL. Claude gets the orientation from its hook; a preamble
    here would be duplication in every single spawn."""
    brief = "Your standing task: clear the backlog."
    delivered = _spawn_delivered(
        tmp_path, monkeypatch, provider="claude", initial_prompt=brief)
    assert delivered == [brief]


def test_spawn_claude_with_no_brief_delivers_nothing(tmp_path, monkeypatch):
    """NEGATIVE CONTROL for the un-gating above — it must not start typing
    into claude panes that were deliberately spawned empty."""
    assert _spawn_delivered(tmp_path, monkeypatch, provider="claude") == []


# ---------------------------------------------------------------------------
# resume()
# ---------------------------------------------------------------------------

def test_resume_codex_prepends_orientation(tmp_path, monkeypatch):
    """The re-drive path: it RESUMES the operator with the standing task, so a
    spawn-only fix would have left the reported incident live."""
    delivered = _resume_delivered(
        tmp_path, monkeypatch, "codex", "Your standing task: clear the backlog.")
    assert len(delivered) == 1
    assert "bsq inbox check" in delivered[0]
    assert "Your standing task: clear the backlog." in delivered[0]


def test_resume_codex_with_no_brief_still_orients(tmp_path, monkeypatch):
    delivered = _resume_delivered(tmp_path, monkeypatch, "codex", None)
    assert len(delivered) == 1
    assert "bsq inbox check" in delivered[0]


def test_resume_claude_is_byte_identical(tmp_path, monkeypatch):
    """NEGATIVE CONTROL."""
    brief = "Delta brief: pick up T-0904."
    assert _resume_delivered(tmp_path, monkeypatch, "claude", brief) == [brief]


def test_resume_claude_with_no_brief_delivers_nothing(tmp_path, monkeypatch):
    assert _resume_delivered(tmp_path, monkeypatch, "claude", None) == []


# ---------------------------------------------------------------------------
# inject_input — the nudge chokepoint every send site funnels through
# ---------------------------------------------------------------------------

def _inject(tmp_path, monkeypatch, pane_cmd: str, text: str,
            md_provider: str | None = None) -> str:
    """Run the real ``inject_input`` action and return what reached the pane."""
    import bot_squad_worker.actions as A
    import bot_squad_worker.sessions as S
    from bot_squad_worker import input_mux

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = _make_cfg(tmp_path, repo)
    sid = "S-u-w-p6"
    if md_provider is not None:
        _write_session_metadata(
            cfg.data_dir / "test-project" / "sessions" / f"{sid}.md",
            {"sid": sid, "provider": md_provider, "window": "w",
             "cwd": str(repo), "status": "active"},
        )

    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(
        S, "list_panes",
        lambda: [S.PaneInfo(pane_id="%6", window="w", pid="123",
                            cwd=str(repo), command=pane_cmd)])
    sent: list[str] = []
    monkeypatch.setattr(
        input_mux, "deliver_direct",
        lambda data_dir, sid_, pane_id, text_, **kw: (sent.append(text_), 1)[1])

    A._action_inject_input({"sid": sid, "text": text})
    assert len(sent) == 1
    return sent[0]


def test_inject_input_expands_the_nudge_for_a_codex_pane(tmp_path, monkeypatch):
    out = _inject(tmp_path, monkeypatch, "codex", "check mail")
    assert "bsq inbox check" in out


def test_inject_input_leaves_the_nudge_bare_for_a_claude_pane(tmp_path, monkeypatch):
    """NEGATIVE CONTROL — the claude fleet's nudge is unchanged."""
    assert _inject(tmp_path, monkeypatch, "claude", "check mail") == "check mail"


def test_inject_input_reads_the_session_md_when_the_agent_has_shelled_out(
        tmp_path, monkeypatch):
    """A codex pane running a child command reports ``bash`` to tmux, which
    resolves to no provider at all. The stamped md is the second reading."""
    out = _inject(tmp_path, monkeypatch, "bash", "check mail",
                  md_provider="codex")
    assert "bsq inbox check" in out


def test_inject_input_unresolvable_provider_keeps_the_bare_nudge(
        tmp_path, monkeypatch):
    """Neither reading answers (no agent in the pane, no session md) — the
    fail-safe is the legacy string, never the expanded one."""
    assert _inject(tmp_path, monkeypatch, "bash", "check mail") == "check mail"


@pytest.mark.parametrize("pane_cmd", ["claude", "codex"])
def test_inject_input_passes_other_text_through_untouched(
        tmp_path, monkeypatch, pane_cmd):
    """Scoped to the nudge and ONLY the nudge: ``/compact`` and every other
    injected payload stay byte-identical on both providers."""
    assert _inject(tmp_path, monkeypatch, pane_cmd, "/compact") == "/compact"
    assert _inject(tmp_path, monkeypatch, pane_cmd,
                   "check mail later") == "check mail later"
