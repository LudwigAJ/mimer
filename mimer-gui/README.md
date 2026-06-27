# Mimer

Mimer is an early Rust `egui`/`eframe` GUI scaffold for a dense ETF and portfolio workstation.

The app remains mock/offline-first. Data Operations can optionally hydrate from REST through the provider/worker boundary; all other workspaces remain local/mock.

## Run

```bash
cargo run
```

Useful development checks:

```bash
cargo fmt
cargo check
cargo clippy
cargo test
```

## Local Storage

Mimer now creates a per-OS app data root and a versioned storage folder:

- Windows: `%APPDATA%/Mimer/v0.1.0/`
- macOS: `~/Library/Application Support/Mimer/v0.1.0/`
- Linux: `$XDG_DATA_HOME/Mimer/v0.1.0/`, falling back to `~/.local/share/Mimer/v0.1.0/`

The app creates directories eagerly and creates `manifest.json` on first launch. Other files are written when needed:

```text
Mimer/
  v0.1.0/
    manifest.json
    config/
      settings.json
      ui_state.json
    cache/
      cache_manifest.json
      charts/
      documents/
      api/
    data/
      local_overrides.json
      saved_views.json
      watchlists.json
    exports/
    logs/
    tmp/
```

Current storage format is JSON for manifest, settings, UI state, and small mock/local metadata. SQLite is deferred: the code has cache policy types and a `LocalCache` trait so structured backend caches can later move to SQLite, while large document/PDF payloads should remain filesystem blobs. Cache can be rebuilt; local user data such as overrides, watchlists, saved views, and future baskets must be protected.

Cache policy is modeled by cache kind. Dashboard/search caches are short-lived; fund detail and time series are medium-lived; source capabilities are long-lived; documents are long-lived and should later invalidate by content hash; derived analytics invalidate when inputs, overrides, source policy, or app/schema version changes. Manual overrides are replayed into effective data and are not erased by cache refresh.

Version migration is explicit. Future versions use sibling folders such as `v0.1.0/` and `v0.2.0/`; the migration model can discover previous versions, plan safe config/user-data copy-forward, and rebuild cache rather than blindly copying incompatible cached backend data.

## Current GUI

- Native title includes app version and supported data modes, for example `Mimer - v0.1.0-alpha (mock/api)`.
- Desktop-style menu bar: File, Edit, View, Navigate, Data, Tools, Window, Help. Domain actions such as Portfolio, Data Operations, Charts, Compare, Spread, Curves, Jobs, and source diagnostics are grouped under those conventional workstation menus.
- Nav/command row: compact Back/Forward/Home, a roomy command/search field, Go, Refresh, and focused keyboard tips.
- Status bar: workspace, mode, data/API status, base currency, last refresh, running jobs, source policy, zoom, storage status, command-history count, and latest feedback.
- Persistent context strip: breadcrumbs, active subject, compact selected subject, page, and clear selection. Full selected-subject details live in hover text and the inspector.
- Optional module rail, right inspector, context strip, and bottom status bar.
- Left navigation: Portfolio, ETFs, Hierarchy, Fund Detail, Charts, Add Instrument, Dividends, Holdings, Exposure, Data Operations, Alerts, Documents, Jobs, Analytics, Curves, Compare, Spreads, Changes, Search, Settings. Document Viewer is a drilldown page opened from rows, commands, or navigation history.
- Central pages: table-first ETF/portfolio dashboard views with mock positions, funds, distributions, holdings, exposure, data operations/readiness, alerts, documents, jobs, and compute diagnostics.
- Search page: mock/local search across portfolios, funds, listings, holdings, documents, and jobs with debounced query updates, type/source filters, a results table, preview inspector, double-click open, context-menu Open/Make Active/Plot/Copy/Show Source, and backend-ready result models.
- Command box: supports examples like `VUSA`, `/ VUSA`, `search VUSA`, `goto jobs`, `data operations`, `refresh data operations`, `use mock data`, `use api data`, `test backend connection`, `copy backend url`, `market data plan`, `source budgets`, `fetch logs`, `reset table columns`, `show all columns`, `job timeline`, `onboarding runs`, `broker imports`, `plot active`, `plot VUSA vs ISF`, `spread VUSA JEPG`, `copy chart row`, `pin inspector`, `back`, `refresh`, `legend`, and `toggle inspector`.
- Command history is session-local. Execute with Enter, then recall previous commands with Up/Down while the command field is focused.
- Help and no-match feedback show compact suggestions, including right-click row menus, double-click opening, sorting, source selection/fallback, overlays, Back/Forward/Home, and document viewer behavior.

