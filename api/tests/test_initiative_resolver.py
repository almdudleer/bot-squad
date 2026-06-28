"""T-0480: api-side smoke test for the initiative alias resolver. The full
behavioral suite + the byte-identical mirror assertion live in
worker/tests/test_initiative_resolver.py; this just confirms the api copy loads
and resolves in the docker runtime."""
from __future__ import annotations

import json
from pathlib import Path

from app.initiative_resolver import normalize_ref, resolve_initiative_ref

SLUG = "bot-squad"


def test_task_id_passthrough(tmp_path: Path):
    assert resolve_initiative_ref(tmp_path, SLUG, "T-0544") == "T-0544"


def test_normalize_ref_strips_one_md():
    assert normalize_ref("foo.md") == "foo"


def test_resolves_via_index(tmp_path: Path):
    vis = tmp_path / SLUG / "vision"
    vis.mkdir(parents=True, exist_ok=True)
    (vis / "initiative_aliases.json").write_text(
        json.dumps({"INI-01": "T-0544", "process-paradigm": "T-0545"}),
        encoding="utf-8",
    )
    assert resolve_initiative_ref(tmp_path, SLUG, "INI-01") == "T-0544"
    assert resolve_initiative_ref(tmp_path, SLUG, "process-paradigm.md") == "T-0545"
    assert resolve_initiative_ref(tmp_path, SLUG, "nope") is None
