use crate::app_model::SelectedInstrument;
use crate::domain::{Alert, AlertSeverity, Fund};
use crate::filter::any_contains_ci;
use crate::pages::{format_source, header_cell, sortable_header_cell};
use crate::table_state::{ColumnDescriptor, SortSpec, TableId, TableLayoutRegistry, TableState};
use crate::ui::table_layout::{managed_column, managed_table_revision, table_layout_controls};
use crate::ui::{metrics, style};
use eframe::egui;
use egui_extras::TableBuilder;
use std::cmp::Ordering;

const COL_SEVERITY: &str = "severity";
const COL_CATEGORY: &str = "category";
const COL_TITLE: &str = "title";
const COL_FUND: &str = "fund";
const COL_STATUS: &str = "status";
const COL_SOURCE: &str = "source";
const COL_READ: &str = "read";
const COL_DISMISSED: &str = "dismissed";
const COL_CREATED: &str = "created";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum AlertColumn {
    Severity,
    Category,
    Title,
    Message,
    Fund,
    Status,
    Source,
    Read,
    Dismissed,
    Created,
}

impl AlertColumn {
    const ALL: [Self; 10] = [
        Self::Severity,
        Self::Category,
        Self::Title,
        Self::Message,
        Self::Fund,
        Self::Status,
        Self::Source,
        Self::Read,
        Self::Dismissed,
        Self::Created,
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("alert column is in ALL")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Severity => COL_SEVERITY,
            Self::Category => COL_CATEGORY,
            Self::Title => COL_TITLE,
            Self::Message => "message",
            Self::Fund => COL_FUND,
            Self::Status => COL_STATUS,
            Self::Source => COL_SOURCE,
            Self::Read => COL_READ,
            Self::Dismissed => COL_DISMISSED,
            Self::Created => COL_CREATED,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Severity => "Severity",
            Self::Category => "Category",
            Self::Title => "Title",
            Self::Message => "Message",
            Self::Fund => "Fund",
            Self::Status => "Status",
            Self::Source => "Source",
            Self::Read => "Read",
            Self::Dismissed => "Dismissed",
            Self::Created => "Created",
        }
    }

    fn payload(self, alert: &Alert) -> (String, String) {
        let value = match self {
            Self::Severity => alert.severity.as_str().to_owned(),
            Self::Category => alert.category.clone(),
            Self::Title => alert.title.clone(),
            Self::Message => alert.message.clone(),
            Self::Fund => alert.fund_ticker.clone().unwrap_or_else(|| "-".to_owned()),
            Self::Status => alert.status.clone(),
            Self::Source => alert.source.clone(),
            Self::Read => alert.read.to_string(),
            Self::Dismissed => alert.dismissed.to_string(),
            Self::Created => alert.created_time.clone(),
        };
        (value.clone(), value)
    }
}

const ALERT_COLUMNS: [ColumnDescriptor; 11] = [
    ColumnDescriptor::new("severity", "Severity", 82.0, 68.0, 140.0).required(),
    ColumnDescriptor::new("category", "Category", 108.0, 84.0, 220.0),
    ColumnDescriptor::new("title", "Title", 180.0, 130.0, 360.0)
        .required()
        .clipped(),
    ColumnDescriptor::new("message", "Message", 330.0, 220.0, 680.0)
        .hidden_by_default()
        .clipped(),
    ColumnDescriptor::new("fund", "Fund", 84.0, 68.0, 140.0),
    ColumnDescriptor::new("status", "Status", 84.0, 68.0, 150.0).required(),
    ColumnDescriptor::new("source", "Source", 104.0, 82.0, 210.0).required(),
    ColumnDescriptor::new("read", "Read", 64.0, 52.0, 100.0),
    ColumnDescriptor::new("dismissed", "Dismissed", 86.0, 70.0, 140.0),
    ColumnDescriptor::new("created", "Created", 126.0, 104.0, 230.0),
    ColumnDescriptor::new("actions", "Actions", 150.0, 118.0, 320.0).required(),
];

