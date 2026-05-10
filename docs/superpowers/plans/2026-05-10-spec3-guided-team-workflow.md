# Guided team workflow — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the bot-squad foundation into a daily-usable workflow: agent_team/dev branch, auto-deploy on clean tree (cctv pattern), worker actions for `deploy` / `git_status` / `tg_notify` / `kick_stuck_now`, APScheduler jobs for `deploy_monitor` / `kick_stuck` / `oauth_refresh`, Stop+UserPromptSubmit hooks, per-project deploy recipes, the `ops/bot-squad/deploy` shim, the squash helper, and a tightened secrets boundary.

**Spec:** `docs/superpowers/specs/2026-05-10-spec3-guided-team-workflow-design.md`. This plan implements §15's order at TDD grain.

**Architecture:** Same two-process split as spec #1 (API in Docker, worker as systemd user unit). Spec #3 fills out the worker's action allowlist and APScheduler job set. No web changes (UI work is spec #4–#6). Per-project recipe shell scripts live under `data/<slug>/deploy/` and are owned by the project, not the framework.

**Tech Stack:** Same as spec #1 — Python 3.12, FastAPI, APScheduler, httpx, pyjwt, pyyaml; bash for recipes and CLI shims. New deps: none beyond what's in `worker/pyproject.toml` already.

---

## File structure (additions only)

