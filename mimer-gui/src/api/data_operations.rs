use crate::api::safety::mask_secret_fragments;
use crate::domain::{
    AlertSeverity, AnalysisSubject, BackendSectionStatus, ConstituentReadinessRow,
    DataDiagnosticIssue, DataOperationStatus, DataOperationsHydration, DataOperationsOrigin,
    DataOperationsSectionFailure, DataOperationsSnapshot, JobRun, JobStatus, MarketDataPlanItem,
    ReadinessStage, ScheduledJob, SourceBudget, SourceFetchLog,
};
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashSet;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ApiEndpoint {
    SchedulerStatus,
    SchedulerDueJobs,
    SourceBudgets,
    SourceFetchLogs,
    MarketDataPlan,
    Dashboard,
    Diagnostics,
    ConstituentExposure,
    JobTimeline,
    RunningJobs,
    JobFailures,
    OnboardingStatus,
    OnboardingRuns,
    BrokerImports,
    Transactions,
    Positions,
}

impl ApiEndpoint {
    pub const ALL: [Self; 16] = [
        Self::SchedulerStatus,
        Self::SchedulerDueJobs,
        Self::SourceBudgets,
        Self::SourceFetchLogs,
        Self::MarketDataPlan,
        Self::Dashboard,
        Self::Diagnostics,
        Self::ConstituentExposure,
        Self::JobTimeline,
        Self::RunningJobs,
        Self::JobFailures,
        Self::OnboardingStatus,
        Self::OnboardingRuns,
        Self::BrokerImports,
        Self::Transactions,
        Self::Positions,
    ];

    pub fn key(self) -> &'static str {
        match self {
            Self::SchedulerStatus => "scheduler_status",
            Self::SchedulerDueJobs => "scheduler_due_jobs",
            Self::SourceBudgets => "source_budgets",
            Self::SourceFetchLogs => "source_fetch_logs",
            Self::MarketDataPlan => "market_data_plan",
            Self::Dashboard => "dashboard_readiness",
            Self::Diagnostics => "diagnostics",
            Self::ConstituentExposure => "constituent_exposure",
            Self::JobTimeline => "job_timeline",
            Self::RunningJobs => "running_jobs",
            Self::JobFailures => "job_failures",
            Self::OnboardingStatus => "onboarding_status",
            Self::OnboardingRuns => "onboarding_runs",
            Self::BrokerImports => "broker_imports",
            Self::Transactions => "transactions",
            Self::Positions => "positions",
        }
    }

    pub fn ordinal(self) -> usize {
        Self::ALL
            .iter()
            .position(|candidate| *candidate == self)
            .unwrap_or(usize::MAX)
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::SchedulerStatus => "Scheduler status",
            Self::SchedulerDueJobs => "Scheduler due jobs",
            Self::SourceBudgets => "Source budgets",
            Self::SourceFetchLogs => "Source fetch logs",
            Self::MarketDataPlan => "Market-data plan",
            Self::Dashboard => "Dashboard readiness",
            Self::Diagnostics => "Diagnostics",
            Self::ConstituentExposure => "Constituent exposure",
            Self::JobTimeline => "Job timeline",
            Self::RunningJobs => "Running jobs",
            Self::JobFailures => "Job failures",
            Self::OnboardingStatus => "Onboarding status",
            Self::OnboardingRuns => "Onboarding runs",
            Self::BrokerImports => "Broker imports",
            Self::Transactions => "Transactions",
            Self::Positions => "Positions",
        }
    }

    pub fn path(self, workspace_id: &str) -> String {
        let workspace_id = encode_path_segment(workspace_id);
        match self {
            Self::SchedulerStatus => "/scheduler/status".to_owned(),
            Self::SchedulerDueJobs => "/scheduler/due-jobs".to_owned(),
            Self::SourceBudgets => "/source-budgets".to_owned(),
            Self::SourceFetchLogs => "/source-fetch-logs?limit=50".to_owned(),
            Self::MarketDataPlan => {
                format!("/workspaces/{workspace_id}/market-data-plan?include_constituents=true")
            }
            Self::Dashboard => format!("/workspaces/{workspace_id}/dashboard"),
            Self::Diagnostics => format!("/workspaces/{workspace_id}/diagnostics"),
            Self::ConstituentExposure => {
                format!("/workspaces/{workspace_id}/exposure?dimension=constituent")
            }
            Self::JobTimeline => {
                format!("/workspaces/{workspace_id}/jobs/timeline?include_running=true&limit=50")
            }
            Self::RunningJobs => format!("/workspaces/{workspace_id}/jobs/running?limit=50"),
            Self::JobFailures => format!("/workspaces/{workspace_id}/jobs/failures?limit=25"),
            Self::OnboardingStatus => {
                format!("/workspaces/{workspace_id}/onboarding/status")
            }
            Self::OnboardingRuns => {
                format!("/workspaces/{workspace_id}/onboarding/runs?limit=10")
            }
            Self::BrokerImports => format!("/workspaces/{workspace_id}/broker-imports"),
            Self::Transactions => format!("/workspaces/{workspace_id}/transactions"),
            Self::Positions => format!("/workspaces/{workspace_id}/positions"),
        }
    }
}

