"""Tests for render_agents_md.py (T-0195 test-commands block + T-0199 rewrite).

T-0195: the template hardcoded watchrobot's `docker exec signal-tracker python
test_api.py`, so EVERY project's generated AGENTS.md advertised watchrobot's
container. The '## Test commands' block is now built from optional per-project
`test_*_cmd` fields in projects.toml.

T-0199: render() now reads the CURRENT vision schema (product.md +
active_initiatives + initiatives/<name>.md — the old north-star/strategy/
tactical trio is gone from live data), and the REST of the template identity
(ops path prefix, stack, stakeholders, Telegram bot, extra hard rules) is
config-driven the same way: optional [projects.<slug>] fields, neutral
placeholders when absent, never another project's identity.

Tests render against fixture vision files in a tmp data dir — exercising the
real `render()` end-to-end — and, for the two real projects, against the ACTUAL
config/projects.toml shipped in this repo, so a registry regression is caught.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent / "render_agents_md.py"
_spec = importlib.util.spec_from_file_location("render_agents_md", _MOD_PATH)
ram = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ram)

# The real registry shipped in this repo — we assert against the ACTUAL
# watchrobot / bot-squad configs, so a regression in projects.toml is caught.
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


# ---------------------------------------------------------------------------
# Fixture vision (CURRENT schema — T-0199)
# ---------------------------------------------------------------------------

def _seed_vision(data_dir: Path, slug: str, *, active: bool = True) -> None:
    vis = data_dir / slug / "vision"
    (vis / "initiatives").mkdir(parents=True, exist_ok=True)
    (vis / "product.md").write_text(
        "# Prod\n\nThe core product statement.\n\n## Distribution\n\nNot for AGENTS.md.\n"
    )
    (vis / "initiatives" / "alpha.md").write_text("# Alpha — first bet\n\nbody\n")
    (vis / "initiatives" / "untitled.md").write_text("no h1 here\n")
    if active:
        (vis / "active_initiatives").write_text("alpha.md\nuntitled.md\nmissing.md\n")


def _synthetic_config(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "projects.toml").write_text(
        '[projects.synthetic]\nslug = "synthetic"\nrepo_path = "/tmp/syn"\n' + extra
    )
    return cfg


# ---------------------------------------------------------------------------
# render_test_commands() — the pure builder (T-0195, unchanged behavior)
# ---------------------------------------------------------------------------

def test_fieldless_project_graceful():
    """A project with NO test_*_cmd fields: no KeyError, neutral placeholder,
    and definitely no stale/leaked command."""
    block = ram.render_test_commands({"slug": "blank"})
    assert "signal-tracker" not in block
    assert "No project-specific test commands" in block
    # Placeholder is a single neutral bullet, not a fabricated command line.
    assert block.count("\n") == 0


def test_only_present_fields_render():
    """Only defined fields produce lines, in the spec's fixed order."""
    block = ram.render_test_commands(
        {"test_lint_cmd": "`npm run lint`", "test_backend_cmd": "`pytest`"}
    )
    assert block == "- Backend: `pytest`\n- Lint: `npm run lint`"


def test_empty_string_field_is_skipped():
    """An empty-string field is treated as absent (falsy), not an empty line."""
    block = ram.render_test_commands({"test_backend_cmd": "", "test_build_cmd": "`make`"})
    assert block == "- Build: `make`"


def test_brace_in_command_survives_format(tmp_path):
    """A command containing literal {braces} must pass through str.format
    untouched (T-0195 DoD: mind brace-escaping). End-to-end via render()."""
    cfg = _synthetic_config(
        tmp_path, 'test_backend_cmd = "`grep -o \'{.*}\' x.json`"\n'
    )
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "grep -o '{.*}' x.json" in out


# ---------------------------------------------------------------------------
# Current vision schema (T-0199)
# ---------------------------------------------------------------------------

