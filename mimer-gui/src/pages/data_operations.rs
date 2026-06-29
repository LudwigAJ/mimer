use crate::api::types::{ApiConfig, DataMode};
use crate::app_model::DashboardSnapshot;
use crate::compute::data_operations::{
    constituent_readiness_status, derive_recommended_actions, fetch_log_copy_text,
    fetch_log_display_key,
};
use crate::domain::{
    AnalysisSubject, BackendSectionStatus, ConstituentReadinessRow, DataDiagnosticIssue,
    DataOperationStatus, DataOperationsSnapshot, JobRun, MarketDataPlanItem, ReadinessStage,
    RecommendedDataAction, ScheduledJob, SourceBudget, SourceFetchLog,
};
use crate::filter::any_contains_ci;
use crate::pages::{Page, bool_text, format_number, format_pct, header_cell, sortable_header_cell};
use crate::table_state::{ColumnDescriptor, SortSpec, TableId, TableLayoutRegistry, TableState};
use crate::ui::table_layout::{managed_column, managed_table_revision, table_layout_controls};
use crate::ui::{metrics, style};
use eframe::egui;
use egui_extras::{Column, TableBuilder};
use std::cmp::Ordering;

const COL_PRIORITY: &str = "priority";
const COL_TYPE: &str = "type";
const COL_SUBJECT: &str = "subject";
const COL_SOURCE: &str = "source";
const COL_REQUESTS: &str = "requests";
const COL_STATUS: &str = "status";
const COL_NEXT_RUN: &str = "next_run";
const COL_LAST_RUN: &str = "last_run";
const COL_TIME: &str = "time";
const COL_KIND: &str = "kind";
const COL_KEY: &str = "key";
const COL_DURATION: &str = "duration";
const COL_FUND: &str = "fund";
const COL_HOLDING: &str = "holding";
const COL_WEIGHT: &str = "weight";
const COL_IDENTITY: &str = "identity";
const COL_PRICE: &str = "price";
const COL_PRICE_DATE: &str = "price_date";
const COL_SEVERITY: &str = "severity";
const COL_TITLE: &str = "title";

