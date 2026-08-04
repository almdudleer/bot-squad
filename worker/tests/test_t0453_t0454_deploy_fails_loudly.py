"""T-0453 + T-0454 — the two ways a watchrobot deploy did not happen and nothing said so.

Both incidents happened on 2026-08-04 within eight minutes of each other and are
one story about how this pipeline reports failure:

- T-0454: a required ``${VAR:?}`` in ``docker-compose.yml`` had no value in any
  protected ``.env``. The deploy died in 0.14s producing a 417-byte log, having
  consumed a queue slot. From outside it was a queue entry that "ran".
- T-0453: another unix user's untracked directory in the deploy clone made the
  force-checkout fail with ``Permission denied``. The worker logged it and left
  the queue file "for retry next tick" — once a minute, forever, with nothing in
  ``processing/``, no ``.rc``, and no notification. Indistinguishable from a slow
  build, which on that box is entirely plausible.

Every test here drives the real code against a real git repo and a real
filesystem; the wedge is reproduced with a real permission wall, not a mock, and
each guard is shown going RED before it is shown going green.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bot_squad_worker import deploy as d
from bot_squad_worker.config import Config, Project


# ---------------------------------------------------------------------------
# Fixtures — a dev clone, a bare origin, and a deploy clone, all real
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          capture_output=True, text=True)


def _make_project(tmp_path: Path) -> Project:
    repo = tmp_path / "devclone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "bot_squad/dev")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hi")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return Project(
        slug="test-deploy",
        display_name="Test Deploy",
        repo_path=repo,
        deploy_branch="bot_squad/dev",
        master_branch="master",
        prod_url="", staging_url="", dev_url="",
        deploy_targets=("staging",),
        tg_chat="0",
        repo_deploy=tmp_path / "deployclone",
    )


def _make_config(tmp_path: Path, project: Project) -> Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    lines = [
        f"[projects.{project.slug}]",
        f'slug = "{project.slug}"',
        f'display_name = "{project.display_name}"',
        f'repo_path = "{project.repo_path}"',
        f'repo_deploy = "{project.repo_deploy}"',
        f'deploy_branch = "{project.deploy_branch}"',
        f'master_branch = "{project.master_branch}"',
        'prod_url = ""', 'staging_url = ""', 'dev_url = ""',
        'deploy_targets = ["staging"]',
        'tg_chat = "0"',
        "created_at = 2026-05-10",
    ]
    (cfg_dir / "projects.toml").write_text("\n".join(lines) + "\n")
    (cfg_dir / "secrets.toml").write_text('[telegram]\nbot_token = ""\n')
    return Config.load(cfg_dir)


def _attach_origin(repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "bot_squad/dev")
    return bare


def _commit_push(repo: Path, relpath: str, body: str, message: str) -> str:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)
    _git(repo, "push", "-q", "origin", "bot_squad/dev")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_recipe(cfg: Config, slug: str, target: str, rc: int = 0) -> Path:
    recipe_dir = cfg.data_dir / slug / "deploy"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / f"{target}.sh"
    recipe.write_text(f'#!/usr/bin/env bash\necho "recipe ran"\nexit {rc}\n')
    recipe.chmod(0o755)
    return recipe


@pytest.fixture(autouse=True)
def _no_systemd_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SYSTEMD_SCOPE", "0")


# ===========================================================================
# T-0454 — the compose/env contract
# ===========================================================================

HARDENED_COMPOSE = """
services:
  app:
    environment:
      - DATABASE_URL=${DATABASE_URL:?set DATABASE_URL in the protected .env file}
      - OPTIONAL=${OPTIONAL:-fallback}
      - BARE=${BARE}
  app-staging:
    environment:
      - DATABASE_URL=${STAGING_DATABASE_URL:?set STAGING_DATABASE_URL in the protected .env file}
      - MAYBE_EMPTY=${MAYBE_EMPTY?must be defined, may be empty}