def test_renders_current_vision_schema(tmp_path):
    """render() reads product.md + active_initiatives — end-to-end, no
    north-star/strategy/tactical files anywhere (they no longer exist)."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "The core product statement." in out
    # product.md subsections after the first H2 stay out of the always-cached file
    assert "Not for AGENTS.md" not in out
    # initiative with H1 → title; without H1 / missing file → stem fallback
    assert "- **Alpha — first bet** — `ops/vision/initiatives/alpha.md`" in out
    assert "- **untitled** — `ops/vision/initiatives/untitled.md`" in out
    assert "- **missing** — `ops/vision/initiatives/missing.md`" in out


def test_feedback_blurb_is_project_agnostic(tmp_path):
    """T-0302: every rendered AGENTS.md advertises the feedback channel —
    `bsq feedback submit` — so every session knows it can/should surface
    product/process friction upstream. It's project-agnostic (no config
    field), so it renders for any project, with the project's own ops prefix."""
    cfg = _synthetic_config(tmp_path, 'ops_path = "ops/custom"\n')
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "## Feedback is welcome and expected" in out
    assert 'bsq feedback submit "<note>"' in out
    # ops prefix flows through the blurb's feedback path too.
    assert "`ops/custom/feedback/`" in out


def test_missing_vision_entirely_graceful(tmp_path):
    """A project with NO vision dir (and no backlog dir) at all: placeholders,
    no exception."""
    cfg = _synthetic_config(tmp_path)
    out = ram.render("synthetic", cfg, tmp_path)
    assert "No `vision/product.md` yet" in out
    assert "No active initiatives" in out


# ---------------------------------------------------------------------------
# kind:initiative backlog tasks (T-0480/T-0562) — the PRIMARY source now
# ---------------------------------------------------------------------------

def _write_initiative_task(backlog: Path, task_id: str, title: str, status: str) -> None:
    backlog.mkdir(parents=True, exist_ok=True)
    (backlog / f"{task_id}-{title}.md").write_text(
        f'---\nid: {task_id}\ntitle: "{title}"\nstatus: {status}\n'
        f"kind: initiative\npriority: 0\n---\n\n# {title}\n"
    )


def test_initiative_tasks_are_primary_source(tmp_path):
    """An open `kind: initiative` backlog task renders as an active initiative,
    pointing at the backlog file — no vision/initiatives/ dir needed at all."""
    cfg = _synthetic_config(tmp_path)
    (tmp_path / "synthetic" / "vision").mkdir(parents=True)
    (tmp_path / "synthetic" / "vision" / "product.md").write_text("# Prod\n\nStatement.\n")
    backlog = tmp_path / "synthetic" / "backlog"
    _write_initiative_task(backlog, "T-0001", "persistent-initiatives", "open")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "- **persistent-initiatives** — `ops/backlog/T-0001-persistent-initiatives.md`" in out


def test_closed_initiative_task_excluded(tmp_path):
    """A `closed` kind:initiative task is done — it must not clutter the
    active-initiatives block (only `closed` is terminal, per T-0480)."""
    cfg = _synthetic_config(tmp_path)
    (tmp_path / "synthetic" / "vision").mkdir(parents=True)
    backlog = tmp_path / "synthetic" / "backlog"
    _write_initiative_task(backlog, "T-0001", "done-thing", "closed")
    _write_initiative_task(backlog, "T-0002", "open-thing", "in_progress")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "done-thing" not in out
    assert "- **open-thing** — `ops/backlog/T-0002-open-thing.md`" in out


def test_initiative_tasks_and_legacy_scheme_combine(tmp_path):
    """A project mid-migration: kind:initiative tasks AND the legacy
    vision/initiatives/ scheme both render (union), so nothing is dropped
    while a project transitions to T-0480."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    backlog = tmp_path / "synthetic" / "backlog"
    _write_initiative_task(backlog, "T-0001", "new-model-bet", "open")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "- **new-model-bet** — `ops/backlog/T-0001-new-model-bet.md`" in out
    assert "- **Alpha — first bet** — `ops/vision/initiatives/alpha.md`" in out


def test_default_ops_prefix(tmp_path):
    """Without ops_path the template uses the scaffold convention: plain `ops`."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "`ops/vision/product.md`" in out
    assert "ops/bot-squad/" not in out