macro_rules! column_index {
    ($all:expr, $value:expr, $name:literal) => {
        $all.iter()
            .position(|column| *column == $value)
            .expect(concat!($name, " column is in ALL"))
    };
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PlanColumn {
    Priority,
    ItemType,
    Subject,
    Reason,
    Source,
    Requests,
    Status,
    NextAction,
}

impl PlanColumn {
    const ALL: [Self; 8] = [
        Self::Priority,
        Self::ItemType,
        Self::Subject,
        Self::Reason,
        Self::Source,
        Self::Requests,
        Self::Status,
        Self::NextAction,
    ];

    fn index(self) -> usize {
        column_index!(Self::ALL, self, "plan")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Priority => COL_PRIORITY,
            Self::ItemType => COL_TYPE,
            Self::Subject => COL_SUBJECT,
            Self::Reason => "reason",
            Self::Source => COL_SOURCE,
            Self::Requests => COL_REQUESTS,
            Self::Status => COL_STATUS,
            Self::NextAction => "next_action",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Priority => "Priority",
            Self::ItemType => "Type",
            Self::Subject => "Subject",
            Self::Reason => "Reason / blocker",
            Self::Source => "Source",
            Self::Requests => "Requests",
            Self::Status => "Status",
            Self::NextAction => "Next action",
        }
    }

    fn payload(self, item: &MarketDataPlanItem) -> (String, String) {
        let value = match self {
            Self::Priority => item.priority.to_string(),
            Self::ItemType => item.item_type.clone(),
            Self::Subject => item.subject_label.clone(),
            Self::Reason => item.blocker.clone().unwrap_or_else(|| item.reason.clone()),
            Self::Source => item.source.clone(),
            Self::Requests => item.estimated_requests.to_string(),
            Self::Status => item.status.as_str().to_owned(),
            Self::NextAction => item.next_action.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SourceBudgetColumn {
    Source,
    Enabled,
    Status,
    Requests,
    Delay,
    Backoff,
    Failures,
    CacheHits,
}

impl SourceBudgetColumn {
    const ALL: [Self; 8] = [
        Self::Source,
        Self::Enabled,
        Self::Status,
        Self::Requests,
        Self::Delay,
        Self::Backoff,
        Self::Failures,
        Self::CacheHits,
    ];

    fn index(self) -> usize {
        column_index!(Self::ALL, self, "source budget")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Source => COL_SOURCE,
            Self::Enabled => "enabled",
            Self::Status => COL_STATUS,
            Self::Requests => COL_REQUESTS,
            Self::Delay => "delay",
            Self::Backoff => "backoff",
            Self::Failures => "failures",
            Self::CacheHits => "cache_hits",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Source => "Source",
            Self::Enabled => "Enabled",
            Self::Status => "Status",
            Self::Requests => "Requests",
            Self::Delay => "Delay",
            Self::Backoff => "Backoff",
            Self::Failures => "Failures",
            Self::CacheHits => "Cache hits",
        }
    }

    fn payload(self, budget: &SourceBudget) -> (String, String) {
        let value = match self {
            Self::Source => budget.source.clone(),
            Self::Enabled => bool_text(budget.enabled).to_owned(),
            Self::Status => budget.status.as_str().to_owned(),
            Self::Requests => request_window(budget),
            Self::Delay => budget.min_delay.clone(),
            Self::Backoff => budget
                .backoff_until
                .clone()
                .unwrap_or_else(|| "-".to_owned()),
            Self::Failures => budget.recent_failures.to_string(),
            Self::CacheHits => budget.cache_hits.to_string(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FetchLogColumn {
    Time,
    Source,
    Kind,
    Key,
    Status,
    Http,
    Duration,
    Cache,
    Limited,
    Error,
}

impl FetchLogColumn {
    const ALL: [Self; 10] = [
        Self::Time,
        Self::Source,
        Self::Kind,
        Self::Key,
        Self::Status,
        Self::Http,
        Self::Duration,
        Self::Cache,
        Self::Limited,
        Self::Error,
    ];

    fn index(self) -> usize {
        column_index!(Self::ALL, self, "fetch log")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Time => COL_TIME,
            Self::Source => COL_SOURCE,
            Self::Kind => COL_KIND,
            Self::Key => COL_KEY,
            Self::Status => COL_STATUS,
            Self::Http => "http",
            Self::Duration => COL_DURATION,
            Self::Cache => "cache",
            Self::Limited => "limited",
            Self::Error => "error",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Time => "Time",
            Self::Source => "Source",
            Self::Kind => "Kind",
            Self::Key => "Key",
            Self::Status => "Status",
            Self::Http => "HTTP",
            Self::Duration => "Duration",
            Self::Cache => "Cache",
            Self::Limited => "Limited",
            Self::Error => "Error",
        }
    }

    fn payload(self, log: &SourceFetchLog) -> (String, String) {
        let value = match self {
            Self::Time => log.time.clone(),
            Self::Source => log.source.clone(),
            Self::Kind => log.request_kind.clone(),
            Self::Key => fetch_log_display_key(log),
            Self::Status => log.status.as_str().to_owned(),
            Self::Http => log
                .http_status
                .map(|status| status.to_string())
                .unwrap_or_else(|| "-".to_owned()),
            Self::Duration => log.duration_ms.to_string(),
            Self::Cache => bool_text(log.cache_hit).to_owned(),
            Self::Limited => bool_text(log.rate_limited).to_owned(),
            Self::Error => log.error.clone().unwrap_or_else(|| "-".to_owned()),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DiagnosticColumn {
    Severity,
    Issue,
    Status,
    Source,
    Recommended,
    Detail,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SchedulerColumn {
    Job,
    Schedule,
    Next,
    Last,
    Lease,
    Source,
    Status,
    Action,
}

impl SchedulerColumn {
    const ALL: [Self; 8] = [
        Self::Job,
        Self::Schedule,
        Self::Next,
        Self::Last,
        Self::Lease,
        Self::Source,
        Self::Status,
        Self::Action,
    ];

    fn index(self) -> usize {
        column_index!(Self::ALL, self, "scheduler")
    }

    fn key(self) -> &'static str {
        DATA_OPERATIONS_SCHEDULER_COLUMNS[self.index()].key
    }

    fn payload(self, job: &ScheduledJob, last_run: Option<&JobRun>) -> (String, String) {
        let running = last_run.is_some_and(|run| run.status.as_str() == "RUNNING");
        let value = match self {
            Self::Job => job.name.clone(),
            Self::Schedule => job.cron_schedule.clone(),
            Self::Next => job.next_run.clone(),
            Self::Last => job.last_run.clone(),
            Self::Lease => if running { "RUNNING" } else { "IDLE" }.to_owned(),
            Self::Source => job.source.clone(),
            Self::Status => scheduler_status(job, last_run).to_owned(),
            Self::Action => format!("run {}", job.name.replace(' ', "_")),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ConstituentColumn {
    Fund,
    Holding,
    Ticker,
    Weight,
    Identity,
    InstrumentListing,
    Price,
    Date,
    Source,
    NextAction,
}

impl ConstituentColumn {
    const ALL: [Self; 10] = [
        Self::Fund,
        Self::Holding,
        Self::Ticker,
        Self::Weight,
        Self::Identity,
        Self::InstrumentListing,
        Self::Price,
        Self::Date,
        Self::Source,
        Self::NextAction,
    ];

    fn index(self) -> usize {
        column_index!(Self::ALL, self, "constituent")
    }

    fn key(self) -> &'static str {
        DATA_OPERATIONS_CONSTITUENT_COLUMNS[self.index()].key
    }

    fn payload(self, row: &ConstituentReadinessRow) -> (String, String) {
        let value = match self {
            Self::Fund => row.fund_ticker.clone(),
            Self::Holding => row.holding_name.clone(),
            Self::Ticker => row.holding_ticker.clone(),
            Self::Weight => format_pct(row.weight_pct),
            Self::Identity => row.identity_status.as_str().to_owned(),
            Self::InstrumentListing => format!(
                "{}/{}",
                row.instrument_id.as_deref().unwrap_or("-"),
                row.listing_id.as_deref().unwrap_or("-")
            ),
            Self::Price => row
                .latest_price
                .map(|value| format_number(value, 2))
                .unwrap_or_else(|| "-".to_owned()),
            Self::Date => row.price_date.clone().unwrap_or_else(|| "-".to_owned()),
            Self::Source => row.price_source.clone(),
            Self::NextAction => format!("{} {}", row.price_status.as_str(), row.next_action),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ApiSectionColumn {
    Section,
    Status,
    Rows,
    Detail,
}

impl ApiSectionColumn {
    const ALL: [Self; 4] = [Self::Section, Self::Status, Self::Rows, Self::Detail];

    fn index(self) -> usize {
        column_index!(Self::ALL, self, "API section")
    }

    fn key(self) -> &'static str {
        DATA_OPERATIONS_API_SECTION_COLUMNS[self.index()].key
    }

    fn payload(self, section: &BackendSectionStatus) -> (String, String) {
        let value = match self {
            Self::Section => section.label.clone(),
            Self::Status => section.status.as_str().to_owned(),
            Self::Rows => section
                .record_count
                .map(|count| count.to_string())
                .unwrap_or_else(|| "-".to_owned()),
            Self::Detail => section.detail.clone(),
        };
        (value.clone(), value)
    }
}

const DATA_OPERATIONS_PLAN_COLUMNS: [ColumnDescriptor; 8] = [
    ColumnDescriptor::new("priority", "Pri", 50.0, 42.0, 90.0).required(),
    ColumnDescriptor::new("type", "Type", 172.0, 130.0, 320.0).clipped(),
    ColumnDescriptor::new("subject", "Subject", 180.0, 130.0, 340.0)
        .required()
        .clipped(),
    ColumnDescriptor::new("reason", "Reason / blocker", 230.0, 160.0, 520.0).clipped(),
    ColumnDescriptor::new("source", "Source", 120.0, 94.0, 220.0).required(),
    ColumnDescriptor::new("requests", "Req", 74.0, 58.0, 130.0),
    ColumnDescriptor::new("status", "Status", 110.0, 84.0, 180.0).required(),
    ColumnDescriptor::new("next_action", "Next action", 190.0, 130.0, 420.0).clipped(),
];

const DATA_OPERATIONS_SOURCE_COLUMNS: [ColumnDescriptor; 9] = [
    ColumnDescriptor::new("source", "Source", 160.0, 110.0, 280.0)
        .required()
        .clipped(),
    ColumnDescriptor::new("enabled", "Enabled", 68.0, 52.0, 110.0),
    ColumnDescriptor::new("status", "Status", 112.0, 84.0, 180.0).required(),
    ColumnDescriptor::new("requests", "Requests", 116.0, 92.0, 200.0),
    ColumnDescriptor::new("delay", "Delay", 82.0, 64.0, 150.0),
    ColumnDescriptor::new("backoff", "Backoff", 118.0, 94.0, 240.0),
    ColumnDescriptor::new("failures", "Failures", 92.0, 72.0, 140.0),
    ColumnDescriptor::new("cache_hits", "Cache hits", 92.0, 72.0, 150.0),
    ColumnDescriptor::new("actions", "Actions / capabilities", 190.0, 140.0, 420.0)
        .hidden_by_default()
        .clipped(),
];

const DATA_OPERATIONS_FETCH_COLUMNS: [ColumnDescriptor; 10] = [
    ColumnDescriptor::new("time", "Time", 128.0, 102.0, 220.0),
    ColumnDescriptor::new("source", "Source", 120.0, 92.0, 220.0).required(),
    ColumnDescriptor::new("kind", "Kind", 130.0, 96.0, 240.0),
    ColumnDescriptor::new("key", "Key", 220.0, 150.0, 520.0).clipped(),
    ColumnDescriptor::new("status", "Status", 104.0, 82.0, 180.0).required(),
    ColumnDescriptor::new("http", "HTTP", 64.0, 52.0, 100.0),
    ColumnDescriptor::new("duration", "Duration", 78.0, 62.0, 140.0),
    ColumnDescriptor::new("cache", "Cache", 64.0, 52.0, 100.0),
    ColumnDescriptor::new("limited", "Limited", 70.0, 56.0, 110.0),
    ColumnDescriptor::new("error", "Error", 260.0, 180.0, 620.0)
        .hidden_by_default()
        .clipped(),
];

const DATA_OPERATIONS_SCHEDULER_COLUMNS: [ColumnDescriptor; 8] = [
    ColumnDescriptor::new("job", "Job", 190.0, 130.0, 360.0)
        .required()
        .clipped(),
    ColumnDescriptor::new("schedule", "Schedule", 110.0, 88.0, 200.0),
    ColumnDescriptor::new("next", "Next", 126.0, 102.0, 230.0),
    ColumnDescriptor::new("last", "Last", 92.0, 74.0, 190.0),
    ColumnDescriptor::new("lease", "Lease", 90.0, 70.0, 150.0),
    ColumnDescriptor::new("source", "Source", 130.0, 96.0, 230.0).required(),
    ColumnDescriptor::new("status", "Status", 90.0, 70.0, 160.0).required(),
    ColumnDescriptor::new("action", "Action", 170.0, 120.0, 360.0).clipped(),
];

const DATA_OPERATIONS_CONSTITUENT_COLUMNS: [ColumnDescriptor; 10] = [
    ColumnDescriptor::new("fund", "Fund", 78.0, 60.0, 140.0).required(),
    ColumnDescriptor::new("holding", "Holding", 170.0, 120.0, 340.0)
        .required()
        .clipped(),
    ColumnDescriptor::new("ticker", "Ticker", 74.0, 58.0, 130.0),
    ColumnDescriptor::new("weight", "Weight", 86.0, 66.0, 140.0),
    ColumnDescriptor::new("identity", "Identity", 96.0, 76.0, 170.0).required(),
    ColumnDescriptor::new(
        "instrument_listing",
        "Instrument/listing",
        150.0,
        106.0,
        320.0,
    )
    .clipped(),
    ColumnDescriptor::new("price", "Price", 96.0, 74.0, 160.0),
    ColumnDescriptor::new("date", "Date", 96.0, 76.0, 180.0),
    ColumnDescriptor::new("source", "Source", 110.0, 84.0, 210.0).required(),
    ColumnDescriptor::new("next_action", "Next action", 170.0, 130.0, 380.0).clipped(),
];

const DATA_OPERATIONS_API_SECTION_COLUMNS: [ColumnDescriptor; 4] = [
    ColumnDescriptor::new("section", "Section", 190.0, 130.0, 360.0)
        .required()
        .clipped(),
    ColumnDescriptor::new("status", "Status", 90.0, 70.0, 160.0).required(),
    ColumnDescriptor::new("rows", "Rows", 80.0, 60.0, 120.0),
    ColumnDescriptor::new("detail", "Detail", 280.0, 180.0, 620.0)
        .hidden_by_default()
        .clipped(),
];

impl DiagnosticColumn {
    const ALL: [Self; 6] = [
        Self::Severity,
        Self::Issue,
        Self::Status,
        Self::Source,
        Self::Recommended,
        Self::Detail,
    ];

    fn index(self) -> usize {
        column_index!(Self::ALL, self, "diagnostic")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Severity => COL_SEVERITY,
            Self::Issue => COL_TITLE,
            Self::Status => COL_STATUS,
            Self::Source => COL_SOURCE,
            Self::Recommended => "recommended",
            Self::Detail => "detail",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Severity => "Severity",
            Self::Issue => "Issue",
            Self::Status => "Status",
            Self::Source => "Source",
            Self::Recommended => "Recommended",
            Self::Detail => "Detail",
        }
    }

    fn payload(self, issue: &DataDiagnosticIssue) -> (String, String) {
        let value = match self {
            Self::Severity => issue.severity.as_str().to_owned(),
            Self::Issue => issue.title.clone(),
            Self::Status => issue.status.as_str().to_owned(),
            Self::Source => issue.source.clone(),
            Self::Recommended => issue.recommended_action.clone(),
            Self::Detail => issue.detail.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Debug)]
pub struct DataOperationsState {
    pub filter: String,
    pub selected_readiness_index: Option<usize>,
    pub actions_table: TableState,
    pub plan_table: TableState,
    pub scheduler_table: TableState,
    pub source_budget_table: TableState,
    pub fetch_log_table: TableState,
    pub constituent_table: TableState,
    pub diagnostic_table: TableState,
    pub api_sections_table: TableState,
    pub active_table: TableId,
}

impl Default for DataOperationsState {
    fn default() -> Self {
        Self {
            filter: String::new(),
            selected_readiness_index: None,
            actions_table: TableState::new(TableId::DataOperationsActions),
            plan_table: TableState::new(TableId::DataOperationsPlan),
            scheduler_table: TableState::new(TableId::DataOperationsScheduler),
            source_budget_table: TableState::new(TableId::DataOperationsSources),
            fetch_log_table: TableState::new(TableId::DataOperationsFetchLogs),
            constituent_table: TableState::new(TableId::DataOperationsConstituents),
            diagnostic_table: TableState::new(TableId::DataOperationsDiagnostics),
            api_sections_table: TableState::new(TableId::DataOperationsApiSections),
            active_table: TableId::DataOperationsReadiness,
        }
    }
}

impl DataOperationsState {
    pub fn select_readiness(&mut self, index: usize) {
        self.clear_selections();
        self.selected_readiness_index = Some(index);
        self.active_table = TableId::DataOperationsReadiness;
    }

    pub fn select_table(&mut self, table_id: TableId, index: usize) {
        self.clear_selections();
        self.active_table = table_id;
        match table_id {
            TableId::DataOperationsActions => self.actions_table.select(index),
            TableId::DataOperationsPlan => self.plan_table.select(index),
            TableId::DataOperationsScheduler => self.scheduler_table.select(index),
            TableId::DataOperationsSources => self.source_budget_table.select(index),
            TableId::DataOperationsFetchLogs => self.fetch_log_table.select(index),
            TableId::DataOperationsConstituents => self.constituent_table.select(index),
            TableId::DataOperationsDiagnostics => self.diagnostic_table.select(index),
            TableId::DataOperationsApiSections => self.api_sections_table.select(index),
            TableId::DataOperationsReadiness
            | TableId::PortfolioPositions
            | TableId::EtfsFunds
            | TableId::ExposureCountries
            | TableId::ExposureSectors
            | TableId::ExposureCurrencies
            | TableId::ExposureTopHoldings
            | TableId::ExposureDiagnostics
            | TableId::Holdings
            | TableId::Documents
            | TableId::Dividends
            | TableId::ScheduledJobs
            | TableId::JobRuns
            | TableId::Alerts
            | TableId::ChartSeriesData
            | TableId::SearchResults
            | TableId::FundDetailListings
            | TableId::FundDetailHoldings
            | TableId::FundDetailDistributions
            | TableId::FundDetailDocuments => {}
        }
    }

    pub fn clear_selections(&mut self) {
        self.selected_readiness_index = None;
        self.actions_table.clear_selection();
        self.plan_table.clear_selection();
        self.scheduler_table.clear_selection();
        self.source_budget_table.clear_selection();
        self.fetch_log_table.clear_selection();
        self.constituent_table.clear_selection();
        self.diagnostic_table.clear_selection();
        self.api_sections_table.clear_selection();
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DataOperationsAction {
    Navigate(Page),
    OpenSubject {
        subject: AnalysisSubject,
        label: String,
    },
    RunJob(String),
    Copy {
        label: String,
        text: String,
    },
    Feedback(String),
    Refresh,
    SetDataMode(DataMode),
}

pub fn render(
    ui: &mut egui::Ui,
    snapshot: &DashboardSnapshot,
    state: &mut DataOperationsState,
    configured_mode: DataMode,
    api_config: &ApiConfig,
    refreshing: bool,
    layouts: &mut TableLayoutRegistry,
) -> Option<DataOperationsAction> {
    let mut action = None;
    let operations = &snapshot.data_operations;

    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let hydration = &operations.hydration;
            let subtitle = hydration_summary(operations);
            style::page_header(
                ui,
                "Data Operations",
                Some("Market Data Readiness"),
                Some(&subtitle),
                |ui| {
                if hydration.origin == crate::domain::DataOperationsOrigin::Mock {
                    style::mock_badge(ui);
                } else {
                    style::status_badge(ui, hydration.origin.as_str());
                }
                style::status_badge(
                    ui,
                    if refreshing {
                        "RUNNING"
                    } else {
                        snapshot.data_status.as_str()
                    },
                );
                ui.monospace(hydration_summary(operations));
                if !hydration.failed_sections.is_empty() {
                    ui.monospace(format!(
                        "{} failed sections",
                        hydration.failed_sections.len()
                    ))
                    .on_hover_text(failed_sections_tooltip(operations))
                    .on_hover_cursor(egui::CursorIcon::Help);
                }
                ui.separator();
                if crate::ui::actions::action_button(
                    ui,
                    "Refresh",
                    "Refresh Data Operations on a background worker.",
                )
                .clicked()
                {
                    action = Some(DataOperationsAction::Refresh);
                }
                if ui
                    .selectable_label(configured_mode == DataMode::Mock, "Use Mock")
                    .on_hover_text("Use local API-shaped fixtures.")
                    .clicked()
                {
                    action = Some(DataOperationsAction::SetDataMode(DataMode::Mock));
                }
                if ui
                    .selectable_label(configured_mode == DataMode::Api, "Use API")
                    .on_hover_text("Hydrate this workspace from REST without blocking the UI.")
                    .clicked()
                {
                    action = Some(DataOperationsAction::SetDataMode(DataMode::Api));
                }
                if crate::ui::actions::action_button(ui, "Open Jobs", "Open scheduler page.")
                    .clicked()
                {
                    action = Some(DataOperationsAction::Navigate(Page::Jobs));
                }
                if crate::ui::actions::action_button(
                    ui,
                    "Open Diagnostics",
                    "Open Analytics diagnostics.",
                )
                .clicked()
                {
                    action = Some(DataOperationsAction::Navigate(Page::Analytics));
                }
                if crate::ui::actions::action_button(ui, "Settings", "Open backend/API settings.")
                    .clicked()
                {
                    action = Some(DataOperationsAction::Navigate(Page::Settings));
                }
                if crate::ui::actions::action_button(
                    ui,
                    "Copy URL",
                    "Copy the configured backend base URL.",
                )
                .clicked()
                {
                    action = Some(DataOperationsAction::Copy {
                        label: "backend URL".to_owned(),
                        text: api_config.base_url.clone(),
                    });
                }
                },
            );
            if !hydration.failed_sections.is_empty() {
                style::state_message(
                    ui,
                    "PARTIAL API",
                    &format!(
                        "{} API sections failed; successful sections and visible fallback data are retained.",
                        hydration.failed_sections.len()
                    ),
                );
            } else if hydration.origin == crate::domain::DataOperationsOrigin::ApiError {
                style::state_message(
                    ui,
                    "API ERROR",
                    hydration
                        .last_error
                        .as_deref()
                        .unwrap_or("API hydration failed; fixture data remains visible."),
                );
            }
            ui.add_space(metrics::SPACE_2);

            filters(ui, state);
            ui.add_space(metrics::SPACE_2);

            readiness_summary(ui, operations, state);
            ui.add_space(metrics::SPACE_2);

            recommended_actions_table(ui, operations, state, &mut action);
            ui.add_space(metrics::SPACE_2);

            market_data_plan_table(ui, operations, state, layouts, &mut action);
            ui.add_space(metrics::SPACE_2);

            scheduler_table(
                ui,
                &snapshot.scheduled_jobs,
                &snapshot.job_runs,
                state,
                layouts,
                &mut action,
            );
            ui.add_space(metrics::SPACE_2);

            source_budgets_table(ui, operations, state, layouts, &mut action);
            ui.add_space(metrics::SPACE_2);

            fetch_logs_table(ui, operations, state, layouts, &mut action);
            ui.add_space(metrics::SPACE_2);

            constituent_coverage_table(ui, operations, state, layouts, &mut action);
            ui.add_space(metrics::SPACE_2);

            diagnostics_table(ui, operations, state, &mut action);
            ui.add_space(metrics::SPACE_2);

            backend_sections_table(ui, &operations.backend_sections, state, layouts);
        });

    action
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    snapshot: &DashboardSnapshot,
    state: &mut DataOperationsState,
    layouts: &mut TableLayoutRegistry,
) -> Option<(AnalysisSubject, String)> {
    if ctx.text_edit_focused() {
        return None;
    }

    let operations = &snapshot.data_operations;
    match state.active_table {
        TableId::DataOperationsPlan => {
            let visible = visible_plan_indices(&operations.market_data_plan, state);
            let columns =
                layouts.visible_indices(TableId::DataOperationsPlan, &DATA_OPERATIONS_PLAN_COLUMNS);
            let enter = move_table_focus(ctx, &mut state.plan_table, &visible, &columns);
            sync_plan_focus(&operations.market_data_plan, state);
            if enter {
                return state
                    .plan_table
                    .selected_index()
                    .and_then(|index| operations.market_data_plan.get(index))
                    .and_then(|item| {
                        item.subject
                            .clone()
                            .map(|subject| (subject, item.subject_label.clone()))
                    });
            }
        }
        TableId::DataOperationsSources => {
            let visible = visible_source_budget_indices(&operations.source_budgets, state);
            let columns = layouts
                .visible_indices(
                    TableId::DataOperationsSources,
                    &DATA_OPERATIONS_SOURCE_COLUMNS,
                )
                .into_iter()
                .filter(|index| *index < SourceBudgetColumn::ALL.len())
                .collect::<Vec<_>>();
            move_table_focus(ctx, &mut state.source_budget_table, &visible, &columns);
            sync_source_budget_focus(&operations.source_budgets, state);
        }
        TableId::DataOperationsFetchLogs => {
            let visible = visible_fetch_log_indices(&operations.fetch_logs, state);
            let columns = layouts.visible_indices(
                TableId::DataOperationsFetchLogs,
                &DATA_OPERATIONS_FETCH_COLUMNS,
            );
            move_table_focus(ctx, &mut state.fetch_log_table, &visible, &columns);
            sync_fetch_log_focus(&operations.fetch_logs, state);
        }
        TableId::DataOperationsDiagnostics => {
            let visible = visible_diagnostic_indices(&operations.diagnostic_issues, state);
            let columns = (0..DiagnosticColumn::ALL.len()).collect::<Vec<_>>();
            move_table_focus(ctx, &mut state.diagnostic_table, &visible, &columns);
            sync_diagnostic_focus(&operations.diagnostic_issues, state);
        }
        TableId::DataOperationsScheduler => {
            let visible =
                visible_scheduler_indices(&snapshot.scheduled_jobs, &snapshot.job_runs, state);
            let columns = layouts.visible_indices(
                TableId::DataOperationsScheduler,
                &DATA_OPERATIONS_SCHEDULER_COLUMNS,
            );
            move_table_focus(ctx, &mut state.scheduler_table, &visible, &columns);
            sync_scheduler_focus(&snapshot.scheduled_jobs, &snapshot.job_runs, state);
        }
        TableId::DataOperationsConstituents => {
            let visible = visible_constituent_indices(&operations.constituent_coverage, state);
            let columns = layouts.visible_indices(
                TableId::DataOperationsConstituents,
                &DATA_OPERATIONS_CONSTITUENT_COLUMNS,
            );
            let enter = move_table_focus(ctx, &mut state.constituent_table, &visible, &columns);
            sync_constituent_focus(&operations.constituent_coverage, state);
            if enter {
                return state
                    .constituent_table
                    .selected_index()
                    .and_then(|index| operations.constituent_coverage.get(index))
                    .map(|row| (row.subject.clone(), row.holding_ticker.clone()));
            }
        }
        TableId::DataOperationsApiSections => {
            let visible = (0..operations.backend_sections.len()).collect::<Vec<_>>();
            let columns = layouts.visible_indices(
                TableId::DataOperationsApiSections,
                &DATA_OPERATIONS_API_SECTION_COLUMNS,
            );
            move_table_focus(ctx, &mut state.api_sections_table, &visible, &columns);
            sync_api_section_focus(&operations.backend_sections, state);
        }
        _ => {}
    }
    None
}

fn move_table_focus(
    ctx: &egui::Context,
    table: &mut TableState,
    visible_indices: &[usize],
    visible_column_indices: &[usize],
) -> bool {
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        table.move_focus_row(visible_indices, -1, Some(0));
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        table.move_focus_row(visible_indices, 1, Some(0));
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        table.move_focus_visible_column(visible_column_indices, -1);
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        table.move_focus_visible_column(visible_column_indices, 1);
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        if table.selected_index().is_none() {
            table.move_focus_row(visible_indices, 1, Some(0));
        }
        return table.selected_index().is_some();
    }
    false
}

fn hydration_summary(operations: &DataOperationsSnapshot) -> String {
    let hydration = &operations.hydration;
    match (
        hydration.origin,
        hydration.refreshed_at.as_deref(),
        hydration.last_error.as_deref(),
    ) {
        (crate::domain::DataOperationsOrigin::Mock, Some(refreshed), _) => {
            format!("fixture data · {refreshed}")
        }
        (crate::domain::DataOperationsOrigin::Mock, None, _) => "fixture data".to_owned(),
        (
            crate::domain::DataOperationsOrigin::Api
            | crate::domain::DataOperationsOrigin::PartialApi,
            Some(refreshed),
            _,
        ) => format!("refreshed {refreshed}"),
        (
            crate::domain::DataOperationsOrigin::StaleApi
            | crate::domain::DataOperationsOrigin::ApiError,
            Some(refreshed),
            Some(error),
        ) => format!("showing previous data from {refreshed} · {error}"),
        (
            crate::domain::DataOperationsOrigin::StaleApi
            | crate::domain::DataOperationsOrigin::ApiError,
            _,
            Some(error),
        ) => format!("showing fallback data · {error}"),
        (_, Some(refreshed), _) => format!("refreshed {refreshed}"),
        (_, None, _) => hydration.origin.as_str().to_owned(),
    }
}

fn failed_sections_tooltip(operations: &DataOperationsSnapshot) -> String {
    operations
        .hydration
        .failed_sections
        .iter()
        .map(|failure| format!("{}: {}", failure.section, failure.message))
        .collect::<Vec<_>>()
        .join("\n")
}

fn backend_sections_table(
    ui: &mut egui::Ui,
    sections: &[BackendSectionStatus],
    state: &mut DataOperationsState,
    layouts: &mut TableLayoutRegistry,
) {
    if sections.is_empty() {
        return;
    }

    ui.label(egui::RichText::new(format!("API sections ({})", sections.len())).strong());
    let visible_row = state
        .api_sections_table
        .focused_row_index
        .or(state.api_sections_table.selected_index())
        .and_then(|index| sections.get(index))
        .map(|section| {
            (
                section.label.as_str(),
                layouts.visible_row_text(
                    TableId::DataOperationsApiSections,
                    &DATA_OPERATIONS_API_SECTION_COLUMNS,
                    &ApiSectionColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(section).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::DataOperationsApiSections,
        &DATA_OPERATIONS_API_SECTION_COLUMNS,
        state
            .api_sections_table
            .focused_column_index
            .and_then(|index| ApiSectionColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.api_sections_table.clear_focus();
    }
    let revision = managed_table_revision(
        layouts,
        TableId::DataOperationsApiSections,
        &DATA_OPERATIONS_API_SECTION_COLUMNS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("data_operations_api_sections", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(190.0);
    for descriptor in DATA_OPERATIONS_API_SECTION_COLUMNS {
        table = table.column(managed_column(
            layouts,
            TableId::DataOperationsApiSections,
            &DATA_OPERATIONS_API_SECTION_COLUMNS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| header_cell(ui, "Section"));
            header.col(|ui| header_cell(ui, "Status"));
            header.col(|ui| header_cell(ui, "Rows"));
            header.col(|ui| header_cell(ui, "Detail"));
        })
        .body(|mut body| {
            for (index, section) in sections.iter().enumerate() {
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.api_sections_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == TableId::DataOperationsApiSections
                            && state.api_sections_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.api_sections_table.is_focused_cell(index, 0),
                        );
                        let response = ui
                            .selectable_label(
                                state.api_sections_table.selection.is_selected(index),
                                &section.label,
                            )
                            .on_hover_text(format!("{} · {}", section.key, section.source));
                        if response.clicked() {
                            focus_api_section_cell(
                                state,
                                index,
                                section,
                                ApiSectionColumn::Section,
                            );
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.api_sections_table.is_focused_cell(index, 1),
                        );
                        style::status_badge(ui, section.status.as_str());
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.api_sections_table.is_focused_cell(index, 2),
                        );
                        ui.monospace(
                            section
                                .record_count
                                .map(|count| count.to_string())
                                .unwrap_or_else(|| "-".to_owned()),
                        );
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.api_sections_table.is_focused_cell(index, 3),
                        );
                        ui.label(&section.detail).on_hover_text(&section.detail);
                    });
                });
            }
        });
}

pub fn selected_copy_payload(
    snapshot: &DashboardSnapshot,
    state: &DataOperationsState,
) -> Option<(String, String)> {
    let operations = &snapshot.data_operations;
    let focused_cell = match state.active_table {
        TableId::DataOperationsPlan => state.plan_table.selected_cell.as_ref(),
        TableId::DataOperationsSources => state.source_budget_table.selected_cell.as_ref(),
        TableId::DataOperationsFetchLogs => state.fetch_log_table.selected_cell.as_ref(),
        TableId::DataOperationsDiagnostics => state.diagnostic_table.selected_cell.as_ref(),
        TableId::DataOperationsScheduler => state.scheduler_table.selected_cell.as_ref(),
        TableId::DataOperationsConstituents => state.constituent_table.selected_cell.as_ref(),
        TableId::DataOperationsApiSections => state.api_sections_table.selected_cell.as_ref(),
        _ => None,
    };
    if let Some(cell) = focused_cell {
        return Some((
            format!("{} {}", cell.column, cell.display_value),
            cell.raw_value.clone(),
        ));
    }

    match state.active_table {
        TableId::DataOperationsReadiness => {
            let index = state.selected_readiness_index?;
            let stage = operations.readiness_stages.get(index)?;
            Some((stage.label.clone(), readiness_copy_text(stage)))
        }
        TableId::DataOperationsActions => {
            let actions = derive_recommended_actions(operations);
            let index = state.actions_table.selected_index()?;
            let action = actions.get(index)?;
            Some((action.label.clone(), recommended_action_copy_text(action)))
        }
        TableId::DataOperationsPlan => {
            let item = state
                .plan_table
                .focused_row_index
                .or(state.plan_table.selected_index())
                .and_then(|index| operations.market_data_plan.get(index))?;
            Some((item.subject_label.clone(), plan_item_copy_text(item)))
        }
        TableId::DataOperationsScheduler => {
            let job = state
                .scheduler_table
                .focused_row_index
                .or(state.scheduler_table.selected_index())
                .and_then(|index| snapshot.scheduled_jobs.get(index))?;
            Some((job.name.clone(), scheduled_job_copy_text(job)))
        }
        TableId::DataOperationsSources => {
            let budget = state
                .source_budget_table
                .focused_row_index
                .or(state.source_budget_table.selected_index())
                .and_then(|index| operations.source_budgets.get(index))?;
            Some((budget.source.clone(), source_budget_copy_text(budget)))
        }
        TableId::DataOperationsFetchLogs => {
            let log = state
                .fetch_log_table
                .focused_row_index
                .or(state.fetch_log_table.selected_index())
                .and_then(|index| operations.fetch_logs.get(index))?;
            Some((log.id.clone(), fetch_log_copy_text(log)))
        }
        TableId::DataOperationsConstituents => {
            let row = state
                .constituent_table
                .focused_row_index
                .or(state.constituent_table.selected_index())
                .and_then(|index| operations.constituent_coverage.get(index))?;
            Some((row.holding_ticker.clone(), constituent_copy_text(row)))
        }
        TableId::DataOperationsDiagnostics => {
            let issue = state
                .diagnostic_table
                .focused_row_index
                .or(state.diagnostic_table.selected_index())
                .and_then(|index| operations.diagnostic_issues.get(index))?;
            Some((issue.id.clone(), diagnostic_copy_text(issue)))
        }
        TableId::DataOperationsApiSections => {
            let section = state
                .api_sections_table
                .focused_row_index
                .or(state.api_sections_table.selected_index())
                .and_then(|index| operations.backend_sections.get(index))?;
            Some((
                section.label.clone(),
                [
                    section.key.clone(),
                    section.label.clone(),
                    section.status.as_str().to_owned(),
                    section
                        .record_count
                        .map(|count| count.to_string())
                        .unwrap_or_else(|| "-".to_owned()),
                    section.source.clone(),
                    section.detail.clone(),
                ]
                .join("\t"),
            ))
        }
        TableId::PortfolioPositions
        | TableId::EtfsFunds
        | TableId::ExposureCountries
        | TableId::ExposureSectors
        | TableId::ExposureCurrencies
        | TableId::ExposureTopHoldings
        | TableId::ExposureDiagnostics
        | TableId::Holdings
        | TableId::Documents
        | TableId::Dividends
        | TableId::ScheduledJobs
        | TableId::JobRuns
        | TableId::Alerts
        | TableId::ChartSeriesData
        | TableId::SearchResults
        | TableId::FundDetailListings
        | TableId::FundDetailHoldings
        | TableId::FundDetailDistributions
        | TableId::FundDetailDocuments => None,
    }
}

fn filters(ui: &mut egui::Ui, state: &mut DataOperationsState) {
    ui.horizontal(|ui| {
        ui.label("Filter");
        ui.add_sized(
            [320.0, 20.0],
            egui::TextEdit::singleline(&mut state.filter)
                .hint_text("plan / source / log / constituent / issue"),
        );
        if ui.button("Clear").clicked() {
            state.filter.clear();
            state.clear_selections();
        }
    });
}

fn readiness_summary(
    ui: &mut egui::Ui,
    operations: &DataOperationsSnapshot,
    state: &mut DataOperationsState,
) {
    style::section_header(ui, "Readiness Summary");
    ui.horizontal_wrapped(|ui| {
        for (index, stage) in operations.readiness_stages.iter().enumerate() {
            let selected = state.selected_readiness_index == Some(index)
                && state.active_table == TableId::DataOperationsReadiness;
            let stroke = if selected {
                egui::Stroke::new(1.0, ui.visuals().selection.stroke.color)
            } else {
                ui.visuals().widgets.noninteractive.bg_stroke
            };
            let response = egui::Frame::group(ui.style())
                .fill(if selected {
                    ui.visuals().selection.bg_fill
                } else {
                    ui.visuals().faint_bg_color
                })
                .stroke(stroke)
                .inner_margin(egui::vec2(6.0, 4.0))
                .show(ui, |ui| {
                    ui.set_min_width(136.0);
                    ui.horizontal(|ui| {
                        ui.strong(&stage.label);
                        style::status_badge(ui, stage.status.as_str());
                    });
                    ui.horizontal(|ui| {
                        if let Some(coverage_pct) = stage.coverage_pct {
                            ui.monospace(format_pct(f64::from(coverage_pct)));
                        }
                        if !stage.count_label.is_empty() {
                            ui.monospace(&stage.count_label);
                        }
                    });
                    ui.horizontal(|ui| {
                        style::source_badge(ui, &stage.source);
                        ui.monospace(&stage.freshness);
                    });
                })
                .response
                .interact(egui::Sense::click())
                .on_hover_text(format!(
                    "{}\nAction: {}\nkey: {}",
                    stage.detail, stage.recommended_action, stage.key
                ))
                .on_hover_cursor(egui::CursorIcon::PointingHand);
            if response.clicked() {
                state.select_readiness(index);
            }
        }
    });
}

fn recommended_actions_table(
    ui: &mut egui::Ui,
    operations: &DataOperationsSnapshot,
    state: &mut DataOperationsState,
    action: &mut Option<DataOperationsAction>,
) {
    let actions = derive_recommended_actions(operations);
    let filtered = visible_action_indices(&actions, state);
    state.actions_table.selection.retain_visible(&filtered);

    ui.label(
        egui::RichText::new(format!(
            "Next recommended actions ({}/{})",
            filtered.len(),
            actions.len()
        ))
        .strong(),
    );
    if filtered.is_empty() {
        style::state_message(
            ui,
            "EMPTY",
            "No recommended actions match the current filter.",
        );
        return;
    }
    TableBuilder::new(ui)
        .id_salt("data_operations_actions")
        .striped(true)
        .resizable(true)
        .max_scroll_height(150.0)
        .column(Column::initial(52.0).at_least(42.0))
        .column(Column::initial(190.0).at_least(140.0).clip(true))
        .column(Column::initial(150.0).at_least(110.0).clip(true))
        .column(Column::initial(92.0).at_least(74.0))
        .column(Column::initial(130.0).at_least(100.0).clip(true))
        .column(Column::initial(170.0).at_least(120.0).clip(true))
        .column(Column::remainder().at_least(180.0).clip(true))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header
                .col(|ui| sortable_header_cell(ui, &mut state.actions_table, COL_PRIORITY, "Pri"));
            header
                .col(|ui| sortable_header_cell(ui, &mut state.actions_table, COL_TITLE, "Action"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.actions_table, COL_SUBJECT, "Target")
            });
            header
                .col(|ui| sortable_header_cell(ui, &mut state.actions_table, COL_STATUS, "Status"));
            header
                .col(|ui| sortable_header_cell(ui, &mut state.actions_table, COL_SOURCE, "Source"));
            header.col(|ui| header_cell(ui, "Actions"));
            header.col(|ui| header_cell(ui, "Reason"));
        })
        .body(|mut body| {
            for index in filtered {
                let row = actions[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut table_row| {
                    table_row.set_selected(
                        state.active_table == TableId::DataOperationsActions
                            && state.actions_table.selection.is_selected(index),
                    );
                    table_row.col(|ui| {
                        ui.monospace(row.priority.to_string());
                    });
                    table_row.col(|ui| {
                        selectable_label(
                            ui,
                            state,
                            TableId::DataOperationsActions,
                            index,
                            &row.label,
                            "Select recommended action.",
                        );
                    });
                    table_row.col(|ui| {
                        ui.label(&row.target);
                    });
                    table_row.col(|ui| {
                        style::status_badge(ui, row.status.as_str());
                    });
                    table_row.col(|ui| {
                        style::source_badge(ui, &row.source);
                    });
                    table_row.col(|ui| {
                        ui.horizontal_wrapped(|ui| {
                            if crate::ui::actions::action_button(
                                ui,
                                row.action_label.as_str(),
                                "Local/mock action; no backend request is made.",
                            )
                            .clicked()
                            {
                                state.select_table(TableId::DataOperationsActions, index);
                                if let Some(job_name) = row.command.strip_prefix("run ") {
                                    *action = Some(DataOperationsAction::RunJob(
                                        job_name.to_ascii_uppercase(),
                                    ));
                                } else {
                                    *action = Some(DataOperationsAction::Feedback(format!(
                                        "OPS: {}",
                                        row.label
                                    )));
                                }
                            }
                            if crate::ui::actions::action_button(
                                ui,
                                "Copy Cmd",
                                "Copy the mock command text.",
                            )
                            .clicked()
                            {
                                state.select_table(TableId::DataOperationsActions, index);
                                *action = Some(DataOperationsAction::Copy {
                                    label: format!("command {}", row.label),
                                    text: row.command.clone(),
                                });
                            }
                        });
                    });
                    table_row.col(|ui| {
                        ui.label(&row.reason).on_hover_text(&row.reason);
                    });
                });
            }
        });
}

fn market_data_plan_table(
    ui: &mut egui::Ui,
    operations: &DataOperationsSnapshot,
    state: &mut DataOperationsState,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<DataOperationsAction>,
) {
    let visible = visible_plan_indices(&operations.market_data_plan, state);
    state.plan_table.retain_visible(&visible);

    ui.label(
        egui::RichText::new(format!(
            "Market data plan ({}/{})",
            visible.len(),
            operations.market_data_plan.len()
        ))
        .strong(),
    );
    if visible.is_empty() {
        style::state_message(ui, "EMPTY", "No market-data plan rows match the filter.");
        return;
    }
    let visible_row = state
        .plan_table
        .focused_row_index
        .or(state.plan_table.selected_index())
        .and_then(|index| operations.market_data_plan.get(index))
        .map(|item| {
            (
                item.subject_label.as_str(),
                layouts.visible_row_text(
                    TableId::DataOperationsPlan,
                    &DATA_OPERATIONS_PLAN_COLUMNS,
                    &PlanColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(item).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::DataOperationsPlan,
        &DATA_OPERATIONS_PLAN_COLUMNS,
        state
            .plan_table
            .focused_column_index
            .and_then(|index| PlanColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.plan_table.clear_focus();
    }
    let revision = managed_table_revision(
        layouts,
        TableId::DataOperationsPlan,
        &DATA_OPERATIONS_PLAN_COLUMNS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("data_operations_plan", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(180.0);
    for descriptor in DATA_OPERATIONS_PLAN_COLUMNS {
        table = table.column(managed_column(
            layouts,
            TableId::DataOperationsPlan,
            &DATA_OPERATIONS_PLAN_COLUMNS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| sortable_header_cell(ui, &mut state.plan_table, COL_PRIORITY, "Pri"));
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.plan_table,
                    COL_TYPE,
                    PlanColumn::ItemType.label(),
                )
            });
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.plan_table,
                    COL_SUBJECT,
                    PlanColumn::Subject.label(),
                )
            });
            header.col(|ui| header_cell(ui, PlanColumn::Reason.label()));
            header.col(|ui| sortable_header_cell(ui, &mut state.plan_table, COL_SOURCE, "Source"));
            header.col(|ui| sortable_header_cell(ui, &mut state.plan_table, COL_REQUESTS, "Req"));
            header.col(|ui| sortable_header_cell(ui, &mut state.plan_table, COL_STATUS, "Status"));
            header.col(|ui| header_cell(ui, "Next action"));
        })
        .body(|mut body| {
            for index in visible {
                let item = operations.market_data_plan[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.plan_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == TableId::DataOperationsPlan
                            && state.plan_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .plan_table
                                .is_focused_cell(index, PlanColumn::Priority.index()),
                        );
                        ui.monospace(item.priority.to_string());
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .plan_table
                                .is_focused_cell(index, PlanColumn::ItemType.index()),
                        );
                        ui.label(&item.item_type).on_hover_text(&item.item_type);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .plan_table
                                .is_focused_cell(index, PlanColumn::Subject.index()),
                        );
                        let response = selectable_label(
                            ui,
                            state,
                            TableId::DataOperationsPlan,
                            index,
                            &item.subject_label,
                            "Single-click selects; double-click opens related subject when available.",
                        );
                        if response.clicked() {
                            focus_plan_cell(state, index, &item, PlanColumn::Subject);
                        }
                        if response.double_clicked() {
                            if let Some(subject) = item.subject.clone() {
                                *action = Some(DataOperationsAction::OpenSubject {
                                    subject,
                                    label: item.subject_label.clone(),
                                });
                            } else {
                                *action = Some(DataOperationsAction::Feedback(format!(
                                    "OPS: selected plan item {}",
                                    item.id
                                )));
                            }
                        }
                        response.context_menu(|ui| {
                            if ui.button("Open Subject").clicked() {
                                state.select_table(TableId::DataOperationsPlan, index);
                                if let Some(subject) = item.subject.clone() {
                                    *action = Some(DataOperationsAction::OpenSubject {
                                        subject,
                                        label: item.subject_label.clone(),
                                    });
                                }
                                ui.close();
                            }
                            if ui.button("Open Jobs").clicked() {
                                *action = Some(DataOperationsAction::Navigate(Page::Jobs));
                                ui.close();
                            }
                            if ui.button("Copy Item").clicked() {
                                *action = Some(DataOperationsAction::Copy {
                                    label: item.subject_label.clone(),
                                    text: plan_item_copy_text(&item),
                                });
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .plan_table
                                .is_focused_cell(index, PlanColumn::Reason.index()),
                        );
                        let text = item.blocker.as_deref().unwrap_or(&item.reason);
                        ui.label(text).on_hover_text(format!(
                            "Reason: {}\nBlocker: {}",
                            item.reason,
                            item.blocker.as_deref().unwrap_or("-")
                        ));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .plan_table
                                .is_focused_cell(index, PlanColumn::Source.index()),
                        );
                        style::source_badge(ui, &item.source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .plan_table
                                .is_focused_cell(index, PlanColumn::Requests.index()),
                        );
                        ui.label(format_number(f64::from(item.estimated_requests), 0));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .plan_table
                                .is_focused_cell(index, PlanColumn::Status.index()),
                        );
                        style::status_badge(ui, item.status.as_str());
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .plan_table
                                .is_focused_cell(index, PlanColumn::NextAction.index()),
                        );
                        ui.horizontal_wrapped(|ui| {
                            ui.label(&item.next_action);
                            if crate::ui::actions::action_button(
                                ui,
                                "Copy",
                                "Copy plan item row.",
                            )
                            .clicked()
                            {
                                state.select_table(TableId::DataOperationsPlan, index);
                                *action = Some(DataOperationsAction::Copy {
                                    label: item.subject_label.clone(),
                                    text: plan_item_copy_text(&item),
                                });
                            }
                        });
                    });
                });
            }
        });
}

