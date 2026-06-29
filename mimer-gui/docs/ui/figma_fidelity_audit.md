# Figma Fidelity Audit

Implementation-facing audit against `references/mimer-figma`, especially `AppShell.tsx`, the shell components, shared `DataTable`/`StatusTag`, page prototypes, and the imported Mimer UX specifications.

| Area | Figma/reference intent | Current egui state | Gap | Fix in this slice | Deferred |
|---|---|---|---|---|---|
| App shell | Layered desktop bars around a dense work area | All shell regions existed | Regions looked visually unrelated | Shared shell frames, tighter dimensions, consistent content margin | Narrow-window bottom inspector |
| Top menu | Conventional desktop menu model | Correct eight top-level menus | Bar treatment was generic | Preserved labels and added shell frame | Native OS menu integration |
| Command/search row | Roomy command field with compact navigation and workspace context | Command field and navigation existed | Workspace selector lived mainly in Settings | Added compact workspace selector without moving status into header | Rich command suggestions |
| Workspace selector | Persistent, compact workspace context | Settings-only control | Weak shell visibility | Added `WS:` selector to command row | Multi-view/tab workspace model |
| Left navigation rail | Grouped, compact modules with selected state and counts | Flat list of 20 pages | Weak hierarchy and no operational counts | Grouped Workspace/Analysis/Data/Tools; Alerts/Jobs/Data Ops counts | Icon strategy and collapsed icon rail |
| Context strip / active subject | Breadcrumb, active, selected, page, pinned context | Already explicit | Styling differed from adjacent bars | Dedicated context-strip frame retained active/selected distinction | Clickable breadcrumb segments |
| Main page header pattern | Title, context, status/source, refresh/action affordances | Mostly overloaded title strings | Inconsistent hierarchy and metadata placement | Shared `page_header`; applied to eight priority pages | Remaining secondary pages |
| Right inspector | Persistent panel with follow/pin, type, title, facts, actions | Functional follow/pin contexts | Width/collapse state was not app-owned | Persisted responsive width, Show/Hide/Reset Layout controls, hidden/pinned status, retained pinned context | Narrow bottom inspector |
| Bottom status bar | Dense operational state only | Good operational content | Generic frame; one duplicated policy label existed historically | Dedicated status frame and compact height | Adaptive overflow on very narrow windows |
| Tables | 20–24 px rows, muted headers, full-row selection, clear focus | TableBuilder used widely | Focus/copy behavior differed by page | Reusable row/cell focus cursor, visible-column navigation, persistent descriptors and cell-first copy on priority tables | Remaining low-value secondary tables and drag ordering |
| Badges/status tags | Compact bordered source/status tags | Strong existing badge system | API/empty/loading vocabulary incomplete | Added API, partial, stale API, API error, loading, empty mappings/tooltips | Confidence scoring |
| Cards/summary strips | Restrained summary strips, not SaaS cards | Mixed grids and grouped frames | Data Ops selection was subtle | Selected readiness stages now use workstation selection fill | Portfolio summary strip redesign |
| Split panes | Resizable rail/inspector with usable center | Resizable side panels existed | Inspector width was not persisted or resettable | Responsive shell clamp plus resizable Documents table/metadata preview with a narrow stacked fallback | Persisted Documents split ratio and bottom inspector |
| Charts workspace | Controls, plot, readouts, data table, explicit focus | Functionally advanced | Header/focus/empty hierarchy inconsistent | Shared header, managed series-data descriptors, visible-column focus, copy-visible-row and empty range state | Brush/range gesture and richer plot/table split resizing |
| Data Operations workspace | Operational summary, partial failure visibility, dense tables | Functionally complete | Header and selected rows felt fragmented | Shared header, partial/API error panel, selected readiness fill, full-row selections | Collapsible sections and saved layout |
| Portfolio page | Summary strip plus dense selected positions table | Functional with overrides | Header overloaded; numeric scan alignment mixed | Shared header, full-row selection, numeric alignment, filtered empty state | Responsive summary strip composition |
| Fund Detail page | Subject-led header, tabs, facts, listings | Functional multi-section page | Generic title did not identify subject | Subject/source/status header plus managed listings, holdings, distributions and documents tables | Stronger tab styling, two-column overview and remaining secondary tables |
| Jobs/Alerts/Documents | Operational headers and table-first workflows | Functional pages | Inconsistent page identity and empty handling | Managed Jobs tables and responsive Documents table/metadata-preview split with reusable provenance UI | Linked job detail split and full document rendering |
| Settings page | Dense form with clear applied/pending/API state | Functional API and storage settings | API errors appeared as plain labels | Shared header and consistent failed/API-error panels | Section tabs and narrower responsive form |
| Empty/loading/error states | Explicit and consistent state vocabulary | Mixed plain labels and ad hoc errors | Weak cross-app consistency | Shared `state_message`; applied globally and to priority tables | Skeleton loading rows |
| Keyboard focus / selected rows | Active, selected row, focused cell are distinct | Model explicit in Charts only | Core tables lacked a shared focus cursor | Focused-row overline, subtle focused-cell fill, arrows/Enter/Esc, inspector sync | Vim traversal and all secondary tables |
| Density/spacing/typography | Compact neutral desktop workstation | Compact mode existed | Repeated magic header heights and generic bars | Shared metrics, muted table headers, tighter shell widths/heights | Custom bundled fonts and tabular-number font setup |

