#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum SourceSelection {
    #[default]
    Canonical,
    Specific(String),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum DataKind {
    Price,
    Nav,
    Holdings,
    Distributions,
    Facts,
    Documents,
    Fx,
    Derived,
}

impl DataKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Price => "price",
            Self::Nav => "nav",
            Self::Holdings => "holdings",
            Self::Distributions => "distributions",
            Self::Facts => "facts",
            Self::Documents => "documents",
            Self::Fx => "fx",
            Self::Derived => "derived",
        }
    }
}

impl SourceSelection {
    pub fn label(&self) -> String {
        match self {
            Self::Canonical => "Canonical".to_owned(),
            Self::Specific(source) => format!("Specific: {source}"),
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum SourcePolicy {
    #[default]
    Canonical,
    IssuerPreferred,
    MarketDataPreferred,
    ManualPreferred,
    Specific(String),
}

impl SourcePolicy {
    pub const PRESETS: [Self; 4] = [
        Self::Canonical,
        Self::IssuerPreferred,
        Self::MarketDataPreferred,
        Self::ManualPreferred,
    ];

    pub fn label(&self) -> String {
        match self {
            Self::Canonical => "Canonical".to_owned(),
            Self::IssuerPreferred => "Issuer preferred".to_owned(),
            Self::MarketDataPreferred => "Market data preferred".to_owned(),
            Self::ManualPreferred => "Manual preferred".to_owned(),
            Self::Specific(source) => format!("Specific: {source}"),
        }
    }

    pub fn encode(&self) -> String {
        match self {
            Self::Canonical => "canonical".to_owned(),
            Self::IssuerPreferred => "issuer".to_owned(),
            Self::MarketDataPreferred => "market".to_owned(),
            Self::ManualPreferred => "manual".to_owned(),
            Self::Specific(source) => format!("specific:{source}"),
        }
    }

    pub fn decode(value: &str) -> Option<Self> {
        match value.trim() {
            "canonical" => Some(Self::Canonical),
            "issuer" => Some(Self::IssuerPreferred),
            "market" => Some(Self::MarketDataPreferred),
            "manual" => Some(Self::ManualPreferred),
            value => value
                .strip_prefix("specific:")
                .filter(|source| !source.is_empty())
                .map(|source| Self::Specific(source.to_owned())),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SourceResolutionStatus {
    Selected,
    Fallback,
    Missing,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SourceAvailability {
    pub available_sources: Vec<String>,
    pub requested_source: SourceSelection,
    pub effective_source: Option<String>,
    pub fallback_source: Option<String>,
    pub status: SourceResolutionStatus,
}

impl SourceAvailability {
    pub fn status_label(&self) -> &'static str {
        match self.status {
            SourceResolutionStatus::Selected => "SOURCE: selected",
            SourceResolutionStatus::Fallback => "SOURCE: fallback",
            SourceResolutionStatus::Missing => "SOURCE: missing",
        }
    }

    pub fn effective_label(&self) -> String {
        match (&self.status, self.effective_source.as_deref()) {
            (SourceResolutionStatus::Selected, Some(source)) => crate::format::fmt_source(source),
            (SourceResolutionStatus::Fallback, Some(source)) => {
                format!("{} fallback", crate::format::fmt_source(source))
            }
            (SourceResolutionStatus::Missing, _) | (_, None) => "SRC: missing".to_owned(),
        }
    }

    pub fn tooltip(&self, kind: DataKind) -> String {
        let requested = self.requested_source.label();
        let effective = self
            .effective_source
            .as_deref()
            .map(crate::format::fmt_source)
            .unwrap_or_else(|| "SRC: -".to_owned());
        let available = if self.available_sources.is_empty() {
            "-".to_owned()
        } else {
            self.available_sources.join(", ")
        };
        format!(
            "Kind: {}\nRequested: {}\nEffective: {}\nAvailable: {}\n{}",
            kind.as_str(),
            requested,
            effective,
            available,
            self.status_label()
        )
    }
}

pub fn resolve_source_selection(
    available_sources: &[String],
    requested_source: &SourceSelection,
    canonical_source: Option<&str>,
) -> SourceAvailability {
    let mut available = available_sources.to_vec();
    available.sort();
    available.dedup();

    let canonical = canonical_source
        .filter(|source| available.iter().any(|candidate| candidate == *source))
        .map(str::to_owned)
        .or_else(|| available.first().cloned());

    match requested_source {
        SourceSelection::Canonical => {
            let status = if canonical.is_some() {
                SourceResolutionStatus::Selected
            } else {
                SourceResolutionStatus::Missing
            };
            SourceAvailability {
                available_sources: available,
                requested_source: requested_source.clone(),
                effective_source: canonical,
                fallback_source: None,
                status,
            }
        }
        SourceSelection::Specific(source) => {
            if available.iter().any(|candidate| candidate == source) {
                SourceAvailability {
                    available_sources: available,
                    requested_source: requested_source.clone(),
                    effective_source: Some(source.clone()),
                    fallback_source: None,
                    status: SourceResolutionStatus::Selected,
                }
            } else {
                SourceAvailability {
                    available_sources: available,
                    requested_source: requested_source.clone(),
                    effective_source: canonical.clone(),
                    fallback_source: canonical,
                    status: SourceResolutionStatus::Fallback,
                }
            }
        }
    }
}

pub fn mock_available_sources_for(
    subject: &crate::domain::AnalysisSubject,
    kind: DataKind,
) -> (Vec<String>, &'static str) {
    use crate::domain::AnalysisSubject;

    let sources = match kind {
        DataKind::Price => match subject {
            AnalysisSubject::FundListing { listing_id, .. }
                if listing_id.contains("jepg") || listing_id.contains("jegp") =>
            {
                vec!["seed", "yfinance"]
            }
            AnalysisSubject::FundListing { .. } => vec!["seed", "stooq", "yfinance"],
            AnalysisSubject::Holding { .. } => vec!["yfinance"],
            AnalysisSubject::WorkspacePortfolio(_) => vec!["seed", "derived"],
            AnalysisSubject::Fund(_)
            | AnalysisSubject::Cash(_)
            | AnalysisSubject::SyntheticModel(_) => {
                vec!["seed"]
            }
        },
        DataKind::Nav => vec!["issuer", "seed"],
        DataKind::Holdings => vec!["issuer", "seed"],
        DataKind::Distributions => vec!["issuer", "seed"],
        DataKind::Facts => vec!["issuer", "seed"],
        DataKind::Documents => vec!["issuer", "seed"],
        DataKind::Fx => vec!["seed", "mock"],
        DataKind::Derived => vec!["derived", "seed"],
    };

    (sources.into_iter().map(str::to_owned).collect(), "seed")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::AnalysisSubject;

    #[test]
    fn uses_specific_source_when_available() {
        let available = vec!["seed".to_owned(), "stooq".to_owned()];
        let resolved = resolve_source_selection(
            &available,
            &SourceSelection::Specific("stooq".to_owned()),
            Some("seed"),
        );

        assert_eq!(resolved.effective_source.as_deref(), Some("stooq"));
        assert_eq!(resolved.status, SourceResolutionStatus::Selected);
    }

    #[test]
    fn falls_back_to_canonical_when_specific_source_is_missing() {
        let available = vec!["seed".to_owned(), "stooq".to_owned()];
        let resolved = resolve_source_selection(
            &available,
            &SourceSelection::Specific("yahoo".to_owned()),
            Some("seed"),
        );

        assert_eq!(resolved.effective_source.as_deref(), Some("seed"));
        assert_eq!(resolved.fallback_source.as_deref(), Some("seed"));
        assert_eq!(resolved.status, SourceResolutionStatus::Fallback);
    }

    #[test]
    fn source_policy_round_trips_specific_values() {
        let policy = SourcePolicy::Specific("issuer".to_owned());

        assert_eq!(SourcePolicy::decode(&policy.encode()), Some(policy));
    }

    #[test]
    fn labels_normal_fallback_without_missing_error() {
        let subject = AnalysisSubject::FundListing {
            fund_id: "fund-jepg".to_owned(),
            listing_id: "listing-jepg".to_owned(),
        };
        let (available, canonical) = mock_available_sources_for(&subject, DataKind::Price);
        let resolved = resolve_source_selection(
            &available,
            &SourceSelection::Specific("stooq".to_owned()),
            Some(canonical),
        );

        assert_eq!(resolved.status, SourceResolutionStatus::Fallback);
        assert_eq!(resolved.effective_source.as_deref(), Some("seed"));
        assert!(resolved.effective_label().contains("fallback"));
    }
}
