use crate::compute::portfolio::PositionOverride;
use crate::domain::{
    Alert, AlertSeverity, DataFreshness, Distribution, DocumentSnapshot, Fund, JobRun, JobStatus,
    Position,
};

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DiagnosticsSummary {
    pub fresh_rows: usize,
    pub stale_rows: usize,
    pub missing_rows: usize,
    pub suspicious_rows: usize,
    pub manual_overrides: usize,
    pub estimated_or_derived_values: usize,
    pub failed_jobs: usize,
    pub ambiguous_instruments: usize,
    pub mock_or_seed_rows: usize,
    pub source_conflicts: usize,
}

#[allow(clippy::too_many_arguments)]
pub fn aggregate_diagnostics(
    positions: &[Position],
    funds: &[Fund],
    distributions: &[Distribution],
    documents: &[DocumentSnapshot],
    job_runs: &[JobRun],
    alerts: &[Alert],
    overrides: &[PositionOverride],
) -> DiagnosticsSummary {
    let fresh_rows = positions
        .iter()
        .filter(|position| position.freshness == DataFreshness::Fresh)
        .count();
    let stale_rows = positions
        .iter()
        .filter(|position| position.freshness != DataFreshness::Fresh)
        .count();
    let missing_rows = documents
        .iter()
        .filter(|document| document.status.eq_ignore_ascii_case("missing"))
        .count();
    let suspicious_rows = alerts
        .iter()
        .filter(|alert| {
            matches!(
                alert.severity,
                AlertSeverity::Warning | AlertSeverity::Critical
            )
        })
        .count();
    let failed_jobs = job_runs
        .iter()
        .filter(|run| run.status == JobStatus::Failed)
        .count();
    let ambiguous_instruments = alerts
        .iter()
        .filter(|alert| alert.category.eq_ignore_ascii_case("resolution"))
        .count();
    let estimated_or_derived_values = positions.len()
        + distributions
            .iter()
            .filter(|distribution| distribution.status.eq_ignore_ascii_case("estimated"))
            .count();
    let mock_or_seed_rows = positions
        .iter()
        .filter(|position| is_mock_or_seed(&position.source))
        .count()
        + funds
            .iter()
            .filter(|fund| is_mock_or_seed(&fund.source))
            .count()
        + documents
            .iter()
            .filter(|document| is_mock_or_seed(&document.source))
            .count();
    let source_conflicts = documents
        .iter()
        .filter(|document| {
            document
                .content_hash_change
                .to_ascii_lowercase()
                .contains("changed")
        })
        .count();

    DiagnosticsSummary {
        fresh_rows,
        stale_rows,
        missing_rows,
        suspicious_rows,
        manual_overrides: overrides.len(),
        estimated_or_derived_values,
        failed_jobs,
        ambiguous_instruments,
        mock_or_seed_rows,
        source_conflicts,
    }
}

fn is_mock_or_seed(source: &str) -> bool {
    matches!(source.trim().to_ascii_lowercase().as_str(), "mock" | "seed")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compute::portfolio::{PositionOverride, PositionOverrideField};
    use crate::domain::{DataFreshness, Distribution, DocumentSnapshot, Position};

    fn position(ticker: &str, freshness: DataFreshness, source: &str) -> Position {
        Position {
            fund_id: format!("fund-{ticker}"),
            listing_id: format!("listing-{ticker}"),
            ticker: ticker.to_owned(),
            name: format!("{ticker} fund"),
            isin: format!("isin-{ticker}"),
            listing_currency: "GBP".to_owned(),
            units: 1.0,
            price: 1.0,
            daily_change: 0.0,
            market_value: 1.0,
            portfolio_weight_pct: 100.0,
            trailing_yield_pct: 1.0,
            projected_income: 0.01,
            freshness,
            source: source.to_owned(),
        }
    }

    #[test]
    fn aggregates_core_diagnostics_counts() {
        let positions = vec![
            position("VUSA", DataFreshness::Fresh, "seed"),
            position("VHYL", DataFreshness::Stale, "stooq"),
        ];
        let distributions = vec![Distribution {
            fund_id: "fund-vusa".to_owned(),
            ticker: "VUSA".to_owned(),
            ex_date: "2026-06-13".to_owned(),
            payment_date: "2026-06-26".to_owned(),
            amount: 0.3,
            currency: "GBP".to_owned(),
            status: "Estimated".to_owned(),
            source: "issuer".to_owned(),
        }];
        let documents = vec![DocumentSnapshot {
            fund_id: "fund-vusa".to_owned(),
            ticker: "VUSA".to_owned(),
            document_type: "factsheet".to_owned(),
            latest_date: "2026-05-31".to_owned(),
            status: "Missing".to_owned(),
            content_hash_change: "hash changed".to_owned(),
            source: "seed".to_owned(),
            last_checked: "2026-06-20".to_owned(),
        }];
        let overrides = vec![PositionOverride::new(
            "listing-vusa",
            PositionOverrideField::Price,
            2.0,
        )];

        let summary = aggregate_diagnostics(
            &positions,
            &[],
            &distributions,
            &documents,
            &[],
            &[],
            &overrides,
        );

        assert_eq!(summary.fresh_rows, 1);
        assert_eq!(summary.stale_rows, 1);
        assert_eq!(summary.missing_rows, 1);
        assert_eq!(summary.manual_overrides, 1);
        assert_eq!(summary.source_conflicts, 1);
    }
}
