use crate::app_model::SelectedInstrument;
use crate::charts::PlotRequest;
use crate::compute::portfolio::{PositionOverride, PositionOverrideField, apply_position_override};
use crate::domain::{AnalysisSubject, PortfolioSummary, Position, Workspace};
use crate::filter::any_contains_ci;
use crate::pages::{
    Page, format_money, format_number, format_pct, format_signed_money, format_source, header_cell,
    metric_cell, sortable_header_cell,
};
use crate::table_state::{
    ColumnDescriptor, EditableCell, SortSpec, TableId, TableLayoutRegistry, TableState,
};
use crate::timeseries::TimeSeriesKind;
use crate::ui::table_layout::{managed_column, managed_table_revision, table_layout_controls};
use crate::ui::{metrics, style};
use eframe::egui;
use egui_extras::TableBuilder;
use std::cmp::Ordering;

const COL_TICKER: &str = "ticker";
const COL_MARKET_VALUE: &str = "market_value";
const COL_WEIGHT: &str = "weight";
const COL_TRAILING_YIELD: &str = "trailing_yield";
const COL_PROJECTED_INCOME: &str = "projected_income";
const COL_FRESHNESS: &str = "freshness";
const COL_UNITS: &str = "units";
const COL_PRICE: &str = "price";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PortfolioColumn {
    Ticker,
    Name,
    Isin,
    Currency,
    Units,
    Price,
    DailyChange,
    MarketValue,
    Weight,
    TrailingYield,
    ProjectedIncome,
    Freshness,
    Source,
}

impl PortfolioColumn {
    const ALL: [Self; 13] = [
        Self::Ticker,
        Self::Name,
        Self::Isin,
        Self::Currency,
        Self::Units,
        Self::Price,
        Self::DailyChange,
        Self::MarketValue,
        Self::Weight,
        Self::TrailingYield,
        Self::ProjectedIncome,
        Self::Freshness,
        Self::Source,
    ];

    const DESCRIPTORS: [ColumnDescriptor; 13] = [
        ColumnDescriptor::new("ticker", "Ticker", 64.0, 54.0, 120.0).required(),
        ColumnDescriptor::new("name", "Name", 260.0, 180.0, 520.0).clipped(),
        ColumnDescriptor::new("isin", "ISIN", 118.0, 100.0, 190.0),
        ColumnDescriptor::new("currency", "Ccy", 72.0, 60.0, 110.0),
        ColumnDescriptor::new("units", "Units", 76.0, 64.0, 150.0).required(),
        ColumnDescriptor::new("price", "Price", 78.0, 64.0, 160.0).required(),
        ColumnDescriptor::new("daily_change", "Daily", 82.0, 68.0, 170.0),
        ColumnDescriptor::new("market_value", "Market value", 112.0, 92.0, 220.0).required(),
        ColumnDescriptor::new("weight", "Weight", 86.0, 72.0, 150.0).required(),
        ColumnDescriptor::new("trailing_yield", "TTM yield", 88.0, 74.0, 160.0),
        ColumnDescriptor::new("projected_income", "Proj. income", 112.0, 92.0, 220.0),
        ColumnDescriptor::new("freshness", "Freshness", 112.0, 92.0, 180.0).required(),
        ColumnDescriptor::new("source", "Source", 120.0, 96.0, 220.0).required(),
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("portfolio column is in ALL")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Ticker => COL_TICKER,
            Self::Name => "name",
            Self::Isin => "isin",
            Self::Currency => "currency",
            Self::Units => COL_UNITS,
            Self::Price => COL_PRICE,
            Self::DailyChange => "daily_change",
            Self::MarketValue => COL_MARKET_VALUE,
            Self::Weight => COL_WEIGHT,
            Self::TrailingYield => COL_TRAILING_YIELD,
            Self::ProjectedIncome => COL_PROJECTED_INCOME,
            Self::Freshness => COL_FRESHNESS,
            Self::Source => "source",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Ticker => "Ticker",
            Self::Name => "Name",
            Self::Isin => "ISIN",
            Self::Currency => "Currency",
            Self::Units => "Units",
            Self::Price => "Price",
            Self::DailyChange => "Daily change",
            Self::MarketValue => "Market value",
            Self::Weight => "Weight",
            Self::TrailingYield => "TTM yield",
            Self::ProjectedIncome => "Projected income",
            Self::Freshness => "Freshness",
            Self::Source => "Source",
        }
    }

    fn payload(self, position: &Position, base_currency: &str) -> (String, String) {
        match self {
            Self::Ticker => (position.ticker.clone(), position.ticker.clone()),
            Self::Name => (position.name.clone(), position.name.clone()),
            Self::Isin => (position.isin.clone(), position.isin.clone()),
            Self::Currency => (
                position.listing_currency.clone(),
                position.listing_currency.clone(),
            ),
            Self::Units => (format_number(position.units, 2), position.units.to_string()),
            Self::Price => (format_number(position.price, 2), position.price.to_string()),
            Self::DailyChange => (
                format_signed_money(&position.listing_currency, position.daily_change),
                position.daily_change.to_string(),
            ),
            Self::MarketValue => (
                format_money(base_currency, position.market_value),
                position.market_value.to_string(),
            ),
            Self::Weight => (
                format_pct(position.portfolio_weight_pct),
                position.portfolio_weight_pct.to_string(),
            ),
            Self::TrailingYield => (
                format_pct(position.trailing_yield_pct),
                position.trailing_yield_pct.to_string(),
            ),
            Self::ProjectedIncome => (
                format_money(base_currency, position.projected_income),
                position.projected_income.to_string(),
            ),
            Self::Freshness => (
                position.freshness.as_str().to_owned(),
                position.freshness.as_str().to_owned(),
            ),
            Self::Source => (format_source(&position.source), position.source.clone()),
        }
    }
}

