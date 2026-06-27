use directories::BaseDirs;
use std::fmt;
use std::path::{Path, PathBuf};

pub const APP_NAME: &str = "Mimer";
pub const APP_MODE: &str = "mock";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoragePaths {
    app_root: PathBuf,
    version_dir: PathBuf,
    manifest_file: PathBuf,
    config_dir: PathBuf,
    settings_file: PathBuf,
    ui_state_file: PathBuf,
    cache_dir: PathBuf,
    cache_manifest_file: PathBuf,
    charts_cache_dir: PathBuf,
    documents_cache_dir: PathBuf,
    api_cache_dir: PathBuf,
    data_dir: PathBuf,
    local_overrides_file: PathBuf,
    saved_views_file: PathBuf,
    watchlists_file: PathBuf,
    exports_dir: PathBuf,
    logs_dir: PathBuf,
    tmp_dir: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoragePathError {
    MissingBaseDirs,
}

impl fmt::Display for StoragePathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingBaseDirs => write!(f, "could not resolve user app data directory"),
        }
    }
}

impl std::error::Error for StoragePathError {}

impl StoragePaths {
    pub fn for_current_app() -> Result<Self, StoragePathError> {
        let app_root = app_data_root()?;
        Ok(Self::from_app_root(app_root, current_app_version()))
    }

    pub fn from_app_root(app_root: impl Into<PathBuf>, app_version: &str) -> Self {
        let app_root = app_root.into();
        let version_dir = app_root.join(version_dir_name(app_version));
        let config_dir = version_dir.join("config");
        let cache_dir = version_dir.join("cache");
        let data_dir = version_dir.join("data");

        Self {
            app_root,
            manifest_file: version_dir.join("manifest.json"),
            settings_file: config_dir.join("settings.json"),
            ui_state_file: config_dir.join("ui_state.json"),
            cache_manifest_file: cache_dir.join("cache_manifest.json"),
            charts_cache_dir: cache_dir.join("charts"),
            documents_cache_dir: cache_dir.join("documents"),
            api_cache_dir: cache_dir.join("api"),
            local_overrides_file: data_dir.join("local_overrides.json"),
            saved_views_file: data_dir.join("saved_views.json"),
            watchlists_file: data_dir.join("watchlists.json"),
            exports_dir: version_dir.join("exports"),
            logs_dir: version_dir.join("logs"),
            tmp_dir: version_dir.join("tmp"),
            version_dir,
            config_dir,
            cache_dir,
            data_dir,
        }
    }

    pub fn directories(&self) -> Vec<&Path> {
        vec![
            &self.version_dir,
            &self.config_dir,
            &self.cache_dir,
            &self.charts_cache_dir,
            &self.documents_cache_dir,
            &self.api_cache_dir,
            &self.data_dir,
            &self.exports_dir,
            &self.logs_dir,
            &self.tmp_dir,
        ]
    }

    pub fn app_root(&self) -> &Path {
        &self.app_root
    }

    pub fn version_dir(&self) -> &Path {
        &self.version_dir
    }

    pub fn manifest_file(&self) -> &Path {
        &self.manifest_file
    }

    pub fn config_dir(&self) -> &Path {
        &self.config_dir
    }

    pub fn settings_file(&self) -> &Path {
        &self.settings_file
    }

    pub fn ui_state_file(&self) -> &Path {
        &self.ui_state_file
    }

    pub fn cache_dir(&self) -> &Path {
        &self.cache_dir
    }

    #[allow(dead_code)]
    pub fn cache_manifest_file(&self) -> &Path {
        &self.cache_manifest_file
    }

    #[allow(dead_code)]
    pub fn charts_cache_dir(&self) -> &Path {
        &self.charts_cache_dir
    }

    #[allow(dead_code)]
    pub fn documents_cache_dir(&self) -> &Path {
        &self.documents_cache_dir
    }

    #[allow(dead_code)]
    pub fn api_cache_dir(&self) -> &Path {
        &self.api_cache_dir
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    #[allow(dead_code)]
    pub fn local_overrides_file(&self) -> &Path {
        &self.local_overrides_file
    }

    #[allow(dead_code)]
    pub fn saved_views_file(&self) -> &Path {
        &self.saved_views_file
    }

    #[allow(dead_code)]
    pub fn watchlists_file(&self) -> &Path {
        &self.watchlists_file
    }

    pub fn exports_dir(&self) -> &Path {
        &self.exports_dir
    }

    pub fn logs_dir(&self) -> &Path {
        &self.logs_dir
    }

    pub fn tmp_dir(&self) -> &Path {
        &self.tmp_dir
    }
}

pub fn current_app_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

pub fn version_dir_name(app_version: &str) -> String {
    format!("v{}", app_version.trim_start_matches('v'))
}

fn app_data_root() -> Result<PathBuf, StoragePathError> {
    #[cfg(target_os = "windows")]
    {
        if let Some(appdata) = env_path("APPDATA") {
            return Ok(appdata.join(APP_NAME));
        }
        let base_dirs = BaseDirs::new().ok_or(StoragePathError::MissingBaseDirs)?;
        return Ok(base_dirs.config_dir().join(APP_NAME));
    }

    #[cfg(target_os = "macos")]
    {
        let base_dirs = BaseDirs::new().ok_or(StoragePathError::MissingBaseDirs)?;
        Ok(base_dirs
            .home_dir()
            .join("Library")
            .join("Application Support")
            .join(APP_NAME))
    }

    #[cfg(all(unix, not(target_os = "macos")))]
    {
        if let Some(xdg_data_home) = env_path("XDG_DATA_HOME") {
            return Ok(xdg_data_home.join(APP_NAME));
        }
        let base_dirs = BaseDirs::new().ok_or(StoragePathError::MissingBaseDirs)?;
        Ok(base_dirs.data_dir().join(APP_NAME))
    }

    #[cfg(not(any(target_os = "windows", target_os = "macos", unix)))]
    {
        let base_dirs = BaseDirs::new().ok_or(StoragePathError::MissingBaseDirs)?;
        Ok(base_dirs.data_dir().join(APP_NAME))
    }
}

#[cfg(any(target_os = "windows", all(unix, not(target_os = "macos"))))]
fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prefixes_version_folder_with_v() {
        assert_eq!(version_dir_name("0.1.0"), "v0.1.0");
        assert_eq!(version_dir_name("v0.2.0"), "v0.2.0");
    }

    #[test]
    fn builds_expected_versioned_layout() {
        let paths = StoragePaths::from_app_root(PathBuf::from("/tmp/Mimer"), "0.1.0");

        assert_eq!(paths.version_dir(), Path::new("/tmp/Mimer/v0.1.0"));
        assert_eq!(
            paths.settings_file(),
            Path::new("/tmp/Mimer/v0.1.0/config/settings.json")
        );
        assert_eq!(
            paths.ui_state_file(),
            Path::new("/tmp/Mimer/v0.1.0/config/ui_state.json")
        );
        assert_eq!(
            paths.local_overrides_file(),
            Path::new("/tmp/Mimer/v0.1.0/data/local_overrides.json")
        );
        assert_eq!(
            paths.documents_cache_dir(),
            Path::new("/tmp/Mimer/v0.1.0/cache/documents")
        );
    }
}
