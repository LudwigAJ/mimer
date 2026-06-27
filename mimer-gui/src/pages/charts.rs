use crate::app_model::DashboardSnapshot;
use crate::charts::{
    ChartDataColumn, ChartDateAxis, ChartMode, ChartNearestPoint, ChartPanelState,
    ChartPointDistanceScale, ChartPointSelection, ChartSeriesId, ChartSeriesPreset,
    ChartSeriesRole, ChartSeriesSet, ChartSeriesSpec, ChartValueMode, PlotRange, PlotRequest,
    build_plot_request, chart_series_for_request, find_nearest_chart_point,
    selected_chart_point_marker, visible_points_for_range as visible_chart_points_for_range,
};
use crate::compute::charts::{DisplayPoint, display_points_for_mode, first_valid_common_base_date};
use crate::domain::AnalysisSubject;
use crate::pages::{format_number, format_source, header_cell};
use crate::source::{
    DataKind, SourceSelection, mock_available_sources_for, resolve_source_selection,
};
use crate::table_state::SortSpec;
use crate::timeseries::{TimeSeries, TimeSeriesKind, TimeSeriesPoint};
use crate::ui::grid_helpers::{KvRow, kv_grid};
use crate::ui::metrics;
use crate::ui::style;
use eframe::egui;
use egui_extras::{Column, TableBuilder};
use egui_plot::{
    CoordinatesFormatter, Corner, Line, Plot, PlotPoint, PlotPoints, PlotUi, Points, VLine,
};
use std::cmp::Ordering;

const COL_DATE: &str = "date";
const COL_SERIES: &str = "series";
const COL_SUBJECT: &str = "subject";
const COL_VALUE: &str = "value";
const COL_RAW_VALUE: &str = "raw_value";
const COL_UNIT: &str = "unit";
const COL_SOURCE: &str = "source";
const COL_STATUS: &str = "status";
const COL_KIND: &str = "kind";
const PLOT_PICK_RADIUS_PX: f64 = 18.0;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ChartPageAction {
    OpenSubject {
        subject: AnalysisSubject,
        label: String,
    },
    PinCurrentSelection,
    Feedback(String),
}

#[derive(Clone, Debug)]
struct SubjectChoice {
    key: String,
    label: String,
    short_label: String,
    subject: AnalysisSubject,
}

#[derive(Clone, Copy)]
struct ChartDisplayContext<'a> {
    range: PlotRange,
    value_mode: ChartValueMode,
    base_date: Option<&'a str>,
}

pub fn render(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    snapshot: &DashboardSnapshot,
) -> Option<ChartPageAction> {
    ensure_default_plot(state, snapshot);
    let mut action = None;

    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let stale_count = snapshot.portfolio_summary.stale_warning_count;
            let subtitle = format!(
                "{} · {} stale · focused cell copies before row or series",
                snapshot.portfolio_summary.base_currency, stale_count
            );
            style::page_header(
                ui,
                "Charts",
                Some(&snapshot.workspace.name),
                Some(&subtitle),
                |ui| {
                    style::mock_badge(ui);
                    style::source_badge(ui, "mock");
                },
            );
            controls(ui, state, snapshot, &mut action);
            recent_plots(ui, state);
            ui.add_space(6.0);

            let Some(request) = state.active_plot.clone() else {
                ui.monospace("No plot loaded. Try: plot portfolio");
                return;
            };

            plot_workspace(ui, state, &request, snapshot, &mut action);
        });

    action
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    state: &mut ChartPanelState,
    all_series: &[TimeSeries],
) -> Option<ChartPageAction> {
    if ctx.text_edit_focused() {
        return None;
    }

    let request = state.active_plot.clone()?;
    let series_set = chart_series_for_request(&request, all_series);
    if series_set.series.is_empty() {
        return None;
    }

    let value_mode = effective_value_mode(request.mode, state.value_mode);
    let base_date = display_base_date(&series_set.series, request.options.range, value_mode);
    let rows = sorted_chart_rows(
        &series_set.series,
        request.options.range,
        value_mode,
        base_date.as_deref(),
        state.data_table.sort.as_ref(),
    );
    let visible_indices = (0..rows.len()).collect::<Vec<_>>();
    state.data_table.selection.retain_visible(&visible_indices);
    if state
        .table_focus
        .row_index
        .is_some_and(|row_index| row_index >= rows.len())
    {
        state.table_focus.clear();
        state.data_table.selected_cell = None;
    }

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::ALT, egui::Key::ArrowUp)) {
        if state.select_relative_series(&series_set, -1) {
            return Some(ChartPageAction::Feedback(
                "CMD: selected previous chart series".to_owned(),
            ));
        }
        return None;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::ALT, egui::Key::ArrowDown)) {
        if state.select_relative_series(&series_set, 1) {
            return Some(ChartPageAction::Feedback(
                "CMD: selected next chart series".to_owned(),
            ));
        }
        return None;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::ALT, egui::Key::ArrowLeft)) {
        if select_relative_point_in_rows(state, &rows, -1) {
            return Some(ChartPageAction::Feedback(
                "CMD: selected previous chart point".to_owned(),
            ));
        }
        return None;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::ALT, egui::Key::ArrowRight)) {
        if select_relative_point_in_rows(state, &rows, 1) {
            return Some(ChartPageAction::Feedback(
                "CMD: selected next chart point".to_owned(),
            ));
        }
        return None;
    }

    let mut moved_row = None;
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        moved_row = state.data_table.selection.move_by(&visible_indices, -1);
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        moved_row = state.data_table.selection.move_by(&visible_indices, 1);
    }
    if let Some(row_index) = moved_row
        && let Some(row_data) = rows.get(row_index)
    {
        let column = state.table_focus.column_or_default();
        select_chart_cell(state, row_index, row_data, column);
    }

    let mut moved_column = None;
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        moved_column = state.table_focus.move_column(-1);
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        moved_column = state.table_focus.move_column(1);
    }
    if let Some(column) = moved_column {
        let row_index = state
            .table_focus
            .row_index
            .or_else(|| state.data_table.selected_index())
            .unwrap_or(0)
            .min(rows.len().saturating_sub(1));
        if let Some(row_data) = rows.get(row_index) {
            select_chart_cell(state, row_index, row_data, column);
        }
    }

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        let row_index = state
            .table_focus
            .row_index
            .or_else(|| state.data_table.selected_index())
            .unwrap_or(0)
            .min(rows.len().saturating_sub(1));
        if let Some(row_data) = rows.get(row_index) {
            let column = state.table_focus.column_or_default();
            select_chart_cell(state, row_index, row_data, column);
            return Some(ChartPageAction::OpenSubject {
                subject: row_data.subject.clone(),
                label: row_data.subject_label.clone(),
            });
        }
    }

    None
}

pub fn select_relative_point(
    state: &mut ChartPanelState,
    all_series: &[TimeSeries],
    offset: isize,
) -> bool {
    let Some(request) = state.active_plot.clone() else {
        return false;
    };
    let series_set = chart_series_for_request(&request, all_series);
    if series_set.series.is_empty() {
        return false;
    }

    let value_mode = effective_value_mode(request.mode, state.value_mode);
    let base_date = display_base_date(&series_set.series, request.options.range, value_mode);
    let rows = sorted_chart_rows(
        &series_set.series,
        request.options.range,
        value_mode,
        base_date.as_deref(),
        state.data_table.sort.as_ref(),
    );
    let visible_indices = (0..rows.len()).collect::<Vec<_>>();
    let Some(row_index) = state.data_table.selection.move_by(&visible_indices, offset) else {
        return false;
    };
    let Some(row_data) = rows.get(row_index) else {
        return false;
    };
    let column = state.table_focus.column_or_default();
    select_chart_cell(state, row_index, row_data, column);
    true
}

pub fn select_relative_series(
    state: &mut ChartPanelState,
    all_series: &[TimeSeries],
    offset: isize,
) -> bool {
    let Some(request) = state.active_plot.clone() else {
        return false;
    };
    let series_set = chart_series_for_request(&request, all_series);
    state.select_relative_series(&series_set, offset)
}

pub fn select_relative_point_in_selected_series(
    state: &mut ChartPanelState,
    all_series: &[TimeSeries],
    offset: isize,
) -> bool {
    let Some(request) = state.active_plot.clone() else {
        return false;
    };
    let series_set = chart_series_for_request(&request, all_series);
    if series_set.series.is_empty() {
        return false;
    }

    let value_mode = effective_value_mode(request.mode, state.value_mode);
    let base_date = display_base_date(&series_set.series, request.options.range, value_mode);
    let rows = sorted_chart_rows(
        &series_set.series,
        request.options.range,
        value_mode,
        base_date.as_deref(),
        state.data_table.sort.as_ref(),
    );
    select_relative_point_in_rows(state, &rows, offset)
}

fn ensure_default_plot(state: &mut ChartPanelState, snapshot: &DashboardSnapshot) {
    if state.active_plot.is_some() {
        return;
    }

    state.set_plot(PlotRequest::new(
        AnalysisSubject::WorkspacePortfolio(snapshot.workspace.id.clone()),
        TimeSeriesKind::PortfolioValue,
        format!("{} value", snapshot.workspace.name),
    ));
}

