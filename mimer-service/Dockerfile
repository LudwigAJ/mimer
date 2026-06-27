# Base image matches .python-version (3.11). uv manages the project venv.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_PREFERENCE=system \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    # Don't update the lockfile at runtime...
    UV_FROZEN=1 \
    # ...and don't re-sync the venv on `uv run` either. Without this, every
    # `uv run` (migrate / api / scheduler / workers) re-syncs against the
    # lockfile at container start — which RE-INSTALLS the dev group the image was
    # built without (--no-dev) and therefore REQUIRES network access just to
    # boot. UV_NO_SYNC makes `uv run` use the pre-built, dev-free venv as-is:
    # no network, no surprise installs, deterministic, offline-safe.
    UV_NO_SYNC=1 \
    # uv cache + home live under /app so the non-root user can write them.
    HOME=/app \
    UV_CACHE_DIR=/app/.uv-cache \
    PATH=/app/.venv/bin:$PATH

# uv: fast, reproducible dependency management.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cached) using only the lockfile metadata. Runtime
# image excludes dev/test dependencies (--no-dev).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy the application and install the project itself.
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN uv sync --frozen --no-dev

# Run as a non-root user. Own /app so uv (cache/home) and the venv are writable.
RUN useradd --create-home --home-dir /app --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Container-level liveness check (no curl in the slim image — use stdlib urllib).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"]

# Default command starts the API only. Migrations are an EXPLICIT step (the
# compose `migrate` service / `uv run alembic upgrade head`) so a first deploy or
# a scaled-out API never races multiple processes through the same migration.
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
