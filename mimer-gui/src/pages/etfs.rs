use crate::app_model::SelectedInstrument;
use crate::charts::PlotRequest;
use crate::domain::{AnalysisSubject, Fund};
use crate::filter::any_contains_ci;
use crate::pages::{
    Page, format_pct, format_source, header_cell, page_heading, sortable_header_cell,
};
use crate::table_state::{ColumnDescriptor, SortSpec, TableId, TableLayoutRegistry, TableState};
use crate::timeseries::TimeSeriesKind;
use crate::ui::grid_helpers::{KvRow, kv_grid};
use crate::ui::metrics;
use crate::ui::style;
use crate::ui::table_layout::{managed_column, managed_table_revision, table_layout_controls};
use eframe::egui;
use egui_extras::TableBuilder;
use std::cmp::Ordering;

const COL_TICKER: &str = "ticker";
const COL_PROVIDER: &str = "provider";
const COL_OCF_TER: &str = "ocf_ter";
const COL_STATUS: &str = "status";
const COL_LAST_REFRESHED: &str = "last_refreshed";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EtfColumn {
    Ticker,
    Name,
    Isin,
    Status,
    Source,
    Provider,
    Strategy,
    OcfTer,
    Distribution,
    Replication,
    LastRefreshed,
}

impl EtfColumn {
    const ALL: [Self; 11] = [
        Self::Ticker,
        Self::Name,
        Self::Isin,
        Self::Status,
        Self::Source,
        Self::Provider,
        Self::Strategy,
        Self::OcfTer,
        Self::Distribution,
        Self::Replication,
        Self::LastRefreshed,
    ];

    const DESCRIPTORS: [ColumnDescriptor; 11] = [
        ColumnDescriptor::new("ticker", "Ticker", 88.0, 76.0, 150.0).required(),
        ColumnDescriptor::new("name", "Fund name", 280.0, 180.0, 520.0).clipped(),
        ColumnDescriptor::new("isin", "ISIN", 116.0, 100.0, 180.0).required(),
        ColumnDescriptor::new("status", "Status", 112.0, 92.0, 170.0).required(),
        ColumnDescriptor::new("source", "Source", 130.0, 108.0, 220.0).required(),
        ColumnDescriptor::new("provider", "Provider", 110.0, 88.0, 220.0),
        ColumnDescriptor::new("strategy", "Strategy", 190.0, 140.0, 360.0)
            .hidden_by_default()
            .clipped(),
        ColumnDescriptor::new("ocf_ter", "OCF/TER", 78.0, 64.0, 130.0),
        ColumnDescriptor::new("distribution", "Dist. freq", 110.0, 88.0, 180.0).hidden_by_default(),
        ColumnDescriptor::new("replication", "Replication/type", 148.0, 110.0, 280.0)
            .hidden_by_default()
            .clipped(),
        ColumnDescriptor::new("last_refreshed", "Last refreshed", 150.0, 110.0, 260.0)
            .hidden_by_default(),
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("ETF column is in ALL")
    }

    fn key(self) -> &'static str {
        Self::DESCRIPTORS[self.index()].key
    }

    fn payload(self, fund: &Fund) -> (String, String) {
        let value = match self {
            Self::Ticker => listing_tickers(fund),
            Self::Name => fund.name.clone(),
            Self::Isin => fund.isin.clone(),
            Self::Status => fund.status.clone(),
            Self::Source => fund.source.clone(),
            Self::Provider => fund.provider.clone(),
            Self::Strategy => fund.strategy.clone(),
            Self::OcfTer => format_pct(f64::from(fund.ocf_ter_pct)),
            Self::Distribution => fund.distribution_frequency.clone(),
            Self::Replication => fund.replication.clone(),
            Self::LastRefreshed => fund.last_refreshed.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Debug)]
pub struct EtfsState {
    pub search: String,
    pub provider: String,
    pub status: String,
    pub table: TableState,
}

