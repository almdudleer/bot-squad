"""T-0394 enumeration guard (T-0381 pattern): one _send_stakeholder_dm SSOT.

The personal pagers (page the human) must route through actions._send_stakeholder_dm
(MAX-primary/TG-failover on this DPI-blocked host). This guard makes a
re-scattered raw stakeholder sender go RED — the SSOT collapse can't silently
regress. Raw TG-client sends are allowed ONLY in group/system senders (audience
is a project group/forum, not a personal DM).
"""
from __future__ import annotations

import ast
from pathlib import Path

import bot_squad_worker

PKG = Path(bot_squad_worker.__file__).parent

# Modules that legitimately call a raw TG client — their audience is a GROUP or
# system channel, not a personal stakeholder DM. Add here (with justification)
# only when a new GROUP sender is introduced.
RAW_TG_SEND_ALLOWED = {
    "actions.py",       # the _send_stakeholder_dm SSOT itself + pause/resume (#deploy-logs, topic_id) + per-user peer tg-mirror
    "jobs.py",          # routine #deploy-logs deploy events (topic_id); KILLED alert + oauth_refresh now route via SSOT (P2-04/P2-08)
    "voice_intake.py",  # voice-note confirmation -> #feedback topic (topic_id)
}
# The TG/MAX client classes themselves.
CLIENT_MODULES = {"tg.py", "max.py"}

# Modules whose personal pagers were collapsed into the SSOT (T-0394, +autopilot P2-08).
PERSONAL_PAGER_MODULES = {"tg_stall.py", "telemetry.py", "autoupdate_apply.py", "autopilot.py"}

# --- T-0406 (P2-08): call-site-level guard ---------------------------------
# The module allowlist above is too coarse: a NEW personal pager added INSIDE an
# allowlisted module is invisible to it (exactly how P2-04's deploy-KILLED page
# hid inside jobs.py). So we also require every raw TG-client ``.send()`` in an
# allowlisted module to pass an explicit group ``topic_id=`` — a bare-chat send
# is presumed a hidden personal pager and goes RED.

# Functions whose raw TG sends ARE the sanctioned personal-page path (the SSOT
# itself) — exempt from the group-topic requirement.
SSOT_FUNCTIONS = {"_send_stakeholder_dm"}

# Call sites that legitimately send a raw per-USER DM (no forum topic): NOT a
# stakeholder page (those route via the SSOT) and NOT a group post (those carry
# a topic_id). Keyed (module, enclosing_function). Add only with justification.
BARE_DM_ALLOWED = {
    ("actions.py", "_action_peer_send"),  # mirrors a peer-bus msg to that user's own bound TG DM
}


def _tg_bound_names(tree: ast.AST) -> set[str]:
    """Local names assigned from ``_get_tg_client(...)`` (e.g. ``tg = ...``)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _is_get_tg_client(node.value):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
    return names


def _is_get_tg_client(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Name) and f.id == "_get_tg_client":
        return True
    # ``A._get_tg_client(cfg)`` (imported-module access, e.g. voice_intake)
    return isinstance(f, ast.Attribute) and f.attr == "_get_tg_client"


def _is_tg_receiver(recv: ast.AST, tg_names: set[str]) -> bool:
    if isinstance(recv, ast.Call) and _is_get_tg_client(recv):
        return True
    return isinstance(recv, ast.Name) and recv.id in tg_names


def _bare_tg_sends(module: str, src: str) -> list[tuple[str, int]]:
    """``(enclosing_function, lineno)`` for raw TG ``.send()`` lacking topic_id=."""
    tree = ast.parse(src)
    tg_names = _tg_bound_names(tree)
    offenders: list[tuple[str, int]] = []

    def walk(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "send"
                and _is_tg_receiver(child.func.value, tg_names)
            ):
                exempt = func in SSOT_FUNCTIONS or (module, func) in BARE_DM_ALLOWED
                if not exempt and not any(kw.arg == "topic_id" for kw in child.keywords):
                    offenders.append((func, child.lineno))
            walk(child, func)

    walk(tree, "<module>")
    return offenders


def test_send_stakeholder_dm_is_the_ssot():
    from bot_squad_worker import actions
    assert hasattr(actions, "_send_stakeholder_dm")


def test_personal_pagers_route_through_ssot():
    for mod in PERSONAL_PAGER_MODULES:
        src = (PKG / mod).read_text()
        assert "_send_stakeholder_dm" in src, f"{mod} must page via the SSOT"
        assert "_get_tg_client" not in src, f"{mod} re-scattered a raw TG sender"
        assert "TgClient(" not in src, f"{mod} re-scattered a raw TG sender"


def test_deploy_killed_alert_routes_through_ssot():
    """P2-04: the loud deploy-KILLED operator alert must page via the SSOT.

    jobs.py legitimately keeps raw GROUP senders (routine #deploy-logs events,
    the oauth_refresh system alert), so it stays in RAW_TG_SEND_ALLOWED — but the
    KILLED alert is a personal page on this DPI-blocked host and must not raw-send
    (it silently dropped before). This locks _alert_operators onto the SSOT.
    """
    src = (PKG / "jobs.py").read_text()
    assert "_send_stakeholder_dm" in src, (
        "jobs.py deploy-KILLED alert must route through actions._send_stakeholder_dm "
        "(MAX-primary), not a raw TG send that drops on this DPI-blocked host"
    )


def test_raw_tg_sends_declare_an_explicit_topic():
    """P2-08 (T-0406): every raw TG-client ``.send()`` in an allowlisted module
    must pass an explicit group ``topic_id=``.

    The module allowlist is intent-coarse; this is the call-site check. A bare-chat
    send (no topic_id) is presumed a hidden personal pager — route it through the
    SSOT (MAX-primary) or, if it is a per-user DM, document it in BARE_DM_ALLOWED.
    """
    offenders = {}
    for mod in RAW_TG_SEND_ALLOWED:
        bare = _bare_tg_sends(mod, (PKG / mod).read_text())
        if bare:
            offenders[mod] = bare
    assert not offenders, (
        f"raw TG .send() without an explicit topic_id= (hidden personal pager?): "
        f"{offenders} — route stakeholder pages through actions._send_stakeholder_dm "
        f"(MAX-primary), give group posts an explicit topic_id=, or document a "
        f"genuine per-user DM in BARE_DM_ALLOWED."
    )


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
