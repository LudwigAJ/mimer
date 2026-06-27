use crate::api::client::{DataOperationsLoad, DataProvider};
use crate::domain::{
    Alert, AlertSeverity, AnalysisSubject, ConstituentReadinessRow, DataDiagnosticIssue,
    DataFreshness, DataOperationStatus, DataOperationsSnapshot, Distribution, DocumentSnapshot,
    ExposureBreakdown, ExposureSlice, Fund, FundListing, HoldingExposure, InstrumentCandidate,
    InstrumentResolutionRequest, InstrumentResolutionResult, JobRun, JobStatus, MarketDataPlanItem,
    PortfolioSummary, Position, ReadinessStage, ScheduledJob, SourceBudget, SourceFetchLog,
    SymbolType, Workspace,
};
use crate::timeseries::{TimeSeries, TimeSeriesKind, TimeSeriesPoint};

#[derive(Clone, Debug)]
pub struct MockDataProvider {
    dataset: MockDataset,
}

impl Default for MockDataProvider {
    fn default() -> Self {
        Self {
            dataset: MockDataset::new(),
        }
    }
}

impl DataProvider for MockDataProvider {
    fn load_workspaces(&self) -> Vec<Workspace> {
        self.dataset.workspaces.clone()
    }

    fn load_portfolio_summary(&self, workspace_id: &str) -> PortfolioSummary {
        let mut summary = self.dataset.summary.clone();
        if let Some(workspace) = self.dataset.workspace(workspace_id) {
            summary.base_currency.clone_from(&workspace.base_currency);
        }
        summary.position_count = self.dataset.positions.len();
        summary
    }

    fn load_positions(&self, _workspace_id: &str) -> Vec<Position> {
        self.dataset.positions.clone()
    }

    fn load_funds(&self) -> Vec<Fund> {
        self.dataset.funds.clone()
    }

    fn load_distributions(&self) -> Vec<Distribution> {
        self.dataset.distributions.clone()
    }

    fn load_holdings(&self) -> Vec<HoldingExposure> {
        self.dataset.holdings.clone()
    }

    fn load_exposure(&self, _workspace_id: &str) -> ExposureBreakdown {
        self.dataset.exposure.clone()
    }

    fn load_alerts(&self, _workspace_id: &str) -> Vec<Alert> {
        self.dataset.alerts.clone()
    }

    fn load_documents(&self) -> Vec<DocumentSnapshot> {
        self.dataset.documents.clone()
    }

    fn load_jobs(&self) -> (Vec<ScheduledJob>, Vec<JobRun>) {
        (
            self.dataset.scheduled_jobs.clone(),
            self.dataset.job_runs.clone(),
        )
    }

    fn load_data_operations(&self, _workspace_id: &str) -> DataOperationsLoad {
        DataOperationsLoad::mock(self.dataset.data_operations.clone())
    }

    fn load_time_series(&self, _workspace_id: &str) -> Vec<TimeSeries> {
        self.dataset.time_series.clone()
    }

    fn resolve_instrument(
        &self,
        request: &InstrumentResolutionRequest,
    ) -> InstrumentResolutionResult {
        self.dataset.resolve_instrument(request)
    }
}

#[derive(Clone, Debug)]
struct MockDataset {
    workspaces: Vec<Workspace>,
    summary: PortfolioSummary,
    positions: Vec<Position>,
    funds: Vec<Fund>,
    distributions: Vec<Distribution>,
    holdings: Vec<HoldingExposure>,
    exposure: ExposureBreakdown,
    alerts: Vec<Alert>,
    documents: Vec<DocumentSnapshot>,
    scheduled_jobs: Vec<ScheduledJob>,
    job_runs: Vec<JobRun>,
    data_operations: DataOperationsSnapshot,
    time_series: Vec<TimeSeries>,
}