impl Default for EtfsState {
    fn default() -> Self {
        Self {
            search: String::new(),
            provider: String::new(),
            status: String::new(),
            table: TableState::new(TableId::EtfsFunds),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EtfsAction {
    OpenDetail,
    Plot(PlotRequest),
    Navigate(Page),
    Feedback(String),
}

pub fn render(
    ui: &mut egui::Ui,
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut EtfsState,
    layouts: &mut TableLayoutRegistry,
) -> Option<EtfsAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "ETFs");
            ui.label("Watched/shared fund reference data. JEPG/JEGP is modeled as one fund with multiple listings.");
            ui.add_space(6.0);
            filters(ui, funds, state);
            ui.add_space(6.0);

            let visible_indices = visible_fund_indices(funds, state);
            state.table.selection.retain_visible(&visible_indices);

            action = funds_table(ui, funds, &visible_indices, selected, state, layouts);
            ui.add_space(metrics::SPACE_2);
            selected_fund_panel(ui, funds, selected);
        });
    action
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut EtfsState,
    layouts: &mut TableLayoutRegistry,
) -> bool {
    if ctx.text_edit_focused() {
        return false;
    }

    let visible_indices = visible_fund_indices(funds, state);
    let mut moved = None;

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        moved = state.table.move_focus_row(&visible_indices, -1, Some(0));
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        moved = state.table.move_focus_row(&visible_indices, 1, Some(0));
    }
    let visible_columns = layouts.visible_indices(TableId::EtfsFunds, &EtfColumn::DESCRIPTORS);
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        state.table.move_focus_visible_column(&visible_columns, -1);
        moved = state.table.focused_row_index;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        state.table.move_focus_visible_column(&visible_columns, 1);
        moved = state.table.focused_row_index;
    }

    if let Some(index) = moved
        && let Some(fund) = funds.get(index)
    {
        select_fund(fund, selected, state, index);
        sync_etf_focus(funds, state);
    }

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        if state.table.selected_index().is_none() {
            state.table.selection.move_by(&visible_indices, 1);
        }
        if let Some(index) = state.table.selected_index()
            && let Some(fund) = funds.get(index)
        {
            select_fund(fund, selected, state, index);
            sync_etf_focus(funds, state);
            return true;
        }
    }

    false
}

fn filters(ui: &mut egui::Ui, funds: &[Fund], state: &mut EtfsState) {
    ui.horizontal_wrapped(|ui| {
        ui.label("Filter");
        let search_width = metrics::fit_width(ui.available_width(), 320.0, 180.0, 380.0);
        ui.add_sized(
            [search_width, 20.0],
            egui::TextEdit::singleline(&mut state.search).hint_text("ticker / name / isin"),
        )
        .on_hover_text("Filters visible funds by ticker, name, ISIN, provider, status, or source.");
        ui.label("Provider");
        egui::ComboBox::from_id_salt("etfs_provider_filter")
            .selected_text(filter_label(&state.provider))
            .show_ui(ui, |ui| {
                ui.selectable_value(&mut state.provider, String::new(), "all");
                for provider in unique_values(funds, |fund| fund.provider.as_str()) {
                    ui.selectable_value(&mut state.provider, provider.clone(), provider);
                }
            });
        ui.label("Status");
        egui::ComboBox::from_id_salt("etfs_status_filter")
            .selected_text(filter_label(&state.status))
            .show_ui(ui, |ui| {
                ui.selectable_value(&mut state.status, String::new(), "all");
                for status in unique_values(funds, |fund| fund.status.as_str()) {
                    ui.selectable_value(&mut state.status, status.clone(), status);
                }
            });
        if ui.button("Clear").clicked() {
            state.search.clear();
            state.provider.clear();
            state.status.clear();
            state.table.clear_selection();
        }
    });
}

