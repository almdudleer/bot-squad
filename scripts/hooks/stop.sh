#!/usr/bin/env bash
# Stop hook — fires when the assistant finishes a turn (the session goes IDLE).
#
# T-0155: it no longer DMs the stakeholder (that flooded his DMs on every idle
# turn, often with a wrong SID prefix). TG is reached two other ways now:
#   1. Explicitly — an agent runs `bsq tg ping <message>` when it actually
#      needs the stakeholder (the tg_notify action).
#   2. Auto-escalation — the worker's tg_stall watchdog (tg_stall.py +
#      tg_stall_tick) pages the stakeholder only when an agent has been
#      blocked on him for >= tg_stall_minutes AND his tmux window isn't open.
#
# T-0470 (M1/F1.7): this is the canonical "session went idle" lifecycle signal.
# We stamp a per-SID idle-anchor marker so the lifecycle engine measures stall
# time from a HOOK signal (turn-end ≈ the subscription cache anchor) instead of
# inferring it from the transcript jsonl mtime. The marker is keyed by SID
# (per-pane) — NOT a cwd-level file — because several Claude panes can share one
# repo cwd; see worker/bot_squad_worker/lifecycle_events.py (the reader + SSOT
# for this marker convention: <cwd>/.claude/bsq_lifecycle/<sid>.stop).
#
# It must never exit non-zero — a broken Stop hook would wedge Claude Code.
set -uo pipefail

# Stamp the per-SID idle-anchor (session_stalled) marker. Best-effort + fully
# self-contained (no worker/python deps, so it can never fail a session).
{
    BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"
    sid=$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null) || sid=""
    if [ -n "$sid" ]; then
        mkdir -p .claude/bsq_lifecycle 2>/dev/null \
            && : > ".claude/bsq_lifecycle/${sid}.stop" 2>/dev/null || true
    fi
} >/dev/null 2>&1 || true

exit 0
