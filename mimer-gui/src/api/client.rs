use crate::api::data_operations::{
    ApiEndpoint, ApiEndpointResponse, DataOperationsApiLoad, endpoint_url, hydrate_data_operations,
};
use crate::api::safety::{contains_secret_material, mask_secret_fragments};
use crate::api::types::{ApiConfig, AuthMode, DataMode};
use crate::app_model::DashboardSnapshot;
use crate::domain::{
    Alert, DataOperationsHydration, DataOperationsOrigin, DataOperationsSectionFailure,
    DataOperationsSnapshot, Distribution, DocumentSnapshot, ExposureBreakdown, Fund,
    HoldingExposure, InstrumentResolutionRequest, InstrumentResolutionResult, JobRun,
    PortfolioSummary, Position, ScheduledJob, Workspace,
};
use crate::timeseries::TimeSeries;
use serde_json::Value;
use std::io::Read;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const MAX_RESPONSE_BYTES: u64 = 4 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct DataOperationsLoad {
    pub operations: DataOperationsSnapshot,
    pub scheduled_jobs: Option<Vec<ScheduledJob>>,
    pub job_runs: Option<Vec<JobRun>>,
}

impl DataOperationsLoad {
    pub fn mock(operations: DataOperationsSnapshot) -> Self {
        Self {
            operations,
            scheduled_jobs: None,
            job_runs: None,
        }
    }
}

pub trait DataProvider {
    fn data_mode(&self) -> DataMode {
        DataMode::Mock
    }

    fn load_workspaces(&self) -> Vec<Workspace>;
    fn load_portfolio_summary(&self, workspace_id: &str) -> PortfolioSummary;
    fn load_positions(&self, workspace_id: &str) -> Vec<Position>;
    fn load_funds(&self) -> Vec<Fund>;
    fn load_distributions(&self) -> Vec<Distribution>;
    fn load_holdings(&self) -> Vec<HoldingExposure>;
    fn load_exposure(&self, workspace_id: &str) -> ExposureBreakdown;
    fn load_alerts(&self, workspace_id: &str) -> Vec<Alert>;
    fn load_documents(&self) -> Vec<DocumentSnapshot>;
    fn load_jobs(&self) -> (Vec<ScheduledJob>, Vec<JobRun>);
    fn load_data_operations(&self, workspace_id: &str) -> DataOperationsLoad;
    fn load_time_series(&self, workspace_id: &str) -> Vec<TimeSeries>;
    fn resolve_instrument(
        &self,
        request: &InstrumentResolutionRequest,
    ) -> InstrumentResolutionResult;
}

#[derive(Clone, Debug)]
#[allow(dead_code)]
pub enum DataRequest {
    LoadDashboard { workspace_id: String },
    RefreshAll { workspace_id: String },
    ResolveInstrument(InstrumentResolutionRequest),
    RunJob { job_name: String },
    LoadFundDetail { fund_id: String },
}

#[derive(Clone, Debug)]
#[allow(dead_code)]
pub enum DataResponse {
    DashboardLoaded(Box<DashboardSnapshot>),
    InstrumentResolved(Box<InstrumentResolutionResult>),
    JobRunUpdated(JobRun),
    Error { request: String, message: String },
}

#[derive(Clone, Debug)]
pub struct ApiDataProvider {
    config: ApiConfig,
    agent: ureq::Agent,
}