```
/home/www/bot-squad/
├── config/
│   └── secrets.toml                    ← NEW — split from auth.toml
├── scripts/
│   ├── cli/
│   │   ├── deploy.sh                   ← NEW — shim, symlinked into project repos
│   │   ├── squash.sh                   ← NEW — squash helper
│   │   ├── pane.sh                     ← NEW — tmux rename-window helper
│   │   └── render_agents_md.py         ← NEW — re-renders AGENTS.md
│   └── hooks/
│       ├── user_prompt_submit.sh       ← NEW — touches .claude/last_user_prompt_ts
│       └── stop.sh                     ← NEW — TG-pings on idle
├── worker/
│   ├── bot_squad_worker/
│   │   ├── actions.py                  ← MODIFIED: add deploy, git_status, tg_notify, kick_stuck_now
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
    └── routes_*                        ← unchanged

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
ops/bot-squad/deploy                    ← NEW symlink → /home/www/bot-squad/scripts/cli/deploy.sh
ops/bot-squad/squash                    ← NEW symlink → /home/www/bot-squad/scripts/cli/squash.sh
.claude/settings.json                   ← MODIFIED: add UserPromptSubmit + Stop hooks
AGENTS.md                               ← MODIFIED: real branching + deploy sections (re-rendered)
```

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
- Create: `/home/www/bot-squad/config/secrets.toml` — owns `bot_token` + future secrets
- Modify: `worker/bot_squad_worker/config.py` — load secrets.toml
- Modify: `api/app/config.py` — drop bot_token (API doesn't need it post-#3)
- Test: `worker/tests/test_config.py`, `api/tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

`worker/tests/test_config.py` — add:

```python
def test_secrets_loads(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "secrets.toml").write_text(
        '[telegram]\nbot_token = "TESTBOT:TOKEN"\n'
    )
    cfg = Config.load(tmp_config_dir)
    assert cfg.tg_bot_token == "TESTBOT:TOKEN"


def test_secrets_missing_raises(tmp_config_dir: Path) -> None:
    # secrets.toml absent → loading the worker config raises a clear error.
    with pytest.raises(FileNotFoundError, match="secrets.toml"):
        Config.load(tmp_config_dir)
```

`api/tests/test_config.py` — modify the auth-loader test so it does NOT expect a bot_token:

```python
def test_auth_config_loads_no_token(tmp_bot_squad: Path) -> None:
    auth = AuthConfig.load(tmp_bot_squad / "config")
    assert not hasattr(auth, "bot_token")        # API does not see the bot token
    assert 12345 in auth.allowed_ids
```

(Adjust the existing `test_auth_config_loads` to expect `bot_token` removed from auth.toml.)

- [ ] **Step 2: Run to verify tests fail**

```bash
cd /home/www/bot-squad/worker && .venv/bin/pytest tests/test_config.py::test_secrets_loads -v
cd /home/www/bot-squad/api && .venv/bin/pytest tests/test_config.py::test_auth_config_loads_no_token -v
```
Expected: both fail (worker has no `tg_bot_token` attr; api still has bot_token in AuthConfig).

- [ ] **Step 3: Update `worker/bot_squad_worker/config.py`**

Add a sibling loader for secrets.toml:

```python
@dataclass(frozen=True)
class Config:
    config_dir: Path
    projects: dict[str, Project] = field(default_factory=dict)
    tg_bot_token: str = ""

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

- [ ] **Step 4: Update `api/app/config.py`**

Remove `bot_token` from `AuthConfig`:

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

API code that previously read `auth_config.bot_token` for TG-Login HMAC verification needs to call the worker's `tg_verify_login` action instead. **Wait — the TG Login HMAC needs the bot_token.** The cleanest fix: a NEW worker action `tg_verify_login(payload)` that the API calls during `/auth/tg`. API ships the payload, worker verifies + returns user info. API then issues the JWT.

So `routes_auth.py::tg_login` becomes:

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
    token = issue_jwt({"tg_id": int(user["id"]), "name": user.get("first_name", "")},
                     request.app.state.jwt_secret,
                     ttl_seconds=cfg.session_ttl_seconds)
    response.set_cookie(...)   # unchanged
    return {"ok": True, "tg_id": int(user["id"])}
```

Add a worker action `tg_verify_login` that wraps the existing `verify_tg_login` but reads the bot token from `Config.tg_bot_token`. Unit-tested directly.

- [ ] **Step 5: Update auth.toml on disk and split out secrets.toml**

```bash
# Move bot_token from auth.toml to secrets.toml.
cd /home/www/bot-squad/config
# Remove the bot_token line from auth.toml (manual edit or sed):
sed -i '/^bot_token/d' auth.toml
# Write secrets.toml:
cat > secrets.toml <<'EOF'
# bot-squad secrets — gitignored, mounted into the worker only.
# Do NOT mount this into the API container.

[telegram]
bot_token = "8036906248:AAGSZYha1vNkKvA3FYoy_tS7YuTQhbo84c4"
EOF
chmod 640 secrets.toml
chgrp www secrets.toml
```

Update root `.gitignore` so `config/secrets.toml` is gitignored (it should be already by `.env` line + manual addition):

```
config/secrets.toml
```

Update docker-compose.yml so the worker (when later containerized in some future spec) would mount config/secrets.toml — but for now, the worker reads from disk. The API container's `./config:/config:ro` mount inherits secrets.toml, which is bad. Change to mount only what the API needs:

In `docker-compose.yml`, replace `- ./config:/config:ro` with two explicit binds:

```yaml
    volumes:
      - ./config/projects.toml:/config/projects.toml:ro
      - ./config/auth.toml:/config/auth.toml:ro
      - ./data:/data:rw
```

This way the API container literally cannot see `secrets.toml` — defense in depth.

- [ ] **Step 6: Run tests**

```bash
cd /home/www/bot-squad/worker && .venv/bin/pytest -v
cd /home/www/bot-squad/api && .venv/bin/pytest -v
```
Expected: all green.

- [ ] **Step 7: Restart services and smoke-test**

```bash
systemctl --user restart bot-squad-worker
sleep 2
curl --unix-socket /home/www/bot-squad/data/_sock/worker.sock http://w/health
docker compose -f /home/www/bot-squad/docker-compose.yml up -d --force-recreate
sleep 5
curl -s https://bot-squad.dev.uzinvestapi.com/api/health
```
Expected: both ok; worker.alive=true.

- [ ] **Step 8: Commit**

```bash
cd /home/www/bot-squad
git add config/auth.toml.example config/secrets.toml.example  # if you wrote examples
git add worker/bot_squad_worker/config.py worker/tests/test_config.py
git add api/app/config.py api/app/routes_auth.py api/tests/test_config.py
git add docker-compose.yml
git commit -m "secrets: split bot_token out of auth.toml; API no longer reads it"
```

---

## Phase 2 — TG client + tg_notify action

### Task 2: Worker TG client module with debounce

**Files:**
- Create: `worker/bot_squad_worker/tg.py`
- Create: `worker/tests/test_tg.py`

- [ ] **Step 1: Failing test**

`worker/tests/test_tg.py`:

```python
"""Tests for worker tg client."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from bot_squad_worker.config import Config
from bot_squad_worker.tg import TgClient, _debounce_path


def test_debounce_skips_duplicates(tmp_config_dir: Path):
    cfg = Config.load(tmp_config_dir)
    client = TgClient(cfg, http_post=_fake_post := _make_fake_post())
    client.send(chat_id="999", text="hello", sid="S-test-foo")
    client.send(chat_id="999", text="hello", sid="S-test-foo")
    assert _fake_post.calls == 1, "duplicate within 60s should debounce"


def test_debounce_allows_after_window(tmp_config_dir: Path, monkeypatch):
    cfg = Config.load(tmp_config_dir)
    fake = _make_fake_post()
    client = TgClient(cfg, http_post=fake, cooldown_sec=1)
    client.send(chat_id="999", text="hello", sid="S-test-foo")
    time.sleep(1.1)
    client.send(chat_id="999", text="hello", sid="S-test-foo")
    assert fake.calls == 2


def test_prefix_includes_sid(tmp_config_dir: Path):
    cfg = Config.load(tmp_config_dir)
    fake = _make_fake_post()
    client = TgClient(cfg, http_post=fake)
    client.send(chat_id="999", text="hello", sid="S-alex-tg-deeplink")
    assert fake.last_text.startswith("[S-alex-tg-deeplink] hello")


def test_no_token_skips_silently(tmp_config_dir: Path):
    # If tg_bot_token is empty, send should no-op without raising.
    (tmp_config_dir / "secrets.toml").write_text("[telegram]\nbot_token = \"\"\n")
    cfg = Config.load(tmp_config_dir)
    fake = _make_fake_post()
    client = TgClient(cfg, http_post=fake)
    client.send(chat_id="999", text="hello", sid="S-x")
    assert fake.calls == 0


def _make_fake_post():
    class Fake:
        def __init__(self): self.calls = 0; self.last_text = None
        def __call__(self, url, data, timeout):
            self.calls += 1; self.last_text = data["text"]
            return type("R", (), {"status_code": 200})()
    return Fake()
```

- [ ] **Step 2: Verify it fails**

```bash
cd /home/www/bot-squad/worker && .venv/bin/pytest tests/test_tg.py -v
```
Expected: ImportError on `bot_squad_worker.tg`.

- [ ] **Step 3: Implementation**

`worker/bot_squad_worker/tg.py`:

```python
"""Telegram outbound client with debounce + SID prefixing."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from bot_squad_worker.config import Config


def _debounce_path(cfg: Config, hash_hex: str) -> Path:
    return cfg.data_dir / "_worker" / "tg_debounce" / hash_hex


class TgClient:
    """Sends Telegram messages via watchbot, with same-payload debounce."""

    TG_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        cfg: Config,
        http_post: Optional[Callable] = None,
        cooldown_sec: int = 60,
    ) -> None:
        self.cfg = cfg
        self.cooldown_sec = cooldown_sec
        self._post = http_post or self._default_post

    def _default_post(self, url: str, data: dict, timeout: float):
        return httpx.post(url, data=data, timeout=timeout)

    def send(self, *, chat_id: str, text: str, sid: str = "", user: str = "") -> bool:
        """Send a message. Returns True if delivered, False if debounced or no token.

        Prefixes the message with [<sid> @ <user>] (or [<sid>] if no user).
        Debounces: same chat+sid+text within `cooldown_sec` is silently skipped.
        Empty bot_token silently skips (test environments, disabled mode).
        """
        if not self.cfg.tg_bot_token:
            return False

        prefix = ""
        if sid and user:
            prefix = f"[{sid} @ {user}] "
        elif sid:
            prefix = f"[{sid}] "
        body = prefix + text

        h = hashlib.sha1(f"{chat_id}:{body}".encode()).hexdigest()
        marker = _debounce_path(self.cfg, h)
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            age = time.time() - marker.stat().st_mtime
            if age < self.cooldown_sec:
                return False
        marker.touch()

        url = self.TG_URL.format(token=self.cfg.tg_bot_token)
        r = self._post(url, {"chat_id": chat_id, "text": body}, 10.0)
        return getattr(r, "status_code", 0) == 200
```

- [ ] **Step 4: Run tests**

```bash
cd /home/www/bot-squad/worker && .venv/bin/pytest tests/test_tg.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/bot_squad_worker/tg.py worker/tests/test_tg.py
git commit -m "worker: TG outbound client with debounce + SID prefix"
```

### Task 3: `tg_notify` worker action

**Files:**
- Modify: `worker/bot_squad_worker/actions.py`
- Modify: `worker/tests/test_actions.py`

- [ ] **Step 1: Failing test**

Add to `test_actions.py`:

```python
def test_tg_notify_dispatches_to_client(tmp_config_dir: Path, monkeypatch):
    from bot_squad_worker import actions as A
    cfg = Config.load(tmp_config_dir)
    sent = []
    class FakeTg:
        def send(self, **kw): sent.append(kw); return True
    monkeypatch.setattr(A, "_get_tg_client", lambda c: FakeTg())
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    out = A.dispatch("tg_notify", {"chat_id": "404580642", "message": "test"})
    assert out["ok"] is True
    assert sent == [{"chat_id": "404580642", "text": "test", "sid": "", "user": ""}]


