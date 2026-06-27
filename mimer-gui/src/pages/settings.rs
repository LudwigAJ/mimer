use crate::api::client::normalize_backend_base_url;
use crate::api::types::{ApiConfig, ApiRuntimeStatus, AuthMode, DataMode};
use crate::domain::Workspace;
use crate::source::SourcePolicy;
use crate::storage::StorageSummary;
use crate::ui_state::LayoutState;
use eframe::egui;

pub const ZOOM_MIN: f32 = 0.7;
pub const ZOOM_MAX: f32 = 1.6;
pub const ZOOM_STEP: f32 = 0.1;
pub const ZOOM_DEFAULT: f32 = 1.0;

#[derive(Clone, Debug)]
pub struct SettingsState {
    pub api_config: ApiConfig,
    pub pending_api_config: ApiConfig,
    pub data_mode: DataMode,
    pub auto_refresh_data_operations: bool,
    pub pending_error: Option<String>,
    pub base_currency: String,
    pub pending_base_currency: String,
    pub refresh_interval_minutes: u32,
    pub pending_refresh_interval_minutes: u32,
    pub density: DensityPreference,
    pub theme: ThemePreference,
    pub source_policy: SourcePolicy,
    pub zoom_factor: f32,
}

impl Default for SettingsState {
    fn default() -> Self {
        let api_config = ApiConfig::default();
        Self {
            api_config: api_config.clone(),
            pending_api_config: api_config,
            data_mode: DataMode::Mock,
            auto_refresh_data_operations: false,
            pending_error: None,
            base_currency: "GBP".to_owned(),
            pending_base_currency: "GBP".to_owned(),
            refresh_interval_minutes: 15,
            pending_refresh_interval_minutes: 15,
            density: DensityPreference::Compact,
            theme: ThemePreference::System,
            source_policy: SourcePolicy::Canonical,
            zoom_factor: ZOOM_DEFAULT,
        }
    }
}

impl SettingsState {
    pub fn apply_pending(&mut self) -> Result<(), String> {
        self.pending_api_config.base_url =
            normalize_backend_base_url(&self.pending_api_config.base_url)?;
        self.api_config = self.pending_api_config.clone();
        self.base_currency.clone_from(&self.pending_base_currency);
        self.refresh_interval_minutes = self.pending_refresh_interval_minutes;
        self.pending_error = None;
        Ok(())
    }

    pub fn revert_pending(&mut self) {
        self.pending_api_config = self.api_config.clone();
        self.pending_base_currency.clone_from(&self.base_currency);
        self.pending_refresh_interval_minutes = self.refresh_interval_minutes;
        self.pending_error = None;
    }

    pub fn has_pending_changes(&self) -> bool {
        self.api_config.base_url != self.pending_api_config.base_url
            || self.api_config.auth_mode != self.pending_api_config.auth_mode
            || self.api_config.timeout_ms != self.pending_api_config.timeout_ms
            || self.api_config.workspace_header_value
                != self.pending_api_config.workspace_header_value
            || self.base_currency != self.pending_base_currency
            || self.refresh_interval_minutes != self.pending_refresh_interval_minutes
    }

    pub fn set_zoom_factor(&mut self, zoom_factor: f32) {
        self.zoom_factor = clamp_zoom_factor(zoom_factor);
    }

    pub fn zoom_in(&mut self) {
        self.set_zoom_factor(self.zoom_factor + ZOOM_STEP);
    }

    pub fn zoom_out(&mut self) {
        self.set_zoom_factor(self.zoom_factor - ZOOM_STEP);
    }

    pub fn reset_zoom(&mut self) {
        self.zoom_factor = ZOOM_DEFAULT;
    }

    pub fn zoom_percent(&self) -> u32 {
        zoom_percent(self.zoom_factor)
    }
}

pub fn clamp_zoom_factor(zoom_factor: f32) -> f32 {
    let rounded = (zoom_factor * 100.0).round() / 100.0;
    rounded.clamp(ZOOM_MIN, ZOOM_MAX)
}