#[derive(Clone, Debug)]
pub struct AlertsState {
    pub table: TableState,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AlertsAction {
    OpenAffected { ticker: String },
    RunRelatedJob { job_name: String },
    Feedback(String),
}

impl Default for AlertsState {
    fn default() -> Self {
        Self {
            table: TableState::new(TableId::Alerts),
        }
    }
}

pub fn render(
    ui: &mut egui::Ui,
    alerts: &mut [Alert],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut AlertsState,
    layouts: &mut TableLayoutRegistry,
) -> Option<AlertsAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let open = alerts
                .iter()
                .filter(|alert| !alert.dismissed && !alert.status.eq_ignore_ascii_case("resolved"))
                .count();
            let critical = alerts
                .iter()
                .filter(|alert| alert.severity == AlertSeverity::Critical && !alert.dismissed)
                .count();
            let subtitle =
                format!("{open} open · {critical} critical · read/dismiss state is local");
            style::page_header(ui, "Alerts", Some("Operations"), Some(&subtitle), |ui| {
                style::mock_badge(ui);
                if critical > 0 {
                    style::status_badge(ui, "CRITICAL");
                }
            });
            ui.add_space(6.0);

            alert_totals(ui, alerts);
            ui.add_space(6.0);
            filters(ui, state);
            ui.add_space(4.0);

            let filtered_indices = visible_alert_indices(alerts, state);
            state.table.retain_visible(&filtered_indices);
            ui.label(
                egui::RichText::new(format!(
                    "Alert rows ({}/{})",
                    filtered_indices.len(),
                    alerts.len()
                ))
                .strong(),
            );
            if filtered_indices.is_empty() {
                style::state_message(ui, "EMPTY", "No alerts match the current filter.");
                return;
            }

