use crate::app_model::DashboardSnapshot;
use crate::charts::{ChartDateAxis, PlotRange, PlotRequest};
use crate::compute::diff::{DiffStatus, mock_entity_diffs};
use crate::domain::{
    AnalysisSubject, Distribution, DocumentSnapshot, Fund, HoldingExposure, JobRun, Position,
};
use crate::format::{fmt_date_str, fmt_optional};
use crate::pages::{format_money, format_number, format_pct, header_cell};
use crate::table_state::{ColumnDescriptor, TableId, TableLayoutRegistry, TableState};
use crate::timeseries::{TimeSeries, TimeSeriesKind, find_series};
use crate::ui::metrics;
use crate::ui::style;
use crate::ui::table_layout::{managed_column, managed_table_revision, table_layout_controls};
use eframe::egui;
use egui_extras::{Column, TableBuilder};
use egui_plot::{Line, Plot, PlotPoints};

#[derive(Clone, Debug)]
pub struct FundDetailState {
    pub tab: FundDetailTab,
    pub listings_table: TableState,
    pub holdings_table: TableState,
    pub distributions_table: TableState,
    pub documents_table: TableState,
    pub active_table: Option<TableId>,
}

impl Default for FundDetailState {
    fn default() -> Self {
        Self {
            tab: FundDetailTab::Overview,
            listings_table: TableState::new(TableId::FundDetailListings),
            holdings_table: TableState::new(TableId::FundDetailHoldings),
            distributions_table: TableState::new(TableId::FundDetailDistributions),
            documents_table: TableState::new(TableId::FundDetailDocuments),
            active_table: None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FundListingColumn {
    Ticker,
    Exchange,
    Currency,
    Unit,
    Venue,
    LastPrice,
    PriceDate,
    Figi,
    Sedol,
    Status,
    Source,
}

impl FundListingColumn {
    const ALL: [Self; 11] = [
        Self::Ticker,
        Self::Exchange,
        Self::Currency,
        Self::Unit,
        Self::Venue,
        Self::LastPrice,
        Self::PriceDate,
        Self::Figi,
        Self::Sedol,
        Self::Status,
        Self::Source,
    ];

    const DESCRIPTORS: [ColumnDescriptor; 11] = [
        ColumnDescriptor::new("ticker", "Ticker", 76.0, 60.0, 130.0).required(),
        ColumnDescriptor::new("exchange", "Exchange", 74.0, 62.0, 140.0),
        ColumnDescriptor::new("currency", "Ccy", 66.0, 54.0, 110.0),
        ColumnDescriptor::new("unit", "Unit", 70.0, 56.0, 120.0).hidden_by_default(),
        ColumnDescriptor::new("venue", "Venue", 180.0, 120.0, 340.0)
            .hidden_by_default()
            .clipped(),
        ColumnDescriptor::new("last_price", "Last price", 90.0, 76.0, 170.0).required(),
        ColumnDescriptor::new("price_date", "Price date", 108.0, 86.0, 190.0),
        ColumnDescriptor::new("figi", "FIGI", 130.0, 100.0, 240.0)
            .hidden_by_default()
            .clipped(),
        ColumnDescriptor::new("sedol", "SEDOL", 108.0, 86.0, 190.0).hidden_by_default(),
        ColumnDescriptor::new("status", "Status", 88.0, 70.0, 160.0).required(),
        ColumnDescriptor::new("source", "Source", 110.0, 88.0, 210.0).required(),
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("fund listing column is in ALL")
    }

    fn key(self) -> &'static str {
        Self::DESCRIPTORS[self.index()].key
    }

    fn payload(self, listing: &crate::domain::FundListing) -> (String, String) {
        let value = match self {
            Self::Ticker => listing.ticker.clone(),
            Self::Exchange => listing.exchange.clone(),
            Self::Currency => listing.currency.clone(),
            Self::Unit => listing.currency_unit.clone(),
            Self::Venue => listing.venue_name.clone(),
            Self::LastPrice => format_money(&listing.currency, listing.last_price),
            Self::PriceDate => listing.last_price_date.clone(),
            Self::Figi => listing.figi.clone().unwrap_or_else(|| "-".to_owned()),
            Self::Sedol => listing.sedol.clone().unwrap_or_else(|| "-".to_owned()),
            Self::Status => listing.status.clone(),
            Self::Source => listing.source.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FundHoldingColumn {
    Company,
    Ticker,
    Country,
    Sector,
    Weight,
    AsOf,
}

impl FundHoldingColumn {
    const ALL: [Self; 6] = [
        Self::Company,
        Self::Ticker,
        Self::Country,
        Self::Sector,
        Self::Weight,
        Self::AsOf,
    ];

    const DESCRIPTORS: [ColumnDescriptor; 6] = [
        ColumnDescriptor::new("company", "Company", 180.0, 130.0, 360.0)
            .required()
            .clipped(),
        ColumnDescriptor::new("ticker", "Ticker", 80.0, 64.0, 140.0).required(),
        ColumnDescriptor::new("country", "Country", 120.0, 90.0, 220.0),
        ColumnDescriptor::new("sector", "Sector", 120.0, 90.0, 240.0),
        ColumnDescriptor::new("weight", "Weight", 70.0, 54.0, 120.0).required(),
        ColumnDescriptor::new("as_of", "As-of", 100.0, 82.0, 180.0),
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("fund holding column is in ALL")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Company => "company",
            Self::Ticker => "ticker",
            Self::Country => "country",
            Self::Sector => "sector",
            Self::Weight => "weight",
            Self::AsOf => "as_of",
        }
    }

    fn payload(self, holding: &HoldingExposure) -> (String, String) {
        let value = match self {
            Self::Company => holding.company.clone(),
            Self::Ticker => holding.ticker.clone(),
            Self::Country => holding.country.clone(),
            Self::Sector => holding.sector.clone(),
            Self::Weight => format_pct(holding.weight_pct),
            Self::AsOf => holding.as_of_date.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FundDistributionColumn {
    ExDate,
    Payment,
    Amount,
    Status,
    Source,
}

impl FundDistributionColumn {
    const ALL: [Self; 5] = [
        Self::ExDate,
        Self::Payment,
        Self::Amount,
        Self::Status,
        Self::Source,
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("fund distribution column is in ALL")
    }

    fn key(self) -> &'static str {
        match self {
            Self::ExDate => "ex_date",
            Self::Payment => "payment_date",
            Self::Amount => "amount",
            Self::Status => "status",
            Self::Source => "source",
        }
    }

    fn payload(self, distribution: &Distribution) -> (String, String) {
        match self {
            Self::ExDate => (distribution.ex_date.clone(), distribution.ex_date.clone()),
            Self::Payment => (
                distribution.payment_date.clone(),
                distribution.payment_date.clone(),
            ),
            Self::Amount => (
                format_money(&distribution.currency, distribution.amount),
                distribution.amount.to_string(),
            ),
            Self::Status => (distribution.status.clone(), distribution.status.clone()),
            Self::Source => (distribution.source.clone(), distribution.source.clone()),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FundDocumentColumn {
    DocumentType,
    LatestDate,
    Status,
    Change,
    LastChecked,
}

impl FundDocumentColumn {
    const ALL: [Self; 5] = [
        Self::DocumentType,
        Self::LatestDate,
        Self::Status,
        Self::Change,
        Self::LastChecked,
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("fund document column is in ALL")
    }

    fn key(self) -> &'static str {
        match self {
            Self::DocumentType => "document_type",
            Self::LatestDate => "latest_date",
            Self::Status => "status",
            Self::Change => "change",
            Self::LastChecked => "last_checked",
        }
    }

    fn payload(self, document: &DocumentSnapshot) -> (String, String) {
        let value = match self {
            Self::DocumentType => document.document_type.clone(),
            Self::LatestDate => document.latest_date.clone(),
            Self::Status => document.status.clone(),
            Self::Change => document.content_hash_change.clone(),
            Self::LastChecked => document.last_checked.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FundDetailTab {
    Overview,
    Prices,
    Distributions,
    Holdings,
    Documents,
    Jobs,
    Diffs,
}

impl FundDetailTab {
    const ALL: [Self; 7] = [
        Self::Overview,
        Self::Prices,
        Self::Distributions,
        Self::Holdings,
        Self::Documents,
        Self::Jobs,
        Self::Diffs,
    ];

    fn label(self) -> &'static str {
        match self {
            Self::Overview => "Overview",
            Self::Prices => "Prices",
            Self::Distributions => "Distributions",
            Self::Holdings => "Holdings",
            Self::Documents => "Documents",
            Self::Jobs => "Jobs",
            Self::Diffs => "Changes",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FundDetailAction {
    Plot(PlotRequest),
    OpenSubject {
        subject: AnalysisSubject,
        label: String,
    },
    OpenDocumentIndex(usize),
}

pub fn render(
    ui: &mut egui::Ui,
    snapshot: &mut DashboardSnapshot,
    state: &mut FundDetailState,
    layouts: &mut TableLayoutRegistry,
) -> Option<FundDetailAction> {
    let selected_fund_id = snapshot.selected.fund_id.clone();
    let selected_listing_id = snapshot.selected.listing_id.clone();
    let mut next_listing = None::<(String, String)>;
    let mut action = None;

    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let Some(fund_id) = selected_fund_id.as_deref() else {
                style::page_header(ui, "Fund Detail", None, None, |_| {});
                style::state_message(
                    ui,
                    "EMPTY",
                    "No fund selected. Select a ticker from Portfolio, ETFs, Documents, Holdings, Alerts, or the command box.",
                );
                ui.monospace("Examples: VUSA, select JEGP, goto ETFs");
                return;
            };

            let Some(fund) = snapshot.funds.iter().find(|fund| fund.id == fund_id) else {
                style::page_header(ui, "Fund Detail", Some(fund_id), None, |_| {});
                style::state_message(
                    ui,
                    "MISSING",
                    "Selected fund is not present in the current snapshot.",
                );
                return;
            };

            let context = fund
                .listings
                .iter()
                .find(|listing| Some(listing.id.as_str()) == selected_listing_id.as_deref())
                .or_else(|| fund.listings.first())
                .map(|listing| listing.ticker.as_str())
                .unwrap_or(fund.isin.as_str());
            let subtitle = format!(
                "{} · {} · refreshed {}",
                fund.name,
                fund.provider,
                fmt_date_str(&fund.last_refreshed)
            );
            style::page_header(
                ui,
                "Fund Detail",
                Some(context),
                Some(&subtitle),
                |ui| {
                    style::status_badge(ui, &fund.status);
                    style::source_badge(ui, &fund.source);
                },
            );
            overview(ui, fund);
            ui.add_space(8.0);
            listings_table(
                ui,
                fund,
                selected_listing_id.as_deref(),
                &mut next_listing,
                state,
                layouts,
            );
            ui.add_space(metrics::SPACE_2);
            freshness_panel(ui, fund, snapshot);
            ui.add_space(metrics::SPACE_2);
            compact_documents_panel(ui, &snapshot.documents, fund);
            ui.separator();
            tab_strip(ui, state);
            ui.add_space(6.0);
            action = tab_content(
                ui,
                state,
                fund,
                selected_listing_id.as_deref(),
                snapshot,
                layouts,
            );
        });

    if let Some((fund_id, listing_id)) = next_listing {
        snapshot.selected.select_listing(fund_id, listing_id);
    }

    action
}

fn tab_strip(ui: &mut egui::Ui, state: &mut FundDetailState) {
    let previous = state.tab;
    ui.horizontal_wrapped(|ui| {
        for tab in FundDetailTab::ALL {
            ui.selectable_value(&mut state.tab, tab, tab.label());
        }
    });
    if state.tab != previous {
        state.active_table = match state.tab {
            FundDetailTab::Holdings => Some(TableId::FundDetailHoldings),
            FundDetailTab::Distributions => Some(TableId::FundDetailDistributions),
            FundDetailTab::Documents => Some(TableId::FundDetailDocuments),
            _ => None,
        };
    }
}

fn tab_content(
    ui: &mut egui::Ui,
    state: &mut FundDetailState,
    fund: &Fund,
    selected_listing_id: Option<&str>,
    snapshot: &DashboardSnapshot,
    layouts: &mut TableLayoutRegistry,
) -> Option<FundDetailAction> {
    match state.tab {
        FundDetailTab::Overview => {
            selected_listing_panel(ui, fund, selected_listing_id);
            ui.add_space(8.0);
            related_position(
                ui,
                &snapshot.positions,
                fund,
                &snapshot.portfolio_summary.base_currency,
            );
            None
        }
        FundDetailTab::Prices => {
            selected_listing_panel(ui, fund, selected_listing_id);
            ui.add_space(8.0);
            price_chart_panel(ui, fund, selected_listing_id, &snapshot.time_series)
        }
        FundDetailTab::Distributions => {
            let row_action = related_distributions(ui, &snapshot.distributions, fund, state);
            row_action.or_else(|| {
                selected_listing(fund, selected_listing_id).and_then(|listing| {
                    if ui.button("Plot Distributions").clicked() {
                        Some(FundDetailAction::Plot(plot_request_for_listing(
                            fund,
                            listing,
                            TimeSeriesKind::Distribution,
                        )))
                    } else {
                        None
                    }
                })
            })
        }
        FundDetailTab::Holdings => related_holdings(ui, &snapshot.holdings, fund, state, layouts),
        FundDetailTab::Documents => related_documents(ui, &snapshot.documents, fund, state),
        FundDetailTab::Jobs => {
            related_jobs(ui, &snapshot.job_runs);
            None
        }
        FundDetailTab::Diffs => {
            related_diffs(ui);
            None
        }
    }
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    snapshot: &DashboardSnapshot,
    state: &mut FundDetailState,
    layouts: &mut TableLayoutRegistry,
) -> Option<FundDetailAction> {
    if ctx.text_edit_focused() {
        return None;
    }
    let fund_id = snapshot.selected.fund_id.as_deref()?;
    let fund = snapshot.funds.iter().find(|fund| fund.id == fund_id)?;

    if state.active_table == Some(TableId::FundDetailListings) {
        let indices = (0..fund.listings.len()).collect::<Vec<_>>();
        let columns =
            layouts.visible_indices(TableId::FundDetailListings, &FundListingColumn::DESCRIPTORS);
        let enter = move_fund_table_focus(ctx, &mut state.listings_table, &indices, &columns);
        sync_fund_listing_focus(fund, state);
        if enter
            && let Some(index) = state.listings_table.selected_index()
            && let Some(listing) = fund.listings.get(index)
        {
            return Some(FundDetailAction::OpenSubject {
                subject: AnalysisSubject::FundListing {
                    fund_id: fund.id.clone(),
                    listing_id: listing.id.clone(),
                },
                label: listing.ticker.clone(),
            });
        }
        return None;
    }

    match state.tab {
        FundDetailTab::Holdings => {
            let source_tickers = fund
                .listings
                .iter()
                .map(|listing| listing.ticker.as_str())
                .collect::<Vec<_>>();
            let indices = snapshot
                .holdings
                .iter()
                .enumerate()
                .filter(|(_, holding)| source_tickers.contains(&holding.source_etf.as_str()))
                .map(|(index, _)| index)
                .take(8)
                .collect::<Vec<_>>();
            let enter = move_fund_table_focus(
                ctx,
                &mut state.holdings_table,
                &indices,
                &layouts
                    .visible_indices(TableId::FundDetailHoldings, &FundHoldingColumn::DESCRIPTORS),
            );
            state.active_table = Some(TableId::FundDetailHoldings);
            sync_fund_holding_focus(&snapshot.holdings, state);
            if enter {
                return state
                    .holdings_table
                    .selected_index()
                    .and_then(|index| snapshot.holdings.get(index))
                    .and_then(|holding| {
                        holding_source_subject(fund, holding).map(|subject| {
                            FundDetailAction::OpenSubject {
                                subject,
                                label: holding.source_etf.clone(),
                            }
                        })
                    });
            }
        }
        FundDetailTab::Distributions => {
            let indices = snapshot
                .distributions
                .iter()
                .enumerate()
                .filter(|(_, distribution)| distribution.fund_id == fund.id)
                .map(|(index, _)| index)
                .collect::<Vec<_>>();
            move_fund_table_focus(
                ctx,
                &mut state.distributions_table,
                &indices,
                &(0..FundDistributionColumn::ALL.len()).collect::<Vec<_>>(),
            );
            state.active_table = Some(TableId::FundDetailDistributions);
            sync_fund_distribution_focus(&snapshot.distributions, state);
        }
        FundDetailTab::Documents => {
            let indices = snapshot
                .documents
                .iter()
                .enumerate()
                .filter(|(_, document)| document.fund_id == fund.id)
                .map(|(index, _)| index)
                .collect::<Vec<_>>();
            let enter = move_fund_table_focus(
                ctx,
                &mut state.documents_table,
                &indices,
                &(0..FundDocumentColumn::ALL.len()).collect::<Vec<_>>(),
            );
            state.active_table = Some(TableId::FundDetailDocuments);
            sync_fund_document_focus(&snapshot.documents, state);
            if enter {
                return state
                    .documents_table
                    .selected_index()
                    .map(FundDetailAction::OpenDocumentIndex);
            }
        }
        _ => {}
    }
    None
}

fn move_fund_table_focus(
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

pub fn selected_copy_payload(
    snapshot: &DashboardSnapshot,
    state: &FundDetailState,
) -> Option<(String, String)> {
    match state.active_table {
        Some(TableId::FundDetailListings) => {
            if let Some(cell) = state.listings_table.selected_cell.as_ref() {
                return Some((cell.display_value.clone(), cell.raw_value.clone()));
            }
            let fund_id = snapshot.selected.fund_id.as_deref()?;
            let fund = snapshot.funds.iter().find(|fund| fund.id == fund_id)?;
            let listing = state
                .listings_table
                .focused_row_index
                .or(state.listings_table.selected_index())
                .and_then(|index| fund.listings.get(index))?;
            Some((
                listing.ticker.clone(),
                FundListingColumn::ALL
                    .iter()
                    .map(|column| column.payload(listing).1)
                    .collect::<Vec<_>>()
                    .join("\t"),
            ))
        }
        Some(TableId::FundDetailHoldings) => {
            if let Some(cell) = state.holdings_table.selected_cell.as_ref() {
                return Some((cell.display_value.clone(), cell.raw_value.clone()));
            }
            let holding = state
                .holdings_table
                .focused_row_index
                .or(state.holdings_table.selected_index())
                .and_then(|index| snapshot.holdings.get(index))?;
            Some((
                holding.ticker.clone(),
                [
                    holding.company.clone(),
                    holding.ticker.clone(),
                    holding.country.clone(),
                    holding.sector.clone(),
                    holding.weight_pct.to_string(),
                    holding.source_etf.clone(),
                    holding.source.clone(),
                ]
                .join("\t"),
            ))
        }
        Some(TableId::FundDetailDistributions) => {
            if let Some(cell) = state.distributions_table.selected_cell.as_ref() {
                return Some((cell.display_value.clone(), cell.raw_value.clone()));
            }
            let distribution = state
                .distributions_table
                .focused_row_index
                .or(state.distributions_table.selected_index())
                .and_then(|index| snapshot.distributions.get(index))?;
            Some((
                distribution.ticker.clone(),
                [
                    distribution.ticker.clone(),
                    distribution.ex_date.clone(),
                    distribution.payment_date.clone(),
                    distribution.amount.to_string(),
                    distribution.currency.clone(),
                    distribution.status.clone(),
                    distribution.source.clone(),
                ]
                .join("\t"),
            ))
        }
        Some(TableId::FundDetailDocuments) => {
            if let Some(cell) = state.documents_table.selected_cell.as_ref() {
                return Some((cell.display_value.clone(), cell.raw_value.clone()));
            }
            let document = state
                .documents_table
                .focused_row_index
                .or(state.documents_table.selected_index())
                .and_then(|index| snapshot.documents.get(index))?;
            Some((
                document.document_type.clone(),
                [
                    document.ticker.clone(),
                    document.document_type.clone(),
                    document.latest_date.clone(),
                    document.status.clone(),
                    document.content_hash_change.clone(),
                    document.source.clone(),
                ]
                .join("\t"),
            ))
        }
        _ => None,
    }
}

fn overview(ui: &mut egui::Ui, fund: &Fund) {
    ui.label(egui::RichText::new("Reference data").strong());
    egui::Grid::new("fund_detail_overview")
        .num_columns(2)
        .min_col_width((ui.available_width() * 0.32).clamp(112.0, 220.0))
        .striped(true)
        .show(ui, |ui| {
            ui.label("Name");
            ui.label(&fund.name);
            ui.end_row();
            ui.label("Provider");
            ui.label(&fund.provider);
            ui.end_row();
            ui.label("ISIN");
            ui.monospace(&fund.isin);
            ui.end_row();
            ui.label("Domicile");
            ui.label(&fund.domicile);
            ui.end_row();
            ui.label("Strategy");
            ui.label(&fund.strategy);
            ui.end_row();
            ui.label("Replication/type");
            ui.label(&fund.replication);
            ui.end_row();
            ui.label("OCF/TER");
            ui.label(format_pct(f64::from(fund.ocf_ter_pct)));
            ui.end_row();
            ui.label("Distribution");
            ui.label(format!(
                "{} | {}",
                fund.distribution_frequency, fund.distribution_policy
            ));
            ui.end_row();
            ui.label("Base currency");
            ui.monospace(&fund.base_currency);
            ui.end_row();
            ui.label("Status");
            style::status_badge(ui, &fund.status);
            ui.end_row();
            ui.label("Last refreshed");
            ui.label(fmt_date_str(&fund.last_refreshed));
            ui.end_row();
            ui.label("Fund ID");
            ui.monospace(&fund.id);
            ui.end_row();
            ui.label("Source");
            style::source_badge(ui, &fund.source);
            ui.end_row();
        });
}

fn listings_table(
    ui: &mut egui::Ui,
    fund: &Fund,
    selected_listing_id: Option<&str>,
    next_listing: &mut Option<(String, String)>,
    state: &mut FundDetailState,
    layouts: &mut TableLayoutRegistry,
) {
    ui.label(egui::RichText::new("Listings").strong());
    let visible_row = state
        .listings_table
        .focused_row_index
        .or(state.listings_table.selected_index())
        .and_then(|index| fund.listings.get(index))
        .map(|listing| {
            (
                listing.ticker.as_str(),
                layouts.visible_row_text(
                    TableId::FundDetailListings,
                    &FundListingColumn::DESCRIPTORS,
                    &FundListingColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(listing).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::FundDetailListings,
        &FundListingColumn::DESCRIPTORS,
        state
            .listings_table
            .focused_column_index
            .and_then(|index| FundListingColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.listings_table.clear_focus();
    }
    let revision = managed_table_revision(
        layouts,
        TableId::FundDetailListings,
        &FundListingColumn::DESCRIPTORS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("fund_detail_listings", revision))
        .striped(true)
        .resizable(false)
        .auto_shrink(false)
        .max_scroll_height(150.0);
    for descriptor in FundListingColumn::DESCRIPTORS {
        table = table.column(managed_column(
            layouts,
            TableId::FundDetailListings,
            &FundListingColumn::DESCRIPTORS,
            descriptor,
        ));
    }

    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for descriptor in FundListingColumn::DESCRIPTORS {
                header.col(|ui| header_cell(ui, descriptor.label));
            }
        })
        .body(|mut body| {
            for (index, listing) in fund.listings.iter().enumerate() {
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.listings_table.is_focused_row(index));
                    row.set_selected(
                        selected_listing_id == Some(listing.id.as_str())
                            || (state.active_table == Some(TableId::FundDetailListings)
                                && state.listings_table.selection.is_selected(index)),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 0),
                        );
                        let is_selected = selected_listing_id == Some(listing.id.as_str());
                        if ui.selectable_label(is_selected, &listing.ticker).clicked() {
                            *next_listing = Some((fund.id.clone(), listing.id.clone()));
                            focus_fund_listing_cell(
                                state,
                                index,
                                listing,
                                FundListingColumn::Ticker,
                            );
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 1),
                        );
                        ui.monospace(&listing.exchange);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 2),
                        );
                        ui.monospace(&listing.currency);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 3),
                        );
                        ui.monospace(&listing.currency_unit);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 4),
                        );
                        ui.label(&listing.venue_name);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 5),
                        );
                        ui.label(format_money(&listing.currency, listing.last_price));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 6),
                        );
                        ui.label(fmt_date_str(&listing.last_price_date));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 7),
                        );
                        ui.monospace(fmt_optional(listing.figi.as_deref()));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 8),
                        );
                        ui.monospace(fmt_optional(listing.sedol.as_deref()));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 9),
                        );
                        style::status_badge(ui, &listing.status);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state.listings_table.is_focused_cell(index, 10),
                        );
                        style::source_badge(ui, &listing.source);
                    });
                });
            }
        });
}