impl MockDataset {
    fn new() -> Self {
        let vusa_listings = vec![
            listing(
                "listing-vusa-lse-gbp",
                "fund-vusa",
                "VUSA",
                "XLON",
                "GBP",
                "London Stock Exchange",
            ),
            listing(
                "listing-vusd-lse-usd",
                "fund-vusa",
                "VUSD",
                "XLON",
                "USD",
                "London Stock Exchange",
            ),
        ];
        let isf_listings = vec![listing(
            "listing-isf-lse-gbp",
            "fund-isf",
            "ISF",
            "XLON",
            "GBP",
            "London Stock Exchange",
        )];
        let jepg_listings = vec![
            listing(
                "listing-jegp-lse-gbp",
                "fund-jepg",
                "JEGP",
                "XLON",
                "GBP",
                "London Stock Exchange",
            ),
            listing(
                "listing-jepg-lse-usd",
                "fund-jepg",
                "JEPG",
                "XLON",
                "USD",
                "London Stock Exchange",
            ),
        ];
        let vhyl_listings = vec![listing(
            "listing-vhyl-lse-gbp",
            "fund-vhyl",
            "VHYL",
            "XLON",
            "GBP",
            "London Stock Exchange",
        )];

        let funds = vec![
            Fund {
                id: s("fund-vusa"),
                name: s("Vanguard S&P 500 UCITS ETF"),
                provider: s("Vanguard"),
                isin: s("IE00B3XXRP09"),
                strategy: s("US large-cap equity"),
                domicile: s("Ireland"),
                base_currency: s("USD"),
                distribution_policy: s("Distributing"),
                ocf_ter_pct: 0.07,
                distribution_frequency: s("Quarterly"),
                replication: s("Physical"),
                status: s("Active"),
                last_refreshed: s("2026-06-20 06:05"),
                source: s("seed"),
                listings: vusa_listings,
            },
            Fund {
                id: s("fund-isf"),
                name: s("iShares Core FTSE 100 UCITS ETF"),
                provider: s("iShares"),
                isin: s("IE0005042456"),
                strategy: s("UK large-cap equity"),
                domicile: s("Ireland"),
                base_currency: s("GBP"),
                distribution_policy: s("Distributing"),
                ocf_ter_pct: 0.07,
                distribution_frequency: s("Quarterly"),
                replication: s("Physical"),
                status: s("Active"),
                last_refreshed: s("2026-06-20 06:03"),
                source: s("seed"),
                listings: isf_listings,
            },
            Fund {
                id: s("fund-jepg"),
                name: s("JPMorgan Global Equity Premium Income Active UCITS ETF"),
                provider: s("J.P. Morgan"),
                isin: s("IE000JEPG001"),
                strategy: s("Global equity covered-call income"),
                domicile: s("Ireland"),
                base_currency: s("USD"),
                distribution_policy: s("Monthly distributing"),
                ocf_ter_pct: 0.35,
                distribution_frequency: s("Monthly"),
                replication: s("Active equity income"),
                status: s("Backfill pending"),
                last_refreshed: s("2026-06-19 22:40"),
                source: s("issuer"),
                listings: jepg_listings,
            },
            Fund {
                id: s("fund-vhyl"),
                name: s("Vanguard FTSE All-World High Dividend Yield UCITS ETF"),
                provider: s("Vanguard"),
                isin: s("IE00B8GKDB10"),
                strategy: s("Global dividend equity"),
                domicile: s("Ireland"),
                base_currency: s("USD"),
                distribution_policy: s("Distributing"),
                ocf_ter_pct: 0.29,
                distribution_frequency: s("Quarterly"),
                replication: s("Physical"),
                status: s("Active"),
                last_refreshed: s("2026-06-20 05:58"),
                source: s("seed"),
                listings: vhyl_listings,
            },
        ];

        Self {
            workspaces: vec![
                Workspace {
                    id: s("workspace-main"),
                    name: s("Main Portfolio"),
                    base_currency: s("GBP"),
                },
                Workspace {
                    id: s("workspace-income"),
                    name: s("Income Watch"),
                    base_currency: s("GBP"),
                },
            ],
            summary: PortfolioSummary {
                total_value: 184_260.42,
                daily_change: 814.77,
                unrealised_gain_loss: 22_931.18,
                trailing_12m_income: 5_842.20,
                projected_annual_income: 6_214.85,
                base_currency: s("GBP"),
                position_count: 4,
                stale_warning_count: 2,
            },
            positions: vec![
                Position {
                    fund_id: s("fund-vusa"),
                    listing_id: s("listing-vusa-lse-gbp"),
                    ticker: s("VUSA"),
                    name: s("Vanguard S&P 500 UCITS ETF"),
                    isin: s("IE00B3XXRP09"),
                    listing_currency: s("GBP"),
                    units: 860.0,
                    price: 92.18,
                    daily_change: 0.42,
                    market_value: 79_274.80,
                    portfolio_weight_pct: 43.03,
                    trailing_yield_pct: 1.18,
                    projected_income: 934.14,
                    freshness: DataFreshness::Fresh,
                    source: s("stooq"),
                },
                Position {
                    fund_id: s("fund-isf"),
                    listing_id: s("listing-isf-lse-gbp"),
                    ticker: s("ISF"),
                    name: s("iShares Core FTSE 100 UCITS ETF"),
                    isin: s("IE0005042456"),
                    listing_currency: s("GBP"),
                    units: 3_200.0,
                    price: 8.26,
                    daily_change: -0.03,
                    market_value: 26_432.00,
                    portfolio_weight_pct: 14.34,
                    trailing_yield_pct: 3.72,
                    projected_income: 983.27,
                    freshness: DataFreshness::Fresh,
                    source: s("stooq"),
                },
                Position {
                    fund_id: s("fund-jepg"),
                    listing_id: s("listing-jegp-lse-gbp"),
                    ticker: s("JEGP"),
                    name: s("JPMorgan Global Equity Premium Income Active UCITS ETF"),
                    isin: s("IE000JEPG001"),
                    listing_currency: s("GBP"),
                    units: 1_470.0,
                    price: 24.64,
                    daily_change: 0.11,
                    market_value: 36_220.80,
                    portfolio_weight_pct: 19.66,
                    trailing_yield_pct: 7.84,
                    projected_income: 2_839.71,
                    freshness: DataFreshness::BackfillPending,
                    source: s("issuer"),
                },
                Position {
                    fund_id: s("fund-vhyl"),
                    listing_id: s("listing-vhyl-lse-gbp"),
                    ticker: s("VHYL"),
                    name: s("Vanguard FTSE All-World High Dividend Yield UCITS ETF"),
                    isin: s("IE00B8GKDB10"),
                    listing_currency: s("GBP"),
                    units: 1_210.0,
                    price: 34.99,
                    daily_change: -0.18,
                    market_value: 42_337.90,
                    portfolio_weight_pct: 22.97,
                    trailing_yield_pct: 3.12,
                    projected_income: 1_457.73,
                    freshness: DataFreshness::Stale,
                    source: s("stooq"),
                },
            ],
            funds,
            distributions: vec![
                distribution(
                    "fund-vusa",
                    "VUSA",
                    "2026-06-13",
                    "2026-06-26",
                    0.317,
                    "GBP",
                    "Declared",
                    "provider",
                ),
                distribution(
                    "fund-isf",
                    "ISF",
                    "2026-06-12",
                    "2026-06-28",
                    0.081,
                    "GBP",
                    "Declared",
                    "exchange",
                ),
                distribution(
                    "fund-jepg",
                    "JEGP",
                    "2026-06-05",
                    "2026-06-24",
                    0.146,
                    "GBP",
                    "Estimated",
                    "scrape",
                ),
                distribution(
                    "fund-vhyl",
                    "VHYL",
                    "2026-03-20",
                    "2026-04-02",
                    0.224,
                    "GBP",
                    "Paid",
                    "provider",
                ),
                distribution(
                    "fund-vusa",
                    "VUSA",
                    "2026-03-14",
                    "2026-03-27",
                    0.291,
                    "GBP",
                    "Paid",
                    "provider",
                ),
            ],
            holdings: vec![
                holding(
                    "Microsoft",
                    "MSFT",
                    "United States",
                    "Technology",
                    5.9,
                    "VUSA",
                    "2026-05-31",
                    "factsheet",
                ),
                holding(
                    "NVIDIA",
                    "NVDA",
                    "United States",
                    "Technology",
                    5.5,
                    "VUSA",
                    "2026-05-31",
                    "factsheet",
                ),
                holding(
                    "Apple",
                    "AAPL",
                    "United States",
                    "Technology",
                    5.1,
                    "VUSA",
                    "2026-05-31",
                    "factsheet",
                ),
                holding(
                    "AstraZeneca",
                    "AZN",
                    "United Kingdom",
                    "Health Care",
                    7.2,
                    "ISF",
                    "2026-05-31",
                    "factsheet",
                ),
                holding(
                    "HSBC",
                    "HSBA",
                    "United Kingdom",
                    "Financials",
                    6.1,
                    "ISF",
                    "2026-05-31",
                    "factsheet",
                ),
                holding(
                    "Nestle",
                    "NESN",
                    "Switzerland",
                    "Consumer Staples",
                    2.3,
                    "VHYL",
                    "2026-05-31",
                    "factsheet",
                ),
            ],
            exposure: ExposureBreakdown {
                countries: vec![
                    slice("United States", 56.4),
                    slice("United Kingdom", 18.1),
                    slice("Japan", 4.6),
                    slice("Switzerland", 3.8),
                    slice("Other", 17.1),
                ],
                sectors: vec![
                    slice("Technology", 26.8),
                    slice("Financials", 15.4),
                    slice("Health Care", 12.2),
                    slice("Industrials", 9.7),
                    slice("Consumer Staples", 8.6),
                ],
                currencies: vec![
                    slice("USD", 63.2),
                    slice("GBP", 21.4),
                    slice("EUR", 6.5),
                    slice("CHF", 3.2),
                    slice("Other", 5.7),
                ],
                top_holdings: vec![
                    slice("Microsoft", 3.2),
                    slice("NVIDIA", 3.0),
                    slice("Apple", 2.8),
                    slice("AstraZeneca", 1.7),
                    slice("HSBC", 1.4),
                ],
            },
            alerts: vec![
                alert(
                    "alert-1",
                    AlertSeverity::Info,
                    "Distribution",
                    "New distribution declared",
                    "VUSA declared a quarterly distribution; payment is due 2026-06-26.",
                    Some("VUSA"),
                    false,
                    false,
                    "2026-06-20 06:20",
                ),
                alert(
                    "alert-2",
                    AlertSeverity::Warning,
                    "Document",
                    "Factsheet changed",
                    "JEGP factsheet hash changed; review holdings and distribution policy text.",
                    Some("JEGP"),
                    false,
                    false,
                    "2026-06-20 05:44",
                ),
                alert(
                    "alert-3",
                    AlertSeverity::Warning,
                    "Holdings",
                    "Large holding weight change",
                    "NVDA look-through weight moved by more than 75 bps since last snapshot.",
                    Some("VUSA"),
                    true,
                    false,
                    "2026-06-19 19:12",
                ),
                alert(
                    "alert-4",
                    AlertSeverity::Critical,
                    "Prices",
                    "Stale price data",
                    "VHYL close price is older than the configured freshness threshold.",
                    Some("VHYL"),
                    false,
                    false,
                    "2026-06-19 18:00",
                ),
                alert(
                    "alert-5",
                    AlertSeverity::Warning,
                    "Resolution",
                    "Ambiguous instrument resolution",
                    "JEPG matched multiple listings for the same underlying fund.",
                    Some("JEPG"),
                    true,
                    false,
                    "2026-06-18 09:30",
                ),
                alert(
                    "alert-6",
                    AlertSeverity::Critical,
                    "Jobs",
                    "Failed ingestion job",
                    "Weekly holdings ingestion failed for one provider document.",
                    None,
                    false,
                    false,
                    "2026-06-17 23:51",
                ),
            ],
            documents: vec![
                document(
                    "fund-vusa",
                    "VUSA",
                    "factsheet",
                    "2026-05-31",
                    "Current",
                    "unchanged",
                    "provider",
                    "2026-06-20 06:10",
                ),
                document(
                    "fund-vusa",
                    "VUSA",
                    "KID",
                    "2026-02-28",
                    "Current",
                    "unchanged",
                    "provider",
                    "2026-06-20 06:10",
                ),
                document(
                    "fund-isf",
                    "ISF",
                    "prospectus",
                    "2026-04-30",
                    "Current",
                    "unchanged",
                    "provider",
                    "2026-06-20 06:04",
                ),
                document(
                    "fund-jepg",
                    "JEGP",
                    "factsheet",
                    "2026-05-31",
                    "Changed",
                    "hash changed",
                    "provider",
                    "2026-06-20 05:44",
                ),
                document(
                    "fund-vhyl",
                    "VHYL",
                    "annual report",
                    "2025-12-31",
                    "Current",
                    "unchanged",
                    "provider",
                    "2026-06-19 22:00",
                ),
                document(
                    "fund-vhyl",
                    "VHYL",
                    "interim report",
                    "2025-06-30",
                    "Missing",
                    "not checked",
                    "provider",
                    "2026-06-19 22:00",
                ),
            ],
            scheduled_jobs: vec![
                scheduled_job(
                    "Daily price ingestion",
                    "prices",
                    "exchange close files",
                    "0 6 * * 1-5",
                    true,
                    "2026-06-20 06:01",
                    "2026-06-22 06:00",
                ),
                scheduled_job(
                    "Daily FX ingestion",
                    "fx",
                    "ECB",
                    "15 6 * * 1-5",
                    true,
                    "2026-06-20 06:15",
                    "2026-06-22 06:15",
                ),
                scheduled_job(
                    "Weekly holdings ingestion",
                    "holdings",
                    "provider factsheets",
                    "0 23 * * 5",
                    true,
                    "2026-06-19 23:00",
                    "2026-06-26 23:00",
                ),
                scheduled_job(
                    "Monthly document snapshot check",
                    "documents",
                    "provider documents",
                    "30 5 1 * *",
                    true,
                    "2026-06-01 05:30",
                    "2026-07-01 05:30",
                ),
            ],
            job_runs: vec![
                job_run(
                    "run-20260620-prices",
                    "prices",
                    "exchange close files",
                    JobStatus::Succeeded,
                    "2026-06-20 06:01",
                    Some("2026-06-20 06:04"),
                    18,
                    4,
                    0,
                    "price refresh complete",
                ),
                job_run(
                    "run-20260620-fx",
                    "fx",
                    "ECB",
                    JobStatus::Succeeded,
                    "2026-06-20 06:15",
                    Some("2026-06-20 06:15"),
                    6,
                    2,
                    0,
                    "fx rates updated",
                ),
                job_run(
                    "run-20260619-holdings",
                    "holdings",
                    "provider factsheets",
                    JobStatus::Failed,
                    "2026-06-19 23:00",
                    Some("2026-06-19 23:08"),
                    92,
                    11,
                    1,
                    "JEGP factsheet parse failed",
                ),
                job_run(
                    "run-20260620-backfill",
                    "documents",
                    "provider documents",
                    JobStatus::Running,
                    "2026-06-20 07:20",
                    None,
                    0,
                    0,
                    0,
                    "checking changed document hashes",
                ),
            ],
            data_operations: mock_data_operations(),
            time_series: mock_time_series(),
        }
    }