fn scheduler_table(
    ui: &mut egui::Ui,
    scheduled_jobs: &[ScheduledJob],
    job_runs: &[JobRun],
    state: &mut DataOperationsState,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<DataOperationsAction>,
) {
    let visible = visible_scheduler_indices(scheduled_jobs, job_runs, state);
    state.scheduler_table.selection.retain_visible(&visible);

    ui.label(
        egui::RichText::new(format!(
            "Scheduler / due jobs ({}/{})",
            visible.len(),
            scheduled_jobs.len()
        ))
        .strong(),
    );
    let visible_row = state
        .scheduler_table
        .focused_row_index
        .or(state.scheduler_table.selected_index())
        .and_then(|index| scheduled_jobs.get(index))
        .map(|job| {
            let last_run = latest_run_for_job(job, job_runs);
            (
                job.name.as_str(),
                layouts.visible_row_text(
                    TableId::DataOperationsScheduler,
                    &DATA_OPERATIONS_SCHEDULER_COLUMNS,
                    &SchedulerColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(job, last_run).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::DataOperationsScheduler,
        &DATA_OPERATIONS_SCHEDULER_COLUMNS,
        state
            .scheduler_table
            .focused_column_index
            .and_then(|index| SchedulerColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.scheduler_table.clear_focus();
    }
    let revision = managed_table_revision(
        layouts,
        TableId::DataOperationsScheduler,
        &DATA_OPERATIONS_SCHEDULER_COLUMNS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("data_operations_scheduler", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(150.0);
    for descriptor in DATA_OPERATIONS_SCHEDULER_COLUMNS {
        table = table.column(managed_column(
            layouts,
            TableId::DataOperationsScheduler,
            &DATA_OPERATIONS_SCHEDULER_COLUMNS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| sortable_header_cell(ui, &mut state.scheduler_table, COL_TITLE, "Job"));
            header.col(|ui| header_cell(ui, "Schedule"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.scheduler_table, COL_NEXT_RUN, "Next")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.scheduler_table, COL_LAST_RUN, "Last")
            });
            header.col(|ui| header_cell(ui, "Lease"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.scheduler_table, COL_SOURCE, "Source")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.scheduler_table, COL_STATUS, "Status")
            });
            header.col(|ui| header_cell(ui, "Action"));
        })
        .body(|mut body| {
            for index in visible {
                let job = scheduled_jobs[index].clone();
                let last_run = latest_run_for_job(&job, job_runs);
                let running = last_run.is_some_and(|run| run.status.as_str() == "RUNNING");
                let status = scheduler_status(&job, last_run);
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.scheduler_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == TableId::DataOperationsScheduler
                            && state.scheduler_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.scheduler_table.is_focused_cell(index, 0),
                        );
                        let response = selectable_label(
                            ui,
                            state,
                            TableId::DataOperationsScheduler,
                            index,
                            &job.name,
                            "Select scheduled job operations context.",
                        );
                        if response.clicked() {
                            focus_scheduler_cell(
                                state,
                                index,
                                &job,
                                last_run,
                                SchedulerColumn::Job,
                            );
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.scheduler_table.is_focused_cell(index, 1),
                        );
                        ui.monospace(&job.cron_schedule);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.scheduler_table.is_focused_cell(index, 2),
                        );
                        ui.label(&job.next_run);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.scheduler_table.is_focused_cell(index, 3),
                        );
                        ui.label(&job.last_run);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.scheduler_table.is_focused_cell(index, 4),
                        );
                        style::status_badge(ui, if running { "RUNNING" } else { "IDLE" });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.scheduler_table.is_focused_cell(index, 5),
                        );
                        style::source_badge(ui, &job.source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.scheduler_table.is_focused_cell(index, 6),
                        );
                        style::status_badge(ui, status);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.scheduler_table.is_focused_cell(index, 7),
                        );
                        ui.horizontal_wrapped(|ui| {
                            if crate::ui::actions::action_button(
                                ui,
                                "Run once",
                                "Queue this local mock job.",
                            )
                            .clicked()
                            {
                                state.select_table(TableId::DataOperationsScheduler, index);
                                *action = Some(DataOperationsAction::RunJob(job.name.clone()));
                            }
                            if crate::ui::actions::action_button(ui, "Open", "Open Jobs page.")
                                .clicked()
                            {
                                state.select_table(TableId::DataOperationsScheduler, index);
                                *action = Some(DataOperationsAction::Navigate(Page::Jobs));
                            }
                            if crate::ui::actions::action_button(ui, "Copy", "Copy worker command.")
                                .clicked()
                            {
                                state.select_table(TableId::DataOperationsScheduler, index);
                                *action = Some(DataOperationsAction::Copy {
                                    label: format!("worker {}", job.name),
                                    text: format!("run {}", job.name.replace(' ', "_")),
                                });
                            }
                        });
                    });
                });
            }
        });
}