            let visible_row = state
                .table
                .focused_row_index
                .or(state.table.selected_index())
                .and_then(|index| alerts.get(index))
                .map(|alert| {
                    (
                        alert.id.as_str(),
                        layouts.visible_row_text(
                            TableId::Alerts,
                            &ALERT_COLUMNS,
                            &[
                                ("severity", alert.severity.as_str().to_owned()),
                                ("category", alert.category.clone()),
                                ("title", alert.title.clone()),
                                ("message", alert.message.clone()),
                                (
                                    "fund",
                                    alert.fund_ticker.clone().unwrap_or_else(|| "-".to_owned()),
                                ),
                                ("status", alert.status.clone()),
                                ("source", alert.source.clone()),
                                ("read", alert.read.to_string()),
                                ("dismissed", alert.dismissed.to_string()),
                                ("created", alert.created_time.clone()),
                                ("actions", "open/read/dismiss/resolve".to_owned()),
                            ],
                        ),
                    )
                });
            if table_layout_controls(
                ui,
                layouts,
                TableId::Alerts,
                &ALERT_COLUMNS,
                state
                    .table
                    .focused_column_index
                    .and_then(|index| AlertColumn::ALL.get(index))
                    .map(|column| column.key()),
                visible_row,
            ) {
                state.table.clear_focus();
            }
            let revision = managed_table_revision(layouts, TableId::Alerts, &ALERT_COLUMNS);
            let mut table = TableBuilder::new(ui)
                .id_salt(("alerts_table", revision))
                .striped(true)
                .resizable(false)
                .max_scroll_height(540.0);
            for descriptor in ALERT_COLUMNS {
                table = table.column(managed_column(
                    layouts,
                    TableId::Alerts,
                    &ALERT_COLUMNS,
                    descriptor,
                ));
            }
            table
                .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
                    header.col(|ui| {
                        sortable_header_cell(
                            ui,
                            &mut state.table,
                            COL_SEVERITY,
                            AlertColumn::Severity.label(),
                        )
                    });
                    header.col(|ui| {
                        sortable_header_cell(ui, &mut state.table, COL_CATEGORY, "Category")
                    });
                    header.col(|ui| {
                        sortable_header_cell(
                            ui,
                            &mut state.table,
                            COL_TITLE,
                            AlertColumn::Title.label(),
                        )
                    });
                    header.col(|ui| header_cell(ui, AlertColumn::Message.label()));
                    header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_FUND, "Fund"));
                    header
                        .col(|ui| sortable_header_cell(ui, &mut state.table, COL_STATUS, "Status"));
                    header
                        .col(|ui| sortable_header_cell(ui, &mut state.table, COL_SOURCE, "Source"));
                    header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_READ, "Read"));
                    header.col(|ui| {
                        sortable_header_cell(ui, &mut state.table, COL_DISMISSED, "Dismissed")
                    });
                    header.col(|ui| {
                        sortable_header_cell(ui, &mut state.table, COL_CREATED, "Created")
                    });
                    header.col(|ui| header_cell(ui, "Actions"));
                })
                .body(|mut body| {
                    for index in filtered_indices {
                        let alert = &mut alerts[index];
                        body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                            row.set_overline(state.table.is_focused_row(index));
                            row.set_selected(state.table.selection.is_selected(index));
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Severity.index()),
                                );
                                style::alert_severity_badge(ui, alert.severity);
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Category.index()),
                                );
                                ui.label(&alert.category);
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Title.index()),
                                );
                                let response = ui
                                    .selectable_label(
                                        state.table.selection.is_selected(index),
                                        &alert.title,
                                    )
                                    .on_hover_text(format!(
                                        "Double-click to open affected object when available\nalert_id: {}",
                                        alert.id
                                    ))
                                    .on_hover_cursor(egui::CursorIcon::PointingHand);
                                if response.clicked() {
                                    select_alert_cell(
                                        state,
                                        index,
                                        alert,
                                        AlertColumn::Title,
                                    );
                                }
                                if response.double_clicked() {
                                    select_alert_cell(
                                        state,
                                        index,
                                        alert,
                                        AlertColumn::Title,
                                    );
                                    if let Some(ticker) = alert.fund_ticker.as_deref() {
                                        select_ticker(funds, ticker, selected);
                                        action = Some(AlertsAction::OpenAffected {
                                            ticker: ticker.to_owned(),
                                        });
                                    } else {
                                        action = Some(AlertsAction::Feedback(format!(
                                            "ALERT: selected {}",
                                            alert.id
                                        )));
                                    }
                                }
                                response.context_menu(|ui| {
                                    if ui.button("Open Affected").clicked() {
                                        if let Some(ticker) = alert.fund_ticker.as_deref() {
                                            select_ticker(funds, ticker, selected);
                                            state.table.select(index);
                                            action = Some(AlertsAction::OpenAffected {
                                                ticker: ticker.to_owned(),
                                            });
                                        }
                                        ui.close();
                                    }
                                    if ui.button("Mark Read").clicked() {
                                        mark_alert_read(alert);
                                        state.table.select(index);
                                        action = Some(AlertsAction::Feedback(format!(
                                            "ALERT: read {}",
                                            alert.id
                                        )));
                                        ui.close();
                                    }
                                    if ui.button("Dismiss").clicked() {
                                        dismiss_alert(alert);
                                        state.table.select(index);
                                        action = Some(AlertsAction::Feedback(format!(
                                            "ALERT: dismissed {}",
                                            alert.id
                                        )));
                                        ui.close();
                                    }
                                    if ui.button("Resolve").clicked() {
                                        resolve_alert(alert);
                                        state.table.select(index);
                                        action = Some(AlertsAction::Feedback(format!(
                                            "ALERT: resolved {}",
                                            alert.id
                                        )));
                                        ui.close();
                                    }
                                    if ui.button("Run Related Job").clicked() {
                                        state.table.select(index);
                                        action = Some(AlertsAction::RunRelatedJob {
                                            job_name: related_job_name(alert),
                                        });
                                        ui.close();
                                    }
                                    if ui.button("Copy Alert").clicked() {
                                        ui.copy_text(alert_copy_text(alert));
                                        action = Some(AlertsAction::Feedback(format!(
                                            "COPIED: alert {}",
                                            alert.id
                                        )));
                                        ui.close();
                                    }
                                    if ui.button("Show Source").clicked() {
                                        ui.copy_text(format_source(&alert.source));
                                        action = Some(AlertsAction::Feedback(format!(
                                            "SOURCE: {} {}",
                                            alert.id,
                                            format_source(&alert.source)
                                        )));
                                        ui.close();
                                    }
                                });
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Message.index()),
                                );
                                ui.label(&alert.message);
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Fund.index()),
                                );
                                if let Some(ticker) = alert.fund_ticker.as_deref() {
                                    let is_selected = selected_ticker(selected, funds)
                                        == Some(ticker)
                                        || state.table.selection.is_selected(index);
                                    if ui.selectable_label(is_selected, ticker).clicked() {
                                        select_alert_cell(
                                            state,
                                            index,
                                            alert,
                                            AlertColumn::Fund,
                                        );
                                        select_ticker(funds, ticker, selected);
                                    }
                                } else {
                                    ui.monospace("-");
                                }
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Status.index()),
                                );
                                style::status_badge(ui, &alert.status);
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Source.index()),
                                );
                                style::source_badge(ui, &alert.source);
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Read.index()),
                                );
                                if ui.checkbox(&mut alert.read, "").clicked() {
                                    select_alert_cell(
                                        state,
                                        index,
                                        alert,
                                        AlertColumn::Read,
                                    );
                                }
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Dismissed.index()),
                                );
                                if ui.checkbox(&mut alert.dismissed, "").clicked() {
                                    select_alert_cell(
                                        state,
                                        index,
                                        alert,
                                        AlertColumn::Dismissed,
                                    );
                                }
                            });
                            row.col(|ui| {
                                style::focused_table_cell(
                                    ui,
                                    state
                                        .table
                                        .is_focused_cell(index, AlertColumn::Created.index()),
                                );
                                ui.label(&alert.created_time);
                            });
                            row.col(|ui| {
                                ui.horizontal_wrapped(|ui| {
                                    if crate::ui::actions::action_button(
                                        ui,
                                        "Open",
                                        "Open affected object.",
                                    )
                                    .clicked()
                                        && let Some(ticker) = alert.fund_ticker.as_deref()
                                    {
                                        select_ticker(funds, ticker, selected);
                                        state.table.select(index);
                                        action = Some(AlertsAction::OpenAffected {
                                            ticker: ticker.to_owned(),
                                        });
                                    }
                                    if crate::ui::actions::action_button(
                                        ui,
                                        "Run",
                                        "Queue a related mock job.",
                                    )
                                    .clicked()
                                    {
                                        state.table.select(index);
                                        action = Some(AlertsAction::RunRelatedJob {
                                            job_name: related_job_name(alert),
                                        });
                                    }
                                    if crate::ui::actions::action_button(
                                        ui,
                                        "Copy",
                                        "Copy alert details.",
                                    )
                                    .clicked()
                                    {
                                        ui.copy_text(alert_copy_text(alert));
                                        state.table.select(index);
                                        action = Some(AlertsAction::Feedback(format!(
                                            "COPIED: alert {}",
                                            alert.id
                                        )));
                                    }
                                });
                            });
                        });
                    }
                });
        });
    action
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    alerts: &[Alert],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut AlertsState,
    layouts: &mut TableLayoutRegistry,
) -> bool {
    if ctx.text_edit_focused() {
        return false;
    }

    let visible_indices = visible_alert_indices(alerts, state);
    let mut moved = false;
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        state.table.move_focus_row(&visible_indices, -1, Some(0));
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        state.table.move_focus_row(&visible_indices, 1, Some(0));
        moved = true;
    }
    let visible_columns = layouts
        .visible_indices(TableId::Alerts, &ALERT_COLUMNS)
        .into_iter()
        .filter(|index| *index < AlertColumn::ALL.len())
        .collect::<Vec<_>>();
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        state.table.move_focus_visible_column(&visible_columns, -1);
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        state.table.move_focus_visible_column(&visible_columns, 1);
        moved = true;
    }
    if moved {
        sync_alert_focus(alerts, state);
    }

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        if state.table.selected_index().is_none() {
            state.table.move_focus_row(&visible_indices, 1, Some(0));
            sync_alert_focus(alerts, state);
        }
        if let Some(alert) = state
            .table
            .selected_index()
            .and_then(|index| alerts.get(index))
            && let Some(ticker) = alert.fund_ticker.as_deref()
        {
            select_ticker(funds, ticker, selected);
            return true;
        }
    }

    false
}