    fn workspace(&self, workspace_id: &str) -> Option<&Workspace> {
        self.workspaces
            .iter()
            .find(|workspace| workspace.id == workspace_id)
    }

    fn fund(&self, fund_id: &str) -> Option<&Fund> {
        self.funds.iter().find(|fund| fund.id == fund_id)
    }

    fn resolve_instrument(
        &self,
        request: &InstrumentResolutionRequest,
    ) -> InstrumentResolutionResult {
        let symbol = request.symbol.trim().to_ascii_uppercase();
        let exchange = request
            .exchange
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_ascii_uppercase);
        let currency = request
            .currency
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_ascii_uppercase);

        match request.symbol_type {
            SymbolType::Ticker => match symbol.as_str() {
                "VUSA" | "VUSD" => self.resolved("fund-vusa", &symbol, exchange, currency),
                "ISF" => self.resolved_with_jobs("fund-isf", &symbol, exchange, currency),
                "JEPG" | "JEGP" => self.ambiguous_jepg(),
                "BACKFILL" | "NEWETF" => InstrumentResolutionResult::PendingBackfill {
                    fund: Some(pending_backfill_fund()),
                    listing: Some(listing(
                        "listing-new-lse-gbp",
                        "fund-new-income",
                        "NEW",
                        "XLON",
                        "GBP",
                        "London Stock Exchange",
                    )),
                    jobs: vec![
                        job_run(
                            "run-mock-backfill-prices",
                            "prices",
                            "manual backfill",
                            JobStatus::Queued,
                            "2026-06-20 12:01",
                            None,
                            0,
                            0,
                            0,
                            "queued price history backfill",
                        ),
                        job_run(
                            "run-mock-backfill-docs",
                            "documents",
                            "manual backfill",
                            JobStatus::Queued,
                            "2026-06-20 12:01",
                            None,
                            0,
                            0,
                            0,
                            "queued document snapshot",
                        ),
                    ],
                },
                _ => InstrumentResolutionResult::NotFound {
                    message: format!(
                        "No mock ticker match for '{symbol}'. Try VUSA, ISF, JEPG, or BACKFILL."
                    ),
                },
            },
            SymbolType::Isin => match symbol.as_str() {
                "IE00B3XXRP09" => self.resolved("fund-vusa", "VUSA", exchange, currency),
                "IE0005042456" => self.resolved("fund-isf", "ISF", exchange, currency),
                "IE000JEPG001" => self.ambiguous_jepg(),
                _ => InstrumentResolutionResult::NotFound {
                    message: format!("No mock ISIN match for '{symbol}'."),
                },
            },
            SymbolType::Figi | SymbolType::Sedol | SymbolType::Cusip => {
                InstrumentResolutionResult::NotFound {
                    message: format!(
                        "Mock resolver has no {} match for '{}'.",
                        request.symbol_type.as_str(),
                        symbol
                    ),
                }
            }
        }
    }

    fn resolved(
        &self,
        fund_id: &str,
        ticker: &str,
        exchange: Option<String>,
        currency: Option<String>,
    ) -> InstrumentResolutionResult {
        let Some(fund) = self.fund(fund_id).cloned() else {
            return InstrumentResolutionResult::NotFound {
                message: s("Mock fund id is not present."),
            };
        };
        let Some(listing) =
            matching_listing(&fund, ticker, exchange.as_deref(), currency.as_deref())
        else {
            return InstrumentResolutionResult::NotFound {
                message: format!(
                    "No listing matched ticker '{ticker}' with the optional exchange/currency filters."
                ),
            };
        };

        InstrumentResolutionResult::Resolved {
            fund,
            listing,
            queued_jobs: Vec::new(),
        }
    }

    fn resolved_with_jobs(
        &self,
        fund_id: &str,
        ticker: &str,
        exchange: Option<String>,
        currency: Option<String>,
    ) -> InstrumentResolutionResult {
        match self.resolved(fund_id, ticker, exchange, currency) {
            InstrumentResolutionResult::Resolved { fund, listing, .. } => {
                InstrumentResolutionResult::Resolved {
                    fund,
                    listing,
                    queued_jobs: vec![job_run(
                        "run-mock-isf-refresh",
                        "holdings",
                        "manual resolve",
                        JobStatus::Queued,
                        "2026-06-20 12:02",
                        None,
                        0,
                        0,
                        0,
                        "queued holdings freshness check",
                    )],
                }
            }
            other => other,
        }
    }

    fn ambiguous_jepg(&self) -> InstrumentResolutionResult {
        let Some(fund) = self.fund("fund-jepg").cloned() else {
            return InstrumentResolutionResult::NotFound {
                message: s("Mock JEPG fund is not present."),
            };
        };

        InstrumentResolutionResult::Ambiguous {
            candidates: fund
                .listings
                .iter()
                .cloned()
                .map(|listing| InstrumentCandidate {
                    fund: fund.clone(),
                    match_reason: format!(
                        "Ticker maps to underlying fund {} listing in {} on {}.",
                        listing.ticker, listing.currency, listing.exchange
                    ),
                    listing,
                })
                .collect(),
        }
    }
}