#[derive(Clone, Debug)]
pub struct PortfolioState {
    pub table: TableState,
    pub overrides: Vec<PositionOverride>,
    pub edit_error: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PortfolioAction {
    OpenPosition {
        subject: AnalysisSubject,
        label: String,
        breadcrumbs: Vec<String>,
    },
    Plot(PlotRequest),
    ClearRowOverrides {
        listing_id: String,
    },
    Navigate(Page),
    Feedback(String),
}

impl Default for PortfolioState {
    fn default() -> Self {
        Self {
            table: TableState::new(TableId::PortfolioPositions),
            overrides: Vec::new(),
            edit_error: None,
        }
    }
}

pub fn render(
    ui: &mut egui::Ui,
    workspace: &Workspace,
    summary: &mut PortfolioSummary,
    positions: &mut [Position],
    selected: &mut SelectedInstrument,
    state: &mut PortfolioState,
    layouts: &mut TableLayoutRegistry,
) -> Option<PortfolioAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let context = format!("WS: {}", workspace.name);
            let subtitle = format!(
                "{} · {} positions · {} stale",
                summary.base_currency, summary.position_count, summary.stale_warning_count
            );
            style::page_header(ui, "Portfolio", Some(&context), Some(&subtitle), |ui| {
                style::mock_badge(ui);
                style::source_badge(ui, "mock");
            });
            summary_grid(ui, summary);
            ui.add_space(6.0);
            position_actions(ui, workspace, positions, selected, state, &mut action);
            position_filter(ui, state);
            ui.add_space(4.0);
            if action.is_none() {
                action =
                    positions_table(ui, workspace, summary, positions, selected, state, layouts);
            }
        });
    action
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    positions: &[Position],
    base_currency: &str,
    selected: &mut SelectedInstrument,
    state: &mut PortfolioState,
    layouts: &mut TableLayoutRegistry,
) -> bool {
    if ctx.text_edit_focused() || state.table.edit.is_editing() {
        return false;
    }

    let visible_indices = visible_position_indices(positions, state);
    let mut moved = false;

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        state.table.move_focus_row(&visible_indices, -1, Some(0));
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        state.table.move_focus_row(&visible_indices, 1, Some(0));
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        let columns =
            layouts.visible_indices(TableId::PortfolioPositions, &PortfolioColumn::DESCRIPTORS);
        state.table.move_focus_visible_column(&columns, -1);
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        let columns =
            layouts.visible_indices(TableId::PortfolioPositions, &PortfolioColumn::DESCRIPTORS);
        state.table.move_focus_visible_column(&columns, 1);
        moved = true;
    }

    if moved {
        sync_position_focus(positions, base_currency, selected, state);
    }

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        if state.table.selected_index().is_none() {
            state.table.move_focus_row(&visible_indices, 1, Some(0));
            sync_position_focus(positions, base_currency, selected, state);
        }
        if let Some(index) = state.table.selected_index()
            && let Some(position) = positions.get(index)
        {
            selected.select_listing(position.fund_id.clone(), position.listing_id.clone());
            return true;
        }
    }

    false
}