fn filters(ui: &mut egui::Ui, state: &mut AlertsState) {
    ui.horizontal(|ui| {
        ui.label("Filter");
        ui.add_sized(
            [260.0, 20.0],
            egui::TextEdit::singleline(&mut state.table.filter)
                .hint_text("severity / category / ticker / status"),
        );
        if ui.button("Clear").clicked() {
            state.table.filter.clear();
            state.table.clear_selection();
        }
    });
}

fn alert_matches(alert: &Alert, filter: &str) -> bool {
    any_contains_ci(
        [
            alert.severity.as_str(),
            alert.category.as_str(),
            alert.title.as_str(),
            alert.message.as_str(),
            alert.fund_ticker.as_deref().unwrap_or("-"),
            alert.status.as_str(),
            alert.source.as_str(),
            alert.created_time.as_str(),
        ],
        filter,
    )
}

fn select_alert_cell(state: &mut AlertsState, index: usize, alert: &Alert, column: AlertColumn) {
    let (display, raw) = column.payload(alert);
    state
        .table
        .select_cell(index, column.index(), column.key(), display, raw);
}

fn sync_alert_focus(alerts: &[Alert], state: &mut AlertsState) {
    let (Some(row_index), Some(column_index)) = (
        state.table.focused_row_index,
        state.table.focused_column_index,
    ) else {
        return;
    };
    let Some(alert) = alerts.get(row_index) else {
        return;
    };
    let Some(column) = AlertColumn::ALL.get(column_index).copied() else {
        return;
    };
    let (display, raw) = column.payload(alert);
    state
        .table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn visible_alert_indices(alerts: &[Alert], state: &AlertsState) -> Vec<usize> {
    let mut indices = alerts
        .iter()
        .enumerate()
        .filter(|(_, alert)| alert_matches(alert, &state.table.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_alert_indices(alerts, &mut indices, state.table.sort.as_ref());
    indices
}

fn sort_alert_indices(alerts: &[Alert], indices: &mut [usize], sort: Option<&SortSpec>) {
    let Some(sort) = sort else {
        return;
    };

    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_alerts(
                &alerts[*left],
                &alerts[*right],
                &sort.column,
            ))
            .then_with(|| alerts[*left].created_time.cmp(&alerts[*right].created_time))
    });
}

