"""T-0878 DoD 5 — a deploy REFUSAL has to reach somebody.

Measured, on this install's own job archive:

    processed/…ef2db586….fail.8   2026-08-06 08:25
    processed/…2086870d….fail.8   2026-08-11 14:22

Two refusals, five days apart, both correct (the install dir was dirty), both
producing a run log whose last lines named the exact files and the exact fix.
What ``deploy_monitor`` said about them was, in full::

    ❌ deploy bot-squad/staging FAILED rc=8

No reason, no remediation, and nothing at all to the session that had asked for
the deploy and was still holding ``{"ok": true, "queue_id": …}``. The install
stayed undeployable for five days and the underlying defect was found by
accident while unblocking an unrelated ticket.

T-0453 already established both halves of the answer — quote the failure, and
tell the requester — but wired them only to the wedged-clone path
(``RC_CLONE_WEDGED``). These tests pin them on the ordinary non-zero-rc path,
plus the one new distinction: a **pre-flight refusal** re-fires identically on
every retry, so it goes to the loud operator channel, while a build failure
that might pass next time does not.

Each guard is shown going green AND shown discriminating — a control for the
classification, a control for the requester filter, and a control proving the
quoted detail can never cost the alert itself.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bot_squad_worker import jobs
from bot_squad_worker.jobs import deploy_monitor
from bot_squad_worker.config import Config, Project


# ---------------------------------------------------------------------------
# Fixtures — real git repo, real recipe, real queue
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path, slug: str = "t0878-proj") -> Project:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t.com"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(args, cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return Project(
        slug=slug, display_name="T0878", repo_path=repo,
        deploy_branch="bot_squad/dev", master_branch="master",
        prod_url="", staging_url="", dev_url="",
        deploy_targets=("staging",), tg_chat="TEST_CHAT_ID",
    )


def _make_config(tmp_path: Path, project: Project) -> Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "projects.toml").write_text(
        f'[projects.{project.slug}]\n'
        f'slug = "{project.slug}"\n'
        f'display_name = "{project.display_name}"\n'
        f'repo_path = "{project.repo_path}"\n'
        f'deploy_branch = "{project.deploy_branch}"\n'
        f'master_branch = "{project.master_branch}"\n'
        'prod_url = ""\nstaging_url = ""\ndev_url = ""\n'
        'deploy_targets = ["staging"]\n'
        f'tg_chat = "{project.tg_chat}"\n'
    )
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    return Config.load(cfg_dir)


#: The literal tail of the 2026-08-11 run log, so what the alert has to carry is
#: the real thing rather than a paraphrase of it.
LIVE_FATAL_TAIL = """[bot-squad/staging] FATAL: install dir has uncommitted changes (excluding data/):
     M api/app/routes_conversations.py
     M config/projects.toml
    ?? worker/bot_squad_worker/agent_provider.py
