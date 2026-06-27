use crate::app_model::SelectedInstrument;
use crate::domain::{Fund, HoldingExposure};
use crate::filter::any_contains_ci;
use crate::pages::{format_number, format_pct, page_heading, sortable_header_cell};
use crate::table_state::{SortSpec, TableId, TableState};
use crate::ui::style;
use eframe::egui;
use egui_extras::{Column, TableBuilder};
use std::cmp::Ordering;

const COL_COMPANY: &str = "company";
const COL_TICKER: &str = "ticker";
const COL_COUNTRY: &str = "country";
const COL_SECTOR: &str = "sector";
const COL_WEIGHT: &str = "weight";
const COL_CHANGE: &str = "change";
const COL_SOURCE_ETF: &str = "source_etf";
const COL_AS_OF: &str = "as_of";
const COL_SOURCE: &str = "source";

#[derive(Clone, Debug)]
pub struct HoldingsState {
    pub table: TableState,
}

impl Default for HoldingsState {
    fn default() -> Self {
        Self {
            table: TableState::new(TableId::Holdings),
        }
    }
}

pub fn render(
    ui: &mut egui::Ui,
    holdings: &[HoldingExposure],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut HoldingsState,
) -> bool {
    let mut open_detail = false;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Holdings");
            ui.label("Look-through holdings from provider factsheets and snapshots.");
            ui.add_space(6.0);
            filters(ui, state);
            ui.add_space(4.0);

            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    open_detail = holdings_table(ui, holdings, funds, selected, state);
                });
                ui.separator();
                ui.vertical(|ui| source_panel(ui, holdings, funds, selected));
            });
        });
    open_detail
}

fn filters(ui: &mut egui::Ui, state: &mut HoldingsState) {
    ui.horizontal(|ui| {
        ui.label("Filter");
        ui.add_sized(
            [260.0, 20.0],
            egui::TextEdit::singleline(&mut state.table.filter)
                .hint_text("company / ticker / country / sector / ETF"),
        );
        if ui.button("Clear").clicked() {
            state.table.filter.clear();
            state.table.clear_selection();
        }
    });
}

fn holdings_table(
    ui: &mut egui::Ui,
    holdings: &[HoldingExposure],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut HoldingsState,
) -> bool {
    let filtered_holdings = visible_holding_indices(holdings, state);
    state.table.selection.retain_visible(&filtered_holdings);
    let mut open_detail = false;

    ui.label(
        egui::RichText::new(format!(
            "Rows ({}/{})",
            filtered_holdings.len(),
            holdings.len()
        ))
        .strong(),
    );

    TableBuilder::new(ui)
        .id_salt("holdings_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(540.0)
        .column(Column::initial(190.0).at_least(140.0).clip(true))
        .column(Column::initial(82.0).at_least(68.0))
        .column(Column::initial(118.0).at_least(92.0))
        .column(Column::initial(132.0).at_least(100.0))
        .column(Column::initial(86.0).at_least(72.0))
        .column(Column::initial(86.0).at_least(72.0))
        .column(Column::initial(86.0).at_least(72.0))
        .column(Column::initial(104.0).at_least(88.0))
        .column(Column::remainder().at_least(120.0))
        .header(18.0, |mut header| {
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_COMPANY, "Company"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_TICKER, "Ticker"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_COUNTRY, "Country"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_SECTOR, "Sector"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_WEIGHT, "Weight"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_CHANGE, "Change"));
            header
                .col(|ui| sortable_header_cell(ui, &mut state.table, COL_SOURCE_ETF, "Source ETF"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_AS_OF, "As-of date"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_SOURCE, "Source"));
        })
        .body(|mut body| {
            for index in filtered_holdings {
                let holding = holdings[index].clone();
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        let response = ui.selectable_label(
                            state.table.selection.is_selected(index),
                            &holding.company,
                        );
                        if response.clicked() {
                            state.table.select(index);
                        }
                        response.context_menu(|ui| {
                            if ui.button("Open Holding").clicked() {
                                state.table.select(index);
                                select_ticker(funds, &holding.source_etf, selected);
                                open_detail = true;
                                ui.close();
                            }
                            if ui.button("Plot if available").clicked() {
                                ui.copy_text(holding.ticker.clone());
                                ui.close();
                            }
                            if ui.button("Show Source ETFs").clicked() {
                                ui.copy_text(holding.source_etf.clone());
                                ui.close();
                            }
                            if ui.button("Explain Exposure").clicked() {
                                ui.copy_text(format!(
                                    "{} {} via {}",
                                    holding.ticker,
                                    format_pct(holding.weight_pct),
                                    holding.source_etf
                                ));
                                ui.close();
                            }
                            if ui.button("Copy Row Label").clicked() {
                                ui.copy_text(format!("{} {}", holding.ticker, holding.company));
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        ui.monospace(&holding.ticker);
                    });
                    row.col(|ui| {
                        ui.label(&holding.country);
                    });
                    row.col(|ui| {
                        ui.label(&holding.sector);
                    });
                    row.col(|ui| {
                        ui.label(format_pct(holding.weight_pct));
                    });
                    row.col(|ui| {
                        ui.label(format_optional_pct(holding.change_since_previous_pct));
                    });
                    row.col(|ui| {
                        let is_selected = selected_source_ticker(selected, funds)
                            .is_some_and(|ticker| ticker == holding.source_etf)
                            || state.table.selection.is_selected(index);
                        let response = ui
                            .selectable_label(is_selected, &holding.source_etf)
                            .on_hover_text("Select source ETF");
                        if response.clicked() {
                            state.table.select(index);
                            select_ticker(funds, &holding.source_etf, selected);
                        }
                        if response.double_clicked() {
                            state.table.select(index);
                            select_ticker(funds, &holding.source_etf, selected);
                            open_detail = true;
                        }
                    });
                    row.col(|ui| {
                        ui.label(&holding.as_of_date);
                    });
                    row.col(|ui| {
                        style::source_badge(ui, &holding.source);
                    });
                });
            }
        });
    open_detail
}

