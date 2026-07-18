#!/usr/bin/env bash
# Focused test for the seed_claude_settings checkpoint (T-0541).
#
# Exercises the install-bring-up step that seeds the install's OWN bot-squad
# clone `.claude/settings.json` with the three lifecycle hooks, reusing the
# scaffolder's `_seed_claude`/`_render_claude_settings` (api/app/project_scaffold.py).
# Sources the REAL install.sh (main suppressed) and drives the real step against
# a temp clone carrying the real seeder module + stub hook scripts, asserting:
#   - settings.json is written with the 3 hooks pointing at
#     <clone>/scripts/hooks/*.sh and env.BOT_SQUAD == <clone>;
#   - non-clobbering: a pre-existing settings.json is preserved;
#   - idempotent: a second run succeeds and leaves content unchanged;
#   - the per-clone git-exclude gains /.claude/.
#
# Host-side (no docker): needs bash, git, python3, jq — all present on the CI
# runner. Runnable on the dev box too. Cleans up on exit.
#
# SOURCE/provenance: T-0541 (scaffold install-level .claude/settings.json hook
#   registration for fresh multi-server installs).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"       # scripts/install/tests -> repo root
INSTALL_SH="$HERE/../install.sh"
SCAFFOLD_SRC="$REPO/api/app/project_scaffold.py"
[[ -f "$INSTALL_SH" ]]   || { echo "FAIL: $INSTALL_SH not found"; exit 1; }
[[ -f "$SCAFFOLD_SRC" ]] || { echo "FAIL: $SCAFFOLD_SRC not found"; exit 1; }
command -v python3 >/dev/null || { echo "FAIL: python3 required"; exit 2; }
command -v jq >/dev/null      || { echo "FAIL: jq required"; exit 2; }

PASS=0; FAIL=0
ok()  { echo "  ok: $*"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $*" >&2; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- build a fake install clone ----------------------------------------------
CLONE="$WORK/install"
mkdir -p "$CLONE/api/app" "$CLONE/scripts/hooks" "$CLONE/data"
cp "$SCAFFOLD_SRC" "$CLONE/api/app/project_scaffold.py"
for h in session_start.sh user_prompt_submit.sh stop.sh; do
  printf '#!/usr/bin/env bash\n: # stub hook for the seeder test\n' > "$CLONE/scripts/hooks/$h"
  chmod +x "$CLONE/scripts/hooks/$h"
done
# git init so _git_ignore_local has a real .git/info/exclude to write.
git -C "$CLONE" init -q

# --- source install.sh with main() suppressed (smoke_engine.sh trick) --------
export BOTSQUAD_INSTALL_DIR="$CLONE"
export BOTSQUAD_STATE_DIR="$WORK/state"
export BOTSQUAD_SKIP_MOTHERSHIP=1
export BOTSQUAD_NONINTERACTIVE=1
TMP_SH="$WORK/install.nomain.sh"
sed 's/^main "\$@"$/# main "$@"  # sourced, main suppressed/' "$INSTALL_SH" > "$TMP_SH"
# shellcheck disable=SC1090
source "$TMP_SH"
# install.sh sets `set -Eeuo pipefail` + a global `trap on_err ERR` at source
# time; drop both so this harness's own if/PASS/FAIL logic governs (section 3
# deliberately drives the step's failure path).
set +e
trap - ERR

SETTINGS="$CLONE/.claude/settings.json"

# step invocations run in a subshell: the step's die_struct helper calls
# `exit 1` on failure, which would otherwise terminate this whole harness
# (even inside an `if`). A subshell contains that exit; the settings.json
# write still lands on the real filesystem.
run_step() { ( step_seed_claude_settings ) >/dev/null 2>&1; }

# --- 1. fresh seed ------------------------------------------------------------
if run_step; then
  ok "step_seed_claude_settings exited 0 on a fresh clone"
else
  bad "step_seed_claude_settings failed on a fresh clone"
fi
[[ -f "$SETTINGS" ]] && ok "settings.json created at $SETTINGS" || bad "settings.json NOT created"

if [[ -f "$SETTINGS" ]]; then
  for pair in "SessionStart:session_start.sh" "UserPromptSubmit:user_prompt_submit.sh" "Stop:stop.sh"; do
    event="${pair%%:*}"; script="${pair##*:}"
    got="$(jq -r --arg e "$event" '.hooks[$e][0].hooks[0].command' "$SETTINGS" 2>/dev/null)"
    want="$CLONE/scripts/hooks/$script"
    [[ "$got" = "$want" ]] && ok "$event hook -> $want" || bad "$event hook = '$got' (want '$want')"
  done
  got_bs="$(jq -r '.env.BOT_SQUAD' "$SETTINGS" 2>/dev/null)"
  [[ "$got_bs" = "$CLONE" ]] && ok "env.BOT_SQUAD pinned to install root" || bad "env.BOT_SQUAD='$got_bs' (want '$CLONE')"
  # git-exclude entry present.
  grep -qxF '/.claude/' "$CLONE/.git/info/exclude" 2>/dev/null \
    && ok "/.claude/ added to .git/info/exclude" || bad "/.claude/ missing from git exclude"
fi

# --- 2. non-clobbering + idempotent ------------------------------------------
SENTINEL='{"kept":"by-user"}'
printf '%s\n' "$SENTINEL" > "$SETTINGS"
if run_step; then
  ok "rerun over an existing settings.json exited 0"
else
  bad "rerun over an existing settings.json failed"
fi
[[ "$(cat "$SETTINGS")" = "$SENTINEL" ]] \
  && ok "non-clobbering: existing settings.json preserved verbatim" \
  || bad "existing settings.json was overwritten (should be preserved)"

# --- 3. missing seeder module -> hard fail with guidance ---------------------
rm -f "$CLONE/api/app/project_scaffold.py"
rm -f "$SETTINGS"
if run_step; then
  bad "expected failure when project_scaffold.py is absent, but step exited 0"
else
  ok "missing seeder module -> step fails (incomplete-clone guard)"
fi

echo ""
echo "================ RESULT: $PASS passed, $FAIL failed ================"
[[ "$FAIL" -eq 0 ]] && echo "SEED_CLAUDE_SETTINGS: GREEN" || echo "SEED_CLAUDE_SETTINGS: RED"
exit "$FAIL"
