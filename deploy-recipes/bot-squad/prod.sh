#!/usr/bin/env bash
# bot-squad → prod (mothership-only): cut a release artifact for attached servers.
#
# T-0081 (update-delivery Chapter II). Sibling of staging.sh:
#   - staging.sh redeploys THIS install (the mothership).
#   - prod.sh produces a versioned tarball + manifest entry that OTHER
#     attached servers pull via the release-feed API (T-0082) and apply
#     via the consumer poller/apply (T-0083/T-0084).
#
# Invocation: launched by the worker deploy queue (worker/bot_squad_worker/deploy.py)
# with cwd = repo_for_target("prod") = the master clone
# (config/projects.toml: projects.bot-squad.repo_master).
#
# Self-check refuses to run on a non-mothership install (defends against an
# attached server accidentally cutting its own release line — see T-0086).
#
# Idempotency: a second same-day invocation produces .2, .3, ... A re-run on
# an already-cut version is rejected by the local tag check before any state
# changes.
#
# Test override env vars (smoke-only — never set in real deploys):
#   BOT_SQUAD               install root (default /home/www/bot-squad)
#   BOTSQUAD_PROD_SKIP_PUSH if "1", skip `git push origin <version>` (no remote)
#   BOTSQUAD_PROD_VERSION   override the computed vYYYY.MM.DD.N tag verbatim
set -euo pipefail

REPO="$(pwd)"
INSTALL_DIR="${BOT_SQUAD:-/home/www/bot-squad}"
MASTER_BRANCH="master"
PROJECTS_TOML="$INSTALL_DIR/config/projects.toml"
RELEASES_DIR="$INSTALL_DIR/data/bot-squad/releases"
MANIFEST="$RELEASES_DIR/index.json"

echo "[bot-squad/prod] master clone: $REPO"
echo "[bot-squad/prod] install dir:  $INSTALL_DIR"

# ---------------------------------------------------------------------------
# 0. Mothership self-check.
#
# Detection: `mothership = true` under [projects.bot-squad] in projects.toml.
# The flag is owned by T-0086; if it's absent we refuse rather than guess —
# the wrong call here is "consumer server cuts its own release line" which
# corrupts the artifact stream for every attached server.
# ---------------------------------------------------------------------------
if [ ! -f "$PROJECTS_TOML" ]; then
    echo "[bot-squad/prod] FATAL: projects.toml not found: $PROJECTS_TOML" >&2
    exit 2
fi
MOTHERSHIP=$(python3 - "$PROJECTS_TOML" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    data = tomllib.load(f)
flag = data.get("projects", {}).get("bot-squad", {}).get("mothership")
print("true" if flag is True else "false" if flag is False else "absent")
PY
)
case "$MOTHERSHIP" in
    true)
        echo "[bot-squad/prod] mothership self-check: OK"
        ;;
    false)
        echo "[bot-squad/prod] FATAL: projects.bot-squad.mothership = false — this install is a consumer, not the release source" >&2
        exit 2
        ;;
    absent)
        echo "[bot-squad/prod] FATAL: projects.bot-squad.mothership is unset — T-0086 must land the flag before prod.sh is runnable" >&2
        exit 2
        ;;
    *)
        echo "[bot-squad/prod] FATAL: unexpected mothership value: '$MOTHERSHIP'" >&2
        exit 2
        ;;
esac

# ---------------------------------------------------------------------------
# 1. Branch + cleanliness checks on the master clone.
# ---------------------------------------------------------------------------
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "$MASTER_BRANCH" ]; then
    echo "[bot-squad/prod] FATAL: expected master clone on '$MASTER_BRANCH', got '$BRANCH' (cwd=$REPO)" >&2
    exit 3
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "[bot-squad/prod] FATAL: master clone tree is dirty — refusing to release" >&2
    git status --short >&2
    exit 4
fi

# ---------------------------------------------------------------------------
# 2. Compute version. Format: vYYYY.MM.DD.N (UTC date + same-day counter).
#    N starts at 1; bumps if index.json already has entries for today.
# ---------------------------------------------------------------------------
mkdir -p "$RELEASES_DIR"

if [ -n "${BOTSQUAD_PROD_VERSION:-}" ]; then
    VERSION="$BOTSQUAD_PROD_VERSION"
    echo "[bot-squad/prod] version override: $VERSION"
