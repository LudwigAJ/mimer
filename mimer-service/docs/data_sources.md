# Data sources & adapter strategy

This document is the **engineering catalogue** for ingestion: what data types and
asset classes the backend supports, which adapters are implemented vs stubbed vs
planned, how sources are ranked, and how provenance/freshness are preserved.

It complements two siblings rather than duplicating them:

- **[`../SOURCES.md`](../SOURCES.md)** — the detailed *vendor research* (URLs,
  pricing, auth, rate limits, licensing caveats; checked 2026‑06‑20). Read it to
  pick a real provider for a given slice.
- **`app/sources/registry.py`** — the *programmatic* capability registry that this
  document narrates; it is exposed read‑only at
  `GET /api/v1/data-sources/capabilities`.

> The goal is **not** to pick one final vendor. The goal is an architecture that
> can support multiple sources safely, one adapter at a time.

## Architecture: four layers

The backend deliberately separates concerns so new sources slot in without ad‑hoc
code in request handlers:

1. **Source adapters** (`app/sources/…`) — small, provider‑specific modules that
   know how to *fetch/parse* one source. They return normalized dataclasses and
   nothing else (no DB, no job bookkeeping). Each is registered behind a protocol
   (`PriceSource`, `IssuerFactsSource`, `DistributionSource`, the resolver
   providers, …) and selected via a `get_*_source(name)` registry.
2. **Ingestion services/workers** (`app/services/*_ingestion.py`, `app/workers/run.py`)
   — provider‑*agnostic* job logic: call an adapter, validate output, upsert
   canonical tables idempotently, record `source` provenance, and write a
   `job_runs` row (status + `records_inserted/updated/failed`).
3. **Canonical read APIs** (`app/api/v1/…`) — stable GUI/client endpoints that do
   not expose provider quirks. The aggregate endpoints (`dashboard`, `detail`,
   `time-series`, `hierarchy`, `diagnostics`) are a contract.
4. **Source/provenance metadata** — every material fact carries a `source`; most
   carry a `status` and a timestamp; a coarse `freshness` (`fresh`/`stale`/
   `missing`) is *derived at read time* (`app/services/freshness.py`).

```
adapter (fetch/parse)  ->  ingestion service (upsert + provenance + job_runs)
                                   |
                                   v
                       canonical tables (Postgres)
                                   |
                                   v
                  canonical read API  ->  GUI / clients
```

## Implementation status (this iteration)

| Data type | Canonical table | Worker / path | Status | Default adapter |
|---|---|---|---|---|
| identity / crosswalk | `security_identifiers`, `funds`, `fund_listings` | `POST /api/v1/instruments` (resolver) | **real** | `stub` (offline) / `openfigi` |
| constituent identity | `instruments`, `instrument_listings`, `instrument_identifiers` | `constituent_identity_resolution` | **real, fixture default (OpenFIGI optional)** | `constituent_identity_fixture` / `openfigi` |
| prices (fund listings) | `prices` | `price_ingestion` | **real** | `stooq` (+`yfinance`) |
| prices (constituents / EOD) | `instrument_prices` | `constituent_eod_price_ingestion` | **real, fixture default (Stooq/yfinance optional)** | `instrument_price_fixture` / `stooq` / `yfinance` |
| prices (any instrument / EOD) | `instrument_prices` | `instrument_eod_price_ingestion` (unified: constituents + resolved imported direct holdings) | **real, fixture default (Stooq/yfinance optional)** | `instrument_price_fixture` / `stooq` / `yfinance` |
| fund facts | `funds` | `issuer_facts_ingestion` | **real, fixture provider** | `issuer_fixture` |
| distributions | `distributions` | `distribution_ingestion` | **real, fixture default + live issuer adapters** | `distribution_fixture` / `jpmorgan_distributions` / `vanguard_distributions` / `vanguard_distributions_export` |
| holdings | `fund_holdings` | `issuer_holdings_ingestion` | **real, fixture default + live issuer adapters** | `holdings_fixture` / `blackrock_ishares_holdings` / `jpmorgan_etf_holdings` / `vanguard_holdings_export` |
| fx rates | `fx_rates` | `fx_ingestion` | **real, fixture provider** | `fx_fixture` |
| documents | `document_snapshots` | `document_snapshot_ingestion` | **real, fixture provider** | `document_fixture` |
| alerts | `alerts` | `alert_generation` | **real (database‑only)** | _(none — derived)_ |
| exposure | `exposure_snapshots`, `exposure_rows` | `exposure_recompute` | **real (database‑only)** | _(none — derived)_ |
| onboarding / data‑readiness | _(orchestration; `job_runs`)_ | `instrument_onboarding` | **real (orchestration; fixture default)** | _(coordinates existing workers)_ |
| nav / premium‑discount | _(none yet)_ | — | **planned** | — |
| corporate actions | _(none yet)_ | — | **planned** | — |
| transactions / broker import | `broker_imports`, `portfolio_transactions` | `broker_csv_import` | **real (offline; `generic_csv_v1`)** | `generic_csv_v1` (CSV text/file) |
| imported‑instrument resolution | `instruments`, `instrument_listings`, `portfolio_transactions` (relink) | `imported_instrument_resolution` | **real, fixture default (OpenFIGI optional)** | `constituent_identity_fixture` / `openfigi` |
| manual transaction corrections | `portfolio_transactions` (relink/status + `raw_payload_json` provenance) | _(operator API: manual‑link / clear‑link / ignore / manual‑review)_ | **real (database‑only; existing identity only — no resolver/OpenFIGI/live fetch, no name‑only link, no instrument creation)** | _(none — manual)_ |
| position reconciliation | `portfolio_position_snapshots` | _(derived on commit + `…/positions`)_ | **real (bounded; quantities + cash, not PnL)** | _(none — derived)_ |
| portfolio valuation / readiness | `portfolio_valuation_snapshots`, `portfolio_valuation_rows` | `portfolio_valuation_recompute` | **real (bounded read model; market‑value context, not PnL)** | _(none — derived from already‑ingested prices/FX)_ |
| portfolio PnL / tax lots / total return / performance attribution | _(none — Rust GUI / local pricer, never the backend)_ | — | **planned** | — |
| reference rates (observations) | `reference_rates` | `rates_ingestion` | **real, fixture default + live US Treasury & ECB (collection only — no curves)** | `rates_fixture` (default) / `us_treasury_rates`·`ecb_rates` (live, explicit) / `boe_rates` (planned) |
| yield curves (fitting / bootstrap / discount factors / forwards) | _(none — Rust local pricer, never the backend)_ | — | **planned** | — |
| bond reference / prices | _(none yet)_ | — | **planned** | — |
| option chains | _(none yet)_ | — | **planned** | — |
| futures contracts | _(none yet)_ | — | **planned** | — |
| scheduling / job leasing | `scheduled_jobs` | `app/workers/scheduler.py` | **real** | _(in‑process)_ |
| source rate‑budgets | `source_rate_limits` | `app/services/source_budget.py` | **real** | _(per‑source)_ |
| fetch logs / request cache | `source_fetch_logs` | `app/services/source_requests.py` | **real** | _(generic)_ |
| market‑data planning | _(computed)_ | `app/services/market_data_planner.py` | **real** | _(read‑only)_ |
| job‑run timeline / failure drilldown | _(read model over `job_runs` + `source_fetch_logs`)_ | `app/services/job_timeline.py` | **real (read model)** | _(secrets‑masked; fetch‑log correlation `partial`)_ |
| running / leased job observability | _(read model over `scheduled_jobs` leases)_ | `app/services/job_leases.py` | **real (read model)** | _(read‑only; no unlock/kill; classifier shared with scheduler/status + diagnostics)_ |

- **real** — provider‑agnostic worker with a live‑capable adapter.
- **fixture** — real worker, but the shipped adapter is **offline** (no network,
  no key) so the system works out of the box and tests stay deterministic. Real
  per‑provider adapters slot in behind the same protocol later.
- **stub** — recognised job type that only records a `success_stub` run.
- **planned** — named in the roadmap; no worker yet.

The machine‑readable version of this table is `GET /api/v1/capabilities`
(`features`, `workers`, `data_types`, `configured_sources`).

## Production data-source readiness (VPS)

The status table above answers *what a source can provide*. This section answers the
**operational** question for a real VPS: *can this source actually ingest live data, and is
it safe to put on the scheduler — or is it fixture‑only, blocked, or not implemented?* That
truth is curated, honestly, in one in‑code matrix (`app/sources/source_readiness.py`),
exposed read‑only at **`GET /api/v1/data-sources/readiness`** and summarised in
`GET /api/v1/capabilities` (`source_readiness`) and `GET /api/v1/diagnostics`
(`verified_live_sources` / `candidate_live_sources` / `scheduler_safe_sources` /
`scheduled_live_jobs` / `fixture_scheduled_jobs` / `missing_required_live_sources` / …).

**Status taxonomy** (worst‑to‑best honesty; deliberately conservative — we never inflate):

| status | meaning |
| --- | --- |
| `fixture` | offline deterministic provider; **dev/testing/smoke only**, NOT a VPS production source |
| `implemented_live` | adapter exists and works with an explicit command/config, but a clean live fetch+parse has not been *recorded* in this environment (also covers offline manual export parsers) |
| `verified_live` | a live fetch+parse succeeded for ≥1 known target; safe to recommend for scheduled use subject to its source budget |
| `candidate` | plausible source whose endpoint shape is known but which is **blocked** or not yet verified (carries the exact blocker) |
| `planned` | not implemented yet (carries the exact next action) |
| `unsupported` | intentionally not supported (a non‑goal, never `planned`) |

**Scheduler‑safe vs explicit‑only.** A source is `safe_for_scheduler=true` only when it is
`verified_live` **or** an explicitly‑designed safe official source, and is budgeted,
cached/logged, has clean failure modes, leaks no secret, and is not brittle browser
scraping. Everything `fixture` / `candidate` / `planned` is **never** scheduler‑safe.
Naming a live `--source` explicitly is itself the opt‑in — a scheduler‑safe live source is
still **explicit‑only** (the configured worker default always stays the offline fixture, so
the scheduler never makes a *surprise* live call); scheduling it means adding an explicit
`scheduled_jobs` row that names `--source`, never flipping the default.

**The matrix (this slice).**

