# Guided team workflow — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the bot-squad foundation into a daily-usable workflow: agent_team/dev branch, auto-deploy on clean tree (cctv pattern), worker actions for `deploy` / `tg_notify` / `kick_stuck_now`, APScheduler jobs for `deploy_monitor` / `kick_stuck` / `oauth_refresh`, Stop+UserPromptSubmit hooks, per-project deploy recipes, the `ops/bot-squad-bin/deploy` shim, tightened secrets boundary.

**Spec:** `docs/superpowers/specs/2026-05-10-spec3-guided-team-workflow-design.md`. This plan implements §15's order at TDD grain.

**Architecture:** Same two-process split as spec #1 (API in Docker, worker as systemd user unit). Spec #3 fills out the worker's action allowlist and APScheduler job set. No web changes (UI work is spec #4–#6). Per-project recipe shell scripts live under `data/<slug>/deploy/` and are owned by the project, not the framework.

**Design principle (per stakeholder feedback):** No hoops for agents. Worker actions exist only when bash/git can't do the job (deploy queue lives outside the project repo; `bot_token` is held only by the worker post-secrets-split). For everything else — `git status`, squashing, renaming a tmux pane — agents use bash directly. Documented in AGENTS.md.

**Tech Stack:** Same as spec #1 — Python 3.12, FastAPI, APScheduler, httpx, pyjwt, pyyaml; bash for recipes and CLI shims. New deps: none.

---

## File structure (additions only)

```
/home/www/bot-squad/
├── config/
│   └── secrets.toml                    ← NEW — split from auth.toml
├── scripts/
│   ├── cli/
│   │   ├── deploy.sh                   ← NEW — shim, symlinked into project repos
│   │   └── render_agents_md.py         ← NEW — re-renders AGENTS.md
│   └── hooks/
│       ├── hook_my_sid.sh              ← NEW — computes SID with pane-id disambig
│       ├── user_prompt_submit.sh       ← NEW — touches .claude/last_user_prompt_ts
│       └── stop.sh                     ← NEW — TG-pings on idle
├── worker/
│   ├── bot_squad_worker/
│   │   ├── actions.py                  ← MODIFIED: add tg_notify, deploy, kick_stuck_now
│   │   ├── jobs.py                     ← MODIFIED: add deploy_monitor, kick_stuck, oauth_refresh
│   │   ├── scheduler.py                ← MODIFIED: register new jobs
│   │   ├── tg.py                       ← NEW — TG bot client + debounce
│   │   ├── deploy.py                   ← NEW — recipe runner, queue management
│   │   └── refresh_oauth.py            ← NEW — ported from cctv
│   └── tests/
│       ├── test_actions.py             ← MODIFIED: tests for new actions
│       ├── test_jobs.py                ← MODIFIED: tests for new jobs
│       ├── test_tg.py                  ← NEW
│       └── test_deploy.py              ← NEW
└── api/app/
    ├── config.py                       ← MODIFIED: drop bot_token reads (moves to worker only)
    └── routes_auth.py                  ← MODIFIED: TG-Login HMAC verification proxied to worker

Per-project (managed via bot-squad CLI):
data/signal-tracker/
├── deploy/
│   ├── staging.sh                      ← NEW — real recipe (not template)
│   └── dev.sh                          ← NEW — real recipe
└── _jobs/deploy/
    ├── queue/                          ← NEW dir
    ├── processing/                     ← NEW dir
    ├── processed/                      ← NEW dir
    └── runs/                           ← NEW dir (one log per deploy)

In each registered project repo (signal_tracker_mgmt/):
ops/bot-squad-bin/deploy                ← NEW symlink → /home/www/bot-squad/scripts/cli/deploy.sh
.claude/settings.json                   ← MODIFIED: add UserPromptSubmit + Stop hooks
AGENTS.md                               ← MODIFIED: real branching + deploy sections (re-rendered),
                                          inline squash recipe in `## Branching & commits`
