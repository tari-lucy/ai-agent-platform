# Build stages only contact public package registries; runtime has no model download.
FROM node:24.19.0-bookworm-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df AS panel
WORKDIR /build/panel
COPY apps/panel/package.json apps/panel/package-lock.json apps/panel/.npmrc ./
RUN npm ci
COPY apps/panel/index.html apps/panel/tsconfig.json apps/panel/vite.config.ts ./
COPY apps/panel/src ./src
RUN --network=none npm run build

FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_NO_PROGRESS=1
WORKDIR /opt/platform
COPY pyproject.toml uv.lock .python-version LICENSE THIRD_PARTY_NOTICES.md ./
COPY apps/core/src ./apps/core/src
COPY apps/worker/src ./apps/worker/src
COPY packages ./packages
FROM builder AS core-deps
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
FROM builder AS worker-deps
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --all-extras --no-editable

FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS runtime
RUN groupadd --gid 10001 platform && useradd --uid 10001 --gid 10001 --no-create-home platform \
    && mkdir -p /var/lib/platform/core /var/lib/platform/models \
    && chown -R 10001:10001 /var/lib/platform
WORKDIR /opt/platform
ENV PATH="/opt/platform/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY deploy/healthcheck.py ./healthcheck.py
USER 10001:10001

FROM runtime AS core
COPY --from=core-deps /opt/platform/.venv ./.venv
COPY --from=panel /build/panel/dist ./panel
COPY deploy/core.py ./core.py
EXPOSE 8080
CMD ["python", "/opt/platform/core.py"]

FROM runtime AS worker
COPY --from=worker-deps /opt/platform/.venv ./.venv
COPY deploy/worker.py ./worker.py
CMD ["python", "/opt/platform/worker.py"]