def test_optional_identity_fields_render(tmp_path):
    """ops_path / stack / stakeholders / tg_bot / extra_hard_rules all flow
    from config into the rendered file."""
    cfg = _synthetic_config(
        tmp_path,
        'ops_path = "ops/custom"\n'
        'stack = "Rust + htmx."\n'
        'stakeholders = "Jane (@jane)."\n'
        'tg_bot = "@synbot"\n'
        'extra_hard_rules = ["Never touch `legacy/`."]\n',
    )
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "`ops/custom/vision/constitution.md`" in out
    assert "- Stack: Rust + htmx." in out
    assert "- Stakeholders: Jane (@jane)." in out
    assert "Bot `@synbot`" in out
    assert "- Never touch `legacy/`." in out


def test_fieldless_project_omits_identity_lines(tmp_path):
    """Absent optional fields → omitted/neutral lines, never another
    project's identity (no @watchbot, no watchrobot stakeholders, no
    signal-tracker-old rule, no Tailwind stack)."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    for leaked in ("@watchbot", "signal-tracker", "Tailwind", "@alexeysdk",
                   "@timpo", "- Stack:", "- Stakeholders:"):
        assert leaked not in out, leaked


def test_absolute_ops_path_no_arrow(tmp_path):
    """When ops_path IS the absolute data dir (bot-squad's case: clone has no
    data symlink), the paths line renders once — no `X → X` arrow noise."""
    _seed_vision(tmp_path, "synthetic")
    data_path = str(tmp_path / "synthetic")
    cfg = _synthetic_config(tmp_path, f'ops_path = "{data_path}"\n')
    out = ram.render("synthetic", cfg, tmp_path)
    assert f"- Vision / backlog / feedback: `{data_path}/`." in out
    assert f"`{data_path}/` → `{data_path}/`" not in out


# ---------------------------------------------------------------------------
# Deploy block: the clean-tree gate is PER TARGET (T-0722, from T-0721)
# ---------------------------------------------------------------------------

# The EXACT two lines the template shipped until T-0722. They are false for any
# project with a deploy clone (`is_clean_for_target` returns True
# unconditionally when `uses_deploy_clone(target)`), and this CLI overwrites
# <repo>/AGENTS.md — so a single manual render used to put the claim back
# everywhere, looking like "the template restored the canon". These are kept
# VERBATIM so the test reds on the REVERT, not merely on a wording change.
_REVERTED_CLAIM_LINES = (
    "- Queues a deploy. The monitor processes it within ~60s when the tree is clean.",
    "- Tree clean = `git status --porcelain` empty (logs/cache excluded).",
)
# Substrings of it that would survive a re-wrap of those lines.
_REVERTED_CLAIM_PHRASES = ("when the tree is clean", "Tree clean =")


def _deploy_block(rendered: str) -> str:
    """Slice the '## Deploy' section out of a rendered AGENTS.md."""
    start = rendered.index("## Deploy")
    end = rendered.index("Hold deploys without killing the worker", start)
    return rendered[start:end]


def _flat(text: str) -> str:
    """Whitespace-normalised, so an assertion survives re-wrapping."""
    return " ".join(text.split())


def test_deploy_block_never_reverts_to_the_blanket_clean_tree_claim(tmp_path):
    """T-0722: the template must not print an unconditional 'processed when the
    tree is clean' / 'Tree clean = ...' pair again — for either kind of
    project. Asserted on the fieldless synthetic project AND on watchrobot's
    real config (the render that would overwrite the file T-0721 fixed)."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    _seed_vision(tmp_path, "watchrobot")
    renders = {
        "synthetic": ram.render("synthetic", cfg, tmp_path),
        "watchrobot": ram.render("watchrobot", _CONFIG_DIR, tmp_path),
    }
    for slug, out in renders.items():
        block = _deploy_block(out)
        for line in _REVERTED_CLAIM_LINES:
            assert line not in block, f"{slug}: reverted line {line!r}"
        for phrase in _REVERTED_CLAIM_PHRASES:
            assert phrase not in _flat(block), f"{slug}: reverted phrase {phrase!r}"


