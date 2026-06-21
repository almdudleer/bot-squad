#!/usr/bin/env bash
# T-0433 P1 — provision the optional voice-intake STT backend into the worker venv.
#
# The [voice] extra (faster-whisper) is NOT a core worker dependency and is NOT
# installed by the base install or synced by a deploy (the worker is a host
# systemd venv, not docker — a deploy syncs CODE + restarts, but never re-pip's
# the venv). So a host that wants voice intake runs THIS once (and again after a
# faster-whisper version bump). Idempotent.
#
# It (a) installs `worker[voice]` into the worker venv and (b) PREFETCHES the
# Whisper model so the first real voice note isn't a cold ~464M download
# mid-conversation, and so HF-reachability fails LOUDLY here at provision time,
# not silently at first voice. (HuggingFace is reachable without the TG proxy;
# only api.telegram.org is DPI-blocked on this host.)
#
# Usage:
#   scripts/install/provision-voice.sh [INSTALL_DIR] [MODEL]
# Env overrides: BOTSQUAD_INSTALL_DIR, VOICE_MODEL (default: small).
set -euo pipefail

INSTALL_DIR="${1:-${BOTSQUAD_INSTALL_DIR:-/home/www/bot-squad}}"
MODEL="${2:-${VOICE_MODEL:-small}}"
VENV="${INSTALL_DIR}/worker/.venv"
PIP="${VENV}/bin/pip"
PY="${VENV}/bin/python"

echo "[provision-voice] install dir : ${INSTALL_DIR}"
echo "[provision-voice] worker venv : ${VENV}"
echo "[provision-voice] model       : ${MODEL}"

if [ ! -x "${PIP}" ]; then
    echo "[provision-voice] FATAL: no worker venv at ${VENV} (run the installer first)." >&2
    exit 2
fi

echo "[provision-voice] installing worker[voice] (faster-whisper)…"
"${PIP}" install -e "${INSTALL_DIR}/worker[voice]"

echo "[provision-voice] prefetching Whisper model '${MODEL}' (downloads from HuggingFace once)…"
"${PY}" - "${MODEL}" <<'PYEOF'
import sys
from faster_whisper import WhisperModel
model = sys.argv[1]
# CPU int8 matches transcribe._faster_whisper; this both DOWNLOADS the weights and
# proves they load — failing loudly here if HF is unreachable or the model name
# is wrong, instead of silently at the first voice note.
WhisperModel(model, device="cpu", compute_type="int8")
print(f"[provision-voice] model '{model}' loaded OK")
PYEOF

echo "[provision-voice] DONE. Set [voice].enabled = true in system_settings.toml and"
echo "[provision-voice] restart the worker:  systemctl --user restart bot-squad-worker"