fn selected_listing_panel(ui: &mut egui::Ui, fund: &Fund, selected_listing_id: Option<&str>) {
    let Some(listing) = selected_listing_id
        .and_then(|id| fund.listings.iter().find(|listing| listing.id == id))
        .or_else(|| fund.listings.first())
    else {
        return;
    };

    ui.label(egui::RichText::new("Selected listing context").strong());
    egui::Grid::new("fund_detail_listing_context")
        .num_columns(2)
        .min_col_width((ui.available_width() * 0.32).clamp(112.0, 190.0))
        .striped(true)
        .show(ui, |ui| {
            ui.label("Ticker");
            ui.monospace(&listing.ticker);
            ui.end_row();
            ui.label("Venue");
            ui.label(&listing.venue_name);
            ui.end_row();
            ui.label("Exchange");
            ui.monospace(&listing.exchange);
            ui.end_row();
            ui.label("Listing ID");
            ui.monospace(&listing.id);
            ui.end_row();
        });
}

fn freshness_panel(ui: &mut egui::Ui, fund: &Fund, snapshot: &DashboardSnapshot) {
    ui.label(egui::RichText::new("Freshness").strong());
    let position = snapshot
        .positions
        .iter()
        .find(|position| position.fund_id == fund.id);

    egui::Grid::new("fund_detail_freshness")
        .num_columns(2)
        .min_col_width((ui.available_width() * 0.36).clamp(120.0, 190.0))
        .striped(true)
        .show(ui, |ui| {
            ui.label("Fund");
            style::status_badge(ui, &fund.status);
            ui.end_row();
            ui.label("Source");
            style::source_badge(ui, &fund.source);
            ui.end_row();
            ui.label("Position");
            if let Some(position) = position {
                style::freshness_badge(ui, position.freshness);
            } else {
                ui.monospace("-");
            }
            ui.end_row();
            ui.label("Last refreshed");
            ui.label(fmt_date_str(&fund.last_refreshed));
            ui.end_row();
        });
}