impl ApiDataProvider {
    pub fn new(mut config: ApiConfig) -> Result<Self, String> {
        config.base_url = normalize_backend_base_url(&config.base_url)?;
        if !(100..=120_000).contains(&config.timeout_ms) {
            return Err("API timeout must be between 100 and 120000 ms".to_owned());
        }
        if config.auth_mode == AuthMode::BearerTokenFuture {
            return Err("bearer-token auth is not implemented in this GUI".to_owned());
        }

        let config_builder = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_millis(config.timeout_ms)))
            .build();
        let agent: ureq::Agent = config_builder.into();
        Ok(Self { config, agent })
    }

    pub fn load_data_operations(
        &self,
        workspace_id: &str,
        fallback_operations: &DataOperationsSnapshot,
        fallback_scheduled_jobs: &[ScheduledJob],
        fallback_job_runs: &[JobRun],
    ) -> DataOperationsApiLoad {
        let refreshed_at = current_utc_timestamp();
        let responses = self.fetch_all(workspace_id);
        hydrate_data_operations(
            fallback_operations,
            fallback_scheduled_jobs,
            fallback_job_runs,
            &self.config.base_url,
            &refreshed_at,
            responses,
        )
    }

    pub fn test_connection(&self) -> Result<String, String> {
        let endpoint = ApiEndpoint::SchedulerStatus;
        self.fetch(endpoint, "connection-test")
            .map(|_| format!("{} reachable", self.config.base_url))
    }

    fn fetch_all(&self, workspace_id: &str) -> Vec<ApiEndpointResponse> {
        thread::scope(|scope| {
            let (sender, receiver) = mpsc::channel();
            for endpoint in ApiEndpoint::ALL {
                let sender = sender.clone();
                let provider = self.clone();
                let workspace_id = workspace_id.to_owned();
                scope.spawn(move || {
                    let response = ApiEndpointResponse {
                        endpoint,
                        result: provider.fetch(endpoint, &workspace_id),
                    };
                    let _ = sender.send(response);
                });
            }
            drop(sender);
            let mut responses = receiver.into_iter().collect::<Vec<_>>();
            responses.sort_by_key(|response| response.endpoint.ordinal());
            responses
        })
    }

    fn fetch(&self, endpoint: ApiEndpoint, workspace_id: &str) -> Result<Value, String> {
        let url = endpoint_url(&self.config.base_url, &endpoint.path(workspace_id));
        let mut request = self.agent.get(&url).header("Accept", "application/json");
        if self.config.auth_mode == AuthMode::DevHeader {
            request = request.header("X-Workspace-ID", self.config.workspace_header_value.trim());
        }

        let mut response = request
            .call()
            .map_err(|err| sanitize_http_error(endpoint, err))?;
        let mut body = String::new();
        response
            .body_mut()
            .as_reader()
            .take(MAX_RESPONSE_BYTES)
            .read_to_string(&mut body)
            .map_err(|err| {
                format!(
                    "{} response could not be read: {}",
                    endpoint.label(),
                    mask_secret_fragments(&err.to_string())
                )
            })?;
        serde_json::from_str(&body).map_err(|err| {
            format!(
                "{} returned invalid JSON: {}",
                endpoint.label(),
                mask_secret_fragments(&err.to_string())
            )
        })
    }
}

#[derive(Clone, Debug)]
pub struct ConfiguredDataProvider<P> {
    fallback: P,
    mode: DataMode,
    api_config: ApiConfig,
    fallback_operations: DataOperationsSnapshot,
    fallback_scheduled_jobs: Vec<ScheduledJob>,
    fallback_job_runs: Vec<JobRun>,
}

impl<P> ConfiguredDataProvider<P> {
    pub fn new(
        fallback: P,
        mode: DataMode,
        api_config: ApiConfig,
        fallback_operations: DataOperationsSnapshot,
        fallback_scheduled_jobs: Vec<ScheduledJob>,
        fallback_job_runs: Vec<JobRun>,
    ) -> Self {
        Self {
            fallback,
            mode,
            api_config,
            fallback_operations,
            fallback_scheduled_jobs,
            fallback_job_runs,
        }
    }
}

impl<P> DataProvider for ConfiguredDataProvider<P>
where
    P: DataProvider,
{
    fn data_mode(&self) -> DataMode {
        self.mode
    }

    fn load_workspaces(&self) -> Vec<Workspace> {
        self.fallback.load_workspaces()
    }

    fn load_portfolio_summary(&self, workspace_id: &str) -> PortfolioSummary {
        self.fallback.load_portfolio_summary(workspace_id)
    }

    fn load_positions(&self, workspace_id: &str) -> Vec<Position> {
        self.fallback.load_positions(workspace_id)
    }

    fn load_funds(&self) -> Vec<Fund> {
        self.fallback.load_funds()
    }

    fn load_distributions(&self) -> Vec<Distribution> {
        self.fallback.load_distributions()
    }

    fn load_holdings(&self) -> Vec<HoldingExposure> {
        self.fallback.load_holdings()
    }

    fn load_exposure(&self, workspace_id: &str) -> ExposureBreakdown {
        self.fallback.load_exposure(workspace_id)
    }

    fn load_alerts(&self, workspace_id: &str) -> Vec<Alert> {
        self.fallback.load_alerts(workspace_id)
    }

    fn load_documents(&self) -> Vec<DocumentSnapshot> {
        self.fallback.load_documents()
    }

    fn load_jobs(&self) -> (Vec<ScheduledJob>, Vec<JobRun>) {
        self.fallback.load_jobs()
    }

    fn load_data_operations(&self, workspace_id: &str) -> DataOperationsLoad {
        if self.mode == DataMode::Mock {
            return self.fallback.load_data_operations(workspace_id);
        }

        match ApiDataProvider::new(self.api_config.clone()) {
            Ok(provider) => {
                let loaded = provider.load_data_operations(
                    workspace_id,
                    &self.fallback_operations,
                    &self.fallback_scheduled_jobs,
                    &self.fallback_job_runs,
                );
                DataOperationsLoad {
                    operations: loaded.operations,
                    scheduled_jobs: Some(loaded.scheduled_jobs),
                    job_runs: Some(loaded.job_runs),
                }
            }
            Err(message) => DataOperationsLoad {
                operations: api_configuration_failure(
                    &self.fallback_operations,
                    &self.api_config.base_url,
                    &message,
                ),
                scheduled_jobs: Some(self.fallback_scheduled_jobs.clone()),
                job_runs: Some(self.fallback_job_runs.clone()),
            },
        }
    }

    fn load_time_series(&self, workspace_id: &str) -> Vec<TimeSeries> {
        self.fallback.load_time_series(workspace_id)
    }

    fn resolve_instrument(
        &self,
        request: &InstrumentResolutionRequest,
    ) -> InstrumentResolutionResult {
        self.fallback.resolve_instrument(request)
    }
}