fn controls(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    snapshot: &DashboardSnapshot,
    action: &mut Option<ChartPageAction>,
) {
    let subject_choices = subject_choices(snapshot);
    let active_request = state.active_plot.clone();
    let mut pending_plot = None;

    ui.horizontal_wrapped(|ui| {
        ui.label("Mode");
        for mode in ChartMode::ALL {
            if ui.selectable_label(state.mode == mode, mode.label()).clicked() {
                state.set_mode(mode);
            }
        }
        ui.separator();
        if let Some(request) = active_request.as_ref() {
            style::badge(
                ui,
                format!("ACTIVE {}", subject_label(snapshot, &request.subject)),
                style::BadgeTone::Info,
                Some("Active chart subject; opening rows changes active subject only on double-click or Open.".to_owned()),
            );
            style::badge(
                ui,
                format!("MODE {}", state.mode.label().to_ascii_uppercase()),
                style::BadgeTone::Neutral,
                Some("Chart workspace mode: Single, Overlay, Compare, or Spread.".to_owned()),
            );
            let effective_mode = effective_value_mode(state.mode, state.value_mode);
            style::badge(
                ui,
                format!("VALUE {}", effective_mode.compact_label()),
                if effective_mode.is_transformed() {
                    style::BadgeTone::Derived
                } else {
                    style::BadgeTone::Neutral
                },
                Some(value_mode_tooltip(state.mode, state.value_mode)),
            );
            if !request.comparison_subjects.is_empty() {
                style::badge(
                    ui,
                    format!("B {}", request.comparison_subjects.len()),
                    style::BadgeTone::Derived,
                    Some("Comparison/overlay subjects attached to this chart.".to_owned()),
                );
            }
            if let Some(point) = state.selected_point.as_ref() {
                style::badge(
                    ui,
                    format!("POINT {}", point.date),
                    style::BadgeTone::Info,
                    Some(point.copy_text()),
                );
            } else if let Some(series_id) = state.selected_series_id.as_ref() {
                style::badge(
                    ui,
                    "SERIES SELECTED",
                    style::BadgeTone::Info,
                    Some(series_id.0.clone()),
                );
            }
            if let (Some(row_index), Some(column)) =
                (state.table_focus.row_index, state.table_focus.column)
            {
                style::badge(
                    ui,
                    format!("CELL {} R{}", column.label(), row_index + 1),
                    style::BadgeTone::Info,
                    Some("Focused chart table cell; Ctrl/Cmd+C copies this cell before row or series.".to_owned()),
                );
            }
        }
    });

    ui.horizontal_wrapped(|ui| {
        if let Some(request) = active_request.as_ref() {
            let current_subject_key = subject_key(&request.subject);
            let mut selected_subject_key = current_subject_key.clone();
            ui.label("Subject");
            egui::ComboBox::from_id_salt("charts_subject_picker")
                .selected_text(subject_label(snapshot, &request.subject))
                .show_ui(ui, |ui| {
                    for choice in &subject_choices {
                        ui.selectable_value(
                            &mut selected_subject_key,
                            choice.key.clone(),
                            choice.label.as_str(),
                        );
                    }
                });
            if selected_subject_key != current_subject_key
                && let Some(choice) = subject_choices
                    .iter()
                    .find(|choice| choice.key == selected_subject_key)
            {
                let preset = ChartSeriesPreset::from_request(request);
                pending_plot = Some(build_plot_request(
                    choice.subject.clone(),
                    choice.short_label.clone(),
                    preset,
                ));
            }

            ui.label("Series");
            let current_preset = ChartSeriesPreset::from_request(request);
            let mut selected_preset = current_preset;
            egui::ComboBox::from_id_salt("charts_series_preset")
                .selected_text(current_preset.label())
                .show_ui(ui, |ui| {
                    for preset in ChartSeriesPreset::ALL {
                        ui.selectable_value(&mut selected_preset, preset, preset.label());
                    }
                });
            if selected_preset != current_preset {
                let mut next = request.clone();
                next.series_kind = selected_preset.primary_kind();
                next.overlay_series_kinds = selected_preset.overlay_kinds();
                next.label = format!(
                    "{} {}",
                    subject_label(snapshot, &next.subject),
                    selected_preset.label()
                );
                pending_plot = Some(next);
            }

            source_selector(ui, state, request, snapshot);
        }

        ui.label("Range");
        egui::ComboBox::from_id_salt("charts_range")
            .selected_text(state.selected_range.as_str())
            .show_ui(ui, |ui| {
                for range in PlotRange::ALL {
                    ui.selectable_value(&mut state.selected_range, range, range.as_str());
                }
            });
        ui.label("Value");
        egui::ComboBox::from_id_salt("charts_value_mode")
            .selected_text(effective_value_mode(state.mode, state.value_mode).label())
            .show_ui(ui, |ui| {
                for mode in ChartValueMode::ALL {
                    ui.selectable_value(&mut state.value_mode, mode, mode.label())
                        .on_hover_text(mode.tooltip());
                }
            });
        if state.mode == ChartMode::Spread && state.value_mode.is_transformed() {
            style::badge(
                ui,
                "SPREAD RAW",
                style::BadgeTone::Warning,
                Some(
                    "Spread mode displays the raw derived A - B series; rebasing spread outputs is deferred."
                        .to_owned(),
                ),
            );
        }
        ui.checkbox(&mut state.show_table, "Table");

        if let Some(request) = active_request.as_ref()
            && ui
                .button("Plot Active")
                .on_hover_text("Replot the active chart subject.")
                .clicked()
        {
            pending_plot = Some(request.clone());
        }
        if ui.button("Portfolio Value").clicked() {
            pending_plot = Some(PlotRequest::new(
                AnalysisSubject::WorkspacePortfolio(snapshot.workspace.id.clone()),
                TimeSeriesKind::PortfolioValue,
                format!("{} value", snapshot.workspace.name),
            ));
        }
        if let Some(request) = crate::charts::plot_request_from_selected(
            &snapshot.selected,
            TimeSeriesKind::Price,
            "Selected Price",
        ) && ui
            .button("Plot Selected")
            .on_hover_text("Plot the globally selected subject.")
            .clicked()
        {
            pending_plot = Some(request);
        }
    });

    ui.horizontal_wrapped(|ui| {
        let can_act = state.active_plot.is_some();
        if crate::ui::actions::action_button_enabled(
            ui,
            can_act,
            "Open Subject",
            "Open the active chart subject.",
        )
        .clicked()
            && let Some(request) = state.active_plot.as_ref()
        {
            *action = Some(ChartPageAction::OpenSubject {
                subject: request.subject.clone(),
                label: subject_label(snapshot, &request.subject),
            });
        }

        overlay_picker(ui, state, &subject_choices);

        if crate::ui::actions::action_button_enabled(
            ui,
            can_act,
            "Add Overlay",
            "Add selected overlay subject to this chart.",
        )
        .clicked()
            && let Some(choice) = subject_choices
                .iter()
                .find(|choice| choice.key == state.overlay_subject_key)
        {
            let added = state.add_comparison_subject(choice.subject.clone());
            let message = if added {
                format!("CMD: overlay {}", choice.short_label)
            } else {
                "CMD: overlay already present".to_owned()
            };
            *action = Some(ChartPageAction::Feedback(message));
        }
        if let Some(request) = state.active_plot.as_ref()
            && let Some(selected_request) = crate::charts::plot_request_from_selected(
                &snapshot.selected,
                request.series_kind,
                snapshot.selected_subject_label(),
            )
            && crate::ui::actions::action_button_enabled(
                ui,
                can_act,
                "Overlay Selected",
                "Add the selected subject as an overlay.",
            )
            .clicked()
        {
            let added = state.add_comparison_subject(selected_request.subject);
            *action = Some(ChartPageAction::Feedback(if added {
                "CMD: overlay selected".to_owned()
            } else {
                "CMD: selected overlay unavailable or already present".to_owned()
            }));
        }
        if crate::ui::actions::action_button_enabled(
            ui,
            can_act,
            "Clear Overlays",
            "Clear overlays, comparison, and spread B subject.",
        )
        .clicked()
            && state.clear_overlays()
        {
            *action = Some(ChartPageAction::Feedback(
                "CMD: cleared overlays".to_owned(),
            ));
        }
        if crate::ui::actions::action_button_enabled(
            ui,
            can_act,
            "Explain Series",
            "Mock explanation placeholder for chart inputs and sources.",
        )
        .clicked()
            && let Some(request) = state.active_plot.as_ref()
        {
            *action = Some(ChartPageAction::Feedback(format!(
                "MOCK explain {} | source/status shown in chart table",
                request.label
            )));
        }
        if crate::ui::actions::action_button_enabled(ui, can_act, "Copy Label", "Copy chart label.")
            .clicked()
            && let Some(request) = state.active_plot.as_ref()
        {
            ui.copy_text(request.label.clone());
            *action = Some(ChartPageAction::Feedback(format!(
                "CMD: copied {}",
                request.label
            )));
        }
        if let Some(point) = state.selected_point.as_ref()
            && crate::ui::actions::action_button(
                ui,
                "Copy Selected Value",
                "Copy selected chart point data.",
            )
            .clicked()
        {
            ui.copy_text(point.copy_text());
            *action = Some(ChartPageAction::Feedback(format!(
                "COPIED: {} {}",
                point.series_label, point.date
            )));
        }
        if let Some(request) = state.active_plot.as_ref()
            && let Some(series_id) = state.selected_series_id.as_ref()
            && crate::ui::actions::action_button(
                ui,
                "Copy Series Data",
                "Copy the selected series as tab-separated data.",
            )
            .clicked()
        {
            let series_set = chart_series_for_request(request, &snapshot.time_series);
            let value_mode = effective_value_mode(request.mode, state.value_mode);
            let base_date =
                display_base_date(&series_set.series, request.options.range, value_mode);
            if let Some(series) = series_set
                .series
                .iter()
                .find(|series| series.id == *series_id)
            {
                ui.copy_text(series_display_data_text(
                    series,
                    request.options.range,
                    value_mode,
                    base_date.as_deref(),
                ));
                *action = Some(ChartPageAction::Feedback(format!(
                    "COPIED: series {}",
                    series.label
                )));
            }
        }
        if ui.button("Reset Plot").clicked() {
            pending_plot = Some(PlotRequest::new(
                AnalysisSubject::WorkspacePortfolio(snapshot.workspace.id.clone()),
                TimeSeriesKind::PortfolioValue,
                format!("{} value", snapshot.workspace.name),
            ));
        }
    });

    mode_controls(ui, state, &subject_choices, action);
    overlay_list(ui, state, &subject_choices, action);

    if let Some(request) = pending_plot {
        state.set_plot(request);
    }
    state.sync_options();
}

fn mode_controls(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    subject_choices: &[SubjectChoice],
    action: &mut Option<ChartPageAction>,
) {
    if !matches!(state.mode, ChartMode::Compare | ChartMode::Spread) {
        return;
    }

    if state.comparison_subject_key.is_empty()
        && let Some(choice) = subject_choices
            .iter()
            .find(|choice| {
                state
                    .active_plot
                    .as_ref()
                    .is_none_or(|plot| choice.subject != plot.subject)
            })
            .or_else(|| subject_choices.first())
    {
        state.comparison_subject_key = choice.key.clone();
    }

    ui.horizontal_wrapped(|ui| {
        let mode_label = match state.mode {
            ChartMode::Compare => "Compare B",
            ChartMode::Spread => "Spread B",
            ChartMode::Single | ChartMode::Overlay => "B",
        };
        ui.label(mode_label);
        egui::ComboBox::from_id_salt("charts_comparison_subject_picker")
            .selected_text(
                subject_choices
                    .iter()
                    .find(|choice| choice.key == state.comparison_subject_key)
                    .map(|choice| choice.short_label.as_str())
                    .unwrap_or("-"),
            )
            .show_ui(ui, |ui| {
                for choice in subject_choices {
                    ui.selectable_value(
                        &mut state.comparison_subject_key,
                        choice.key.clone(),
                        choice.label.as_str(),
                    );
                }
            });

        let can_act = state.active_plot.is_some();
        let verb = if state.mode == ChartMode::Spread {
            "Spread Against"
        } else {
            "Compare With"
        };
        if crate::ui::actions::action_button_enabled(ui, can_act, verb, "Set chart B subject.")
            .clicked()
            && let Some(choice) = subject_choices
                .iter()
                .find(|choice| choice.key == state.comparison_subject_key)
        {
            let changed = if state.mode == ChartMode::Spread {
                state.set_spread_subject(choice.subject.clone())
            } else {
                state.set_comparison_subject(choice.subject.clone())
            };
            *action = Some(ChartPageAction::Feedback(if changed {
                format!("CMD: {} {}", verb.to_ascii_lowercase(), choice.short_label)
            } else {
                "CMD: B subject unavailable".to_owned()
            }));
        }
        if crate::ui::actions::action_button_enabled(
            ui,
            can_act,
            "Swap A/B",
            "Swap primary and comparison/spread subject.",
        )
        .clicked()
        {
            *action = Some(ChartPageAction::Feedback(if state.swap_operands() {
                "CMD: swapped A/B".to_owned()
            } else {
                "CMD: no B subject to swap".to_owned()
            }));
        }
        if crate::ui::actions::action_button_enabled(
            ui,
            can_act,
            "Clear B",
            "Clear comparison/spread subject and return to single mode.",
        )
        .clicked()
            && state.clear_overlays()
        {
            *action = Some(ChartPageAction::Feedback("CMD: cleared B".to_owned()));
        }
    });
}

