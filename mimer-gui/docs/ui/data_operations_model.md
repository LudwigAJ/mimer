# Data Operations / Market Data Readiness

Data Operations is a dense operational workspace for answering:

- what data is ready, stale, missing, blocked, or mock-backed
- which source budget or fetch log explains a blocker
- which market-data plan item should run next
- which constituent identities or prices are not ready for exposure

The page is mock/offline-first and can hydrate from REST when API mode is enabled. Endpoint logic remains outside page rendering and updates `DashboardSnapshot::data_operations` through the provider/worker boundary.

## Page Structure

The workspace renders these sections:

- Readiness Summary: compact stages for Holdings, Identity, Prices, FX, Exposure, Performance, Alerts, Jobs, and Sources.
- Next Recommended Actions: derived from actionable market-data plan items and diagnostics.
- Market Data Plan: priority, item type, subject, reason/blocker, source, estimated requests, status, and next action.
- Scheduler / Due Jobs: scheduled jobs with last status, due/planned state, running lease indicator, source, and mock run/copy actions.
- Source Budgets: source availability, request window, delay/backoff, failures, cache hits, next allowed time, and capabilities.
- Recent Fetch Logs: request kind/key, source, status, HTTP/status, duration, cache/rate-limit flags, and masked error details.
- Constituent Coverage: fund, holding, weight, identity, instrument/listing, latest price, price date/source, price status, and next action.
- Diagnostics / Blocking Issues: critical/warning operational issues with recommended action and related page.
- API Sections: endpoint availability, mapped record count, and sanitized section-level error detail for core and newer observability endpoints.

## Interaction Model

Single click selects an operations row and updates the right inspector while it follows selection. Pinned inspector contexts use owned `InspectorContext` variants for readiness stages, plan items, source budgets, fetch logs, constituent readiness, and diagnostics.

Double-clicking plan/constituent rows opens the modeled subject when one exists. Actions labeled `Run now` or `Run once` are local mock actions and only update feedback/mock job state.

The header exposes Refresh, Use Mock, Use API, Settings, Copy URL, last refresh, and hydration status. Provenance is one of `MOCK`, `API`, `PARTIAL API`, `STALE API`, or `API ERROR`.

Copy actions and keyboard copy prefer the selected operations row. Fetch-log display/copy and API errors mask API keys, tokens, bearer/authorization values, passwords, secrets, and URL userinfo.

## Backend Boundary

No backend business logic, async runtime, auth implementation, or direct database access is present. `ureq` performs blocking REST calls only inside a background worker. The worker sends an owned snapshot back through a channel; the egui render loop only polls the channel.

Refresh behavior:

- Mock mode reloads API-shaped fixtures.
- API mode starts from previous/mock rows, fetches endpoint sections in parallel background threads, and replaces only successfully mapped sections.
- Failed or unsupported endpoints become section failures. If some calls succeed, the result is `PARTIAL API`; if all fail, previous API data becomes `STALE API` or mock fallback becomes `API ERROR`.
- Each refresh has a generation id. Superseded responses are ignored.
- Timeouts are per request and configurable in Settings.

Hydrated endpoint families:

- scheduler status and due jobs
- source budgets and source fetch logs
- workspace market-data plan, dashboard readiness, diagnostics, and constituent exposure
- workspace job timeline, running jobs, and failures
- workspace onboarding status and runs
- broker imports, transactions, and positions

Job timeline/running/failure payloads map into the existing `JobRun` model. Onboarding, broker import, transaction, and position endpoints currently expose availability/counts in API Sections; dedicated detailed tables are deferred until backend response contracts are stable.

## Configuration

- Default mode: Mock.
- Default base URL: `http://localhost:8080/api/v1`.
- Persisted fields: data mode, base URL, timeout, workspace header value, and Data Operations auto-refresh.
- Base URLs with credentials, query strings, fragments, or secret-like assignments are rejected before persistence.
- Test Connection performs a background `GET /scheduler/status`.

The GUI must keep mock/offline mode working and must preserve source, status, provenance, freshness, and the fund/listing distinction.