fn funds_table(
    ui: &mut egui::Ui,
    funds: &[Fund],
    visible_indices: &[usize],
    selected: &mut SelectedInstrument,
    state: &mut EtfsState,
    layouts: &mut TableLayoutRegistry,
) -> Option<EtfsAction> {
    let mut action = None;
    let table_height = (ui.available_height() - 24.0).clamp(280.0, 620.0);
    let visible_row = state
        .table
        .focused_row_index
        .or(state.table.selected_index())
        .and_then(|index| funds.get(index))
        .map(|fund| {
            (
                listing_tickers(fund),
                layouts.visible_row_text(
                    TableId::EtfsFunds,
                    &EtfColumn::DESCRIPTORS,
                    &EtfColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(fund).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::EtfsFunds,
        &EtfColumn::DESCRIPTORS,
        state
            .table
            .focused_column_index
            .and_then(|index| EtfColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row
            .as_ref()
            .map(|(label, text)| (label.as_str(), text.clone())),
    ) {
        state.table.clear_focus();
    }
    let revision = managed_table_revision(layouts, TableId::EtfsFunds, &EtfColumn::DESCRIPTORS);
    let mut table = TableBuilder::new(ui)
        .id_salt(("etfs_fund_table", revision))
        .striped(true)
        .resizable(false)
        .auto_shrink(false)
        .max_scroll_height(table_height);
    for descriptor in EtfColumn::DESCRIPTORS {
        table = table.column(managed_column(
            layouts,
            TableId::EtfsFunds,
            &EtfColumn::DESCRIPTORS,
            descriptor,
        ));
    }

    table
        .header(18.0, |mut header| {
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_TICKER, "Ticker"));
            header.col(|ui| header_cell(ui, "Fund name"));
            header.col(|ui| header_cell(ui, "ISIN"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_STATUS, "Status"));
            header.col(|ui| header_cell(ui, "Source"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.table, COL_PROVIDER, "Provider");
            });
            header.col(|ui| header_cell(ui, "Strategy"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_OCF_TER, "OCF/TER"));
            header.col(|ui| header_cell(ui, "Dist. freq"));
            header.col(|ui| header_cell(ui, "Replication/type"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.table, COL_LAST_REFRESHED, "Last refreshed");
            });
        })
        .body(|mut body| {
            for index in visible_indices {
                let fund = &funds[*index];
                body.row(20.0, |mut row| {
                    row.set_overline(state.table.is_focused_row(*index));
                    row.set_selected(state.table.selection.is_selected(*index));
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::Ticker.index()),
                        );
                        let is_selected = state.table.selection.is_selected(*index)
                            || selected.fund_id.as_deref() == Some(&fund.id);
                        let response = ui
                            .selectable_label(is_selected, listing_tickers(fund))
                            .on_hover_text(format!(
                                "Double-click to open Fund Detail\n{}\nISIN: {}\nSource: {}",
                                fund.name,
                                fund.isin,
                                format_source(&fund.source)
                            ))
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            select_fund(fund, selected, state, *index);
                            focus_etf_cell(state, *index, fund, EtfColumn::Ticker);
                        }
                        if response.double_clicked() {
                            select_fund(fund, selected, state, *index);
                            action = Some(EtfsAction::OpenDetail);
                        }
                        response.context_menu(|ui| {
                            if ui.button("Open").clicked() {
                                select_fund(fund, selected, state, *index);
                                action = Some(EtfsAction::OpenDetail);
                                ui.close();
                            }
                            if ui.button("Make Active").clicked() {
                                select_fund(fund, selected, state, *index);
                                action = Some(EtfsAction::Feedback(format!(
                                    "ACTIVE: {}",
                                    listing_tickers(fund)
                                )));
                                ui.close();
                            }
                            if ui.button("Plot Price").clicked() {
                                select_fund(fund, selected, state, *index);
                                if let Some(request) = plot_fund(fund, TimeSeriesKind::Price) {
                                    action = Some(EtfsAction::Plot(request));
                                }
                                ui.close();
                            }
                            if ui.button("Plot NAV vs Price").clicked() {
                                select_fund(fund, selected, state, *index);
                                if let Some(request) = plot_fund(fund, TimeSeriesKind::Nav) {
                                    action = Some(EtfsAction::Plot(
                                        request.with_overlay(TimeSeriesKind::Price),
                                    ));
                                }
                                ui.close();
                            }
                            if ui.button("View Holdings").clicked() {
                                select_fund(fund, selected, state, *index);
                                action = Some(EtfsAction::Navigate(Page::Holdings));
                                ui.close();
                            }
                            if ui.button("View Documents").clicked() {
                                select_fund(fund, selected, state, *index);
                                action = Some(EtfsAction::Navigate(Page::Documents));
                                ui.close();
                            }
                            if ui.button("Compare").clicked() {
                                select_fund(fund, selected, state, *index);
                                action = Some(EtfsAction::Navigate(Page::Compare));
                                ui.close();
                            }
                            if ui.button("Source").clicked() {
                                ui.copy_text(format_source(&fund.source));
                                action = Some(EtfsAction::Feedback(format!(
                                    "CMD: source {} {}",
                                    listing_tickers(fund),
                                    format_source(&fund.source)
                                )));
                                ui.close();
                            }
                            if ui.button("Copy Ticker").clicked() {
                                let tickers = listing_tickers(fund);
                                ui.copy_text(tickers.clone());
                                action = Some(EtfsAction::Feedback(format!("COPIED: {tickers}")));
                                ui.close();
                            }
                            if ui.button("Copy ISIN").clicked() {
                                ui.copy_text(fund.isin.clone());
                                action =
                                    Some(EtfsAction::Feedback(format!("COPIED: {}", fund.isin)));
                                ui.close();
                            }
                            if ui.button("Copy Fund Name").clicked() {
                                ui.copy_text(fund.name.clone());
                                action =
                                    Some(EtfsAction::Feedback(format!("COPIED: {}", fund.name)));
                                ui.close();
                            }
                            if ui.button("Copy Row").clicked() {
                                ui.copy_text(fund_copy_text(fund));
                                action = Some(EtfsAction::Feedback("COPIED: fund row".to_owned()));
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.table.is_focused_cell(*index, EtfColumn::Name.index()),
                        );
                        ui.label(&fund.name);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.table.is_focused_cell(*index, EtfColumn::Isin.index()),
                        );
                        ui.monospace(&fund.isin);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::Status.index()),
                        );
                        style::status_badge(ui, &fund.status);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::Source.index()),
                        );
                        style::source_badge(ui, &fund.source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::Provider.index()),
                        );
                        ui.label(&fund.provider);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::Strategy.index()),
                        );
                        ui.label(&fund.strategy);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::OcfTer.index()),
                        );
                        ui.label(format_pct(f64::from(fund.ocf_ter_pct)));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::Distribution.index()),
                        );
                        ui.label(&fund.distribution_frequency);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::Replication.index()),
                        );
                        ui.label(&fund.replication);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(*index, EtfColumn::LastRefreshed.index()),
                        );
                        ui.label(&fund.last_refreshed);
                    });
                });
            }
        });
    action
}