fn overlay_list(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    subject_choices: &[SubjectChoice],
    action: &mut Option<ChartPageAction>,
) {
    let overlays = state
        .active_plot
        .as_ref()
        .map(|request| request.comparison_subjects.clone())
        .unwrap_or_default();
    if overlays.is_empty() {
        return;
    }

    ui.horizontal_wrapped(|ui| {
        ui.label(egui::RichText::new("Series B/Overlays").weak());
        for subject in overlays {
            let label = subject_choices
                .iter()
                .find(|choice| choice.subject == subject)
                .map(|choice| choice.short_label.as_str())
                .unwrap_or_else(|| subject.short_id());
            if ui
                .small_button(format!("x {label}"))
                .on_hover_text("Remove this comparison/overlay subject.")
                .clicked()
            {
                let removed = state.remove_comparison_subject(&subject);
                *action = Some(ChartPageAction::Feedback(if removed {
                    format!("CMD: removed overlay {label}")
                } else {
                    "CMD: overlay unavailable".to_owned()
                }));
            }
        }
    });
}

fn overlay_picker(ui: &mut egui::Ui, state: &mut ChartPanelState, choices: &[SubjectChoice]) {
    if state.overlay_subject_key.is_empty()
        && let Some(choice) = choices.first()
    {
        state.overlay_subject_key = choice.key.clone();
    }

    ui.label("Overlay");
    egui::ComboBox::from_id_salt("charts_overlay_subject_picker")
        .selected_text(
            choices
                .iter()
                .find(|choice| choice.key == state.overlay_subject_key)
                .map(|choice| choice.short_label.as_str())
                .unwrap_or("-"),
        )
        .show_ui(ui, |ui| {
            for choice in choices {
                ui.selectable_value(
                    &mut state.overlay_subject_key,
                    choice.key.clone(),
                    choice.label.as_str(),
                );
            }
        });
}

fn source_selector(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    request: &PlotRequest,
    snapshot: &DashboardSnapshot,
) {
    let kind = data_kind_for_series(request.series_kind);
    let (mut available_sources, canonical_fallback) =
        mock_available_sources_for(&request.subject, kind);
    available_sources.extend(available_sources_for_request(
        request,
        &snapshot.time_series,
    ));
    available_sources.sort();
    available_sources.dedup();
    let plotted_series = plot_series_for_request(request, &snapshot.time_series);
    let canonical = plotted_series
        .first()
        .map(|series| series.source.as_str())
        .or(Some(canonical_fallback));
    let resolution =
        resolve_source_selection(&available_sources, &state.source_selection, canonical);

    ui.label("Source");
    if resolution.available_sources.len() <= 1 {
        style::source_resolution_badge(ui, &resolution, kind)
            .on_hover_cursor(egui::CursorIcon::Help);
    } else {
        egui::ComboBox::from_id_salt("charts_source_selection")
            .selected_text(state.source_selection.label())
            .show_ui(ui, |ui| {
                ui.selectable_value(
                    &mut state.source_selection,
                    SourceSelection::Canonical,
                    "Canonical",
                );
                for source in &resolution.available_sources {
                    ui.selectable_value(
                        &mut state.source_selection,
                        SourceSelection::Specific(source.clone()),
                        format_source(source),
                    );
                }
            });
        style::source_resolution_badge(ui, &resolution, kind)
            .on_hover_cursor(egui::CursorIcon::Help);
    }
}

fn recent_plots(ui: &mut egui::Ui, state: &mut ChartPanelState) {
    if state.recent_plots.is_empty() {
        return;
    }

    let recent = state.recent_plots.clone();
    ui.horizontal_wrapped(|ui| {
        ui.label("Recent");
        for (index, request) in recent.iter().take(6).enumerate() {
            if ui
                .small_button(compact_plot_label(&request.label))
                .clicked()
            {
                state.select_recent(index);
            }
        }
    });
}

fn compact_plot_label(label: &str) -> String {
    if label.len() > 28 {
        format!("{}...", &label[..25])
    } else {
        label.to_owned()
    }
}

fn plot_workspace(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    request: &PlotRequest,
    snapshot: &DashboardSnapshot,
    action: &mut Option<ChartPageAction>,
) {
    let available = ui.available_size_before_wrap();
    let table_height = if request.options.show_table {
        (available.y * 0.32).clamp(150.0, 260.0)
    } else {
        0.0
    };
    let plot_height = (available.y - table_height - 22.0).max(260.0);
    let series_set = chart_series_for_request(request, &snapshot.time_series);
    let value_mode = effective_value_mode(request.mode, state.value_mode);
    let base_date = display_base_date(&series_set.series, request.options.range, value_mode);
    let display = ChartDisplayContext {
        range: request.options.range,
        value_mode,
        base_date: base_date.as_deref(),
    };
    reconcile_chart_selection(state, &series_set, display);

    plot_area(
        ui,
        state,
        request,
        &series_set,
        plot_height,
        display,
        action,
    );
    ui.add_space(metrics::SPACE_2);
    plot_metadata(
        ui,
        state,
        request,
        &series_set,
        snapshot,
        value_mode,
        base_date.as_deref(),
    );

    if request.options.show_table {
        ui.add_space(metrics::SPACE_2);
        if series_set.series.is_empty() {
            ui.monospace("No chart data. Try: plot portfolio");
        } else {
            series_table(
                ui,
                "charts_series_table",
                &series_set.series,
                display,
                table_height,
                state,
                action,
            );
        }
    }
}

fn reconcile_chart_selection(
    state: &mut ChartPanelState,
    series_set: &ChartSeriesSet,
    display: ChartDisplayContext<'_>,
) {
    if let Some(series_id) = state.selected_series_id.as_ref()
        && !series_set
            .series
            .iter()
            .any(|series| series.id == *series_id)
    {
        state.clear_chart_selection();
        return;
    }

    if let Some(selected) = state.selected_point.as_ref()
        && selected_chart_point_marker(series_set, display.range, display.value_mode, selected)
            .is_none()
    {
        state.selected_point = None;
        state.data_table.clear_selection();
        state.table_focus.clear();
    }
}

fn plot_area(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    request: &PlotRequest,
    series_set: &ChartSeriesSet,
    plot_height: f32,
    display: ChartDisplayContext<'_>,
    action: &mut Option<ChartPageAction>,
) {
    ui.label(egui::RichText::new(&request.label).strong());
    let Some(primary) = series_set.series.first() else {
        ui.monospace(format!(
            "No mock {} series exists for this subject.",
            request.series_kind.as_str()
        ));
        if request.mode == ChartMode::Spread {
            ui.horizontal_wrapped(|ui| {
                style::derived_badge(ui);
                style::status_badge(ui, "MISSING");
                ui.monospace("Cannot align spread series for matching dates.");
            });
        }
        return;
    };

    ui.monospace(format!(
        "Series: {} | Points: {}",
        primary.label,
        display_points(
            primary,
            display.range,
            display.value_mode,
            display.base_date
        )
        .len()
    ));
    ui.horizontal_wrapped(|ui| {
        style::source_badge(ui, &primary.source);
        style::status_badge(ui, &primary.status);
        if display.value_mode.is_transformed() {
            style::badge(
                ui,
                display.value_mode.compact_label(),
                style::BadgeTone::Derived,
                Some(transform_status_text(display.value_mode, display.base_date)),
            );
        }
        if request.mode == ChartMode::Spread {
            style::derived_badge(ui);
        }
    });
    series_selector(ui, state, request, &series_set.series, display, action);
    if let Some(spread) = series_set.spread.as_ref() {
        spread_status_row(ui, state, request, spread, &series_set.series, action);
    }
    if request.mode == ChartMode::Compare {
        comparison_summary_table(
            ui,
            &series_set.series,
            display.range,
            display.value_mode,
            display.base_date,
        );
    }
    if has_mixed_units(&series_set.series) {
        ui.colored_label(
            egui::Color32::from_rgb(220, 176, 82),
            "WARN: mixed units on one mock axis",
        );
    }

    let selected_marker = state.selected_point.as_ref().and_then(|selected| {
        selected_chart_point_marker(series_set, display.range, display.value_mode, selected)
    });
    let mut hover_nearest: Option<ChartNearestPoint> = None;
    let mut clicked_nearest: Option<ChartNearestPoint> = None;
    let mut double_clicked_nearest: Option<ChartNearestPoint> = None;
    let axis_range = display.range;

    let plot_response = Plot::new("charts_active_plot")
        .width(ui.available_width())
        .height(plot_height)
        .allow_scroll(false)
        .x_axis_label("Date")
        .x_axis_formatter(move |mark, visible_range| {
            let span_days = (*visible_range.end() - *visible_range.start()).abs();
            let formatted = ChartDateAxis::format_tick_for_span(mark.value, span_days);
            if formatted.is_empty() {
                ChartDateAxis::format_tick(mark.value, axis_range)
            } else {
                formatted
            }
        })
        .coordinates_formatter(
            Corner::LeftTop,
            CoordinatesFormatter::new(|point, _bounds| {
                let date = ChartDateAxis::x_to_date(point.x).unwrap_or_else(|| "-".to_owned());
                format!("date: {date}\ny: {}", format_number(point.y, 4))
            }),
        )
        .show_crosshair(true)
        .show(ui, |plot_ui| {
            for series in &series_set.series {
                plot_ui.line(line_from_series(
                    series,
                    display.range,
                    display.value_mode,
                    display.base_date,
                ));
            }

            if let Some(marker) = selected_marker.as_ref() {
                draw_plot_marker(
                    plot_ui,
                    marker,
                    "selected point",
                    egui::Color32::from_rgb(90, 160, 255),
                );
            }

            if plot_ui.response().hovered()
                && let Some(cursor) = plot_ui.pointer_coordinate()
            {
                hover_nearest = find_nearest_chart_point(
                    series_set,
                    display.range,
                    display.value_mode,
                    cursor.x,
                    cursor.y,
                    PLOT_PICK_RADIUS_PX,
                    plot_distance_scale(plot_ui),
                );
            }

            if let Some(marker) = hover_nearest.as_ref() {
                draw_plot_marker(
                    plot_ui,
                    marker,
                    "hover point",
                    egui::Color32::from_rgb(240, 190, 80),
                );
            }

            let response = plot_ui.response();
            if response.double_clicked() {
                double_clicked_nearest = hover_nearest.clone();
            } else if response.clicked() {
                clicked_nearest = hover_nearest.clone();
            }
        });

    if let Some(nearest) = double_clicked_nearest.as_ref() {
        let rows = sorted_chart_rows(
            &series_set.series,
            display.range,
            display.value_mode,
            display.base_date,
            state.data_table.sort.as_ref(),
        );
        select_nearest_plot_point(state, nearest, &rows);
        *action = Some(ChartPageAction::OpenSubject {
            subject: nearest.subject.clone(),
            label: nearest.series_label.clone(),
        });
    } else if let Some(nearest) = clicked_nearest.as_ref() {
        let rows = sorted_chart_rows(
            &series_set.series,
            display.range,
            display.value_mode,
            display.base_date,
            state.data_table.sort.as_ref(),
        );
        select_nearest_plot_point(state, nearest, &rows);
        *action = Some(ChartPageAction::Feedback(format!(
            "CMD: selected {} {}",
            nearest.series_label, nearest.date
        )));
    }

    let hover_text = hover_nearest
        .as_ref()
        .map(nearest_point_tooltip)
        .unwrap_or_else(|| {
            "Chart area. Hover near a point for source/status; click near a point to select."
                .to_owned()
        });
    let response = plot_response
        .response
        .on_hover_cursor(egui::CursorIcon::Crosshair)
        .on_hover_text(hover_text);
    response.context_menu(|ui| {
        if let Some(hover) = hover_nearest.as_ref()
            && ui.button("Copy Hover Value").clicked()
        {
            ui.copy_text(hover.copy_value_text());
            *action = Some(ChartPageAction::Feedback(format!(
                "COPIED: hover {} {}",
                hover.series_label, hover.date
            )));
            ui.close();
        }
        if let Some(point) = state.selected_point.as_ref()
            && ui.button("Copy Selected Point").clicked()
        {
            ui.copy_text(point.copy_text());
            *action = Some(ChartPageAction::Feedback(format!(
                "COPIED: {} {}",
                point.series_label, point.date
            )));
            ui.close();
        }
        if let Some(series) = selected_series(series_set, state)
            && ui.button("Copy Selected Series").clicked()
        {
            ui.copy_text(series_display_data_text(
                series,
                display.range,
                display.value_mode,
                display.base_date,
            ));
            *action = Some(ChartPageAction::Feedback(format!(
                "COPIED: series {}",
                series.label
            )));
            ui.close();
        }
        if ui.button("Open Selected Subject").clicked() {
            if let Some(point) = state.selected_point.as_ref() {
                *action = Some(ChartPageAction::OpenSubject {
                    subject: point.subject.clone(),
                    label: point.series_label.clone(),
                });
            } else if let Some(series) = selected_series(series_set, state) {
                *action = Some(ChartPageAction::OpenSubject {
                    subject: series.subject.clone(),
                    label: series.label.clone(),
                });
            } else {
                *action = Some(ChartPageAction::OpenSubject {
                    subject: request.subject.clone(),
                    label: request.label.clone(),
                });
            }
            ui.close();
        }
        if (state.selected_point.is_some() || state.selected_series_id.is_some())
            && ui.button("Pin Inspector").clicked()
        {
            *action = Some(ChartPageAction::PinCurrentSelection);
            ui.close();
        }
        if ui.button("Clear Chart Selection").clicked() {
            state.clear_chart_selection();
            *action = Some(ChartPageAction::Feedback(
                "CMD: cleared chart selection".to_owned(),
            ));
            ui.close();
        }
        if ui.button("Clear Overlays").clicked() {
            if state.clear_overlays() {
                *action = Some(ChartPageAction::Feedback(
                    "CMD: cleared overlays".to_owned(),
                ));
            }
            ui.close();
        }
        if ui.button("Explain Series").clicked() {
            *action = Some(ChartPageAction::Feedback(format!(
                "MOCK explain {} | source/status in data table",
                request.label
            )));
            ui.close();
        }
        if ui.button("Reset Plot").clicked() {
            *action = Some(ChartPageAction::Feedback(
                "CMD: use Reset Plot control".to_owned(),
            ));
            ui.close();
        }
        ui.separator();
        ui.label("Value mode");
        if ui.button("Raw").clicked() {
            state.value_mode = ChartValueMode::Raw;
            *action = Some(ChartPageAction::Feedback("CMD: chart value raw".to_owned()));
            ui.close();
        }
        if ui.button("Rebased 100").clicked() {
            state.value_mode = ChartValueMode::Rebased100;
            *action = Some(ChartPageAction::Feedback(
                "CMD: chart value rebased 100".to_owned(),
            ));
            ui.close();
        }
        if ui.button("% Change").clicked() {
            state.value_mode = ChartValueMode::PercentChange;
            *action = Some(ChartPageAction::Feedback(
                "CMD: chart value percent".to_owned(),
            ));
            ui.close();
        }
        ui.separator();
        if ui.button("Open Subject").clicked() {
            *action = Some(ChartPageAction::OpenSubject {
                subject: request.subject.clone(),
                label: request.label.clone(),
            });
            ui.close();
        }
        if ui.button("Copy Series Label").clicked() {
            ui.copy_text(request.label.clone());
            *action = Some(ChartPageAction::Feedback(format!(
                "COPIED: {}",
                request.label
            )));
            ui.close();
        }
    });

    plot_point_readout(ui, hover_nearest.as_ref(), state.selected_point.as_ref());
}

