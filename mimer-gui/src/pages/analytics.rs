use crate::app_model::DashboardSnapshot;
use crate::compute::portfolio::PositionOverride;
use crate::compute::{
    diagnostics as diagnostics_compute, explain, exposure as exposure_compute, nav,
    portfolio as portfolio_compute, regression,
};
use crate::pages::{format_money, format_number, format_pct, header_cell, page_heading};
use crate::ui::metrics;
use eframe::egui;
use egui_extras::{Column, TableBuilder};

const ANALYTICS_METRIC_COLUMN_MIN_WIDTH: f32 = 420.0;
const ANALYTICS_DATA_COLUMN_MIN_WIDTH: f32 = 560.0;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AnalyticsAction {
    ClearRowOverrides { listing_id: String },
}

pub fn render(
    ui: &mut egui::Ui,
    snapshot: &DashboardSnapshot,
    overrides: &[PositionOverride],
) -> Option<AnalyticsAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Analytics | Diagnostics | SRC: derived/mock");

            responsive_pair(
                ui,
                ANALYTICS_METRIC_COLUMN_MIN_WIDTH,
                |ui| portfolio_checks(ui, snapshot),
                |ui| diagnostics_panel(ui, snapshot, overrides),
            );
            ui.add_space(metrics::SPACE_2);

            responsive_pair(
                ui,
                ANALYTICS_DATA_COLUMN_MIN_WIDTH,
                |ui| override_panel(ui, snapshot, overrides, &mut action),
                regression_table,
            );
            ui.add_space(metrics::SPACE_2);

            responsive_pair(
                ui,
                ANALYTICS_METRIC_COLUMN_MIN_WIDTH,
                |ui| nav_stub(ui, snapshot),
                |ui| risk_and_concentration(ui, snapshot),
            );
            ui.add_space(metrics::SPACE_2);

            exposure_checks(ui, snapshot);
            ui.add_space(metrics::SPACE_2);

            responsive_pair(
                ui,
                ANALYTICS_METRIC_COLUMN_MIN_WIDTH,
                |ui| pnl_explain_placeholder(ui, snapshot),
                |ui| projection_placeholder(ui, snapshot),
            );
            ui.add_space(metrics::SPACE_2);

            explain_panel(ui, snapshot, overrides);
        });

    action
}

struct MetricRow {
    label: &'static str,
    value: String,
}

fn metric_row(label: &'static str, value: impl Into<String>) -> MetricRow {
    MetricRow {
        label,
        value: value.into(),
    }
}

fn metric_table(ui: &mut egui::Ui, id: &str, title: &str, rows: Vec<MetricRow>) {
    ui.label(egui::RichText::new(title).strong());

    let max_height = ((rows.len() as f32 + 1.0) * metrics::ROW_HEIGHT_COMPACT).clamp(72.0, 240.0);

    TableBuilder::new(ui)
        .id_salt(id)
        .striped(true)
        .resizable(true)
        .max_scroll_height(max_height)
        .column(Column::initial(180.0).at_least(120.0).clip(true))
        .column(Column::remainder().at_least(120.0).clip(true))
        .header(18.0, |mut header| {
            header.col(|ui| header_cell(ui, "Metric"));
            header.col(|ui| header_cell(ui, "Value"));
        })
        .body(|mut body| {
            for row in &rows {
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut table_row| {
                    table_row.col(|ui| {
                        ui.label(row.label);
                    });
                    table_row.col(|ui| {
                        let response = ui.monospace(&row.value);
                        if row.value.len() > 18 {
                            response.on_hover_text(&row.value);
                        }
                    });
                });
            }
        });
}

fn responsive_pair(
    ui: &mut egui::Ui,
    min_column_width: f32,
    left: impl FnOnce(&mut egui::Ui),
    right: impl FnOnce(&mut egui::Ui),
) {
    let gap = metrics::SPACE_3;
    let available_width = ui.available_width();

    if available_width >= (min_column_width * 2.0) + gap {
        let column_width = ((available_width - gap) / 2.0).floor();
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = gap;
            ui.vertical(|ui| {
                ui.set_width(column_width);
                left(ui);
            });
            ui.vertical(|ui| {
                ui.set_width(column_width);
                right(ui);
            });
        });
    } else {
        left(ui);
        ui.add_space(metrics::SPACE_2);
        right(ui);
    }
}