## Keyboard Workflow

- `Ctrl/Cmd+K`: focus command/search.
- `Ctrl/Cmd+F`: open the Search workspace and focus its query.
- `Esc`: clear command text or dismiss transient help/legend windows.
- `F5` or `Ctrl/Cmd+R`: queue a background refresh. Mock mode reloads fixtures; API mode hydrates Data Operations while retaining mock data elsewhere.
- `Ctrl/Cmd+C`: copy the selected row/cell/subject payload, falling back to the active subject.
- In Charts, arrow up/down moves the focused chart data row, arrow left/right moves the focused chart data cell, and Enter opens the focused row subject.
- In Charts, Alt/Option+Up/Down cycles the selected series, and Alt/Option+Left/Right cycles points within the selected series.
- In Charts, `Ctrl/Cmd+C` copies focused cell first, then selected chart point/row, then selected chart series, then chart summary.
- `Ctrl/Cmd++`, `Ctrl/Cmd+=`, `Ctrl/Cmd+-`, `Ctrl/Cmd+0`: zoom in, zoom out, and reset application zoom.
- `Ctrl/Cmd+1`: Portfolio.
- `Ctrl/Cmd+2`: ETFs/Instruments.
- `Ctrl/Cmd+3`: Fund Detail.
- `Ctrl/Cmd+4`: Jobs.
- `Ctrl/Cmd+5`: Alerts.
- `Ctrl/Cmd+6`: Documents.
- `Ctrl/Cmd+I`: toggle inspector.
- `Ctrl/Cmd+B`: toggle left navigation.

## Table And Inspector Model

- Prefer `egui_extras::TableBuilder` for scan/sort/select data. Use `egui::Grid` for compact key/value facts, settings forms, metadata, source/provenance summaries, and inspector facts.
- Grid cells should reserve enough key/value width, truncate compact values only when needed, and expose full values through tooltips or copy actions. Shared helpers live under `src/ui/`.
- Main pages use flexible available-width/available-height sizing where practical. Side detail panels yield width to central tables on smaller windows, and wide screens keep table plus inspector/detail panes visible.
- Portfolio, ETFs, Holdings, Dividends, Documents, Jobs, Alerts, and chart data tables keep explicit selected/sort/filter state where relevant.
- Most operational tables support click-to-sort headers with `↑`/`↓` markers and predictable text, numeric, or date-like ordering.
- Portfolio, ETFs, Documents, Jobs, Holdings, Alerts, Dividends, and chart rows expose right-click/context-menu actions where they are useful.
- Portfolio, Holdings, Documents, Jobs, and Alerts have compact case-insensitive filters local to each page.
- Double-clicking portfolio/fund/holding source rows opens through the navigation stack where applicable; double-clicking a document row opens an in-app Document Viewer.
- The Document Viewer shows mock metadata, source/status, a stable mock URI, content-hash/change metadata, extracted fields, change summary, and a placeholder preview for future PDF rendering.
- The context strip exposes Back, Forward, Home, and compact breadcrumbs such as `Main Portfolio > VUSA`.
- `AnalysisSubject` is the lightweight subject model for things that can be opened, inspected, held, plotted, compared, or drilled into. It currently covers workspace portfolios, funds, fund listings, holdings, cash, and synthetic models without erasing their domain differences.
- The Hierarchy page renders an investable tree with workspace portfolios, mock sub-portfolios, ETF listings, and look-through holdings.
- The right inspector is resizable and context-sensitive. It follows selected rows by default, falls back to the active subject when nothing row-specific is selected, and can be pinned so later row selections do not replace the panel until unpinned.
- Portfolio and instrument inspectors expose quick actions such as Open, Plot Price, Plot Value, Plot NAV vs Price, Plot Distributions, and Explain Value.
- Manual override status is visible in Portfolio cells (`MANUAL`) and dependent values (`DERIVED`/`EST`); Analytics shows a compact override list with row-level clear actions.
- Alert read/dismiss/resolve controls and related job actions remain local mock UI state.
- Important row values support context-menu copy actions. Portfolio, ETF/Fund, Document, Jobs, and Chart flows use `COPIED: ...` feedback when data is copied to the clipboard.
- Clickable rows and editable cells use cursor affordances where they clarify behavior; editable portfolio cells expose edit/override tooltips.
- Managed workstation tables persist stable column widths and visibility in UI preferences. Their compact Columns menu can hide/show low-priority fields, widen or narrow the focused column, show all, reset defaults, and copy only visible row values. `View -> Show All Table Columns` and `View -> Reset Table Columns` apply globally.
- Column order follows stable descriptors and is stored for forward compatibility, but drag reordering and native drag-resize persistence are deferred.