fn draw_plot_marker(
    plot_ui: &mut PlotUi<'_>,
    marker: &ChartNearestPoint,
    label: &str,
    color: egui::Color32,
) {
    plot_ui.vline(
        VLine::new(format!("{label} guide"), marker.x)
            .color(color)
            .width(1.0)
            .allow_hover(false),
    );
    let points: PlotPoints<'_> = std::iter::once([marker.x, marker.y]).collect();
    plot_ui.points(
        Points::new(format!("{label} marker"), points)
            .radius(4.0)
            .color(color)
            .allow_hover(false),
    );
}

fn plot_distance_scale(plot_ui: &PlotUi<'_>) -> ChartPointDistanceScale {
    let origin = plot_ui.screen_from_plot(PlotPoint::new(0.0, 0.0));
    let x = plot_ui.screen_from_plot(PlotPoint::new(1.0, 0.0));
    let y = plot_ui.screen_from_plot(PlotPoint::new(0.0, 1.0));
    ChartPointDistanceScale::new((x.x - origin.x) as f64, (y.y - origin.y) as f64)
}

fn select_nearest_plot_point(
    state: &mut ChartPanelState,
    nearest: &ChartNearestPoint,
    rows: &[ChartDataRow],
) {
    if let Some((row_index, row_data)) = rows
        .iter()
        .enumerate()
        .find(|(_, row)| chart_row_matches_nearest(row, nearest))
    {
        select_chart_cell(state, row_index, row_data, ChartDataColumn::Value);
    } else {
        state.data_table.clear_selection();
        state.table_focus.clear();
        state.select_point(nearest.point_selection());
    }
}

fn chart_row_matches_nearest(row: &ChartDataRow, nearest: &ChartNearestPoint) -> bool {
    row.series_id == nearest.series_id
        && row.date == nearest.date
        && row.value_mode == nearest.value_mode
        && (row.value - nearest.value).abs() <= f64::EPSILON
        && (row.raw_value - nearest.raw_value).abs() <= f64::EPSILON
}

fn nearest_point_tooltip(nearest: &ChartNearestPoint) -> String {
    let mut lines = vec![
        format!("Cursor: {}", nearest.cursor_date.as_deref().unwrap_or("-")),
        format!("Nearest: {}", nearest.date),
        format!("Series: {}", nearest.series_label),
        format!("Subject: {}", nearest.subject.short_id()),
        format!(
            "Display: {}",
            display_value_label(nearest.value, &nearest.unit, nearest.value_mode)
        ),
        format!(
            "Raw: {} {}",
            format_number(nearest.raw_value, 4),
            nearest.raw_unit
        ),
        format!("Source: {}", nearest.source),
        format!("Status: {}", nearest.status),
        format!("Value mode: {}", nearest.value_mode.label()),
    ];
    lines.push(format!("Distance: {:.1}", nearest.distance_screen_or_plot));
    lines.join("\n")
}

fn plot_point_readout(
    ui: &mut egui::Ui,
    hover: Option<&ChartNearestPoint>,
    selected: Option<&ChartPointSelection>,
) {
    if let Some(point) = hover {
        ui.horizontal_wrapped(|ui| {
            style::badge(
                ui,
                "HOVER",
                style::BadgeTone::Info,
                Some("Nearest plotted point under the cursor.".to_owned()),
            );
            ui.monospace(format!(
                "{} | {} | cursor {} | nearest {} | {}",
                point.series_label,
                point.subject.short_id(),
                point.cursor_date.as_deref().unwrap_or("-"),
                point.date,
                display_value_label(point.value, &point.unit, point.value_mode)
            ));
            if point.value_mode.is_transformed() {
                ui.monospace(format!(
                    "raw {} {}",
                    format_number(point.raw_value, 4),
                    point.raw_unit
                ));
            }
            style::source_badge(ui, &point.source);
            style::status_badge(ui, &point.status);
            style::badge(
                ui,
                point.value_mode.compact_label(),
                if point.value_mode.is_transformed() {
                    style::BadgeTone::Derived
                } else {
                    style::BadgeTone::Neutral
                },
                Some(point.value_mode.tooltip().to_owned()),
            );
        });
    } else if let Some(point) = selected {
        ui.horizontal_wrapped(|ui| {
            style::badge(
                ui,
                "SELECTED",
                style::BadgeTone::Info,
                Some(
                    "Selected chart point. Ctrl/Cmd+C copies this after focused cells.".to_owned(),
                ),
            );
            ui.monospace(format!(
                "{} | {} | {}",
                point.series_label,
                point.date,
                display_value_label(point.value, &point.unit, point.value_mode)
            ));
            if point.value_mode.is_transformed() {
                ui.monospace(format!(
                    "raw {} {}",
                    format_number(point.raw_value, 4),
                    point.raw_unit
                ));
            }
            style::source_badge(ui, &point.source);
            style::status_badge(ui, &point.status);
            style::badge(
                ui,
                point.value_mode.compact_label(),
                if point.value_mode.is_transformed() {
                    style::BadgeTone::Derived
                } else {
                    style::BadgeTone::Neutral
                },
                Some(point.value_mode.tooltip().to_owned()),
            );
        });
    }
}

fn selected_series<'a>(
    series_set: &'a ChartSeriesSet,
    state: &ChartPanelState,
) -> Option<&'a ChartSeriesSpec> {
    let selected_id = state.selected_series_id.as_ref()?;
    series_set
        .series
        .iter()
        .find(|series| series.id == *selected_id)
}

