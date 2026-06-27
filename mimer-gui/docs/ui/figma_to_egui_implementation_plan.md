# Figma-To-egui Implementation Plan

## 1. Screens Found

- Shell: menu bar, command row, context strip, left navigation rail, right inspector, status bar.
- Core tables: Portfolio, ETFs, Holdings, Dividends, Documents, Alerts, Jobs, Search.
- Drilldowns and analytical views: Fund Detail, Document Viewer, Charts, Compare, Spreads, Changes, Analytics, Hierarchy, Exposure, Curves, Settings.
- Reference docs cover active subject, navigation, state badges, source fallback, overrides, alerts, jobs, documents, and chart/compare/spread flows.

## 2. UX / State-Flow Ideas

- Single click selects a row and updates inspector context; double click opens and makes the row the active subject through the navigation stack.
- Active subject drives the context strip and default commands; selected row/cell drives inspector detail and copy/edit actions.
- Inspector follows selected row first and falls back to active subject when no row is selected.
- Source, freshness, mock, manual, derived, estimated, missing, failed, queued, and running states must be visible as compact tags, not buried in debug text.
- Alerts/jobs/documents should stay table-first, with row actions and in-app drilldowns rather than primary modals.

## 3. Component Mapping To egui

- Figma shell -> `TopBottomPanel`/`SidePanel`/`CentralPanel`.
- Web cards -> restrained `Frame`/section helpers only where framing adds structure.
- Web badge -> small `RichText` label inside an egui `Frame`.
- HTML table -> `egui_extras::TableBuilder`; metadata -> `egui::Grid`.
- Drawer/inspector -> right `SidePanel`; document viewer -> navigation page.
- Command palette/temporary help -> egui `Window`.
- Chart controls and overlays -> explicit chart/page state plus `egui_plot`.

## 4. Completed Foundation Slice

- Add a reusable egui style/badge layer under `src/ui/`.
- Apply consistent source/status/freshness badges to shell, inspector, and high-traffic tables.
- Make the context strip clearer about active subject versus selected row.
- Improve table status/source affordances without changing data models or backend boundaries.
- Document the selected implementation slice for future agents.

## 5. Inspector Follow/Pin Slice

- Added an explicit inspector state model with `FollowSelection` and `Pinned` modes.
- Follow mode is the default: selected row/cell context wins; if nothing page-specific is selected, the inspector falls back to the active subject.
- Pinned mode stores an owned `InspectorContext` snapshot. Later row selections do not replace the panel until the user unpins.
- The inspector header shows `FOLLOWING SELECTION` or `PINNED: <kind>`, with tooltips explaining why the panel is or is not changing.
- Pinned contexts can be opened where an in-app target exists: fund/listing subjects open Fund Detail, documents open Document Viewer, alerts open the affected subject, and jobs reselect their Jobs page row.
- The context strip remains compact: it shows ACTIVE, SELECTED, PAGE, and only shows PINNED when the inspector is pinned.

## 6. Row Actions And Interaction Rules

- Alerts now expose row/context/inspector actions for Open affected, Mark read, Dismiss, Resolve, Run related mock job, Copy alert, and diagnostics/source inspection.
- Jobs now expose row/context/inspector actions for Run now, Open latest/detail placeholder, Run similar, Copy job/run, diagnostics, source, last/next run, and record counters.
- Documents now expose row/context/inspector actions for Open Viewer, Open related Fund, Copy URL/hash/metadata, Show History, and Compare Previous metadata/hash placeholder.
- Single click selects rows and updates the inspector only while following selection.
- Double click opens where practical: documents open the in-app viewer; alerts open the affected subject when known; jobs show local detail feedback; existing Portfolio/ETF open behavior is preserved.
- Right-click menus mirror the visible row actions for Alerts, Jobs, and Documents.
- Commands added or refined: `pin inspector`, `unpin inspector`, `open pinned`, `dismiss selected alert`, `resolve selected alert`, and `run selected job`.

## 7. Unified Chart Workspace Slice