else
    DATE_TAG=$(date -u +%Y.%m.%d)
    NEXT_N=$(python3 - "$MANIFEST" "$DATE_TAG" <<'PY'
import json, sys, pathlib
manifest_path = pathlib.Path(sys.argv[1])
date_tag = sys.argv[2]
prefix = f"v{date_tag}."
n = 0
if manifest_path.exists():
    data = json.loads(manifest_path.read_text())
    for entry in data.get("releases", []):
        v = entry.get("version", "")
        if v.startswith(prefix):
            try:
                n = max(n, int(v[len(prefix):]))
            except ValueError:
                pass
print(n + 1)
PY
    )
    VERSION="v${DATE_TAG}.${NEXT_N}"
    echo "[bot-squad/prod] next version: $VERSION"
fi

# Belt-and-braces: refuse if the tag already exists locally. This catches a
# partial-failure replay (tag created, manifest write crashed) where the
# manifest-derived counter would happily collide.
if git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null; then
    echo "[bot-squad/prod] FATAL: tag $VERSION already exists locally — delete it and re-run, or set BOTSQUAD_PROD_VERSION to a fresh tag" >&2
    exit 5
fi

GIT_SHA=$(git rev-parse HEAD)

# ---------------------------------------------------------------------------
# 3. Tag the master HEAD and push to origin.
# ---------------------------------------------------------------------------
git tag "$VERSION"
if [ "${BOTSQUAD_PROD_SKIP_PUSH:-0}" = "1" ]; then
    echo "[bot-squad/prod] BOTSQUAD_PROD_SKIP_PUSH=1 — skipping git push (smoke-test mode)"
else
    git push origin "$VERSION"
fi

# ---------------------------------------------------------------------------
# 4. Archive the tag to a tarball (write to a .tmp first so a crash mid-write
#    can't leave a half-tarball that matches the eventual sha256 entry).
# ---------------------------------------------------------------------------
TARBALL_REL="data/bot-squad/releases/${VERSION}.tar.gz"
TARBALL_ABS="$INSTALL_DIR/${TARBALL_REL}"
TARBALL_TMP="${TARBALL_ABS}.tmp"
git archive --format=tar.gz -o "$TARBALL_TMP" "$VERSION"
mv "$TARBALL_TMP" "$TARBALL_ABS"

# ---------------------------------------------------------------------------
# 5. Compute sha256, gather notes, append manifest entry atomically.
#    Notes = body of data/bot-squad/releases/<version>.md if present, else "".
# ---------------------------------------------------------------------------
SHA256=$(sha256sum "$TARBALL_ABS" | awk '{print $1}')
CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

NOTES_FILE="$RELEASES_DIR/${VERSION}.md"
if [ -f "$NOTES_FILE" ]; then
    NOTES_PATH="$NOTES_FILE"
else
    NOTES_PATH=""
fi

python3 - "$MANIFEST" "$VERSION" "$GIT_SHA" "$CREATED_AT" "$TARBALL_REL" "$SHA256" "$NOTES_PATH" <<'PY'
import json, os, pathlib, sys, tempfile
manifest_path, version, git_sha, created_at, tarball_rel, sha256, notes_path = sys.argv[1:8]
notes = pathlib.Path(notes_path).read_text() if notes_path else ""
m = pathlib.Path(manifest_path)
data = json.loads(m.read_text()) if m.exists() else {"current": None, "releases": []}
# Refuse to silently overwrite an existing entry for the same version —
# tarball check above should have caught this, but make the failure mode
# explicit at the manifest layer too.
for entry in data.get("releases", []):
    if entry.get("version") == version:
        raise SystemExit(f"manifest already contains entry for {version}")
data.setdefault("releases", []).append({
    "version": version,
    "git_sha": git_sha,
    "created_at": created_at,
    "tarball_path": tarball_rel,
    "sha256": sha256,
    "notes": notes,
})
data["current"] = version
fd, tmp = tempfile.mkstemp(dir=str(m.parent), prefix=".index.", suffix=".json")
with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, m)
PY

echo "[bot-squad/prod] release cut: $VERSION (git_sha=$GIT_SHA, sha256=$SHA256)"
echo "[bot-squad/prod] tarball:  $TARBALL_ABS"
echo "[bot-squad/prod] manifest: $MANIFEST"