fn compact_documents_panel(ui: &mut egui::Ui, documents: &[DocumentSnapshot], fund: &Fund) {
    ui.label(egui::RichText::new("Documents").strong());
    for document in documents
        .iter()
        .filter(|document| document.fund_id == fund.id)
        .take(5)
    {
        ui.horizontal_wrapped(|ui| {
            ui.monospace(&document.document_type);
            style::status_badge(ui, &document.status);
        });
    }
}

fn related_position(ui: &mut egui::Ui, positions: &[Position], fund: &Fund, base_currency: &str) {
    ui.label(egui::RichText::new("Workspace position").strong());
    if let Some(position) = positions
        .iter()
        .find(|position| position.fund_id == fund.id)
    {
        egui::Grid::new("fund_detail_position")
            .num_columns(2)
            .min_col_width((ui.available_width() * 0.32).clamp(112.0, 200.0))
            .striped(true)
            .show(ui, |ui| {
                ui.label("Units");
                ui.monospace(format!("{:.2}", position.units));
                ui.end_row();
                ui.label("Market value");
                ui.monospace(format_money(base_currency, position.market_value));
                ui.end_row();
                ui.label("Weight");
                ui.monospace(format_pct(position.portfolio_weight_pct));
                ui.end_row();
                ui.label("Projected income");
                ui.monospace(format_money(base_currency, position.projected_income));
                ui.end_row();
                ui.label("Freshness");
                style::freshness_badge(ui, position.freshness);
                ui.end_row();
                ui.label("Position ticker");
                ui.monospace(&position.ticker);
                ui.end_row();
            });
    } else {
        ui.label("No workspace position in mock data.");
    }
}