#[derive(Clone, Debug)]
pub struct ApiEndpointResponse {
    pub endpoint: ApiEndpoint,
    pub result: Result<Value, String>,
}

#[derive(Clone, Debug)]
pub struct DataOperationsApiLoad {
    pub operations: DataOperationsSnapshot,
    pub scheduled_jobs: Vec<ScheduledJob>,
    pub job_runs: Vec<JobRun>,
}

pub fn hydrate_data_operations(
    fallback_operations: &DataOperationsSnapshot,
    fallback_scheduled_jobs: &[ScheduledJob],
    fallback_job_runs: &[JobRun],
    base_url: &str,
    refreshed_at: &str,
    responses: Vec<ApiEndpointResponse>,
) -> DataOperationsApiLoad {
    let mut operations = fallback_operations.clone();
    let mut scheduled_jobs = fallback_scheduled_jobs.to_vec();
    let mut job_runs = fallback_job_runs.to_vec();
    let mut failures = Vec::new();
    let mut backend_sections = Vec::new();
    let mut successful_sections = 0usize;

    for response in responses {
        match response.result {
            Ok(value) => {
                let mapped = apply_endpoint_payload(
                    response.endpoint,
                    &value,
                    &mut operations,
                    &mut scheduled_jobs,
                    &mut job_runs,
                );
                match mapped {
                    Ok(record_count) => {
                        successful_sections += 1;
                        backend_sections.push(BackendSectionStatus {
                            key: response.endpoint.key().to_owned(),
                            label: response.endpoint.label().to_owned(),
                            status: DataOperationStatus::Ready,
                            record_count,
                            detail: section_success_detail(record_count),
                            source: "api".to_owned(),
                        });
                    }
                    Err(message) => {
                        let message = mask_secret_fragments(&message);
                        failures.push(DataOperationsSectionFailure {
                            section: response.endpoint.label().to_owned(),
                            message: message.clone(),
                        });
                        backend_sections.push(BackendSectionStatus {
                            key: response.endpoint.key().to_owned(),
                            label: response.endpoint.label().to_owned(),
                            status: DataOperationStatus::Partial,
                            record_count: record_count(&value),
                            detail: message,
                            source: "api".to_owned(),
                        });
                    }
                }
            }
            Err(message) => {
                let message = mask_secret_fragments(&message);
                failures.push(DataOperationsSectionFailure {
                    section: response.endpoint.label().to_owned(),
                    message: message.clone(),
                });
                backend_sections.push(BackendSectionStatus {
                    key: response.endpoint.key().to_owned(),
                    label: response.endpoint.label().to_owned(),
                    status: DataOperationStatus::Failed,
                    record_count: None,
                    detail: message,
                    source: "api".to_owned(),
                });
            }
        }
    }

    let origin = if successful_sections == 0 {
        match fallback_operations.hydration.origin {
            DataOperationsOrigin::Api
            | DataOperationsOrigin::PartialApi
            | DataOperationsOrigin::StaleApi => DataOperationsOrigin::StaleApi,
            DataOperationsOrigin::Mock | DataOperationsOrigin::ApiError => {
                DataOperationsOrigin::ApiError
            }
        }
    } else if failures.is_empty() {
        DataOperationsOrigin::Api
    } else {
        DataOperationsOrigin::PartialApi
    };

    let last_error = failures
        .first()
        .map(|failure| format!("{}: {}", failure.section, failure.message));
    let previous_refresh = fallback_operations.hydration.refreshed_at.clone();
    operations.backend_sections = backend_sections;
    operations.hydration = DataOperationsHydration {
        origin,
        refreshed_at: if successful_sections == 0 {
            previous_refresh
        } else {
            Some(refreshed_at.to_owned())
        },
        base_url: Some(mask_secret_fragments(base_url)),
        failed_sections: failures,
        last_error,
    };

    DataOperationsApiLoad {
        operations,
        scheduled_jobs,
        job_runs,
    }
}