## Charts And Plot Requests

- Plotting is a first-class workflow: table actions, Compare/Spread commands, and chart controls create a `PlotRequest`, then the Charts page renders the active request.
- `src/timeseries.rs` defines mock `TimeSeries`, `TimeSeriesPoint`, and `TimeSeriesKind` data. Dates remain strings for now.
- `src/charts.rs` maps those ISO dates to plot x coordinates as days since the Unix epoch. Calendar gaps are preserved; weekends, holidays, and sparse mock series appear as real gaps rather than compressed point indexes.
- Mock time-series data includes ETF prices, VUSA NAV, VUSA distributions, portfolio value, and projected income.
- The Charts page uses `egui_plot` for utilitarian line plots and keeps source/status labels plus a sortable data table visible.
- Charts expands into available central-panel width/height, keeps controls wrapped compactly, uses the plot as the main flexible region, and avoids squeezing the plot with metadata when the window is narrow.
- Charts has explicit `Single`, `Overlay`, `Compare`, and `Spread` modes, a compact subject picker, series preset picker, source selector/fallback indicator, value-mode selector (`Raw`, `Rebased 100`, `% Change`), range selector (`1M`, `3M`, `6M`, `1Y`, `All`), recent plot history, overlay picker/list, source/status metadata, and a combined series table.
- The chart data table distinguishes selected series, selected point, focused cell, and active subject. Displayed value and raw value are both visible, with source/status/kind retained for row, point, and series copy payloads.
- The plot area supports date-axis tick labels, nearest-point hover readouts, click-to-select point picking, double-click-to-open near a point, selected/hover guide markers, and a plot context menu for copying hover/selected values, opening the selected subject, pinning the inspector, clearing chart selection, resetting the chart, clearing overlays, and changing value mode. Single-click plot selection does not make the subject active.
- Plot/table sync is conservative: table selection highlights the corresponding plotted point at its date x-value, plot selection selects the related series and focuses the matching table row by series id plus date when it can be found, and stale selected points are cleared after range or value-mode changes.
- Hover and selected-point readouts include cursor date, nearest point date, source, status/freshness-style tags, value mode, displayed value, raw value when transformed, and derived spread metadata where available. Range controls filter by date window (`1M`, `3M`, `6M`, `1Y`, `All`) rather than last-N point counts.
- `compare VUSA ISF` and `plot VUSA vs ISF` open Charts in compare mode with A/B series plus a compact summary table. Compare summaries show latest common A/B values, A - B, percent difference, raw common values, common-date coverage, value mode, base date, and A/B source/status. Legacy Compare navigation seeds the same Charts workspace mode.
- `spread VUSA JEPG`, `VUSA - JEPG`, and `plot spread VUSA JEPG` open Charts in spread mode. Spread data is derived only from matching mock dates and is labeled as `DERIVED`, `PARTIAL`, or `MISSING`; no real pricing or return analytics are run. Spread summaries expose the expression, latest spread, matching-date coverage, A/B sources, and copy/swap/compare/open actions.
- `Rebased 100` and `% Change` display modes use the first valid common non-zero base point for compare/overlay series. Spread mode remains raw A - B and visibly labels transformed spread output as deferred.
- Fund Detail uses the same time-series model for inline date-axis Price/NAV charts and chart-data tables.
- Drag/brush date-range gestures remain deferred; egui_plot panning/zooming is still kept conservative for this mock workstation slice.
- Curves uses validated `YYYY-MM-DD` date text input. The current generated `egui_extras` docs for this dependency set do not expose `DatePickerButton`, so a native date picker is deferred until the dependency provides one or a lightweight date widget is introduced.

