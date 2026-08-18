"""Where the live project registry lives — T-0878.

``config/projects.toml`` is the **live registry the install mutates**: every
project registered through ``POST /api/projects`` (and every ``PUT
/{slug}/tg``) writes a ``[projects.<slug>]`` block into it. Until T-0878 that
file was also **git-tracked**, and the bot-squad staging recipe syncs the
install with ``git merge --ff-only origin/<branch>`` straight into the install
dir. From the moment a project was registered there were exactly two outcomes:

1. the recipe's dirty-install guard fired (``exit 8``) and every deploy was
   refused until a human noticed — measured live: 2026-08-06 08:27 and
   2026-08-11 14:23, five days apart, both correct, both reaching nobody; or
2. without that guard the fast-forward would overwrite the file with the
   repo's version and **silently de-register the project** — slug, repo paths,
   ``tg_chat``, everything.

The fix is to stop the repo being the storage for a value the install owns.
``config/projects.toml`` is now git-IGNORED, exactly like its three siblings in
the same directory (``auth.toml``, ``secrets.toml``, ``system_settings.toml``)
— all install-owned, none tracked. The repo ships
``config/projects.default.toml`` as the tracked seed a fresh install starts
from.

The property this buys, and the one the tests pin (see
``api/tests/test_t0878_registry_survives_deploy.py``), is **not** the file
layout: it is that ``git checkout`` / ``merge --ff-only`` on the install can no
longer delete a live project. Widening the recipe's dirty-check to skip
``config/`` would have made deploys pass again *and* re-armed outcome 2; the
guard still fails closed on every tracked path.

The API cannot ``import bot_squad_worker`` (separate packages, no shared
import), so this module exists twice — ``worker/bot_squad_worker/registry.py``
and ``api/app/registry.py`` — and the two are kept **byte-identical**, the same
anti-drift contract as ``task_search.py`` and ``idalloc.py``. A test asserts it.
Edit one, copy it to the other.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

#: Install-owned live registry. Git-ignored; written by the API's project
#: routes; read by everything else (including the ``bsq`` shell helpers, which
#: hardcode this basename under ``$BOT_SQUAD/config/``).
LIVE_NAME = "projects.toml"

#: Tracked seed shipped in the repo. Only ever READ, and only when an install
#: has no live registry yet.
DEFAULT_NAME = "projects.default.toml"


def live_path(config_dir: Path) -> Path:
    """The path writers must use. Always the live file, never the seed."""
    return Path(config_dir) / LIVE_NAME


def default_path(config_dir: Path) -> Path:
    return Path(config_dir) / DEFAULT_NAME


def resolve(config_dir: Path, *, seed: bool = False) -> Path:
    """The registry file to READ.

    Prefers the live file; falls back to the tracked seed when an install has
    never registered anything. With ``seed=True`` the seed is first COPIED to
    the live path (atomically, best-effort) so the shell readers — which know
    only ``$BOT_SQUAD/config/projects.toml`` — find a file where they look.
    A read-only or racing config dir degrades to reading the seed in place
    rather than raising: seeding is a convenience, never a precondition.
    """
    live = live_path(config_dir)
    if live.exists():
        return live
    default = default_path(config_dir)
    if not default.exists():
        # Neither present — hand back the live path so the caller's own
        # "not found" error names the file operators actually look for.
        return live
    if seed:
        _seed(default, live)
        if live.exists():
            return live
    return default


def _seed(default: Path, live: Path) -> None:
    """Atomically materialise ``live`` from ``default``. Never raises."""
    tmp = live.with_name(live.name + f".seed.{os.getpid()}.tmp")
    try:
        tmp.write_text(default.read_text())
        os.rename(tmp, live)
        log.info("registry: seeded %s from %s", live, default.name)
    except OSError as e:  # read-only mount, permissions, lost race
        log.warning("registry: could not seed %s from %s: %s", live, default.name, e)
        try:
            tmp.unlink()
        except OSError:
            pass