```

Things deliberately NOT created (the no-hoops rule):

- No `scripts/cli/squash.sh` — agents run `git reset --soft $(git merge-base HEAD master) && git commit -m "..."` directly
- No `scripts/cli/pane.sh` — `tmux rename-window <feature>` is two words
- No `git_status` worker action — agents and `deploy_monitor` both call `git status` via subprocess directly

---

## Conventions

- All new code follows TDD: failing test first, run to verify it fails, write impl, run to verify pass, commit.
- `data/<slug>/_jobs/deploy/queue/` is the source of truth for queued deploys; everything else (processing/, processed/, runs/, monitor.log) is derived.
- The worker's `actions.py` registry is the security boundary. Every new action MUST validate exact parameter keys (rejecting extras), look up the slug against `projects.toml`, and raise `ActionError` for any caller mistake.
- Each task ends with a single git commit (`<scope>: <imperative>` ≤70 chars).
- All bash scripts use `set -euo pipefail`. All Python uses type hints.

---

## Phase 1 — secrets split (low-risk prep)

### Task 1: Split `secrets.toml` out of `auth.toml`

**Files:**
- Modify: `/home/www/bot-squad/config/auth.toml` — remove `bot_token`
- Create: `/home/www/bot-squad/config/secrets.toml` — owns `bot_token` + future privileged secrets
- Modify: `worker/bot_squad_worker/config.py` — load secrets.toml
- Modify: `api/app/config.py` — drop bot_token (API doesn't need it post-#3)
- Modify: `api/app/routes_auth.py` — TG-Login HMAC verification proxies to a new worker action `tg_verify_login`
- Modify: `worker/bot_squad_worker/actions.py` — add `tg_verify_login` action that wraps `verify_tg_login` with the bot token from secrets.toml
- Modify: `docker-compose.yml` — split the `./config:/config:ro` mount into per-file binds so the API container literally cannot see secrets.toml
- Test: `worker/tests/test_config.py`, `worker/tests/test_actions.py`, `api/tests/test_config.py`, `api/tests/test_routes_auth.py`

- [ ] **Step 1: Failing tests**

`worker/tests/test_config.py` — add:

```python
def test_secrets_loads(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_bot_token == "TESTBOT:TOKEN"


def test_secrets_missing_raises(tmp_config_dir: Path) -> None:
    with pytest.raises(FileNotFoundError, match="secrets.toml"):
        Config.load(tmp_config_dir)
```

`worker/tests/test_actions.py` — add:

```python
def test_tg_verify_login_dispatches(tmp_config_dir: Path, monkeypatch):
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    from bot_squad_worker import actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    payload = _make_tg_payload("TESTBOT:TOKEN", tg_id=12345)
    out = A.dispatch("tg_verify_login", {"payload": payload})
    assert out["ok"] is True
    assert out["user"]["id"] == 12345
```

(Reuse `_make_tg_payload` helper from spec #1's test_auth.py — copy it into a worker test fixture.)

`api/tests/test_config.py` — modify the existing `test_auth_config_loads` so it does NOT assert a `bot_token` attribute on `AuthConfig`.

`api/tests/test_routes_auth.py` — modify the login flow tests so they patch the worker socket with a fake server that returns `{ok: true, user: {...}}` from `/actions/tg_verify_login`.

- [ ] **Step 2: Verify tests fail**

```bash
cd /home/www/bot-squad/worker && .venv/bin/pytest tests/test_config.py tests/test_actions.py -v
cd /home/www/bot-squad/api && .venv/bin/pytest tests/test_config.py tests/test_routes_auth.py -v
```

- [ ] **Step 3: Worker `Config.load`**

Update `worker/bot_squad_worker/config.py`:

```python
@dataclass(frozen=True)
class Config:
    config_dir: Path
    projects: dict[str, Project] = field(default_factory=dict)
    tg_bot_token: str = ""

    # ... existing properties ...

    @classmethod
    def load(cls, config_dir: Path) -> "Config":
        projects_toml = config_dir / "projects.toml"
        if not projects_toml.exists():
            raise FileNotFoundError(f"projects.toml not found: {projects_toml}")
        secrets_toml = config_dir / "secrets.toml"
        if not secrets_toml.exists():
            raise FileNotFoundError(f"secrets.toml not found: {secrets_toml}")
        raw = tomllib.loads(projects_toml.read_text())
        projects = {slug: Project.from_toml(p) for slug, p in raw.get("projects", {}).items()}
        sec = tomllib.loads(secrets_toml.read_text())
        return cls(
            config_dir=config_dir,
            projects=projects,
            tg_bot_token=sec.get("telegram", {}).get("bot_token", ""),
        )
```

- [ ] **Step 4: API `AuthConfig`**

Drop `bot_token` from the dataclass and from the loader:

```python
@dataclass(frozen=True)
class AuthConfig:
    allowed_ids: tuple[int, ...]
    session_ttl_seconds: int
    auth_age_max: int

    @classmethod
    def load(cls, config_dir: Path) -> "AuthConfig":
        raw = tomllib.loads((config_dir / "auth.toml").read_text())
        tg = raw["telegram_login"]
        return cls(
            allowed_ids=tuple(int(i) for i in tg["allowed_ids"]),
            session_ttl_seconds=cls._parse_ttl(tg["session_ttl"]),
            auth_age_max=int(tg.get("auth_age_max", 86400)),
        )
```

- [ ] **Step 5: Worker `tg_verify_login` action**

In `actions.py` add:

```python
from app.auth import verify_tg_login as _verify_tg_login_impl
# Wait — the api's auth module isn't importable from the worker.
# Copy the verify_tg_login function into worker/bot_squad_worker/auth.py
# (it's small) so the worker doesn't depend on the api package.

def _action_tg_verify_login(params: dict) -> dict:
    extra = set(params) - {"payload"}
    if extra:
        raise ActionError(f"tg_verify_login got unexpected params: {sorted(extra)}")
    if "payload" not in params:
        raise ActionError("tg_verify_login missing required param: payload")
    cfg = _get_config()
    if not cfg.tg_bot_token:
        raise ActionError("tg_verify_login: bot token not configured")
    try:
        from bot_squad_worker.auth import verify_tg_login
        user = verify_tg_login(params["payload"], cfg.tg_bot_token, cfg.tg_auth_age_max)
        return {"ok": True, "user": user}
    except Exception as e:
        return {"ok": False, "error": str(e)}


ACTION_REGISTRY["tg_verify_login"] = _action_tg_verify_login
```

Create `worker/bot_squad_worker/auth.py` with a verbatim copy of api's `verify_tg_login` function (HMAC + auth_date checks). Don't import across the api/worker boundary.

Add `tg_auth_age_max` to `Config` (read from secrets.toml `telegram.auth_age_max`, default 86400).

- [ ] **Step 6: API `routes_auth.py` proxies to worker**

```python
@router.post("/tg")
async def tg_login(request: Request, response: Response, payload: dict) -> dict:
    cfg = request.app.state.auth_config
    client = WorkerClient(request.app.state.sock_path)
    try:
        result = await client.call_action("tg_verify_login", {"payload": payload})
    except WorkerError as e:
        raise HTTPException(status_code=502, detail=f"worker unavailable: {e}")
    if not result.get("ok"):
        raise HTTPException(status_code=401, detail=result.get("error", "tg login rejected"))
    user = result["user"]
    if int(user["id"]) not in cfg.allowed_ids:
        raise HTTPException(status_code=403, detail="not allowed")
    token = issue_jwt(
        {"tg_id": int(user["id"]), "name": user.get("first_name", "")},
        request.app.state.jwt_secret,
        ttl_seconds=cfg.session_ttl_seconds,
    )
    response.set_cookie(
        key=COOKIE_NAME, value=token,
        max_age=cfg.session_ttl_seconds,
        httponly=True, secure=request.app.state.cookie_secure, samesite="lax",
    )
    return {"ok": True, "tg_id": int(user["id"])}
```

The API can drop the import of `app.auth.verify_tg_login` (or keep the file for `issue_jwt`/`verify_jwt` only).

- [ ] **Step 7: Move `bot_token` on disk**

```bash
cd /home/www/bot-squad/config
sed -i '/^bot_token/d' auth.toml
cat > secrets.toml <<'EOF'
# bot-squad secrets — gitignored, mounted into the worker only.
# Do NOT mount this into the API container.

[telegram]
bot_token = "<TG_BOT_TOKEN>"
auth_age_max = 86400
EOF
chmod 640 secrets.toml
chgrp www secrets.toml
```

- [ ] **Step 8: docker-compose.yml mount split**

Replace `- ./config:/config:ro` with explicit per-file binds so the API container can't see secrets.toml:

```yaml
    volumes:
      - ./config/projects.toml:/config/projects.toml:ro
      - ./config/auth.toml:/config/auth.toml:ro
      - ./data:/data:rw
```

- [ ] **Step 9: gitignore**

`config/secrets.toml` is already covered by spec #1's `.gitignore` (which has `config/auth.toml` ignored — check that secrets.toml is also ignored, add if not).

- [ ] **Step 10: Run tests + smoke**

```bash
cd /home/www/bot-squad/worker && .venv/bin/pytest -v
cd /home/www/bot-squad/api && .venv/bin/pytest -v
systemctl --user restart bot-squad-worker
sleep 2
docker compose -f /home/www/bot-squad/docker-compose.yml up -d --force-recreate
sleep 5
curl -s https://bot-squad.dev.uzinvestapi.com/api/health
# Then in browser: log in via TG widget and confirm cookie issued.
```

- [ ] **Step 11: Commit**

```bash
cd /home/www/bot-squad
git add config/auth.toml config/secrets.toml worker/bot_squad_worker/{config,actions,auth}.py worker/tests/{test_config,test_actions}.py api/app/{config,routes_auth}.py api/tests/{test_config,test_routes_auth}.py docker-compose.yml .gitignore
git commit -m "secrets: split bot_token into secrets.toml; API proxies TG-Login to worker"
```

---

## Phase 2 — TG client + tg_notify action

### Task 2: Worker TG client module with debounce

**Files:**
- Create: `worker/bot_squad_worker/tg.py`
- Create: `worker/tests/test_tg.py`

(Implementation: see spec §7. Test cases: debounce skips duplicates within window, allows after window, prefix includes SID, empty token no-ops silently. Code mirrors cctv-backend's `tg_notify.sh` logic in Python.)

- [ ] **Steps 1–4**: TDD cycle exactly as in Phase 2 of the previous plan revision (test → fail → implement → pass).

- [ ] **Step 5: Commit**

```bash
git add worker/bot_squad_worker/tg.py worker/tests/test_tg.py
git commit -m "worker: TG outbound client with debounce + SID prefix"
```

### Task 3: `tg_notify` and `kick_stuck_now` actions

**Files:**
- Modify: `worker/bot_squad_worker/actions.py`
- Modify: `worker/tests/test_actions.py`

Both actions take a slug or chat_id, validate params strictly, and call into the TG client / kick_stuck function respectively. `kick_stuck_now` is a thin sync wrapper around the daily `kick_stuck(cfg)` job (see Task 7).

- [ ] **Steps 1–4**: TDD cycle. Tests cover slug→chat_id resolution, parameter validation rejecting extras, no-token silent skip.

- [ ] **Step 5: Commit**

```bash
git add worker/bot_squad_worker/actions.py worker/tests/test_actions.py
git commit -m "worker: tg_notify action; kick_stuck_now stub"
```

(Note: `kick_stuck_now` may produce empty messages until Task 7 lands. That's fine for v1 of the action — the action contract is "fire what kick_stuck would fire", which becomes useful once kick_stuck is non-trivial.)

---

## Phase 3 — Deploy infrastructure

### Task 4: Deploy queue + recipe runner module

**Files:**
- Create: `worker/bot_squad_worker/deploy.py`
- Create: `worker/tests/test_deploy.py`

`deploy.py` exposes:
- `enqueue(cfg, slug, target, reason, requested_by) -> queue_id`
- `list_queued(cfg, slug) -> list[Path]`
- `run_next(cfg, slug) -> DeployResult | None` — runs the next queued recipe IF tree is clean; returns `None` if queue empty or tree dirty
- Uses `subprocess.run(["git", "status", "--porcelain"], cwd=str(proj.repo_path))` directly (no worker action)

- [ ] **Step 1: Failing test**

`worker/tests/test_deploy.py` — 5 tests:
1. `test_enqueue_writes_queue_file` — basic enqueue produces JSON file with the expected keys
2. `test_enqueue_rejects_unknown_target` — bad target raises `ValueError`
3. `test_run_next_executes_recipe_on_clean_tree` — succeeds, queue file moves to processed/.ok
4. `test_run_next_skips_when_dirty` — returns None, queue file remains
5. `test_run_next_failure_records_rc` — recipe exits 7, queue file moves to processed/.fail.7

(Test fixture: a real git repo in `tmp_path` plus a recipe shell script. No mocks.)

- [ ] **Steps 2–4**: TDD cycle. Implementation per spec §4 — see `worker/bot_squad_worker/deploy.py` outline in the previous revision of this plan.

- [ ] **Step 5: Commit**

```bash
git add worker/bot_squad_worker/deploy.py worker/tests/test_deploy.py
git commit -m "worker: deploy queue + per-project recipe runner"
```

### Task 5: `deploy` worker action

**Files:**
- Modify: `worker/bot_squad_worker/actions.py`
- Modify: `worker/tests/test_actions.py`

The action takes `{slug, target, reason, requested_by}` and calls `deploy.enqueue(...)`. Returns `{ok, queue_id, queued_at}`. Strict param validation as ever.

- [ ] **Steps 1–4**: TDD cycle.
- [ ] **Step 5: Commit**

```bash
git commit -m "worker: deploy action queues request file"
```

### Task 6: `deploy_monitor` scheduler job

**Files:**
- Modify: `worker/bot_squad_worker/jobs.py`
- Modify: `worker/bot_squad_worker/scheduler.py`
- Modify: `worker/tests/test_jobs.py`

For each registered project: pop oldest queue file → if tree clean run recipe → TG-ping success/fail. Iterates per-project. Honors per-project danger windows (`projects.toml::deploy_danger_windows`, list of `"HH:MM-HH:MM <tz>"` strings; if missing, no windows). Catches and logs all exceptions per-project so one project's failure doesn't kill the job.

- [ ] **Step 1: Failing test**

Test fixture: project with a recipe that succeeds + a queued deploy. Assert: queue empty after job, processed has `.ok` file, fake TG client received both queue-time and success messages.

Second test: project with the recipe that fails (rc=7). Assert: processed has `.fail.7`, TG message contains "FAILED rc=7".

Third test: project with a queued deploy but a dirty tree. Assert: queue still has the file, no TG messages.

- [ ] **Steps 2–4**: TDD cycle.
- [ ] **Step 5: Register in scheduler.py**

```python
sched.add_job(deploy_monitor, "interval", seconds=60, args=[cfg], id="deploy_monitor",
              replace_existing=True)
```

- [ ] **Step 6: Commit**

```bash
git commit -m "worker: deploy_monitor scheduler job"
```

### Task 7: `kick_stuck` daily summary

**Files:**
- Modify: `worker/bot_squad_worker/jobs.py`
- Modify: `worker/bot_squad_worker/scheduler.py`
- Modify: `worker/tests/test_jobs.py`

Daily at 11:59 UTC (cctv timing). For each project, aggregate state: dirty? queued deploys? failed deploys in last 24h? OAuth refresh status? Send ONE consolidated TG message per project per day (via the debounce mechanism this is naturally idempotent within the cooldown window).

- [ ] **Step 1: Failing tests** — clean state → no message; dirty + queued → one message with expected text.

- [ ] **Steps 2–4**: TDD cycle.

- [ ] **Step 5: Register**

```python
sched.add_job(kick_stuck, "cron", hour=11, minute=59, args=[cfg], id="kick_stuck",
              replace_existing=True)
```

- [ ] **Step 6: Commit**

```bash
git commit -m "worker: kick_stuck daily summary job"
```

### Task 8: `oauth_refresh` job (port from cctv)

**Files:**
- Create: `worker/bot_squad_worker/refresh_oauth.py`
- Modify: `worker/bot_squad_worker/jobs.py`
- Modify: `worker/bot_squad_worker/scheduler.py`
- Modify: `worker/tests/test_jobs.py`

Port the logic from the archived `signal-tracker-old/ops_refresh_oauth.py` (originally cctv's). Wraps it in a function callable by APScheduler. TG-pings on hard failure.

- [ ] **Step 1**: Read the archived script.
- [ ] **Step 2**: Port to a Python module with a single `refresh_oauth(cfg) -> dict` function.
- [ ] **Step 3**: Failing test — mock the underlying refresh and assert TG-ping on failure.
- [ ] **Step 4**: Implementation, register at `interval, hours=6`.
- [ ] **Step 5: Commit**

```bash
git commit -m "worker: oauth_refresh job ported from cctv-backend"
```

---

## Phase 4 — CLI shim, hooks, and project init

### Task 9: `deploy.sh` shim + project init drops it

**Files:**
- Create: `scripts/cli/deploy.sh`
- Modify: `scripts/cli/bot-squad` (project init drops the symlink + creates agent_team/dev branch + scaffolds recipes)

`deploy.sh` resolves slug from CWD, computes SID via the same logic as `hook_my_sid.sh` (Task 10), and POSTs to `/actions/deploy` over the Unix socket. Per-project shim: `<repo>/ops/bot-squad-bin/deploy` → symlink to this script.

- [ ] **Step 1: Write `deploy.sh`** — see spec §4.1 for the exact code (with `S-<user>-<window>-p<pane_id>` SID).

- [ ] **Step 2: Update `bot-squad project init`** — extend with three new behaviors:
  1. Drop `ops/bot-squad-bin/deploy` symlink (alongside the existing `ops/bot-squad/` data symlink — different dir, no collision)
  2. `--create-deploy-branch` flag (default true on first init): `git -C $repo checkout -b agent_team/dev master` if not present
  3. `--scaffold-recipes` flag (default true on first init): drop `data/<slug>/deploy/{staging,dev}.sh.example` template files

- [ ] **Step 3: Smoke-test in /tmp**

```bash
mkdir -p /tmp/init-test && git -C /tmp/init-test init -q
git -C /tmp/init-test commit --allow-empty -m base
/home/www/bot-squad/scripts/cli/bot-squad project init init-smoke --repo /tmp/init-test
ls -la /tmp/init-test/ops/bot-squad-bin/
ls -la /home/www/bot-squad/data/init-smoke/deploy/
git -C /tmp/init-test branch
# Expected: agent_team/dev branch exists; deploy symlink present; recipe templates dropped.
rm -rf /tmp/init-test /home/www/bot-squad/data/init-smoke
sed -i '/^\[projects\.init-smoke\]/,/^$/d' /home/www/bot-squad/config/projects.toml
```

- [ ] **Step 4: Commit**

```bash
git add scripts/cli/deploy.sh scripts/cli/bot-squad
git commit -m "cli: deploy shim + project init creates agent_team/dev + scaffolds recipes"
```

### Task 10: Hooks — `hook_my_sid.sh`, `user_prompt_submit.sh`, `stop.sh`

**Files:**
- Create: `scripts/hooks/hook_my_sid.sh`
- Create: `scripts/hooks/user_prompt_submit.sh`
- Create: `scripts/hooks/stop.sh`

`hook_my_sid.sh` (the new SID format with pane disambiguation):

```bash
#!/usr/bin/env bash
# Print SID for the current tmux context, including pane id so duplicate
# window names ("claude") still produce distinct SIDs.
# Format: S-<user>-<window>-p<pane>
set -uo pipefail

if [ -z "${TMUX:-}" ] || ! command -v tmux >/dev/null 2>&1; then
    exit 1
fi
user=$(id -un 2>/dev/null || echo u)
user=${user//[^A-Za-z0-9_]/_}

target=""
[ -n "${TMUX_PANE:-}" ] && target="-t $TMUX_PANE"
window=$(tmux display-message -p $target -F '#W' 2>/dev/null) || exit 1
window=${window//[^A-Za-z0-9_-]/_}
[ -z "$window" ] && exit 1

# Pane id like "%5"; strip the leading % so it stays alphanumeric in SIDs.
pane_raw=$(tmux display-message -p $target -F '#{pane_id}' 2>/dev/null) || exit 1
pane=${pane_raw#%}

echo "S-${user}-${window}-p${pane}"
```

`user_prompt_submit.sh`:

```bash
#!/usr/bin/env bash
set -uo pipefail
mkdir -p .claude
touch .claude/last_user_prompt_ts
exit 0
```

`stop.sh`:

```bash
#!/usr/bin/env bash
set -uo pipefail
BOT_SQUAD="${BOT_SQUAD:-/home/www/bot-squad}"

sid=$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null) || exit 0

stamp=.claude/last_user_prompt_ts
[ -f "$stamp" ] || exit 0
mtime=$(stat -c %Y "$stamp" 2>/dev/null || echo 0)
now=$(date +%s)
[ $((now - mtime)) -gt 60 ] || exit 0

slug=$(python3 - <<'PY'
import os, sys, tomllib
cfg = tomllib.loads(open("/home/www/bot-squad/config/projects.toml").read())
cwd = os.getcwd()
for slug, p in cfg.get("projects", {}).items():
    repo = p.get("repo_path", "")
    if cwd == repo or cwd.startswith(repo.rstrip("/") + "/"):
        print(slug); break
PY
)
[ -n "$slug" ] || exit 0

curl -sS --unix-socket "${WORKER_SOCK:-$BOT_SQUAD/data/_sock/worker.sock}" \
    -X POST -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"slug":sys.argv[1],"sid":sys.argv[2],"user":sys.argv[3],"message":"needs your input"}))' "$slug" "$sid" "$(id -un)")" \
    http://w/actions/tg_notify >/dev/null