fn source_budgets_table(
    ui: &mut egui::Ui,
    operations: &DataOperationsSnapshot,
    state: &mut DataOperationsState,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<DataOperationsAction>,
) {
    let visible = visible_source_budget_indices(&operations.source_budgets, state);
    state.source_budget_table.retain_visible(&visible);
    ui.label(
        egui::RichText::new(format!(
            "Source budgets ({}/{})",
            visible.len(),
            operations.source_budgets.len()
        ))
        .strong(),
    );
    let visible_row = state
        .source_budget_table
        .focused_row_index
        .or(state.source_budget_table.selected_index())
        .and_then(|index| operations.source_budgets.get(index))
        .map(|budget| {
            let mut cells = SourceBudgetColumn::ALL
                .iter()
                .map(|column| (column.key(), column.payload(budget).1))
                .collect::<Vec<_>>();
            cells.push(("actions", budget.capabilities.join(", ")));
            (
                budget.source.as_str(),
                layouts.visible_row_text(
                    TableId::DataOperationsSources,
                    &DATA_OPERATIONS_SOURCE_COLUMNS,
                    &cells,
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::DataOperationsSources,
        &DATA_OPERATIONS_SOURCE_COLUMNS,
        state
            .source_budget_table
            .focused_column_index
            .and_then(|index| SourceBudgetColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.source_budget_table.clear_focus();
    }
    let revision = managed_table_revision(
        layouts,
        TableId::DataOperationsSources,
        &DATA_OPERATIONS_SOURCE_COLUMNS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("data_operations_source_budgets", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(170.0);
    for descriptor in DATA_OPERATIONS_SOURCE_COLUMNS {
        table = table.column(managed_column(
            layouts,
            TableId::DataOperationsSources,
            &DATA_OPERATIONS_SOURCE_COLUMNS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.source_budget_table,
                    COL_SOURCE,
                    SourceBudgetColumn::Source.label(),
                )
            });
            header.col(|ui| header_cell(ui, "Enabled"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.source_budget_table, COL_STATUS, "Status")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.source_budget_table, COL_REQUESTS, "Requests")
            });
            header.col(|ui| header_cell(ui, "Delay"));
            header.col(|ui| header_cell(ui, "Backoff"));
            header.col(|ui| header_cell(ui, "Failures"));
            header.col(|ui| header_cell(ui, "Cache hits"));
            header.col(|ui| header_cell(ui, "Actions / capabilities"));
        })
        .body(|mut body| {
            for index in visible {
                let budget = operations.source_budgets[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.source_budget_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == TableId::DataOperationsSources
                            && state.source_budget_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .source_budget_table
                                .is_focused_cell(index, SourceBudgetColumn::Source.index()),
                        );
                        let response = selectable_label(
                            ui,
                            state,
                            TableId::DataOperationsSources,
                            index,
                            &budget.source,
                            "Select source budget.",
                        );
                        if response.clicked() {
                            focus_source_budget_cell(
                                state,
                                index,
                                &budget,
                                SourceBudgetColumn::Source,
                            );
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .source_budget_table
                                .is_focused_cell(index, SourceBudgetColumn::Enabled.index()),
                        );
                        ui.label(bool_text(budget.enabled));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .source_budget_table
                                .is_focused_cell(index, SourceBudgetColumn::Status.index()),
                        );
                        style::status_badge(ui, budget.status.as_str());
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .source_budget_table
                                .is_focused_cell(index, SourceBudgetColumn::Requests.index()),
                        );
                        ui.monospace(request_window(&budget));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .source_budget_table
                                .is_focused_cell(index, SourceBudgetColumn::Delay.index()),
                        );
                        ui.label(&budget.min_delay);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .source_budget_table
                                .is_focused_cell(index, SourceBudgetColumn::Backoff.index()),
                        );
                        ui.label(budget.backoff_until.as_deref().unwrap_or("-"));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .source_budget_table
                                .is_focused_cell(index, SourceBudgetColumn::Failures.index()),
                        );
                        ui.label(format_number(f64::from(budget.recent_failures), 0));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .source_budget_table
                                .is_focused_cell(index, SourceBudgetColumn::CacheHits.index()),
                        );
                        ui.label(format_number(f64::from(budget.cache_hits), 0));
                    });
                    row.col(|ui| {
                        ui.horizontal_wrapped(|ui| {
                            if crate::ui::actions::action_button(ui, "Logs", "Open fetch logs.")
                                .clicked()
                            {
                                state.select_table(TableId::DataOperationsSources, index);
                                *action = Some(DataOperationsAction::Feedback(format!(
                                    "OPS: logs {}",
                                    budget.source
                                )));
                            }
                            if crate::ui::actions::action_button(ui, "Copy", "Copy source budget.")
                                .clicked()
                            {
                                state.select_table(TableId::DataOperationsSources, index);
                                *action = Some(DataOperationsAction::Copy {
                                    label: budget.source.clone(),
                                    text: source_budget_copy_text(&budget),
                                });
                            }
                            ui.label(budget.capabilities.join(", "));
                        });
                    });
                });
            }
        });
}

