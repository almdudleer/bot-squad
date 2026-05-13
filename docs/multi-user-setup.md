# Multi-user setup (Phase 2)

bot-squad supports multiple Linux users on one host: each user has their
own tmux server (their own sessions, panes, claude installs), and the
bot-squad coordinator routes session ops to a per-user worker process.

## Architecture (one host)

- **Coordinator** (1): `bot-squad-worker.service` running as the
  coordinator linux user (default `almdudleer`). Binds
  `/home/www/bot-squad/data/_sock/worker.sock`. Runs the scheduler.
  Handles non-tmux actions (deploy, tg_*, peer_*, autonomous_*,
  task_progress_add, scheduler_state). Also serves tmux ops for the
  coordinator user — one process, both roles.
- **User worker** (N): `bot-squad-user-worker.service` running in
  `systemd --user` for each non-coordinator user. Binds
  `data/_sock/user-<USER>.sock`. Handles only tmux ops.

The API container connects to the right socket per request. SIDs encode
the linux user (`S-<linux_user>-…`), so pause/suspend/resume route
automatically. Spawn lands in the logged-in UI user's linux_user. List
fans out across every configured user worker and merges results.

## Adding a Linux user

1. Add OS account and put them in the `www` group (so the per-user
   socket dir is reachable):
   ```
   sudo adduser --disabled-password edem
   sudo usermod -a -G www edem
   sudo loginctl enable-linger edem
   ```
2. Install the per-user worker unit:
   ```
   sudo install -m 0644 -o edem -g edem \
       /home/www/bot-squad/systemd/bot-squad-user-worker.service \
       /home/edem/.config/systemd/user/
   sudo -u edem XDG_RUNTIME_DIR=/run/user/$(id -u edem) \
       systemctl --user daemon-reload
   sudo -u edem XDG_RUNTIME_DIR=/run/user/$(id -u edem) \
       systemctl --user enable --now bot-squad-user-worker.service
   ```
3. Map the UI username to the Linux user in
   `/home/www/bot-squad/config/auth.toml`:
   ```
   [users]
   edem = "<bcrypt-hash>"

   [user_meta.edem]
   linux_user = "edem"
   is_admin = false
   ```
4. Reload the API: `cd /home/www/bot-squad && docker compose restart bot-squad-api`.
5. Verify: log in as `edem` in the UI, spawn a session — it lands in
   `edem`'s tmux, not the coordinator's. `list_sessions` shows both
   users' sessions for the project (filtered to project repo).

Phase 3 will surface a Users admin tab; until then this is operator-managed.

## Single-user back-compat

A bare deploy with only the default `[users]` works exactly as before —
the coordinator handles its own tmux. Adding `[user_meta]` is opt-in.
