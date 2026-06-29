# Agent Notes

This is a Rust `egui`/`eframe` GUI project. Keep the interface dense, practical, and workstation-like.

## Working Rules

- Search local egui examples/docs before using unfamiliar egui APIs:
  - `references/egui/examples`
  - `target/doc`
- Keep rendering immediate-mode. Do not store widget objects as state.
- Keep UI state explicit in structs such as `MimerApp`, page state structs, and `DashboardSnapshot`.
- Keep financial/domain logic out of page rendering. Put reusable calculations under `src/compute/`.
- Keep API/client-facing structs and the data boundary separate from pages.
- Use `AnalysisSubject`/investable-style abstractions for open, plot, compare, and drilldown workflows, but do not make every domain object literally the same type too early.
- Do not use modals for primary drilldown navigation. Use the navigation stack, Back/Forward/Home, and breadcrumbs for deep inspection.
- Keep chart data, time-series lookup, and `PlotRequest` construction separate from chart rendering.
- Use `AnalysisSubject`, `PlotRequest`, and `NavigationStack` for open, plot, compare, and drilldown workflows rather than ad hoc page jumps.
- Keep plot history, selected chart subject, overlays, and chart options in explicit state; do not store egui widgets.
- Keep analytical engines in `src/compute/`; future PnL explain and projection modules should consume effective data, not raw UI table values.
- Do not block the UI thread with network, database, or expensive compute work.
- Keep UI responsive; do not block the egui render loop.
- Keep API loading behind `DataProvider` or request/response worker boundaries.
- Future backend hydration should update `DashboardSnapshot` through app state, not page modules.
- Do not add backend code in GUI polish tasks.
- Do not connect directly to Postgres from the GUI.
- Do not add auth, WebSockets, or async runtimes unless the task explicitly requires it.
- Do not scatter filesystem paths across the app. App data, config, cache, data, export, log, and tmp paths must go through `src/storage/`.
- Do not store secrets in config or UI-state JSON. API/auth settings in the GUI are placeholders unless explicitly changed by a backend task.
- Prefer `egui_extras::TableBuilder` for dense tables when available.
- Prefer tables for scan/sort/select row-and-column data. Use label/cell blocks only for true summaries, short status panels, and form-like settings.
- Use `egui::Grid` for compact key/value facts, settings forms, inspector facts, source/provenance summaries, document metadata, selected subject details, and override detail rows. Ensure grid key/value columns have enough width; use truncation plus full-value tooltips/copy actions for long values.
- Avoid glossy SaaS styling, animation, gradients, decorative cards, and large padding.
- Preserve the workstation shell: menu bar, command/search, context strip, module rail, main work area, optional inspector, and status bar.
- Keep command/search roomy. Put mode, API, freshness, source policy, and other operational status in the status bar instead of duplicating them in the header.
- Keep the command/search row spacious. Back/Forward/Home can be compact; status belongs in the status bar, and active/selected context belongs in the context strip or inspector.
- Keep status/source/provenance visible for financial data. Prefer compact tags such as `FRESH`, `STALE`, `MOCK`, `SEED`, `SRC: issuer`, and `SRC: mock`.
- Keep selected context concise in the strip, preferring ticker, then ISIN, then short label. Full selected-subject detail belongs in hover text and/or the inspector.
- Theme and density changes must visibly apply to the egui `Context`; avoid settings controls that appear to change but do nothing.
- Keep global settings/state and per-view state separable for future tabs. Per-view state includes page, navigation, selection, active plot, filters/sorts, and table namespace.
- Do not confuse change/diff history with 1-to-1 spreads. Use `Changes` for document/entity changes and `Spreads` for left-minus-right asset or portfolio relationships.
- Support source selection/fallback without hiding provenance. If a chosen source is unavailable, fall back visibly to the canonical/default source.
- Source selectors must be availability-aware by subject and data kind. Normal fallback to an available canonical/default source is not an error; only no available source should look like a warning.
- Most relevant tables should be sortable with clear asc/desc indicators and stable text/numeric/date-like ordering.
- Avoid brittle hard-coded widths/heights. Prefer `available_width`, `available_size`, remainder table columns, flexible central-panel layouts, clean wrapping, and `src/ui/metrics.rs` constants where practical.
- Side panels should be resizable where useful. The inspector needs sensible min/default/max widths and should not make central content unusable.
- Tooltips should explain compact tags and show full values for truncated/compact labels, especially selected subjects, breadcrumbs, sources, manual overrides, derived values, editable cells, chart series, document hashes/URLs, job status, spread fields, settings controls, and zoom controls.
- Support clipboard copy and context-menu copy for important table rows, subjects, documents, chart labels, IDs, ISINs, URLs, hashes, and row summaries. Keyboard copy should prefer selected cell, then selected row/subject, then active subject.
- Use cursor changes where they clarify behavior: pointing hand for clickable/openable rows, text/cell cursor for editable table cells, crosshair for charts when appropriate, and help/copy cursors for compact tooltip/copy affordances.
- Document rows should open an in-app viewer on double-click. External viewers are secondary actions only.
- Important rows should have context menus or compact equivalent row actions, especially Portfolio, ETFs/Funds, Documents, and Jobs.
- Command/search should remain useful for power users; extend it with simple mock commands before adding heavyweight UI.
- Keep command parsing and command history testable outside egui rendering.
- Keep Search backend-ready but mock/local until explicitly asked to wire a real backend. Search should remain debounced and should route open/plot/copy/drilldown through `AnalysisSubject`, `PlotRequest`, and `NavigationStack`.
- Keep table filters simple and page-local unless a reusable abstraction emerges naturally.
- Preserve the distinction between active subject and selected subject. Active subject drives page-level actions; selected row/cell/subject drives selected actions and clipboard payloads.
- Preserve local manual overrides through refresh; replay overrides into effective data before hierarchy, chart, or analytics views consume positions.
- Keep manual overrides local unless explicitly asked otherwise.
- Avoid writing settings or UI state every frame. Use debounce for search text, settings persistence, config text fields, source search fields, and future expensive filters/requests.
- Cache can be rebuilt; user-created data must be protected. Cache refresh must not erase local overrides, watchlists, saved views, pinned instruments, or future baskets.
- Version migrations must be explicit. Copy safe config/user data forward intentionally; do not blindly copy incompatible cache between version folders.
- Future services/API work must preserve source/status/provenance and freshness fields.
- Backend work must preserve the fund/listing distinction; ticker is not identity.
- Time series provided by services should include subject, kind, unit, source, status, and point-level provenance.
- Zoom changes must visibly apply to the egui `Context`, be clamped to a reasonable range, and stay reachable through menu, shortcut, command, and settings controls.

