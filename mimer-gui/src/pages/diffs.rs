use crate::compute::diff::{DiffStatus, EntityDiff, mock_entity_diffs};
use crate::pages::{header_cell, page_heading};
use eframe::egui::{self, Color32};
use egui_extras::{Column, TableBuilder};

pub fn render(ui: &mut egui::Ui) {
    let diffs = mock_entity_diffs();

    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Changes");
            ui.label("Mock document/entity change review for factsheets, holdings, and derived values. Use Spreads for VUSA - JEPG style asset relationships.");
            ui.add_space(6.0);

            summary(ui, &diffs);
            ui.add_space(8.0);
            diff_table(ui, &diffs);
        });
}

fn summary(ui: &mut egui::Ui, diffs: &[EntityDiff]) {
    let changed = diffs
        .iter()
        .flat_map(|diff| diff.fields.iter())
        .filter(|field| field.status == DiffStatus::Changed)
        .count();
    let added = diffs
        .iter()
        .flat_map(|diff| diff.fields.iter())
        .filter(|field| field.status == DiffStatus::Added)
        .count();
    let removed = diffs
        .iter()
        .flat_map(|diff| diff.fields.iter())
        .filter(|field| field.status == DiffStatus::Removed)
        .count();

    ui.horizontal(|ui| {
        ui.label("Entities");
        ui.monospace(diffs.len().to_string());
        ui.separator();
        ui.label("Changed");
        ui.monospace(changed.to_string());
        ui.separator();
        ui.label("Added");
        ui.monospace(added.to_string());
        ui.separator();
        ui.label("Removed");
        ui.monospace(removed.to_string());
    });
}

fn diff_table(ui: &mut egui::Ui, diffs: &[EntityDiff]) {
    TableBuilder::new(ui)
        .id_salt("diffs_entity_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(560.0)
        .column(Column::initial(100.0).at_least(80.0))
        .column(Column::initial(180.0).at_least(130.0).clip(true))
        .column(Column::initial(120.0).at_least(90.0))
        .column(Column::initial(120.0).at_least(90.0))
        .column(Column::initial(160.0).at_least(110.0).clip(true))
        .column(Column::initial(220.0).at_least(150.0).clip(true))
        .column(Column::initial(220.0).at_least(150.0).clip(true))
        .column(Column::remainder().at_least(100.0))
        .header(18.0, |mut header| {
            for label in [
                "Entity",
                "Name",
                "Left",
                "Right",
                "Field",
                "Left value",
                "Right value",
                "Status",
            ] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for diff in diffs {
                for field in &diff.fields {
                    body.row(20.0, |mut row| {
                        row.col(|ui| {
                            ui.label(&diff.entity_type);
                        });
                        row.col(|ui| {
                            ui.label(&diff.entity_name);
                        });
                        row.col(|ui| {
                            ui.label(&diff.left_label);
                        });
                        row.col(|ui| {
                            ui.label(&diff.right_label);
                        });
                        row.col(|ui| {
                            ui.label(&field.field_name);
                        });
                        row.col(|ui| {
                            ui.label(field.left_value.as_deref().unwrap_or("-"));
                        });
                        row.col(|ui| {
                            ui.label(field.right_value.as_deref().unwrap_or("-"));
                        });
                        row.col(|ui| {
                            ui.colored_label(status_color(field.status), field.status.as_str());
                        });
                    });
                }
            }
        });
}

fn status_color(status: DiffStatus) -> Color32 {
    match status {
        DiffStatus::Unchanged => Color32::from_rgb(150, 150, 150),
        DiffStatus::Added => Color32::from_rgb(120, 190, 120),
        DiffStatus::Removed => Color32::from_rgb(230, 110, 95),
        DiffStatus::Changed => Color32::from_rgb(220, 176, 82),
    }
}
