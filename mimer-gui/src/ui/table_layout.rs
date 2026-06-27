use crate::table_state::{ColumnDescriptor, TableId, TableLayoutRegistry};
use eframe::egui;
use egui_extras::Column;

pub const COLUMN_WIDTH_STEP: f32 = 16.0;

pub fn managed_column(
    layouts: &mut TableLayoutRegistry,
    table_id: TableId,
    descriptors: &[ColumnDescriptor],
    descriptor: ColumnDescriptor,
) -> Column {
    if !layouts.is_visible(table_id, descriptor, descriptors) {
        return Column::initial(0.0)
            .at_least(0.0)
            .at_most(0.0)
            .clip(true)
            .resizable(false);
    }

    let width = layouts.width(table_id, descriptor, descriptors);
    Column::initial(width)
        .at_least(descriptor.min_width)
        .at_most(descriptor.max_width)
        .clip(descriptor.clip)
        .resizable(false)
}

pub fn managed_table_revision(
    layouts: &mut TableLayoutRegistry,
    table_id: TableId,
    descriptors: &[ColumnDescriptor],
) -> (u64, u64) {
    layouts.table_revision(table_id, descriptors)
}

pub fn table_layout_controls(
    ui: &mut egui::Ui,
    layouts: &mut TableLayoutRegistry,
    table_id: TableId,
    descriptors: &[ColumnDescriptor],
    focused_column_key: Option<&str>,
    visible_row: Option<(&str, String)>,
) -> bool {
    layouts.ensure(table_id, descriptors);
    let mut changed = false;

    ui.horizontal_wrapped(|ui| {
        ui.menu_button("Columns", |ui| {
            ui.set_min_width(210.0);
            for descriptor in descriptors {
                let mut visible = layouts.is_visible(table_id, *descriptor, descriptors);
                let response = ui.add_enabled(
                    descriptor.hideable,
                    egui::Checkbox::new(&mut visible, descriptor.label),
                );
                if response.changed() {
                    changed |= layouts.set_visible(
                        table_id,
                        descriptors,
                        descriptor.key,
                        visible,
                    );
                }
            }

            ui.separator();
            if let Some(key) = focused_column_key
                && let Some(descriptor) = descriptors.iter().find(|column| column.key == key)
            {
                ui.label(egui::RichText::new(format!("Focused: {}", descriptor.label)).weak());
                ui.horizontal(|ui| {
                    if ui.button("Narrower").clicked() {
                        changed |= layouts.adjust_width(
                            table_id,
                            descriptors,
                            key,
                            -COLUMN_WIDTH_STEP,
                        );
                    }
                    if ui.button("Wider").clicked() {
                        changed |= layouts.adjust_width(
                            table_id,
                            descriptors,
                            key,
                            COLUMN_WIDTH_STEP,
                        );
                    }
                });
                if descriptor.hideable && ui.button("Hide focused column").clicked() {
                    changed |= layouts.set_visible(table_id, descriptors, key, false);
                }
                ui.separator();
            }

            if ui.button("Show all columns").clicked() {
                changed |= layouts.show_all(table_id, descriptors);
            }
            if ui.button("Reset columns").clicked() {
                layouts.reset(table_id, descriptors);
                changed = true;
            }
        })
        .response
        .on_hover_text(
            "Choose visible columns and adjust persistent focused-column width. Column order follows the stable descriptor order.",
        );

        if let Some((label, text)) = visible_row
            && ui
                .button("Copy visible row")
                .on_hover_text("Copy only columns currently shown in this table.")
                .clicked()
        {
            ui.copy_text(text);
            ui.label(egui::RichText::new(format!("COPIED {label}")).weak());
        }
    });

    changed
}
