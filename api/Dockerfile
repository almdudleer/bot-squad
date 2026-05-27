# Multi-stage: build web, then assemble api with web/dist as static.

FROM node:20-alpine AS web-builder
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
RUN npm run build

FROM python:3.12-slim AS api
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

COPY api/pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir .

COPY api/app /app/app
COPY --from=web-builder /web/dist /app/web/dist

ENV CONFIG_DIR=/config \
    DATA_DIR=/data \
    WORKER_SOCK=/data/_sock/worker.sock \
    BUILD_AT_IMPORT=1 \
    COOKIE_SECURE=1

EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips '*' so Starlette honors traefik's
# X-Forwarded-Proto: in particular, `request.url_for(...)` in routes_releases
# was emitting http:// scheme even on https traffic, breaking the consumer's
# tarball download (T-0090 dogfood finding). Trust ALL forwarders because
# traefik is the only ingress in front of this container and the container
# port isn't exposed externally.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
