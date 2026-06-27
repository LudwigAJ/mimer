"""Guards for the deployment assets (Dockerfile, compose, env templates, smoke).

These are intentionally lightweight, dependency-free text assertions — they keep
the deploy surface honest (services present, non-root image, no committed
secrets, migration head aligned) without standing up Docker in CI.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from app.services import capabilities as capabilities_service

_REPO_ROOT = Path(__file__).resolve().parent.parent

# A bare UUID assigned to a value (the shape of a real OpenFIGI key) must never
# appear in a tracked *.env.example. Placeholders like <your-api-key> are fine.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text()


def test_dockerfile_runs_as_non_root_and_exposes_port() -> None:
    dockerfile = _read("Dockerfile")
    assert "FROM python:3.11-slim" in dockerfile
    # Non-root: a user is created and selected.
    assert "useradd" in dockerfile
    assert re.search(r"^USER appuser", dockerfile, re.MULTILINE)
    assert "EXPOSE 8080" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    # The default command starts the API, not migrations (migrations are explicit).
    cmd_line = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    assert "uvicorn" in cmd_line
    assert "alembic" not in cmd_line


def test_compose_defines_expected_services() -> None:
    compose = _read("infra/docker-compose.yml")
    for service in ("\n  postgres:", "\n  migrate:", "\n  api:", "\n  scheduler:"):
        assert service in compose, f"missing service block {service!r}"
    # Migrations run via the one-shot migrate service (JSON-array command form).
    assert '"alembic", "upgrade", "head"' in compose
    # Postgres is bound to localhost only — never exposed publicly.
    assert "127.0.0.1:${POSTGRES_PORT:-5432}:5432" in compose
    # The API waits for migrations to finish before starting.
    assert "service_completed_successfully" in compose


def test_prod_override_hardens_api_and_postgres() -> None:
    prod = _read("infra/docker-compose.prod.yml")
    # API bound to localhost by default (reverse proxy / tunnel in front).
    assert "127.0.0.1" in prod
    # Postgres host port dropped in production. `!reset` is required because
    # Compose merges `ports` by appending — a plain `[]` would not remove it.
    assert "ports: !reset []" in prod


def test_prod_override_api_ports_replace_not_append() -> None:
    """The API port list must REPLACE the base file's, not merge into it.

    Compose merges `ports` by appending, so the base file's public
    ``${API_PORT}:8080`` (0.0.0.0) binding would survive a plain override and the
    API would stay publicly exposed alongside the localhost-only one. ``!override``
    is what drops it — without this the localhost-only guarantee is silently lost.
    """
    prod = _read("infra/docker-compose.prod.yml")
    assert "ports: !override" in prod, (
        "prod api `ports` must use !override so the base 0.0.0.0 binding is replaced"
    )
    # The only host binding the prod override publishes is the localhost one.
    assert "${API_BIND_HOST:-127.0.0.1}:${API_PORT:-8080}:8080" in prod


def test_no_reverse_proxy_assets_are_added() -> None:
    """This service does not ship or manage a proxy — the VPS already has Caddy.

    Guard against a future slice accidentally adding a Caddy/nginx/Traefik
    compose file or service block here.
    """
    infra = _REPO_ROOT / "infra"
    forbidden_files = [
        "docker-compose.caddy.yml",
        "docker-compose.nginx.yml",
        "docker-compose.traefik.yml",
        "Caddyfile",
    ]
    for name in forbidden_files:
        assert not (infra / name).exists(), f"unexpected reverse-proxy asset infra/{name}"
        assert not (_REPO_ROOT / name).exists(), f"unexpected reverse-proxy asset {name}"
    # No proxy *service* in any compose file (a `reverse_proxy` directive inside a
    # docs Caddy snippet is fine; a compose service block named caddy/nginx/etc.
    # is not).
    for compose_rel in ("infra/docker-compose.yml", "infra/docker-compose.prod.yml"):
        compose = _read(compose_rel)
        for svc in ("\n  caddy:", "\n  nginx:", "\n  traefik:"):
            assert svc not in compose, f"{compose_rel} defines a proxy service {svc!r}"


def test_dockerfile_boots_offline_no_runtime_sync() -> None:
    """The runtime image must not re-sync deps at start (offline-safe boot).

    Without UV_NO_SYNC, every `uv run` (migrate/api/scheduler/workers) re-syncs
    against the lockfile and RE-INSTALLS the dev group the image was built without
    (--no-dev) — requiring network access just to boot the container.
    """
    dockerfile = _read("Dockerfile")
    assert "UV_NO_SYNC=1" in dockerfile
    # Build still installs without dev dependencies.
    assert "--no-dev" in dockerfile


def test_scheduler_disables_http_healthcheck() -> None:
    """The scheduler has no HTTP server, so the image's :8080 healthcheck must be
    disabled for it (otherwise the container reports `unhealthy` forever)."""
    compose = _read("infra/docker-compose.yml")
    scheduler_block = compose.split("\n  scheduler:", 1)[1]
    assert "healthcheck:" in scheduler_block and "disable: true" in scheduler_block


def test_scheduler_uses_entrypoint_so_run_args_append() -> None:
    """`docker compose run --rm scheduler --once` must pass `--once` THROUGH to the
    scheduler module. `compose run` overrides a service's `command` but not its
    `entrypoint`, so the scheduler invocation lives in `entrypoint` (with an empty
    `command`) — otherwise `--once` would be exec'd as a binary and fail."""
    compose = _read("infra/docker-compose.yml")
    scheduler_block = compose.split("\n  scheduler:", 1)[1].split("\nvolumes:", 1)[0]
    assert "entrypoint:" in scheduler_block
    assert "app.workers.scheduler" in scheduler_block
    # The long-running `profile up` form relies on no default extra args.
    assert "command: []" in scheduler_block


