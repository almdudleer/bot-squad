---
name: hybrid-monitoring
description: Use when a metric or health signal is being watched by hand or by AI on a schedule — "we keep manually checking X", an eyeballed dashboard number, a periodic curl after deploys, a routine whose instruction amounts to "verify everything is ok" — or when an anomaly an AI session just diagnosed should become permanent coverage. Covers declaring code-level metric monitors with threshold triggers that attach AI only on breach.
---

# Hybrid monitoring — code watches, AI attaches on breach

Calling AI on a schedule to check a healthy system is waste. Declare a **monitor routine** instead: plain worker code runs a probe at any cadence (≥5s), a threshold judge confirms the breach (anti-flap), and only then an AI session is spawned, briefed with the firing event. Zero tokens while healthy.

## When a metric deserves a monitor

- a recurring manual check — someone curls / greps / eyeballs it
- a prod or staging signal re-verified after every deploy
- anything a scheduled AI sweep keeps re-confirming as "still ok"
- an anomaly an AI session just diagnosed → ratchet it (below)

## The ratchet rule (D-0048 §5.2, verbatim)

**never add an AI-checks-everything-ok routine — convert each anomaly the AI finds into a threshold monitor, so code coverage grows and AI attaches only on real signal.**

This includes "fallbacks": a cron routine whose instruction is "check X, exit if ok" is the same anti-pattern — the healthy-path check belongs in code, always.

## How to declare — engine is live; CLI flags are coming

`bsq routine declare` accepts **schedule triggers only** for now (monitor flags land in a later slice). Until then, declare through the worker API on the install host — it validates the spec, allocates the R-id, and writes the routine md. Do NOT hand-write routine mds or hand-pick ids.

```python
# install host, worker venv
from pathlib import Path
from bot_squad_worker.config import Config
from bot_squad_worker import routines

cfg = Config.load(Path("<install>/config"))
routines.declare(cfg, "<slug>", trigger="monitor",
    title="staging /var disk watch",
    instruction=("/var is filling (value, threshold, onset are in your TRIGGER "
                 "EVENT brief). Find the top growers, report to the operator "
                 "with a recommended cleanup. Do NOT delete anything yourself."),
    monitor={"probe": "shell",
             "cmd": "df -P /var | awk 'NR==2{sub(/%/,\"\",$5); print $5}'",
             "interval_s": 300, "timeout_s": 10,
             "judge": "numeric_gt", "threshold": 85,
             "persist_s": 600, "cooldown_s": 1800})
```

## Spec cheat-sheet (the `monitor:` block)

| key | meaning | notes |
|---|---|---|
| `probe` + `cmd` | `shell` + the command | only `shell` today; `http` is a coming slice |
| `interval_s` | probe cadence, seconds | min 5, default 30 |
| `timeout_s` | probe hard-kill, seconds | **mandatory** — declare-time error if missing |
| `judge` + `threshold` | breach test | see below |
| `persist_s` | breach must HOLD this long before the first fire | **seconds, not a failure count**; default 0 — always set it |
| `cooldown_s` | min gap between fires of one ongoing breach | seconds; default 0 — always set it |
| `on_breach` | `spawn` (attach AI) | only `spawn` today; `notify` (code-only alert) is a coming slice |

Judges: `numeric_gt` / `numeric_lt` / `numeric_ne` — stdout must parse as a number, and a nonzero exit means *probe error*, never a breach · `nonzero_exit` — the exit code IS the signal (no threshold needed) · `regex_match` — `re.search(threshold, stdout)`.

**Anti-flap defaults: `persist_s` ≥ 2 × `interval_s`, `cooldown_s` ≥ 1800 (30 min).** Deviate only with an argued reason (e.g. a pager-grade signal where a single confirmed sample must fire).

## What happens on fire

- One session spawns with a **TRIGGER EVENT** brief: value, judge, threshold, breach onset, fires in the last 24h, last result-artifact pointer — it starts knowing why it exists, no re-probing.
- If a session already owns this routine, it gets a one-line nudge instead of a second spawn (one breach = one process). Capacity-deferred spawns retry next tick with cooldown unstamped.
- Recovery (only after an actual fire) and broken-probe self-alert (10 consecutive probe errors) are surfaced too. Every fire/recover/monitor_broken appends to `data/<slug>/routines/events.ndjson` — the observability contract (see the install's events-schema reference doc).
- Runtime state is a disposable sidecar `routines/state/<R-id>.json`. To reset a misbehaving monitor, delete the sidecar — never churn the routine md frontmatter.

## Write the instruction for the breach moment

The routine body is what the attached AI *does* on breach: diagnose and report. No unattended destructive remediation (deletes, restarts, redeploys) unless the stakeholder explicitly delegated it in the instruction.

## Common mistakes

- `persist: 2` meaning "2 consecutive failures" — the key is `persist_s` and it's **seconds**.
- Declaring a cron routine with a "check if ok" instruction — ratchet violation; use a monitor.
- Hand-dropping a routine md into `routines/` — skips validation and id allocation; use `routines.declare()`.
- A numeric judge with a probe that exits nonzero on the bad case — that's the error path, not a breach; use `nonzero_exit`.
- Omitting `timeout_s` or unknown spec keys — both are declare-time `RoutineError`s (the typo guard is strict).