| data_type | source | provider | status | sched‑safe | secret | url cfg | gateway | worker | cadence | blocker / next action |
| --- | --- | --- | --- | :--: | :--: | :--: | :--: | --- | --- | --- |
| reference_rates | `rates_fixture` | (offline) | fixture | – | – | – | – | rates_ingestion | dev only | default; dev/demo only |
| reference_rates | `us_treasury_rates` | US Treasury | implemented_live | ✅ | – | – | – | rates_ingestion | daily (early UTC) | official XML feed; bounded‑verify then add explicit job |
| reference_rates | `ecb_rates` | ECB SDMX | implemented_live | ✅ | – | – | – | rates_ingestion | business daily | official SDMX API; explicit‑only |
| reference_rates | `boe_rates` | Bank of England | planned | – | – | – | – | rates_ingestion | daily (later) | IADB CSV export 403s a plain client; verify clean path (IUDBEDR/IUDSOIA) |
| fx_rates | `fx_fixture` | (offline) | fixture | – | – | – | – | fx_ingestion | dev only | default; **FX is still fixture‑only** |
| fx_rates | `ecb` | ECB EUR ref | planned | – | – | – | – | fx_ingestion | daily ~16:00 CET | no live ECB FX adapter implemented yet |
| prices | `stooq` | Stooq (free EOD) | implemented_live | ✅ | – | – | – | price_ingestion / instrument_eod_price_ingestion | daily EOD | free/non‑contractual/fragile; default fund‑price source |
| prices | `yfinance` | Yahoo (unofficial) | implemented_live | – | – | – | – | price_ingestion / instrument_eod_price_ingestion | daily EOD (fallback) | unofficial; explicit‑only fallback |
| prices | `instrument_price_fixture` | (offline) | fixture | – | – | – | – | instrument_eod_price_ingestion | dev only | default constituent/imported price source; dev/demo only |
| holdings | `holdings_fixture` | (offline) | fixture | – | – | – | – | issuer_holdings_ingestion | dev only | default; dev/demo only |
| holdings | `blackrock_ishares_holdings` | iShares/BlackRock | **verified_live** | ✅ | – | ✅ | – | issuer_holdings_ingestion | daily/weekly | verified for ISF (2026‑06‑25); explicit‑only |
| holdings | `jpmorgan_etf_holdings` | J.P. Morgan AM | candidate | – | – | ✅ | – | issuer_holdings_ingestion | blocked | JEPG returns legacy binary `.xls` → `binary_unsupported`; verify CSV/HTML/`.xlsx` variant |
| holdings | `vanguard_holdings_export` | Vanguard (manual) | implemented_live | – | – | ✅ | – | issuer_holdings_ingestion | manual | offline export parser (no live fetch) |
| holdings | `vanguard_holdings` | Vanguard | planned | – | – | ✅ | – | issuer_holdings_ingestion | planned | no stable official endpoint verified; do NOT scrape HTML |
| distributions | `distribution_fixture` | (offline) | fixture | – | – | – | – | distribution_ingestion | dev only | default; dev/demo only |
| distributions | `jpmorgan_distributions` | J.P. Morgan AM | implemented_live | – | – | ✅ | – | distribution_ingestion | not scheduled | no verified per‑product URL yet (don’t assume from holdings) |
| distributions | `vanguard_distributions` | Vanguard | candidate | – | – | ✅ | – | distribution_ingestion | blocked | VUSA TLS handshake rejected (`SSLV3_ALERT_HANDSHAKE_FAILURE`) |
| distributions | `vanguard_distributions_export` | Vanguard (manual) | implemented_live | – | – | ✅ | – | distribution_ingestion | manual | offline export parser |
| distributions | `blackrock_ishares_distributions` | iShares/BlackRock | planned | – | – | ✅ | – | distribution_ingestion | planned | no clean official endpoint; NEVER guess from holdings ajax URL |
| identity | `constituent_identity_fixture` | (offline) | fixture | – | – | – | – | constituent_identity_resolution / imported_instrument_resolution | dev only | default; dev/demo only |
| identity | `openfigi` | OpenFIGI | implemented_live | ✅ | – | – | – | constituent_identity_resolution / imported_instrument_resolution | after holdings/imports; or daily unresolved queue (strict budget) | batched; key optional, never logged; never name‑only |
| transactions | `broker_csv` | generic CSV | implemented_live | – | – | – | – | broker_csv_import | on upload | production‑ready, file‑driven (no remote endpoint to schedule) |
| transactions | `ibkr_flex_import` | Interactive Brokers | **planned (high‑priority)** | – | ✅ | – | – | — | daily (when impl) | broker/account truth; needs Flex token + query id |
| prices | `ibkr_market_data` | Interactive Brokers | planned (optional) | – | ✅ | – | ✅ | — | not default | entitlement/session dependent; needs TWS/IB Gateway |
| market_series | `stooq_market_series` | Stooq | planned | – | – | – | – | — | daily EOD (when impl) | no `market_series` table yet (storage deferred); classification‑only |

(`secret` = `requires_secret`, `url cfg` = `requires_url_config`, `gateway` =
`requires_running_gateway`.)

### Verifying a live source (bounded, explicit)

Each live adapter is verifiable with a small, bounded run that goes through
`guarded_fetch` (budget → cache → fetch log → fetch). **Never run a broad live sweep**, and
**never** schedule a `candidate`/`planned` source.

```bash
# Reference rates (official, scheduler‑safe once verified):
uv run python -m app.workers.run rates_ingestion --source us_treasury_rates --limit 10
uv run python -m app.workers.run rates_ingestion --source ecb_rates --limit 10
# Fund-listing + instrument prices (free EOD; budgeted):
uv run python -m app.workers.run price_ingestion --source stooq --fund-id <VUSA_ID> --limit 10
uv run python -m app.workers.run instrument_eod_price_ingestion --source stooq --workspace-id 1 --limit 10
# Issuer holdings (verified for ISF; --verify-source ingests nothing):
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id <ISF_ID> --source blackrock_ishares_holdings --verify-source
# Identity (batched; strict budget):
uv run python -m app.workers.run constituent_identity_resolution --source openfigi --limit 25
```

### The worker cascade (consequence chains)

The market‑data planner (`/workspaces/{id}/market-data/plan`) + the
`instrument_onboarding` orchestrator make these chains **visible and runnable** without a
queue engine — each plan item names the `source_candidates` that would run it.

**New/updated ETF or fund:**

```
fetch_listing_price → refresh_holdings → resolve_constituent_identity →
fetch_constituent_price → fetch_fx_rate → exposure_recompute →
recompute_portfolio_valuation → alert_generation
```

**Broker import:**

```
broker_csv_import → imported_instrument_resolution (manual corrections for
unresolved/ambiguous) → fetch_imported_instrument_price → fetch_imported_fx_rate →
position reconciliation → recompute_portfolio_valuation → diagnostics/alerts
```

`manual_review` rows surface as a non‑urgent `manual_review_imported_instrument` plan item;
`ignored` rows stop emitting any resolve/price/FX work (but stay visible in diagnostics).

### Recommended scheduler cadences (scheduler‑safe sources only)

| chain step | worker | cadence |
| --- | --- | --- |
| reference rates (USD) | `rates_ingestion --source us_treasury_rates` | daily, early UTC |
| reference rates (EUR) | `rates_ingestion --source ecb_rates` | business daily |
| fund‑listing prices | `price_ingestion` (`stooq`) | daily EOD |
| instrument/constituent prices | `instrument_eod_price_ingestion` (explicit live) | daily EOD |
| issuer holdings (ISF) | `issuer_holdings_ingestion --source blackrock_ishares_holdings` | daily or weekly |
| identity resolution | `constituent_identity_resolution` | after new holdings/imports, or daily unresolved queue (strict budget) |
| exposure recompute | `exposure_recompute` | after holdings/prices/FX, or daily |
| portfolio valuation | `portfolio_valuation_recompute` | after prices/FX/imports, or daily |
| alerts | `alert_generation` | after recomputes, or daily |

**Do not schedule** candidate/broken live sources (JPM holdings, Vanguard live
distributions, iShares distributions) or fixture sources as production defaults. The seed
`scheduled_jobs` are **dev schedules** seeded one interval out (not due) and default to the
offline fixture for every issuer/fx/document worker; the one exception is
`daily_price_ingestion`, whose configured default (`stooq`) is a live free EOD source
(budgeted/logged). Diagnostics count `scheduled_live_jobs` vs `fixture_scheduled_jobs` so a
fixture scheduled in production is never mistaken for live readiness.

### Stooq market series (generic benchmark/futures — NOT securities)

Stooq also publishes generic **market series** that are useful curve/market context but are
**not** tradable securities. `app/sources/stooq_market_series.py` classifies a symbol into:

- **`sovereign_yield_benchmark_series`** — e.g. `10YDEY.B` (Germany 10Y benchmark *yield*),
  `1MFRY.B` (France 1M). A country/tenor generic yield series, **NOT an ISIN‑level bond**.
- **`sovereign_benchmark_price_series`** — e.g. `10YDEP.B` (Germany 10Y benchmark *price*),
  `2YITP.B` (Italy 2Y). A country/tenor generic price series, **NOT an ISIN‑level bond**.
- **`rates_futures_series`** — e.g. `ZN.F` (10Y T‑Note), `ZB.F` (30Y T‑Bond), `GG.F` (Euro
  Bund), `G.F` (Long Gilt). A root/continuous/generic futures series, **NOT an
  expiry‑specific contract** (an expiry contract looks like `ZNM6` = root + month + year,
  which the `.F` roots are not — `ZNM6` classifies as `unknown`, never a tradable future).

**Critical modelling rules** (enforced by `tests/test_stooq_market_series.py`): never store
a sovereign benchmark yield/price series as an actual bond holding; never store a `.F`
series as an expiry‑specific contract; store these as `market_series` / the specific
benchmark category, **never on the bond/instrument security master**.

**Storage is deliberately deferred** (a new table + migration would be a large slice on its
own). Concrete schema proposal for when it lands:

```
market_series            (id, symbol, source, category, country_or_region, tenor,
                          tenor_months, unit, currency, description, status,
                          created_at, updated_at)   # one row per series
market_series_points     (id, market_series_id, observation_date, value, status, source)
                          # unique (market_series_id, observation_date, source)
```

Ingestion would reuse the same pattern as `reference_rates`: a generic `market_series`
worker, the Stooq CSV adapter behind `guarded_fetch` (budget/cache/log), `Decimal` values,
idempotent upserts, configured symbol lists — collection only, no curve building.

### Interactive Brokers (IBKR) — planned

Classified as two **separate** sources (do not conflate):

- **`ibkr_flex_import`** (broker/account truth, **planned, HIGH‑PRIORITY**): the Flex Web
  Service delivering positions, trades, cash, dividends, fees, FX conversions and corporate
  actions. Design constraints: requires a Flex **token + query id** (**must never** be
  logged / written to a fetch log / job message / diagnostics); must be **idempotent**;
  should feed the existing `broker_imports` / `portfolio_transactions` path (not a parallel
  ledger), then trigger the resolve → price → FX → valuation cascade. A scheduled daily
  import is the intended end state. Not implemented in this slice.
- **`ibkr_market_data`** (optional market data, **planned**): entitlement / session /
  subscription dependent; needs a running TWS / IB Gateway session. Not a default source;
  kept distinct from the Flex import.

## Safe fetching: scheduler, budgets, fetch logs, planner

Before broad stock/constituent ingestion, the service ships the operational
layer that keeps external fetching safe. **Why this matters:** an ETF can hold
hundreds of stocks, so naively resolving identifiers or pulling EOD prices once
per holding would hammer OpenFIGI / yfinance / Stooq / issuer sites. That is
forbidden (see [`AGENTS.md`](../AGENTS.md)).

- **Scheduler + job leasing** (`app/workers/scheduler.py`): an in‑process worker
  claims **due** `scheduled_jobs`, leases them with an atomic conditional
  `UPDATE` (so only one of N processes runs a job; crashed leases expire and are
  reclaimable), and runs the existing `run_job` directly — no cron/queue/broker.
- **Source rate‑budgets** (`source_rate_limits` + `app/services/source_budget.py`):
  per‑source *may‑I‑fetch‑now?* with min‑delay, rolling windows, backoff and
  batch size. Fixtures permissive; `openfigi`/`yfinance`/`stooq` conservative.
- **Fetch logs / request cache** (`source_fetch_logs` +
  `app/services/source_requests.py`): every external attempt is logged under a
  deterministic, **secrets‑free** request key; a recent identical success is a
  cache hit, so identical requests are not repeated. No API keys / auth headers /
  tokenised URLs are ever stored.