fn comparison_summary_table(
    ui: &mut egui::Ui,
    series: &[ChartSeriesSpec],
    range: PlotRange,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
) {
    let [left, right, ..] = series else {
        return;
    };
    let left_points = display_points(left, range, value_mode, base_date);
    let right_points = display_points(right, range, value_mode, base_date);
    let common_dates = left_points
        .iter()
        .filter(|left_point| {
            right_points
                .iter()
                .any(|right_point| right_point.date == left_point.date)
        })
        .map(|point| point.date.clone())
        .collect::<Vec<_>>();
    let common_points = common_dates.len();
    let first_common = common_dates.first().map(String::as_str).unwrap_or("-");
    let latest_common = common_dates.last().map(String::as_str).unwrap_or("-");
    let latest_common_date = common_dates.last().map(String::as_str);
    let left_latest_common =
        latest_common_date.and_then(|date| left_points.iter().find(|point| point.date == date));
    let right_latest_common =
        latest_common_date.and_then(|date| right_points.iter().find(|point| point.date == date));
    let left_raw_points = visible_points(left, range);
    let right_raw_points = visible_points(right, range);
    let left_raw_latest_common =
        latest_common_date.and_then(|date| left_raw_points.iter().find(|point| point.date == date));
    let right_raw_latest_common = latest_common_date
        .and_then(|date| right_raw_points.iter().find(|point| point.date == date));
    let (diff, pct_diff) = match (left_latest_common, right_latest_common) {
        (Some(left_point), Some(right_point)) => {
            let diff = left_point.value - right_point.value;
            let pct = if right_point.value.abs() > f64::EPSILON {
                Some(diff / right_point.value)
            } else {
                None
            };
            (Some(diff), pct)
        }
        _ => (None, None),
    };

    ui.label(egui::RichText::new("Comparison summary").strong());
    TableBuilder::new(ui)
        .id_salt("charts_comparison_summary")
        .striped(true)
        .resizable(true)
        .max_scroll_height(96.0)
        .column(Column::initial(120.0).at_least(90.0))
        .column(Column::initial(160.0).at_least(120.0).clip(true))
        .column(Column::initial(160.0).at_least(120.0).clip(true))
        .column(Column::initial(150.0).at_least(112.0))
        .column(Column::remainder().at_least(160.0))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for label in ["Metric", "A", "B", "A - B", "Source / status"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                row.col(|ui| {
                    ui.label("Latest common");
                });
                row.col(|ui| {
                    ui.monospace(latest_display_value_label(
                        left_latest_common,
                        value_mode.display_unit(&left.unit).as_str(),
                        value_mode,
                    ));
                });
                row.col(|ui| {
                    ui.monospace(latest_display_value_label(
                        right_latest_common,
                        value_mode.display_unit(&right.unit).as_str(),
                        value_mode,
                    ));
                });
                row.col(|ui| {
                    ui.monospace(display_diff_label(diff, value_mode));
                });
                row.col(|ui| {
                    ui.monospace(format!(
                        "{} / {} | {} / {} | latest {}",
                        format_source(&left.source),
                        format_source(&right.source),
                        left.status,
                        right.status,
                        latest_common
                    ));
                });
            });
            body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                row.col(|ui| {
                    ui.label("% difference");
                });
                row.col(|ui| {
                    ui.monospace(left.label.as_str());
                });
                row.col(|ui| {
                    ui.monospace(right.label.as_str());
                });
                row.col(|ui| {
                    ui.monospace(
                        pct_diff
                            .map(crate::format::fmt_percent)
                            .unwrap_or_else(|| "-".to_owned()),
                    );
                });
                row.col(|ui| {
                    ui.monospace(format!(
                        "{} common / {} A / {} B",
                        common_points,
                        left_points.len(),
                        right_points.len()
                    ));
                });
            });
            body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                row.col(|ui| {
                    ui.label("Latest raw common");
                });
                row.col(|ui| {
                    ui.monospace(raw_value_label(left_raw_latest_common, &left.unit));
                });
                row.col(|ui| {
                    ui.monospace(raw_value_label(right_raw_latest_common, &right.unit));
                });
                row.col(|ui| {
                    let raw_diff = match (left_raw_latest_common, right_raw_latest_common) {
                        (Some(left), Some(right)) => Some(left.value - right.value),
                        _ => None,
                    };
                    ui.monospace(
                        raw_diff
                            .map(|value| format_number(value, 4))
                            .unwrap_or_else(|| "-".to_owned()),
                    );
                });
                row.col(|ui| {
                    ui.monospace("Raw values before chart display transform");
                });
            });
            body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                row.col(|ui| {
                    ui.label("Common range");
                });
                row.col(|ui| {
                    ui.monospace(first_common);
                });
                row.col(|ui| {
                    ui.monospace(latest_common);
                });
                row.col(|ui| {
                    ui.monospace(value_mode.label());
                });
                row.col(|ui| {
                    ui.monospace(
                        base_date
                            .map(|date| format!("base date {date}"))
                            .unwrap_or_else(|| "base date -".to_owned()),
                    );
                });
            });
        });
}

fn latest_display_value_label(
    point: Option<&DisplayPoint>,
    unit: &str,
    value_mode: ChartValueMode,
) -> String {
    point
        .map(|point| display_value_label(point.value, unit, value_mode))
        .unwrap_or_else(|| "-".to_owned())
}

fn raw_value_label(point: Option<&TimeSeriesPoint>, unit: &str) -> String {
    point
        .map(|point| format!("{} {}", format_number(point.value, 4), unit))
        .unwrap_or_else(|| "-".to_owned())
}

fn series_selector(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    request: &PlotRequest,
    series: &[ChartSeriesSpec],
    display: ChartDisplayContext<'_>,
    action: &mut Option<ChartPageAction>,
) {
    ui.horizontal_wrapped(|ui| {
        ui.label("Series");
        for spec in series {
            let selected = state.selected_series_id.as_ref() == Some(&spec.id);
            let response = ui
                .selectable_label(selected, format!("{} {}", spec.role.label(), spec.label))
                .on_hover_text(format!(
                    "{}\nKind: {}\nUnit: {}\nSource: {}\nStatus: {}",
                    spec.label,
                    spec.kind.as_str(),
                    spec.unit,
                    spec.source,
                    spec.status
                ))
                .on_hover_cursor(egui::CursorIcon::PointingHand);
            if response.clicked() {
                state.select_series(spec.id.clone());
            }
            response.context_menu(|ui| {
                if ui.button("Select Series").clicked() {
                    state.select_series(spec.id.clone());
                    ui.close();
                }
                if ui.button("Open Subject").clicked() {
                    state.select_series(spec.id.clone());
                    *action = Some(ChartPageAction::OpenSubject {
                        subject: spec.subject.clone(),
                        label: spec.label.clone(),
                    });
                    ui.close();
                }
                if ui.button("Copy Series Label").clicked() {
                    ui.copy_text(spec.label.clone());
                    state.select_series(spec.id.clone());
                    *action = Some(ChartPageAction::Feedback(format!("COPIED: {}", spec.label)));
                    ui.close();
                }
                if ui.button("Copy Series Data").clicked() {
                    ui.copy_text(series_display_data_text(
                        spec,
                        display.range,
                        display.value_mode,
                        display.base_date,
                    ));
                    state.select_series(spec.id.clone());
                    *action = Some(ChartPageAction::Feedback(format!(
                        "COPIED: series {}",
                        spec.label
                    )));
                    ui.close();
                }
                if ui.button("Pin Inspector").clicked() {
                    state.select_series(spec.id.clone());
                    *action = Some(ChartPageAction::PinCurrentSelection);
                    ui.close();
                }
                if matches!(
                    spec.role,
                    ChartSeriesRole::SubjectOverlay | ChartSeriesRole::Comparison
                ) && ui.button("Remove Overlay").clicked()
                {
                    let removed = state.remove_comparison_subject(&spec.subject);
                    *action = Some(ChartPageAction::Feedback(if removed {
                        format!("CMD: removed overlay {}", spec.label)
                    } else {
                        "CMD: overlay unavailable".to_owned()
                    }));
                    ui.close();
                }
                if spec.subject != request.subject && ui.button("Make Primary").clicked() {
                    let mut next = request.clone();
                    next.subject = spec.subject.clone();
                    next.series_kind = spec.kind;
                    next.label = format!("{} {}", spec.label, spec.kind.as_str());
                    next.comparison_subjects
                        .retain(|subject| *subject != spec.subject);
                    if request.subject != spec.subject
                        && !next.comparison_subjects.contains(&request.subject)
                    {
                        next.comparison_subjects.insert(0, request.subject.clone());
                    }
                    state.set_plot(next);
                    *action = Some(ChartPageAction::Feedback(format!(
                        "CMD: primary {}",
                        spec.label
                    )));
                    ui.close();
                }
            });
        }
    });
}

fn spread_status_row(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    request: &PlotRequest,
    spread: &crate::charts::SpreadSeriesSummary,
    series: &[ChartSeriesSpec],
    action: &mut Option<ChartPageAction>,
) {
    let expression = format!("{} - {}", spread.left_label, spread.right_label);
    let latest = series
        .first()
        .and_then(|series| series.points.last())
        .map(|point| (point.date.as_str(), point.value));
    ui.horizontal_wrapped(|ui| {
        style::derived_badge(ui);
        style::status_badge(ui, &spread.status);
        style::source_badge(ui, &spread.left_source);
        style::source_badge(ui, &spread.right_source);
        ui.monospace(&expression);
        if let Some((date, value)) = latest {
            ui.monospace(format!("latest {} @ {}", format_number(value, 4), date));
        }
        ui.monospace(spread.coverage_label())
            .on_hover_text(format!(
                "Spread is derived from matching dates only.\nLeft: {} / {}\nRight: {} / {}\nPartial: {}",
                spread.left_label,
                spread.left_source,
                spread.right_label,
                spread.right_source,
                spread.partial
            ))
            .on_hover_cursor(egui::CursorIcon::Help);
        if crate::ui::actions::action_button(ui, "Copy Expr", "Copy A - B expression.").clicked() {
            ui.copy_text(expression.clone());
            *action = Some(ChartPageAction::Feedback(format!("COPIED: {expression}")));
        }
        if crate::ui::actions::action_button_enabled(
            ui,
            latest.is_some(),
            "Copy Latest",
            "Copy latest derived spread value.",
        )
        .clicked()
            && let Some((date, value)) = latest
        {
            let text = [date.to_owned(), format_number(value, 4), expression.clone()].join("\t");
            ui.copy_text(text);
            *action = Some(ChartPageAction::Feedback(format!("COPIED: spread {date}")));
        }
        if crate::ui::actions::action_button_enabled(
            ui,
            !series.is_empty(),
            "Copy Series",
            "Copy derived spread series.",
        )
        .clicked()
            && let Some(series) = series.first()
        {
            ui.copy_text(series_display_data_text(
                series,
                request.options.range,
                ChartValueMode::Raw,
                None,
            ));
            *action = Some(ChartPageAction::Feedback(format!(
                "COPIED: spread series {}",
                series.label
            )));
        }
        if crate::ui::actions::action_button(ui, "Swap A/B", "Swap spread operands.").clicked() {
            *action = Some(ChartPageAction::Feedback(if state.swap_operands() {
                "CMD: swapped A/B".to_owned()
            } else {
                "CMD: no B subject to swap".to_owned()
            }));
        }
        if crate::ui::actions::action_button(ui, "Compare A/B", "Show A/B as comparison series.")
            .clicked()
        {
            state.set_mode(ChartMode::Compare);
            *action = Some(ChartPageAction::Feedback("CMD: compare A/B".to_owned()));
        }
        if crate::ui::actions::action_button(ui, "Open A", "Open primary spread subject.").clicked()
        {
            *action = Some(ChartPageAction::OpenSubject {
                subject: request.subject.clone(),
                label: spread.left_label.clone(),
            });
        }
        if crate::ui::actions::action_button_enabled(
            ui,
            !request.comparison_subjects.is_empty(),
            "Open B",
            "Open secondary spread subject.",
        )
        .clicked()
            && let Some(subject) = request.comparison_subjects.first()
        {
            *action = Some(ChartPageAction::OpenSubject {
                subject: subject.clone(),
                label: spread.right_label.clone(),
            });
        }
    });
}

fn line_from_series(
    series: &ChartSeriesSpec,
    range: PlotRange,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
) -> Line<'static> {
    let points: PlotPoints<'_> = plot_points_from_series(series, range, value_mode, base_date)
        .into_iter()
        .collect();
    Line::new(series.label.clone(), points)
}

fn plot_points_from_series(
    series: &ChartSeriesSpec,
    range: PlotRange,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
) -> Vec<[f64; 2]> {
    display_points(series, range, value_mode, base_date)
        .into_iter()
        .filter_map(|point| {
            let x = ChartDateAxis::date_to_x(&point.date)?;
            point.value.is_finite().then_some([x, point.value])
        })
        .collect()
}

