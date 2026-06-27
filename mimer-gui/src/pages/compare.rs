#![allow(dead_code)]

use crate::charts::PlotRequest;
use crate::compute::diff::{DiffStatus, field_diff};
use crate::domain::{AnalysisSubject, Fund, HoldingExposure};
use crate::pages::{format_pct, format_source, header_cell, page_heading};
use crate::timeseries::TimeSeriesKind;
use eframe::egui::{self, Color32};
use egui_extras::{Column, TableBuilder};

#[derive(Clone, Debug, Default)]
pub struct CompareState {
    pub left_fund_id: String,
    pub right_fund_id: String,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CompareAction {
    PlotComparison(PlotRequest),
}

pub fn render(
    ui: &mut egui::Ui,
    state: &mut CompareState,
    funds: &[Fund],
    holdings: &[HoldingExposure],
) -> Option<CompareAction> {
    initialise_state(state, funds);
    let mut action = None;

    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Compare | Series-ready | SRC: mock");

            selectors(ui, state, funds, &mut action);
            ui.add_space(8.0);

            let left = funds.iter().find(|fund| fund.id == state.left_fund_id);
            let right = funds.iter().find(|fund| fund.id == state.right_fund_id);

            match (left, right) {
                (Some(left), Some(right)) => {
                    overview(ui, left, right);
                    ui.add_space(8.0);
                    diff_table(ui, left, right);
                    ui.add_space(8.0);
                    listing_table(ui, left, right);
                    ui.add_space(8.0);
                    overlap_table(ui, left, right, holdings);
                }
                _ => {
                    ui.monospace("Select two funds.");
                }
            }
        });

    action
}

fn overlap_table(ui: &mut egui::Ui, left: &Fund, right: &Fund, holdings: &[HoldingExposure]) {
    ui.label(egui::RichText::new("Top holdings overlap / freshness").strong());
    let left_tickers = listing_tickers(left);
    let right_tickers = listing_tickers(right);
    TableBuilder::new(ui)
        .id_salt("compare_overlap_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(180.0)
        .column(Column::initial(170.0).at_least(120.0).clip(true))
        .column(Column::initial(76.0).at_least(60.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(118.0).at_least(90.0))
        .column(Column::remainder().at_least(120.0))
        .header(18.0, |mut header| {
            for label in [
                "Company", "Ticker", "Left wt", "Right wt", "Delta", "Source", "Status",
            ] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for holding in holdings.iter().take(8) {
                let left_weight = weight_for_sources(holdings, &holding.ticker, &left_tickers);
                let right_weight = weight_for_sources(holdings, &holding.ticker, &right_tickers);
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        ui.label(&holding.company);
                    });
                    row.col(|ui| {
                        ui.monospace(&holding.ticker);
                    });
                    row.col(|ui| {
                        ui.label(optional_weight(left_weight));
                    });
                    row.col(|ui| {
                        ui.label(optional_weight(right_weight));
                    });
                    row.col(|ui| {
                        ui.label(match (left_weight, right_weight) {
                            (Some(left), Some(right)) => format_pct(left - right),
                            _ => "-".to_owned(),
                        });
                    });
                    row.col(|ui| {
                        ui.monospace(format_source(&holding.source));
                    });
                    row.col(|ui| {
                        ui.label(if left_weight.is_some() && right_weight.is_some() {
                            "OVERLAP"
                        } else {
                            "ONE-SIDED"
                        });
                    });
                });
            }
        });
}

fn listing_tickers(fund: &Fund) -> Vec<&str> {
    fund.listings
        .iter()
        .map(|listing| listing.ticker.as_str())
        .collect()
}

fn weight_for_sources(
    holdings: &[HoldingExposure],
    holding_ticker: &str,
    source_tickers: &[&str],
) -> Option<f64> {
    holdings
        .iter()
        .find(|holding| {
            holding.ticker == holding_ticker
                && source_tickers
                    .iter()
                    .any(|source_ticker| *source_ticker == holding.source_etf)
        })
        .map(|holding| holding.weight_pct)
}

fn optional_weight(weight: Option<f64>) -> String {
    weight.map(format_pct).unwrap_or_else(|| "-".to_owned())
}

fn initialise_state(state: &mut CompareState, funds: &[Fund]) {
    if state.left_fund_id.is_empty() {
        state.left_fund_id = funds
            .first()
            .map(|fund| fund.id.clone())
            .unwrap_or_default();
    }
    if state.right_fund_id.is_empty() {
        state.right_fund_id = funds
            .get(1)
            .or_else(|| funds.first())
            .map(|fund| fund.id.clone())
            .unwrap_or_default();
    }
}

