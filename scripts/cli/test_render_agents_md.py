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
# The two REAL projects against the REAL registry
# ---------------------------------------------------------------------------

def _test_block(rendered: str) -> str:
    """Slice the '## Test commands' section out of a rendered AGENTS.md."""
    start = rendered.index("## Test commands")
    end = rendered.index("## When you need more", start)
    return rendered[start:end].strip()


def test_watchrobot_real_config(tmp_path):
    """watchrobot renders with ITS identity: legacy ops/bot-squad prefix, the
    signal-tracker-old hard rule (now config-driven), @watchbot, and the
    test-commands block CORRECTED by T-0724."""
    _seed_vision(tmp_path, "watchrobot")
    out = ram.render("watchrobot", _CONFIG_DIR, tmp_path)
    assert "`ops/bot-squad/vision/constitution.md`" in out
    assert "archived/signal-tracker-old" in out
    assert "Bot `@watchbot`" in out
    assert "Tailwind" in out
    assert _test_block(out) == (
        "## Test commands\n\n"
        "- Backend: `backend/run_tests.sh` (T-0338 — manages its own venv and "
        "defaults `DATABASE_URL` to `signal_tracker_dev`; bare `pytest` points "
        "at PROD). Caveats: `ops/bot-squad/AGENT_INSTRUCTIONS.md`.\n"
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
