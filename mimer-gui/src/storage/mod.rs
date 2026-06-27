pub mod cache;
pub mod manifest;
pub mod migration;
pub mod paths;
pub mod settings;

use manifest::{StorageManifest, unix_timestamp_string};
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

pub use migration::{
    StorageMigrationPlan, find_previous_version_dirs, migration_plan_from_previous_version,
};
pub use paths::{APP_MODE, StoragePaths, current_app_version};
pub use settings::{StoredAppSettings, StoredUiState};

#[derive(Clone, Debug)]
pub struct StorageManager {
    paths: StoragePaths,
    manifest: StorageManifest,
    migration_plan: StorageMigrationPlan,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageSummary {
    pub app_root: String,
    pub version_dir: String,
    pub schema_version: u32,
    pub manifest_file: String,
    pub settings_file: String,
    pub ui_state_file: String,
    pub config_dir: String,
    pub cache_dir: String,
    pub data_dir: String,
    pub exports_dir: String,
    pub logs_dir: String,
    pub tmp_dir: String,
    pub cache_status: String,
    pub migration_status: String,
}

#[derive(Debug)]
pub enum StorageError {
    ResolvePath(paths::StoragePathError),
    CreateDirectory {
        path: PathBuf,
        source: std::io::Error,
    },
    ReadFile {
        path: PathBuf,
        source: std::io::Error,
    },
    WriteFile {
        path: PathBuf,
        source: std::io::Error,
    },
    ParseJson {
        path: PathBuf,
        source: serde_json::Error,
    },
    EncodeJson {
        path: PathBuf,
        source: serde_json::Error,
    },
    ScanVersions {
        path: PathBuf,
        source: std::io::Error,
    },
}

impl StorageManager {
    pub fn initialize() -> Result<Self, StorageError> {
        let paths = StoragePaths::for_current_app().map_err(StorageError::ResolvePath)?;
        Self::initialize_with_paths(paths)
    }

    pub fn initialize_with_paths(paths: StoragePaths) -> Result<Self, StorageError> {
        ensure_directories(&paths)?;
        let previous_versions = find_previous_version_dirs(paths.app_root(), current_app_version())
            .map_err(|source| StorageError::ScanVersions {
                path: paths.app_root().to_path_buf(),
                source,
            })?;
        let migration_plan =
            migration_plan_from_previous_version(previous_versions.last(), current_app_version());
        let manifest = load_or_create_manifest(paths.manifest_file())?;

        Ok(Self {
            paths,
            manifest,
            migration_plan,
        })
    }

    pub fn paths(&self) -> &StoragePaths {
        &self.paths
    }

    pub fn load_settings(&self) -> Result<Option<StoredAppSettings>, StorageError> {
        read_json(self.paths.settings_file())
    }

    pub fn save_settings(&self, settings: &StoredAppSettings) -> Result<(), StorageError> {
        write_json(self.paths.settings_file(), settings)
    }

    pub fn load_ui_state(&self) -> Result<Option<StoredUiState>, StorageError> {
        read_json(self.paths.ui_state_file())
    }

    pub fn save_ui_state(&self, ui_state: &StoredUiState) -> Result<(), StorageError> {
        write_json(self.paths.ui_state_file(), ui_state)
    }

    pub fn summary(&self) -> StorageSummary {
        StorageSummary {
            app_root: display_path(self.paths.app_root()),
            version_dir: display_path(self.paths.version_dir()),
            schema_version: self.manifest.storage_schema_version,
            manifest_file: display_path(self.paths.manifest_file()),
            settings_file: display_path(self.paths.settings_file()),
            ui_state_file: display_path(self.paths.ui_state_file()),
            config_dir: display_path(self.paths.config_dir()),
            cache_dir: display_path(self.paths.cache_dir()),
            data_dir: display_path(self.paths.data_dir()),
            exports_dir: display_path(self.paths.exports_dir()),
            logs_dir: display_path(self.paths.logs_dir()),
            tmp_dir: display_path(self.paths.tmp_dir()),
            cache_status: "JSON metadata; SQLite deferred; filesystem blobs for documents"
                .to_owned(),
            migration_status: self.migration_plan.summary(),
        }
    }
}

impl fmt::Display for StorageError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ResolvePath(err) => write!(f, "{err}"),
            Self::CreateDirectory { path, .. } => {
                write!(
                    f,
                    "could not create storage directory {}",
                    display_path(path)
                )
            }
            Self::ReadFile { path, .. } => {
                write!(f, "could not read storage file {}", display_path(path))
            }
            Self::WriteFile { path, .. } => {
                write!(f, "could not write storage file {}", display_path(path))
            }
            Self::ParseJson { path, .. } => {
                write!(
                    f,
                    "could not parse JSON storage file {}",
                    display_path(path)
                )
            }
            Self::EncodeJson { path, .. } => {
                write!(
                    f,
                    "could not encode JSON storage file {}",
                    display_path(path)
                )
            }
            Self::ScanVersions { path, .. } => {
                write!(
                    f,
                    "could not scan version directories in {}",
                    display_path(path)
                )
            }
        }
    }
}