fn matching_listing(
    fund: &Fund,
    ticker: &str,
    exchange: Option<&str>,
    currency: Option<&str>,
) -> Option<FundListing> {
    fund.listings
        .iter()
        .find(|listing| {
            listing.ticker == ticker
                && exchange.is_none_or(|wanted| listing.exchange == wanted)
                && currency.is_none_or(|wanted| listing.currency == wanted)
        })
        .or_else(|| {
            fund.listings
                .iter()
                .find(|listing| listing.ticker == ticker)
        })
        .cloned()
}

fn pending_backfill_fund() -> Fund {
    Fund {
        id: s("fund-new-income"),
        name: s("Mock Income ETF Pending Backfill"),
        provider: s("Mock Provider"),
        isin: s("IE000MOCK001"),
        strategy: s("Income ETF awaiting reference data"),
        domicile: s("Ireland"),
        base_currency: s("GBP"),
        distribution_policy: s("Unknown"),
        ocf_ter_pct: 0.24,
        distribution_frequency: s("Unknown"),
        replication: s("Unknown"),
        status: s("Backfill pending"),
        last_refreshed: s("not yet refreshed"),
        source: s("mock"),
        listings: vec![listing(
            "listing-new-lse-gbp",
            "fund-new-income",
            "NEW",
            "XLON",
            "GBP",
            "London Stock Exchange",
        )],
    }
}

fn s(value: &str) -> String {
    value.to_owned()
}

fn listing(
    id: &str,
    fund_id: &str,
    ticker: &str,
    exchange: &str,
    currency: &str,
    venue_name: &str,
) -> FundListing {
    FundListing {
        id: s(id),
        fund_id: s(fund_id),
        ticker: s(ticker),
        exchange: s(exchange),
        currency: s(currency),
        venue_name: s(venue_name),
        currency_unit: if currency == "GBP" {
            s("GBp")
        } else {
            s(currency)
        },
        figi: Some(format!("BBG-MOCK-{ticker}")),
        sedol: Some(format!("SEDOL-{ticker}")),
        last_price: mock_listing_price(ticker),
        last_price_date: s("2026-06-20"),
        status: s("Active"),
        source: s("seed"),
    }
}

fn mock_listing_price(ticker: &str) -> f64 {
    match ticker {
        "VUSA" => 92.18,
        "VUSD" => 123.42,
        "ISF" => 8.26,
        "JEGP" => 24.64,
        "JEPG" => 31.28,
        "VHYL" => 34.99,
        _ => 10.0,
    }
}

#[allow(clippy::too_many_arguments)]
fn distribution(
    fund_id: &str,
    ticker: &str,
    ex_date: &str,
    payment_date: &str,
    amount: f64,
    currency: &str,
    status: &str,
    source: &str,
) -> Distribution {
    Distribution {
        fund_id: s(fund_id),
        ticker: s(ticker),
        ex_date: s(ex_date),
        payment_date: s(payment_date),
        amount,
        currency: s(currency),
        status: s(status),
        source: s(source),
    }
}

#[allow(clippy::too_many_arguments)]
fn holding(
    company: &str,
    ticker: &str,
    country: &str,
    sector: &str,
    weight_pct: f64,
    source_etf: &str,
    as_of_date: &str,
    source: &str,
) -> HoldingExposure {
    HoldingExposure {
        company: s(company),
        ticker: s(ticker),
        country: s(country),
        sector: s(sector),
        weight_pct,
        change_since_previous_pct: mock_holding_change(ticker),
        source_etf: s(source_etf),
        as_of_date: s(as_of_date),
        source: s(source),
    }
}

fn mock_holding_change(ticker: &str) -> Option<f64> {
    match ticker {
        "NVDA" => Some(0.82),
        "MSFT" => Some(-0.14),
        "AAPL" => Some(0.06),
        "AZN" => Some(-0.21),
        "HSBA" => Some(0.18),
        _ => None,
    }
}

fn slice(label: &str, value_pct: f64) -> ExposureSlice {
    ExposureSlice {
        label: s(label),
        value_pct,
    }
}

#[allow(clippy::too_many_arguments)]
fn alert(
    id: &str,
    severity: AlertSeverity,
    category: &str,
    title: &str,
    message: &str,
    fund_ticker: Option<&str>,
    read: bool,
    dismissed: bool,
    created_time: &str,
) -> Alert {
    Alert {
        id: s(id),
        severity,
        category: s(category),
        title: s(title),
        message: s(message),
        fund_ticker: fund_ticker.map(s),
        status: if dismissed {
            s("DISMISSED")
        } else if read {
            s("READ")
        } else {
            s("OPEN")
        },
        source: s("mock"),
        read,
        dismissed,
        created_time: s(created_time),
    }
}

#[allow(clippy::too_many_arguments)]
fn document(
    fund_id: &str,
    ticker: &str,
    document_type: &str,
    latest_date: &str,
    status: &str,
    content_hash_change: &str,
    source: &str,
    last_checked: &str,
) -> DocumentSnapshot {
    DocumentSnapshot {
        fund_id: s(fund_id),
        ticker: s(ticker),
        document_type: s(document_type),
        latest_date: s(latest_date),
        status: s(status),
        content_hash_change: s(content_hash_change),
        source: s(source),
        last_checked: s(last_checked),
    }
}

