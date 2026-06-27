# AGENTS.md

Guidance for AI agents and contributors working in this repository.

## What this repository is

A **standalone ETF / portfolio analytics backend service**. It exposes a
RESTful JSON API (FastAPI) backed by Postgres. It is consumed by separate
clients (desktop GUI, browser dashboard, CLI, automation workers) that live in
**other** repositories.

## Hard rules

- **Do not add GUI/frontend code here.** This is a backend service only.
- **Keep the layers separate:**
  - `app/api/` — HTTP routing only (thin; no business logic).
  - `app/schemas/` — Pydantic request/response models.
  - `app/services/` — business logic; operate on an `AsyncSession`.
  - `app/db/models.py` — SQLAlchemy ORM models (the canonical schema).
- **Identity:** use internal fund id + **ISIN** as canonical identity.
  A **ticker is not identity** — it is an exchange/currency-specific alias.
  One fund has many listings (see `Fund` → `FundListing`).
- **Money:** use `Decimal` / `Numeric` everywhere for monetary and weight
  values. **Never use floats for money.** Decimals serialise to JSON as strings.
- **Sources:** every fact-bearing row carries a `source`. Keep ingestion
  separate from the core API (see the `job_runs` / `data_sources` tables and the
  roadmap). Do not bolt scraping into request handlers.
- **Shared vs private data:** keep shared/reference data (funds, listings,
  prices, distributions, holdings, documents, fx_rates, data_sources, job runs)
  separate from workspace/private data. **Every workspace-private table must
  carry `workspace_id`** (positions, transactions, watchlists, settings,
  `alerts`). Never make positions or other private data global.
- **Auth:** do not add full SaaS auth yet. v1 resolves the workspace from the
  URL path or the `X-Workspace-ID` header (default workspace fallback). A simple
  **shared Bearer token** optionally guards the whole `/api/v1` surface: set
  `MIMER_API_TOKEN` (blank/unset = disabled) and every `/api/v1` request must send
  `Authorization: Bearer <token>` (constant-time compared in
  `app/api/security.py:require_api_token`, applied as a router dependency in
  `app/api/router.py`). `/health` + `/health/db` stay unauthenticated. This is
  **not** user identity — no user DB, sessions, cookies or OAuth; do not grow it
  into one without an explicit decision. **Do not log `MIMER_API_TOKEN` and do not
  expose it in diagnostics/capabilities** (only whether auth is enabled, if ever).
- **Scheduler:** a real in-process scheduler exists
  (`app/workers/scheduler.py`): it claims **due** `scheduled_jobs`, **leases**
  them (atomic conditional `UPDATE` on `locked_by`/`lock_expires_at`), and runs
  the existing `app.workers.run.run_job` directly. Keep it that way — **no**
  Celery/RQ/Kafka, **no** OS cron, **no** `pg_cron`, **no** subprocess per job.
  The scheduler imports and calls business logic; it never shells out.
- **External data fetching (read this before touching any source adapter):**
  - **Do not call external data sources inside uncontrolled loops.** An ETF can
    hold hundreds of stocks; per-holding fan-out is forbidden.
  - **Do not call OpenFIGI / yfinance / Stooq / issuer sites per holding** without
    **dedupe + budget + cache**. Use the market-data planner to dedupe and
    prioritise first (`app/services/market_data_planner.py`).
  - **Use source budgets** (`app/services/source_budget.py` →
    `check_budget` / `guarded_fetch`) for any new live adapter. `guarded_fetch`
    is the one place to call out: cache → budget → fetch-log → fetch.
  - **External fetches must have request keys / fetch logs** where possible
    (`source_fetch_logs`); the request key is `source + request_kind + normalised
    params` and must be deterministic.
  - **Do not print secrets. Do not store API keys in logs.** Request keys drop
    credential params; fetch logs store an `endpoint_label` + hashes, never API
    keys, auth headers or tokenised URLs. API responses expose only
    `openfigi_api_key_configured: true/false`.
  - **Tests must not require live network.** Ship an offline fixture/mocked path;
    `OPENFIGI_API_KEY` is neutralised in the test suite.
- **Local-first:** preserve local-first compatibility where practical — the
  backend must be usable purely as a shared reference-data provider, with a
  client holding private positions locally.
- **Identity resolution:**
  - Do not assume tickers are globally unique; a ticker is exchange/currency
    local. Prefer ISIN/FIGI for identity. Funds dedupe on ISIN.
  - Return candidates for ambiguous/low-confidence ticker resolution — never
    silently create the wrong fund. Auto-create only on a single high-confidence
    match that carries an ISIN.
  - Store resolver provenance in `security_identifiers` (source, confidence, raw
    payload).
