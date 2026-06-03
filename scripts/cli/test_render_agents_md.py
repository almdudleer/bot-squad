"""Tests for render_agents_md.py's config-driven '## Test commands' block (T-0195).

Before T-0195 the template hardcoded watchrobot's
`docker exec signal-tracker python test_api.py`, so EVERY project's generated
AGENTS.md advertised watchrobot's container. The block is now built from
optional per-project `test_*_cmd` fields in projects.toml.

The module reads vision/{north-star,strategy,tactical}.md to render the full
template; those files no longer exist in live data (the vision schema moved to
product.md etc.), so these tests render against minimal fixture vision files in
a tmp data dir — exercising the real `render()` end-to-end, not just the helper.
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
# render_test_commands() — the pure builder
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


def test_brace_in_command_survives_format():
    """A command containing literal {braces} must pass through str.format
    untouched (DoD: mind brace-escaping)."""
    project = {"test_backend_cmd": "`grep -o '{.*}' x.json`"}
    block = ram.render_test_commands(project)
    # Substituted as a single {test_commands} value → never re-scanned.
    out = ram.TEMPLATE.format(
        display_name="X", slug="x", repo_path="/x",
        north_star_body="ns", strategy_body="st", tactical_body="ta",
        test_commands=block,
    )
    assert "grep -o '{.*}' x.json" in out


# ---------------------------------------------------------------------------
# Full render() against fixture vision + the REAL registry
# ---------------------------------------------------------------------------

def _seed_vision(data_dir: Path, slug: str) -> None:
    vis = data_dir / slug / "vision"
    vis.mkdir(parents=True, exist_ok=True)
    (vis / "north-star.md").write_text("# North star\n\nWin.\n")
    (vis / "strategy.md").write_text("# Strategy\n\nBet on it.\n")
    (vis / "tactical.md").write_text("# Tactical\n\nDo the thing.\n")


def _test_block(rendered: str) -> str:
    """Slice the '## Test commands' section out of a rendered AGENTS.md."""
    start = rendered.index("## Test commands")
    end = rendered.index("## When you need more", start)
    return rendered[start:end].strip()


def test_watchrobot_block_unchanged(tmp_path):
    """watchrobot's rendered test block must be byte-identical to the lines its
    AGENTS.md carried before the template stopped hardcoding them."""
    _seed_vision(tmp_path, "watchrobot")
    out = ram.render("watchrobot", _CONFIG_DIR, tmp_path)
    assert _test_block(out) == (
        "## Test commands\n\n"
        "- Backend: `docker exec signal-tracker python test_api.py`\n"
        "- Frontend e2e: `npm run test:e2e` (from `web/`)\n"
        "- Type-check: from `web/`, `npx tsc -b --noEmit`\n"
        "- Lint: from `web/`, `npm run lint`"
    )


def test_botsquad_no_signaltracker_leak(tmp_path):
    """bot-squad's AGENTS.md gets its OWN commands; the TEST-COMMANDS block
    never advertises watchrobot's signal-tracker container.

    NB: the assertion is scoped to the test block on purpose — the rest of the
    template is independently bot-squad/watchrobot-hardcoded (`ops/bot-squad/`,
    a `signal-tracker-old` hard rule, `@watchbot`), which is a SEPARATE, broader
    issue tracked in its own follow-up ticket, not T-0195's DoD."""
    _seed_vision(tmp_path, "bot-squad")
    out = ram.render("bot-squad", _CONFIG_DIR, tmp_path)
    block = _test_block(out)
    assert "signal-tracker" not in block
    assert "- Backend: `cd api && pytest`, `cd worker && pytest`" in block
    assert "- Build: `cd web && npm run build` (build only — no e2e yet)" in block


def test_fieldless_project_full_render(tmp_path):
    """An end-to-end render of a project with no test_*_cmd fields: no KeyError,
    placeholder in the section, no signal-tracker leak."""
    slug = "synthetic"
    _seed_vision(tmp_path, slug)
    project = {"display_name": "Synthetic", "repo_path": "/tmp/syn"}
    rendered = ram.TEMPLATE.format(
        display_name="Synthetic", slug=slug, repo_path="/tmp/syn",
        north_star_body="ns", strategy_body="st", tactical_body="ta",
        test_commands=ram.render_test_commands(project),
    )
    test_block = _test_block(rendered)
    assert "signal-tracker" not in test_block
    assert "No project-specific test commands" in test_block


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
