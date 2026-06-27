use crate::domain::{ExposureBreakdown, ExposureSlice};
use crate::pages::{format_pct, header_cell, page_heading};
use crate::table_state::{ColumnDescriptor, TableId, TableLayoutRegistry, TableState};
use crate::ui::style;
use crate::ui::table_layout::{managed_column, managed_table_revision, table_layout_controls};
use eframe::egui;
use egui_extras::TableBuilder;

const BREAKDOWN_COLUMNS: [ColumnDescriptor; 2] = [
    ColumnDescriptor::new("bucket", "Bucket", 240.0, 160.0, 480.0)
        .required()
        .clipped(),
    ColumnDescriptor::new("weight", "Weight", 90.0, 70.0, 150.0).required(),
];

const DIAGNOSTIC_COLUMNS: [ColumnDescriptor; 4] = [
    ColumnDescriptor::new("input", "Input", 160.0, 120.0, 260.0).required(),
    ColumnDescriptor::new("source", "Source", 120.0, 90.0, 220.0).required(),
    ColumnDescriptor::new("status", "Status", 100.0, 78.0, 160.0).required(),
    ColumnDescriptor::new("note", "Note", 260.0, 180.0, 520.0)
        .hidden_by_default()
        .clipped(),
];

const DIAGNOSTIC_ROWS: [(&str, &str, &str, &str); 3] = [
    (
        "Holdings",
        "issuer",
        "SEED",
        "factsheet look-through snapshot",
    ),
    (
        "Prices",
        "stooq",
        "STALE",
        "VHYL close older than threshold",
    ),
    ("FX", "mock", "FRESH", "mock GBP conversion basis"),
];

#[derive(Clone, Debug)]
pub struct ExposureState {
    pub countries_table: TableState,
    pub sectors_table: TableState,
    pub currencies_table: TableState,
    pub top_holdings_table: TableState,
    pub diagnostics_table: TableState,
    pub active_table: TableId,
}

impl Default for ExposureState {
    fn default() -> Self {
        Self {
            countries_table: TableState::new(TableId::ExposureCountries),
            sectors_table: TableState::new(TableId::ExposureSectors),
            currencies_table: TableState::new(TableId::ExposureCurrencies),
            top_holdings_table: TableState::new(TableId::ExposureTopHoldings),
            diagnostics_table: TableState::new(TableId::ExposureDiagnostics),
            active_table: TableId::ExposureCountries,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ExposureAction {
    PinInspector,
    Feedback(String),
}

pub fn render(
    ui: &mut egui::Ui,
    exposure: &ExposureBreakdown,
    state: &mut ExposureState,
    layouts: &mut TableLayoutRegistry,
) -> Option<ExposureAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Exposure");
            breakdown_table(
                ui,
                TableId::ExposureCountries,
                "Country exposure",
                &exposure.countries,
                &mut state.countries_table,
                &mut state.active_table,
                layouts,
                &mut action,
            );
            ui.add_space(8.0);
            breakdown_table(
                ui,
                TableId::ExposureSectors,
                "Sector exposure",
                &exposure.sectors,
                &mut state.sectors_table,
                &mut state.active_table,
                layouts,
                &mut action,
            );
            ui.add_space(8.0);
            breakdown_table(
                ui,
                TableId::ExposureCurrencies,
                "Currency exposure",
                &exposure.currencies,
                &mut state.currencies_table,
                &mut state.active_table,
                layouts,
                &mut action,
            );
            ui.add_space(8.0);
            breakdown_table(
                ui,
                TableId::ExposureTopHoldings,
                "Top underlying holdings",
                &exposure.top_holdings,
                &mut state.top_holdings_table,
                &mut state.active_table,
                layouts,
                &mut action,
            );
            ui.add_space(8.0);
            diagnostics_table(ui, state, layouts, &mut action);
        });
    action
}

