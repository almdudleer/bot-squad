# Phase-2 execution runbook — linza install (T-0328)

**Phase 2 is NOT authorized.** This is prep for when it is — a reviewed
script for the claude-supervised session that runs it, so execution follows
a plan instead of improvising against a production-adjacent host. Read
alongside `README.md` (env vars + the open questions) and the T-0328 ticket's
`## Phase-1 security read-back` (why each fix below exists) and the
`⚠ GOTCHA` section (the `!reset`-vs-`!override` compose footgun — re-test
before touching that file further, don't just trust it).

Every checkpoint below is from `install.sh`'s `INSTALL_STEPS` array, in
order. "No action needed" means: linza's platform already satisfies it (per
the June 2026 recon, re-verify live since state drifts) and nothing in this
runbook changes its behavior.

## Before starting `install.sh` at all

1. **[BLOCKED ON STAKEHOLDER]** Host owner creates the `botsquad` Linux
   account (`adduser`/`useradd`) and grants it sudo. Scope (matching the
   host's existing blanket `NOPASSWD: ALL` convention, vs. a sudoers entry
   scoped to exactly what `install.sh` issues) is the open question in
   `README.md` — get the answer before this step, don't guess a scope.
2. Log in **as `botsquad`**, in a fresh session (not `sudo -u botsquad` from
   another shell) — Unix group membership is fixed at session start, and
   `install_docker`'s own checkpoint (below) depends on this being a real
   fresh login.