"""


def test_compose_required_vars_splits_the_two_required_forms() -> None:
    """`${VAR:?}` and `${VAR?}` are required; `${VAR}` and `${VAR:-x}` are not.

    The distinction is not pedantry — treating `${BARE}` as required would refuse
    deploys that work today, and a gate that false-refuses gets turned off.
    """
    nonempty, defined = d.compose_required_vars(HARDENED_COMPOSE)
    assert nonempty == {"DATABASE_URL", "STAGING_DATABASE_URL"}
    assert defined == {"MAYBE_EMPTY"}
    # The two non-required forms appear in NEITHER set.
    assert "OPTIONAL" not in nonempty | defined
    assert "BARE" not in nonempty | defined


def test_parse_env_file_forms(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        "export EXPORTED=other\n"
        'QUOTED="has spaces"\n'
        "EMPTY=\n"
        "not a key line\n"
        "URL=postgresql://u:p@h/db?x=1\n"
    )
    parsed = d.parse_env_file(env)
    assert parsed["PLAIN"] == "value"
    assert parsed["EXPORTED"] == "other"
    assert parsed["QUOTED"] == "has spaces"
    assert parsed["EMPTY"] == ""
    assert parsed["URL"] == "postgresql://u:p@h/db?x=1"
    assert "not a key line" not in parsed
    # An unreadable file provides NOTHING — never "all good".
    assert d.parse_env_file(tmp_path / "does-not-exist") == {}


def _setup_compose_project(tmp_path: Path) -> tuple[Project, Config]:
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _commit_push(proj.repo_path, "docker-compose.yml", HARDENED_COMPOSE, "harden compose")
    _make_recipe(cfg, proj.slug, "staging")
    # Provision the deploy clone so `.env` has somewhere to live.
    proj.repo_deploy.parent.mkdir(parents=True, exist_ok=True)
    d._ensure_deploy_clone(proj.repo_path, proj.repo_deploy, proj.deploy_branch)
    return proj, cfg


@pytest.fixture(autouse=True)
def _no_ambient_required_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate honours the worker's own environment (compose does too), so an
    ambient DATABASE_URL on the dev box would silently satisfy every arm and
    make the RED tests green for the wrong reason."""
    for name in ("DATABASE_URL", "STAGING_DATABASE_URL", "MAYBE_EMPTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BOT_SQUAD_DEPLOY_SKIP_ENV_CHECK", raising=False)


def test_gate_goes_red_when_the_env_supplies_nothing(tmp_path: Path) -> None:
    """THE incident: three required vars, no protected .env has any of them."""
    proj, cfg = _setup_compose_project(tmp_path)

    gaps = d.compose_env_gaps(cfg, proj.slug, "staging")

    assert gaps["missing"] == ["DATABASE_URL", "MAYBE_EMPTY", "STAGING_DATABASE_URL"]
    assert gaps["checked"] == 3
    # It names WHICH compose file it judged — provenance, not a bare verdict.
    assert "origin/bot_squad/dev:docker-compose.yml" in gaps["compose"]


def test_gate_goes_green_once_the_env_file_supplies_them(tmp_path: Path) -> None:
    """The control arm. Same code, same compose file — only the .env changed."""
    proj, cfg = _setup_compose_project(tmp_path)
    (proj.repo_deploy / ".env").write_text(
        "DATABASE_URL=postgresql://u:p@h/prod\n"
        "STAGING_DATABASE_URL=postgresql://u:p@h/dev\n"
        "MAYBE_EMPTY=\n"          # `${VAR?}` — defined-but-empty is enough
    )

    assert d.compose_env_gaps(cfg, proj.slug, "staging")["missing"] == []


def test_empty_value_does_not_satisfy_the_nonempty_form(tmp_path: Path) -> None:
    """`${VAR:?}` refuses an empty value, so the gate must too — otherwise it
    passes a file that compose will still reject, which is worse than no gate."""
    proj, cfg = _setup_compose_project(tmp_path)
    (proj.repo_deploy / ".env").write_text(
        "DATABASE_URL=\n"
        "STAGING_DATABASE_URL=postgresql://u:p@h/dev\n"
        "MAYBE_EMPTY=\n"
    )

    assert d.compose_env_gaps(cfg, proj.slug, "staging")["missing"] == ["DATABASE_URL"]