## Result

This slice improves shell hierarchy, page identity, selected-state clarity, API/error presentation, and table scanability without changing backend boundaries or navigation semantics.

## Split panes and table focus

| Area | Current state | Figma/reference intent | Gap | Fix in this slice | Deferred |
|---|---|---|---|---|---|
| Data Operations | Multiple linked operational tables with selection/inspector contexts | Keyboard-first plan/log/budget/diagnostic inspection | Only row selection and context copy | Focused cells for Market Data Plan, Source Budgets, Fetch Logs, Diagnostics; arrows, Enter for plan subjects, cell-first copy | Scheduler/actions/constituents remain row-oriented |
| Charts | Existing explicit chart row/cell focus | Plot and table focus remain distinct from active subject | Fixed columns ignored workstation layout state | Stable descriptors, persistent visibility/width, visible-column navigation and copy-visible-row | Brush/range gesture and plot/table ratio persistence |
| Portfolio | Selected positions and editable override cells | Dense spreadsheet-like movement and copy | Only editable cells had cell focus | All 13 columns participate in focus; row/column arrows, Enter open, focused cell/row copy | Column-width persistence |
| Fund Detail | Tabbed holdings, distributions, and documents | Selection should update inspector without replacing active fund | Detail tables were display-only | Focus/selection/copy on Holdings, Distributions, Documents; document/source opens stay separate | Listings/prices/jobs/diffs cell focus |
| Jobs | Scheduled jobs and run history with active-table state | Keyboard traversal should remain within the active table | Up/down only; copy was row-only | Cell focus for both tables, preserved active table, Enter detail, cell-first copy | Linked job-detail split |
| Alerts | Actionable alert table | Selected alert drives inspector; open affected is separate | Up/down did not carry focused-cell copy state | Ten-column focus, local checkbox actions retain focus, Enter opens affected subject | Real backend lifecycle mutations |
| Documents | Responsive list and in-app viewer | List selection, focused metadata, double-click viewer | Metadata required a separate viewer | Resizable wide table/preview split, narrow stacked preview, shared metadata component and pinned-inspector stability | Persist split ratio in app preferences; full PDF rendering remains a non-goal |
| Exposure | Read-only aggregate table | Scan-first analytical table | No explicit focus model | No change; lower value than operational tables in this slice | Focus/copy pass |
| Settings | Dense forms rather than row/column analysis | Forms keep native control focus | Table focus is not generally applicable | Layout persistence is reflected through app UI state | Settings table focus where future tables emerge |
| Inspector/layout | Resizable right side panel | Wide/right, medium/narrower, narrow/collapsible | Width and collapse were not durable | Responsive min/max clamp, persisted width and visibility, hidden/pinned status, header Hide, Window Show/Hide/Reset | Bottom mode under narrow breakpoint |

## Still visibly different from the prototype

- egui uses native immediate-mode layout rather than CSS sticky/flex composition.
- The inspector remains a right side panel at all desktop widths instead of moving below content at a narrow breakpoint; it now narrows and can be collapsed.
- Tabs and segmented controls remain egui selectable controls rather than web-style tab underlines.
- Managed tables persist explicit programmatic widths and visibility, but do not yet capture egui drag-resize or support drag reordering.
- The prototype's rich chart brush and icon rail remain deferred. The Documents preview is metadata-only because the GUI domain currently exposes a stable mock URI rather than service `title`/`url`/`language` fields or PDF bytes.

