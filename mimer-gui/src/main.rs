mod api;
mod app;
mod app_info;
mod app_model;
mod charts;
mod command;
mod compute;
mod debounce;
mod domain;
mod filter;
mod format;
mod inspector;
mod mock_data;
mod navigation;
mod pages;
mod source;
mod storage;
mod table_state;
mod timeseries;
mod ui;
mod ui_state;

use eframe::egui;

fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(app_info::window_title())
            .with_inner_size([1440.0, 900.0])
            .with_min_inner_size([980.0, 620.0]),
        ..Default::default()
    };

    eframe::run_native(
        &app_info::window_title(),
        options,
        Box::new(|cc| Ok(Box::new(app::MimerApp::new(cc)))),
    )
}