- **`guarded_fetch(...)`** is the single entry point a live adapter should use
  (cache → budget → log → fetch). The OpenFIGI resolver already goes through it.
- **Market‑data planner** (`app/services/market_data_planner.py`): a read‑only,
  **deduped, prioritised** plan of what to resolve/fetch (one identity item per
  constituent even if held via several funds), with estimated request cost per
  source — the gate before constituent EOD ingestion.

**Why constituent EOD prices are not pulled naively:** the planner first
collapses the union of all funds' holdings into unique constituents, ranks them
(held positions and top weights first), and estimates the per‑source request
budget. The **`constituent_identity_resolution`** worker consumes that plan's
unresolved identity items in budgeted batches behind `guarded_fetch` — never in a
per‑holding loop — resolving each constituent into the canonical instrument master
(`instruments` / `instrument_listings` / `instrument_identifiers`). The
**`constituent_eod_price_ingestion`** worker then consumes the plan's
`fetch_constituent_price` items the same budgeted way: it dedupes the resolved
listings (Apple priced once even if held via several funds), fetches them one
symbol at a time behind `guarded_fetch` (offline `instrument_price_fixture` by
default; live Stooq/yfinance only when explicitly requested, with `--limit`), and
upserts bars into `instrument_prices`. Future official rates / yield curves and
bonds expansion follows the same pattern.

**Unified instrument prices (constituent *and* imported direct holdings):** the
**`instrument_eod_price_ingestion`** worker generalises this to *any* canonical
`instrument_listing`. It consumes both the plan's `fetch_constituent_price` and
`fetch_imported_instrument_price` items, unioning resolved constituents with the
resolved directly-held imported broker holdings that
`imported_instrument_resolution` linked to a listing — deduped, so an instrument
held both ways is priced once. There is **no separate price table for imported
holdings**: once a broker transaction (e.g. a TSLA buy) resolves to a listing it
prices through the same selector + idempotent upsert into `instrument_prices`, and
becomes chartable via `/instrument-listings/{id}/prices` and `…/time-series` like
any other listing. Same sources, budgets, fetch logs and idempotency/freshness
rules as the constituent worker (skip fresh unless `--force`; skip no-ticker
listings); `constituent_eod_price_ingestion` stays a constituent-only entry point
sharing the code. **Deferred** (not here, not in the price worker): PnL, tax lots,
total return, corporate actions, and exact broker cash balances — those live in
the Rust GUI / local pricer.

**Manual transaction corrections** (`app/services/transaction_corrections.py`):
the operator cleanup layer for imported rows the automatic bridge cannot safely
resolve (`unresolved_instrument` / `ambiguous_instrument`) or linked wrongly.
Endpoints (workspace‑scoped, behind the `/api/v1` bearer auth) **manual‑link** a
row to *existing* `instrument`/`instrument_listing`/`fund`/`fund_listing`,
**clear** a mistaken link (reset to `unresolved_instrument` or `manual_review`),
**ignore** a non‑portfolio row, or **manual‑review** (park) it; a bounded
**correction‑context** read returns the safe auto‑resolution plus identifier
(ISIN/FIGI) and exact‑ticker candidates to choose from. Rules: a manual link
attaches existing identity only — it **never creates an instrument, never calls a
resolver/OpenFIGI/a live price/FX source, and never name‑only guesses**
(`automatic_name_only_resolution` is `unsupported`); clearing a link **never
deletes** the canonical instrument/listing/fund; provenance is appended into
`raw_payload_json` (`manual_correction` + bounded history), so no migration is
needed. A correction re‑reconciles the bounded position snapshot and *recommends*
(never runs) a valuation recompute / price fetch. `ignored` / `manual_review`
state stays visible in diagnostics and the planner (an `ignored` row stops
emitting urgent resolve items; a `manual_review` row surfaces as the non‑urgent
`manual_review_imported_instrument`). Not PnL — no gain/tax‑lot/total‑return field.

**Constituent identity resolution** (`app/services/constituent_identity.py` +
`app/sources/constituents.py`): two resolvers behind one protocol — an offline
deterministic `constituent_identity_fixture` (default; ISIN + normalised‑name
keyed, with a few ambiguous/not‑found/failing cases) and live `openfigi` batches
(≤10 jobs/request, all through `guarded_fetch`; key in the header only). Identity
dedupes on a deterministic `instrument.identity_key` (ISIN > share‑class FIGI >
composite FIGI > FIGI > normalised name+country+currency). Rules: requests are
deduped across funds; name‑only is never sent to OpenFIGI; an ISIN that maps to
many venues of the *same* security (shared share‑class FIGI) collapses to one
instrument with listings, while genuinely different securities stay **ambiguous**
and are never linked; re‑runs are idempotent; a `manual` instrument/link is never
clobbered. `fund_holdings.identity_status` makes unresolved/resolved/ambiguous/
not_found/failed state visible without re‑resolving.

## Source types, asset classes, data types

These controlled vocabularies are defined once in `app/sources/registry.py`:

- **Source types:** `identifier`, `issuer`, `market_data`, `fx`, `broker`,
  `manual`, `derived`, `seed`.
- **Asset classes:** `etf`, `mutual_fund`, `equity`, `bond`, `future`, `option`,
  `fx`, `cash`, `index`, `crypto`, `commodity`.
- **Data types:** `identity`, `fund_facts`, `holdings`, `distributions`, `prices`,
  `nav`, `fx_rates`, `documents`, `corporate_actions`, `option_chain`,
  `futures_contracts`, `bond_reference`, `bond_prices`, `yield_curves`,
  `transactions`.

## Source priority & provenance

Two complementary mechanisms rank sources:

- **Per‑fact `source` column** — every fact row records where it came from.
- **`data_sources` table** — a per‑source `priority` (lower = higher priority) and
  `is_active` flag, exposed at `GET /api/v1/data-sources`.

The convention used by ingestion (e.g. `issuer_facts_ingestion`): **`manual`
outranks an issuer, an issuer outranks `seed`/automated, empty fields are always
filled.** A worker must never silently clobber a higher‑priority source. Distinct
sources may legitimately coexist for the same logical fact (e.g. a `seed` and a
`distribution_fixture` distribution on different ex‑dates, or two price sources on
the same date) — provenance differs, so both are kept and surfaced.

**Free/convenience vs paid/reliable.** Free public sources (Stooq, Yahoo, issuer
pages) are fine for personal/internal EOD and prototyping but are fragile,
delayed, and often non‑contractual. Reliable bond/option/futures reference and
pricing, and broad iNAV, are generally **paid**. The capability registry tags each
source with a `reliability_tier` (`fixture` < `official` < `free` < `freemium` <
`paid` < `manual`/`derived`) so the GUI and operators can see the trade‑off. See
`SOURCES.md` for the per‑vendor detail.

## Catalogue by data type

The notes below summarise the engineering view; `SOURCES.md` has the verified URLs
and caveats for each named provider.

### A. Identifier / security master / crosswalk — *real*
Map ticker/ISIN/CUSIP/SEDOL/FIGI + exchange/MIC + currency to a canonical
instrument so identity is never ticker‑only. **Sources:** OpenFIGI (implemented),
offline `stub` (implemented); broker CSV, exchange reference, paid security‑master
vendors later. Stored in `security_identifiers` with source/confidence/raw
payload. Future: richer MIC handling, confidence scoring, ambiguity‑resolution UI.

### B. ETF / fund issuer facts — *real (fixture provider)*
Name, provider, domicile, base currency, share class, distribution policy,
benchmark, strategy, OCF/TER, AUM, replication, holdings count, factsheet date.
**Sources:** `issuer_fixture` (implemented); Vanguard, iShares/BlackRock, JPMAM,
SPDR/SSGA, Invesco, Amundi, Xtrackers/DWS, WisdomTree, VanEck, LGIM, UBS AM,
FMP fund info later. Future: one issuer live adapter at a time, PDF/factsheet
parsing, document hash/version tracking.

### C. ETF holdings / constituents — *real (fixture default + live issuer adapters)*
Look‑through exposure, top holdings, country/sector/currency, snapshot diffs.
**Sources:** `holdings_fixture` (implemented, offline, **default**);
`blackrock_ishares_holdings` + `jpmorgan_etf_holdings` (implemented **live**,
explicit‑only); `vanguard_holdings_export` (implemented, offline exported‑file
parser); `vanguard_holdings` live + FMP holdings API (`planned`). The
`HoldingsSource` protocol (`app/sources/holdings.py`) returns normalised
`HoldingRecord`s (`as_of_date`, name, ticker/ISIN/SEDOL/CUSIP/FIGI, country,
sector, industry, currency, weight, market value, shares, source, status; extra
fields such as asset class / security type / price / coupon / maturity preserved
in `raw_payload_json`). The provider‑agnostic `holdings_ingestion` service upserts
them into `fund_holdings`.

**Live issuer adapters (explicit‑only).** The configured holdings default stays the
offline fixture, so the worker/scheduler never makes a surprise live call. A live
adapter takes an explicit `--url` download override first, then a *usable*
(verified/candidate) per‑fund URL from the shared **known issuer source config**
registry (`app/sources/issuer_source_config.py`, keyed by ISIN + source; see §D2);
without either it is a clean no‑op (empty). Every download goes through
`guarded_fetch` (recent‑success cache → source budget → fetch log → fetch); a budget
block / cache hit / fetch error yields an empty list (never a retry). Budgets are
conservative: ≤10/min, concurrency 1, ≥1s min delay (`source_budget._DEFAULT_BUDGETS`).
No secrets are stored — the fetch log keeps a host/path `endpoint_label` (no query
string) and an ISIN‑only request key.

* **iShares / BlackRock** (`blackrock_ishares_holdings`): the issuer‑hosted holdings
  CSV (`…/{productId}/{slug}/{ajaxId}.ajax?dataType=fund&fileName={TICKER}_holdings&fileType=csv`).
  Query params are case‑sensitive; the numeric `ajaxId` is **not** globally constant,
  so each known URL is the exact verified one (no page discovery). The CSV has a
  metadata preamble before the holdings table — the parser scans for the header row
  by column names and reads the `Fund Holdings as of` disclosure date from the
  preamble. Verified example: ISF (iShares Core FTSE 100 UCITS ETF, IE0005042456).
* **J.P. Morgan AM** (`jpmorgan_etf_holdings`): the daily ETF holdings export
  (`am.jpmorgan.com/FundsMarketingHandler/excel?…&type=dailyETFHoldings`). The `cusip`
  query param may carry an ISIN‑like UCITS identifier. The payload is content‑sniffed
  (CSV / TSV / HTML table, **or an OOXML `.xlsx` workbook** parsed with the stdlib —
  `app/sources/spreadsheet.py`, `zipfile` + `xml.etree`, **no pandas / xlrd / calamine
  dependency**, the same "use the stdlib" stance as the issuer HTML‑table extractor);
  the weight prefers `% of Net Assets` over `% of Market Value`. The live downloader is
  content‑aware — it returns text for a text body and raw **bytes** for a binary
  workbook, so a binary body is preserved byte‑exact for the sniffer (text decoding
  would corrupt it). **The legacy binary `.xls` (OLE2/BIFF) is still deferred:**
  decoding it needs a binary‑Excel dependency the project deliberately avoids; it is
  *detected* and surfaced by `--verify-source` as `reason=binary_unsupported` (a precise
  verdict, not a vague empty parse), and is a clean no‑op for ingestion. JEPG
  (IE0003UVYC20) returns exactly this legacy binary `.xls`, so its config stays
  **candidate** — the fix is a CSV / HTML‑table / `.xlsx` endpoint variant (or, only if
  ever justified, an optional `xlrd`/`calamine` extra: documented, **not wired**, since
  there is no verified binary payload to test against and AGENTS.md keeps the default
  install minimal). Verified US example shape: JEPI (CUSIP 46641Q332).