fn fetch_logs_table(
    ui: &mut egui::Ui,
    operations: &DataOperationsSnapshot,
    state: &mut DataOperationsState,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<DataOperationsAction>,
) {
    let visible = visible_fetch_log_indices(&operations.fetch_logs, state);
    state.fetch_log_table.retain_visible(&visible);
    ui.label(
        egui::RichText::new(format!(
            "Recent fetch logs ({}/{})",
            visible.len(),
            operations.fetch_logs.len()
        ))
        .strong(),
    );
    let visible_row = state
        .fetch_log_table
        .focused_row_index
        .or(state.fetch_log_table.selected_index())
        .and_then(|index| operations.fetch_logs.get(index))
        .map(|log| {
            (
                log.id.as_str(),
                layouts.visible_row_text(
                    TableId::DataOperationsFetchLogs,
                    &DATA_OPERATIONS_FETCH_COLUMNS,
                    &FetchLogColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(log).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::DataOperationsFetchLogs,
        &DATA_OPERATIONS_FETCH_COLUMNS,
        state
            .fetch_log_table
            .focused_column_index
            .and_then(|index| FetchLogColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.fetch_log_table.clear_focus();
    }
    let revision = managed_table_revision(
        layouts,
        TableId::DataOperationsFetchLogs,
        &DATA_OPERATIONS_FETCH_COLUMNS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("data_operations_fetch_logs", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(170.0);
    for descriptor in DATA_OPERATIONS_FETCH_COLUMNS {
        table = table.column(managed_column(
            layouts,
            TableId::DataOperationsFetchLogs,
            &DATA_OPERATIONS_FETCH_COLUMNS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| sortable_header_cell(ui, &mut state.fetch_log_table, COL_TIME, "Time"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.fetch_log_table, COL_SOURCE, "Source")
            });
            header.col(|ui| sortable_header_cell(ui, &mut state.fetch_log_table, COL_KIND, "Kind"));
            header.col(|ui| sortable_header_cell(ui, &mut state.fetch_log_table, COL_KEY, "Key"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.fetch_log_table, COL_STATUS, "Status")
            });
            header.col(|ui| header_cell(ui, "HTTP"));
            header
                .col(|ui| sortable_header_cell(ui, &mut state.fetch_log_table, COL_DURATION, "ms"));
            header.col(|ui| header_cell(ui, "Cache"));
            header.col(|ui| header_cell(ui, "Limited"));
            header.col(|ui| header_cell(ui, FetchLogColumn::Error.label()));
        })
        .body(|mut body| {
            for index in visible {
                let log = operations.fetch_logs[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.fetch_log_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == TableId::DataOperationsFetchLogs
                            && state.fetch_log_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Time.index()),
                        );
                        ui.label(&log.time);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Source.index()),
                        );
                        style::source_badge(ui, &log.source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Kind.index()),
                        );
                        ui.label(&log.request_kind);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Key.index()),
                        );
                        let key = fetch_log_display_key(&log);
                        let response = selectable_label(
                            ui,
                            state,
                            TableId::DataOperationsFetchLogs,
                            index,
                            &key,
                            "Secrets are masked in fetch-log display and copy payloads.",
                        )
                        .on_hover_text(&key);
                        if response.clicked() {
                            focus_fetch_log_cell(state, index, &log, FetchLogColumn::Key);
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Status.index()),
                        );
                        style::status_badge(ui, log.status.as_str());
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Http.index()),
                        );
                        ui.monospace(
                            log.http_status
                                .map(|status| status.to_string())
                                .unwrap_or_else(|| "-".to_owned()),
                        );
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Duration.index()),
                        );
                        ui.label(format_number(f64::from(log.duration_ms), 0));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Cache.index()),
                        );
                        ui.label(bool_text(log.cache_hit));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Limited.index()),
                        );
                        ui.label(bool_text(log.rate_limited));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .fetch_log_table
                                .is_focused_cell(index, FetchLogColumn::Error.index()),
                        );
                        ui.horizontal_wrapped(|ui| {
                            ui.label(log.error.as_deref().unwrap_or("-"));
                            if crate::ui::actions::action_button(
                                ui,
                                "Copy",
                                "Copy fetch log with secrets masked.",
                            )
                            .clicked()
                            {
                                state.select_table(TableId::DataOperationsFetchLogs, index);
                                *action = Some(DataOperationsAction::Copy {
                                    label: log.id.clone(),
                                    text: fetch_log_copy_text(&log),
                                });
                            }
                        });
                    });
                });
            }
        });
}

