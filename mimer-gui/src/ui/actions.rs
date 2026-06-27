use eframe::egui::{self, Response};

pub fn action_button(ui: &mut egui::Ui, label: &str, tooltip: &str) -> Response {
    ui.small_button(label)
        .on_hover_text(tooltip)
        .on_hover_cursor(egui::CursorIcon::PointingHand)
}

pub fn action_button_enabled(
    ui: &mut egui::Ui,
    enabled: bool,
    label: &str,
    tooltip: &str,
) -> Response {
    ui.add_enabled(enabled, egui::Button::new(label).small())
        .on_hover_text(tooltip)
}