fn price_chart_panel(
    ui: &mut egui::Ui,
    fund: &Fund,
    selected_listing_id: Option<&str>,
    time_series: &[TimeSeries],
) -> Option<FundDetailAction> {
    let listing = selected_listing(fund, selected_listing_id)?;
    let mut action = None;

    ui.horizontal_wrapped(|ui| {
        if ui.button("Open in Charts").clicked() || ui.button("Plot Price").clicked() {
            action = Some(FundDetailAction::Plot(plot_request_for_listing(
                fund,
                listing,
                TimeSeriesKind::Price,
            )));
        }
        if ui.button("Plot NAV").clicked() {
            action = Some(FundDetailAction::Plot(plot_request_for_listing(
                fund,
                listing,
                TimeSeriesKind::Nav,
            )));
        }
        if ui.button("Plot NAV vs Price").clicked() {
            action = Some(FundDetailAction::Plot(
                plot_request_for_listing(fund, listing, TimeSeriesKind::Nav)
                    .with_overlay(TimeSeriesKind::Price),
            ));
        }
        if ui.button("Plot Distributions").clicked() {
            action = Some(FundDetailAction::Plot(plot_request_for_listing(
                fund,
                listing,
                TimeSeriesKind::Distribution,
            )));
        }
    });

    ui.add_space(6.0);
    inline_listing_plot(ui, fund, listing, time_series);
    ui.add_space(8.0);
    price_table(ui, fund);
    action
}