exit 0
```

All three: mode 755, owned `almdudleer:www`.

- [ ] **Step 1: Write the three scripts**.
- [ ] **Step 2: Smoke test**

```bash
# Inside a tmux pane:
/home/www/bot-squad/scripts/hooks/hook_my_sid.sh
# Expected output: S-almdudleer-<window>-p<N>
cd /home/almdudleer/signal_tracker_mgmt
/home/www/bot-squad/scripts/hooks/user_prompt_submit.sh
ls -la .claude/last_user_prompt_ts
# Make stamp old, then run stop.sh:
touch -d '5 minutes ago' .claude/last_user_prompt_ts
BOT_SQUAD=/home/www/bot-squad /home/www/bot-squad/scripts/hooks/stop.sh
# Expected: TG ping arrives.
```

- [ ] **Step 3: Commit**

```bash
git add scripts/hooks/{hook_my_sid,user_prompt_submit,stop}.sh
git commit -m "hooks: SID with pane-id disambig + UserPromptSubmit + Stop ping"
```

### Task 11: Update `.claude/settings.json` template in `bot-squad project init`

**Files:**
- Modify: `scripts/cli/bot-squad`

Update the heredoc that writes `.claude/settings.json` to include all three hooks:

```json
{
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/session_start.sh"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/user_prompt_submit.sh"}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/stop.sh"}]}]
  }
}
```

Make `bot-squad project init` overwrite an existing `.claude/settings.json` only if its current content is a strict subset of the new template — otherwise leave the existing file alone (don't clobber custom user additions).

- [ ] **Step 1: Update the heredoc and overwrite logic**.

- [ ] **Step 2: Re-run for signal-tracker**

```bash
/home/www/bot-squad/scripts/cli/bot-squad project init signal-tracker --repo /home/almdudleer/signal_tracker_mgmt
git -C /home/almdudleer/signal_tracker_mgmt diff .claude/settings.json
git -C /home/almdudleer/signal_tracker_mgmt add .claude/settings.json
git -C /home/almdudleer/signal_tracker_mgmt commit -m "ops: add UserPromptSubmit + Stop hooks"
```

- [ ] **Step 3: Commit the CLI change in bot-squad**

```bash
cd /home/www/bot-squad
git add scripts/cli/bot-squad
git commit -m "cli: project init drops UserPromptSubmit + Stop hook config"
```

---

## Phase 5 — agent_team/dev branch + signal-tracker recipes + AGENTS.md

### Task 12: Verify agent_team/dev branch exists for signal-tracker

The previous-revision plan had a separate task to create the branch via `git checkout -b`. Now Task 9's project-init re-run handles it. Just verify:

- [ ] **Step 1**: `git -C /home/almdudleer/signal_tracker_mgmt branch -a` shows `agent_team/dev` and `remotes/origin/agent_team/dev`. If origin doesn't have it, push.

```bash
git -C /home/almdudleer/signal_tracker_mgmt push -u origin agent_team/dev
```

(No commit in bot-squad here; this is a one-time signal-tracker setup.)

### Task 13: Write signal-tracker's deploy recipes

**Files:**
- Create: `/home/www/bot-squad/data/signal-tracker/deploy/staging.sh`
- Create: `/home/www/bot-squad/data/signal-tracker/deploy/dev.sh`

`staging.sh` per spec §4.4 — `git checkout staging && git merge agent_team/dev --no-edit --no-ff && docker compose build/up signal-tracker-staging && smoke curl`. `dev.sh` mirrors with `signal-tracker-dev` and the dev URL. Both have `trap 'git checkout "$ORIG"' EXIT` so a mid-recipe failure restores the branch.

- [ ] **Step 1**: Write both, `chmod 755`.
- [ ] **Step 2**: Smoke (manual) — see Task 15.

(No bot-squad commit; recipes live in gitignored `data/`.)

### Task 14: `render_agents_md.py` + `bot-squad render-agents-md`

**Files:**
- Create: `scripts/cli/render_agents_md.py`
- Modify: `scripts/cli/bot-squad`

A small Python script that takes a slug, reads `data/<slug>/vision/{north-star,strategy,tactical}.md`, and writes a fresh AGENTS.md to the project's repo per spec §10's template.

The template includes the inline squash recipe:

```markdown
## Branching & commits

