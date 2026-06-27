use crate::app_model::DashboardSnapshot;
use crate::charts::PlotRequest;
use crate::debounce::{DebouncedText, DebouncedValue};
use crate::domain::AnalysisSubject;
use crate::filter::any_contains_ci;
use crate::pages::{Page, format_source, header_cell, page_heading};
use crate::table_state::{TableId, TableState};
use crate::timeseries::TimeSeriesKind;
use crate::ui::metrics;
use eframe::egui;
use egui_extras::{Column, TableBuilder};
use std::cmp::Ordering;
use std::time::{Duration, Instant};

const SEARCH_DEBOUNCE: Duration = Duration::from_millis(250);

#[derive(Clone, Debug)]
pub struct SearchState {
    pub query_text: String,
    pub debounced_query: DebouncedText,
    pub result_type: SearchResultFilter,
    pub source_filter: SearchSourceFilter,
    pub table: TableState,
    pub focus_query_next_frame: bool,
}

impl Default for SearchState {
    fn default() -> Self {
        Self {
            query_text: String::new(),
            debounced_query: DebouncedValue::new(String::new(), SEARCH_DEBOUNCE),
            result_type: SearchResultFilter::All,
            source_filter: SearchSourceFilter::All,
            table: TableState::new(TableId::SearchResults),
            focus_query_next_frame: false,
        }
    }
}

impl SearchState {
    pub fn set_query_now(&mut self, query: impl Into<String>) {
        let query = query.into();
        self.query_text = query.clone();
        self.debounced_query.set_committed(query);
        self.table.clear_selection();
        self.focus_query_next_frame = true;
    }