* **Vanguard** (`vanguard_holdings_export` implemented; `vanguard_holdings` planned):
  no stable official machine‑readable endpoint was verified, so the live adapter
  stays planned — **we do not scrape the brittle product‑page HTML as a canonical
  source.** `vanguard_holdings_export` is an OFFLINE parser for a manually exported
  official Vanguard holdings CSV: pass the local file path via `--url`
  (status `official_export`), or, for offline demo/tests, a small bundled sample
  keyed by ISIN (status `fixture`). Follow‑up: confirm a stable Vanguard
  spreadsheet/API URL before wiring `vanguard_holdings` (behind `guarded_fetch`).

**Upsert key.** Each holding gets a deterministic `holding_key`
(ISIN > FIGI > CUSIP > SEDOL > normalised `name|ticker`) and the row is unique on
`(fund_id, as_of_date, source, holding_key)` — so re‑runs/backfills never
duplicate and different sources keep their own snapshot. Not fuzzy matching. A bad
row (no name / unparseable weight) is isolated (skipped/counted), never failing the
whole file.

**Freshness / as‑of.** A holding belongs to a disclosure date (`as_of_date`); live
adapters read it from the file (the fixture stamps the previous month‑end). The
holdings freshness window is 45 days (`app/services/freshness.py`). Reads select a
single coherent snapshot per fund (`latest_holdings_by_fund`): the highest‑priority
source present (manual > vanguard export > live issuer > fixture > seed), then the
most recent `as_of_date` — sources never mix in a read, which keeps look‑through
exposure from double‑counting.

**Exposure.** `app/services/exposure.py` weights each fund's latest holdings by the
portfolio position's market‑value weight to derive look‑through country/sector
(and listing‑level currency) exposure (e.g. 50% in VUSA × 7% Apple ⇒ 3.5%).

**Compute boundary (non‑goals).** This slice is *collection only*: fetch, parse,
normalise and persist published holdings with provenance/freshness. **No** look‑
through analytics, PnL, total return, or index‑constituent substitution here or
anywhere in the backend — those belong in the Rust GUI / local pricer. Constituent
identity resolution stays a separate worker (holdings ingestion never calls
OpenFIGI). See SOURCES.md for vendor specifics and licensing.

### D. ETF / fund distributions — *real (fixture default + live issuer adapters)*
Declared distributions: ex/record/payment/distribution dates, per‑share amount,
currency, distribution type, frequency, share class. The default stays the offline
`distribution_fixture` (so the worker/scheduler never makes a surprise live call);
live issuer adapters are **explicit‑only** (named with `--source`, an explicit
`--url`) and route every download through `guarded_fetch` (recent‑success cache →
source budget → fetch log → fetch), store no secrets, and never scrape brittle HTML.

* **J.P. Morgan AM** (`jpmorgan_distributions`): the fund distribution export from
  the same `FundsMarketingHandler` family as holdings, with a distribution handler
  type (`am.jpmorgan.com/FundsMarketingHandler/excel?…&type=fundDistribution`; the
  `compositionOfFundDistribution` type also exists). The payload is content‑sniffed
  (CSV / TSV / HTML table, **or an OOXML `.xlsx` workbook** via the stdlib); columns are
  mapped robustly (Ex‑Date / Record Date / Payment Date / Distribution Amount / Currency
  / Distribution Type / Frequency / Share Class). **The legacy binary `.xls` (OLE2) is
  deferred** (no pandas / no binary‑Excel dependency; surfaced as
  `reason=binary_unsupported`). Explicit‑only: no JPM distribution URL is registered as a
  known config yet (the exact per‑product distribution handler URL must be verified
  before it is trusted — **do not assume it from the holdings download**), so pass a
  verified `--url`.
* **Vanguard** (`vanguard_distributions` live; `vanguard_distributions_export`
  offline): the official product‑data JSON/JSONP dataset
  (`api.vanguard.com/rs/gre/gra/…/datasets/urd-product-port-specific.json?vars=portId:<id>,issueType:F`)
  exposes a `distributionHistory` list. The parser strips a JSONP callback wrapper,
  locates the distribution‑history list (provider‑agnostic key search) and maps each
  row's keys (ex‑dividend / record / payable / distribution dates, amount, currency,
  type, frequency). `vanguard_distributions` is the live adapter: it uses an explicit
  `--url` first, then the **candidate** VUSA config in the known issuer source config
  registry (portId 9503, issueType F — see §D2), so `distribution_ingestion --fund-id
  <VUSA> --source vanguard_distributions` runs with no `--url`. The live download sends
  **conservative, identifying official headers** (a `User-Agent` naming the Mimer research
  client, `Accept: application/json,text/javascript,*/*`, `Accept-Language: en-GB`) —
  **no cookies, no browser automation, no TLS/browser fingerprint spoofing** (AGENTS.md).
  The config is `candidate` only — a prior live fetch was rejected at the **TLS handshake**
  (`SSLV3_ALERT_HANDSHAKE_FAILURE`), a transport‑layer rejection that conservative HTTP
  headers alone may not resolve; **do not promote it to `verified`** until a live
  fetch+parse from a network where the endpoint is reachable returns the expected
  `distributionHistory`. The offline `vanguard_distributions_export` parser is the safe
  fallback.
  `vanguard_distributions_export` is an OFFLINE parser for a manually exported
  JSON/JSONP/CSV file (status `official_export`), with a small bundled JSON sample
  keyed by ISIN for offline demo/tests (status `fixture`). **We do not scrape the
  brittle product‑page HTML as a canonical source.**
* **iShares / BlackRock** (`blackrock_ishares_distributions`): **planned.** No clean
  official machine‑readable iShares distribution download/endpoint has been verified.
  The holdings `…ajax?dataType=fund&fileName=…&fileType=csv` pattern is per‑fund and
  **does not imply** a distribution feed, so the adapter is a planned placeholder
  that fails cleanly (a clean failed `job_run`) rather than guessing a URL. The
  2026‑06‑25 issuer‑source slice deliberately did **not** add this adapter: a clean
  official distribution endpoint could not be confirmed within the bounded discovery
  window without guessing a URL pattern, so it stays planned. Follow‑up (unchanged):
  verify the iShares product‑page "Distributions" data export / download control (the
  product‑page data layer or a `dataType=` variant) before wiring behind
  `guarded_fetch` — never derive it from the holdings download.

**Upsert key.** A distribution is unique on `(fund_id, ex_date, source)` — re‑runs/
backfills never duplicate and different sources keep their own rows. The identity
date is the ex‑date, falling back to the distribution/payment/record date when an
issuer feed omits an explicit ex‑date (so the event is still keyable); the original
issuer dates are preserved on the row + in `raw_payload_json`. Amount + currency are
core (currency is detected from an explicit column, a `(USD)` header suffix or a
currency symbol); a row missing either, or with no parseable date, is isolated
(skipped/counted), never failing the whole file.

**Freshness / coverage.** The distribution freshness window is 120 days
(`app/services/freshness.py`, quarterly‑ish cadence). The market‑data planner emits
a `refresh_distributions` item for a distributing (or unknown‑policy) held fund with
no/stale distributions (accumulating funds pay nothing, so they are never flagged);
diagnostics expose `distributions`, `missing_distributions`, `stale_distributions`,
`latest_distribution_date` and `distribution_ingestion_failures`.

**Compute boundary (non‑goals).** This slice is *collection only*: fetch, parse,
normalise and persist published distribution observations with provenance/freshness.
**No** dividend forecasting, yield/income projection, tax treatment/classification,
total return or PnL here or anywhere in the backend — those belong in the Rust GUI /
local pricer. `distribution_type` is stored verbatim, never interpreted for tax. We
do **not** use Yahoo / JustETF / Morningstar / TradingView as a canonical
distribution source.

### D2. Known issuer source configuration + verification — *real (in‑code registry)*
A small, explicit, **in‑code** registry of issuer‑published holdings/distribution
download URLs keyed by fund ISIN + `source_name`
(`app/sources/issuer_source_config.py`). It is the single home for "which verified
URL does this fund use for this live source", consumed by the holdings/distribution
adapters, the market‑data planner, the capabilities catalogue and diagnostics. It is
in‑code (not a DB table) because the rest of the source layer already is, the set of
verified endpoints is tiny and hand‑curated, and the URLs are public issuer‑hosted
files (no secrets) — a DB‑backed admin/config system would be overbuilt here.

**Config shape.** `fund_isin`, `ticker`, `provider`, `data_type`
(`holdings`/`distributions`), `source_name`, `url`, `source_status`, `verified_at`
(nullable), `notes`.

**Status vocabulary + usage convention.**
* `verified`  — a clean live fetch+parse has been confirmed for this product.
* `candidate` — the URL/endpoint shape is known/observed but not yet confirmed by a
  live fetch+parse in this environment (the honest default — we do not inflate).
* `planned`   — recorded for documentation, not usable.
* `disabled`  — explicitly turned off (kept for provenance).

A config is **usable** (its URL is auto‑supplied to the worker without `--url`) only
when its status is `verified` or `candidate` **and** the live `--source` is
explicitly named — the configured ingestion default always stays the offline fixture,
so the worker/scheduler never makes a surprise live call. `planned`/`disabled`
configs are never auto‑used. Naming the live `--source` is itself the explicit
opt‑in, which is why a `candidate` config is usable but never the default.

**Seeded configs.** A `candidate` is promoted to `verified` only after a clean
`--verify-source` live fetch+parse (recorded with `verified_at`).

| Fund | Ticker | data_type | source_name | status | live-check outcome |
| --- | --- | --- | --- | --- | --- |
| `IE0005042456` | ISF | holdings | `blackrock_ishares_holdings` | **verified** (2026‑06‑25) | clean CSV → ~107 holdings parsed |
| `IE0003UVYC20` | JEPG | holdings | `jpmorgan_etf_holdings` | candidate | HTTP 200 but a legacy binary `.xls` (OLE2) body → `reason=binary_unsupported` (CSV/TSV/HTML‑table + OOXML `.xlsx` parse; old binary `.xls` deferred) |
| `IE00B3XXRP09` | VUSA | distributions | `vanguard_distributions` | candidate | TLS handshake rejected (`SSLV3_ALERT_HANDSHAKE_FAILURE`); now sends conservative official headers but the rejection is transport‑layer — not cleanly reachable unattended |

**Worker lookup (no `--url`).** With a usable config present, the live `--source`
runs without `--url`:

```bash
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id <ISF>  --source blackrock_ishares_holdings
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id <JEPG> --source jpmorgan_etf_holdings
uv run python -m app.workers.run distribution_ingestion    --fund-id <VUSA> --source vanguard_distributions
# --url still overrides everything for a single fund:
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id <X> --source blackrock_ishares_holdings --url "<csv>"
# workspace scope selects only funds with a matching config (others are a clean no-op):
uv run python -m app.workers.run issuer_holdings_ingestion --workspace-id 1 --source blackrock_ishares_holdings --limit 10
```

A fund with no usable config and no `--url` is a clean no‑op (`no_provider_match`),
never an error.

