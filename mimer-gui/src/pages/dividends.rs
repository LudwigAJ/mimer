use crate::app_model::SelectedInstrument;
use crate::domain::{Distribution, Fund, PortfolioSummary};
use crate::pages::{
    format_money, format_source, header_cell, metric_cell, page_heading, sortable_header_cell,
};
use crate::table_state::{SortSpec, TableId, TableState};
use crate::ui::style;
use eframe::egui;
use egui_extras::{Column, TableBuilder};
use std::cmp::Ordering;

const COL_TICKER: &str = "ticker";
const COL_EX_DATE: &str = "ex_date";
const COL_PAYMENT_DATE: &str = "payment_date";
const COL_AMOUNT: &str = "amount";
const COL_STATUS: &str = "status";
const COL_SOURCE: &str = "source";

#[derive(Clone, Debug)]
pub struct DividendsState {
    pub selected_row: Option<usize>,
    pub table: TableState,
}

impl Default for DividendsState {
    fn default() -> Self {
        Self {
            selected_row: None,
            table: TableState::new(TableId::Dividends),
        }
    }
}

pub fn render(
    ui: &mut egui::Ui,
    distributions: &[Distribution],
    summary: &PortfolioSummary,
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut DividendsState,
) {
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Dividends");
            projected_income(ui, summary);
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                ui.vertical(|ui| distribution_table(ui, distributions, funds, selected, state));
                ui.separator();
                ui.vertical(|ui| distribution_inspector(ui, distributions, summary, state));
            });
        });
}

fn projected_income(ui: &mut egui::Ui, summary: &PortfolioSummary) {
    ui.label(egui::RichText::new("Projected income").strong());
    egui::Grid::new("dividend_projection_grid")
        .num_columns(3)
        .striped(true)
        .show(ui, |ui| {
            metric_cell(
                ui,
                "Trailing 12 months",
                format_money(&summary.base_currency, summary.trailing_12m_income),
            );
            metric_cell(
                ui,
                "Projected annual",
                format_money(&summary.base_currency, summary.projected_annual_income),
            );
            metric_cell(
                ui,
                "Next 90 days",
                format_money(
                    &summary.base_currency,
                    summary.projected_annual_income * 0.26,
                ),
            );
            ui.end_row();
        });
}

fn distribution_table(
    ui: &mut egui::Ui,
    distributions: &[Distribution],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut DividendsState,
) {
    ui.label(egui::RichText::new("Distribution history").strong());
    let visible_indices = visible_distribution_indices(distributions, state);
    state.table.selection.retain_visible(&visible_indices);

    TableBuilder::new(ui)
        .id_salt("distribution_history_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(470.0)
        .column(Column::initial(72.0).at_least(60.0))
        .column(Column::initial(96.0).at_least(84.0))
        .column(Column::initial(110.0).at_least(96.0))
        .column(Column::initial(92.0).at_least(78.0))
        .column(Column::initial(72.0).at_least(60.0))
        .column(Column::initial(98.0).at_least(82.0))
        .column(Column::initial(126.0).at_least(100.0))
        .column(Column::remainder().at_least(120.0))
        .header(18.0, |mut header| {
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_TICKER, "ETF"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_EX_DATE, "Ex-date"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.table, COL_PAYMENT_DATE, "Payment date");
            });
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_AMOUNT, "Amount"));
            header.col(|ui| header_cell(ui, "Currency"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_STATUS, "Status"));
            header.col(|ui| header_cell(ui, "Annualised"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_SOURCE, "Source"));
        })
        .body(|mut body| {
            for index in visible_indices {
                let distribution = &distributions[index];
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        let is_selected = state.selected_row == Some(index)
                            || selected.fund_id.as_deref() == Some(distribution.fund_id.as_str());
                        let response = ui
                            .selectable_label(is_selected, &distribution.ticker)
                            .on_hover_text(format!("fund_id: {}", distribution.fund_id));
                        if response.clicked() {
                            state.selected_row = Some(index);
                            state.table.select(index);
                            select_distribution_fund(funds, distribution, selected);
                        }
                        response.context_menu(|ui| {
                            if ui.button("Copy Row Label").clicked() {
                                ui.copy_text(format!(
                                    "{} {} {}",
                                    distribution.ticker,
                                    distribution.ex_date,
                                    format_money(&distribution.currency, distribution.amount)
                                ));
                                ui.close();
                            }
                            if ui.button("Show Source").clicked() {
                                ui.copy_text(format_source(&distribution.source));
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        ui.label(&distribution.ex_date);
                    });
                    row.col(|ui| {
                        ui.label(&distribution.payment_date);
                    });
                    row.col(|ui| {
                        ui.label(format_money(&distribution.currency, distribution.amount));
                    });
                    row.col(|ui| {
                        ui.label(&distribution.currency);
                    });
                    row.col(|ui| {
                        style::status_badge(ui, &distribution.status);
                    });
                    row.col(|ui| {
                        ui.label(format_money(
                            &distribution.currency,
                            distribution.amount * annualisation_factor(&distribution.status),
                        ));
                    });
                    row.col(|ui| {
                        style::source_badge(ui, &distribution.source);
                    });
                });
            }
        });
}