fn compare_alerts(left: &Alert, right: &Alert, column: &str) -> Ordering {
    match column {
        COL_SEVERITY => severity_rank(left.severity).cmp(&severity_rank(right.severity)),
        COL_CATEGORY => left.category.cmp(&right.category),
        COL_TITLE => left.title.cmp(&right.title),
        COL_FUND => left.fund_ticker.cmp(&right.fund_ticker),
        COL_STATUS => left.status.cmp(&right.status),
        COL_SOURCE => left.source.cmp(&right.source),
        COL_READ => left.read.cmp(&right.read),
        COL_DISMISSED => left.dismissed.cmp(&right.dismissed),
        COL_CREATED => left.created_time.cmp(&right.created_time),
        _ => Ordering::Equal,
    }
}

fn severity_rank(severity: AlertSeverity) -> u8 {
    match severity {
        AlertSeverity::Info => 0,
        AlertSeverity::Warning => 1,
        AlertSeverity::Critical => 2,
    }
}

fn alert_totals(ui: &mut egui::Ui, alerts: &[Alert]) {
    let unread = alerts.iter().filter(|alert| !alert.read).count();
    let critical = alerts
        .iter()
        .filter(|alert| alert.severity == AlertSeverity::Critical)
        .count();
    let dismissed = alerts.iter().filter(|alert| alert.dismissed).count();

    egui::Grid::new("alerts_totals_grid")
        .num_columns(2)
        .striped(true)
        .show(ui, |ui| {
            ui.label("Unread");
            ui.monospace(unread.to_string());
            ui.end_row();
            ui.label("Critical");
            ui.monospace(critical.to_string());
            ui.end_row();
            ui.label("Dismissed");
            ui.monospace(dismissed.to_string());
            ui.end_row();
        });
}