def test_deploy_block_splits_the_gate_by_target(tmp_path):
    """DoD 1: the gate is stated PER TARGET, naming the code that decides it —
    so a reader can check their own project instead of trusting a blanket
    sentence."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    block = _flat(_deploy_block(ram.render("synthetic", cfg, tmp_path)))
    assert "clean-tree gate is PER TARGET" in block
    assert "is_clean_for_target" in block
    assert "uses_deploy_clone(target)" in block
    # both arms present: gate OFF behind a deploy clone, LIVE for in-place
    assert "returns `True` UNCONDITIONALLY" in block
    assert "IN PLACE is the one the gate is live for" in block
    assert "git merge --ff-only" in block
    # and the constraint that actually applies behind a deploy clone
    assert "PUBLICATION, not cleanliness" in block


def test_deploy_block_is_true_for_a_project_without_a_deploy_clone(tmp_path):
    """DoD 1: no watchrobot specifics. The synthetic project has neither
    `repo_deploy` nor `repo_master`, so its gate is live on EVERY target — the
    rendered text must say so and must not describe another project's clones,
    targets or recipe log lines."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    block = _deploy_block(ram.render("synthetic", cfg, tmp_path))
    flat = _flat(block)
    assert "neither `repo_deploy` nor `repo_master`" in flat
    assert "deploys in place on EVERY target" in flat
    for leaked in ("watchrobot", "signal-tracker", "building in place",
                   "VCS_REF", "~/watchrobot"):
        assert leaked not in block, leaked
    # `staging`/`prod` may appear only as this project's configured targets,
    # never as the subject of a gate claim.
    assert "for `staging` it does not exist" not in flat
    assert "`prod` is the target the gate is live for" not in flat


# ---------------------------------------------------------------------------
# Deploy shim + Telegram token source (T-0726)
# ---------------------------------------------------------------------------

# The exact strings the template shipped until T-0726. `ops/…-bin/` is planted
# by nothing — `project_scaffold._link_ops` creates the `ops` data symlink and
# stops — so a fresh project got three commands it does not have, and
# watchrobot got them in two of its three clones. Kept VERBATIM so a revert
# reds here rather than shipping quietly.
_REVERTED_BIN_PATHS = (
    "ops/bot-squad-bin/deploy",
    "ops/bot-squad-bin/pause-deploys",
    "ops/bot-squad-bin/resume-deploys",
)


def test_deploy_commands_name_the_install_not_an_in_clone_symlink(tmp_path):
    """T-0726 DoD 1: the three deploy commands resolve from every clone."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    for reverted in _REVERTED_BIN_PATHS:
        assert reverted not in out, reverted
    assert "`$BOT_SQUAD/scripts/cli/deploy.sh <target>" in out
    assert "$BOT_SQUAD/scripts/cli/pause-deploys.sh" in out
    assert "$BOT_SQUAD/scripts/cli/resume-deploys.sh" in out
    # $BOT_SQUAD is expanded where it is first used, not only 60 lines later.
    assert _flat(out).index("the bot-squad install root, default") < \
        _flat(out).index("`$BOT_SQUAD/api/app/resources/roles/`")


def test_deploy_block_states_the_cwd_constraint(tmp_path):
    """Measured 2026-08-18: `deploy.sh` resolves the slug from `$PWD` against
    `repo_path` and ONLY that field, so the command cannot work from a master
    or deploy clone even where the path resolves. A path that resolves in front
    of a command that always refuses is worse than a missing path, so the
    rendered file says which clone to run it from — and names THIS project's."""
    cfg = _synthetic_config(tmp_path)
    _seed_vision(tmp_path, "synthetic")
    block = _flat(_deploy_block(ram.render("synthetic", cfg, tmp_path)))
    assert "Run it from inside `/tmp/syn`" in block
    assert "that field ONLY" in block
    assert "not in any registered project" in block