fn inline_listing_plot(
    ui: &mut egui::Ui,
    fund: &Fund,
    listing: &crate::domain::FundListing,
    time_series: &[TimeSeries],
) {
    let subject = listing_subject(fund, listing);
    ui.label(egui::RichText::new("Price / NAV chart").strong());
    let price = find_series(time_series, &subject, TimeSeriesKind::Price);
    let nav = find_series(time_series, &subject, TimeSeriesKind::Nav);

    if price.is_none() && nav.is_none() {
        ui.label("No mock price or NAV series for this listing.");
        return;
    }

    Plot::new("fund_detail_price_nav_plot")
        .width(ui.available_width())
        .height((ui.available_height() * 0.40).clamp(240.0, 380.0))
        .allow_scroll(false)
        .x_axis_label("Date")
        .x_axis_formatter(|mark, visible_range| {
            let span_days = (*visible_range.end() - *visible_range.start()).abs();
            let formatted = ChartDateAxis::format_tick_for_span(mark.value, span_days);
            if formatted.is_empty() {
                ChartDateAxis::format_tick(mark.value, PlotRange::OneYear)
            } else {
                formatted
            }
        })
        .show(ui, |plot_ui| {
            if let Some(series) = price {
                plot_ui.line(series_line(series));
            }
            if let Some(series) = nav {
                plot_ui.line(series_line(series));
            }
        });

    if let Some(series) = price.or(nav) {
        compact_series_table(ui, "fund_detail_chart_data", series);
    }
}

