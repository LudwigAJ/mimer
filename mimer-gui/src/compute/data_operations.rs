use crate::api::safety::mask_secret_fragments;
use crate::domain::{
    ConstituentReadinessRow, DataOperationStatus, DataOperationsSnapshot, RecommendedDataAction,
    SourceFetchLog,
};

#[cfg(test)]
pub fn readiness_status_from_coverage(
    coverage_pct: f32,
    blocked_count: usize,
    stale_count: usize,
) -> DataOperationStatus {
    if blocked_count > 0 {
        DataOperationStatus::Blocked
    } else if coverage_pct <= 0.0 {
        DataOperationStatus::Missing
    } else if stale_count > 0 {
        DataOperationStatus::Stale
    } else if coverage_pct >= 99.5 {
        DataOperationStatus::Ready
    } else {
        DataOperationStatus::Partial
    }
}

pub fn derive_recommended_actions(
    operations: &DataOperationsSnapshot,
) -> Vec<RecommendedDataAction> {
    let mut actions = operations
        .market_data_plan
        .iter()
        .filter(|item| plan_status_needs_action(item.status))
        .map(|item| RecommendedDataAction {
            priority: item.priority,
            label: item.next_action.clone(),
            target: item.subject_label.clone(),
            reason: item
                .blocker
                .as_ref()
                .filter(|blocker| !blocker.trim().is_empty())
                .cloned()
                .unwrap_or_else(|| item.reason.clone()),
            status: item.status,
            command: command_for_plan_item(&item.item_type, &item.subject_label),
            action_label: action_label_for_status(item.status).to_owned(),
            source: item.source.clone(),
        })
        .collect::<Vec<_>>();

    actions.extend(
        operations
            .diagnostic_issues
            .iter()
            .filter(|issue| plan_status_needs_action(issue.status))
            .map(|issue| RecommendedDataAction {
                priority: 80,
                label: issue.recommended_action.clone(),
                target: issue.related_page.clone(),
                reason: issue.detail.clone(),
                status: issue.status,
                command: "diagnostics".to_owned(),
                action_label: "Open Diagnostics".to_owned(),
                source: issue.source.clone(),
            }),
    );

    actions.sort_by(|left, right| {
        left.priority
            .cmp(&right.priority)
            .then_with(|| severity_rank(right.status).cmp(&severity_rank(left.status)))
            .then_with(|| left.label.cmp(&right.label))
    });
    actions
}

pub fn constituent_readiness_status(row: &ConstituentReadinessRow) -> DataOperationStatus {
    match (row.identity_status, row.price_status) {
        (DataOperationStatus::Ambiguous, _) => DataOperationStatus::Ambiguous,
        (DataOperationStatus::Missing, _) => DataOperationStatus::Missing,
        (DataOperationStatus::Failed, _) | (_, DataOperationStatus::Failed) => {
            DataOperationStatus::Failed
        }
        (DataOperationStatus::Blocked, _) | (_, DataOperationStatus::Blocked) => {
            DataOperationStatus::Blocked
        }
        (_, DataOperationStatus::Missing) => DataOperationStatus::Missing,
        (_, DataOperationStatus::Stale) => DataOperationStatus::Stale,
        (_, DataOperationStatus::Partial) => DataOperationStatus::Partial,
        _ => DataOperationStatus::Ready,
    }
}

pub fn fetch_log_display_key(log: &SourceFetchLog) -> String {
    mask_secret_fragments(&log.request_key)
}

pub fn fetch_log_copy_text(log: &SourceFetchLog) -> String {
    [
        log.id.clone(),
        log.time.clone(),
        log.source.clone(),
        log.request_kind.clone(),
        fetch_log_display_key(log),
        log.status.as_str().to_owned(),
        log.http_status
            .map(|status| status.to_string())
            .unwrap_or_else(|| "-".to_owned()),
        log.duration_ms.to_string(),
        bool_text(log.cache_hit),
        bool_text(log.rate_limited),
        log.error
            .as_deref()
            .map(mask_secret_fragments)
            .unwrap_or_else(|| "-".to_owned()),
    ]
    .join("\t")
}