def test_telegram_makes_no_token_claim_when_unconfigured(tmp_path):
    """T-0726 DoD 3: the template used to tell EVERY project its token was in
    "`.env` / `secrets.toml`". watchrobot has no `secrets.toml` at all. An
    unconfigured project now gets the bot name and no location guess."""
    cfg = _synthetic_config(tmp_path, 'tg_bot = "@synbot"\n')
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "Bot `@synbot`. Ping the stakeholder" in out
    assert "secrets.toml" not in out
    assert "token in `.env`" not in out


def test_telegram_token_source_is_config_driven(tmp_path):
    cfg = _synthetic_config(
        tmp_path, 'tg_bot = "@synbot"\ntg_token_source = "`BOT_TOKEN` in `.envrc`"\n')
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "Bot `@synbot` (token: `BOT_TOKEN` in `.envrc`)." in out


def test_no_bot_no_token_line(tmp_path):
    """A `tg_token_source` without a `tg_bot` renders nothing — the sentence
    it belongs to is the bot sentence."""
    cfg = _synthetic_config(tmp_path, 'tg_token_source = "`X` in `.env`"\n')
    _seed_vision(tmp_path, "synthetic")
    out = ram.render("synthetic", cfg, tmp_path)
    assert "token:" not in out
    assert "Ping the stakeholder rarely" in out


def test_real_projects_name_their_own_token_source(tmp_path):
    """The two real projects are exactly why the old hardcoded phrase could not
    be right for both: watchrobot's token is `TELEGRAM_BOT_TOKEN` in `.env`
    (`backend/config.py:127`) and it has no `secrets.toml` anywhere, while
    bot-squad's own token IS in `config/secrets.toml` — and bot-squad registers
    no `tg_bot`, so it must get no bot sentence and no token claim at all."""
    _seed_vision(tmp_path, "watchrobot")
    wr = ram.render("watchrobot", _CONFIG_DIR, tmp_path)
    assert ("Bot `@watchbot` (token: `TELEGRAM_BOT_TOKEN` in `.env`, "
            "read at `backend/config.py:127`)." in wr)
    assert "secrets.toml" not in wr

    _seed_vision(tmp_path, "bot-squad")
    bs = ram.render("bot-squad", _CONFIG_DIR, tmp_path)
    assert "Bot `" not in bs
    assert "token" not in bs.split("## Telegram")[1]


# ---------------------------------------------------------------------------
# Path resolvability gate (T-0726)
# ---------------------------------------------------------------------------

# The rendered file names paths; until T-0726 nothing ever walked them. A
# dangling `ops/bot-squad` symlink (-> `data/signal-tracker`, renamed away by
# T-0189 on 2026-06-02) survived ~2.5 months, and the same prefix was simply
# ABSENT from watchrobot's master and deploy clones the whole time — so all 23
# `ops/bot-squad/...` paths in the every-turn file were dead for a session
# doing prod hotfixes. These tests exist to make that state RED, so the first
# two build the exact failures and assert the colour, and the rest pin the one
# way this predicate could go quietly vacuous (treating "cannot read" as "ok"
# or as "broken").


def _clone_config(tmp_path: Path, clones: dict[str, Path], ops: str = "ops") -> Path:
    """projects.toml for one project whose clones are real dirs on disk."""
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    body = ['[projects.probe]', 'slug = "probe"', f'ops_path = "{ops}"']
    for field, path in clones.items():
        body.append(f'{field} = "{path}"')
    (cfg / "projects.toml").write_text("\n".join(body) + "\n")
    return cfg


def _statuses(findings: list[dict]) -> dict[str, str]:
    """{where: status} — the findings keyed by the clone they were probed in."""
    return {f["where"]: f["status"] for f in findings}