- Working branch: `agent_team/dev`. Branch off master, never push, never merge,
  never amend.
- Commit prefix: `[backend]`, `[web]`, `[ops]`, `[docs]`. Imperative summary,
  ≤70 chars. Co-Authored-By auto-added.
- Squash before requesting a deploy or handing off:
    `BASE=$(git merge-base HEAD master)`
    `git reset --soft "$BASE" && git commit -m "<single-line message>"`
  (Don't squash on master/staging, with a dirty tree, or if BASE == HEAD.)

## Deploy

`ops/bot-squad-bin/deploy <target> "<reason>"` — targets: `staging`, `dev`.
- Queues a deploy. The monitor processes it within ~60s when the tree is clean.
- Tree clean = `git status --porcelain` empty (logs/cache excluded).
- TG-pings on success/failure.
- You don't manage the loop. Commit, squash, request, walk away.

Manual prod release: stakeholder reviews staging → merges agent_team/dev (or
staging) into master → builds the prod container → deploys. Agents never
deploy prod.
```

- [ ] **Step 1**: Write `render_agents_md.py`.

- [ ] **Step 2**: Wire `bot-squad render-agents-md <slug>` subcommand.

- [ ] **Step 3**: Run for signal-tracker

```bash
/home/www/bot-squad/scripts/cli/bot-squad render-agents-md signal-tracker
git -C /home/almdudleer/signal_tracker_mgmt diff AGENTS.md
git -C /home/almdudleer/signal_tracker_mgmt add AGENTS.md
git -C /home/almdudleer/signal_tracker_mgmt commit -m "ops: re-render AGENTS.md with real branching/deploy"
```

- [ ] **Step 4**: Commit the script (in bot-squad)

```bash
cd /home/www/bot-squad
git add scripts/cli/render_agents_md.py scripts/cli/bot-squad
git commit -m "cli: render_agents_md helper + render-agents-md subcommand"
```

---

## Phase 6 — End-to-end verification

### Task 15: Smoke-test the entire deploy flow

- [ ] **Step 1: Restart worker to load new actions/jobs**

```bash
systemctl --user restart bot-squad-worker
sleep 3
systemctl --user status bot-squad-worker
journalctl --user -u bot-squad-worker -n 30
```

- [ ] **Step 2: Verify new actions are reachable via Unix socket**

```bash
SOCK=/home/www/bot-squad/data/_sock/worker.sock
curl --unix-socket "$SOCK" http://w/health
curl --unix-socket "$SOCK" -X POST -H 'Content-Type: application/json' \
    -d '{"chat_id":"404580642","message":"spec #3 smoke test"}' http://w/actions/tg_notify
```
Expected: 200s; TG message arrives on phone.

- [ ] **Step 3: Make a real commit on agent_team/dev and request deploy**

```bash
cd /home/almdudleer/signal_tracker_mgmt
git checkout agent_team/dev
echo "<!-- spec #3 smoke -->" >> docs/superpowers/specs/2026-05-10-spec3-guided-team-workflow-design.md
git add -A
git commit -m "[docs] spec #3 smoke test"
# Squash (manual recipe per AGENTS.md):
BASE=$(git merge-base HEAD master)
git reset --soft "$BASE" && git commit -m "[docs] spec #3 smoke"
# Request deploy:
ops/bot-squad-bin/deploy staging "spec #3 smoke test"
# Wait ~60s; tail the run log:
ls /home/www/bot-squad/data/signal-tracker/_jobs/deploy/runs/
tail -f /home/www/bot-squad/data/signal-tracker/_jobs/deploy/runs/<latest-id>.log
```
Expected: TG-ping at queue time, recipe runs, TG-ping on success, https://signal-staging.dev.uzinvestapi.com reflects the new build.

- [ ] **Step 4: Test failure path**

Make a deliberately-bad recipe (`exit 7`), request deploy, assert TG-ping reports failure rc=7.

- [ ] **Step 5: Test idle TG-ping (Stop hook)**

Open a fresh Claude session in `signal_tracker_mgmt`. Send a prompt. Wait 90s without responding. Verify TG-ping `[<sid>] needs your input` arrives, and that the SID has the form `S-almdudleer-<window>-p<N>`.

- [ ] **Step 6: Verify cron is still untouched**

```bash
crontab -l | grep -E 'bot-squad' && echo "BUG: cron entry exists" || echo "OK: cron unchanged"
```

- [ ] **Step 7: Final review**

```bash
git -C /home/www/bot-squad log --oneline | head -25
```

---

## Self-review

Before declaring spec #3 done:

- [ ] All worker tests pass: `cd /home/www/bot-squad/worker && .venv/bin/pytest -v`
- [ ] All API tests pass: `cd /home/www/bot-squad/api && .venv/bin/pytest -v`
- [ ] `systemctl --user is-active bot-squad-worker` is `active`
- [ ] `data/_sock/worker.sock` mode `0660 group www`, accessible from API container
- [ ] `data/signal-tracker/_jobs/deploy/queue/` is empty (no stuck deploys)
- [ ] Live test: agent commit on `agent_team/dev` → manual squash → `ops/bot-squad-bin/deploy staging "smoke"` → TG ping success → staging URL reflects change
- [ ] Live test: Stop hook fires after 60s of inactivity, SID format `S-<user>-<window>-p<pane>`
- [ ] `crontab -l` shows ONLY pre-existing leadintel lines
- [ ] `secrets.toml` exists; `auth.toml` no longer has `bot_token`; API container's mount excludes `secrets.toml` (`docker exec bot-squad-api ls /config/` shows only projects.toml + auth.toml)
- [ ] `agent_team/dev` branch exists in signal-tracker, pushed to origin
- [ ] AGENTS.md has the real `## Branching & commits` (with inline squash recipe) and `## Deploy` sections
- [ ] No `git_status` worker action exists (action registry has only `noop`, `deploy`, `tg_notify`, `tg_verify_login`, `kick_stuck_now`)
- [ ] No `bot-squad squash` or `bot-squad pane` subcommands