fn summary_grid(ui: &mut egui::Ui, summary: &PortfolioSummary) {
    egui::Grid::new("portfolio_summary_grid")
        .num_columns(4)
        .min_col_width((ui.available_width() / 4.4).clamp(150.0, 240.0))
        .striped(true)
        .show(ui, |ui| {
            metric_cell(
                ui,
                "Total value",
                format_money(&summary.base_currency, summary.total_value),
            );
            metric_cell(
                ui,
                "Daily change",
                format_signed_money(&summary.base_currency, summary.daily_change),
            );
            metric_cell(
                ui,
                "Unrealised gain/loss",
                format_signed_money(&summary.base_currency, summary.unrealised_gain_loss),
            );
            metric_cell(
                ui,
                "TTM income",
                format_money(&summary.base_currency, summary.trailing_12m_income),
            );
            ui.end_row();

            metric_cell(
                ui,
                "Projected annual income",
                format_money(&summary.base_currency, summary.projected_annual_income),
            );
            metric_cell(ui, "Base currency", summary.base_currency.clone());
            metric_cell(ui, "Positions", summary.position_count.to_string());
            metric_cell(
                ui,
                "Stale data warnings",
                summary.stale_warning_count.to_string(),
            );
            ui.end_row();
        });
}

fn position_filter(ui: &mut egui::Ui, state: &mut PortfolioState) {
    ui.horizontal(|ui| {
        ui.label("Filter");
        ui.add_sized(
            [(ui.available_width() * 0.28).clamp(220.0, 360.0), 20.0],
            egui::TextEdit::singleline(&mut state.table.filter)
                .hint_text("ticker / name / isin / ccy"),
        )
        .on_hover_text(
            "Filter the local portfolio table by ticker, name, ISIN, currency, status, or source.",
        );
        if ui.button("Clear").clicked() {
            state.table.filter.clear();
            state.table.clear_selection();
            state.table.edit.cancel();
            state.edit_error = None;
        }
        if !state.overrides.is_empty() {
            style::manual_badge(ui);
            ui.monospace(state.overrides.len().to_string());
        }
        if let Some(error) = state.edit_error.as_deref() {
            style::error_label(ui, error);
        }
    });
}