**Verifying a source (`--verify-source`).** `--verify-source` runs exactly one
guarded fetch+parse through the live adapter (cache → budget → fetch log → fetch) and
reports whether the endpoint returned a clean machine‑readable payload with the
expected shape (≥1 row; holdings need a name+weight+identifier, distributions need
amount+currency+date) — **without ingesting anything** (the only side effect is the
fetch log). Because the registry is in‑code it does not persist `verified_at`; it
prints a `SourceVerificationReport` (and the worker folds it into the `job_run`
message). Promotion `candidate → verified` is then a deliberate code change.

The report carries a `fetch_outcome` (the HTTP/fetch‑log status), a detected
`payload_format` (`text` / `xlsx` / `xls` / `pdf` / …) and a stable **`reason`** verdict
so operators get precise evidence — the reasons are distinct:

| `reason` | meaning | run / config |
| --- | --- | --- |
| `verified` | clean live fetch+parse with the expected shape | success; safe to promote |
| `binary_unsupported` | HTTP 200 but an undecoded binary workbook (legacy `.xls` / PDF) | failed run; keep `candidate` |
| `zero_rows` | parseable payload, but no rows | failed run; keep `candidate` |
| `missing_fields` | rows parsed, but missing expected identifiers/fields | failed run; keep `candidate` |
| `cache_hit` | served from the recent‑success cache (no live call) | clean no‑op (success) |
| `budget_blocked` | budget/backoff — no live call made | clean no‑op (success) |
| `no_url` | no `--url` and no usable known config | clean no‑op (success) |
| `fetch_error` | HTTP/network/parse failure | failed run; keep `candidate` |
| `unknown_source` | not a recognised live holdings/distribution adapter | failed run |

`binary_unsupported` is deliberately distinct from `zero_rows`: a `.xls` (OLE2) body is
*recognised* (and reported as such) rather than mis‑described as an empty parse.

```bash
uv run python -m app.workers.run issuer_holdings_ingestion --fund-id <ISF>  --source blackrock_ishares_holdings --verify-source
uv run python -m app.workers.run distribution_ingestion    --fund-id <VUSA> --source vanguard_distributions --verify-source
```

**Planner behaviour.** For a fund that needs a holdings/distribution refresh the
planner sets `known_config` / `config_status` / `needs_url_config` and a
`recommended_command`: when a usable config exists, the recommended command runs the
live `--source` with no `--url`; when none exists, `needs_url_config=true` and the
recommended action is to configure an issuer source URL (the offline fixture default
still works either way — `needs_url_config` is about *live* coverage, not a blocker).

**Diagnostics.** `issuer_source_configs` / `verified_issuer_source_configs` /
`candidate_issuer_source_configs` count the registry (global); `missing_holdings_
source_config` / `missing_distribution_source_config` count scoped funds with no
usable live config — *informational coverage only*, never an alert (the fixture
default works).

**Capabilities.** `GET /api/v1/data-sources/capabilities?data_type=holdings`
(and `…?data_type=distributions`) expose, per source, `requires_url`,
`known_config_available`, `config_status` and `example_fund_identifiers`
(`ticker:ISIN`, public issuer identifiers only).

**Non‑goals.** No scraping (issuer‑published files/APIs only); no third‑party
canonical sources (Yahoo/JustETF/Morningstar/TradingView); no analytics — this is
source verification + configuration only. We never guess a distribution URL from a
holdings URL, and never mark a config `verified` without a successful live
fetch+parse.

### E. Equity / ETF / fund prices — *real*
Market price time series, latest price, charting, stale checks. **Sources:**
`stooq` (implemented, default), `yfinance` (implemented, fallback); Alpha Vantage,
Tiingo, Finnhub, Massive/Polygon, FMP, Nasdaq Data Link, paid vendors later.

Two stores, one rule (a row belongs to a *listing* and always records a `source`):

* **Fund listings** — `price_ingestion` writes `prices` keyed
  `(fund_listing_id, price_date, source)` (a single close per fund listing).
* **Constituents** — `constituent_eod_price_ingestion`
  (`app/services/instrument_prices.py` + `app/sources/instrument_prices.py`) writes
  `instrument_prices` keyed `(instrument_listing_id, price_date, source)` — a
  generic OHLC + `adjusted_close` + `volume` + `currency` + `status` bar for a
  resolved constituent listing. Default `instrument_price_fixture` (offline,
  deterministic, knows the seeded equities); live `stooq` / `yfinance` only when
  requested, fetched one symbol at a time behind `guarded_fetch`. Only resolved
  constituents are priced; listings are deduped across funds; upserts are
  idempotent; a `manual` price is never clobbered. Surfaced via
  `GET /funds/{id}/constituents?include_prices=true`, `instrument-listings/{id}/
  prices`, and `…/time-series?kind=price` (subjects `instrument` /
  `instrument_listing`). This is the prerequisite for true look-through valuation,
  exposure drift, top‑holding performance and stock detail pages — the valuation
  wiring itself is a later slice.
* **Any instrument (unified)** — `instrument_eod_price_ingestion` writes the same
  `instrument_prices` table for *any* resolved listing, unioning constituents with
  the **resolved directly-held imported broker holdings** that
  `imported_instrument_resolution` linked to a listing (deduped, so an instrument
  held both ways is priced once). No separate table for imported holdings — an
  imported TSLA charts through `instrument-listings/{id}/prices` like a
  constituent. `constituent_eod_price_ingestion` remains a constituent-only entry
  point sharing the same selector + upsert.

Future: better exchange/ticker mapping, real adjusted close from a corporate-action
source, intraday vs EOD, source‑quality checks.

### F. NAV / iNAV / premium‑discount — *planned*
Compare market price vs NAV; ETF premium/discount. **Sources:** issuer NAV history
(iShares/Vanguard), exchange/fund data, commercial vendors. Free robust iNAV is a
known gap. Future: add a NAV model/table, issuer NAV ingestion, NAV‑vs‑price
endpoint. The `time-series?kind=nav` endpoint already returns
`status="unavailable"` (never fabricated).

### G. FX rates — *real (fixture provider, this iteration)*
Convert trading/distribution currency to the workspace base currency. **Sources:**
`fx_fixture` (implemented, offline); ECB reference rates, Bank of England (both
catalogued, `planned`), exchangerate.host, Alpha Vantage FX, broker FX. The
`FxSource` protocol (`app/sources/fx.py`) returns normalised `FxRateRecord`s
(`rate_date`, base/quote, `rate`, `source`, `status`); the provider-agnostic
`fx_ingestion` service infers the needed currencies from
workspaces/listings/distributions/holdings (or takes `--base`/`--quote`) and
upserts into `fx_rates`.

**Upsert key.** `fx_rates` is unique on
`(rate_date, base_currency, quote_currency, source)` — re-runs/backfills never
duplicate, only a genuine `rate`/`status` change counts as an update, and distinct
sources coexist. `rate` = units of quote currency per 1 base unit.

**Stored vs computed.** Only **canonical pairs** are stored. Inverse and cross
rates are computed in the lookup service (`app/services/fx.py`): a pair resolves as
**direct → inverse → triangulated** via a pivot (USD/EUR/GBP). A missing rate
yields an explicit *missing* status (never a silent 1). Conversions carry
rate/source/freshness and source-policy metadata
(`requested_source`/`effective_source`/`fallback_used`).

**Valuation.** The portfolio summary values each position in its local/listing
currency (pence GBX → GBP), converts to the workspace base currency, and exposes
`market_value_local`/`market_value_base`/`fx_rate`/`fx_source`/`fx_status`;
`total_market_value` is in base currency. Distributions get an optional
`amount_base` overlay (FX as of payment/ex-date, falling back to the nearest stored
rate, so it is a derived convenience). Diagnostics count
`missing_fx_rates`/`stale_fx_rates`/`unconverted_positions`/`fx_conversion_failures`.

**Endpoints.** `GET /api/v1/fx/rates`, `GET /api/v1/fx/time-series`
(subject `type="fx_pair"`, inverts a pair when only the opposite direction is
stored) and `GET /api/v1/fx/convert` (rate + full provenance).

**Future:** implement a live ECB EUR-reference-rate adapter behind the same
`FxSource` protocol (information-only, ~16:00 CET), enabled only when configured;
richer source-priority policy.

### H. Bonds / fixed income — *planned*
Reference, prices, yields, accrued interest, duration, cashflows, curves.
**Sources:** broker CSV/manual, exchange data, OpenFIGI, govt debt offices (UK DMO,
US Treasury), central‑bank curves (BoE/ECB/FRED), Nasdaq Data Link, paid vendors.
**Reliable bond reference/pricing is much harder to get free than equity/ETF EOD —
design the adapter interface, but do not pretend one free source solves it.**
Future: a generic instrument/security model first (see roadmap), then bond
reference, cashflow model, curves, pricing engine.

### H2. Official / reference rates — *real (fixture default + live US Treasury & ECB adapters)*
Official/reference rate **observations** stored in `reference_rates`: central‑bank
policy rates (ECB main refinancing / deposit / marginal lending, BoE Bank Rate),
overnight benchmarks (€STR, SONIA, SOFR, Fed Funds effective) and government par
yields (US Treasury 1M…30Y). One row = one published rate for one `rate_date`,
keyed on `(rate_date, currency, country_or_region, rate_family, rate_name, tenor,
source)` (idempotent; NULL `tenor` for policy/overnight rates), carrying
`rate_value` (Decimal), `unit`, `tenor`/`tenor_months`, `source`, `status` and
`source_url`.

**Worker:** `rates_ingestion` (`app/services/rates_ingestion.py` +
`app/sources/rates.py`; read shaping in `app/services/rates.py`).

**Sources.** The default stays the offline fixture; live adapters are
**explicit‑only** (named with `--source`), so the worker/scheduler never makes a
surprise live call.

| Source | Status | Coverage | Official endpoint |
| --- | --- | --- | --- |
| `rates_fixture` | implemented, offline (default) | EUR/GBP/USD policy + overnight + US par yields | none (deterministic) |
| `us_treasury_rates` | **implemented, live** | USD `treasury_par_yield` 1M…30Y | Treasury *Daily Treasury Par Yield Curve Rates* XML feed (`home.treasury.gov/.../interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=<year>`) |
| `ecb_rates` | **implemented, live** | EUR policy/facility (`policy_rate`/`deposit_facility`/`lending_facility`) + €STR (`overnight_rate`) | ECB Data Portal SDMX API (`data-api.ecb.europa.eu/service/data/<flow>/<key>?format=csvdata`) |
| `boe_rates` | **planned** | GBP Bank Rate + SONIA | BoE IADB / Statistical Database (see follow‑up below) |

**US Treasury live adapter (`us_treasury_rates`).** Fetches the official Treasury
par‑yield XML feed (the same machine‑readable feed the Treasury site renders), one
calendar year per request (bounded to the most recent few years for a backfill),
through `guarded_fetch` (recent‑success cache → source budget → fetch log → fetch;
20s timeout; conservative budget: ≤10 req/min, ≥1s spacing). The parser
(`parse_treasury_par_yield_xml`, pure/offline) maps the feed's `BC_*` columns to
tenors `1M, 2M, 3M, 4M, 6M, 1Y, 2Y, 3Y, 5Y, 7Y, 10Y, 20Y, 30Y` →
`rate_name=US_TREASURY_PAR_YIELD`, `rate_family=treasury_par_yield`,
`currency=USD`, `unit=percent`, `status=official`. The 6‑week `BC_1_5MONTH` and the
duplicate `BC_30YEARDISPLAY` are skipped; a missing tenor cell is skipped and a
non‑numeric cell is isolated (Decimal parsing, never float). Rows upsert
idempotently and re‑runs inside the cache TTL make no call. SOFR / Fed Funds are
**not** forced into this adapter (different official source); it stays par‑yield‑only.

