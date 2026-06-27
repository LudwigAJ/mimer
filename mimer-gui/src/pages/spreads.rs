#![allow(dead_code)]

use crate::charts::PlotRequest;
use crate::domain::{AnalysisSubject, Fund};
use crate::pages::{format_source, header_cell, page_heading};
use crate::timeseries::TimeSeriesKind;
use eframe::egui;
use egui_extras::{Column, TableBuilder};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SpreadMetric {
    Price,
    Return,
    Yield,
    Nav,
    MarketValue,
}

impl SpreadMetric {
    pub const ALL: [Self; 5] = [
        Self::Price,
        Self::Return,
        Self::Yield,
        Self::Nav,
        Self::MarketValue,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Price => "price",
            Self::Return => "return",
            Self::Yield => "yield",
            Self::Nav => "NAV",
            Self::MarketValue => "market value",
        }
    }
}

#[derive(Clone, Debug)]
pub struct SpreadsState {
    pub left_fund_id: String,
    pub right_fund_id: String,
    pub metric: SpreadMetric,
    pub message: String,
}

impl Default for SpreadsState {
    fn default() -> Self {
        Self {
            left_fund_id: String::new(),
            right_fund_id: String::new(),
            metric: SpreadMetric::Price,
            message: String::new(),
        }
    }
}

pub fn render(ui: &mut egui::Ui, state: &mut SpreadsState, funds: &[Fund]) {
    initialise_state(state, funds);

    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Spreads | 1-to-1 relative view | SRC: mock");
            ui.label("Mock spread workflow for left minus right relationships. No backend analytics are run in this iteration.");
            ui.add_space(6.0);

            ui.horizontal_wrapped(|ui| {
                ui.label("Left");
                fund_combo(ui, "spreads_left_fund", &mut state.left_fund_id, funds);
                ui.label("Right");
                fund_combo(ui, "spreads_right_fund", &mut state.right_fund_id, funds);
                ui.label("Metric");
                egui::ComboBox::from_id_salt("spreads_metric")
                    .selected_text(state.metric.label())
                    .show_ui(ui, |ui| {
                        for metric in SpreadMetric::ALL {
                            ui.selectable_value(&mut state.metric, metric, metric.label());
                        }
                    });
                if ui.button("Refresh mock spread").clicked() {
                    state.message = spread_label(state, funds);
                }
                if !state.message.is_empty() {
                    ui.label(egui::RichText::new(&state.message).weak());
                }
            });

            ui.add_space(8.0);
            spread_summary(ui, state, funds);
            ui.add_space(8.0);
            spread_table(ui, state, funds);
        });
}

pub fn set_spread(state: &mut SpreadsState, left_fund_id: String, right_fund_id: String) {
    state.left_fund_id = left_fund_id;
    state.right_fund_id = right_fund_id;
    state.metric = SpreadMetric::Price;
    state.message = "CMD: spread set".to_owned();
}

pub fn plot_spread_request(
    funds: &[Fund],
    left_fund_id: &str,
    right_fund_id: &str,
) -> Option<PlotRequest> {
    let left = funds.iter().find(|fund| fund.id == left_fund_id)?;
    let right = funds.iter().find(|fund| fund.id == right_fund_id)?;
    let (left_subject, left_label) = primary_listing_subject_and_label(left)?;
    let (right_subject, right_label) = primary_listing_subject_and_label(right)?;

    Some(PlotRequest::spread(
        left_subject,
        right_subject,
        TimeSeriesKind::Price,
        format!("Spread {left_label} - {right_label} Price"),
    ))
}