pub(crate) fn mark_alert_read(alert: &mut Alert) {
    alert.read = true;
}

pub(crate) fn dismiss_alert(alert: &mut Alert) {
    alert.read = true;
    alert.dismissed = true;
    if !alert.status.eq_ignore_ascii_case("resolved") {
        alert.status = "DISMISSED".to_owned();
    }
}

pub(crate) fn resolve_alert(alert: &mut Alert) {
    alert.read = true;
    alert.dismissed = false;
    alert.status = "RESOLVED".to_owned();
}

pub(crate) fn alert_copy_text(alert: &Alert) -> String {
    [
        alert.id.clone(),
        alert.severity.as_str().to_owned(),
        alert.category.clone(),
        alert.title.clone(),
        alert.message.clone(),
        alert.fund_ticker.clone().unwrap_or_else(|| "-".to_owned()),
        alert.status.clone(),
        alert.source.clone(),
        alert.created_time.clone(),
    ]
    .join("\t")
}

fn related_job_name(alert: &Alert) -> String {
    let category = alert.category.replace(' ', "_").to_ascii_uppercase();
    alert
        .fund_ticker
        .as_deref()
        .map(|ticker| format!("{category}_{ticker}"))
        .unwrap_or(category)
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

fn selected_ticker<'a>(selected: &SelectedInstrument, funds: &'a [Fund]) -> Option<&'a str> {
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

    fn alert(severity: AlertSeverity, created_time: &str) -> Alert {
        Alert {
            id: created_time.to_owned(),
            severity,
            category: "Prices".to_owned(),
            title: "Alert".to_owned(),
            message: "Message".to_owned(),
            fund_ticker: Some("VUSA".to_owned()),
            status: "OPEN".to_owned(),
            source: "mock".to_owned(),
            read: false,
            dismissed: false,
            created_time: created_time.to_owned(),
        }
    }

    #[test]
    fn alert_actions_update_local_state() {
        let mut alert = alert(AlertSeverity::Warning, "2026-06-20");

        mark_alert_read(&mut alert);
        assert!(alert.read);
        assert!(!alert.dismissed);

        dismiss_alert(&mut alert);
        assert!(alert.read);
        assert!(alert.dismissed);
        assert_eq!(alert.status, "DISMISSED");

        resolve_alert(&mut alert);
        assert!(alert.read);
        assert!(!alert.dismissed);
        assert_eq!(alert.status, "RESOLVED");
    }

    #[test]
    fn sorts_alerts_by_severity_desc() {
        let alerts = vec![
            alert(AlertSeverity::Info, "2026-06-18"),
            alert(AlertSeverity::Critical, "2026-06-20"),
        ];
        let mut state = AlertsState::default();
        state.table.toggle_sort(COL_SEVERITY);
        state.table.toggle_sort(COL_SEVERITY);

        assert_eq!(visible_alert_indices(&alerts, &state), vec![1, 0]);
    }

    #[test]
    fn alert_columns_cover_operational_copy_fields() {
        assert_eq!(AlertColumn::ALL.len(), 10);
        assert_eq!(ALERT_COLUMNS.len(), 11);
        assert_eq!(AlertColumn::Message.key(), "message");
        assert!(!ALERT_COLUMNS[AlertColumn::Message.index()].default_visible);
        assert_eq!(AlertColumn::Dismissed.label(), "Dismissed");
        assert_eq!(AlertColumn::Created.index(), 9);
        assert!(!ALERT_COLUMNS[0].hideable);
    }
}