fn visible_holding_indices(holdings: &[HoldingExposure], state: &HoldingsState) -> Vec<usize> {
    let mut indices = holdings
        .iter()
        .enumerate()
        .filter(|(_, holding)| holding_matches(holding, &state.table.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_holding_indices(holdings, &mut indices, state.table.sort.as_ref());
    indices
}

fn sort_holding_indices(
    holdings: &[HoldingExposure],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };

    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_holdings(
                &holdings[*left],
                &holdings[*right],
                &sort.column,
            ))
            .then_with(|| holdings[*left].ticker.cmp(&holdings[*right].ticker))
    });
}

fn compare_holdings(left: &HoldingExposure, right: &HoldingExposure, column: &str) -> Ordering {
    match column {
        COL_COMPANY => left.company.cmp(&right.company),
        COL_TICKER => left.ticker.cmp(&right.ticker),
        COL_COUNTRY => left.country.cmp(&right.country),
        COL_SECTOR => left.sector.cmp(&right.sector),
        COL_WEIGHT => left.weight_pct.total_cmp(&right.weight_pct),
        COL_CHANGE => compare_optional_f64(
            left.change_since_previous_pct,
            right.change_since_previous_pct,
        ),
        COL_SOURCE_ETF => left.source_etf.cmp(&right.source_etf),
        COL_AS_OF => left.as_of_date.cmp(&right.as_of_date),
        COL_SOURCE => left.source.cmp(&right.source),
        _ => Ordering::Equal,
    }
}

fn compare_optional_f64(left: Option<f64>, right: Option<f64>) -> Ordering {
    match (left, right) {
        (Some(left), Some(right)) => left.total_cmp(&right),
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => Ordering::Equal,
    }
}

fn holding_matches(holding: &HoldingExposure, filter: &str) -> bool {
    any_contains_ci(
        [
            holding.company.as_str(),
            holding.ticker.as_str(),
            holding.country.as_str(),
            holding.sector.as_str(),
            holding.source_etf.as_str(),
            holding.as_of_date.as_str(),
            holding.source.as_str(),
        ],
        filter,
    )
}

fn format_optional_pct(value: Option<f64>) -> String {
    value
        .map(|value| {
            let sign = if value >= 0.0 { "+" } else { "" };
            format!("{sign}{}%", format_number(value, 2))
        })
        .unwrap_or_else(|| "-".to_owned())
}

fn source_panel(
    ui: &mut egui::Ui,
    holdings: &[HoldingExposure],
    funds: &[Fund],
    selected: &SelectedInstrument,
) {
    ui.set_min_width(260.0);
    ui.label(egui::RichText::new("Selected source").strong());
    let Some(fund_id) = selected.fund_id.as_deref() else {
        ui.label("Selected: -");
        return;
    };
    let Some(fund) = funds.iter().find(|fund| fund.id == fund_id) else {
        ui.label("Selected fund not found.");
        return;
    };

    let rows = holdings
        .iter()
        .filter(|holding| {
            fund.listings
                .iter()
                .any(|listing| listing.ticker == holding.source_etf)
        })
        .collect::<Vec<_>>();
    ui.label(&fund.name);
    ui.monospace(&fund.isin);
    ui.separator();
    ui.label(format!("Mock holdings rows: {}", rows.len()));
    for holding in rows.iter().take(5) {
        ui.horizontal(|ui| {
            ui.monospace(format_pct(holding.weight_pct));
            ui.label(&holding.company);
        });
    }
}

fn select_ticker(funds: &[Fund], ticker: &str, selected: &mut SelectedInstrument) {
    if let Some((fund, listing)) = funds.iter().find_map(|fund| {
        fund.listings
            .iter()
            .find(|listing| listing.ticker == ticker)
            .map(|listing| (fund, listing))
    }) {
        selected.select_listing(fund.id.clone(), listing.id.clone());
    }
}

fn selected_source_ticker<'a>(selected: &SelectedInstrument, funds: &'a [Fund]) -> Option<&'a str> {
    let listing_id = selected.listing_id.as_deref()?;
    funds
        .iter()
        .flat_map(|fund| fund.listings.iter())
        .find(|listing| listing.id == listing_id)
        .map(|listing| listing.ticker.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn holding(ticker: &str, weight_pct: f64) -> HoldingExposure {
        HoldingExposure {
            company: format!("{ticker} plc"),
            ticker: ticker.to_owned(),
            country: "GB".to_owned(),
            sector: "Financials".to_owned(),
            weight_pct,
            change_since_previous_pct: None,
            source_etf: "ISF".to_owned(),
            as_of_date: "2026-06-20".to_owned(),
            source: "issuer".to_owned(),
        }
    }

    #[test]
    fn sorts_holdings_by_weight_desc() {
        let holdings = vec![holding("A", 1.0), holding("B", 3.0)];
        let mut state = HoldingsState::default();
        state.table.toggle_sort(COL_WEIGHT);
        state.table.toggle_sort(COL_WEIGHT);

        assert_eq!(visible_holding_indices(&holdings, &state), vec![1, 0]);
    }
}