fn apply_endpoint_payload(
    endpoint: ApiEndpoint,
    value: &Value,
    operations: &mut DataOperationsSnapshot,
    scheduled_jobs: &mut Vec<ScheduledJob>,
    job_runs: &mut Vec<JobRun>,
) -> Result<Option<usize>, String> {
    match endpoint {
        ApiEndpoint::Dashboard => {
            let rows = deserialize_collection::<ApiReadinessStage>(
                value,
                &["readiness_stages", "readiness", "stages", "items", "data"],
            )?;
            if rows.is_empty() {
                return Err("dashboard payload contained no readiness stages".to_owned());
            }
            operations.readiness_stages = rows.into_iter().map(ReadinessStage::from).collect();
            Ok(Some(operations.readiness_stages.len()))
        }
        ApiEndpoint::MarketDataPlan => {
            let rows = deserialize_collection::<ApiMarketDataPlanItem>(
                value,
                &["items", "plan", "market_data_plan", "actions", "data"],
            )?;
            operations.market_data_plan = rows.into_iter().map(MarketDataPlanItem::from).collect();
            Ok(Some(operations.market_data_plan.len()))
        }
        ApiEndpoint::SourceBudgets => {
            let rows = deserialize_collection::<ApiSourceBudget>(
                value,
                &["items", "budgets", "source_budgets", "data"],
            )?;
            operations.source_budgets = rows.into_iter().map(SourceBudget::from).collect();
            Ok(Some(operations.source_budgets.len()))
        }
        ApiEndpoint::SourceFetchLogs => {
            let rows = deserialize_collection::<ApiSourceFetchLog>(
                value,
                &["items", "logs", "fetch_logs", "source_fetch_logs", "data"],
            )?;
            operations.fetch_logs = rows.into_iter().map(SourceFetchLog::from).collect();
            Ok(Some(operations.fetch_logs.len()))
        }
        ApiEndpoint::Diagnostics => {
            let rows = deserialize_collection::<ApiDiagnosticIssue>(
                value,
                &["items", "issues", "diagnostics", "data"],
            )?;
            operations.diagnostic_issues =
                rows.into_iter().map(DataDiagnosticIssue::from).collect();
            Ok(Some(operations.diagnostic_issues.len()))
        }
        ApiEndpoint::ConstituentExposure => {
            let rows = deserialize_collection::<ApiConstituentReadiness>(
                value,
                &[
                    "items",
                    "constituents",
                    "holdings",
                    "exposures",
                    "rows",
                    "data",
                ],
            )?;
            operations.constituent_coverage = rows
                .into_iter()
                .map(ConstituentReadinessRow::from)
                .collect();
            Ok(Some(operations.constituent_coverage.len()))
        }
        ApiEndpoint::SchedulerDueJobs => {
            let rows = deserialize_collection::<ApiScheduledJob>(
                value,
                &["items", "jobs", "due_jobs", "scheduled_jobs", "data"],
            )?;
            *scheduled_jobs = rows.into_iter().map(ScheduledJob::from).collect();
            Ok(Some(scheduled_jobs.len()))
        }
        ApiEndpoint::JobTimeline | ApiEndpoint::RunningJobs | ApiEndpoint::JobFailures => {
            let rows = deserialize_collection::<ApiJobRun>(
                value,
                &["items", "runs", "jobs", "timeline", "data"],
            )?;
            merge_job_runs(job_runs, rows.into_iter().map(JobRun::from));
            Ok(record_count(value).or(Some(0)))
        }
        ApiEndpoint::SchedulerStatus
        | ApiEndpoint::OnboardingStatus
        | ApiEndpoint::OnboardingRuns
        | ApiEndpoint::BrokerImports
        | ApiEndpoint::Transactions
        | ApiEndpoint::Positions => Ok(record_count(value)),
    }
}