fn portfolio_checks(ui: &mut egui::Ui, snapshot: &DashboardSnapshot) {
    let computed = portfolio_compute::calculate_summary(
        &snapshot.positions,
        &snapshot.portfolio_summary.base_currency,
    );
    let income_yield = portfolio_compute::projected_income_yield(&snapshot.portfolio_summary);
    let value_delta = computed.total_value - snapshot.portfolio_summary.total_value;
    let income_delta =
        computed.projected_annual_income - snapshot.portfolio_summary.projected_annual_income;

    metric_table(
        ui,
        "analytics_portfolio_checks",
        "Portfolio compute checks",
        vec![
            metric_row(
                "Snapshot value",
                format_money(
                    &snapshot.portfolio_summary.base_currency,
                    snapshot.portfolio_summary.total_value,
                ),
            ),
            metric_row(
                "Computed value",
                format_money(&computed.base_currency, computed.total_value),
            ),
            metric_row(
                "Value delta",
                format_money(&computed.base_currency, value_delta),
            ),
            metric_row("Projected income yield", format_pct(income_yield)),
            metric_row(
                "Snapshot income",
                format_money(
                    &snapshot.portfolio_summary.base_currency,
                    snapshot.portfolio_summary.projected_annual_income,
                ),
            ),
            metric_row(
                "Computed income",
                format_money(&computed.base_currency, computed.projected_annual_income),
            ),
            metric_row(
                "Income delta",
                format_money(&computed.base_currency, income_delta),
            ),
            metric_row("Stale warnings", computed.stale_warning_count.to_string()),
        ],
    );
}

fn nav_stub(ui: &mut egui::Ui, snapshot: &DashboardSnapshot) {
    let gross_assets = snapshot.portfolio_summary.total_value * 1.003;
    let liabilities = snapshot.portfolio_summary.total_value * 0.003;
    let shares_outstanding = 1_000_000.0;
    let estimate = nav::estimate_nav(gross_assets, liabilities, shares_outstanding);

    metric_table(
        ui,
        "analytics_nav_stub",
        "NAV/re-pricing stub",
        vec![
            metric_row(
                "Gross assets",
                format_money(
                    &snapshot.portfolio_summary.base_currency,
                    estimate.gross_assets,
                ),
            ),
            metric_row(
                "Liabilities",
                format_money(
                    &snapshot.portfolio_summary.base_currency,
                    estimate.liabilities,
                ),
            ),
            metric_row("Shares out", format_number(estimate.shares_outstanding, 0)),
            metric_row(
                "NAV/share",
                format_money(
                    &snapshot.portfolio_summary.base_currency,
                    estimate.nav_per_share,
                ),
            ),
        ],
    );
}

fn exposure_checks(ui: &mut egui::Ui, snapshot: &DashboardSnapshot) {
    let countries = exposure_compute::aggregate_by_country(&snapshot.holdings);
    let sectors = exposure_compute::aggregate_by_sector(&snapshot.holdings);

    ui.label(egui::RichText::new("Exposure aggregation checks").strong());
    responsive_pair(
        ui,
        ANALYTICS_METRIC_COLUMN_MIN_WIDTH,
        |ui| compact_exposure_table(ui, "analytics_country_check", "By country", &countries),
        |ui| compact_exposure_table(ui, "analytics_sector_check", "By sector", &sectors),
    );
}

fn compact_exposure_table(
    ui: &mut egui::Ui,
    id: &str,
    title: &str,
    rows: &[crate::domain::ExposureSlice],
) {
    ui.vertical(|ui| {
        ui.label(title);
        TableBuilder::new(ui)
            .id_salt(id)
            .striped(true)
            .resizable(true)
            .max_scroll_height(160.0)
            .column(Column::initial(180.0).at_least(120.0).clip(true))
            .column(Column::initial(84.0).at_least(68.0))
            .header(18.0, |mut header| {
                header.col(|ui| header_cell(ui, "Bucket"));
                header.col(|ui| header_cell(ui, "Weight"));
            })
            .body(|mut body| {
                for row in rows.iter().take(5) {
                    body.row(20.0, |mut table_row| {
                        table_row.col(|ui| {
                            ui.label(&row.label);
                        });
                        table_row.col(|ui| {
                            ui.label(format_pct(row.value_pct));
                        });
                    });
                }
            });
    });
}