## Settings And View State

- Theme and density changes apply immediately to the egui context and are persisted with existing shell preferences.
- Application zoom applies through the egui context, is clamped to a workstation-friendly range, is available from the View menu, command box, Settings, and keyboard shortcuts, and is persisted with shell preferences.
- Data mode and Data Operations auto-refresh apply immediately. Backend URL, timeout, workspace header, refresh interval, and base-currency edits remain pending until Apply; invalid or secret-bearing URLs are rejected.
- Settings and UI-state JSON writes are debounced so typing or panel resizing does not write every frame. App save/exit still forces a final write.
- Settings/About expose storage root, version folder, config/settings/UI-state/cache/data/export/log paths, schema version, cache status, and migration status. Path actions copy the path; destructive storage reset/clear actions are not implemented without confirmation.
- Default source policy is mock/local state with options such as Canonical, Issuer preferred, Market data preferred, Manual preferred, and specific-source persistence support.
- `src/ui_state.rs` contains a lightweight future `WorkspaceView`/`AppModel` sketch so current global settings remain separable from future per-view state such as page, navigation, selection, active plot, and table namespace.
- The UI distinguishes active subject from selected subject. Active subject drives page-level actions such as `plot active`; selected row/cell/subject drives `copy selected`, `plot selected`, and row-specific context actions.

## Status And Source Tags

- Status/source tags remain compact and visible in tables, context, inspector, Settings, and the `legend` command.
- Common tags include `FRESH`, `STALE`, `PENDING`, `FAILED`, `MOCK`, `SEED`, `EST`, `MANUAL`, `CONFLICT`, `AMBIG`, `MISSING`, `QUEUED`, `RUNNING`, `DONE`, and `SRC: ...`.
- Source selection is explicit where mock data has more than one source. If a requested source is unavailable, the UI falls back to canonical/default source and shows the fallback status instead of hiding provenance.
- Mock source availability is subject- and data-kind aware for price, NAV, holdings, distributions, facts, documents, FX, and derived values. Normal fallback is not treated as an error; missing source availability is the warning case.
- Analytics includes a compact Diagnostics section for fresh/stale/missing/suspicious rows, manual overrides, estimated/derived values, failed jobs, ambiguous instruments, mock/seed rows, and source conflicts.

## Data Operations

- Data Operations is the Market Data Readiness workspace. It shows readiness stages, next recommended actions, market-data plan items, scheduler/due jobs, source budgets, fetch logs, constituent identity/price coverage, blocking diagnostics, and compact endpoint availability/count rows.
- The page uses `DashboardSnapshot::data_operations` and `DataProvider::load_data_operations`; page rendering does not call HTTP or a database.
- Mock mode is the safe default. API mode uses `http://localhost:8080/api/v1` by default, enforces a configurable per-request timeout, and can auto-refresh when Data Operations opens.
- REST calls run on background threads. A request generation id discards superseded responses; successful endpoint sections replace fallback rows while failed sections retain previous/mock rows.
- Provenance states are explicit: `MOCK`, `API`, `PARTIAL API`, `STALE API`, and `API ERROR`. Failed sections and the last sanitized error remain visible in the page and Settings.
- Hydration currently requests scheduler status/due jobs, source budgets/fetch logs, workspace market-data plan/dashboard/diagnostics/constituent exposure, job timeline/running/failures, onboarding status/runs, broker imports, transactions, and positions. Job runs populate the existing Jobs/Data Operations scheduler model; newer endpoint families are summarized in the API-sections table.
- Actions such as `Run now`, `Run once`, and copied worker commands are local/mock UI actions. They update feedback or clipboard text only.
- Fetch-log request keys, API errors, diagnostic details, and copied fetch-log errors mask API keys, tokens, authorization/bearer values, passwords, secrets, and URL userinfo. Backend URLs containing credentials, queries, or fragments are not persisted.
- Inspector follow/pin supports readiness stages, market-data plan items, source budgets, fetch logs, constituent coverage rows, and diagnostics.