def test_gate_reds_on_a_dangling_symlink(tmp_path):
    """THE failure that actually happened: `ops` is a symlink whose target was
    renamed away. It is present to `lstat`, absent to `stat`, and a naive
    `Path.exists()` check calls it missing without saying why — so assert both
    the red AND that the detail names the dead target."""
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "ops").symlink_to(tmp_path / "data-renamed-away")
    project = {"repo_path": str(clone)}

    findings = ram.check_project_paths(project, "probe")
    assert _statuses(findings) == {"repo_path": "broken"}
    assert "dangling symlink" in findings[0]["detail"]
    assert "data-renamed-away" in findings[0]["detail"]
    assert findings[0]["status"] in ram.RED_STATUSES


def test_gate_reds_when_a_relative_ops_path_is_missing_from_a_SECOND_clone(tmp_path):
    """The 2.5-month hole in full: the prefix resolves in the clone someone
    planted it in and is absent from the others. A gate that only ever looked
    at `repo_path` would have been green for all of it."""
    dev, master, deploy = (tmp_path / n for n in ("dev", "master", "deploy"))
    for d in (dev, master, deploy):
        d.mkdir()
    (tmp_path / "data").mkdir()
    (dev / "ops").symlink_to(tmp_path / "data")  # planted here only
    project = {"repo_path": str(dev), "repo_master": str(master),
               "repo_deploy": str(deploy)}

    findings = ram.check_project_paths(project, "probe")
    assert _statuses(findings) == {
        "repo_path": "ok", "repo_master": "missing", "repo_deploy": "missing",
    }
    assert sum(f["status"] in ram.RED_STATUSES for f in findings) == 2


def test_gate_is_green_once_the_symlink_points_somewhere_real(tmp_path):
    """Positive control on the SAME layout as the two reds above — the
    predicate distinguishes, rather than always failing."""
    dev, master = tmp_path / "dev", tmp_path / "master"
    for d in (dev, master):
        d.mkdir()
    (tmp_path / "data").mkdir()
    for d in (dev, master):
        (d / "ops").symlink_to(tmp_path / "data")
    project = {"repo_path": str(dev), "repo_master": str(master)}

    findings = ram.check_project_paths(project, "probe")
    assert _statuses(findings) == {"repo_path": "ok", "repo_master": "ok"}
    assert not [f for f in findings if f["status"] in ram.RED_STATUSES]


def test_an_absolute_ops_path_is_probed_once_not_per_clone(tmp_path):
    """T-0726's fix for watchrobot: an absolute `ops_path` is the same path
    from every clone, so it is checked once — and still reds when it is gone."""
    data = tmp_path / "data"
    data.mkdir()
    dev = tmp_path / "dev"
    dev.mkdir()
    ok = ram.check_project_paths(
        {"repo_path": str(dev), "ops_path": str(data)}, "probe")
    assert _statuses(ok) == {"absolute": "ok"}
    assert len(ok) == 1

    gone = ram.check_project_paths(
        {"repo_path": str(dev), "ops_path": str(tmp_path / "nope")}, "probe")
    assert _statuses(gone) == {"absolute": "missing"}


