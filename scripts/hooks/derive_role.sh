#!/usr/bin/env bash
# derive_role.sh — bash mirror of bot_squad_worker.sessions._derive_role.
#
# Maps a session's WINDOW NAME → role enum: operator | teamlead | dev.
#
# This is the bash half of a deliberate two-language mirror; the python half in
# worker/bot_squad_worker/sessions.py::_derive_role is the canonical reference
# (same mirror discipline as the T-0075 frontmatter pair and the idalloc pair).
# KEEP THEM IN SYNC: any change to the marker precedence here MUST be made in
# _derive_role and vice-versa. scripts/hooks/test_derive_role.sh cross-checks
# this function against the live python _derive_role and fails on any drift.
#
# Precedence (first match wins), case-insensitive — mirrors the python regexes
#   _OPERATOR_WINDOW_RE = (?:^|[-_])operator$
#   _PROD_TL_WINDOW_RE  = (?:^|[-_])prod[-_](?:tl|teamlead)$
#   _QA_WINDOW_RE       = (?:^|[-_])qa$
#   _USERCONV_WINDOW_RE = (?:^|[-_])user[-_]conversation$
#   _TL_WINDOW_RE       = (?:^|[-_])(?:tl|teamlead)$
#   _DEV_WINDOW_RE      = ^dev[-_]                                     (T-0964)
#   _UNIVERSAL_WINDOW_RE    = ^universal[-_]bsq[-_]session(?:[-_]|$)   (T-0964)
#   _USER_SESSION_WINDOW_RE = (?:^|[-_])user[-_]session(?:$|[-_])      (T-0964)
#     0. explicit dev_ / dev- PREFIX (dev_add_ui_button)        → dev
#     1. operator window marker (operator, <x>-operator)        → operator
#     2. prod-TL window marker (prod-tl, <x>_prod_teamlead, …)  → prod-teamlead
#     3. qa window marker (qa, <x>-qa)                          → qa
#     4. user-conversation: the legacy <gu_id>-user-conversation
#        PLUS the T-0964 user-facing names universal_bsq_session
#        and user_session[_<who>]                               → user-conversation
#     5. teamlead/tl window marker (<x>-TL, <x>_teamlead, …)    → teamlead
#     6. default — incl. task-less / marker-less sessions       → dev
#
# T-0964: the dev_ PREFIX arm is first and authoritative, so a feature slug that
# happens to end in another role's marker (dev_move_the_qa) stays a dev — that
# is the point of naming a bud for its role.
#
# The prod-TL arm MUST precede the plain-TL arm (T-0197): a `…-prod-tl` window
# also ends in `tl`, so plain-TL precedence would otherwise swallow it. A
# `prod-ops-tl` window has no `prod` adjacent to the trailing `-tl` → stays
# teamlead.
#
# task_id / initiative deliberately DO NOT influence the role (T-0175): a
# task-less, marker-less session is a dev (most often a dev that finished its
# task), not a teamlead. This replaces the old SessionStart heuristic
# (task-less ⟹ teamlead) that mislabelled operator sessions (T-0041).
bsq_derive_role() {
    local w_lc
    w_lc="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
    case "$w_lc" in
        dev-*|dev_*)
            printf 'dev' ;;
        operator|*[-_]operator)
            printf 'operator' ;;
        prod-tl|prod_tl|prod-teamlead|prod_teamlead|*[-_]prod-tl|*[-_]prod_tl|*[-_]prod-teamlead|*[-_]prod_teamlead)
            printf 'prod-teamlead' ;;
        qa|*[-_]qa)
            printf 'qa' ;;
        user-conversation|user_conversation|*[-_]user-conversation|*[-_]user_conversation)
            printf 'user-conversation' ;;
        universal-bsq-session|universal_bsq_session|universal-bsq-session[-_]*|universal_bsq_session[-_]*)
            printf 'user-conversation' ;;
        user-session|user_session|*[-_]user-session|*[-_]user_session|user-session[-_]*|user_session[-_]*|*[-_]user-session[-_]*|*[-_]user_session[-_]*)
            printf 'user-conversation' ;;
        tl|teamlead|*[-_]tl|*[-_]teamlead)
            printf 'teamlead' ;;
        *)
            printf 'dev' ;;
    esac
}