## Workstation UI Philosophy

The GUI should feel like a compact personal financial analysis terminal: dense, source-aware, keyboard-friendly, table-first, flexible under resizing, and built for investigation. Menu and command actions may be mock/no-op while the backend is unstable, but the app should always make data mode, status, freshness, and source/provenance visible.

Top-row layout is intentionally sparse: navigation and command/search live in the command row; active/selected context lives in the context strip and inspector; operational status belongs in the status bar. Shell sizing uses `src/ui/metrics.rs` for common spacing, row heights, panel widths, and breakpoints rather than scattering new magic numbers.

The current fidelity pass adds shared workstation frames, a command-row workspace selector, grouped rail navigation with operational counts, reusable page headers, full-row selection on priority tables, a distinct focused chart cell, scrollable follow/pin inspector hierarchy, and consistent loading/empty/API-error panels. See [docs/ui/figma_fidelity_audit.md](docs/ui/figma_fidelity_audit.md) for the implementation matrix and deferred prototype differences.

The inspector is resizable, collapsible, and persisted. Its width is clamped against the available shell width; `Window` provides Show Inspector, Hide Inspector, and Reset Layout. Hiding the panel does not discard a pinned inspector context, and the context/status strips expose hidden/pinned state.

Priority workstation tables distinguish selected row, focused row, focused cell, and active subject. Arrow keys move focus, Enter opens natural row targets, Escape clears table focus, and copy resolves in this order: focused cell, focused row, selected row, then page/active summary. Coverage includes Portfolio, ETFs, Exposure, Fund Detail listings/holdings/distributions/documents, Jobs, Alerts, Documents, and Data Operations plan/scheduler/budget/log/constituent/diagnostic/API-section tables. Persistent column layouts cover Portfolio, ETFs, Exposure, Alerts, Documents, Fund Detail listings/holdings, and the descriptor-backed Data Operations tables.

## Architecture

- `src/app.rs`: `eframe::App` shell, menu/command/status bars, navigation, page routing, and explicit page state.
- `src/app_info.rs`: app name, package version, alpha/mock status, and native title helper.
- `src/app_model.rs`: central `DashboardSnapshot`, `LoadState`, refresh status, selected fund/listing context, investable tree, and mock time-series data.
- `src/domain.rs`: GUI/domain-facing data structures, `AnalysisSubject`, investable hierarchy nodes, dependency/value trace models, and future PnL/projection data shapes.
- `src/navigation.rs`: drilldown stack, Back/Forward/Home behavior, and breadcrumb entries.
- `src/charts.rs`: plot request and chart panel state models.
- `src/compute/charts.rs`: chart value transforms and matching-date spread derivation.
- `src/compute/diagnostics.rs`: data-quality aggregation for the Analytics diagnostics panel.
- `src/compute/data_operations.rs`: pure readiness/action derivation, constituent status mapping, and fetch-log secret masking for Data Operations.
- `src/timeseries.rs`: time-series domain model and lookup helpers.
- `src/api/`: `DataProvider` boundary, configured mock/API provider, blocking `ureq` client used only by workers, tolerant Data Operations response mapping, endpoint construction, and secret masking.
- `src/command.rs`: testable command matching, command outcomes, and session command history.
- `src/debounce.rs`: lightweight debounced value helper for search and settings persistence.
- `src/filter.rs`: shared case-insensitive filter matching helpers.
- `src/mock_data.rs`: static mock data and mock instrument resolver.
- `src/pages/`: immediate-mode page rendering only, including the Data Operations readiness workspace.
- `src/source.rs`: source-selection, source-policy, and fallback-resolution helpers.
- `src/storage/`: app data path resolution, versioned storage layout, JSON manifest/settings/UI state, cache policy scaffolding, and migration planning.
- `src/ui/`: small UI helpers for key/value grids, validated date input, and layout metrics.
- `src/compute/`: non-UI compute stubs for portfolio totals, exposure aggregation, NAV, regressions, curves, and diffs.
- `src/format.rs`: formatting helpers shared by pages and tests.
- `src/ui_state.rs`: explicit layout/panel visibility state and future single-view/tab-state sketch.