- **Ingestion / workers:**
  - **Two-layer split is mandatory.** A *source adapter* (`app/sources/…`) only
    fetches/parses one provider and returns normalized dataclasses with
    provenance — it must not touch the DB or job bookkeeping. The
    provider-*agnostic* *ingestion service/worker* (`app/services/*_ingestion.py`,
    `app/workers/run.py`) calls the adapter, validates output, upserts canonical
    tables idempotently, records `source`, and writes a `job_runs` row
    (status + `records_inserted/updated/failed`). Keep them separate.
  - Keep source adapters isolated behind `app/sources` + `app/services/resolver`
    providers, selected via a `get_*_source(name)` registry that reads a
    `*_SOURCE_DEFAULT` setting; the API route must not depend on a specific
    provider. **Add every new source behind such an adapter** — never inline
    scraping/parsing into a service or route.
  - **Register new sources** in `app/sources/registry.py` (`SourceCapability`) and
    keep the `capabilities` service / `docs/data_sources.md` status table honest
    (real/fixture/stub/planned). `MIGRATION_HEAD` in `app/services/capabilities.py`
    is bumped with each Alembic revision (a test enforces this).
  - Keep workers callable from the CLI (`python -m app.workers.run ...`).
  - **Idempotency:** upserts must respect the relevant unique constraint
    (`prices` on `(fund_listing_id, price_date, source)`; `distributions` on
    `(fund_id, ex_date, source)`; `fund_holdings` on
    `(fund_id, as_of_date, source, holding_key)`; `fx_rates` on
    `(rate_date, base_currency, quote_currency, source)`; `document_snapshots` on
    `(fund_id, document_type, source, content_hash)`; `alerts` on
    `(workspace_id, dedupe_key)`; `exposure_snapshots` on
    `(workspace_id, as_of_date, input_hash)`) so re-runs/backfills never
    duplicate rows. Every ingestion run writes a `job_runs` row. For holdings,
    `holding_key` is a deterministic identity derived by the source/ingestion
    layer (ISIN > FIGI > CUSIP > SEDOL > normalised name+ticker) — not fuzzy
    matching. Reads pick a single coherent snapshot per fund via
    `holdings_ingestion.latest_holdings_by_fund` (highest-priority source, then
    most recent `as_of_date`) so seed/fixture/manual rows never mix.
  - **Respect source priority.** `manual` outranks an issuer; an issuer outranks
    `seed`/automated; empty fields are always filled; never silently clobber a
    higher-priority source. Distinct sources may coexist for the same logical
    fact (provenance differs) — keep both.
  - **Document provider limitations** in the adapter docstring + registry notes
    (delays, fragility, licensing, jurisdiction variants). Free/public data is not
    equally authoritative; reliable bond/option/futures data is generally paid.
  - Do not add a queue/broker until needed (jobs run synchronously for now).
  - Real ingestion is implemented for `price_ingestion`, `issuer_facts_ingestion`,
    `distribution_ingestion`, `issuer_holdings_ingestion`, `fx_ingestion` and
    `document_snapshot_ingestion` (most default to offline **fixture** sources).
    `alert_generation` and `exposure_recompute` are real and **database-only** (no
    source adapter — they derive from existing signals). Use mocked/fixture external
    responses in tests — **never hit live APIs** (no test may require network).
  - **Live issuer holdings adapters:** `issuer_holdings_ingestion` defaults to the
    offline `holdings_fixture` but has live, **explicit-only**, `guarded_fetch`-ed
    issuer adapters (`app/sources/holdings.py`): `blackrock_ishares_holdings`
    (issuer CSV), `jpmorgan_etf_holdings` (FundsMarketingHandler, content-sniffed
    CSV/TSV/HTML-table). `vanguard_holdings_export` is an offline exported-file
    parser; `vanguard_holdings` live is **planned**. Rules (do not regress):
    - **Do not use Yahoo / JustETF / Morningstar / TradingView as a canonical
      holdings source.** Issuer-published files only.
    - **Do not treat index constituents as ETF holdings** (no index-constituent
      substitution).
    - **Do not scrape brittle Vanguard (or other) product-page HTML** as a canonical
      source — keep `vanguard_holdings` planned until a stable official
      machine-readable endpoint is verified.
    - **Do not call identity resolvers (OpenFIGI) from holdings ingestion** — identity
      resolution stays a separate worker/stage. Populate the canonical identifier
      columns so it can build safe requests.
    - **Do not bypass `guarded_fetch` / source budgets** for any live download; keep
      request keys/fetch logs secrets-free (ISIN-only key, host/path endpoint label).
    - **Do not add pandas / xlrd / calamine** for holdings parsing. The stdlib parses
      what we support: CSV/TSV (`csv`), HTML tables (`html.parser`) and OOXML `.xlsx`
      workbooks (`app/sources/spreadsheet.py` — `zipfile` + `xml.etree`). The **legacy
      binary `.xls` (OLE2/BIFF) stays deferred** (it needs a binary-Excel dependency we
      avoid); it is detected and surfaced by `--verify-source` as
      `reason=binary_unsupported`, never decoded. The live downloader is content-aware
      (raw bytes for a binary body so the workbook sniffer is byte-exact); do not text-
      decode a binary response.
    - **Do not move look-through analytics / PnL / total return into the backend** —
      this layer only collects, normalises and persists published holdings.
    - The live adapters take an explicit `--url` (single-fund) or a verified
      known-URL registry; without one they are a clean no-op. A bad row is isolated
      (counted), never failing the whole file.
  - **Live issuer distribution adapters:** `distribution_ingestion` defaults to the
    offline `distribution_fixture` but has live, **explicit-only**, `guarded_fetch`-ed
    issuer adapters (`app/sources/distributions.py`): `jpmorgan_distributions`
    (FundsMarketingHandler `?type=fundDistribution`, content-sniffed CSV/TSV/HTML-table)
    and `vanguard_distributions` (product-data `distributionHistory` JSON/JSONP).
    `vanguard_distributions_export` is an offline exported-file parser (JSON/JSONP/CSV);
    `blackrock_ishares_distributions` live is **planned** (no clean official iShares
    distribution endpoint verified). The canonical row carries ex/record/payment/
    distribution dates, amount, currency, distribution type, frequency, share class,
    `status` and `raw_payload_json`; idempotent upsert on `(fund_id, ex_date, source)`
    (a row with no ex-date falls back to its distribution/payment/record date as the
    identity date). Rules (do not regress):
    - **Do not use Yahoo / JustETF / Morningstar / TradingView as a canonical
      distribution source.** Issuer-published files/APIs only.
    - **Do not scrape brittle Vanguard (or other) product-page HTML** — use the
      official product-data JSON or a manually exported file.
    - **Do not guess an iShares distribution URL** from the holdings `...ajax`
      pattern; keep `blackrock_ishares_distributions` planned until a clean official
      endpoint is verified.
    - **Do not forecast dividends, project yield, compute tax treatment, total
      return or PnL** in the backend — this layer only collects, normalises and
      persists published distribution *observations* (those engines live in the Rust
      GUI / local pricer).
    - **Do not bypass `guarded_fetch` / source budgets** for any live download; keep
      request keys/fetch logs secrets-free (ISIN-only key, host/path endpoint label).
    - **Do not add pandas / xlrd / calamine** for distribution parsing. The stdlib
      parses what we support: CSV/TSV (`csv`), JSON/JSONP (`json`), HTML tables
      (`html.parser`) and OOXML `.xlsx` workbooks (`app/sources/spreadsheet.py`). The
      **legacy binary `.xls` (OLE2) stays deferred** (`reason=binary_unsupported`),
      never decoded.
    - The live adapters require an explicit `--url`; without one they are a clean
      no-op (the default stays the offline fixture). A bad row is isolated (counted),
      never failing the whole file.
  - **Known issuer source configuration + verification:** the per-fund verified/
    candidate live download URLs live in one in-code registry
    (`app/sources/issuer_source_config.py`, keyed by ISIN + `source_name`, each with a
    `source_status`: `verified`/`candidate`/`planned`/`disabled`). The holdings/
    distribution adapters resolve `--url` first, then a *usable* (verified/candidate)
    config; the planner (`known_config`/`config_status`/`needs_url_config`/
    `recommended_command`), the capabilities endpoint (`requires_url`/
    `known_config_available`/`config_status`/`example_fund_identifiers`) and
    diagnostics (`issuer_source_configs`/`verified_*`/`candidate_*`/
    `missing_holdings_source_config`/`missing_distribution_source_config`) all read it.
    `--verify-source` (worker flag) / `verify_issuer_source_config` run ONE guarded
    fetch+parse and report whether a config can be promoted, without ingesting. Rules
    (do not regress):
    - **Do not mark a config `verified` without a successful live fetch+parse.** Seed
      new endpoints as `candidate`; promotion is a deliberate code change after a
      clean `--verify-source` (the in-code registry stores no `verified_at`).
    - **Do not make a candidate (or any live) source the ingestion default.** A
      config is auto-used only when its live `--source` is explicitly named — the
      configured default always stays the offline fixture.
    - **Do not hide missing source config.** Surface it in the planner
      (`needs_url_config=true` + a "configure an issuer source URL" recommended
      action) and diagnostics (`missing_*_source_config`, informational — never an
      alert; the fixture default still works). A missing config is a clean no-op,
      never an error.
    - **Do not scrape Vanguard (or other) product-page HTML**, and **do not guess a
      distribution URL from a holdings URL** — verify each endpoint per product.
    - **Do not use browser automation / cookies / TLS or browser fingerprint spoofing**
      to reach Vanguard (or any source). A live adapter may send conservative,
      *identifying* official headers (a research-client `User-Agent` + `Accept` +
      `Accept-Language`, no cookies); a config that only works via browser-like hacks is
      **not** verified — keep it `candidate` and prefer the offline `*_export` fallback.
    - `--verify-source` must not ingest, must not bypass budgets/cache/fetch logs, and
      must keep request keys/endpoint labels secrets-free (ISIN-only key, host/path
      label). It is verification + configuration only — **no analytics**. It reports a
      stable `reason` verdict (`verified` / `binary_unsupported` / `zero_rows` /
      `missing_fields` / `cache_hit` / `budget_blocked` / `no_url` / `fetch_error` /
      `unknown_source`) + the detected `payload_format` — keep those distinct and honest.
  - **Constituent identity resolution:** `constituent_identity_resolution`
    (`app/services/constituent_identity.py` + `app/sources/constituents.py`)
    resolves ETF/fund *constituents* into the canonical instrument master
    (`instruments` / `instrument_listings` / `instrument_identifiers`), the
    prerequisite for constituent EOD prices. It is real plumbing with an offline
    `constituent_identity_fixture` default and a live, **batched**, `guarded_fetch`-ed
    `openfigi` path. Rules (do not regress): dedupe requests across funds; **never
    send name-only to OpenFIGI**; resolve on strong identifiers (ISIN/FIGI/CUSIP/
    SEDOL — collapse multi-venue listings of one security via the share-class FIGI);
    **never link an ambiguous/not-found result** to a guessed instrument; idempotent
    upserts on the deterministic `instrument.identity_key` / `listing_key` /
    `(instrument_id, scheme, value, source)`; **never clobber a `manual`**
    instrument or holding link.
  - **Constituent EOD prices:** `constituent_eod_price_ingestion`
    (`app/services/instrument_prices.py` + `app/sources/instrument_prices.py`)
    fetches end-of-day bars for *resolved* constituents into `instrument_prices`
    (a generic OHLC + adjusted-close + volume table keyed on
    `(instrument_listing_id, price_date, source)`), the prerequisite for true
    look-through valuation, exposure drift, top-holding performance and stock
    detail pages. It is real plumbing with an offline `instrument_price_fixture`
    default and live, **budget-guarded**, one-symbol-at-a-time `stooq`/`yfinance`
    paths. Rules (do not regress): **only price resolved constituents** (linked to
    an instrument) — never guess prices for ambiguous/not-found/unresolved
    holdings; **dedupe listings across funds** (Apple priced once, even if held via
    several funds) so a live provider never loops per holding; route every live
    call through `guarded_fetch` (source budget → fetch log → request cache) and
    keep request keys secrets-free; **do not call yfinance/Stooq/OpenFIGI in
    uncontrolled per-holding loops** — use the market-data planner's
    `fetch_constituent_price` backlog, respect `--limit` and the source budget's
    batch size; a missing/failed/budget-blocked/cached listing is isolated and
    counted, never failing the whole job; idempotent upsert (rerun ⇒ no duplicate,
    identical value ⇒ no update, changed OHLC/adjusted/volume/status ⇒ update only
    the changed row, distinct sources coexist); **never clobber a `manual`** price
    row; the seeded scheduler job and the worker default stay **offline** (the
    scheduler passes `source_name=None`, so the fixture provider runs — live
    Stooq/yfinance only when explicitly requested with a small `--limit`). Do
    **not** fold rates/yield curves, bond prices or broker CSV import into this
    price worker — they are their own slices (reference rates + broker import have
    since shipped). `instrument_prices` is a *complement* to fund-listing `prices`
    (do not merge the two). These bars now feed **true constituent look-through valuation**
    (see the exposure rules below).
  - **Unified instrument EOD prices:** `instrument_eod_price_ingestion`
    (`ingest_instrument_eod_prices` in `app/services/instrument_prices.py`) is the
    one price path for **any** resolved `instrument_listing` — ETF/fund
    constituents *and* directly-held imported broker holdings (and future
    manually-linked instruments). Once `imported_instrument_resolution` links a
    broker transaction to a canonical listing, it is priced through the **same**
    selector (`select_priceable_listings`, which unions constituents + resolved
    imported listings, deduped) and the **same** idempotent upsert into
    `instrument_prices` — so an imported TSLA is chartable like any constituent.
    Rules (do not regress): **do NOT create a separate price table for imported
    holdings** — `instrument_listings`/`instrument_prices` is the common path;
    **only price resolved listings** (a resolved/ready imported transaction with a
    listing, or a linked constituent) — never unresolved/ambiguous transactions
    (they carry no listing); **dedupe** so an instrument held both as a constituent
    and imported directly is priced once; skip **unpriceable** (no-ticker) listings
    and **fresh** ones (unless `--force`); **do not bypass the source budget /
    fetch log / request cache** for live Stooq/yfinance; **do NOT compute PnL /
    tax lots / total return / corporate actions in the price worker** (those belong
    in the Rust GUI / local pricer); **do not loop unbounded** over all instruments
    (respect `--limit` + the budget batch size). `constituent_eod_price_ingestion`
    stays a backward-compatible constituent-only entry point sharing this code.
  - **Documents / change detection:** document snapshots live in
    `document_snapshots`; ingestion hashes content
    (`app/services/documents.py:compute_document_hash`) and inserts a NEW snapshot
    when the hash changes (`change_status` new|changed, linking
    `previous_snapshot_id`/`previous_content_hash`). **Old snapshots are history —
    never delete or overwrite them.** The DB holds metadata + hashes only, not
    blobs; **no PDF text extraction / OCR** yet (a later worker).
  - **FX / currency conversion:** FX rates live in `fx_rates`
    (`rate` = quote units per 1 base unit); only canonical pairs are stored —
    inverse and cross rates are computed in the lookup service (`app/services/fx.py`:
    `get_fx_rate`/`convert_amount`, resolving direct → inverse → triangulated). A
    missing rate returns an explicit *missing* status, **never a silent 1**. The
    portfolio summary values positions in local currency then converts to the
    workspace base currency, carrying `fx_rate`/`fx_source`/`fx_status`; pence
    (GBX) is normalised to GBP. Keep this provenance — do not collapse to a single
    market value.
  - **Alerts / alert_generation:** alerts are **workspace-scoped** rows in
    `alerts` and are **derived data** — the `alert_generation` worker
    (`app/services/alert_generation.py`) turns existing diagnostics/freshness/
    change signals into them. Follow these rules:
    - **Alert rules must be deterministic and testable.** Keep rule logic pure
      (`app/services/alert_rules.py`): rules take a prepared `AlertContext` and
      return `AlertCandidate`s with **no DB/network I/O**. Reuse the existing
      diagnostics/freshness/FX/document-change services for signals — do not
      re-derive them ad hoc.
    - **Generation must be idempotent.** Upsert by `(workspace_id, dedupe_key)`;
      a re-run updates `last_seen_at`, never inserts a duplicate. Encode the
      issue's material identity in the `dedupe_key` (e.g. the content hash) so a
      genuinely new issue gets a new key/row.
    - **Alerts are workspace-scoped.** Always carry `workspace_id`; never leak one
      workspace's alerts into another. Generate per workspace; one workspace
      failing must not abort the others.
    - **Do not spam duplicate alerts.** Keep thresholds centralised
      (`PRICE_STALE_DAYS`, `HOLDINGS_STALE_DAYS`, `FAILED_JOB_LOOKBACK_DAYS`, …)
      and bound list responses.
    - **Preserve read/dismiss/resolve semantics.** An auto-resolvable issue that
      disappears → `resolved`; a returning issue reactivates a resolved alert; a
      **dismissed** alert with the same `dedupe_key` stays dismissed. One-time
      informational alerts (new document/distribution) are not auto-resolved.
    - **No notification delivery** (email/push) and **no user-configurable rule
      DSL** without an explicit request — generation produces DB rows only.
  - **Exposure / exposure_recompute:** exposure is **derived, cached, workspace-
    scoped** data (`exposure_snapshots` / `exposure_rows`) written by the
    `exposure_recompute` worker (`app/services/exposure_recompute.py`). Follow:
    - **Deterministic, no network.** Compute exposure purely from DB rows
      (positions, latest prices, FX, selected holdings snapshots). Reuse the
      existing `FxIndex` / `latest_holdings_by_fund` — do not duplicate valuation
      logic or fetch anything.
    - **No duplicate snapshots on unchanged inputs.** Idempotency is the
      deterministic `input_hash` over normalized inputs vs the latest snapshot;
      identical inputs ⇒ nothing written, changed inputs ⇒ a new snapshot (old
      ones kept as history). Unique on `(workspace_id, as_of_date, input_hash)`.
    - **Preserve source/provenance/status.** Carry `source` and per-row `status`
      (`ok`/`unclassified`/`missing_holdings`/`fx_missing`/`approximate`).
    - **Do not silently ignore missing holdings/FX.** Surface them as an
      `Unclassified` bucket + `coverage_weight`/`unclassified_weight` and
      `missing_holdings_count`/`missing_fx_count`; mark FX-missing positions
      rather than pretending a conversion happened.
    - **Avoid fund-only naming.** Keep the model generic (`dimension`/`bucket`/
      `label`/`lookthrough_weight`) so direct equities/bonds/cash/indices slot in
      later without a schema rewrite.
    - **Tests build their own input data** and never require network.
  - **True constituent look-through valuation** (`app/services/constituent_valuation.py`,
    folded into `exposure_recompute`). Adds a constituent price/FX-aware layer on
    top of the fund-level exposure. Rules (do not regress):
    - **Do not imply exact underlying share ownership.** The implied constituent
      value is **weight-based** (`position_market_value_base × holding_weight`),
      not a share×price notional — ETFs publish weights, not your share counts.
      The constituent EOD price + FX are *coverage/contribution context* only. Use
      a share/price-based value **only** if holdings carry exact `shares`/
      `market_value` *and* the `valuation_method` says so
      (`holding_shares_x_price` / `holding_market_value`); never overfit.
    - **Do not silently treat missing price/FX as zero.** Classify and surface it
      (`price_missing` / `fx_missing` / `missing_listing` / `unresolved_identity`),
      and emit a `constituent_price_status` funnel that sums to ~1.0.
    - **Do not break fund-level exposure.** The constituent layer is *additive* —
      keep the existing `fund`/`holding`/`country`/`sector`/`industry`/`currency`/
      `source` dimensions and `coverage_weight` unchanged.
    - **Coverage is weight-based and nested** (`holdings ≥ identity ≥ price ≥ fx`,
      as fractions of total portfolio value); counts are by **distinct resolved
      instrument** (deduped across funds — Apple via two ETFs counts once).
    - **Fold the new inputs into `input_hash`** (holding identity links,
      constituent prices used, their FX) so resolution/price changes create a new
      snapshot and reruns stay idempotent.
    - **Conservative, grouped alerts only.** No per-small-holding alerts; stay
      silent in the clean pre-resolution state (identity coverage 0 ⇒ planner
      signal, not an alert). Do **not** implement PnL attribution or total-return
      analytics here.
  - **Exposure drift** (`app/services/exposure_drift.py`): a read/compute layer
    that diffs two ``exposure_snapshots`` (default previous-vs-latest) — no table,
    no worker, no network. Rules (do not regress):
    - **Do not call exposure drift exact PnL.** It compares snapshots;
      ``delta_market_value_base`` is the change in the weight-based *implied*
      value, not realised cash PnL or total return. The constituent
      ``price_context_contribution`` is an explicit *price-context estimate*
      (label it so) — never PnL.
    - **Do not infer trades from exposure deltas.** A weight/value delta means the
      look-through estimate moved (rebalance, price, FX, holdings refresh) — never
      assert the user or the ETF bought/sold anything.
    - **Never compare snapshots from different workspaces.** Resolve explicit
      snapshot ids workspace-scoped (``get_snapshot`` 404s a foreign snapshot);
      match constituents by ``instrument_id`` (fallback ``bucket``).
    - **Insufficient history is not an error.** With <2 snapshots return
      ``status=insufficient_history`` (and quiet diagnostics/alerts), not a 500.
    - **No per-small-holding alert spam.** Drift alerts are grouped per
      workspace/dimension, threshold-gated (``EXPOSURE_DRIFT_WEIGHT_THRESHOLD`` /
      ``COVERAGE_DETERIORATION`` in ``alert_rules``), and auto-resolve when drift
      falls back below threshold.
  - **Top-holding performance** (`app/services/holding_performance.py`): a
    read/compute layer over cached snapshots + ``instrument_prices`` reporting a
    **price-context contribution estimate** (``base_implied_market_value_base ×
    local price_return``). Rules (do not regress):
    - **Do not call price-context contribution exact PnL.** It is an estimate, not
      realised PnL, total return or trade attribution. Label it
      ``price_context_contribution`` / ``price-context estimate``.
    - **Do not infer user trades or ETF rebalance causes** from a contribution or
      a price move.
    - **price_return is local-currency** this slice (same listing/currency at both
      endpoints). FX drift between dates is **not** applied — surface ``fx_rate_*``
      as context only; FX-adjusted return is deferred to the Rust GUI / local pricer.
    - **Do not build a heavy Python analytics loop.** Prefer SQL/window/``GROUP
      BY`` queries and **bounded** result sets (``instrument_prices.prices_asof_*``
      / ``prices_on_dates_for_listings``; cap the working set; default ``limit``
      conservative). Never load a listing's whole price history into Python.
    - **Data-quality diagnostics only, no price-move alerts.** Flag *why a
      contribution view is incomplete* (missing/stale prices, FX context); never
      alert because a constituent moved.
  - **Instrument onboarding / data-readiness** (`app/services/instrument_onboarding.py`,
    worker `instrument_onboarding`): an **orchestration** layer that takes a
    workspace/fund from "not ready" to "data-ready enough for charts/exposure/
    performance" by coordinating the existing workers
    (holdings → constituent_identity → constituent_prices → fx →
    exposure_recompute → alerts). Rules (do not regress):
    - **Orchestrate; never re-implement a worker.** Execution calls
      ``app.workers.run.run_job`` per stage (the same dispatch the CLI/scheduler
      use) — it must not copy a worker's internals. The plan is read-only and is
      driven by the **market-data planner** + current DB state, not hardcoded.
    - **Safe-by-default source mode.** Default is offline ``fixture``; ``live``
      must be explicit and still goes through each worker's source budget / fetch
      log / request cache. The seeded onboarding scheduled job is **manual**
      (never auto-runs).
    - **Plan-only writes nothing.** ``build_onboarding_plan`` /
      ``--plan-only`` perform no DB writes and no network I/O.
    - **Failure policy.** A hard blocker (e.g. no holdings) stops dependent
      stages and records ``partial_success``; a non-critical failure continues
      where safe. Each stage is individually skippable if already fresh/complete.
    - **Bounded.** Reuse the planner / readiness counts; do not add per-instrument
      Python loops, dataframe analytics, or a heavy valuation engine here.
    - **Readiness is data-quality / coverage, never investment quality.**
    - **Run history is a structured read model, not a free-text channel.** Each
      parent run persists typed stage rows (status / reason / timings / child
      `job_run` ids / counts) in ``job_runs.payload_json`` (migration ``0015``);
      ``app/services/onboarding_runs.py`` serves the bounded
      ``/onboarding/runs`` + ``/onboarding/runs/{id}`` read model over it. **Do
      not parse the human-readable ``message`` for core logic when structured
      metadata is present** — it is a log line, not the source of truth (the
      legacy ``<stage>=failed`` message parse is a fallback for pre-``0015`` runs
      only, marked ``legacy_metadata``). Keep the read model bounded
      (``job_type='instrument_onboarding'``, workspace/fund scope, latest-first,
      capped ``limit``); **do not turn onboarding observability into a workflow
      engine** (no DAG runtime, no stage retries/branching, no new business
      logic — observability only).
  - **Job-run timeline / failure drilldown** (`app/services/job_timeline.py`,
    schemas `app/schemas/job_timeline.py`): a **generic, bounded read model over
    all ``job_runs``** (timeline / detail / failures, global + workspace-scoped)
    for the GUI Data Operations page. Rules (do not regress):
    - **Do not parse free-text messages when typed payloads are available.**
      Orchestration runs expand into typed stages + child runs from
      ``job_runs.payload_json`` (the ``0015`` structured metadata), never the
      ``message``. ``message`` is a log line, not the source of truth.
    - **Do not turn job observability into a workflow engine.** It is read-only:
      no DAG runtime, no stage retries/branching, no execution. Recommended
      actions are **codes + labels only** — the GUI navigates; the endpoint never
      runs anything.
    - **Do not add heavy analytics to Python backend loops.** Bounded SQL only:
      latest-first, capped limits (timeline default 100 / max 500; fetch logs
      default 25 / max 100), indexed scope / ``(job_type, id)`` filters,
      correlation windows bound by the run's own span. No per-instrument loops,
      no dataframe analytics, no live calls.
    - **Keep queries bounded.** Never scan unbounded ``job_runs`` /
      ``source_fetch_logs``; always filter + cap.
    - **Source fetch-log correlation is approximate — say so.** There is no exact
      run↔fetch FK; associate by source + time window and label it
      ``fetch_log_correlation=time_window_source`` (or ``unavailable``). Never
      pretend it is exact (capabilities advertise it as ``partial``).
    - **Mask secrets in any observability response.** Every message / payload /
      fetch-log request key / endpoint label / error string is run through
      ``app/services/secret_masking.py`` (recursive for JSON) before leaving the
      API — defence-in-depth on top of the already secrets-free fetch-log layer.
      Enforce scope: a workspace-scoped run detail 404s a foreign run.
  - **Broker CSV import / transaction ledger / position reconciliation**
    (`app/services/broker_imports.py` + `app/sources/broker_imports.py`, worker
    `broker_csv_import`): the bridge from the market-data workstation to the
    *user portfolio* workstation. Parses a broker CSV (`generic_csv_v1`) into the
    canonical, workspace-private `portfolio_transactions` ledger + a bounded
    position reconciliation. Two-layer split holds: the parser adapter is pure
    (no DB / no network); the ingestion service does resolution + persistence +
    `job_runs` bookkeeping. Rules (do not regress):
    - **Do not infer identity from name-only imported rows.** Resolve against
      *existing* identity only, in priority order ISIN → FIGI → a **unique**
      ticker(+currency) (funds/listings, `security_identifiers`, the constituent
      instrument master). An ambiguous/unmatched row is stored with its
      symbol/ISIN and `status=unresolved_instrument` — **never** a guessed link,
      **never** an auto-created fund/instrument from a name.
    - **Do not call live resolvers during import.** No OpenFIGI / yfinance /
      Stooq from the *import* path. Unresolved imported symbols are resolved
      separately by the `imported_instrument_resolution` bridge (below) and are
      surfaced read-only by diagnostics + the market-data planner. Any live
      resolution must route through source budgets / fetch logs / request cache.
    - **Idempotency is mandatory.** `broker_imports` unique on
      `(workspace_id, source_hash)` (re-committing the same file is a duplicate
      no-op); `portfolio_transactions` unique on
      `(workspace_id, transaction_key, source)` (content hash — the same
      transaction shared by two files is stored once); position snapshots unique
      on `(workspace_id, as_of_date, input_hash)` like exposure snapshots.
      **Preview writes nothing.** A bad row is isolated + flagged, never crashing
      the import.
    - **Do not treat imported transactions as PnL.** Reconciliation is bounded
      SQL aggregation (`quantity = buys − sells` per instrument; signed cash flow
      per currency; fees/taxes totals) — **not** market value, realised/unrealised
      PnL, tax lots, IRR or total return. Those belong in the Rust GUI / local
      pricer; do not add a heavy Python portfolio engine here. Do **not** clobber
      the existing manual `portfolio_positions` CRUD table — reconciliation lives
      in `portfolio_position_snapshots` (derived, idempotent).
  - **Imported-instrument resolution bridge**
    (`app/services/imported_instrument_resolution.py`, worker
    `imported_instrument_resolution`, resolve API + dry-run): turns
    `status=unresolved_instrument` broker-import transactions into the canonical
    `instruments`/`instrument_listings` universe and relinks them, then
    re-reconciles the position snapshot. It is a *bridge*, **not a second identity
    system** — reuse, do not fork:
    - **Reuse the shared resolvers + upsert.** Build deduped `ConstituentRequest`s
      and resolve through the *same* `constituent_identity_fixture` / `openfigi`
      resolvers; upsert via `constituent_identity.upsert_candidate_instrument`
      (deduped on the same identity keys). Check *existing* identity first
      (`broker_imports.build_resolution_index`) so a symbol resolved since import
      links with no resolver call. Do **not** add a parallel instrument upsert.
    - **Do not resolve name-only imported rows.** Requests are ISIN → FIGI →
      ticker(+currency) only; a name-only row is `skipped_unsafe` and left for
      manual handling. OpenFIGI only receives ISIN/FIGI (a bare imported ticker has
      no exchange) — never name-only or bare-ticker, and only via the budget/
      fetch-log/cache guard. Offline fixture is the default; live is opt-in.
    - **Ambiguous → never linked.** A materially-ambiguous result sets
      `status=ambiguous_instrument` (parked for a human); `not_found`/`failed` stay
      `unresolved_instrument`. Do **not** choose arbitrarily between materially
      different candidates. Do **not** clobber a manual / already-`ready` link.
    - **Idempotent + bounded.** A rerun creates no duplicate instruments and
      relinks nothing; `dry_run=true` writes nothing. Resolution recomputes the
      bounded snapshot only — it is **not** PnL. Do not do heavy analytics in the
      backend loop.
  - **Manual transaction corrections** (`app/services/transaction_corrections.py`,
    endpoints under `…/transactions/{id}/manual-link|clear-link|ignore|manual-review`
    + `…/transactions/manual-review` and `…/transactions/{id}/correction-context`):
    the operator-driven cleanup layer for `unresolved_instrument` /
    `ambiguous_instrument` / mis-linked imported rows. Provenance is appended into
    the existing `raw_payload_json` (`manual_correction` + bounded
    `manual_correction_history`) — **no migration, no audit table**. A correction
    that changes the ledger/links re-reconciles the bounded position snapshot via the
    shared `broker_imports.write_position_snapshot` and *recommends* (never runs) a
    valuation recompute / price fetch. Status vocabulary reuses the existing one plus
    a new `manual_review`; `manual_review` is in `LEDGER_STATUSES` + `UNLINKED_STATUSES`
    (a flagged, parked row), `ignored` is in neither (drops out of reconciliation).
    Rules (do not regress):
    - **Do not guess name-only links.** Candidate context is built from identifiers
      (ISIN/FIGI) and *exact-ticker* matches only; a broker-supplied name is never a
      link, and `automatic_name_only_resolution` stays `unsupported` (a non-goal,
      never `planned`/`real`). A manual link only attaches *existing* identity.
    - **Do not call live resolvers / OpenFIGI / a price/FX source in manual
      correction endpoints.** All lookups are bounded SQL over already-stored rows;
      a manual link **never creates an instrument**.
    - **Do not delete canonical instruments/listings/funds when clearing a link.**
      Clearing only nulls the transaction's FK (reset to `unresolved_instrument`, or
      `manual_review`).
    - **Do not hide ignored / manual-review state** from diagnostics or the planner.
      Surface it (`manual_review_transactions` / `ignored_import_transactions` /
      `manual_linked_transactions`; planner `manual_review_imported_instrument`), and
      an `ignored` row must stop emitting urgent resolve items.
    - **Bounded + not PnL.** Candidate queries are capped; corrections store no
      realised/unrealised gain, tax lot or total-return field.
  - **Portfolio valuation / readiness snapshots** (`app/services/portfolio_valuation.py`,
    worker `portfolio_valuation_recompute`, tables `portfolio_valuation_snapshots` /
    `portfolio_valuation_rows`, migration `0019`): a **bounded, cacheable read model**
    one layer above the position reconciliation. It joins the reconciled positions
    (net quantity per instrument) + cash (per currency) to the **latest already-
    ingested** fund/instrument price + FX (at/before `as_of_date`) and reports a
    per-position market-value *context* in base currency plus a `valuation_status` /
    `readiness_status` and the `blocking_reasons`. It reuses the broker-import
    reconciliation (`committed_transactions` / `reconcile_transactions`) — it does not
    fork the position/cash aggregation. Rules (do not regress):
    - **Do not fetch market data inside `portfolio_valuation_recompute`.** It
      consumes already-ingested `prices` / `instrument_prices` / `fx_rates` only —
      no live price/FX source, ever.
    - **Do not call OpenFIGI / any identity resolver from valuation.** It values
      *already-resolved* links; unresolved/ambiguous rows stay blocked (the resolve
      backlog is the planner's / `imported_instrument_resolution`'s job).
    - **Do not compute PnL / tax lots / realised-unrealised gain / total return /
      performance attribution / dividend forecasting** here — it is market-value
      *context*, not analytics (those live in the Rust GUI / local pricer). Keep
      `portfolio_pnl` / `tax_lots` / `total_return` / `performance_attribution`
      **planned**; never mark them real.
    - **Do not hide missing price/FX or invent a value.** A row that cannot be
      valued safely reports the blocker (`missing_price` / `missing_fx` /
      `unresolved_instrument` / `ambiguous_instrument`); the planner emits the
      `fetch_*` / resolve / `recompute_portfolio_valuation` actions and diagnostics
      count coverage. GBX (pence) is normalised to GBP; a missing FX path is never a
      silent rate of 1.
    - **Idempotent + bounded.** `input_hash` keys on the reconciled positions/cash
      plus every price/FX used (unchanged inputs ⇒ no new snapshot; a new price/FX
      or (re)resolution ⇒ a new snapshot; old snapshots kept as history). Unique on
      `(workspace_id, as_of_date, input_hash)`; bounded by `limit`. Do **not** add a
      heavy Python valuation loop.
  - **Valuation history / summary / dashboard read models** (same
    `app/services/portfolio_valuation.py`: `get_portfolio_valuation_history`,
    `build_summary`, `build_dashboard_block`; endpoints
    `…/portfolio/valuation/history` & `…/summary`; dashboard `portfolio_valuation`
    block): **bounded, snapshot-backed read models** over the snapshots the worker
    already persisted. The history series is oldest-first (newest `limit`, max 500),
    each point carrying the snapshot's per-status counts, `total_market_value_base`
    (a coverage figure), a `valuation_coverage_ratio` and a snapshot-level
    `readiness_status` (`ready`/`partial`/`blocked`/`stale`/`empty`). Rules (do not
    regress):
    - **Do not compute returns from valuation history in the backend.** Never
      difference two snapshots (consecutive `total_market_value_base` values) into a
      return / percentage change / time-weighted return — that is the Rust GUI /
      local pricer's job.
    - **Do not label valuation deltas as PnL.** `total_market_value_base` is a
      coverage figure (sum of the *valued* rows), not a gain/loss; there is no
      cost-basis, realised/unrealised or performance field in any of these schemas.
    - **Do not recompute valuation inside dashboard / history / summary reads.**
      They read `portfolio_valuation_snapshots` only; the recompute worker (or the
      `POST …/recompute` endpoint) is the only writer. The dashboard block shows the
      latest snapshot or `status=missing` — it never values on the read path.
    - **Do not fetch market data from the history/summary endpoints.** No live
      price/FX source, no identity resolver — pure SQL reads over existing snapshots.
    - **Keep history bounded and snapshot-based.** `limit` is clamped (max 500) and
      the broker-account breakdown is a small distinct-then-latest scan; do not add a
      per-day backfill engine or an unbounded scan.
  - **Official / reference rates:** `rates_ingestion`
    (`app/services/rates_ingestion.py` + `app/sources/rates.py`, read shaping in
    `app/services/rates.py`) collects + persists official/reference rate
    *observations* into `reference_rates` (idempotent on
    `(rate_date, currency, country_or_region, rate_family, rate_name, tenor,
    source)`; NULL `tenor` for policy/overnight rates is matched via an explicit
    `IS NULL` in the upsert). Central-bank policy rates (ECB main refinancing /
    deposit / marginal lending, BoE Bank Rate), overnight benchmarks (€STR, SONIA,
    SOFR, Fed Funds effective) and government par yields (US Treasury 1M..30Y). It
    is real plumbing with an offline `rates_fixture` default plus two **live**
    explicit-only adapters via `guarded_fetch`: **US Treasury par yields**
    (`us_treasury_rates`, official `home.treasury.gov` XML feed) and **ECB**
    (`ecb_rates`, official ECB Data Portal SDMX API — `FM` key interest rates +
    `EST` €STR, `format=csvdata`, one bounded request per dataflow). `boe_rates`
    stays explicit + **planned**: the official series codes are known
    (`IUDBEDR`/`IUDSOIA`) but the BoE IADB CSV export returns HTTP 403 to a plain
    client, so a clean non-brittle access path must be verified first — see
    `docs/data_sources.md`.
    Rules (do not regress):
    - **Do not build curves in the backend.** No curve fitting, **no
      bootstrapping**, no interpolation/extrapolation, no discount-factor
      construction, no forward rates, no bond pricing, no scenario/rates pricer —
      anywhere. The backend stores official observations only; curve construction
      and analytics belong in the Rust GUI / local pricer. There is no curve /
      discount-factor / pricing table, and `yield_curves` stays a **planned** data
      type (distinct from the real `reference_rates`). **Do not mark yield curves
      real.**
    - **Do not interpolate or bootstrap in `rates_ingestion`.** Persist only the
      dates/tenors the source actually published; gaps stay gaps.
    - **Live adapters must be safe.** Fetch only official, machine-readable
      files/APIs through `guarded_fetch` (source budget → fetch log → request cache
      → timeout), one bounded request at a time (`us_treasury_rates` fetches one
      calendar year per call; `ecb_rates` one combined request per SDMX dataflow
      — `FM` + `EST`). **Do not guess ECB/BoE (or any) series keys — verify them
      against the official portal first; we do not guess.** **Do not add brittle
      HTML scraping for official rates. Do not use a third-party feed / aggregator /
      FRED as a canonical official-rate adapter — only the central bank / treasury's
      own machine-readable source. Do not bypass `guarded_fetch` / source budgets,
      and never store secrets.** A live source must stay **explicit-only** (the
      default is the offline fixture) — **do not make a live rates source the default
      without explicit approval**, so the worker/scheduler never makes a surprise
      call. Unverified providers fail cleanly (a planned `NotImplementedError`
      recorded as a failed run) rather than guessing an endpoint/series key — this is
      why `boe_rates` stays planned (IADB CSV export returns 403 to a plain client).
    - **Idempotent + bounded + isolated.** Re-running the fixture changes nothing
      (all skipped, no dupes); one bad observation is counted `failed`, never
      fatal; `--limit` bounds the run. Values are `Decimal`; keep `status`/
      provenance/`source_url`. The market-data planner emits
      `fetch_reference_rates` items for missing/stale supported currencies and
      diagnostics count coverage — neither builds or evaluates a curve.
  - **Source readiness + scheduler safety** (`app/sources/source_readiness.py`,
    `app/sources/stooq_market_series.py`, exposed at `GET /api/v1/data-sources/readiness`,
    `GET /api/v1/capabilities` `source_readiness`, and the diagnostics readiness counts): the
    operational‑honesty layer that says, per source, whether it can ingest real data on a VPS
    and is safe to schedule. Rules (do not regress):
    - **Do not mark a fixture as production‑ready.** A `*_fixture` source is `fixture`
      (dev/testing/smoke only) and is **never** `safe_for_scheduler`. Do not relabel it
      `implemented_live`/`verified_live`, and do not present a fixture default scheduled in
      production as live readiness (diagnostics split `scheduled_live_jobs` vs
      `fixture_scheduled_jobs` for exactly this reason).
    - **Do not mark a source `verified_live` without a recorded clean live fetch+parse**, and
      keep `last_verified_at` in lock‑step with `issuer_source_config` (no drift). Blocked
      sources stay `candidate` (carry the exact blocker); unimplemented stay `planned` (carry
      the next action).
    - **Do not schedule candidate/broken or planned sources by default.** `safe_for_scheduler`
      is true only for `verified_live` or an explicitly‑designed safe official source
      (budgeted, cached/logged, clean failures, no secret, no brittle scraping); a
      scheduler‑safe live source is still **explicit‑only** (the worker default stays the
      offline fixture — never a surprise live call).
    - **Do not treat Stooq sovereign benchmark series as actual bonds.** A `…Y.B`/`…P.B`
      sovereign yield/price symbol is a country/tenor generic `market_series`, NOT an
      ISIN‑level bond — never store it on the bond/instrument security master.
    - **Do not treat Stooq `.F` series as expiry‑specific futures unless verified.** `.F`
      symbols are roots/continuous series (`rates_futures_series`); an expiry contract
      (`ZNM6`) is not modelled (classifies `unknown`). Storage is deferred — implement the
      `market_series` table per the docs schema proposal, never a faked bond/future row.
    - **Do not use a ticker as identity** anywhere in this layer; example targets are public
      `ticker:ISIN` labels only (identity is the ISIN). **Do not bypass source budgets / fetch
      logs / request cache** for any live source. **Do not log IBKR / OpenFIGI / any API
      token** — the readiness matrix may *describe* that a token is required, but never embeds
      a secret value or a tokenised URL.
    - **IBKR is planned, not implemented this slice.** `ibkr_flex_import` (broker/account
      truth) is planned + HIGH‑PRIORITY; `ibkr_market_data` is planned + optional. When
      wiring Flex: idempotent, token never logged, feed the existing
      `broker_imports`/`portfolio_transactions` path, then trigger the resolve → price → FX →
      valuation cascade.
  - **Running / leased job observability** (`app/services/job_leases.py`,
    schemas in `app/schemas/job_timeline.py`): the **live counterpart** of the
    timeline (which covers *completed* ``job_runs``). It classifies the
    ``scheduled_jobs`` lease columns the scheduler maintains into a bounded,
    read-only view (running / stuck / expired / due / blocked-by-lease) for the
    GUI Data Operations page — *what is running now, what is leased but stuck,
    which lease expires soon and who owns it, which jobs are due but blocked*.
    Surfaced via ``GET /jobs/running``, ``GET /jobs/leases``, the workspace
    ``/jobs/running``, and ``/jobs/timeline?include_running=true`` (adds
    ``live_jobs`` + ``running_summary`` without changing the completed ``runs``
    list). Rules (do not regress):
    - **Do not add mutation endpoints for unlocking/killing jobs** (no
      unlock / force-unlock / kill / force-release / lease-repair) unless
      explicitly requested. This slice is read-only; recommended actions are
      **codes + labels only** (the GUI navigates — nothing is executed).
    - **Do not rewrite the scheduler for observability.** Classify the existing
      lease columns; never duplicate claim/release logic or add scheduler
      side-effects in a read path.
    - **Use the same lease classification helpers across ``/scheduler/status``,
      diagnostics, and the timeline.** ``classify_lease`` /
      ``lease_summary_counts`` are the single definition of running / stuck /
      expired / due / blocked — those surfaces must never disagree.
    - **Keep queries bounded** (leased-or-due rows only, capped ``limit`` default
      100 / max 500) and timezone-aware (SQLite round-trips naive datetimes).
    - **``scheduled_jobs`` are global infrastructure** (no ``workspace_id``); the
      workspace-scoped running view returns the same global scheduler health and
      validates the workspace exists — it does not invent a workspace scope.
