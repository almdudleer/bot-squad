# bot-squad

Product-led agent operations framework. Hosts the API + worker that drive
agent sessions across multiple projects.

- Spec: `signal_tracker_mgmt/docs/superpowers/specs/2026-05-10-bot-squad-foundation-and-migration-design.md`
- Plan: `signal_tracker_mgmt/docs/superpowers/plans/2026-05-10-bot-squad-foundation-and-migration.md`

## Components

- `api/` — FastAPI in Docker. Serves UI + REST. TG-Login gated. No host access.
- `worker/` — systemd user unit. APScheduler + Unix-socket sync RPC. Has host access.
- `web/` — React + Vite + TS + Bootstrap. Built into a static bundle served by the API.
- `scripts/` — hooks, CLIs, migration scripts.
- `config/` — `projects.toml` (registry), `auth.toml` (TG allowlist + secrets, gitignored).
- `data/` — runtime, gitignored. Per-project subdirs hold backlog, vision, feedback, sessions, runs.

## Bring up

```
docker compose up -d                                  # API container
systemctl --user enable --now bot-squad-worker        # worker (after install)
```