- `src/charts.rs` now carries an explicit chart workspace model: `ChartMode`, `ChartSeriesSpec`, `ChartSeriesId`, `ChartPointSelection`, spread summaries, selected series, selected point, source selection, range, overlays, and comparison/spread subject state.
- Charts is the primary analytical workspace. Legacy Compare and Spreads routes seed `ChartMode::Compare` or `ChartMode::Spread` and render the Charts page instead of maintaining separate primary workspaces.
- Chart modes are `Single`, `Overlay`, `Compare`, and `Spread`. Compare uses A/B overlaid series plus a compact summary table. Spread uses A - B with a derived series from matching dates only.
- Chart controls now expose mode selection, active chart subject, range, source fallback, primary subject, B subject for compare/spread, overlay count/list, selected series/point badges, open/copy/reset actions, and selected point/series copy actions.
- The chart data table uses the unified series set and includes date, series, subject, value, unit, source, status, and kind. Selecting a row stores a chart point selection; double-click opens the related subject; context menus provide copy/open/source/pin actions.
- Inspector follow/pin supports chart workspace, chart series, chart point, and spread derivation contexts without making selected series the active subject.
- Commands route through chart state: `plot active`, `plot selected`, `plot VUSA`, `overlay ISF`, `add overlay ISF`, `clear overlays`, `compare VUSA ISF`, `plot VUSA vs ISF`, `spread VUSA JEPG`, `plot spread VUSA JEPG`, `swap comparison`, `chart mode ...`, `copy chart`, `copy selected point`, and `open chart subject`.
- Spread derivation is local/mock and intentionally conservative: only matching dates are subtracted, unmatched dates are not fabricated, and the UI marks the series as `DERIVED`, `PARTIAL`, or `MISSING` as applicable.

## 8. Chart Focus And Value-Mode Slice

- Added explicit chart table focus: focused row, focused column, selected chart point, selected series, and active subject remain separate.
- Chart table clicks select point/series and focus the clicked cell. Arrow up/down moves chart rows, arrow left/right moves chart cells, and Enter opens the focused row subject.
- `Ctrl/Cmd+C` and copy commands now prioritize focused chart cell, selected chart point/row, selected series, then chart workspace summary.
- Chart point inspector detail shows displayed value, raw value, value mode, source, status, and row/value/source copy actions.
- Series chips/list entries expose select, open, copy label, copy all series data, pin inspector, remove overlay, and make-primary actions where applicable.
- Compare/overlay charts support `Raw`, `Rebased 100`, and `% Change` display modes. Rebased/percent modes use the first valid common base point and retain raw values in table/copy payloads.
- Spread charts remain raw derived A - B and visibly label transformed spread display as deferred.
- Compare summaries now show latest common values, A - B, percent difference, raw common values, common-date coverage, source/status, value mode, and base date.
- Spread summaries show expression, latest spread, coverage, A/B sources, derived status, and actions for copy/swap/compare/open.
- Commands added or refined: `chart value raw`, `chart value rebased 100`, `chart value percent`, `copy chart row`, `copy chart value`, `copy selected series`, `clear chart selection`, `select next point`, and `select previous point`.

## 9. Chart Plot Interaction Slice

- Plot hover now finds the nearest visible point in the current chart value mode and exposes a compact readout with series, subject, date, displayed value, raw value when transformed, source, status, and value mode.
- Plot click selects the nearest point when it is within the pick threshold, selects the related series, and maps table focus back to the matching chart row when available. It does not make the subject active and does not navigate.
- Selected and hovered points draw lightweight guide markers with `egui_plot` points and vertical guide lines. `egui_plot` crosshair display is enabled for plot-cursor orientation.
- The pure nearest-point helper ignores invalid/missing values, respects raw/rebased/percent display values, works for derived spread series, and keeps stale selections from surviving range/value-mode changes.
- Series and point cycling are available through commands (`select next series`, `select previous series`, `select next point`, `select previous point`) and chart shortcuts (Alt/Option+Up/Down for series, Alt/Option+Left/Right for points).
- The plot context menu exposes copy/open/pin/clear/reset/value-mode actions only when they are meaningful. Hover value copy is available from the plot menu; keyboard copy remains table-first.
- The top menu bar follows the workstation desktop model: `File`, `Edit`, `View`, `Navigate`, `Data`, `Tools`, `Window`, and `Help`, with domain actions grouped under those conventional menus.

## 10. Date-Axis Plotting Slice

