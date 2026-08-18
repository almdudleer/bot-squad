"""T-0904 — provider-neutral boot orientation for agent CLIs with no hooks.

WHAT WENT WRONG (measured, 2026-08-18). A codex-provider operator was sent three
peer messages. Each one injects the literal words ``check mail`` into the
recipient's pane, a convention every bot-squad session is supposed to know means
"run ``bsq inbox check``". Codex did not know it — it searched the host for
himalaya/meli/notmuch/aerc/mutt, found none, and answered "I can't access mail
yet: no Gmail/Outlook connector is connected". Three times. ``bsq inbox check``
was never run.

The wording was not the bug. That session had received **no bot-squad boot
orientation through any channel**, because every channel that carries it is
Claude-only:

- ``scripts/hooks/session_start.sh`` prints the MESSAGE BUS block, but it is a
  ``SessionStart`` hook wired in ``.claude/settings.json``; Codex has no hooks.
- ``framework-skills/bot-squad-cli/SKILL.md`` is reached through Claude Code's
  project-skill discovery; Codex enumerates only ``~/.codex/skills/.system/*``.
- ``AGENTS.md`` (which Codex *does* auto-read) does not exist in this repo —
  ``scripts/cli/render_agents_md.py`` can render one but no code path calls it.
- ``bsq --help``'s epilog explains it, to a session that already knows to run
  ``bsq``.

So this module puts the orientation on the ONE channel that reaches every
provider and is already proven to work: the composer-delivered prompt. Two
seams consume it, and they are complementary rather than redundant:

1. :func:`with_orientation` — ``sessions.spawn`` / ``sessions.resume`` prepend
   the block to whatever brief they were going to deliver (and deliver it alone
   when there is no brief). This is the boot-time fix, and it covers every
   spawn caller — the operator re-drive, ``bsq spawn``, autopilot, routines,
   constant teams — because they all funnel through those two functions.
2. :func:`mail_nudge` — ``actions._action_inject_input`` makes the nudge itself
   self-describing for such a provider. A prompt preamble is delivered once and
   is gone after the first compaction; the nudge arrives every time, so it has
   to stand on its own.

Deliberately NOT done here: rendering ``AGENTS.md`` at spawn time. It would
reach Codex (it auto-reads it) but it mutates the SHARED working tree on every
spawn, racing whichever peers are mid-edit in it — a side effect the prompt
channel does not have. Generating and committing an ``AGENTS.md`` for this repo
is a legitimate separate change; it is not something a spawn should do.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bot_squad_worker import agent_provider as _agent_provider


#: The bare peer-bus nudge, unchanged for providers that get the hook.
MAIL_SIGNAL = "check mail"

#: The self-describing form for a provider with no SessionStart hook.
#:
#: SHORT AND ONE LINE, both measured rather than chosen (see MAIL_NUDGE_MAX_LEN
#: and the arms recorded on T-0904). This exact string was verified on a live
#: codex pane: it submitted and the session answered "Draining the bot-squad
#: peer bus now." A longer 83-char draft did NOT submit — twice, through both
#: transports — while an 87-char benign line did, so the failure is neither
#: length nor wrapping nor any of the punctuation tested. Until that is
#: explained, staying close to the shape that has always worked on this
#: transport is the safe design, not a stylistic preference.
MAIL_SIGNAL_SELF_DESCRIBING = (
    "check mail = bot-squad peer bus, not email: run bsq inbox check"
)

#: Nudge budget. ``inject_input`` sends one send-keys + Enter PER LINE, so the
#: nudge must be one line; this keeps it inside one 80-column DISPLAY line too.
MAIL_NUDGE_MAX_LEN = 76


def needs_prompt_orientation(provider_name: str | None) -> bool:
    """True when this provider gets NO SessionStart hook, so the orientation
    has to ride the prompt instead."""
    return not _agent_provider.get(provider_name).runs_session_start_hook


def mail_nudge(provider_name: str | None) -> str:
    """The peer-bus nudge text to inject for ``provider_name``.

    Claude sessions keep the bare ``check mail`` they have always had (the hook
    taught them what it means, and the string is pinned by tests + the
    ``input_mux`` byte-identical contract). A hook-less provider gets the
    single-line self-describing form.
    """
    if needs_prompt_orientation(provider_name):
        return MAIL_SIGNAL_SELF_DESCRIBING
    return MAIL_SIGNAL


def orientation_block(
    *,
    sid: str = "",
    slug: str = "",
    role: str = "",
    data_dir: Any = None,
) -> str:
    """The boot orientation itself — what the SessionStart hook would have said.

    Every field is optional: a caller that cannot resolve one leaves it out
    rather than printing a placeholder that reads like a real value.
    """
    ident: list[str] = []
    if sid:
        ident.append(f"  Your SID: {sid}")
    if slug:
        ident.append(f"  Project:  {slug}")
    if role:
        ident.append(f"  Role:     {role}")

    instructions = ""
    if data_dir and slug:
        instructions = str(Path(data_dir) / slug / "AGENT_INSTRUCTIONS.md")

    lines = [
        "=== BOT-SQUAD SESSION ORIENTATION ===",
        "(auto-injected because your agent CLI runs no SessionStart hook — this",
        "is the same orientation Claude-provider sessions get from that hook.)",
        "",
        "You are a worker session inside bot-squad, not a standalone assistant.",
    ]
    if ident:
        lines += [""] + ident
    lines += [
        "",
        "1. `bsq` IS THE SYSTEM INTERFACE. It is on your PATH; run it from this",
        "   repo. `bsq --help` lists every verb, `bsq <verb> --help` its flags.",
        "   It resolves your project from $PWD and your SID from tmux by itself.",
        "",
        '2. "check mail" IS NOT E-MAIL, AND THERE IS NO MAIL CLIENT ON THIS HOST.',
        "   When another session sends you something, bot-squad's peer message",
        "   bus types a one-line nudge into your composer — `check mail`, or a",
        "   longer line that starts with those words and says the same thing.",
        "   That is the whole signal. When you see it, run exactly:",
        "       bsq inbox check",
        "   Send to a peer with:",
        '       bsq peer send <SID|teamlead|dev|all> "<text>"',
        "   Never go looking for a mail account, connector, or mail CLI.",
        "",
        "3. READ YOUR FULL BRIEFING BEFORE ANYTHING ELSE — product, team",
        "   protocol, your role contract and your task bindings, one command:",
        "       bsq brief",
    ]
    if instructions:
        lines += [
            "",
            "4. Project recipes, paths and git rules:",
            f"       {instructions}",
        ]
    lines.append("=== END ORIENTATION ===")
    return "\n".join(lines)


def with_orientation(
    prompt: str | None,
    *,
    sid: str = "",
    slug: str = "",
    role: str = "",
    data_dir: Any = None,
) -> str:
    """``prompt`` with the orientation block prepended.

    Returns the block alone when there is no prompt — a hook-less session that
    was spawned with no brief still has to be told what it is sitting in.
    """
    block = orientation_block(sid=sid, slug=slug, role=role, data_dir=data_dir)
    body = (prompt or "").strip()
    if not body:
        return block
    return f"{block}\n\nYour task follows.\n\n{body}"