fn constituent_coverage_table(
    ui: &mut egui::Ui,
    operations: &DataOperationsSnapshot,
    state: &mut DataOperationsState,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<DataOperationsAction>,
) {
    let visible = visible_constituent_indices(&operations.constituent_coverage, state);
    state.constituent_table.selection.retain_visible(&visible);
    ui.label(
        egui::RichText::new(format!(
            "Constituent identity & price coverage ({}/{})",
            visible.len(),
            operations.constituent_coverage.len()
        ))
        .strong(),
    );
    let visible_row = state
        .constituent_table
        .focused_row_index
        .or(state.constituent_table.selected_index())
        .and_then(|index| operations.constituent_coverage.get(index))
        .map(|row| {
            (
                row.holding_ticker.as_str(),
                layouts.visible_row_text(
                    TableId::DataOperationsConstituents,
                    &DATA_OPERATIONS_CONSTITUENT_COLUMNS,
                    &ConstituentColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(row).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::DataOperationsConstituents,
        &DATA_OPERATIONS_CONSTITUENT_COLUMNS,
        state
            .constituent_table
            .focused_column_index
            .and_then(|index| ConstituentColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.constituent_table.clear_focus();
    }
    let revision = managed_table_revision(
        layouts,
        TableId::DataOperationsConstituents,
        &DATA_OPERATIONS_CONSTITUENT_COLUMNS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("data_operations_constituents", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(190.0);
    for descriptor in DATA_OPERATIONS_CONSTITUENT_COLUMNS {
        table = table.column(managed_column(
            layouts,
            TableId::DataOperationsConstituents,
            &DATA_OPERATIONS_CONSTITUENT_COLUMNS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header
                .col(|ui| sortable_header_cell(ui, &mut state.constituent_table, COL_FUND, "Fund"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.constituent_table, COL_HOLDING, "Holding")
            });
            header.col(|ui| header_cell(ui, "Ticker"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.constituent_table, COL_WEIGHT, "Weight")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.constituent_table, COL_IDENTITY, "Identity")
            });
            header.col(|ui| header_cell(ui, "Instrument/listing"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.constituent_table, COL_PRICE, "Price")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.constituent_table, COL_PRICE_DATE, "Date")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.constituent_table, COL_SOURCE, "Source")
            });
            header.col(|ui| header_cell(ui, "Next action"));
        })
        .body(|mut body| {
            for index in visible {
                let row_data = operations.constituent_coverage[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.constituent_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == TableId::DataOperationsConstituents
                            && state.constituent_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 0),
                        );
                        ui.monospace(&row_data.fund_ticker);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 1),
                        );
                        let response = selectable_label(
                            ui,
                            state,
                            TableId::DataOperationsConstituents,
                            index,
                            &row_data.holding_name,
                            "Select constituent readiness row.",
                        );
                        if response.double_clicked() {
                            *action = Some(DataOperationsAction::OpenSubject {
                                subject: row_data.subject.clone(),
                                label: row_data.holding_ticker.clone(),
                            });
                        }
                        if response.clicked() {
                            focus_constituent_cell(
                                state,
                                index,
                                &row_data,
                                ConstituentColumn::Holding,
                            );
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 2),
                        );
                        ui.monospace(&row_data.holding_ticker);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 3),
                        );
                        ui.label(format_pct(row_data.weight_pct));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 4),
                        );
                        style::status_badge(ui, row_data.identity_status.as_str());
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 5),
                        );
                        ui.label(format!(
                            "{}/{}",
                            row_data.instrument_id.as_deref().unwrap_or("-"),
                            row_data.listing_id.as_deref().unwrap_or("-")
                        ))
                        .on_hover_text(format!(
                            "Instrument: {}\nListing: {}",
                            row_data.instrument_id.as_deref().unwrap_or("-"),
                            row_data.listing_id.as_deref().unwrap_or("-")
                        ));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 6),
                        );
                        ui.monospace(
                            row_data
                                .latest_price
                                .map(|value| format_number(value, 2))
                                .unwrap_or_else(|| "-".to_owned()),
                        );
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 7),
                        );
                        ui.label(row_data.price_date.as_deref().unwrap_or("-"));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 8),
                        );
                        style::source_badge(ui, &row_data.price_source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.constituent_table.is_focused_cell(index, 9),
                        );
                        ui.horizontal_wrapped(|ui| {
                            style::status_badge(ui, row_data.price_status.as_str());
                            ui.label(&row_data.next_action);
                        });
                    });
                });
            }
        });
}

fn diagnostics_table(
    ui: &mut egui::Ui,
    operations: &DataOperationsSnapshot,
    state: &mut DataOperationsState,
    action: &mut Option<DataOperationsAction>,
) {
    let visible = visible_diagnostic_indices(&operations.diagnostic_issues, state);
    state.diagnostic_table.retain_visible(&visible);
    ui.label(
        egui::RichText::new(format!(
            "Diagnostics / blocking issues ({}/{})",
            visible.len(),
            operations.diagnostic_issues.len()
        ))
        .strong(),
    );
    TableBuilder::new(ui)
        .id_salt("data_operations_diagnostics")
        .striped(true)
        .resizable(true)
        .max_scroll_height(160.0)
        .column(Column::initial(82.0).at_least(64.0))
        .column(Column::initial(190.0).at_least(130.0).clip(true))
        .column(Column::initial(110.0).at_least(82.0))
        .column(Column::initial(110.0).at_least(82.0))
        .column(Column::initial(150.0).at_least(110.0).clip(true))
        .column(Column::remainder().at_least(220.0).clip(true))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.diagnostic_table, COL_SEVERITY, "Severity")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.diagnostic_table, COL_TITLE, "Issue")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.diagnostic_table, COL_STATUS, "Status")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.diagnostic_table, COL_SOURCE, "Source")
            });
            header.col(|ui| header_cell(ui, DiagnosticColumn::Recommended.label()));
            header.col(|ui| header_cell(ui, "Detail / actions"));
        })
        .body(|mut body| {
            for index in visible {
                let issue = operations.diagnostic_issues[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.diagnostic_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == TableId::DataOperationsDiagnostics
                            && state.diagnostic_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .diagnostic_table
                                .is_focused_cell(index, DiagnosticColumn::Severity.index()),
                        );
                        style::alert_severity_badge(ui, issue.severity);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .diagnostic_table
                                .is_focused_cell(index, DiagnosticColumn::Issue.index()),
                        );
                        let response = selectable_label(
                            ui,
                            state,
                            TableId::DataOperationsDiagnostics,
                            index,
                            &issue.title,
                            "Select diagnostic issue.",
                        );
                        if response.clicked() {
                            focus_diagnostic_cell(state, index, &issue, DiagnosticColumn::Issue);
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .diagnostic_table
                                .is_focused_cell(index, DiagnosticColumn::Status.index()),
                        );
                        style::status_badge(ui, issue.status.as_str());
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .diagnostic_table
                                .is_focused_cell(index, DiagnosticColumn::Source.index()),
                        );
                        style::source_badge(ui, &issue.source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .diagnostic_table
                                .is_focused_cell(index, DiagnosticColumn::Recommended.index()),
                        );
                        ui.label(&issue.recommended_action);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .diagnostic_table
                                .is_focused_cell(index, DiagnosticColumn::Detail.index()),
                        );
                        ui.horizontal_wrapped(|ui| {
                            ui.label(&issue.detail).on_hover_text(&issue.detail);
                            if crate::ui::actions::action_button(
                                ui,
                                "Copy",
                                "Copy diagnostic issue.",
                            )
                            .clicked()
                            {
                                state.select_table(TableId::DataOperationsDiagnostics, index);
                                *action = Some(DataOperationsAction::Copy {
                                    label: issue.id.clone(),
                                    text: diagnostic_copy_text(&issue),
                                });
                            }
                        });
                    });
                });
            }
        });
}

fn focus_plan_cell(
    state: &mut DataOperationsState,
    index: usize,
    item: &MarketDataPlanItem,
    column: PlanColumn,
) {
    let (display, raw) = column.payload(item);
    state
        .plan_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = TableId::DataOperationsPlan;
}