def test_tg_notify_with_slug_resolves_chat(tmp_config_dir: Path, monkeypatch):
    from bot_squad_worker import actions as A
    cfg = Config.load(tmp_config_dir)
    sent = []
    class FakeTg:
        def send(self, **kw): sent.append(kw); return True
    monkeypatch.setattr(A, "_get_tg_client", lambda c: FakeTg())
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    A.dispatch("tg_notify", {"slug": "test-project", "message": "hi"})
    assert sent[0]["chat_id"] == cfg.projects["test-project"].tg_chat


def test_tg_notify_rejects_missing_message():
    from bot_squad_worker import actions as A
    with pytest.raises(A.ActionError, match="missing.*message"):
        A.dispatch("tg_notify", {"chat_id": "0"})


def test_tg_notify_rejects_unknown_slug():
    from bot_squad_worker import actions as A
    with pytest.raises(A.ActionError, match="unknown slug"):
        A.dispatch("tg_notify", {"slug": "nope", "message": "x"})
```

- [ ] **Step 2: Verify it fails**

```bash
cd /home/www/bot-squad/worker && .venv/bin/pytest tests/test_actions.py -v
```
Expected: import errors / failures on the new tests.

- [ ] **Step 3: Implementation**

In `actions.py`, add:

```python
import os
from pathlib import Path
from bot_squad_worker.config import Config
from bot_squad_worker.tg import TgClient

_CFG: Config | None = None
_TG: TgClient | None = None


def _get_config() -> Config:
    global _CFG
    if _CFG is None:
        _CFG = Config.load(Path(os.environ.get("BOT_SQUAD_CONFIG", "/home/www/bot-squad/config")))
    return _CFG


def _get_tg_client(cfg: Config) -> TgClient:
    global _TG
    if _TG is None:
        _TG = TgClient(cfg)
    return _TG


def _action_tg_notify(params: dict) -> dict:
    extra = set(params) - {"slug", "chat_id", "message", "sid", "user"}
    if extra:
        raise ActionError(f"tg_notify got unexpected params: {sorted(extra)}")
    if "message" not in params:
        raise ActionError("tg_notify missing required param: message")

    cfg = _get_config()
    chat_id = params.get("chat_id")
    if not chat_id:
        slug = params.get("slug")
        if slug not in cfg.projects:
            raise ActionError(f"tg_notify: unknown slug {slug!r}")
        chat_id = cfg.projects[slug].tg_chat
    tg = _get_tg_client(cfg)
    sent = tg.send(
        chat_id=str(chat_id),
        text=params["message"],
        sid=params.get("sid", ""),
        user=params.get("user", ""),
    )
    return {"ok": True, "sent": sent}


ACTION_REGISTRY["tg_notify"] = _action_tg_notify
```

- [ ] **Step 4: Run tests**

```bash
cd /home/www/bot-squad/worker && .venv/bin/pytest tests/test_actions.py -v
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add worker/bot_squad_worker/actions.py worker/tests/test_actions.py
git commit -m "worker: tg_notify action with slug/chat resolution"
```

---

## Phase 3 — Deploy infrastructure

### Task 4: `git_status` worker action

**Files:**
- Modify: `worker/bot_squad_worker/actions.py`
- Modify: `worker/tests/test_actions.py`

- [ ] **Step 1: Failing test**

Add:

```python
def test_git_status_clean(tmp_config_dir: Path, monkeypatch):
    from bot_squad_worker import actions as A
    import subprocess, tempfile

    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=x@y", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)

    # Override projects.toml's repo_path to point at this temp repo.
    p = Project(slug="test-project", display_name="Test",
                repo_path=Path(repo), deploy_branch="agent_team/dev",
                master_branch="master", prod_url="", staging_url="", dev_url="",
                deploy_targets=("staging",), tg_chat="0")
    cfg = Config(config_dir=tmp_config_dir, projects={"test-project": p})
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    out = A.dispatch("git_status", {"slug": "test-project"})
    assert out == {"ok": True, "clean": True, "dirty_paths": []}


def test_git_status_dirty(tmp_config_dir: Path, monkeypatch):
    # Same setup but with an untracked file in the repo.
    ...   # (full test analogous to clean case)


def test_git_status_unknown_slug(tmp_config_dir: Path, monkeypatch):
    from bot_squad_worker import actions as A
    cfg = Config.load(tmp_config_dir)
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(A.ActionError, match="unknown slug"):
        A.dispatch("git_status", {"slug": "nope"})
```

- [ ] **Step 2: Verify it fails**

Expected: `unknown action: 'git_status'`.

- [ ] **Step 3: Implementation**

In `actions.py`:

```python
import subprocess


_QUIESCENCE_IGNORE_PATTERNS = (
    "ops/state/",
    "tests/playwright-report/",
    "__pycache__/",
)


def _is_ignored_for_quiescence(path: str) -> bool:
    return any(p in path for p in _QUIESCENCE_IGNORE_PATTERNS)


def _action_git_status(params: dict) -> dict:
    extra = set(params) - {"slug"}
    if extra:
        raise ActionError(f"git_status got unexpected params: {sorted(extra)}")
    if "slug" not in params:
        raise ActionError("git_status missing required param: slug")
    cfg = _get_config()
    proj = cfg.projects.get(params["slug"])
    if proj is None:
        raise ActionError(f"git_status: unknown slug {params['slug']!r}")
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(proj.repo_path),
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise ActionError(f"git status failed: {r.stderr.strip()}")
    dirty = [
        line[3:] for line in r.stdout.splitlines()
        if line and not _is_ignored_for_quiescence(line[3:])
    ]
    return {"ok": True, "clean": len(dirty) == 0, "dirty_paths": dirty[:5]}


ACTION_REGISTRY["git_status"] = _action_git_status
```

- [ ] **Step 4: Run tests**

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/bot_squad_worker/actions.py worker/tests/test_actions.py
git commit -m "worker: git_status action with quiescence-ignore filter"
```

### Task 5: Deploy queue + recipe runner module

**Files:**
- Create: `worker/bot_squad_worker/deploy.py`
- Create: `worker/tests/test_deploy.py`

- [ ] **Step 1: Failing test**

`worker/tests/test_deploy.py`:

```python
"""Tests for deploy queue + recipe runner."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bot_squad_worker.config import Config, Project
from bot_squad_worker.deploy import (
    enqueue,
    list_queued,
    run_next,
    DeployResult,
)


@pytest.fixture
def proj_with_repo(tmp_config_dir: Path, tmp_path: Path) -> tuple[Config, Path]:
    """Provides a Config + a tiny git repo with a recipe that just `echo OK`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=x@y", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)

    p = Project(slug="proj", display_name="Proj",
                repo_path=repo, deploy_branch="agent_team/dev",
                master_branch="master", prod_url="", staging_url="", dev_url="",
                deploy_targets=("staging",), tg_chat="0")
    cfg = Config(config_dir=tmp_config_dir, projects={"proj": p})

    # Drop a recipe.
    recipe = cfg.data_dir / "proj" / "deploy" / "staging.sh"
    recipe.parent.mkdir(parents=True, exist_ok=True)
    recipe.write_text("#!/bin/bash\necho hello world\n")
    recipe.chmod(0o755)
    return cfg, repo


def test_enqueue_writes_queue_file(proj_with_repo):
    cfg, _ = proj_with_repo
    qid = enqueue(cfg, slug="proj", target="staging", reason="test", requested_by="me")
    files = list_queued(cfg, slug="proj")
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["target"] == "staging"
    assert body["id"] == qid


def test_enqueue_rejects_unknown_target(proj_with_repo):
    cfg, _ = proj_with_repo
    with pytest.raises(ValueError, match="unknown target"):
        enqueue(cfg, slug="proj", target="prod", reason="x", requested_by="me")


def test_run_next_executes_recipe_on_clean_tree(proj_with_repo):
    cfg, repo = proj_with_repo
    enqueue(cfg, slug="proj", target="staging", reason="test", requested_by="me")
    result = run_next(cfg, slug="proj")
    assert result is not None
    assert result.success
    assert "hello world" in result.log
    # Queue file is in processed/, with .ok suffix.
    processed = list((cfg.data_dir / "proj" / "_jobs" / "deploy" / "processed").glob("*.ok"))
    assert len(processed) == 1


def test_run_next_skips_when_dirty(proj_with_repo):
    cfg, repo = proj_with_repo
    (repo / "dirty.txt").write_text("uncommitted")
    enqueue(cfg, slug="proj", target="staging", reason="x", requested_by="me")
    result = run_next(cfg, slug="proj")
    assert result is None       # nothing run; queue file remains
    files = list_queued(cfg, slug="proj")
    assert len(files) == 1


def test_run_next_failure_records_rc(proj_with_repo):
    cfg, _ = proj_with_repo
    bad = cfg.data_dir / "proj" / "deploy" / "staging.sh"
    bad.write_text("#!/bin/bash\nexit 7\n")
    enqueue(cfg, slug="proj", target="staging", reason="bad", requested_by="me")
    result = run_next(cfg, slug="proj")
    assert result is not None
    assert not result.success
    assert result.return_code == 7
    fails = list((cfg.data_dir / "proj" / "_jobs" / "deploy" / "processed").glob("*.fail.7"))
    assert len(fails) == 1
```

- [ ] **Step 2: Verify it fails**

Expected: ImportError on `bot_squad_worker.deploy`.

- [ ] **Step 3: Implementation**

`worker/bot_squad_worker/deploy.py`:

```python
"""Deploy queue + per-project recipe runner."""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bot_squad_worker.config import Config


@dataclass
class DeployResult:
    queue_id: str
    success: bool
    return_code: int
    log: str


def _queue_dir(cfg: Config, slug: str) -> Path:
    return cfg.data_dir / slug / "_jobs" / "deploy" / "queue"


def _processed_dir(cfg: Config, slug: str) -> Path:
    return cfg.data_dir / slug / "_jobs" / "deploy" / "processed"


def _processing_dir(cfg: Config, slug: str) -> Path:
    return cfg.data_dir / slug / "_jobs" / "deploy" / "processing"


def _runs_dir(cfg: Config, slug: str) -> Path:
    return cfg.data_dir / slug / "_jobs" / "deploy" / "runs"


def _recipe_path(cfg: Config, slug: str, target: str) -> Path:
    return cfg.data_dir / slug / "deploy" / f"{target}.sh"


def enqueue(cfg: Config, *, slug: str, target: str, reason: str, requested_by: str) -> str:
    proj = cfg.projects.get(slug)
    if proj is None:
        raise ValueError(f"unknown slug: {slug}")
    if target not in proj.deploy_targets:
        raise ValueError(f"unknown target: {target} (allowed: {sorted(proj.deploy_targets)})")
    qd = _queue_dir(cfg, slug)
    qd.mkdir(parents=True, exist_ok=True)
    qid = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    body = {
        "id": qid,
        "slug": slug,
        "target": target,
        "reason": reason,
        "requested_by": requested_by,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (qd / f"{qid}.json").write_text(json.dumps(body, indent=2))
    return qid


def list_queued(cfg: Config, *, slug: str) -> list[Path]:
    qd = _queue_dir(cfg, slug)
    if not qd.exists():
        return []
    return sorted(qd.glob("*.json"))


def _is_clean(repo: Path) -> bool:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return False
    return all(
        any(skip in line for skip in ("ops/state/", "playwright-report/", "__pycache__/"))
        or not line.strip()
        for line in r.stdout.splitlines()
    )


def run_next(cfg: Config, *, slug: str) -> Optional[DeployResult]:
    """Pop the oldest queued deploy for slug and run its recipe.

    Returns None if queue is empty OR the tree is dirty.
    """
    queue = list_queued(cfg, slug=slug)
    if not queue:
        return None
    proj = cfg.projects[slug]
    if not _is_clean(proj.repo_path):
        return None
    qfile = queue[0]
    body = json.loads(qfile.read_text())
    target = body["target"]
    recipe = _recipe_path(cfg, slug, target)
    if not recipe.exists():
        # Move to processed with a synthetic failure (no recipe = misconfigured).
        _processed_dir(cfg, slug).mkdir(parents=True, exist_ok=True)
        qfile.rename(_processed_dir(cfg, slug) / f"{qfile.stem}.fail.99")
        return DeployResult(queue_id=body["id"], success=False, return_code=99,
                            log=f"recipe missing: {recipe}")

    # Move to processing/ then run.
    _processing_dir(cfg, slug).mkdir(parents=True, exist_ok=True)
    _runs_dir(cfg, slug).mkdir(parents=True, exist_ok=True)
    in_flight = _processing_dir(cfg, slug) / qfile.name
    qfile.rename(in_flight)

    log_path = _runs_dir(cfg, slug) / f"{body['id']}.log"
    with log_path.open("w") as logfile:
        logfile.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] starting {target} for {slug}\n")
        logfile.flush()
        rc = subprocess.run(["bash", str(recipe)],
                            cwd=str(proj.repo_path),
                            stdout=logfile, stderr=subprocess.STDOUT,
                            check=False).returncode
        logfile.write(f"\n[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] exit {rc}\n")
    log = log_path.read_text()

    suffix = ".ok" if rc == 0 else f".fail.{rc}"
    _processed_dir(cfg, slug).mkdir(parents=True, exist_ok=True)
    in_flight.rename(_processed_dir(cfg, slug) / f"{in_flight.stem}{suffix}")
    return DeployResult(queue_id=body["id"], success=(rc == 0), return_code=rc, log=log)
```