fn regression_table(ui: &mut egui::Ui) {
    let rows = regression::mock_regressions();
    ui.label(egui::RichText::new("Regression diagnostics").strong());
    TableBuilder::new(ui)
        .id_salt("analytics_regression_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(180.0)
        .column(Column::initial(80.0).at_least(64.0))
        .column(Column::initial(120.0).at_least(90.0))
        .column(Column::initial(80.0).at_least(64.0))
        .column(Column::initial(80.0).at_least(64.0))
        .column(Column::initial(86.0).at_least(70.0))
        .column(Column::initial(110.0).at_least(90.0))
        .column(Column::remainder().at_least(90.0))
        .header(18.0, |mut header| {
            for label in [
                "Target", "Factor", "Beta", "t-stat", "R2", "Window", "Status",
            ] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for row in &rows {
                body.row(20.0, |mut table_row| {
                    table_row.col(|ui| {
                        ui.monospace(&row.target);
                    });
                    table_row.col(|ui| {
                        ui.label(&row.factor);
                    });
                    table_row.col(|ui| {
                        ui.label(format_number(row.beta, 2));
                    });
                    table_row.col(|ui| {
                        ui.label(format_number(row.t_stat, 1));
                    });
                    table_row.col(|ui| {
                        ui.label(format_pct(row.r_squared * 100.0));
                    });
                    table_row.col(|ui| {
                        ui.label(&row.window);
                    });
                    table_row.col(|ui| {
                        ui.label(&row.status);
                    });
                });
            }
        });
}

fn diagnostics_panel(
    ui: &mut egui::Ui,
    snapshot: &DashboardSnapshot,
    overrides: &[PositionOverride],
) {
    let diagnostics = diagnostics_compute::aggregate_diagnostics(
        &snapshot.positions,
        &snapshot.funds,
        &snapshot.distributions,
        &snapshot.documents,
        &snapshot.job_runs,
        &snapshot.alerts,
        overrides,
    );

    metric_table(
        ui,
        "analytics_diagnostics_table",
        "Diagnostics",
        vec![
            metric_row("Fresh", diagnostics.fresh_rows.to_string()),
            metric_row("Stale", diagnostics.stale_rows.to_string()),
            metric_row("Missing", diagnostics.missing_rows.to_string()),
            metric_row("Suspicious", diagnostics.suspicious_rows.to_string()),
            metric_row("Manual", diagnostics.manual_overrides.to_string()),
            metric_row(
                "EST/DERIVED",
                diagnostics.estimated_or_derived_values.to_string(),
            ),
            metric_row("Failed jobs", diagnostics.failed_jobs.to_string()),
            metric_row("AMBIG", diagnostics.ambiguous_instruments.to_string()),
            metric_row("MOCK/SEED", diagnostics.mock_or_seed_rows.to_string()),
            metric_row("CONFLICT", diagnostics.source_conflicts.to_string()),
        ],
    );
}

fn override_panel(
    ui: &mut egui::Ui,
    snapshot: &DashboardSnapshot,
    overrides: &[PositionOverride],
    action: &mut Option<AnalyticsAction>,
) {
    ui.label(egui::RichText::new("Overrides").strong());
    if overrides.is_empty() {
        ui.monospace("No overrides. Edit a Portfolio cell to create a local manual override.");
        return;
    }

    TableBuilder::new(ui)
        .id_salt("analytics_override_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(180.0)
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(120.0).at_least(90.0))
        .column(Column::initial(110.0).at_least(86.0))
        .column(Column::initial(110.0).at_least(86.0))
        .column(Column::initial(190.0).at_least(130.0).clip(true))
        .column(Column::remainder().at_least(80.0))
        .header(18.0, |mut header| {
            for label in [
                "Entity", "Field", "Original", "Override", "Affects", "Clear",
            ] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for position_override in overrides {
                let position = snapshot
                    .positions
                    .iter()
                    .find(|position| position.listing_id == position_override.listing_id);
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        ui.monospace(
                            position
                                .map(|position| position.ticker.as_str())
                                .unwrap_or(position_override.listing_id.as_str()),
                        );
                    });
                    row.col(|ui| {
                        ui.label(position_override.field.as_str());
                    });
                    row.col(|ui| {
                        ui.monospace("seed");
                    });
                    row.col(|ui| {
                        ui.monospace(format_number(position_override.value, 4));
                    });
                    row.col(|ui| {
                        ui.label(override_affects(position_override.field));
                    });
                    row.col(|ui| {
                        if ui.small_button("Clear").clicked() {
                            *action = Some(AnalyticsAction::ClearRowOverrides {
                                listing_id: position_override.listing_id.clone(),
                            });
                        }
                    });
                });
            }
        });
}

fn override_affects(field: crate::compute::portfolio::PositionOverrideField) -> &'static str {
    match field {
        crate::compute::portfolio::PositionOverrideField::Units
        | crate::compute::portfolio::PositionOverrideField::Price => "market value, weight, income",
        crate::compute::portfolio::PositionOverrideField::TrailingYieldPct => "projected income",
    }
}

fn risk_and_concentration(ui: &mut egui::Ui, snapshot: &DashboardSnapshot) {
    let top_weight = snapshot
        .positions
        .iter()
        .map(|position| position.portfolio_weight_pct)
        .fold(0.0, f64::max);
    let top_3_weight = snapshot
        .positions
        .iter()
        .take(3)
        .map(|position| position.portfolio_weight_pct)
        .sum::<f64>();
    let stale_inputs = snapshot.portfolio_summary.stale_warning_count;

    metric_table(
        ui,
        "analytics_concentration",
        "Risk/return and concentration placeholders",
        vec![
            metric_row("Top position", format_pct(top_weight)),
            metric_row("Top 3 positions", format_pct(top_3_weight)),
            metric_row("Mock volatility", format_pct(12.8)),
            metric_row("Stale inputs", stale_inputs.to_string()),
            metric_row("Mock Sharpe", format_number(0.74, 2)),
            metric_row("Mock max drawdown", format_pct(-18.4)),
            metric_row("Income concentration", format_pct(45.7)),
            metric_row("Status", "MOCK"),
        ],
    );
}

