use crate::charts::{ChartMode, ChartValueMode};
use crate::domain::{AnalysisSubject, Fund, FundListing};
use crate::pages::Page;
use crate::timeseries::TimeSeriesKind;

pub const COMMAND_SUGGESTIONS: &str = "Try: VUSA | data operations | refresh data operations | use api data | / VUSA | plot active | compare VUSA ISF | back";

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CommandAction {
    Navigate(Page),
    Search(String),
    SelectInstrument {
        fund_id: String,
        listing_id: Option<String>,
    },
    Compare {
        left_fund_id: String,
        right_fund_id: String,
    },
    Spread {
        left_fund_id: String,
        right_fund_id: String,
        plot: bool,
    },
    Diff {
        fund_id: Option<String>,
        listing_id: Option<String>,
        topic: String,
    },
    PlotSubject {
        subject: AnalysisSubject,
        series_kind: TimeSeriesKind,
        overlay_series_kinds: Vec<TimeSeriesKind>,
        comparison_subjects: Vec<AnalysisSubject>,
        label: String,
    },
    OverlaySubject {
        subject: AnalysisSubject,
        series_kind: TimeSeriesKind,
        label: String,
    },
    PlotSelected {
        series_kind: TimeSeriesKind,
        overlay_series_kinds: Vec<TimeSeriesKind>,
    },
    PlotActive,
    PlotPortfolio {
        series_kind: TimeSeriesKind,
    },
    SetChartMode(ChartMode),
    SetChartValueMode(ChartValueMode),
    SwapComparison,
    CopyChartRow,
    CopyChartValue,
    CopySelectedSeries,
    ClearChartSelection,
    SelectNextSeries,
    SelectPreviousSeries,
    SelectNextPoint,
    SelectPreviousPoint,
    OpenActive,
    CopyActive,
    CopySelected,
    CopyChart,
    CopyStatic {
        label: String,
        text: String,
    },
    ShowSources,
    ZoomIn,
    ZoomOut,
    ZoomReset,
    RunJob(String),
    AddInstrument(String),
    Back,
    Forward,
    Home,
    Drilldown,
    ShowHelp,
    ShowLegend,
    ShowOverrides,
    ClearOverrides,
    ClearSelectedOverrides,
    ClearOverlays,
    Diagnostics,
    Clear,
    Refresh,
    RefreshDataOperations,
    UseMockData,
    UseApiData,
    TestBackendConnection,
    CopyBackendUrl,
    ToggleInspector,
    ResetLayout,
    ResetTableColumns,
    ShowAllTableColumns,
    ClearTableFocus,
    PinInspector,
    UnpinInspector,
    OpenPinnedInspector,
    ToggleNav,
    ToggleContext,
    ToggleStatus,
    OpenSelected,
    OpenDocument,
    DismissSelectedAlert,
    ResolveSelectedAlert,
    RunSelectedJob,
    NoMatch(String),
    Empty,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CommandOutcome {
    Navigated(Page),
    Search(String),
    SelectedInstrument {
        ticker: String,
        fund_id: String,
        listing_id: Option<String>,
    },
    AddInstrument(String),
    Compare {
        left: String,
        right: String,
    },
    Spread {
        left: String,
        right: String,
        plot: bool,
    },
    Diff(String),
    Plot(String),
    Copied(String),
    Source(String),
    Zoom(u32),
    RunJob(String),
    Refreshed,
    Toggled(&'static str, bool),
    Cleared,
    Help,
    Legend,
    NoMatch(String),
    Error(String),
    Empty,
}

impl CommandOutcome {
    pub fn feedback(&self) -> String {
        match self {
            Self::Navigated(page) => format!("CMD: opened {}", page.label()),
            Self::Search(query) => {
                if query.trim().is_empty() {
                    "CMD: opened Search".to_owned()
                } else {
                    format!("CMD: search {}", query.trim())
                }
            }
            Self::SelectedInstrument { ticker, .. } => format!("CMD: selected {ticker}"),
            Self::AddInstrument(symbol) => format!("CMD: add {symbol}"),
            Self::Compare { left, right } => format!("CMD: compare {left} {right}"),
            Self::Spread { left, right, plot } => {
                if *plot {
                    format!("CMD: plot spread {left} - {right}")
                } else {
                    format!("CMD: spread {left} - {right}")
                }
            }
            Self::Diff(topic) => format!("CMD: diff {topic}"),
            Self::Plot(label) => format!("CMD: plot {label}"),
            Self::Copied(label) => format!("COPIED: {label}"),
            Self::Source(label) => format!("SOURCE: {label}"),
            Self::Zoom(percent) => format!("VIEW: zoom {percent}%"),
            Self::RunJob(job_name) => format!("CMD: mock run {job_name}"),
            Self::Refreshed => "CMD: queued refresh".to_owned(),
            Self::Toggled(label, enabled) => {
                format!("CMD: {label} {}", if *enabled { "shown" } else { "hidden" })
            }
            Self::Cleared => "CMD: cleared".to_owned(),
            Self::Help => "CMD: opened command help".to_owned(),
            Self::Legend => "CMD: opened data status legend".to_owned(),
            Self::NoMatch(input) => format!("CMD: no match for \"{input}\""),
            Self::Error(message) => format!("CMD: error {message}"),
            Self::Empty => "CMD: empty".to_owned(),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct CommandHistory {
    entries: Vec<String>,
    cursor: Option<usize>,
}

impl CommandHistory {
    pub fn push(&mut self, input: &str) {
        let trimmed = input.trim();
        if trimmed.is_empty() {
            self.cursor = None;
            return;
        }

        if self.entries.last().is_none_or(|last| last != trimmed) {
            self.entries.push(trimmed.to_owned());
        }
        self.cursor = None;
    }

    pub fn previous(&mut self) -> Option<&str> {
        if self.entries.is_empty() {
            return None;
        }

        let index = match self.cursor {
            Some(index) if index > 0 => index - 1,
            Some(index) => index,
            None => self.entries.len() - 1,
        };
        self.cursor = Some(index);
        self.entries.get(index).map(String::as_str)
    }

    pub fn next(&mut self) -> Option<&str> {
        let cursor = self.cursor?;
        if cursor + 1 >= self.entries.len() {
            self.cursor = None;
            return Some("");
        }

        let index = cursor + 1;
        self.cursor = Some(index);
        self.entries.get(index).map(String::as_str)
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    #[cfg(test)]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

pub fn match_command(input: &str, funds: &[Fund]) -> CommandAction {
    let raw = input.trim();
    if raw.is_empty() {
        return CommandAction::Empty;
    }

    let normalized = raw.to_ascii_lowercase();
    if let Some(query) = parse_search_command(raw, &normalized) {
        return CommandAction::Search(query);
    }
    match normalized.as_str() {
        "help" | "?" => return CommandAction::ShowHelp,
        "legend" | "data status legend" | "status legend" => return CommandAction::ShowLegend,
        "show overrides" | "overrides" => return CommandAction::ShowOverrides,
        "clear overrides" => return CommandAction::ClearOverrides,
        "clear override" | "clear row overrides" => return CommandAction::ClearSelectedOverrides,
        "clear overlays" | "clear overlay" => return CommandAction::ClearOverlays,
        "diagnostics" | "data diagnostics" | "show diagnostics" => {
            return CommandAction::Diagnostics;
        }
        "clear" => return CommandAction::Clear,
        "refresh" => return CommandAction::Refresh,
        "refresh data operations" | "refresh operations" => {
            return CommandAction::RefreshDataOperations;
        }
        "use mock data" | "use mock" | "mock mode" => return CommandAction::UseMockData,
        "use api data" | "use api" | "api mode" => return CommandAction::UseApiData,
        "test backend connection" | "test api" | "test connection" => {
            return CommandAction::TestBackendConnection;
        }
        "copy backend url" | "copy api url" => return CommandAction::CopyBackendUrl,
        "back" | "go back" => return CommandAction::Back,
        "forward" | "go forward" => return CommandAction::Forward,
        "home" | "go home" => return CommandAction::Home,
        "drilldown" | "drill down" => return CommandAction::Drilldown,
        "toggle inspector" | "inspector" => return CommandAction::ToggleInspector,
        "reset layout" | "window reset layout" => return CommandAction::ResetLayout,
        "reset table columns" | "reset columns" => return CommandAction::ResetTableColumns,
        "show all table columns" | "show all columns" => {
            return CommandAction::ShowAllTableColumns;
        }
        "clear table focus" | "clear focused cell" | "clear focused row" => {
            return CommandAction::ClearTableFocus;
        }
        "pin inspector" | "pin" => return CommandAction::PinInspector,
        "unpin inspector" | "unpin" => return CommandAction::UnpinInspector,
        "open pinned" | "open pinned inspector" => return CommandAction::OpenPinnedInspector,
        "toggle nav" | "toggle navigation" | "toggle left navigation" => {
            return CommandAction::ToggleNav;
        }
        "toggle context" | "toggle context strip" => return CommandAction::ToggleContext,
        "toggle status" | "toggle status bar" => return CommandAction::ToggleStatus,
        "open selected" | "open focused row" | "details" | "detail" => {
            return CommandAction::OpenSelected;
        }
        "open selected source" | "open selected plan item" => {
            return CommandAction::OpenSelected;
        }
        "open active" => return CommandAction::OpenActive,
        "open document" | "open selected document" => return CommandAction::OpenDocument,
        "dismiss selected alert" | "dismiss alert" => return CommandAction::DismissSelectedAlert,
        "resolve selected alert" | "resolve alert" => return CommandAction::ResolveSelectedAlert,
        "run selected job" | "run job" => return CommandAction::RunSelectedJob,
        "copy active" => return CommandAction::CopyActive,
        "copy chart" | "copy active chart" => return CommandAction::CopyChart,
        "copy chart row" | "copy selected row" | "copy selected chart row" => {
            return CommandAction::CopyChartRow;
        }
        "copy chart value" | "copy selected value" => return CommandAction::CopyChartValue,
        "copy selected series" | "copy chart series" => return CommandAction::CopySelectedSeries,
        "copy selected point" | "copy chart point" => return CommandAction::CopySelected,
        "clear chart selection" | "clear selected point" | "clear selected series" => {
            return CommandAction::ClearChartSelection;
        }
        "select next series" | "next series" | "chart next series" => {
            return CommandAction::SelectNextSeries;
        }
        "select previous series" | "previous series" | "prev series" | "chart previous series" => {
            return CommandAction::SelectPreviousSeries;
        }
        "select next point" | "next point" | "chart next point" => {
            return CommandAction::SelectNextPoint;
        }
        "select previous point" | "previous point" | "prev point" | "chart previous point" => {
            return CommandAction::SelectPreviousPoint;
        }
        "copy selected" | "copy focused cell" | "copy focused row" | "copy table cell"
        | "copy table row" | "copy" => return CommandAction::CopySelected,
        "copy onboarding command" => {
            return CommandAction::CopyStatic {
                label: "onboarding command".to_owned(),
                text: "mimer mock onboard --workspace workspace-main".to_owned(),
            };
        }
        "copy price ingestion command" => {
            return CommandAction::CopyStatic {
                label: "price ingestion command".to_owned(),
                text: "run constituent_price_ingestion".to_owned(),
            };
        }
        "copy identity resolution command" => {
            return CommandAction::CopyStatic {
                label: "identity resolution command".to_owned(),
                text: "run constituent_identity_resolution".to_owned(),
            };
        }
        "copy exposure recompute command" => {
            return CommandAction::CopyStatic {
                label: "exposure recompute command".to_owned(),
                text: "run recompute_exposure".to_owned(),
            };
        }
        "open chart subject" => return CommandAction::OpenActive,
        "source" | "sources" | "show sources" => return CommandAction::ShowSources,
        "zoom in" => return CommandAction::ZoomIn,
        "zoom out" => return CommandAction::ZoomOut,
        "zoom reset" | "reset zoom" | "zoom 100" | "zoom 100%" => {
            return CommandAction::ZoomReset;
        }
        "plot active" | "chart active" => return CommandAction::PlotActive,
        "plot selected" | "chart selected" => {
            return CommandAction::PlotSelected {
                series_kind: TimeSeriesKind::Price,
                overlay_series_kinds: Vec::new(),
            };
        }
        "plot portfolio" | "plot portfolio value" | "plot value" | "chart portfolio" => {
            return CommandAction::PlotPortfolio {
                series_kind: TimeSeriesKind::PortfolioValue,
            };
        }
        "portfolio" => return CommandAction::Navigate(Page::Portfolio),
        "etfs" | "instruments" => return CommandAction::Navigate(Page::Etfs),
        "hierarchy" | "tree" => return CommandAction::Navigate(Page::Hierarchy),
        "charts" | "chart" => return CommandAction::Navigate(Page::Charts),
        "jobs" => return CommandAction::Navigate(Page::Jobs),
        "alerts" => return CommandAction::Navigate(Page::Alerts),
        "documents" | "docs" => return CommandAction::Navigate(Page::Documents),
        "curves" => return CommandAction::Navigate(Page::Curves),
        "analytics" => return CommandAction::Navigate(Page::Analytics),
        "data operations" | "operations" | "readiness" | "market data" | "market data plan"
        | "source budgets" | "fetch logs" | "job timeline" | "running jobs" | "onboarding runs"
        | "broker imports" | "transactions" | "positions" => {
            return CommandAction::Navigate(Page::DataOperations);
        }
        "scheduler" => return CommandAction::Navigate(Page::Jobs),
        "search" | "find" => return CommandAction::Search(String::new()),
        _ => {}
    }

    if let Some(mode) = parse_chart_mode_command(&normalized) {
        return CommandAction::SetChartMode(mode);
    }
    if let Some(mode) = parse_chart_value_mode_command(&normalized) {
        return CommandAction::SetChartValueMode(mode);
    }

    if matches!(
        normalized.as_str(),
        "swap comparison" | "swap spread" | "swap a/b" | "swap chart operands"
    ) {
        return CommandAction::SwapComparison;
    }

    let query = normalized
        .strip_prefix("goto ")
        .or_else(|| normalized.strip_prefix("go "))
        .or_else(|| normalized.strip_prefix("open "))
        .unwrap_or(normalized.as_str())
        .trim();

    if let Some(page) = match_page(query) {
        return CommandAction::Navigate(page);
    }

    if let Some(rest) = normalized.strip_prefix("compare ") {
        return match_compare(raw, rest, funds);
    }

    if let Some(rest) = normalized.strip_prefix("spread ") {
        return match_spread(raw, rest, funds, false);
    }

    if let Some(rest) = normalized
        .strip_prefix("plot ")
        .or_else(|| normalized.strip_prefix("chart "))
    {
        return match_plot(raw, rest, funds);
    }

    if let Some(rest) = normalized
        .strip_prefix("overlay ")
        .or_else(|| normalized.strip_prefix("add overlay "))
    {
        return match_overlay(raw, rest, funds);
    }

    if let Some(rest) = normalized.strip_prefix("diff ") {
        return match_diff(rest, funds);
    }

    if let Some(rest) = normalized.strip_prefix("run ") {
        let job = rest.trim();
        if job.is_empty() {
            return CommandAction::NoMatch(raw.to_owned());
        }
        return CommandAction::RunJob(job.to_ascii_uppercase());
    }

    if let Some(symbol) = normalized.strip_prefix("select ") {
        if let Some((fund, listing)) = match_instrument(symbol.trim(), funds) {
            return select_action(fund, listing);
        }
        return CommandAction::NoMatch(raw.to_owned());
    }

    if let Some(symbol) = normalized.strip_prefix("add ") {
        let symbol = symbol.trim();
        if symbol.is_empty() {
            return CommandAction::NoMatch(raw.to_owned());
        }
        return CommandAction::AddInstrument(symbol.to_ascii_uppercase());
    }

    if let Some((left, right)) = normalized.split_once(" - ") {
        return match_spread_pair(raw, left, right, funds, false);
    }

    if let Some((fund, listing)) = match_instrument(query, funds) {
        return select_action(fund, listing);
    }

    CommandAction::NoMatch(raw.to_owned())
}

fn parse_chart_mode_command(normalized: &str) -> Option<ChartMode> {
    let rest = normalized
        .strip_prefix("chart mode ")
        .or_else(|| normalized.strip_prefix("mode "))?
        .trim();
    match rest {
        "single" => Some(ChartMode::Single),
        "overlay" | "overlays" => Some(ChartMode::Overlay),
        "compare" | "comparison" => Some(ChartMode::Compare),
        "spread" | "spreads" => Some(ChartMode::Spread),
        _ => None,
    }
}

fn parse_chart_value_mode_command(normalized: &str) -> Option<ChartValueMode> {
    let rest = normalized
        .strip_prefix("chart value mode ")
        .or_else(|| normalized.strip_prefix("chart mode value "))
        .or_else(|| normalized.strip_prefix("chart value "))
        .or_else(|| normalized.strip_prefix("value mode "))
        .unwrap_or(normalized)
        .trim();
    match rest {
        "raw" | "chart raw" => Some(ChartValueMode::Raw),
        "rebased" | "rebased 100" | "rebase" | "rebase chart" | "normalize chart"
        | "normalise chart" | "normalized" | "normalised" => Some(ChartValueMode::Rebased100),
        "percent" | "percent change" | "% change" | "pct" | "pct change" => {
            Some(ChartValueMode::PercentChange)
        }
        _ => None,
    }
}

fn parse_search_command(raw: &str, normalized: &str) -> Option<String> {
    if normalized == "/" {
        return Some(String::new());
    }
    for prefix in ["/ ", "search ", "find "] {
        if normalized.starts_with(prefix) {
            return Some(raw[prefix.len()..].trim().to_owned());
        }
    }
    None
}

fn match_plot(raw: &str, rest: &str, funds: &[Fund]) -> CommandAction {
    let rest = rest.trim();
    if rest.is_empty() {
        return CommandAction::NoMatch(raw.to_owned());
    }

    if matches!(rest, "selected" | "price selected") {
        return CommandAction::PlotSelected {
            series_kind: TimeSeriesKind::Price,
            overlay_series_kinds: Vec::new(),
        };
    }
    if matches!(rest, "nav selected") {
        return CommandAction::PlotSelected {
            series_kind: TimeSeriesKind::Nav,
            overlay_series_kinds: Vec::new(),
        };
    }
    if matches!(rest, "nav vs price selected" | "price vs nav selected") {
        return CommandAction::PlotSelected {
            series_kind: TimeSeriesKind::Nav,
            overlay_series_kinds: vec![TimeSeriesKind::Price],
        };
    }
    if matches!(rest, "portfolio" | "portfolio value" | "value") {
        return CommandAction::PlotPortfolio {
            series_kind: TimeSeriesKind::PortfolioValue,
        };
    }

    if let Some(rest) = rest.strip_prefix("spread ") {
        return match_spread(raw, rest, funds, true);
    }

    if let Some(action) = match_plot_comparison(raw, rest, funds) {
        return action;
    }

    let (series_kind, overlay_series_kinds, query) =
        if let Some(query) = rest.strip_prefix("nav vs price ") {
            (TimeSeriesKind::Nav, vec![TimeSeriesKind::Price], query)
        } else if let Some(query) = rest.strip_prefix("price vs nav ") {
            (TimeSeriesKind::Price, vec![TimeSeriesKind::Nav], query)
        } else if let Some(query) = rest.strip_prefix("nav ") {
            (TimeSeriesKind::Nav, Vec::new(), query)
        } else if let Some(query) = rest.strip_prefix("price ") {
            (TimeSeriesKind::Price, Vec::new(), query)
        } else if let Some(query) = rest
            .strip_prefix("dividends ")
            .or_else(|| rest.strip_prefix("distributions "))
        {
            (TimeSeriesKind::Distribution, Vec::new(), query)
        } else if let Some(query) = rest.strip_prefix("value ") {
            (TimeSeriesKind::MarketValue, Vec::new(), query)
        } else {
            (TimeSeriesKind::Price, Vec::new(), rest)
        };

    let Some((fund, listing)) = match_instrument(query.trim(), funds) else {
        return CommandAction::NoMatch(raw.to_owned());
    };

    let (subject, label) = subject_for_plot(fund, listing, series_kind);
    CommandAction::PlotSubject {
        subject,
        series_kind,
        overlay_series_kinds,
        comparison_subjects: Vec::new(),
        label,
    }
}

fn match_plot_comparison(raw: &str, rest: &str, funds: &[Fund]) -> Option<CommandAction> {
    let (left_query, right_query) = rest.split_once(" vs ")?;
    if matches!(left_query.trim(), "nav" | "price") {
        return None;
    }

    let Some((left_fund, left_listing)) = match_instrument(left_query.trim(), funds) else {
        return Some(CommandAction::NoMatch(raw.to_owned()));
    };
    let Some((right_fund, right_listing)) = match_instrument(right_query.trim(), funds) else {
        return Some(CommandAction::NoMatch(raw.to_owned()));
    };

    let (subject, left_label) = subject_for_plot(left_fund, left_listing, TimeSeriesKind::Price);
    let (comparison_subject, right_label) =
        subject_for_plot(right_fund, right_listing, TimeSeriesKind::Price);

    Some(CommandAction::PlotSubject {
        subject,
        series_kind: TimeSeriesKind::Price,
        overlay_series_kinds: Vec::new(),
        comparison_subjects: vec![comparison_subject],
        label: format!("{left_label} vs {right_label}"),
    })
}

fn match_overlay(raw: &str, rest: &str, funds: &[Fund]) -> CommandAction {
    let query = rest.trim();
    let Some((fund, listing)) = match_instrument(query, funds) else {
        return CommandAction::NoMatch(raw.to_owned());
    };
    let (subject, label) = subject_for_plot(fund, listing, TimeSeriesKind::Price);
    CommandAction::OverlaySubject {
        subject,
        series_kind: TimeSeriesKind::Price,
        label,
    }
}

fn match_compare(raw: &str, rest: &str, funds: &[Fund]) -> CommandAction {
    let symbols = rest.split_whitespace().collect::<Vec<_>>();
    let [left, right, ..] = symbols.as_slice() else {
        return CommandAction::NoMatch(raw.to_owned());
    };

    let Some((left_fund, _)) = match_instrument(left, funds) else {
        return CommandAction::NoMatch(raw.to_owned());
    };
    let Some((right_fund, _)) = match_instrument(right, funds) else {
        return CommandAction::NoMatch(raw.to_owned());
    };

    CommandAction::Compare {
        left_fund_id: left_fund.id.clone(),
        right_fund_id: right_fund.id.clone(),
    }
}

fn match_spread(raw: &str, rest: &str, funds: &[Fund], plot: bool) -> CommandAction {
    let symbols = rest.split_whitespace().collect::<Vec<_>>();
    let [left, right, ..] = symbols.as_slice() else {
        return CommandAction::NoMatch(raw.to_owned());
    };
    match_spread_pair(raw, left, right, funds, plot)
}

fn match_spread_pair(
    raw: &str,
    left: &str,
    right: &str,
    funds: &[Fund],
    plot: bool,
) -> CommandAction {
    let Some((left_fund, _)) = match_instrument(left.trim(), funds) else {
        return CommandAction::NoMatch(raw.to_owned());
    };
    let Some((right_fund, _)) = match_instrument(right.trim(), funds) else {
        return CommandAction::NoMatch(raw.to_owned());
    };

    CommandAction::Spread {
        left_fund_id: left_fund.id.clone(),
        right_fund_id: right_fund.id.clone(),
        plot,
    }
}

fn match_diff(rest: &str, funds: &[Fund]) -> CommandAction {
    let mut parts = rest.split_whitespace();
    let first = parts.next();
    let topic = parts.collect::<Vec<_>>().join(" ");

    match first.and_then(|query| match_instrument(query, funds)) {
        Some((fund, listing)) => CommandAction::Diff {
            fund_id: Some(fund.id.clone()),
            listing_id: listing.map(|listing| listing.id.clone()),
            topic: if topic.is_empty() {
                "instrument".to_owned()
            } else {
                topic
            },
        },
        None => CommandAction::Diff {
            fund_id: None,
            listing_id: None,
            topic: rest.trim().to_owned(),
        },
    }
}

fn match_page(query: &str) -> Option<Page> {
    if query.is_empty() {
        return None;
    }

    if let Some(page) = match_page_alias(query) {
        return Some(page);
    }

    Page::ALL
        .iter()
        .copied()
        .find(|page| page.label().to_ascii_lowercase().contains(query))
}

fn match_page_alias(query: &str) -> Option<Page> {
    match query {
        "portfolio" | "positions" => Some(Page::Portfolio),
        "etf" | "etfs" | "instrument" | "instruments" => Some(Page::Etfs),
        "hierarchy" | "tree" | "portfolio tree" => Some(Page::Hierarchy),
        "fund" | "fund detail" | "detail" | "details" => Some(Page::FundDetail),
        "chart" | "charts" | "plot" | "plots" => Some(Page::Charts),
        "add" | "add instrument" | "resolve" => Some(Page::AddInstrument),
        "dividend" | "dividends" | "distributions" => Some(Page::Dividends),
        "holding" | "holdings" => Some(Page::Holdings),
        "exposure" | "exposures" => Some(Page::Exposure),
        "data operations" | "operations" | "readiness" | "market data" | "market data plan"
        | "source budget" | "source budgets" | "fetch log" | "fetch logs" => {
            Some(Page::DataOperations)
        }
        "alert" | "alerts" => Some(Page::Alerts),
        "document" | "documents" | "docs" => Some(Page::Documents),
        "job" | "jobs" => Some(Page::Jobs),
        "scheduler" => Some(Page::Jobs),
        "analytics" | "analysis" => Some(Page::Analytics),
        "curve" | "curves" => Some(Page::Curves),
        "compare" => Some(Page::Compare),
        "spread" | "spreads" | "relative" => Some(Page::Spreads),
        "diff" | "diffs" | "change" | "changes" => Some(Page::Diffs),
        "search" | "find" => Some(Page::Search),
        "settings" | "prefs" => Some(Page::Settings),
        _ => None,
    }
}

fn match_instrument<'a>(
    query: &str,
    funds: &'a [Fund],
) -> Option<(&'a Fund, Option<&'a FundListing>)> {
    let query = query.to_ascii_lowercase();

    funds.iter().find_map(|fund| {
        if fund.isin.to_ascii_lowercase() == query
            || fund.name.to_ascii_lowercase().contains(&query)
        {
            return Some((fund, fund.listings.first()));
        }

        fund.listings
            .iter()
            .find(|listing| listing.ticker.to_ascii_lowercase() == query)
            .map(|listing| (fund, Some(listing)))
    })
}

fn select_action(fund: &Fund, listing: Option<&FundListing>) -> CommandAction {
    CommandAction::SelectInstrument {
        fund_id: fund.id.clone(),
        listing_id: listing.map(|listing| listing.id.clone()),
    }
}

fn subject_for_plot(
    fund: &Fund,
    listing: Option<&FundListing>,
    series_kind: TimeSeriesKind,
) -> (AnalysisSubject, String) {
    match listing.or_else(|| fund.listings.first()) {
        Some(listing) => (
            AnalysisSubject::FundListing {
                fund_id: fund.id.clone(),
                listing_id: listing.id.clone(),
            },
            format!("{} {}", listing.ticker, series_kind.as_str()),
        ),
        None => (
            AnalysisSubject::Fund(fund.id.clone()),
            format!("{} {}", fund.name, series_kind.as_str()),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::FundListing;

    fn test_funds() -> Vec<Fund> {
        vec![
            test_fund("fund-vusa", "Vanguard S&P 500 UCITS ETF", "VUSA"),
            test_fund(
                "fund-jepg",
                "JPMorgan Global Equity Premium Income ETF",
                "JEPG",
            ),
        ]
    }

    fn test_fund(id: &str, name: &str, ticker: &str) -> Fund {
        Fund {
            id: id.to_owned(),
            name: name.to_owned(),
            provider: "Provider".to_owned(),
            isin: format!("ISIN-{ticker}"),
            strategy: "Equity".to_owned(),
            domicile: "Ireland".to_owned(),
            base_currency: "USD".to_owned(),
            distribution_policy: "Distributing".to_owned(),
            ocf_ter_pct: 0.1,
            distribution_frequency: "Quarterly".to_owned(),
            replication: "Physical".to_owned(),
            status: "Active".to_owned(),
            last_refreshed: "2026-06-20".to_owned(),
            source: "seed".to_owned(),
            listings: vec![FundListing {
                id: format!("listing-{ticker}"),
                fund_id: id.to_owned(),
                ticker: ticker.to_owned(),
                exchange: "XLON".to_owned(),
                currency: "GBP".to_owned(),
                venue_name: "London Stock Exchange".to_owned(),
                currency_unit: "GBp".to_owned(),
                figi: None,
                sedol: None,
                last_price: 1.0,
                last_price_date: "2026-06-20".to_owned(),
                status: "Active".to_owned(),
                source: "seed".to_owned(),
            }],
        }
    }

    #[test]
    fn matches_page_commands() {
        assert_eq!(
            match_command("goto jobs", &[]),
            CommandAction::Navigate(Page::Jobs)
        );
        assert_eq!(
            match_command("documents", &[]),
            CommandAction::Navigate(Page::Documents)
        );
        assert_eq!(
            match_command("instruments", &[]),
            CommandAction::Navigate(Page::Etfs)
        );
        assert_eq!(
            match_command("goto charts", &[]),
            CommandAction::Navigate(Page::Charts)
        );
        assert_eq!(
            match_command("data operations", &[]),
            CommandAction::Navigate(Page::DataOperations)
        );
        assert_eq!(
            match_command("market data plan", &[]),
            CommandAction::Navigate(Page::DataOperations)
        );
        assert_eq!(
            match_command("source budgets", &[]),
            CommandAction::Navigate(Page::DataOperations)
        );
        assert_eq!(
            match_command("fetch logs", &[]),
            CommandAction::Navigate(Page::DataOperations)
        );
        assert_eq!(
            match_command("scheduler", &[]),
            CommandAction::Navigate(Page::Jobs)
        );
        assert_eq!(
            match_command("hierarchy", &[]),
            CommandAction::Navigate(Page::Hierarchy)
        );
        assert_eq!(
            match_command("open selected", &[]),
            CommandAction::OpenSelected
        );
        assert_eq!(
            match_command("search VUSA", &[]),
            CommandAction::Search("VUSA".to_owned())
        );
        assert_eq!(
            match_command("/ VUSA", &[]),
            CommandAction::Search("VUSA".to_owned())
        );
        assert_eq!(
            match_command("find VUSA", &[]),
            CommandAction::Search("VUSA".to_owned())
        );
        assert_eq!(match_command("open active", &[]), CommandAction::OpenActive);
        assert_eq!(
            match_command("open document", &[]),
            CommandAction::OpenDocument
        );
        assert_eq!(
            match_command("copy selected", &[]),
            CommandAction::CopySelected
        );
        assert_eq!(match_command("copy active", &[]), CommandAction::CopyActive);
        assert_eq!(match_command("copy chart", &[]), CommandAction::CopyChart);
        assert_eq!(
            match_command("copy price ingestion command", &[]),
            CommandAction::CopyStatic {
                label: "price ingestion command".to_owned(),
                text: "run constituent_price_ingestion".to_owned(),
            }
        );
        assert_eq!(
            match_command("copy selected point", &[]),
            CommandAction::CopySelected
        );
        assert_eq!(
            match_command("open chart subject", &[]),
            CommandAction::OpenActive
        );
        assert_eq!(match_command("source", &[]), CommandAction::ShowSources);
        assert_eq!(match_command("zoom in", &[]), CommandAction::ZoomIn);
        assert_eq!(match_command("zoom out", &[]), CommandAction::ZoomOut);
        assert_eq!(match_command("zoom reset", &[]), CommandAction::ZoomReset);
        assert_eq!(match_command("plot active", &[]), CommandAction::PlotActive);
    }

    #[test]
    fn matches_ticker_and_add_commands() {
        let funds = vec![Fund {
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
                figi: Some("BBG000000001".to_owned()),
                sedol: Some("B3XXRP0".to_owned()),
                last_price: 92.18,
                last_price_date: "2026-06-20".to_owned(),
                status: "Active".to_owned(),
                source: "seed".to_owned(),
            }],
        }];

        assert_eq!(
            match_command("VUSA", &funds),
            CommandAction::SelectInstrument {
                fund_id: "fund-vusa".to_owned(),
                listing_id: Some("listing-vusa".to_owned())
            }
        );
        assert_eq!(
            match_command("add VWRP", &funds),
            CommandAction::AddInstrument("VWRP".to_owned())
        );
    }

    #[test]
    fn matches_workstation_commands() {
        let funds = vec![
            Fund {
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
                    figi: Some("BBG000000001".to_owned()),
                    sedol: Some("B3XXRP0".to_owned()),
                    last_price: 92.18,
                    last_price_date: "2026-06-20".to_owned(),
                    status: "Active".to_owned(),
                    source: "seed".to_owned(),
                }],
            },
            Fund {
                id: "fund-isf".to_owned(),
                name: "iShares Core FTSE 100 UCITS ETF".to_owned(),
                provider: "iShares".to_owned(),
                isin: "IE0005042456".to_owned(),
                strategy: "UK equity".to_owned(),
                domicile: "Ireland".to_owned(),
                base_currency: "GBP".to_owned(),
                distribution_policy: "Distributing".to_owned(),
                ocf_ter_pct: 0.07,
                distribution_frequency: "Quarterly".to_owned(),
                replication: "Physical".to_owned(),
                status: "Active".to_owned(),
                last_refreshed: "2026-06-20".to_owned(),
                source: "seed".to_owned(),
                listings: vec![FundListing {
                    id: "listing-isf".to_owned(),
                    fund_id: "fund-isf".to_owned(),
                    ticker: "ISF".to_owned(),
                    exchange: "XLON".to_owned(),
                    currency: "GBP".to_owned(),
                    venue_name: "London Stock Exchange".to_owned(),
                    currency_unit: "GBp".to_owned(),
                    figi: Some("BBG000000002".to_owned()),
                    sedol: Some("B1FTEST".to_owned()),
                    last_price: 8.26,
                    last_price_date: "2026-06-20".to_owned(),
                    status: "Active".to_owned(),
                    source: "seed".to_owned(),
                }],
            },
        ];

        assert_eq!(
            match_command("open alerts", &funds),
            CommandAction::Navigate(Page::Alerts)
        );
        assert_eq!(
            match_command("compare VUSA ISF", &funds),
            CommandAction::Compare {
                left_fund_id: "fund-vusa".to_owned(),
                right_fund_id: "fund-isf".to_owned(),
            }
        );
        assert_eq!(
            match_command("diff VUSA holdings", &funds),
            CommandAction::Diff {
                fund_id: Some("fund-vusa".to_owned()),
                listing_id: Some("listing-vusa".to_owned()),
                topic: "holdings".to_owned(),
            }
        );
        assert_eq!(
            match_command("run daily_price_ingestion", &funds),
            CommandAction::RunJob("DAILY_PRICE_INGESTION".to_owned())
        );
        assert_eq!(match_command("legend", &funds), CommandAction::ShowLegend);
        assert_eq!(match_command("refresh", &funds), CommandAction::Refresh);
        assert_eq!(
            match_command("refresh data operations", &funds),
            CommandAction::RefreshDataOperations
        );
        assert_eq!(
            match_command("use mock data", &funds),
            CommandAction::UseMockData
        );
        assert_eq!(
            match_command("use api data", &funds),
            CommandAction::UseApiData
        );
        assert_eq!(
            match_command("test backend connection", &funds),
            CommandAction::TestBackendConnection
        );
        assert_eq!(
            match_command("copy backend url", &funds),
            CommandAction::CopyBackendUrl
        );
        assert_eq!(
            match_command("toggle inspector", &funds),
            CommandAction::ToggleInspector
        );
        assert_eq!(
            match_command("reset layout", &funds),
            CommandAction::ResetLayout
        );
        assert_eq!(
            match_command("reset table columns", &funds),
            CommandAction::ResetTableColumns
        );
        assert_eq!(
            match_command("show all columns", &funds),
            CommandAction::ShowAllTableColumns
        );
        assert_eq!(
            match_command("clear table focus", &funds),
            CommandAction::ClearTableFocus
        );
        assert_eq!(
            match_command("copy focused cell", &funds),
            CommandAction::CopySelected
        );
        assert_eq!(
            match_command("open focused row", &funds),
            CommandAction::OpenSelected
        );
        assert_eq!(
            match_command("pin inspector", &funds),
            CommandAction::PinInspector
        );
        assert_eq!(
            match_command("unpin inspector", &funds),
            CommandAction::UnpinInspector
        );
        assert_eq!(
            match_command("open pinned", &funds),
            CommandAction::OpenPinnedInspector
        );
        assert_eq!(
            match_command("dismiss selected alert", &funds),
            CommandAction::DismissSelectedAlert
        );
        assert_eq!(
            match_command("resolve selected alert", &funds),
            CommandAction::ResolveSelectedAlert
        );
        assert_eq!(
            match_command("run selected job", &funds),
            CommandAction::RunSelectedJob
        );
        assert_eq!(match_command("back", &funds), CommandAction::Back);
        assert_eq!(match_command("forward", &funds), CommandAction::Forward);
        assert_eq!(match_command("home", &funds), CommandAction::Home);
        assert_eq!(
            match_command("show overrides", &funds),
            CommandAction::ShowOverrides
        );
        assert_eq!(
            match_command("clear override", &funds),
            CommandAction::ClearSelectedOverrides
        );
        assert_eq!(
            match_command("clear overrides", &funds),
            CommandAction::ClearOverrides
        );
        assert_eq!(
            match_command("clear overlays", &funds),
            CommandAction::ClearOverlays
        );
        assert_eq!(
            match_command("chart mode compare", &funds),
            CommandAction::SetChartMode(ChartMode::Compare)
        );
        assert_eq!(
            match_command("chart mode spread", &funds),
            CommandAction::SetChartMode(ChartMode::Spread)
        );
        assert_eq!(
            match_command("chart value raw", &funds),
            CommandAction::SetChartValueMode(ChartValueMode::Raw)
        );
        assert_eq!(
            match_command("chart value mode raw", &funds),
            CommandAction::SetChartValueMode(ChartValueMode::Raw)
        );
        assert_eq!(
            match_command("chart value rebased 100", &funds),
            CommandAction::SetChartValueMode(ChartValueMode::Rebased100)
        );
        assert_eq!(
            match_command("normalize chart", &funds),
            CommandAction::SetChartValueMode(ChartValueMode::Rebased100)
        );
        assert_eq!(
            match_command("chart value percent", &funds),
            CommandAction::SetChartValueMode(ChartValueMode::PercentChange)
        );
        assert_eq!(
            match_command("copy chart row", &funds),
            CommandAction::CopyChartRow
        );
        assert_eq!(
            match_command("copy chart value", &funds),
            CommandAction::CopyChartValue
        );
        assert_eq!(
            match_command("copy selected series", &funds),
            CommandAction::CopySelectedSeries
        );
        assert_eq!(
            match_command("clear chart selection", &funds),
            CommandAction::ClearChartSelection
        );
        assert_eq!(
            match_command("select next series", &funds),
            CommandAction::SelectNextSeries
        );
        assert_eq!(
            match_command("select previous series", &funds),
            CommandAction::SelectPreviousSeries
        );
        assert_eq!(
            match_command("select next point", &funds),
            CommandAction::SelectNextPoint
        );
        assert_eq!(
            match_command("select previous point", &funds),
            CommandAction::SelectPreviousPoint
        );
        assert_eq!(
            match_command("swap comparison", &funds),
            CommandAction::SwapComparison
        );
        assert_eq!(
            match_command("diagnostics", &funds),
            CommandAction::Diagnostics
        );
    }

    #[test]
    fn matches_spread_commands() {
        let funds = test_funds();

        assert_eq!(
            match_command("spread VUSA JEPG", &funds),
            CommandAction::Spread {
                left_fund_id: "fund-vusa".to_owned(),
                right_fund_id: "fund-jepg".to_owned(),
                plot: false,
            }
        );
        assert_eq!(
            match_command("VUSA - JEPG", &funds),
            CommandAction::Spread {
                left_fund_id: "fund-vusa".to_owned(),
                right_fund_id: "fund-jepg".to_owned(),
                plot: false,
            }
        );
        assert_eq!(
            match_command("plot spread VUSA JEPG", &funds),
            CommandAction::Spread {
                left_fund_id: "fund-vusa".to_owned(),
                right_fund_id: "fund-jepg".to_owned(),
                plot: true,
            }
        );
    }

    #[test]
    fn matches_plot_commands() {
        let funds = vec![
            Fund {
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
            },
            Fund {
                id: "fund-isf".to_owned(),
                name: "iShares Core FTSE 100 UCITS ETF".to_owned(),
                provider: "iShares".to_owned(),
                isin: "IE0005042456".to_owned(),
                strategy: "UK equity".to_owned(),
                domicile: "Ireland".to_owned(),
                base_currency: "GBP".to_owned(),
                distribution_policy: "Distributing".to_owned(),
                ocf_ter_pct: 0.07,
                distribution_frequency: "Quarterly".to_owned(),
                replication: "Physical".to_owned(),
                status: "Active".to_owned(),
                last_refreshed: "2026-06-20".to_owned(),
                source: "seed".to_owned(),
                listings: vec![FundListing {
                    id: "listing-isf".to_owned(),
                    fund_id: "fund-isf".to_owned(),
                    ticker: "ISF".to_owned(),
                    exchange: "XLON".to_owned(),
                    currency: "GBP".to_owned(),
                    venue_name: "London Stock Exchange".to_owned(),
                    currency_unit: "GBp".to_owned(),
                    figi: None,
                    sedol: None,
                    last_price: 8.26,
                    last_price_date: "2026-06-20".to_owned(),
                    status: "Active".to_owned(),
                    source: "seed".to_owned(),
                }],
            },
        ];

        assert_eq!(
            match_command("plot VUSA", &funds),
            CommandAction::PlotSubject {
                subject: AnalysisSubject::FundListing {
                    fund_id: "fund-vusa".to_owned(),
                    listing_id: "listing-vusa".to_owned()
                },
                series_kind: TimeSeriesKind::Price,
                overlay_series_kinds: Vec::new(),
                comparison_subjects: Vec::new(),
                label: "VUSA Price".to_owned()
            }
        );
        assert_eq!(
            match_command("plot nav vs price VUSA", &funds),
            CommandAction::PlotSubject {
                subject: AnalysisSubject::FundListing {
                    fund_id: "fund-vusa".to_owned(),
                    listing_id: "listing-vusa".to_owned()
                },
                series_kind: TimeSeriesKind::Nav,
                overlay_series_kinds: vec![TimeSeriesKind::Price],
                comparison_subjects: Vec::new(),
                label: "VUSA NAV".to_owned()
            }
        );
        assert_eq!(
            match_command("plot VUSA vs ISF", &funds),
            CommandAction::PlotSubject {
                subject: AnalysisSubject::FundListing {
                    fund_id: "fund-vusa".to_owned(),
                    listing_id: "listing-vusa".to_owned()
                },
                series_kind: TimeSeriesKind::Price,
                overlay_series_kinds: Vec::new(),
                comparison_subjects: vec![AnalysisSubject::FundListing {
                    fund_id: "fund-isf".to_owned(),
                    listing_id: "listing-isf".to_owned()
                }],
                label: "VUSA Price vs ISF Price".to_owned()
            }
        );
        assert_eq!(
            match_command("overlay ISF", &funds),
            CommandAction::OverlaySubject {
                subject: AnalysisSubject::FundListing {
                    fund_id: "fund-isf".to_owned(),
                    listing_id: "listing-isf".to_owned()
                },
                series_kind: TimeSeriesKind::Price,
                label: "ISF Price".to_owned()
            }
        );
        assert_eq!(
            match_command("add overlay ISF", &funds),
            CommandAction::OverlaySubject {
                subject: AnalysisSubject::FundListing {
                    fund_id: "fund-isf".to_owned(),
                    listing_id: "listing-isf".to_owned()
                },
                series_kind: TimeSeriesKind::Price,
                label: "ISF Price".to_owned()
            }
        );
        assert_eq!(
            match_command("plot portfolio", &funds),
            CommandAction::PlotPortfolio {
                series_kind: TimeSeriesKind::PortfolioValue
            }
        );
    }

    #[test]
    fn command_history_recalls_commands_without_duplicate_tails() {
        let mut history = CommandHistory::default();
        assert!(history.is_empty());

        history.push(" VUSA ");
        history.push("goto jobs");
        history.push("goto jobs");

        assert_eq!(history.len(), 2);
        assert_eq!(history.previous(), Some("goto jobs"));
        assert_eq!(history.previous(), Some("VUSA"));
        assert_eq!(history.previous(), Some("VUSA"));
        assert_eq!(history.next(), Some("goto jobs"));
        assert_eq!(history.next(), Some(""));
    }
}