pub fn zoom_percent(zoom_factor: f32) -> u32 {
    (clamp_zoom_factor(zoom_factor) * 100.0).round() as u32
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DensityPreference {
    Compact,
    Comfortable,
}

impl DensityPreference {
    const ALL: [Self; 2] = [Self::Compact, Self::Comfortable];

    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Compact => "compact",
            Self::Comfortable => "comfortable",
        }
    }

    pub(crate) fn from_str(value: &str) -> Option<Self> {
        match value {
            "compact" => Some(Self::Compact),
            "comfortable" => Some(Self::Comfortable),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ThemePreference {
    System,
    Light,
    Dark,
}

impl ThemePreference {
    const ALL: [Self; 3] = [Self::System, Self::Light, Self::Dark];

    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::Light => "light",
            Self::Dark => "dark",
        }
    }

    pub(crate) fn from_str(value: &str) -> Option<Self> {
        match value {
            "system" => Some(Self::System),
            "light" => Some(Self::Light),
            "dark" => Some(Self::Dark),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SettingsAction {
    ThemeChanged(ThemePreference),
    DensityChanged(DensityPreference),
    SourcePolicyChanged(SourcePolicy),
    ZoomChanged(u32),
    StoragePathCopied(String),
    DataModeChanged(DataMode),
    AutoRefreshChanged(bool),
    RefreshDataOperations,
    TestConnection,
    PendingApplied,
    PendingApplyFailed(String),
    PendingReverted,
}

#[allow(clippy::too_many_arguments)]
pub fn render(
    ui: &mut egui::Ui,
    state: &mut SettingsState,
    layout: &mut LayoutState,
    workspaces: &[Workspace],
    selected_workspace_id: &mut String,
    storage_summary: Option<&StorageSummary>,
    api_runtime: &ApiRuntimeStatus,
    api_busy: bool,
) -> Option<SettingsAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let subtitle = "Theme, density, source policy, data mode, and auto-refresh apply immediately; connection fields remain pending until Apply.";
            crate::ui::style::page_header(
                ui,
                "Settings",
                Some("Application"),
                Some(subtitle),
                |ui| {
                    if state.data_mode == DataMode::Mock {
                        crate::ui::style::mock_badge(ui);
                    } else {
                        crate::ui::style::status_badge(ui, "API");
                    }
                    crate::ui::style::status_badge(ui, api_runtime.connection.as_str());
                },
            );
            ui.add_space(6.0);

            let target_width = (ui.available_width() * 0.72).clamp(620.0, 980.0);
            ui.set_min_width(target_width);
            egui::Grid::new("settings_grid")
                .num_columns(2)
                .min_col_width(168.0)
                .striped(true)
                .show(ui, |ui| {
                    ui.label("Selected workspace");
                    egui::ComboBox::from_id_salt("settings_workspace_select")
                        .selected_text(workspace_label(workspaces, selected_workspace_id))
                        .show_ui(ui, |ui| {
                            for workspace in workspaces {
                                ui.selectable_value(
                                    selected_workspace_id,
                                    workspace.id.clone(),
                                    workspace.name.as_str(),
                                );
                            }
                        });
                    ui.end_row();

                    ui.label("Base currency");
                    ui.add_sized(
                        [220.0, 20.0],
                        egui::TextEdit::singleline(&mut state.pending_base_currency),
                    )
                    .on_hover_text("Pending workspace default. Apply saves the pending value.");
                    ui.end_row();

                    ui.label("Data mode");
                    let previous_mode = state.data_mode;
                    egui::ComboBox::from_id_salt("settings_data_mode_select")
                        .selected_text(state.data_mode.as_str())
                        .show_ui(ui, |ui| {
                            for mode in DataMode::ALL {
                                ui.selectable_value(&mut state.data_mode, mode, mode.as_str());
                            }
                        });
                    if state.data_mode != previous_mode {
                        action = Some(SettingsAction::DataModeChanged(state.data_mode));
                    }
                    ui.end_row();

                    ui.label("API base URL");
                    ui.add_sized(
                        [360.0, 20.0],
                        egui::TextEdit::singleline(&mut state.pending_api_config.base_url),
                    )
                    .on_hover_text("REST base URL. Credentials, query strings, and fragments are rejected.");
                    ui.end_row();

                    ui.label("API timeout (ms)");
                    ui.add(
                        egui::DragValue::new(&mut state.pending_api_config.timeout_ms)
                            .range(100..=120_000)
                            .speed(100),
                    )
                    .on_hover_text("Per-request timeout. API calls run only on background workers.");
                    ui.end_row();

                    ui.label("Auth mode");
                    egui::ComboBox::from_id_salt("settings_auth_mode_select")
                        .selected_text(state.pending_api_config.auth_mode.as_str())
                        .show_ui(ui, |ui| {
                            for auth_mode in AuthMode::ALL {
                                ui.selectable_value(
                                    &mut state.pending_api_config.auth_mode,
                                    auth_mode,
                                    auth_mode.as_str(),
                                );
                            }
                        });
                    ui.end_row();

                    ui.label("Workspace ID/header value");
                    ui.add_sized(
                        [280.0, 20.0],
                        egui::TextEdit::singleline(
                            &mut state.pending_api_config.workspace_header_value,
                        ),
                    )
                    .on_hover_text("Mock API boundary setting. Apply saves it locally.");
                    ui.end_row();

                    ui.label("Refresh interval (minutes)");
                    ui.add(egui::DragValue::new(&mut state.pending_refresh_interval_minutes).range(1..=1440));
                    ui.end_row();

                    ui.label("Auto-refresh Data Operations");
                    let previous_auto_refresh = state.auto_refresh_data_operations;
                    ui.checkbox(&mut state.auto_refresh_data_operations, "")
                        .on_hover_text(
                            "When API mode is active, refresh Data Operations when the page is opened.",
                        );
                    if state.auto_refresh_data_operations != previous_auto_refresh {
                        action = Some(SettingsAction::AutoRefreshChanged(
                            state.auto_refresh_data_operations,
                        ));
                    }
                    ui.end_row();

                    ui.label("Theme preference");
                    let previous_theme = state.theme;
                    egui::ComboBox::from_id_salt("settings_theme_select")
                        .selected_text(state.theme.as_str())
                        .show_ui(ui, |ui| {
                            for theme in ThemePreference::ALL {
                                ui.selectable_value(&mut state.theme, theme, theme.as_str());
                            }
                        });
                    if state.theme != previous_theme {
                        action = Some(SettingsAction::ThemeChanged(state.theme));
                    }
                    ui.end_row();

                    ui.label("Density preference");
                    let previous_density = state.density;
                    egui::ComboBox::from_id_salt("settings_density_select")
                        .selected_text(state.density.as_str())
                        .show_ui(ui, |ui| {
                            for density in DensityPreference::ALL {
                                ui.selectable_value(&mut state.density, density, density.as_str());
                            }
                        });
                    if state.density != previous_density {
                        action = Some(SettingsAction::DensityChanged(state.density));
                    }
                    ui.end_row();

                    ui.label("Default source policy");
                    let previous_policy = state.source_policy.clone();
                    egui::ComboBox::from_id_salt("settings_source_policy_select")
                        .selected_text(state.source_policy.label())
                        .show_ui(ui, |ui| {
                            for policy in SourcePolicy::PRESETS {
                                ui.selectable_value(
                                    &mut state.source_policy,
                                    policy.clone(),
                                    policy.label(),
                                );
                            }
                        })
                        .response
                        .on_hover_text(
                            "Default source preference. Views fall back visibly when a selected source is unavailable.",
                        );
                    if state.source_policy != previous_policy {
                        action = Some(SettingsAction::SourcePolicyChanged(
                            state.source_policy.clone(),
                        ));
                    }
                    ui.end_row();

                    ui.label("Application zoom");
                    ui.horizontal_wrapped(|ui| {
                        if ui.small_button("-").on_hover_text("Zoom out (Ctrl/Cmd+-)").clicked() {
                            state.zoom_out();
                            action = Some(SettingsAction::ZoomChanged(state.zoom_percent()));
                        }
                        ui.monospace(format!("{}%", state.zoom_percent()))
                            .on_hover_text("Global egui zoom factor; persisted with preferences.");
                        if ui.small_button("+").on_hover_text("Zoom in (Ctrl/Cmd++)").clicked() {
                            state.zoom_in();
                            action = Some(SettingsAction::ZoomChanged(state.zoom_percent()));
                        }
                        if ui
                            .small_button("100%")
                            .on_hover_text("Reset zoom (Ctrl/Cmd+0)")
                            .clicked()
                        {
                            state.reset_zoom();
                            action = Some(SettingsAction::ZoomChanged(state.zoom_percent()));
                        }
                    });
                    ui.end_row();

                    ui.label("Show left navigation");
                    ui.checkbox(&mut layout.show_left_navigation, "");
                    ui.end_row();

                    ui.label("Show inspector");
                    ui.checkbox(&mut layout.show_inspector, "");
                    ui.end_row();

                    ui.label("Show context strip");
                    ui.checkbox(&mut layout.show_context_strip, "");
                    ui.end_row();

                    ui.label("Show status bar");
                    ui.checkbox(&mut layout.show_status_bar, "");
                    ui.end_row();
                });

            ui.add_space(8.0);
            ui.horizontal_wrapped(|ui| {
                let dirty = state.has_pending_changes();
                if ui
                    .add_enabled(dirty, egui::Button::new("Apply pending fields"))
                    .clicked()
                    || (dirty
                        && ui.ctx().text_edit_focused()
                        && ui.input(|input| input.key_pressed(egui::Key::Enter)))
                {
                    match state.apply_pending() {
                        Ok(()) => action = Some(SettingsAction::PendingApplied),
                        Err(message) => {
                            state.pending_error = Some(message.clone());
                            action = Some(SettingsAction::PendingApplyFailed(message));
                        }
                    }
                }
                if ui
                    .add_enabled(dirty, egui::Button::new("Revert pending fields"))
                    .clicked()
                {
                    state.revert_pending();
                    action = Some(SettingsAction::PendingReverted);
                }
                if dirty {
                    ui.monospace("PENDING");
                } else {
                    ui.monospace("SAVED");
                }
            });
            if let Some(message) = state.pending_error.as_deref() {
                crate::ui::style::state_message(ui, "FAILED", message);
            }

            ui.add_space(6.0);
            ui.horizontal_wrapped(|ui| {
                if ui
                    .add_enabled(!api_busy, egui::Button::new("Test connection"))
                    .on_hover_text("GET scheduler status on a background worker.")
                    .clicked()
                {
                    action = Some(SettingsAction::TestConnection);
                }
                if ui
                    .add_enabled(!api_busy, egui::Button::new("Refresh Data Operations"))
                    .on_hover_text("Hydrate Data Operations in API mode; reload fixtures in mock mode.")
                    .clicked()
                {
                    action = Some(SettingsAction::RefreshDataOperations);
                }
                crate::ui::style::status_badge(ui, api_runtime.connection.as_str());
                if let Some(last_checked_at) = api_runtime.last_checked_at.as_deref() {
                    ui.monospace(format!("checked {last_checked_at}"));
                }
            });
            if let Some(message) = api_runtime.last_error.as_deref() {
                crate::ui::style::state_message(ui, "API ERROR", message);
            }

            ui.add_space(8.0);
            if let Some(summary) = storage_summary {
                ui.label(egui::RichText::new("Storage").strong());
                egui::Grid::new("settings_storage_grid")
                    .num_columns(3)
                    .min_col_width(128.0)
                    .striped(true)
                    .show(ui, |ui| {
                        storage_row(ui, "Root", &summary.app_root, &mut action);
                        storage_row(ui, "Version", &summary.version_dir, &mut action);
                        storage_row(ui, "Config", &summary.config_dir, &mut action);
                        storage_row(ui, "Settings", &summary.settings_file, &mut action);
                        storage_row(ui, "UI state", &summary.ui_state_file, &mut action);
                        storage_row(ui, "Cache", &summary.cache_dir, &mut action);
                        storage_row(ui, "Data", &summary.data_dir, &mut action);
                        storage_row(ui, "Exports", &summary.exports_dir, &mut action);
                        storage_row(ui, "Logs", &summary.logs_dir, &mut action);
                    });
                ui.horizontal_wrapped(|ui| {
                    ui.monospace(format!("schema {}", summary.schema_version));
                    ui.separator();
                    ui.monospace(&summary.cache_status)
                        .on_hover_text("SQLite is deferred; cache metadata is JSON and blobs live under cache/.");
                    ui.separator();
                    ui.monospace(&summary.migration_status)
                        .on_hover_text("Copy-forward is explicit. Cache is rebuildable; user data must be protected.");
                });
                ui.add_space(8.0);
            }

            ui.add_space(8.0);
            ui.label(egui::RichText::new("Data status legend").strong());
            egui::Grid::new("settings_status_legend")
                .num_columns(2)
                .min_col_width(120.0)
                .striped(true)
                .show(ui, |ui| {
                    for (tag, meaning) in [
                        ("FRESH", "current enough for display"),
                        ("STALE", "needs refresh"),
                        ("PENDING", "resolver/backfill pending"),
                        ("FAILED", "source or job failed"),
                        ("MOCK", "local mock data"),
                        ("SEED", "seed dataset"),
                        ("EST", "estimated/computed"),
                        ("MANUAL", "manual override"),
                        ("CONFLICT", "sources disagree"),
                        ("AMBIG", "ambiguous identifier"),
                        ("MISSING", "expected field missing"),
                    ] {
                        ui.monospace(tag);
                        ui.label(meaning);
                        ui.end_row();
                    }
                });
        });
    action
}

