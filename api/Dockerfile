# Multi-stage: build web, then assemble api with web/dist as static.

# T-1004: DIGEST, NOT TAG. `node:20-alpine` is a mutable pointer on a
# registry we do not control — the same rebuild-moves-the-tag defect as
# bot-squad-api:latest (T-1001), one layer down and somewhere we cannot
# even retain the outgoing artefact. Digest measured 2026-09-06 as the one
# this image was actually built from, so pinning it moves nothing today.
FROM node:20-alpine@sha256:f598378b5240225e6beab68fa9f356db1fb8efe55173e6d4d8153113bb8f333c AS web-builder
ARG VITE_MOTHERSHIP=0
ENV VITE_MOTHERSHIP=${VITE_MOTHERSHIP}
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# Mothership add-server wizard ?raw-imports the canonical chat-agent
# install prompt via the `@install/...` vite alias (T-0050, see
# web/vite.config.ts). The alias resolves relative to vite.config.ts, so
# the file has to live at /scripts/install/ — sibling to /web — for the
# docker build to match local-dev layout.
COPY scripts/install /scripts/install
# T-0776: `npm run build` is `tsc -b && vite build`, and web/tsconfig.json
# includes ALL of src/ — test files too. apiShapeContract.test.ts (T-0764)
# imports the response-shape pin as `../../api/response_shapes.json`, which
# from /web/src resolves to /api/ — so the pin has to live there, sibling to
# /web, for the same reason scripts/install does above. Without it tsc fails
# TS2307 and the whole deploy dies at the build, which is exactly what
# happened on the 62d8073 batch. The vitest run never catches this: outside
# docker the real api/ is simply there.
COPY api/response_shapes.json /api/response_shapes.json
RUN npm run build

# T-1004: DIGEST, NOT TAG — see the web-builder stage above. Measured
# 2026-09-06 as the base the running image was built from.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS api
WORKDIR /app
# `git` is needed by app/project_scaffold.py (T-0051) for the
# `new_from_scratch` project-create mode — it runs `git init` /
# `git clone` to seed the dev and master clones. The
# `paths_as_they_are` mode doesn't need git, but we keep it always
# present so a host with no git on PATH can't degrade the wizard
# silently.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

# T-1004 — INSTALL FROM THE LOCK, THEN PROVE THE RESULT MATCHES IT.
#
# The old line was `pip install -e ".[dev]"` against 13 `>=` floors with no
# lockfile, so the image was not a function of the ref: two builds of the same
# commit on two days resolved whatever PyPI served each day. Measured
# consequence, not theory — an api suite result that changed because a test
# split MOVED between fastapi versions.
#
# The `|| pip install .` fallback is gone deliberately. A fallback that installs
# a DIFFERENT dependency set when the primary fails is how a build reports
# success while producing an environment nobody asked for; with a lock, failing
# loudly is the whole point.
#
# The diff at the end is the control that makes this a pin rather than a
# comment: if pip resolves ANYTHING the lock does not name — a transitive that
# moved, a marker that evaluated differently, a stale layer — the build FAILS
# HERE instead of shipping an image whose contents nobody compared. Names are
# lowercased and underscores normalised because pip freeze and PyPI disagree on
# both (pydantic_core vs pydantic-core, PyYAML vs pyyaml).
COPY api/pyproject.toml api/requirements.lock ./
# T-1004/R-0010: scratch paths honour TMPDIR (unset -> /tmp, unchanged image
# behaviour) so this SAME body can be executed on the host by the test suite
# without two concurrent executions overwriting each other's scratch files.
# `RUN` is not Dockerfile-substituted — the build shell expands ${TMPDIR:-/tmp}.
RUN pip install --no-cache-dir -r requirements.lock \
 && pip install --no-cache-dir --no-deps -e . \
 && pip freeze --exclude-editable | grep -E '^[A-Za-z0-9_.-]+==' \
      | tr 'A-Z_' 'a-z-' | sort > "${TMPDIR:-/tmp}/installed.txt" \
 && grep -E '^[A-Za-z0-9_.-]+==' requirements.lock \
      | tr 'A-Z_' 'a-z-' | sort > "${TMPDIR:-/tmp}/locked.txt" \
 && { diff -u "${TMPDIR:-/tmp}/locked.txt" "${TMPDIR:-/tmp}/installed.txt" \
      || { echo "T-1004: LOCK DRIFT — the installed set is not the locked set."; \
           echo "The image would not be a function of the ref. Refusing."; \
           exit 1; }; } \
 && rm -f "${TMPDIR:-/tmp}/installed.txt" "${TMPDIR:-/tmp}/locked.txt"

COPY api/app /app/app
COPY --from=web-builder /web/dist /app/web/dist
# Install bundle served by the mothership at /i/<token>/{install.sh,instructions.md}
# (routes_mothership._bundle_dir() -> INSTALL_BUNDLE_DIR or /app/scripts/install).
# Without this the bundle GETs 500 "install bundle missing" on every MOTHERSHIP=1
# deploy and the multi-server install can't even download the installer (T-0263).
COPY scripts/install /app/scripts/install

# T-0379: bake the deployed git sha into the image so /api/health can report
# what's actually running (and the recipe can assert running==deployed). Passed
# by the deploy recipe via `--build-arg GIT_SHA=$(git rev-parse HEAD)`; defaults
# to "unknown" for local/un-stamped builds.
ARG GIT_SHA=unknown

ENV CONFIG_DIR=/config \
    DATA_DIR=/data \
    WORKER_SOCK=/data/_sock/worker.sock \
    BUILD_AT_IMPORT=1 \
    COOKIE_SECURE=1 \
    BOT_SQUAD_GIT_SHA=${GIT_SHA}

EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips '*' so Starlette honors traefik's
# X-Forwarded-Proto: in particular, `request.url_for(...)` in routes_releases
# was emitting http:// scheme even on https traffic, breaking the consumer's
# tarball download (T-0090 dogfood finding). Trust ALL forwarders because
# traefik is the only ingress in front of this container and the container
# port isn't exposed externally.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