- **Backend compute boundary (read before adding any analytics):**
  - **Do not move heavy analytics/pricing into Python backend loops.** Use the
    backend for orchestration, persistence, source budgets, fetch logs, the
    scheduler, diagnostics, and precomputed/cacheable DB snapshots.
  - **If backend compute is necessary, prefer Postgres-backed bounded queries or
    snapshots** (bounded SQL aggregation, materialised/idempotent cached results),
    not dataframe-style analytics or unbounded per-instrument computation.
  - **Interactive/heavy valuation belongs in the Rust GUI / local pricer**, not in
    FastAPI request handlers or worker loops.
  - **Do not call live sources in uncontrolled loops.** Dedupe + budget + cache
    via the market-data planner and ``guarded_fetch`` first.
  - **Do not bypass source budgets / fetch logs / request cache / job_runs /
    scheduler leasing / the market-data planner.**
  - **Tests must be offline. No secrets in logs.**
- **Model shape:** the schema is fund-centric (`funds` + `fund_listings`), which
  is correct for ETFs/funds. The generic instrument master (`instruments` /
  `instrument_listings` / `instrument_identifiers`) now exists **for ETF/fund
  constituents** (equities today; bond/future/option/index later) — it is a
  *complement*, not a replacement: funds/listings stay the home of ETF identity.
  **Do not migrate funds onto the instrument master or otherwise merge the two**
  without an explicit decision; only extend the instrument master as new asset
  classes (bonds/derivatives) need first-class support. See `docs/data_sources.md`.