fn price_table(ui: &mut egui::Ui, fund: &Fund) {
    ui.label(egui::RichText::new("Recent prices").strong());
    TableBuilder::new(ui)
        .id_salt("fund_detail_prices")
        .striped(true)
        .resizable(true)
        .max_scroll_height(160.0)
        .column(Column::initial(78.0).at_least(60.0))
        .column(Column::initial(90.0).at_least(70.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(112.0).at_least(90.0))
        .column(Column::remainder().at_least(104.0))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for label in ["Ticker", "Date", "Price", "Source", "Status"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for listing in &fund.listings {
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.col(|ui| {
                        ui.monospace(&listing.ticker);
                    });
                    row.col(|ui| {
                        ui.label(fmt_date_str(&listing.last_price_date));
                    });
                    row.col(|ui| {
                        ui.label(format_money(&listing.currency, listing.last_price));
                    });
                    row.col(|ui| {
                        style::source_badge(ui, &listing.source);
                    });
                    row.col(|ui| {
                        style::status_badge(ui, &listing.status);
                    });
                });
            }
        });
}

fn compact_series_table(ui: &mut egui::Ui, id: &str, series: &TimeSeries) {
    ui.label(egui::RichText::new("Chart data").strong());
    TableBuilder::new(ui)
        .id_salt(id)
        .striped(true)
        .resizable(true)
        .max_scroll_height(120.0)
        .column(Column::initial(94.0).at_least(74.0))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(90.0).at_least(70.0))
        .column(Column::remainder().at_least(100.0))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for label in ["Date", "Value", "Status", "Source"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for point in series.points.iter().rev().take(6) {
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.col(|ui| {
                        ui.label(&point.date);
                    });
                    row.col(|ui| {
                        ui.monospace(format_number(point.value, 4));
                    });
                    row.col(|ui| {
                        style::status_badge(ui, &point.status);
                    });
                    row.col(|ui| {
                        style::source_badge(ui, &point.source);
                    });
                });
            }
        });
}

fn series_line(series: &TimeSeries) -> Line<'_> {
    let points: PlotPoints<'_> = series
        .points
        .iter()
        .filter_map(|point| {
            let x = ChartDateAxis::date_to_x(&point.date)?;
            point.value.is_finite().then_some([x, point.value])
        })
        .collect();
    Line::new(series.label.clone(), points)
}

fn selected_listing<'a>(
    fund: &'a Fund,
    selected_listing_id: Option<&str>,
) -> Option<&'a crate::domain::FundListing> {
    selected_listing_id
        .and_then(|id| fund.listings.iter().find(|listing| listing.id == id))
        .or_else(|| fund.listings.first())
}

fn plot_request_for_listing(
    fund: &Fund,
    listing: &crate::domain::FundListing,
    kind: TimeSeriesKind,
) -> PlotRequest {
    PlotRequest::new(
        listing_subject(fund, listing),
        kind,
        format!("{} {}", listing.ticker, kind.as_str()),
    )
}

fn listing_subject(fund: &Fund, listing: &crate::domain::FundListing) -> AnalysisSubject {
    AnalysisSubject::FundListing {
        fund_id: fund.id.clone(),
        listing_id: listing.id.clone(),
    }
}

fn holding_source_subject(fund: &Fund, holding: &HoldingExposure) -> Option<AnalysisSubject> {
    fund.listings
        .iter()
        .find(|listing| listing.ticker == holding.source_etf)
        .or_else(|| fund.listings.first())
        .map(|listing| listing_subject(fund, listing))
}

fn focus_fund_holding_cell(
    state: &mut FundDetailState,
    index: usize,
    holding: &HoldingExposure,
    column: FundHoldingColumn,
) {
    let (display, raw) = column.payload(holding);
    state
        .holdings_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = Some(TableId::FundDetailHoldings);
}

fn focus_fund_listing_cell(
    state: &mut FundDetailState,
    index: usize,
    listing: &crate::domain::FundListing,
    column: FundListingColumn,
) {
    let (display, raw) = column.payload(listing);
    state
        .listings_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = Some(TableId::FundDetailListings);
}