def test_env_examples_have_no_committed_secrets() -> None:
    for rel in (".env.example", "infra/.env.example"):
        text = _read(rel)
        # No real-looking (UUID) key value committed.
        assert not _UUID_RE.search(text), f"{rel} contains a UUID-shaped secret"
        # The OpenFIGI key placeholder must be blank (no inline value).
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("OPENFIGI_API_KEY=") and not stripped.startswith("#"):
                assert stripped == "OPENFIGI_API_KEY=", f"{rel}: {line!r} has a value"


def test_smoke_script_exists_and_is_executable() -> None:
    script = _REPO_ROOT / "scripts" / "smoke_api.sh"
    assert script.exists()
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR, "smoke_api.sh should be executable"
    body = script.read_text()
    assert "/health" in body and "/health/db" in body
    assert "capabilities" in body


def test_smoke_script_migration_head_matches_capabilities() -> None:
    # The smoke test asserts a specific alembic head; keep it aligned with code.
    body = (_REPO_ROOT / "scripts" / "smoke_api.sh").read_text()
    assert f"EXPECTED_MIGRATION_HEAD:-{capabilities_service.MIGRATION_HEAD}" in body


def test_smoke_script_defaults_to_localhost_and_supports_mimer_base_url() -> None:
    body = (_REPO_ROOT / "scripts" / "smoke_api.sh").read_text()
    # Default base resolves to the localhost-only port the prod compose binds.
    assert "MIMER_BASE_URL" in body
    assert "127.0.0.1:${API_PORT:-8080}" in body
    # No live-provider / ingestion calls in a smoke run.
    for forbidden in ("--source", "workers.run", "ingestion"):
        assert forbidden not in body, f"smoke script must not invoke {forbidden!r}"


def test_smoke_script_is_auth_aware_and_never_echoes_token() -> None:
    """The smoke script sends a Bearer header on /api/v1 when MIMER_API_TOKEN is
    set, and must never print the token."""
    body = (_REPO_ROOT / "scripts" / "smoke_api.sh").read_text()
    assert "MIMER_API_TOKEN" in body
    assert "Authorization: Bearer" in body
    # The token is only ever passed to curl as an argument — never echoed/printed.
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(("echo", "printf", "print")):
            assert "MIMER_API_TOKEN" not in stripped, f"token must not be printed: {line!r}"


def test_env_examples_document_blank_api_token() -> None:
    """Both env templates ship a BLANK MIMER_API_TOKEN (auth disabled by default,
    no committed secret)."""
    for rel in (".env.example", "infra/.env.example"):
        text = _read(rel)
        token_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("MIMER_API_TOKEN=") and not line.strip().startswith("#")
        ]
        assert token_lines == ["MIMER_API_TOKEN="], f"{rel}: expected a single blank token line"


def test_compose_passes_api_token_into_api() -> None:
    """The compose `api` service forwards MIMER_API_TOKEN into the container so an
    auth-enabled run is possible (blank => unauthenticated local dev)."""
    compose = _read("infra/docker-compose.yml")
    api_block = compose.split("\n  api:", 1)[1].split("\n  scheduler:", 1)[0]
    assert "MIMER_API_TOKEN: ${MIMER_API_TOKEN:-}" in api_block


def test_operations_docs_document_existing_caddy_proxy() -> None:
    """Docs hand off to the VPS's EXISTING reverse proxy with a minimal snippet —
    they do not introduce managed proxy infrastructure."""
    ops = _read("docs/operations.md")
    # Minimal Caddy snippet pointing at the localhost API port.
    assert "reverse_proxy 127.0.0.1:8080" in ops
    # Framed as the existing/external proxy, and image handoff is documented.
    assert "existing" in ops.lower() and "caddy" in ops.lower()
    assert "docker save" in ops and "docker load" in ops  # image transfer path


def test_dockerignore_excludes_env_and_tests() -> None:
    ignore = _read(".dockerignore")
    assert ".env" in ignore
    assert "tests/" in ignore


def test_no_real_env_file_is_tracked_by_git() -> None:
    # Defense in depth: a developer .env must never be committed. We assert the
    # tracked tree (git ls-files) carries no .env except *.env.example.
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return  # not a git checkout (e.g. sdist) — nothing to assert
    tracked_env = [
        line
        for line in out.splitlines()
        if os.path.basename(line) == ".env"
        or (line.endswith(".env") and not line.endswith(".env.example"))
    ]
    assert tracked_env == [], f"tracked secret env files: {tracked_env}"
