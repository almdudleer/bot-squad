#!/usr/bin/env bash
# test_derive_role.sh — regression test for the bash role-derivation mirror.
#
# Asserts that scripts/hooks/derive_role.sh::bsq_derive_role agrees with the
# canonical python worker bot_squad_worker.sessions._derive_role for a matrix of
# window names (T-0041). The hook's role-doc selection rides on this function;
# any drift between the two languages would resurface the "operator labelled
# TEAMLEAD" bug, so the test fails loudly on mismatch.
#
# Run: bash scripts/hooks/test_derive_role.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
. "$HERE/derive_role.sh"

# Window matrix: realistic markers + edge cases (false-positive suffixes,
# case-insensitivity). Empty window is asserted separately (bash assoc arrays
# can't key on ""). Format: "window<TAB>expected".
CASES=(
    "bot-squad-operator	operator"
    "operator	operator"
    "OPERATOR	operator"
    "multi_server-TL	teamlead"
    "signal-tracker_teamlead	teamlead"
    "tl	teamlead"
    "heatmaps	dev"
    "adhoc-notes	dev"
    "operator-as-distinct-role-not-teamlead	teamlead"  # -teamlead suffix ⟹ teamlead (shared FP)
    "xoperator	dev"                                      # no separator ⟹ dev
    "total	dev"                                          # ends in 'al', not a tl marker ⟹ dev
)

fail=0
WINDOWS=()

# 1. bash function matches the documented contract.
for case in "${CASES[@]}"; do
    w="${case%%	*}"; want="${case##*	}"
    WINDOWS+=("$w")
    got="$(bsq_derive_role "$w")"
    if [ "$got" != "$want" ]; then
        printf 'FAIL [contract] window=%-42q bash=%-9s want=%s\n' "$w" "$got" "$want"
        fail=1
    fi
done

# empty window ⟹ dev (separate assertion — can't key an assoc array on "")
if [ "$(bsq_derive_role "")" != "dev" ]; then
    printf 'FAIL [contract] window=(empty) bash=%s want=dev\n' "$(bsq_derive_role "")"
    fail=1
fi

# 2. bash function agrees with the canonical python _derive_role (byte-for-
#    behavior — the whole point of the mirror). Skipped only if python/worker
#    is unimportable in this environment.
if py_out="$(cd "$REPO/worker" && python3 - "${WINDOWS[@]}" <<'PY' 2>/dev/null
import sys
from bot_squad_worker.sessions import _derive_role
for w in sys.argv[1:]:
    print(f"{w}\t{_derive_role(w, None, None)}")
PY
)"; then
    while IFS=$'\t' read -r w role; do
        bash_role="$(bsq_derive_role "$w")"
        if [ "$bash_role" != "$role" ]; then
            printf 'FAIL [drift] window=%-42q bash=%-9s python=%s\n' "$w" "$bash_role" "$role"
            fail=1
        fi
    done <<< "$py_out"
else
    echo "WARN: could not import python _derive_role — skipped cross-language drift check"
fi

if [ "$fail" -eq 0 ]; then
    echo "PASS: bsq_derive_role matches contract + python _derive_role ($((${#WINDOWS[@]})) windows)"
fi
exit "$fail"
