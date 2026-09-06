"""T-0964: the names the USER sees.

Stakeholder (2026-09-06): «сейчас из конкретных жалоб
gu_dc8262b6cea9098d98e04d7e-user-conversation -- вообще не понятно что это, эти
айдишники юзеру не надо светить, для юзера должно быть четко
universal_bsq_session, если budding, то уже по ролям кто есть кто,
dev_add_ui_button, operator, user_session и т.п.»

The rename is cheap; what it BREAKS is not. The gid used to ride in the window
precisely so an attendant was self-identifying from its immutable SID alone.
Taking it out means the (slug, gid) key now lives in the ``global_user_id`` md
field, and three things that used to be one lookup have to keep agreeing:

  * ``live_user_conversation_sid``  — is one already attending this user?
  * ``_find_suspended_user_conversation`` — is one resumable?
  * ``uc_redrive.is_attendant_answer`` — did the attendant actually reply?

Each is tested BOTH ways here — a post-rename session found by its field, and a
pre-rename session found by its legacy window — because both eras are on disk
at once and a fix that only serves the new shape silently re-drives, re-spawns
or duplicates every attendant that predates it.
"""
from __future__ import annotations

import pytest

from bot_squad_worker import sessions as S


# ---------------------------------------------------------------------------
# Role derivation — the window still has to say what the session IS.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("win", [
    "universal_bsq_session", "universal-bsq-session", "UNIVERSAL_BSQ_SESSION",
    "user_session", "user-session", "user_session_flomaster", "user-session-2",
])
def test_user_facing_windows_derive_the_user_conversation_role(win):
    assert S._derive_role(win, None, None) == "user-conversation"


def test_the_legacy_gid_window_still_derives_it():
    """Sessions spawned before the rename are still running."""
    assert S._derive_role("gu_a1b2c3-user-conversation", None, None) == (
        "user-conversation")


@pytest.mark.parametrize("win,role", [
    ("dev_add_ui_button", "dev"),
    ("dev-add-ui-button", "dev"),
    # The point of the prefix arm: a feature slug ending in another role's
    # marker must NOT hand the session that role's contract.
    ("dev_move_the_qa", "dev"),
    ("dev_drop_the_operator", "dev"),
    ("dev_rewrite_the_tl", "dev"),
])
def test_dev_prefix_is_authoritative(win, role):
    assert S._derive_role(win, None, None) == role


# The collision is REAL and it is at the END of the string, not the start.
# Reported live (operator, 2026-09-06) on p642, a dev lane on T-0929 whose
# window is the faithful slug of a ticket titled "operator drive mechanism …".
# That one is classified correctly TODAY — every marker regex is suffix-
# anchored, so a slug that merely BEGINS with `operator` is a dev — and the
# first case below pins that, because it is the specimen a future edit is most
# likely to "fix" into a prefix match and break.
#
# What the report gets right is the class: a ticket TITLE mints a window, and
# titles ending in a role word are ordinary. Those are the ones that really did
# mis-derive, which is what the `dev_` prefix closes.
def test_a_slug_that_merely_begins_with_a_role_word_is_a_dev():
    for win in ("operator-drive-mechanism-undisclosed-sta",
                "qa-coverage-for-the-intake-path",
                "teamlead-handoff-is-losing-context"):
        assert S._derive_role(win, "T-0929", None) == "dev", win


@pytest.mark.parametrize("title_slug", [
    "make-the-operator", "improve-the-qa", "rewrite-the-teamlead",
])
def test_a_title_ENDING_in_a_role_word_used_to_mis_derive(title_slug):
    """The bare-slug window (pre-T-0964) genuinely hands the dev someone else's
    contract — asserted as the defect it is, so the fix below is measured
    against a red, not a hypothetical."""
    assert S._derive_role(title_slug, "T-0929", None) != "dev"


@pytest.mark.parametrize("title_slug", [
    "make-the-operator", "improve-the-qa", "rewrite-the-teamlead",
])
def test_and_the_dev_prefix_closes_it(title_slug):
    win = "dev_" + title_slug.replace("-", "_")
    assert S._derive_role(win, "T-0929", None) == "dev"