fn sync_plan_focus(items: &[MarketDataPlanItem], state: &mut DataOperationsState) {
    let (Some(row_index), Some(column_index)) = (
        state.plan_table.focused_row_index,
        state.plan_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(item), Some(column)) = (
        items.get(row_index),
        PlanColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(item);
    state
        .plan_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn focus_source_budget_cell(
    state: &mut DataOperationsState,
    index: usize,
    budget: &SourceBudget,
    column: SourceBudgetColumn,
) {
    let (display, raw) = column.payload(budget);
    state
        .source_budget_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = TableId::DataOperationsSources;
}

fn sync_source_budget_focus(budgets: &[SourceBudget], state: &mut DataOperationsState) {
    let (Some(row_index), Some(column_index)) = (
        state.source_budget_table.focused_row_index,
        state.source_budget_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(budget), Some(column)) = (
        budgets.get(row_index),
        SourceBudgetColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(budget);
    state
        .source_budget_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn focus_fetch_log_cell(
    state: &mut DataOperationsState,
    index: usize,
    log: &SourceFetchLog,
    column: FetchLogColumn,
) {
    let (display, raw) = column.payload(log);
    state
        .fetch_log_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = TableId::DataOperationsFetchLogs;
}

fn sync_fetch_log_focus(logs: &[SourceFetchLog], state: &mut DataOperationsState) {
    let (Some(row_index), Some(column_index)) = (
        state.fetch_log_table.focused_row_index,
        state.fetch_log_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(log), Some(column)) = (
        logs.get(row_index),
        FetchLogColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(log);
    state
        .fetch_log_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn focus_diagnostic_cell(
    state: &mut DataOperationsState,
    index: usize,
    issue: &DataDiagnosticIssue,
    column: DiagnosticColumn,
) {
    let (display, raw) = column.payload(issue);
    state
        .diagnostic_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = TableId::DataOperationsDiagnostics;
}

fn sync_diagnostic_focus(issues: &[DataDiagnosticIssue], state: &mut DataOperationsState) {
    let (Some(row_index), Some(column_index)) = (
        state.diagnostic_table.focused_row_index,
        state.diagnostic_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(issue), Some(column)) = (
        issues.get(row_index),
        DiagnosticColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(issue);
    state
        .diagnostic_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn focus_scheduler_cell(
    state: &mut DataOperationsState,
    index: usize,
    job: &ScheduledJob,
    last_run: Option<&JobRun>,
    column: SchedulerColumn,
) {
    let (display, raw) = column.payload(job, last_run);
    state
        .scheduler_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = TableId::DataOperationsScheduler;
}

fn sync_scheduler_focus(jobs: &[ScheduledJob], runs: &[JobRun], state: &mut DataOperationsState) {
    let (Some(row_index), Some(column_index)) = (
        state.scheduler_table.focused_row_index,
        state.scheduler_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(job), Some(column)) = (
        jobs.get(row_index),
        SchedulerColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(job, latest_run_for_job(job, runs));
    state
        .scheduler_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn focus_constituent_cell(
    state: &mut DataOperationsState,
    index: usize,
    row: &ConstituentReadinessRow,
    column: ConstituentColumn,
) {
    let (display, raw) = column.payload(row);
    state
        .constituent_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = TableId::DataOperationsConstituents;
}

fn sync_constituent_focus(rows: &[ConstituentReadinessRow], state: &mut DataOperationsState) {
    let (Some(row_index), Some(column_index)) = (
        state.constituent_table.focused_row_index,
        state.constituent_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(row), Some(column)) = (
        rows.get(row_index),
        ConstituentColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(row);
    state
        .constituent_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn focus_api_section_cell(
    state: &mut DataOperationsState,
    index: usize,
    section: &BackendSectionStatus,
    column: ApiSectionColumn,
) {
    let (display, raw) = column.payload(section);
    state
        .api_sections_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = TableId::DataOperationsApiSections;
}

fn sync_api_section_focus(sections: &[BackendSectionStatus], state: &mut DataOperationsState) {
    let (Some(row_index), Some(column_index)) = (
        state.api_sections_table.focused_row_index,
        state.api_sections_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(section), Some(column)) = (
        sections.get(row_index),
        ApiSectionColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(section);
    state
        .api_sections_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn selectable_label(
    ui: &mut egui::Ui,
    state: &mut DataOperationsState,
    table_id: TableId,
    index: usize,
    label: &str,
    tooltip: &str,
) -> egui::Response {
    let selected = match table_id {
        TableId::DataOperationsActions => state.actions_table.selection.is_selected(index),
        TableId::DataOperationsPlan => state.plan_table.selection.is_selected(index),
        TableId::DataOperationsScheduler => state.scheduler_table.selection.is_selected(index),
        TableId::DataOperationsSources => state.source_budget_table.selection.is_selected(index),
        TableId::DataOperationsFetchLogs => state.fetch_log_table.selection.is_selected(index),
        TableId::DataOperationsConstituents => state.constituent_table.selection.is_selected(index),
        TableId::DataOperationsDiagnostics => state.diagnostic_table.selection.is_selected(index),
        TableId::DataOperationsReadiness
        | TableId::PortfolioPositions
        | TableId::EtfsFunds
        | TableId::ExposureCountries
        | TableId::ExposureSectors
        | TableId::ExposureCurrencies
        | TableId::ExposureTopHoldings
        | TableId::ExposureDiagnostics
        | TableId::Holdings
        | TableId::Documents
        | TableId::Dividends
        | TableId::ScheduledJobs
        | TableId::JobRuns
        | TableId::Alerts
        | TableId::ChartSeriesData
        | TableId::SearchResults
        | TableId::DataOperationsApiSections
        | TableId::FundDetailListings
        | TableId::FundDetailHoldings
        | TableId::FundDetailDistributions
        | TableId::FundDetailDocuments => false,
    } && state.active_table == table_id;

    let response = ui
        .selectable_label(selected, label)
        .on_hover_text(tooltip)
        .on_hover_cursor(egui::CursorIcon::PointingHand);
    if response.clicked() {
        state.select_table(table_id, index);
    }
    response
}

fn visible_action_indices(
    actions: &[RecommendedDataAction],
    state: &DataOperationsState,
) -> Vec<usize> {
    let mut indices = actions
        .iter()
        .enumerate()
        .filter(|(_, action)| action_matches(action, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_action_indices(actions, &mut indices, state.actions_table.sort.as_ref());
    indices
}

fn visible_plan_indices(items: &[MarketDataPlanItem], state: &DataOperationsState) -> Vec<usize> {
    let mut indices = items
        .iter()
        .enumerate()
        .filter(|(_, item)| plan_item_matches(item, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_plan_indices(items, &mut indices, state.plan_table.sort.as_ref());
    indices
}

fn visible_scheduler_indices(
    jobs: &[ScheduledJob],
    runs: &[JobRun],
    state: &DataOperationsState,
) -> Vec<usize> {
    let mut indices = jobs
        .iter()
        .enumerate()
        .filter(|(_, job)| scheduler_job_matches(job, runs, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_scheduler_indices(
        jobs,
        runs,
        &mut indices,
        state.scheduler_table.sort.as_ref(),
    );
    indices
}

fn visible_source_budget_indices(
    budgets: &[SourceBudget],
    state: &DataOperationsState,
) -> Vec<usize> {
    let mut indices = budgets
        .iter()
        .enumerate()
        .filter(|(_, budget)| source_budget_matches(budget, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_source_budget_indices(
        budgets,
        &mut indices,
        state.source_budget_table.sort.as_ref(),
    );
    indices
}

fn visible_fetch_log_indices(logs: &[SourceFetchLog], state: &DataOperationsState) -> Vec<usize> {
    let mut indices = logs
        .iter()
        .enumerate()
        .filter(|(_, log)| fetch_log_matches(log, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_fetch_log_indices(logs, &mut indices, state.fetch_log_table.sort.as_ref());
    indices
}

fn visible_constituent_indices(
    rows: &[ConstituentReadinessRow],
    state: &DataOperationsState,
) -> Vec<usize> {
    let mut indices = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| constituent_matches(row, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_constituent_indices(rows, &mut indices, state.constituent_table.sort.as_ref());
    indices
}

fn visible_diagnostic_indices(
    issues: &[DataDiagnosticIssue],
    state: &DataOperationsState,
) -> Vec<usize> {
    let mut indices = issues
        .iter()
        .enumerate()
        .filter(|(_, issue)| diagnostic_matches(issue, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_diagnostic_indices(issues, &mut indices, state.diagnostic_table.sort.as_ref());
    indices
}

fn action_matches(action: &RecommendedDataAction, filter: &str) -> bool {
    any_contains_ci(
        [
            action.label.as_str(),
            action.target.as_str(),
            action.reason.as_str(),
            action.status.as_str(),
            action.source.as_str(),
            action.command.as_str(),
        ],
        filter,
    )
}

fn plan_item_matches(item: &MarketDataPlanItem, filter: &str) -> bool {
    any_contains_ci(
        [
            item.id.as_str(),
            item.item_type.as_str(),
            item.subject_label.as_str(),
            item.reason.as_str(),
            item.source.as_str(),
            item.status.as_str(),
            item.blocker.as_deref().unwrap_or("-"),
            item.next_action.as_str(),
        ],
        filter,
    )
}

fn scheduler_job_matches(job: &ScheduledJob, runs: &[JobRun], filter: &str) -> bool {
    let status = scheduler_status(job, latest_run_for_job(job, runs));
    any_contains_ci(
        [
            job.name.as_str(),
            job.job_type.as_str(),
            job.source.as_str(),
            job.cron_schedule.as_str(),
            job.last_run.as_str(),
            job.next_run.as_str(),
            status,
        ],
        filter,
    )
}

fn source_budget_matches(budget: &SourceBudget, filter: &str) -> bool {
    let capabilities = budget.capabilities.join(", ");
    any_contains_ci(
        [
            budget.source.as_str(),
            budget.status.as_str(),
            budget.window.as_str(),
            budget.min_delay.as_str(),
            budget.backoff_until.as_deref().unwrap_or("-"),
            budget.next_allowed.as_str(),
            capabilities.as_str(),
        ],
        filter,
    )
}

fn fetch_log_matches(log: &SourceFetchLog, filter: &str) -> bool {
    let key = fetch_log_display_key(log);
    any_contains_ci(
        [
            log.id.as_str(),
            log.time.as_str(),
            log.source.as_str(),
            log.request_kind.as_str(),
            key.as_str(),
            log.status.as_str(),
            log.error.as_deref().unwrap_or("-"),
        ],
        filter,
    )
}

fn constituent_matches(row: &ConstituentReadinessRow, filter: &str) -> bool {
    any_contains_ci(
        [
            row.fund_ticker.as_str(),
            row.holding_name.as_str(),
            row.holding_ticker.as_str(),
            row.identity_status.as_str(),
            row.instrument_id.as_deref().unwrap_or("-"),
            row.listing_id.as_deref().unwrap_or("-"),
            row.price_source.as_str(),
            row.price_status.as_str(),
            row.next_action.as_str(),
        ],
        filter,
    )
}

fn diagnostic_matches(issue: &DataDiagnosticIssue, filter: &str) -> bool {
    any_contains_ci(
        [
            issue.severity.as_str(),
            issue.title.as_str(),
            issue.detail.as_str(),
            issue.status.as_str(),
            issue.source.as_str(),
            issue.recommended_action.as_str(),
            issue.related_page.as_str(),
        ],
        filter,
    )
}

fn sort_action_indices(
    actions: &[RecommendedDataAction],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };
    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_actions(
                &actions[*left],
                &actions[*right],
                &sort.column,
            ))
            .then_with(|| actions[*left].label.cmp(&actions[*right].label))
    });
}

fn sort_plan_indices(items: &[MarketDataPlanItem], indices: &mut [usize], sort: Option<&SortSpec>) {
    let Some(sort) = sort else {
        return;
    };
    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_plan_items(
                &items[*left],
                &items[*right],
                &sort.column,
            ))
            .then_with(|| items[*left].id.cmp(&items[*right].id))
    });
}

fn sort_scheduler_indices(
    jobs: &[ScheduledJob],
    runs: &[JobRun],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };
    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_scheduler_jobs(
                &jobs[*left],
                latest_run_for_job(&jobs[*left], runs),
                &jobs[*right],
                latest_run_for_job(&jobs[*right], runs),
                &sort.column,
            ))
            .then_with(|| jobs[*left].name.cmp(&jobs[*right].name))
    });
}

fn sort_source_budget_indices(
    budgets: &[SourceBudget],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };
    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_source_budgets(
                &budgets[*left],
                &budgets[*right],
                &sort.column,
            ))
            .then_with(|| budgets[*left].source.cmp(&budgets[*right].source))
    });
}

fn sort_fetch_log_indices(logs: &[SourceFetchLog], indices: &mut [usize], sort: Option<&SortSpec>) {
    let Some(sort) = sort else {
        return;
    };
    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_fetch_logs(
                &logs[*left],
                &logs[*right],
                &sort.column,
            ))
            .then_with(|| logs[*left].id.cmp(&logs[*right].id))
    });
}

fn sort_constituent_indices(
    rows: &[ConstituentReadinessRow],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };
    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_constituents(
                &rows[*left],
                &rows[*right],
                &sort.column,
            ))
            .then_with(|| rows[*left].holding_ticker.cmp(&rows[*right].holding_ticker))
    });
}

fn sort_diagnostic_indices(
    issues: &[DataDiagnosticIssue],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };
    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_diagnostics(
                &issues[*left],
                &issues[*right],
                &sort.column,
            ))
            .then_with(|| issues[*left].id.cmp(&issues[*right].id))
    });
}

fn compare_actions(
    left: &RecommendedDataAction,
    right: &RecommendedDataAction,
    column: &str,
) -> Ordering {
    match column {
        COL_PRIORITY => left.priority.cmp(&right.priority),
        COL_TITLE => left.label.cmp(&right.label),
        COL_SUBJECT => left.target.cmp(&right.target),
        COL_STATUS => status_rank(left.status).cmp(&status_rank(right.status)),
        COL_SOURCE => left.source.cmp(&right.source),
        _ => Ordering::Equal,
    }
}

fn compare_plan_items(
    left: &MarketDataPlanItem,
    right: &MarketDataPlanItem,
    column: &str,
) -> Ordering {
    match column {
        COL_PRIORITY => left.priority.cmp(&right.priority),
        COL_TYPE => left.item_type.cmp(&right.item_type),
        COL_SUBJECT => left.subject_label.cmp(&right.subject_label),
        COL_SOURCE => left.source.cmp(&right.source),
        COL_REQUESTS => left.estimated_requests.cmp(&right.estimated_requests),
        COL_STATUS => status_rank(left.status).cmp(&status_rank(right.status)),
        _ => Ordering::Equal,
    }
}

fn compare_scheduler_jobs(
    left: &ScheduledJob,
    left_run: Option<&JobRun>,
    right: &ScheduledJob,
    right_run: Option<&JobRun>,
    column: &str,
) -> Ordering {
    match column {
        COL_TITLE => left.name.cmp(&right.name),
        COL_NEXT_RUN => left.next_run.cmp(&right.next_run),
        COL_LAST_RUN => left.last_run.cmp(&right.last_run),
        COL_SOURCE => left.source.cmp(&right.source),
        COL_STATUS => scheduler_status(left, left_run).cmp(scheduler_status(right, right_run)),
        _ => Ordering::Equal,
    }
}

fn compare_source_budgets(left: &SourceBudget, right: &SourceBudget, column: &str) -> Ordering {
    match column {
        COL_SOURCE => left.source.cmp(&right.source),
        COL_STATUS => status_rank(left.status).cmp(&status_rank(right.status)),
        COL_REQUESTS => left.requests_used.cmp(&right.requests_used),
        _ => Ordering::Equal,
    }
}

fn compare_fetch_logs(left: &SourceFetchLog, right: &SourceFetchLog, column: &str) -> Ordering {
    match column {
        COL_TIME => left.time.cmp(&right.time),
        COL_SOURCE => left.source.cmp(&right.source),
        COL_KIND => left.request_kind.cmp(&right.request_kind),
        COL_KEY => fetch_log_display_key(left).cmp(&fetch_log_display_key(right)),
        COL_STATUS => status_rank(left.status).cmp(&status_rank(right.status)),
        COL_DURATION => left.duration_ms.cmp(&right.duration_ms),
        _ => Ordering::Equal,
    }
}

fn compare_constituents(
    left: &ConstituentReadinessRow,
    right: &ConstituentReadinessRow,
    column: &str,
) -> Ordering {
    match column {
        COL_FUND => left.fund_ticker.cmp(&right.fund_ticker),
        COL_HOLDING => left.holding_name.cmp(&right.holding_name),
        COL_WEIGHT => left.weight_pct.total_cmp(&right.weight_pct),
        COL_IDENTITY => status_rank(left.identity_status).cmp(&status_rank(right.identity_status)),
        COL_PRICE => left
            .latest_price
            .partial_cmp(&right.latest_price)
            .unwrap_or(Ordering::Equal),
        COL_PRICE_DATE => left.price_date.cmp(&right.price_date),
        COL_SOURCE => left.price_source.cmp(&right.price_source),
        _ => Ordering::Equal,
    }
}

fn compare_diagnostics(
    left: &DataDiagnosticIssue,
    right: &DataDiagnosticIssue,
    column: &str,
) -> Ordering {
    match column {
        COL_SEVERITY => left.severity.as_str().cmp(right.severity.as_str()),
        COL_TITLE => left.title.cmp(&right.title),
        COL_STATUS => status_rank(left.status).cmp(&status_rank(right.status)),
        COL_SOURCE => left.source.cmp(&right.source),
        _ => Ordering::Equal,
    }
}

fn latest_run_for_job<'a>(job: &ScheduledJob, runs: &'a [JobRun]) -> Option<&'a JobRun> {
    runs.iter()
        .filter(|run| {
            run.job_type.eq_ignore_ascii_case(&job.job_type)
                || run.source.eq_ignore_ascii_case(&job.source)
        })
        .max_by(|left, right| left.started.cmp(&right.started))
}

