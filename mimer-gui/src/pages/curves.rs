use crate::compute::curves as curve_compute;
use crate::pages::{format_number, format_pct, format_source, header_cell, page_heading};
use crate::ui::date::date_text_field;
use crate::ui::metrics;
use eframe::egui;
use egui_extras::{Column, TableBuilder};
use egui_plot::{Line, Plot, PlotPoints};

#[derive(Clone, Debug)]
pub struct CurvesState {
    pub currency: CurveCurrency,
    pub curve_type: CurveType,
    pub valuation_date: String,
}

impl Default for CurvesState {
    fn default() -> Self {
        Self {
            currency: CurveCurrency::Gbp,
            curve_type: CurveType::Zero,
            valuation_date: "2026-06-20".to_owned(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CurveCurrency {
    Gbp,
    Usd,
    Eur,
}

impl CurveCurrency {
    const ALL: [Self; 3] = [Self::Gbp, Self::Usd, Self::Eur];

    fn as_str(self) -> &'static str {
        match self {
            Self::Gbp => "GBP",
            Self::Usd => "USD",
            Self::Eur => "EUR",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CurveType {
    Zero,
    Discount,
    Par,
    Spread,
    Forward,
}

impl CurveType {
    const ALL: [Self; 5] = [
        Self::Zero,
        Self::Discount,
        Self::Par,
        Self::Spread,
        Self::Forward,
    ];

    fn as_str(self) -> &'static str {
        match self {
            Self::Zero => "zero",
            Self::Discount => "discount",
            Self::Par => "par",
            Self::Spread => "spread",
            Self::Forward => "forward",
        }
    }
}

pub fn render(ui: &mut egui::Ui, state: &mut CurvesState) {
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Curves");
            ui.label(
                "Mock rates and discount-curve workspace for future pricing/NAV calculations.",
            );
            ui.add_space(6.0);

            controls(ui, state);
            ui.add_space(8.0);

            let curve =
                curve_compute::mock_curve(state.currency.as_str(), state.curve_type.as_str());
            curve_table(ui, &curve);
            ui.add_space(metrics::SPACE_2);
            curve_plot(ui, state, &curve);
        });
}

fn controls(ui: &mut egui::Ui, state: &mut CurvesState) {
    ui.horizontal_wrapped(|ui| {
        ui.label("Currency");
        egui::ComboBox::from_id_salt("curve_currency_select")
            .selected_text(state.currency.as_str())
            .show_ui(ui, |ui| {
                for currency in CurveCurrency::ALL {
                    ui.selectable_value(&mut state.currency, currency, currency.as_str());
                }
            });

        ui.label("Curve");
        egui::ComboBox::from_id_salt("curve_type_select")
            .selected_text(state.curve_type.as_str())
            .show_ui(ui, |ui| {
                for curve_type in CurveType::ALL {
                    ui.selectable_value(&mut state.curve_type, curve_type, curve_type.as_str());
                }
            });

        ui.label("Date");
        date_text_field(ui, "curves_valuation_date", &mut state.valuation_date);

        ui.monospace("Source: mock close marks");
    });
}

fn curve_table(ui: &mut egui::Ui, curve: &[curve_compute::CurvePoint]) {
    ui.label(egui::RichText::new("Curve points").strong());
    let table_height = (ui.available_height() - 24.0).clamp(220.0, 420.0);
    TableBuilder::new(ui)
        .id_salt("curves_points_table")
        .striped(true)
        .resizable(true)
        .auto_shrink(false)
        .max_scroll_height(table_height)
        .column(Column::initial(74.0).at_least(58.0))
        .column(Column::initial(70.0).at_least(56.0))
        .column(Column::initial(88.0).at_least(70.0))
        .column(Column::initial(98.0).at_least(76.0))
        .column(Column::initial(88.0).at_least(70.0))
        .column(Column::initial(90.0).at_least(70.0))
        .column(Column::initial(102.0).at_least(82.0))
        .column(Column::remainder().at_least(80.0))
        .header(18.0, |mut header| {
            header.col(|ui| header_cell(ui, "Tenor"));
            header.col(|ui| header_cell(ui, "Years"));
            header.col(|ui| header_cell(ui, "Zero"));
            header.col(|ui| header_cell(ui, "Discount"));
            header.col(|ui| header_cell(ui, "Par"));
            header.col(|ui| header_cell(ui, "Spread"));
            header.col(|ui| header_cell(ui, "Source"));
            header.col(|ui| header_cell(ui, "Status"));
        })
        .body(|mut body| {
            for point in curve {
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        ui.monospace(&point.tenor);
                    });
                    row.col(|ui| {
                        ui.label(format_number(point.years, 2));
                    });
                    row.col(|ui| {
                        ui.label(format_pct(point.value_pct));
                    });
                    row.col(|ui| {
                        ui.label(format_number(point.discount_factor, 5));
                    });
                    row.col(|ui| {
                        ui.label(format_pct(point.par_rate_pct));
                    });
                    row.col(|ui| {
                        ui.label(format!("{}bp", format_number(point.spread_bps, 0)));
                    });
                    row.col(|ui| {
                        ui.monospace(format_source(&point.source));
                    });
                    row.col(|ui| {
                        ui.label(&point.status);
                    });
                });
            }
        });
}

fn curve_plot(ui: &mut egui::Ui, state: &CurvesState, curve: &[curve_compute::CurvePoint]) {
    ui.label(egui::RichText::new("Curve shape").strong());
    let points: PlotPoints<'_> = curve
        .iter()
        .map(|point| [point.years, point.value_pct])
        .collect();
    let line = Line::new(
        format!("{} {}", state.currency.as_str(), state.curve_type.as_str()),
        points,
    );

    Plot::new("curves_plot")
        .width(ui.available_width())
        .height((ui.available_height() - 26.0).clamp(260.0, 480.0))
        .allow_scroll(false)
        .show(ui, |plot_ui| {
            plot_ui.line(line);
        });
}