[bot-squad/staging] These edits would be overwritten by this deploy. Move them to the dev clone or revert."""


def _make_recipe(cfg: Config, slug: str, target: str, rc: int,
                 body: str = "") -> Path:
    recipe_dir = cfg.data_dir / slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / f"{target}.sh"
    lines = ["#!/usr/bin/env bash", "set -uo pipefail"]
    for line in body.splitlines():
        lines.append("echo " + _sh_quote(line))
    lines.append(f"exit {rc}")
    recipe.write_text("\n".join(lines) + "\n")
    recipe.chmod(0o755)
    return recipe


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


class _FakeTgClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, chat_id: str, text: str, sid: str = "", user: str = "",
             urgent: bool = False, topic_id: int | None = None,
             debounce: bool = True, delivery: dict | None = None) -> bool:
        self.calls.append({"chat_id": chat_id, "text": text, "urgent": urgent})
        return True


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch):
    """Capture every outward channel deploy_monitor can use."""
    fake_tg = _FakeTgClient()
    from bot_squad_worker import actions as A
    monkeypatch.setattr(A, "_get_tg_client", lambda _cfg: fake_tg)

    operator_alerts: list[str] = []
    requester_notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jobs, "_alert_operators",
        lambda cfg, slug, project, text: operator_alerts.append(text),
    )
    _real_notify = jobs._notify_requester

    def _spy_notify(cfg, slug, requested_by, text):
        # Wrap the REAL function so its own "is this a session?" filter is the
        # thing under test, not a stand-in for it.
        before = len(requester_notices)
        monkeypatch_target.append((requested_by, text))
        _real_notify_capture(cfg, slug, requested_by, text)
        assert len(requester_notices) >= before

    monkeypatch_target: list[tuple[str, str]] = []

    def _real_notify_capture(cfg, slug, requested_by, text):
        from bot_squad_worker import intersession as _is
        monkeypatch.setattr(
            _is, "send_notice",
            lambda c, s, frm, to, t: requester_notices.append((to, t)),
        )
        _real_notify(cfg, slug, requested_by, text)

    monkeypatch.setattr(jobs, "_notify_requester", _spy_notify)
    return {
        "tg": fake_tg,
        "operator_alerts": operator_alerts,
        "requester_notices": requester_notices,
        "notify_calls": monkeypatch_target,
    }


def _run(cfg: Config, slug: str, *, requested_by: str) -> None:
    from bot_squad_worker import deploy as _deploy
    _deploy.enqueue(cfg, slug, "staging", "t0878", requested_by)
    deploy_monitor(cfg)


def _failure_text(spy) -> str:
    hits = [c["text"] for c in spy["tg"].calls if "❌" in c["text"]]
    assert hits, f"no failure message was sent at all: {spy['tg'].calls}"
    return hits[-1]


# ---------------------------------------------------------------------------
# The refusal path
# ---------------------------------------------------------------------------

def test_rc8_refusal_quotes_the_recipes_own_FATAL_block(tmp_path, spy) -> None:
    """Nobody should have to open a run log to learn why a deploy was refused."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=8, body=LIVE_FATAL_TAIL)

    _run(cfg, proj.slug, requested_by="S-almdudleer-some-dev-p149")

    text = _failure_text(spy)
    assert "rc=8" in text
    assert "FATAL: install dir has uncommitted changes" in text
    assert "M config/projects.toml" in text
    assert "Move them to the dev clone or revert" in text


def test_rc8_refusal_says_it_will_not_clear_on_its_own(tmp_path, spy) -> None:
    """The five-day gap was not "nobody saw a message" — it was nobody knowing
    the message meant *every* subsequent deploy would fail the same way."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=8, body=LIVE_FATAL_TAIL)

    _run(cfg, proj.slug, requested_by="S-almdudleer-some-dev-p149")

    text = _failure_text(spy)
    assert "REFUSED" in text
    assert "does NOT clear on its own" in text


def test_rc8_refusal_reaches_the_operators(tmp_path, spy) -> None:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=8, body=LIVE_FATAL_TAIL)

    _run(cfg, proj.slug, requested_by="S-almdudleer-some-dev-p149")

    assert len(spy["operator_alerts"]) == 1
    assert "rc=8" in spy["operator_alerts"][0]


def test_rc8_refusal_reaches_the_session_that_asked(tmp_path, spy) -> None:
    """The requester is holding an {"ok": true, queue_id} that nothing else will
    ever contradict — T-0453's point, never applied to this path."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=8, body=LIVE_FATAL_TAIL)

    _run(cfg, proj.slug, requested_by="S-almdudleer-some-dev-p149")

    assert [to for to, _ in spy["requester_notices"]] == \
        ["S-almdudleer-some-dev-p149"]
    assert "rc=8" in spy["requester_notices"][0][1]


# ---------------------------------------------------------------------------
# Controls — each guard has to DISCRIMINATE, not just fire
# ---------------------------------------------------------------------------

def test_CONTROL_a_build_failure_is_not_escalated_to_operators(tmp_path, spy) -> None:
    """rc=6 (image missing) can pass on a retry, so it is not a refusal. If this
    goes red the classification has become "everything is urgent", which is the
    same as "nothing is"."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=6, body="[x] FATAL: no image")

    _run(cfg, proj.slug, requested_by="S-almdudleer-some-dev-p149")

    text = _failure_text(spy)
    assert "rc=6" in text
    assert "REFUSED" not in text
    assert spy["operator_alerts"] == []
    # …but it still carries the reason and still reaches the requester: those
    # two are unconditional, and only the escalation is classified.
    assert "FATAL: no image" in text
    assert [to for to, _ in spy["requester_notices"]] == \
        ["S-almdudleer-some-dev-p149"]


def test_CONTROL_a_non_session_requester_gets_no_notice(tmp_path, spy) -> None:
    """A CLI/cron caller is not a pane to write into. The filter is the real
    ``_notify_requester``; this asserts it is still doing the filtering."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=8, body=LIVE_FATAL_TAIL)

    _run(cfg, proj.slug, requested_by="almdudleer")

    assert spy["requester_notices"] == []
    assert len(spy["operator_alerts"]) == 1  # the loud path is unaffected