def test_dev_prefix_does_not_swallow_words_merely_starting_with_dev():
    """`dev` must be a whole segment — `develop-qa` is not a dev bud."""
    assert S._derive_role("develop-qa", None, None) == "qa"


def test_user_session_marker_stays_segment_anchored():
    """Mirrors recycle_gate's T-0616 regex: `user-sessions` is a different word
    and must not be read as the human's own session."""
    assert S._derive_role("user-sessions", None, None) == "dev"
    assert S._derive_role("user-feedback", None, None) == "dev"


# ---------------------------------------------------------------------------
# The gid moved to the md — and every reader has to find it BOTH ways.
# ---------------------------------------------------------------------------

def _cfg(tmp_path):
    from types import SimpleNamespace
    return SimpleNamespace(data_dir=tmp_path, projects={"p": object()})


def _md(tmp_path, sid, *, window="", gid="", **extra):
    d = tmp_path / "p" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"sid": sid}
    if window:
        meta["window"] = window
    if gid:
        meta["global_user_id"] = gid
    meta.update(extra)
    S._write_session_metadata(d / f"{sid}.md", meta)
    return d / f"{sid}.md"


def test_session_global_user_id_treats_the_unset_sentinel_as_absent():
    assert S.session_global_user_id({"global_user_id": "gu_x"}) == "gu_x"
    assert S.session_global_user_id({"global_user_id": "~"}) == ""
    assert S.session_global_user_id({}) == ""
    assert S.session_global_user_id(None) == ""


def test_live_attendant_is_found_by_the_md_field(tmp_path, monkeypatch):
    sid = "S-u-universal_bsq_session-p3"
    _md(tmp_path, sid, window="universal_bsq_session", gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {sid})
    assert S.live_user_conversation_sid(_cfg(tmp_path), "p", "gu_a") == sid


def test_live_attendant_from_before_the_rename_is_found_by_its_window(
        tmp_path, monkeypatch):
    """No `global_user_id` field at all — the pre-T-0964 shape on disk today."""
    sid = "S-u-gu_a-user-conversation-p4"
    _md(tmp_path, sid, window="gu_a-user-conversation")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {sid})
    assert S.live_user_conversation_sid(_cfg(tmp_path), "p", "gu_a") == sid


def test_another_users_attendant_is_not_returned(tmp_path, monkeypatch):
    """Both now share a window shape, so only the field can tell them apart —
    a miss here hands one person's conversation thread to another."""
    a = "S-u-universal_bsq_session-p3"
    b = "S-u-user_session_flomaster-p5"
    _md(tmp_path, a, window="universal_bsq_session", gid="gu_a")
    _md(tmp_path, b, window="user_session_flomaster", gid="gu_b")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a, b})
    assert S.live_user_conversation_sid(_cfg(tmp_path), "p", "gu_b") == b


