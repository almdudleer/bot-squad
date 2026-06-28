"""T-0480: alias resolver for old initiative refs (INI-NN / .md basename / stem)
→ the new initiative-task T-id. Byte-identical worker/api mirror; pure json+regex
(the index BUILDER that reads `aka:` frontmatter lives in the migration script)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot_squad_worker.initiative_resolver import (
    load_alias_index,
    normalize_ref,
    resolve_initiative_ref,
)

SLUG = "bot-squad"


def _write_index(data_dir: Path, mapping: dict) -> None:
    p = data_dir / SLUG / "vision"
    p.mkdir(parents=True, exist_ok=True)
    (p / "initiative_aliases.json").write_text(json.dumps(mapping), encoding="utf-8")


# --- normalize_ref ----------------------------------------------------------

def test_normalize_ref_strips_one_md():
    assert normalize_ref("foo.md") == "foo"
    assert normalize_ref("foo.md.md") == "foo.md"
    assert normalize_ref("foo") == "foo"
    assert normalize_ref("INI-01") == "INI-01"


# --- T-id passthrough -------------------------------------------------------

def test_resolves_task_id_passthrough(tmp_path: Path):
    assert resolve_initiative_ref(tmp_path, SLUG, "T-0544") == "T-0544"


def test_resolves_task_id_with_md_suffix(tmp_path: Path):
    assert resolve_initiative_ref(tmp_path, SLUG, "T-0544.md") == "T-0544"


# --- empties ----------------------------------------------------------------

@pytest.mark.parametrize("ref", ["", "   ", None])
def test_empty_ref_is_none(tmp_path: Path, ref):
    assert resolve_initiative_ref(tmp_path, SLUG, ref) is None


def test_unknown_ref_no_index_is_none(tmp_path: Path):
    assert resolve_initiative_ref(tmp_path, SLUG, "process-paradigm") is None


# --- alias-index lookup -----------------------------------------------------

def test_resolves_ini_number(tmp_path: Path):
    _write_index(tmp_path, {"INI-01": "T-0544"})
    assert resolve_initiative_ref(tmp_path, SLUG, "INI-01") == "T-0544"


def test_resolves_basename_and_stem(tmp_path: Path):
    _write_index(tmp_path, {"process-paradigm": "T-0545"})
    assert resolve_initiative_ref(tmp_path, SLUG, "process-paradigm") == "T-0545"
    # a .md basename input must resolve via the same stored stem key
    assert resolve_initiative_ref(tmp_path, SLUG, "process-paradigm.md") == "T-0545"


def test_index_keys_with_md_are_normalized(tmp_path: Path):
    # the stored key carries a .md; both stem and basename inputs must hit it
    _write_index(tmp_path, {"foo.md": "T-0546"})
    assert resolve_initiative_ref(tmp_path, SLUG, "foo") == "T-0546"
    assert resolve_initiative_ref(tmp_path, SLUG, "foo.md") == "T-0546"


def test_malformed_index_treated_as_empty(tmp_path: Path):
    p = tmp_path / SLUG / "vision"
    p.mkdir(parents=True, exist_ok=True)
    (p / "initiative_aliases.json").write_text("{not valid json", encoding="utf-8")
    assert resolve_initiative_ref(tmp_path, SLUG, "INI-01") is None


def test_load_alias_index_missing_is_empty(tmp_path: Path):
    assert load_alias_index(tmp_path, SLUG) == {}


# --- byte-identical worker/api mirror ---------------------------------------

def test_worker_and_api_copies_are_byte_identical():
    """The worker (systemd) and API (docker) run in separate environments and do
    not import each other, so the resolver is duplicated. The two copies MUST
    stay byte-identical — otherwise an agent read and a web read could resolve an
    old initiative ref to different tasks."""
    repo = Path(__file__).resolve().parents[2]
    worker_copy = repo / "worker" / "bot_squad_worker" / "initiative_resolver.py"
    api_copy = repo / "api" / "app" / "initiative_resolver.py"
    assert api_copy.exists(), f"API mirror missing: {api_copy}"
    assert worker_copy.read_bytes() == api_copy.read_bytes(), (
        "worker/bot_squad_worker/initiative_resolver.py and "
        "api/app/initiative_resolver.py have drifted — re-sync them "
        "(they must be byte-identical)."
    )