- **GUI hydration endpoints (do not break):** the GUI is hydrated by a small set
  of *aggregate* read endpoints — the workspace `dashboard`, `diagnostics` and
  `hierarchy`; the `funds/{id}/detail`; and the `time-series` endpoints. Treat
  their response shapes as a contract (see the README "Client / GUI integration
  contract"). When you change them:
  - **Preserve source/status/provenance/freshness fields.** Records carry some
    of `source`, `status`, `as_of_date`, `last_refreshed_at`, `last_price_at`,
    `last_resolved_at`, `created_at`, `updated_at`, plus derived `freshness`
    (`fresh`/`stale`/`missing`). Don't drop them; add them where the DB supports
    them. Freshness is *derived at read time* (`app/services/freshness.py`), not
    stored.
  - **Keep aggregate endpoints bounded.** Never return unbounded history from
    `dashboard`/`detail`/`hierarchy`. They return latest/recent slices; deep
    history is fetched via the granular or `time-series` endpoints. New sections
    must be capped.
  - **Never fabricate data.** NAV/yield/derived series that have no backing table
    return `status="unavailable"`/`"derived"` with honest, marked values — never
    invented numbers passed off as real.
  - Expose **both** granular resource endpoints (debugging/specific views) and
    these aggregate endpoints (GUI hydration). Don't make the GUI do 15 calls for
    the initial view.
- **Security:** never expose Postgres publicly; never commit `.env` or secrets.
- **Prefer explicit, boring code** over clever abstractions. The domain is not
  stable yet — avoid premature generalisation.
- **Deployment (Docker/Compose — see `docs/operations.md`):**
  - **Do not commit real secrets.** Only `*.env.example` files are tracked;
    `.env`, `infra/.env`, `*.env` are git-ignored. `.env.example` files must
    contain placeholders only (blank `OPENFIGI_API_KEY=`, example passwords).
  - **Do not make live data sources default.** Compose/worker examples default
    to offline `*_fixture` sources; live sources stay explicit-only (`--source`,
    bounded by `--limit`, behind the budget + fetch log).
  - **Do not expose the raw API publicly.** The compose stack is private-network
    / private-VPS ready. The prod override binds the API to `127.0.0.1`; the VPS's
    **existing** Caddy proxies to it. Set a strong `MIMER_API_TOKEN` on any
    internet-reachable deployment (the `/api/v1` Bearer auth is the in-app guard);
    keep Postgres off any public port.
  - **Do not log `MIMER_API_TOKEN`.** Never print it, never write it to a fetch
    log / job message / diagnostics payload. It is only ever constant-time
    compared in `app/api/security.py`.
  - **Do not expose `MIMER_API_TOKEN` in diagnostics/capabilities** (or any API
    response). Capabilities/diagnostics build explicit field sets — keep the token
    out of them (mirror the `openfigi_api_key_configured: true/false` pattern if a
    flag is ever needed).
  - **Do not add reverse-proxy containers** (Caddy/nginx/Traefik compose files or
    service blocks). The VPS Caddy is **external** and managed outside this repo; a
    test (`test_no_reverse_proxy_assets_are_added`) enforces this. Docs may carry a
    minimal *existing*-Caddy `reverse_proxy 127.0.0.1:8080` snippet only.
  - **Do not deploy from this repo automatically.** No CI/CD deploy step, no
    `docker push`/`scp`/`ssh`-to-VPS automation wired into a command or test.
    Deployment is a deliberate, documented, human-run handoff
    (`docs/operations.md` §§ 2–2b).
  - **Do not auto-run destructive DB commands.** Migrations are an explicit step
    (the `migrate` service / `alembic upgrade head`); the API container does not
    migrate on start. Never wire a drop/reset into a container command.
  - **Keep the migration head aligned with tests.** `MIGRATION_HEAD` in
    `app/services/capabilities.py`, the chain in `tests/test_migrations.py`, the
    smoke test's expected head, and `docs/operations.md` all reference the same
    Alembic head — bump them together.
  - **Runtime image runs as non-root** (`appuser`) and writes only under `/app`.
    Keep healthchecks cheap (`/health` liveness, `/health/db` readiness) — never
    run migrations or diagnostics from a healthcheck.

## Before finishing any change

Run, from the repo root:

```bash
uv run ruff format .
uv run ruff check .
uv run pytest
```

All three must pass. If you change the ORM models, also update the Alembic
migration(s) and keep them consistent with `app/db/models.py`.