fn initialise_state(state: &mut SpreadsState, funds: &[Fund]) {
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

fn fund_combo(ui: &mut egui::Ui, id: &str, selected: &mut String, funds: &[Fund]) {
    egui::ComboBox::from_id_salt(id)
        .selected_text(fund_short_label(funds, selected))
        .show_ui(ui, |ui| {
            for fund in funds {
                ui.selectable_value(selected, fund.id.clone(), fund_short_label_one(fund));
            }
        });
}

fn spread_summary(ui: &mut egui::Ui, state: &SpreadsState, funds: &[Fund]) {
    let left = funds.iter().find(|fund| fund.id == state.left_fund_id);
    let right = funds.iter().find(|fund| fund.id == state.right_fund_id);

    TableBuilder::new(ui)
        .id_salt("spreads_summary_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(92.0)
        .column(Column::initial(120.0).at_least(86.0))
        .column(Column::initial(220.0).at_least(150.0).clip(true))
        .column(Column::initial(220.0).at_least(150.0).clip(true))
        .column(Column::remainder().at_least(160.0))
        .header(18.0, |mut header| {
            for label in ["Operation", "Left", "Right", "Status"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            body.row(22.0, |mut row| {
                row.col(|ui| {
                    ui.monospace("left - right");
                });
                row.col(|ui| {
                    ui.label(
                        left.map(fund_short_label_one)
                            .unwrap_or_else(|| "-".to_owned()),
                    );
                });
                row.col(|ui| {
                    ui.label(
                        right
                            .map(fund_short_label_one)
                            .unwrap_or_else(|| "-".to_owned()),
                    );
                });
                row.col(|ui| {
                    ui.monospace(format!(
                        "{} | {}",
                        state.metric.label(),
                        format_source("derived")
                    ));
                });
            });
        });
}

fn spread_table(ui: &mut egui::Ui, state: &SpreadsState, funds: &[Fund]) {
    let left = funds.iter().find(|fund| fund.id == state.left_fund_id);
    let right = funds.iter().find(|fund| fund.id == state.right_fund_id);
    ui.label(egui::RichText::new("Mock spread observations").strong());

    TableBuilder::new(ui)
        .id_salt("spreads_observations_table")
        .striped(true)
        .resizable(true)
        .auto_shrink(false)
        .max_scroll_height(280.0)
        .column(Column::initial(110.0).at_least(90.0))
        .column(Column::initial(130.0).at_least(100.0))
        .column(Column::initial(130.0).at_least(100.0))
        .column(Column::initial(130.0).at_least(100.0))
        .column(Column::initial(120.0).at_least(90.0))
        .column(Column::remainder().at_least(140.0))
        .header(18.0, |mut header| {
            for label in ["Date", "Left", "Right", "Spread", "Metric", "Source"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for (date, left_value, right_value) in [
                ("2026-04-30", 100.0, 98.6),
                ("2026-05-31", 102.4, 99.2),
                ("2026-06-20", 103.1, 100.6),
            ] {
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        ui.label(date);
                    });
                    row.col(|ui| {
                        ui.monospace(format!("{left_value:.2}"));
                    });
                    row.col(|ui| {
                        ui.monospace(format!("{right_value:.2}"));
                    });
                    row.col(|ui| {
                        ui.monospace(format!("{:.2}", left_value - right_value));
                    });
                    row.col(|ui| {
                        ui.label(state.metric.label());
                    });
                    row.col(|ui| {
                        ui.monospace(format!(
                            "{} / {} | {}",
                            left.map(fund_short_label_one)
                                .unwrap_or_else(|| "-".to_owned()),
                            right
                                .map(fund_short_label_one)
                                .unwrap_or_else(|| "-".to_owned()),
                            format_source("derived")
                        ));
                    });
                });
            }
        });
}

fn spread_label(state: &SpreadsState, funds: &[Fund]) -> String {
    format!(
        "DONE {} - {} {} spread",
        fund_short_label(funds, &state.left_fund_id),
        fund_short_label(funds, &state.right_fund_id),
        state.metric.label()
    )
}

fn fund_short_label(funds: &[Fund], fund_id: &str) -> String {
    funds
        .iter()
        .find(|fund| fund.id == fund_id)
        .map(fund_short_label_one)
        .unwrap_or_else(|| "-".to_owned())
}

fn fund_short_label_one(fund: &Fund) -> String {
    fund.listings
        .first()
        .map(|listing| listing.ticker.clone())
        .unwrap_or_else(|| fund.name.clone())
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn set_spread_updates_left_and_right_subjects() {
        let mut state = SpreadsState::default();

        set_spread(&mut state, "fund-vusa".to_owned(), "fund-jepg".to_owned());

        assert_eq!(state.left_fund_id, "fund-vusa");
        assert_eq!(state.right_fund_id, "fund-jepg");
        assert_eq!(state.metric, SpreadMetric::Price);
    }

    #[test]
    fn creates_explicit_spread_plot_request() {
        let funds = vec![
            test_fund("fund-vusa", "VUSA"),
            test_fund("fund-jepg", "JEPG"),
        ];

        let request = plot_spread_request(&funds, "fund-vusa", "fund-jepg")
            .expect("listing-backed funds create a spread plot request");

        assert_eq!(request.mode, crate::charts::ChartMode::Spread);
        assert_eq!(request.series_kind, TimeSeriesKind::Price);
        assert_eq!(request.comparison_subjects.len(), 1);
        assert_eq!(request.label, "Spread VUSA - JEPG Price");
    }

    fn test_fund(id: &str, ticker: &str) -> Fund {
        Fund {
            id: id.to_owned(),
            name: format!("{ticker} fund"),
            provider: "Provider".to_owned(),
            isin: format!("ISIN-{ticker}"),
            strategy: "Equity".to_owned(),
            domicile: "Ireland".to_owned(),
            base_currency: "GBP".to_owned(),
            distribution_policy: "Distributing".to_owned(),
            ocf_ter_pct: 0.1,
            distribution_frequency: "Quarterly".to_owned(),
            replication: "Physical".to_owned(),
            status: "Active".to_owned(),
            last_refreshed: "2026-06-20".to_owned(),
            source: "seed".to_owned(),
            listings: vec![crate::domain::FundListing {
                id: format!("listing-{ticker}"),
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
}