fn visible_distribution_indices(
    distributions: &[Distribution],
    state: &DividendsState,
) -> Vec<usize> {
    let mut indices = (0..distributions.len()).collect::<Vec<_>>();
    sort_distribution_indices(distributions, &mut indices, state.table.sort.as_ref());
    indices
}

fn sort_distribution_indices(
    distributions: &[Distribution],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };

    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_distributions(
                &distributions[*left],
                &distributions[*right],
                &sort.column,
            ))
            .then_with(|| {
                distributions[*left]
                    .ticker
                    .cmp(&distributions[*right].ticker)
            })
    });
}

fn compare_distributions(left: &Distribution, right: &Distribution, column: &str) -> Ordering {
    match column {
        COL_TICKER => left.ticker.cmp(&right.ticker),
        COL_EX_DATE => left.ex_date.cmp(&right.ex_date),
        COL_PAYMENT_DATE => left.payment_date.cmp(&right.payment_date),
        COL_AMOUNT => left.amount.total_cmp(&right.amount),
        COL_STATUS => left.status.cmp(&right.status),
        COL_SOURCE => left.source.cmp(&right.source),
        _ => Ordering::Equal,
    }
}

fn distribution_inspector(
    ui: &mut egui::Ui,
    distributions: &[Distribution],
    summary: &PortfolioSummary,
    state: &DividendsState,
) {
    ui.set_min_width(260.0);
    ui.label(egui::RichText::new("Distribution inspect").strong());
    let Some(distribution) = state
        .selected_row
        .and_then(|index| distributions.get(index))
    else {
        ui.label("Selected distribution: -");
        ui.label("Click an ETF in the table.");
        return;
    };

    egui::Grid::new("dividend_inspector_grid")
        .num_columns(2)
        .striped(true)
        .show(ui, |ui| {
            ui.label("ETF");
            ui.monospace(&distribution.ticker);
            ui.end_row();
            ui.label("Amount");
            ui.monospace(format_money(&distribution.currency, distribution.amount));
            ui.end_row();
            ui.label("Annualised");
            ui.monospace(format_money(
                &distribution.currency,
                distribution.amount * annualisation_factor(&distribution.status),
            ));
            ui.end_row();
            ui.label("Status");
            style::status_badge(ui, &distribution.status);
            ui.end_row();
            ui.label("Source");
            style::source_badge(ui, &distribution.source);
            ui.end_row();
            ui.label("Assumption");
            ui.label(if distribution.status.eq_ignore_ascii_case("estimated") {
                "monthly run-rate"
            } else {
                "quarterly run-rate"
            });
            ui.end_row();
            ui.label("Portfolio fwd income");
            ui.monospace(format_money(
                &summary.base_currency,
                summary.projected_annual_income,
            ));
            ui.end_row();
        });
}

fn select_distribution_fund(
    funds: &[Fund],
    distribution: &Distribution,
    selected: &mut SelectedInstrument,
) {
    let Some(fund) = funds.iter().find(|fund| fund.id == distribution.fund_id) else {
        selected.select_fund(distribution.fund_id.clone());
        return;
    };

    if let Some(listing) = fund
        .listings
        .iter()
        .find(|listing| listing.ticker == distribution.ticker)
        .or_else(|| fund.listings.first())
    {
        selected.select_listing(fund.id.clone(), listing.id.clone());
    } else {
        selected.select_fund(fund.id.clone());
    }
}

fn annualisation_factor(status: &str) -> f64 {
    if status.eq_ignore_ascii_case("estimated") {
        12.0
    } else {
        4.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn distribution(ticker: &str, amount: f64, date: &str) -> Distribution {
        Distribution {
            fund_id: format!("fund-{ticker}"),
            ticker: ticker.to_owned(),
            ex_date: date.to_owned(),
            payment_date: date.to_owned(),
            amount,
            currency: "GBP".to_owned(),
            status: "Paid".to_owned(),
            source: "issuer".to_owned(),
        }
    }

    #[test]
    fn sorts_distributions_by_amount_desc() {
        let distributions = vec![
            distribution("VUSA", 0.1, "2026-06-01"),
            distribution("ISF", 0.3, "2026-06-01"),
        ];
        let mut state = DividendsState::default();
        state.table.toggle_sort(COL_AMOUNT);
        state.table.toggle_sort(COL_AMOUNT);

        assert_eq!(
            visible_distribution_indices(&distributions, &state),
            vec![1, 0]
        );
    }
}