fn selectors(
    ui: &mut egui::Ui,
    state: &mut CompareState,
    funds: &[Fund],
    action: &mut Option<CompareAction>,
) {
    ui.horizontal(|ui| {
        ui.label("Left");
        fund_combo(ui, "compare_left_fund", &mut state.left_fund_id, funds);
        ui.label("Right");
        fund_combo(ui, "compare_right_fund", &mut state.right_fund_id, funds);
        if ui.button("Compare").clicked() {
            state.message = "DONE mock comparison refreshed".to_owned();
        }
        if ui.button("Plot Comparison").clicked() {
            if let Some(request) =
                plot_comparison_request(funds, &state.left_fund_id, &state.right_fund_id)
            {
                *action = Some(CompareAction::PlotComparison(request));
            } else {
                state.message = "FAILED comparison plot subject".to_owned();
            }
        }
        if !state.message.is_empty() {
            ui.label(egui::RichText::new(&state.message).weak());
        }
    });
}

pub fn plot_comparison_request(
    funds: &[Fund],
    left_fund_id: &str,
    right_fund_id: &str,
) -> Option<PlotRequest> {
    let left = funds.iter().find(|fund| fund.id == left_fund_id)?;
    let right = funds.iter().find(|fund| fund.id == right_fund_id)?;
    let (left_subject, left_label) = primary_listing_subject_and_label(left)?;
    let (right_subject, right_label) = primary_listing_subject_and_label(right)?;

    Some(
        PlotRequest::new(
            left_subject,
            TimeSeriesKind::Price,
            format!("{left_label} vs {right_label} Price"),
        )
        .with_comparison(right_subject),
    )
}

fn primary_listing_subject_and_label(fund: &Fund) -> Option<(AnalysisSubject, String)> {
    let listing = fund.listings.first()?;
    Some((
        AnalysisSubject::FundListing {
            fund_id: fund.id.clone(),
            listing_id: listing.id.clone(),
        },
        listing.ticker.clone(),
    ))
}

fn fund_combo(ui: &mut egui::Ui, id: &str, selected: &mut String, funds: &[Fund]) {
    egui::ComboBox::from_id_salt(id)
        .selected_text(fund_label(funds, selected))
        .show_ui(ui, |ui| {
            for fund in funds {
                ui.selectable_value(selected, fund.id.clone(), fund_label_short(fund));
            }
        });
}

fn overview(ui: &mut egui::Ui, left: &Fund, right: &Fund) {
    ui.label(egui::RichText::new("Selected funds").strong());
    egui::Grid::new("compare_overview")
        .num_columns(3)
        .striped(true)
        .show(ui, |ui| {
            ui.label("");
            ui.strong(listing_label(left));
            ui.strong(listing_label(right));
            ui.end_row();

            overview_row(ui, "Name", &left.name, &right.name);
            overview_row(ui, "Provider", &left.provider, &right.provider);
            overview_row(ui, "ISIN", &left.isin, &right.isin);
            overview_row(ui, "Strategy", &left.strategy, &right.strategy);
            overview_row(
                ui,
                "Base currency",
                &left.base_currency,
                &right.base_currency,
            );
            overview_row(
                ui,
                "Distribution",
                &left.distribution_frequency,
                &right.distribution_frequency,
            );
            overview_row(ui, "Status", &left.status, &right.status);
        });
}

fn overview_row(ui: &mut egui::Ui, label: &str, left: &str, right: &str) {
    ui.label(label);
    ui.label(left);
    ui.label(right);
    ui.end_row();
}

fn diff_table(ui: &mut egui::Ui, left: &Fund, right: &Fund) {
    let diffs = [
        field_diff("provider", Some(&left.provider), Some(&right.provider)),
        field_diff("isin", Some(&left.isin), Some(&right.isin)),
        field_diff("strategy", Some(&left.strategy), Some(&right.strategy)),
        field_diff(
            "ocf/ter",
            Some(&format_pct(f64::from(left.ocf_ter_pct))),
            Some(&format_pct(f64::from(right.ocf_ter_pct))),
        ),
        field_diff(
            "distribution frequency",
            Some(&left.distribution_frequency),
            Some(&right.distribution_frequency),
        ),
        field_diff(
            "replication/type",
            Some(&left.replication),
            Some(&right.replication),
        ),
        field_diff("status", Some(&left.status), Some(&right.status)),
    ];

    ui.label(egui::RichText::new("Field diff").strong());
    TableBuilder::new(ui)
        .id_salt("compare_diff_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(220.0)
        .column(Column::initial(170.0).at_least(120.0))
        .column(Column::initial(280.0).at_least(180.0).clip(true))
        .column(Column::initial(280.0).at_least(180.0).clip(true))
        .column(Column::remainder().at_least(100.0))
        .header(18.0, |mut header| {
            for label in ["Field", "Left", "Right", "Status"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for diff in &diffs {
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        ui.label(&diff.field_name);
                    });
                    row.col(|ui| {
                        ui.label(diff.left_value.as_deref().unwrap_or("-"));
                    });
                    row.col(|ui| {
                        ui.label(diff.right_value.as_deref().unwrap_or("-"));
                    });
                    row.col(|ui| {
                        ui.colored_label(status_color(diff.status), diff.status.as_str());
                    });
                });
            }
        });
}