fn series_display_data_text(
    series: &ChartSeriesSpec,
    range: PlotRange,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
) -> String {
    let mut lines = vec![
        "date\tseries\tdisplay_value\tdisplay_unit\traw_value\traw_unit\tsource\tstatus\tkind\trole\tvalue_mode\tx_mode"
            .to_owned(),
    ];
    let display_unit = value_mode.display_unit(&series.unit);
    lines.extend(
        display_points(series, range, value_mode, base_date)
            .into_iter()
            .map(|point| {
                [
                    point.date,
                    series.label.clone(),
                    point.value.to_string(),
                    display_unit.clone(),
                    point.raw_value.to_string(),
                    series.unit.clone(),
                    point.source,
                    point.status,
                    series.kind.as_str().to_owned(),
                    series.role.label().to_owned(),
                    value_mode.label().to_owned(),
                    "date".to_owned(),
                ]
                .join("\t")
            }),
    );
    lines.join("\n")
}

fn plot_metadata(
    ui: &mut egui::Ui,
    state: &ChartPanelState,
    request: &PlotRequest,
    series_set: &ChartSeriesSet,
    snapshot: &DashboardSnapshot,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
) {
    ui.label(egui::RichText::new("Selected").strong());
    let subject = subject_label(snapshot, &request.subject);
    let overlays = request
        .overlay_series_kinds
        .iter()
        .map(|kind| kind.as_str())
        .collect::<Vec<_>>()
        .join(", ")
        .if_empty("-");
    let rows = vec![
        KvRow::new("Subject", subject.clone())
            .with_tooltip(format!("Active chart subject\n{subject}"))
            .copyable(),
        KvRow::new("Mode", state.mode.label()),
        KvRow::new(
            "Value mode",
            effective_value_mode(state.mode, value_mode).label(),
        )
        .with_tooltip(value_mode_tooltip(state.mode, value_mode)),
        KvRow::new("Base date", base_date.unwrap_or("-"))
            .with_tooltip("First valid common point used for rebased/percent display modes."),
        KvRow::new("Kind", request.series_kind.as_str()),
        KvRow::new("Range", request.options.range.as_str()),
        KvRow::new("Compare", request.comparison_subjects.len().to_string()),
        KvRow::new("Overlays", overlays).with_tooltip("Overlay series on this chart"),
        KvRow::new("History", state.recent_plots.len().to_string()),
    ];
    kv_grid(ui, "charts_plot_request_grid", &rows);

    if let Some(spread) = series_set.spread.as_ref() {
        ui.separator();
        ui.label(egui::RichText::new("Spread derivation").strong());
        let source_rows = vec![
            KvRow::new(
                "Expression",
                format!("{} - {}", spread.left_label, spread.right_label),
            ),
            KvRow::new("Coverage", spread.coverage_label()).with_tooltip(
                "Derived with matching dates only; unmatched dates are not fabricated.",
            ),
            KvRow::new("Source A", spread.left_source.clone())
                .with_short_value(format_source(&spread.left_source))
                .copyable(),
            KvRow::new("Source B", spread.right_source.clone())
                .with_short_value(format_source(&spread.right_source))
                .copyable(),
            KvRow::new("Status", spread.status.clone()),
        ];
        kv_grid(ui, "charts_spread_source_grid", &source_rows);
    } else if let Some(series) = series_set.series.first() {
        ui.separator();
        ui.label(egui::RichText::new("Source").strong());
        let source_rows = vec![
            KvRow::new("Points", series.points.len().to_string()),
            KvRow::new("Source", series.source.clone())
                .with_short_value(format_source(&series.source))
                .with_tooltip("Series-level source; point-level provenance is in the table")
                .copyable(),
            KvRow::new("Status", series.status.clone()),
            KvRow::new("Unit", series.unit.clone()),
        ];
        kv_grid(ui, "charts_series_source_grid", &source_rows);
    }
}

#[derive(Clone, Debug)]
struct ChartDataRow {
    series_id: ChartSeriesId,
    subject: AnalysisSubject,
    subject_label: String,
    date: String,
    series: String,
    kind: TimeSeriesKind,
    value: f64,
    raw_value: f64,
    unit: String,
    raw_unit: String,
    source: String,
    status: String,
    role: ChartSeriesRole,
    value_mode: ChartValueMode,
}

impl ChartDataRow {
    fn point_selection(&self) -> ChartPointSelection {
        ChartPointSelection {
            series_id: self.series_id.clone(),
            subject: self.subject.clone(),
            series_label: self.series.clone(),
            series_kind: self.kind,
            date: self.date.clone(),
            value: self.value,
            raw_value: self.raw_value,
            unit: self.unit.clone(),
            raw_unit: self.raw_unit.clone(),
            source: self.source.clone(),
            status: self.status.clone(),
            value_mode: self.value_mode,
        }
    }

    fn copy_text(&self) -> String {
        [
            self.date.clone(),
            self.series.clone(),
            self.subject_label.clone(),
            format_number(self.value, 4),
            self.unit.clone(),
            format_number(self.raw_value, 4),
            self.raw_unit.clone(),
            self.source.clone(),
            self.status.clone(),
            self.kind.as_str().to_owned(),
            self.role.label().to_owned(),
            self.value_mode.label().to_owned(),
            "date".to_owned(),
        ]
        .join("\t")
    }

    fn cell_payload(&self, column: ChartDataColumn) -> (String, String) {
        match column {
            ChartDataColumn::Date => (self.date.clone(), self.date.clone()),
            ChartDataColumn::Series => (self.series.clone(), self.series.clone()),
            ChartDataColumn::Subject => (self.subject_label.clone(), self.subject_label.clone()),
            ChartDataColumn::Value => (
                display_value_label(self.value, &self.unit, self.value_mode),
                self.value.to_string(),
            ),
            ChartDataColumn::RawValue => (
                format!("{} {}", format_number(self.raw_value, 4), self.raw_unit),
                self.raw_value.to_string(),
            ),
            ChartDataColumn::Unit => (self.unit.clone(), self.unit.clone()),
            ChartDataColumn::Source => (format_source(&self.source), self.source.clone()),
            ChartDataColumn::Status => (self.status.clone(), self.status.clone()),
            ChartDataColumn::Kind => (self.kind.as_str().to_owned(), self.kind.as_str().to_owned()),
        }
    }
}

fn select_chart_cell(
    state: &mut ChartPanelState,
    row_index: usize,
    row_data: &ChartDataRow,
    column: ChartDataColumn,
) {
    let (display, raw) = row_data.cell_payload(column);
    let column_index = ChartDataColumn::ALL
        .iter()
        .position(|candidate| *candidate == column)
        .unwrap_or_default();
    state
        .data_table
        .select_cell(row_index, column_index, column.as_str(), display, raw);
    state.set_table_focus(row_index, column);
    state.select_point(row_data.point_selection());
}

fn select_relative_point_in_rows(
    state: &mut ChartPanelState,
    rows: &[ChartDataRow],
    offset: isize,
) -> bool {
    if rows.is_empty() {
        state.clear_chart_selection();
        return false;
    }

    let selected_series_id = state
        .selected_series_id
        .clone()
        .or_else(|| rows.first().map(|row| row.series_id.clone()));
    let Some(series_id) = selected_series_id else {
        return false;
    };

    let mut row_indices = rows
        .iter()
        .enumerate()
        .filter_map(|(row_index, row)| (row.series_id == series_id).then_some(row_index))
        .collect::<Vec<_>>();
    row_indices.sort_by(|left, right| {
        rows[*left]
            .date
            .cmp(&rows[*right].date)
            .then_with(|| rows[*left].series.cmp(&rows[*right].series))
    });
    if row_indices.is_empty() {
        return false;
    }

    let current_row = state
        .selected_point
        .as_ref()
        .and_then(|point| {
            rows.iter().position(|row| {
                row.series_id == point.series_id
                    && row.date == point.date
                    && row.value_mode == point.value_mode
            })
        })
        .or_else(|| {
            state.table_focus.row_index.filter(|row_index| {
                rows.get(*row_index)
                    .is_some_and(|row| row.series_id == series_id)
            })
        });
    let current_position =
        current_row.and_then(|row_index| row_indices.iter().position(|index| *index == row_index));
    let len = row_indices.len();
    let last_position = len - 1;
    let step = offset.unsigned_abs() % len;
    let next_position = match (current_position, offset.cmp(&0)) {
        (Some(position), Ordering::Less) => (position + len - step) % len,
        (Some(position), Ordering::Greater) => (position + step) % len,
        (Some(position), Ordering::Equal) => position,
        (None, Ordering::Less) => last_position,
        (None, Ordering::Equal | Ordering::Greater) => 0,
    };
    let row_index = row_indices[next_position];
    let Some(row_data) = rows.get(row_index) else {
        return false;
    };
    let column = state.table_focus.column_or_default();
    select_chart_cell(state, row_index, row_data, column);
    true
}

fn chart_cell(
    ui: &mut egui::Ui,
    state: &mut ChartPanelState,
    row_index: usize,
    row_data: &ChartDataRow,
    column: ChartDataColumn,
    text: impl Into<String>,
    tooltip: impl Into<String>,
) -> egui::Response {
    let focused =
        state.table_focus.row_index == Some(row_index) && state.table_focus.column == Some(column);
    let selected = focused || state.data_table.selection.is_selected(row_index);
    let text = text.into();
    let tooltip = tooltip.into();
    let add_cell = |ui: &mut egui::Ui| {
        ui.selectable_label(selected, text)
            .on_hover_text(tooltip)
            .on_hover_cursor(egui::CursorIcon::PointingHand)
    };
    let response = if matches!(column, ChartDataColumn::Value | ChartDataColumn::RawValue) {
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), add_cell)
            .inner
    } else {
        add_cell(ui)
    }
    .on_hover_cursor(egui::CursorIcon::PointingHand);
    let response = if focused {
        response.highlight().on_hover_text(
            "Focused cell. Arrow keys move focus; Ctrl/Cmd+C copies this cell first.",
        )
    } else {
        response
    };
    if response.clicked() {
        select_chart_cell(state, row_index, row_data, column);
    }
    response
}