fn scheduled_job(
    name: &str,
    job_type: &str,
    source: &str,
    cron_schedule: &str,
    active: bool,
    last_run: &str,
    next_run: &str,
) -> ScheduledJob {
    ScheduledJob {
        name: s(name),
        job_type: s(job_type),
        source: s(source),
        cron_schedule: s(cron_schedule),
        active,
        last_run: s(last_run),
        next_run: s(next_run),
    }
}

#[allow(clippy::too_many_arguments)]
fn job_run(
    id: &str,
    job_type: &str,
    source: &str,
    status: JobStatus,
    started: &str,
    finished: Option<&str>,
    inserted: u32,
    updated: u32,
    failed: u32,
    message: &str,
) -> JobRun {
    JobRun {
        id: s(id),
        job_type: s(job_type),
        source: s(source),
        status,
        started: s(started),
        finished: finished.map(s),
        inserted,
        updated,
        failed,
        message: s(message),
    }
}

fn mock_data_operations() -> DataOperationsSnapshot {
    DataOperationsSnapshot {
        readiness_stages: vec![
            readiness_stage(
                "holdings",
                "Holdings",
                DataOperationStatus::Partial,
                Some(86.0),
                "6 holdings",
                "Issuer factsheets loaded, but one holdings ingestion failed for JEGP.",
                "issuer_fixture",
                "2026-06-19 23:08",
                "Run holdings ingestion",
            ),
            readiness_stage(
                "identity",
                "Identity",
                DataOperationStatus::Partial,
                Some(84.0),
                "12 ambiguous",
                "Most top holdings are mapped; JEPG listings remain ambiguous.",
                "constituent_identity_fixture",
                "2026-06-20 05:44",
                "Resolve constituent identities",
            ),
            readiness_stage(
                "prices",
                "Prices",
                DataOperationStatus::Stale,
                Some(79.0),
                "31 missing",
                "Portfolio prices are mostly current; constituent EOD prices have gaps.",
                "instrument_price_fixture",
                "2026-06-20 06:04",
                "Fetch constituent EOD prices",
            ),
            readiness_stage(
                "fx",
                "FX",
                DataOperationStatus::Ok,
                Some(100.0),
                "6 rates",
                "Mock ECB fixture has the workspace currency pairs needed today.",
                "fx_fixture",
                "2026-06-20 06:15",
                "No action",
            ),
            readiness_stage(
                "exposure",
                "Exposure",
                DataOperationStatus::Ready,
                Some(100.0),
                "latest 2026-06-20",
                "Current exposure can be computed from effective mock holdings.",
                "derived",
                "2026-06-20 06:20",
                "Recompute after price backfill",
            ),
            readiness_stage(
                "performance",
                "Performance",
                DataOperationStatus::Mock,
                Some(62.0),
                "TR deferred",
                "Top-holding contribution is represented as fixture-shaped data only.",
                "mock",
                "2026-06-20 06:20",
                "Review top-holding performance",
            ),
            readiness_stage(
                "alerts",
                "Alerts",
                DataOperationStatus::Failed,
                Some(50.0),
                "2 critical",
                "Critical stale-price and failed-job alerts are open.",
                "mock",
                "2026-06-20 06:20",
                "Review failed fetch logs",
            ),
            readiness_stage(
                "jobs",
                "Jobs",
                DataOperationStatus::Running,
                Some(75.0),
                "1 running",
                "Document backfill is running; holdings ingestion failed last run.",
                "scheduler_fixture",
                "2026-06-20 07:20",
                "Open Jobs",
            ),
            readiness_stage(
                "sources",
                "Sources",
                DataOperationStatus::BudgetBlocked,
                Some(88.0),
                "openfigi backoff",
                "OpenFIGI identity lookups are backing off after rate limits.",
                "source_budget_fixture",
                "2026-06-20 07:24",
                "Open source budget",
            ),
        ],
        market_data_plan: vec![
            plan_item(
                "plan-resolve-jepg",
                1,
                "resolve_constituent_identity",
                "JEPG/JEGP listing identity",
                Some(AnalysisSubject::Fund("fund-jepg".to_owned())),
                "Ticker maps to two listings for the same underlying fund.",
                "openfigi",
                2,
                DataOperationStatus::Ambiguous,
                Some("OpenFIGI budget is backing off until 2026-06-20 08:10"),
                "Resolve identity",
            ),
            plan_item(
                "plan-fetch-constituent-prices",
                2,
                "fetch_constituent_price",
                "VUSA top holdings",
                Some(AnalysisSubject::Holding {
                    ticker: "MSFT".to_owned(),
                    source: "VUSA".to_owned(),
                }),
                "31 constituent close prices are missing for the latest exposure date.",
                "yfinance",
                31,
                DataOperationStatus::Missing,
                None,
                "Fetch constituent EOD prices",
            ),
            plan_item(
                "plan-refresh-holdings-jegp",
                3,
                "refresh_holdings",
                "JEGP holdings",
                Some(AnalysisSubject::Fund("fund-jepg".to_owned())),
                "Latest factsheet changed and the parser failed on one provider document.",
                "issuer_fixture",
                1,
                DataOperationStatus::Failed,
                Some("JEGP factsheet parse failed"),
                "Run holdings ingestion",
            ),
            plan_item(
                "plan-refresh-documents",
                4,
                "refresh_documents",
                "Changed documents",
                None,
                "JEGP factsheet hash changed since previous snapshot.",
                "document_fixture",
                3,
                DataOperationStatus::Needed,
                None,
                "Review changed document",
            ),
            plan_item(
                "plan-fetch-fx",
                5,
                "fetch_fx_rate",
                "USD/GBP FX",
                Some(AnalysisSubject::Cash("USD".to_owned())),
                "FX rates are current in mock mode.",
                "fx_fixture",
                0,
                DataOperationStatus::Ready,
                None,
                "No action",
            ),
            plan_item(
                "plan-recompute-exposure",
                6,
                "recompute_exposure",
                "Main Portfolio",
                Some(AnalysisSubject::WorkspacePortfolio(
                    "workspace-main".to_owned(),
                )),
                "Exposure is ready but should be recomputed after constituent price backfill.",
                "derived",
                0,
                DataOperationStatus::Planned,
                None,
                "Recompute exposure",
            ),
        ],
        source_budgets: vec![
            source_budget(
                "openfigi",
                true,
                DataOperationStatus::BudgetBlocked,
                25,
                25,
                "per 6h",
                "6s",
                Some("2026-06-20 08:10"),
                3,
                8,
                "2026-06-20 08:10",
                &["identity", "listing", "figi"],
            ),
            source_budget(
                "stooq",
                true,
                DataOperationStatus::Ok,
                18,
                200,
                "per day",
                "1s",
                None,
                0,
                42,
                "now",
                &["prices", "eod"],
            ),
            source_budget(
                "yfinance",
                true,
                DataOperationStatus::Stale,
                140,
                500,
                "per day",
                "2s",
                None,
                2,
                76,
                "2026-06-20 07:30",
                &["constituent prices", "splits"],
            ),
            source_budget(
                "issuer_fixture",
                true,
                DataOperationStatus::Failed,
                6,
                50,
                "per day",
                "10s",
                None,
                1,
                11,
                "now",
                &["holdings", "factsheets", "documents"],
            ),
            source_budget(
                "instrument_price_fixture",
                true,
                DataOperationStatus::Mock,
                0,
                0,
                "fixture",
                "0s",
                None,
                0,
                18,
                "now",
                &["prices", "offline"],
            ),
            source_budget(
                "constituent_identity_fixture",
                true,
                DataOperationStatus::Mock,
                0,
                0,
                "fixture",
                "0s",
                None,
                0,
                12,
                "now",
                &["identity", "offline"],
            ),
            source_budget(
                "fx_fixture",
                true,
                DataOperationStatus::Ok,
                0,
                0,
                "fixture",
                "0s",
                None,
                0,
                6,
                "now",
                &["fx", "offline"],
            ),
            source_budget(
                "document_fixture",
                true,
                DataOperationStatus::Running,
                4,
                25,
                "per hour",
                "5s",
                None,
                0,
                18,
                "running",
                &["documents", "hashes"],
            ),
            source_budget(
                "distribution_fixture",
                true,
                DataOperationStatus::Ok,
                0,
                0,
                "fixture",
                "0s",
                None,
                0,
                5,
                "now",
                &["distributions", "offline"],
            ),
        ],
        fetch_logs: vec![
            fetch_log(
                "fetch-1",
                "2026-06-20 07:24",
                "openfigi",
                "identity_map",
                "https://openfigi.example/mapping?apikey=dev-secret&isin=IE000JEPG001",
                DataOperationStatus::BudgetBlocked,
                Some(429),
                1420,
                false,
                true,
                Some("rate limit; retry after 46m"),
            ),
            fetch_log(
                "fetch-2",
                "2026-06-20 07:20",
                "document_fixture",
                "document_hash",
                "issuer://JEGP/factsheet/2026-05-31",
                DataOperationStatus::Running,
                None,
                0,
                false,
                false,
                None,
            ),
            fetch_log(
                "fetch-3",
                "2026-06-20 06:04",
                "stooq",
                "listing_eod",
                "XLON:VUSA:2026-06-20",
                DataOperationStatus::Fresh,
                Some(200),
                320,
                false,
                false,
                None,
            ),
            fetch_log(
                "fetch-4",
                "2026-06-19 23:08",
                "issuer_fixture",
                "factsheet_parse",
                "issuer://JEGP/factsheet/2026-05-31 token=fixture-secret",
                DataOperationStatus::Failed,
                Some(200),
                870,
                true,
                false,
                Some("table header changed"),
            ),
            fetch_log(
                "fetch-5",
                "2026-06-20 06:15",
                "fx_fixture",
                "fx_rate",
                "ECB:USDGBP:2026-06-20",
                DataOperationStatus::Fresh,
                Some(200),
                40,
                true,
                false,
                None,
            ),
        ],
        constituent_coverage: vec![
            constituent_row(
                "VUSA",
                "Microsoft",
                "MSFT",
                5.9,
                DataOperationStatus::Ready,
                Some("instrument-msft"),
                Some("listing-msft-us"),
                Some(478.87),
                Some("2026-06-20"),
                "yfinance",
                DataOperationStatus::Fresh,
                "Ready for exposure",
            ),
            constituent_row(
                "VUSA",
                "NVIDIA",
                "NVDA",
                5.5,
                DataOperationStatus::Ready,
                Some("instrument-nvda"),
                Some("listing-nvda-us"),
                Some(142.63),
                Some("2026-06-18"),
                "yfinance",
                DataOperationStatus::Stale,
                "Fetch price",
            ),
            constituent_row(
                "VUSA",
                "Apple",
                "AAPL",
                5.1,
                DataOperationStatus::Ready,
                Some("instrument-aapl"),
                Some("listing-aapl-us"),
                Some(198.42),
                Some("2026-06-20"),
                "yfinance",
                DataOperationStatus::Fresh,
                "Ready for exposure",
            ),
            constituent_row(
                "ISF",
                "AstraZeneca",
                "AZN",
                7.2,
                DataOperationStatus::Ready,
                Some("instrument-azn"),
                Some("listing-azn-lse"),
                None,
                None,
                "stooq",
                DataOperationStatus::Missing,
                "Fetch price",
            ),
            constituent_row(
                "JEGP",
                "JPMorgan Global Equity Premium Income",
                "JEGP",
                19.66,
                DataOperationStatus::Ambiguous,
                Some("fund-jepg"),
                None,
                Some(24.64),
                Some("2026-06-20"),
                "issuer_fixture",
                DataOperationStatus::Fresh,
                "Resolve identity",
            ),
            constituent_row(
                "VHYL",
                "Nestle",
                "NESN",
                2.3,
                DataOperationStatus::Ready,
                Some("instrument-nesn"),
                Some("listing-nesn-six"),
                Some(91.12),
                Some("2026-06-17"),
                "yfinance",
                DataOperationStatus::Stale,
                "Fetch price",
            ),
        ],
        diagnostic_issues: vec![
            diagnostic_issue(
                "diag-openfigi-budget",
                AlertSeverity::Critical,
                "Identity source budget blocked",
                "OpenFIGI lookups are rate-limited; identity plan items should wait or use fixture fallback.",
                DataOperationStatus::BudgetBlocked,
                "openfigi",
                "Open source budget",
                "Source Budgets",
            ),
            diagnostic_issue(
                "diag-jegp-parser",
                AlertSeverity::Critical,
                "Holdings parser failed",
                "JEGP factsheet hash changed and the table parser failed on the latest snapshot.",
                DataOperationStatus::Failed,
                "issuer_fixture",
                "Review failed fetch logs",
                "Fetch Logs",
            ),
            diagnostic_issue(
                "diag-price-gaps",
                AlertSeverity::Warning,
                "Constituent prices missing",
                "31 top-holding price points are missing or stale for exposure contribution.",
                DataOperationStatus::Missing,
                "yfinance",
                "Fetch constituent EOD prices",
                "Constituent Coverage",
            ),
        ],
        backend_sections: Vec::new(),
        hydration: crate::domain::DataOperationsHydration {
            origin: crate::domain::DataOperationsOrigin::Mock,
            refreshed_at: Some("2026-06-20 07:24 mock".to_owned()),
            base_url: None,
            failed_sections: Vec::new(),
            last_error: None,
        },
    }
}