fn position_actions(
    ui: &mut egui::Ui,
    workspace: &Workspace,
    positions: &mut [Position],
    selected: &SelectedInstrument,
    state: &mut PortfolioState,
    action: &mut Option<PortfolioAction>,
) {
    ui.horizontal_wrapped(|ui| {
        if ui.button("Plot Portfolio Value").clicked() {
            *action = Some(PortfolioAction::Plot(PlotRequest::new(
                AnalysisSubject::WorkspacePortfolio(workspace.id.clone()),
                TimeSeriesKind::PortfolioValue,
                format!("{} value", workspace.name),
            )));
        }

        let selected_position = state
            .table
            .selected_index()
            .and_then(|index| positions.get(index));
        let has_selected_position = selected_position.is_some();

        if ui
            .add_enabled(has_selected_position, egui::Button::new("Open"))
            .clicked()
            && let Some(position) = selected_position
        {
            *action = Some(open_position_action(workspace, position));
        }
        if ui
            .add_enabled(has_selected_position, egui::Button::new("Plot Price"))
            .clicked()
            && let Some(position) = selected_position
        {
            *action = Some(PortfolioAction::Plot(plot_position(
                position,
                TimeSeriesKind::Price,
            )));
        }
        if ui
            .add_enabled(has_selected_position, egui::Button::new("Plot Value"))
            .clicked()
            && let Some(position) = selected_position
        {
            *action = Some(PortfolioAction::Plot(plot_position(
                position,
                TimeSeriesKind::MarketValue,
            )));
        }
        if ui
            .add_enabled(has_selected_position, egui::Button::new("Explain Value"))
            .clicked()
            && let Some(position) = selected_position
        {
            *action = Some(PortfolioAction::Feedback(format!(
                "CMD: explain {} market value in inspector/Analytics",
                position.ticker
            )));
        }
        if ui
            .add_enabled(
                selected.listing_id.as_ref().is_some_and(|listing_id| {
                    state
                        .overrides
                        .iter()
                        .any(|position_override| position_override.listing_id == *listing_id)
                }),
                egui::Button::new("Clear Row Overrides"),
            )
            .clicked()
            && let Some(listing_id) = selected.listing_id.as_deref()
        {
            *action = Some(PortfolioAction::ClearRowOverrides {
                listing_id: listing_id.to_owned(),
            });
        }
    });
}

