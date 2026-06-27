use std::cmp::Ordering;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VersionDir {
    pub version: String,
    pub path: PathBuf,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageMigrationPlan {
    pub from_version: Option<String>,
    pub to_version: String,
    pub copy_config: bool,
    pub copy_user_data: bool,
    pub rebuild_cache: bool,
    pub notes: Vec<String>,
}

impl StorageMigrationPlan {
    pub fn summary(&self) -> String {
        match self.from_version.as_deref() {
            Some(from_version) => format!(
                "from {from_version} to {} | config: {} | data: {} | cache: rebuild",
                self.to_version,
                bool_label(self.copy_config),
                bool_label(self.copy_user_data)
            ),
            None => format!("fresh {}", self.to_version),
        }
    }
}

pub fn find_previous_version_dirs(
    app_root: &Path,
    current_version: &str,
) -> io::Result<Vec<VersionDir>> {
    if !app_root.exists() {
        return Ok(Vec::new());
    }

    let mut versions = Vec::new();
    for entry in fs::read_dir(app_root)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let file_name = entry.file_name();
        let Some(name) = file_name.to_str() else {
            continue;
        };
        let Some(version) = parse_version_dir_name(name) else {
            continue;
        };
        if compare_versions(&version, current_version) == Ordering::Less {
            versions.push(VersionDir {
                version,
                path: entry.path(),
            });
        }
    }

    versions.sort_by(|left, right| compare_versions(&left.version, &right.version));
    Ok(versions)
}

pub fn migration_plan_from_previous_version(
    previous: Option<&VersionDir>,
    current_version: &str,
) -> StorageMigrationPlan {
    match previous {
        Some(previous) => StorageMigrationPlan {
            from_version: Some(previous.version.clone()),
            to_version: current_version.to_owned(),
            copy_config: true,
            copy_user_data: true,
            rebuild_cache: true,
            notes: vec![
                "Copy safe config and user data forward explicitly.".to_owned(),
                "Do not copy cache blindly; backend/source caches are rebuildable.".to_owned(),
                "Preserve manual overrides/watchlists where schemas are compatible.".to_owned(),
            ],
        },
        None => StorageMigrationPlan {
            from_version: None,
            to_version: current_version.to_owned(),
            copy_config: false,
            copy_user_data: false,
            rebuild_cache: false,
            notes: vec!["No previous version directory discovered.".to_owned()],
        },
    }
}

#[allow(dead_code)]
pub fn copy_forward_config_if_safe(
    from_version_dir: &Path,
    to_version_dir: &Path,
) -> io::Result<Vec<PathBuf>> {
    let copy_pairs = [
        ("config/settings.json", "config/settings.json"),
        ("config/ui_state.json", "config/ui_state.json"),
        ("data/local_overrides.json", "data/local_overrides.json"),
        ("data/saved_views.json", "data/saved_views.json"),
        ("data/watchlists.json", "data/watchlists.json"),
    ];
    let mut copied = Vec::new();

    for (from_relative, to_relative) in copy_pairs {
        let from = from_version_dir.join(from_relative);
        let to = to_version_dir.join(to_relative);
        if !from.exists() || to.exists() {
            continue;
        }
        if let Some(parent) = to.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(&from, &to)?;
        copied.push(to);
    }

    Ok(copied)
}

fn parse_version_dir_name(name: &str) -> Option<String> {
    name.strip_prefix('v')
        .filter(|version| !version.is_empty())
        .filter(|version| version.chars().all(|ch| ch.is_ascii_digit() || ch == '.'))
        .map(str::to_owned)
}

fn compare_versions(left: &str, right: &str) -> Ordering {
    let left_parts = numeric_parts(left);
    let right_parts = numeric_parts(right);
    for index in 0..left_parts.len().max(right_parts.len()) {
        let left = left_parts.get(index).copied().unwrap_or_default();
        let right = right_parts.get(index).copied().unwrap_or_default();
        match left.cmp(&right) {
            Ordering::Equal => {}
            ordering => return ordering,
        }
    }
    Ordering::Equal
}

fn numeric_parts(version: &str) -> Vec<u32> {
    version
        .split('.')
        .map(|part| part.parse::<u32>().unwrap_or_default())
        .collect()
}

fn bool_label(value: bool) -> &'static str {
    if value { "copy" } else { "skip" }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn discovers_previous_version_dirs_in_order() {
        let root = unique_temp_dir("mimer-migration");
        fs::create_dir_all(root.join("v0.1.0")).expect("create v0.1.0");
        fs::create_dir_all(root.join("v0.2.0")).expect("create v0.2.0");
        fs::create_dir_all(root.join("v0.10.0")).expect("create v0.10.0");
        fs::create_dir_all(root.join("scratch")).expect("create scratch");

        let versions = find_previous_version_dirs(&root, "0.10.0").expect("scan versions");

        assert_eq!(
            versions
                .iter()
                .map(|version| version.version.as_str())
                .collect::<Vec<_>>(),
            vec!["0.1.0", "0.2.0"]
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn migration_plan_copies_config_and_rebuilds_cache() {
        let previous = VersionDir {
            version: "0.1.0".to_owned(),
            path: PathBuf::from("/tmp/Mimer/v0.1.0"),
        };

        let plan = migration_plan_from_previous_version(Some(&previous), "0.2.0");

        assert!(plan.copy_config);
        assert!(plan.copy_user_data);
        assert!(plan.rebuild_cache);
        assert_eq!(plan.from_version.as_deref(), Some("0.1.0"));
    }

    fn unique_temp_dir(prefix: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
    }
}