fn explain_panel(ui: &mut egui::Ui, snapshot: &DashboardSnapshot, overrides: &[PositionOverride]) {
    ui.label(egui::RichText::new("Explain this number").strong());
    let selected_position = snapshot
        .selected
        .listing_id
        .as_deref()
        .and_then(|listing_id| {
            snapshot
                .positions
                .iter()
                .find(|position| position.listing_id == listing_id)
        });

    let Some(position) = selected_position.or_else(|| snapshot.positions.first()) else {
        ui.label("No position available for dependency tracing.");
        return;
    };

    let manual_count = overrides
        .iter()
        .filter(|position_override| position_override.listing_id == position.listing_id)
        .count();
    let trace = explain::market_value_trace(
        position,
        &snapshot.portfolio_summary.base_currency,
        manual_count,
    );
    ui.monospace(format!(
        "{} = {}",
        trace.label,
        format_money(&trace.unit, trace.value)
    ));
    for note in &trace.notes {
        ui.label(note);
    }

    TableBuilder::new(ui)
        .id_salt("analytics_dependency_trace")
        .striped(true)
        .resizable(true)
        .max_scroll_height(120.0)
        .column(Column::initial(110.0).at_least(84.0))
        .column(Column::initial(160.0).at_least(120.0).clip(true))
        .column(Column::initial(110.0).at_least(84.0))
        .column(Column::initial(120.0).at_least(90.0))
        .column(Column::remainder().at_least(90.0))
        .header(18.0, |mut header| {
            for label in ["Entity", "Field", "Value used", "Source", "Status"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for dependency in &trace.dependencies {
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        ui.monospace(&dependency.entity_type);
                    });
                    row.col(|ui| {
                        ui.label(&dependency.field_name);
                    });
                    row.col(|ui| {
                        ui.monospace(&dependency.value_used);
                    });
                    row.col(|ui| {
                        ui.monospace(&dependency.source);
                    });
                    row.col(|ui| {
                        ui.label(&dependency.status);
                    });
                });
            }
        });
}

fn pnl_explain_placeholder(ui: &mut egui::Ui, snapshot: &DashboardSnapshot) {
    metric_table(
        ui,
        "analytics_pnl_explain",
        "PnL Explain",
        vec![
            metric_row(
                "Mock total change",
                format_money(
                    &snapshot.portfolio_summary.base_currency,
                    snapshot.portfolio_summary.daily_change,
                ),
            ),
            metric_row("Price move", format_pct(62.0)),
            metric_row("FX", format_pct(8.0)),
            metric_row("Distributions", format_pct(30.0)),
            metric_row("Status", "MOCK"),
            metric_row("Inputs", "effective positions"),
            metric_row(
                "Missing/stale",
                snapshot.portfolio_summary.stale_warning_count.to_string(),
            ),
            metric_row("Next", "engine placeholder"),
        ],
    );
}

fn projection_placeholder(ui: &mut egui::Ui, snapshot: &DashboardSnapshot) {
    ui.label(egui::RichText::new("Projection / What-if").strong());
    let rows = vec![
        (
            "ETF price +5%",
            format_money(
                &snapshot.portfolio_summary.base_currency,
                snapshot.portfolio_summary.total_value * 0.05,
            ),
            "yield unchanged",
            "MOCK",
        ),
        (
            "Distribution -10%",
            format_money(
                &snapshot.portfolio_summary.base_currency,
                -snapshot.portfolio_summary.projected_annual_income * 0.10,
            ),
            "FX not applied",
            "effective data",
        ),
    ];

    TableBuilder::new(ui)
        .id_salt("analytics_projection")
        .striped(true)
        .resizable(true)
        .max_scroll_height(92.0)
        .column(Column::initial(150.0).at_least(120.0).clip(true))
        .column(Column::initial(120.0).at_least(92.0))
        .column(Column::initial(130.0).at_least(96.0).clip(true))
        .column(Column::remainder().at_least(90.0).clip(true))
        .header(18.0, |mut header| {
            for label in ["Scenario", "Impact", "Shock", "Status/inputs"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for (scenario, impact, shock, status) in rows {
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut table_row| {
                    table_row.col(|ui| {
                        ui.label(scenario);
                    });
                    table_row.col(|ui| {
                        ui.monospace(&impact);
                    });
                    table_row.col(|ui| {
                        ui.label(shock);
                    });
                    table_row.col(|ui| {
                        ui.label(status);
                    });
                });
            }
        });
}