fn visible_fund_indices(funds: &[Fund], state: &EtfsState) -> Vec<usize> {
    let mut indices = funds
        .iter()
        .enumerate()
        .filter(|(_, fund)| fund_matches(fund, state))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();

    sort_fund_indices(funds, &mut indices, state.table.sort.as_ref());
    indices
}

fn sort_fund_indices(funds: &[Fund], indices: &mut [usize], sort: Option<&SortSpec>) {
    let Some(sort) = sort else {
        return;
    };

    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_funds(&funds[*left], &funds[*right], &sort.column))
            .then_with(|| {
                first_listing_ticker(&funds[*left]).cmp(first_listing_ticker(&funds[*right]))
            })
    });
}

fn compare_funds(left: &Fund, right: &Fund, column: &str) -> Ordering {
    match column {
        COL_TICKER => first_listing_ticker(left).cmp(first_listing_ticker(right)),
        COL_PROVIDER => left.provider.cmp(&right.provider),
        COL_OCF_TER => left.ocf_ter_pct.total_cmp(&right.ocf_ter_pct),
        COL_STATUS => left.status.cmp(&right.status),
        COL_LAST_REFRESHED => left.last_refreshed.cmp(&right.last_refreshed),
        _ => Ordering::Equal,
    }
}

fn fund_matches(fund: &Fund, state: &EtfsState) -> bool {
    let search_matches = any_contains_ci(
        fund.listings
            .iter()
            .map(|listing| listing.ticker.as_str())
            .chain([
                fund.name.as_str(),
                fund.isin.as_str(),
                fund.provider.as_str(),
                fund.status.as_str(),
                fund.source.as_str(),
            ]),
        &state.search,
    );

    let provider_matches = state.provider.is_empty() || fund.provider == state.provider;
    let status_matches = state.status.is_empty() || fund.status == state.status;

    search_matches && provider_matches && status_matches
}

