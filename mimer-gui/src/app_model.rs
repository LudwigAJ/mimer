use crate::api::types::DataMode;
use crate::domain::{
    Alert, DataOperationsSnapshot, Distribution, DocumentSnapshot, ExposureBreakdown, Fund,
    FundListing, HoldingExposure, InvestableNode, JobRun, PortfolioSummary, Position, ScheduledJob,
    Workspace,
};
use crate::format::{fmt_source, fmt_status};
use crate::timeseries::TimeSeries;

#[derive(Clone, Debug)]
#[allow(dead_code)]
pub enum LoadState<T> {
    Idle,
    Loading,
    Loaded(T),
    Error(String),
}

impl<T> LoadState<T> {
    pub fn as_loaded(&self) -> Option<&T> {
        match self {
            Self::Loaded(value) => Some(value),
            Self::Idle | Self::Loading | Self::Error(_) => None,
        }
    }

    pub fn as_loaded_mut(&mut self) -> Option<&mut T> {
        match self {
            Self::Loaded(value) => Some(value),
            Self::Idle | Self::Loading | Self::Error(_) => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[allow(dead_code)]
pub enum RefreshStatus {
    Fresh,
    Stale,
    Refreshing,
    Error,
}

impl RefreshStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Fresh => "FRESH",
            Self::Stale => "STALE",
            Self::Refreshing => "RUNNING",
            Self::Error => "FAILED",
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SelectedInstrument {
    pub fund_id: Option<String>,
    pub listing_id: Option<String>,
}

impl SelectedInstrument {
    pub fn clear(&mut self) {
        self.fund_id = None;
        self.listing_id = None;
    }

    pub fn select_fund(&mut self, fund_id: impl Into<String>) {
        self.fund_id = Some(fund_id.into());
        self.listing_id = None;
    }

    pub fn select_listing(&mut self, fund_id: impl Into<String>, listing_id: impl Into<String>) {
        self.fund_id = Some(fund_id.into());
        self.listing_id = Some(listing_id.into());
    }

    pub fn is_empty(&self) -> bool {
        self.fund_id.is_none()
    }
}

#[derive(Clone, Debug)]
pub struct DashboardSnapshot {
    pub workspace: Workspace,
    pub workspaces: Vec<Workspace>,
    pub portfolio_summary: PortfolioSummary,
    pub positions: Vec<Position>,
    pub funds: Vec<Fund>,
    pub distributions: Vec<Distribution>,
    pub holdings: Vec<HoldingExposure>,
    pub exposures: ExposureBreakdown,
    pub alerts: Vec<Alert>,
    pub documents: Vec<DocumentSnapshot>,
    pub scheduled_jobs: Vec<ScheduledJob>,
    pub job_runs: Vec<JobRun>,
    pub data_operations: DataOperationsSnapshot,
    pub portfolio_tree: InvestableNode,
    pub time_series: Vec<TimeSeries>,
    pub selected: SelectedInstrument,
    pub last_refresh_at: String,
    pub data_mode: DataMode,
    pub data_status: RefreshStatus,
}

impl DashboardSnapshot {
    pub fn selected_fund(&self) -> Option<&Fund> {
        let fund_id = self.selected.fund_id.as_deref()?;
        self.find_fund_by_id(fund_id)
    }

    pub fn selected_listing(&self) -> Option<&FundListing> {
        let listing_id = self.selected.listing_id.as_deref()?;
        self.funds
            .iter()
            .flat_map(|fund| fund.listings.iter())
            .find(|listing| listing.id == listing_id)
    }

    pub fn selected_context_label(&self) -> String {
        format!("Selected: {}", self.selected_subject_label())
    }

    pub fn selected_subject_label(&self) -> String {
        let Some(fund) = self.selected_fund() else {
            return "-".to_owned();
        };

        if let Some(listing) = self.selected_listing() {
            return listing.ticker.clone();
        }

        if !fund.isin.trim().is_empty() {
            fund.isin.clone()
        } else if !fund.name.trim().is_empty() {
            fund.name.clone()
        } else {
            "-".to_owned()
        }
    }

    pub fn selected_subject_tooltip(&self) -> String {
        let Some(fund) = self.selected_fund() else {
            return "No selected subject".to_owned();
        };

        let listing = self.selected_listing();
        let position = listing.and_then(|listing| {
            self.positions
                .iter()
                .find(|position| position.fund_id == fund.id && position.listing_id == listing.id)
        });
        let status = position
            .map(|position| fmt_status(position.freshness.as_str()))
            .unwrap_or_else(|| fmt_status(&fund.status));
        let source = position
            .map(|position| fmt_source(&position.source))
            .or_else(|| listing.map(|listing| fmt_source(&listing.source)))
            .unwrap_or_else(|| fmt_source(&fund.source));
        let listing_line = listing
            .map(|listing| format!("Listing: {} / {}", listing.exchange, listing.currency))
            .unwrap_or_else(|| format!("Fund base: {}", fund.base_currency));

        format!(
            "{}\nISIN: {}\n{}\nStatus: {}\nSource: {}",
            fund.name, fund.isin, listing_line, status, source
        )
    }

    #[allow(dead_code)]
    pub fn find_fund_by_ticker(&self, ticker: &str) -> Option<(&Fund, &FundListing)> {
        self.funds.iter().find_map(|fund| {
            fund.listings
                .iter()
                .find(|listing| listing.ticker.eq_ignore_ascii_case(ticker))
                .map(|listing| (fund, listing))
        })
    }

    pub fn find_fund_by_id(&self, fund_id: &str) -> Option<&Fund> {
        self.funds.iter().find(|fund| fund.id == fund_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AnalysisSubject, InvestableKind};

    #[test]
    fn selected_subject_prefers_listing_ticker() {
        let snapshot = selected_test_snapshot(Some("listing-vusa".to_owned()));

        assert_eq!(snapshot.selected_subject_label(), "VUSA");
        assert!(
            snapshot
                .selected_subject_tooltip()
                .contains("Listing: XLON / GBP")
        );
    }

    #[test]
    fn selected_subject_falls_back_to_isin_for_fund_selection() {
        let snapshot = selected_test_snapshot(None);

        assert_eq!(snapshot.selected_subject_label(), "IE00B3XXRP09");
        assert!(snapshot.selected_subject_tooltip().contains("Vanguard"));
    }

    fn selected_test_snapshot(listing_id: Option<String>) -> DashboardSnapshot {
        let fund = Fund {
            id: "fund-vusa".to_owned(),
            name: "Vanguard S&P 500 UCITS ETF".to_owned(),
            provider: "Vanguard".to_owned(),
            isin: "IE00B3XXRP09".to_owned(),
            strategy: "US equity".to_owned(),
            domicile: "Ireland".to_owned(),
            base_currency: "USD".to_owned(),
            distribution_policy: "Distributing".to_owned(),
            ocf_ter_pct: 0.07,
            distribution_frequency: "Quarterly".to_owned(),
            replication: "Physical".to_owned(),
            status: "Active".to_owned(),
            last_refreshed: "2026-06-20".to_owned(),
            source: "seed".to_owned(),
            listings: vec![FundListing {
                id: "listing-vusa".to_owned(),
                fund_id: "fund-vusa".to_owned(),
                ticker: "VUSA".to_owned(),
                exchange: "XLON".to_owned(),
                currency: "GBP".to_owned(),
                venue_name: "London Stock Exchange".to_owned(),
                currency_unit: "GBp".to_owned(),
                figi: None,
                sedol: None,
                last_price: 92.18,
                last_price_date: "2026-06-20".to_owned(),
                status: "Active".to_owned(),
                source: "seed".to_owned(),
            }],
        };

        DashboardSnapshot {
            workspace: Workspace {
                id: "workspace-main".to_owned(),
                name: "Main Portfolio".to_owned(),
                base_currency: "GBP".to_owned(),
            },
            workspaces: Vec::new(),
            portfolio_summary: PortfolioSummary {
                total_value: 0.0,
                daily_change: 0.0,
                unrealised_gain_loss: 0.0,
                trailing_12m_income: 0.0,
                projected_annual_income: 0.0,
                base_currency: "GBP".to_owned(),
                position_count: 0,
                stale_warning_count: 0,
            },
            positions: Vec::new(),
            funds: vec![fund],
            distributions: Vec::new(),
            holdings: Vec::new(),
            exposures: ExposureBreakdown {
                countries: Vec::new(),
                sectors: Vec::new(),
                currencies: Vec::new(),
                top_holdings: Vec::new(),
            },
            alerts: Vec::new(),
            documents: Vec::new(),
            scheduled_jobs: Vec::new(),
            job_runs: Vec::new(),
            data_operations: DataOperationsSnapshot::default(),
            portfolio_tree: InvestableNode::new(
                "root",
                AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
                "Main Portfolio",
                InvestableKind::Portfolio,
            ),
            time_series: Vec::new(),
            selected: SelectedInstrument {
                fund_id: Some("fund-vusa".to_owned()),
                listing_id,
            },
            last_refresh_at: "2026-06-20".to_owned(),
            data_mode: DataMode::Mock,
            data_status: RefreshStatus::Fresh,
        }
    }
}
