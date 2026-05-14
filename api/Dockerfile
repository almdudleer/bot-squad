# Multi-stage: build web, then assemble api with web/dist as static.

FROM node:20-alpine AS web-builder
ARG VITE_MOTHERSHIP=0
ENV VITE_MOTHERSHIP=${VITE_MOTHERSHIP}
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# Mothership add-server wizard ?raw-imports the canonical chat-agent
# install prompt from scripts/install/. Sibling path to /web so the
# `../scripts/...` resolution from vite/Rollup matches local dev.
COPY scripts/install /scripts/install
RUN npm run build

FROM python:3.12-slim AS api
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