fn unique_values(funds: &[Fund], value: impl Fn(&Fund) -> &str) -> Vec<String> {
    let mut values = funds
        .iter()
        .map(|fund| value(fund).to_owned())
        .collect::<Vec<_>>();
    values.sort();
    values.dedup();
    values
}

fn filter_label(value: &str) -> &str {
    if value.is_empty() { "all" } else { value }
}

fn selected_fund_panel(ui: &mut egui::Ui, funds: &[Fund], selected: &SelectedInstrument) {
    ui.label(egui::RichText::new("Selected fund").strong());
    let Some(fund_id) = selected.fund_id.as_deref() else {
        ui.label("Selected: -");
        return;
    };
    let Some(fund) = funds.iter().find(|fund| fund.id == fund_id) else {
        ui.label("Selected fund not found.");
        return;
    };

    let rows = vec![
        KvRow::new("Name", fund.name.clone())
            .with_tooltip("Full fund name")
            .copyable(),
        KvRow::new("Provider", fund.provider.clone()),
        KvRow::new("ISIN", fund.isin.clone()).copyable(),
        KvRow::new("Domicile", fund.domicile.clone()),
        KvRow::new("Policy", fund.distribution_policy.clone()),
        KvRow::new("Status", fund.status.clone()),
        KvRow::new("Source", fund.source.clone())
            .with_short_value(format_source(&fund.source))
            .with_tooltip("Reference-data source for this mock fund")
            .copyable(),
    ];
    kv_grid(ui, "etfs_selected_fund_grid", &rows);
}

fn select_fund(
    fund: &Fund,
    selected: &mut SelectedInstrument,
    state: &mut EtfsState,
    index: usize,
) {
    state.table.select(index);
    if let Some(listing) = fund.listings.first() {
        selected.select_listing(fund.id.clone(), listing.id.clone());
    } else {
        selected.select_fund(fund.id.clone());
    }
}

fn focus_etf_cell(state: &mut EtfsState, index: usize, fund: &Fund, column: EtfColumn) {
    let (display, raw) = column.payload(fund);
    state
        .table
        .select_cell(index, column.index(), column.key(), display, raw);
}

fn sync_etf_focus(funds: &[Fund], state: &mut EtfsState) {
    let (Some(row_index), Some(column_index)) = (
        state.table.focused_row_index,
        state.table.focused_column_index,
    ) else {
        return;
    };
    let (Some(fund), Some(column)) = (
        funds.get(row_index),
        EtfColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(fund);
    state
        .table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn listing_tickers(fund: &Fund) -> String {
    fund.listings
        .iter()
        .map(|listing| listing.ticker.as_str())
        .collect::<Vec<_>>()
        .join("/")
}

fn first_listing_ticker(fund: &Fund) -> &str {
    fund.listings
        .first()
        .map(|listing| listing.ticker.as_str())
        .unwrap_or("-")
}

fn fund_copy_text(fund: &Fund) -> String {
    [
        listing_tickers(fund),
        fund.isin.clone(),
        fund.name.clone(),
        fund.provider.clone(),
        fund.status.clone(),
        format_source(&fund.source),
    ]
    .join("\t")
}

fn plot_fund(fund: &Fund, kind: TimeSeriesKind) -> Option<PlotRequest> {
    let listing = fund.listings.first()?;
    Some(PlotRequest::new(
        AnalysisSubject::FundListing {
            fund_id: fund.id.clone(),
            listing_id: listing.id.clone(),
        },
        kind,
        format!("{} {}", listing.ticker, kind.as_str()),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn etf_columns_match_focus_and_layout_descriptors() {
        assert_eq!(EtfColumn::ALL.len(), EtfColumn::DESCRIPTORS.len());
        assert_eq!(EtfColumn::Ticker.key(), "ticker");
        assert_eq!(EtfColumn::Source.index(), 4);
        assert!(!EtfColumn::DESCRIPTORS[EtfColumn::Strategy.index()].default_visible);
        assert!(!EtfColumn::DESCRIPTORS[0].hideable);
    }
}