fn listing_table(ui: &mut egui::Ui, left: &Fund, right: &Fund) {
    ui.label(egui::RichText::new("Listing coverage").strong());
    TableBuilder::new(ui)
        .id_salt("compare_listing_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(180.0)
        .column(Column::initial(86.0).at_least(66.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(180.0).at_least(120.0).clip(true))
        .column(Column::initial(86.0).at_least(66.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::remainder().at_least(120.0).clip(true))
        .header(18.0, |mut header| {
            for label in [
                "L ticker",
                "L exchange",
                "L ccy",
                "L venue",
                "R ticker",
                "R exchange",
                "R ccy",
                "R venue",
            ] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            let rows = left.listings.len().max(right.listings.len());
            for index in 0..rows {
                let left_listing = left.listings.get(index);
                let right_listing = right.listings.get(index);
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        ui.monospace(
                            left_listing
                                .map(|listing| listing.ticker.as_str())
                                .unwrap_or("-"),
                        );
                    });
                    row.col(|ui| {
                        ui.monospace(
                            left_listing
                                .map(|listing| listing.exchange.as_str())
                                .unwrap_or("-"),
                        );
                    });
                    row.col(|ui| {
                        ui.monospace(
                            left_listing
                                .map(|listing| listing.currency.as_str())
                                .unwrap_or("-"),
                        );
                    });
                    row.col(|ui| {
                        ui.label(
                            left_listing
                                .map(|listing| listing.venue_name.as_str())
                                .unwrap_or("-"),
                        );
                    });
                    row.col(|ui| {
                        ui.monospace(
                            right_listing
                                .map(|listing| listing.ticker.as_str())
                                .unwrap_or("-"),
                        );
                    });
                    row.col(|ui| {
                        ui.monospace(
                            right_listing
                                .map(|listing| listing.exchange.as_str())
                                .unwrap_or("-"),
                        );
                    });
                    row.col(|ui| {
                        ui.monospace(
                            right_listing
                                .map(|listing| listing.currency.as_str())
                                .unwrap_or("-"),
                        );
                    });
                    row.col(|ui| {
                        ui.label(
                            right_listing
                                .map(|listing| listing.venue_name.as_str())
                                .unwrap_or("-"),
                        );
                    });
                });
            }
        });
}

fn fund_label(funds: &[Fund], fund_id: &str) -> String {
    funds
        .iter()
        .find(|fund| fund.id == fund_id)
        .map(fund_label_short)
        .unwrap_or_else(|| "No fund".to_owned())
}

fn fund_label_short(fund: &Fund) -> String {
    format!("{} | {}", listing_label(fund), fund.name)
}

fn listing_label(fund: &Fund) -> String {
    fund.listings
        .iter()
        .map(|listing| listing.ticker.as_str())
        .collect::<Vec<_>>()
        .join("/")
}

fn status_color(status: DiffStatus) -> Color32 {
    match status {
        DiffStatus::Unchanged => Color32::from_rgb(150, 150, 150),
        DiffStatus::Added => Color32::from_rgb(120, 190, 120),
        DiffStatus::Removed => Color32::from_rgb(230, 110, 95),
        DiffStatus::Changed => Color32::from_rgb(220, 176, 82),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::FundListing;

    fn fund(id: &str, ticker: &str, listing_id: &str) -> Fund {
        Fund {
            id: id.to_owned(),
            name: format!("{ticker} fund"),
            provider: "provider".to_owned(),
            isin: format!("isin-{ticker}"),
            strategy: "equity".to_owned(),
            domicile: "Ireland".to_owned(),
            base_currency: "GBP".to_owned(),
            distribution_policy: "Distributing".to_owned(),
            ocf_ter_pct: 0.07,
            distribution_frequency: "Quarterly".to_owned(),
            replication: "Physical".to_owned(),
            status: "Active".to_owned(),
            last_refreshed: "2026-06-20".to_owned(),
            source: "seed".to_owned(),
            listings: vec![FundListing {
                id: listing_id.to_owned(),
                fund_id: id.to_owned(),
                ticker: ticker.to_owned(),
                exchange: "XLON".to_owned(),
                currency: "GBP".to_owned(),
                venue_name: "London Stock Exchange".to_owned(),
                currency_unit: "GBp".to_owned(),
                figi: None,
                sedol: None,
                last_price: 1.0,
                last_price_date: "2026-06-20".to_owned(),
                status: "Active".to_owned(),
                source: "seed".to_owned(),
            }],
        }
    }

    #[test]
    fn maps_compare_state_to_chart_overlay_request() {
        let funds = vec![
            fund("fund-vusa", "VUSA", "listing-vusa"),
            fund("fund-isf", "ISF", "listing-isf"),
        ];

        let request =
            plot_comparison_request(&funds, "fund-vusa", "fund-isf").expect("comparison request");

        assert_eq!(request.series_kind, TimeSeriesKind::Price);
        assert_eq!(request.comparison_subjects.len(), 1);
        assert_eq!(request.label, "VUSA vs ISF Price");
    }
}
