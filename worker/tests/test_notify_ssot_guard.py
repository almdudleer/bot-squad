"""T-0394 enumeration guard (T-0381 pattern): one _send_stakeholder_dm SSOT.

The personal pagers (page the human) must route through actions._send_stakeholder_dm
(MAX-primary/TG-failover on this DPI-blocked host). This guard makes a
re-scattered raw stakeholder sender go RED — the SSOT collapse can't silently
regress. Raw TG-client sends are allowed ONLY in group/system senders (audience
is a project group/forum, not a personal DM).
"""
from __future__ import annotations

from pathlib import Path

import bot_squad_worker

PKG = Path(bot_squad_worker.__file__).parent

# Modules that legitimately call a raw TG client — their audience is a GROUP or
# system channel, not a personal stakeholder DM. Add here (with justification)
# only when a new GROUP sender is introduced.
RAW_TG_SEND_ALLOWED = {
    "actions.py",       # the _send_stakeholder_dm SSOT itself + pause/resume (#deploy-logs) + per-user peer tg-mirror
    "jobs.py",          # deploy events + _alert_operators (#deploy-logs) + oauth_refresh system alert
    "autopilot.py",     # autopilot run status -> project group
    "autonomous.py",    # autonomous run status -> project group
    "voice_intake.py",  # voice-note confirmation -> #feedback topic
}
# The TG/MAX client classes themselves.
CLIENT_MODULES = {"tg.py", "max.py"}

# Modules whose personal pagers were collapsed into the SSOT (T-0394).
PERSONAL_PAGER_MODULES = {"tg_stall.py", "telemetry.py", "autoupdate_apply.py"}


def test_send_stakeholder_dm_is_the_ssot():
    from bot_squad_worker import actions
    assert hasattr(actions, "_send_stakeholder_dm")


def test_personal_pagers_route_through_ssot():
    for mod in PERSONAL_PAGER_MODULES:
        src = (PKG / mod).read_text()
        assert "_send_stakeholder_dm" in src, f"{mod} must page via the SSOT"
        assert "_get_tg_client" not in src, f"{mod} re-scattered a raw TG sender"
        assert "TgClient(" not in src, f"{mod} re-scattered a raw TG sender"


def test_no_unsanctioned_raw_tg_sender():
    """A NEW personal pager bypassing _send_stakeholder_dm goes RED here."""
    offenders = []
    for py in sorted(PKG.glob("*.py")):
        if py.name in RAW_TG_SEND_ALLOWED or py.name in CLIENT_MODULES:
            continue
        src = py.read_text()
        if "_get_tg_client(" in src or "TgClient(" in src:
            offenders.append(py.name)
    assert not offenders, (
        f"raw TG-client sender(s) outside the allowlist: {offenders} — route "
        f"personal pages through actions._send_stakeholder_dm, or add to "
        f"RAW_TG_SEND_ALLOWED if it is a legitimate group/system sender."
    )
