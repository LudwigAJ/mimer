use crate::table_state::TableLayoutRegistry;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct StoredAppSettings {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub theme: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub density: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub zoom_factor: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_policy: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub base_currency: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refresh_interval_minutes: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_base_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data_mode: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_timeout_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auto_refresh_data_operations: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_auth_mode: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace_header_value: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct StoredUiState {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub selected_workspace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_active_page: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub show_left_navigation: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub show_inspector: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub show_context_strip: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub show_status_bar: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub inspector_width: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub layout_revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub table_layouts: Option<TableLayoutRegistry>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_document_omits_absent_values() {
        let settings = StoredAppSettings {
            theme: Some("dark".to_owned()),
            ..Default::default()
        };
        let encoded = serde_json::to_string(&settings).expect("settings should encode");

        assert!(encoded.contains("theme"));
        assert!(!encoded.contains("api_base_url"));
    }

    #[test]
    fn ui_state_document_keeps_layout_flags() {
        let ui_state = StoredUiState {
            selected_workspace_id: Some("workspace-main".to_owned()),
            show_status_bar: Some(false),
            inspector_width: Some(320.0),
            table_layouts: Some(TableLayoutRegistry::default()),
            ..Default::default()
        };

        assert_eq!(
            ui_state.selected_workspace_id.as_deref(),
            Some("workspace-main")
        );
        assert_eq!(ui_state.show_status_bar, Some(false));
        assert_eq!(ui_state.inspector_width, Some(320.0));
        assert!(ui_state.table_layouts.is_some());
    }
}