- [ ] **Step 4: Run tests**

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/bot_squad_worker/deploy.py worker/tests/test_deploy.py
git commit -m "worker: deploy queue + per-project recipe runner"
```

### Task 6: `deploy` worker action

**Files:**
- Modify: `worker/bot_squad_worker/actions.py`
- Modify: `worker/tests/test_actions.py`

- [ ] **Step 1: Failing test**

```python
def test_deploy_action_enqueues(tmp_config_dir: Path, monkeypatch, tmp_path: Path):
    from bot_squad_worker import actions as A, deploy as D
    # Set up a project with a target...
    p = Project(slug="proj", display_name="Proj", repo_path=tmp_path,
                deploy_branch="agent_team/dev", master_branch="master",
                prod_url="", staging_url="", dev_url="",
                deploy_targets=("staging",), tg_chat="0")
    cfg = Config(config_dir=tmp_config_dir, projects={"proj": p})
    monkeypatch.setattr(A, "_get_config", lambda: cfg)

    out = A.dispatch("deploy", {"slug": "proj", "target": "staging",
                                 "reason": "test", "requested_by": "me"})
    assert out["ok"] is True
    assert "queue_id" in out
    assert len(D.list_queued(cfg, slug="proj")) == 1


def test_deploy_action_rejects_bad_target(tmp_config_dir, monkeypatch, tmp_path):
    from bot_squad_worker import actions as A
    p = Project(slug="proj", display_name="Proj", repo_path=tmp_path,
                deploy_branch="agent_team/dev", master_branch="master",
                prod_url="", staging_url="", dev_url="",
                deploy_targets=("staging",), tg_chat="0")
    cfg = Config(config_dir=tmp_config_dir, projects={"proj": p})
    monkeypatch.setattr(A, "_get_config", lambda: cfg)
    with pytest.raises(A.ActionError, match="unknown target"):
        A.dispatch("deploy", {"slug": "proj", "target": "prod",
                              "reason": "x", "requested_by": "me"})
```

- [ ] **Step 2: Verify failure** — `unknown action: 'deploy'`.

- [ ] **Step 3: Implementation**

```python
from bot_squad_worker import deploy as _deploy_mod


def _action_deploy(params: dict) -> dict:
    extra = set(params) - {"slug", "target", "reason", "requested_by"}
    if extra:
        raise ActionError(f"deploy got unexpected params: {sorted(extra)}")
    for required in ("slug", "target", "reason", "requested_by"):
        if required not in params:
            raise ActionError(f"deploy missing required param: {required}")
    cfg = _get_config()
    try:
        qid = _deploy_mod.enqueue(
            cfg,
            slug=params["slug"],
            target=params["target"],
            reason=params["reason"],
            requested_by=params["requested_by"],
        )
    except ValueError as e:
        raise ActionError(str(e)) from e
    return {"ok": True, "queue_id": qid, "queued_at": time.time()}


ACTION_REGISTRY["deploy"] = _action_deploy
```

- [ ] **Step 4: Run tests** — green.

- [ ] **Step 5: Commit**

```bash
git add worker/bot_squad_worker/actions.py worker/tests/test_actions.py
git commit -m "worker: deploy action queues request file"
```

### Task 7: `deploy_monitor` scheduler job

**Files:**
- Modify: `worker/bot_squad_worker/jobs.py`
- Modify: `worker/bot_squad_worker/scheduler.py`
- Modify: `worker/tests/test_jobs.py`

- [ ] **Step 1: Failing test**

`test_jobs.py` add:

```python
def test_deploy_monitor_runs_queued_and_pings_on_success(tmp_config_dir, tmp_path, monkeypatch):
    from bot_squad_worker import jobs, deploy
    # Build a project with a recipe that succeeds.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=x@y", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    p = Project(slug="proj", display_name="Proj", repo_path=repo,
                deploy_branch="agent_team/dev", master_branch="master",
                prod_url="", staging_url="", dev_url="",
                deploy_targets=("staging",), tg_chat="999")
    cfg = Config(config_dir=tmp_config_dir, projects={"proj": p})
    recipe = cfg.data_dir / "proj" / "deploy" / "staging.sh"
    recipe.parent.mkdir(parents=True, exist_ok=True)
    recipe.write_text("#!/bin/bash\necho deployed\n")
    recipe.chmod(0o755)

    deploy.enqueue(cfg, slug="proj", target="staging", reason="test", requested_by="me")

    sent = []
    class FakeTg:
        def send(self, **kw): sent.append(kw); return True
    monkeypatch.setattr(jobs, "_get_tg_client", lambda c: FakeTg())

    jobs.deploy_monitor(cfg)

    # Recipe ran, queue empty, processed has .ok file, TG was pinged twice (queued + success).
    assert deploy.list_queued(cfg, slug="proj") == []
    assert any(t["chat_id"] == "999" and "succeeded" in t["text"] for t in sent)
```

- [ ] **Step 2: Verify failure** — `deploy_monitor` doesn't exist.

- [ ] **Step 3: Implementation**

In `jobs.py`:

```python
import logging

from bot_squad_worker import deploy
from bot_squad_worker.tg import TgClient

log = logging.getLogger(__name__)
_TG: TgClient | None = None


def _get_tg_client(cfg: Config) -> TgClient:
    global _TG
    if _TG is None:
        _TG = TgClient(cfg)
    return _TG


def deploy_monitor(cfg: Config) -> None:
    """Iterate registered projects; pop and run any queued deploy."""
    tg = _get_tg_client(cfg)
    for slug, proj in cfg.projects.items():
        try:
            queue = deploy.list_queued(cfg, slug=slug)
            if not queue:
                continue
            # First TG ping: deploy queued.
            head = json.loads(queue[0].read_text())
            tg.send(chat_id=str(proj.tg_chat),
                    text=f"🚚 deploy {head['target']} starting — {slug} — {head['reason']}",
                    sid=f"deploy_monitor")
            result = deploy.run_next(cfg, slug=slug)
            if result is None:
                # Tree dirty.
                continue
            if result.success:
                tg.send(chat_id=str(proj.tg_chat),
                        text=f"✅ deploy {head['target']} succeeded — {slug}",
                        sid="deploy_monitor")
            else:
                tg.send(chat_id=str(proj.tg_chat),
                        text=f"❌ deploy {head['target']} FAILED rc={result.return_code} — {slug}",
                        sid="deploy_monitor")
        except Exception as e:
            log.exception("deploy_monitor error for %s: %s", slug, e)
```

- [ ] **Step 4: Register in `scheduler.py`**

```python
from bot_squad_worker.jobs import deploy_monitor

sched.add_job(deploy_monitor, "interval", seconds=60, args=[cfg], id="deploy_monitor",
              replace_existing=True)