#[allow(clippy::too_many_arguments)]
fn readiness_stage(
    key: &str,
    label: &str,
    status: DataOperationStatus,
    coverage_pct: Option<f32>,
    count_label: &str,
    detail: &str,
    source: &str,
    freshness: &str,
    recommended_action: &str,
) -> ReadinessStage {
    ReadinessStage {
        key: s(key),
        label: s(label),
        status,
        coverage_pct,
        count_label: s(count_label),
        detail: s(detail),
        source: s(source),
        freshness: s(freshness),
        recommended_action: s(recommended_action),
    }
}

#[allow(clippy::too_many_arguments)]
fn plan_item(
    id: &str,
    priority: u8,
    item_type: &str,
    subject_label: &str,
    subject: Option<AnalysisSubject>,
    reason: &str,
    source: &str,
    estimated_requests: u32,
    status: DataOperationStatus,
    blocker: Option<&str>,
    next_action: &str,
) -> MarketDataPlanItem {
    MarketDataPlanItem {
        id: s(id),
        priority,
        item_type: s(item_type),
        subject_label: s(subject_label),
        subject,
        reason: s(reason),
        source: s(source),
        estimated_requests,
        status,
        blocker: blocker.map(s),
        next_action: s(next_action),
    }
}

#[allow(clippy::too_many_arguments)]
fn source_budget(
    source: &str,
    enabled: bool,
    status: DataOperationStatus,
    requests_used: u32,
    requests_limit: u32,
    window: &str,
    min_delay: &str,
    backoff_until: Option<&str>,
    recent_failures: u32,
    cache_hits: u32,
    next_allowed: &str,
    capabilities: &[&str],
) -> SourceBudget {
    SourceBudget {
        source: s(source),
        enabled,
        status,
        requests_used,
        requests_limit,
        window: s(window),
        min_delay: s(min_delay),
        backoff_until: backoff_until.map(s),
        recent_failures,
        cache_hits,
        next_allowed: s(next_allowed),
        capabilities: capabilities.iter().map(|value| s(value)).collect(),
    }
}