impl std::error::Error for StorageError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::ResolvePath(err) => Some(err),
            Self::CreateDirectory { source, .. }
            | Self::ReadFile { source, .. }
            | Self::WriteFile { source, .. }
            | Self::ScanVersions { source, .. } => Some(source),
            Self::ParseJson { source, .. } | Self::EncodeJson { source, .. } => Some(source),
        }
    }
}

fn ensure_directories(paths: &StoragePaths) -> Result<(), StorageError> {
    for directory in paths.directories() {
        fs::create_dir_all(directory).map_err(|source| StorageError::CreateDirectory {
            path: directory.to_path_buf(),
            source,
        })?;
    }
    Ok(())
}

fn load_or_create_manifest(path: &Path) -> Result<StorageManifest, StorageError> {
    let now = unix_timestamp_string();
    let mut manifest = read_json(path)?
        .unwrap_or_else(|| StorageManifest::new(current_app_version(), APP_MODE, now.clone()));
    manifest.mark_opened(now);
    write_json(path, &manifest)?;
    Ok(manifest)
}

fn read_json<T: DeserializeOwned>(path: &Path) -> Result<Option<T>, StorageError> {
    if !path.exists() {
        return Ok(None);
    }
    let content = fs::read_to_string(path).map_err(|source| StorageError::ReadFile {
        path: path.to_path_buf(),
        source,
    })?;
    serde_json::from_str(&content)
        .map(Some)
        .map_err(|source| StorageError::ParseJson {
            path: path.to_path_buf(),
            source,
        })
}

fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<(), StorageError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|source| StorageError::CreateDirectory {
            path: parent.to_path_buf(),
            source,
        })?;
    }
    let encoded =
        serde_json::to_string_pretty(value).map_err(|source| StorageError::EncodeJson {
            path: path.to_path_buf(),
            source,
        })?;
    fs::write(path, format!("{encoded}\n")).map_err(|source| StorageError::WriteFile {
        path: path.to_path_buf(),
        source,
    })
}

fn display_path(path: &Path) -> String {
    path.display().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn initializes_versioned_storage_and_manifest() {
        let root = unique_temp_dir("mimer-storage");
        let paths = StoragePaths::from_app_root(&root, current_app_version());
        let manager = StorageManager::initialize_with_paths(paths).expect("storage initializes");

        assert!(manager.paths().version_dir().exists());
        assert!(
            manager
                .paths()
                .settings_file()
                .parent()
                .is_some_and(Path::exists)
        );
        assert!(manager.paths().manifest_file().exists());
        assert_eq!(manager.manifest.app, paths::APP_NAME);
        assert_eq!(
            manager.manifest.storage_schema_version,
            manifest::STORAGE_SCHEMA_VERSION
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn round_trips_settings_and_ui_state_json() {
        let root = unique_temp_dir("mimer-storage-json");
        let paths = StoragePaths::from_app_root(&root, current_app_version());
        let manager = StorageManager::initialize_with_paths(paths).expect("storage initializes");
        let settings = StoredAppSettings {
            theme: Some("dark".to_owned()),
            zoom_factor: Some(1.1),
            ..Default::default()
        };
        let ui_state = StoredUiState {
            selected_workspace_id: Some("workspace-main".to_owned()),
            show_inspector: Some(false),
            ..Default::default()
        };

        manager
            .save_settings(&settings)
            .expect("settings should save");
        manager
            .save_ui_state(&ui_state)
            .expect("ui state should save");

        assert_eq!(
            manager.load_settings().expect("settings load"),
            Some(settings)
        );
        assert_eq!(
            manager.load_ui_state().expect("ui state load"),
            Some(ui_state)
        );

        let _ = fs::remove_dir_all(root);
    }

    fn unique_temp_dir(prefix: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
    }
}