def test_unreadable_clone_is_unverifiable_not_red(tmp_path):
    """`os.path.exists()` answers False for PermissionError exactly as it does
    for a deletion, and the live registry holds a project owned by another
    unix user (guestent, under /home/flomaster). A gate that reds on a path
    nobody here can fix gets muted, which is how a silent 2.5 months happens —
    so 'cannot read' must be its own answer and must NOT fail the run."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "ops").mkdir()
    locked.chmod(0o000)
    try:
        findings = ram.check_project_paths({"repo_path": str(locked)}, "probe")
        status = _statuses(findings)["repo_path"]
    finally:
        locked.chmod(0o755)
    assert status == "unverifiable"
    assert status not in ram.RED_STATUSES


def test_probe_path_reports_a_plain_missing_path(tmp_path):
    assert ram.probe_path(tmp_path / "nothing")[0] == "missing"
    assert ram.probe_path(tmp_path)[0] == "ok"


def test_install_shims_are_checked(tmp_path):
    """The '## Deploy' block names `$BOT_SQUAD/scripts/cli/*.sh`; those are
    paths too, and an install missing them reds."""
    root = tmp_path / "install"
    (root / "scripts" / "cli").mkdir(parents=True)
    assert [f["status"] for f in ram.check_install_paths(root)] == \
        ["missing", "missing", "missing"]
    for name in ("deploy.sh", "pause-deploys.sh", "resume-deploys.sh"):
        (root / "scripts" / "cli" / name).write_text("#!/bin/sh\n")
    assert {f["status"] for f in ram.check_install_paths(root)} == {"ok"}


def test_live_registry_names_no_unresolvable_path():
    """The watchdog itself: every path the SHIPPED registry promises resolves
    where a session would look for it. This is the assertion that was missing
    for 2.5 months. Unreadable clones (another unix user's) are reported as
    unverifiable and deliberately do not fail this."""
    projects = ram.load_projects(_CONFIG_DIR)
    findings: list[dict] = []
    for slug, project in sorted(projects.items()):
        findings.extend(ram.check_project_paths(project, slug))
    red = [f for f in findings if f["status"] in ram.RED_STATUSES]
    assert not red, "unresolvable paths in projects.toml:\n" + \
        ram.format_findings(red)


# ---------------------------------------------------------------------------
# The two REAL projects against the REAL registry
# ---------------------------------------------------------------------------

def _test_block(rendered: str) -> str:
    """Slice the '## Test commands' section out of a rendered AGENTS.md."""
    start = rendered.index("## Test commands")
    end = rendered.index("## When you need more", start)
    return rendered[start:end].strip()


def test_watchrobot_real_config(tmp_path):
    """watchrobot renders with ITS identity: the absolute ops prefix (T-0726),
    the signal-tracker-old hard rule (now config-driven), @watchbot, and the
    test-commands block CORRECTED by T-0724."""
    _seed_vision(tmp_path, "watchrobot")
    out = ram.render("watchrobot", _CONFIG_DIR, tmp_path)
    assert "`/home/www/bot-squad/data/watchrobot/vision/constitution.md`" in out
    # T-0726: the in-clone prefix exists ONLY in the dev clone (untracked), so
    # naming it made 23 paths dead for a session in the master clone. Reds on a
    # revert of ops_path, not merely on a wording change.
    assert "ops/bot-squad/" not in out
    assert "archived/signal-tracker-old" in out
    assert "Bot `@watchbot`" in out
    assert "Tailwind" in out
    assert _test_block(out) == (
        "## Test commands\n\n"
        "- Backend: `backend/run_tests.sh` (T-0338 — manages its own venv and "
        "defaults `DATABASE_URL` to `signal_tracker_dev`; bare `pytest` points "
        "at PROD). Caveats: "
        "`/home/www/bot-squad/data/watchrobot/AGENT_INSTRUCTIONS.md`.\n"
        "- Frontend unit: `web/run_tests.sh` (= `npm test` from `web/`; "
        "`node:test`, NOT vitest — needs node >= 22.6, the script finds it). "
        "Gates the staging image build (T-0677).\n"
        "- Frontend e2e: `npm test` from `tests/` (a separate package — there "
        "is no `test:e2e` in `web/` and never has been)\n"
        "- Type-check: from `web/`, `npx tsc -b` — `-p tsconfig.json` checks "
        "ZERO files and always exits 0 (T-0471)\n"
        "- Lint: **not wired up** — no `lint` script and no `eslint.config.*` "
        "in `web/`, though the toolchain is in devDependencies (T-0375)"
    )
    # T-0724: the three commands that were configured here until 2026-08-18 do
    # not exist in the watchrobot tree — `signal-tracker` is the PROD container
    # and carries no pytest, and `web/package.json` has neither `test:e2e` nor
    # `lint`. Reds on a revert of the config, which is how they got shipped.
    for refuted in ("docker exec signal-tracker python test_api.py",
                    "`npm run test:e2e`",
                    "- Lint: from `web/`, `npm run lint`"):
        assert refuted not in out, refuted
    # No bot-squad-project identity in watchrobot's render.
    for leaked in ("cd api && pytest", "/home/almdudleer/bot-squad", "Bootstrap"):
        assert leaked not in out, leaked


def test_frontend_unit_field_is_rendered_and_optional(tmp_path):
    """T-0724 added `test_frontend_unit_cmd`. It renders under its own label,
    sits between Build and Frontend e2e, and stays absent for a project that
    does not configure it (bot-squad) — no KeyError, no empty bullet."""
    _seed_vision(tmp_path, "watchrobot")
    wr = ram.render("watchrobot", _CONFIG_DIR, tmp_path)
    block = _test_block(wr)
    assert "- Frontend unit: " in block
    assert block.index("- Frontend unit:") < block.index("- Frontend e2e:")

    _seed_vision(tmp_path, "bot-squad")
    bs = ram.render("bot-squad", _CONFIG_DIR, tmp_path)
    assert "Frontend unit" not in bs


def test_botsquad_real_config_no_watchrobot_leak(tmp_path):
    """T-0199 headline: bot-squad's WHOLE rendered file (not just the test
    block) carries zero watchrobot identity."""
    _seed_vision(tmp_path, "bot-squad")
    out = ram.render("bot-squad", _CONFIG_DIR, tmp_path)
    for leaked in ("signal-tracker", "@watchbot", "watchrobot", "Tailwind",
                   "@timpo", "@alexeysdk", "asyncpg"):
        assert leaked not in out, leaked
    assert "- Backend: `cd api && pytest`, `cd worker && pytest`" in out
    assert "- Build: `cd web && npm run build` (build only — no e2e yet)" in out
    # ops_path is the absolute install data dir (clone has no data symlink).
    assert "`/home/www/bot-squad/data/bot-squad/vision/constitution.md`" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Registry resolution (T-0878)
# ---------------------------------------------------------------------------

# `config/projects.toml` is the install-owned LIVE registry and is git-ignored;
# the repo tracks `config/projects.default.toml` as the seed. Every test above
# loads `_CONFIG_DIR`, so in a fresh clone (and in CI, which is exactly that)
# there is no live file at all — the fallback below is what keeps them from all
# going red on a checkout, and it is invisible on a dev box where the live file
# happens to exist.


def test_registry_falls_back_to_the_tracked_seed(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "projects.default.toml").write_text(
        '[projects.seeded]\nslug = "seeded"\nrepo_path = "/tmp/s"\n'
    )
    assert ram.registry_file(cfg).name == "projects.default.toml"
    assert set(ram.load_projects(cfg)) == {"seeded"}


def test_live_registry_wins_over_the_seed(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "projects.default.toml").write_text(
        '[projects.seeded]\nslug = "seeded"\nrepo_path = "/tmp/s"\n'
    )
    (cfg / "projects.toml").write_text(
        '[projects.live]\nslug = "live"\nrepo_path = "/tmp/l"\n'
    )
    assert ram.registry_file(cfg).name == "projects.toml"
    assert set(ram.load_projects(cfg)) == {"live"}


def test_the_shipped_seed_is_what_the_real_registry_tests_read_in_a_clone():
    """The 34 tests above must work off the TRACKED file, not off a live file
    that only exists on a machine someone has already deployed. Assert the seed
    is present and carries the two real projects they assert against."""
    import tomllib

    seed = _CONFIG_DIR / "projects.default.toml"
    assert seed.exists(), "config/projects.default.toml missing from the repo"
    with open(seed, "rb") as fh:
        seeded = tomllib.load(fh).get("projects", {})
    assert {"bot-squad", "watchrobot"} <= set(seeded)