fn positions_table(
    ui: &mut egui::Ui,
    workspace: &Workspace,
    summary: &mut PortfolioSummary,
    positions: &mut [Position],
    selected: &mut SelectedInstrument,
    state: &mut PortfolioState,
    layouts: &mut TableLayoutRegistry,
) -> Option<PortfolioAction> {
    let mut action = None;
    let base_currency = summary.base_currency.clone();
    let visible_indices = visible_position_indices(positions, state);
    state.table.retain_visible(&visible_indices);
    let table_height = (ui.available_height() - 8.0).max(260.0);

    ui.label(
        egui::RichText::new(format!(
            "ETF positions ({}/{})",
            visible_indices.len(),
            positions.len()
        ))
        .strong(),
    );
    if visible_indices.is_empty() {
        style::state_message(
            ui,
            "EMPTY",
            "No portfolio positions match the current filter.",
        );
        return None;
    }

    let visible_row = state
        .table
        .focused_row_index
        .or(state.table.selected_index())
        .and_then(|index| positions.get(index))
        .map(|position| {
            (
                position.ticker.as_str(),
                layouts.visible_row_text(
                    TableId::PortfolioPositions,
                    &PortfolioColumn::DESCRIPTORS,
                    &PortfolioColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(position, &base_currency).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::PortfolioPositions,
        &PortfolioColumn::DESCRIPTORS,
        state
            .table
            .focused_column_index
            .and_then(|index| PortfolioColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.table.clear_focus();
    }
    let revision = managed_table_revision(
        layouts,
        TableId::PortfolioPositions,
        &PortfolioColumn::DESCRIPTORS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("portfolio_positions_table", revision))
        .striped(true)
        .resizable(false)
        .auto_shrink(false)
        .max_scroll_height(table_height);
    for descriptor in PortfolioColumn::DESCRIPTORS {
        table = table.column(managed_column(
            layouts,
            TableId::PortfolioPositions,
            &PortfolioColumn::DESCRIPTORS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.table,
                    COL_TICKER,
                    PortfolioColumn::Ticker.label(),
                )
            });
            header.col(|ui| header_cell(ui, PortfolioColumn::Name.label()));
            header.col(|ui| header_cell(ui, PortfolioColumn::Isin.label()));
            header.col(|ui| header_cell(ui, "Ccy"));
            header.col(|ui| header_cell(ui, PortfolioColumn::Units.label()));
            header.col(|ui| header_cell(ui, PortfolioColumn::Price.label()));
            header.col(|ui| header_cell(ui, "Daily"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.table, COL_MARKET_VALUE, "Market value");
            });
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_WEIGHT, "Weight"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.table, COL_TRAILING_YIELD, "TTM yield");
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.table, COL_PROJECTED_INCOME, "Proj. income");
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.table, COL_FRESHNESS, "Freshness");
            });
            header.col(|ui| header_cell(ui, "Source"));
        })
        .body(|mut body| {
            for index in visible_indices {
                let position = positions[index].clone();
                let row_has_override = state
                    .overrides
                    .iter()
                    .any(|position_override| position_override.listing_id == position.listing_id);
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.table.is_focused_row(index));
                    row.set_selected(
                        state.table.selection.is_selected(index)
                            || selected.fund_id.as_deref() == Some(&position.fund_id),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::Ticker.index()),
                        );
                        let is_selected = state.table.selection.is_selected(index)
                            || selected.fund_id.as_deref() == Some(&position.fund_id);
                        let response = ui
                            .selectable_label(is_selected, &position.ticker)
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            select_position_cell(
                                &position,
                                selected,
                                state,
                                index,
                                PortfolioColumn::Ticker,
                                &base_currency,
                            );
                        }
                        if response.double_clicked() {
                            select_position_cell(
                                &position,
                                selected,
                                state,
                                index,
                                PortfolioColumn::Ticker,
                                &base_currency,
                            );
                            action = Some(open_position_action(workspace, &position));
                        }
                        response.context_menu(|ui| {
                            if ui.button("Open").clicked() {
                                action = Some(open_position_action(workspace, &position));
                                ui.close();
                            }
                            if ui.button("Make Active").clicked() {
                                action = Some(open_position_action(workspace, &position));
                                ui.close();
                            }
                            if ui.button("Plot Price").clicked() {
                                action = Some(PortfolioAction::Plot(plot_position(
                                    &position,
                                    TimeSeriesKind::Price,
                                )));
                                ui.close();
                            }
                            if ui.button("Plot Value").clicked() {
                                action = Some(PortfolioAction::Plot(plot_position(
                                    &position,
                                    TimeSeriesKind::MarketValue,
                                )));
                                ui.close();
                            }
                            if ui.button("Explain Value").clicked() {
                                action = Some(PortfolioAction::Feedback(format!(
                                    "CMD: explain {} market value in inspector/Analytics",
                                    position.ticker
                                )));
                                ui.close();
                            }
                            if ui.button("Edit Override").clicked() {
                                state.table.edit.begin(
                                    EditableCell::new(state.table.id, index, COL_PRICE),
                                    format!("{:.2}", position.price),
                                );
                                state.edit_error = None;
                                ui.close();
                            }
                            if ui
                                .add_enabled(
                                    row_has_override,
                                    egui::Button::new("Clear Row Overrides"),
                                )
                                .clicked()
                            {
                                action = Some(PortfolioAction::ClearRowOverrides {
                                    listing_id: position.listing_id.clone(),
                                });
                                ui.close();
                            }
                            if ui.button("Compare").clicked() {
                                action = Some(PortfolioAction::Navigate(Page::Compare));
                                ui.close();
                            }
                            if ui.button("Source").clicked() {
                                ui.copy_text(format_source(&position.source));
                                action = Some(PortfolioAction::Feedback(format!(
                                    "CMD: source {} {}",
                                    position.ticker,
                                    format_source(&position.source)
                                )));
                                ui.close();
                            }
                            if ui.button("Copy Ticker").clicked() {
                                ui.copy_text(position.ticker.clone());
                                action = Some(PortfolioAction::Feedback(format!(
                                    "COPIED: {}",
                                    position.ticker
                                )));
                                ui.close();
                            }
                            if ui.button("Copy ISIN").clicked() {
                                ui.copy_text(position.isin.clone());
                                action = Some(PortfolioAction::Feedback(format!(
                                    "COPIED: {}",
                                    position.isin
                                )));
                                ui.close();
                            }
                            if ui.button("Copy Value").clicked() {
                                let value = format_money(&base_currency, position.market_value);
                                ui.copy_text(value.clone());
                                action =
                                    Some(PortfolioAction::Feedback(format!("COPIED: {value}")));
                                ui.close();
                            }
                            if ui.button("Copy Row").clicked() {
                                let row_text = position_copy_text(&position, &base_currency);
                                ui.copy_text(row_text);
                                action = Some(PortfolioAction::Feedback(
                                    "COPIED: row summary".to_owned(),
                                ));
                                ui.close();
                            }
                        });
                        response.on_hover_text(format!(
                            "fund_id: {}\nlisting_id: {}",
                            position.fund_id, position.listing_id
                        ));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::Name.index()),
                        );
                        ui.label(&position.name);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::Isin.index()),
                        );
                        ui.monospace(&position.isin);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::Currency.index()),
                        );
                        ui.label(&position.listing_currency);
                    });
                    row.col(|ui| {
                        let focused = state
                            .table
                            .is_focused_cell(index, PortfolioColumn::Units.index());
                        editable_number_cell(
                            ui,
                            summary,
                            positions,
                            selected,
                            state,
                            &position,
                            index,
                            COL_UNITS,
                            PositionOverrideField::Units,
                            position.units,
                            2,
                            focused,
                        );
                    });
                    row.col(|ui| {
                        let focused = state
                            .table
                            .is_focused_cell(index, PortfolioColumn::Price.index());
                        editable_number_cell(
                            ui,
                            summary,
                            positions,
                            selected,
                            state,
                            &position,
                            index,
                            COL_PRICE,
                            PositionOverrideField::Price,
                            position.price,
                            2,
                            focused,
                        );
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::DailyChange.index()),
                        );
                        style::numeric_label(
                            ui,
                            format_signed_money(&position.listing_currency, position.daily_change),
                        );
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::MarketValue.index()),
                        );
                        let value = format_money(&base_currency, position.market_value);
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if row_has_override {
                                style::derived_badge(ui);
                            }
                            ui.monospace(value);
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::Weight.index()),
                        );
                        style::numeric_label(ui, format_pct(position.portfolio_weight_pct));
                    });
                    row.col(|ui| {
                        let focused = state
                            .table
                            .is_focused_cell(index, PortfolioColumn::TrailingYield.index());
                        editable_number_cell(
                            ui,
                            summary,
                            positions,
                            selected,
                            state,
                            &position,
                            index,
                            COL_TRAILING_YIELD,
                            PositionOverrideField::TrailingYieldPct,
                            position.trailing_yield_pct,
                            2,
                            focused,
                        );
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::ProjectedIncome.index()),
                        );
                        let value = format_money(&base_currency, position.projected_income);
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if row_has_override {
                                style::estimated_badge(ui);
                            }
                            ui.monospace(value);
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::Freshness.index()),
                        );
                        style::freshness_badge(ui, position.freshness);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, PortfolioColumn::Source.index()),
                        );
                        style::source_badge(ui, &position.source);
                    });
                });
            }
        });
    action
}