fn scheduler_status(job: &ScheduledJob, latest_run: Option<&JobRun>) -> &'static str {
    if !job.active {
        return "DISABLED";
    }
    if let Some(run) = latest_run {
        if run.status.as_str() == "RUNNING" {
            return "RUNNING";
        }
        if run.status.as_str() == "FAILED" {
            return "FAILED";
        }
    }
    if job.next_run.as_str() <= "2026-06-23" {
        "NEEDED"
    } else {
        "PLANNED"
    }
}

fn request_window(budget: &SourceBudget) -> String {
    if budget.requests_limit == 0 {
        budget.window.clone()
    } else {
        format!(
            "{}/{} {}",
            budget.requests_used, budget.requests_limit, budget.window
        )
    }
}

fn status_rank(status: DataOperationStatus) -> u8 {
    match status {
        DataOperationStatus::Ready | DataOperationStatus::Ok | DataOperationStatus::Fresh => 0,
        DataOperationStatus::Mock | DataOperationStatus::Planned | DataOperationStatus::Unknown => {
            1
        }
        DataOperationStatus::Running | DataOperationStatus::Needed => 2,
        DataOperationStatus::Partial | DataOperationStatus::Stale => 3,
        DataOperationStatus::Missing | DataOperationStatus::Ambiguous => 4,
        DataOperationStatus::Failed
        | DataOperationStatus::Blocked
        | DataOperationStatus::BudgetBlocked => 5,
    }
}

pub fn readiness_copy_text(stage: &ReadinessStage) -> String {
    [
        stage.key.clone(),
        stage.label.clone(),
        stage.status.as_str().to_owned(),
        stage
            .coverage_pct
            .map(|value| format!("{value:.1}%"))
            .unwrap_or_else(|| "-".to_owned()),
        stage.count_label.clone(),
        stage.source.clone(),
        stage.freshness.clone(),
        stage.recommended_action.clone(),
        stage.detail.clone(),
    ]
    .join("\t")
}

pub fn recommended_action_copy_text(action: &RecommendedDataAction) -> String {
    [
        action.priority.to_string(),
        action.label.clone(),
        action.target.clone(),
        action.status.as_str().to_owned(),
        action.source.clone(),
        action.command.clone(),
        action.reason.clone(),
    ]
    .join("\t")
}

pub fn plan_item_copy_text(item: &MarketDataPlanItem) -> String {
    [
        item.id.clone(),
        item.priority.to_string(),
        item.item_type.clone(),
        item.subject_label.clone(),
        item.reason.clone(),
        item.source.clone(),
        item.estimated_requests.to_string(),
        item.status.as_str().to_owned(),
        item.blocker.clone().unwrap_or_else(|| "-".to_owned()),
        item.next_action.clone(),
    ]
    .join("\t")
}

pub fn scheduled_job_copy_text(job: &ScheduledJob) -> String {
    [
        job.name.clone(),
        job.job_type.clone(),
        job.source.clone(),
        job.cron_schedule.clone(),
        bool_text(job.active).to_owned(),
        job.last_run.clone(),
        job.next_run.clone(),
    ]
    .join("\t")
}

pub fn source_budget_copy_text(budget: &SourceBudget) -> String {
    [
        budget.source.clone(),
        bool_text(budget.enabled).to_owned(),
        budget.status.as_str().to_owned(),
        request_window(budget),
        budget.min_delay.clone(),
        budget
            .backoff_until
            .clone()
            .unwrap_or_else(|| "-".to_owned()),
        budget.recent_failures.to_string(),
        budget.cache_hits.to_string(),
        budget.next_allowed.clone(),
        budget.capabilities.join(", "),
    ]
    .join("\t")
}

pub fn constituent_copy_text(row: &ConstituentReadinessRow) -> String {
    [
        row.fund_ticker.clone(),
        row.holding_name.clone(),
        row.holding_ticker.clone(),
        row.weight_pct.to_string(),
        row.identity_status.as_str().to_owned(),
        row.instrument_id.clone().unwrap_or_else(|| "-".to_owned()),
        row.listing_id.clone().unwrap_or_else(|| "-".to_owned()),
        row.latest_price
            .map(|value| value.to_string())
            .unwrap_or_else(|| "-".to_owned()),
        row.price_date.clone().unwrap_or_else(|| "-".to_owned()),
        row.price_source.clone(),
        row.price_status.as_str().to_owned(),
        constituent_readiness_status(row).as_str().to_owned(),
        row.next_action.clone(),
    ]
    .join("\t")
}

pub fn diagnostic_copy_text(issue: &DataDiagnosticIssue) -> String {
    [
        issue.id.clone(),
        issue.severity.as_str().to_owned(),
        issue.title.clone(),
        issue.status.as_str().to_owned(),
        issue.source.clone(),
        issue.recommended_action.clone(),
        issue.related_page.clone(),
        issue.detail.clone(),
    ]
    .join("\t")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan_item(priority: u8, status: DataOperationStatus) -> MarketDataPlanItem {
        MarketDataPlanItem {
            id: format!("plan-{priority}"),
            priority,
            item_type: "fetch_constituent_price".to_owned(),
            subject_label: format!("Subject {priority}"),
            subject: None,
            reason: "reason".to_owned(),
            source: "mock".to_owned(),
            estimated_requests: u32::from(priority),
            status,
            blocker: None,
            next_action: "Fetch".to_owned(),
        }
    }

    #[test]
    fn filters_and_sorts_plan_items() {
        let mut state = DataOperationsState {
            filter: "price".to_owned(),
            ..DataOperationsState::default()
        };
        state.plan_table.toggle_sort(COL_PRIORITY);
        state.plan_table.toggle_sort(COL_PRIORITY);
        let rows = vec![
            plan_item(1, DataOperationStatus::Missing),
            plan_item(3, DataOperationStatus::Needed),
        ];

        assert_eq!(visible_plan_indices(&rows, &state), vec![1, 0]);
    }

    #[test]
    fn source_budget_copy_includes_capabilities() {
        let budget = SourceBudget {
            source: "openfigi".to_owned(),
            enabled: true,
            status: DataOperationStatus::BudgetBlocked,
            requests_used: 25,
            requests_limit: 25,
            window: "per 6h".to_owned(),
            min_delay: "6s".to_owned(),
            backoff_until: Some("2026-06-20 08:10".to_owned()),
            recent_failures: 3,
            cache_hits: 8,
            next_allowed: "2026-06-20 08:10".to_owned(),
            capabilities: vec!["identity".to_owned(), "figi".to_owned()],
        };

        let text = source_budget_copy_text(&budget);
        assert!(text.contains("openfigi"));
        assert!(text.contains("identity, figi"));
    }

    #[test]
    fn data_operations_focus_columns_cover_priority_tables() {
        assert_eq!(PlanColumn::ALL.len(), 8);
        assert_eq!(PlanColumn::Subject.key(), COL_SUBJECT);
        assert_eq!(SourceBudgetColumn::CacheHits.index(), 7);
        assert_eq!(FetchLogColumn::Key.label(), "Key");
        assert_eq!(FetchLogColumn::Error.index(), 9);
        assert_eq!(DiagnosticColumn::Detail.key(), "detail");
        assert_eq!(
            SchedulerColumn::ALL.len(),
            DATA_OPERATIONS_SCHEDULER_COLUMNS.len()
        );
        assert_eq!(
            ConstituentColumn::ALL.len(),
            DATA_OPERATIONS_CONSTITUENT_COLUMNS.len()
        );
        assert_eq!(
            ApiSectionColumn::ALL.len(),
            DATA_OPERATIONS_API_SECTION_COLUMNS.len()
        );
        assert_eq!(
            TableId::DataOperationsScheduler.key(),
            "data_operations.scheduler"
        );
        assert_eq!(DATA_OPERATIONS_CONSTITUENT_COLUMNS[0].key, "fund");
        assert_eq!(DATA_OPERATIONS_API_SECTION_COLUMNS[3].key, "detail");
    }
}