**ECB live adapter (`ecb_rates`).** Fetches the official ECB Data Portal SDMX 2.1
REST API (`https://data-api.ecb.europa.eu/service/data/<flow>/<key>?format=csvdata`,
optional `startPeriod`/`endPeriod`), one bounded request per dataflow through
`guarded_fetch` (recent‑success cache → source budget → fetch log → fetch; 20s
timeout; conservative budget: ≤10 req/min, ≥1s spacing, concurrency 1). The two
dataflows + verified series keys (each fetched as one combined request via the SDMX
`+` operator) are:

| Dataflow | Series key | → `rate_name` / `rate_family` |
| --- | --- | --- |
| `FM` (key interest rates, change‑date) | `B.U2.EUR.4F.KR.MRR_FR.LEV` | `ECB_MAIN_REFINANCING_RATE` / `policy_rate` |
| `FM` | `B.U2.EUR.4F.KR.DFR.LEV` | `ECB_DEPOSIT_FACILITY_RATE` / `deposit_facility` |
| `FM` | `B.U2.EUR.4F.KR.MLFR.LEV` | `ECB_MARGINAL_LENDING_RATE` / `lending_facility` |
| `EST` (€STR, daily) | `B.EU000A2X2A25.WT` | `ESTR` / `overnight_rate` |

All rows normalise to `currency=EUR`, `country_or_region=euro_area`, `unit=percent`,
`status=official`, `tenor=NULL`. The parser (`parse_ecb_sdmx_csv`, pure/offline)
reads `KEY` / `TIME_PERIOD` / `OBS_VALUE` **by column name** (the `FM` and `EST`
dataflows order columns differently), maps each `KEY` to its series, parses `Decimal`
(never float) and ISO dates. An unknown series KEY is ignored; an empty value is
skipped; a non‑numeric value is isolated. **Observations are stored as supplied:**
ECB key interest rates are a *change‑date* series (one observation per rate change),
so they are **not** forward‑filled into a daily series; €STR is daily. No
interpolation / curve building anywhere. The two dataflows are spaced by the budget
min‑delay (same politeness pattern as the live price adapters). SONIA/Bank Rate are
**not** forced into this adapter (different official source — see BoE below).

**BoE — planned, follow‑up to verify before wiring (`boe_rates`).** The canonical
official series codes are confirmed — **Bank Rate = `IUDBEDR`**, **SONIA = `IUDSOIA`**
in the Bank of England *Interactive Database* (IADB) — but the IADB CSV export
(`www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&SeriesCodes=IUDBEDR,IUDSOIA&…`)
returns **HTTP 403 Forbidden** to a plain programmatic client. Kept **planned** until
a clean, non‑brittle machine‑readable access path is confirmed (the official source,
not a third‑party/FRED feed, and without resorting to browser‑emulation or HTML
scraping). When implemented it mirrors `ecb_rates`: `guarded_fetch`, conservative
budget, `Decimal` parsing, idempotent upsert, `currency=GBP`,
`country_or_region=united_kingdom`, `BOE_BANK_RATE`/`policy_rate` +
`SONIA`/`overnight_rate`.