def test_a_dead_attendant_is_not_returned(tmp_path, monkeypatch):
    sid = "S-u-universal_bsq_session-p3"
    _md(tmp_path, sid, window="universal_bsq_session", gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    assert S.live_user_conversation_sid(_cfg(tmp_path), "p", "gu_a") is None


# ---------------------------------------------------------------------------
# Which name a NEW attendant gets.
# ---------------------------------------------------------------------------

def test_first_attendant_gets_the_universal_name(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    assert S.user_facing_window(_cfg(tmp_path), "p", "gu_a") == "universal_bsq_session"


def test_the_id_is_never_rendered_into_the_name(tmp_path, monkeypatch):
    """The complaint, stated as an assertion."""
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    gid = "gu_dc8262b6cea9098d98e04d7e"
    assert gid not in S.user_facing_window(_cfg(tmp_path), "p", gid)


def test_a_second_user_gets_a_named_session(tmp_path, monkeypatch):
    a = "S-u-universal_bsq_session-p3"
    _md(tmp_path, a, window="universal_bsq_session", gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    (tmp_path / "_mothership").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_mothership" / "users.json").write_text(
        '{"users": [{"id": "gu_b", "username": "flomaster"}]}')
    assert S.user_facing_window(_cfg(tmp_path), "p", "gu_b") == "user_session_flomaster"


def test_an_unresolvable_second_user_gets_a_position_not_an_id(
        tmp_path, monkeypatch):
    """No users store ⟹ still never the gid."""
    a = "S-u-universal_bsq_session-p3"
    _md(tmp_path, a, window="universal_bsq_session", gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    win = S.user_facing_window(_cfg(tmp_path), "p", "gu_b")
    assert win == "user_session_2"
    assert "gu_b" not in win


def test_the_same_user_reconnecting_keeps_the_universal_name(tmp_path, monkeypatch):
    """A stale md for THIS gid must not push the user onto a `_2` name."""
    a = "S-u-universal_bsq_session-p3"
    _md(tmp_path, a, window="universal_bsq_session", gid="gu_a")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.user_facing_window(_cfg(tmp_path), "p", "gu_a") == "universal_bsq_session"


def test_the_users_own_PRE_RENAME_attendant_is_not_read_as_a_stranger(
        tmp_path, monkeypatch):
    """Measured on the live install, and it was wrong: filtering on the md
    field alone read the stakeholder's own window-keyed attendant (no
    `global_user_id` — the shape on disk today) as somebody else and offered
    him `user_session_alexey`. "Someone else's attendant" must mean the same
    thing here as it does in `live_user_conversation_sid`: BOTH matchers."""
    a = "S-u-gu_a-user-conversation-p3"
    _md(tmp_path, a, window="gu_a-user-conversation")  # no gid field
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {a})
    assert S.user_facing_window(_cfg(tmp_path), "p", "gu_a") == "universal_bsq_session"


def test_a_crafted_gid_is_still_rejected(tmp_path, monkeypatch):
    """The gid no longer reaches the window, but it still reaches a flock
    filename and a spawn command — the safe-segment check must not be dropped
    along with the window it used to guard."""
    from bot_squad_worker.actions import ActionError
    monkeypatch.setattr(S, "_live_agent_sids", lambda: set())
    for bad in ("", "gu a", "gu;rm", "../x", "a/b", "$(x)"):
        with pytest.raises(ActionError):
            S.user_facing_window(_cfg(tmp_path), "p", bad)
    # A trailing newline is NORMALISED rather than refused (the validator
    # strips before matching — pre-existing, and safe: nothing downstream sees
    # the newline). Asserted as the behaviour it is, not the one the T-0895
    # docstring reads like.
    assert "\n" not in S.user_facing_window(_cfg(tmp_path), "p", "gu_a\n")


def test_live_user_conversation_sids_counts_by_role_not_window(
        tmp_path, monkeypatch):
    """A session that MORPHED into user-conversation (the T-0932 budding half)
    is the user's session whatever its window still says."""
    dev = "S-u-dev_thing-p1"
    morphed = "S-u-some_old_window-p2"
    _md(tmp_path, dev, window="dev_thing")
    _md(tmp_path, morphed, window="some_old_window", role="user-conversation")
    monkeypatch.setattr(S, "_live_agent_sids", lambda: {dev, morphed})
    rows = S.live_user_conversation_sids(_cfg(tmp_path), "p")
    assert [r["sid"] for r in rows] == [morphed]


# ---------------------------------------------------------------------------
# The md-rebuild whitelist. This is the guard with teeth: `suspend` rebuilds
# the md from an explicit key list, so an unlisted field is dropped silently —
# exactly how `model` was lost until T-0678. Dropping THIS one un-keys a
# suspended attendant from its user, and the next message spawns a duplicate
# instead of resuming the conversation.
# ---------------------------------------------------------------------------

def test_suspend_preserves_the_global_user_id(tmp_path, monkeypatch):
    from types import SimpleNamespace

    sid = "S-u-universal_bsq_session-p3"
    md = _md(tmp_path, sid, window="universal_bsq_session", gid="gu_a",
             claude_uuid="uuid-1", status="active", started_at="2026-09-06T00:00:00Z")
    cfg = SimpleNamespace(data_dir=tmp_path,
                          projects={"p": SimpleNamespace(repo_path=str(tmp_path))})
    pane = S.PaneInfo(pane_id="%3", window="universal_bsq_session", pid=1,
                      cwd=str(tmp_path), command="claude", session="p-29")
    monkeypatch.setattr(S, "list_panes", lambda: [pane])
    monkeypatch.setattr(S, "compute_sid", lambda *a, **k: sid)
    monkeypatch.setattr(S, "_run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(S, "_get_current_user", lambda: "u")
    monkeypatch.setattr(S, "_get_user_home", lambda: str(tmp_path))

    S.suspend(cfg, "p", sid)

    meta = S._read_session_metadata(md)
    assert S.session_global_user_id(meta) == "gu_a", (
        "suspend's whitelist dropped global_user_id — a suspended attendant is "
        "now un-keyed from its user")


# ---------------------------------------------------------------------------
# uc_redrive: "did the attendant answer" must recognise BOTH eras, or it
# re-drives a conversation that was answered.
# ---------------------------------------------------------------------------

def test_attendant_lineage_spans_both_naming_eras(tmp_path):
    from bot_squad_worker import uc_redrive as U

    new_sid = "S-u-universal_bsq_session-p3"
    _md(tmp_path, new_sid, window="universal_bsq_session", gid="gu_a")
    wins = U.attendant_windows(_cfg(tmp_path), "p", "gu_a")
    assert "universal_bsq_session" in wins       # this era
    assert "gu_a-user-conversation" in wins      # the retired one


def test_a_post_rename_attendants_reply_counts_as_an_answer(tmp_path):
    from bot_squad_worker import uc_redrive as U

    sid = "S-u-universal_bsq_session-p3"
    _md(tmp_path, sid, window="universal_bsq_session", gid="gu_a")
    wins = U.attendant_windows(_cfg(tmp_path), "p", "gu_a")
    assert U.is_attendant_answer(f"session:{sid}", wins) is True
    # …and someone else's session still does not.
    assert U.is_attendant_answer("session:S-u-dev_thing-p9", wins) is False


def test_a_pre_rename_attendants_reply_still_counts(tmp_path):
    from bot_squad_worker import uc_redrive as U

    wins = U.attendant_windows(_cfg(tmp_path), "p", "gu_a")
    assert U.is_attendant_answer(
        "session:S-u-gu_a-user-conversation-p4", wins) is True


def test_a_gid_no_attendant_could_have_has_no_lineage(tmp_path):
    from bot_squad_worker import uc_redrive as U
    assert U.attendant_windows(_cfg(tmp_path), "p", "not a gid") == set()


# ---------------------------------------------------------------------------
# The root session must never be recyclable. Its ROLE already exempts it, but
# `user_session_exempt`'s window arm exists as an INDEPENDENT belt for a caller
# holding a window and no derived role — and that belt is exactly what T-0616
# added after the role-only check let `user-session-p8` ride the full
# idle_timeout recycle path. Leaving the root out of it reopens that hole under
# a new name.
# ---------------------------------------------------------------------------

def test_the_root_session_is_recycle_exempt_by_WINDOW_alone():
    from bot_squad_worker.recycle_gate import user_session_exempt
    for win in ("universal_bsq_session", "universal-bsq-session",
                "user_session", "user_session_flomaster", "user-session-2"):
        assert user_session_exempt(None, win, None) is True, win


def test_the_window_belt_stays_narrow():
    from bot_squad_worker.recycle_gate import user_session_exempt
    for win in ("user-sessions", "user-feedback", "dev_thing", "operator"):
        assert user_session_exempt(None, win, None) is False, win


def test_the_root_session_is_also_exempt_by_its_derived_role():
    from bot_squad_worker.recycle_gate import user_session_exempt
    role = S._derive_role("universal_bsq_session", None, None)
    assert user_session_exempt(role, None, None) is True
