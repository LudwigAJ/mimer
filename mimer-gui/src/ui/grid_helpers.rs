use eframe::egui;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KvRow {
    pub label: String,
    pub short_value: String,
    pub full_value: String,
    pub tooltip: Option<String>,
    pub copyable: bool,
}

impl KvRow {
    pub fn new(label: impl Into<String>, value: impl Into<String>) -> Self {
        let value = value.into();
        Self {
            label: label.into(),
            short_value: value.clone(),
            full_value: value,
            tooltip: None,
            copyable: false,
        }
    }

    pub fn with_short_value(mut self, short_value: impl Into<String>) -> Self {
        self.short_value = short_value.into();
        self
    }

    pub fn with_tooltip(mut self, tooltip: impl Into<String>) -> Self {
        self.tooltip = Some(tooltip.into());
        self
    }

    pub fn copyable(mut self) -> Self {
        self.copyable = true;
        self
    }
}

pub fn kv_grid(ui: &mut egui::Ui, id: impl std::hash::Hash + std::fmt::Debug, rows: &[KvRow]) {
    let min_col_width = (ui.available_width() * 0.34).clamp(120.0, 180.0);
    egui::Grid::new(id)
        .num_columns(2)
        .min_col_width(min_col_width)
        .spacing(egui::vec2(10.0, 4.0))
        .striped(true)
        .show(ui, |ui| {
            for row in rows {
                ui.label(egui::RichText::new(&row.label).weak());
                let display_value = truncate_end(&row.short_value, 72);
                let mut response = ui
                    .add(
                        egui::Label::new(egui::RichText::new(display_value).monospace()).truncate(),
                    )
                    .on_hover_text(row.tooltip.as_deref().unwrap_or(&row.full_value));
                if row.copyable {
                    response = response.on_hover_cursor(egui::CursorIcon::Copy);
                    response.context_menu(|ui| {
                        if ui.button("Copy").clicked() {
                            ui.copy_text(row.full_value.clone());
                            ui.close();
                        }
                    });
                }
                let _ = response;
                ui.end_row();
            }
        });
}

pub fn truncate_end(value: &str, max_chars: usize) -> String {
    if max_chars == 0 {
        return String::new();
    }
    let mut chars = value.chars();
    let prefix = chars.by_ref().take(max_chars).collect::<String>();
    if chars.next().is_some() {
        if max_chars <= 3 {
            ".".repeat(max_chars)
        } else {
            format!(
                "{}...",
                prefix.chars().take(max_chars - 3).collect::<String>()
            )
        }
    } else {
        prefix
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncates_by_chars_not_bytes() {
        assert_eq!(truncate_end("Vanguard", 5), "Va...");
        assert_eq!(truncate_end("VUSA", 8), "VUSA");
        assert_eq!(truncate_end("éééé", 3), "...");
    }
}