fn workspace_label(workspaces: &[Workspace], selected_workspace_id: &str) -> String {
    workspaces
        .iter()
        .find(|workspace| workspace.id == selected_workspace_id)
        .map(|workspace| workspace.name.clone())
        .unwrap_or_else(|| "No workspace".to_owned())
}

fn storage_row(ui: &mut egui::Ui, label: &str, path: &str, action: &mut Option<SettingsAction>) {
    ui.label(label);
    ui.monospace(path)
        .on_hover_text(path)
        .on_hover_cursor(egui::CursorIcon::Help);
    if ui.small_button("Copy").clicked() {
        ui.copy_text(path.to_owned());
        *action = Some(SettingsAction::StoragePathCopied(label.to_owned()));
    }
    ui.end_row();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pending_settings_apply_to_active_values() {
        let mut state = SettingsState::default();
        state.pending_api_config.base_url = "http://localhost:9999".to_owned();
        state.pending_base_currency = "USD".to_owned();

        assert!(state.has_pending_changes());

        state.apply_pending().expect("valid settings should apply");

        assert_eq!(state.api_config.base_url, "http://localhost:9999");
        assert_eq!(state.base_currency, "USD");
        assert!(!state.has_pending_changes());
    }

    #[test]
    fn pending_settings_reject_secret_bearing_urls() {
        let mut state = SettingsState::default();
        state.pending_api_config.base_url = "https://example.test/api/v1?token=secret".to_owned();

        let err = state
            .apply_pending()
            .expect_err("secret-bearing URL must be rejected");

        assert!(err.contains("query string"));
        assert_eq!(state.api_config.base_url, "http://localhost:8080/api/v1");
    }

    #[test]
    fn pending_settings_revert_to_active_values() {
        let mut state = SettingsState::default();
        state.pending_api_config.base_url = "http://localhost:9999".to_owned();

        state.revert_pending();

        assert_eq!(state.pending_api_config.base_url, state.api_config.base_url);
        assert!(!state.has_pending_changes());
    }

    #[test]
    fn zoom_factor_clamps_and_rounds() {
        assert_eq!(zoom_percent(0.63), 70);
        assert_eq!(zoom_percent(1.02), 102);
        assert_eq!(zoom_percent(2.0), 160);

        let mut state = SettingsState::default();
        state.zoom_in();
        assert_eq!(state.zoom_percent(), 110);
        state.reset_zoom();
        assert_eq!(state.zoom_percent(), 100);
    }
}
