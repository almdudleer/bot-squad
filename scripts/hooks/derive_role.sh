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
#   _TL_WINDOW_RE       = (?:^|[-_])(?:tl|teamlead)$
#     1. operator window marker (operator, <x>-operator)        → operator
#     2. prod-TL window marker (prod-tl, <x>_prod_teamlead, …)  → prod-teamlead
#     3. qa window marker (qa, <x>-qa)                          → qa
#     4. teamlead/tl window marker (<x>-TL, <x>_teamlead, …)    → teamlead
#     5. default — incl. task-less / marker-less sessions       → dev
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
        operator|*[-_]operator)
            printf 'operator' ;;
        prod-tl|prod_tl|prod-teamlead|prod_teamlead|*[-_]prod-tl|*[-_]prod_tl|*[-_]prod-teamlead|*[-_]prod_teamlead)
            printf 'prod-teamlead' ;;
        qa|*[-_]qa)
            printf 'qa' ;;
        tl|teamlead|*[-_]tl|*[-_]teamlead)
            printf 'teamlead' ;;
        *)
            printf 'dev' ;;
    esac
}