- `ChartDateAxis` maps ISO date strings to days since the Unix epoch and formats dates back for ticks, hover coordinates, and copy/readout context.
- Chart workspace plot lines, selected markers, hover markers, nearest-point picking, and the Fund Detail inline Price/NAV plot now use real date x-values instead of point indexes.
- Calendar gaps are preserved. Missing weekends, holidays, and sparse series show as horizontal gaps; no dates are fabricated.
- X-axis tick labels use egui_plot's formatter: short ranges show day/month, medium ranges show month/year, and long ranges show years.
- Range controls are date-window filters (`1M`, `3M`, `6M`, `1Y`, `All`) based on the latest valid point date, not last-N row counts.
- Plot hover readout includes the cursor date and nearest point date. Single-click selects the nearest point without changing the active subject; double-click near a point opens that point's subject.
- Plot/table sync remains keyed by series id plus date, with value-mode validity checks clearing stale selected points.
- Chart point, row, series, and workspace copy payloads now identify `x_mode=date` where applicable.

## 11. Data Operations / Market Data Readiness Slice

- Added a dedicated `Data Operations` workspace for readiness, plan, scheduler, source budget, fetch log, constituent coverage, and blocking diagnostics workflows.
- The page is table-first through `DashboardSnapshot::data_operations`, with mock fixtures as the default/fallback.
- Readiness stages summarize Holdings, Identity, Prices, FX, Exposure, Performance, Alerts, Jobs, and Sources with compact status/source/freshness badges.
- Next recommended actions are derived from actionable plan items and diagnostics; mock run/copy actions do not pretend to call a backend.
- Market-data plan, source budgets, fetch logs, constituent coverage, and diagnostics have sortable/filterable tables, row selection, context copy, and inspector follow/pin contexts.
- Fetch-log request keys are masked for secret-like fragments before display or copy.
- The top menu keeps the workstation model `File`, `Edit`, `View`, `Navigate`, `Data`, `Tools`, `Window`, `Help`; Data Operations is under `Data` and the left rail.
- Commands added include `data operations`, `market data plan`, `source budgets`, `fetch logs`, and copy-only mock worker commands.
- See [data_operations_model.md](./data_operations_model.md) for the mock/API boundary and endpoint mapping.

## 12. Data Operations REST Hydration Slice

- Added persisted Mock/API mode, backend base URL, per-request timeout, workspace header, and Data Operations auto-refresh.
- Added a configured provider that delegates the rest of the app to `MockDataProvider` and hydrates only Data Operations through REST.
- Blocking `ureq` requests run exclusively on background workers; no Tokio runtime was added.
- Refreshes carry generation ids so superseded responses cannot overwrite newer state.
- Endpoint results merge section-by-section into previous/mock data. The UI exposes `API`, `PARTIAL API`, `STALE API`, and `API ERROR` without hiding successful sections.
- Scheduler due jobs and job timeline/running/failure data map into the existing scheduler/job-run models.
- Onboarding, broker import, transaction, and position endpoints are represented as compact API-section availability/count rows.
- API errors and fetch-log payloads are sanitized; secret-bearing backend URLs are rejected before persistence.

## 13. Deferred Backend/API Actions

- Full alert resolution workflow with real Fix/Run Job/Override effects.
- Vim-like table traversal beyond arrow-key chart focus.
- PDF rendering/OCR and real external document opening.
- Full-app backend hydration beyond Data Operations, real source-selection mutations, WebSockets, auth, or async runtime changes.
- Detailed onboarding-run, broker-import, transaction, and position tables; current hydration exposes endpoint availability/counts until contracts stabilize.
- Real pricing, total return, currency conversion, FX-aware spread analytics, and transformed spread outputs beyond available mock time-series points.
- Drag/brush selection and richer chart gestures.

## 14. Risks / Ambiguities

- Prototype uses web layout and CSS colors; egui translation should preserve density and hierarchy, not pixel-perfect styling.
- Some statuses are currently plain strings, so helper mapping must be tolerant and conservative.
- Source fallback data is mock/local and availability-aware only for current modeled subjects and data kinds.
- Applying badges too broadly can reduce table density; use compact monospace tags and tooltips.
- Alert/job/document actions are local/mock only; they prepare the UI contract but do not call a scheduler, HTTP API, PDF renderer, or OCR engine.
- The prototype includes richer chart gestures and visual polish than this egui slice implements; current egui work prioritizes explicit state, source visibility, date-accurate plotting, and predictable navigation over pixel-perfect interaction.

## 15. Figma Fidelity / Workstation Polish Slice