def test_gate_reads_the_compose_file_from_ORIGIN_not_the_stale_clone(tmp_path: Path) -> None:
    """The precise shape of the incident, and the reason the check is not a
    working-tree read.

    The deploy clone is force-synced to origin AT RUN TIME, so at enqueue time
    its on-disk compose file is the PREVIOUS release's. Here that stale copy has
    no required variables at all — a gate reading it would wave the deploy
    through into exactly the wall it hit.
    """
    proj, cfg = _setup_compose_project(tmp_path)
    (proj.repo_deploy / "docker-compose.yml").write_text(
        "services:\n  app:\n    environment:\n      - DATABASE_URL=postgresql://hardcoded\n"
    )

    gaps = d.compose_env_gaps(cfg, proj.slug, "staging")

    assert gaps["missing"] == ["DATABASE_URL", "MAYBE_EMPTY", "STAGING_DATABASE_URL"]
    assert "origin/" in gaps["compose"]


def test_enqueue_REFUSES_and_burns_no_queue_slot(tmp_path: Path) -> None:
    """Fail at request time, not at parse time — with the missing names."""
    proj, cfg = _setup_compose_project(tmp_path)

    with pytest.raises(d.ComposeEnvError) as exc:
        d.enqueue(cfg, proj.slug, "staging", "ship it", "S-requester")

    message = str(exc.value)
    for name in ("DATABASE_URL", "STAGING_DATABASE_URL", "MAYBE_EMPTY"):
        assert name in message
    # ComposeEnvError is a ValueError, so `_action_deploy`'s existing
    # `except ValueError` turns it into an ActionError for the caller.
    assert isinstance(exc.value, ValueError)
    # Nothing was queued: the whole point is not to consume a slot for a job
    # that cannot start.
    assert d.list_queued(cfg, proj.slug) == []


def test_enqueue_proceeds_once_the_env_is_complete(tmp_path: Path) -> None:
    proj, cfg = _setup_compose_project(tmp_path)
    (proj.repo_deploy / ".env").write_text(
        "DATABASE_URL=postgresql://u:p@h/prod\n"
        "STAGING_DATABASE_URL=postgresql://u:p@h/dev\n"
        "MAYBE_EMPTY=\n"
    )

    queue_id = d.enqueue(cfg, proj.slug, "staging", "ship it", "S-requester")

    assert queue_id
    assert len(d.list_queued(cfg, proj.slug)) == 1


def test_worker_environment_satisfies_a_required_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No false refusals: compose consults the shell env, and the recipe
    subprocess inherits the worker's, so the gate must accept it there too."""
    proj, cfg = _setup_compose_project(tmp_path)
    (proj.repo_deploy / ".env").write_text("DATABASE_URL=postgresql://u:p@h/prod\n")
    monkeypatch.setenv("STAGING_DATABASE_URL", "postgresql://u:p@h/dev")
    monkeypatch.setenv("MAYBE_EMPTY", "")

    assert d.compose_env_gaps(cfg, proj.slug, "staging")["missing"] == []


def test_gate_stays_silent_when_there_is_no_compose_file(tmp_path: Path) -> None:
    """Fail OPEN when the check cannot do its job. Most projects have no compose
    file at all; a gate that blocks when confused is worse than the hole."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(cfg, proj.slug, "staging")

    assert d.compose_env_gaps(cfg, proj.slug, "staging")["missing"] == []
    assert d.enqueue(cfg, proj.slug, "staging", "no compose here", "S-x")


def test_kill_switch_reproduces_the_OLD_behaviour_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escape hatch, and the negative control for the gate above.

    With the check off — which is what the code did before T-0454 — the very same
    unsatisfiable deploy is accepted and consumes a queue slot. That is the
    defect, reproduced on demand: it proves the refusal above comes from the gate
    and not from something incidental in the fixture.
    """
    proj, cfg = _setup_compose_project(tmp_path)
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_SKIP_ENV_CHECK", "1")

    assert d.enqueue(cfg, proj.slug, "staging", "operator override", "S-x")
    assert len(d.list_queued(cfg, proj.slug)) == 1, "the old behaviour burns a queue slot"


