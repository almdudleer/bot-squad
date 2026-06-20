"""Per-project clone health read-model + pull-master (T-0296).

Uses REAL throwaway git repos (a shared bare origin + dev/prod clones) so the
ahead-behind / clean-dirty / ff-only logic is exercised against actual git, not
mocks — the same style as test_deploy.py.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from bot_squad_worker import clones
from bot_squad_worker.config import Config, Project


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _init_origin_and_clones(tmp: Path) -> tuple[Path, Path, Path]:
    """Bare origin on 'master' + a dev clone + a prod clone, all in sync."""
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "master", str(origin)], check=True)

    dev = tmp / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True)
    _run(dev, "config", "user.email", "t@e.com")
    _run(dev, "config", "user.name", "T")
    (dev / "README.md").write_text("hi\n")
    _run(dev, "add", "README.md")
    _run(dev, "commit", "-q", "-m", "init")
    _run(dev, "push", "-q", "origin", "master")

    prod = tmp / "prod"
    subprocess.run(["git", "clone", "-q", str(origin), str(prod)], check=True)
    _run(prod, "config", "user.email", "t@e.com")
    _run(prod, "config", "user.name", "T")
    return origin, dev, prod


def _make_cfg(tmp: Path, dev: Path, prod: Path | None, workspace: Path | None = None) -> Config:
    cfg_dir = tmp / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    lines = [
        "[projects.demo]",
        'slug = "demo"',
        'display_name = "Demo"',
        f'repo_path = "{dev}"',
    ]
    if prod is not None:
        lines.append(f'repo_master = "{prod}"')
    if workspace is not None:
        lines.append(f'repo_workspace = "{workspace}"')
    lines += [
        'deploy_branch = "master"',
        'master_branch = "master"',
        'prod_url = ""', 'staging_url = ""', 'dev_url = ""',
        'deploy_targets = ["prod"]',
        'tg_chat = "0"',
        "created_at = 2026-05-10",
    ]
    (cfg_dir / "projects.toml").write_text("\n".join(lines) + "\n")
    return Config.load(cfg_dir)


def test_clone_status_clean_in_sync(tmp_path: Path):
    _, dev, prod = _init_origin_and_clones(tmp_path)
    st = clones.clone_status(_make_cfg(tmp_path, dev, prod), "demo")
    assert st["dev"]["present"] is True
    assert st["dev"]["branch"] == "master"
    assert st["dev"]["clean"] is True
    assert st["dev"]["ahead"] == 0 and st["dev"]["behind"] == 0
    assert st["prod"]["present"] is True
    assert st["last_deploy"] is None


def test_clone_status_dev_ahead(tmp_path: Path):
    _, dev, prod = _init_origin_and_clones(tmp_path)
    (dev / "f.txt").write_text("x\n")
    _run(dev, "add", "f.txt")
    _run(dev, "commit", "-q", "-m", "local only")
    st = clones.clone_status(_make_cfg(tmp_path, dev, prod), "demo")
    assert st["dev"]["ahead"] == 1
    assert st["dev"]["behind"] == 0


def test_clone_status_dev_dirty(tmp_path: Path):
    _, dev, prod = _init_origin_and_clones(tmp_path)
    (dev / "uncommitted.txt").write_text("dirty\n")
    st = clones.clone_status(_make_cfg(tmp_path, dev, prod), "demo")
    assert st["dev"]["clean"] is False


def test_clone_status_prod_not_configured(tmp_path: Path):
    _, dev, _ = _init_origin_and_clones(tmp_path)
    st = clones.clone_status(_make_cfg(tmp_path, dev, None), "demo")
    assert st["prod"] == {"configured": False}


def test_clone_status_workspace_present(tmp_path: Path):
    _, dev, prod = _init_origin_and_clones(tmp_path)
    ws = tmp_path / "workspace"
    ws.mkdir()
    st = clones.clone_status(_make_cfg(tmp_path, dev, prod, workspace=ws), "demo")
    assert st["repo_workspace"] == str(ws)
    assert st["workspace_present"] is True


def test_clone_status_last_deploy(tmp_path: Path):
    _, dev, prod = _init_origin_and_clones(tmp_path)
    cfg = _make_cfg(tmp_path, dev, prod)
    runs = cfg.data_dir / "demo" / "_jobs" / "deploy" / "runs"
    runs.mkdir(parents=True)
    import json
    (runs / "1700000000000-q1.json").write_text(json.dumps({
        "target": "prod", "reason": "ship it", "requested_by": "alice",
        "ok": True, "returncode": 0,
    }))
    st = clones.clone_status(cfg, "demo")
    assert st["last_deploy"]["target"] == "prod"
    assert st["last_deploy"]["requested_by"] == "alice"
    assert st["last_deploy"]["ok"] is True


def test_pull_master_fast_forwards(tmp_path: Path):
    _, dev, prod = _init_origin_and_clones(tmp_path)
    # advance origin via dev so prod is now behind
    (dev / "new.txt").write_text("y\n")
    _run(dev, "add", "new.txt")
    _run(dev, "commit", "-q", "-m", "advance")
    _run(dev, "push", "-q", "origin", "master")
    cfg = _make_cfg(tmp_path, dev, prod)

    before = clones.clone_status(cfg, "demo")["prod"]
    assert before["behind"] == 1

    res = clones.pull_master(cfg, "demo")
    assert res["ok"] is True, res
    after = clones.clone_status(cfg, "demo")["prod"]
    assert after["behind"] == 0
    assert (prod / "new.txt").exists()


def test_pull_master_no_master_clone(tmp_path: Path):
    _, dev, _ = _init_origin_and_clones(tmp_path)
    res = clones.pull_master(_make_cfg(tmp_path, dev, None), "demo")
    assert res["ok"] is False
    assert "repo_master" in res["detail"]


def test_pull_master_diverged_fails_ff_only(tmp_path: Path):
    _, dev, prod = _init_origin_and_clones(tmp_path)
    # diverge prod: a local commit that origin doesn't have, AND advance origin
    (prod / "p.txt").write_text("prod-only\n")
    _run(prod, "add", "p.txt")
    _run(prod, "commit", "-q", "-m", "prod local")
    (dev / "o.txt").write_text("origin\n")
    _run(dev, "add", "o.txt")
    _run(dev, "commit", "-q", "-m", "origin advance")
    _run(dev, "push", "-q", "origin", "master")

    res = clones.pull_master(_make_cfg(tmp_path, dev, prod), "demo")
    assert res["ok"] is False
    assert "ff-only" in res["detail"] or "diverged" in res["detail"]
