# Operations & deployment

How to run the Mimer backend (FastAPI + Postgres) with Docker Compose on a
single host or a private VPS, plus migrations, workers, health checks, smoke
tests, backups, and security assumptions.

> **Scope.** This is a **private-beta** deployment guide. The compose stack is
> **private-network / private-VPS ready**. The API supports an optional shared
> **Bearer token** (`MIMER_API_TOKEN`) guarding `/api/v1`; set it (plus your
> existing reverse proxy / TLS) before exposing anything. See
> [Security](#security) for the full model.
>
> **Not deployed yet.** This iteration hardens and verifies the **local** Docker
> runtime + auth ahead of a VPS move — it does **not** deploy. The VPS handoff
> (§§ 2–2b) is the documented, human-run next step, not an automated one.

Files involved:

| File | Purpose |
| ---- | ------- |
| `Dockerfile` | Runtime image (uv, non-root, API by default) |
| `infra/docker-compose.yml` | Base stack: `postgres`, `migrate`, `api`, optional `scheduler` |
| `infra/docker-compose.prod.yml` | VPS hardening override (localhost-bound API, log rotation) |
| `infra/.env.example` | Compose env template → copy to `infra/.env` |
| `.env.example` | Host-run env template → copy to `.env` |
| `scripts/smoke_api.sh` | Post-deploy read-only smoke test |

---

## 1. Local Docker Compose quickstart

```bash
cd infra
cp .env.example .env          # then edit POSTGRES_PASSWORD etc.

docker compose up -d postgres # start the database, wait for healthy
docker compose run --rm migrate   # apply migrations (explicit, idempotent)
docker compose up -d --build api  # build + start the API

# Optional: load demo seed data (idempotent; safe to skip in production).
docker compose run --rm api uv run python -m app.seed.seed_data

# Verify.
curl -fsS http://localhost:8080/health
../scripts/smoke_api.sh
```

API: `http://localhost:8080` — interactive docs at `/docs`.

The `api` service `depends_on` the `migrate` service completing successfully and
Postgres being healthy, so `docker compose up -d` alone will also run migrations
first. Running `migrate` explicitly (as above) is the recommended first-deploy
flow because it surfaces migration errors before the API container starts.

## 2. Production / VPS quickstart

Layer the prod override on top of the base file. It binds the API to
`127.0.0.1` **only** (so your existing reverse proxy terminates external
traffic), drops the Postgres host port entirely, and adds container log
rotation. This guide assumes the VPS **already runs Caddy** — the stack here
does not ship or manage a proxy; it just exposes a localhost port for Caddy to
forward to (see [§ 2a](#2a-point-your-existing-caddy-at-the-api)).

```bash
cd infra
cp .env.example .env
# Edit .env: set a STRONG POSTGRES_PASSWORD, ENVIRONMENT=production,
# CORS_ALLOW_ORIGINS to your real client origin(s), and — on any internet-reachable
# box — a STRONG MIMER_API_TOKEN (e.g. `openssl rand -hex 32`) to require
# `Authorization: Bearer <token>` on /api/v1 (leave blank for unauthenticated dev).

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

$COMPOSE build api          # build the runtime image
$COMPOSE up -d postgres     # start Postgres, wait for healthy
$COMPOSE run --rm migrate   # apply migrations (explicit, idempotent)
$COMPOSE up -d api          # start the API (bound to 127.0.0.1:${API_PORT:-8080})

# Optional in-process scheduler (claims + runs due scheduled_jobs):
$COMPOSE --profile scheduler up -d scheduler

# Smoke (defaults to http://127.0.0.1:${API_PORT:-8080}):
../scripts/smoke_api.sh
```

> The prod override binds the API to the loopback interface only — verify with
> `$COMPOSE ps` (the `api` row reads `127.0.0.1:8080->8080/tcp`, never
> `0.0.0.0:...`) and that Postgres shows **no** published host port. Nothing on
> the public internet can reach either directly; only your existing Caddy can.

### 2a. Point your existing Caddy at the API

The VPS already has Caddy, so there is **nothing to install here** — just add a
site block to your existing `Caddyfile` that reverse-proxies your domain to the
localhost port the container publishes:

```caddyfile
your.domain.example {
    reverse_proxy 127.0.0.1:8080
}
```

Then reload Caddy (`caddy reload` / `systemctl reload caddy`) and verify the
public path end-to-end:

```bash
MIMER_BASE_URL=https://your.domain.example ../scripts/smoke_api.sh
```

Caddy handles TLS; the app neither terminates TLS nor assumes it owns it. If you
publish on a non-default port, set `API_PORT` in `infra/.env` and point
`reverse_proxy` at the same port. Do **not** set `API_BIND_HOST=0.0.0.0` unless
you are deliberately fronting the API some other way — it removes the
localhost-only guarantee.

**Caddy does not need the API token.** Bearer auth is enforced *inside* the API
(`MIMER_API_TOKEN`), so Caddy just forwards the client's `Authorization` header
through untouched. Keep the token out of the `Caddyfile` and Caddy logs. Smoke the
public path with the token:

```bash
MIMER_BASE_URL=https://your.domain.example MIMER_API_TOKEN='…' ../scripts/smoke_api.sh
```

### 2b. Moving the image to the VPS

Two supported paths — pick one.

**A. Build on the VPS (simplest; recommended).** The repo is on the box, so just
pull and build in place:

```bash
cd /opt/mimer            # your checkout
git pull
COMPOSE="docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml"
$COMPOSE build api
$COMPOSE run --rm migrate
$COMPOSE up -d api
```

**B. Build locally, transfer the image.** Useful when the VPS should not build
(low RAM, no toolchain, air-gapped). Build a tagged image, then either push to a
registry **or** ship a tarball.

Via a registry:

```bash
docker build -t mimer-api:$(git rev-parse --short HEAD) .
docker tag  mimer-api:$(git rev-parse --short HEAD) <registry>/mimer-api:<tag>
docker push <registry>/mimer-api:<tag>
# On the VPS: docker pull <registry>/mimer-api:<tag>
# then `docker tag <registry>/mimer-api:<tag> mimer-service:latest` so compose
# (which expects image: mimer-service:latest) uses it without rebuilding.
```

Via a tarball (no registry needed):

```bash
TAG=$(git rev-parse --short HEAD)
docker build -t mimer-service:latest .
docker save mimer-service:latest | gzip > mimer-api_${TAG}.tar.gz
scp mimer-api_${TAG}.tar.gz user@vps:/opt/mimer/
ssh user@vps 'docker load < /opt/mimer/mimer-api_'${TAG}'.tar.gz'
```

Either way, the compose files reference `image: mimer-service:latest`, so once
that tag exists on the VPS you run `migrate` then `up -d api` **without**
`build` (Compose uses the loaded/pulled image). The migration + worker commands
still come from the same image — copy the `infra/` compose files and your
`infra/.env` to the box regardless of which path you choose.

> The runtime image is **offline-safe**: it boots with no network access (no
> dependency download at start — deps are baked in at build time and `uv run`
> never re-syncs; see `UV_NO_SYNC` in the `Dockerfile`). Only the workers you
> explicitly point at a live `--source` make outbound calls.

## 3. Migration workflow

Migrations are **explicit** — the API container does not auto-migrate on start
(so a first deploy or a scaled API never races multiple processes through the
same migration). Alembic reads `DATABASE_URL` from the environment.

```bash
cd infra

# Apply all migrations (idempotent — no-op when already at head).
docker compose run --rm migrate

# Inspect current / available revisions.
docker compose run --rm migrate uv run alembic current
docker compose run --rm migrate uv run alembic history

# Roll back one revision (rarely needed; review the downgrade first).
docker compose run --rm migrate uv run alembic downgrade -1
```

Current head: **0019**. The `/api/v1/capabilities` payload advertises the head
the code expects (`migration_head`); the smoke test asserts it matches.

> Never auto-run destructive DB operations from a container start command. The
> `migrate` service only runs `alembic upgrade head`.

## 4. Worker commands

Workers are one-off jobs run inside the same image. They default to **offline
fixture** sources and never call a live data source unless you name one with
`--source`.

```bash
cd infra

# Prices for a workspace's resolved instruments (offline fixture by default):
docker compose run --rm api uv run python -m app.workers.run \
  instrument_eod_price_ingestion --workspace-id 1

# Issuer holdings for one fund (offline fixture):
docker compose run --rm api uv run python -m app.workers.run \
  issuer_holdings_ingestion --fund-id 1 --source holdings_fixture

# Reference rates (offline fixture):
docker compose run --rm api uv run python -m app.workers.run \
  rates_ingestion --source rates_fixture

# Recompute the bounded portfolio valuation snapshot (consumes already-ingested
# prices/FX only — no fetch, no PnL):
docker compose run --rm api uv run python -m app.workers.run \
  portfolio_valuation_recompute --workspace-id 1

# FX rates (offline fixture):
docker compose run --rm api uv run python -m app.workers.run fx_ingestion
```

**Live sources are explicit-only** — never the default. They go through the
source budget + fetch log; always bound them with a small `--limit`:

```bash
# Live US Treasury par yields (explicit; budget-guarded):
docker compose run --rm api uv run python -m app.workers.run \
  rates_ingestion --source us_treasury_rates --limit 100

# Live ECB rates (explicit; budget-guarded):
docker compose run --rm api uv run python -m app.workers.run \
  rates_ingestion --source ecb_rates --limit 100

# Bounded, safe live verification of a target fund's sources (VUSA/ISF/JEPG).
# Verify-only: stores nothing, promotes nothing, a blocked provider never fails it.
docker compose run --rm api uv run python -m app.workers.run \
  verify_fund_sources --fund-symbol ISF --limit 10
docker compose run --rm api uv run python -m app.workers.run \
  verify_fund_sources --all-target-funds --limit 10
```

See the README for the full worker catalogue and flags. Per-fund live readiness for
VUSA/ISF/JEPG is at `GET /api/v1/data-sources/fund-coverage` (full detail in
`docs/data_sources.md` § *Target-fund coverage*).

### Scheduler

The `scheduler` service (compose profile `scheduler`) runs the in-process
scheduler loop: it claims **due** `scheduled_jobs`, leases them, and runs the
same `app.workers.run` logic — no external queue/broker. The service uses an
`entrypoint`, so extra args (`--once`, `--poll-seconds N`) pass straight through.
One pass on demand, then the long-running profile:

```bash
docker compose run --rm scheduler --once            # one pass, then exit
docker compose --profile scheduler up -d scheduler  # long-running poll loop
docker compose logs --tail=100 scheduler            # inspect it
```

**Which sources are safe to schedule.** Before scheduling any live ingestion, check the
**production data-source readiness matrix** — `GET /api/v1/data-sources/readiness` (full
detail in `docs/data_sources.md` § *Production data-source readiness*). Only
`safe_for_scheduler=true` sources should drive a scheduled job, and they stay
**explicit-only**: add an explicit `scheduled_jobs` row that names the live `--source`,
never flip the worker's default off the offline fixture. Today the scheduler-safe sources
are `us_treasury_rates` / `ecb_rates` (rates), `stooq` (fund/instrument prices), `openfigi`
(identity, strict budget) and `blackrock_ishares_holdings` (ISF holdings, verified). **Do
not** schedule blocked candidates (`jpmorgan_etf_holdings`, `vanguard_distributions`),
planned sources (`boe_rates`, live Vanguard/iShares, IBKR), or fixture sources as production
defaults — `GET /api/v1/diagnostics` reports `scheduled_live_jobs` vs `fixture_scheduled_jobs`
and `missing_required_live_sources` so a fixture scheduled in production is never mistaken
for live readiness. The seeded `scheduled_jobs` are **dev** schedules (seeded one interval
out, fixture defaults) except `daily_price_ingestion`, whose default `stooq` is live free
EOD. Recommended cadences (rates daily, prices daily EOD, holdings daily/weekly, identity
after holdings/imports, exposure/valuation/alerts after recomputes) are tabulated in
`docs/data_sources.md`.

## 5. Health checks

| Endpoint | Meaning |
| -------- | ------- |
| `GET /health` | Liveness — process is up. No DB touch. Used by the container `HEALTHCHECK`. |
| `GET /health/db` | Readiness — API can reach Postgres (`SELECT 1`). `503` if not. |
| `GET /api/v1/diagnostics` | DB-backed operational counts (not a healthcheck — heavier). |

```bash
curl -fsS http://localhost:8080/health        # {"status":"ok"}
curl -fsS http://localhost:8080/health/db     # {"status":"ok","database":"connected"}
```

The Docker image ships a `HEALTHCHECK` that polls `/health`; compose reports the
`api` service as `healthy` once it responds. Both health endpoints are
**unauthenticated** even when `MIMER_API_TOKEN` is set, so the probe and your
existing Caddy never need a token.

## 6. Smoke test

After any deploy, run the read-only smoke test. It checks liveness, DB
connectivity, the expected migration head, and that the key read endpoints
(capabilities, diagnostics, market-data plan, dashboard, portfolio valuation
summary) respond — including a clean empty/missing state.

The base URL resolves as `MIMER_BASE_URL` > `BASE_URL` >
`http://127.0.0.1:${API_PORT:-8080}` (the localhost port the prod compose
binds — exactly what Caddy proxies to).

```bash
scripts/smoke_api.sh                                          # 127.0.0.1:${API_PORT:-8080}, ws 1
API_PORT=9000 scripts/smoke_api.sh                           # localhost:9000
MIMER_API_TOKEN='…' scripts/smoke_api.sh                     # auth-enabled API
MIMER_BASE_URL=https://your.domain.example scripts/smoke_api.sh   # through your Caddy
MIMER_BASE_URL=https://your.domain.example MIMER_API_TOKEN='…' scripts/smoke_api.sh
WORKSPACE_ID=2 scripts/smoke_api.sh                          # a different workspace
```

When `MIMER_API_TOKEN` is set, the smoke script sends `Authorization: Bearer
<token>` on the `/api/v1` checks (and leaves `/health` + `/health/db`
unauthenticated). The token is only ever passed to `curl` as an argument — it is
never printed. Against an auth-enabled API you **must** pass the matching token or
the `/api/v1` checks fail with `401`.

Exit code `0` = no failures. The four **core** checks (health, db, migration
head, diagnostics) must pass. The three **workspace-scoped** checks are *skipped*
(reported, not failed) when the workspace does not exist yet — e.g. a fresh
deploy before any data is seeded — so a clean, data-less box still smokes green.
Seed (`docker compose run --rm api uv run python -m app.seed.seed_data`) or pass
`WORKSPACE_ID` to exercise them.

## 7. Backups & restore

Postgres data lives in the named Docker volume `pgdata` (see
`docker volume inspect infra_pgdata` for the host path). **A volume on the VPS is
not a backup** — take logical dumps and copy them off the box.

```bash
cd infra
mkdir -p backups

# Backup (logical dump):
docker compose exec -T postgres \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > "backups/mimer_$(date +%F).sql"

# Restore into a running (empty) database:
cat backups/mimer_2026-06-26.sql | \
  docker compose exec -T postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB"
```

(`$POSTGRES_USER` / `$POSTGRES_DB` come from your `infra/.env`; `source .env`
first or pass them inline.)

Recommendations:

- **Copy dumps off the VPS** (e.g. `scp`, object storage) — a local dump dies
  with the box.
- Test a restore into a throwaway database periodically.
- `backups/` is git-ignored; never commit dumps (they contain real data).

## 8. Logging

Logs go to stdout/stderr (structured by `LOG_LEVEL`, default `info`).

```bash
cd infra
docker compose logs -f api
docker compose logs -f postgres
docker compose logs -f scheduler   # if running the scheduler profile
```

Set `LOG_LEVEL=warning` in `infra/.env` to quiet a production box. The prod
override rotates container logs (`max-size=10m`, `max-file=5`) so a long-running
VPS does not fill its disk.

## 9. Security

### API auth (Bearer token)

The API supports an **optional shared Bearer token** guarding the whole `/api/v1`
surface:

| `MIMER_API_TOKEN` | Behaviour |
| ----------------- | --------- |
| blank / unset | Auth **disabled** — every `/api/v1` route is open (local dev). |
| non-empty | Each `/api/v1` request must send `Authorization: Bearer <token>`; missing/malformed/wrong ⇒ `401`. |

- `GET /health` and `GET /health/db` are **always unauthenticated** (the
  container `HEALTHCHECK` and Caddy never need a token).
- The token is **constant-time** compared
  (`app/api/security.py:require_api_token`, applied as a `/api/v1` router
  dependency). It is **never logged** and **never returned** in any response
  (capabilities/diagnostics expose only data, never the token).
- This is a single shared secret, **not** user identity — no user DB, sessions,
  cookies or OAuth. Workspace resolution (URL path / `X-Workspace-ID` / default)
  is unchanged and is still a dev convenience, not per-user authorization.

Generate and set a strong token on any internet-reachable box:

```bash
openssl rand -hex 32                 # generate
# put it in infra/.env as MIMER_API_TOKEN=<value>, then recreate the API:
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate api
```

Quick manual check (auth enabled):

```bash
curl -fsS http://127.0.0.1:8080/health                                 # 200 (no token)
curl -i   http://127.0.0.1:8080/api/v1/capabilities                    # 401 (no token)
curl -fsS -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8080/api/v1/capabilities                            # 200
```

Caddy forwards the client's `Authorization` header untouched — it does **not**
need to know the token. Keep the token out of the `Caddyfile`.

### Other

- **This compose deployment is private-network / private-VPS ready.** Even with a
  token set, keep it behind your existing reverse proxy / TLS — do **not** publish
  the raw API.
- **Never expose Postgres publicly.** The base file binds it to `127.0.0.1`
  only; the prod override removes the host port entirely.
- **Use a strong `POSTGRES_PASSWORD`.** Change it from the example default.
- **Restrict the VPS firewall** to SSH (+ your reverse proxy) only; prefer an
  SSH tunnel or VPN for private use.
- **Never commit `.env`** (or any real secret — including `MIMER_API_TOKEN`).
  `.env`, `infra/.env`, and `*.env` are git-ignored; only `*.env.example` files
  are tracked (with a **blank** `MIMER_API_TOKEN=`).
- The API never returns secrets — it exposes only
  `openfigi_api_key_configured: true/false`; fetch logs store hashes/labels, not
  keys.

## 10. Troubleshooting

| Symptom | Likely cause / fix |
| ------- | ------------------ |
| API container restarts / unhealthy | DB unreachable or migrations not applied. Check `docker compose logs api`; run `docker compose run --rm migrate`. |
| `connection refused` to Postgres | Postgres not healthy yet, or `DATABASE_URL` host wrong. Inside compose it must be `@postgres:5432`; on the host `@localhost:5432`. |
| `GET /health/db` returns 503 | API is up but cannot reach Postgres — check the DB container and `DATABASE_URL`. |
| Smoke test migration-head check fails | Image is older/newer than the DB. Rebuild the image and re-run `migrate`. |
| `password authentication failed` | `POSTGRES_PASSWORD` changed after the volume was initialised. Either restore the old password or recreate the volume (destroys data). |
| `/api/v1/...` returns `401 unauthorized` | `MIMER_API_TOKEN` is set on the API but the request sent no / a wrong `Authorization: Bearer <token>`. Pass the matching token (or unset the var for unauthenticated dev). `/health` is never affected. |
| Worker hits the network unexpectedly | A live `--source` was named. Omit it (or use a `*_fixture` source) to stay offline. |
| Permission denied writing in container | The image runs as non-root (`appuser`); writeable paths live under `/app`. Do not write elsewhere. |
| Caddy returns 502 / connection refused | API not up, or not on the port Caddy targets. Check `$COMPOSE ps` (the `api` row should read `127.0.0.1:${API_PORT}->8080/tcp`) and that the `reverse_proxy` target matches `API_PORT`. |
| API reachable from outside the box on `:8080` | The prod override was not layered (`-f docker-compose.prod.yml`), or `API_BIND_HOST=0.0.0.0` was set. The prod override binds the API to `127.0.0.1` only. |
| Smoke shows `SKIP (workspace N not found)` | Not an error — a fresh DB has no workspaces. Run the seed command, or pass `WORKSPACE_ID` for an existing workspace. |
| Container fails to start needing to download packages | An old image (pre-`UV_NO_SYNC`) re-synced dev deps at boot. Rebuild the image; the runtime venv is baked in and boots offline. |