#[allow(clippy::too_many_arguments)]
fn editable_number_cell(
    ui: &mut egui::Ui,
    summary: &mut PortfolioSummary,
    positions: &mut [Position],
    selected: &mut SelectedInstrument,
    state: &mut PortfolioState,
    position: &Position,
    row_index: usize,
    column: &str,
    field: PositionOverrideField,
    value: f64,
    decimals: usize,
    focused: bool,
) {
    style::focused_table_cell(ui, focused);
    if state
        .table
        .edit
        .is_editing_cell(state.table.id, row_index, column)
    {
        let response = ui
            .add_sized(
                [72.0, 18.0],
                egui::TextEdit::singleline(&mut state.table.edit.draft),
            )
            .on_hover_cursor(egui::CursorIcon::Text);
        response.request_focus();

        if response.lost_focus() && ui.input(|input| input.key_pressed(egui::Key::Enter)) {
            commit_number_edit(summary, positions, state, &position.listing_id, field);
        }
        return;
    }

    let is_manual = state.overrides.iter().any(|position_override| {
        position_override.listing_id == position.listing_id && position_override.field == field
    });
    let display_value = format_number(value, decimals);
    let display_value = if is_manual {
        format!("{display_value} MANUAL")
    } else {
        display_value
    };
    let response = ui
        .add(egui::Label::new(display_value).sense(egui::Sense::click()))
        .on_hover_text("Editable cell. Click selects/copies this cell; double-click edits. Enter commits; Esc cancels.")
        .on_hover_cursor(egui::CursorIcon::Cell);
    if response.clicked() {
        let portfolio_column = match column {
            COL_UNITS => PortfolioColumn::Units,
            COL_PRICE => PortfolioColumn::Price,
            COL_TRAILING_YIELD => PortfolioColumn::TrailingYield,
            _ => PortfolioColumn::Ticker,
        };
        select_position_cell(
            position,
            selected,
            state,
            row_index,
            portfolio_column,
            &summary.base_currency,
        );
    }
    if response.double_clicked() {
        select_position(position, selected, state, row_index);
        state.table.edit.begin(
            EditableCell::new(state.table.id, row_index, column),
            format!("{:.*}", decimals, value),
        );
        state.edit_error = None;
    }
}