```

- [ ] **Step 5: Run tests** — green.

- [ ] **Step 6: Commit**

```bash
git add worker/bot_squad_worker/jobs.py worker/bot_squad_worker/scheduler.py worker/tests/test_jobs.py
git commit -m "worker: deploy_monitor scheduler job — runs recipes when tree is clean"
```

### Task 8: `kick_stuck` and `kick_stuck_now` action

(Mirrors task 7's structure: write test fixtures with a project that has dirty tree + queued deploys; assert summary message contains expected counts; commit. Implementation of `kick_stuck(cfg)` builds the daily summary and calls TgClient. `_action_kick_stuck_now(params)` just calls `kick_stuck(cfg)` synchronously.)

Acceptance: `kick_stuck` job sends one consolidated TG per project per day if anything is "stuck" (dirty tree, queued+pending deploy, etc.); registered in scheduler at `cron, hour=11, minute=59`.

- [ ] **Step 1**: Failing test in `test_jobs.py` covering: (a) clean state → no message; (b) dirty + queued → one message with expected text. Failing test in `test_actions.py` for `kick_stuck_now`.

- [ ] **Step 2-4**: Implementation in `jobs.py`. Add `kick_stuck(cfg)`, register in scheduler. Add `_action_kick_stuck_now` to actions registry.

- [ ] **Step 5**: Run tests, commit.

```bash
git commit -m "worker: kick_stuck daily summary + on-demand action"
```

### Task 9: `oauth_refresh` job (port from cctv)

`scripts/cli/refresh_oauth.py` exists in the archived `signal-tracker-old/`. Port the logic into `worker/bot_squad_worker/refresh_oauth.py` and call it from a new `oauth_refresh(cfg)` job that fires every 6 hours.

- [ ] **Step 1**: Read the archived `signal-tracker-old/ops_refresh_oauth.py`. Port its core (idempotent token refresh) into a Python module that can be called as a function (not just CLI).

- [ ] **Step 2**: Failing test asserting the function returns a status string and TG-pings on failure (mock the actual refresh).

- [ ] **Step 3**: Implementation + scheduler registration.

- [ ] **Step 4**: Run tests, commit.

```bash
git commit -m "worker: oauth_refresh job ported from cctv-backend"
```

---

## Phase 4 — CLI shims, hooks, and project init

### Task 10: `deploy.sh` shim + project init drops it

**Files:**
- Create: `scripts/cli/deploy.sh`
- Modify: `scripts/cli/bot-squad` (project init drops the symlink)

- [ ] **Step 1: Write the shim**

`scripts/cli/deploy.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:?usage: deploy <target> \"<reason>\"}"
REASON="${2:-(no reason given)}"
SOCK="${WORKER_SOCK:-/home/www/bot-squad/data/_sock/worker.sock}"
SLUG=$(python3 - <<'PY'
import os, sys, tomllib
cfg = tomllib.loads(open("/home/www/bot-squad/config/projects.toml").read())
cwd = os.getcwd()
for slug, p in cfg.get("projects", {}).items():
    repo = p.get("repo_path", "")
    if cwd == repo or cwd.startswith(repo.rstrip("/") + "/"):
        print(slug); break
PY
)
[ -n "$SLUG" ] || { echo "deploy: not in a registered bot-squad project (cwd=$(pwd))" >&2; exit 2; }

USER_NAME=$(id -un)
SID=""
if [ -n "${TMUX:-}" ] && command -v tmux >/dev/null; then
    target="${TMUX_PANE:+-t $TMUX_PANE}"
    WIN=$(tmux display-message -p $target -F '#W' 2>/dev/null) || WIN=""
    [ -n "$WIN" ] && SID="S-${USER_NAME}-${WIN}"
fi
REQUESTED_BY="${SID:-$USER_NAME}"

curl -sS --unix-socket "$SOCK" \
    -X POST -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"slug":sys.argv[1],"target":sys.argv[2],"reason":sys.argv[3],"requested_by":sys.argv[4]}))' "$SLUG" "$TARGET" "$REASON" "$REQUESTED_BY")" \
    http://w/actions/deploy
echo
```

Mode 755, owned `almdudleer:www`.

- [ ] **Step 2: Update `bot-squad project init` to drop a symlink**

In the `cmd_project_init` function, after creating `ops/bot-squad`, add:

```bash
mkdir -p "$repo/ops/bot-squad-bin"
ln -sf "$BOT_SQUAD/scripts/cli/deploy.sh" "$repo/ops/bot-squad-bin/deploy"
ln -sf "$BOT_SQUAD/scripts/cli/squash.sh" "$repo/ops/bot-squad-bin/squash"
```

(Uses `ops/bot-squad-bin/` to avoid colliding with the `ops/bot-squad/` data symlink.)

Update AGENTS.md template references and the spec accordingly.

(Wait — the AGENTS.md says `ops/bot-squad/deploy`. Let me reconcile: drop the deploy/squash binaries INTO the same `ops/bot-squad/` symlinked dir, but that dir is the data dir — putting executables in there is awkward. Better: put them in `ops/bot-squad-bin/` and reference that path in AGENTS.md. Update the AGENTS.md wording in the migration script.)

- [ ] **Step 3: Smoke-test in /tmp**

```bash
mkdir -p /tmp/init-test
git -C /tmp/init-test init -q
/home/www/bot-squad/scripts/cli/bot-squad project init init-smoke --repo /tmp/init-test
ls -la /tmp/init-test/ops/bot-squad-bin/
```
Expected: deploy + squash symlinks present. Clean up after.

- [ ] **Step 4: Commit**

```bash
git add scripts/cli/deploy.sh scripts/cli/bot-squad
git commit -m "cli: deploy shim + project init drops ops/bot-squad-bin/ shortcuts"
```

### Task 11: `squash.sh` helper

**Files:**
- Create: `scripts/cli/squash.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# squash.sh — collapse all commits since master into one.
# Usage:  ops/bot-squad-bin/squash "<single-line message>"
set -euo pipefail

MSG="${1:?usage: squash \"<single-line commit message>\"}"

if [ -n "$(git status --porcelain)" ]; then
    echo "squash: working tree dirty; commit or stash first" >&2
    exit 2
fi

CUR=$(git rev-parse --abbrev-ref HEAD)
case "$CUR" in
    master|main|staging|HEAD)
        echo "squash: refusing to squash on '$CUR'" >&2
        exit 2
        ;;
esac

BASE=$(git merge-base HEAD master)
if [ "$BASE" = "$(git rev-parse HEAD)" ]; then
    echo "squash: nothing to squash (HEAD == merge-base with master)" >&2
    exit 0
fi