fn sync_fund_listing_focus(fund: &Fund, state: &mut FundDetailState) {
    let (Some(row_index), Some(column_index)) = (
        state.listings_table.focused_row_index,
        state.listings_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(listing), Some(column)) = (
        fund.listings.get(row_index),
        FundListingColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(listing);
    state
        .listings_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn sync_fund_holding_focus(holdings: &[HoldingExposure], state: &mut FundDetailState) {
    let (Some(row_index), Some(column_index)) = (
        state.holdings_table.focused_row_index,
        state.holdings_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(holding), Some(column)) = (
        holdings.get(row_index),
        FundHoldingColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(holding);
    state
        .holdings_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn focus_fund_distribution_cell(
    state: &mut FundDetailState,
    index: usize,
    distribution: &Distribution,
    column: FundDistributionColumn,
) {
    let (display, raw) = column.payload(distribution);
    state
        .distributions_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = Some(TableId::FundDetailDistributions);
}

fn sync_fund_distribution_focus(distributions: &[Distribution], state: &mut FundDetailState) {
    let (Some(row_index), Some(column_index)) = (
        state.distributions_table.focused_row_index,
        state.distributions_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(distribution), Some(column)) = (
        distributions.get(row_index),
        FundDistributionColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(distribution);
    state
        .distributions_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn focus_fund_document_cell(
    state: &mut FundDetailState,
    index: usize,
    document: &DocumentSnapshot,
    column: FundDocumentColumn,
) {
    let (display, raw) = column.payload(document);
    state
        .documents_table
        .select_cell(index, column.index(), column.key(), display, raw);
    state.active_table = Some(TableId::FundDetailDocuments);
}

fn sync_fund_document_focus(documents: &[DocumentSnapshot], state: &mut FundDetailState) {
    let (Some(row_index), Some(column_index)) = (
        state.documents_table.focused_row_index,
        state.documents_table.focused_column_index,
    ) else {
        return;
    };
    let (Some(document), Some(column)) = (
        documents.get(row_index),
        FundDocumentColumn::ALL.get(column_index).copied(),
    ) else {
        return;
    };
    let (display, raw) = column.payload(document);
    state
        .documents_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn related_distributions(
    ui: &mut egui::Ui,
    distributions: &[Distribution],
    fund: &Fund,
    state: &mut FundDetailState,
) -> Option<FundDetailAction> {
    ui.label(egui::RichText::new("Recent distributions").strong());
    let indices = distributions
        .iter()
        .enumerate()
        .filter(|(_, distribution)| distribution.fund_id == fund.id)
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    state.distributions_table.retain_visible(&indices);
    compact_distribution_table(
        ui,
        "fund_detail_distributions",
        distributions,
        &indices,
        state,
    );
    None
}

fn related_diffs(ui: &mut egui::Ui) {
    ui.label(egui::RichText::new("Field diffs").strong());
    TableBuilder::new(ui)
        .id_salt("fund_detail_diffs")
        .striped(true)
        .resizable(true)
        .max_scroll_height(220.0)
        .column(Column::initial(150.0).at_least(110.0))
        .column(Column::initial(180.0).at_least(130.0).clip(true))
        .column(Column::initial(180.0).at_least(130.0).clip(true))
        .column(Column::remainder().at_least(92.0))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for label in ["Field", "Left", "Right", "Status"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for diff in mock_entity_diffs() {
                for field in diff.fields {
                    body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                        row.col(|ui| {
                            ui.label(field.field_name);
                        });
                        row.col(|ui| {
                            ui.label(field.left_value.as_deref().unwrap_or("-"));
                        });
                        row.col(|ui| {
                            ui.label(field.right_value.as_deref().unwrap_or("-"));
                        });
                        row.col(|ui| {
                            ui.label(match field.status {
                                DiffStatus::Unchanged => "UNCHANGED",
                                DiffStatus::Added => "ADDED",
                                DiffStatus::Removed => "REMOVED",
                                DiffStatus::Changed => "CHANGED",
                            });
                        });
                    });
                }
            }
        });
}

fn related_holdings(
    ui: &mut egui::Ui,
    holdings: &[HoldingExposure],
    fund: &Fund,
    state: &mut FundDetailState,
    layouts: &mut TableLayoutRegistry,
) -> Option<FundDetailAction> {
    ui.label(egui::RichText::new("Top look-through holdings").strong());
    let source_tickers = fund
        .listings
        .iter()
        .map(|listing| listing.ticker.as_str())
        .collect::<Vec<_>>();
    let indices = holdings
        .iter()
        .enumerate()
        .filter(|(_, holding)| source_tickers.contains(&holding.source_etf.as_str()))
        .map(|(index, _)| index)
        .take(8)
        .collect::<Vec<_>>();
    state.holdings_table.retain_visible(&indices);
    let visible_row = state
        .holdings_table
        .focused_row_index
        .or(state.holdings_table.selected_index())
        .and_then(|index| holdings.get(index))
        .map(|holding| {
            (
                holding.ticker.as_str(),
                layouts.visible_row_text(
                    TableId::FundDetailHoldings,
                    &FundHoldingColumn::DESCRIPTORS,
                    &FundHoldingColumn::ALL
                        .iter()
                        .map(|column| (column.key(), column.payload(holding).1))
                        .collect::<Vec<_>>(),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::FundDetailHoldings,
        &FundHoldingColumn::DESCRIPTORS,
        state
            .holdings_table
            .focused_column_index
            .and_then(|index| FundHoldingColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.holdings_table.clear_focus();
    }
    let mut action = None;
    let revision = managed_table_revision(
        layouts,
        TableId::FundDetailHoldings,
        &FundHoldingColumn::DESCRIPTORS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("fund_detail_holdings", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(160.0);
    for descriptor in FundHoldingColumn::DESCRIPTORS {
        table = table.column(managed_column(
            layouts,
            TableId::FundDetailHoldings,
            &FundHoldingColumn::DESCRIPTORS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for descriptor in FundHoldingColumn::DESCRIPTORS {
                header.col(|ui| header_cell(ui, descriptor.label));
            }
        })
        .body(|mut body| {
            for index in indices {
                let holding = &holdings[index];
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.holdings_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == Some(TableId::FundDetailHoldings)
                            && state.holdings_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .holdings_table
                                .is_focused_cell(index, FundHoldingColumn::Company.index()),
                        );
                        ui.label(&holding.company);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .holdings_table
                                .is_focused_cell(index, FundHoldingColumn::Ticker.index()),
                        );
                        let response = ui
                            .selectable_label(
                                state.holdings_table.selection.is_selected(index),
                                &holding.ticker,
                            )
                            .on_hover_text("Double-click to open the source fund/listing.")
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            focus_fund_holding_cell(
                                state,
                                index,
                                holding,
                                FundHoldingColumn::Ticker,
                            );
                        }
                        if response.double_clicked()
                            && let Some(subject) = holding_source_subject(fund, holding)
                        {
                            action = Some(FundDetailAction::OpenSubject {
                                subject,
                                label: holding.source_etf.clone(),
                            });
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .holdings_table
                                .is_focused_cell(index, FundHoldingColumn::Country.index()),
                        );
                        ui.label(&holding.country);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .holdings_table
                                .is_focused_cell(index, FundHoldingColumn::Sector.index()),
                        );
                        ui.label(&holding.sector);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .holdings_table
                                .is_focused_cell(index, FundHoldingColumn::Weight.index()),
                        );
                        ui.label(format_pct(holding.weight_pct));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .holdings_table
                                .is_focused_cell(index, FundHoldingColumn::AsOf.index()),
                        );
                        ui.label(fmt_date_str(&holding.as_of_date));
                    });
                });
            }
        });
    action
}

fn related_jobs(ui: &mut egui::Ui, job_runs: &[JobRun]) {
    ui.label(egui::RichText::new("Recent job context").strong());
    TableBuilder::new(ui)
        .id_salt("fund_detail_jobs")
        .striped(true)
        .resizable(true)
        .max_scroll_height(140.0)
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(128.0).at_least(96.0).clip(true))
        .column(Column::initial(86.0).at_least(68.0))
        .column(Column::initial(128.0).at_least(104.0))
        .column(Column::remainder().at_least(160.0).clip(true))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for label in ["Type", "Source", "Status", "Started", "Message"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for run in job_runs.iter().take(5) {
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.col(|ui| {
                        ui.label(&run.job_type);
                    });
                    row.col(|ui| {
                        style::source_badge(ui, &run.source);
                    });
                    row.col(|ui| {
                        style::job_status_badge(ui, run.status);
                    });
                    row.col(|ui| {
                        ui.label(&run.started);
                    });
                    row.col(|ui| {
                        ui.label(&run.message);
                    });
                });
            }
        });
}