# ===========================================================================
# T-0453 — the wedged deploy clone
# ===========================================================================


@pytest.fixture
def _restore_modes():
    """chmod any locked directory back before tmp_path teardown, which would
    otherwise fail to remove it — the wedge is real enough to bite the test."""
    locked: list[Path] = []
    yield locked
    for p in locked:
        try:
            p.chmod(0o755)
        except OSError:
            pass


def _wedge_the_clone(proj: Project, tmp_path: Path, _restore_modes: list) -> str:
    """Reproduce the incident: a directory the checkout MUST modify, which this
    process cannot write into.

    The real cause was a directory owned by another unix user (``aqice``), which
    a test cannot create without another uid — but the mechanism is identical
    and the ticket names this substitution: removing or replacing an entry needs
    write permission on the CONTAINING directory, and this process does not have
    it. The file contents in the incident were byte-identical to the committed
    blobs; permission on the directory alone was enough.
    """
    _commit_push(proj.repo_path, "certs/tls.pem", "v1", "add certs")
    d._ensure_deploy_clone(proj.repo_path, proj.repo_deploy, proj.deploy_branch)
    assert (proj.repo_deploy / "certs" / "tls.pem").read_text() == "v1"
    # Now origin moves on and the checkout must REWRITE that file.
    sha = _commit_push(proj.repo_path, "certs/tls.pem", "v2", "rotate certs")
    locked = proj.repo_deploy / "certs"
    locked.chmod(0o555)
    _restore_modes.append(locked)
    return sha


def test_permission_wall_is_classified_PERMANENT(tmp_path: Path, _restore_modes) -> None:
    """First, prove the wedge reproduces at all — a real git checkout failing on
    a real permission, not a stubbed stderr."""
    proj = _make_project(tmp_path)
    _attach_origin(proj.repo_path, tmp_path)
    _wedge_the_clone(proj, tmp_path, _restore_modes)

    sync = d._ensure_deploy_clone(proj.repo_path, proj.repo_deploy, proj.deploy_branch)

    assert sync.ok is False
    assert bool(sync) is False
    assert sync.stage == "checkout"
    assert "permission denied" in sync.stderr.lower()
    assert sync.permanent is True