fn deserialize_collection<T>(value: &Value, keys: &[&str]) -> Result<Vec<T>, String>
where
    T: for<'de> Deserialize<'de>,
{
    let collection = collection_value(value, keys);
    serde_json::from_value::<Vec<T>>(collection)
        .map_err(|err| format!("could not map response payload: {err}"))
}

fn collection_value(value: &Value, keys: &[&str]) -> Value {
    if value.is_array() {
        return value.clone();
    }
    for key in keys {
        if let Some(candidate) = value.get(*key) {
            if candidate.is_array() {
                return candidate.clone();
            }
            if let Some(nested) = candidate.get("items").filter(|nested| nested.is_array()) {
                return nested.clone();
            }
        }
    }
    Value::Array(Vec::new())
}

fn record_count(value: &Value) -> Option<usize> {
    if let Some(items) = value.as_array() {
        return Some(items.len());
    }
    for key in [
        "items",
        "data",
        "results",
        "runs",
        "jobs",
        "issues",
        "positions",
        "transactions",
        "imports",
    ] {
        if let Some(items) = value.get(key).and_then(Value::as_array) {
            return Some(items.len());
        }
    }
    value
        .get("count")
        .or_else(|| value.get("total"))
        .and_then(Value::as_u64)
        .and_then(|count| usize::try_from(count).ok())
}

fn section_success_detail(record_count: Option<usize>) -> String {
    record_count
        .map(|count| format!("{count} records"))
        .unwrap_or_else(|| "endpoint available".to_owned())
}

fn merge_job_runs(existing: &mut Vec<JobRun>, incoming: impl IntoIterator<Item = JobRun>) {
    let mut seen = existing
        .iter()
        .map(|run| run.id.clone())
        .collect::<HashSet<_>>();
    for run in incoming {
        if seen.insert(run.id.clone()) {
            existing.push(run);
        } else if let Some(current) = existing.iter_mut().find(|current| current.id == run.id) {
            *current = run;
        }
    }
    existing.sort_by(|left, right| right.started.cmp(&left.started));
}