fn related_documents(
    ui: &mut egui::Ui,
    documents: &[DocumentSnapshot],
    fund: &Fund,
    state: &mut FundDetailState,
) -> Option<FundDetailAction> {
    ui.label(egui::RichText::new("Documents").strong());
    let indices = documents
        .iter()
        .enumerate()
        .filter(|(_, document)| document.fund_id == fund.id)
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    state.documents_table.retain_visible(&indices);
    let mut action = None;
    TableBuilder::new(ui)
        .id_salt("fund_detail_documents")
        .striped(true)
        .resizable(true)
        .max_scroll_height(160.0)
        .column(Column::initial(126.0).at_least(96.0))
        .column(Column::initial(104.0).at_least(82.0))
        .column(Column::initial(94.0).at_least(74.0))
        .column(Column::initial(138.0).at_least(104.0).clip(true))
        .column(Column::remainder().at_least(120.0))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for label in ["Type", "Latest date", "Status", "Change", "Last checked"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for index in indices {
                let document = &documents[index];
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.documents_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == Some(TableId::FundDetailDocuments)
                            && state.documents_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .documents_table
                                .is_focused_cell(index, FundDocumentColumn::DocumentType.index()),
                        );
                        let response = ui
                            .selectable_label(
                                state.documents_table.selection.is_selected(index),
                                &document.document_type,
                            )
                            .on_hover_text("Double-click to open the in-app document viewer.")
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            focus_fund_document_cell(
                                state,
                                index,
                                document,
                                FundDocumentColumn::DocumentType,
                            );
                        }
                        if response.double_clicked() {
                            action = Some(FundDetailAction::OpenDocumentIndex(index));
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .documents_table
                                .is_focused_cell(index, FundDocumentColumn::LatestDate.index()),
                        );
                        ui.label(fmt_date_str(&document.latest_date));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .documents_table
                                .is_focused_cell(index, FundDocumentColumn::Status.index()),
                        );
                        style::status_badge(ui, &document.status);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .documents_table
                                .is_focused_cell(index, FundDocumentColumn::Change.index()),
                        );
                        ui.label(&document.content_hash_change);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .documents_table
                                .is_focused_cell(index, FundDocumentColumn::LastChecked.index()),
                        );
                        ui.label(fmt_date_str(&document.last_checked));
                    });
                });
            }
        });
    action
}

fn compact_distribution_table(
    ui: &mut egui::Ui,
    id: &str,
    distributions: &[Distribution],
    indices: &[usize],
    state: &mut FundDetailState,
) {
    TableBuilder::new(ui)
        .id_salt(id)
        .striped(true)
        .resizable(true)
        .max_scroll_height(140.0)
        .column(Column::initial(90.0).at_least(74.0))
        .column(Column::initial(104.0).at_least(86.0))
        .column(Column::initial(80.0).at_least(64.0))
        .column(Column::initial(88.0).at_least(70.0))
        .column(Column::remainder().at_least(100.0))
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            for label in ["Ex-date", "Payment", "Amount", "Status", "Source"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for index in indices.iter().copied() {
                let distribution = &distributions[index];
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.distributions_table.is_focused_row(index));
                    row.set_selected(
                        state.active_table == Some(TableId::FundDetailDistributions)
                            && state.distributions_table.selection.is_selected(index),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .distributions_table
                                .is_focused_cell(index, FundDistributionColumn::ExDate.index()),
                        );
                        let response = ui
                            .selectable_label(
                                state.distributions_table.selection.is_selected(index),
                                fmt_date_str(&distribution.ex_date),
                            )
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            focus_fund_distribution_cell(
                                state,
                                index,
                                distribution,
                                FundDistributionColumn::ExDate,
                            );
                        }
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .distributions_table
                                .is_focused_cell(index, FundDistributionColumn::Payment.index()),
                        );
                        ui.label(fmt_date_str(&distribution.payment_date));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .distributions_table
                                .is_focused_cell(index, FundDistributionColumn::Amount.index()),
                        );
                        ui.label(format_money(&distribution.currency, distribution.amount));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .distributions_table
                                .is_focused_cell(index, FundDistributionColumn::Status.index()),
                        );
                        style::status_badge(ui, &distribution.status);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .distributions_table
                                .is_focused_cell(index, FundDistributionColumn::Source.index()),
                        );
                        style::source_badge(ui, &distribution.source);
                    });
                });
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fund_detail_focus_columns_cover_priority_tables() {
        assert_eq!(FundHoldingColumn::ALL.len(), 6);
        assert_eq!(
            FundHoldingColumn::ALL.len(),
            FundHoldingColumn::DESCRIPTORS.len()
        );
        assert_eq!(FundHoldingColumn::Weight.key(), "weight");
        assert_eq!(FundHoldingColumn::DESCRIPTORS[0].key, "company");
        assert_eq!(FundDistributionColumn::ALL.len(), 5);
        assert_eq!(FundDistributionColumn::Amount.index(), 2);
        assert_eq!(FundDocumentColumn::ALL.len(), 5);
        assert_eq!(FundDocumentColumn::Change.key(), "change");
    }
}
