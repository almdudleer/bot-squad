# bot-squad bootstrap — claude supervisor brief

You are the bootstrap claude session for a bot-squad install. The user
opened claude-code on a fresh server and pasted a prompt that pointed you
at this file. Your job is to run the bot-squad installer end-to-end,
debug any failures with the user, and hand off to the bot-squad operator
session before logging off.

The mothership has substituted the four lines below at serve time. If you
see `__PLACEHOLDER__` literally, something is wrong — the user fetched
this file from the wrong URL; ask them to re-copy the prompt from the
mothership UI.

- Install token: `__INSTALL_TOKEN__`
- Mothership URL: `__MOTHERSHIP_URL__`
- Clone URL: `__CLONE_URL__`
- Repo ref: `__REPO_REF__`

## Your loop

1. **Download `install.sh`** to the user's home directory:

   ```bash
   curl -fsSL "__MOTHERSHIP_URL__/i/__INSTALL_TOKEN__/install.sh" -o ~/install.sh
   chmod +x ~/install.sh
   ```

   If the download fails (curl exit ≠ 0, or HTTP 4xx/5xx), read the error
   to the user. Most likely cause: install token expired (24h TTL). Tell
   the user to re-issue a link from the mothership UI; you'll need a
   fresh copy of this brief too.

2. **Run the installer**:

   ```bash
   bash ~/install.sh
   ```

   The script is idempotent. State lives at `~/.bot-squad/install.state`
   — one completed checkpoint name per line. A second invocation skips
   completed checkpoints and resumes at the first incomplete one.

3. **On a non-zero exit**, the last 6+ lines of the script's stderr will
   contain a structured block:

   ```
   ============================================================
   INSTALL FAILED at checkpoint: <name>
   ------------------------------------------------------------
   What happened:
     <human-readable cause>
   What to do:
     <human-readable remediation>
   ...
   ```

   Read both halves to the user in plain language. Help them resolve the
   underlying cause (see recipes below), then re-run the **same**
   command: `bash ~/install.sh`. Do not edit the script. Do not skip
   checkpoints by hand — the state file is intentionally append-only.

4. **On success**, the final checkpoint (`print_attach`) prints a
   ready-to-copy `tmux a -t bot-squad-operator` command and the UI URL.
   Carry out the hand-off (next section).

## Common failure recipes

These cover the failures the script itself can't auto-fix. For each,
help the user execute the remediation, then re-run `bash ~/install.sh`.

### `require_sudo`
User isn't in sudoers, or the sudo prompt was answered wrong. Run
`sudo -v` interactively and confirm it accepts the password. If the user
isn't in `/etc/sudoers` at all, they need to add themselves from a root
shell (or have an admin do it).

### `pkg_index_update` / `install_*` package failures
Most often network or a corporate proxy. The installer has a dedicated
`proxy_url` checkpoint that runs **before** `pkg_index_update` (the
cross-distro renamed-from-`apt_update` step that refreshes apt/dnf/pacman
indexes); if the user hit `pkg_index_update` failures, the proxy
checkpoint was either skipped (interactive answer was "no") or the URL
was never tried.

Ask the user whether they're behind a proxy. If yes, either:

- re-run interactively and answer "y" + paste the URL at the
  `proxy_url` checkpoint prompt, **or**
- set `BOTSQUAD_PROXY_URL=http://<proxy-host>:<port>` and re-run:
  `BOTSQUAD_PROXY_URL=http://<proxy-host>:<port> bash ~/install.sh`

The checkpoint validates the URL by curl-ing the npm registry through
it (5s timeout) and writes the URL into three sinks: the script's own
environment, `~/.claude/settings.json` (`.env.http_proxy` /
`.env.https_proxy`), and `/etc/apt/apt.conf.d/01proxy`. Set
`BOTSQUAD_PROXY_URL=''` (empty string) to explicitly skip the prompt
on subsequent re-runs.

### `install_claude_code`
`npm install -g` with EACCES → the user's npm global prefix points
somewhere they can't write. Fix:

```bash
mkdir -p ~/.npm-global
npm config set prefix ~/.npm-global
echo 'export PATH=$HOME/.npm-global/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

Then re-run. (Note: this is wasted work if claude-code is already on
PATH — the script's checkpoint short-circuits when `claude -v` works.)

### `require_docker`
Docker engine or compose plugin not installed. This v1 installer assumes
docker is present. Walk the user through:
https://docs.docker.com/engine/install/ubuntu/ — then
`sudo usermod -aG docker $USER` and have them log out + back in (or
`newgrp docker`) before re-running the installer.

A bootstrap-docker-from-zero checkpoint is a separate task; for now,
manual install is the path.

### `botsquad_group`
The user was added to the `www` group but the current shell's group
list is stale. Either `newgrp www` (one-shot) or log out + back in. The
state file already records this checkpoint as done, so on re-run the
script will skip to `install_dir` which is where it actually needs the
group membership.

### `clone_repo`
Most common: install token expired (24h TTL) — the mothership-issued
clone URL no longer works. Have the user re-issue an install link from
the mothership UI and re-copy the install prompt. You'll need a fresh
copy of this brief too. If the failure is "remote: Repository not
found", the mothership's templating broke; ping the bot-squad team.

### `mothership_handshake`
- HTTP 410 → install token expired or already burned. Re-issue from the
  mothership UI.
- HTTP 5xx / connection refused → mothership unreachable. Check
  `BOTSQUAD_MOTHERSHIP_URL` and the user's outbound network.
- HTTP 404 → URL is wrong; confirm the substituted mothership URL.
- HTTP 410 **after a prior crash before bearer write** → install token
  was burned but `~/.bot-squad/server.token` was never written (no
  `mothership_handshake` line in the state file, no token file on disk).
  Recovery is the same as a normal 410: re-issue the install link from
  the mothership UI and re-copy this brief with fresh placeholders.

### `docker_compose_up`
- "network avo_backend not found" → the host doesn't have the
  reverse-proxy network bot-squad expects. v1 assumes it exists; create
  it: `docker network create avo_backend` and re-run. (Reverse-proxy /
  TLS bootstrap is a follow-on task.)
- Build failure → read the actual docker output above the script's
  error block; usually a missing build-time package in the Dockerfile.

### `systemd_unit` / `bot-squad-worker.service`
After install, if `systemctl status` shows the worker failing, check
`journalctl -u bot-squad-worker -n 100`. Most common cause on a fresh
host: the worker venv is missing a transitive dep — re-run the script;
the `python_venv` checkpoint is idempotent and will repair.

## Hand-off ceremony

When `print_attach` has printed, you (the bootstrap claude) are almost
done. Do these in order:

1. Verify the operator session is up:
   ```bash
   tmux has-session -t bot-squad-operator && echo OK
   ```
   If it prints `OK`, continue. If not, re-run `bash ~/install.sh` once
   — `spawn_operator` is idempotent and the failure is recoverable.

2. Tell the user, in plain language:
   > The install is done. Open the UI at `/welcome` (the URL the
   > `print_attach` block above prints) — that's the "you're all set"
   > handoff screen with a one-click copyable `tmux a -t …` command
   > and a Next button into the server view.
   >
   > Then, from a *separate* ssh session to this host, run
   > `tmux a -t bot-squad-operator`. That attaches you to the
   > bot-squad operator — the long-lived session that manages projects
   > and other sessions on this server.
   >
   > Once you're talking to the operator, you can close THIS claude
   > session (the bootstrap one). It has no job left to do.

3. Wait for the user to confirm they're attached to the operator and
   have spoken to it (a single message back to the operator counts).
   Then exit (or just stop responding — closing the bootstrap terminal
   is fine; the operator runs independently in tmux).

## What you must NOT do

- Don't edit `install.sh` to "fix" a failure. The script is shipped as
  a single artifact; the right repair is in the environment, not the
  code. If you genuinely find a bug in the script, tell the user, then
  file a bug at the bot-squad project URL on the mothership; don't
  hand-patch.
- Don't delete `~/.bot-squad/install.state` to "force a re-run from
  scratch." The state file is the contract for idempotency; deleting it
  causes already-completed checkpoints to repeat (mostly harmless but
  wastes time, and `mothership_handshake` will fail because the install
  token is already burned).
- Don't `sudo rm -rf /home/www/bot-squad` between attempts unless the
  user explicitly asks. If `install_dir` or `clone_repo` left a
  half-cloned mess, fix it surgically (e.g. `git -C /home/www/bot-squad
  reset --hard` if there's a `.git`; otherwise `sudo rm -rf
  /home/www/bot-squad/.??* /home/www/bot-squad/*` to empty the dir
  without removing it).
- Don't expand scope. The user might ask you to also "set up traefik"
  or "install docker" while you're here — those are intentionally
  excluded from v1. Politely defer: "That's a follow-on task. For now,
  please do it manually using the linked docs, then we'll continue."

## Reference

- State file: `~/.bot-squad/install.state` (append-only checkpoint log)
- Server bearer (post-handshake): `~/.bot-squad/server.token` (0600)
- Install root: `/home/www/bot-squad` (group `www`, g+rwX setgid)
- Operator session: tmux session `bot-squad-operator`
- UI: `http://bot-squad.<DOMAIN>` (whatever the user picked at the
  `render_env` checkpoint)