## Still Mock-Only Areas

- HTTP hydration is limited to Data Operations. Portfolio, funds, charts, documents, alerts, search, and analytical inputs remain mock/local.
- Mock mode remains fully offline and does not require a backend.
- Portfolio hierarchy and chart series remain local/mock data. They are shaped for future provider hydration but do not call a backend.
- Chart rebasing and percent-change modes transform existing mock time-series points only; they do not fetch market data or compute real total returns.
- Add Instrument uses a mock resolver for resolved, ambiguous, not-found, and pending-backfill cases.
- Job `Run now` and menu-triggered job actions only record local mock UI feedback.
- Analytics, Curves, Compare, Spreads, and Changes use deterministic mock calculations or placeholders.
- Menu actions for imports, exports, data refreshes, explanations, and job triggers update mock status text only.
- Auth settings remain placeholders. Bearer-token auth is not implemented and no secret field is stored.

## GUI Data Contract / Future API Expectations

Pages receive data from `DashboardSnapshot` and do not construct mock data directly. The `DataProvider` trait defines the current read/resolve surface:

- workspaces
- funds and listings
- portfolio summary and positions
- prices and other time series
- NAV time series
- distributions
- holdings and exposure
- documents
- scheduled jobs and job runs
- data operations / market data readiness
- alerts
- FX rates, eventually
- manual overrides, eventually
- instrument resolution

The current configured provider hydrates only `DashboardSnapshot::data_operations` off the UI thread and delegates all other reads to `MockDataProvider`. Future slices can expand the same boundary without moving endpoint knowledge into pages.

`DataRequest` and `DataResponse` outline the future worker boundary for loading dashboards, refreshing all data, resolving instruments, running jobs, and loading fund detail. Real network/database work should happen outside the egui render loop and flow back into app state through that boundary.

The current data-flow rule is:

```text
raw provider data + local overrides -> effective data -> compute module -> derived/explainable result -> UI view
```

Manual overrides are replayed after mock refresh and before portfolio hierarchy or analytics views consume positions.

Backend/API work should preserve these GUI-side concepts:

- Fund identity and listing identity are distinct; ticker is not identity.
- One fund may have many listings with different tickers, currencies, exchanges, FIGIs, and SEDOLs.
- A portfolio may contain fund listings now and may later contain funds, cash, synthetic models, or other portfolios.
- Time series should carry subject, kind, unit, source, status, and point-level source/status.
- Displayed values may be raw, manual, derived, or estimated.
- Local overrides are currently GUI-only; future sync must not assume every displayed value is backend-owned.
- Freshness fields and last-refreshed timestamps must survive provider hydration.
- Source/provenance tags such as `SRC: seed`, `SRC: mock`, `SRC: issuer`, `SRC: stooq`, `SRC: manual`, and `SRC: derived` are part of the workstation contract, not decorative text.

## Compute / Analytics Direction

`src/compute/` is the home for non-UI calculations. It currently contains lightweight mock/simple logic for portfolio totals, nested investable/effective values, dependency traces, exposure aggregation, NAV, regressions, curves, and diffs. Future pricing, re-pricing, NAV, curve, regression, PnL explain, projection/what-if, and 1-to-1 spread engines should expand there or behind similarly isolated modules, not inside egui page rendering.
