#![allow(dead_code)]

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CacheKind {
    Dashboard,
    FundDetail,
    TimeSeries,
    SearchResults,
    Documents,
    SourceCapabilities,
    DerivedAnalytics,
}

impl CacheKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Dashboard => "dashboard",
            Self::FundDetail => "fund_detail",
            Self::TimeSeries => "time_series",
            Self::SearchResults => "search_results",
            Self::Documents => "documents",
            Self::SourceCapabilities => "source_capabilities",
            Self::DerivedAnalytics => "derived_analytics",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CachePolicy {
    pub ttl_seconds: u64,
    pub stale_while_revalidate_seconds: u64,
    pub invalidate_on_version_change: bool,
    pub invalidate_on_source_policy_change: bool,
}

impl CachePolicy {
    pub fn for_kind(kind: CacheKind) -> Self {
        match kind {
            CacheKind::Dashboard => Self::new(600, 300, true, true),
            CacheKind::FundDetail => Self::new(3_600, 900, true, true),
            CacheKind::TimeSeries => Self::new(21_600, 3_600, true, true),
            CacheKind::SearchResults => Self::new(900, 300, true, true),
            CacheKind::Documents => Self::new(2_592_000, 86_400, false, false),
            CacheKind::SourceCapabilities => Self::new(86_400, 3_600, true, false),
            CacheKind::DerivedAnalytics => Self::new(300, 0, true, true),
        }
    }

    pub const fn new(
        ttl_seconds: u64,
        stale_while_revalidate_seconds: u64,
        invalidate_on_version_change: bool,
        invalidate_on_source_policy_change: bool,
    ) -> Self {
        Self {
            ttl_seconds,
            stale_while_revalidate_seconds,
            invalidate_on_version_change,
            invalidate_on_source_policy_change,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CacheEntryMeta {
    pub key: String,
    pub kind: CacheKind,
    pub fetched_at_epoch_seconds: u64,
    pub source_policy: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_hash: Option<String>,
}

impl CacheEntryMeta {
    pub fn is_expired(&self, now_epoch_seconds: u64, policy: &CachePolicy) -> bool {
        is_expired(
            self.fetched_at_epoch_seconds,
            now_epoch_seconds,
            policy.ttl_seconds,
        )
    }
}

pub trait LocalCache {
    fn meta(&self, key: &str) -> Option<&CacheEntryMeta>;
    fn put_meta(&mut self, meta: CacheEntryMeta);
    fn remove(&mut self, key: &str) -> Option<CacheEntryMeta>;
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct JsonCacheManifest {
    pub entries: BTreeMap<String, CacheEntryMeta>,
}

impl LocalCache for JsonCacheManifest {
    fn meta(&self, key: &str) -> Option<&CacheEntryMeta> {
        self.entries.get(key)
    }

    fn put_meta(&mut self, meta: CacheEntryMeta) {
        self.entries.insert(meta.key.clone(), meta);
    }

    fn remove(&mut self, key: &str) -> Option<CacheEntryMeta> {
        self.entries.remove(key)
    }
}

pub fn is_expired(fetched_at_epoch_seconds: u64, now_epoch_seconds: u64, ttl_seconds: u64) -> bool {
    now_epoch_seconds.saturating_sub(fetched_at_epoch_seconds) >= ttl_seconds
}

pub fn cache_key_for(subject: &str, data_kind: &str, source_policy: &str) -> String {
    cache_key_for_parts(&[subject, data_kind, source_policy])
}

pub fn cache_key_for_parts(parts: &[&str]) -> String {
    parts
        .iter()
        .map(|part| sanitize_key_part(part))
        .collect::<Vec<_>>()
        .join("__")
}

fn sanitize_key_part(part: &str) -> String {
    let sanitized = part
        .trim()
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.') {
                ch.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect::<String>();

    sanitized
        .split('_')
        .filter(|segment| !segment.is_empty())
        .collect::<Vec<_>>()
        .join("_")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_expiry_uses_numeric_ttl() {
        assert!(!is_expired(100, 159, 60));
        assert!(is_expired(100, 160, 60));
        assert!(is_expired(100, 10_000, 60));
    }

    #[test]
    fn cache_key_is_stable_and_filesystem_friendly() {
        assert_eq!(
            cache_key_for("VUSA / XLON", "price", "Issuer preferred"),
            "vusa_xlon__price__issuer_preferred"
        );
    }

    #[test]
    fn default_policies_capture_invalidation_rules() {
        let dashboard = CachePolicy::for_kind(CacheKind::Dashboard);
        let documents = CachePolicy::for_kind(CacheKind::Documents);

        assert!(dashboard.invalidate_on_version_change);
        assert!(dashboard.invalidate_on_source_policy_change);
        assert!(!documents.invalidate_on_source_policy_change);
        assert!(documents.ttl_seconds > dashboard.ttl_seconds);
    }
}
