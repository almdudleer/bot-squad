#!/usr/bin/env bash
# Stop hook — intentionally a no-op since T-0155.
#
# This hook USED to fire a Telegram DM ("needs your input") on *every* idle
# Stop event. That flooded the stakeholder's DMs on every idle turn, often
# with a wrong SID prefix. Per T-0155 the implicit per-idle flood is removed.
#
# TG is now reached two ways instead:
#   1. Explicitly — an agent runs `bsq tg ping <message>` when it actually
#      needs the stakeholder (the tg_notify action).
#   2. Auto-escalation — the worker's tg_stall watchdog (tg_stall.py +
#      tg_stall_tick) pages the stakeholder only when an agent has been
#      blocked on him for >= tg_stall_minutes AND his tmux window isn't open.
#
# The hook is kept (rather than deleted) so the deployed Stop-hook wiring in
# every session's settings.json still resolves to a valid script. It must
# never exit non-zero — a broken Stop hook would wedge Claude Code sessions.
set -uo pipefail
exit 0