fn commit_number_edit(
    summary: &mut PortfolioSummary,
    positions: &mut [Position],
    state: &mut PortfolioState,
    listing_id: &str,
    field: PositionOverrideField,
) {
    let value = match parse_edit_value(&state.table.edit.draft) {
        Ok(value) => value,
        Err(message) => {
            state.edit_error = Some(message);
            return;
        }
    };

    match apply_position_override(positions, &summary.base_currency, listing_id, field, value) {
        Ok(new_summary) => {
            *summary = new_summary;
            upsert_override(&mut state.overrides, listing_id, field, value);
            state.table.edit.cancel();
            state.edit_error = None;
        }
        Err(err) => {
            state.edit_error = Some(format!("EDIT: {err}"));
        }
    }
}

fn parse_edit_value(raw: &str) -> Result<f64, String> {
    raw.trim()
        .replace(',', "")
        .parse::<f64>()
        .map_err(|_| format!("EDIT: invalid number '{}'", raw.trim()))
}

fn upsert_override(
    overrides: &mut Vec<PositionOverride>,
    listing_id: &str,
    field: PositionOverrideField,
    value: f64,
) {
    if let Some(position_override) = overrides.iter_mut().find(|position_override| {
        position_override.listing_id == listing_id && position_override.field == field
    }) {
        position_override.value = value;
    } else {
        overrides.push(PositionOverride::new(listing_id, field, value));
    }
}

fn open_position_action(workspace: &Workspace, position: &Position) -> PortfolioAction {
    PortfolioAction::OpenPosition {
        subject: AnalysisSubject::FundListing {
            fund_id: position.fund_id.clone(),
            listing_id: position.listing_id.clone(),
        },
        label: position.ticker.clone(),
        breadcrumbs: vec![workspace.name.clone(), position.ticker.clone()],
    }
}

fn plot_position(position: &Position, kind: TimeSeriesKind) -> PlotRequest {
    PlotRequest::new(
        AnalysisSubject::FundListing {
            fund_id: position.fund_id.clone(),
            listing_id: position.listing_id.clone(),
        },
        kind,
        format!("{} {}", position.ticker, kind.as_str()),
    )
}

