# Base images keep the normal Docker Hub defaults. The arguments exist so a
# restricted network can build from the same official images via a mirror
# without changing any Python dependency or version.
ARG NODE_IMAGE=node:24-alpine
ARG PYTHON_IMAGE=python:3.11-slim

FROM ${NODE_IMAGE} AS frontend
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json /web/
RUN npm ci
COPY frontend /web/
RUN npm run build

# Pinned uv release; the lock file is installed with --frozen below.
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM ${PYTHON_IMAGE}

ARG INSTALL_RERANK=false
ARG IMAGE_REVISION=""

LABEL org.opencontainers.image.revision="${IMAGE_REVISION}"

# Safe image defaults. The dedicated local after-sales Compose file overrides
# these explicitly for its loopback Fake scenario; production remains the
# image default and local model loaders stay disabled unless opted in.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    LOCAL_EMBEDDING_ENABLED=false \
    CROSS_ENCODER_ENABLED=false \
    UNSTRUCTURED_ENABLED=false \
    PYTHONPATH=/app \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv

# Only pyproject.toml/uv.lock are needed to create the locked environment.
COPY pyproject.toml uv.lock README.md /app/
COPY alembic.ini /app/
COPY app /app/app
COPY alembic /app/alembic
COPY knowledge /app/knowledge
COPY --from=frontend /web/dist /app/frontend/dist

# --no-install-project keeps the runtime environment limited to the locked
# third-party dependency set; the application runs from /app via PYTHONPATH.
RUN if [ "$INSTALL_RERANK" = "true" ]; then \
        uv sync --frozen --no-dev --no-install-project --extra rerank; \
    else \
        uv sync --frozen --no-dev --no-install-project; \
    fi \
    && mkdir -p /app/data/local \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