git reset --soft "$BASE"
git commit -m "$MSG"
echo "squashed $(git rev-list --count "$BASE..HEAD" 2>/dev/null || echo 1) commit(s) into one."
```

- [ ] **Step 2: Smoke-test**

```bash
mkdir -p /tmp/squash-test && cd /tmp/squash-test
git init -q
git -c user.email=x@y -c user.name=t commit --allow-empty -m base
git checkout -b agent_team/dev
git -c user.email=x@y -c user.name=t commit --allow-empty -m "step 1"
git -c user.email=x@y -c user.name=t commit --allow-empty -m "step 2"
git -c user.email=x@y -c user.name=t commit --allow-empty -m "step 3"
/home/www/bot-squad/scripts/cli/squash.sh "feat: smoke squash"
git log --oneline
# Expected: 2 commits — "feat: smoke squash" and "base".
cd / && rm -rf /tmp/squash-test
```

- [ ] **Step 3: Commit**

```bash
git add scripts/cli/squash.sh
git commit -m "cli: squash helper collapses agent_team/dev commits since master"
```

### Task 12: `pane.sh` and `bot-squad pane`

**Files:**
- Create: `scripts/cli/pane.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# pane.sh — rename current tmux window so the SID picks up the feature handle.
# Usage:  bot-squad pane <feature>
set -euo pipefail
FEATURE="${1:?usage: pane <feature>}"
[ -n "${TMUX:-}" ] || { echo "pane: not in a tmux session" >&2; exit 2; }
tmux rename-window "$FEATURE"
echo "renamed window to '$FEATURE'; SID = S-$(id -un)-$FEATURE"
```

- [ ] **Step 2: Wire into `bot-squad` CLI**

In `scripts/cli/bot-squad`, add a `pane` subcommand that dispatches to this script.

- [ ] **Step 3: Commit**

```bash
git add scripts/cli/pane.sh scripts/cli/bot-squad
git commit -m "cli: bot-squad pane helper renames tmux window"
```

### Task 13: `user_prompt_submit.sh` and `stop.sh` hooks

**Files:**
- Create: `scripts/hooks/user_prompt_submit.sh`
- Create: `scripts/hooks/stop.sh`

- [ ] **Step 1: Write `user_prompt_submit.sh`**

```bash
#!/usr/bin/env bash
# user_prompt_submit.sh — touch .claude/last_user_prompt_ts so Stop hook can
# decide whether the session is idle.
set -uo pipefail
mkdir -p .claude
touch .claude/last_user_prompt_ts
exit 0
```

- [ ] **Step 2: Write `stop.sh`**

```bash
#!/usr/bin/env bash
# stop.sh — TG-ping if the session is idle waiting for user input.
set -uo pipefail

# Resolve our own SID for the prefix.
sid=$("$BOT_SQUAD/scripts/hooks/hook_my_sid.sh" 2>/dev/null) || true
[ -n "$sid" ] || exit 0

stamp=.claude/last_user_prompt_ts
[ -f "$stamp" ] || exit 0
mtime=$(stat -c %Y "$stamp" 2>/dev/null || echo 0)
now=$(date +%s)
age=$((now - mtime))
# Only ping if the user hasn't talked in >60s (i.e. session is idle awaiting input).
[ "$age" -gt 60 ] || exit 0

# Resolve project from CWD.
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

curl -sS --unix-socket "${WORKER_SOCK:-/home/www/bot-squad/data/_sock/worker.sock}" \
    -X POST -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"slug":sys.argv[1],"sid":sys.argv[2],"user":sys.argv[3],"message":"needs your input"}))' "$slug" "$sid" "$(id -un)")" \
    http://w/actions/tg_notify >/dev/null
exit 0
```

- [ ] **Step 3: Reuse `hook_my_sid.sh` from cctv**

Port `hook_my_sid.sh` from the archived `signal-tracker-old/` if there's an equivalent (in cctv there is; we don't have it yet in bot-squad). Drop into `scripts/hooks/hook_my_sid.sh`:

```bash
#!/usr/bin/env bash
set -uo pipefail
[ -n "${TMUX:-}" ] && command -v tmux >/dev/null || exit 1
user=$(id -un); user=${user//[^A-Za-z0-9_]/_}
target=""
[ -n "${TMUX_PANE:-}" ] && target="-t $TMUX_PANE"
window=$(tmux display-message -p $target -F '#W' 2>/dev/null) || exit 1
window=${window//[^A-Za-z0-9_-]/_}
[ -z "$window" ] && exit 1
echo "S-$user-$window"
```

`chmod 755`, owned `almdudleer:www`.

- [ ] **Step 4: Smoke test the hooks manually**

```bash
cd /home/almdudleer/signal_tracker_mgmt
mkdir -p .claude
/home/www/bot-squad/scripts/hooks/user_prompt_submit.sh
ls -la .claude/last_user_prompt_ts
# Force "stale": touch with old mtime
touch -d '5 minutes ago' .claude/last_user_prompt_ts
# Run stop.sh: should fire a TG ping
BOT_SQUAD=/home/www/bot-squad /home/www/bot-squad/scripts/hooks/stop.sh
# Check phone for the ping
```

- [ ] **Step 5: Commit**

```bash
git add scripts/hooks/user_prompt_submit.sh scripts/hooks/stop.sh scripts/hooks/hook_my_sid.sh
git commit -m "hooks: UserPromptSubmit timestamp + Stop idle TG-ping"
```

### Task 14: Update `.claude/settings.json` template in CLI

**Files:**
- Modify: `scripts/cli/bot-squad`

- [ ] **Step 1: Update the settings.json heredoc**

In `cmd_project_init`, replace the existing `.claude/settings.json` content with:

```json
{
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/session_start.sh"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/user_prompt_submit.sh"}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "/home/www/bot-squad/scripts/hooks/stop.sh"}]}]
  }
}
```

Make `bot-squad project init` overwrite `.claude/settings.json` if it exists AND its current content is a strict subset of the new content. Otherwise leave alone (don't clobber custom additions).

- [ ] **Step 2: Re-run for signal-tracker**

```bash
/home/www/bot-squad/scripts/cli/bot-squad project init signal-tracker --repo /home/almdudleer/signal_tracker_mgmt
cat /home/almdudleer/signal_tracker_mgmt/.claude/settings.json
# Expected: now has all three hooks.
git -C /home/almdudleer/signal_tracker_mgmt diff .claude/settings.json
git -C /home/almdudleer/signal_tracker_mgmt add .claude/settings.json
git -C /home/almdudleer/signal_tracker_mgmt commit -m "ops: add UserPromptSubmit + Stop hooks"
```

- [ ] **Step 3: Commit the CLI change**

```bash
git -C /home/www/bot-squad add scripts/cli/bot-squad
git -C /home/www/bot-squad commit -m "cli: project init drops UserPromptSubmit + Stop hook config"
```

---

## Phase 5 — agent_team/dev branch + signal-tracker recipes

### Task 15: Create the agent_team/dev branch in signal_tracker_mgmt

- [ ] **Step 1: Verify clean tree**

```bash
git -C /home/almdudleer/signal_tracker_mgmt status
```
Expected: clean (or only the .claude/settings.json change from task 14).

- [ ] **Step 2: Branch off master**

```bash
cd /home/almdudleer/signal_tracker_mgmt
git checkout -b agent_team/dev master
git push -u origin agent_team/dev
git checkout master
```

- [ ] **Step 3: Update `bot-squad project init` to handle this idempotently**

Add a `--create-deploy-branch` flag (defaults to true on first init for a project) that runs the equivalent commands. Skip if the branch already exists.

- [ ] **Step 4: Commit (in bot-squad)**

```bash
git -C /home/www/bot-squad add scripts/cli/bot-squad
git -C /home/www/bot-squad commit -m "cli: project init optionally creates agent_team/dev branch"
```

### Task 16: Write signal-tracker's deploy recipes

**Files:**
- Create: `/home/www/bot-squad/data/signal-tracker/deploy/staging.sh`
- Create: `/home/www/bot-squad/data/signal-tracker/deploy/dev.sh`

- [ ] **Step 1: Write `staging.sh`**

(See spec §4.4 for the full content.)

```bash
#!/usr/bin/env bash
# signal-tracker → staging
set -euo pipefail
REPO=/home/almdudleer/signal_tracker_mgmt
cd "$REPO"
ORIG=$(git rev-parse --abbrev-ref HEAD)
trap 'git checkout "$ORIG" >/dev/null 2>&1 || true' EXIT
git fetch origin
git checkout staging
if ! git merge agent_team/dev --no-edit --no-ff; then
    echo "merge conflict — aborting" >&2
    git merge --abort
    exit 2
