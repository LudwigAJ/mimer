# mimer-service — ETF / Portfolio Data Service

A standalone backend service for a personal ETF / portfolio analytics system.
It stores ETF and portfolio data in Postgres and serves it over a clean,
versioned REST API (FastAPI). It is designed to be consumed later by one or more
clients (desktop GUI, browser dashboard, CLI, automation workers) — there is no
GUI in this repository.

> First clean foundation: canonical data model + read-focused REST API + seed
> data + migrations + Docker. Real data ingestion is **not** implemented yet
> (see [Roadmap](#roadmap)).

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Run locally](#run-locally)
- [Migrations](#migrations)
- [Seed data](#seed-data)
- [Multi-tenant: shared vs private data](#multi-tenant-shared-vs-private-data)
- [Adding instruments & price ingestion](#adding-instruments--price-ingestion)
- [Alerts](#alerts)
- [Exposure](#exposure)
- [API endpoints](#api-endpoints)
- [Example curl commands](#example-curl-commands)
- [Client / GUI integration contract](#client--gui-integration-contract)
- [Manual overrides](#manual-overrides)
- [Environment variables](#environment-variables)
- [Docker Compose](#docker-compose)
- [Data source strategy](#data-source-strategy)
- [Security](#security--auth)
- [Testing](#testing)
- [Not implemented yet](#not-implemented-yet)
- [Roadmap](#roadmap)

## What it does

- Stores funds, listings, prices, distributions, holdings, portfolio positions,
  documents, alerts, FX rates and ingestion metadata in Postgres.
- Serves them as JSON under `/api/v1`.
- Computes a GUI-friendly **portfolio summary** (market value, unrealised P/L,
  trailing-12-month income, projected income) and approximate **look-through
  exposure** (country / sector / currency).
- Models the core domain correctly: **a fund is identified by ISIN, not ticker**;
  one fund can have many listings, currencies and tickers.

## Architecture

```
app/
  main.py            FastAPI app factory
  api/
    deps.py          shared dependencies (DB session)
    router.py        aggregates v1 routers under /api/v1
    v1/              one module per resource (funds, portfolio, …)
  core/
    config.py        env-driven settings (pydantic-settings)
    logging.py       logging setup
    errors.py        structured error envelope + handlers
  db/
    base.py          declarative Base
    models.py        SQLAlchemy ORM models (canonical schema)
    session.py       async engine / session (asyncpg)
  schemas/           Pydantic request/response models
  services/          business logic operating on AsyncSession
  seed/seed_data.py  idempotent seed data
alembic/             migrations (async env)
tests/               pytest (in-memory SQLite, no Postgres required)
infra/               docker-compose.yml + .env.example
Dockerfile
```

**Layering:** `api` → `services` → `db`. Routes are thin; business logic lives in
services; schemas define the JSON contract. Decimals are used for all money and
serialised as **strings** in JSON to avoid float precision issues.

**Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2.x (async), asyncpg,
Alembic, Postgres, `uv`, pytest, ruff, Docker.

## Multi-tenant: shared vs private data

The backend is built to support multiple users/workspaces later, while staying
trivial to run as a single-user service today.

**Shared / reference data** (reusable across all workspaces): `funds`,
`fund_listings`, `prices`, `distributions`, `fund_holdings`,
`document_snapshots`, `fx_rates`, `data_sources`, `ingestion_runs` / `job_runs`.
These are served from non-workspace-scoped endpoints.

**Workspace / private data** (belongs to one workspace, always carries
`workspace_id`): `portfolio_positions`, `portfolio_transactions`, `watchlists` /
`watchlist_items`, `workspace_settings`, and `alerts` (workspace-scoped, see
below). Served from `/api/v1/workspaces/{workspace_id}/...`.

**Why positions are workspace-scoped:** fund/ETF facts are objective and shared,
but *holdings, cost basis, account names and settings are personal*. Scoping
them by workspace prevents one workspace's portfolio from leaking into another
and lets the same reference data back many portfolios. (Tickers are likewise not
identity — see Data source strategy.)

**Alerts** are **workspace-scoped** rows in `alerts` (one per workspace per
distinct issue). They are *derived data*: the `alert_generation` worker turns
existing diagnostics / change signals (stale prices, missing FX, changed
documents, failed jobs, …) into structured alerts. Read / dismiss / resolve state
lives on the row itself (`status` + `read_at` / `dismissed_at` / `resolved_at`),
and idempotency is keyed by `(workspace_id, dedupe_key)`. See **Alerts** below.

**v1 auth / workspace resolution (dev only):** there is **no per-user identity
yet**. The workspace is resolved as: explicit id in the URL path → otherwise the
`X-Workspace-ID` header → otherwise the default (lowest-id) workspace. `GET
/api/v1/me` returns the default seeded user and its workspaces. An optional
**shared Bearer token** can gate the whole `/api/v1` surface for deployment: set
`MIMER_API_TOKEN` and every `/api/v1` request must send `Authorization: Bearer
<token>` (constant-time compared; `/health` + `/health/db` stay open). Blank/unset
= disabled (local dev). This is a single shared secret, **not** identity —
**production auth** (real identity, session/token handling, per-user membership
enforcement) is intentionally still not implemented. The authorization model the
schema is ready for: a user may only access workspaces they are a member of
(`workspace_members`), and private data is never exposed across workspaces.

**Scheduler / jobs (design + real workers, no scheduler):** `scheduled_jobs`
defines what should run (name, `job_type`, `schedule_cron`, `is_active`);
`job_runs` records executions (status, timings, record counts). `POST
/api/v1/jobs/{id}/run` runs the **real worker** for price / issuer-facts /
distribution / holdings / fx / document ingestion, `alert_generation` and
`exposure_recompute`, and records a `success_stub` run for any job type without a
worker yet (e.g. `broker_csv_import`) — there is no real scheduler yet, runs are
synchronous.
`job_runs` **supersedes** the older `ingestion_runs` table (kept for backward
compatibility). Intended future execution: VPS cron → worker commands,
APScheduler in a worker, or Celery/RQ/Arq, optionally in a scheduled Docker
worker container.

**Local-first compatibility (future):** the design supports two modes without
committing to either now:

1. *Server-side portfolio mode* — positions, settings, watchlists and alert
   state live in Postgres; clients read/write workspace-scoped data via the API.
2. *Local-first portfolio mode* — a client stores private positions/cost basis
   locally and uses the backend purely for shared reference data (funds,
   listings, prices, distributions, FX, holdings, documents), computing private
   analytics locally.

Local-first sync is **not** implemented; the separation of shared vs private
data simply keeps it possible.

## Run locally

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and a reachable Postgres
(use the Docker Compose Postgres below, or your own).

```bash
# 1. Install dependencies into a managed venv
uv sync

# 2. Point at your database (defaults to localhost:5432 / etf / etf_password)
export DATABASE_URL="postgresql+asyncpg://etf:etf_password@localhost:5432/etf_data"

# 3. Create the schema
uv run alembic upgrade head

# 4. Load seed data
uv run python -m app.seed.seed_data

# 5. Run the API (FastAPI dev server, reload enabled)
uv run fastapi dev app/main.py --port 8080
# …or with uvicorn directly (used in Docker):
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

The API is then at `http://localhost:8080`, interactive docs at
`http://localhost:8080/docs`.

> Tip: the easiest local Postgres is `docker compose -f infra/docker-compose.yml up -d postgres`
> (binds to `127.0.0.1:5432`).

## Migrations

Alembic is configured for the async engine and reads `DATABASE_URL` from the
environment (never from `alembic.ini`).

```bash
uv run alembic upgrade head            # apply all migrations
uv run alembic downgrade -1            # roll back one
uv run alembic revision -m "message"   # new (hand-written) revision
uv run alembic upgrade head --sql      # render SQL without connecting (offline)
```

Migrations are hand-written to match `app/db/models.py`:

- `0001_initial_schema` — reference data + portfolio positions + alerts.
- `0002_workspaces_and_jobs` — users/workspaces/members/settings, scopes
  `portfolio_positions` with `workspace_id`, moves alert read-state into
  `workspace_alerts`, and adds transactions, watchlists, scheduled jobs and job
  runs.
- `0003_instrument_resolution` — `security_identifiers` crosswalk, lifecycle
  `status` + freshness columns on `funds`/`fund_listings`, and optional
  `fund_id`/`fund_listing_id` targets on `job_runs`.
- `0004_fund_facts_source` — adds `funds.source` (provenance of fund facts;
  populated by the `issuer_facts_ingestion` worker).
- `0005_distribution_provenance` — adds the `(fund_id, ex_date, source)` unique
  constraint on `distributions` so the `distribution_ingestion` worker can upsert
  idempotently.
- `0006_holdings_snapshots` — extends `fund_holdings` with identifier columns
  (SEDOL/CUSIP/FIGI), classification (industry/currency), economics
  (market_value/shares), `status`, `raw_payload_json`, and a deterministic
  `holding_key`; adds the `(fund_id, as_of_date, source, holding_key)` unique
  constraint so the `issuer_holdings_ingestion` worker upserts idempotently. Pure
  metadata note: `ix_security_identifiers_scheme_value` (created in 0003) is now
  also declared on the model, reconciling a prior autogenerate drift — no DDL
  change, no runtime behaviour change.
- `0007_fx_rates_status_payload` — adds `status` + `raw_payload_json` to
  `fx_rates` so the `fx_ingestion` worker can record provenance.
- `0008_document_snapshots_ingestion` — extends `document_snapshots` with
  metadata, `fetched_at`, and change-detection columns (`change_status`,
  `previous_*`), plus the `(fund_id, document_type, source, content_hash)` unique
  constraint for idempotent document ingestion.
- `0009_workspace_scoped_alerts` — replaces the old global `alerts` +
  `workspace_alerts` split with a single **workspace-scoped** `alerts` table
  (status/source/related-entity pointers/`dedupe_key`/seen+resolved timestamps)
  and the `(workspace_id, dedupe_key)` unique constraint for idempotent
  `alert_generation`.
- `0010_exposure_snapshots` — adds the derived exposure store
  (`exposure_snapshots` + `exposure_rows`) the `exposure_recompute` worker writes:
  a per-workspace `input_hash` for idempotency, coverage/unclassified weights,
  missing-holdings/FX counts, and generic `dimension`/`bucket`/`label` rows.
  Unique on `(workspace_id, as_of_date, input_hash)`.

`uv run alembic upgrade head` applies the full chain (current head **0019**;
revisions past `0010` are listed in `alembic/versions/`). When a database is
available, confirm there is no drift with
`uv run alembic revision --autogenerate -m check` (it should produce an empty
migration). In Docker, run migrations via the one-shot `migrate` service —
see [`docs/operations.md`](docs/operations.md).

## Seed data

```bash
uv run python -m app.seed.seed_data
```

Idempotent — it is a no-op if funds already exist. Everything is tagged
`source = "seed"` and uses placeholder prices / holdings / distributions. It also
seeds one default **user**, one **workspace** ("Personal", `base_currency=GBP`)
with an `owner` membership and a `base_currency` setting, the portfolio positions
scoped to that workspace, and four **scheduled jobs** (daily price, daily FX,
weekly holdings, monthly document snapshot — definitions only, not executed).
Seeded funds:

- **VUSA** — Vanguard S&P 500 UCITS ETF (London GBP listing).
- **ISF** — iShares Core FTSE 100 UCITS ETF (London listing, **GBX/pence**).
- **JPMorgan Global Equity Premium Income Active UCITS ETF** — **one fund with
  multiple listings** (JEPG/GBP, JEGP/USD, JEPG/EUR on Xetra) to demonstrate the
  "ticker is not identity" rule.

## Adding instruments & price ingestion

This is the first real ingestion path: a client submits a symbol, the backend
resolves its identity, creates/reuses the fund + listing, queues backfill jobs,
and a worker fetches real prices that the existing read API then serves.

**Why identity resolution is separate from storage.** The thing a client types
(a ticker) is not the thing we store identity by. Resolution maps an external
identifier → a canonical instrument; storage (`funds`/`fund_listings`) is keyed
on ISIN + (fund, ticker, exchange). Keeping them separate means we can swap
resolver providers, record provenance, and refuse to store anything we can't
identify confidently. Every resolved identifier is recorded in
`security_identifiers` (scheme, value, source, confidence, raw payload).

**Why ticker-only lookup is ambiguous.** A ticker is exchange- and
currency-local: the same ticker can exist on multiple venues or map to different
funds, and one fund has many tickers. So ISIN/FIGI are preferred. If a ticker
cannot be resolved to a single high-confidence instrument **with an ISIN**, the
API returns candidates and creates nothing — it never guesses.

### `POST /api/v1/instruments`

Request (`exchange`/`currency` optional but improve confidence):

```bash
curl -s -X POST http://localhost:8080/api/v1/instruments \
  -H 'content-type: application/json' \
  -d '{"symbol":"VUSA","symbol_type":"ticker","exchange":"LSE","currency":"GBP"}'
# symbol_type ∈ ticker | isin | figi | sedol | cusip
```

Outcomes:

- **202 Accepted** — single high-confidence match. Reuses the fund if the ISIN
  already exists (else creates it `status=pending`), reuses/creates the listing,
  and **queues backfill `job_runs`** (`price_ingestion`, `distribution_ingestion`,
  `issuer_facts_ingestion`, `issuer_holdings_ingestion`,
  `document_snapshot_ingestion`). Returns `fund_id`, `fund_listing_id`,
  `resolved`, `created`, and `job_run_ids`.
- **409 Conflict** — ambiguous/low-confidence; returns `candidates`, creates
  nothing.
- **404 Not Found** — no match (`{"error":{"code":"instrument_not_found",...}}`).

Resolver providers are isolated behind an interface (`app/services/resolver.py`):

- `stub` (default, `RESOLVER_DEFAULT_PROVIDER=stub`) — offline, deterministic
  fixture so the system works with no network/API key. Knows the seeded
  instruments and one ambiguous ticker (`AMBI`).
- `openfigi` — calls the OpenFIGI v3 mapping API (`OPENFIGI_API_KEY` optional).
  Note OpenFIGI returns FIGI, not ISIN, so ticker lookups there are reported as
  lower confidence and will not auto-create a fund.

### Running price ingestion (the one real worker)

The worker is callable from the CLI (and by the API job trigger; cron later):

```bash
# Ingest the queued backfill for one listing (claims its queued job_run):
uv run python -m app.workers.run price_ingestion --fund-listing-id 1

# Ingest all listings:
uv run python -m app.workers.run price_ingestion

# Choose a source explicitly:
uv run python -m app.workers.run price_ingestion --fund-listing-id 1 --source yfinance
```

It fetches daily prices, **upserts** into `prices` (idempotent on
`fund_listing_id + price_date + source`), records `records_inserted/updated/failed`
and a status (`success` / `partial_success` / `failed`) on the `job_run`, and
stamps `fund_listings.last_price_at` (flipping `pending` → `active`).

- **Price source (v1):** `stooq` by default (`PRICE_SOURCE_DEFAULT`), with a
  Yahoo-chart `yfinance` fallback. Both are isolated behind `app/sources/` and
  every price row stores its `source`.
- **Triggering via API:** `POST /api/v1/jobs/{job_id}/run` runs the real worker
  for `price_ingestion`, `issuer_facts_ingestion`, `distribution_ingestion`,
  `issuer_holdings_ingestion` and `fx_ingestion` scheduled jobs and a
  `success_stub` for every other job type. It runs **synchronously** for now;
  this will move to a background worker later (no queue/broker yet). A best-effort
  guard returns `409 job_already_running` if a run for that job is still
  `running` (e.g. a previous crash left it mid-flight).

### Running issuer facts ingestion (the second real worker)

Enriches **fund facts** (official name, provider, domicile, base currency,
distribution policy, strategy, OCF/TER) from an issuer source, records
provenance on `funds.source`, stamps `last_refreshed_at`, and flips `pending` →
`active`:

```bash
# Enrich one fund (claims its queued issuer_facts_ingestion backfill run):
uv run python -m app.workers.run issuer_facts_ingestion --fund-id 1

# Enrich all pending/stale/seed-sourced funds:
uv run python -m app.workers.run issuer_facts_ingestion
```

- **Source priority:** it never overwrites a higher-priority source (e.g.
  `manual`), but an issuer **outranks `seed`**; empty fields are always filled.
  Re-runs are idempotent (no further field changes once applied).
- **Provider (v1):** an **offline fixture** (`issuer_fixture`, in
  `app/sources/issuer.py`) so it needs no network and tests use no live calls.
  Real per-issuer adapters slot in behind the `IssuerFactsSource` protocol.
- `records_inserted` counts funds activated (`pending` → `active`),
  `records_updated` counts fields changed, `records_failed` counts funds with no
  issuer match.

### Running distribution ingestion (the third real worker)

Upserts **declared distributions** (ex/record/payment/distribution dates,
per-share amount, currency, distribution type, frequency, share class, status)
into `distributions` through the `DistributionSource` adapter boundary.
Distributions belong to the *fund* (not a listing):

```bash
# Ingest distributions for one fund (claims its queued distribution backfill run):
uv run python -m app.workers.run distribution_ingestion --fund-id 1

# Ingest all distributing funds (skips accumulating funds):
uv run python -m app.workers.run distribution_ingestion

# Choose a source explicitly:
uv run python -m app.workers.run distribution_ingestion --source distribution_fixture

# Live J.P. Morgan fund distribution export (explicit-only; --url required):
uv run python -m app.workers.run distribution_ingestion \
  --fund-id 2 \
  --source jpmorgan_distributions \
  --url "https://am.jpmorgan.com/FundsMarketingHandler/excel?country=gb&cusip=IE0003UVYC20&locale=en-GB&role=adv&type=fundDistribution"

# Live Vanguard product-data distributionHistory — VUSA has a candidate known config,
# so no --url is needed (the config supplies the product-data URL):
uv run python -m app.workers.run distribution_ingestion --fund-id 3 --source vanguard_distributions

# Verify-only: one guarded fetch+parse of the config, no ingestion (reports if promotable):
uv run python -m app.workers.run distribution_ingestion --fund-id 3 --source vanguard_distributions --verify-source

# --url still overrides the known config for a single fund:
uv run python -m app.workers.run distribution_ingestion \
  --fund-id 3 \
  --source vanguard_distributions \
  --url "https://api.vanguard.com/rs/gre/gra/1.7.0/datasets/urd-product-port-specific.json?vars=portId:9503,issueType:F"

# Offline parser for a manually exported Vanguard distribution file (JSON/JSONP/CSV):
uv run python -m app.workers.run distribution_ingestion --fund-id 3 --source vanguard_distributions_export --url ./vanguard_dist.json

# All held funds in a workspace, bounded (only funds with a matching config fetch; the rest no-op):
uv run python -m app.workers.run distribution_ingestion --workspace-id 1 --source vanguard_distributions --limit 10
```

- **Idempotent upsert:** keyed on the `(fund_id, ex_date, source)` unique
  constraint, so re-runs and backfills never duplicate rows. `records_inserted`
  counts new rows, `records_updated` counts rows whose amount/dates/type/status
  changed, `records_failed` counts per-record errors. A row with no parseable
  ex-date falls back to its distribution/payment/record date as the identity date.
  Bad rows are isolated (skipped), never failing the whole file.
- **Default provider:** an **offline fixture** (`distribution_fixture`, in
  `app/sources/distributions.py`) so the worker/scheduler needs no network and
  tests use no live calls.
- **Live issuer adapters (explicit-only, `guarded_fetch`-ed):**
  - `jpmorgan_distributions` — J.P. Morgan AM fund distribution export
    (`FundsMarketingHandler?type=fundDistribution`; content-sniffed CSV / TSV /
    HTML-table or OOXML `.xlsx` via the stdlib; legacy binary `.xls` deferred);
  - `vanguard_distributions` — Vanguard product-data `distributionHistory`
    (official JSON/JSONP product-data API; JSONP wrapper stripped; conservative
    identifying official headers, no cookies / no fingerprint spoofing);
  - `vanguard_distributions_export` — **offline** parser for a manually exported
    official Vanguard distribution file (JSON/JSONP/CSV; pass the local path via
    `--url`);
  - `blackrock_ishares_distributions` — **planned** (no clean official iShares
    distribution endpoint verified; never guessed from the holdings ajax pattern).

  A live adapter uses an explicit `--url` first, then a *usable* (verified/candidate)
  per-fund URL from the **known issuer source config** registry (VUSA carries a
  candidate `vanguard_distributions` config — see below); without either it is a clean
  no-op (the distribution default stays the offline fixture, so the scheduler never
  makes a surprise live call). Every download goes through `guarded_fetch`
  (recent-success cache → source budget → fetch log → fetch) with conservative budgets
  (≤10/min, concurrency 1, ≥1s min delay). **Collection only** — no dividend
  forecasting, yield projection, tax treatment, total return or PnL anywhere in the
  backend (those live in the Rust local pricer).
- **Known issuer source config + verification:** a small in-code registry
  (`app/sources/issuer_source_config.py`) maps fund ISIN + source → a verified/
  candidate download URL, so a live `--source` runs without `--url`. Seeded configs:
  ISF holdings (`blackrock_ishares_holdings`, **verified** 2026‑06‑25 — a clean live
  fetch parsed ~107 holdings), JEPG holdings (`jpmorgan_etf_holdings`, candidate —
  live fetch returned a legacy binary `.xls`, reported `reason=binary_unsupported`),
  VUSA distributions (`vanguard_distributions`, candidate — live fetch hit a TLS
  handshake failure). A config is auto-used only when its live `--source` is named
  (the default stays the fixture). `--verify-source` runs one guarded fetch+parse and
  reports whether a config can be promoted to `verified`, **without ingesting** — its
  report carries a detected `payload_format` and a stable **`reason`** verdict
  (`verified` / `binary_unsupported` / `zero_rows` / `missing_fields` / `cache_hit` /
  `budget_blocked` / `no_url` / `fetch_error` / `unknown_source`) so the evidence is
  precise. The parsers content-sniff CSV/TSV/HTML-table **and OOXML `.xlsx`** (stdlib,
  no pandas); the legacy binary `.xls` (OLE2) stays deferred. The planner flags
  `needs_url_config` for funds without a config; diagnostics count config coverage; the
  capabilities endpoint exposes `requires_url` / `known_config_available` /
  `config_status` / `example_fund_identifiers`. **Never** mark a config `verified`
  without a clean live fetch+parse, **never** make a candidate the default, **never**
  guess a distribution URL from a holdings URL, and **never** reach a source via browser
  automation / cookies / TLS spoofing. See `docs/data_sources.md` §D2.
- **Triggering via API:** `POST /api/v1/jobs/{job_id}/run` runs the real worker
  for `distribution_ingestion` scheduled jobs (alongside `price_ingestion` and
  `issuer_facts_ingestion`).

Reading distributions + the distribution source catalogue:

```bash
curl -s "http://localhost:8080/api/v1/funds/1/detail"
curl -s "http://localhost:8080/api/v1/funds/1/distributions"
curl -s "http://localhost:8080/api/v1/workspaces/1/market-data-plan"
curl -s "http://localhost:8080/api/v1/data-sources/capabilities?data_type=distributions"
```

### Running holdings ingestion (the fourth real worker)

Upserts **look-through holdings** (constituent name, identifiers, country/sector/
industry/currency, weight, market value, shares) into `fund_holdings` through the
`HoldingsSource` adapter boundary. Holdings belong to the *fund* (not a listing)
and are snapshotted by disclosure date (`as_of_date`):

```bash
# Ingest holdings for one fund (offline fixture default; claims a queued backfill):
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 1

# Ingest all eligible funds:
uv run python -m app.workers.run issuer_holdings_ingestion

# Choose a source explicitly:
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 1 --source holdings_fixture

# Live iShares/BlackRock holdings CSV — ISF has a candidate known config, so no --url:
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 1 --source blackrock_ishares_holdings

# Verify-only: one guarded fetch+parse of the config, no ingestion (reports if promotable):
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 1 --source blackrock_ishares_holdings --verify-source

# --url still overrides the known config for a single fund:
uv run python -m app.workers.run issuer_holdings_ingestion \
  --fund-id 1 \
  --source blackrock_ishares_holdings \
  --url "https://www.blackrock.com/uk/individual/products/251795/ishares-ftse-100-ucits-etf-inc-fund/1472631233320.ajax?dataType=fund&fileName=ISF_holdings&fileType=csv"

# Live J.P. Morgan ETF holdings export — JEPG has a candidate known config (no --url):
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 2 --source jpmorgan_etf_holdings

# All held funds in a workspace, bounded (only funds with a matching config fetch; rest no-op):
uv run python -m app.workers.run issuer_holdings_ingestion --workspace-id 1 --source blackrock_ishares_holdings --limit 10

# Vanguard exported-file parser (offline; --url = local exported CSV path):
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 3 --source vanguard_holdings_export
```

- **Idempotent upsert:** each holding gets a deterministic `holding_key`
  (ISIN > FIGI > CUSIP > SEDOL > normalised `name|ticker`) and rows are unique on
  `(fund_id, as_of_date, source, holding_key)`, so re-runs/backfills never
  duplicate. `records_inserted`/`records_updated`/`records_failed` are counted; the
  run message carries the full breakdown (`selected_funds`/`fetched`/`skipped`/
  `bad_rows`/`source`/`is_fixture`). A bad row (no name / unparseable weight) is
  isolated, never failing the whole file. Different sources keep their own snapshot.
- **Snapshot reads:** a fund may carry seed + fixture + live-issuer (+ manual)
  holdings. Reads select **one coherent snapshot** per fund (highest-priority source
  — manual > vanguard export > live issuer > fixture > seed — then newest
  `as_of_date`) so sources never mix and look-through exposure is not double-counted.
- **Providers:** the default is an **offline fixture** (`holdings_fixture`) with
  realistic top-10 holdings for the seeded ETFs (no network, tests use no live
  calls). Live, **explicit-only**, `guarded_fetch`-ed issuer adapters
  (`app/sources/holdings.py`): `blackrock_ishares_holdings` (issuer CSV, scans past
  the metadata preamble), `jpmorgan_etf_holdings` (content-sniffed CSV/TSV/HTML
  table or OOXML `.xlsx` via the stdlib; legacy binary `.xls` deferred,
  `reason=binary_unsupported`). `vanguard_holdings_export` parses a manually
  exported official Vanguard file; live `vanguard_holdings` is **planned** (no stable
  official endpoint verified — no HTML scraping). Live adapters take a configured/
  known download URL, never the scheduler default, route through source budgets +
  fetch logs + request cache, and store no secrets.
- **Compute boundary:** collection only — **no** look-through analytics, PnL, total
  return or index-constituent substitution; identity resolution stays a separate
  worker (holdings ingestion never calls OpenFIGI).
- **Triggering via API:** `POST /api/v1/jobs/{job_id}/run` runs the real worker
  for `issuer_holdings_ingestion` scheduled jobs.

Reading holdings + provenance (and the holdings source catalogue):

```bash
curl -s "http://localhost:8080/api/v1/funds/1/holdings"
curl -s "http://localhost:8080/api/v1/funds/1/constituents"
curl -s "http://localhost:8080/api/v1/workspaces/1/market-data-plan?include_constituents=true"
curl -s "http://localhost:8080/api/v1/data-sources/capabilities?data_type=holdings"
```

### Running FX ingestion (the fifth real worker) + currency-aware valuation

Upserts **FX rates** (`rate` = quote-currency units per 1 base-currency unit) into
`fx_rates` through the `FxSource` adapter boundary, then powers multi-currency
valuation across the dashboard/summary/diagnostics:

```bash
# Infer the needed currencies from the data and ingest GBP -> {USD, EUR, ...}:
uv run python -m app.workers.run fx_ingestion

# Choose the source explicitly:
uv run python -m app.workers.run fx_ingestion --source fx_fixture

# Pin base + one or more quote currencies:
uv run python -m app.workers.run fx_ingestion --base GBP --quote USD
uv run python -m app.workers.run fx_ingestion --base GBP --quote USD --quote EUR
```

- **Idempotent upsert:** unique on `(rate_date, base_currency, quote_currency,
  source)`; a re-run inserts nothing and only counts an update when a rate genuinely
  changes. Distinct sources coexist; only `rate`/`status`/`raw_payload_json` are
  mutable. One bad currency pair never fails the whole job.
- **Inverse / triangulation:** only canonical pairs are stored; inverse and cross
  rates are computed in the lookup service (no duplicated inverse rows).
- **Provider (v1):** an **offline fixture** (`fx_fixture`, in `app/sources/fx.py`)
  with a consistent USD-anchored cross-rate table and a bounded daily history, so
  it needs no network and tests use no live calls. A real ECB adapter slots in
  behind the `FxSource` protocol later.

### Running document ingestion (the sixth real worker) + change detection

Upserts **fund documents** (factsheet/KID/KIID/prospectus/annual report) into
`document_snapshots` through the `DocumentSource` adapter boundary, hashing content
and detecting changes:

```bash
# Ingest documents for one fund (claims its queued document backfill run):
uv run python -m app.workers.run document_snapshot_ingestion --fund-id 1

# Ingest all eligible funds:
uv run python -m app.workers.run document_snapshot_ingestion

# Choose the source explicitly:
uv run python -m app.workers.run document_snapshot_ingestion --source document_fixture
```

- **Content hash + change detection:** unique on `(fund_id, document_type, source,
  content_hash)`. A re-run with identical content is a no-op (bumps `fetched_at`);
  a changed hash inserts a **new** snapshot (`change_status=changed`) that links the
  prior one — history is preserved, nothing is deleted.
- **Provider (v1):** an **offline fixture** (`document_fixture`, in
  `app/sources/documents.py`) with small deterministic text per document, so it
  needs no network and tests use no live calls. **No PDF parsing / OCR.**
- **Reads:** documents appear in `GET /api/v1/funds/{id}/documents` (with
  `document_type` / `latest_only` filters), `GET /api/v1/documents/{id}`, fund
  detail, the dashboard, and diagnostics.

### Running alert generation (the seventh real worker)

Turns existing backend signals into **workspace-scoped alerts** in the `alerts`
table — no external provider, database-only:

```bash
# Generate alerts for every workspace (claims a queued alert_generation run):
uv run python -m app.workers.run alert_generation

# Generate for one workspace only:
uv run python -m app.workers.run alert_generation --workspace-id 1
```

- **Rules (deterministic, pure):** changed/new/missing documents, failed jobs,
  stale/missing prices, missing/stale FX, missing/stale holdings, pending/ambiguous
  instruments, price-source conflicts, upcoming distributions. Thresholds are
  centralised in `app/services/alert_rules.py`
  (`PRICE_STALE_DAYS=5`, `HOLDINGS_STALE_DAYS=45`, `FAILED_JOB_LOOKBACK_DAYS=7`, …).
- **Idempotent:** keyed by `(workspace_id, dedupe_key)`. A re-run updates
  `last_seen_at` instead of duplicating. An issue that disappears auto-resolves
  (where the rule is auto-resolvable); a returning issue reactivates a resolved
  alert; a **dismissed** alert with the same key stays dismissed (only a
  *materially* different issue — a new `dedupe_key` — produces a new alert).
- **Per-workspace isolation:** one workspace failing is recorded
  (`records_failed`) and does not abort the others.
- See **Alerts** below for severities/categories/statuses and the API.

### Running exposure recompute (the eighth real worker)

Turns look-through exposure into a cached, inspectable dataset in
`exposure_snapshots` / `exposure_rows` — database-only, no provider:

```bash
# Recompute exposure for every workspace (claims a queued run):
uv run python -m app.workers.run exposure_recompute

# Recompute for one workspace only:
uv run python -m app.workers.run exposure_recompute --workspace-id 1
```

- **Inputs:** current positions/units, latest listing prices, FX (via the same
  `FxIndex` as the portfolio summary), and the selected holdings snapshots.
- **Dimensions:** `fund` (direct position weight), `holding` (look-through),
  `country`, `sector`, `industry`, `currency`, `source` (provenance). Generic
  `dimension`/`bucket`/`label` rows so direct equities/bonds/cash slot in later.
- **Idempotent:** a deterministic `input_hash` over the material inputs. If the
  latest snapshot already has that hash, nothing is written; a changed input
  inserts a **new** snapshot (old ones kept as history for drift detection).
- **Honest coverage:** `coverage_weight` (looked-through fraction),
  `unclassified_weight`, an `Unclassified` bucket, and `missing_holdings_count` /
  `missing_fx_count`. Missing FX marks affected rows `fx_missing` rather than
  pretending a conversion happened; currency look-through marks `approximate`
  where it falls back to the listing currency.
- See **Exposure** below for the model, endpoints and dashboard/diagnostics.

### What is real vs stubbed here

| Path | Status |
|---|---|
| Identity resolution (`stub` + `openfigi`) | **real** |
| `POST /api/v1/instruments` create/reuse + backfill queueing | **real** |
| `price_ingestion` worker (Stooq/Yahoo) | **real** |
| `issuer_facts_ingestion` worker (offline **fixture** issuer source) | **real (fixture provider)** |
| `distribution_ingestion` worker (distributions → `distributions`; offline **fixture** default + live **`jpmorgan_distributions`** & **`vanguard_distributions`** adapters + offline **`vanguard_distributions_export`** parser, explicit-only; `blackrock_ishares_distributions` planned; collection only, no dividend forecasting) | **real (fixture default; live JPM + Vanguard)** |
| `issuer_holdings_ingestion` worker (offline **fixture** default + live **`blackrock_ishares_holdings`** / **`jpmorgan_etf_holdings`** issuer adapters + offline **`vanguard_holdings_export`** parser, explicit-only; `vanguard_holdings` planned; collection only) | **real (fixture default; live iShares + JPM)** |
| `fx_ingestion` worker (offline **fixture** FX source) + currency-aware valuation | **real (fixture provider)** |
| `document_snapshot_ingestion` worker (offline **fixture** document source) + content-hash change detection | **real (fixture provider)** |
| `alert_generation` worker (database-only; no external provider) | **real** |
| `exposure_recompute` worker (database-only; no external provider) | **real** |
| Scheduler worker (in-process; claims/leases/runs due jobs) | **real** |
| Job leasing / duplicate-run prevention | **real** |
| Source rate-budgets + fetch logs / request cache | **real** |
| Market-data planner (read-only, deduped, prioritised) | **real** |
| `constituent_identity_resolution` worker (offline **fixture** + live OpenFIGI batches) | **real (fixture default; OpenFIGI optional)** |
| `constituent_eod_price_ingestion` worker (offline **fixture** + live Stooq/yfinance) | **real (fixture default; Stooq/yfinance optional)** |
| `instrument_eod_price_ingestion` worker (unified: constituents + resolved imported direct holdings → `instrument_prices`) | **real (fixture default; Stooq/yfinance optional)** |
| True constituent look-through valuation (weight-based implied value + price/FX coverage, in `exposure_recompute`) | **real (database-only)** |
| Exposure drift + top movers (compares two snapshots; weight/value deltas + price-context estimate) | **real (database-only)** |
| Top-holding performance / price-context contribution (base implied value × local price return over a window) | **real (database-only)** |
| Instrument onboarding / data-readiness orchestration (`instrument_onboarding`; plan + run over the existing ingestion/recompute workers) | **real (orchestration; fixture default)** |
| Onboarding run history / stage observability (typed stage rows + child-run correlation in `job_runs.payload_json`; bounded read model) | **real (read model; database-only)** |
| Job-run timeline / failure drilldown (generic bounded read model over all `job_runs`; secrets-masked; fetch-log correlation `partial`) | **real (read model; database-only)** |
| `broker_csv_import` worker + preview/commit API (`generic_csv_v1`; offline; resolves existing identity only) → canonical transaction ledger | **real (offline)** |
| `imported_instrument_resolution` worker + resolve API (existing identity first, then shared fixture/OpenFIGI resolvers; dry-run; relink + recompute) | **real (offline fixture default)** |
| Position reconciliation (`…/positions`; buys−sells per instrument + cash per currency; idempotent snapshot) | **real (bounded; not PnL)** |
| `portfolio_valuation_recompute` worker + valuation API (`…/portfolio/valuation`; values reconciled positions/cash from already-ingested prices/FX; readiness blockers) | **real (bounded read model; not PnL)** |
| Valuation history / summary read model + dashboard block (`…/portfolio/valuation/history` & `…/summary`; oldest-first coverage/readiness series over persisted snapshots; no recompute on read) | **real (bounded; snapshots only; not PnL/returns)** |
| `rates_ingestion` worker (reference rates → `reference_rates`; offline **fixture** default + live **`us_treasury_rates`** par-yield & **`ecb_rates`** (ECB key rates + €STR) adapters, explicit-only; collection only, no curves) | **real (fixture default; live US Treasury + ECB)** |
| Yield curves / curve fitting / bootstrapping / discount factors / forward rates / bond pricing | planned (Rust local pricer, **never** the backend) |
| Portfolio PnL / tax lots / total-return / performance attribution / dividend forecasting | planned (Rust GUI / local pricer, **never** the backend) |
| Bond reference / broker-specific parsers | planned |

The machine-readable version of this table is `GET /api/v1/capabilities`
(`features` / `workers` / `data_types`). See [`docs/data_sources.md`](docs/data_sources.md)
for the full data-source catalogue and adapter strategy, and
[`SOURCES.md`](SOURCES.md) for the vendor research.

**`issuer_facts_ingestion` is real but provider-light:** it enriches fund facts
(official name, provider, domicile, base currency, distribution policy, strategy,
OCF/TER) through the `IssuerFactsSource` adapter boundary
(`app/sources/issuer.py`), records provenance on `funds.source`, respects source
priority (never clobbers `manual`; an issuer outranks `seed`), stamps
`last_refreshed_at` and flips `pending` → `active`. The shipped provider is an
**offline fixture** (`issuer_fixture`) so it works with no network and tests
never hit live APIs. Real per-issuer adapters (Vanguard / iShares / JPMAM
scraping or API) slot in behind the same protocol later.

**`distribution_ingestion` is real but provider-light** (same shape as issuer
facts): it upserts declared distributions (ex/record/payment dates, amount,
currency, status) into `distributions` through the `DistributionSource` adapter
boundary (`app/sources/distributions.py`), keyed on the
`(fund_id, ex_date, source)` unique constraint so re-runs/backfills are
idempotent, records `source` provenance, and writes a `job_runs` row. The shipped
provider is an **offline fixture** (`distribution_fixture`) so it needs no network
and tests never hit live APIs. Real adapters (issuer distribution pages/PDFs, or a
secondary corporate-actions API such as Tiingo/Alpha Vantage) slot in behind the
same protocol later.

**`issuer_holdings_ingestion` is real with live issuer adapters:** it upserts
look-through holdings into `fund_holdings` through the `HoldingsSource` adapter
boundary (`app/sources/holdings.py`), keyed on
`(fund_id, as_of_date, source, holding_key)` so re-runs/backfills are idempotent,
records `source` provenance, writes a `job_runs` row, and feeds look-through
exposure. The default provider is an **offline fixture** (`holdings_fixture`). Live,
**explicit-only**, `guarded_fetch`-ed issuer adapters parse the issuer-published
files: `blackrock_ishares_holdings` (iShares/BlackRock CSV — scans past the metadata
preamble for the holdings header) and `jpmorgan_etf_holdings` (J.P. Morgan
FundsMarketingHandler export — content-sniffed CSV/TSV/HTML table or OOXML `.xlsx`
via the stdlib; legacy binary `.xls` deferred). `vanguard_holdings_export` parses a manually exported official Vanguard
file (offline); live `vanguard_holdings` stays **planned** (no stable official
machine-readable endpoint verified — no brittle HTML scraping). The live adapters
take a configured/known download URL (or `--url` override, single-fund), route every
call through source budgets + fetch logs + request cache, store no secrets, and are
**collection only** — no look-through analytics/PnL, and identity resolution stays a
separate worker (no OpenFIGI calls from holdings ingestion).

**`fx_ingestion` is real but provider-light** (same shape as the others): it
fetches FX rates through the `FxSource` adapter boundary (`app/sources/fx.py`) and
upserts them into `fx_rates`, keyed on the `(rate_date, base_currency,
quote_currency, source)` unique constraint so re-runs/backfills are idempotent,
records `source`/`status` provenance, and writes a `job_runs` row. With no args it
**infers** the currencies it needs from workspaces/listings/distributions/holdings;
`--base`/`--quote` pin them. The shipped provider is an **offline fixture**
(`fx_fixture`) — a consistent USD-anchored cross-rate table — so it needs no
network and tests never hit live APIs. A real ECB EUR-reference-rate adapter slots
in behind the same protocol later (it is catalogued but not enabled).

Conversion/lookup is a separate read-side service (`app/services/fx.py`):
`get_fx_rate` / `convert_amount` resolve a pair as **direct → inverse →
triangulated (via USD/EUR/GBP)**, return a clear *missing* status rather than a
silent rate of 1, and carry rate/source/freshness plus source-policy metadata
(`requested_source`/`effective_source`/`fallback_used`). The dashboard portfolio
summary now values each position in its **local/listing currency** and converts to
the **workspace base currency** with this engine, exposing
`market_value_local`/`market_value_base`/`fx_rate`/`fx_source`/`fx_status`;
`total_market_value` is in base currency. Distributions carry an optional
base-currency overlay (`amount_base`), and diagnostics count
`missing_fx_rates`/`stale_fx_rates`/`unconverted_positions`/`fx_conversion_failures`.

**`document_snapshot_ingestion` is real but provider-light** (same shape as the
others) and adds **content-hash change detection**: it fetches published fund
documents (factsheet/KID/KIID/prospectus/annual report) through the
`DocumentSource` adapter boundary (`app/sources/documents.py`), hashes their
content (`app/services/documents.py:compute_document_hash` — SHA-256 of the
bytes/text, or stable metadata when absent), and upserts into `document_snapshots`
keyed on `(fund_id, document_type, source, content_hash)`. Per document, relative
to the latest stored snapshot of the same (fund, type, source): **new** (first
version) and **changed** (different hash) insert a NEW snapshot — *old snapshots
are preserved as history* and the new row links the prior one
(`previous_snapshot_id`/`previous_content_hash`); **unchanged** bumps `fetched_at`
without a new row. The shipped provider is an **offline fixture**
(`document_fixture`) with small deterministic content — **no PDF text extraction /
OCR**. Documents flow into fund detail, the dashboard and diagnostics
(`missing_documents`/`stale_documents`/`changed_documents`/`new_documents`/`failed_document_jobs`).

**Document storage stance.** The DB stores document *metadata + URLs + content
hashes + change history* only — never large binary blobs. Downloaded PDFs would
later go to object storage / a filesystem cache; the content hash is what decides
whether a document changed. PDF text extraction and structured-field/text diffing
are explicitly future workers; tests use fixture bytes/text and never download a
live PDF. A real issuer document adapter (Vanguard / iShares / JPMAM product pages)
slots in behind the same `DocumentSource` protocol later.

## Alerts

Alerts are **workspace-scoped, derived data**: the `alert_generation` worker
turns existing diagnostics / change signals into structured rows in `alerts` that
the GUI can show, read, dismiss, resolve and filter. There is **no email/push
delivery** and **no scheduler** — generation is run on demand (CLI or job
trigger), synchronously.

**Severities:** `info` · `warning` · `error` · `critical`.
**Categories:** `document` · `price` · `fx` · `holdings` · `distribution` ·
`job` · `instrument` · `source` · `data_quality` · `system`.
**Statuses:** `active` → `read` (opened) / `dismissed` (hidden) / `resolved`
(issue gone).

**Current rules** (`app/services/alert_rules.py`, pure & deterministic):

| Rule | Severity | dedupe_key prefix | Auto-resolves |
|---|---|---|---|
| Document changed | warning | `document_changed` | yes |
| New document | info | `document_new` | no (one-time) |
| Missing key documents (factsheet/KID/KIID/prospectus) | warning | `document_missing` | yes |
| Failed / partial job run | error / warning | `job_failed` | yes (ages out of lookback) |
| Stale price (`>PRICE_STALE_DAYS`) | warning | `price_stale` | yes |
| Missing price | error | `price_missing` | yes |
| Missing FX path to base | warning | `fx_missing` | yes |
| Stale FX rate | warning | `fx_stale` | yes |
| Missing holdings snapshot | warning | `holdings_missing` | yes |
| Stale holdings (`>HOLDINGS_STALE_DAYS`) | warning | `holdings_stale` | yes |
| Pending fund/listing | info | `instrument_pending` | yes |
| Ambiguous identifier (confidence ≠ high) | warning | `instrument_ambiguous` | yes |
| Price-source conflict | info | `source_conflict` | yes |
| Upcoming/declared distribution | info | `distribution_new` | no (one-time) |

**Idempotency / lifecycle:** keyed by `(workspace_id, dedupe_key)`. Re-running
updates `last_seen_at` (and any changed content) rather than inserting a
duplicate. When an issue disappears, an auto-resolvable alert is marked
`resolved`; if it returns, the resolved alert reactivates. A **dismissed** alert
with the same key is *not* resurrected — only a materially different issue (a new
`dedupe_key`, e.g. a new document content hash) creates a fresh alert.

**Thresholds** are centralised constants: `PRICE_STALE_DAYS`, `FX_STALE_DAYS`,
`HOLDINGS_STALE_DAYS`, `DOCUMENT_STALE_DAYS`, `FAILED_JOB_LOOKBACK_DAYS`.

**Dashboard / diagnostics:** the dashboard carries recent open alerts plus an
`alert_summary` (active/unread counts, highest severity, by-severity/by-category
breakdowns); diagnostics carries `active_alerts` / `unread_alerts` /
`critical_alerts` / `error_alerts` / `warning_alerts` / `document_alerts` /
`price_alerts` / `fx_alerts` / `job_alerts`.

**Future rules:** manual/estimated-value flags, richer cross-source conflicts,
configurable per-workspace thresholds/rule toggles, and notification delivery —
all intentionally out of scope here.

## Exposure

Look-through exposure is a **derived, cached** dataset (`exposure_snapshots` +
`exposure_rows`) written by the `exposure_recompute` worker — inspectable,
timestamped, provenance/coverage aware, and reusable by the dashboard, the
exposure API and (later) drift detection. The legacy ad-hoc slice computation
(`GET /api/v1/exposure`, the dashboard's `exposures` block) is kept for backwards
compatibility.

**Inputs** (everything is DB-derived; no network): workspace positions + units,
latest listing prices, FX conversions, and the selected holdings snapshots, plus
base currency, as-of date and source policy.

**Computation.** Each position is valued in the workspace base currency (same
`FxIndex` as the portfolio summary). Direct **fund** weight =
`position_market_value_base / total_portfolio_market_value_base`. Look-through
contribution = `fund_weight * holding_weight` (e.g. 60% in VUSA × 7% Apple = 4.2%
Apple). Market value contribution = `total_market_value_base * lookthrough_weight`.

**Dimensions:** `fund`, `holding`, `country`, `sector`, `industry`, `currency`,
`source`. Country/sector/industry/holding/source sum look-through weight by the
holding attribute; an **Unclassified** bucket captures the un-looked-through
remainder (no snapshot, or constituent weights summing < 1). Currency prefers the
holding currency where present and otherwise falls back to the listing currency,
marked `approximate`. (`asset_class` is intentionally deferred until instruments
carry an asset-class field — the model already supports new dimensions with no
schema change.)

**Idempotency / input hash.** Each snapshot carries a deterministic `input_hash`
(SHA-256 of normalized positions/units, prices used, FX used, holdings snapshots,
base currency, as-of, source policy) plus component digests
(`position_snapshot_hash` / `holdings_snapshot_hash` / `fx_snapshot_hash`).
Recompute compares against the latest snapshot: same hash ⇒ nothing written;
changed inputs ⇒ a **new** snapshot (old ones preserved as history). Unique on
`(workspace_id, as_of_date, input_hash)`.

**Coverage & honesty.** `coverage_weight` (looked-through fraction),
`unclassified_weight`, `missing_holdings_count`, `missing_fx_count`; rows carry a
`status` (`ok` / `unclassified` / `missing_holdings` / `fx_missing` /
`approximate`) and `source`. Missing FX marks rows rather than fabricating a
conversion; snapshot `status` is `ok` / `partial` / `empty`.

**Dashboard / diagnostics.** The dashboard `exposure` block shows the latest
snapshot's top sectors/countries/currencies/holdings plus coverage, age and a
`cached` / `stale` / `recompute_needed` / `missing` status. Diagnostics add
`missing_exposure_snapshots`, `stale_exposure_snapshots`,
`exposure_recompute_failures`, `low_exposure_coverage`,
`missing_holdings_for_exposure`, `missing_fx_for_exposure` and
`unclassified_exposure_weight`. Optional alerts (category `exposure`):
`exposure_stale`, `exposure_low_coverage`, `exposure_recompute_failed`.

**Endpoints:** `GET /api/v1/workspaces/{id}/exposure` (latest snapshot; falls
back to an on-the-fly computation flagged `cached=false`), with
`?dimension=`/`?snapshot_id=`/`?limit=`, and
`GET /api/v1/workspaces/{id}/exposure/snapshots` for the history.

**Known limitations.** Exposure is a snapshot of *current* positions (not a time
series of valuations); coverage depends on fixture holdings (so it reads as
`partial`); `asset_class` and direct non-fund instruments are future work; there
is no drift-alerting beyond the snapshot history that would enable it.

**Future stock/constituent readiness.** The model is intentionally instrument-/
dimension-generic so ETF constituent equities, direct stock/bond/cash positions,
indices and derivatives can be added later behind a security master +
constituent EOD ingestion without a schema rewrite.

### True constituent look-through valuation

`exposure_recompute` now feeds resolved constituent **EOD prices** + **FX** into
the same snapshot, so you can value an ETF's *underlying* constituents (the total
Apple you hold across every fund), not just the fund wrapper. It is additive —
the fund-level dimensions above are unchanged — and lives in
`app/services/constituent_valuation.py`.

**Fund-level value vs constituent implied value.** The original look-through
distributes a fund's value through holdings *weights*. The constituent layer adds
the resolved instrument, its latest EOD price and the FX to base as **valuation
context**. Crucially, the implied constituent value is still **weight-based**:

```
implied_market_value_base = position_market_value_base × holding_weight
```

It is **not** a share×price notional. ETFs publish weights, not the exact share
counts inside *your* position, so the constituent price/FX is attached for
coverage/contribution context — never used to invent a notional. The
`valuation_method` field says which: `fund_weight_lookthrough` (no price) or
`fund_weight_with_constituent_price_context` (priced). `holding_market_value` /
`holding_shares_x_price` are reserved for when holdings carry exact
shares/market values.

**Dependency chain:** `issuer_holdings_ingestion` → `constituent_identity_resolution`
→ `constituent_eod_price_ingestion` → `fx_ingestion` → `exposure_recompute`. Each
link deepens coverage; a missing link is surfaced, never treated as zero.

**New dimensions** (the fund-level ones are unchanged):
- `constituent` — one bucket per resolved instrument (deduped across funds), with
  typed context columns (`instrument_id`, `instrument_listing_id`, `price_date`,
  `price_source`, `price_status`, `fx_rate`, `fx_source`, `valuation_method`).
  Unresolved weight aggregates into `__unresolved__`; the not-looked-through
  remainder into `__unclassified__`.
- `constituent_price_status` — a coverage **funnel** that sums to ~1.0:
  `priced_fresh` / `priced_stale` / `price_missing` / `fx_missing` /
  `missing_listing` / `unresolved_identity` / `unclassified`.
- `constituent_source` — priced weight by price provenance.

The `holding` rows are also enriched with the same constituent context columns.

**Coverage metrics** (weight-based, fractions of *total portfolio value*, nested
`holdings ≥ identity ≥ price ≥ fx`):

```
holdings_coverage_weight   # = coverage_weight (looked-through holdings)
identity_coverage_weight   # of that, resolved to a constituent instrument
price_coverage_weight      # of that, has a constituent EOD price (any freshness)
fx_coverage_weight         # of that, price currency converts to base
```

plus **distinct-resolved-instrument** counts (Apple via two ETFs counts once):
`constituent_count`, `resolved_constituent_count`, `priced_constituent_count`,
`stale_constituent_price_count`, `missing_constituent_price_count`,
`constituent_fx_missing_count`. Example: *VUSA holdings cover 40% of fund weight;
95% of that resolves to an identity; 90% of that has a fresh EOD price.*

**Status semantics** (constituent rows): `ok` (resolved + fresh price + FX),
`stale_price`, `fx_missing`, `price_missing`, `missing_listing`,
`unresolved_identity`, `unclassified`.

**Idempotency.** The `input_hash` now also folds in the holding identity links,
the constituent prices used and their FX, so a (re)resolution or a changed
constituent price yields a new snapshot; an unchanged rerun writes nothing.

**API / dashboard.** `GET .../exposure?dimension=constituent` and
`?dimension=constituent_price_status` return the new rows; every exposure
response / snapshot summary carries a `constituent_coverage` block, and the
dashboard `exposure` block adds `top_constituents` + `constituent_coverage`.

**Diagnostics** (workspace-scoped, derived from the latest snapshot):
`low_constituent_identity_coverage`, `low_constituent_price_coverage`,
`constituent_valuation_fx_missing`, `constituent_valuation_unclassified_weight`.
**Alerts** are conservative and grouped per workspace — and stay *silent* until
some constituents resolve (the clean pre-resolution state is a planner signal,
not an alert): `constituent_identity_coverage_low`,
`constituent_price_coverage_low` (category `exposure`),
`constituent_valuation_fx_missing` (category `fx`).

**Market-data planner** rollup gains `true_lookthrough_ready`,
`blocked_by_missing_identity`, `blocked_by_missing_price`,
`blocked_by_missing_fx` so the GUI can point at the job/data that unblocks the
valuation.

**Limitations.** Weight-based implied value only (no exact-share notional, no PnL
attribution, no total-return); coverage depends on fixture holdings/prices; rates
curves and broker CSV import are still future work.

```bash
# Full offline look-through chain for workspace 1
uv run python -m app.workers.run issuer_holdings_ingestion
uv run python -m app.workers.run constituent_identity_resolution --source constituent_identity_fixture
uv run python -m app.workers.run constituent_eod_price_ingestion --source instrument_price_fixture
uv run python -m app.workers.run fx_ingestion
uv run python -m app.workers.run exposure_recompute --workspace-id 1

curl -s "http://localhost:8080/api/v1/workspaces/1/exposure?dimension=constituent"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure?dimension=constituent_price_status"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure/snapshots"
curl -s "http://localhost:8080/api/v1/workspaces/1/dashboard"
curl -s "http://localhost:8080/api/v1/workspaces/1/diagnostics"
```

### Exposure drift and top movers

Drift **compares two exposure snapshots** for one workspace to explain *what
changed* — it is a read/compute layer (`app/services/exposure_drift.py`) over the
cached snapshots, with **no new table and no worker**. Default comparison is
**previous vs latest**; explicit `base_snapshot_id` / `comparison_snapshot_id`
are workspace-scoped (a snapshot from another workspace 404s — no cross-workspace
comparison).

**It is not PnL.** Drift compares snapshots; it does **not** infer that you (or
the ETF) traded anything, and `delta_market_value_base` is the change in the
weight-based *implied* value, not realised cash PnL or total return.

**Dimensions:** `constituent`, `country`, `sector`, `industry`, `currency`,
`source`, `constituent_price_status`. Constituent rows match by resolved
`instrument_id` (so Apple is one row even across funds), other dimensions by
`bucket`.

**Per-row deltas** (`comparison - base`): `delta_weight` / `abs_delta_weight`,
`delta_market_value_base` / `abs_delta_market_value_base`, status / price-status
change, and a `change_kind` ∈ `appeared | disappeared | increased | decreased |
status_changed | unchanged`. **Summary** adds `total_abs_weight_delta`,
`total_abs_market_value_delta_base`, the appeared/disappeared/changed counts and
`identity_/price_/fx_coverage_delta`.

**Price-context contribution (estimate, constituent only).** When a resolved
constituent has an EOD price in *both* snapshots, drift fetches the closes from
`instrument_prices` and reports `price_return = comparison/base − 1` and
`price_context_contribution_base = base_implied_market_value × price_return`.
This is labelled a **price-context estimate** — not PnL, not total return (no
shares, no trades).

**Dependency chain:** holdings → identity resolution → constituent prices → FX →
`exposure_recompute` (two snapshots) → drift comparison. Drift needs ≥2 snapshots;
with fewer it returns `status=insufficient_history`.

**Dashboard / diagnostics / alerts.** The dashboard `exposure.drift` block shows
the compact latest-vs-previous constituent drift (top movers + coverage deltas).
Diagnostics add `large_constituent_exposure_drift`, `large_sector_exposure_drift`,
`large_currency_exposure_drift`, `price_coverage_deteriorated`,
`fx_coverage_deteriorated`, `no_prior_exposure_snapshot_for_drift`. Conservative,
per-workspace, auto-resolving alerts (silent with <2 snapshots): category
`exposure` — `exposure_drift_constituent`, `exposure_drift_sector`,
`constituent_price_coverage_deteriorated`; category `fx` —
`constituent_fx_coverage_deteriorated`.

```bash
# Two snapshots: recompute, change a small input, recompute again
uv run python -m app.workers.run exposure_recompute --workspace-id 1
# (e.g. re-run holdings/price/fx ingestion, or edit a holding) then:
uv run python -m app.workers.run exposure_recompute --workspace-id 1

curl -s "http://localhost:8080/api/v1/workspaces/1/exposure/drift?dimension=constituent"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure/drift?dimension=sector"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure/drift?dimension=constituent&base_snapshot_id=1&comparison_snapshot_id=2"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure/top-movers?dimension=constituent&limit=10"
curl -s "http://localhost:8080/api/v1/workspaces/1/dashboard"
```

### Top-holding performance / price-context contribution

The bridge from "what *changed* between two snapshots?" (drift) to "what likely
*drove value* over this window?". A read/compute service
(`app/services/holding_performance.py`) over the cached snapshots +
`instrument_prices` — no table, no worker, no network.

**It is not PnL.** This is a **price-context contribution estimate**, not realised
PnL, total return or trade attribution, and it does **not** infer buys/sells or
ETF rebalance causes. For each resolved constituent held in both snapshots:

```
price_return                  = comparison_price / base_price − 1     # local ccy
price_context_contribution_base = base_implied_market_value_base × price_return
```

`base_implied_market_value_base` is the weight-based implied value from the base
snapshot (look-through), and `price_return` is a **local-currency** return (same
listing/currency at both endpoints, so the ratio is currency-neutral).

**FX handling (this slice).** FX drift between the two dates is **not** applied —
the return is local. The constituent currency→base rate as of each endpoint is
surfaced as context (`fx_rate_base`, `fx_rate_comparison`, `fx_source`) for a
future FX-adjusted return in the Rust GUI / local pricer; `price_return_basis` is
`local` (or `base` when the constituent is already base-currency). Non-base
constituents lacking an FX rate are counted (`fx_missing_count`) but still get a
local-return contribution.

**Snapshot/date selection.** Defaults to **previous-vs-latest** snapshot; each
constituent's base/comparison price is the exact bar its snapshot row captured
(so movement between two same-day snapshots is seen). Explicit `start_date` /
`end_date` switch to a uniform as-of window for every listing. Explicit snapshot
ids are workspace-scoped (no cross-workspace). Returns `insufficient_history`
(<2 snapshots) or `insufficient_price_data` (nothing priced).

**Bounded + SQL-friendly.** Snapshot rows are already one-per-constituent; prices
come from two batched `GROUP BY` / exact-date queries over the *capped* top-weight
listing set (`limit`, default 50) — never the whole price history, never an
unbounded per-instrument loop, no dataframe analytics. Anything heavier/interactive
belongs in the Rust GUI / local pricer.

**Per-row status:** `ok | missing_base_price | missing_comparison_price |
stale_price | unresolved | partial`. **Dashboard:** `exposure.top_holding_performance`
shows top positive/negative contributors + missing/FX counts. **Diagnostics**
(data-quality only — never "AAPL moved 5%"): `top_holding_performance_missing_prices`,
`top_holding_performance_fx_missing`, `top_holding_performance_insufficient_history`.
No new alerts (ordinary price moves are not alerted).

```bash
uv run python -m app.workers.run issuer_holdings_ingestion
uv run python -m app.workers.run constituent_identity_resolution --source constituent_identity_fixture
uv run python -m app.workers.run constituent_eod_price_ingestion --source instrument_price_fixture
uv run python -m app.workers.run fx_ingestion
uv run python -m app.workers.run exposure_recompute --workspace-id 1
# (change a price/holding, recompute again for a second snapshot)

curl -s "http://localhost:8080/api/v1/workspaces/1/exposure/top-holding-performance?limit=20"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure/top-holding-performance?sort=contribution&limit=10"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure/top-holding-performance?base_snapshot_id=1&comparison_snapshot_id=2"
curl -s "http://localhost:8080/api/v1/workspaces/1/dashboard"
```

### Instrument onboarding / data-readiness orchestration

The bridge that turns the individual ingestion/recompute workers into a single,
inspectable, idempotent flow that takes a workspace or fund from "not ready" to
**data-ready enough for charts / exposure / performance**. It is an
*orchestration* layer (`app/services/instrument_onboarding.py`, worker
`instrument_onboarding`), **not** a new analytics engine: it coordinates the
existing workers and never re-implements one.

**Why it exists.** Getting a fund chart/exposure ready means running, in order,
`issuer_holdings_ingestion` → `constituent_identity_resolution` →
`constituent_eod_price_ingestion` → `fx_ingestion` → `exposure_recompute` →
`alert_generation`. Onboarding makes that chain easy to *plan, run, inspect,
retry and explain* — a single run cascades through the stages (after identity
resolves, prices run in the same invocation), and re-running is a safe no-op once
data-ready.

**Stages** (dependency order): `holdings`, `constituent_identity`,
`constituent_prices`, `fx`, `exposure_recompute`, `alerts`. Each stage is
`ready` / `needed` / `skipped` / `blocked` / `complete`, driven by the
**market-data planner** + current DB state — never hardcoded. Each is
individually skippable when already fresh.

**Plan first (read-only, no writes, no network).** `build_onboarding_plan`
returns the stages, current readiness/coverage, blocking issues, estimated
requests by source, the jobs that would run, the safe/default source choices, and
the next recommended action.

**Source-mode policy (safe by default).**

- `fixture` (default) — every stage uses its offline fixture source. Fully
  offline; the safe default for tests / local demos / the seeded scheduler.
- `live` (explicit) — stages with a live-capable adapter use it (identity →
  OpenFIGI, constituent prices → Stooq), still budgeted/cached/logged via
  `guarded_fetch`. Stages with no live adapter (holdings, FX) fall back to the
  offline fixture and the plan emits a warning. **Live must be explicit.**

**Execution + job_run/stage reporting.** A run records a **parent**
`instrument_onboarding` `job_run`; each stage dispatches the existing worker(s)
as **child** `job_runs`. The parent persists **structured stage metadata** in
`job_runs.payload_json` (migration `0015`) — the typed, GUI-facing source of
truth — and keeps a human-readable `message` for logs. Failure policy: a hard
blocker (e.g. no holdings) stops dependent stages and records `partial_success`;
a non-critical failure (e.g. partial constituent prices, missing FX) continues
where safe and the coverage is reported as degraded.

**Run history / stage observability** (read model — migration `0015`). The GUI
asks "what did this onboarding run do?" without parsing the free-text `message`.
Each parent run stores, in `payload_json`, the scope, source mode, the skip
flags, the next recommended action, blocking issues, and a typed row **per
stage**: `status` (`success` / `partial_success` / `failed` / `skipped` /
`blocked`), a structured `reason` (`already_ready`, `skipped_by_flag`,
`blocked_by_missing_holdings`, `blocked_by_unresolved_identity`, `worker_failed`,
…), `source` / `expected_offline`, `started_at` / `finished_at` / `duration_ms`,
the **child `job_run` ids** the stage produced, and `records_inserted/updated/
failed`. `app/services/onboarding_runs.py` serves this as a **bounded** read
model (`job_type='instrument_onboarding'`, filtered by workspace/fund scope,
latest-first, `limit` default 50 / max 200, served by the `(job_type, id)`
index). Runs predating `0015` have no payload and are surfaced as
`legacy_metadata: true` with empty stages and the `message` preserved — never
crash, never back-parse free text. Scoping is enforced: a run is only visible via
its own workspace/fund (404 otherwise).

```text
GET /api/v1/workspaces/{workspace_id}/onboarding/runs?limit=50   # summaries, latest first
GET /api/v1/workspaces/{workspace_id}/onboarding/runs/{run_id}   # stages + child job runs
GET /api/v1/funds/{fund_id}/onboarding/runs?limit=50
GET /api/v1/funds/{fund_id}/onboarding/runs/{run_id}
```

**Readiness / coverage summary** (data-quality, *not* investment quality):
`holdings_ready` / `identity_ready` / `constituent_prices_ready` / `fx_ready` /
`exposure_ready` / `top_holding_performance_ready`, the weight-based coverage
fractions from the latest exposure snapshot, the exposure snapshot count, missing
top constituent prices, ambiguous constituents, and a 0..1 `score`.

**Scopes:** workspace and fund. The seeded scheduled job is **manual** (never
auto-runs); trigger explicitly via the CLI or `POST /jobs/{id}/run`.

```bash
# Plan-only (no writes, no network) then a safe offline run, then re-plan:
uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --plan-only
uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --source-mode fixture
uv run python -m app.workers.run instrument_onboarding --fund-id 1 --source-mode fixture
uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --plan-only
# Per-stage skips + a capped, explicit live run (small limit; never loops per holding):
uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --skip-exposure --skip-alerts
uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --source-mode live --limit 25
# No scope -> onboard every workspace under one umbrella job_run:
uv run python -m app.workers.run instrument_onboarding --source-mode fixture

curl -s  "http://localhost:8080/api/v1/workspaces/1/onboarding/plan"
curl -s  "http://localhost:8080/api/v1/workspaces/1/onboarding/status"
curl -X POST "http://localhost:8080/api/v1/workspaces/1/onboarding/run"
curl -X POST "http://localhost:8080/api/v1/workspaces/1/onboarding/run?source_mode=fixture&skip_alerts=true"
curl -s  "http://localhost:8080/api/v1/funds/1/onboarding/plan"
curl -X POST "http://localhost:8080/api/v1/funds/1/onboarding/run"
# Onboarding run history / stage observability (read model):
curl -s  "http://localhost:8080/api/v1/workspaces/1/onboarding/runs"
curl -s  "http://localhost:8080/api/v1/workspaces/1/onboarding/runs?limit=10"
curl -s  "http://localhost:8080/api/v1/workspaces/1/onboarding/runs/29"   # stages + child runs
curl -s  "http://localhost:8080/api/v1/funds/1/onboarding/runs"
curl -s  "http://localhost:8080/api/v1/funds/1/onboarding/runs/30"
curl -s  "http://localhost:8080/api/v1/workspaces/1/dashboard"      # .onboarding block (latest run)
curl -s  "http://localhost:8080/api/v1/workspaces/1/diagnostics"    # onboarding_* counts
```

**Compute boundary (see [`AGENTS.md`](AGENTS.md)).** Onboarding is bounded
orchestration/persistence: it reuses the planner + readiness counts and the
existing worker dispatch, with no per-instrument Python loops, no dataframe
analytics, and no live source calls of its own. Heavy/interactive valuation stays
in the Rust GUI / local pricer.

## Broker CSV import & transaction ledger

The bridge from the **market-data workstation** to the **user portfolio
workstation**: ingest a broker CSV export into a canonical, workspace-private
transaction ledger (`portfolio_transactions`) and a bounded position
reconciliation. Source adapter (pure parser) in
[`app/sources/broker_imports.py`](app/sources/broker_imports.py); ingestion
service in [`app/services/broker_imports.py`](app/services/broker_imports.py).

**`generic_csv_v1` format** — a forgiving generic CSV. Canonical columns (a few
common aliases accepted, e.g. `Trade Date→date`, `Transaction Type→type`,
`Ticker→symbol`, `Qty→quantity`, `Commission→fees`, `Total→net_amount`,
`CCY→currency`):

```text
date, settle_date, type, symbol, isin, figi, name, quantity, price,
gross_amount, fees, taxes, net_amount, currency, cash_currency, fx_rate,
broker_account, notes
```

`type` is normalised to `buy | sell | dividend | cash_deposit | cash_withdrawal |
fee | tax | fx | interest | unknown`. `date` and `type` columns are required; a
row needs a `currency`, a *trade* needs a `quantity`, a *cash movement* needs an
amount. A bad row (bad date / decimal / missing field) is **isolated and
flagged**, never crashing the import; an unmapped type is a `warning`, not an
error.

**Preview vs commit.**

* **preview** — parse + duplicate-file check + per-row instrument resolution.
  **Writes nothing.** Returns row statuses, counts, the file hash, a duplicate
  flag, and the unresolved count.
* **commit** — idempotently persist the `broker_import` (+ raw `broker_import_rows`
  for provenance) and canonical `portfolio_transactions`, then write an
  idempotent position-reconciliation snapshot.

**Idempotency / duplicate detection.** A `broker_import` is unique on
`(workspace_id, source_hash)`, so re-committing the **same file** is a duplicate
no-op (`duplicate=true`). A transaction is unique on `(workspace_id,
transaction_key, source)` (a content hash), so the **same transaction shared by
two files** is stored once. A position snapshot is unique on `(workspace_id,
as_of_date, input_hash)` like exposure snapshots — an unchanged ledger
re-reconciles to the same hash and writes nothing.

**Instrument resolution (offline, existing identity only).** Each row resolves
against existing identity in priority order — **ISIN → FIGI → a *unique*
ticker(+currency)** — using `funds`/`fund_listings`, `security_identifiers`, the
constituent `instruments`/`instrument_listings`/`instrument_identifiers`. There
are **no live resolver calls and no name-only guessing**: an ambiguous or
unmatched row is stored with its symbol/ISIN and `status=unresolved_instrument`
(never dropped, never a wrong link), and surfaced via diagnostics for later
resolution.

**Position reconciliation (NOT PnL).** `GET …/positions` is a derived, bounded
SQL aggregation over committed transactions: `quantity = buys − sells` per
instrument key, signed cash flow per currency, and fees/taxes totals. It does
**not** compute market value, realised/unrealised PnL, tax lots, IRR or total
return — those belong in the Rust GUI / local pricer.

```bash
# Preview (no writes) then commit, with an inline CSV:
curl -X POST "http://localhost:8080/api/v1/workspaces/1/broker-imports/preview" \
  -H "Content-Type: application/json" \
  -d '{"broker_name":"generic_csv_v1","source_filename":"sample.csv","csv_text":"date,type,symbol,isin,name,quantity,price,net_amount,currency\n2026-06-20,buy,VUSA,IE00B3XXRP09,Vanguard S&P 500 UCITS ETF,10,80.5,-805,GBP"}'

curl -X POST "http://localhost:8080/api/v1/workspaces/1/broker-imports/commit" \
  -H "Content-Type: application/json" \
  -d '{"broker_name":"generic_csv_v1","source_filename":"sample.csv","csv_text":"date,type,symbol,isin,name,quantity,price,net_amount,currency\n2026-06-20,buy,VUSA,IE00B3XXRP09,Vanguard S&P 500 UCITS ETF,10,80.5,-805,GBP"}'

# Import history + detail, the transaction ledger, and the reconciled positions:
curl -s "http://localhost:8080/api/v1/workspaces/1/broker-imports"
curl -s "http://localhost:8080/api/v1/workspaces/1/broker-imports/1"
curl -s "http://localhost:8080/api/v1/workspaces/1/transactions"
curl -s "http://localhost:8080/api/v1/workspaces/1/transactions?status=unresolved_instrument"
curl -s "http://localhost:8080/api/v1/workspaces/1/positions"
curl -s "http://localhost:8080/api/v1/workspaces/1/diagnostics"   # broker_imports / portfolio_transactions / unresolved_*

# Worker path (offline): commit the bundled generic_csv_v1 sample, or a local file.
uv run python -m app.workers.run broker_csv_import --workspace-id 1
uv run python -m app.workers.run broker_csv_import --workspace-id 1 --csv-path ./my_broker_export.csv
```

**Not implemented yet (deliberately):** PnL / realised gains, tax lots, total
return, corporate-action handling, and broker-specific adapters. Unresolved
imported symbols *are* now resolvable (next section), and unresolved / ambiguous /
mis-linked rows are correctable by hand — see **Manual transaction corrections**.

### Imported-instrument resolution

The **bridge** that turns directly-held imported instruments (TSLA, AAPL, …) from
`status=unresolved_instrument` ledger rows into the canonical
`instruments`/`instrument_listings` universe, so they can participate in instrument
prices, charts, the market-data planner, exposure/portfolio diagnostics and a
future GUI/local-pricer PnL. Service:
[`app/services/imported_instrument_resolution.py`](app/services/imported_instrument_resolution.py).
It is a *bridge*, not a second identity system — it reuses the constituent
resolvers and the shared instrument upsert.

**How a transaction is resolved (in order).**

1. **existing identity first** — re-check the row against `funds`/`fund_listings`,
   `security_identifiers` and the constituent `instruments`/`instrument_listings`
   (`broker_imports.build_resolution_index`). A symbol that became resolvable since
   import (a fund added, or a constituent resolved) links with **no resolver call**.
2. **safe resolver request** — for the rest, build *deduped* requests in priority
   **ISIN → FIGI → ticker(+currency)** and resolve through the same
   `constituent_identity_fixture` (offline default) / `openfigi` (live, opt-in,
   budget-guarded) resolvers. The canonical rows are upserted through the shared
   `constituent_identity.upsert_candidate_instrument` (deduped on the same identity
   keys — never a duplicate instrument).
3. **link + recompute** — a resolved transaction gets its `instrument_id` /
   `instrument_listing_id` (and `fund_id`/`fund_listing_id` where applicable) and
   `status=resolved`, the raw `symbol`/`isin`/`name` are preserved, and the bounded
   position snapshot is re-reconciled (now keyed on the resolved instrument).

**Safe-request rules (identical safety contract to constituent resolution).**

* **Never name-only.** A row with only a broker name (no ISIN/FIGI/ticker) is
  `skipped_unsafe` and left for manual handling — never auto-created.
* **OpenFIGI** only receives ISIN/FIGI (a bare imported ticker has no exchange, so
  it is fixture-only); it is never called for name-only or bare-ticker rows.
* **Ambiguous → not linked.** A materially-ambiguous result sets
  `status=ambiguous_instrument` (parked for manual disambiguation); `not_found` /
  `failed` stay `unresolved_instrument`. A reason is recorded in `raw_payload_json`.
* **Never clobbers** a manually-linked / already-resolved (`ready`) transaction.
* **Idempotent.** A rerun creates no duplicate instruments and relinks nothing.

**Dry-run vs commit.** `dry_run=true` builds requests + candidates and reports the
outcome counts but **writes nothing** (no upserts, no link changes, no snapshot) —
a safe preview. `dry_run=false` persists.

**Transaction statuses.** `committed` (resolved at import) · `unresolved_instrument`
(no safe identity) · `ambiguous_instrument` (needs a human) · `resolved` / `ready`
(now linked). All of these participate in the bounded position reconciliation; only
`unresolved_instrument` / `ambiguous_instrument` keep a position row flagged.

```bash
# Worker (offline fixture default; OpenFIGI only when explicitly asked):
uv run python -m app.workers.run imported_instrument_resolution --workspace-id 1 --source constituent_identity_fixture
uv run python -m app.workers.run imported_instrument_resolution --workspace-id 1 --source openfigi --limit 10
uv run python -m app.workers.run imported_instrument_resolution --broker-import-id 1
uv run python -m app.workers.run imported_instrument_resolution --transaction-id 123

# Unresolved/ambiguous ledger rows, a dry-run preview, then commit:
curl -s "http://localhost:8080/api/v1/workspaces/1/transactions/unresolved"

curl -X POST "http://localhost:8080/api/v1/workspaces/1/transactions/resolve" \
  -H "Content-Type: application/json" \
  -d '{"source":"constituent_identity_fixture","limit":100,"dry_run":true}'

curl -X POST "http://localhost:8080/api/v1/workspaces/1/transactions/resolve" \
  -H "Content-Type: application/json" \
  -d '{"source":"constituent_identity_fixture","limit":100,"dry_run":false}'

# Resolve a single import; one transaction's detail; ledger + positions show links:
curl -X POST "http://localhost:8080/api/v1/workspaces/1/broker-imports/1/resolve" \
  -H "Content-Type: application/json" -d '{"source":"constituent_identity_fixture"}'
curl -s "http://localhost:8080/api/v1/workspaces/1/transactions/123"
curl -s "http://localhost:8080/api/v1/workspaces/1/transactions?status=resolved"
curl -s "http://localhost:8080/api/v1/workspaces/1/positions"
curl -s "http://localhost:8080/api/v1/workspaces/1/market-data-plan?include_constituents=true"
```

The market-data planner now surfaces the imported backlog read-only:
`resolve_imported_instrument` (safe identifier), `ambiguous_imported_instrument` /
`manual_review_imported_instrument` (need a human), `fetch_imported_instrument_price`
and `fetch_imported_fx_rate` (resolved listings missing price / FX). Diagnostics add
`ambiguous_import_transactions`, `imported_instruments_ready_for_prices`,
`missing_imported_instrument_prices` and `imported_instrument_resolution_failures`.

### Manual transaction corrections

When automatic resolution cannot safely resolve a row — `unresolved_instrument`
(no safe identity), `ambiguous_instrument` (several candidates), or a wrong
automatic link — an operator cleans it up through the **manual correction**
endpoints. Service:
[`app/services/transaction_corrections.py`](app/services/transaction_corrections.py).
These are database-only edits over the existing ledger: they **never create an
instrument, never call a resolver / OpenFIGI / a live price/FX source, and never
name-only guess a link**.

| Endpoint | Effect |
| --- | --- |
| `GET …/transactions/manual-review` | The "needs a human" queue: `unresolved_instrument` + `ambiguous_instrument` + `manual_review` (optional `?status=`). |
| `GET …/transactions/{id}/correction-context` | Bounded candidate context to choose a link (see below). |
| `POST …/transactions/{id}/manual-link` | Link to **existing** `instrument` / `instrument_listing` / `fund` / `fund_listing` (status → `resolved`). |
| `POST …/transactions/{id}/clear-link` | Clear a mistaken link (status → `unresolved_instrument`, or `manual_review` via `reset_status`). |
| `POST …/transactions/{id}/ignore` | Drop the row from the portfolio (status → `ignored`; auditable). |
| `POST …/transactions/{id}/manual-review` | Park the row for later (status → `manual_review`; kept in the ledger, flagged). |

**Manual link rules.** At least one target id is required; targets must exist; a
supplied listing must belong to its supplied instrument/fund; instrument and fund
targets cannot be mixed (an orphan listing backfills its parent). A manual link
attaches *existing* canonical identity only — it does not create one.

**Correction context (bounded, read-only).** Echoes the raw imported
symbol/ISIN/FIGI/name/currency, the current link, the **safe auto-resolution**
the resolver bridge would pick (`suggested_link`, or `null`), **identifier
candidates** (ISIN/FIGI matches against funds/listings/instruments), **ticker
candidates** (exact-ticker listings, each with a `same_currency` hint), the most
recent stored resolver outcome, the last manual correction, and a
`recommended_action`. Name-only rows return **no** candidates and are never
auto-linked.

**Provenance.** Every correction appends a `manual_correction` block (latest) plus
a bounded `manual_correction_history` list into the transaction's
`raw_payload_json` — `action`, `reason`, `corrected_at`, `previous_status`,
`previous_links`, `new_links` — without clobbering the resolver's `resolution`
key. No migration: provenance lives in the existing JSON column.

**Recompute follow-through (automatic vs recommended).** A correction that changes
the ledger/links **automatically** re-reconciles the bounded position snapshot via
the existing idempotent reconciliation helper. It then **recommends, but never
runs** the follow-up: the response carries `position_snapshot_updated`,
`valuation_recompute_needed`, `market_data_plan_changed` and `recommended_actions`
(e.g. `recompute_portfolio_valuation`, `fetch_imported_instrument_price`). The
endpoint fetches nothing and runs no heavy recompute. Diagnostics add
`manual_review_transactions`, `ignored_import_transactions` and
`manual_linked_transactions`; an `ignored` row stops emitting urgent planner
items, while a `manual_review` row shows up as a non-urgent
`manual_review_imported_instrument`.

```bash
# The manual-review queue + bounded candidate context for one row:
curl -s "http://localhost:8080/api/v1/workspaces/1/transactions/manual-review"
curl -s "http://localhost:8080/api/v1/workspaces/1/transactions/123/correction-context"

# Manually link to an existing fund listing, then (if needed) clear it:
curl -X POST "http://localhost:8080/api/v1/workspaces/1/transactions/123/manual-link" \
  -H "Content-Type: application/json" \
  -d '{"fund_listing_id":1,"correction_reason":"it is VUSA"}'
curl -X POST "http://localhost:8080/api/v1/workspaces/1/transactions/123/clear-link" \
  -H "Content-Type: application/json" -d '{"correction_reason":"mislabelled"}'

# Ignore a non-portfolio row, or park one for manual review:
curl -X POST "http://localhost:8080/api/v1/workspaces/1/transactions/124/ignore" -d '{}'
curl -X POST "http://localhost:8080/api/v1/workspaces/1/transactions/125/manual-review" -d '{}'
```

**Still deferred:** PnL / tax lots / total return, corporate actions,
broker-specific adapters, and **name-only auto-linking (a deliberate non-goal —
capability `automatic_name_only_resolution=unsupported`).**

## Portfolio valuation / readiness snapshots

A **bounded, cacheable read model** one layer above the position reconciliation:
it joins the reconciled positions (net quantity per instrument) and cash (per
currency) to the **latest already-ingested** fund/instrument price + FX (at or
before `as_of_date`) and answers, for a workspace:

> What do I hold? Which positions have a latest price? Which have the required
> FX? What is the latest market-value *context* per position, in base currency?
> Which rows are unresolved / unpriced / missing FX — and why?

Service [`app/services/portfolio_valuation.py`](app/services/portfolio_valuation.py);
tables `portfolio_valuation_snapshots` / `portfolio_valuation_rows` (migration
`0019`). It is **NOT** PnL — there is no cost basis, realised/unrealised gain, tax
lots, total return or performance attribution (those live in the Rust GUI / local
pricer; see the compute boundary in `AGENTS.md`).

**What it computes (per row).** `position_type` (`fund_listing` /
`instrument_listing` / `instrument` / `fund` / `cash` / `unresolved` /
`ambiguous`), net `quantity`, `local_currency` / `base_currency`, the latest
`price` + its date/source/freshness, the `fx_rate_to_base` + its date/source/
freshness, `market_value_local` / `market_value_base`, a `valuation_status`
(`valued` · `missing_price` · `missing_fx` · `unresolved_instrument` ·
`ambiguous_instrument` · `cash_only` · `zero_quantity` · `stale_price` ·
`stale_fx`), a `readiness_status` (`ready` / `blocked` / `stale` / `cash`) and the
`blocking_reasons`. GBX (pence) listings are normalised to GBP.

**What it consumes.** `portfolio_transactions` (via the existing reconciliation),
fund-listing `prices`, `instrument_prices`, `fx_rates`. Nothing else.

**Safety / compute boundary (do not regress).** It calls **no** live price/FX
source and **no** identity resolver; a value that cannot be computed safely is
reported as a *blocker*, never invented (missing price → `fetch_*_price`; missing
FX → `fetch_fx_rate`; unresolved/ambiguous → the resolve backlog — all surfaced by
the market-data planner). Idempotent like exposure: `input_hash` keys on the
reconciled positions/cash plus every price/FX used, so an unchanged input set
re-values to the same hash and writes nothing; a new price/FX (or a (re)resolution)
yields a new snapshot. Bounded by `limit`.

```bash
# Worker (database-only; never fetches; --workspace-id, or every workspace):
uv run python -m app.workers.run portfolio_valuation_recompute --workspace-id 1
uv run python -m app.workers.run portfolio_valuation_recompute --workspace-id 1 --as-of-date 2026-06-25
uv run python -m app.workers.run portfolio_valuation_recompute --workspace-id 1 --base-currency GBP
uv run python -m app.workers.run portfolio_valuation_recompute --workspace-id 1 --broker-account-id 1

# Read model (GUI-friendly: summary counts + rows + provenance/freshness):
curl -s "http://localhost:8080/api/v1/workspaces/1/portfolio/valuation/latest"
curl -s "http://localhost:8080/api/v1/workspaces/1/portfolio/valuation/latest?valuation_status=missing_price"
curl -s "http://localhost:8080/api/v1/workspaces/1/portfolio/valuation/coverage"
curl -s "http://localhost:8080/api/v1/workspaces/1/portfolio/valuation"   # latest, or on-the-fly when none
curl -X POST "http://localhost:8080/api/v1/workspaces/1/portfolio/valuation/recompute"
```

The market-data planner emits a `recompute_portfolio_valuation` item (local
recompute, `estimated_requests=0`) when the ledger has no / a stale snapshot, and
diagnostics add `portfolio_positions` / `…_valued` / `…_missing_price` /
`…_missing_fx` / `…_unresolved` / `…_ambiguous`,
`portfolio_valuation_snapshot_stale`, `latest_portfolio_valuation_snapshot_at`,
`portfolio_valuation_failures`, `portfolio_valuation_history_points`,
`portfolio_valuation_latest_coverage_ratio` and `portfolio_valuation_readiness_status`.
Capabilities advertise `portfolio_valuation_recompute` **real** and
`portfolio_valuation` **partial** (coverage depends on how much price/FX/identity
has been ingested); `portfolio_pnl` / `tax_lots` / `total_return` /
`performance_attribution` stay **planned**.

### Valuation history / readiness series + dashboard block

A **bounded, snapshot-backed read model** over the snapshots above (no recompute on
the read path). It answers: *what is the latest market-value context, how has
coverage moved across recent snapshots, how many positions were valued / missing
price / missing FX / unresolved / ambiguous / stale over time, and which broker
account has the worst coverage?* It is **NOT** PnL/returns/performance — consecutive
points are never differenced into a return, and a value change is never labelled a
gain (those live in the Rust GUI / local pricer; see the compute boundary in
`AGENTS.md`).

```text
GET /api/v1/workspaces/{id}/portfolio/valuation/history   # oldest-first series (chart)
    ?broker_account_id=&start_date=&end_date=&base_currency=&limit=   # limit max 500
GET /api/v1/workspaces/{id}/portfolio/valuation/summary   # latest context + readiness rollup
    ?broker_account_id=&base_currency=
```

Each **history point** carries `snapshot_id`, `as_of_date`, `created_at`,
`base_currency`, `broker_account_id`, the per-status counts
(`positions_selected` / `positions_valued` / `missing_price_count` /
`missing_fx_count` / `unresolved_count` / `ambiguous_count` / `stale_price_count` /
`stale_fx_count` / `cash_row_count`), `total_market_value_base` (a coverage figure —
sum of the *valued* rows, **not** a return), a `valuation_coverage_ratio`
(`positions_valued / positions_selected`) and a snapshot-level `readiness_status`.

`readiness_status` rolls a snapshot's blocker counts up to one badge:
`ready` (all selected positions valued, no stale blockers) · `partial` (some valued
but missing-price/FX or unresolved/ambiguous remain) · `blocked` (nothing valued and
hard blockers dominate) · `stale` (valued, only stale price/FX) · `empty` (no
positions/cash). The **summary** adds `blocking_reasons`, a `history_points` count
and a `broker_accounts` breakdown (latest coverage per account — populated only when
account-scoped snapshots exist; workspace-level snapshots have a NULL account).

The workspace **dashboard** (`GET /workspaces/{id}/dashboard`) now carries a
`portfolio_valuation` block: the latest snapshot's market value, coverage ratio,
`readiness_status`, blocker counts, a `needs_recompute` flag (a ledger exists but no
snapshot, or the latest snapshot has aged past the freshness window) and a single
`recommended_action` (`run portfolio_valuation_recompute` · `resolve imported
instruments` · `fetch missing prices` · `fetch missing FX`). The dashboard **reads
the latest snapshot only — it never recomputes valuation**; `status=missing` when
none exists yet. Capabilities advertise `portfolio_valuation_history` and
`portfolio_valuation_dashboard` **real**.

## Job-run timeline & failure drilldown

A **generic, bounded observability read model over *all* `job_runs`** (every
worker, not just onboarding) for the GUI Data Operations page —
`app/services/job_timeline.py`. It answers, without each page querying many
endpoints or parsing free text: *what ran recently, which failed, which is
running, what did a run do, which source fetches happened near it, was a budget /
backoff involved, what to inspect next.* It is **observability only** — never a
workflow engine (no DAG runtime, no retries/branching, no new business logic).

```text
GET /api/v1/jobs/timeline?limit=100&job_type=&status=     # bounded, latest first
GET /api/v1/jobs/runs/{run_id}                            # full drilldown
GET /api/v1/jobs/failures?limit=50                        # failed/partial only
GET /api/v1/workspaces/{workspace_id}/jobs/timeline?limit=100
GET /api/v1/workspaces/{workspace_id}/jobs/failures?limit=50
GET /api/v1/workspaces/{workspace_id}/jobs/runs/{run_id}  # 404 if foreign
```

The simple `GET /api/v1/jobs/runs` list (envelope of raw run rows) is kept
unchanged for backward compatibility; the timeline is the richer, GUI-friendly
view. Limits are clamped (`limit` default 100 / max 500; the simple list keeps
its own bound).

**Timeline item** (summary): `run_id`, `job_type`, scope (`workspace_id` /
`fund_id` / `fund_listing_id` / `scheduled_job_id` + a `scope_label`), `status` +
derived `severity` (`error` / `warning` / `running` / `ok`), `started_at` /
`finished_at` / `duration_ms`, `records_inserted/updated/failed`, `source_name`,
the (masked) `message`, `is_orchestration` / `has_payload` / `has_children` /
`child_run_count`, a coarse `has_fetch_logs` hint, and the primary
`recommended_action` code.

**Run detail** adds: the masked structured `payload`; typed `stages` + `child_runs`
for orchestration runs (`instrument_onboarding`, expanded from the `0015`
`payload_json` — *never* parsed from `message`); `related_fetch_logs`;
`source_budget_context`; `related_entities`; and `recommended_actions`
(code + label).

**Source fetch-log correlation is approximate and labelled honestly.** There is
no exact run↔fetch FK yet, so a run is associated with `source_fetch_logs` by
**source name + time window** (`started_at` … `finished_at` + small buffer); the
response carries `fetch_log_correlation: time_window_source` (or `unavailable`
for DB-only producers / onboarding modes that have no real fetch source). Related
logs are **bounded** (default 25 / max 100, latest first). Capabilities advertise
`source_fetch_log_correlation: partial` to make the approximation explicit.

**Source budget context** (read-only): for a run whose source has a budget row,
the detail reports `enabled`, the current decision `status` (`ok` / `in_backoff` /
`min_delay` / `rate_limited_*` / …), `allowed`, `wait_seconds`, `backoff_until` /
`next_allowed_at`, and rolling-24h `recent_failures` / `cache_hits` /
`rate_limited_recently`.

**Recommended actions** are deterministic **codes + labels** for the GUI to
deep-link — *nothing is executed from these endpoints*. Examples:
`constituent_identity_resolution` failed → `check_source_budget` /
`open_fetch_logs` / `rerun_identity_resolution`; `constituent_eod_price_ingestion`
partial → `open_missing_prices` / `open_source_budget` / `rerun_price_ingestion`;
`instrument_onboarding` partial → `open_onboarding_run` / `open_diagnostics` /
`run_next_recommended_stage`; a source in backoff → `wait_for_backoff` /
`open_source_budget` / `open_fetch_logs`; `exposure_recompute` failed →
`open_diagnostics` / `check_missing_prices_fx` / `rerun_exposure`.

**Secret masking.** Anything echoed from stored data — `message`, `payload_json`,
fetch-log `request_key` / `endpoint_label` / `error_message` — is run through
`app/services/secret_masking.py` (recursive for JSON) before it leaves the API.
The fetch-log layer is already built secrets-free (request keys drop credential
params; only labels + hashes are stored); this is defence-in-depth so a leaked
`api_key=` / `token=` / `Bearer …` never reaches a client.

```bash
curl -s "http://localhost:8080/api/v1/jobs/timeline?limit=25"
curl -s "http://localhost:8080/api/v1/jobs/runs/42"
curl -s "http://localhost:8080/api/v1/jobs/failures?limit=25"
curl -s "http://localhost:8080/api/v1/workspaces/1/jobs/timeline?limit=50"
curl -s "http://localhost:8080/api/v1/workspaces/1/jobs/failures?limit=25"
curl -s "http://localhost:8080/api/v1/workspaces/1/diagnostics"  # recent_partial_job_runs, latest_failed_job_run_*
```

**Compute boundary (see [`AGENTS.md`](AGENTS.md)).** Bounded SQL-backed read
model only: latest-first, capped limits, indexed scope/`(job_type, id)` filters,
correlation windows bound by the run's own span. No per-instrument loops, no
analytics, no live calls.

### Running / leased job observability

The timeline above covers *completed* `job_runs`. Its **live counterpart**
(`app/services/job_leases.py`) answers what is happening *right now* on the
scheduler, for the same GUI Data Operations page:

```text
what is running right now
what is leased but possibly stuck
which lease expires soon, and which worker owns it
when the last heartbeat happened
which scheduled jobs are due but not yet claimed
which due jobs are blocked by an active lease
```

It is a **bounded, read-only read model** over the `scheduled_jobs` lease columns
the scheduler maintains (`locked_at` / `locked_by` / `lock_expires_at` /
`last_heartbeat_at` / `max_runtime_seconds` / `next_run_at` / `schedule_kind`) —
**not** a scheduler rewrite, and there is deliberately **no** unlock / kill /
force-release endpoint (see [`AGENTS.md`](AGENTS.md)).

```text
GET /api/v1/jobs/running?limit=100&include_due=true     # live rows + summary
GET /api/v1/jobs/leases?status=running|stuck|expired|due&limit=100
GET /api/v1/workspaces/{workspace_id}/jobs/running?limit=100
GET /api/v1/jobs/timeline?include_running=true&limit=100             # enriched
GET /api/v1/workspaces/{workspace_id}/jobs/timeline?include_running=true
```

**Lease states** (one mutually-exclusive classification per scheduled job, from
the single `classify_lease` helper):

| `lease_status` | meaning |
| --- | --- |
| `running` | leased, lease not expired, worker healthy |
| `stuck` | leased, **not** expired, but held past `max_runtime_seconds` or heartbeat gone stale (worker likely died) |
| `expired` | `lock_expires_at` has passed — reclaimable on the next scheduler pass |
| `due` | active, non-manual, `next_run_at <= now`, **not** leased — claimable now |
| `not_leased` | none of the above (manual / inactive / future) — not surfaced as a live row |

`blocked_by_lease` is a derived flag (not a state): a `running`/`stuck` job whose
`next_run_at` has already passed — the scheduler cannot claim it until the lease
clears (so it is **not** counted as `due`).

**Each live row** carries `kind` (`running_lease` / `stuck_lease` /
`expired_lease` / `due_scheduled_job`), `scheduled_job_id`, `name`, `job_type`,
`source_name`, `lease_status` + `severity`, the raw lease columns, plus derived
`started_at_for_timeline`, `age_seconds`, `seconds_until_expiry`, `is_expired` /
`is_stuck` / `is_blocked_by_lease`, and **recommended actions** (codes + labels
only — e.g. `wait_for_worker`, `check_worker_health`, `inspect_stuck_lease`,
`open_scheduler`, `rerun_when_unlocked`; never unlock/kill). Rows are ordered
**most urgent first** (expired → stuck → running → due, longest-held first).

`/jobs/running` (and `/jobs/leases`) returns a `summary` with `running_count` /
`expired_lease_count` / `stuck_lease_count` / `due_count` /
`blocked_by_lease_count`. With `include_running=true`, the timeline response is
enriched with `live_jobs` + `running_summary` while the completed `runs` list is
unchanged (existing clients are unaffected).

`scheduled_jobs` are **global shared infrastructure** (no `workspace_id`), so the
workspace-scoped running view returns the same global scheduler health (the
completed `runs` in a workspace timeline stay workspace-scoped). The same lease
classifier feeds `/scheduler/status` (`running_leases` / `stuck_leases` /
`expired_leases` / `blocked_by_lease` / `next_due_at`) and diagnostics
(`running_job_leases` / `stuck_job_leases` / `expired_job_leases` /
`blocked_scheduled_jobs_by_lease` / `due_scheduled_jobs`) so the numbers always
agree. Capabilities advertise `running_job_timeline` / `job_lease_observability` /
`stuck_lease_read_model` as `real`.

```bash
curl -s "http://localhost:8080/api/v1/jobs/running?limit=50"
curl -s "http://localhost:8080/api/v1/jobs/leases?status=stuck&limit=50"
curl -s "http://localhost:8080/api/v1/jobs/timeline?include_running=true&limit=50"
curl -s "http://localhost:8080/api/v1/workspaces/1/jobs/running?limit=50"
curl -s "http://localhost:8080/api/v1/workspaces/1/jobs/timeline?include_running=true&limit=50"
curl -s "http://localhost:8080/api/v1/scheduler/status"   # + running/stuck/expired_leases, next_due_at
```

## Scheduler & operational foundation

This is the operational layer that makes recurring jobs and external data
fetching **safe, observable, idempotent and rate-limited** — the prerequisite
before pulling identifiers/prices for the *hundreds* of constituents an ETF can
hold. The hard rule (see [`AGENTS.md`](AGENTS.md)): **never call OpenFIGI /
yfinance / Stooq / issuer sites once per holding in an uncontrolled loop.**

There is deliberately **no** Celery/RQ/Kafka, **no** OS cron, **no** `pg_cron`,
and **no** subprocess per job. The scheduler is a plain Python process that
*imports and calls* the same `app.workers.run.run_job` business logic as the CLI.

### Production data-source readiness

One in-code matrix (`app/sources/source_readiness.py`) answers the **operational** question
per source: *can it ingest real data on a VPS, and is it safe to schedule — or is it
fixture-only, blocked, or not implemented?* It is exposed read-only at
**`GET /api/v1/data-sources/readiness`**, summarised in `GET /api/v1/capabilities`
(`source_readiness`) and counted in diagnostics (`verified_live_sources` /
`candidate_live_sources` / `scheduler_safe_sources` / `scheduled_live_jobs` /
`fixture_scheduled_jobs` / `missing_required_live_sources`).

Status is conservative and honest: `fixture` (dev only) < `implemented_live` (works
explicitly, not yet recorded-verified) < `verified_live` (a clean live fetch+parse
succeeded) ; plus `candidate` (blocked — carries the exact blocker), `planned` (carries the
next action), `unsupported`. A source is `safe_for_scheduler` only when it is `verified_live`
or an explicitly-designed safe official source (budgeted/cached/logged, clean failures, no
secret, no brittle scraping) — and even then it stays **explicit-only** (the worker default
stays the offline fixture; scheduling means an explicit `scheduled_jobs` row naming
`--source`, never flipping the default). Today: `blackrock_ishares_holdings` (ISF) is
verified-live; `us_treasury_rates` / `ecb_rates` / `stooq` / `openfigi` are scheduler-safe
official/live; `jpmorgan_etf_holdings` + `vanguard_distributions` are blocked candidates;
`boe_rates`, live Vanguard/iShares, `stooq_market_series` and **IBKR Flex** (high-priority)
are planned. See `docs/data_sources.md` § *Production data-source readiness* for the full
matrix, verify commands, cascade chains, recommended cadences, the Stooq market-series
schema proposal, and the IBKR plan.

```bash
curl -s "http://localhost:8080/api/v1/data-sources/readiness"
curl -s "http://localhost:8080/api/v1/data-sources/readiness?scheduler_safe=true"
curl -s "http://localhost:8080/api/v1/data-sources/readiness?status=candidate"
```

### Target-fund coverage (VUSA / ISF / JEPG)

Where the readiness matrix is keyed by *source*, the **fund coverage matrix**
(`app/sources/fund_source_coverage.py`) is keyed by *(fund, data type)* — for the three
target funds and the six data types (facts / listing price / NAV / holdings / distributions /
documents): *can the backend fetch/parse/store it live, is it scheduler-safe, or what blocks
it?* A pure composition of the readiness + issuer-config registries (never drifts), exposed at
**`GET /api/v1/data-sources/fund-coverage`** (`?fund_symbol=VUSA`), summarised in
`GET /api/v1/capabilities` (`fund_coverage`) and counted in diagnostics
(`target_funds_with_live_price` / `…_holdings` / `…_distributions` / `…_facts` /
`…_documents` / `fund_sources_verified_live` / `fund_sources_fixture_only` /
`fund_source_blockers`). Honest state today (a 2026-06-27 bounded `verify_fund_sources` run):
**ISF holdings verified-live** (108 holdings); **JEPG holdings fetched & parsed live as OOXML
`.xlsx`** (247 holdings) but its 2026-06-25 fetch was a binary `.xls`, so the format is unstable
across runs and it stays a `candidate` (promotion waits for a stable re-verify); VUSA/JEPG
**distributions** are blocked candidates (TLS / no verified URL); **Stooq did not return a clean
EOD CSV** for the LSE ETF symbols (404 / HTML interstitial — confirm the symbol or use yfinance),
so listing prices stay `implemented_live` but unverified for these tickers; facts/documents are
fixture-fed (not live); **NAV is `planned`** and is never conflated with the listing close.

The `verify_fund_sources` worker runs **bounded, safe** live checks per fund (one guarded
Stooq fetch for the listing, one guarded issuer fetch+parse for holdings/distributions when a
usable config exists, honest `skipped_no_live_source` for facts/NAV/documents). It stores
nothing, promotes nothing, and a blocked provider never fails the run:

```bash
curl -s "http://localhost:8080/api/v1/data-sources/fund-coverage?fund_symbol=ISF"
uv run python -m app.workers.run verify_fund_sources --fund-symbol ISF --limit 10
uv run python -m app.workers.run verify_fund_sources --all-target-funds --limit 10
```

### Scheduler worker

```bash
uv run python -m app.workers.scheduler            # loop forever (polls)
uv run python -m app.workers.scheduler --once      # one pass over due jobs, exit
uv run python -m app.workers.scheduler --poll-seconds 30
```

A pass: initialise `next_run_at` for active non-`manual` jobs that lack one →
select **due** jobs (active, non-manual, `next_run_at <= now`, not currently
leased) → **claim** each atomically → run it via `run_job` → record a `job_run`,
advance `next_run_at`, release the lease. Failures are isolated per job
(`last_status=failed`) and never kill the loop.

### Job leasing (duplicate-run prevention)

A due job is claimed with a single **atomic conditional `UPDATE`** that stamps
`locked_by` / `locked_at` / `lock_expires_at` only if the row is unlocked or its
lease has expired. So even if several scheduler processes exist, exactly one runs
a given job; a crashed lease is **reclaimable** after `lock_expires_at`. This is
backend-agnostic (works on Postgres and the SQLite test DB) — no
`SELECT … FOR UPDATE` required. New lease/policy columns live on `scheduled_jobs`
(`locked_at`/`locked_by`/`lock_expires_at`/`last_heartbeat_at`/
`max_runtime_seconds`/`misfire_policy`/`retry_policy`).

### Schedule semantics

`schedule_kind ∈ manual | hourly | daily | weekly | interval`. `manual` never
runs automatically; the named kinds map to fixed intervals; `interval` uses
`interval_seconds`. `schedule_cron` is retained for display/forward-compat and is
**not** used to compute `next_run_at`. Internal scheduling is always UTC
(`timezone` is for display). Misfire policy is **run-once-then-schedule**: an
overdue job runs once and its next occurrence is computed from *now*, so it never
piles up. A failed run still computes the next occurrence.

### Source rate-budgets

`source_rate_limits` holds one budget per source answering: *may source X fetch
now? how long to wait? is it in backoff? what batch size?* Offline fixtures are
permissive; `openfigi`/`yfinance`/`stooq` are conservative (e.g. OpenFIGI 6/min,
`batch_size=10`, 300 ms min-delay). Window counts come from the fetch log, so the
budget and observability agree. `app/services/source_budget.py` exposes
`check_budget` (→ `SourceBudgetDecision`), `apply_backoff`, `note_request`, and a
`guarded_fetch(...)` wrapper that is the *one place* a live adapter should call
out: cache → budget → log → fetch.

### Fetch logs / request cache (no-spam design)

`source_fetch_logs` records every external fetch attempt with a **deterministic,
secrets-free request key** (`source + request_kind + normalised params`).
`should_skip_recent_success(...)` turns a recent identical success into a cache
hit (TTL `REQUEST_CACHE_TTL_SECONDS`, default 6h) so identical requests are not
repeated. **Security:** request keys drop credential-like params; logs store an
`endpoint_label` (host/path class) and hashes — never API keys, auth headers or
tokenised URLs. The **OpenFIGI resolver** is wired through `guarded_fetch` when a
DB session is available, and degrades gracefully (no candidates, no exception)
when budget-blocked.

### Market-data planner

`app/services/market_data_planner.py` produces a **read-only, deduped,
prioritised** plan of what to resolve/fetch for a workspace's held funds and
constituents — *without any network I/O*:

```
held funds → held listings w/ missing/stale prices  (fetch_listing_price, prio 1)
           → currencies with no FX path to base      (fetch_fx_rate, prio 1)
           → constituents not yet resolved           (resolve_constituent_identity)
           → funds w/ missing/stale holdings/facts    (refresh_holdings / refresh_fund_facts)
```

It **dedupes** (Apple held via VUSA *and* JEPG ⇒ one identity item), prioritises
(held positions and top-weight constituents first), and reports the **estimated
request cost per source** (`estimated_requests_by_source`). This is the gate
before pulling EOD prices for ETFs with hundreds of constituents.

```bash
GET /api/v1/scheduler/status
GET /api/v1/scheduler/due-jobs
POST /api/v1/scheduler/run-once
GET /api/v1/source-budgets
GET /api/v1/source-budgets/{source_name}
GET /api/v1/source-fetch-logs?source=&status=&request_kind=&limit=
GET /api/v1/workspaces/{id}/market-data-plan?include_constituents=true
```

Diagnostics gain operational counts (`due_scheduled_jobs`, `running_jobs`,
`stuck_jobs`, `expired_job_leases`, `recent_failed_fetches`,
`rate_limited_sources`, `sources_in_backoff`) plus live lease health from the
shared classifier (`running_job_leases`, `stuck_job_leases`,
`blocked_scheduled_jobs_by_lease`) and, per workspace,
`market_data_plan_items` / `unresolved_constituent_identities` /
`estimated_market_data_requests`. `/scheduler/status` exposes the same lease
breakdown (`running_leases` / `stuck_leases` / `expired_leases` /
`blocked_by_lease`) plus `next_due_at`. See **Running / leased job
observability** above for the `/jobs/running` live read model.

### Constituent identity resolution

`constituent_identity_resolution` (worker + `app/services/constituent_identity.py`)
turns the market-data planner's *unresolved constituent* items into a **canonical
instrument master** — the prerequisite for constituent EOD price ingestion. An ETF
holds hundreds of stocks; this resolves each holding once, into a shared identity
that look-through exposure and (later) per-stock prices attach to.

**Model** (migration `0012`): `instruments` (a real-world security, e.g. Apple Inc
— deduped on a deterministic `identity_key`: ISIN > share-class FIGI > composite
FIGI > FIGI > normalised name+country+currency), `instrument_listings` (tradable
listings — ticker/MIC/currency, so price ingestion knows what to fetch),
`instrument_identifiers` (ISIN/FIGI/CUSIP/SEDOL/… crosswalk + provenance).
`fund_holdings` gains `holding_instrument_id` plus `identity_status`
(`resolved` / `ambiguous` / `not_found` / `failed` / `manual`).

**Resolvers** (`app/sources/constituents.py`, two-layer split):

* `constituent_identity_fixture` (default) — offline, deterministic, keyed by ISIN
  + normalised name. Knows the seeded equities plus a few ambiguous / not-found /
  failing cases. Powers offline tests and local demos; no key, no network.
* `openfigi` — live OpenFIGI v3 mapping in **batches** (≤10 jobs/request), every
  call through `guarded_fetch` (cache → budget → fetch-log → fetch). Resolves by
  ISIN/FIGI/CUSIP/SEDOL (a bare ticker only when an exchange code *and* currency
  narrow it). An ISIN that maps to many venues of the *same* security (shared
  share-class FIGI) collapses to one instrument with listings; genuinely different
  securities are left **ambiguous**, never linked.

**Safety / idempotency:** requests are deduped (one Apple request even if held via
several funds); name-only is never sent to OpenFIGI; re-runs never duplicate
instruments/listings/identifiers (deterministic keys + unique constraints);
ambiguous/not-found results never link a holding to a guessed instrument; a
`manual` instrument or holding link is never clobbered. Defaults to the offline
fixture so the scheduler never makes a surprise live call.

```bash
# offline fixture (default) — safe, no key, no network
uv run python -m app.workers.run constituent_identity_resolution --source constituent_identity_fixture
uv run python -m app.workers.run constituent_identity_resolution --fund-id 1 --source constituent_identity_fixture
uv run python -m app.workers.run constituent_identity_resolution --workspace-id 1
# live OpenFIGI, budget-guarded + bounded
uv run python -m app.workers.run constituent_identity_resolution --source openfigi --limit 50
```

The job_run records `inserted=resolved`, `updated=ambiguous+not_found`,
`failed=failed`, with the full breakdown (incl. `skipped_budget` /
`skipped_cached` / `skipped_unsafe`) in its message. The planner then stops
emitting identity items for resolved holdings and begins emitting
`fetch_constituent_price` items (consumed by **constituent EOD price ingestion**,
below); its summary gains `resolved_constituents` / `ambiguous_constituents` /
`not_found_constituents` / `constituents_ready_for_eod_prices` /
`estimated_openfigi_requests` / `estimated_price_requests`. Diagnostics gain
`ambiguous_constituent_identities`, `constituent_identity_resolution_failures`,
`budget_blocked_constituent_resolution` and `constituents_ready_for_eod_prices`.

```bash
GET /api/v1/funds/{fund_id}/holdings?include_identity=true
GET /api/v1/funds/{fund_id}/constituents?status=unresolved   # resolved|ambiguous|not_found|failed
GET /api/v1/instruments/{instrument_id}
GET /api/v1/instruments/{instrument_id}/listings
```

### OpenFIGI API key

Set `OPENFIGI_API_KEY` in `.env` to raise OpenFIGI's rate limit. The key travels
**only** in the request header — never in a request key, fetch log, capabilities
payload, or any API response. `GET /api/v1/capabilities` exposes
`openfigi_api_key_configured: true/false` and nothing more. With no key the
resolver still works (just at the lower public rate limit). **Tests never require
the key** and run fully offline.

### Constituent EOD price ingestion

`constituent_eod_price_ingestion` (worker + `app/services/instrument_prices.py`)
consumes the planner's `fetch_constituent_price` backlog: it fetches end-of-day
prices for **resolved** constituent listings and stores them in
`instrument_prices`. This is what lets Mimer start to value ETF constituents and
later support true look-through valuation, exposure drift, top-holding
performance, constituent charts and stock detail pages.

**Model** (migration `0013`): `instrument_prices` — a generic EOD bar (OHLC +
`adjusted_close` + `volume` + `currency` + `source` + `status`) for an
`instrument_listing`, deduped on `(instrument_listing_id, price_date, source)` so
re-runs/backfills never duplicate a bar and distinct sources coexist. It is a
**complement** to the fund-listing `prices` table (which stays a single close per
fund listing), not a replacement — both belong to a *listing*, always record a
`source`, and now both drive time-series endpoints. `instrument_listings` gains
`last_price_at` (bumped on ingestion) so read-side freshness + the planner can
tell fresh / missing / stale apart.

**Sources** (`app/sources/instrument_prices.py`, two-layer split):

* `instrument_price_fixture` (default) — offline, deterministic OHLC history for
  the seeded constituents (the same universe the identity fixture resolves).
  Unknown listings return `no_data`, never an error. No key, no network.
* `stooq` / `yfinance` — live wrappers around the fund-price adapters, fetched
  **one symbol at a time** through `guarded_fetch` (cache → budget → fetch-log →
  fetch) with a politeness delay. Only with `--source stooq|yfinance`; never the
  default, never from the seeded scheduler job.

**Safety / idempotency:** only resolved constituents are priced (ambiguous /
not-found / unresolved holdings are skipped, never guessed); listings are deduped
across funds (Apple priced once even if held via VUSA *and* JPM) so a live
provider never loops per holding; `--limit` and the source budget's batch size
bound the work; a missing/failed/budget-blocked/cached listing is isolated and
counted, never failing the job; a rerun inserts nothing and updates nothing
(identical values), a changed OHLC/adjusted/volume/status updates only the
changed row, and a `manual` price is never clobbered.

```bash
# offline fixture (default) — safe, no key, no network
uv run python -m app.workers.run constituent_eod_price_ingestion --source instrument_price_fixture
uv run python -m app.workers.run constituent_eod_price_ingestion --fund-id 1
uv run python -m app.workers.run constituent_eod_price_ingestion --workspace-id 1
uv run python -m app.workers.run constituent_eod_price_ingestion --instrument-id 1
uv run python -m app.workers.run constituent_eod_price_ingestion --instrument-listing-id 1
# live, budget-guarded + bounded (use a small --limit; never spam)
uv run python -m app.workers.run constituent_eod_price_ingestion --source stooq --limit 20
```

The job_run records `inserted` / `updated` / `failed`, with the full breakdown
(`listings`, `no_data`, `skipped_budget`, `skipped_cached`) in its message. After
a run, fresh listings drop out of the planner's `fetch_constituent_price` items;
its summary gains `constituent_prices_fresh` / `constituent_prices_missing` /
`constituent_prices_stale`. Diagnostics gain `missing_constituent_prices`,
`stale_constituent_prices`, `constituent_price_ingestion_failures`,
`budget_blocked_constituent_price_fetches` and `constituent_price_coverage`
(fraction of resolved listings with a fresh price). Full true look-through
*valuation* is intentionally a later slice — this one only stores the prices.

### Unified instrument EOD price ingestion

`instrument_eod_price_ingestion` (worker + `ingest_instrument_eod_prices` in
`app/services/instrument_prices.py`) is the **single** price path for any
canonical `instrument_listing`, no matter where it came from:

* ETF/fund **constituents** (resolved by `constituent_identity_resolution`);
* directly-held **imported broker holdings** (resolved by
  `imported_instrument_resolution` — e.g. a TSLA/AAPL/MSFT buy in a broker CSV);
* future **manually-linked** instruments.

There is deliberately **no separate price table for imported holdings**: once a
broker transaction resolves to a canonical listing it is priced through the same
selector + idempotent upsert as a constituent and lands in `instrument_prices`,
so an imported TSLA becomes chartable (`/instrument-listings/{id}/prices` and
`…/time-series`) and visible in Data Operations exactly like any other listing.

**Selection** (`select_priceable_listings`) unions resolved constituents and
resolved imported direct holdings, deduped by listing (Apple priced once even if
held via an ETF *and* imported directly). It scopes by `--workspace-id` /
`--fund-id` / `--broker-import-id` / `--instrument-listing-id` /
`--instrument-id` / `--transaction-id`, skips **unpriceable** listings (no
ticker) and — for the bulk scopes — listings whose price is still **fresh**
(re-price them with `--force`). It never selects unresolved/ambiguous imported
transactions (they carry no listing).

**Sources / safety / idempotency** are identical to the constituent worker (same
fixture + `stooq`/`yfinance` adapters, same source budget / fetch-log / request
cache, same per-bar isolation, same `manual`-never-clobbered rule). The fixture
universe includes the imported direct holdings (TSLA, AAPL, MSFT, …, JEPG) so the
offline import → resolve → price flow lines up end to end. `constituent_eod_price_ingestion`
remains a constituent-only entry point and shares the same selector + upsert.

```bash
# offline fixture (default) — prices constituents + resolved imported holdings
uv run python -m app.workers.run instrument_eod_price_ingestion --source instrument_price_fixture
uv run python -m app.workers.run instrument_eod_price_ingestion --workspace-id 1
uv run python -m app.workers.run instrument_eod_price_ingestion --fund-id 1
uv run python -m app.workers.run instrument_eod_price_ingestion --broker-import-id 1
uv run python -m app.workers.run instrument_eod_price_ingestion --instrument-listing-id 25
uv run python -m app.workers.run instrument_eod_price_ingestion --workspace-id 1 --force
# live, budget-guarded + bounded (use a small --limit; never spam)
uv run python -m app.workers.run instrument_eod_price_ingestion --source stooq --limit 25
```

The job_run records `inserted` / `updated` / `failed`; its message carries the
full breakdown (`selected`, `skipped_fresh`, `skipped_unpriceable`, `no_data`,
`rate_limited`, `cached`). Diagnostics gain `instrument_price_ingestion_failures`;
`missing_imported_instrument_prices` (from the plan) drops to 0 once the resolved
imported listings are priced. **Deferred** (not in this slice, not in the price
worker): PnL, tax lots, total return, corporate actions, and exact broker cash
balances — those belong in the Rust GUI / local pricer.

```bash
GET /api/v1/funds/{fund_id}/constituents?include_prices=true   # latest EOD close per resolved constituent
GET /api/v1/instrument-listings/{id}/prices
GET /api/v1/instrument-listings/{id}/time-series?kind=price&range=1y
GET /api/v1/instruments/{instrument_id}/prices                 # primary listing
GET /api/v1/instruments/{instrument_id}/time-series?kind=price
```

### Official / reference-rate ingestion

`rates_ingestion` (worker + `app/services/rates_ingestion.py` +
`app/sources/rates.py`, read shaping in `app/services/rates.py`) **collects and
persists official/reference rate observations** into `reference_rates` so the
GUI / local pricer can later consume them. Stored rates:

* **EUR** — ECB main refinancing / deposit facility / marginal lending rates, €STR;
* **GBP** — BoE Bank Rate, SONIA;
* **USD** — US Treasury par yields (1M, 3M, 6M, 1Y, 2Y, 5Y, 10Y, 30Y), SOFR, Fed Funds effective.

Each row is one observation keyed on
`(rate_date, currency, country_or_region, rate_family, rate_name, tenor, source)`
(idempotent upsert; NULL `tenor` for policy/overnight rates), carrying
`rate_value` (Decimal), `unit` (`percent`), `tenor`/`tenor_months`, `source`,
`status` (provider provenance) and `source_url`.

**Sources.** The default is an **offline fixture** (`rates_fixture`). Two **live**
**explicit-only** adapters fetch official machine-readable feeds through
`guarded_fetch` (recent-success cache → source budget → fetch log → fetch; 20s
timeout; conservative budget):

* **`us_treasury_rates`** — USD `treasury_par_yield` (1M…30Y) from the Treasury
  *Daily Treasury Par Yield Curve Rates* **XML feed** (`home.treasury.gov`), one
  calendar year per request.
* **`ecb_rates`** — EUR ECB key interest rates (main refinancing / deposit facility
  / marginal lending) + €STR from the official **ECB Data Portal SDMX API**
  (`data-api.ecb.europa.eu/service/data/<flow>/<key>?format=csvdata`), one combined
  request per dataflow (`FM` key rates + `EST` €STR). The parser reads the SDMX CSV
  **by column name** (`KEY`/`TIME_PERIOD`/`OBS_VALUE`), parses `Decimal` values,
  maps each series to `rate_name`/`rate_family`, and stores observations **as
  supplied** — ECB key rates are a *change-date* series (not forward-filled into a
  daily series); €STR is daily.

Both store `status=official` and parse `Decimal` (skipping missing cells, isolating
bad ones) — **never** a curve. `boe_rates` (BoE IADB, series `IUDBEDR`/`IUDSOIA`)
stays **planned**: the codes are known but the IADB CSV export returns HTTP 403 to a
plain client, so a clean non-brittle access path must be verified first (no guessing,
no scraping, no third-party/FRED feeds — see `docs/data_sources.md`). Live sources
are explicit-only, so the worker/scheduler never makes a surprise live call.

This slice is **collection + persistence + monitoring only**. The backend
**never** builds yield curves, fits or bootstraps curves, interpolates,
constructs discount factors, computes forward rates or prices bonds — those live
in the Rust GUI / local pricer. `yield_curves` stays a **planned** data type
(distinct from the real `reference_rates`); there is no curve / discount-factor /
pricing table.

```bash
uv run python -m app.workers.run rates_ingestion                       # offline fixture (all series)
uv run python -m app.workers.run rates_ingestion --source rates_fixture
uv run python -m app.workers.run rates_ingestion --currency EUR
uv run python -m app.workers.run rates_ingestion --rate-family treasury_par_yield
uv run python -m app.workers.run rates_ingestion --start-date 2026-01-01 --end-date 2026-06-24
uv run python -m app.workers.run rates_ingestion --source us_treasury_rates --start-date 2026-01-01 --limit 100  # live USD par yields
uv run python -m app.workers.run rates_ingestion --source ecb_rates --start-date 2026-01-01 --limit 100          # live EUR ECB rates + €STR
uv run python -m app.workers.run rates_ingestion --source ecb_rates --rate-family overnight_rate                 # €STR only
uv run python -m app.workers.run rates_ingestion --source boe_rates    # planned -> clean failed run
```

The job_run records `inserted` / `updated` / `failed`; its message carries the
breakdown (`selected`, `skipped`, date range). The market-data planner emits
`fetch_reference_rates` items for supported currencies (EUR/GBP/USD) with
missing/stale official rates (EUR's `source_candidates` include `ecb_rates`, USD's
`us_treasury_rates`), and diagnostics gain `reference_rates`,
`missing_reference_rates`, `stale_reference_rates`, `latest_reference_rate_date`
and `rates_ingestion_failures` — none of which build or evaluate a curve.

```bash
GET /api/v1/rates                 ?currency=&country_or_region=&rate_family=&rate_name=&tenor=&source=&date=&start_date=&end_date=&limit=
GET /api/v1/rates/latest          ?currency=&country_or_region=&rate_family=&rate_name=&source=&limit=   # newest per series
GET /api/v1/rates/sources         # rates-source catalogue: adapter_status, is_fixture, requires_live_fetch, is_default
GET /api/v1/rates/time-series     ?rate_name=&currency=&country_or_region=&tenor=&source=&start_date=&end_date=&limit=
```

## API endpoints

Response style: **list** endpoints return `{"data": [...], "meta": {"count": N}}`;
**single-resource** and **summary** endpoints return the object directly. All
decimal values are JSON strings.

```
GET    /health

# Workspace / user (see "Multi-tenant" below)
GET    /api/v1/me
GET    /api/v1/workspaces
GET    /api/v1/workspaces/{workspace_id}
GET    /api/v1/workspaces/{workspace_id}/settings
PUT    /api/v1/workspaces/{workspace_id}/settings

# Workspace-scoped private data (canonical)
GET    /api/v1/workspaces/{workspace_id}/portfolio/positions
POST   /api/v1/workspaces/{workspace_id}/portfolio/positions
PUT    /api/v1/workspaces/{workspace_id}/portfolio/positions/{position_id}
DELETE /api/v1/workspaces/{workspace_id}/portfolio/positions/{position_id}
GET    /api/v1/workspaces/{workspace_id}/portfolio/summary
GET    /api/v1/workspaces/{workspace_id}/exposure            ?dimension=&snapshot_id=&limit=
GET    /api/v1/workspaces/{workspace_id}/exposure/snapshots  ?limit=

# Broker CSV import -> canonical transaction ledger -> bounded position reconciliation
POST   /api/v1/workspaces/{workspace_id}/broker-imports/preview   # read-only (no writes)
POST   /api/v1/workspaces/{workspace_id}/broker-imports/commit    # idempotent
GET    /api/v1/workspaces/{workspace_id}/broker-imports          ?limit=
GET    /api/v1/workspaces/{workspace_id}/broker-imports/{import_id}
POST   /api/v1/workspaces/{workspace_id}/broker-imports/{import_id}/resolve  # resolve one import's unresolved txns
GET    /api/v1/workspaces/{workspace_id}/transactions    ?limit=&transaction_type=&status=&broker_import_id=
GET    /api/v1/workspaces/{workspace_id}/transactions/unresolved  ?limit=    # awaiting/needing manual resolution
GET    /api/v1/workspaces/{workspace_id}/transactions/{transaction_id}
POST   /api/v1/workspaces/{workspace_id}/transactions/resolve     # imported-instrument resolution (dry_run supported)
GET    /api/v1/workspaces/{workspace_id}/positions               # derived (buys−sells + cash); NOT PnL

# Workspace aggregate / GUI-hydration endpoints (bounded snapshots)
GET    /api/v1/workspaces/{workspace_id}/dashboard
GET    /api/v1/workspaces/{workspace_id}/diagnostics
GET    /api/v1/workspaces/{workspace_id}/hierarchy
GET    /api/v1/workspaces/{workspace_id}/portfolio/time-series ?kind=&range=&source=

# Workspace alerts (derived by the alert_generation worker)
GET    /api/v1/workspaces/{workspace_id}/alerts          ?status=&category=&severity=&limit=
POST   /api/v1/workspaces/{workspace_id}/alerts/mark-all-read
POST   /api/v1/workspaces/{workspace_id}/alerts/{alert_id}/read
POST   /api/v1/workspaces/{workspace_id}/alerts/{alert_id}/dismiss
POST   /api/v1/workspaces/{workspace_id}/alerts/{alert_id}/resolve

# Shared / reference data (not workspace-scoped)
GET    /api/v1/funds
GET    /api/v1/funds/{fund_id}
GET    /api/v1/funds/{fund_id}/detail ?include_prices=&include_holdings=&history_days=
GET    /api/v1/funds/{fund_id}/listings
GET    /api/v1/funds/{fund_id}/distributions
GET    /api/v1/funds/{fund_id}/holdings   ?as_of_date=&source=&limit=&include_identity=   # one snapshot + provenance
GET    /api/v1/funds/{fund_id}/constituents ?status=&include_prices=   # identity state + rollup (+ latest EOD price)
GET    /api/v1/funds/{fund_id}/documents   ?document_type=&latest_only=&limit=   # snapshots + change status
GET    /api/v1/funds/{fund_id}/time-series          ?kind=&range=&source=
GET    /api/v1/fund-listings/{fund_listing_id}/time-series ?kind=&range=&source=
GET    /api/v1/distributions          ?fund_id=&limit=
GET    /api/v1/holdings               ?fund_id=&limit=
GET    /api/v1/documents              ?fund_id=&document_type=&limit=
GET    /api/v1/documents/{document_snapshot_id}   # one snapshot + change provenance
GET    /api/v1/fx-rates               ?base_currency=&quote_currency=&limit=
GET    /api/v1/fx/rates               ?base=&quote=&source=&limit=
GET    /api/v1/fx/time-series         ?base=&quote=&range=&source=   # subject.type="fx_pair"
GET    /api/v1/fx/convert             ?from=&to=&amount=&as_of=&source=   # rate + provenance
GET    /api/v1/rates                  ?currency=&country_or_region=&rate_family=&rate_name=&tenor=&source=&date=&start_date=&end_date=&limit=
GET    /api/v1/rates/latest           ?currency=&country_or_region=&rate_family=&rate_name=&source=&limit=
GET    /api/v1/rates/sources          # rates-source catalogue (implemented/planned)
GET    /api/v1/rates/time-series      ?rate_name=&currency=&country_or_region=&tenor=&source=&start_date=&end_date=&limit=
GET    /api/v1/diagnostics            # global data-quality counts

# Ingestion entrypoint
POST   /api/v1/instruments            # resolve a symbol -> create/reuse + queue backfill
GET    /api/v1/instruments/{instrument_id}            # canonical constituent + listings + identifiers
GET    /api/v1/instruments/{instrument_id}/listings   # tradable listings (EOD price targets)
GET    /api/v1/instruments/{instrument_id}/prices            ?source=&limit=   # primary listing EOD bars
GET    /api/v1/instruments/{instrument_id}/time-series       ?kind=price&range=&source=
GET    /api/v1/instrument-listings/{id}/prices              ?source=&limit=   # constituent EOD bars
GET    /api/v1/instrument-listings/{id}/time-series         ?kind=price&range=&source=

# Jobs / automation
GET    /api/v1/jobs                   # each carries schedule_kind, lease state, implementation_status
GET    /api/v1/jobs/runs              ?job_type=&fund_id=&fund_listing_id=&status=&limit=   # simple list (compat)
GET    /api/v1/jobs/timeline          ?job_type=&status=&limit=&include_running=   # bounded run timeline (read model)
GET    /api/v1/jobs/runs/{run_id}     # full drilldown: scope/payload/stages/children/fetch logs/budget/actions
GET    /api/v1/jobs/failures          ?limit=   # recent failed/partial runs + recommended actions
GET    /api/v1/jobs/running           ?limit=&include_due=   # live running/leased/stuck/expired/due + summary (read-only)
GET    /api/v1/jobs/leases            ?status=running|stuck|expired|due&limit=   # lease rows, status-filtered
GET    /api/v1/jobs/{job_id}
POST   /api/v1/jobs/{job_id}/run      # ingestion + alert_generation real; others stub
GET    /api/v1/workspaces/{workspace_id}/jobs/timeline    ?limit=&include_running=
GET    /api/v1/workspaces/{workspace_id}/jobs/failures    ?limit=
GET    /api/v1/workspaces/{workspace_id}/jobs/running     ?limit=&include_due=   # global scheduler health (shared infra)
GET    /api/v1/workspaces/{workspace_id}/jobs/runs/{run_id}   # 404 if foreign

# Scheduler / operational platform (job leasing, source budgets, fetch logs)
GET    /api/v1/scheduler/status                 # active/due/leased + running/stuck/expired_leases, next_due_at
GET    /api/v1/scheduler/due-jobs
POST   /api/v1/scheduler/run-once               # run one pass now (claim + run due jobs)
GET    /api/v1/source-budgets                   # per-source budget + current decision
GET    /api/v1/source-budgets/{source_name}
GET    /api/v1/source-fetch-logs                ?source=&status=&request_kind=&limit=

# Market-data planning (workspace-scoped, read-only, no network I/O)
GET    /api/v1/workspaces/{workspace_id}/market-data-plan   ?include_constituents=true

# Data sources / service capability discovery
GET    /api/v1/data-sources                    ?source_type=&is_active=   # priority registry (DB)
GET    /api/v1/data-sources/capabilities       ?source_type=&data_type=&adapter_status=
GET    /api/v1/capabilities                    # what's real/fixture/stub/planned (no secrets)

# Legacy aliases (deprecated) — resolve workspace via X-Workspace-ID / default
GET    /api/v1/portfolio/positions
POST   /api/v1/portfolio/positions
PUT    /api/v1/portfolio/positions/{position_id}
DELETE /api/v1/portfolio/positions/{position_id}
GET    /api/v1/portfolio/summary
GET    /api/v1/exposure
```

### Error shape

```json
{ "error": { "code": "fund_not_found", "message": "Fund not found" } }
```

## Example curl commands

```bash
# Health (liveness) + DB readiness
curl -s http://localhost:8080/health
# {"status":"ok"}
curl -s http://localhost:8080/health/db
# {"status":"ok","database":"connected"}   (503 if Postgres is unreachable)

# List funds
curl -s http://localhost:8080/api/v1/funds
# {"data":[{"id":1,"isin":"IE00B3XXRP09","name":"Vanguard S&P 500 UCITS ETF", ...}], "meta":{"count":3}}

# Who am I + my workspaces (dev: default user)
curl -s http://localhost:8080/api/v1/me

# Portfolio summary (GUI-friendly), workspace-scoped
curl -s http://localhost:8080/api/v1/workspaces/1/portfolio/summary
# {
#   "base_currency": "GBP",
#   "total_market_value": "12381.10",
#   "daily_change": null,
#   "unrealised_gain_loss": "847.24",
#   "trailing_12m_income": "246.68",
#   "projected_annual_income": "246.68",
#   "positions": [ { "ticker": "VUSA", "market_value": "7500.00", ... } ]
# }

# Legacy alias (workspace via header, or default workspace)
curl -s http://localhost:8080/api/v1/portfolio/summary -H 'X-Workspace-ID: 1'

# Create a position (workspace-scoped)
curl -s -X POST http://localhost:8080/api/v1/workspaces/1/portfolio/positions \
  -H 'content-type: application/json' \
  -d '{"fund_listing_id":1,"units":"100","average_cost":"70.00","cost_currency":"GBP","account_name":"ISA"}'

# Trigger a scheduled job (ingestion + alert_generation run real; others stub)
curl -s -X POST http://localhost:8080/api/v1/jobs/1/run

# --- GUI hydration / aggregate endpoints ---

# Workspace dashboard (one bounded snapshot for the main workstation view)
curl -s http://localhost:8080/api/v1/workspaces/1/dashboard

# Workspace + global data-quality diagnostics
curl -s http://localhost:8080/api/v1/workspaces/1/diagnostics
curl -s http://localhost:8080/api/v1/diagnostics

# Investable hierarchy (Portfolio -> positions -> top holdings)
curl -s http://localhost:8080/api/v1/workspaces/1/hierarchy

# Fund detail page (facts, listings+latest price, distributions, holdings, docs, jobs, ids)
curl -s "http://localhost:8080/api/v1/funds/1/detail?include_prices=true&history_days=365"

# Charts: listing price series, fund distribution series, derived portfolio value
curl -s "http://localhost:8080/api/v1/fund-listings/1/time-series?kind=price&range=1y"
curl -s "http://localhost:8080/api/v1/funds/1/time-series?kind=distribution&range=all"
curl -s "http://localhost:8080/api/v1/workspaces/1/portfolio/time-series?kind=portfolio_value&range=1y"

# Job runs, filtered
curl -s "http://localhost:8080/api/v1/jobs/runs?job_type=price_ingestion&status=queued"

# Job-run timeline / failure drilldown (Data Operations page)
curl -s "http://localhost:8080/api/v1/jobs/timeline?limit=25"
curl -s "http://localhost:8080/api/v1/jobs/runs/42"
curl -s "http://localhost:8080/api/v1/jobs/failures?limit=25"
curl -s "http://localhost:8080/api/v1/workspaces/1/jobs/timeline?limit=50"
curl -s "http://localhost:8080/api/v1/workspaces/1/jobs/failures?limit=25"

# Running / leased / stuck / due scheduled jobs (live; read-only — no unlock/kill)
curl -s "http://localhost:8080/api/v1/jobs/running?limit=50"
curl -s "http://localhost:8080/api/v1/jobs/leases?status=stuck&limit=50"
curl -s "http://localhost:8080/api/v1/jobs/timeline?include_running=true&limit=50"
curl -s "http://localhost:8080/api/v1/workspaces/1/jobs/running?limit=50"
curl -s "http://localhost:8080/api/v1/workspaces/1/jobs/timeline?include_running=true&limit=50"

# --- Data sources / capability discovery ---

# What the service implements (real/fixture/stub/planned), configured sources, no secrets
curl -s http://localhost:8080/api/v1/capabilities

# Source priority registry (DB) and the capability catalogue (code)
curl -s http://localhost:8080/api/v1/data-sources
curl -s "http://localhost:8080/api/v1/data-sources/capabilities?adapter_status=implemented"

# --- Holdings ingestion + reads (offline fixture) ---

# Ingest look-through holdings (one fund, then all eligible funds)
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 1
uv run python -m app.workers.run issuer_holdings_ingestion

# Latest holdings snapshot for a fund (+ provenance: source/status/as_of_date)
curl -s http://localhost:8080/api/v1/funds/1/holdings
# Pin a specific snapshot, or bound it:
curl -s "http://localhost:8080/api/v1/funds/1/holdings?source=seed"
curl -s "http://localhost:8080/api/v1/funds/1/holdings?limit=20"

# Holdings flow through the aggregate read endpoints
curl -s "http://localhost:8080/api/v1/funds/1/detail?include_holdings=true"
curl -s http://localhost:8080/api/v1/workspaces/1/hierarchy
curl -s http://localhost:8080/api/v1/workspaces/1/dashboard
curl -s http://localhost:8080/api/v1/workspaces/1/exposure

# Which sources can provide holdings (holdings_fixture is implemented)
curl -s "http://localhost:8080/api/v1/data-sources/capabilities?data_type=holdings"

# --- Constituent identity resolution (offline fixture + live OpenFIGI) ---

# Resolve constituents to canonical instruments (offline fixture: no key/network)
uv run python -m app.workers.run constituent_identity_resolution --source constituent_identity_fixture
uv run python -m app.workers.run constituent_identity_resolution --fund-id 1 --source constituent_identity_fixture
# Live OpenFIGI, budget-guarded + bounded (only with a key; never printed)
uv run python -m app.workers.run constituent_identity_resolution --source openfigi --limit 50

# Holdings with resolved identity, the constituents view, and the instrument master
curl -s "http://localhost:8080/api/v1/funds/1/holdings?include_identity=true"
curl -s "http://localhost:8080/api/v1/funds/1/constituents"
curl -s "http://localhost:8080/api/v1/funds/1/constituents?status=unresolved"
curl -s "http://localhost:8080/api/v1/instruments/1"
curl -s "http://localhost:8080/api/v1/instruments/1/listings"

# The plan reflects resolution; fetch logs stay secrets-free
curl -s "http://localhost:8080/api/v1/workspaces/1/market-data-plan?include_constituents=true"
curl -s "http://localhost:8080/api/v1/source-fetch-logs?source=openfigi"
curl -s "http://localhost:8080/api/v1/source-budgets/openfigi"

# --- Constituent EOD price ingestion (offline fixture + live Stooq/yfinance) ---

# Resolve identities first, then fetch EOD prices for resolved constituents
uv run python -m app.workers.run issuer_holdings_ingestion
uv run python -m app.workers.run constituent_identity_resolution --source constituent_identity_fixture
uv run python -m app.workers.run constituent_eod_price_ingestion --source instrument_price_fixture
uv run python -m app.workers.run constituent_eod_price_ingestion --source instrument_price_fixture --fund-id 1
# Live, budget-guarded + bounded (use a small --limit; never spam)
uv run python -m app.workers.run constituent_eod_price_ingestion --source stooq --limit 20

# --- Unified instrument EOD prices: imported direct holdings become chartable ---

# Import a broker CSV, resolve its directly-held instruments, then price them
uv run python -m app.workers.run broker_csv_import --workspace-id 1
uv run python -m app.workers.run imported_instrument_resolution --workspace-id 1 --source constituent_identity_fixture
uv run python -m app.workers.run instrument_eod_price_ingestion --workspace-id 1 --source instrument_price_fixture
uv run python -m app.workers.run instrument_eod_price_ingestion --instrument-listing-id 25 --source instrument_price_fixture

# The imported holding now has a price series + chart, like any other listing
curl -s "http://localhost:8080/api/v1/workspaces/1/market-data-plan?include_constituents=true"
curl -s "http://localhost:8080/api/v1/workspaces/1/transactions?status=resolved"
curl -s "http://localhost:8080/api/v1/workspaces/1/positions"
curl -s "http://localhost:8080/api/v1/instrument-listings/25/prices"
curl -s "http://localhost:8080/api/v1/instrument-listings/25/time-series?kind=price"

# Latest EOD close per resolved constituent, listing bars, and the price chart
curl -s "http://localhost:8080/api/v1/funds/1/constituents?include_prices=true"
curl -s "http://localhost:8080/api/v1/instrument-listings/1/prices"
curl -s "http://localhost:8080/api/v1/instrument-listings/1/time-series?kind=price&range=1y"
curl -s "http://localhost:8080/api/v1/instruments/1/prices"
# The plan shows fresh listings dropping out; fetch logs/budgets stay visible
curl -s "http://localhost:8080/api/v1/workspaces/1/market-data-plan?include_constituents=true"
curl -s "http://localhost:8080/api/v1/source-fetch-logs?source=instrument_price_fixture"
curl -s "http://localhost:8080/api/v1/source-budgets/stooq"

# --- FX ingestion + currency-aware valuation (offline fixture) ---

# Ingest FX rates (infer currencies, or pin base/quote)
uv run python -m app.workers.run fx_ingestion
uv run python -m app.workers.run fx_ingestion --source fx_fixture
uv run python -m app.workers.run fx_ingestion --base GBP --quote USD

# FX rates + pair time-series + a conversion with full provenance
curl -s "http://localhost:8080/api/v1/fx/rates?base=GBP&quote=USD"
curl -s "http://localhost:8080/api/v1/fx/time-series?base=GBP&quote=USD&range=1y"
curl -s "http://localhost:8080/api/v1/fx/convert?from=USD&to=GBP&amount=1500"

# Dashboard positions now carry market_value_local/base + fx_rate/source/status;
# diagnostics count missing/stale FX and unconverted positions.
curl -s http://localhost:8080/api/v1/workspaces/1/dashboard
curl -s http://localhost:8080/api/v1/workspaces/1/diagnostics

# Which sources can provide FX (fx_fixture is implemented; ecb/boe planned)
curl -s "http://localhost:8080/api/v1/data-sources/capabilities?data_type=fx_rates"

# --- Official / reference-rate ingestion (fixture default + live US Treasury & ECB) ---

# Collect official/reference rate observations (all series, or filtered)
uv run python -m app.workers.run rates_ingestion --source rates_fixture
uv run python -m app.workers.run rates_ingestion --currency EUR

# Live US Treasury par yields (explicit-only; official XML feed via guarded_fetch)
uv run python -m app.workers.run rates_ingestion --source us_treasury_rates --start-date 2026-01-01 --limit 100

# Live ECB key interest rates + €STR (explicit-only; official Data Portal SDMX via guarded_fetch)
uv run python -m app.workers.run rates_ingestion --source ecb_rates --start-date 2026-01-01 --limit 100
uv run python -m app.workers.run rates_ingestion --source ecb_rates --rate-family overnight_rate   # €STR only

# Latest official rates per currency, a single series' history, and the catalogue
curl -s "http://localhost:8080/api/v1/rates/latest?currency=EUR"
curl -s "http://localhost:8080/api/v1/rates/latest?currency=EUR&source=ecb_rates"
curl -s "http://localhost:8080/api/v1/rates/latest?currency=GBP"
curl -s "http://localhost:8080/api/v1/rates/latest?currency=USD"
curl -s "http://localhost:8080/api/v1/rates/latest?currency=USD&source=us_treasury_rates"
curl -s "http://localhost:8080/api/v1/rates/time-series?rate_name=ESTR&currency=EUR"
curl -s "http://localhost:8080/api/v1/rates?source=ecb_rates&rate_family=policy_rate"
curl -s "http://localhost:8080/api/v1/rates/time-series?rate_name=US_TREASURY_PAR_YIELD&tenor=10Y&currency=USD"
curl -s "http://localhost:8080/api/v1/rates?country_or_region=united_states&rate_family=treasury_par_yield"
curl -s "http://localhost:8080/api/v1/rates/sources"

# Reference rates flow into capabilities + diagnostics (no curves are built)
curl -s "http://localhost:8080/api/v1/capabilities"
curl -s "http://localhost:8080/api/v1/diagnostics"

# Which sources can provide reference rates (rates_fixture + us_treasury_rates + ecb_rates implemented; boe planned)
curl -s "http://localhost:8080/api/v1/data-sources/capabilities?data_type=reference_rates"

# --- Document ingestion + change detection (offline fixture) ---

# Ingest documents (one fund, then all eligible funds)
uv run python -m app.workers.run document_snapshot_ingestion --fund-id 1
uv run python -m app.workers.run document_snapshot_ingestion

# Fund documents: full snapshot history, filter by type, or latest-per-type
curl -s http://localhost:8080/api/v1/funds/1/documents
curl -s "http://localhost:8080/api/v1/funds/1/documents?document_type=factsheet"
curl -s "http://localhost:8080/api/v1/funds/1/documents?latest_only=true"
curl -s http://localhost:8080/api/v1/documents/1

# Documents flow through fund detail, the dashboard and diagnostics
curl -s "http://localhost:8080/api/v1/funds/1/detail"
curl -s http://localhost:8080/api/v1/workspaces/1/dashboard
curl -s http://localhost:8080/api/v1/workspaces/1/diagnostics

# Which sources can provide documents (document_fixture is implemented)
curl -s "http://localhost:8080/api/v1/data-sources/capabilities?data_type=documents"

# --- Alert generation + workspace alerts (database-only, idempotent) ---

# Generate alerts for all workspaces, or one (re-running is idempotent)
uv run python -m app.workers.run alert_generation
uv run python -m app.workers.run alert_generation --workspace-id 1

# Or trigger via the job API (find the alert_generation job id in GET /api/v1/jobs)
curl -s -X POST http://localhost:8080/api/v1/jobs/{alert_generation_job_id}/run

# List + filter workspace alerts
curl -s http://localhost:8080/api/v1/workspaces/1/alerts
curl -s "http://localhost:8080/api/v1/workspaces/1/alerts?status=active"
curl -s "http://localhost:8080/api/v1/workspaces/1/alerts?category=document"
curl -s "http://localhost:8080/api/v1/workspaces/1/alerts?severity=warning"

# Read / dismiss / resolve a single alert; mark all read
curl -s -X POST http://localhost:8080/api/v1/workspaces/1/alerts/1/read
curl -s -X POST http://localhost:8080/api/v1/workspaces/1/alerts/1/dismiss
curl -s -X POST http://localhost:8080/api/v1/workspaces/1/alerts/1/resolve
curl -s -X POST http://localhost:8080/api/v1/workspaces/1/alerts/mark-all-read

# Alerts also flow into the dashboard (alert_summary) and diagnostics (alert counts)
curl -s http://localhost:8080/api/v1/workspaces/1/dashboard
curl -s http://localhost:8080/api/v1/workspaces/1/diagnostics

# --- Exposure recompute + derived exposure (database-only, idempotent) ---

# Recompute cached look-through exposure (all workspaces, or one); re-runs are no-ops
uv run python -m app.workers.run exposure_recompute
uv run python -m app.workers.run exposure_recompute --workspace-id 1

# Or trigger via the job API (find the exposure_recompute job id in GET /api/v1/jobs)
curl -s -X POST http://localhost:8080/api/v1/jobs/{exposure_recompute_job_id}/run

# Latest exposure snapshot, then filter by dimension (holding supports limit)
curl -s http://localhost:8080/api/v1/workspaces/1/exposure
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure?dimension=sector"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure?dimension=country"
curl -s "http://localhost:8080/api/v1/workspaces/1/exposure?dimension=holding&limit=20"

# Snapshot history (metadata + row counts)
curl -s http://localhost:8080/api/v1/workspaces/1/exposure/snapshots

# Exposure also flows into the dashboard (exposure block) and diagnostics
curl -s http://localhost:8080/api/v1/workspaces/1/dashboard
curl -s http://localhost:8080/api/v1/workspaces/1/diagnostics
```

## Environment variables

| Variable             | Default                                                            | Purpose                              |
| -------------------- | ----------------------------------------------------------------- | ------------------------------------ |
| `ENVIRONMENT`        | `development`                                                      | Free-form deployment label (logs/diagnostics) |
| `DATABASE_URL`       | `postgresql+asyncpg://etf:etf_password@localhost:5432/etf_data`   | Async SQLAlchemy connection string   |
| `API_HOST`           | `0.0.0.0`                                                          | API bind host (in-container)         |
| `API_PORT`           | `8080`                                                             | API port (also the published host port in Compose) |
| `LOG_LEVEL`          | `info`                                                             | Log level                            |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000,http://localhost:5173`                     | Comma-separated allowed CORS origins |
| `BASE_CURRENCY`      | `GBP`                                                              | Portfolio reporting base currency    |
| `RESOLVER_DEFAULT_PROVIDER` | `stub`                                                     | Instrument resolver (`stub`/`openfigi`) |
| `OPENFIGI_API_KEY`   | _(none)_                                                          | Optional OpenFIGI key (higher rate)  |
| `PRICE_SOURCE_DEFAULT` | `stooq`                                                        | Default price source (`stooq`/`yfinance`) |
| `DISTRIBUTION_SOURCE_DEFAULT` | `distribution_fixture`                                  | Default distribution source adapter  |
| `ISSUER_FACTS_SOURCE_DEFAULT` | `issuer_fixture`                                        | Default issuer-facts source adapter  |
| `RATES_SOURCE_DEFAULT` | `rates_fixture`                                               | Default reference-rate source adapter |

For Docker, `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` (and optional
`POSTGRES_PORT`) initialise the Postgres container; the compose file derives the
in-network `DATABASE_URL` from them (host `postgres`, port `5432`), so you
normally only edit `POSTGRES_PASSWORD`. The prod override also reads
`API_BIND_HOST` (default `127.0.0.1`) and `API_PORT` (default `8080`) to decide
which host interface/port the API is published on — leave `API_BIND_HOST` at
`127.0.0.1` so only your reverse proxy can reach it. See
[`infra/.env.example`](infra/.env.example) and the host-run template
[`.env.example`](.env.example). Full deployment guidance is in
[`docs/operations.md`](docs/operations.md).

## Docker Compose

Full deployment, migration, worker, backup, and security guidance lives in
[`docs/operations.md`](docs/operations.md). Quickstart:

```bash
cd infra
cp .env.example .env               # then edit POSTGRES_PASSWORD etc.

docker compose up -d postgres      # start the database
docker compose run --rm migrate    # apply migrations (explicit, idempotent)
docker compose up -d --build api   # build + start the API

# Optional demo seed data once the stack is up:
docker compose run --rm api uv run python -m app.seed.seed_data

# Smoke test:
curl -fsS http://localhost:8080/health
../scripts/smoke_api.sh
```

Services in `infra/docker-compose.yml`:

- **postgres** — named volume (`pgdata`), bound to `127.0.0.1:5432` only,
  **never exposed publicly**, with a `pg_isready` healthcheck.
- **migrate** — one-shot `alembic upgrade head` (same image/env as the API).
- **api** — listens on `8080` (host port via `API_PORT`); waits for `migrate`
  to complete and Postgres to be healthy; ships a `/health` healthcheck. The API
  container does **not** auto-migrate on start — migrations are an explicit step.
- **scheduler** (opt-in profile) — `docker compose --profile scheduler up -d scheduler`.

For a VPS, layer the production override (binds the API to `127.0.0.1` **only**,
drops the Postgres host port, rotates logs):

```bash
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$COMPOSE build api
$COMPOSE up -d postgres
$COMPOSE run --rm migrate
$COMPOSE up -d api
../scripts/smoke_api.sh
```

The API is published on `127.0.0.1:${API_PORT:-8080}` only — point your VPS's
**existing** reverse proxy (e.g. Caddy) at it; the stack does not ship or manage
a proxy:

```caddyfile
your.domain.example {
    reverse_proxy 127.0.0.1:8080
}
```

> **Private-beta.** Set a strong `MIMER_API_TOKEN` to require `Authorization:
> Bearer <token>` on `/api/v1` (blank = unauthenticated local dev); `/health` +
> `/health/db` stay open. Even with a token, keep the API behind your existing
> reverse proxy / tunnel — Caddy forwards the header untouched and needs no token.
> **Not deployed yet:** this slice verifies the local Docker runtime + auth ahead
> of the VPS move. Full VPS handoff (image transfer, migrate, smoke, workers,
> scheduler, backups, Caddy snippet, troubleshooting) is in
> [`docs/operations.md`](docs/operations.md) §§ 2–2b.

## Client / GUI integration contract

The backend exposes **both** granular resource endpoints (for debugging and
specific views) and **client-friendly aggregate endpoints** (for one-call GUI
hydration). The GUI should not need 15 HTTP calls to draw its initial workstation
view — use the aggregates below, then fetch deeper detail on demand. All decimal
values are JSON strings; aggregate responses are **bounded** (latest/recent
slices, not full history).

### Provenance & freshness fields

Records carry provenance/lifecycle fields where the schema supports them — some
of `source`, `status`, `as_of_date`, `last_refreshed_at`, `last_price_at`,
`last_resolved_at`, `created_at`, `updated_at` — plus a **derived** `freshness`
state (`fresh` | `stale` | `missing`, computed at read time from the relevant
timestamp). The dashboard/detail responses also include a `freshness` summary per
data domain and a `data_quality` diagnostics block. The GUI should surface these
(badges, "as of" labels) and must treat them as load-bearing.

### 1. Recommended initial load flow

```text
GET /api/v1/workspaces                               # pick a workspace
GET /api/v1/workspaces/{workspace_id}/dashboard      # hydrate the whole workstation
GET /api/v1/workspaces/{workspace_id}/diagnostics    # data-quality badges
```

The `dashboard` response has these sections (each bounded):

```text
workspace            portfolio_summary    positions         funds
fund_listings        distributions        holdings          exposures
exposure             documents            alerts            alert_summary
scheduled_jobs       job_runs             fx_rates          data_quality
freshness
```

(`exposures` is the legacy ad-hoc slice block; `exposure` is the cached
derived-snapshot block — see **Exposure**.)

### 2. Instrument add flow

```text
POST /api/v1/instruments                 # resolve -> create/reuse + queue backfill
GET  /api/v1/jobs/runs?fund_id={id}      # watch the queued/real backfill runs
GET  /api/v1/funds/{fund_id}/detail      # hydrate the new fund's detail page
```

### 3. Chart flow

```text
GET /api/v1/fund-listings/{id}/time-series?kind=price&range=1y
GET /api/v1/funds/{id}/time-series?kind=distribution&range=all
GET /api/v1/workspaces/{id}/portfolio/time-series?kind=portfolio_value&range=1y
```

`kind` ∈ `price | nav | market_value | distribution | yield | portfolio_value |
fx`; `range` ∈ `1m | 3m | 6m | 1y | all`. Series with no backing data yet (NAV,
yield, fx) return `status="unavailable"` with no points — **the API never
fabricates chart data**. Derived series (portfolio value/income) are marked
`status="derived"` and may be sparse when little price history exists.

### 4. Jobs flow

```text
GET  /api/v1/jobs                        # scheduled job definitions
GET  /api/v1/jobs/runs?job_type=&fund_id=&fund_listing_id=&status=
POST /api/v1/jobs/{id}/run               # price/issuer-facts real; others stub
```

### 5. Important invariants

- **A ticker is not identity.** A fund is identified by internal id + **ISIN**.
- One fund can have **many listings** (exchanges/currencies); two tickers can be
  the same fund (e.g. JEPG/JEGP).
- **Prices belong to listings**; **distributions/holdings/documents belong to
  funds**. Positions are **workspace-private** (always carry `workspace_id`).
- **Source/status/provenance/freshness fields must be preserved** through the GUI
  — don't drop them on round-trips.
- The client may hold **local overrides the backend does not know about** (see
  below); a refresh must not silently erase them.

## Manual overrides

The GUI currently keeps **manual overrides local-only** — the backend does not
store or sync them yet, by design.

- **Backend source data stays canonical/raw.** The API returns issuer/exchange
  /seed-sourced values with their `source`/`status`; it never silently merges a
  client's edits.
- **The client owns its overrides for now.** It should keep them in its own local
  store and re-apply them on top of API responses when rendering.
- **Refresh must not erase user overrides.** Because overrides live on the client,
  re-fetching reference data cannot delete them; the client decides how raw vs
  overridden values are displayed.
- **Distinguish raw from derived/manual/estimated.** Derived values already carry
  `source="derived"` and series carry `status` (`active`/`derived`/`unavailable`);
  a future manual layer should use `source="manual"` so the GUI can show "edited"
  badges and diff against the raw source.

**Intended future backend design (not implemented):** an optional
workspace-scoped `manual_overrides` table (keyed by `workspace_id` + target
entity/field) that the read endpoints overlay on top of raw data, with `source`
priority (`manual` outranks automated ingestion — the `issuer_facts` worker
already respects this for `funds.source`). Until then, treat overrides as a pure
client concern. There is **no** `manual_overrides` table today; nothing is
half-built.

## Data source strategy

The schema is built around **multiple data sources**, even though no real
ingestion is implemented yet. Every fact-bearing table records a `source`, and a
`data_sources` table (with `source_type` and `priority`) supports future source
ranking.

1. **Issuer / provider** (`issuer`) — preferred source of truth for *fund facts*
   (name, ISIN, OCF/TER, domicile, strategy, distribution policy), *official
   holdings*, *official distribution history* and *documents* (factsheet, KID,
   prospectus, annual/interim report). E.g. Vanguard (VUSA), iShares/BlackRock
   (ISF), J.P. Morgan AM (JEPG/JEGP). Issuer data should outrank third-party
   summaries for these facts because it is authoritative and timely.
2. **Exchange / market-data** (`exchange`, `market_data`) — used for *market
   price*, price date, trading currency, ticker, and (later) volume / bid-ask /
   NAV / premium-discount. E.g. LSE delayed data, Stooq, Alpha Vantage, Yahoo as
   a **fallback only**. Free sources are not equally authoritative, so the
   `source` is stored on **every** price row.
3. **FX** (`fx`) — used to convert trading and distribution currencies into the
   portfolio base currency, and to handle GBP/GBX/USD/EUR correctly. Historical
   rates are stored in `fx_rates` (rate = quote units per 1 base unit). E.g. ECB
   reference rates.
4. **Broker / user import** (`broker`) — the **authoritative** source for the
   user's actual positions: units owned and average cost (later: transactions,
   acquisition dates, fees/taxes). Positions are seeded manually for now; broker
   CSV import comes later.
5. **Manual override** (`manual`) — for correcting stale/missing/duplicated
   public data (ticker mappings, listings, distributions, cost basis, ignored
   alerts). Manual overrides should usually outrank automated ingestion.

**Why ticker-only identity is unsafe:** one fund can have several listings with
different tickers, exchanges and currency units (e.g. JEPG in GBP and JEGP in
USD are the *same fund*). Identity is therefore the internal fund id + ISIN;
tickers are exchange/currency aliases on `fund_listings`.

**Ingestion jobs status:** `price_ingestion`, `issuer_facts_ingestion`,
`distribution_ingestion`, `issuer_holdings_ingestion`, `fx_ingestion`,
`document_snapshot_ingestion`, `alert_generation`, `exposure_recompute`,
`constituent_identity_resolution` and `constituent_eod_price_ingestion` are
**real** (all external ingestion defaults to offline fixture providers, with live
Stooq/yfinance/OpenFIGI behind the source budget when explicitly requested;
`alert_generation` and `exposure_recompute` are database-only with no external
provider); `rates_curve_ingestion`, `bond_reference_ingestion` and
`broker_csv_import` are **planned**. The `scheduled_jobs` + `job_runs` tables
track
schedule, source, job type, status, timings, message and record counts
(`job_runs` supersedes the legacy `ingestion_runs` table). The live status is at
`GET /api/v1/capabilities`.

**Catalogue & registry:** the full data-source catalogue, adapter strategy,
licensing caution and future multi-asset roadmap live in
[`docs/data_sources.md`](docs/data_sources.md); the vendor research is in
[`SOURCES.md`](SOURCES.md). A programmatic **source capability registry**
(`app/sources/registry.py`, exposed at `GET /api/v1/data-sources/capabilities`)
records, per source, its type, asset classes, data types, whether it needs an API
key, history/intraday/live support, reliability tier and whether an adapter is
implemented — so provider assumptions are not hard-coded across the codebase.

## Security & auth

- Postgres is never exposed publicly (localhost-bound port only in Compose).
- `.env` and secrets are git-ignored; only `*.env.example` files are committed
  (with a **blank** `MIMER_API_TOKEN=` — never a real token).
- CORS is restrictive by default but allows localhost dev origins.
- **Optional shared Bearer token.** Set `MIMER_API_TOKEN` and every `/api/v1`
  request must send `Authorization: Bearer <token>` (constant-time compared in
  `app/api/security.py`, applied as a `/api/v1` router dependency); blank/unset
  disables it. `/health` + `/health/db` stay unauthenticated. The token is never
  logged and never returned by any endpoint (capabilities/diagnostics included).
  Generate one with `openssl rand -hex 32`. See
  [`docs/operations.md` § Security](docs/operations.md).
- **Still no per-user identity.** For stronger isolation, layer on top:
  - **Reverse-proxy auth** (Caddy/nginx/oauth2-proxy in front of the API).
  - **VPN / Tailscale-only access** (no public exposure at all).
  - Full per-user identity + enforced workspace membership (future work).

## Testing

```bash
uv run pytest
```

Tests run against an **in-memory SQLite** database (via `aiosqlite`) using the
real ORM models and seed builders, with the DB dependency overridden — so no
running Postgres is required. Covered: health, funds (incl. multi-listing, the
structured 404, and source/status/provenance fields), the portfolio summary
(incl. GBX→GBP conversion and decimal-as-string output), position create/delete,
instrument resolution, price + issuer-facts ingestion workers (with
mocked/fixture sources — **no live network**), the GUI aggregate endpoints
(`dashboard`, `funds/{id}/detail`, listing/fund/portfolio `time-series`,
`hierarchy`, `diagnostics`), jobs filtering + duplicate-run guard, a
migration-chain render check, and the **alert engine** (pure rules, per-rule
generation, idempotent re-run / auto-resolve / dismissed-stays-dismissed, the
`alert_generation` worker for one/all workspaces with per-workspace failure
isolation, the alert API, and dashboard/diagnostics alert counts), and the
**exposure engine** (deterministic input hash, per-rule look-through maths,
coverage/unclassified handling, missing-holdings/FX statuses, idempotent
recompute, the `exposure_recompute` worker, the exposure API, and dashboard/
diagnostics/alert integration).

**What remains for tests:** a Postgres-backed integration test (e.g. via
`testcontainers`) to validate Postgres-specific DDL/behaviour and the Alembic
migration itself; broader coverage of FX edge cases.

## Not implemented yet

- Real **live** issuer/holdings/document/FX scraping. `price_ingestion`
  (Stooq/Yahoo) is live; `issuer_facts_ingestion`, `distribution_ingestion`,
  `issuer_holdings_ingestion`, `fx_ingestion` and `document_snapshot_ingestion` run
  against **offline fixture** providers; no scheduler runs any of them on a timer
  yet. Document **PDF text extraction / OCR** and text-level diffing are not
  implemented (only metadata + content-hash change detection).
- Production **per-user** authentication / authorization and enforced workspace
  membership (v1 uses the default workspace / `X-Workspace-ID` header with no
  identity). A single optional shared **Bearer token** (`MIMER_API_TOKEN`) can
  already gate `/api/v1` for deployment, but it is a deployment guard, not user
  identity.
- **Alert notification delivery** (email/push) and a user-configurable rule DSL /
  per-workspace thresholds — `alert_generation` produces alerts in the DB only.
- **True look-through valuation** of ETF constituents. Constituent EOD prices are
  now ingested (`constituent_eod_price_ingestion` → `instrument_prices`) and
  surfaced per constituent / via time-series, but the exposure recompute still
  values *funds*, not their underlying stocks — wiring constituent prices into
  look-through valuation, exposure drift and top-holding performance is the next
  slice. Official **reference rates** are now collected (`rates_ingestion` →
  `reference_rates`): the offline fixture plus **live US Treasury par-yield**
  (`us_treasury_rates`, official XML feed) and **live ECB** (`ecb_rates`, official
  Data Portal SDMX API — key interest rates + €STR) adapters, both via
  `guarded_fetch`; the `boe_rates` live adapter stays planned (IADB CSV export
  returns 403 to a plain client). **Yield curves** (fitting / bootstrapping / discount
  factors / forward rates) stay planned and live in the Rust local pricer, **never**
  the backend. Bond prices are also planned.
- **Exposure drift detection** (sector/holding weight changes vs prior snapshot),
  `asset_class` exposure, and direct non-fund instruments — the
  `exposure_snapshots` history and generic `dimension` model are ready for these.
- Local-first client sync (the shared/private split keeps it possible).
- `daily_change` in the summary (needs previous-day prices) — returned as `null`.
- Transactions / watchlists are modelled (tables) but have no endpoints yet;
  positions remain the source of truth.
- DuckDB analytics layer.

## Roadmap

- **Scheduler / job-leasing / rate-budget foundation** (the next slice) — a
  scheduler worker that claims due `scheduled_jobs`, leases runs, enforces
  per-source rate budgets, logs fetches and caches requests. This is the
  prerequisite for safe live market data (OpenFIGI/yfinance/Stooq) and ETF
  **constituent / security-master** ingestion (constituent EOD prices, then
  exposure drift alerts).
- Real ETF data **ingestion workers** (issuer facts/holdings/docs, exchange
  prices, FX) writing through `ingestion_runs` with source/priority handling.
- **Scheduled** price / distribution / FX / exposure updates — a real scheduler
  executing `scheduled_jobs` and writing `job_runs` (VPS cron / APScheduler /
  Celery-RQ-Arq / Docker worker).
- **Document download + change detection** (content hash / date diffing).
- **DuckDB analytics layer** for derived tables and snapshot calculations
  (Postgres stays the canonical source of truth).
- **Authentication** (bearer token → reverse-proxy / VPN) plus enforced
  per-user **workspace membership**.
- **Local-first client mode** and optional sync of private workspace data.
- **Backups** of Postgres.
- **Deployment** behind a reverse proxy / HTTPS / VPN on a VPS.
