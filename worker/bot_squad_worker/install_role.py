"""Install role helper — is this install the mothership for a given project?

The mothership produces releases that other attached servers consume. The
self-exclusion guard exists so the producer never accidentally consumes its
own releases (the consumer-side poller / apply short-circuit when this
returns True). See T-0086.

SSOT: the ``mothership`` flag on the project block in projects.toml.
Belt-and-suspenders: also compare ``prod_url`` against a hardcoded
mothership URL — guards against accidental flag-drop on the real
mothership.
"""
from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

# Hardcoded so a misconfig / flag-drop on the actual mothership can't turn
# it into a consumer of its own releases.
MOTHERSHIP_URL = "https://botsquad.dev"


def _default_config_dir() -> Path:
    return Path(os.environ.get("BOT_SQUAD", "/home/www/bot-squad")) / "config"


def is_mothership(slug: str = "bot-squad", config_dir: Path | None = None) -> bool:
    """Return True if THIS install is the mothership for ``slug``.

    Reads projects.toml directly (rather than going through Config) so
    callers don't need the full app config in hand — useful for one-shot
    scripts (prod.sh) and tests.

    Returns False if projects.toml is missing or unreadable, or if the
    slug isn't present. Callers that need to distinguish "definitely not
    mothership" from "config missing" should check the file separately.
    """
    cfg_dir = config_dir if config_dir is not None else _default_config_dir()
    projects_toml = cfg_dir / "projects.toml"
    if not projects_toml.exists():
        return False
    try:
        raw = tomllib.loads(projects_toml.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return False
    project = raw.get("projects", {}).get(slug)
    if not project:
        return False
    # SSOT: explicit flag.
    if project.get("mothership") is True:
        return True
    # Belt-and-suspenders: URL match. Catches the case where the flag was
    # accidentally dropped from the mothership's projects.toml.
    if project.get("prod_url") == MOTHERSHIP_URL:
        return True
    return False


def warn_if_misconfigured(
    config_dir: Path,
    slug: str = "bot-squad",
    log: logging.Logger | None = None,
) -> None:
    """Log a WARNING if the mothership flag and deploy_targets disagree.

    Two misconfig shapes worth a loud heads-up at worker startup:
      - mothership=True but ``"prod"`` not in deploy_targets — producer
        pipeline isn't wired; the mothership won't actually publish
        releases for other servers to consume.
      - mothership not set (or False) but ``"prod"`` IS in deploy_targets
        — this install is producing a "prod" artifact yet doesn't claim
        to be the mothership; likely flag-drop or wrong server.

    Silent (no log) when the slug is absent from projects.toml.
    """
    if log is None:
        log = logging.getLogger("bot-squad-worker.install_role")

    projects_toml = config_dir / "projects.toml"
    if not projects_toml.exists():
        return
    try:
        raw = tomllib.loads(projects_toml.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return
    project = raw.get("projects", {}).get(slug)
    if not project:
        return

    mflag = is_mothership(slug=slug, config_dir=config_dir)
    deploy_targets = project.get("deploy_targets") or []
    has_prod = "prod" in deploy_targets

    if mflag and not has_prod:
        log.warning(
            "install_role: %s is mothership but deploy_targets=%r lacks 'prod' — "
            "producer pipeline not wired; mothership won't publish releases",
            slug, list(deploy_targets),
        )
    elif (not mflag) and has_prod:
        log.warning(
            "install_role: %s has 'prod' in deploy_targets=%r but is NOT marked "
            "mothership — likely misconfig (flag-drop or wrong server)",
            slug, list(deploy_targets),
        )