fn position_copy_text(position: &Position, base_currency: &str) -> String {
    [
        position.ticker.clone(),
        position.isin.clone(),
        position.name.clone(),
        format_money(base_currency, position.market_value),
        format_pct(position.portfolio_weight_pct),
        format_source(&position.source),
    ]
    .join("\t")
}

fn select_position(
    position: &Position,
    selected: &mut SelectedInstrument,
    state: &mut PortfolioState,
    index: usize,
) {
    state.table.select(index);
    selected.select_listing(position.fund_id.clone(), position.listing_id.clone());
}

fn select_position_cell(
    position: &Position,
    selected: &mut SelectedInstrument,
    state: &mut PortfolioState,
    index: usize,
    column: PortfolioColumn,
    base_currency: &str,
) {
    selected.select_listing(position.fund_id.clone(), position.listing_id.clone());
    let (display, raw) = column.payload(position, base_currency);
    state
        .table
        .select_cell(index, column.index(), column.key(), display, raw);
}

fn sync_position_focus(
    positions: &[Position],
    base_currency: &str,
    selected: &mut SelectedInstrument,
    state: &mut PortfolioState,
) {
    let (Some(row_index), Some(column_index)) = (
        state.table.focused_row_index,
        state.table.focused_column_index,
    ) else {
        return;
    };
    let Some(position) = positions.get(row_index) else {
        return;
    };
    let Some(column) = PortfolioColumn::ALL.get(column_index).copied() else {
        return;
    };
    selected.select_listing(position.fund_id.clone(), position.listing_id.clone());
    let (display, raw) = column.payload(position, base_currency);
    state
        .table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn visible_position_indices(positions: &[Position], state: &PortfolioState) -> Vec<usize> {
    let mut indices = positions
        .iter()
        .enumerate()
        .filter(|(_, position)| position_matches(position, &state.table.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();

    sort_position_indices(positions, &mut indices, state.table.sort.as_ref());
    indices
}

fn sort_position_indices(positions: &[Position], indices: &mut [usize], sort: Option<&SortSpec>) {
    let Some(sort) = sort else {
        return;
    };

    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_positions(
                &positions[*left],
                &positions[*right],
                &sort.column,
            ))
            .then_with(|| positions[*left].ticker.cmp(&positions[*right].ticker))
    });
}

fn compare_positions(left: &Position, right: &Position, column: &str) -> Ordering {
    match column {
        COL_TICKER => left.ticker.cmp(&right.ticker),
        COL_MARKET_VALUE => left.market_value.total_cmp(&right.market_value),
        COL_WEIGHT => left
            .portfolio_weight_pct
            .total_cmp(&right.portfolio_weight_pct),
        COL_TRAILING_YIELD => left.trailing_yield_pct.total_cmp(&right.trailing_yield_pct),
        COL_PROJECTED_INCOME => left.projected_income.total_cmp(&right.projected_income),
        COL_FRESHNESS => left.freshness.as_str().cmp(right.freshness.as_str()),
        _ => Ordering::Equal,
    }
}

fn position_matches(position: &Position, filter: &str) -> bool {
    any_contains_ci(
        [
            position.ticker.as_str(),
            position.name.as_str(),
            position.isin.as_str(),
            position.listing_currency.as_str(),
            position.freshness.as_str(),
            position.source.as_str(),
        ],
        filter,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn portfolio_columns_have_stable_labels_and_keys() {
        assert_eq!(PortfolioColumn::ALL.len(), 13);
        assert_eq!(PortfolioColumn::Ticker.label(), "Ticker");
        assert_eq!(PortfolioColumn::MarketValue.key(), COL_MARKET_VALUE);
        assert_eq!(PortfolioColumn::Source.label(), "Source");
        assert_eq!(PortfolioColumn::Source.index(), 12);
    }
}