#[allow(clippy::too_many_arguments)]
fn fetch_log(
    id: &str,
    time: &str,
    source: &str,
    request_kind: &str,
    request_key: &str,
    status: DataOperationStatus,
    http_status: Option<u16>,
    duration_ms: u32,
    cache_hit: bool,
    rate_limited: bool,
    error: Option<&str>,
) -> SourceFetchLog {
    SourceFetchLog {
        id: s(id),
        time: s(time),
        source: s(source),
        request_kind: s(request_kind),
        request_key: s(request_key),
        status,
        http_status,
        duration_ms,
        cache_hit,
        rate_limited,
        error: error.map(s),
    }
}

#[allow(clippy::too_many_arguments)]
fn constituent_row(
    fund_ticker: &str,
    holding_name: &str,
    holding_ticker: &str,
    weight_pct: f64,
    identity_status: DataOperationStatus,
    instrument_id: Option<&str>,
    listing_id: Option<&str>,
    latest_price: Option<f64>,
    price_date: Option<&str>,
    price_source: &str,
    price_status: DataOperationStatus,
    next_action: &str,
) -> ConstituentReadinessRow {
    ConstituentReadinessRow {
        fund_ticker: s(fund_ticker),
        holding_name: s(holding_name),
        holding_ticker: s(holding_ticker),
        weight_pct,
        subject: AnalysisSubject::Holding {
            ticker: s(holding_ticker),
            source: s(fund_ticker),
        },
        identity_status,
        instrument_id: instrument_id.map(s),
        listing_id: listing_id.map(s),
        latest_price,
        price_date: price_date.map(s),
        price_source: s(price_source),
        price_status,
        next_action: s(next_action),
    }
}

#[allow(clippy::too_many_arguments)]
fn diagnostic_issue(
    id: &str,
    severity: AlertSeverity,
    title: &str,
    detail: &str,
    status: DataOperationStatus,
    source: &str,
    recommended_action: &str,
    related_page: &str,
) -> DataDiagnosticIssue {
    DataDiagnosticIssue {
        id: s(id),
        severity,
        title: s(title),
        detail: s(detail),
        status,
        source: s(source),
        recommended_action: s(recommended_action),
        related_page: s(related_page),
    }
}

fn mock_time_series() -> Vec<TimeSeries> {
    vec![
        series(
            "portfolio-main-value",
            AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
            "Main Portfolio value",
            TimeSeriesKind::PortfolioValue,
            "GBP",
            &[
                ("2025-07-31", 161_240.0),
                ("2025-08-31", 164_510.0),
                ("2025-09-30", 166_880.0),
                ("2025-10-31", 169_210.0),
                ("2025-11-30", 171_020.0),
                ("2025-12-31", 173_650.0),
                ("2026-01-31", 175_130.0),
                ("2026-02-28", 176_940.0),
                ("2026-03-31", 179_360.0),
                ("2026-04-30", 181_910.0),
                ("2026-05-31", 183_440.0),
                ("2026-06-20", 184_260.42),
            ],
            "seed",
            "FRESH",
        ),
        series(
            "portfolio-main-income",
            AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
            "Projected income",
            TimeSeriesKind::ProjectedIncome,
            "GBP",
            &[
                ("2025-07-31", 5_180.0),
                ("2025-08-31", 5_240.0),
                ("2025-09-30", 5_310.0),
                ("2025-10-31", 5_420.0),
                ("2025-11-30", 5_560.0),
                ("2025-12-31", 5_630.0),
                ("2026-01-31", 5_740.0),
                ("2026-02-28", 5_810.0),
                ("2026-03-31", 5_930.0),
                ("2026-04-30", 6_020.0),
                ("2026-05-31", 6_130.0),
                ("2026-06-20", 6_214.85),
            ],
            "seed",
            "FRESH",
        ),
        listing_price_series(
            "fund-vusa",
            "listing-vusa-lse-gbp",
            "VUSA price",
            "GBP",
            &[
                ("2025-07-31", 82.14),
                ("2025-08-31", 83.32),
                ("2025-09-30", 84.05),
                ("2025-10-31", 85.44),
                ("2025-11-30", 86.20),
                ("2025-12-31", 87.11),
                ("2026-01-31", 88.04),
                ("2026-02-28", 89.33),
                ("2026-03-31", 90.62),
                ("2026-04-30", 91.08),
                ("2026-05-31", 91.76),
                ("2026-06-20", 92.18),
            ],
            "stooq",
            "FRESH",
        ),
        listing_market_value_series(
            "fund-vusa",
            "listing-vusa-lse-gbp",
            "VUSA market value",
            &[
                ("2025-07-31", 70_640.40),
                ("2025-08-31", 71_655.20),
                ("2025-09-30", 72_283.00),
                ("2025-10-31", 73_478.40),
                ("2025-11-30", 74_132.00),
                ("2025-12-31", 74_914.60),
                ("2026-01-31", 75_714.40),
                ("2026-02-28", 76_823.80),
                ("2026-03-31", 77_933.20),
                ("2026-04-30", 78_328.80),
                ("2026-05-31", 78_913.60),
                ("2026-06-20", 79_274.80),
            ],
            "stooq",
            "FRESH",
        ),
        listing_price_series(
            "fund-isf",
            "listing-isf-lse-gbp",
            "ISF price",
            "GBP",
            &[
                ("2025-07-31", 7.66),
                ("2025-08-31", 7.72),
                ("2025-09-30", 7.81),
                ("2025-10-31", 7.90),
                ("2025-11-30", 7.98),
                ("2025-12-31", 8.05),
                ("2026-01-31", 8.10),
                ("2026-02-28", 8.18),
                ("2026-03-31", 8.21),
                ("2026-04-30", 8.24),
                ("2026-05-31", 8.29),
                ("2026-06-20", 8.26),
            ],
            "stooq",
            "FRESH",
        ),
        listing_market_value_series(
            "fund-isf",
            "listing-isf-lse-gbp",
            "ISF market value",
            &[
                ("2025-07-31", 24_512.00),
                ("2025-08-31", 24_704.00),
                ("2025-09-30", 24_992.00),
                ("2025-10-31", 25_280.00),
                ("2025-11-30", 25_536.00),
                ("2025-12-31", 25_760.00),
                ("2026-01-31", 25_920.00),
                ("2026-02-28", 26_176.00),
                ("2026-03-31", 26_272.00),
                ("2026-04-30", 26_368.00),
                ("2026-05-31", 26_528.00),
                ("2026-06-20", 26_432.00),
            ],
            "stooq",
            "FRESH",
        ),
        listing_price_series(
            "fund-jepg",
            "listing-jegp-lse-gbp",
            "JEGP price",
            "GBP",
            &[
                ("2025-07-31", 22.80),
                ("2025-08-31", 23.02),
                ("2025-09-30", 23.20),
                ("2025-10-31", 23.36),
                ("2025-11-30", 23.48),
                ("2025-12-31", 23.60),
                ("2026-01-31", 23.82),
                ("2026-02-28", 23.95),
                ("2026-03-31", 24.12),
                ("2026-04-30", 24.25),
                ("2026-05-31", 24.53),
                ("2026-06-20", 24.64),
            ],
            "issuer",
            "PENDING",
        ),
        listing_market_value_series(
            "fund-jepg",
            "listing-jegp-lse-gbp",
            "JEGP market value",
            &[
                ("2025-07-31", 33_516.00),
                ("2025-08-31", 33_839.40),
                ("2025-09-30", 34_104.00),
                ("2025-10-31", 34_339.20),
                ("2025-11-30", 34_515.60),
                ("2025-12-31", 34_692.00),
                ("2026-01-31", 35_015.40),
                ("2026-02-28", 35_206.50),
                ("2026-03-31", 35_456.40),
                ("2026-04-30", 35_647.50),
                ("2026-05-31", 36_059.10),
                ("2026-06-20", 36_220.80),
            ],
            "issuer",
            "PENDING",
        ),
        listing_price_series(
            "fund-vhyl",
            "listing-vhyl-lse-gbp",
            "VHYL price",
            "GBP",
            &[
                ("2025-07-31", 33.12),
                ("2025-08-31", 33.28),
                ("2025-09-30", 33.61),
                ("2025-10-31", 33.80),
                ("2025-11-30", 34.05),
                ("2025-12-31", 34.18),
                ("2026-01-31", 34.42),
                ("2026-02-28", 34.58),
                ("2026-03-31", 34.72),
                ("2026-04-30", 35.08),
                ("2026-05-31", 35.17),
                ("2026-06-19", 34.99),
            ],
            "stooq",
            "STALE",
        ),
        listing_market_value_series(
            "fund-vhyl",
            "listing-vhyl-lse-gbp",
            "VHYL market value",
            &[
                ("2025-07-31", 40_075.20),
                ("2025-08-31", 40_268.80),
                ("2025-09-30", 40_668.10),
                ("2025-10-31", 40_898.00),
                ("2025-11-30", 41_200.50),
                ("2025-12-31", 41_357.80),
                ("2026-01-31", 41_648.20),
                ("2026-02-28", 41_841.80),
                ("2026-03-31", 42_011.20),
                ("2026-04-30", 42_446.80),
                ("2026-05-31", 42_555.70),
                ("2026-06-19", 42_337.90),
            ],
            "stooq",
            "STALE",
        ),
        series(
            "vusa-nav",
            AnalysisSubject::FundListing {
                fund_id: "fund-vusa".to_owned(),
                listing_id: "listing-vusa-lse-gbp".to_owned(),
            },
            "VUSA NAV",
            TimeSeriesKind::Nav,
            "GBP",
            &[
                ("2026-01-31", 88.01),
                ("2026-02-28", 89.29),
                ("2026-03-31", 90.55),
                ("2026-04-30", 91.02),
                ("2026-05-31", 91.70),
                ("2026-06-20", 92.09),
            ],
            "issuer",
            "FRESH",
        ),
        series(
            "vusa-distributions",
            AnalysisSubject::FundListing {
                fund_id: "fund-vusa".to_owned(),
                listing_id: "listing-vusa-lse-gbp".to_owned(),
            },
            "VUSA distributions",
            TimeSeriesKind::Distribution,
            "GBP/share",
            &[
                ("2025-09-13", 0.274),
                ("2025-12-13", 0.286),
                ("2026-03-14", 0.291),
                ("2026-06-13", 0.317),
            ],
            "provider",
            "FRESH",
        ),
    ]
}

