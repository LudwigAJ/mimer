pub mod add_instrument;
pub mod alerts;
pub mod analytics;
pub mod charts;
pub mod compare;
pub mod curves;
pub mod data_operations;
pub mod diffs;
pub mod dividends;
pub mod documents;
pub mod etfs;
pub mod exposure;
pub mod fund_detail;
pub mod hierarchy;
pub mod holdings;
pub mod jobs;
pub mod portfolio;
pub mod search;
pub mod settings;
pub mod spreads;

pub use crate::format::{
    fmt_decimal as format_number, fmt_money as format_money, fmt_percent as format_pct,
    fmt_signed_money as format_signed_money, fmt_source as format_source,
};
use crate::table_state::TableState;
use eframe::egui;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Page {
    Portfolio,
    Etfs,
    Hierarchy,
    FundDetail,
    Charts,
    AddInstrument,
    Dividends,
    Holdings,
    Exposure,
    DataOperations,
    Alerts,
    Documents,
    DocumentViewer,
    Jobs,
    Analytics,
    Curves,
    Compare,
    Spreads,
    Diffs,
    Search,
    Settings,
}

impl Page {
    pub const ALL: [Self; 20] = [
        Self::Portfolio,
        Self::Etfs,
        Self::Hierarchy,
        Self::FundDetail,
        Self::Charts,
        Self::AddInstrument,
        Self::Dividends,
        Self::Holdings,
        Self::Exposure,
        Self::DataOperations,
        Self::Alerts,
        Self::Documents,
        Self::Jobs,
        Self::Analytics,
        Self::Curves,
        Self::Compare,
        Self::Spreads,
        Self::Diffs,
        Self::Search,
        Self::Settings,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Portfolio => "Portfolio",
            Self::Etfs => "ETFs",
            Self::Hierarchy => "Hierarchy",
            Self::FundDetail => "Fund Detail",
            Self::Charts => "Charts",
            Self::AddInstrument => "Add Instrument",
            Self::Dividends => "Dividends",
            Self::Holdings => "Holdings",
            Self::Exposure => "Exposure",
            Self::DataOperations => "Data Operations",
            Self::Alerts => "Alerts",
            Self::Documents => "Documents",
            Self::DocumentViewer => "Document Viewer",
            Self::Jobs => "Jobs",
            Self::Analytics => "Analytics",
            Self::Curves => "Curves",
            Self::Compare => "Compare",
            Self::Spreads => "Spreads",
            Self::Diffs => "Changes",
            Self::Search => "Search",
            Self::Settings => "Settings",
        }
    }
}

pub fn page_heading(ui: &mut egui::Ui, title: &str) {
    crate::ui::style::page_header(ui, title, None, None, |_| {});
    ui.add_space(crate::ui::metrics::SPACE_1);
}

pub fn header_cell(ui: &mut egui::Ui, text: &str) {
    ui.label(crate::ui::style::table_header_text(text));
}

pub fn sortable_header_cell(ui: &mut egui::Ui, table: &mut TableState, column: &str, text: &str) {
    let label = table
        .sort_direction(column)
        .map(|direction| format!("{text} {}", direction.marker()))
        .unwrap_or_else(|| text.to_owned());

    if ui
        .add(egui::Button::new(crate::ui::style::table_header_text(&label)).frame(false))
        .on_hover_text("Sort cycles ascending, descending, then default.")
        .clicked()
    {
        table.toggle_sort(column);
    }
}

pub fn metric_cell(ui: &mut egui::Ui, label: &str, value: impl Into<String>) {
    ui.vertical(|ui| {
        ui.label(egui::RichText::new(label).weak());
        ui.monospace(value.into());
    });
}

pub fn bool_text(value: bool) -> &'static str {
    if value { "yes" } else { "no" }
}