fn series_table(
    ui: &mut egui::Ui,
    id: &str,
    series: &[ChartSeriesSpec],
    display: ChartDisplayContext<'_>,
    max_height: f32,
    state: &mut ChartPanelState,
    action: &mut Option<ChartPageAction>,
) {
    ui.label(egui::RichText::new("Series data").strong());
    let rows = sorted_chart_rows(
        series,
        display.range,
        display.value_mode,
        display.base_date,
        state.data_table.sort.as_ref(),
    );
    if rows.is_empty() {
        style::state_message(
            ui,
            "EMPTY",
            "No chart points are visible for the selected range and value mode.",
        );
        return;
    }
    TableBuilder::new(ui)
        .id_salt(id)
        .striped(true)
        .resizable(true)
        .auto_shrink(false)
        .max_scroll_height(max_height.max(150.0))
        .column(Column::initial(96.0).at_least(78.0))
        .column(Column::initial(160.0).at_least(110.0).clip(true))
        .column(Column::initial(100.0).at_least(78.0).clip(true))
        .column(Column::initial(102.0).at_least(82.0))
        .column(Column::initial(102.0).at_least(82.0))
        .column(Column::initial(68.0).at_least(54.0))
        .column(Column::initial(100.0).at_least(76.0))
        .column(Column::initial(92.0).at_least(70.0))
        .column(Column::remainder().at_least(88.0))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| {
                crate::pages::sortable_header_cell(ui, &mut state.data_table, COL_DATE, "Date")
            });
            header.col(|ui| {
                crate::pages::sortable_header_cell(ui, &mut state.data_table, COL_SERIES, "Series")
            });
            header.col(|ui| {
                crate::pages::sortable_header_cell(
                    ui,
                    &mut state.data_table,
                    COL_SUBJECT,
                    "Subject",
                )
            });
            header.col(|ui| {
                crate::pages::sortable_header_cell(ui, &mut state.data_table, COL_VALUE, "Value")
            });
            header.col(|ui| {
                crate::pages::sortable_header_cell(ui, &mut state.data_table, COL_RAW_VALUE, "Raw")
            });
            header.col(|ui| {
                crate::pages::sortable_header_cell(ui, &mut state.data_table, COL_UNIT, "Unit")
            });
            header.col(|ui| {
                crate::pages::sortable_header_cell(ui, &mut state.data_table, COL_SOURCE, "Source")
            });
            header.col(|ui| {
                crate::pages::sortable_header_cell(ui, &mut state.data_table, COL_STATUS, "Status")
            });
            header.col(|ui| {
                crate::pages::sortable_header_cell(ui, &mut state.data_table, COL_KIND, "Kind")
            });
        })
        .body(|mut body| {
            for (row_index, row_data) in rows.iter().enumerate() {
                body.row(crate::ui::metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_selected(
                        state.data_table.selection.is_selected(row_index)
                            || state.table_focus.row_index == Some(row_index),
                    );
                    row.col(|ui| {
                        let response = chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::Date,
                            row_data.date.clone(),
                            "Click to focus date cell and select chart point; double-click to open subject.",
                        );
                        if response.double_clicked() {
                            *action = Some(ChartPageAction::OpenSubject {
                                subject: row_data.subject.clone(),
                                label: row_data.subject_label.clone(),
                            });
                        }
                        response.context_menu(|ui| {
                            if ui.button("Copy Value").clicked() {
                                ui.copy_text(display_value_label(
                                    row_data.value,
                                    &row_data.unit,
                                    row_data.value_mode,
                                ));
                                *action = Some(ChartPageAction::Feedback(format!(
                                    "COPIED: {} {}",
                                    row_data.series, row_data.date
                                )));
                                ui.close();
                            }
                            if ui.button("Copy Row").clicked() {
                                ui.copy_text(row_data.copy_text());
                                *action = Some(ChartPageAction::Feedback(format!(
                                    "COPIED: row {}",
                                    row_data.series
                                )));
                                ui.close();
                            }
                            if ui.button("Open Subject").clicked() {
                                *action = Some(ChartPageAction::OpenSubject {
                                    subject: row_data.subject.clone(),
                                    label: row_data.subject_label.clone(),
                                });
                                ui.close();
                            }
                            if ui.button("Pin Inspector").clicked() {
                                select_chart_cell(
                                    state,
                                    row_index,
                                    row_data,
                                    ChartDataColumn::Date,
                                );
                                *action = Some(ChartPageAction::PinCurrentSelection);
                                ui.close();
                            }
                            if ui.button("Copy Raw Value").clicked() {
                                ui.copy_text(format_number(row_data.raw_value, 4));
                                *action = Some(ChartPageAction::Feedback(format!(
                                    "COPIED: raw {} {}",
                                    row_data.series, row_data.date
                                )));
                                ui.close();
                            }
                            if ui.button("Show Source").clicked() {
                                *action = Some(ChartPageAction::Feedback(format!(
                                    "SOURCE: {} | {} | {}",
                                    row_data.series, row_data.source, row_data.status
                                )));
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::Series,
                            row_data.series.clone(),
                            "Series label. Ctrl/Cmd+C copies this cell when focused.",
                        );
                    });
                    row.col(|ui| {
                        chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::Subject,
                            row_data.subject_label.clone(),
                            format!(
                                "{}\nDouble-click date cell or use row context menu to open.",
                                row_data.subject.kind_label()
                            ),
                        );
                    });
                    row.col(|ui| {
                        chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::Value,
                            display_value_label(row_data.value, &row_data.unit, row_data.value_mode),
                            "Displayed chart value for the current value mode.",
                        );
                    });
                    row.col(|ui| {
                        chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::RawValue,
                            format_number(row_data.raw_value, 4),
                            "Raw source value before any display transform.",
                        );
                    });
                    row.col(|ui| {
                        chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::Unit,
                            row_data.unit.clone(),
                            "Display unit for the current value mode.",
                        );
                    });
                    row.col(|ui| {
                        chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::Source,
                            format_source(&row_data.source),
                            format!("Source: {}", row_data.source),
                        );
                    });
                    row.col(|ui| {
                        chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::Status,
                            row_data.status.clone(),
                            "Point status.",
                        );
                    });
                    row.col(|ui| {
                        chart_cell(
                            ui,
                            state,
                            row_index,
                            row_data,
                            ChartDataColumn::Kind,
                            row_data.kind.as_str(),
                            "Time-series kind.",
                        );
                    });
                });
            }
        });
}

fn sorted_chart_rows(
    series: &[ChartSeriesSpec],
    range: PlotRange,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
    sort: Option<&SortSpec>,
) -> Vec<ChartDataRow> {
    let mut rows = series
        .iter()
        .flat_map(|series| {
            let unit = value_mode.display_unit(&series.unit);
            display_points(series, range, value_mode, base_date)
                .into_iter()
                .map(move |point| ChartDataRow {
                    series_id: series.id.clone(),
                    subject: series.subject.clone(),
                    subject_label: series.subject.short_id().to_owned(),
                    date: point.date,
                    series: series.label.clone(),
                    kind: series.kind,
                    value: point.value,
                    raw_value: point.raw_value,
                    unit: unit.clone(),
                    raw_unit: series.unit.clone(),
                    source: point.source,
                    status: point.status,
                    role: series.role,
                    value_mode,
                })
        })
        .collect::<Vec<_>>();

    if let Some(sort) = sort {
        rows.sort_by(|left, right| {
            sort.direction
                .apply(compare_chart_rows(left, right, &sort.column))
                .then_with(|| left.series.cmp(&right.series))
        });
    } else {
        rows.sort_by(|left, right| {
            right
                .date
                .cmp(&left.date)
                .then_with(|| left.series.cmp(&right.series))
        });
    }
    rows
}

fn compare_chart_rows(left: &ChartDataRow, right: &ChartDataRow, column: &str) -> Ordering {
    match column {
        COL_DATE => left.date.cmp(&right.date),
        COL_SERIES => left.series.cmp(&right.series),
        COL_SUBJECT => left.subject_label.cmp(&right.subject_label),
        COL_VALUE => left.value.total_cmp(&right.value),
        COL_RAW_VALUE => left.raw_value.total_cmp(&right.raw_value),
        COL_UNIT => left.unit.cmp(&right.unit),
        COL_SOURCE => left.source.cmp(&right.source),
        COL_STATUS => left.status.cmp(&right.status),
        COL_KIND => left.kind.as_str().cmp(right.kind.as_str()),
        _ => Ordering::Equal,
    }
}

fn plot_series_for_request(
    request: &PlotRequest,
    all_series: &[TimeSeries],
) -> Vec<ChartSeriesSpec> {
    chart_series_for_request(request, all_series).series
}

fn available_sources_for_request(request: &PlotRequest, all_series: &[TimeSeries]) -> Vec<String> {
    let mut sources = plot_series_for_request(request, all_series)
        .iter()
        .flat_map(|series| {
            std::iter::once(series.source.clone())
                .chain(series.points.iter().map(|point| point.source.clone()))
        })
        .collect::<Vec<_>>();
    sources.sort();
    sources.dedup();
    sources
}

fn visible_points(series: &ChartSeriesSpec, range: PlotRange) -> Vec<TimeSeriesPoint> {
    visible_chart_points_for_range(&series.points, range)
}

fn display_points(
    series: &ChartSeriesSpec,
    range: PlotRange,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
) -> Vec<DisplayPoint> {
    let visible = visible_points(series, range);
    display_points_for_mode(&visible, value_mode, base_date)
}

fn display_base_date(
    series: &[ChartSeriesSpec],
    range: PlotRange,
    value_mode: ChartValueMode,
) -> Option<String> {
    if value_mode == ChartValueMode::Raw {
        return None;
    }

    let visible = series
        .iter()
        .map(|series| visible_points(series, range))
        .collect::<Vec<_>>();
    let visible_refs = visible.iter().map(Vec::as_slice).collect::<Vec<_>>();
    first_valid_common_base_date(&visible_refs)
}

fn effective_value_mode(mode: ChartMode, requested: ChartValueMode) -> ChartValueMode {
    if mode == ChartMode::Spread {
        ChartValueMode::Raw
    } else {
        requested
    }
}

fn value_mode_tooltip(mode: ChartMode, requested: ChartValueMode) -> String {
    if mode == ChartMode::Spread && requested.is_transformed() {
        "Spread mode displays raw derived A - B values; rebased/percent spread transforms are deferred."
            .to_owned()
    } else {
        requested.tooltip().to_owned()
    }
}

fn transform_status_text(value_mode: ChartValueMode, base_date: Option<&str>) -> String {
    match (value_mode, base_date) {
        (ChartValueMode::Raw, _) => value_mode.tooltip().to_owned(),
        (_, Some(date)) => format!("{} Base date: {date}", value_mode.tooltip()),
        (_, None) => {
            format!(
                "{} No valid common base point is available.",
                value_mode.tooltip()
            )
        }
    }
}

fn display_value_label(value: f64, unit: &str, value_mode: ChartValueMode) -> String {
    match value_mode {
        ChartValueMode::Raw | ChartValueMode::Rebased100 => {
            format!("{} {}", format_number(value, 4), unit)
        }
        ChartValueMode::PercentChange => crate::format::fmt_percent(value),
    }
}

fn display_diff_label(diff: Option<f64>, value_mode: ChartValueMode) -> String {
    let Some(diff) = diff else {
        return "-".to_owned();
    };
    match value_mode {
        ChartValueMode::PercentChange => crate::format::fmt_percent(diff),
        ChartValueMode::Raw | ChartValueMode::Rebased100 => format_number(diff, 4),
    }
}

fn has_mixed_units(series: &[ChartSeriesSpec]) -> bool {
    let Some(first) = series.first() else {
        return false;
    };
    series.iter().any(|series| series.unit != first.unit)
}

fn data_kind_for_series(kind: TimeSeriesKind) -> DataKind {
    match kind {
        TimeSeriesKind::Price | TimeSeriesKind::MarketValue => DataKind::Price,
        TimeSeriesKind::Nav => DataKind::Nav,
        TimeSeriesKind::Distribution => DataKind::Distributions,
        TimeSeriesKind::FxRate => DataKind::Fx,
        TimeSeriesKind::PortfolioValue
        | TimeSeriesKind::PortfolioPnL
        | TimeSeriesKind::ProjectedIncome
        | TimeSeriesKind::CurvePoint
        | TimeSeriesKind::Yield
        | TimeSeriesKind::Custom => DataKind::Derived,
    }
}

