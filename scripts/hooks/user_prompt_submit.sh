#!/usr/bin/env bash
# UserPromptSubmit hook — record that the user sent a prompt.
# Creates/updates .claude/last_user_prompt_ts in the current working directory.
# Claude Code runs hooks with cwd = project root, so this lands in the right place.
set -uo pipefail
mkdir -p .claude
touch .claude/last_user_prompt_ts
exit 0
