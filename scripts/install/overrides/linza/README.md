# linza install overrides (T-0328)

Prepared during the Phase-1 security read-back (see T-0328's `## Context` /
`## Phase-1 security read-back` for the full analysis these fix). **Prepared
against the dev clone only — nothing here has touched the linza host.**
Phase 2 remains unauthorized pending stakeholder answers on the open items
below.

These three files close the gaps from the read-back that are ours to fix
(config, not decisions):

- `docker-compose.linza.yml` — network name / certresolver / mothership
  router / almdudleer bind-mount fixes for the `bot-squad-api` container.
- `bot-squad-worker.linza.service` — same class of fix for the coordinator
  systemd unit's `ReadWritePaths` + install-dir paths.
- this file — the exact env-var overrides for `install.sh`, and the explicit
  open questions that are NOT resolved here on purpose.

## Env-var overrides for `install.sh`

Export these before running the installer on linza (do not rely on any
default — the defaults are what caused finding (1) in the read-back):

```
export BOTSQUAD_GROUP=botsquad
export BOTSQUAD_INSTALL_DIR=/home/botsquad/bot-squad
export BOTSQUAD_REVERSE_PROXY_MODE=shared
export BOTSQUAD_REVERSE_PROXY_NETWORK=traefik
export BOTSQUAD_COORDINATOR_HOME=/home/botsquad   # consumed by docker-compose.linza.yml
export BOTSQUAD_DOMAIN=deployments.linzametrics.com   # ⚠ see OPEN item below — confirm the exact hostname/DNS spelling first
export BOTSQUAD_TG_PROXY_URL=<the real internal SOCKS proxy URL>   # ⚠ not filled in here — see OPEN item below
```

`BOTSQUAD_NONINTERACTIVE` must stay unset/0 — the whole point of "claude-
supervised, not unattended" (DoD's last line, read-back point 6) is that a
missing/ambiguous value stops and prompts instead of silently guessing.

## Playbook for applying the two override files

`install.sh` has no native hook for a compose override with an arbitrary
name, or a per-host systemd unit — both need a one-time manual step at the
right point in the checkpoint sequence:

1. Run `install.sh` normally through `clone_repo`.
2. **Before** the `systemd_unit` checkpoint runs: copy
   `bot-squad-worker.linza.service`'s contents over
   `$BOTSQUAD_INSTALL_DIR/systemd/bot-squad-worker.service` in the freshly
   cloned working tree (a local file swap in that clone, not a repo commit —
   if `BOTSQUAD_INSTALL_DIR` differs from `/home/botsquad/bot-squad`, update
   the paths inside the copied file to match first). Let `systemd_unit` run
   after that; it installs this corrected content verbatim.
3. Let `install.sh` continue through `install_reverse_proxy` (a no-op in
   `shared` mode) up to `docker_compose_up`. That checkpoint's plain `docker
   compose -f docker-compose.yml up -d --build` will fail (no `avo_backend`
   network on linza) — expected, checkpoints are resumable.
4. Run manually instead:
   ```
   cd $BOTSQUAD_INSTALL_DIR && docker compose \
     -f docker-compose.yml \
     -f scripts/install/overrides/linza/docker-compose.linza.yml \
     up -d --build
   ```
5. Mark the checkpoint done so the resumed `install.sh` run doesn't retry the
   plain command: `echo docker_compose_up >> $BOTSQUAD_STATE_DIR/install.state`.
6. Resume `install.sh` for the remaining checkpoints
   (`agent_teams_flag`/`spawn_operator`/`print_attach`).

## OPEN — deliberately left unresolved, need an explicit answer before Phase 2

Per the operator's instruction: do not bake in an assumption on any of these.

- **Sudo scope for `botsquad`.** `install.sh` needs *some* sudo (groupadd,
  usermod, apt-get, systemd/systemctl, loginctl). Every existing human
  account on linza has blanket `(ALL) NOPASSWD: ALL`. Scoped sudoers
  (only the specific commands `install.sh` issues) vs. matching the host's
  existing blanket convention is the operator/stakeholder's call — read-back
  point 1.
- **T-0217 residual risk.** Mothership holds each connected server's
  plaintext `server_bearer` (T-0217, still `planned`/open) — connecting a
  customer-network-holding host makes that gap more consequential. Accept as
  tracked risk and proceed, or fix T-0217 first — stakeholder's call, per
  read-back point 4.
- **Root/docker-group footprint.** P6 isolates the Linux *account* from
  almdudleer's existing orchestrator; it does not change that the coordinator
  runs as root (no `User=` in the unit) and needs `docker`-group membership
  (root-equivalent). This install adds one more root-equivalent, docker-group
  daemon to a host holding customer WireGuard mesh keys, regardless of the
  account being "dedicated." Worth the stakeholder hearing explicitly before
  saying go — read-back point 1's nuance paragraph.
- **`BOTSQUAD_DOMAIN` / Host-rule spelling.** The DoD text says
  `botsquad.deployments.linzametrics.com` (no hyphen); the base compose's own
  convention renders `bot-squad.${DOMAIN}` (with a hyphen). This override
  followed the DoD text literally in the Host rule — confirm which one DNS
  is actually (or will be) provisioned for before running; a mismatch here
  just means the router never gets traffic, not a security issue, but it's
  an easy thing to get wrong once and not notice until traffic doesn't show up.
- **Who creates the `botsquad` Linux account, and how** (`adduser`/`useradd`,
  shell, home ownership) — `install.sh` never does this itself (read-back
  point 1). Needs an owner before the supervised run starts.
- **The exact TG proxy URL** — deliberately left as a placeholder above; the
  June recon only confirmed *presence* of an internal SOCKS5 endpoint
  (`10.99.0.1:1080`, no embedded credential noted) without reading
  `~/linzahelper/config.env`'s actual `TG_PROXY` value (that's almdudleer's
  file; no fresh reason to open it for this read-back). Whoever runs Phase 2
  should pull the real value from that file or ask the host owner directly.
