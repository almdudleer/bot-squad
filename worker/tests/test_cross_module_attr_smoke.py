"""T-0186: tests for the cross-module attribute smoke (absorption-split gate).

The smoke lives in scripts/lint/ (stdlib-only, shared by CI + pre-push), so we
load it by path rather than as a package import.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SMOKE_PATH = _REPO / "scripts" / "lint" / "cross_module_attr_smoke.py"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("cross_module_attr_smoke", _SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Register before exec so the module's frozen dataclass (Pair) can resolve
    # its own __module__ in sys.modules during @dataclass processing.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


smoke = _load_smoke()


# A self-contained synthetic pair so the split/coherent cases don't depend on
# the real source (which legitimately changes over time).
_PROVIDER_COHERENT = '''
from dataclasses import dataclass

@dataclass
class Config:
    tg_bot_token: str = ""
    tg_default_chat_id: str = ""

    @property
    def data_dir(self):
        return None

    def project_data_dir(self, slug):
        return slug
'''

# Same provider with the tg_default_chat_id field swept away by a split absorb.
_PROVIDER_SPLIT = '''
from dataclasses import dataclass

@dataclass
class Config:
    tg_bot_token: str = ""

    @property
    def data_dir(self):
        return None

    def project_data_dir(self, slug):
        return slug
'''

_CONSUMER = '''
def _get_config():
    return None

def handle():
    cfg = _get_config()
    token = cfg.tg_bot_token
    chat = cfg.tg_default_chat_id          # the field a split would drop
    d = cfg.data_dir                        # property, must resolve
    return cfg.project_data_dir("x"), token, chat, d
'''

_PAIR = smoke.Pair(
    name="synthetic",
    consumer="consumer.py",
    provider="config.py",
    provider_class="Config",
    seed_vars=(),                 # rely on auto-detection from _get_config()
    factory_names=("_get_config",),
)


def test_coherent_pair_has_no_offenders():
    assert smoke.audit(_CONSUMER, _PROVIDER_COHERENT, _PAIR) == []


def test_split_absorption_is_flagged():
    offenders = smoke.audit(_CONSUMER, _PROVIDER_SPLIT, _PAIR)
    # Every cfg.tg_default_chat_id access is reported; nothing else.
    assert offenders, "expected the dropped-field access to be flagged"
    assert {attr for _, _, attr in offenders} == {"tg_default_chat_id"}


def test_factory_assigned_var_is_auto_detected():
    """A var bound from the factory is checked even without an explicit seed."""
    src = "def f():\n    c = _get_config()\n    return c.nope\n"
    offenders = smoke.audit(src, _PROVIDER_COHERENT, _PAIR)
    assert offenders == [(3, "c", "nope")]


def test_provider_members_include_fields_and_methods():
    members = smoke.provider_class_members(_PROVIDER_COHERENT, "Config")
    assert {"tg_bot_token", "tg_default_chat_id", "data_dir", "project_data_dir"} <= members


def test_dunder_access_is_ignored():
    src = "def f():\n    cfg = _get_config()\n    return cfg.__class__\n"
    assert smoke.audit(src, _PROVIDER_COHERENT, _PAIR) == []


def test_real_repo_pairs_resolve_clean():
    """The live coupling must be coherent at HEAD — this is the deploy gate."""
    if not _SMOKE_PATH.exists():
        pytest.skip("smoke script not present")
    offenders: list[str] = []
    for pair in smoke.PAIRS:
        offenders.extend(smoke.check_pair(_REPO, pair))
    assert offenders == [], "real module pairs have an unresolved attribute:\n" + "\n".join(offenders)
