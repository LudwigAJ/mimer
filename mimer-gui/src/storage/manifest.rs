use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};

pub const STORAGE_SCHEMA_VERSION: u32 = 1;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StorageManifest {
    pub app: String,
    pub app_version: String,
    pub storage_schema_version: u32,
    pub created_at: String,
    pub last_opened_at: String,
    pub mode: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub migrations: Vec<StorageMigrationRecord>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StorageMigrationRecord {
    pub from_version: String,
    pub to_version: String,
    pub action: String,
    pub recorded_at: String,
}

impl StorageManifest {
    pub fn new(app_version: &str, mode: &str, now: impl Into<String>) -> Self {
        let now = now.into();
        Self {
            app: super::paths::APP_NAME.to_owned(),
            app_version: app_version.to_owned(),
            storage_schema_version: STORAGE_SCHEMA_VERSION,
            created_at: now.clone(),
            last_opened_at: now,
            mode: mode.to_owned(),
            migrations: Vec::new(),
        }
    }

    pub fn mark_opened(&mut self, now: impl Into<String>) {
        self.last_opened_at = now.into();
        self.app_version = super::paths::current_app_version().to_owned();
        self.storage_schema_version = STORAGE_SCHEMA_VERSION;
        self.mode = super::paths::APP_MODE.to_owned();
    }
}

pub fn unix_timestamp_string() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    format!("unix:{seconds}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_keeps_created_time_when_opened() {
        let mut manifest = StorageManifest::new("0.1.0", "mock", "unix:1");

        manifest.mark_opened("unix:2");

        assert_eq!(manifest.created_at, "unix:1");
        assert_eq!(manifest.last_opened_at, "unix:2");
        assert_eq!(manifest.storage_schema_version, STORAGE_SCHEMA_VERSION);
        assert_eq!(manifest.mode, "mock");
    }
}
