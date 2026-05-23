#!/usr/bin/env bash
# bot-squad per-user worker enablement — one-shot for an attached Linux user.
#
# Context: T-0067 picks the per-user systemd-user worker shape. The system-
# wide unit lives at /etc/systemd/user/bot-squad-user-worker.service (dropped
# by install.sh:step_per_user_worker_unit). What we still need per-attached-
# user is:
#
#   1. loginctl enable-linger <USER>  — so the worker survives logout
#      (the user has no session at /run/user/<uid> otherwise).
#   2. systemctl --user daemon-reload + enable --now — actually start it.
#
# Step 1 needs sudo (loginctl); step 2 does NOT. Both are run AS THE USER
# THEMSELVES on first login — there's no install-owner privilege transfer.
#
# Idempotent: re-running on an already-enabled account is a no-op.
#
# Exit codes:
#   0 — worker socket is up
#   1 — anything went wrong; the script prints what + how to fix.

set -Eeuo pipefail

INSTALL_DIR="${BOTSQUAD_INSTALL_DIR:-/home/www/bot-squad}"
UNIT="bot-squad-user-worker.service"
USER_NAME="$(id -un)"
USER_UID="$(id -u)"
SOCK_PATH="${INSTALL_DIR}/data/_sock/user-${USER_NAME}.sock"
SYSTEM_UNIT_PATH="/etc/systemd/user/${UNIT}"

log()  { printf '\033[36m[per-user-worker]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m[per-user-worker]\033[0m %s\n' "$*" >&2; }
die()  {
  printf '\n[per-user-worker] FAILED: %s\n' "$1" >&2
  printf '[per-user-worker] How to fix: %s\n' "$2" >&2
  exit 1
}

# --- preflight ---------------------------------------------------------------

[[ -f "$SYSTEM_UNIT_PATH" ]] || die \
  "system-wide unit $SYSTEM_UNIT_PATH not found." \
  "Re-run the bot-squad installer on this host so step_per_user_worker_unit
drops the unit into /etc/systemd/user/."

command -v systemctl >/dev/null 2>&1 || die \
  "systemctl is not on PATH." \
  "Install systemd (this host is probably non-systemd; T-0055/T-0056 track
Alpine/NixOS support — out of scope today)."

command -v loginctl >/dev/null 2>&1 || die \
  "loginctl is not on PATH." \
  "Install systemd's loginctl (typically part of the systemd package)."

# --- 1. enable-linger (needs sudo) -------------------------------------------

if loginctl show-user "$USER_NAME" 2>/dev/null | grep -qxF "Linger=yes"; then
  log "linger already enabled for $USER_NAME"
else
  log "enabling linger for $USER_NAME (you may be prompted for your sudo password)"
  if ! sudo loginctl enable-linger "$USER_NAME"; then
    die "sudo loginctl enable-linger failed for $USER_NAME." \
        "Make sure your account has sudo (or ask root to run
'sudo loginctl enable-linger $USER_NAME' once)."
  fi
fi

# --- 2. user systemctl daemon-reload + enable --now --------------------------

# When running this script under su/sudo, XDG_RUNTIME_DIR may not be set. Set
# it explicitly so systemctl --user finds the right user-bus.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${USER_UID}}"

if [[ ! -d "$XDG_RUNTIME_DIR" ]]; then
  # The runtime dir is created when the user logs in (or when linger kicks
  # in for the first time). Linger was just enabled; give systemd a moment.
  for _ in 1 2 3 4 5; do
    [[ -d "$XDG_RUNTIME_DIR" ]] && break
    sleep 1
  done
fi
[[ -d "$XDG_RUNTIME_DIR" ]] || die \
  "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR does not exist for $USER_NAME." \
  "Log out and back in once (or open a fresh ssh session as $USER_NAME) so
systemd creates the user runtime dir, then re-run this script."

if ! systemctl --user daemon-reload; then
  die "systemctl --user daemon-reload failed." \
      "Inspect 'journalctl --user -n 50' for the underlying cause."
fi

if ! systemctl --user enable --now "$UNIT"; then
  die "systemctl --user enable --now $UNIT failed." \
      "Inspect 'systemctl --user status $UNIT' and 'journalctl --user -u $UNIT
-n 50' for the underlying cause. Common issues: worker venv missing at
${INSTALL_DIR}/worker/.venv (re-run the installer to provision it), or
${INSTALL_DIR}/config not readable by $USER_NAME (membership in the 'www'
group fixes it)."
fi

# --- 3. verify the socket is up ----------------------------------------------

for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ -S "$SOCK_PATH" ]] && break
  sleep 1
done

if [[ -S "$SOCK_PATH" ]]; then
  log "OK — $SOCK_PATH is up"
  ls -l "$SOCK_PATH" >&2 || true
  exit 0
fi

die "worker started but $SOCK_PATH never appeared." \
    "Inspect 'systemctl --user status $UNIT' and
'journalctl --user -u $UNIT -n 100' to see why the worker exited or
which socket path it actually bound to."