fn plan_status_needs_action(status: DataOperationStatus) -> bool {
    matches!(
        status,
        DataOperationStatus::Needed
            | DataOperationStatus::Blocked
            | DataOperationStatus::BudgetBlocked
            | DataOperationStatus::Missing
            | DataOperationStatus::Stale
            | DataOperationStatus::Failed
            | DataOperationStatus::Ambiguous
            | DataOperationStatus::Partial
            | DataOperationStatus::Unknown
    )
}

fn action_label_for_status(status: DataOperationStatus) -> &'static str {
    match status {
        DataOperationStatus::Blocked | DataOperationStatus::BudgetBlocked => "Open Source",
        DataOperationStatus::Ambiguous => "Resolve",
        DataOperationStatus::Failed => "Review Logs",
        DataOperationStatus::Missing
        | DataOperationStatus::Needed
        | DataOperationStatus::Stale
        | DataOperationStatus::Partial => "Run now (mock)",
        DataOperationStatus::Ok
        | DataOperationStatus::Ready
        | DataOperationStatus::Mock
        | DataOperationStatus::Running
        | DataOperationStatus::Fresh
        | DataOperationStatus::Planned
        | DataOperationStatus::Unknown => "Open",
    }
}

fn command_for_plan_item(item_type: &str, subject_label: &str) -> String {
    match item_type {
        "resolve_constituent_identity" => {
            format!(
                "resolve {}",
                subject_label
                    .split_whitespace()
                    .next()
                    .unwrap_or(subject_label)
            )
        }
        "fetch_constituent_price" => "run constituent_price_ingestion".to_owned(),
        "fetch_fx_rate" => "run fx_ingestion".to_owned(),
        "refresh_holdings" => "run holdings_ingestion".to_owned(),
        "recompute_exposure" => "run recompute_exposure".to_owned(),
        "refresh_documents" => "run document_snapshot_check".to_owned(),
        _ => format!("run {}", item_type.replace('-', "_")),
    }
}

fn severity_rank(status: DataOperationStatus) -> u8 {
    match status {
        DataOperationStatus::Failed
        | DataOperationStatus::Blocked
        | DataOperationStatus::BudgetBlocked => 4,
        DataOperationStatus::Missing | DataOperationStatus::Ambiguous => 3,
        DataOperationStatus::Stale | DataOperationStatus::Partial => 2,
        DataOperationStatus::Needed
        | DataOperationStatus::Running
        | DataOperationStatus::Unknown => 1,
        DataOperationStatus::Ok
        | DataOperationStatus::Ready
        | DataOperationStatus::Mock
        | DataOperationStatus::Fresh
        | DataOperationStatus::Planned => 0,
    }
}