## Table column persistence and remaining focus coverage

`Yes` means the table uses stable descriptors and serialized layout state. `Partial` means focus or table behavior exists, but the table has not moved to the persistent descriptor path.

| Page/table | Descriptors | Focused row | Focused cell | Persistent width | Visibility/order controls | Inspector sync | Gap | Fix in this slice | Deferred |
|---|---:|---:|---:|---:|---|---:|---|---|---|
| Data Operations / Market Data Plan | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Widths were volatile | Managed layout and visible-column focus | Drag reorder |
| Data Operations / Source Budgets | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Widths were volatile | Managed layout and focused-row copy | Drag reorder |
| Data Operations / Fetch Logs | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Long/error fields dominated width | Hideable low-priority fields and masked visible-row copy | Drag reorder |
| Data Operations / Diagnostics | No | Yes | Yes | No | No | Yes | Existing focus only | Focus/copy retained | Persistent descriptors |
| Data Operations / Readiness Summary | No | Yes | No | No | No | Yes | Summary rows, not a primary column table | Audited only | Cell focus if it becomes analytical |
| Data Operations / Recommended Actions | No | Yes | No | No | No | Yes | Row-oriented action list | Audited only | Cell focus/descriptors |
| Data Operations / Scheduler/Due Jobs | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Row-only before this slice | Cell focus, inspector and managed layout | Linked job detail |
| Data Operations / Constituent Coverage | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Secondary table lacked focus | Cell focus, open, copy and managed layout | Drag reorder |
| Data Operations / API Section Availability | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Endpoint summary was display-only | Cell focus, copy, generic inspector context | Detailed endpoint tables |
| Data Operations / Broker imports, transactions, positions | No | No | No | No | No | No | Only summarized by API availability rows | No new backend/domain rows invented | Contract-backed detail tables |
| Charts / series data | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Fixed columns ignored workstation layout state | Managed layout, visible-column focus and copy-visible-row | Drag reorder |
| Charts / series and comparison summaries | No | Partial | Partial | No | No | Yes | Compact summary controls, not uniform tables | Audited only | Descriptor pass |
| Portfolio / positions | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Widths were volatile | Managed layout and visible-column focus | Drag reorder |
| Fund Detail / listings | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Listing facts were not keyboard-addressable | Full focus/open/copy and managed layout | Drag reorder |
| Fund Detail / holdings | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Widths were volatile | Managed layout and visible-column focus | Drag reorder |
| Fund Detail / distributions | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Existing focus only | Managed ex/pay date, amount, currency, status and source columns | Record date/frequency when domain fields exist |
| Fund Detail / documents | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Existing focus only | Managed type/date/status/source/change/checked/URL columns | Language/title when domain fields exist |
| Fund Detail / facts, prices, jobs, changes | No | No | No | No | No | Active-fund fallback | Grids/plots/secondary tables remain mixed | Audited; no rewrite | Focus/descriptors where useful |
| ETFs / funds and listings | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Page lacked explicit cell focus | Full focus/open/copy and managed layout | Listing-specific secondary table |
| Exposure / country, sector, currency, top holdings | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Entire page lacked explicit table state | Per-table focus/copy/inspector and managed layout | Link richer holding subjects |
| Exposure / diagnostics | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Display-only diagnostic rows | Focus/copy/pin inspector | Domain-specific diagnostic subjects |
| Jobs / scheduled and runs | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Action columns did not share descriptor indexing | Managed descriptors, visible-column focus, copy-visible-row and hidden Run ID | Linked job detail split |
| Alerts / alerts | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Long message consumed width | Message hidden by default; managed layout | Backend lifecycle mutations |
| Documents / snapshots | Yes | Yes | Yes | Yes | Visibility; fixed descriptor order | Yes | Preview required navigation | Responsive metadata preview split; hash/checked hidden by default | App-level split-ratio persistence |

Managed table layouts are stored with UI preferences. Volatile row selection and cell focus are not persisted. `View -> Show All Table Columns`, `View -> Reset Table Columns`, and command aliases reset or reveal layouts globally; each managed table also exposes compact column visibility, focused-column width, show-all, reset, and copy-visible-row controls.