fn listing_price_series(
    fund_id: &str,
    listing_id: &str,
    label: &str,
    unit: &str,
    points: &[(&str, f64)],
    source: &str,
    status: &str,
) -> TimeSeries {
    series(
        listing_id,
        AnalysisSubject::FundListing {
            fund_id: fund_id.to_owned(),
            listing_id: listing_id.to_owned(),
        },
        label,
        TimeSeriesKind::Price,
        unit,
        points,
        source,
        status,
    )
}

fn listing_market_value_series(
    fund_id: &str,
    listing_id: &str,
    label: &str,
    points: &[(&str, f64)],
    source: &str,
    status: &str,
) -> TimeSeries {
    series(
        listing_id,
        AnalysisSubject::FundListing {
            fund_id: fund_id.to_owned(),
            listing_id: listing_id.to_owned(),
        },
        label,
        TimeSeriesKind::MarketValue,
        "GBP",
        points,
        source,
        status,
    )
}

#[allow(clippy::too_many_arguments)]
fn series(
    id: &str,
    subject: AnalysisSubject,
    label: &str,
    kind: TimeSeriesKind,
    unit: &str,
    points: &[(&str, f64)],
    source: &str,
    status: &str,
) -> TimeSeries {
    TimeSeries::new(
        format!(
            "{id}-{}",
            kind.as_str().to_ascii_lowercase().replace(' ', "-")
        ),
        subject,
        label,
        kind,
        unit,
        points
            .iter()
            .map(|(date, value)| TimeSeriesPoint::new(*date, *value, status, source))
            .collect(),
    )
    .with_source(source)
    .with_status(status)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ticker_request(symbol: &str) -> InstrumentResolutionRequest {
        InstrumentResolutionRequest {
            symbol: symbol.to_owned(),
            symbol_type: SymbolType::Ticker,
            exchange: None,
            currency: None,
        }
    }

    #[test]
    fn resolves_known_ticker() {
        let provider = MockDataProvider::default();
        let result = provider.resolve_instrument(&ticker_request("VUSA"));

        match result {
            InstrumentResolutionResult::Resolved { fund, listing, .. } => {
                assert_eq!(fund.id, "fund-vusa");
                assert_eq!(listing.ticker, "VUSA");
                assert_eq!(listing.source, "seed");
            }
            other => panic!("expected resolved result, got {other:?}"),
        }
    }

    #[test]
    fn returns_ambiguous_jepg_listings() {
        let provider = MockDataProvider::default();
        let result = provider.resolve_instrument(&ticker_request("JEPG"));

        match result {
            InstrumentResolutionResult::Ambiguous { candidates } => {
                assert_eq!(candidates.len(), 2);
                assert!(
                    candidates
                        .iter()
                        .any(|candidate| candidate.listing.ticker == "JEGP")
                );
                assert!(
                    candidates
                        .iter()
                        .any(|candidate| candidate.listing.ticker == "JEPG")
                );
            }
            other => panic!("expected ambiguous result, got {other:?}"),
        }
    }

    #[test]
    fn reports_unknown_ticker_as_not_found() {
        let provider = MockDataProvider::default();
        let result = provider.resolve_instrument(&ticker_request("NOPE"));

        assert!(matches!(
            result,
            InstrumentResolutionResult::NotFound { .. }
        ));
    }

    #[test]
    fn loads_mock_time_series_for_portfolio_and_vusa() {
        let provider = MockDataProvider::default();
        let series = provider.load_time_series("workspace-main");

        assert!(series.iter().any(|series| {
            series.subject == AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned())
                && series.kind == TimeSeriesKind::PortfolioValue
        }));
        assert!(series.iter().any(|series| {
            series.subject
                == (AnalysisSubject::FundListing {
                    fund_id: "fund-vusa".to_owned(),
                    listing_id: "listing-vusa-lse-gbp".to_owned(),
                })
                && series.kind == TimeSeriesKind::Price
        }));
    }
}