- Added `docs/ui/figma_fidelity_audit.md` as the implementation matrix for shell, page, table, inspector, state, density, and priority-page gaps.
- Shell bars now use shared workstation frames and tighter metrics. The command row includes a compact workspace selector; the left rail is grouped and exposes Alerts, Jobs, and Data Operations counts.
- Added a reusable page-header pattern and applied it to Data Operations, Charts, Portfolio, Fund Detail, Jobs, Alerts, Documents, and Settings.
- Priority tables now share header height/text styling and use full-row selected state. Charts retains a distinct focused-cell highlight and copy priority.
- Inspector content is scrollable and presents follow/pin mode, context type, title, and empty state more clearly without changing owned pinned contexts.
- Shared state presentation now covers `MOCK`, `API`, `PARTIAL API`, `STALE API`, `API ERROR`, `LOADING`, `EMPTY`, `FAILED`, `MISSING`, `DERIVED`, `MANUAL`, and `FIXTURE`.
- Data Operations shows partial/API failures as a compact operational state panel and strengthens selected readiness/table rows.
- Settings uses the same API/error vocabulary as Data Operations while preserving the existing background worker and secret-bearing URL rejection.

Deferred: narrow-window bottom inspector, focused cells for remaining secondary tables, persistent table columns, icon/collapsed rail, chart brush gestures, document preview split, and page-header migration for lower-priority pages.

## 16. Split-Pane And Focused-Cell Interaction Slice

- `LayoutState` now owns inspector width, visibility, and a layout revision used to reset egui panel sizing safely. Width is clamped against available shell width so the main work area remains the priority.
- Inspector width and visibility persist in UI-state JSON. `Window` exposes Show Inspector, Hide Inspector, and Reset Layout; the inspector header can hide itself while retaining pinned context.
- Context/status strips expose `FOLLOW`, `PINNED`, `HIDDEN`, and `HIDDEN/PINNED` inspector state without duplicating full inspector content.
- `TableState` now separates selected row from focused row and optional focused column. Clearing focus does not clear the active subject or selected row.
- Priority tables use a focused-row overline and subtle focused-cell fill. Arrow up/down moves rows, left/right moves modeled columns, Enter opens natural targets, and Escape clears table focus.
- Copy priority is focused cell, focused row, selected row, then page/active summary. Existing Charts behavior remains intact.
- Focused-cell coverage: Portfolio; Fund Detail Holdings/Distributions/Documents; Jobs scheduled/runs; Alerts; Documents; Data Operations Market Data Plan/Source Budgets/Fetch Logs/Diagnostics.
- Added commands/aliases: `reset layout`, `clear table focus`, `copy focused cell`, `copy focused row`, and `open focused row`. Existing `toggle inspector`, `pin inspector`, and `unpin inspector` remain.
- Table focus drives inspector follow context. Pinned inspector context remains owned and unchanged while selection/focus moves.

Deferred: true bottom inspector mode, persistent table column widths/order/visibility, focused cells for every secondary table, collapsed icon rail, chart brushing, and document preview split.

## 17. Persistent Table Layout And Remaining Focus Slice

- Added stable table ids, stable column keys, descriptor defaults, min/max width clamping, visibility state, fixed order state, and serialized table-layout preferences.
- Managed tables use explicit widths rather than relying on inaccessible `egui_extras::TableBuilder` drag-width state. Width changes are available in the compact Columns menu and survive restart.
- Per-table controls provide hide/show, hide focused column, narrower/wider focused column, show all, reset, and copy visible row. `View` and command actions provide global show-all/reset.
- Persistent layout coverage includes Portfolio positions, ETFs, Exposure breakdowns/diagnostics, Alerts, Documents, Fund Detail listings/holdings, and Data Operations plan/scheduler/source budgets/fetch logs/constituents/API sections.
- New focused-cell coverage includes ETFs, all Exposure tables, Data Operations scheduler/constituents/API sections, and Fund Detail listings. Existing focus behavior remains for Portfolio, Charts, Jobs, Alerts, Documents, and Fund Detail holdings/distributions/documents.
- Copy priority remains focused cell, focused row, selected row, then page/active summary. Copy-visible-row omits hidden columns.
- Focus updates inspector follow contexts; pinned contexts remain owned and unchanged.
- Layout persistence excludes volatile row selections, focused rows/cells, filters, and active subjects.

Deferred: drag-to-reorder, persistence of native egui drag-resize state, Jobs/chart/remaining Fund Detail descriptor migration, document preview split, narrow bottom inspector, collapsed icon rail, and chart brushing.