fn bool_text(value: bool) -> String {
    let text = if value { "yes" } else { "no" };
    text.to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::AlertSeverity;
    use crate::domain::{AnalysisSubject, DataDiagnosticIssue, MarketDataPlanItem};

    #[test]
    fn maps_readiness_coverage_to_status() {
        assert_eq!(
            readiness_status_from_coverage(100.0, 0, 0),
            DataOperationStatus::Ready
        );
        assert_eq!(
            readiness_status_from_coverage(84.0, 0, 0),
            DataOperationStatus::Partial
        );
        assert_eq!(
            readiness_status_from_coverage(84.0, 1, 0),
            DataOperationStatus::Blocked
        );
        assert_eq!(
            readiness_status_from_coverage(100.0, 0, 2),
            DataOperationStatus::Stale
        );
        assert_eq!(
            readiness_status_from_coverage(0.0, 0, 0),
            DataOperationStatus::Missing
        );
    }

    #[test]
    fn masks_fetch_log_secrets_for_display_and_copy() {
        let log = SourceFetchLog {
            id: "log-1".to_owned(),
            time: "2026-06-23 10:00".to_owned(),
            source: "openfigi".to_owned(),
            request_kind: "identity".to_owned(),
            request_key: "https://example.test?apikey=dev-secret&isin=IE000JEPG001 token=abc"
                .to_owned(),
            status: DataOperationStatus::Failed,
            http_status: Some(429),
            duration_ms: 1300,
            cache_hit: false,
            rate_limited: true,
            error: Some("rate limited".to_owned()),
        };

        let display = fetch_log_display_key(&log);
        assert!(display.contains("apikey=***"));
        assert!(display.contains("token=***"));
        assert!(!display.contains("dev-secret"));
        assert!(!fetch_log_copy_text(&log).contains("abc"));
    }

    #[test]
    fn constituent_status_prioritizes_identity_then_price() {
        let mut row = constituent_row();
        assert_eq!(
            constituent_readiness_status(&row),
            DataOperationStatus::Ready
        );

        row.price_status = DataOperationStatus::Stale;
        assert_eq!(
            constituent_readiness_status(&row),
            DataOperationStatus::Stale
        );

        row.identity_status = DataOperationStatus::Ambiguous;
        assert_eq!(
            constituent_readiness_status(&row),
            DataOperationStatus::Ambiguous
        );
    }

    #[test]
    fn derives_actions_from_plan_items_and_diagnostics() {
        let operations = DataOperationsSnapshot {
            readiness_stages: Vec::new(),
            market_data_plan: vec![
                MarketDataPlanItem {
                    id: "ready".to_owned(),
                    priority: 9,
                    item_type: "recompute_exposure".to_owned(),
                    subject_label: "Portfolio".to_owned(),
                    subject: None,
                    reason: "already fresh".to_owned(),
                    source: "derived".to_owned(),
                    estimated_requests: 0,
                    status: DataOperationStatus::Ready,
                    blocker: None,
                    next_action: "Nothing".to_owned(),
                },
                MarketDataPlanItem {
                    id: "missing".to_owned(),
                    priority: 1,
                    item_type: "fetch_constituent_price".to_owned(),
                    subject_label: "MSFT".to_owned(),
                    subject: Some(AnalysisSubject::Holding {
                        ticker: "MSFT".to_owned(),
                        source: "VUSA".to_owned(),
                    }),
                    reason: "price missing".to_owned(),
                    source: "yfinance".to_owned(),
                    estimated_requests: 1,
                    status: DataOperationStatus::Missing,
                    blocker: None,
                    next_action: "Fetch price".to_owned(),
                },
            ],
            source_budgets: Vec::new(),
            fetch_logs: Vec::new(),
            constituent_coverage: Vec::new(),
            diagnostic_issues: vec![DataDiagnosticIssue {
                id: "diag".to_owned(),
                severity: AlertSeverity::Critical,
                title: "Budget blocked".to_owned(),
                detail: "source is backing off".to_owned(),
                status: DataOperationStatus::BudgetBlocked,
                source: "openfigi".to_owned(),
                recommended_action: "Open source budget".to_owned(),
                related_page: "Source Budgets".to_owned(),
            }],
            ..Default::default()
        };

        let actions = derive_recommended_actions(&operations);
        assert_eq!(actions.len(), 2);
        assert_eq!(actions[0].label, "Fetch price");
        assert!(
            actions
                .iter()
                .any(|action| action.label == "Open source budget")
        );
    }
}

#[cfg(test)]
fn constituent_row() -> ConstituentReadinessRow {
    ConstituentReadinessRow {
        fund_ticker: "VUSA".to_owned(),
        holding_name: "Microsoft".to_owned(),
        holding_ticker: "MSFT".to_owned(),
        weight_pct: 5.9,
        subject: crate::domain::AnalysisSubject::Holding {
            ticker: "MSFT".to_owned(),
            source: "VUSA".to_owned(),
        },
        identity_status: DataOperationStatus::Ready,
        instrument_id: Some("instrument-msft".to_owned()),
        listing_id: Some("listing-msft-us".to_owned()),
        latest_price: Some(410.0),
        price_date: Some("2026-06-20".to_owned()),
        price_source: "yfinance".to_owned(),
        price_status: DataOperationStatus::Fresh,
        next_action: "Ready for exposure".to_owned(),
    }
}