def test_CONTROL_an_unreadable_log_still_produces_the_alert(
    tmp_path, spy, monkeypatch
) -> None:
    """Quoting the log is a convenience. If it ever becomes a precondition, a
    missing log turns a reported failure into an UNREPORTED one — and measured
    here, that failure would also be silent: ``deploy_monitor`` swallows
    per-project exceptions, so a raising extractor kills the whole alert and
    logs it where nobody is looking. Hence two assertions: the helper cannot
    raise, and an empty detail still ships a usable message.
    """
    # 1. The helper's contract on every bad input it can be handed.
    assert jobs._recipe_failure_detail(None) == ""
    assert jobs._recipe_failure_detail(tmp_path / "does-not-exist.log") == ""
    assert jobs._recipe_failure_detail(tmp_path) == ""   # a directory

    # 2. With no detail to quote, the alert is still an alert.
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _make_recipe(cfg, proj.slug, "staging", rc=8, body=LIVE_FATAL_TAIL)
    monkeypatch.setattr(jobs, "_recipe_failure_detail", lambda _p: "")

    _run(cfg, proj.slug, requested_by="S-almdudleer-x-p1")

    text = _failure_text(spy)
    assert "rc=8" in text
    assert "REFUSED" in text
    assert "Log: " in text          # the fallback: where to look by hand
    assert len(spy["operator_alerts"]) == 1
    assert [to for to, _ in spy["requester_notices"]] == ["S-almdudleer-x-p1"]


# ---------------------------------------------------------------------------
# The extractor itself
# ---------------------------------------------------------------------------

def test_detail_prefers_the_FATAL_block_over_the_tail(tmp_path) -> None:
    log = tmp_path / "run.log"
    log.write_text(
        "\n".join(["noise"] * 40) + "\n" + LIVE_FATAL_TAIL + "\n"
    )
    detail = jobs._recipe_failure_detail(log)
    assert detail.startswith("[bot-squad/staging] FATAL:")
    assert "noise" not in detail


def test_detail_falls_back_to_the_tail_when_there_is_no_FATAL(tmp_path) -> None:
    log = tmp_path / "run.log"
    log.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    detail = jobs._recipe_failure_detail(log)
    assert "line 49" in detail
    assert "line 0" not in detail


def test_detail_is_capped(tmp_path) -> None:
    log = tmp_path / "run.log"
    log.write_text("FATAL: " + ("x" * 50_000))
    detail = jobs._recipe_failure_detail(log)
    assert len(detail) < jobs._DETAIL_MAX_CHARS + 100
    assert "truncated" in detail


def test_refusal_codes_match_the_shipped_recipe(tmp_path) -> None:
    """The set is read off deploy-recipes/bot-squad/staging.sh. If the recipe
    grows a new pre-flight `exit N` and nobody adds it here, that refusal goes
    back to being a silent one — so assert every exit code the recipe uses
    before its first irreversible step is classified."""
    repo = Path(__file__).resolve().parents[2]
    recipe = repo / "deploy-recipes" / "bot-squad" / "staging.sh"
    if not recipe.exists():
        pytest.skip("recipe not present in this checkout")
    text = recipe.read_text()
    # Everything before `docker compose build` is pre-flight: nothing has
    # shipped yet, and the same state produces the same exit next tick.
    preflight = text.split("docker compose build")[0]
    codes = {
        int(m) for m in __import__("re").findall(r"^\s*exit (\d+)\s*$",
                                                 preflight, flags=8)
    }
    codes.discard(0)
    unclassified = codes - set(jobs.RECIPE_REFUSAL_RCS)
    assert not unclassified, (
        f"staging.sh refuses with exit {sorted(unclassified)} before shipping "
        "anything, but jobs.RECIPE_REFUSAL_RCS does not list it — that refusal "
        "would reach only the project log channel"
    )
