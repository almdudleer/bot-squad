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

## How to declare — the `bsq` CLI (T-0604; pending-deploy: needs the worker running a build ≥ T-0604)

Declare through `bsq routine declare --trigger monitor` — it round-trips the worker socket, which validates the spec, allocates the R-id, and writes the routine md. Do NOT hand-write routine mds or hand-pick ids. (If the install's worker predates T-0604 the socket rejects the `monitor` param — fall back to `routines.declare(cfg, slug, trigger="monitor", monitor={...})` from the worker venv until the deploy lands.)

```bash
bsq routine declare --trigger monitor \
  --cmd "df -P /var | awk 'NR==2{sub(/%/,\"\",\$5); print \$5}'" \
  --interval 5m --timeout 10s \
  --judge numeric_gt --threshold 85 \
  --persist 10m --cooldown 30m \
  --title "staging /var disk watch" \
  --provenance "stakeholder:2026-07-05" \
  "/var is filling (value, threshold, onset are in your TRIGGER EVENT brief). \
Find the top growers, report to the operator with a recommended cleanup. \
Do NOT delete anything yourself."
```

Durations take `30s / 15m / 2h / 1d` (bare number = seconds). An http probe swaps `--cmd`/`--judge`/`--threshold` for `--probe http --url <https://…>` plus optional `--expect-status` (default 200) and `--latency-budget-ms` — it judges itself. `--on-breach notify` = code-only alert, no AI (linza `severity: info` analog); `--on-recover notify` = a ✅ when the metric returns within threshold (sent only if the breach actually fired).

`bsq routine list` shows the live monitor columns from sidecar state — `last=<value> breach=YES|no last_fire=<ts>` plus any standing mute. To silence a KNOWN breach: `bsq routine mute R-NNNN 45m 'why'` — it keeps probing (recovery is still seen) but never fires; `bsq routine mute R-NNNN 0` unmutes, and a breach still standing fires on the next tick.

## Spec cheat-sheet (CLI flag → `monitor:` frontmatter key)

| flag → key | meaning | notes |
|---|---|---|
| `--probe` | `shell` (default) or `http` | |
| `--cmd` → `cmd` | [shell] the command; stdout is the metric | |
| `--url` → `url` | [http] endpoint to GET; judges itself via `--expect-status`/`--latency-budget-ms` | don't set `--judge`/`--threshold` |
| `--interval` → `interval_s` | probe cadence | min 5s, default 30s |
| `--timeout` → `timeout_s` | probe hard-kill | **mandatory** — declare-time error if missing |
| `--judge` + `--threshold` | [shell] breach test | see below |
| `--persist` → `persist_s` | breach must HOLD this long before the first fire | **a duration, not a failure count**; default 0 — always set it |
| `--cooldown` → `cooldown_s` | min gap between fires of one ongoing breach | default 0 — always set it |
| `--on-breach` → `on_breach` | `spawn` (attach AI, default) or `notify` (code-only alert) | |
| `--on-recover` → `on_recover` | `notify` = ✅ on recovery (only after a real fire) | |

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

- `--persist 2` meaning "2 consecutive failures" — persist is a **duration** (breach must hold that long).
- Declaring a cron routine with a "check if ok" instruction — ratchet violation; use a monitor.
- Hand-dropping a routine md into `routines/` — skips validation and id allocation; use `bsq routine declare`.
- A numeric judge with a probe that exits nonzero on the bad case — that's the error path, not a breach; use `--judge nonzero_exit`.
- Omitting `--timeout` or unknown spec keys — both are declare-time errors (the typo guard is strict).
- Hand-editing the md to silence a noisy monitor — that's what `bsq routine mute <R-NNNN> <dur> '<reason>'` is for; mute survives a sidecar reset because it lives on the md.
