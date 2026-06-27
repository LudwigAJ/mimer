#[derive(Clone, Debug)]
pub struct Workspace {
    pub id: String,
    pub name: String,
    pub base_currency: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
#[allow(dead_code)]
pub enum AnalysisSubject {
    WorkspacePortfolio(String),
    Fund(String),
    FundListing { fund_id: String, listing_id: String },
    Holding { ticker: String, source: String },
    Cash(String),
    SyntheticModel(String),
}

impl AnalysisSubject {
    #[allow(dead_code)]
    pub fn short_id(&self) -> &str {
        match self {
            Self::WorkspacePortfolio(workspace_id) => workspace_id,
            Self::Fund(fund_id) => fund_id,
            Self::FundListing { listing_id, .. } => listing_id,
            Self::Holding { ticker, .. } => ticker,
            Self::Cash(currency) => currency,
            Self::SyntheticModel(model_id) => model_id,
        }
    }

    pub fn kind_label(&self) -> &'static str {
        match self {
            Self::WorkspacePortfolio(_) => "portfolio",
            Self::Fund(_) => "fund",
            Self::FundListing { .. } => "listing",
            Self::Holding { .. } => "holding",
            Self::Cash(_) => "cash",
            Self::SyntheticModel(_) => "model",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[allow(dead_code)]
pub enum InvestableKind {
    Portfolio,
    Fund,
    Listing,
    Holding,
    Cash,
    Synthetic,
}

impl InvestableKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Portfolio => "Portfolio",
            Self::Fund => "Fund",
            Self::Listing => "Listing",
            Self::Holding => "Holding",
            Self::Cash => "Cash",
            Self::Synthetic => "Synthetic",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct InvestableNode {
    pub id: String,
    pub subject: AnalysisSubject,
    pub label: String,
    pub kind: InvestableKind,
    pub ticker: Option<String>,
    pub isin: Option<String>,
    pub currency: Option<String>,
    pub value: Option<f64>,
    pub weight_pct: Option<f64>,
    pub status: String,
    pub source: String,
    pub children: Vec<InvestableNode>,
}

impl InvestableNode {
    pub fn new(
        id: impl Into<String>,
        subject: AnalysisSubject,
        label: impl Into<String>,
        kind: InvestableKind,
    ) -> Self {
        Self {
            id: id.into(),
            subject,
            label: label.into(),
            kind,
            ticker: None,
            isin: None,
            currency: None,
            value: None,
            weight_pct: None,
            status: "SEED".to_owned(),
            source: "seed".to_owned(),
            children: Vec::new(),
        }
    }

    pub fn with_ticker(mut self, ticker: impl Into<String>) -> Self {
        self.ticker = Some(ticker.into());
        self
    }

    pub fn with_isin(mut self, isin: impl Into<String>) -> Self {
        self.isin = Some(isin.into());
        self
    }

    pub fn with_currency(mut self, currency: impl Into<String>) -> Self {
        self.currency = Some(currency.into());
        self
    }

    pub fn with_value(mut self, value: f64) -> Self {
        self.value = Some(value);
        self
    }

    pub fn with_weight_pct(mut self, weight_pct: f64) -> Self {
        self.weight_pct = Some(weight_pct);
        self
    }

    pub fn with_status(mut self, status: impl Into<String>) -> Self {
        self.status = status.into();
        self
    }

    pub fn with_source(mut self, source: impl Into<String>) -> Self {
        self.source = source.into();
        self
    }

    pub fn push_child(&mut self, child: InvestableNode) -> Result<(), InvestableTreeError> {
        if child.contains_id(&self.id) {
            return Err(InvestableTreeError::Cycle {
                parent_id: self.id.clone(),
                child_id: child.id,
            });
        }
        if self.contains_id(&child.id) {
            return Err(InvestableTreeError::DuplicateId(child.id));
        }
        self.children.push(child);
        Ok(())
    }

    pub fn contains_id(&self, id: &str) -> bool {
        self.id == id || self.children.iter().any(|child| child.contains_id(id))
    }

    pub fn find(&self, id: &str) -> Option<&InvestableNode> {
        if self.id == id {
            return Some(self);
        }
        self.children.iter().find_map(|child| child.find(id))
    }

    pub fn path_labels(&self, id: &str) -> Option<Vec<String>> {
        if self.id == id {
            return Some(vec![self.label.clone()]);
        }

        for child in &self.children {
            if let Some(mut labels) = child.path_labels(id) {
                labels.insert(0, self.label.clone());
                return Some(labels);
            }
        }

        None
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum InvestableTreeError {
    DuplicateId(String),
    Cycle { parent_id: String, child_id: String },
}

#[derive(Clone, Debug, PartialEq)]
pub struct ComputedValue {
    pub label: String,
    pub value: f64,
    pub unit: String,
    pub status: String,
    pub source: String,
    pub dependencies: Vec<ValueDependency>,
    pub notes: Vec<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ValueDependency {
    pub entity_type: String,
    pub entity_id: String,
    pub field_name: String,
    pub value_used: String,
    pub source: String,
    pub status: String,
}

#[derive(Clone, Debug, PartialEq)]
#[allow(dead_code)]
pub struct ScenarioInput {
    pub name: String,
    pub target_entity: AnalysisSubject,
    pub field_name: String,
    pub base_value: f64,
    pub shocked_value: f64,
    pub shock_type: String,
}

#[derive(Clone, Debug, PartialEq)]
#[allow(dead_code)]
pub struct ScenarioResult {
    pub name: String,
    pub outputs: Vec<ComputedValue>,
    pub changed_values: Vec<ValueDependency>,
    pub diagnostics: Vec<String>,
}

#[derive(Clone, Debug, PartialEq)]
#[allow(dead_code)]
pub struct PnLExplain {
    pub period_start: String,
    pub period_end: String,
    pub total_change: f64,
    pub components: Vec<PnLComponent>,
}

#[derive(Clone, Debug, PartialEq)]
#[allow(dead_code)]
pub struct PnLComponent {
    pub name: String,
    pub value: f64,
    pub percentage_of_total: f64,
    pub source: String,
    pub status: String,
    pub children: Vec<PnLComponent>,
}

#[derive(Clone, Debug)]
pub struct Fund {
    pub id: String,
    pub name: String,
    pub provider: String,
    pub isin: String,
    pub strategy: String,
    pub domicile: String,
    pub base_currency: String,
    pub distribution_policy: String,
    pub ocf_ter_pct: f32,
    pub distribution_frequency: String,
    pub replication: String,
    pub status: String,
    pub last_refreshed: String,
    pub source: String,
    pub listings: Vec<FundListing>,
}

#[derive(Clone, Debug)]
pub struct FundListing {
    pub id: String,
    pub fund_id: String,
    pub ticker: String,
    pub exchange: String,
    pub currency: String,
    pub venue_name: String,
    pub currency_unit: String,
    pub figi: Option<String>,
    pub sedol: Option<String>,
    pub last_price: f64,
    pub last_price_date: String,
    pub status: String,
    pub source: String,
}

#[derive(Clone, Debug)]
pub struct Position {
    pub fund_id: String,
    pub listing_id: String,
    pub ticker: String,
    pub name: String,
    pub isin: String,
    pub listing_currency: String,
    pub units: f64,
    pub price: f64,
    pub daily_change: f64,
    pub market_value: f64,
    pub portfolio_weight_pct: f64,
    pub trailing_yield_pct: f64,
    pub projected_income: f64,
    pub freshness: DataFreshness,
    pub source: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DataFreshness {
    Fresh,
    Stale,
    BackfillPending,
}

impl DataFreshness {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Fresh => "FRESH",
            Self::Stale => "STALE",
            Self::BackfillPending => "PENDING",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Distribution {
    pub fund_id: String,
    pub ticker: String,
    pub ex_date: String,
    pub payment_date: String,
    pub amount: f64,
    pub currency: String,
    pub status: String,
    pub source: String,
}

#[derive(Clone, Debug)]
pub struct HoldingExposure {
    pub company: String,
    pub ticker: String,
    pub country: String,
    pub sector: String,
    pub weight_pct: f64,
    pub change_since_previous_pct: Option<f64>,
    pub source_etf: String,
    pub as_of_date: String,
    pub source: String,
}

#[derive(Clone, Debug)]
pub struct ExposureBreakdown {
    pub countries: Vec<ExposureSlice>,
    pub sectors: Vec<ExposureSlice>,
    pub currencies: Vec<ExposureSlice>,
    pub top_holdings: Vec<ExposureSlice>,
}

#[derive(Clone, Debug)]
pub struct ExposureSlice {
    pub label: String,
    pub value_pct: f64,
}

#[derive(Clone, Debug)]
pub struct Alert {
    pub id: String,
    pub severity: AlertSeverity,
    pub category: String,
    pub title: String,
    pub message: String,
    pub fund_ticker: Option<String>,
    pub status: String,
    pub source: String,
    pub read: bool,
    pub dismissed: bool,
    pub created_time: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AlertSeverity {
    Info,
    Warning,
    Critical,
}

impl AlertSeverity {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Info => "Info",
            Self::Warning => "Warning",
            Self::Critical => "Critical",
        }
    }
}

#[derive(Clone, Debug)]
pub struct DocumentSnapshot {
    pub fund_id: String,
    pub ticker: String,
    pub document_type: String,
    pub latest_date: String,
    pub status: String,
    pub content_hash_change: String,
    pub source: String,
    pub last_checked: String,
}

#[derive(Clone, Debug)]
pub struct ScheduledJob {
    pub name: String,
    pub job_type: String,
    pub source: String,
    pub cron_schedule: String,
    pub active: bool,
    pub last_run: String,
    pub next_run: String,
}

#[derive(Clone, Debug)]
pub struct JobRun {
    pub id: String,
    pub job_type: String,
    pub source: String,
    pub status: JobStatus,
    pub started: String,
    pub finished: Option<String>,
    pub inserted: u32,
    pub updated: u32,
    pub failed: u32,
    pub message: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JobStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
    Unknown,
}

impl JobStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "QUEUED",
            Self::Running => "RUNNING",
            Self::Succeeded => "DONE",
            Self::Failed => "FAILED",
            Self::Unknown => "UNKNOWN",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DataOperationStatus {
    Ok,
    Ready,
    Partial,
    Missing,
    Stale,
    Failed,
    Blocked,
    Mock,
    Running,
    Needed,
    Fresh,
    Ambiguous,
    BudgetBlocked,
    Planned,
    Unknown,
}

impl DataOperationStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "OK",
            Self::Ready => "READY",
            Self::Partial => "PARTIAL",
            Self::Missing => "MISSING",
            Self::Stale => "STALE",
            Self::Failed => "FAILED",
            Self::Blocked => "BLOCKED",
            Self::Mock => "MOCK",
            Self::Running => "RUNNING",
            Self::Needed => "NEEDED",
            Self::Fresh => "FRESH",
            Self::Ambiguous => "AMBIG",
            Self::BudgetBlocked => "BUDGET_BLOCKED",
            Self::Planned => "PLANNED",
            Self::Unknown => "UNKNOWN",
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct DataOperationsSnapshot {
    pub readiness_stages: Vec<ReadinessStage>,
    pub market_data_plan: Vec<MarketDataPlanItem>,
    pub source_budgets: Vec<SourceBudget>,
    pub fetch_logs: Vec<SourceFetchLog>,
    pub constituent_coverage: Vec<ConstituentReadinessRow>,
    pub diagnostic_issues: Vec<DataDiagnosticIssue>,
    pub backend_sections: Vec<BackendSectionStatus>,
    pub hydration: DataOperationsHydration,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum DataOperationsOrigin {
    #[default]
    Mock,
    Api,
    PartialApi,
    StaleApi,
    ApiError,
}

impl DataOperationsOrigin {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Mock => "MOCK",
            Self::Api => "API",
            Self::PartialApi => "PARTIAL API",
            Self::StaleApi => "STALE API",
            Self::ApiError => "API ERROR",
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DataOperationsHydration {
    pub origin: DataOperationsOrigin,
    pub refreshed_at: Option<String>,
    pub base_url: Option<String>,
    pub failed_sections: Vec<DataOperationsSectionFailure>,
    pub last_error: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DataOperationsSectionFailure {
    pub section: String,
    pub message: String,
}

#[derive(Clone, Debug)]
pub struct BackendSectionStatus {
    pub key: String,
    pub label: String,
    pub status: DataOperationStatus,
    pub record_count: Option<usize>,
    pub detail: String,
    pub source: String,
}

#[derive(Clone, Debug)]
pub struct ReadinessStage {
    pub key: String,
    pub label: String,
    pub status: DataOperationStatus,
    pub coverage_pct: Option<f32>,
    pub count_label: String,
    pub detail: String,
    pub source: String,
    pub freshness: String,
    pub recommended_action: String,
}

#[derive(Clone, Debug)]
pub struct RecommendedDataAction {
    pub priority: u8,
    pub label: String,
    pub target: String,
    pub reason: String,
    pub status: DataOperationStatus,
    pub command: String,
    pub action_label: String,
    pub source: String,
}

#[derive(Clone, Debug)]
pub struct MarketDataPlanItem {
    pub id: String,
    pub priority: u8,
    pub item_type: String,
    pub subject_label: String,
    pub subject: Option<AnalysisSubject>,
    pub reason: String,
    pub source: String,
    pub estimated_requests: u32,
    pub status: DataOperationStatus,
    pub blocker: Option<String>,
    pub next_action: String,
}

#[derive(Clone, Debug)]
pub struct SourceBudget {
    pub source: String,
    pub enabled: bool,
    pub status: DataOperationStatus,
    pub requests_used: u32,
    pub requests_limit: u32,
    pub window: String,
    pub min_delay: String,
    pub backoff_until: Option<String>,
    pub recent_failures: u32,
    pub cache_hits: u32,
    pub next_allowed: String,
    pub capabilities: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct SourceFetchLog {
    pub id: String,
    pub time: String,
    pub source: String,
    pub request_kind: String,
    pub request_key: String,
    pub status: DataOperationStatus,
    pub http_status: Option<u16>,
    pub duration_ms: u32,
    pub cache_hit: bool,
    pub rate_limited: bool,
    pub error: Option<String>,
}

#[derive(Clone, Debug)]
pub struct ConstituentReadinessRow {
    pub fund_ticker: String,
    pub holding_name: String,
    pub holding_ticker: String,
    pub weight_pct: f64,
    pub subject: AnalysisSubject,
    pub identity_status: DataOperationStatus,
    pub instrument_id: Option<String>,
    pub listing_id: Option<String>,
    pub latest_price: Option<f64>,
    pub price_date: Option<String>,
    pub price_source: String,
    pub price_status: DataOperationStatus,
    pub next_action: String,
}

#[derive(Clone, Debug)]
pub struct DataDiagnosticIssue {
    pub id: String,
    pub severity: AlertSeverity,
    pub title: String,
    pub detail: String,
    pub status: DataOperationStatus,
    pub source: String,
    pub recommended_action: String,
    pub related_page: String,
}

#[derive(Clone, Debug)]
pub struct PortfolioSummary {
    pub total_value: f64,
    pub daily_change: f64,
    pub unrealised_gain_loss: f64,
    pub trailing_12m_income: f64,
    pub projected_annual_income: f64,
    pub base_currency: String,
    pub position_count: usize,
    pub stale_warning_count: usize,
}

#[derive(Clone, Debug)]
pub struct InstrumentResolutionRequest {
    pub symbol: String,
    pub symbol_type: SymbolType,
    pub exchange: Option<String>,
    pub currency: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SymbolType {
    Ticker,
    Isin,
    Figi,
    Sedol,
    Cusip,
}

impl SymbolType {
    pub const ALL: [Self; 5] = [
        Self::Ticker,
        Self::Isin,
        Self::Figi,
        Self::Sedol,
        Self::Cusip,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ticker => "ticker",
            Self::Isin => "isin",
            Self::Figi => "figi",
            Self::Sedol => "sedol",
            Self::Cusip => "cusip",
        }
    }
}

#[derive(Clone, Debug)]
pub enum InstrumentResolutionResult {
    Resolved {
        fund: Fund,
        listing: FundListing,
        queued_jobs: Vec<JobRun>,
    },
    Ambiguous {
        candidates: Vec<InstrumentCandidate>,
    },
    NotFound {
        message: String,
    },
    PendingBackfill {
        fund: Option<Fund>,
        listing: Option<FundListing>,
        jobs: Vec<JobRun>,
    },
}

impl InstrumentResolutionResult {
    pub fn status_label(&self) -> &'static str {
        match self {
            Self::Resolved { .. } => "DONE",
            Self::Ambiguous { .. } => "AMBIG",
            Self::NotFound { .. } => "MISSING",
            Self::PendingBackfill { .. } => "PENDING",
        }
    }
}

#[derive(Clone, Debug)]
pub struct InstrumentCandidate {
    pub fund: Fund,
    pub listing: FundListing,
    pub match_reason: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn investable_tree_rejects_duplicate_ids() {
        let mut root = InvestableNode::new(
            "root",
            AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
            "Main",
            InvestableKind::Portfolio,
        );

        root.push_child(InvestableNode::new(
            "child",
            AnalysisSubject::SyntheticModel("child".to_owned()),
            "Child",
            InvestableKind::Synthetic,
        ))
        .expect("first child should insert");

        let err = root
            .push_child(InvestableNode::new(
                "child",
                AnalysisSubject::SyntheticModel("child-2".to_owned()),
                "Duplicate",
                InvestableKind::Synthetic,
            ))
            .expect_err("duplicate id should be rejected");

        assert_eq!(err, InvestableTreeError::DuplicateId("child".to_owned()));
    }

    #[test]
    fn investable_tree_rejects_child_that_contains_parent_id() {
        let mut root = InvestableNode::new(
            "root",
            AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
            "Main",
            InvestableKind::Portfolio,
        );
        let mut child = InvestableNode::new(
            "child",
            AnalysisSubject::SyntheticModel("child".to_owned()),
            "Child",
            InvestableKind::Synthetic,
        );
        child
            .push_child(InvestableNode::new(
                "root",
                AnalysisSubject::SyntheticModel("nested-root".to_owned()),
                "Nested root",
                InvestableKind::Synthetic,
            ))
            .expect("nested child can be built before attaching");

        let err = root
            .push_child(child)
            .expect_err("attaching child with parent id should be rejected");

        assert_eq!(
            err,
            InvestableTreeError::Cycle {
                parent_id: "root".to_owned(),
                child_id: "child".to_owned()
            }
        );
    }

    #[test]
    fn investable_tree_returns_breadcrumb_labels() {
        let mut root = InvestableNode::new(
            "root",
            AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
            "Main Portfolio",
            InvestableKind::Portfolio,
        );
        let mut fund = InvestableNode::new(
            "vusa",
            AnalysisSubject::Fund("fund-vusa".to_owned()),
            "VUSA",
            InvestableKind::Fund,
        );
        fund.push_child(InvestableNode::new(
            "msft",
            AnalysisSubject::Holding {
                ticker: "MSFT".to_owned(),
                source: "VUSA".to_owned(),
            },
            "Microsoft",
            InvestableKind::Holding,
        ))
        .expect("holding should insert");
        root.push_child(fund).expect("fund should insert");

        assert_eq!(
            root.path_labels("msft"),
            Some(vec![
                "Main Portfolio".to_owned(),
                "VUSA".to_owned(),
                "Microsoft".to_owned()
            ])
        );
    }
}
