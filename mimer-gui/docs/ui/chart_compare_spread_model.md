# Chart / Compare / Spread Model

## Workspace

Charts is the primary analytical workspace for single-series, overlay, compare, and spread workflows. Compare and Spreads remain route names, but they seed the chart state and render the Charts page.

Core state lives in `src/charts.rs`:

- `ChartMode`: `Single`, `Overlay`, `Compare`, `Spread`.
- `ChartValueMode`: `Raw`, `Rebased100`, `PercentChange`.
- `PlotRequest`: primary subject, series kind, label, overlays, comparison subjects, mode, and options.
- `ChartPanelState`: active plot, range/table/source controls, overlay/comparison selectors, selected series, selected point, recent plots, and chart table state.
- `ChartSeriesSpec`: owned render/table/inspector representation for each visible series.
- `ChartPointSelection`: selected point metadata for copy and inspector follow mode.
- `ChartTableFocus`: focused chart data row and column for keyboard movement and copy priority.
- `ChartDateAxis`: pure date-axis conversion for plotting, tick labels, hover coordinates, and date-window range filtering.

## Mode Rules

- Single: one primary subject and one series kind.
- Overlay: primary subject plus kind overlays or additional subject overlays.
- Compare: primary A plus comparison B, shown as overlaid series with a compact A/B summary.
- Spread: primary A minus B, derived from matching dates only.

`PlotRequest::with_comparison` defaults to compare mode. `PlotRequest::spread` explicitly marks spread mode. Switching to single clears B subjects and overlays.

## Selection And Navigation

- Single click on a chart series selects that series and clears stale point/table-cell focus.
- Single click on a chart data cell selects the row point, selected series, and focused cell.
- Hovering near a plotted point computes the nearest visible point in the current value mode and shows a compact readout with series, subject, date, displayed value, raw value when transformed, source, status, and value mode.
- Single click near a plotted point selects the nearest point, selects its series, and maps focus back to the matching chart table row when available. It does not change the active subject or navigate.
- Double-click near a plotted point opens that point's related subject through the existing navigation flow. Single-click remains selection only.
- Double-click on a chart data row opens that row subject through the navigation stack and makes it active.
- Arrow up/down moves selected chart rows. Arrow left/right moves the focused chart data column.
- Alt/Option+Up/Down cycles previous/next visible series. Alt/Option+Left/Right cycles previous/next point within the selected series.
- Enter opens the focused/selected row subject.
- Selected chart series/point is not the active subject. Active subject changes only through plot/open/navigation flows.
- Pinned inspector contexts are owned snapshots and are not replaced by later chart selections until unpinned.

Copy priority is:

1. Focused chart data cell.
2. Selected chart point/row.
3. Selected chart series.
4. Chart workspace summary.

Focused cell copy uses the cell raw payload; row/series copies include displayed value, raw value, unit, raw unit, source, status, kind, role, value mode, and `x_mode=date` where available.

The plot context menu can copy the hover value when the cursor is near a point. Keyboard copy remains table-first so a transient hover does not steal clipboard priority from an explicitly focused cell or selected row.

## Plot Picking

Nearest-point picking is implemented as pure chart logic plus a thin `egui_plot` adapter:

- Candidate points are the same visible/transformed points used by the plot and chart table.
- Raw, rebased, and percent modes are respected. Invalid, missing, or non-finite display/raw values are ignored.
- Spread points are eligible as derived series, but spread display remains raw A - B.
- The selected point is represented as chart point metadata, not as an active subject.
- Selection is reconciled after range or value-mode changes; stale selected points and table focus are cleared when the point disappears.

The plot uses `egui_plot` pointer coordinates and `screen_from_plot` scaling for a pixel-like threshold. The x-axis is date-based: `ChartDateAxis` maps ISO dates to days since the Unix epoch. Calendar gaps such as weekends and holidays are preserved as empty space. Guide lines, selected/hover markers, nearest-point matching, and plot/table sync use the selected point's real date x-value. The retained `point_index` is only a deterministic tie-breaker.

Range controls are date windows rather than point counts:

- `1M`: latest valid point date minus 31 days.
- `3M`: latest valid point date minus 92 days.
- `6M`: latest valid point date minus 183 days.
- `1Y`: latest valid point date minus 366 days.
- `All`: all points.

Date tick labels use the plot axis formatter. Short ranges show day/month, medium ranges show month/year, and long ranges show years. Invalid date coordinates format as blank labels.

## Value Modes

Chart display values can be transformed without changing raw source data:

- Raw: original mock/source value and unit.
- Rebased 100: first valid common point becomes `100`; later values are `value / base * 100`.
- Percent Change: first valid common point becomes `0%`; later values are `value / base - 1`.

Rebasing requires a finite, non-zero first common base value. If no valid common base exists, transformed display points are omitted and the UI labels the missing base. Spread mode currently displays raw derived A - B values even if a transformed value mode is selected; the header shows `SPREAD RAW`.

The chart data table exposes both displayed value and raw value columns so users can distinguish raw, rebased, percent, and derived values.

## Compare Summary

Compare mode shows A/B series plus a compact summary:

- latest common A and B values in the current value mode
- A - B and percent difference on the latest common date
- raw A/B values and raw difference on the latest common date
- first common date, latest common date, common point count, value mode, base date, and A/B source/status

In rebased or percent modes, displayed differences refer to transformed values. The raw row keeps the source values visible.

## Spread Derivation

Spread series are derived locally from the available mock `TimeSeries` points:

- Match dates exactly.
- Compute `left.value - right.value`.
- Do not invent missing dates.
- Mark the derived series `DERIVED`, `PARTIAL`, or `MISSING`.
- Show source A, source B, and coverage in the chart UI and inspector.
- Expose actions to copy expression, copy latest spread, copy spread series, swap A/B, compare A/B, and open A or B.

This is not a real pricing, return, rebasing, FX, or analytics engine.

## Command Model

Commands route into the same chart state:

- `plot active`, `plot selected`, `plot VUSA`, `chart VUSA`
- `overlay ISF`, `add overlay ISF`, `clear overlays`
- `compare VUSA ISF`, `plot VUSA vs ISF`
- `spread VUSA JEPG`, `plot spread VUSA JEPG`
- `swap comparison`, `chart mode single|overlay|compare|spread`
- `chart value raw`, `chart value rebased 100`, `chart value percent`
- `copy chart`, `copy chart row`, `copy chart value`, `copy selected series`, `copy selected point`, `open chart subject`
- `clear chart selection`, `select next series`, `select previous series`, `select next point`, `select previous point`

## Deferred Work

- Real backend hydration and source availability APIs.
- Drag/brush selection and richer chart gestures.
- FX-aware spreads, total-return analytics, and real market-data calculations.
- Rebased or percent transformed spread outputs beyond raw A - B.