fn encode_path_segment(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

pub fn endpoint_url(base_url: &str, path: &str) -> String {
    format!(
        "{}/{}",
        base_url.trim_end_matches('/'),
        path.trim_start_matches('/')
    )
}

pub fn parse_operation_status(value: &str) -> DataOperationStatus {
    match normalize_status(value).as_str() {
        "ok" | "success" | "succeeded" | "healthy" => DataOperationStatus::Ok,
        "ready" | "complete" | "completed" | "done" => DataOperationStatus::Ready,
        "partial" | "degraded" => DataOperationStatus::Partial,
        "missing" | "not_found" | "unavailable" => DataOperationStatus::Missing,
        "stale" | "expired" => DataOperationStatus::Stale,
        "failed" | "error" => DataOperationStatus::Failed,
        "blocked" => DataOperationStatus::Blocked,
        "mock" | "fixture" | "seed" => DataOperationStatus::Mock,
        "running" | "leased" | "in_progress" => DataOperationStatus::Running,
        "needed" | "due" | "action_required" => DataOperationStatus::Needed,
        "fresh" | "current" => DataOperationStatus::Fresh,
        "ambiguous" | "multiple_matches" => DataOperationStatus::Ambiguous,
        "budget_blocked" | "rate_limited" | "backoff" => DataOperationStatus::BudgetBlocked,
        "planned" | "queued" | "pending" => DataOperationStatus::Planned,
        _ => DataOperationStatus::Unknown,
    }
}

pub fn parse_job_status(value: &str) -> JobStatus {
    match normalize_status(value).as_str() {
        "queued" | "pending" | "planned" => JobStatus::Queued,
        "running" | "leased" | "in_progress" => JobStatus::Running,
        "succeeded" | "success" | "done" | "complete" | "completed" => JobStatus::Succeeded,
        "failed" | "error" | "cancelled" | "canceled" => JobStatus::Failed,
        _ => JobStatus::Unknown,
    }
}

fn normalize_status(value: &str) -> String {
    value
        .trim()
        .to_ascii_lowercase()
        .replace([' ', '-', '/'], "_")
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ApiReadinessStage {
    #[serde(default, alias = "stage_key")]
    key: String,
    #[serde(default, alias = "name", alias = "stage")]
    label: String,
    #[serde(default)]
    status: String,
    #[serde(default, alias = "coverage", alias = "coverage_percent")]
    coverage_pct: Option<f32>,
    #[serde(default, alias = "count", alias = "summary")]
    count_label: String,
    #[serde(default, alias = "message", alias = "reason")]
    detail: String,
    #[serde(default, alias = "provider")]
    source: String,
    #[serde(default, alias = "as_of", alias = "updated_at")]
    freshness: String,
    #[serde(default, alias = "next_action", alias = "action")]
    recommended_action: String,
}

impl From<ApiReadinessStage> for ReadinessStage {
    fn from(value: ApiReadinessStage) -> Self {
        let label = non_empty(value.label, &value.key);
        Self {
            key: non_empty(value.key, &label).to_ascii_lowercase(),
            label,
            status: parse_operation_status(&value.status),
            coverage_pct: value.coverage_pct,
            count_label: value.count_label,
            detail: value.detail,
            source: non_empty(value.source, "api"),
            freshness: value.freshness,
            recommended_action: value.recommended_action,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ApiMarketDataPlanItem {
    #[serde(default, alias = "plan_item_id")]
    id: String,
    #[serde(default)]
    priority: u8,
    #[serde(default, alias = "type", alias = "kind", alias = "action_type")]
    item_type: String,
    #[serde(default, alias = "subject", alias = "target", alias = "label")]
    subject_label: String,
    #[serde(default)]
    fund_id: Option<String>,
    #[serde(default)]
    listing_id: Option<String>,
    #[serde(default)]
    ticker: Option<String>,
    #[serde(default, alias = "detail", alias = "message")]
    reason: String,
    #[serde(default, alias = "provider")]
    source: String,
    #[serde(default, alias = "request_count", alias = "estimated_request_count")]
    estimated_requests: u32,
    #[serde(default)]
    status: String,
    #[serde(default, alias = "blocked_reason", alias = "error")]
    blocker: Option<String>,
    #[serde(default, alias = "action", alias = "recommended_action")]
    next_action: String,
}

impl From<ApiMarketDataPlanItem> for MarketDataPlanItem {
    fn from(value: ApiMarketDataPlanItem) -> Self {
        let subject = match (value.fund_id.clone(), value.listing_id.clone()) {
            (Some(fund_id), Some(listing_id)) => Some(AnalysisSubject::FundListing {
                fund_id,
                listing_id,
            }),
            (Some(fund_id), None) => Some(AnalysisSubject::Fund(fund_id)),
            (None, None) => value.ticker.clone().map(|ticker| AnalysisSubject::Holding {
                ticker,
                source: value.source.clone(),
            }),
            (None, Some(_)) => None,
        };
        let subject_label = value
            .subject_label
            .trim()
            .is_empty()
            .then(|| value.ticker.clone())
            .flatten()
            .unwrap_or(value.subject_label);
        Self {
            id: non_empty(value.id, &format!("api-plan-{}", value.priority)),
            priority: value.priority,
            item_type: value.item_type,
            subject_label,
            subject,
            reason: value.reason,
            source: non_empty(value.source, "api"),
            estimated_requests: value.estimated_requests,
            status: parse_operation_status(&value.status),
            blocker: value.blocker.filter(|blocker| !blocker.trim().is_empty()),
            next_action: value.next_action,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ApiSourceBudget {
    #[serde(default, alias = "source_name", alias = "name")]
    source: String,
    #[serde(default = "default_true", alias = "is_enabled")]
    enabled: bool,
    #[serde(default)]
    status: String,
    #[serde(default, alias = "used")]
    requests_used: u32,
    #[serde(default, alias = "limit")]
    requests_limit: u32,
    #[serde(default, alias = "window_label", alias = "period")]
    window: String,
    #[serde(default, alias = "minimum_delay")]
    min_delay: String,
    #[serde(default, alias = "backoff_until_at")]
    backoff_until: Option<String>,
    #[serde(default, alias = "failure_count")]
    recent_failures: u32,
    #[serde(default, alias = "cache_hit_count")]
    cache_hits: u32,
    #[serde(default, alias = "next_allowed_at")]
    next_allowed: String,
    #[serde(default)]
    capabilities: Vec<String>,
}

impl From<ApiSourceBudget> for SourceBudget {
    fn from(value: ApiSourceBudget) -> Self {
        Self {
            source: non_empty(value.source, "unknown"),
            enabled: value.enabled,
            status: parse_operation_status(&value.status),
            requests_used: value.requests_used,
            requests_limit: value.requests_limit,
            window: value.window,
            min_delay: value.min_delay,
            backoff_until: value.backoff_until,
            recent_failures: value.recent_failures,
            cache_hits: value.cache_hits,
            next_allowed: value.next_allowed,
            capabilities: value.capabilities,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ApiSourceFetchLog {
    #[serde(default, alias = "fetch_log_id", alias = "run_id")]
    id: String,
    #[serde(
        default,
        alias = "created_at",
        alias = "started_at",
        alias = "timestamp"
    )]
    time: String,
    #[serde(default, alias = "source_name", alias = "provider")]
    source: String,
    #[serde(default, alias = "kind", alias = "request_type")]
    request_kind: String,
    #[serde(default, alias = "url", alias = "request", alias = "key")]
    request_key: String,
    #[serde(default)]
    status: String,
    #[serde(default, alias = "status_code")]
    http_status: Option<u16>,
    #[serde(default, alias = "elapsed_ms")]
    duration_ms: u32,
    #[serde(default, alias = "from_cache")]
    cache_hit: bool,
    #[serde(default, alias = "is_rate_limited")]
    rate_limited: bool,
    #[serde(default, alias = "error_message", alias = "message")]
    error: Option<String>,
}

impl From<ApiSourceFetchLog> for SourceFetchLog {
    fn from(value: ApiSourceFetchLog) -> Self {
        Self {
            id: value.id,
            time: value.time,
            source: non_empty(value.source, "unknown"),
            request_kind: value.request_kind,
            request_key: mask_secret_fragments(&value.request_key),
            status: parse_operation_status(&value.status),
            http_status: value.http_status,
            duration_ms: value.duration_ms,
            cache_hit: value.cache_hit,
            rate_limited: value.rate_limited,
            error: value.error.map(|error| mask_secret_fragments(&error)),
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ApiDiagnosticIssue {
    #[serde(default, alias = "issue_id")]
    id: String,
    #[serde(default)]
    severity: String,
    #[serde(default, alias = "name")]
    title: String,
    #[serde(default, alias = "message", alias = "reason")]
    detail: String,
    #[serde(default)]
    status: String,
    #[serde(default, alias = "provider")]
    source: String,
    #[serde(default, alias = "action", alias = "next_action")]
    recommended_action: String,
    #[serde(default, alias = "page", alias = "section")]
    related_page: String,
}

impl From<ApiDiagnosticIssue> for DataDiagnosticIssue {
    fn from(value: ApiDiagnosticIssue) -> Self {
        Self {
            id: value.id,
            severity: match normalize_status(&value.severity).as_str() {
                "critical" | "error" | "high" => AlertSeverity::Critical,
                "warning" | "warn" | "medium" => AlertSeverity::Warning,
                _ => AlertSeverity::Info,
            },
            title: value.title,
            detail: mask_secret_fragments(&value.detail),
            status: parse_operation_status(&value.status),
            source: non_empty(value.source, "api"),
            recommended_action: value.recommended_action,
            related_page: value.related_page,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ApiConstituentReadiness {
    #[serde(default, alias = "fund_symbol", alias = "parent_ticker")]
    fund_ticker: String,
    #[serde(default, alias = "name", alias = "company")]
    holding_name: String,
    #[serde(default, alias = "ticker", alias = "symbol")]
    holding_ticker: String,
    #[serde(default, alias = "weight", alias = "portfolio_weight_pct")]
    weight_pct: f64,
    #[serde(default)]
    identity_status: String,
    #[serde(default)]
    instrument_id: Option<String>,
    #[serde(default, alias = "instrument_listing_id")]
    listing_id: Option<String>,
    #[serde(default, alias = "price", alias = "close")]
    latest_price: Option<f64>,
    #[serde(default, alias = "as_of_date", alias = "latest_price_date")]
    price_date: Option<String>,
    #[serde(default, alias = "source", alias = "provider")]
    price_source: String,
    #[serde(default, alias = "status")]
    price_status: String,
    #[serde(default, alias = "action", alias = "recommended_action")]
    next_action: String,
}

impl From<ApiConstituentReadiness> for ConstituentReadinessRow {
    fn from(value: ApiConstituentReadiness) -> Self {
        let holding_ticker = value.holding_ticker;
        let fund_ticker = value.fund_ticker;
        Self {
            fund_ticker: fund_ticker.clone(),
            holding_name: value.holding_name,
            holding_ticker: holding_ticker.clone(),
            weight_pct: value.weight_pct,
            subject: AnalysisSubject::Holding {
                ticker: holding_ticker,
                source: fund_ticker,
            },
            identity_status: parse_operation_status(&value.identity_status),
            instrument_id: value.instrument_id,
            listing_id: value.listing_id,
            latest_price: value.latest_price,
            price_date: value.price_date,
            price_source: non_empty(value.price_source, "api"),
            price_status: parse_operation_status(&value.price_status),
            next_action: value.next_action,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ApiScheduledJob {
    #[serde(default, alias = "job_name", alias = "id")]
    name: String,
    #[serde(default, alias = "type", alias = "kind")]
    job_type: String,
    #[serde(default, alias = "provider")]
    source: String,
    #[serde(default, alias = "cron", alias = "schedule")]
    cron_schedule: String,
    #[serde(default = "default_true", alias = "enabled", alias = "is_active")]
    active: bool,
    #[serde(default, alias = "last_run_at")]
    last_run: String,
    #[serde(default, alias = "next_run_at", alias = "due_at")]
    next_run: String,
}

impl From<ApiScheduledJob> for ScheduledJob {
    fn from(value: ApiScheduledJob) -> Self {
        Self {
            name: non_empty(value.name, &value.job_type),
            job_type: value.job_type,
            source: non_empty(value.source, "api"),
            cron_schedule: value.cron_schedule,
            active: value.active,
            last_run: value.last_run,
            next_run: value.next_run,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ApiJobRun {
    #[serde(default, alias = "run_id", alias = "job_run_id")]
    id: String,
    #[serde(default, alias = "type", alias = "job_name", alias = "kind")]
    job_type: String,
    #[serde(default, alias = "provider")]
    source: String,
    #[serde(default)]
    status: String,
    #[serde(default, alias = "started_at", alias = "leased_at")]
    started: String,
    #[serde(default, alias = "finished_at", alias = "completed_at")]
    finished: Option<String>,
    #[serde(default, alias = "inserted_count")]
    inserted: u32,
    #[serde(default, alias = "updated_count")]
    updated: u32,
    #[serde(default, alias = "failed_count", alias = "error_count")]
    failed: u32,
    #[serde(default, alias = "detail", alias = "error")]
    message: String,
}

impl From<ApiJobRun> for JobRun {
    fn from(value: ApiJobRun) -> Self {
        Self {
            id: value.id,
            job_type: value.job_type,
            source: non_empty(value.source, "api"),
            status: parse_job_status(&value.status),
            started: value.started,
            finished: value.finished,
            inserted: value.inserted,
            updated: value.updated,
            failed: value.failed,
            message: mask_secret_fragments(&value.message),
        }
    }
}

fn non_empty(value: String, fallback: &str) -> String {
    if value.trim().is_empty() {
        fallback.to_owned()
    } else {
        value
    }
}

fn default_true() -> bool {
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(endpoint: ApiEndpoint, json: &str) -> ApiEndpointResponse {
        ApiEndpointResponse {
            endpoint,
            result: serde_json::from_str(json).map_err(|err| err.to_string()),
        }
    }

    #[test]
    fn builds_joined_and_encoded_endpoint_urls() {
        assert_eq!(
            endpoint_url(
                "http://localhost:8080/api/v1/",
                &ApiEndpoint::Dashboard.path("workspace main")
            ),
            "http://localhost:8080/api/v1/workspaces/workspace%20main/dashboard"
        );
    }

    #[test]
    fn maps_unknown_status_strings_without_panicking() {
        assert_eq!(
            parse_operation_status("new-backend-state"),
            DataOperationStatus::Unknown
        );
        assert_eq!(parse_job_status("paused"), JobStatus::Unknown);
    }

    #[test]
    fn maps_partial_api_payload_and_retains_failed_fallback_sections() {
        let fallback = DataOperationsSnapshot {
            source_budgets: vec![SourceBudget {
                source: "mock-source".to_owned(),
                enabled: true,
                status: DataOperationStatus::Mock,
                requests_used: 0,
                requests_limit: 0,
                window: "fixture".to_owned(),
                min_delay: "0s".to_owned(),
                backoff_until: None,
                recent_failures: 0,
                cache_hits: 0,
                next_allowed: "now".to_owned(),
                capabilities: Vec::new(),
            }],
            ..Default::default()
        };
        let responses = vec![
            ok(
                ApiEndpoint::MarketDataPlan,
                r#"{"items":[{"id":"plan-1","priority":1,"type":"fetch_price","subject":"MSFT","status":"needed"}]}"#,
            ),
            ApiEndpointResponse {
                endpoint: ApiEndpoint::SourceBudgets,
                result: Err("HTTP 404 token=secret".to_owned()),
            },
        ];

        let loaded = hydrate_data_operations(
            &fallback,
            &[],
            &[],
            "http://localhost:8080/api/v1",
            "2026-06-23 12:00:00 UTC",
            responses,
        );

        assert_eq!(
            loaded.operations.hydration.origin,
            DataOperationsOrigin::PartialApi
        );
        assert_eq!(loaded.operations.market_data_plan.len(), 1);
        assert_eq!(loaded.operations.source_budgets[0].source, "mock-source");
        assert!(
            loaded.operations.hydration.failed_sections[0]
                .message
                .contains("token=***")
        );
    }

    #[test]
    fn missing_optional_fields_are_tolerated() {
        let loaded = hydrate_data_operations(
            &DataOperationsSnapshot::default(),
            &[],
            &[],
            "http://localhost:8080/api/v1",
            "2026-06-23 12:00:00 UTC",
            vec![ok(
                ApiEndpoint::SourceFetchLogs,
                r#"[{"id":"fetch-1","status":"done","request_key":"x?apikey=secret"}]"#,
            )],
        );

        assert_eq!(loaded.operations.fetch_logs.len(), 1);
        assert_eq!(
            loaded.operations.fetch_logs[0].status,
            DataOperationStatus::Ready
        );
        assert!(
            loaded.operations.fetch_logs[0]
                .request_key
                .contains("apikey=***")
        );
    }

    #[test]
    fn maps_job_timeline_and_running_rows() {
        let loaded = hydrate_data_operations(
            &DataOperationsSnapshot::default(),
            &[],
            &[],
            "http://localhost:8080/api/v1",
            "2026-06-23 12:00:00 UTC",
            vec![
                ok(
                    ApiEndpoint::JobTimeline,
                    r#"{"runs":[{"run_id":"run-1","job_name":"prices","status":"succeeded","started_at":"2026-06-23T10:00:00Z"}]}"#,
                ),
                ok(
                    ApiEndpoint::RunningJobs,
                    r#"[{"run_id":"run-2","job_name":"holdings","status":"leased","started_at":"2026-06-23T11:00:00Z"}]"#,
                ),
            ],
        );

        assert_eq!(loaded.job_runs.len(), 2);
        assert!(
            loaded
                .job_runs
                .iter()
                .any(|run| run.status == JobStatus::Running)
        );
    }
}