    pub fn selected_result(&self, snapshot: &DashboardSnapshot) -> Option<SearchResult> {
        let results = mock_search(
            snapshot,
            self.debounced_query.committed(),
            self.result_type,
            &self.source_filter,
        );
        self.table
            .selected_index()
            .and_then(|index| results.get(index).cloned())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SearchResultFilter {
    All,
    Funds,
    Listings,
    Holdings,
    Documents,
    Jobs,
    Portfolios,
}

impl SearchResultFilter {
    const ALL: [Self; 7] = [
        Self::All,
        Self::Funds,
        Self::Listings,
        Self::Holdings,
        Self::Documents,
        Self::Jobs,
        Self::Portfolios,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::All => "All",
            Self::Funds => "Funds",
            Self::Listings => "Listings",
            Self::Holdings => "Holdings",
            Self::Documents => "Documents",
            Self::Jobs => "Jobs",
            Self::Portfolios => "Portfolios",
        }
    }

    fn accepts(self, result_type: SearchResultType) -> bool {
        match self {
            Self::All => true,
            Self::Funds => result_type == SearchResultType::Fund,
            Self::Listings => result_type == SearchResultType::Listing,
            Self::Holdings => result_type == SearchResultType::Holding,
            Self::Documents => result_type == SearchResultType::Document,
            Self::Jobs => result_type == SearchResultType::Job,
            Self::Portfolios => result_type == SearchResultType::Portfolio,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SearchSourceFilter {
    All,
    Canonical,
    Specific(String),
}

impl SearchSourceFilter {
    fn label(&self) -> String {
        match self {
            Self::All => "All".to_owned(),
            Self::Canonical => "Canonical".to_owned(),
            Self::Specific(source) => source.clone(),
        }
    }

    fn accepts(&self, source: &str) -> bool {
        match self {
            Self::All => true,
            Self::Canonical => source == "seed",
            Self::Specific(expected) => source.eq_ignore_ascii_case(expected),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SearchResultType {
    Fund,
    Listing,
    Holding,
    Document,
    Job,
    Portfolio,
}

impl SearchResultType {
    pub fn label(self) -> &'static str {
        match self {
            Self::Fund => "Fund",
            Self::Listing => "Listing",
            Self::Holding => "Holding",
            Self::Document => "Document",
            Self::Job => "Job",
            Self::Portfolio => "Portfolio",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SearchResult {
    pub result_type: SearchResultType,
    pub label: String,
    pub symbol: Option<String>,
    pub identifier: Option<String>,
    pub description: String,
    pub subject: AnalysisSubject,
    pub source: String,
    pub status: String,
    pub score: i32,
    pub target_page: Page,
    pub row_index: Option<usize>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum SearchAction {
    Open(SearchResult),
    MakeActive(SearchResult),
    Plot(PlotRequest),
    Copy { label: String, text: String },
    ShowSource(String),
}

pub fn render(
    ui: &mut egui::Ui,
    snapshot: &DashboardSnapshot,
    state: &mut SearchState,
) -> Option<SearchAction> {
    let now = Instant::now();
    if state.debounced_query.commit_if_due(now) {
        state.table.clear_selection();
    }
    if let Some(delay) = state.debounced_query.remaining_delay(now) {
        ui.ctx().request_repaint_after(delay);
    }

    let mut open_top_result = false;
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Search");
            ui.horizontal_wrapped(|ui| {
                ui.label("Query");
                let query_width = metrics::fit_width(
                    ui.available_width(),
                    ui.available_width() * 0.70,
                    180.0,
                    760.0,
                );
                let response = ui.add_sized(
                    [query_width, metrics::ROW_HEIGHT_COMPACT],
                    egui::TextEdit::singleline(&mut state.query_text)
                        .hint_text("VUSA / ISIN / provider / document / job"),
                );
                if state.focus_query_next_frame {
                    response.request_focus();
                    state.focus_query_next_frame = false;
                }
                if response.changed() {
                    state
                        .debounced_query
                        .set_pending(state.query_text.clone(), now);
                }
                if response.has_focus() && ui.input(|input| input.key_pressed(egui::Key::Enter)) {
                    state
                        .debounced_query
                        .set_pending(state.query_text.clone(), now);
                    state.debounced_query.commit_now();
                    state.table.clear_selection();
                    open_top_result = true;
                }
                if response.has_focus()
                    && ui.input(|input| input.key_pressed(egui::Key::Escape))
                    && state.debounced_query.cancel()
                {
                    state.query_text = state.debounced_query.committed().clone();
                }
                if state.debounced_query.has_pending() {
                    ui.monospace("PENDING");
                } else {
                    ui.monospace("APPLIED");
                }
            });

            ui.add_space(metrics::SPACE_1);
            ui.horizontal_wrapped(|ui| {
                ui.label("Type");
                egui::ComboBox::from_id_salt("search_type_filter")
                    .selected_text(state.result_type.label())
                    .show_ui(ui, |ui| {
                        for result_type in SearchResultFilter::ALL {
                            ui.selectable_value(
                                &mut state.result_type,
                                result_type,
                                result_type.label(),
                            );
                        }
                    });
                ui.label("Source");
                egui::ComboBox::from_id_salt("search_source_filter")
                    .selected_text(state.source_filter.label())
                    .show_ui(ui, |ui| {
                        ui.selectable_value(
                            &mut state.source_filter,
                            SearchSourceFilter::All,
                            "All",
                        );
                        ui.selectable_value(
                            &mut state.source_filter,
                            SearchSourceFilter::Canonical,
                            "Canonical",
                        );
                        for source in ["seed", "mock", "stooq", "issuer", "derived"] {
                            ui.selectable_value(
                                &mut state.source_filter,
                                SearchSourceFilter::Specific(source.to_owned()),
                                source,
                            );
                        }
                    });
                if ui.button("Clear").clicked() {
                    state.set_query_now("");
                }
                ui.label(egui::RichText::new("Ctrl/Cmd+F focuses this workspace").weak());
            });

            ui.add_space(metrics::SPACE_2);
            let results = mock_search(
                snapshot,
                state.debounced_query.committed(),
                state.result_type,
                &state.source_filter,
            );
            if let Some(index) = state.table.selected_index()
                && index >= results.len()
            {
                state.table.clear_selection();
            }

            if open_top_result && let Some(result) = results.first() {
                action = Some(SearchAction::Open(result.clone()));
            }

            if action.is_none() {
                action = results_table(ui, &results, state);
            }
            ui.add_space(metrics::SPACE_2);
            result_inspector(ui, &results, state);
        });
    action
}

pub fn mock_search(
    snapshot: &DashboardSnapshot,
    query: &str,
    result_filter: SearchResultFilter,
    source_filter: &SearchSourceFilter,
) -> Vec<SearchResult> {
    let query = query.trim();
    if query.is_empty() {
        return Vec::new();
    }

    let mut results = Vec::new();

    for workspace in &snapshot.workspaces {
        push_if_match(
            &mut results,
            query,
            result_filter,
            source_filter,
            SearchResult {
                result_type: SearchResultType::Portfolio,
                label: workspace.name.clone(),
                symbol: Some(workspace.id.clone()),
                identifier: Some(workspace.base_currency.clone()),
                description: format!("Workspace portfolio in {}", workspace.base_currency),
                subject: AnalysisSubject::WorkspacePortfolio(workspace.id.clone()),
                source: "seed".to_owned(),
                status: "MOCK".to_owned(),
                score: 0,
                target_page: Page::Portfolio,
                row_index: None,
            },
            &[&workspace.id, &workspace.name, &workspace.base_currency],
        );
    }

    for fund in &snapshot.funds {
        let tickers = fund
            .listings
            .iter()
            .map(|listing| listing.ticker.as_str())
            .collect::<Vec<_>>()
            .join("/");
        let ticker_fields = fund
            .listings
            .iter()
            .flat_map(|listing| {
                [
                    listing.ticker.as_str(),
                    listing.exchange.as_str(),
                    listing.currency.as_str(),
                    listing.figi.as_deref().unwrap_or(""),
                    listing.sedol.as_deref().unwrap_or(""),
                ]
            })
            .collect::<Vec<_>>();
        let mut fields = vec![
            fund.id.as_str(),
            fund.name.as_str(),
            fund.provider.as_str(),
            fund.isin.as_str(),
            fund.strategy.as_str(),
            fund.status.as_str(),
            fund.source.as_str(),
            tickers.as_str(),
        ];
        fields.extend(ticker_fields);
        push_if_match(
            &mut results,
            query,
            result_filter,
            source_filter,
            SearchResult {
                result_type: SearchResultType::Fund,
                label: fund.name.clone(),
                symbol: Some(tickers.clone()),
                identifier: Some(fund.isin.clone()),
                description: format!(
                    "{} | {} | {}",
                    fund.provider, fund.strategy, fund.base_currency
                ),
                subject: AnalysisSubject::Fund(fund.id.clone()),
                source: fund.source.clone(),
                status: fund.status.clone(),
                score: 0,
                target_page: Page::FundDetail,
                row_index: None,
            },
            &fields,
        );

        for listing in &fund.listings {
            let fields = [
                listing.id.as_str(),
                listing.ticker.as_str(),
                listing.exchange.as_str(),
                listing.currency.as_str(),
                listing.venue_name.as_str(),
                listing.figi.as_deref().unwrap_or(""),
                listing.sedol.as_deref().unwrap_or(""),
                fund.name.as_str(),
                fund.isin.as_str(),
                listing.source.as_str(),
            ];
            push_if_match(
                &mut results,
                query,
                result_filter,
                source_filter,
                SearchResult {
                    result_type: SearchResultType::Listing,
                    label: fund.name.clone(),
                    symbol: Some(listing.ticker.clone()),
                    identifier: Some(
                        listing
                            .figi
                            .clone()
                            .or_else(|| listing.sedol.clone())
                            .unwrap_or_else(|| listing.id.clone()),
                    ),
                    description: format!(
                        "{} | {} | {}",
                        listing.exchange, listing.currency, fund.isin
                    ),
                    subject: AnalysisSubject::FundListing {
                        fund_id: fund.id.clone(),
                        listing_id: listing.id.clone(),
                    },
                    source: listing.source.clone(),
                    status: listing.status.clone(),
                    score: 0,
                    target_page: Page::FundDetail,
                    row_index: None,
                },
                &fields,
            );
        }
    }

    for (index, holding) in snapshot.holdings.iter().enumerate() {
        let fields = [
            holding.company.as_str(),
            holding.ticker.as_str(),
            holding.country.as_str(),
            holding.sector.as_str(),
            holding.source_etf.as_str(),
            holding.source.as_str(),
        ];
        push_if_match(
            &mut results,
            query,
            result_filter,
            source_filter,
            SearchResult {
                result_type: SearchResultType::Holding,
                label: holding.company.clone(),
                symbol: Some(holding.ticker.clone()),
                identifier: Some(holding.source_etf.clone()),
                description: format!(
                    "{} | {} | weight {:.2}%",
                    holding.country, holding.sector, holding.weight_pct
                ),
                subject: AnalysisSubject::Holding {
                    ticker: holding.ticker.clone(),
                    source: holding.source.clone(),
                },
                source: holding.source.clone(),
                status: holding.as_of_date.clone(),
                score: 0,
                target_page: Page::Holdings,
                row_index: Some(index),
            },
            &fields,
        );
    }

    for (index, document) in snapshot.documents.iter().enumerate() {
        let fields = [
            document.fund_id.as_str(),
            document.ticker.as_str(),
            document.document_type.as_str(),
            document.latest_date.as_str(),
            document.status.as_str(),
            document.content_hash_change.as_str(),
            document.source.as_str(),
        ];
        push_if_match(
            &mut results,
            query,
            result_filter,
            source_filter,
            SearchResult {
                result_type: SearchResultType::Document,
                label: format!("{} {}", document.ticker, document.document_type),
                symbol: Some(document.ticker.clone()),
                identifier: Some(document.content_hash_change.clone()),
                description: format!(
                    "{} | checked {}",
                    document.latest_date, document.last_checked
                ),
                subject: AnalysisSubject::Fund(document.fund_id.clone()),
                source: document.source.clone(),
                status: document.status.clone(),
                score: 0,
                target_page: Page::DocumentViewer,
                row_index: Some(index),
            },
            &fields,
        );
    }

    for (index, job) in snapshot.scheduled_jobs.iter().enumerate() {
        let fields = [
            job.name.as_str(),
            job.job_type.as_str(),
            job.cron_schedule.as_str(),
            job.last_run.as_str(),
            job.next_run.as_str(),
            job.source.as_str(),
        ];
        push_if_match(
            &mut results,
            query,
            result_filter,
            source_filter,
            SearchResult {
                result_type: SearchResultType::Job,
                label: job.name.clone(),
                symbol: Some(job.job_type.clone()),
                identifier: Some(job.cron_schedule.clone()),
                description: format!("Last {} | next {}", job.last_run, job.next_run),
                subject: AnalysisSubject::SyntheticModel(format!("job:{}", job.name)),
                source: job.source.clone(),
                status: if job.active { "ACTIVE" } else { "INACTIVE" }.to_owned(),
                score: 0,
                target_page: Page::Jobs,
                row_index: Some(index),
            },
            &fields,
        );
    }

    for (index, run) in snapshot.job_runs.iter().enumerate() {
        let fields = [
            run.id.as_str(),
            run.job_type.as_str(),
            run.status.as_str(),
            run.started.as_str(),
            run.finished.as_deref().unwrap_or(""),
            run.message.as_str(),
            run.source.as_str(),
        ];
        push_if_match(
            &mut results,
            query,
            result_filter,
            source_filter,
            SearchResult {
                result_type: SearchResultType::Job,
                label: run.id.clone(),
                symbol: Some(run.job_type.clone()),
                identifier: Some(run.started.clone()),
                description: run.message.clone(),
                subject: AnalysisSubject::SyntheticModel(format!("job-run:{}", run.id)),
                source: run.source.clone(),
                status: run.status.as_str().to_owned(),
                score: 0,
                target_page: Page::Jobs,
                row_index: Some(index),
            },
            &fields,
        );
    }

    results.sort_by(sort_results);
    results
}

fn results_table(
    ui: &mut egui::Ui,
    results: &[SearchResult],
    state: &mut SearchState,
) -> Option<SearchAction> {
    let mut action = None;

    if !ui.ctx().text_edit_focused() {
        handle_result_keyboard(ui.ctx(), results, state, &mut action);
    }

    if results.is_empty() {
        ui.group(|ui| {
            ui.set_min_height(160.0);
            ui.label("No local matches. Try a ticker, ISIN, provider, document type, or job name.");
        });
        return action;
    }

    let table_height = (ui.available_height() - metrics::SPACE_6).clamp(260.0, 720.0);
    let compact = ui.available_width() < 760.0;
    let mut table = TableBuilder::new(ui)
        .id_salt("search_results_table")
        .striped(true)
        .resizable(true)
        .auto_shrink(false)
        .max_scroll_height(table_height);

    if compact {
        table = table
            .column(Column::initial(92.0).at_least(76.0))
            .column(Column::initial(116.0).at_least(84.0).clip(true))
            .column(Column::remainder().at_least(220.0).clip(true))
            .column(Column::initial(110.0).at_least(86.0))
            .column(Column::initial(104.0).at_least(80.0))
            .column(Column::initial(96.0).at_least(82.0));
    } else {
        table = table
            .column(Column::initial(92.0).at_least(76.0))
            .column(Column::initial(116.0).at_least(84.0).clip(true))
            .column(Column::remainder().at_least(220.0).clip(true))
            .column(Column::initial(178.0).at_least(120.0).clip(true))
            .column(Column::initial(110.0).at_least(86.0))
            .column(Column::initial(104.0).at_least(80.0))
            .column(Column::initial(96.0).at_least(82.0));
    }

    table
        .header(18.0, |mut header| {
            header.col(|ui| header_cell(ui, "Type"));
            header.col(|ui| header_cell(ui, "Symbol"));
            header.col(|ui| header_cell(ui, "Name"));
            if !compact {
                header.col(|ui| header_cell(ui, "Identifier"));
            }
            header.col(|ui| header_cell(ui, "Source"));
            header.col(|ui| header_cell(ui, "Status"));
            header.col(|ui| header_cell(ui, "Action"));
        })
        .body(|mut body| {
            for (index, result) in results.iter().enumerate() {
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.col(|ui| {
                        ui.monospace(result.result_type.label());
                    });
                    row.col(|ui| {
                        let symbol = result.symbol.as_deref().unwrap_or("-");
                        let response = ui
                            .selectable_label(state.table.selection.is_selected(index), symbol)
                            .on_hover_text(result.description.as_str())
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        handle_result_response(&response, index, result, state, &mut action);
                    });
                    row.col(|ui| {
                        let response = ui
                            .selectable_label(
                                state.table.selection.is_selected(index),
                                &result.label,
                            )
                            .on_hover_text(result.description.as_str())
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        handle_result_response(&response, index, result, state, &mut action);
                    });
                    if !compact {
                        row.col(|ui| {
                            ui.monospace(result.identifier.as_deref().unwrap_or("-"))
                                .on_hover_text(result.identifier.as_deref().unwrap_or("-"));
                        });
                    }
                    row.col(|ui| {
                        ui.monospace(format_source(&result.source))
                            .on_hover_text(format!("Source: {}", result.source));
                    });
                    row.col(|ui| {
                        ui.monospace(&result.status)
                            .on_hover_text(format!("Status: {}", result.status));
                    });
                    row.col(|ui| {
                        if ui.small_button("Open").clicked() {
                            state.table.select(index);
                            action = Some(SearchAction::Open(result.clone()));
                        }
                    });
                });
            }
        });

    action
}

fn result_inspector(ui: &mut egui::Ui, results: &[SearchResult], state: &SearchState) {
    ui.label(egui::RichText::new("Preview").weak());
    ui.separator();
    let Some(result) = state
        .table
        .selected_index()
        .and_then(|index| results.get(index))
    else {
        ui.label("Select a result to inspect its source, status, and target action.");
        return;
    };

    egui::Grid::new("search_result_preview_grid")
        .num_columns(2)
        .min_col_width(110.0)
        .striped(true)
        .show(ui, |ui| {
            for (field, value) in [
                ("Type", result.result_type.label().to_owned()),
                ("Label", result.label.clone()),
                (
                    "Symbol",
                    result.symbol.clone().unwrap_or_else(|| "-".to_owned()),
                ),
                (
                    "Identifier",
                    result.identifier.clone().unwrap_or_else(|| "-".to_owned()),
                ),
                ("Source", format_source(&result.source)),
                ("Status", result.status.clone()),
                ("Score", result.score.to_string()),
                ("Target", result.target_page.label().to_owned()),
            ] {
                ui.label(field);
                ui.monospace(value);
                ui.end_row();
            }
        });
    ui.add_space(metrics::SPACE_2);
    ui.label(&result.description);
}

fn handle_result_response(
    response: &egui::Response,
    index: usize,
    result: &SearchResult,
    state: &mut SearchState,
    action: &mut Option<SearchAction>,
) {
    if response.clicked() {
        state.table.select(index);
    }
    if response.double_clicked() {
        state.table.select(index);
        *action = Some(SearchAction::Open(result.clone()));
    }
    response.context_menu(|ui| {
        if ui.button("Open").clicked() {
            state.table.select(index);
            *action = Some(SearchAction::Open(result.clone()));
            ui.close();
        }
        if ui.button("Make Active").clicked() {
            state.table.select(index);
            *action = Some(SearchAction::MakeActive(result.clone()));
            ui.close();
        }
        if ui
            .add_enabled(can_plot(result), egui::Button::new("Plot"))
            .clicked()
        {
            state.table.select(index);
            *action = plot_for_result(result).map(SearchAction::Plot);
            ui.close();
        }
        if let Some(symbol) = result.symbol.as_ref()
            && ui.button("Copy Symbol").clicked()
        {
            *action = Some(SearchAction::Copy {
                label: format!("symbol {symbol}"),
                text: symbol.clone(),
            });
            ui.close();
        }
        if let Some(identifier) = result.identifier.as_ref()
            && ui.button("Copy Identifier").clicked()
        {
            *action = Some(SearchAction::Copy {
                label: format!("identifier {identifier}"),
                text: identifier.clone(),
            });
            ui.close();
        }
        if ui.button("Show Source").clicked() {
            *action = Some(SearchAction::ShowSource(format!(
                "{} | {} | {}",
                result.result_type.label(),
                format_source(&result.source),
                result.status
            )));
            ui.close();
        }
    });
}

fn handle_result_keyboard(
    ctx: &egui::Context,
    results: &[SearchResult],
    state: &mut SearchState,
    action: &mut Option<SearchAction>,
) {
    let visible = (0..results.len()).collect::<Vec<_>>();
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        state.table.selection.move_by(&visible, -1);
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        state.table.selection.move_by(&visible, 1);
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        if state.table.selected_index().is_none() {
            state.table.selection.move_by(&visible, 1);
        }
        if let Some(result) = state
            .table
            .selected_index()
            .and_then(|index| results.get(index))
        {
            *action = Some(SearchAction::Open(result.clone()));
        }
    }
}

fn push_if_match(
    results: &mut Vec<SearchResult>,
    query: &str,
    result_filter: SearchResultFilter,
    source_filter: &SearchSourceFilter,
    mut result: SearchResult,
    fields: &[&str],
) {
    if !result_filter.accepts(result.result_type) || !source_filter.accepts(&result.source) {
        return;
    }
    let Some(score) = score_match(query, fields) else {
        return;
    };
    result.score = score;
    results.push(result);
}

fn score_match(query: &str, fields: &[&str]) -> Option<i32> {
    let normalized = query.trim().to_ascii_lowercase();
    if normalized.is_empty() {
        return None;
    }

    let mut best = None;
    for field in fields {
        let field = field.trim();
        if field.is_empty() {
            continue;
        }
        let field_lower = field.to_ascii_lowercase();
        let score = if field_lower == normalized {
            100
        } else if field_lower.starts_with(&normalized) {
            80
        } else if field_lower.contains(&normalized) {
            55
        } else if any_contains_ci(field_lower.split_whitespace(), &normalized) {
            35
        } else {
            continue;
        };
        best = Some(best.map_or(score, |current: i32| current.max(score)));
    }
    best
}

fn sort_results(left: &SearchResult, right: &SearchResult) -> Ordering {
    right
        .score
        .cmp(&left.score)
        .then_with(|| left.result_type.label().cmp(right.result_type.label()))
        .then_with(|| left.label.cmp(&right.label))
}

fn can_plot(result: &SearchResult) -> bool {
    matches!(
        result.result_type,
        SearchResultType::Fund | SearchResultType::Listing | SearchResultType::Portfolio
    )
}

fn plot_for_result(result: &SearchResult) -> Option<PlotRequest> {
    let series_kind = match result.subject {
        AnalysisSubject::WorkspacePortfolio(_) => TimeSeriesKind::PortfolioValue,
        AnalysisSubject::Fund(_)
        | AnalysisSubject::FundListing { .. }
        | AnalysisSubject::Holding { .. }
        | AnalysisSubject::Cash(_)
        | AnalysisSubject::SyntheticModel(_) => TimeSeriesKind::Price,
    };
    can_plot(result).then(|| {
        PlotRequest::new(
            result.subject.clone(),
            series_kind,
            format!(
                "{} {}",
                result.symbol.as_deref().unwrap_or(&result.label),
                series_kind.as_str()
            ),
        )
    })
}

pub fn copy_text_for_result(result: &SearchResult) -> String {
    [
        result.result_type.label().to_owned(),
        result.symbol.clone().unwrap_or_else(|| "-".to_owned()),
        result.label.clone(),
        result.identifier.clone().unwrap_or_else(|| "-".to_owned()),
        format_source(&result.source),
        result.status.clone(),
    ]
    .join("\t")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::types::DataMode;
    use crate::app_model::{RefreshStatus, SelectedInstrument};
    use crate::domain::{
        DataOperationsSnapshot, DocumentSnapshot, ExposureBreakdown, Fund, FundListing,
        HoldingExposure, InvestableKind, InvestableNode, JobRun, JobStatus, PortfolioSummary,
        ScheduledJob, Workspace,
    };

    #[test]
    fn mock_search_finds_listing_by_ticker() {
        let snapshot = test_snapshot();
        let results = mock_search(
            &snapshot,
            "VUSA",
            SearchResultFilter::All,
            &SearchSourceFilter::All,
        );

        assert!(results.iter().any(|result| {
            result.result_type == SearchResultType::Listing
                && result.symbol.as_deref() == Some("VUSA")
        }));
    }

    #[test]
    fn mock_search_filters_documents_by_source() {
        let snapshot = test_snapshot();
        let results = mock_search(
            &snapshot,
            "factsheet",
            SearchResultFilter::Documents,
            &SearchSourceFilter::Specific("issuer".to_owned()),
        );

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].result_type, SearchResultType::Document);
    }

    fn test_snapshot() -> DashboardSnapshot {
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
                figi: Some("BBG000000001".to_owned()),
                sedol: Some("B3XXRP0".to_owned()),
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
            workspaces: vec![Workspace {
                id: "workspace-main".to_owned(),
                name: "Main Portfolio".to_owned(),
                base_currency: "GBP".to_owned(),
            }],
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
            holdings: vec![HoldingExposure {
                company: "Microsoft".to_owned(),
                ticker: "MSFT".to_owned(),
                country: "US".to_owned(),
                sector: "Technology".to_owned(),
                weight_pct: 7.2,
                change_since_previous_pct: None,
                source_etf: "VUSA".to_owned(),
                as_of_date: "2026-06-20".to_owned(),
                source: "issuer".to_owned(),
            }],
            exposures: ExposureBreakdown {
                countries: Vec::new(),
                sectors: Vec::new(),
                currencies: Vec::new(),
                top_holdings: Vec::new(),
            },
            alerts: Vec::new(),
            documents: vec![DocumentSnapshot {
                fund_id: "fund-vusa".to_owned(),
                ticker: "VUSA".to_owned(),
                document_type: "Factsheet".to_owned(),
                latest_date: "2026-06-20".to_owned(),
                status: "FRESH".to_owned(),
                content_hash_change: "hash-1".to_owned(),
                source: "issuer".to_owned(),
                last_checked: "2026-06-20".to_owned(),
            }],
            scheduled_jobs: vec![ScheduledJob {
                name: "Price ingestion".to_owned(),
                job_type: "PRICE".to_owned(),
                source: "mock".to_owned(),
                cron_schedule: "0 7 * * *".to_owned(),
                active: true,
                last_run: "2026-06-20".to_owned(),
                next_run: "2026-06-21".to_owned(),
            }],
            job_runs: vec![JobRun {
                id: "job-1".to_owned(),
                job_type: "PRICE".to_owned(),
                source: "mock".to_owned(),
                status: JobStatus::Succeeded,
                started: "2026-06-20".to_owned(),
                finished: Some("2026-06-20".to_owned()),
                inserted: 1,
                updated: 0,
                failed: 0,
                message: "done".to_owned(),
            }],
            data_operations: DataOperationsSnapshot::default(),
            portfolio_tree: InvestableNode::new(
                "root",
                AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
                "Main",
                InvestableKind::Portfolio,
            ),
            time_series: Vec::new(),
            selected: SelectedInstrument::default(),
            last_refresh_at: "2026-06-20".to_owned(),
            data_mode: DataMode::Mock,
            data_status: RefreshStatus::Fresh,
        }
    }
}