pub fn normalize_backend_base_url(value: &str) -> Result<String, String> {
    let trimmed = value.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        return Err("backend base URL is required".to_owned());
    }
    if !trimmed.starts_with("http://") && !trimmed.starts_with("https://") {
        return Err("backend base URL must start with http:// or https://".to_owned());
    }
    if trimmed.chars().any(char::is_whitespace) {
        return Err("backend base URL cannot contain whitespace".to_owned());
    }
    if trimmed.contains('?') || trimmed.contains('#') {
        return Err("backend base URL cannot contain a query string or fragment".to_owned());
    }
    if contains_secret_material(trimmed) {
        return Err(
            "backend base URL must not contain credentials or secret parameters".to_owned(),
        );
    }
    Ok(trimmed.to_owned())
}

pub fn current_utc_timestamp() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    format_utc_timestamp(seconds)
}

fn format_utc_timestamp(seconds: u64) -> String {
    let days = i64::try_from(seconds / 86_400).unwrap_or(i64::MAX);
    let seconds_of_day = seconds % 86_400;
    let hour = seconds_of_day / 3_600;
    let minute = (seconds_of_day % 3_600) / 60;
    let second = seconds_of_day % 60;
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02} UTC")
}

fn civil_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month, day)
}

fn sanitize_http_error(endpoint: ApiEndpoint, error: ureq::Error) -> String {
    match error {
        ureq::Error::StatusCode(code) => {
            format!("{} returned HTTP {code}", endpoint.label())
        }
        other => format!(
            "{} request failed: {}",
            endpoint.label(),
            mask_secret_fragments(&other.to_string())
        ),
    }
}

fn api_configuration_failure(
    fallback: &DataOperationsSnapshot,
    base_url: &str,
    message: &str,
) -> DataOperationsSnapshot {
    let mut operations = fallback.clone();
    let message = mask_secret_fragments(message);
    operations.hydration = DataOperationsHydration {
        origin: match fallback.hydration.origin {
            DataOperationsOrigin::Api
            | DataOperationsOrigin::PartialApi
            | DataOperationsOrigin::StaleApi => DataOperationsOrigin::StaleApi,
            DataOperationsOrigin::Mock | DataOperationsOrigin::ApiError => {
                DataOperationsOrigin::ApiError
            }
        },
        refreshed_at: fallback.hydration.refreshed_at.clone(),
        base_url: Some(mask_secret_fragments(base_url)),
        failed_sections: vec![DataOperationsSectionFailure {
            section: "API configuration".to_owned(),
            message: message.clone(),
        }],
        last_error: Some(message),
    };
    operations
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_and_normalizes_backend_base_urls() {
        assert_eq!(
            normalize_backend_base_url(" http://localhost:8080/api/v1/ "),
            Ok("http://localhost:8080/api/v1".to_owned())
        );
        assert!(normalize_backend_base_url("localhost:8080").is_err());
        assert!(normalize_backend_base_url("https://user:pass@example.test/api/v1").is_err());
        assert!(normalize_backend_base_url("https://example.test/api/v1?token=secret").is_err());
    }

    #[test]
    fn formats_unix_epoch_as_utc_timestamp() {
        assert_eq!(format_utc_timestamp(0), "1970-01-01 00:00:00 UTC");
        assert_eq!(
            format_utc_timestamp(1_782_172_800),
            "2026-06-23 00:00:00 UTC"
        );
    }

    #[test]
    fn invalid_api_configuration_keeps_mock_fallback() {
        let fallback = DataOperationsSnapshot::default();
        let failed = api_configuration_failure(
            &fallback,
            "http://localhost:8080/api/v1?token=secret",
            "backend URL token=secret is invalid",
        );

        assert_eq!(failed.hydration.origin, DataOperationsOrigin::ApiError);
        assert!(
            failed
                .hydration
                .last_error
                .as_deref()
                .is_some_and(|message| !message.contains("secret"))
        );
    }
}