## Descriptor-backed tables

- Keep stable table IDs, column keys, descriptor order, focus indices, rendered column order, and copy payload keys aligned.
- Reuse `src/ui/table_layout.rs` for descriptor-backed table controls and `src/ui/documents.rs` for document metadata/preview UI.
- When table columns are hidden, keyboard navigation must traverse visible descriptors and copy-visible-row must omit hidden values.
- Prefer a focused page submodule or reusable UI component when adding a major section to a page already above roughly 1,000 lines. Do not grow page files into catch-all command, state, preview, and rendering containers.
- Keep source, status, freshness, and provenance naming aligned with service schemas, but do not invent GUI fields when the current `DashboardSnapshot` contract does not expose them.
- Do not add full PDF rendering or backend behavior to solve a metadata-preview task.

## Verification

For visual verification, inspection is optional and development-only. Normal app startup must remain independent of inspection and MCP. When a native display and the `egui` MCP server are available, launch the inspectable build from `mimer-gui/` with:

```bash
EGUI_INSPECTION=1 cargo run --features inspection
```

The expected MCP configuration uses server name `egui` and command `/Users/ludwigjonsson/.cargo/bin/egui-mcp`; eframe listens on the default inspection port, expected to be `5719`. Install the executable only if it is missing:

```bash
cargo install --git https://github.com/rerun-io/kittest_inspector egui_mcp
```

Use inspection to verify the shell and affected workflows where possible, but keep compile/tests independent of it and report honestly when GUI launch or MCP connectivity is unavailable.

Run these before handing off changes:

```bash
cargo fmt
cargo check
cargo clippy
cargo test
```

Fix compiler errors and reasonable clippy warnings. If a check cannot be run, state why.

## Current Boundary

Mock data is provided by `MockDataProvider`. Future API integration should plug in behind `DataProvider` and hydrate `DashboardSnapshot`; page modules should remain mostly unaware of HTTP endpoints.