**API:** `GET /api/v1/rates`, `/rates/latest`, `/rates/sources`,
`/rates/time-series`. `/rates/sources` now reports per source: `adapter_status`
(implemented/planned), `is_fixture`, `requires_live_fetch` (makes official network
calls) and `is_default` — so a client can tell the offline default from an
explicit‑only live adapter. The market‑data planner emits `fetch_reference_rates`
items for supported currencies (EUR/GBP/USD) with missing/stale rates (EUR's
`source_candidates` include `ecb_rates`, USD's `us_treasury_rates`); diagnostics count
`reference_rates` / `missing_reference_rates` / `stale_reference_rates` /
`latest_reference_rate_date` / `rates_ingestion_failures`.

**Capabilities convention.** Following the same rule as the constituent price
workers (Stooq/yfinance live adapters exist but the default is the fixture), the
`rates_ingestion` feature and the `reference_rates` data type stay reported as
`fixture` (the *default* source is offline). Live‑adapter availability is surfaced
honestly through `/rates/sources` and the source‑capability catalogue
(`us_treasury_rates` + `ecb_rates` → `adapter_status=implemented`; `boe_rates` →
`planned`).

**Explicit non‑goals (compute boundary — see AGENTS.md).** This is *collection +
normalisation + persistence + monitoring* only. The backend does **not** build
yield curves, fit or **bootstrap** curves, **interpolate**/extrapolate, construct
**discount factors**, compute **forward rates**, **price bonds**, run a rates
pricer, or compute PnL. There is no curve / discount‑factor / pricing table, and
`yield_curves` stays a **planned** data type (distinct from the real
`reference_rates`). Curve construction and analytics live in the Rust GUI / local
pricer, which consumes these observations.

### I. Futures — *planned (interface only)*
Prices, continuous contracts, expiry/rolls, curves. **Sources:** Databento,
Nasdaq Data Link, exchange (CME/Eurex), broker export. Split into two adapters:
reference/contracts metadata vs market‑data prices. No live implementation now.

### J. Options — *planned (interface only)*
Chains, prices, implied vol, Greeks, expiry/strike. **Sources:** Tradier, Massive/
Polygon, Alpha Vantage, Databento, Cboe, paid vendors. Future: `option_chain`
endpoint + adapter later. No live implementation now.

### K. Corporate actions / reference events — *planned*
Splits, dividends, mergers, ticker changes, fund mergers/closures, benchmark or
distribution‑policy changes. **Sources:** issuer/exchange notices, Alpha Vantage/
FMP/Polygon/Tiingo, broker data, manual. Future: a `corporate_actions` table,
event‑driven alerts, document‑diff integration.

### L. Documents — *real (fixture provider, this iteration)*
Factsheets, KIDs/KIIDs, prospectuses, annual/interim reports, with **content‑hash
change detection**. **Sources:** `document_fixture` (implemented, offline);
`issuer_documents` (issuer product pages/PDFs, `planned`), exchange document pages,
fund data vendors. The `DocumentSource` protocol (`app/sources/documents.py`)
returns normalised `DocumentRecord`s (type, title, url, date, language, region,
content_type, small content text/bytes, source, status). The provider‑agnostic
`document_ingestion` service hashes the content
(`app/services/documents.py:compute_document_hash` — SHA‑256 of bytes/text, else
stable metadata) and upserts into `document_snapshots`.

**Upsert key + change detection.** Unique on
`(fund_id, document_type, source, content_hash)`. Per document, vs the latest
stored snapshot of the same (fund, type, source): **new** / **changed** insert a
new snapshot (`change_status`; the changed row links the prior via
`previous_snapshot_id`/`previous_content_hash`), **unchanged** bumps `fetched_at`
without a new row. **Old snapshots are kept as history** — the change log backs the
GUI's document Changes view and future `document_changed`/`document_new` alerts.

**Reads / diagnostics.** `GET /api/v1/funds/{id}/documents`
(`document_type`/`latest_only`), `GET /api/v1/documents/{id}`, plus fund detail and
the dashboard. Diagnostics count
`missing_documents`/`stale_documents`/`changed_documents`/`new_documents`/`failed_document_jobs`.

**Storage stance.** The DB stores document *metadata + URLs + hashes + change
history* only — **never large binary blobs**. Downloaded PDFs would later go to
object storage / a filesystem cache; the content hash decides whether a document
changed. **No PDF text extraction / OCR** yet, and no text‑level diffing — those are
later workers; tests use fixture bytes/text and never download a live PDF.

**Future:** a live issuer document adapter (Vanguard/iShares/JPMAM) behind the same
protocol; PDF text extraction + structured‑field/text diffing.

### L2. Alerts — *real (database‑only, this iteration)*
Workspace‑scoped alerts in `alerts`, **derived** from the signals above — there is
**no source adapter and no network**. The `alert_generation` worker
(`app/services/alert_generation.py`) loads a per‑workspace `AlertContext` from
existing diagnostics/freshness/change services and runs the pure rules in
`app/services/alert_rules.py` (changed/new/missing documents, failed jobs,
stale/missing prices, missing/stale FX, missing/stale holdings, pending/ambiguous
instruments, price‑source conflicts, upcoming distributions).

**Upsert key + lifecycle.** Idempotent on `(workspace_id, dedupe_key)`; a re‑run
updates `last_seen_at`. An auto‑resolvable issue that disappears → `resolved`; a
returning issue reactivates; a **dismissed** alert with the same key stays
dismissed. Thresholds are centralised constants (`PRICE_STALE_DAYS`,
`HOLDINGS_STALE_DAYS`, `FAILED_JOB_LOOKBACK_DAYS`, …).

**Reads.** `GET /api/v1/workspaces/{id}/alerts` (filter by status/category/severity)
+ read/dismiss/resolve/mark‑all‑read; alerts also surface on the dashboard
(`alert_summary`) and diagnostics (alert counts).

**Future:** notification delivery (email/push), a user‑configurable rule DSL /
per‑workspace thresholds, and richer cross‑source conflict rules — all out of scope
here.

### L3. Exposure — *real (database‑only, this iteration)*
Cached look‑through exposure in `exposure_snapshots` / `exposure_rows`, **derived**
from positions + latest prices + FX + selected holdings snapshots — **no source
adapter and no network**. The `exposure_recompute` worker
(`app/services/exposure_recompute.py`) values each position in base currency
(reusing `FxIndex`), distributes look‑through weight across
`fund`/`holding`/`country`/`sector`/`industry`/`currency`/`source` dimensions, and
writes a snapshot + rows.

**Upsert key + idempotency.** A deterministic `input_hash` (positions/units,
prices used, FX used, holdings snapshots, base currency, as‑of, source policy) keys
idempotency: unchanged inputs vs the latest snapshot write nothing; changed inputs
insert a **new** snapshot (history preserved for drift detection). Unique on
`(workspace_id, as_of_date, input_hash)`.

**Coverage / honesty.** `coverage_weight`, `unclassified_weight` + an
`Unclassified` bucket, and `missing_holdings_count`/`missing_fx_count`; rows carry
a `status` (`ok`/`unclassified`/`missing_holdings`/`fx_missing`/`approximate`).
Currency look‑through prefers holding currency, else the listing currency
(`approximate`).

**Reads.** `GET /api/v1/workspaces/{id}/exposure` (latest snapshot, with
`dimension`/`snapshot_id`/`limit`; on‑the‑fly fallback flagged `cached=false`) and
`/exposure/snapshots`; also surfaced on the dashboard (`exposure` block) and
diagnostics (coverage/staleness counts), with optional `exposure`‑category alerts.

**True constituent look‑through valuation** (`app/services/constituent_valuation.py`,
folded into `exposure_recompute`). Feeds resolved constituent **EOD prices**
(`instrument_prices`) + **FX** into the snapshot so you can value an ETF's
*underlying* constituents (the total Apple across every fund), not just the
wrapper. Additive — the dimensions above are unchanged.

- **Fund value vs constituent implied value.** Implied constituent value stays
  **weight‑based** (`position_market_value_base × holding_weight`), *never* a
  share×price notional — ETFs publish weights, not your share counts. The
  constituent price/FX is coverage/contribution context; `valuation_method` says
  which (`fund_weight_lookthrough` vs `fund_weight_with_constituent_price_context`).
- **New dimensions:** `constituent` (one bucket per resolved instrument, deduped
  across funds, with typed `instrument_id`/`instrument_listing_id`/`price_*`/`fx_*`/
  `valuation_method` context), `constituent_price_status` (a coverage funnel:
  `priced_fresh`/`priced_stale`/`price_missing`/`fx_missing`/`missing_listing`/
  `unresolved_identity`/`unclassified`, summing to ~1.0) and `constituent_source`.
- **Coverage metrics** (weight‑based, nested `holdings ≥ identity ≥ price ≥ fx`)
  plus distinct‑resolved‑instrument counts, on every exposure response /
  snapshot summary (`constituent_coverage`) and the dashboard (`top_constituents`).
- **Dependency chain:** holdings → identity resolution → constituent prices → FX →
  `exposure_recompute`. A missing link is surfaced, never zeroed. The
  market‑data planner reports `true_lookthrough_ready` / `blocked_by_missing_*`.
- **Diagnostics/alerts:** `low_constituent_identity_coverage`,
  `low_constituent_price_coverage`, `constituent_valuation_fx_missing` —
  conservative, grouped per workspace, silent until some constituents resolve.

**Exposure drift + top movers** (`app/services/exposure_drift.py`). A read/compute
layer that diffs two snapshots (default previous‑vs‑latest; explicit ids are
workspace‑scoped, no cross‑workspace comparison). No table, no worker, no network.

- **Compares snapshots only** — not trades, not ETF rebalance causes, not PnL.
  `delta_market_value_base` is the change in the weight‑based *implied* value.
- **Dimensions:** constituent (matched by `instrument_id`), country, sector,
  industry, currency, source, constituent_price_status.
- **Per‑row:** weight/value deltas, status + price‑status change, `change_kind`
  (appeared/disappeared/increased/decreased/status_changed/unchanged). **Summary:**
  total abs deltas, appeared/disappeared counts, identity/price/fx coverage deltas.
- **Price‑context contribution (estimate, constituent only):** when a resolved
  constituent is priced in both snapshots, `price_return = comp/base − 1` and
  `base_implied_value × price_return` — a *price‑context estimate*, never PnL.
- **Dependency chain:** holdings → identity → constituent prices → FX →
  `exposure_recompute` (≥2 snapshots) → drift. Fewer than two ⇒
  `insufficient_history`.
- **Reads:** `GET /workspaces/{id}/exposure/drift` and `/exposure/top-movers`
  (`dimension`/`base_snapshot_id`/`comparison_snapshot_id`/`sort`/`limit`); a
  compact block on the dashboard (`exposure.drift`); diagnostics
  (`large_*_exposure_drift`, `*_coverage_deteriorated`,
  `no_prior_exposure_snapshot_for_drift`) and conservative, auto‑resolving,
  per‑workspace alerts.

**Top‑holding performance / price‑context contribution**
(`app/services/holding_performance.py`). The bridge from drift to "what likely
*drove value* over this window?". For the heaviest resolved constituents it pairs
the base + comparison snapshot rows and reports
`base_implied_market_value_base × local price_return`.

- **Not PnL** — a *price‑context estimate*, never realised PnL, total return or
  trade attribution; never infers buys/sells or ETF rebalance causes.
- **Local‑currency return** this slice (same listing/currency at both endpoints);
  FX drift between dates is not applied — `fx_rate_base`/`fx_rate_comparison` are
  surfaced as context for a future FX‑adjusted return in the Rust GUI/local pricer.
- **Bounded + SQL‑friendly:** snapshot rows are one‑per‑constituent; prices come
  from two batched `GROUP BY` / exact‑date queries over the capped top‑weight
  listing set — no whole‑history loads, no per‑instrument loops, no dataframes.
- **Selection:** previous‑vs‑latest by default (per‑constituent snapshot price
  dates), or explicit `start_date`/`end_date` (uniform as‑of) / snapshot ids
  (workspace‑scoped). `insufficient_history` / `insufficient_price_data` states.
- **Reads:** `GET /workspaces/{id}/exposure/top-holding-performance`
  (`limit`/`sort`/`base_snapshot_id`/`comparison_snapshot_id`/`start_date`/
  `end_date`); a compact dashboard block (`exposure.top_holding_performance`);
  data‑quality diagnostics (`top_holding_performance_missing_prices`/`_fx_missing`/
  `_insufficient_history`). No price‑move alerts.

**Future:** FX‑adjusted return, PnL attribution, total‑return analytics,
transaction‑level performance, `asset_class` and direct non‑fund instruments — the
generic `dimension`/`bucket` model and snapshot history are ready. Heavier /
interactive analytics belong in the Rust GUI / local pricer (see the architectural
boundary), not the backend.

### L5. Instrument onboarding / data‑readiness — *real (orchestration; database‑only)*
Not a data source — an **orchestration** layer
(`app/services/instrument_onboarding.py`, worker `instrument_onboarding`) that
coordinates the existing workers into a data‑readiness pipeline
(holdings → constituent_identity → constituent_prices → fx → exposure_recompute →
alerts) for a workspace or fund. It never re‑implements a worker: execution calls
the same `app.workers.run.run_job` dispatch the CLI/scheduler use.

- **Plan (read‑only):** `build_onboarding_plan` reports per‑stage status
  (`ready`/`needed`/`skipped`/`blocked`/`complete`), readiness/coverage,
  estimated requests by source, jobs that would run, and the next action —
  driven by the **market‑data planner** + DB state, with no writes / no network.
- **Source mode:** `fixture` (default, fully offline) or `live` (explicit;
  identity → OpenFIGI, prices → Stooq, still budgeted/cached/logged; holdings/FX
  fall back to fixture with a warning). The seeded scheduled job is **manual**.
- **Run:** a parent `instrument_onboarding` `job_run`; a single run cascades
  through the stages and a hard blocker stops dependents (`partial_success`).
  Each stage dispatches the existing worker(s) as **child** `job_runs`.
- **Run history / stage observability (read model, migration `0015`):** the
  parent run persists **typed stage rows** in `job_runs.payload_json` — per stage
  the `status` (`success`/`partial_success`/`failed`/`skipped`/`blocked`), a
  structured `reason` (`already_ready`, `skipped_by_flag`,
  `blocked_by_missing_holdings`, `blocked_by_unresolved_identity`,
  `worker_failed`, …), `source`/`expected_offline`, timings (`duration_ms`), the
  **child `job_run` ids** the stage produced, and record counts — plus scope,
  source mode and next action. `app/services/onboarding_runs.py` serves this as a
  bounded read model (`job_type='instrument_onboarding'`, workspace/fund scope,
  latest‑first, `limit` default 50 / max 200, `(job_type, id)` index). Pre‑`0015`
  runs have no payload → `legacy_metadata: true` (empty stages, `message` kept).
  The `message` stays human‑readable but is **not** parsed for core logic.
- **Reads:** `GET/POST /workspaces/{id}/onboarding/plan|run|status`,
  `GET /workspaces/{id}/onboarding/runs[/{run_id}]` and the fund equivalents
  (`GET/POST /funds/{id}/onboarding/plan|run`,
  `GET /funds/{id}/onboarding/runs[/{run_id}]`); a dashboard `onboarding` block
  (latest‑run id/status/duration/failed‑stage) and `onboarding_*` diagnostics
  counts (incl. `onboarding_recent_failures`,
  `onboarding_legacy_runs_without_stage_metadata`).

### L6. Job‑run timeline / failure drilldown — *real (read model; database‑only)*
A **generic, bounded observability read model over *all* `job_runs`**
(`app/services/job_timeline.py`, schemas `app/schemas/job_timeline.py`) for the
GUI Data Operations page — *not* a new analytics engine and *not* a workflow
engine. It generalises the onboarding‑specific run history (L5) to every worker
while leaving the onboarding endpoints intact.

- **Timeline / failures (lists):** latest‑first, bounded summaries (`limit`
  default 100 / max 500). Each item carries scope (`workspace_id` / `fund_id` /
  `fund_listing_id` / `scheduled_job_id` + `scope_label`), `status` + derived
  `severity`, timing + `duration_ms`, counts, `source_name`, masked `message`,
  `is_orchestration` / `has_payload` / `has_children`, a coarse `has_fetch_logs`
  hint, and the primary `recommended_action`. Failures are the same shape filtered
  to `failed` / `partial_success`.
- **Run detail:** the masked structured `payload`; typed `stages` + `child_runs`
  for orchestration runs (`instrument_onboarding`, expanded from the `0015`
  `payload_json` — never from `message`); `related_fetch_logs`;
  `source_budget_context`; `related_entities`; and `recommended_actions`
  (code + label, never executed).
- **Fetch‑log correlation is approximate.** No exact run↔fetch FK exists; a run is
  associated with `source_fetch_logs` by **source name + time window**
  (`started_at` … `finished_at` + small buffer), bounded (default 25 / max 100,
  latest first), labelled `fetch_log_correlation=time_window_source` (or
  `unavailable` for DB‑only producers / onboarding modes). Capabilities advertise
  `source_fetch_log_correlation: partial` to keep this honest.
- **Source budget context** is read‑only: `enabled`, current decision `status`,
  `allowed`, `wait_seconds`, `backoff_until` / `next_allowed_at`, and rolling‑24h
  `recent_failures` / `cache_hits` / `rate_limited_recently`.
- **Secret masking** (`app/services/secret_masking.py`, recursive for JSON) is
  applied to every surfaced message / payload / request key / endpoint label /
  error string — defence‑in‑depth on top of the already secrets‑free fetch‑log
  layer.
- **Reads:** `GET /jobs/timeline`, `GET /jobs/runs/{run_id}`, `GET /jobs/failures`,
  and the workspace‑scoped `GET /workspaces/{id}/jobs/timeline|failures` +
  `GET /workspaces/{id}/jobs/runs/{run_id}` (404 if foreign). The simple
  `GET /jobs/runs` list is unchanged (backward compatible). Diagnostics add
  `recent_partial_job_runs` + `latest_failed_job_run_id/type`.
- **Compute boundary:** bounded SQL only (capped limits, indexed scope /
  `(job_type, id)` filters, correlation windows bound by the run's own span); no
  per‑instrument loops, no analytics, no live calls.

### L7. Running / leased job observability — *real (read model; database‑only)*
The **live counterpart** of L6: where the timeline covers *completed* `job_runs`,
this classifies the `scheduled_jobs` lease columns the scheduler maintains into a
bounded, read‑only view of *what is happening now*
(`app/services/job_leases.py`, schemas in `app/schemas/job_timeline.py`). For the
GUI Data Operations page: what is running now, what is leased but stuck, which
lease expires soon and which worker owns it, when the last heartbeat happened,
which scheduled jobs are due but unclaimed, and which due jobs are blocked by an
active lease.

- **Lease classification** (one mutually‑exclusive state per scheduled job, from
  the single `classify_lease` helper): `running` (healthy active lease) /
  `stuck` (active but past `max_runtime_seconds` or heartbeat gone stale —
  worker likely died) / `expired` (`lock_expires_at` passed, reclaimable next
  pass) / `due` (active, non‑manual, overdue, not leased) / `not_leased`.
  `blocked_by_lease` is a derived flag: a running/stuck job whose `next_run_at`
  has passed (so the scheduler cannot claim it until the lease clears).
- **Reads:** `GET /jobs/running` (+ `summary`), `GET /jobs/leases?status=…`, the
  workspace `GET /workspaces/{id}/jobs/running`, and
  `GET /jobs/timeline?include_running=true` (adds `live_jobs` + `running_summary`
  without changing the completed `runs` list). Rows are ordered most‑urgent‑first
  and carry recommended actions as **codes + labels only**.
- **One definition everywhere:** the same `classify_lease` / `lease_summary_counts`
  helpers feed `/scheduler/status` (`running_leases` / `stuck_leases` /
  `expired_leases` / `blocked_by_lease` / `next_due_at`) and diagnostics
  (`running_job_leases` / `stuck_job_leases` / `expired_job_leases` /
  `blocked_scheduled_jobs_by_lease` / `due_scheduled_jobs`), so the surfaces never
  disagree. Capabilities advertise `running_job_timeline` /
  `job_lease_observability` / `stuck_lease_read_model` as `real`.
- **Read‑only, bounded, no scheduler rewrite.** `scheduled_jobs` are global shared
  infrastructure (no `workspace_id`), so the workspace view returns the same
  global scheduler health. There is deliberately **no** unlock / kill /
  force‑release / lease‑repair endpoint — it classifies the lease columns, never
  mutates them. Queries are bounded (leased‑or‑due only, capped `limit`).

### M. Broker / user imports — *real (offline `generic_csv_v1`, this iteration)*
The bridge from the market‑data workstation to the *user portfolio* workstation.
A broker CSV export is parsed into the canonical, workspace‑private
`portfolio_transactions` ledger (`broker_imports` + raw `broker_import_rows` for
provenance), and committed transactions reconcile into a bounded
`portfolio_position_snapshots` read model.

- **Adapter (pure):** `app/sources/broker_imports.py` — `generic_csv_v1`, a
  forgiving generic CSV with a small alias set. Deterministic, no DB / no
  network. A bad row (bad date/decimal/missing field) is isolated + flagged; an
  unmapped `type` is a warning. `date` + `type` columns required; trades need a
  `quantity`, cash movements need an amount, every row needs a `currency`.
- **Ingestion (DB):** `app/services/broker_imports.py` — preview (read‑only),
  idempotent commit (`broker_imports` unique on `(workspace_id, source_hash)`;
  transactions unique on `(workspace_id, transaction_key, source)`), and a
  bounded reconciliation (`quantity = buys − sells` per instrument; signed cash
  per currency) idempotent on `input_hash`.
- **Resolution (at import):** existing identity only — **ISIN → FIGI → unique
  ticker(+currency)** — across funds/listings, `security_identifiers`, and the
  constituent instrument master. **No live calls, no name‑only guesses**: an
  ambiguous/unmatched row is stored `unresolved_instrument` and surfaced via
  diagnostics, never linked to a guessed instrument or used to auto‑create one.

### M2. Imported‑instrument resolution bridge — *real (offline fixture default, this iteration)*
`status=unresolved_instrument` imported transactions (directly‑held TSLA/AAPL/…)
are turned into the canonical `instruments`/`instrument_listings` universe and
relinked so they can be priced/charted/looked‑through. Service:
`app/services/imported_instrument_resolution.py`; worker
`imported_instrument_resolution`; API `POST …/transactions/resolve` (+ a dry‑run)
and `POST …/broker-imports/{id}/resolve`. It is a **bridge, not a second identity
system** — it reuses the constituent resolvers and the shared instrument upsert.

- **Order:** (1) existing identity first (`broker_imports.build_resolution_index`)
  — a symbol resolvable since import links with **no** resolver call; (2) deduped,
  source‑safe resolver requests **ISIN → FIGI → ticker(+currency)** through the
  shared `constituent_identity_fixture` (offline default) / `openfigi` (live,
  opt‑in, budget‑guarded) resolvers, upserted via
  `constituent_identity.upsert_candidate_instrument`; (3) backfill links + status
  and re‑reconcile the bounded snapshot.
- **Safe‑request rules:** never name‑only (`skipped_unsafe`, never auto‑created);
  OpenFIGI gets ISIN/FIGI only (a bare imported ticker has no exchange);
  ambiguous → `ambiguous_instrument` (not linked); `not_found`/`failed` stay
  `unresolved_instrument`; never clobber a manual / already‑`ready` link.
- **Fixture:** the shared `constituent_identity_fixture` now also resolves common
  directly‑held instruments (TSLA, JEPG, plus the existing AAPL/MSFT/…). Seeded
  ETFs (VUSA/ISF) resolve to the existing **fund** listing via existing identity —
  never a duplicate instrument.
- **Dry‑run:** `dry_run=true` reports outcome counts but writes nothing.
- **Statuses:** `committed` (resolved at import) · `unresolved_instrument` ·
  `ambiguous_instrument` · `resolved`/`ready` (linked). All participate in the
  bounded reconciliation; only the two unlinked statuses keep a position flagged.

**Not implemented (deliberately):** manual transaction CRUD, PnL / realised gains,
tax lots, total return, corporate actions, broker‑specific parsers. The existing
manual `portfolio_positions` CRUD table is untouched. **Future sources:** IBKR
Flex/Client Portal, Saxo OpenAPI, Trading 212, broker‑specific parsers behind the
same parser protocol.

### M3. Portfolio valuation / readiness snapshots — *real (bounded read model; database‑only)*
One layer above the reconciliation: it joins the reconciled positions (net
quantity per instrument) + cash (per currency) to the **latest already‑ingested**
fund/instrument price + FX (at/before `as_of_date`) and reports a per‑position
market‑value *context* in base currency, with a `valuation_status`
(`valued`/`missing_price`/`missing_fx`/`unresolved_instrument`/`ambiguous_instrument`/
`cash_only`/`zero_quantity`/`stale_price`/`stale_fx`), a `readiness_status` and the
`blocking_reasons`. Service `app/services/portfolio_valuation.py`; worker
`portfolio_valuation_recompute`; tables `portfolio_valuation_snapshots` /
`portfolio_valuation_rows` (migration `0019`); API
`GET …/portfolio/valuation[/latest|/coverage]` + `POST …/portfolio/valuation/recompute`.

- **Consumes already‑ingested data only** (`prices` / `instrument_prices` /
  `fx_rates` + the reconciliation) — **no live price/FX fetch, no identity
  resolver**, ever. GBX is normalised to GBP; a missing FX path is never a silent 1.
- **Reuses the reconciliation** (`broker_imports.committed_transactions` /
  `reconcile_transactions`) — it does not fork the position/cash aggregation.
- **Idempotent + bounded:** `input_hash` over the reconciled positions/cash + every
  price/FX used (unchanged ⇒ no write; a new price/FX or (re)resolution ⇒ a new
  snapshot); unique `(workspace_id, as_of_date, input_hash)`; bounded by `limit`.
- **Not PnL.** No cost basis, realised/unrealised gain, tax lots, total return or
  performance attribution — a value that cannot be computed safely is reported as a
  *blocker*, never invented. Those engines live in the Rust GUI / local pricer and
  stay **planned**. The planner emits `recompute_portfolio_valuation` (a local
  recompute, `estimated_requests=0`) when the snapshot is missing/stale.

### M3a. Valuation history / summary / dashboard read models — *real (bounded; snapshots only)*
A **bounded, snapshot‑backed read model** over the M3 snapshots (no recompute on the
read path). Service `app/services/portfolio_valuation.py`
(`get_portfolio_valuation_history` / `build_summary` / `build_dashboard_block`); API
`GET …/portfolio/valuation/history` (oldest‑first series; `broker_account_id` /
`start_date` / `end_date` / `base_currency` / `limit` ≤ 500) and
`GET …/portfolio/valuation/summary`; plus a `portfolio_valuation` block on the
workspace dashboard.

- **Coverage / readiness only — never returns/PnL/performance.** Each history point
  carries the snapshot's per‑status counts, `total_market_value_base` (a coverage
  figure, *not* a return), a `valuation_coverage_ratio` and a snapshot‑level
  `readiness_status` (`ready`/`partial`/`blocked`/`stale`/`empty`). Consecutive
  points are **never differenced** into a return, and a value delta is **never**
  labelled PnL.
- **Reads snapshots only.** No live price/FX fetch, no identity resolver, no
  recompute — the recompute worker / `POST …/recompute` is the only writer. The
  dashboard block shows the latest snapshot (or `status=missing`) plus a
  `needs_recompute` flag + a single `recommended_action`.
- **Bounded.** History `limit` is clamped (max 500, newest window presented
  oldest‑first); the `broker_accounts` breakdown is a small distinct‑then‑latest scan
  (populated only for account‑scoped snapshots). No per‑day backfill engine.
- Capabilities advertise `portfolio_valuation_history` / `portfolio_valuation_dashboard`
  **real**; diagnostics add `portfolio_valuation_history_points`,
  `portfolio_valuation_latest_coverage_ratio` and `portfolio_valuation_readiness_status`.

## Licensing & usage caution

Not legal advice — engineering caution:

- Free/public data may be fine for **personal/internal** use but often **restricts
  redistribution/display** (e.g. FMP requires a display licence; DMO’s
  FTSE‑Tradeweb prices are non‑commercial only).
- **Delayed exchange data has terms** (LSE marks site data ≥15 min delayed; ECB/BoE
  publish reference, not transaction, rates).
- **Issuer pages are not stable APIs** — HTML/PDF structure, jurisdiction variants
  and session behaviour change; avoid aggressive scraping, **cache responsibly**,
  and keep a clear fallback.
- **Reliable bond/option/futures data is generally paid.** Don’t hide that behind a
  free adapter.
- **Every adapter must record `source`** and avoid hiding uncertainty.
- **Tests must never depend on live network** — use fixtures/mocks. Live calls are
  for opt‑in integration checks only, rate‑limited.

## Future architecture: generic multi‑asset model

The current model is **fund‑centric** (`funds` + `fund_listings`), which is correct
for ETFs/funds today. **Do not refactor it into a generic security master
prematurely.** The likely direction when equities/bonds/derivatives need first‑class
support:

```text
instruments
  id, instrument_type, name, primary_identifier, primary_identifier_scheme,
  currency, country, status, source

instrument_listings
  id, instrument_id, ticker, exchange, mic, currency, figi, sedol, cusip, isin

funds
  id, instrument_id (nullable/future), isin, provider, domicile, strategy,
  distribution_policy, ocf

bonds
  id, instrument_id, issuer, coupon, maturity, day_count, coupon_frequency

derivatives
  id, instrument_id, underlying_instrument_id, contract_type, expiry, strike,
  multiplier
```

Migration sketch: introduce `instruments` as a thin spine, backfill one row per
existing fund/listing, add a nullable `instrument_id` FK to `funds`/`fund_listings`,
then add `bonds`/`derivatives` as needed. Adapters and the capability registry are
already keyed by asset class, so the read APIs and ingestion contracts do not have
to change shape when this lands.

## Adding a new source — checklist

1. Append a `SourceCapability` to `app/sources/registry.py` (`adapter_status="planned"`).
2. Write the adapter behind the relevant `app/sources` protocol; return normalized
   dataclasses with provenance, never touch the DB.
3. Register it in the source registry and add a config default if it should be
   selectable (`*_SOURCE_DEFAULT`).
4. Make the ingestion service upsert idempotently, record `source`, and write a
   `job_runs` row; keep it provider‑agnostic.
5. Add tests with **fixtures** (no live network). Flip `adapter_status` to
   `implemented` and update the status table above + `capabilities` service.
6. Never break the GUI aggregate endpoints; preserve source/status/freshness.