3. Export the full env-var block from `README.md`:
   `BOTSQUAD_GROUP`, `BOTSQUAD_INSTALL_DIR`, `BOTSQUAD_REVERSE_PROXY_MODE`,
   `BOTSQUAD_REVERSE_PROXY_NETWORK`, `BOTSQUAD_COORDINATOR_HOME`. Leave
   `BOTSQUAD_NONINTERACTIVE` unset.
   - **[BLOCKED ON STAKEHOLDER]** `BOTSQUAD_DOMAIN` — confirm the hyphen
     spelling before setting it (README's "domain hyphen mismatch" item).
   - **[BLOCKED ON STAKEHOLDER/HOST OWNER]** `BOTSQUAD_TG_PROXY_URL` — pull
     the real value (from `~/linzahelper/config.env`'s `TG_PROXY` or the
     host owner directly), don't set a placeholder.
4. Host owner re-checks disk headroom **right now, not from the Phase-1
   read-back's numbers** (`docker system df -v`, then a scoped `docker image
   prune` if needed — never a blanket `-a` on this shared daemon). P5.

## Checkpoint walk-through

| # | checkpoint | linza-specific note |
|---|---|---|
| 1 | `detect_distro` | No action needed — Ubuntu 24.04 confirmed compatible. |
| 2 | `require_sudo` | Needs botsquad's sudo grant from pre-flight step 1 to already exist. If it fails here, that's the signal the grant wasn't actually set up — stop and go back, don't work around it with `sudo -v` as a different user. |
| 3 | `proxy_url` | Answer **no** / leave `BOTSQUAD_PROXY_URL=""`. This is the general apt/npm/curl proxy — linza has direct general internet (confirmed live: GitHub, Grafana, GitLab all reachable direct). Only Telegram is DPI-blocked here; that's the separate `tg_proxy` checkpoint (#17), don't conflate the two. |
| 4 | `pkg_index_update` | No action needed. |
| 5 | `install_base_pkgs` | No action needed (curl/git/jq/ca-certificates). |
| 6 | `install_tmux` | No action needed — tmux 3.4 already present system-wide. |
| 7 | `install_nodejs` | **Re-interprets P7.** botsquad is a separate Linux account from `almdudleer` — it does NOT inherit almdudleer's nvm-installed node v22 (nvm is per-user). This checkpoint will apt/NodeSource-install a fresh system node for botsquad. That's fine and arguably cleaner than the original P7 plan (no cross-user nvm PATH plumbing needed) — just don't be surprised it doesn't reuse the nvm one from the June recon. |
| 8 | `install_claude_code` | Same reasoning as #7 — will `sudo npm install -g @anthropic-ai/claude-code` fresh for botsquad, since almdudleer's nvm-based `claude` isn't on botsquad's PATH. This IS the corrected shape of P7 — confirm `claude` resolves on botsquad's PATH after this checkpoint, that's the actual DoD intent. |
| 9 | `install_docker` | docker-ce is already installed system-wide (June recon), so the apt portion is redundant but harmless. The real effect: `sudo usermod -aG docker botsquad` (read-back finding — root-equivalent grant, expected/required, not avoidable without a rootless-docker install path bot-squad doesn't currently support). **Expect this run to HALT here on the first invocation** — the script's own comment explains why: this shell inherited the pre-usermod group set, so `docker compose version` still fails even after the usermod succeeds. Log out, log back in as botsquad, re-run `install.sh` — it resumes past this checkpoint automatically (state file). Not a bug. |
| 10 | `botsquad_group` | **This is where finding (1) gets fixed** — only if `BOTSQUAD_GROUP=botsquad` was exported in pre-flight step 3. If that export was skipped, this checkpoint folds botsquad into the pre-existing production `www` group (GID 1007) — the exact hole the Phase-1 read-back flagged. Double-check the env var landed (`echo $BOTSQUAD_GROUP`) before letting this run. |
| 11 | `install_dir` | Needs `BOTSQUAD_INSTALL_DIR` set (pre-flight step 3) to something NOT under `/home/www` — creates the dir, chgrp's it to the (now-correct) botsquad group. |
| 12 | `clone_repo` | Clones bot-squad into `$BOTSQUAD_INSTALL_DIR`. **Immediately after this checkpoint succeeds and BEFORE #18 (`systemd_unit`) runs:** copy `bot-squad-worker.linza.service`'s content over `$BOTSQUAD_INSTALL_DIR/systemd/bot-squad-worker.service` in the freshly cloned tree. If a different `BOTSQUAD_INSTALL_DIR`/`BOTSQUAD_COORDINATOR_HOME` than `/home/botsquad/bot-squad` / `/home/botsquad` was chosen, edit the paths inside the copied file to match first — it's a static file, not templated. |
| 13 | `render_env` | Prompts for `BOTSQUAD_DOMAIN` (pre-set from pre-flight step 3, pending the stakeholder's hyphen answer) and `BOTSQUAD_TG_BOT_TOKEN` (default `MOTHERSHIP_PROXY` — no dedicated bot token is called for in the DoD, so accepting the default here is a reasonable choice, not a stakeholder question). Auto-generates `JWT_SECRET`/`BOT_SQUAD_SECRETS_KEY`, writes `.env` at 0640 group-owned by the (correct, by now) botsquad group. |
| 14 | `mothership_handshake` | Needs the mothership install token/URL (comes from however Phase 2 is actually invoked — e.g. a link from the mothership UI — not something set manually here). Server bearer lands at `~/.bot-squad/server.token`, 0600, under botsquad's own real `$HOME` — correctly scoped now that botsquad isn't folded into `www`. Remember: T-0217 is still open (read-back finding 4) — this is the step that actually creates the exposure T-0217 is about; make sure the stakeholder's answer on that item landed before running this checkpoint, not after. |
| 15 | `python_venv` | No action needed. |
| 16 | `seed_claude_settings` | No action needed. |
| 17 | `tg_proxy` | Needs `BOTSQUAD_TG_PROXY_URL` set to the REAL value (pre-flight step 3's open item) — without it, all TG sends from this install fail silently later, not at install time. |
| 18 | `systemd_unit` | Installs whatever is at `$BOTSQUAD_INSTALL_DIR/systemd/bot-squad-worker.service` — this only installs the corrected linza unit if step #12's file-swap actually happened first. Verify before this runs: `diff $BOTSQUAD_INSTALL_DIR/systemd/bot-squad-worker.service scripts/install/overrides/linza/bot-squad-worker.linza.service` (paths adjusted for the actual install dir) should show no meaningful difference. |
| 19 | `per_user_worker_unit` | No action needed — this unit's paths are all relative to `$BOTSQUAD_INSTALL_DIR`, no hardcoded almdudleer references (verified by reading the file). |
| 20 | `install_reverse_proxy` | With `BOTSQUAD_REVERSE_PROXY_MODE=shared` set, this is a documented no-op — it does NOT write the compose override itself. That happens manually at the next checkpoint. |
| 21 | `docker_compose_up` | **The manual-intervention step.** The plain `docker compose -f docker-compose.yml up -d --build` this checkpoint runs WILL fail — `avo_backend` network doesn't exist on linza (expected, per read-back finding 2). Instead, run manually: `cd $BOTSQUAD_INSTALL_DIR && docker compose -f docker-compose.yml -f scripts/install/overrides/linza/docker-compose.linza.yml up -d --build` (with `BOTSQUAD_COORDINATOR_HOME` and `BOTSQUAD_DOMAIN` exported in that shell). **Before trusting this**, re-run `docker compose config` with both `-f` flags and eyeball the resolved `labels:`/`volumes:` sections are non-empty and correct — see the `⚠ GOTCHA` on the ticket, don't assume the override file still behaves the same if anyone has touched it since. Then mark the checkpoint done so the resumed `install.sh` doesn't retry the plain command: `echo docker_compose_up >> $BOTSQUAD_STATE_DIR/install.state`. |
| 22 | `agent_teams_flag` | No action needed — writes to botsquad's own `~/.claude/settings.json`. |
| 23 | `spawn_operator` | Spawns `claude --dangerously-skip-permissions` in a **new tmux session under botsquad's own tmux server** — confirms P6's tmux isolation actually holds at this step (separate from almdudleer's tmux entirely). Watch it start cleanly; this is the first autonomous thing that runs post-install. |
| 24 | `print_attach` | Cosmetic — prints the attach command and UI URL. No action needed. |

## After `install.sh` completes

- Verify the traefik router actually appears and routes correctly:
  `docker logs <traefik container>` for ACME/router errors, and confirm the
  chosen domain resolves and serves TLS — don't just trust a clean install.sh
  exit.
- Confirm `systemctl status bot-squad-worker` is active and NOT flagging
  permission errors against the repointed `ReadWritePaths`.
- Register the host in the mothership registry per the DoD's last line —
  this is a separate action from the handshake at checkpoint #14 (that's the
  server-side connect; registry registration is the operator-facing step).
- Report back to the operator with what actually happened at each manual
  step above — this runbook is a plan, not a guarantee; note any deviation.