#[allow(clippy::too_many_arguments)]
fn breakdown_table(
    ui: &mut egui::Ui,
    table_id: TableId,
    title: &str,
    rows: &[ExposureSlice],
    table_state: &mut TableState,
    active_table: &mut TableId,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<ExposureAction>,
) {
    ui.label(egui::RichText::new(title).strong());
    let selected = table_state
        .focused_row_index
        .or(table_state.selected_index())
        .and_then(|index| rows.get(index));
    let visible_row = selected.map(|item| {
        (
            item.label.as_str(),
            layouts.visible_row_text(
                table_id,
                &BREAKDOWN_COLUMNS,
                &[
                    ("bucket", item.label.clone()),
                    ("weight", format_pct(item.value_pct)),
                ],
            ),
        )
    });
    if table_layout_controls(
        ui,
        layouts,
        table_id,
        &BREAKDOWN_COLUMNS,
        table_state.focused_column_index.map(|index| {
            BREAKDOWN_COLUMNS
                .get(index)
                .map_or("bucket", |column| column.key)
        }),
        visible_row,
    ) {
        table_state.clear_focus();
    }

    let revision = managed_table_revision(layouts, table_id, &BREAKDOWN_COLUMNS);
    let mut table = TableBuilder::new(ui)
        .id_salt((table_id.key(), revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(170.0);
    for descriptor in BREAKDOWN_COLUMNS {
        table = table.column(managed_column(
            layouts,
            table_id,
            &BREAKDOWN_COLUMNS,
            descriptor,
        ));
    }
    table
        .header(18.0, |mut header| {
            header.col(|ui| header_cell(ui, "Bucket"));
            header.col(|ui| header_cell(ui, "Weight"));
        })
        .body(|mut body| {
            for (index, item) in rows.iter().enumerate() {
                body.row(20.0, |mut row| {
                    row.set_overline(table_state.is_focused_row(index));
                    row.set_selected(
                        *active_table == table_id && table_state.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(ui, table_state.is_focused_cell(index, 0));
                        let response = ui
                            .selectable_label(table_state.selection.is_selected(index), &item.label)
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            table_state.select_cell(
                                index,
                                0,
                                "bucket",
                                item.label.clone(),
                                item.label.clone(),
                            );
                            *active_table = table_id;
                        }
                        response.context_menu(|ui| {
                            if ui.button("Copy visible row").clicked() {
                                ui.copy_text(layouts.visible_row_text(
                                    table_id,
                                    &BREAKDOWN_COLUMNS,
                                    &[
                                        ("bucket", item.label.clone()),
                                        ("weight", format_pct(item.value_pct)),
                                    ],
                                ));
                                *action = Some(ExposureAction::Feedback(
                                    "COPIED: exposure row".to_owned(),
                                ));
                                ui.close();
                            }
                            if ui.button("Pin Inspector").clicked() {
                                table_state.select(index);
                                *active_table = table_id;
                                *action = Some(ExposureAction::PinInspector);
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(ui, table_state.is_focused_cell(index, 1));
                        ui.label(format_pct(item.value_pct));
                    });
                });
            }
        });
}

fn diagnostics_table(
    ui: &mut egui::Ui,
    state: &mut ExposureState,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<ExposureAction>,
) {
    ui.label(egui::RichText::new("Source/staleness diagnostics").strong());
    let table_id = TableId::ExposureDiagnostics;
    let selected = state
        .diagnostics_table
        .focused_row_index
        .or(state.diagnostics_table.selected_index())
        .and_then(|index| DIAGNOSTIC_ROWS.get(index));
    let visible_row = selected.map(|(input, source, status, note)| {
        (
            *input,
            layouts.visible_row_text(
                table_id,
                &DIAGNOSTIC_COLUMNS,
                &[
                    ("input", (*input).to_owned()),
                    ("source", (*source).to_owned()),
                    ("status", (*status).to_owned()),
                    ("note", (*note).to_owned()),
                ],
            ),
        )
    });
    if table_layout_controls(
        ui,
        layouts,
        table_id,
        &DIAGNOSTIC_COLUMNS,
        state
            .diagnostics_table
            .focused_column_index
            .and_then(|index| DIAGNOSTIC_COLUMNS.get(index))
            .map(|column| column.key),
        visible_row,
    ) {
        state.diagnostics_table.clear_focus();
    }

    let revision = managed_table_revision(layouts, table_id, &DIAGNOSTIC_COLUMNS);
    let mut table = TableBuilder::new(ui)
        .id_salt(("exposure_diagnostics_table", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(120.0);
    for descriptor in DIAGNOSTIC_COLUMNS {
        table = table.column(managed_column(
            layouts,
            table_id,
            &DIAGNOSTIC_COLUMNS,
            descriptor,
        ));
    }
    table
        .header(18.0, |mut header| {
            for label in ["Input", "Source", "Status", "Note"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for (index, (input, source, status, note)) in DIAGNOSTIC_ROWS.iter().enumerate() {
                body.row(20.0, |mut row| {
                    row.set_overline(state.diagnostics_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == table_id
                            && state.diagnostics_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.diagnostics_table.is_focused_cell(index, 0),
                        );
                        let response = ui.selectable_label(
                            state.diagnostics_table.selection.is_selected(index),
                            *input,
                        );
                        if response.clicked() {
                            state
                                .diagnostics_table
                                .select_cell(index, 0, "input", *input, *input);
                            state.active_table = table_id;
                        }
                        response.context_menu(|ui| {
                            if ui.button("Pin Inspector").clicked() {
                                state.diagnostics_table.select(index);
                                state.active_table = table_id;
                                *action = Some(ExposureAction::PinInspector);
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.diagnostics_table.is_focused_cell(index, 1),
                        );
                        style::source_badge(ui, source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.diagnostics_table.is_focused_cell(index, 2),
                        );
                        style::status_badge(ui, status);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.diagnostics_table.is_focused_cell(index, 3),
                        );
                        ui.label(*note);
                    });
                });
            }
        });
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    exposure: &ExposureBreakdown,
    state: &mut ExposureState,
    layouts: &mut TableLayoutRegistry,
) {
    if ctx.text_edit_focused() {
        return;
    }
    let (table, row_count, descriptors) = match state.active_table {
        TableId::ExposureSectors => (
            &mut state.sectors_table,
            exposure.sectors.len(),
            BREAKDOWN_COLUMNS.as_slice(),
        ),
        TableId::ExposureCurrencies => (
            &mut state.currencies_table,
            exposure.currencies.len(),
            BREAKDOWN_COLUMNS.as_slice(),
        ),
        TableId::ExposureTopHoldings => (
            &mut state.top_holdings_table,
            exposure.top_holdings.len(),
            BREAKDOWN_COLUMNS.as_slice(),
        ),
        TableId::ExposureDiagnostics => (
            &mut state.diagnostics_table,
            DIAGNOSTIC_ROWS.len(),
            DIAGNOSTIC_COLUMNS.as_slice(),
        ),
        _ => (
            &mut state.countries_table,
            exposure.countries.len(),
            BREAKDOWN_COLUMNS.as_slice(),
        ),
    };
    let rows = (0..row_count).collect::<Vec<_>>();
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        table.move_focus_row(&rows, -1, Some(0));
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        table.move_focus_row(&rows, 1, Some(0));
    }
    let columns = layouts.visible_indices(state.active_table, descriptors);
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        table.move_focus_visible_column(&columns, -1);
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        table.move_focus_visible_column(&columns, 1);
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter))
        && table.selected_index().is_none()
    {
        table.move_focus_row(&rows, 1, columns.first().copied());
    }
    sync_focus(exposure, state);
}

fn sync_focus(exposure: &ExposureBreakdown, state: &mut ExposureState) {
    match state.active_table {
        TableId::ExposureSectors => {
            sync_breakdown_focus(&mut state.sectors_table, &exposure.sectors);
        }
        TableId::ExposureCurrencies => {
            sync_breakdown_focus(&mut state.currencies_table, &exposure.currencies);
        }
        TableId::ExposureTopHoldings => {
            sync_breakdown_focus(&mut state.top_holdings_table, &exposure.top_holdings);
        }
        TableId::ExposureDiagnostics => {
            let Some(row_index) = state.diagnostics_table.focused_row_index else {
                return;
            };
            let Some(column_index) = state.diagnostics_table.focused_column_index else {
                return;
            };
            let Some((input, source, status, note)) = DIAGNOSTIC_ROWS.get(row_index) else {
                return;
            };
            let values = [*input, *source, *status, *note];
            let Some(value) = values.get(column_index) else {
                return;
            };
            let key = DIAGNOSTIC_COLUMNS[column_index].key;
            state
                .diagnostics_table
                .set_focused_cell_payload(key, *value, *value);
        }
        _ => {
            sync_breakdown_focus(&mut state.countries_table, &exposure.countries);
        }
    }
}

fn sync_breakdown_focus(table: &mut TableState, rows: &[ExposureSlice]) {
    let Some(row_index) = table.focused_row_index else {
        return;
    };
    let Some(row) = rows.get(row_index) else {
        return;
    };
    let Some(column_index) = table.focused_column_index else {
        return;
    };
    let (key, value) = match column_index {
        0 => ("bucket", row.label.clone()),
        1 => ("weight", format_pct(row.value_pct)),
        _ => return,
    };
    table.set_focused_cell_payload(key, value.clone(), value);
}

pub fn selected_copy_payload(
    exposure: &ExposureBreakdown,
    state: &ExposureState,
) -> Option<(String, String)> {
    let table = match state.active_table {
        TableId::ExposureSectors => &state.sectors_table,
        TableId::ExposureCurrencies => &state.currencies_table,
        TableId::ExposureTopHoldings => &state.top_holdings_table,
        TableId::ExposureDiagnostics => &state.diagnostics_table,
        _ => &state.countries_table,
    };
    if let Some(cell) = table.selected_cell.as_ref() {
        return Some((cell.display_value.clone(), cell.raw_value.clone()));
    }
    let index = table.focused_row_index.or(table.selected_index())?;
    if state.active_table == TableId::ExposureDiagnostics {
        let row = DIAGNOSTIC_ROWS.get(index)?;
        return Some((row.0.to_owned(), [row.0, row.1, row.2, row.3].join("\t")));
    }
    let row = match state.active_table {
        TableId::ExposureSectors => exposure.sectors.get(index),
        TableId::ExposureCurrencies => exposure.currencies.get(index),
        TableId::ExposureTopHoldings => exposure.top_holdings.get(index),
        _ => exposure.countries.get(index),
    }?;
    Some((
        row.label.clone(),
        format!("{}\t{}", row.label, format_pct(row.value_pct)),
    ))
}

pub fn inspector_details(
    exposure: &ExposureBreakdown,
    state: &ExposureState,
) -> Option<(String, String)> {
    let table = match state.active_table {
        TableId::ExposureSectors => &state.sectors_table,
        TableId::ExposureCurrencies => &state.currencies_table,
        TableId::ExposureTopHoldings => &state.top_holdings_table,
        TableId::ExposureDiagnostics => &state.diagnostics_table,
        _ => &state.countries_table,
    };
    let index = table.focused_row_index.or(table.selected_index())?;
    if state.active_table == TableId::ExposureDiagnostics {
        let row = DIAGNOSTIC_ROWS.get(index)?;
        return Some((
            row.0.to_owned(),
            format!("Source: {}\nStatus: {}\n{}", row.1, row.2, row.3),
        ));
    }
    let row = match state.active_table {
        TableId::ExposureSectors => exposure.sectors.get(index),
        TableId::ExposureCurrencies => exposure.currencies.get(index),
        TableId::ExposureTopHoldings => exposure.top_holdings.get(index),
        _ => exposure.countries.get(index),
    }?;
    Some((
        row.label.clone(),
        format!("Weight: {}", format_pct(row.value_pct)),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exposure_tables_have_stable_focus_columns() {
        assert_eq!(TableId::ExposureCountries.key(), "exposure.countries");
        assert_eq!(BREAKDOWN_COLUMNS[0].key, "bucket");
        assert_eq!(DIAGNOSTIC_COLUMNS[3].key, "note");
        assert!(!DIAGNOSTIC_COLUMNS[3].default_visible);
    }
}