fi
docker compose build signal-tracker-staging
docker compose up -d --force-recreate signal-tracker-staging
sleep 5
curl -fsS https://signal-staging.dev.uzinvestapi.com/api/version >/dev/null
```

`chmod 755`.

- [ ] **Step 2: Write `dev.sh`**

Mirror with `signal-tracker-dev` and the dev URL.

- [ ] **Step 3: Smoke-test (manual)**

Make a tiny commit on agent_team/dev, run `ops/bot-squad-bin/deploy staging "smoke"`, watch the worker log + TG ping. Since this is data dir not git-tracked, no commit needed.

### Task 17: Update AGENTS.md (rendered)

**Files:**
- Create: `scripts/cli/render_agents_md.py`
- Run: `bot-squad render-agents-md signal-tracker` to regenerate AGENTS.md with the real branching + deploy sections.

- [ ] **Step 1: Write `render_agents_md.py`**

A small Python script that takes a slug, reads:
- `data/<slug>/vision/north-star.md` (first paragraph)
- `data/<slug>/vision/strategy.md` (full)
- `data/<slug>/vision/tactical.md` (full)

…and writes a fresh AGENTS.md to `<repo_path>/AGENTS.md` matching spec §10's template (with placeholders filled in).

- [ ] **Step 2: Run for signal-tracker**

```bash
/home/www/bot-squad/scripts/cli/bot-squad render-agents-md signal-tracker
git -C /home/almdudleer/signal_tracker_mgmt diff AGENTS.md
git -C /home/almdudleer/signal_tracker_mgmt add AGENTS.md
git -C /home/almdudleer/signal_tracker_mgmt commit -m "ops: re-render AGENTS.md with real branching/deploy"
```

- [ ] **Step 3: Commit the script (in bot-squad)**

```bash
git -C /home/www/bot-squad add scripts/cli/render_agents_md.py scripts/cli/bot-squad
git -C /home/www/bot-squad commit -m "cli: render_agents_md helper"
```

---

## Phase 6 — End-to-end verification

### Task 18: Smoke-test the entire deploy flow

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
    -d '{"slug":"signal-tracker"}' http://w/actions/git_status
curl --unix-socket "$SOCK" -X POST -H 'Content-Type: application/json' \
    -d '{"chat_id":"404580642","message":"spec #3 smoke test"}' http://w/actions/tg_notify
```
Expected: 200s; final TG message arrives on phone.

- [ ] **Step 3: Make a real commit on agent_team/dev and request deploy**

```bash
cd /home/almdudleer/signal_tracker_mgmt
git checkout agent_team/dev
echo "<!-- spec #3 smoke -->" >> docs/superpowers/specs/2026-05-10-spec3-guided-team-workflow-design.md
git add -A
git commit -m "[docs] spec #3 smoke test"
ops/bot-squad-bin/deploy staging "spec #3 smoke test"
# Wait ~60s for the monitor to pick up; tail the run log:
ls /home/www/bot-squad/data/signal-tracker/_jobs/deploy/runs/
tail -f /home/www/bot-squad/data/signal-tracker/_jobs/deploy/runs/<latest-id>.log
```
Expected: TG-ping at queue time, recipe runs, TG-ping on success, https://signal-staging.dev.uzinvestapi.com reflects the new build.

- [ ] **Step 4: Test failure path**

Make a deliberately-bad recipe, request deploy, assert TG-ping reports failure.

- [ ] **Step 5: Test idle TG-ping (Stop hook)**

Open a fresh Claude session in `signal_tracker_mgmt`. Send a prompt. Wait 90s without responding. Verify TG-ping `[<sid>] needs your input` arrives.

- [ ] **Step 6: Verify cron is still untouched**

```bash
crontab -l | grep -E 'bot-squad' && echo "BUG: cron entry exists" || echo "OK: cron unchanged"
```

- [ ] **Step 7: Final commit (rollup of any fixups)**

```bash
git -C /home/www/bot-squad log --oneline | head -25
```

---

## Self-review

Before declaring spec #3 done:

- [ ] All worker tests pass: `cd /home/www/bot-squad/worker && .venv/bin/pytest -v`
- [ ] All API tests pass: `cd /home/www/bot-squad/api && .venv/bin/pytest -v`
- [ ] `systemctl --user is-active bot-squad-worker` is `active`
- [ ] `data/_sock/worker.sock` exists, mode 0660 group www
- [ ] `data/signal-tracker/_jobs/deploy/queue/` is empty (no stuck deploys)
- [ ] Live test: agent commit → squash → deploy staging → TG ping success → staging URL reflects change
- [ ] Live test: Stop hook fires after 60s of inactivity
- [ ] `crontab -l` shows ONLY pre-existing leadintel lines (no spec #3 additions)
- [ ] `~/.claude/settings.json` has `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (already set in spec #1)
- [ ] AGENTS.md has the real `## Branching & commits` and `## Deploy` sections
- [ ] `secrets.toml` exists, `auth.toml` no longer has `bot_token`, API container's mount excludes `secrets.toml`
- [ ] `agent_team/dev` branch exists in signal-tracker, pushed to origin