fn subject_choices(snapshot: &DashboardSnapshot) -> Vec<SubjectChoice> {
    let mut choices = vec![SubjectChoice {
        key: subject_key(&AnalysisSubject::WorkspacePortfolio(
            snapshot.workspace.id.clone(),
        )),
        label: format!("Portfolio | {}", snapshot.workspace.name),
        short_label: "Portfolio".to_owned(),
        subject: AnalysisSubject::WorkspacePortfolio(snapshot.workspace.id.clone()),
    }];

    if let Some(selected) =
        crate::charts::plot_request_from_selected(&snapshot.selected, TimeSeriesKind::Price, "")
    {
        let label = subject_label(snapshot, &selected.subject);
        choices.push(SubjectChoice {
            key: format!("selected:{}", subject_key(&selected.subject)),
            label: format!("selected | {label}"),
            short_label: label,
            subject: selected.subject,
        });
    }

    for fund in &snapshot.funds {
        for listing in &fund.listings {
            let subject = AnalysisSubject::FundListing {
                fund_id: fund.id.clone(),
                listing_id: listing.id.clone(),
            };
            choices.push(SubjectChoice {
                key: subject_key(&subject),
                label: format!("{} | {} | {}", listing.ticker, listing.currency, fund.name),
                short_label: listing.ticker.clone(),
                subject,
            });
        }
    }

    choices
}

fn subject_key(subject: &AnalysisSubject) -> String {
    match subject {
        AnalysisSubject::WorkspacePortfolio(workspace_id) => format!("portfolio:{workspace_id}"),
        AnalysisSubject::Fund(fund_id) => format!("fund:{fund_id}"),
        AnalysisSubject::FundListing {
            fund_id,
            listing_id,
        } => format!("listing:{fund_id}:{listing_id}"),
        AnalysisSubject::Holding { ticker, source } => format!("holding:{ticker}:{source}"),
        AnalysisSubject::Cash(currency) => format!("cash:{currency}"),
        AnalysisSubject::SyntheticModel(model_id) => format!("model:{model_id}"),
    }
}

pub fn subject_label(snapshot: &DashboardSnapshot, subject: &AnalysisSubject) -> String {
    match subject {
        AnalysisSubject::WorkspacePortfolio(workspace_id) => snapshot
            .workspaces
            .iter()
            .find(|workspace| workspace.id == *workspace_id)
            .map(|workspace| workspace.name.clone())
            .unwrap_or_else(|| workspace_id.clone()),
        AnalysisSubject::Fund(fund_id) => snapshot
            .find_fund_by_id(fund_id)
            .map(|fund| fund.name.clone())
            .unwrap_or_else(|| fund_id.clone()),
        AnalysisSubject::FundListing {
            fund_id,
            listing_id,
        } => snapshot
            .find_fund_by_id(fund_id)
            .and_then(|fund| {
                fund.listings
                    .iter()
                    .find(|listing| listing.id == *listing_id)
                    .map(|listing| listing.ticker.clone())
            })
            .unwrap_or_else(|| listing_id.clone()),
        AnalysisSubject::Holding { ticker, source } => format!("{ticker} via {source}"),
        AnalysisSubject::Cash(currency) => format!("Cash {currency}"),
        AnalysisSubject::SyntheticModel(model_id) => model_id.clone(),
    }
}

trait EmptyLabel {
    fn if_empty(self, fallback: &str) -> String;
}

impl EmptyLabel for String {
    fn if_empty(self, fallback: &str) -> String {
        if self.is_empty() {
            fallback.to_owned()
        } else {
            self
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::table_state::SortDirection;

    fn date_x(date: &str) -> f64 {
        ChartDateAxis::date_to_x(date).expect("valid test date")
    }

    #[test]
    fn chart_rows_sort_by_value_desc() {
        let subject = AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned());
        let series = TimeSeries::new(
            "s",
            subject,
            "Series",
            TimeSeriesKind::PortfolioValue,
            "GBP",
            vec![
                TimeSeriesPoint::new("2026-06-19", 1.0, "FRESH", "seed"),
                TimeSeriesPoint::new("2026-06-20", 3.0, "FRESH", "seed"),
            ],
        );
        let series_refs = vec![ChartSeriesSpec::from_time_series(
            &series,
            ChartSeriesRole::Primary,
            "Series".to_owned(),
        )];
        let rows = sorted_chart_rows(
            &series_refs,
            PlotRange::All,
            ChartValueMode::Raw,
            None,
            Some(&SortSpec::new(COL_VALUE, SortDirection::Desc)),
        );

        assert_eq!(rows[0].value, 3.0);
    }

    #[test]
    fn chart_rows_include_rebased_display_and_raw_values() {
        let subject = AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned());
        let series = TimeSeries::new(
            "s",
            subject,
            "Series",
            TimeSeriesKind::PortfolioValue,
            "GBP",
            vec![
                TimeSeriesPoint::new("2026-06-19", 2.0, "FRESH", "seed"),
                TimeSeriesPoint::new("2026-06-20", 3.0, "FRESH", "seed"),
            ],
        );
        let series_refs = vec![ChartSeriesSpec::from_time_series(
            &series,
            ChartSeriesRole::Primary,
            "Series".to_owned(),
        )];
        let base_date = display_base_date(&series_refs, PlotRange::All, ChartValueMode::Rebased100);
        let rows = sorted_chart_rows(
            &series_refs,
            PlotRange::All,
            ChartValueMode::Rebased100,
            base_date.as_deref(),
            Some(&SortSpec::new(COL_DATE, SortDirection::Asc)),
        );

        assert_eq!(base_date.as_deref(), Some("2026-06-19"));
        assert_eq!(rows[0].value, 100.0);
        assert_eq!(rows[1].value, 150.0);
        assert_eq!(rows[1].raw_value, 3.0);
        assert_eq!(rows[0].unit, "index");
        assert_eq!(rows[0].raw_unit, "GBP");
    }

    #[test]
    fn plot_points_use_date_x_values() {
        let subject = AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned());
        let series = TimeSeries::new(
            "s",
            subject,
            "Series",
            TimeSeriesKind::PortfolioValue,
            "GBP",
            vec![
                TimeSeriesPoint::new("2026-06-19", 2.0, "FRESH", "seed"),
                TimeSeriesPoint::new("2026-06-22", 3.0, "FRESH", "seed"),
            ],
        );
        let series_ref = ChartSeriesSpec::from_time_series(
            &series,
            ChartSeriesRole::Primary,
            "Series".to_owned(),
        );

        let points =
            plot_points_from_series(&series_ref, PlotRange::All, ChartValueMode::Raw, None);

        assert_eq!(
            points,
            vec![[date_x("2026-06-19"), 2.0], [date_x("2026-06-22"), 3.0]]
        );
    }

    #[test]
    fn chart_row_copy_payload_marks_date_x_mode() {
        let subject = AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned());
        let series = TimeSeries::new(
            "s",
            subject,
            "Series",
            TimeSeriesKind::PortfolioValue,
            "GBP",
            vec![TimeSeriesPoint::new("2026-06-19", 2.0, "FRESH", "seed")],
        );
        let series_refs = vec![ChartSeriesSpec::from_time_series(
            &series,
            ChartSeriesRole::Primary,
            "Series".to_owned(),
        )];
        let rows = sorted_chart_rows(
            &series_refs,
            PlotRange::All,
            ChartValueMode::Raw,
            None,
            None,
        );

        assert!(rows[0].copy_text().ends_with("\tdate"));
    }

    #[test]
    fn plot_nearest_selection_updates_point_series_and_table_focus() {
        let subject = AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned());
        let series = TimeSeries::new(
            "s",
            subject,
            "Series",
            TimeSeriesKind::PortfolioValue,
            "GBP",
            vec![
                TimeSeriesPoint::new("2026-06-19", 2.0, "FRESH", "seed"),
                TimeSeriesPoint::new("2026-06-20", 3.0, "FRESH", "seed"),
            ],
        );
        let series_refs = vec![ChartSeriesSpec::from_time_series(
            &series,
            ChartSeriesRole::Primary,
            "Series".to_owned(),
        )];
        let set = ChartSeriesSet {
            series: series_refs.clone(),
            spread: None,
        };
        let nearest = find_nearest_chart_point(
            &set,
            PlotRange::All,
            ChartValueMode::Raw,
            date_x("2026-06-20"),
            3.0,
            1.0,
            ChartPointDistanceScale::new(1.0, 1.0),
        )
        .expect("nearest");
        let rows = sorted_chart_rows(
            &series_refs,
            PlotRange::All,
            ChartValueMode::Raw,
            None,
            None,
        );
        let mut state = ChartPanelState::default();

        select_nearest_plot_point(&mut state, &nearest, &rows);

        assert_eq!(state.selected_series_id, Some(nearest.series_id));
        assert_eq!(
            state
                .selected_point
                .as_ref()
                .map(|point| point.date.as_str()),
            Some("2026-06-20")
        );
        assert_eq!(state.table_focus.column, Some(ChartDataColumn::Value));
        assert_eq!(
            state
                .data_table
                .selected_cell
                .as_ref()
                .map(|cell| cell.column.as_str()),
            Some("value")
        );
        assert_eq!(state.table_focus.row_index, Some(0));
    }

    #[test]
    fn selected_series_point_cycling_maps_back_to_sorted_table_rows() {
        let subject = AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned());
        let series = TimeSeries::new(
            "s",
            subject,
            "Series",
            TimeSeriesKind::PortfolioValue,
            "GBP",
            vec![
                TimeSeriesPoint::new("2026-06-18", 1.0, "FRESH", "seed"),
                TimeSeriesPoint::new("2026-06-19", 2.0, "FRESH", "seed"),
                TimeSeriesPoint::new("2026-06-20", 3.0, "FRESH", "seed"),
            ],
        );
        let series_refs = vec![ChartSeriesSpec::from_time_series(
            &series,
            ChartSeriesRole::Primary,
            "Series".to_owned(),
        )];
        let rows = sorted_chart_rows(
            &series_refs,
            PlotRange::All,
            ChartValueMode::Raw,
            None,
            None,
        );
        let mut state = ChartPanelState::default();
        state.select_series(series_refs[0].id.clone());

        assert!(select_relative_point_in_rows(&mut state, &rows, 1));
        assert_eq!(
            state
                .selected_point
                .as_ref()
                .map(|point| point.date.as_str()),
            Some("2026-06-18")
        );
        assert!(select_relative_point_in_rows(&mut state, &rows, 1));
        assert_eq!(
            state
                .selected_point
                .as_ref()
                .map(|point| point.date.as_str()),
            Some("2026-06-19")
        );
        assert_eq!(
            rows[state.table_focus.row_index.expect("focused row")]
                .date
                .as_str(),
            "2026-06-19"
        );
        assert!(select_relative_point_in_rows(&mut state, &rows, 1));
        assert_eq!(
            state
                .selected_point
                .as_ref()
                .map(|point| point.date.as_str()),
            Some("2026-06-20")
        );
        assert!(select_relative_point_in_rows(&mut state, &rows, 1));
        assert_eq!(
            state
                .selected_point
                .as_ref()
                .map(|point| point.date.as_str()),
            Some("2026-06-18")
        );
    }
}