def test_transient_failure_is_NOT_classified_permanent(tmp_path: Path) -> None:
    """A network/fetch fault keeps the bounded retry — it is not the same class
    as a permission this process can never acquire."""
    proj = _make_project(tmp_path)
    _attach_origin(proj.repo_path, tmp_path)
    d._ensure_deploy_clone(proj.repo_path, proj.repo_deploy, proj.deploy_branch)
    # Make origin unreachable the way an outage would.
    _git(proj.repo_deploy, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    sync = d._ensure_deploy_clone(proj.repo_path, proj.repo_deploy, proj.deploy_branch)

    assert sync.ok is False
    assert sync.stage == "fetch"
    assert sync.permanent is False


def test_wedged_deploy_FAILS_the_job_instead_of_retrying_forever(
    tmp_path: Path, _restore_modes
) -> None:
    """The defect, end to end: a wedged clone used to leave the queue file for a
    retry that could never succeed. It now terminates on the FIRST permanent
    failure, lands in processed/.fail, and carries the git stderr."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(cfg, proj.slug, "staging")
    _wedge_the_clone(proj, tmp_path, _restore_modes)

    queue_id = d.enqueue(cfg, proj.slug, "staging", "ship it", "S-requester")
    result = d.run_next(cfg, proj.slug)

    assert result is not None, "a wedged deploy must not report 'nothing happened'"
    assert result.ok is False
    assert result.returncode == d.RC_CLONE_WEDGED
    assert result.queue_id == queue_id
    assert result.requested_by == "S-requester"
    # The queue is UNBLOCKED and the outcome is on disk where .rc/.ok readers look.
    assert d.list_queued(cfg, proj.slug) == []
    failed = list(d._processed_dir(cfg, proj.slug).glob(f"*.fail.{d.RC_CLONE_WEDGED}"))
    assert len(failed) == 1
    assert list(d._processing_dir(cfg, proj.slug).glob("*.json")) == []
    # The underlying git stderr is attached — to the result AND to the run log,
    # so a reader who only has the log still learns why.
    assert "permission denied" in result.failure_detail.lower()
    assert "permission denied" in result.log_path.read_text().lower()
    # And it names the DIRECTORY that is the actual wall — git blamed the file
    # ('unable to unlink old certs/tls.pem'), but the permission that matters
    # belongs to its parent, and a reader sent to the file chmods the wrong thing.
    assert result.failure_detail.count("/certs\n") or "/certs " in result.failure_detail
    assert str(proj.repo_deploy / "certs") in result.failure_detail
    # The remediation that actually works: rename, not delete — deleting an entry
    # needs write on the containing directory too.
    assert f"mv {proj.repo_deploy}/certs" in result.failure_detail


def test_NEGATIVE_CONTROL_the_old_unbounded_retry_reproduces(
    tmp_path: Path, _restore_modes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turn the two new behaviours off and the reported defect comes straight back.

    A guard that has never been watched fail is not evidence. Here permanence
    classification is disabled and the retry budget is set absurdly high — i.e.
    the pre-T-0453 code — against the SAME wedge the test above terminates. The
    deploy goes back to being invisible: ``run_next`` returns None, the queue file
    stays, ``processing/`` is empty, no rc is recorded, forever.
    """
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(cfg, proj.slug, "staging")
    _wedge_the_clone(proj, tmp_path, _restore_modes)
    monkeypatch.setattr(d, "_classify_clone_error", lambda stderr: False)
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_CLONE_MAX_RETRIES", "10000")

    d.enqueue(cfg, proj.slug, "staging", "ship it", "S-requester")

    for _ in range(7):  # the seven minutes the operator spent watching nothing
        assert d.run_next(cfg, proj.slug) is None
    assert len(d.list_queued(cfg, proj.slug)) == 1
    assert list(d._processing_dir(cfg, proj.slug).glob("*.json")) == []
    assert list(d._processed_dir(cfg, proj.slug).glob("*")) == []


def test_transient_failures_are_BOUNDED_not_unlimited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient fault still retries — but a fixed number of times, after which
    it fails loudly rather than looping once a minute forever."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(cfg, proj.slug, "staging")
    d._ensure_deploy_clone(proj.repo_path, proj.repo_deploy, proj.deploy_branch)
    _git(proj.repo_deploy, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    monkeypatch.setenv("BOT_SQUAD_DEPLOY_CLONE_MAX_RETRIES", "3")

    d.enqueue(cfg, proj.slug, "staging", "ship it", "S-requester")

    # Attempts 1 and 2: deferred, queue file preserved (the old behaviour, and
    # still the right answer for a fault that may clear).
    for attempt in (1, 2):
        assert d.run_next(cfg, proj.slug) is None, f"attempt {attempt} should defer"
        assert len(d.list_queued(cfg, proj.slug)) == 1
        assert d.clone_failure_state(cfg, proj.slug)["count"] == attempt

    # Attempt 3 exhausts the budget and fails the job.
    result = d.run_next(cfg, proj.slug)
    assert result is not None and result.returncode == d.RC_CLONE_WEDGED
    assert d.list_queued(cfg, proj.slug) == []
    assert "3 attempt(s)" in result.failure_detail


def test_every_job_blocked_by_the_same_clone_fails_together(
    tmp_path: Path, _restore_modes
) -> None:
    """HOL-blocking was one of the three harms. All the jobs behind the wall are
    failed in one sweep, so the queue empties and one alert covers them."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(cfg, proj.slug, "staging")
    _wedge_the_clone(proj, tmp_path, _restore_modes)

    for i in range(3):
        d.enqueue(cfg, proj.slug, "staging", f"deploy {i}", f"S-requester-{i}")

    result = d.run_next(cfg, proj.slug)

    assert result is not None and result.returncode == d.RC_CLONE_WEDGED
    assert result.collapsed_count == 3
    assert d.list_queued(cfg, proj.slug) == []
    assert len(list(d._processed_dir(cfg, proj.slug).glob(f"*.fail.{d.RC_CLONE_WEDGED}"))) == 3


def test_failure_counter_resets_after_a_good_sync(tmp_path: Path) -> None:
    """A transient blip must not accumulate across unrelated deploys and fail a
    healthy one on its first attempt."""
    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(cfg, proj.slug, "staging")
    good_url = _git(proj.repo_path, "remote", "get-url", "origin").stdout.strip()
    d._ensure_deploy_clone(proj.repo_path, proj.repo_deploy, proj.deploy_branch)
    _git(proj.repo_deploy, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    d.enqueue(cfg, proj.slug, "staging", "will blip", "S-x")
    assert d.run_next(cfg, proj.slug) is None
    assert d.clone_failure_state(cfg, proj.slug)["count"] == 1

    _git(proj.repo_deploy, "remote", "set-url", "origin", good_url)
    result = d.run_next(cfg, proj.slug)

    assert result is not None and result.ok is True
    assert d.clone_failure_state(cfg, proj.slug) == {}


def test_foreign_owned_paths_are_named(tmp_path: Path) -> None:
    """The diagnostic that turns 'Permission denied' into an actionable list.

    Ownership cannot be faked without a second uid, so this asserts the scanner's
    contract on the tree it CAN build: everything here belongs to this process,
    so nothing is reported. The permission-wedge test above proves the pathway
    the scanner feeds is exercised for real.
    """
    repo = tmp_path / "clone"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "f.txt").write_text("x")

    assert d._foreign_owned_paths(repo) == ()


# ===========================================================================
# T-0453 — it has to reach a human
# ===========================================================================


def test_wedge_alerts_the_operator_AND_the_requester(
    tmp_path: Path, _restore_modes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect is the silence. The recipe never ran, so none of the existing
    surfaces (.ok/.rc files, run log tail, /api/health) had anything to say —
    this asserts the monitor now uses the loud path a failed deploy already uses,
    and tells the session that asked."""
    from bot_squad_worker import jobs

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)
    _attach_origin(proj.repo_path, tmp_path)
    _make_recipe(cfg, proj.slug, "staging")
    _wedge_the_clone(proj, tmp_path, _restore_modes)
    d.enqueue(cfg, proj.slug, "staging", "ship it", "S-requester")

    operator_alerts: list[str] = []
    requester_notices: list[tuple[str, str]] = []
    channel_sends: list[str] = []
    monkeypatch.setattr(jobs, "_alert_operators",
                        lambda cfg, slug, project, text: operator_alerts.append(text))
    monkeypatch.setattr(jobs, "_notify_requester",
                        lambda cfg, slug, sid, text: requester_notices.append((sid, text)))

    class _Chan:
        def send(self, text, **kw):
            channel_sends.append(text)

    monkeypatch.setattr("bot_squad_worker.channels.get_channel", lambda cfg, project: _Chan())

    jobs._run_project_deploy(cfg, proj.slug, cfg.projects[proj.slug])

    assert len(operator_alerts) == 1, "a wedged deploy must reach the operator"
    assert "WEDGED" in operator_alerts[0]
    assert "permission denied" in operator_alerts[0].lower()
    assert requester_notices == [("S-requester", operator_alerts[0])], \
        "the requester holds an {ok: true, queue_id} that nothing else contradicts"
    # And it is NOT reported as a plain green deploy.
    assert not any("✅" in t for t in channel_sends)


def test_requester_notice_skips_non_session_callers(tmp_path: Path) -> None:
    """CLI/cron requesters have no pane; a send attempt would only log noise.
    Must never raise — a dead pane turning a reported failure into an unreported
    one is the exact bug class this ticket is about."""
    from bot_squad_worker import jobs

    proj = _make_project(tmp_path)
    cfg = _make_config(tmp_path, proj)

    jobs._notify_requester(cfg, proj.slug, "cli", "text")
    jobs._notify_requester(cfg, proj.slug, "", "text")
    jobs._notify_requester(cfg, proj.slug, "S-nonexistent-session", "text")
