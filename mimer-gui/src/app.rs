use crate::api::client::{
    ApiDataProvider, ConfiguredDataProvider, DataProvider, current_utc_timestamp,
    normalize_backend_base_url,
};
use crate::api::types::{ApiConnectionStatus, ApiRuntimeStatus, AuthMode, DataMode};
use crate::app_info;
use crate::app_model::{DashboardSnapshot, LoadState, RefreshStatus, SelectedInstrument};
use crate::charts::{ChartMode, ChartPanelState, ChartSeriesSpec, ChartValueMode, PlotRequest};
use crate::command::{
    COMMAND_SUGGESTIONS, CommandAction, CommandHistory, CommandOutcome, match_command,
};
use crate::compute::portfolio::{apply_position_overrides, build_investable_tree};
use crate::debounce::DebouncedValue;
use crate::domain::{AnalysisSubject, Workspace};
use crate::inspector::{InspectorContext, InspectorMode, InspectorState};
use crate::mock_data::MockDataProvider;
use crate::navigation::{NavigationEntry, NavigationStack};
use crate::pages::add_instrument::AddInstrumentState;
use crate::pages::alerts::AlertsState;
use crate::pages::compare::CompareState;
use crate::pages::curves::CurvesState;
use crate::pages::data_operations::DataOperationsState;
use crate::pages::dividends::DividendsState;
use crate::pages::documents::DocumentsState;
use crate::pages::etfs::EtfsState;
use crate::pages::exposure::ExposureState;
use crate::pages::fund_detail::FundDetailState;
use crate::pages::hierarchy::HierarchyState;
use crate::pages::holdings::HoldingsState;
use crate::pages::jobs::JobsState;
use crate::pages::portfolio::PortfolioState;
use crate::pages::search::SearchState;
use crate::pages::settings::SettingsState;
use crate::pages::spreads::SpreadsState;
use crate::pages::{
    Page, add_instrument, alerts, analytics, charts as charts_page, compare, curves,
    data_operations, diffs, dividends, documents, etfs, exposure, fund_detail,
    hierarchy as hierarchy_page, holdings, jobs, portfolio, search, settings, spreads,
};
use crate::source::{
    DataKind, SourcePolicy, SourceSelection, mock_available_sources_for, resolve_source_selection,
};
use crate::storage::{StorageManager, StorageSummary, StoredAppSettings, StoredUiState};
use crate::table_state::TableLayoutRegistry;
use crate::timeseries::TimeSeriesKind;
use crate::ui::{metrics, style};
use crate::ui_state::LayoutState;
use eframe::egui;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

const PREFERENCES_KEY: &str = "mimer.preferences.v1";
const PREFERENCE_WRITE_DEBOUNCE: Duration = Duration::from_millis(900);
const COMMAND_INPUT_ID: &str = "mimer_command_input";
const COMMAND_GO_WIDTH: f32 = 42.0;
const COMMAND_SEARCH_WIDTH: f32 = 64.0;
pub(crate) const TOP_MENU_LABELS: [&str; 8] = [
    "File", "Edit", "View", "Navigate", "Data", "Tools", "Window", "Help",
];

#[derive(Clone, Debug)]
enum AppAction {
    Open(NavigationEntry),
    OpenDocumentIndex(usize),
    Plot(PlotRequest),
    Navigate(Page),
    PinInspector,
    ClearRowOverrides(String),
    MakeActive {
        subject: AnalysisSubject,
        label: String,
    },
    Copy {
        label: String,
        text: String,
    },
    Feedback(String),
    RefreshDataOperations,
    SetDataMode(DataMode),
    TestBackendConnection,
    Back,
}

#[derive(Clone, Debug)]
enum InspectorDetailAction {
    Open(InspectorContext),
    Plot(PlotRequest),
    Navigate(Page),
    ClearRowOverrides(String),
    MarkAlertRead(usize),
    DismissAlert(usize),
    ResolveAlert(usize),
    RunJob(String),
    Copy(CopyPayload),
    Feedback(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct CopyPayload {
    label: String,
    text: String,
}

impl CopyPayload {
    fn new(label: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            label: label.into(),
            text: text.into(),
        }
    }
}

fn choose_copy_payload(
    focused_cell: Option<CopyPayload>,
    focused_row: Option<CopyPayload>,
    selected_row: Option<CopyPayload>,
    summary: Option<CopyPayload>,
) -> Option<CopyPayload> {
    focused_cell.or(focused_row).or(selected_row).or(summary)
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct SubjectContext {
    subject: AnalysisSubject,
    label: String,
    tooltip: String,
}

#[derive(Debug)]
struct RefreshWorkerResult {
    generation: u64,
    snapshot: DashboardSnapshot,
}

#[derive(Debug)]
struct ApiTestWorkerResult {
    generation: u64,
    checked_at: String,
    result: Result<String, String>,
}

pub struct MimerApp {
    egui_ctx: egui::Context,
    provider: MockDataProvider,
    snapshot: LoadState<DashboardSnapshot>,
    refresh_receiver: Option<mpsc::Receiver<RefreshWorkerResult>>,
    refresh_generation: u64,
    api_test_receiver: Option<mpsc::Receiver<ApiTestWorkerResult>>,
    api_test_generation: u64,
    api_runtime: ApiRuntimeStatus,
    selected_workspace_id: String,
    page: Page,
    navigation: NavigationStack,
    command_input: String,
    command_feedback: String,
    command_history: CommandHistory,
    focus_command_next_frame: bool,
    show_command_help: bool,
    show_status_legend: bool,
    add_instrument: AddInstrumentState,
    alerts: AlertsState,
    portfolio: PortfolioState,
    hierarchy: HierarchyState,
    charts: ChartPanelState,
    etfs: EtfsState,
    exposure: ExposureState,
    dividends: DividendsState,
    documents: DocumentsState,
    fund_detail: FundDetailState,
    holdings: HoldingsState,
    jobs: JobsState,
    data_operations: DataOperationsState,
    curves: CurvesState,
    compare: CompareState,
    spreads: SpreadsState,
    search: SearchState,
    settings: SettingsState,
    layout: LayoutState,
    table_layouts: TableLayoutRegistry,
    inspector: InspectorState,
    storage: Option<StorageManager>,
    storage_status: String,
    preference_write: DebouncedValue<AppPreferences>,
    mock_job_message: Option<String>,
    show_about: bool,
    data_operations_page_was_open: bool,
}

impl MimerApp {
    pub fn new(cc: &eframe::CreationContext<'_>) -> Self {
        let provider = MockDataProvider::default();
        let workspaces = provider.load_workspaces();
        let (storage, mut storage_status) = match StorageManager::initialize() {
            Ok(storage) => {
                let summary = storage.summary();
                (Some(storage), format!("READY {}", summary.version_dir))
            }
            Err(err) => (None, format!("UNAVAILABLE: {err}")),
        };
        let disk_preferences =
            storage
                .as_ref()
                .and_then(|storage| match AppPreferences::load_from_storage(storage) {
                    Ok(preferences) => preferences,
                    Err(err) => {
                        storage_status = format!("READY, preference load skipped: {err}");
                        None
                    }
                });
        let preferences = disk_preferences.or_else(|| AppPreferences::load(cc.storage));
        let default_workspace_id = workspaces
            .first()
            .map(|workspace| workspace.id.clone())
            .unwrap_or_else(|| "workspace-main".to_owned());
        let selected_workspace_id = preferences
            .as_ref()
            .and_then(|preferences| preferences.selected_workspace_id.clone())
            .filter(|workspace_id| {
                workspaces
                    .iter()
                    .any(|workspace| workspace.id == *workspace_id)
            })
            .unwrap_or(default_workspace_id);
        let snapshot = load_snapshot(
            &provider,
            &selected_workspace_id,
            SelectedInstrument::default(),
            "2026-06-20 12:00 mock".to_owned(),
        );
        let navigation = NavigationStack::new(home_navigation_entry(&snapshot.workspace));
        let mut settings = SettingsState::default();
        let mut layout = LayoutState::default();
        let table_layouts = preferences
            .as_ref()
            .and_then(|preferences| preferences.table_layouts.clone())
            .unwrap_or_default();
        let mut initial_page = Page::Portfolio;
        if let Some(preferences) = preferences.as_ref() {
            preferences.apply(&mut settings, &mut layout);
            if let Some(page) = preferences.active_page {
                initial_page = page;
            }
        }
        configure_context(
            &cc.egui_ctx,
            settings.theme,
            settings.density,
            settings.zoom_factor,
        );
        let initial_preferences = AppPreferences::from_state(
            &selected_workspace_id,
            &layout,
            &table_layouts,
            &settings,
            initial_page,
        );
        let api_runtime = ApiRuntimeStatus {
            connection: if settings.data_mode == DataMode::Mock {
                ApiConnectionStatus::NotUsed
            } else {
                ApiConnectionStatus::Disconnected
            },
            ..Default::default()
        };

        Self {
            egui_ctx: cc.egui_ctx.clone(),
            provider,
            snapshot: LoadState::Loaded(snapshot),
            refresh_receiver: None,
            refresh_generation: 0,
            api_test_receiver: None,
            api_test_generation: 0,
            api_runtime,
            selected_workspace_id,
            page: initial_page,
            navigation,
            command_input: String::new(),
            command_feedback: String::new(),
            command_history: CommandHistory::default(),
            focus_command_next_frame: false,
            show_command_help: false,
            show_status_legend: false,
            add_instrument: AddInstrumentState::default(),
            alerts: AlertsState::default(),
            portfolio: PortfolioState::default(),
            hierarchy: HierarchyState::default(),
            charts: ChartPanelState::default(),
            etfs: EtfsState::default(),
            exposure: ExposureState::default(),
            dividends: DividendsState::default(),
            documents: DocumentsState::default(),
            fund_detail: FundDetailState::default(),
            holdings: HoldingsState::default(),
            jobs: JobsState::default(),
            data_operations: DataOperationsState::default(),
            curves: CurvesState::default(),
            compare: CompareState::default(),
            spreads: SpreadsState::default(),
            search: SearchState::default(),
            settings,
            layout,
            table_layouts,
            inspector: InspectorState::default(),
            storage,
            storage_status,
            preference_write: DebouncedValue::new(initial_preferences, PREFERENCE_WRITE_DEBOUNCE),
            mock_job_message: None,
            show_about: false,
            data_operations_page_was_open: false,
        }
    }

    fn refresh_data(&mut self) {
        self.refresh_generation = self.refresh_generation.saturating_add(1);
        let generation = self.refresh_generation;
        let selected = self
            .snapshot
            .as_loaded()
            .map(|snapshot| snapshot.selected.clone())
            .unwrap_or_default();
        let (fallback_operations, fallback_scheduled_jobs, fallback_job_runs) = self
            .snapshot
            .as_loaded()
            .map(|snapshot| {
                (
                    snapshot.data_operations.clone(),
                    snapshot.scheduled_jobs.clone(),
                    snapshot.job_runs.clone(),
                )
            })
            .unwrap_or_default();
        let refresh_time = current_utc_timestamp();
        let provider = ConfiguredDataProvider::new(
            self.provider.clone(),
            self.settings.data_mode,
            self.settings.api_config.clone(),
            fallback_operations,
            fallback_scheduled_jobs,
            fallback_job_runs,
        );
        let workspace_id = self.selected_workspace_id.clone();
        let (sender, receiver) = mpsc::channel();
        let ctx = self.egui_ctx.clone();

        if let Some(snapshot) = self.snapshot.as_loaded_mut() {
            snapshot.data_status = RefreshStatus::Refreshing;
        } else {
            self.snapshot = LoadState::Loading;
        }
        self.api_runtime.connection = if self.settings.data_mode == DataMode::Api {
            ApiConnectionStatus::Checking
        } else {
            ApiConnectionStatus::NotUsed
        };

        thread::spawn(move || {
            let snapshot = load_snapshot(&provider, &workspace_id, selected, refresh_time);
            if sender
                .send(RefreshWorkerResult {
                    generation,
                    snapshot,
                })
                .is_ok()
            {
                ctx.request_repaint();
            }
        });

        self.refresh_receiver = Some(receiver);
        self.command_feedback = format!(
            "CMD: queued {} refresh",
            self.settings.data_mode.as_str().to_ascii_lowercase()
        );
    }

    fn poll_refresh_worker(&mut self) {
        let Some(result) = self.refresh_receiver.as_ref().map(mpsc::Receiver::try_recv) else {
            return;
        };

        match result {
            Ok(result) => {
                self.refresh_receiver = None;
                if !is_current_refresh_response(self.refresh_generation, result.generation) {
                    self.command_feedback = "CMD: discarded superseded refresh".to_owned();
                    return;
                }
                let mut snapshot = result.snapshot;
                if snapshot.workspace.id != self.selected_workspace_id {
                    self.command_feedback =
                        "CMD: discarded stale refresh; queued current workspace".to_owned();
                    self.refresh_data();
                    return;
                }
                let workspace_changed = self
                    .snapshot
                    .as_loaded()
                    .is_none_or(|current| current.workspace.id != snapshot.workspace.id);
                let override_result = self.apply_portfolio_overrides(&mut snapshot);
                if workspace_changed {
                    self.navigation =
                        NavigationStack::new(home_navigation_entry(&snapshot.workspace));
                    self.page = Page::Portfolio;
                }
                self.api_runtime = api_runtime_from_snapshot(&snapshot);
                self.snapshot = LoadState::Loaded(snapshot);
                self.command_feedback = override_result.unwrap_or_else(|| {
                    let mode = self.settings.data_mode.as_str().to_ascii_lowercase();
                    let hydration = self
                        .snapshot
                        .as_loaded()
                        .map(|snapshot| snapshot.data_operations.hydration.origin.as_str())
                        .unwrap_or("-");
                    if self.portfolio.overrides.is_empty() {
                        format!("CMD: refreshed {mode} data | {hydration}")
                    } else {
                        format!(
                            "CMD: refreshed {mode} data | {hydration} | MANUAL {}",
                            self.portfolio.overrides.len()
                        )
                    }
                });
            }
            Err(mpsc::TryRecvError::Empty) => {}
            Err(mpsc::TryRecvError::Disconnected) => {
                self.refresh_receiver = None;
                if let Some(snapshot) = self.snapshot.as_loaded_mut() {
                    snapshot.data_status = RefreshStatus::Error;
                }
                self.api_runtime.connection = if self.settings.data_mode == DataMode::Api {
                    ApiConnectionStatus::Disconnected
                } else {
                    ApiConnectionStatus::NotUsed
                };
                self.api_runtime.last_error =
                    Some("refresh worker disconnected; previous data retained".to_owned());
                self.command_feedback =
                    "CMD: refresh worker disconnected; previous data retained".to_owned();
            }
        }
    }

    fn test_backend_connection(&mut self) {
        self.api_test_generation = self.api_test_generation.saturating_add(1);
        let generation = self.api_test_generation;
        let config = self.settings.api_config.clone();
        let (sender, receiver) = mpsc::channel();
        let ctx = self.egui_ctx.clone();
        self.api_runtime.connection = ApiConnectionStatus::Checking;
        self.api_runtime.last_error = None;

        thread::spawn(move || {
            let checked_at = current_utc_timestamp();
            let result =
                ApiDataProvider::new(config).and_then(|provider| provider.test_connection());
            if sender
                .send(ApiTestWorkerResult {
                    generation,
                    checked_at,
                    result,
                })
                .is_ok()
            {
                ctx.request_repaint();
            }
        });

        self.api_test_receiver = Some(receiver);
        self.command_feedback = "API: connection test queued".to_owned();
    }

    fn poll_api_test_worker(&mut self) {
        let Some(result) = self
            .api_test_receiver
            .as_ref()
            .map(mpsc::Receiver::try_recv)
        else {
            return;
        };

        match result {
            Ok(result) => {
                self.api_test_receiver = None;
                if result.generation != self.api_test_generation {
                    return;
                }
                self.api_runtime.last_checked_at = Some(result.checked_at);
                match result.result {
                    Ok(message) => {
                        self.api_runtime.connection = ApiConnectionStatus::Connected;
                        self.api_runtime.last_error = None;
                        self.command_feedback = format!("API: {message}");
                    }
                    Err(message) => {
                        let message = crate::api::safety::mask_secret_fragments(&message);
                        self.api_runtime.connection = ApiConnectionStatus::Disconnected;
                        self.api_runtime.last_error = Some(message.clone());
                        self.command_feedback = format!("API: {message}");
                    }
                }
            }
            Err(mpsc::TryRecvError::Empty) => {}
            Err(mpsc::TryRecvError::Disconnected) => {
                self.api_test_receiver = None;
                self.api_runtime.connection = ApiConnectionStatus::Disconnected;
                self.api_runtime.last_error =
                    Some("connection-test worker disconnected".to_owned());
            }
        }
    }

    fn current_preferences(&self) -> AppPreferences {
        AppPreferences::from_state(
            &self.selected_workspace_id,
            &self.layout,
            &self.table_layouts,
            &self.settings,
            self.page,
        )
    }

    fn schedule_preference_write(&mut self, ctx: &egui::Context) {
        let now = Instant::now();
        let preferences = self.current_preferences();
        if &preferences != self.preference_write.editable_value() {
            self.preference_write.set_pending(preferences, now);
        }
        if let Some(delay) = self.preference_write.remaining_delay(now) {
            ctx.request_repaint_after(delay);
        }
        if self.preference_write.commit_if_due(now) {
            let preferences = self.preference_write.committed().clone();
            self.persist_preferences_to_disk(&preferences);
        }
    }

    fn persist_preferences_to_disk(&mut self, preferences: &AppPreferences) {
        let Some(storage) = self.storage.as_ref() else {
            return;
        };
        let (settings, ui_state) = preferences.to_storage_documents();
        match storage
            .save_settings(&settings)
            .and_then(|()| storage.save_ui_state(&ui_state))
        {
            Ok(()) => {
                self.storage_status = format!("READY {}", storage.paths().version_dir().display());
            }
            Err(err) => {
                self.storage_status = format!("WRITE FAILED: {err}");
            }
        }
    }

    fn storage_summary(&self) -> Option<StorageSummary> {
        self.storage.as_ref().map(StorageManager::summary)
    }

    fn apply_portfolio_overrides(&self, snapshot: &mut DashboardSnapshot) -> Option<String> {
        if self.portfolio.overrides.is_empty() {
            return None;
        }

        match apply_position_overrides(
            &mut snapshot.positions,
            &snapshot.portfolio_summary.base_currency,
            &self.portfolio.overrides,
        ) {
            Ok(summary) => {
                snapshot.portfolio_summary = summary;
                snapshot.portfolio_tree = build_investable_tree(
                    &snapshot.workspace,
                    &snapshot.portfolio_summary,
                    &snapshot.positions,
                    &snapshot.funds,
                    &snapshot.holdings,
                );
                None
            }
            Err(err) => Some(format!(
                "CMD: refresh loaded, override replay failed: {err}"
            )),
        }
    }

    fn cancel_active_edit(&mut self) -> bool {
        match self.page {
            Page::Portfolio if self.portfolio.table.edit.is_editing() => {
                self.portfolio.table.edit.cancel();
                self.portfolio.edit_error = None;
                true
            }
            Page::Settings if self.settings.has_pending_changes() => {
                self.settings.revert_pending();
                true
            }
            _ => false,
        }
    }

    fn clear_current_table_focus(&mut self) -> bool {
        let table = match self.page {
            Page::Portfolio => Some(&mut self.portfolio.table),
            Page::Etfs => Some(&mut self.etfs.table),
            Page::Documents | Page::DocumentViewer => Some(&mut self.documents.table),
            Page::Alerts => Some(&mut self.alerts.table),
            Page::FundDetail => match self.fund_detail.active_table {
                Some(crate::table_state::TableId::FundDetailListings) => {
                    Some(&mut self.fund_detail.listings_table)
                }
                Some(crate::table_state::TableId::FundDetailHoldings) => {
                    Some(&mut self.fund_detail.holdings_table)
                }
                Some(crate::table_state::TableId::FundDetailDistributions) => {
                    Some(&mut self.fund_detail.distributions_table)
                }
                Some(crate::table_state::TableId::FundDetailDocuments) => {
                    Some(&mut self.fund_detail.documents_table)
                }
                _ => None,
            },
            Page::Jobs => {
                if self.jobs.active_table == crate::table_state::TableId::JobRuns {
                    Some(&mut self.jobs.runs_table)
                } else {
                    Some(&mut self.jobs.scheduled_table)
                }
            }
            Page::DataOperations => match self.data_operations.active_table {
                crate::table_state::TableId::DataOperationsPlan => {
                    Some(&mut self.data_operations.plan_table)
                }
                crate::table_state::TableId::DataOperationsSources => {
                    Some(&mut self.data_operations.source_budget_table)
                }
                crate::table_state::TableId::DataOperationsFetchLogs => {
                    Some(&mut self.data_operations.fetch_log_table)
                }
                crate::table_state::TableId::DataOperationsDiagnostics => {
                    Some(&mut self.data_operations.diagnostic_table)
                }
                crate::table_state::TableId::DataOperationsScheduler => {
                    Some(&mut self.data_operations.scheduler_table)
                }
                crate::table_state::TableId::DataOperationsConstituents => {
                    Some(&mut self.data_operations.constituent_table)
                }
                crate::table_state::TableId::DataOperationsApiSections => {
                    Some(&mut self.data_operations.api_sections_table)
                }
                _ => None,
            },
            Page::Exposure => match self.exposure.active_table {
                crate::table_state::TableId::ExposureSectors => {
                    Some(&mut self.exposure.sectors_table)
                }
                crate::table_state::TableId::ExposureCurrencies => {
                    Some(&mut self.exposure.currencies_table)
                }
                crate::table_state::TableId::ExposureTopHoldings => {
                    Some(&mut self.exposure.top_holdings_table)
                }
                crate::table_state::TableId::ExposureDiagnostics => {
                    Some(&mut self.exposure.diagnostics_table)
                }
                _ => Some(&mut self.exposure.countries_table),
            },
            _ => None,
        };
        let Some(table) = table else {
            return false;
        };
        let had_focus = table.focused_row_index.is_some() || table.selected_cell.is_some();
        table.clear_focus();
        had_focus
    }

    fn clear_current_table_selection(&mut self) -> bool {
        let cleared = match self.page {
            Page::Portfolio => {
                let had_selection = self.portfolio.table.selected_index().is_some();
                self.portfolio.table.clear_selection();
                had_selection
            }
            Page::Etfs => {
                let had_selection = self.etfs.table.selected_index().is_some();
                self.etfs.table.clear_selection();
                had_selection
            }
            Page::Documents | Page::DocumentViewer => {
                let had_selection = self.documents.table.selected_index().is_some();
                self.documents.table.clear_selection();
                had_selection
            }
            Page::Alerts => {
                let had_selection = self.alerts.table.selected_index().is_some();
                self.alerts.table.clear_selection();
                had_selection
            }
            Page::Jobs => {
                let had_selection = self.jobs.scheduled_table.selected_index().is_some()
                    || self.jobs.runs_table.selected_index().is_some();
                self.jobs.scheduled_table.clear_selection();
                self.jobs.runs_table.clear_selection();
                had_selection
            }
            Page::FundDetail => {
                let had_selection = self.fund_detail.listings_table.selected_index().is_some()
                    || self.fund_detail.holdings_table.selected_index().is_some()
                    || self
                        .fund_detail
                        .distributions_table
                        .selected_index()
                        .is_some()
                    || self.fund_detail.documents_table.selected_index().is_some();
                self.fund_detail.listings_table.clear_selection();
                self.fund_detail.holdings_table.clear_selection();
                self.fund_detail.distributions_table.clear_selection();
                self.fund_detail.documents_table.clear_selection();
                had_selection
            }
            Page::DataOperations => {
                let had_selection = self.data_operations.selected_readiness_index.is_some()
                    || self.data_operations.plan_table.selected_index().is_some()
                    || self
                        .data_operations
                        .source_budget_table
                        .selected_index()
                        .is_some()
                    || self
                        .data_operations
                        .fetch_log_table
                        .selected_index()
                        .is_some()
                    || self
                        .data_operations
                        .diagnostic_table
                        .selected_index()
                        .is_some()
                    || self
                        .data_operations
                        .scheduler_table
                        .selected_index()
                        .is_some()
                    || self
                        .data_operations
                        .constituent_table
                        .selected_index()
                        .is_some()
                    || self
                        .data_operations
                        .api_sections_table
                        .selected_index()
                        .is_some();
                self.data_operations.clear_selections();
                had_selection
            }
            Page::Exposure => {
                let had_selection = self.exposure.countries_table.selected_index().is_some()
                    || self.exposure.sectors_table.selected_index().is_some()
                    || self.exposure.currencies_table.selected_index().is_some()
                    || self.exposure.top_holdings_table.selected_index().is_some()
                    || self.exposure.diagnostics_table.selected_index().is_some();
                self.exposure.countries_table.clear_selection();
                self.exposure.sectors_table.clear_selection();
                self.exposure.currencies_table.clear_selection();
                self.exposure.top_holdings_table.clear_selection();
                self.exposure.diagnostics_table.clear_selection();
                had_selection
            }
            Page::Charts | Page::Compare | Page::Spreads => {
                let had_selection = self.charts.data_table.selected_index().is_some()
                    || self.charts.selected_point.is_some()
                    || self.charts.selected_series_id.is_some();
                self.charts.clear_chart_selection();
                had_selection
            }
            _ => false,
        };
        if cleared && let Some(snapshot) = self.snapshot.as_loaded_mut() {
            snapshot.selected.clear();
        }
        cleared
    }

    fn zoom_in(&mut self) {
        self.settings.zoom_in();
        self.egui_ctx.set_zoom_factor(self.settings.zoom_factor);
        self.command_feedback = format!("VIEW: zoom {}%", self.settings.zoom_percent());
    }

    fn zoom_out(&mut self) {
        self.settings.zoom_out();
        self.egui_ctx.set_zoom_factor(self.settings.zoom_factor);
        self.command_feedback = format!("VIEW: zoom {}%", self.settings.zoom_percent());
    }

    fn reset_zoom(&mut self) {
        self.settings.reset_zoom();
        self.egui_ctx.set_zoom_factor(self.settings.zoom_factor);
        self.command_feedback = "VIEW: zoom reset to 100%".to_owned();
    }

    fn copy_to_clipboard(&mut self, payload: CopyPayload) -> CommandOutcome {
        self.egui_ctx.copy_text(payload.text);
        CommandOutcome::Copied(payload.label)
    }

    fn copy_selected_payload(&self) -> Option<CopyPayload> {
        let snapshot = self.snapshot.as_loaded()?;

        match self.page {
            Page::Portfolio => {
                let cell = self.portfolio.table.selected_cell.as_ref().map(|cell| {
                    CopyPayload::new(cell.display_value.clone(), cell.raw_value.clone())
                });
                let focused_row = self
                    .portfolio
                    .table
                    .focused_row_index
                    .and_then(|index| snapshot.positions.get(index))
                    .map(|position| {
                        CopyPayload::new(
                            format!("row {}", position.ticker),
                            portfolio_row_copy_text(
                                position,
                                &snapshot.portfolio_summary.base_currency,
                            ),
                        )
                    });
                let selected_row = self
                    .portfolio
                    .table
                    .selected_index()
                    .and_then(|index| snapshot.positions.get(index))
                    .map(|position| {
                        CopyPayload::new(
                            format!("row {}", position.ticker),
                            portfolio_row_copy_text(
                                position,
                                &snapshot.portfolio_summary.base_currency,
                            ),
                        )
                    });
                if let Some(payload) = choose_copy_payload(cell, focused_row, selected_row, None) {
                    return Some(payload);
                }
            }
            Page::Etfs => {
                if let Some(cell) = self.etfs.table.selected_cell.as_ref() {
                    return Some(CopyPayload::new(
                        format!("ETF cell {}", cell.column),
                        cell.raw_value.clone(),
                    ));
                }
                if let Some(fund) = self
                    .etfs
                    .table
                    .focused_row_index
                    .or(self.etfs.table.selected_index())
                    .and_then(|index| snapshot.funds.get(index))
                {
                    return Some(CopyPayload::new(
                        fund.listings
                            .first()
                            .map(|listing| listing.ticker.clone())
                            .unwrap_or_else(|| fund.isin.clone()),
                        fund_row_copy_text(fund),
                    ));
                }
            }
            Page::Documents | Page::DocumentViewer => {
                let cell = self.documents.table.selected_cell.as_ref().map(|cell| {
                    CopyPayload::new(
                        format!("document cell {}", cell.column),
                        cell.raw_value.clone(),
                    )
                });
                let focused_row = self
                    .documents
                    .table
                    .focused_row_index
                    .and_then(|index| snapshot.documents.get(index))
                    .map(|document| {
                        CopyPayload::new(
                            format!("document {}", document.ticker),
                            document_row_copy_text(document),
                        )
                    });
                let selected_row = self
                    .documents
                    .table
                    .selected_index()
                    .and_then(|index| snapshot.documents.get(index))
                    .map(|document| {
                        CopyPayload::new(
                            format!("document {}", document.ticker),
                            document_row_copy_text(document),
                        )
                    });
                if let Some(payload) = choose_copy_payload(cell, focused_row, selected_row, None) {
                    return Some(payload);
                }
            }
            Page::Jobs => {
                let active_cell = if self.jobs.active_table == crate::table_state::TableId::JobRuns
                {
                    self.jobs.runs_table.selected_cell.as_ref()
                } else {
                    self.jobs.scheduled_table.selected_cell.as_ref()
                };
                if let Some(cell) = active_cell {
                    return Some(CopyPayload::new(
                        format!("job cell {}", cell.column),
                        cell.raw_value.clone(),
                    ));
                }
                if let Some(run) = self
                    .jobs
                    .runs_table
                    .selected_index()
                    .and_then(|index| snapshot.job_runs.get(index))
                {
                    return Some(CopyPayload::new(
                        format!("job run {}", run.id),
                        job_run_copy_text(run),
                    ));
                }
                if let Some(job) = self
                    .jobs
                    .scheduled_table
                    .selected_index()
                    .and_then(|index| snapshot.scheduled_jobs.get(index))
                {
                    return Some(CopyPayload::new(
                        format!("job {}", job.name),
                        scheduled_job_copy_text(job),
                    ));
                }
            }
            Page::DataOperations => {
                if let Some((label, text)) =
                    data_operations::selected_copy_payload(snapshot, &self.data_operations)
                {
                    return Some(CopyPayload::new(label, text));
                }
            }
            Page::Charts | Page::Compare | Page::Spreads => {
                if let Some(request) = self.charts.active_plot.as_ref() {
                    if let Some(cell) = self.charts.data_table.selected_cell.as_ref() {
                        return Some(CopyPayload::new(
                            format!("chart cell {} {}", cell.column, cell.display_value),
                            cell.raw_value.clone(),
                        ));
                    }
                    if let Some(point) = self.charts.selected_point.as_ref() {
                        return Some(CopyPayload::new(point.label(), point.copy_text()));
                    }

                    let series_set =
                        crate::charts::chart_series_for_request(request, &snapshot.time_series);
                    if let Some(series_id) = self.charts.selected_series_id.as_ref()
                        && let Some(series) = series_set
                            .series
                            .iter()
                            .find(|series| series.id == *series_id)
                    {
                        let value_mode = effective_chart_value_mode(&self.charts, request);
                        let base_date =
                            chart_base_date(series_set.series.as_slice(), request, value_mode);
                        return Some(CopyPayload::new(
                            format!("series {}", series.label),
                            chart_series_copy_text_for_mode(
                                series,
                                request,
                                value_mode,
                                base_date.as_deref(),
                            ),
                        ));
                    }

                    return Some(CopyPayload::new(
                        request.label.clone(),
                        chart_workspace_copy_text(
                            request,
                            series_set.series.as_slice(),
                            effective_chart_value_mode(&self.charts, request),
                        ),
                    ));
                }
            }
            Page::Search => {
                if let Some(result) = self.search.selected_result(snapshot) {
                    return Some(CopyPayload::new(
                        result.label.clone(),
                        search::copy_text_for_result(&result),
                    ));
                }
            }
            Page::Alerts => {
                let cell = self.alerts.table.selected_cell.as_ref().map(|cell| {
                    CopyPayload::new(
                        format!("alert cell {}", cell.column),
                        cell.raw_value.clone(),
                    )
                });
                let focused_row = self
                    .alerts
                    .table
                    .focused_row_index
                    .and_then(|index| snapshot.alerts.get(index))
                    .map(|alert| {
                        CopyPayload::new(
                            format!("alert {}", alert.id),
                            alerts::alert_copy_text(alert),
                        )
                    });
                let selected_row = self
                    .alerts
                    .table
                    .selected_index()
                    .and_then(|index| snapshot.alerts.get(index))
                    .map(|alert| {
                        CopyPayload::new(
                            format!("alert {}", alert.id),
                            alerts::alert_copy_text(alert),
                        )
                    });
                if let Some(payload) = choose_copy_payload(cell, focused_row, selected_row, None) {
                    return Some(payload);
                }
            }
            Page::FundDetail => {
                if let Some((label, text)) =
                    fund_detail::selected_copy_payload(snapshot, &self.fund_detail)
                {
                    return Some(CopyPayload::new(label, text));
                }
            }
            Page::Exposure => {
                if let Some((label, text)) =
                    exposure::selected_copy_payload(&snapshot.exposures, &self.exposure)
                {
                    return Some(CopyPayload::new(label, text));
                }
            }
            Page::Hierarchy
            | Page::AddInstrument
            | Page::Dividends
            | Page::Holdings
            | Page::Analytics
            | Page::Curves
            | Page::Diffs
            | Page::Settings => {}
        }

        selected_subject_payload(snapshot)
    }

    fn copy_active_payload(&self) -> Option<CopyPayload> {
        let active = self.active_subject_context()?;
        Some(CopyPayload::new(active.label.clone(), active.label))
    }

    fn active_subject_context(&self) -> Option<SubjectContext> {
        let snapshot = self.snapshot.as_loaded()?;
        match self.page {
            Page::Portfolio => Some(SubjectContext {
                subject: AnalysisSubject::WorkspacePortfolio(snapshot.workspace.id.clone()),
                label: snapshot.workspace.name.clone(),
                tooltip: format!(
                    "Active: workspace portfolio\nBase: {}\nValue: {}",
                    snapshot.portfolio_summary.base_currency,
                    crate::format::fmt_money(
                        &snapshot.portfolio_summary.base_currency,
                        snapshot.portfolio_summary.total_value,
                    )
                ),
            }),
            Page::Charts => self.charts.active_plot.as_ref().map(|request| {
                let label = charts_page::subject_label(snapshot, &request.subject);
                SubjectContext {
                    subject: request.subject.clone(),
                    label,
                    tooltip: format!(
                        "Active chart subject\nSeries: {}\nPlot: {}\nRange: {}",
                        request.series_kind.as_str(),
                        request.label,
                        request.options.range.as_str(),
                    ),
                }
            }),
            Page::Documents | Page::DocumentViewer => {
                if let Some(document) = self
                    .documents
                    .table
                    .selected_index()
                    .and_then(|index| snapshot.documents.get(index))
                {
                    return Some(SubjectContext {
                        subject: AnalysisSubject::Fund(document.fund_id.clone()),
                        label: format!("{} {}", document.ticker, document.document_type),
                        tooltip: format!(
                            "Active document\nDate: {}\nStatus: {}\nSource: {}",
                            document.latest_date,
                            document.status,
                            crate::format::fmt_source(&document.source),
                        ),
                    });
                }
                active_from_selected(snapshot)
            }
            Page::FundDetail
            | Page::Etfs
            | Page::Hierarchy
            | Page::Dividends
            | Page::Holdings
            | Page::Exposure
            | Page::Alerts
            | Page::Analytics
            | Page::Curves
            | Page::Compare
            | Page::Spreads
            | Page::Jobs
            | Page::DataOperations
            | Page::Diffs
            | Page::Search
            | Page::Settings
            | Page::AddInstrument => active_from_selected(snapshot),
        }
        .or_else(|| {
            Some(SubjectContext {
                subject: AnalysisSubject::WorkspacePortfolio(snapshot.workspace.id.clone()),
                label: snapshot.workspace.name.clone(),
                tooltip: "Active: workspace portfolio".to_owned(),
            })
        })
    }

    fn resolve_follow_inspector_context(&self) -> Option<InspectorContext> {
        let snapshot = self.snapshot.as_loaded()?;

        let selected_context = match self.page {
            Page::Portfolio => self
                .portfolio
                .table
                .selected_index()
                .and_then(|index| {
                    snapshot
                        .positions
                        .get(index)
                        .map(|position| (index, position))
                })
                .map(
                    |(row_index, position)| InspectorContext::PortfolioPosition {
                        row_index,
                        subject: AnalysisSubject::FundListing {
                            fund_id: position.fund_id.clone(),
                            listing_id: position.listing_id.clone(),
                        },
                        label: position.ticker.clone(),
                    },
                ),
            Page::Etfs => self
                .etfs
                .table
                .selected_index()
                .and_then(|index| snapshot.funds.get(index).map(|fund| (index, fund)))
                .map(|(row_index, fund)| InspectorContext::Fund {
                    row_index,
                    subject: AnalysisSubject::Fund(fund.id.clone()),
                    label: compact_fund_label(fund),
                }),
            Page::Dividends => self
                .dividends
                .selected_row
                .and_then(|index| {
                    snapshot
                        .distributions
                        .get(index)
                        .map(|distribution| (index, distribution))
                })
                .map(|(row_index, distribution)| InspectorContext::Distribution {
                    row_index,
                    subject: subject_for_fund_ticker(
                        snapshot,
                        &distribution.fund_id,
                        &distribution.ticker,
                    )
                    .unwrap_or_else(|| AnalysisSubject::Fund(distribution.fund_id.clone())),
                    label: format!("{} {}", distribution.ticker, distribution.ex_date),
                }),
            Page::Holdings => self
                .holdings
                .table
                .selected_index()
                .and_then(|index| snapshot.holdings.get(index).map(|holding| (index, holding)))
                .map(|(row_index, holding)| InspectorContext::Holding {
                    row_index,
                    subject: AnalysisSubject::Holding {
                        ticker: holding.ticker.clone(),
                        source: holding.source_etf.clone(),
                    },
                    label: format!("{} {}", holding.ticker, holding.company),
                }),
            Page::Documents | Page::DocumentViewer => self
                .documents
                .table
                .selected_index()
                .and_then(|index| {
                    snapshot
                        .documents
                        .get(index)
                        .map(|document| (index, document))
                })
                .map(|(row_index, document)| InspectorContext::DocumentSnapshot {
                    row_index,
                    subject: AnalysisSubject::Fund(document.fund_id.clone()),
                    label: format!("{} {}", document.ticker, document.document_type),
                }),
            Page::Alerts => self
                .alerts
                .table
                .selected_index()
                .and_then(|index| snapshot.alerts.get(index).map(|alert| (index, alert)))
                .map(|(row_index, alert)| InspectorContext::Alert {
                    row_index,
                    alert_id: alert.id.clone(),
                    affected_subject: alert
                        .fund_ticker
                        .as_deref()
                        .and_then(|ticker| subject_for_ticker(snapshot, ticker)),
                    label: alert.title.clone(),
                }),
            Page::Jobs => if self.jobs.active_table == crate::table_state::TableId::JobRuns {
                self.jobs
                    .runs_table
                    .selected_index()
                    .and_then(|index| snapshot.job_runs.get(index).map(|run| (index, run)))
                    .map(|(row_index, run)| InspectorContext::JobRun {
                        row_index,
                        run_id: run.id.clone(),
                        label: format!("{} {}", run.job_type, run.status.as_str()),
                    })
            } else {
                self.jobs
                    .scheduled_table
                    .selected_index()
                    .and_then(|index| snapshot.scheduled_jobs.get(index).map(|job| (index, job)))
                    .map(|(row_index, job)| InspectorContext::ScheduledJob {
                        row_index,
                        name: job.name.clone(),
                        label: job.name.clone(),
                    })
            }
            .or_else(|| {
                self.jobs
                    .runs_table
                    .selected_index()
                    .and_then(|index| snapshot.job_runs.get(index).map(|run| (index, run)))
                    .map(|(row_index, run)| InspectorContext::JobRun {
                        row_index,
                        run_id: run.id.clone(),
                        label: format!("{} {}", run.job_type, run.status.as_str()),
                    })
            })
            .or_else(|| {
                self.jobs
                    .scheduled_table
                    .selected_index()
                    .and_then(|index| snapshot.scheduled_jobs.get(index).map(|job| (index, job)))
                    .map(|(row_index, job)| InspectorContext::ScheduledJob {
                        row_index,
                        name: job.name.clone(),
                        label: job.name.clone(),
                    })
            }),
            Page::DataOperations => match self.data_operations.active_table {
                crate::table_state::TableId::DataOperationsReadiness => self
                    .data_operations
                    .selected_readiness_index
                    .and_then(|index| {
                        snapshot
                            .data_operations
                            .readiness_stages
                            .get(index)
                            .map(|stage| (index, stage))
                    })
                    .map(|(row_index, stage)| InspectorContext::ReadinessStage {
                        row_index,
                        key: stage.key.clone(),
                        label: stage.label.clone(),
                    }),
                crate::table_state::TableId::DataOperationsPlan => self
                    .data_operations
                    .plan_table
                    .selected_index()
                    .and_then(|index| {
                        snapshot
                            .data_operations
                            .market_data_plan
                            .get(index)
                            .map(|item| (index, item))
                    })
                    .map(|(row_index, item)| InspectorContext::MarketDataPlanItem {
                        row_index,
                        item_id: item.id.clone(),
                        subject: item.subject.clone(),
                        label: item.subject_label.clone(),
                    }),
                crate::table_state::TableId::DataOperationsScheduler => self
                    .data_operations
                    .scheduler_table
                    .selected_index()
                    .and_then(|index| snapshot.scheduled_jobs.get(index).map(|job| (index, job)))
                    .map(|(row_index, job)| InspectorContext::ScheduledJob {
                        row_index,
                        name: job.name.clone(),
                        label: job.name.clone(),
                    }),
                crate::table_state::TableId::DataOperationsSources => self
                    .data_operations
                    .source_budget_table
                    .selected_index()
                    .and_then(|index| {
                        snapshot
                            .data_operations
                            .source_budgets
                            .get(index)
                            .map(|budget| (index, budget))
                    })
                    .map(|(row_index, budget)| InspectorContext::SourceBudget {
                        row_index,
                        source: budget.source.clone(),
                        label: budget.source.clone(),
                    }),
                crate::table_state::TableId::DataOperationsFetchLogs => self
                    .data_operations
                    .fetch_log_table
                    .selected_index()
                    .and_then(|index| {
                        snapshot
                            .data_operations
                            .fetch_logs
                            .get(index)
                            .map(|log| (index, log))
                    })
                    .map(|(row_index, log)| InspectorContext::FetchLog {
                        row_index,
                        log_id: log.id.clone(),
                        label: log.id.clone(),
                    }),
                crate::table_state::TableId::DataOperationsConstituents => self
                    .data_operations
                    .constituent_table
                    .selected_index()
                    .and_then(|index| {
                        snapshot
                            .data_operations
                            .constituent_coverage
                            .get(index)
                            .map(|row| (index, row))
                    })
                    .map(|(row_index, row)| InspectorContext::ConstituentReadiness {
                        row_index,
                        subject: row.subject.clone(),
                        label: row.holding_ticker.clone(),
                    }),
                crate::table_state::TableId::DataOperationsDiagnostics => self
                    .data_operations
                    .diagnostic_table
                    .selected_index()
                    .and_then(|index| {
                        snapshot
                            .data_operations
                            .diagnostic_issues
                            .get(index)
                            .map(|issue| (index, issue))
                    })
                    .map(|(row_index, issue)| InspectorContext::DataDiagnosticIssue {
                        row_index,
                        issue_id: issue.id.clone(),
                        label: issue.title.clone(),
                    }),
                crate::table_state::TableId::DataOperationsApiSections => self
                    .data_operations
                    .api_sections_table
                    .selected_index()
                    .and_then(|index| {
                        snapshot
                            .data_operations
                            .backend_sections
                            .get(index)
                            .map(|section| (index, section))
                    })
                    .map(|(row_index, section)| InspectorContext::TableRow {
                        table_id: crate::table_state::TableId::DataOperationsApiSections
                            .key()
                            .to_owned(),
                        row_index,
                        label: section.label.clone(),
                        details: format!(
                            "Status: {}\nRows: {}\nSource: {}\n{}",
                            section.status.as_str(),
                            section
                                .record_count
                                .map(|count| count.to_string())
                                .unwrap_or_else(|| "-".to_owned()),
                            section.source,
                            section.detail
                        ),
                    }),
                crate::table_state::TableId::DataOperationsActions
                | crate::table_state::TableId::PortfolioPositions
                | crate::table_state::TableId::EtfsFunds
                | crate::table_state::TableId::ExposureCountries
                | crate::table_state::TableId::ExposureSectors
                | crate::table_state::TableId::ExposureCurrencies
                | crate::table_state::TableId::ExposureTopHoldings
                | crate::table_state::TableId::ExposureDiagnostics
                | crate::table_state::TableId::Holdings
                | crate::table_state::TableId::Documents
                | crate::table_state::TableId::Dividends
                | crate::table_state::TableId::ScheduledJobs
                | crate::table_state::TableId::JobRuns
                | crate::table_state::TableId::Alerts
                | crate::table_state::TableId::ChartSeriesData
                | crate::table_state::TableId::SearchResults
                | crate::table_state::TableId::FundDetailListings
                | crate::table_state::TableId::FundDetailHoldings
                | crate::table_state::TableId::FundDetailDistributions
                | crate::table_state::TableId::FundDetailDocuments => None,
            },
            Page::Charts | Page::Compare | Page::Spreads => {
                let request = self.charts.active_plot.as_ref()?;
                if let Some(point) = self.charts.selected_point.as_ref() {
                    Some(InspectorContext::ChartPoint {
                        subject: point.subject.clone(),
                        series_id: point.series_id.0.clone(),
                        series_kind: point.series_kind.as_str().to_owned(),
                        label: point.series_label.clone(),
                        date: point.date.clone(),
                        value: crate::format::fmt_decimal(point.value, 4),
                        raw_value: crate::format::fmt_decimal(point.raw_value, 4),
                        unit: point.unit.clone(),
                        raw_unit: point.raw_unit.clone(),
                        source: point.source.clone(),
                        status: point.status.clone(),
                        value_mode: point.value_mode.label().to_owned(),
                    })
                } else {
                    let series_set =
                        crate::charts::chart_series_for_request(request, &snapshot.time_series);
                    if let Some(series_id) = self.charts.selected_series_id.as_ref()
                        && let Some(series) = series_set
                            .series
                            .iter()
                            .find(|series| series.id == *series_id)
                    {
                        Some(InspectorContext::ChartSeries {
                            subject: series.subject.clone(),
                            series_id: series.id.0.clone(),
                            series_kind: series.kind.as_str().to_owned(),
                            label: series.label.clone(),
                            unit: series.unit.clone(),
                            source: series.source.clone(),
                            status: series.status.clone(),
                            role: series.role.label().to_owned(),
                        })
                    } else if request.mode == ChartMode::Spread {
                        series_set
                            .spread
                            .as_ref()
                            .map(|spread| InspectorContext::ChartSpread {
                                left_label: spread.left_label.clone(),
                                right_label: spread.right_label.clone(),
                                label: request.label.clone(),
                                coverage: spread.coverage_label(),
                                left_source: spread.left_source.clone(),
                                right_source: spread.right_source.clone(),
                                status: spread.status.clone(),
                            })
                    } else {
                        Some(InspectorContext::ChartWorkspace {
                            subject: request.subject.clone(),
                            mode: request.mode.label().to_owned(),
                            label: request.label.clone(),
                        })
                    }
                }
            }
            Page::FundDetail => match self.fund_detail.active_table {
                Some(crate::table_state::TableId::FundDetailListings) => {
                    let fund_id = snapshot.selected.fund_id.as_deref()?;
                    let fund = snapshot.funds.iter().find(|fund| fund.id == fund_id)?;
                    self.fund_detail
                        .listings_table
                        .selected_index()
                        .and_then(|index| fund.listings.get(index))
                        .map(|listing| InspectorContext::FundListing {
                            subject: AnalysisSubject::FundListing {
                                fund_id: fund.id.clone(),
                                listing_id: listing.id.clone(),
                            },
                            label: listing.ticker.clone(),
                        })
                }
                Some(crate::table_state::TableId::FundDetailHoldings) => self
                    .fund_detail
                    .holdings_table
                    .selected_index()
                    .and_then(|index| snapshot.holdings.get(index).map(|holding| (index, holding)))
                    .map(|(row_index, holding)| InspectorContext::Holding {
                        row_index,
                        subject: AnalysisSubject::Holding {
                            ticker: holding.ticker.clone(),
                            source: holding.source_etf.clone(),
                        },
                        label: format!("{} {}", holding.ticker, holding.company),
                    }),
                Some(crate::table_state::TableId::FundDetailDistributions) => self
                    .fund_detail
                    .distributions_table
                    .selected_index()
                    .and_then(|index| {
                        snapshot
                            .distributions
                            .get(index)
                            .map(|distribution| (index, distribution))
                    })
                    .map(|(row_index, distribution)| InspectorContext::Distribution {
                        row_index,
                        subject: subject_for_fund_ticker(
                            snapshot,
                            &distribution.fund_id,
                            &distribution.ticker,
                        )
                        .unwrap_or_else(|| AnalysisSubject::Fund(distribution.fund_id.clone())),
                        label: format!("{} {}", distribution.ticker, distribution.ex_date),
                    }),
                Some(crate::table_state::TableId::FundDetailDocuments) => self
                    .fund_detail
                    .documents_table
                    .selected_index()
                    .and_then(|index| {
                        snapshot
                            .documents
                            .get(index)
                            .map(|document| (index, document))
                    })
                    .map(|(row_index, document)| InspectorContext::DocumentSnapshot {
                        row_index,
                        subject: AnalysisSubject::Fund(document.fund_id.clone()),
                        label: format!("{} {}", document.ticker, document.document_type),
                    }),
                _ => None,
            }
            .or_else(|| {
                snapshot.selected.fund_id.as_ref().map(|fund_id| {
                    let label =
                        selection_label(snapshot, fund_id, snapshot.selected.listing_id.as_deref());
                    if let Some(listing_id) = snapshot.selected.listing_id.as_ref() {
                        InspectorContext::FundListing {
                            subject: AnalysisSubject::FundListing {
                                fund_id: fund_id.clone(),
                                listing_id: listing_id.clone(),
                            },
                            label,
                        }
                    } else {
                        InspectorContext::ActiveSubject {
                            subject: AnalysisSubject::Fund(fund_id.clone()),
                            label,
                            tooltip: snapshot.selected_subject_tooltip(),
                        }
                    }
                })
            }),
            Page::Exposure => exposure::inspector_details(&snapshot.exposures, &self.exposure).map(
                |(label, details)| InspectorContext::TableRow {
                    table_id: self.exposure.active_table.key().to_owned(),
                    row_index: match self.exposure.active_table {
                        crate::table_state::TableId::ExposureSectors => {
                            self.exposure.sectors_table.selected_index()
                        }
                        crate::table_state::TableId::ExposureCurrencies => {
                            self.exposure.currencies_table.selected_index()
                        }
                        crate::table_state::TableId::ExposureTopHoldings => {
                            self.exposure.top_holdings_table.selected_index()
                        }
                        crate::table_state::TableId::ExposureDiagnostics => {
                            self.exposure.diagnostics_table.selected_index()
                        }
                        _ => self.exposure.countries_table.selected_index(),
                    }
                    .unwrap_or_default(),
                    label,
                    details,
                },
            ),
            Page::Hierarchy
            | Page::AddInstrument
            | Page::Analytics
            | Page::Curves
            | Page::Diffs
            | Page::Search
            | Page::Settings => None,
        };

        selected_context.or_else(|| {
            self.active_subject_context()
                .map(|active| InspectorContext::ActiveSubject {
                    subject: active.subject,
                    label: active.label,
                    tooltip: active.tooltip,
                })
        })
    }

    fn active_plot_request(&self) -> Option<PlotRequest> {
        let active = self.active_subject_context()?;
        let series_kind = match &active.subject {
            AnalysisSubject::WorkspacePortfolio(_) => TimeSeriesKind::PortfolioValue,
            AnalysisSubject::Fund(_)
            | AnalysisSubject::FundListing { .. }
            | AnalysisSubject::Holding { .. }
            | AnalysisSubject::Cash(_)
            | AnalysisSubject::SyntheticModel(_) => TimeSeriesKind::Price,
        };
        Some(PlotRequest::new(
            active.subject,
            series_kind,
            format!("{} {}", active.label, series_kind.as_str()),
        ))
    }

    fn active_source_summary(&self) -> Option<String> {
        let active = self.active_subject_context()?;
        let kind = self.active_data_kind();
        let (available, canonical) = mock_available_sources_for(&active.subject, kind);
        let resolved =
            resolve_source_selection(&available, &SourceSelection::Canonical, Some(canonical));
        Some(format!(
            "{} | {} | {}",
            kind.as_str(),
            resolved.effective_label(),
            resolved.status_label()
        ))
    }

    fn active_data_kind(&self) -> DataKind {
        match self.page {
            Page::Documents | Page::DocumentViewer => DataKind::Documents,
            Page::Holdings => DataKind::Holdings,
            Page::Dividends => DataKind::Distributions,
            Page::Charts => self
                .charts
                .active_plot
                .as_ref()
                .map(|request| data_kind_for_series(request.series_kind))
                .unwrap_or(DataKind::Price),
            Page::Analytics | Page::Compare | Page::Spreads => DataKind::Derived,
            Page::Curves => DataKind::Fx,
            Page::FundDetail
            | Page::Portfolio
            | Page::Etfs
            | Page::Hierarchy
            | Page::Exposure
            | Page::Alerts
            | Page::Jobs
            | Page::DataOperations
            | Page::Diffs
            | Page::Search
            | Page::Settings
            | Page::AddInstrument => DataKind::Facts,
        }
    }

    fn handle_table_keyboard(&mut self, ctx: &egui::Context) {
        let mut pending_page = None;
        let mut feedback = None;
        let mut pending_app_action = None;

        {
            let Some(snapshot) = self.snapshot.as_loaded_mut() else {
                return;
            };

            match self.page {
                Page::Portfolio => {
                    if portfolio::handle_keyboard(
                        ctx,
                        &snapshot.positions,
                        &snapshot.portfolio_summary.base_currency,
                        &mut snapshot.selected,
                        &mut self.portfolio,
                        &mut self.table_layouts,
                    ) {
                        pending_page = Some(Page::FundDetail);
                        feedback = Some(CommandOutcome::Navigated(Page::FundDetail).feedback());
                    }
                }
                Page::Etfs => {
                    if etfs::handle_keyboard(
                        ctx,
                        &snapshot.funds,
                        &mut snapshot.selected,
                        &mut self.etfs,
                        &mut self.table_layouts,
                    ) {
                        pending_page = Some(Page::FundDetail);
                        feedback = Some(CommandOutcome::Navigated(Page::FundDetail).feedback());
                    }
                }
                Page::Jobs => {
                    if jobs::handle_keyboard(
                        ctx,
                        &snapshot.scheduled_jobs,
                        &snapshot.job_runs,
                        &mut self.mock_job_message,
                        &mut self.jobs,
                    ) {
                        feedback = self.mock_job_message.clone();
                    }
                }
                Page::Documents => {
                    if documents::handle_keyboard(
                        ctx,
                        &snapshot.documents,
                        &snapshot.funds,
                        &mut snapshot.selected,
                        &mut self.documents,
                        &mut self.table_layouts,
                    ) {
                        pending_page = Some(Page::DocumentViewer);
                        feedback = Some(CommandOutcome::Navigated(Page::DocumentViewer).feedback());
                    }
                }
                Page::Alerts => {
                    if alerts::handle_keyboard(
                        ctx,
                        &snapshot.alerts,
                        &snapshot.funds,
                        &mut snapshot.selected,
                        &mut self.alerts,
                        &mut self.table_layouts,
                    ) {
                        feedback = Some("CMD: selected alert instrument".to_owned());
                    }
                }
                Page::Charts | Page::Compare | Page::Spreads => {
                    if let Some(action) =
                        charts_page::handle_keyboard(ctx, &mut self.charts, &snapshot.time_series)
                    {
                        pending_app_action = Some(map_chart_action(snapshot, action));
                    }
                }
                Page::DataOperations => {
                    if let Some((subject, label)) = data_operations::handle_keyboard(
                        ctx,
                        snapshot,
                        &mut self.data_operations,
                        &mut self.table_layouts,
                    ) {
                        let page = page_for_subject(&subject);
                        pending_app_action = Some(AppAction::Open(
                            NavigationEntry::new(subject, label.clone(), page).with_breadcrumbs(
                                vec![
                                    snapshot.workspace.name.clone(),
                                    "Data Operations".to_owned(),
                                    label,
                                ],
                            ),
                        ));
                    }
                }
                Page::FundDetail => {
                    if let Some(action) = fund_detail::handle_keyboard(
                        ctx,
                        snapshot,
                        &mut self.fund_detail,
                        &mut self.table_layouts,
                    ) {
                        pending_app_action = Some(match action {
                            fund_detail::FundDetailAction::Plot(request) => {
                                AppAction::Plot(request)
                            }
                            fund_detail::FundDetailAction::OpenSubject { subject, label } => {
                                let page = page_for_subject(&subject);
                                AppAction::Open(
                                    NavigationEntry::new(subject, label.clone(), page)
                                        .with_breadcrumbs(vec![
                                            snapshot.workspace.name.clone(),
                                            "Fund Detail".to_owned(),
                                            label,
                                        ]),
                                )
                            }
                            fund_detail::FundDetailAction::OpenDocumentIndex(index) => {
                                AppAction::OpenDocumentIndex(index)
                            }
                        });
                    }
                }
                Page::Exposure => {
                    exposure::handle_keyboard(
                        ctx,
                        &snapshot.exposures,
                        &mut self.exposure,
                        &mut self.table_layouts,
                    );
                }
                Page::Hierarchy
                | Page::AddInstrument
                | Page::Dividends
                | Page::Holdings
                | Page::Analytics
                | Page::Curves
                | Page::DocumentViewer
                | Page::Diffs
                | Page::Search
                | Page::Settings => {}
            }

            if let Some(page) = pending_page {
                if page == Page::FundDetail
                    && let Some(entry) = selected_navigation_entry(snapshot, page)
                {
                    self.navigation.open(entry);
                }
                self.page = page;
            }
        }
        if let Some(action) = pending_app_action {
            self.apply_app_action(action);
        }
        if let Some(feedback) = feedback {
            self.command_feedback = feedback;
        }
    }

    fn handle_shortcuts(&mut self, ui: &egui::Ui) {
        if consume_shortcut(ui.ctx(), egui::Key::K) {
            self.focus_command_next_frame = true;
            self.command_feedback = "CMD: focus command".to_owned();
        }
        if consume_shortcut(ui.ctx(), egui::Key::F) {
            self.page = Page::Search;
            self.search.focus_query_next_frame = true;
            self.command_feedback = "CMD: opened Search".to_owned();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Plus)
            || consume_shortcut(ui.ctx(), egui::Key::Equals)
        {
            self.zoom_in();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Minus) {
            self.zoom_out();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Num0) {
            self.reset_zoom();
        }
        if !ui.ctx().text_edit_focused() && consume_shortcut(ui.ctx(), egui::Key::C) {
            let outcome = self
                .copy_selected_payload()
                .or_else(|| self.copy_active_payload())
                .map(|payload| self.copy_to_clipboard(payload))
                .unwrap_or_else(|| CommandOutcome::Error("nothing selected to copy".to_owned()));
            self.command_feedback = outcome.feedback();
        }
        if ui
            .ctx()
            .input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::F5))
            || consume_shortcut(ui.ctx(), egui::Key::R)
        {
            self.refresh_data();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Num1) {
            self.page = Page::Portfolio;
            self.command_feedback = CommandOutcome::Navigated(Page::Portfolio).feedback();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Num2) {
            self.page = Page::Etfs;
            self.command_feedback = CommandOutcome::Navigated(Page::Etfs).feedback();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Num3) {
            self.page = Page::FundDetail;
            self.command_feedback = CommandOutcome::Navigated(Page::FundDetail).feedback();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Num4) {
            self.page = Page::Jobs;
            self.command_feedback = CommandOutcome::Navigated(Page::Jobs).feedback();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Num5) {
            self.page = Page::Alerts;
            self.command_feedback = CommandOutcome::Navigated(Page::Alerts).feedback();
        }
        if consume_shortcut(ui.ctx(), egui::Key::Num6) {
            self.page = Page::Documents;
            self.command_feedback = CommandOutcome::Navigated(Page::Documents).feedback();
        }
        if consume_shortcut(ui.ctx(), egui::Key::I) {
            self.layout.show_inspector = !self.layout.show_inspector;
            self.command_feedback =
                CommandOutcome::Toggled("inspector", self.layout.show_inspector).feedback();
        }
        if consume_shortcut(ui.ctx(), egui::Key::B) {
            self.layout.show_left_navigation = !self.layout.show_left_navigation;
            self.command_feedback =
                CommandOutcome::Toggled("left navigation", self.layout.show_left_navigation)
                    .feedback();
        }
        if ui
            .ctx()
            .input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Escape))
        {
            if self.cancel_active_edit() {
                self.command_feedback = "CMD: cancelled edit".to_owned();
            } else if self.clear_current_table_focus() {
                self.command_feedback = "CMD: cleared table focus".to_owned();
            } else {
                self.show_command_help = false;
                self.show_status_legend = false;
                if self.command_input.is_empty() {
                    self.command_feedback = "CMD: cleared message".to_owned();
                } else {
                    self.command_input.clear();
                    self.command_feedback = "CMD: cleared command".to_owned();
                }
            }
        }

        self.handle_table_keyboard(ui.ctx());
    }

    fn top_menu_bar(&mut self, ui: &mut egui::Ui) {
        egui::MenuBar::new().ui(ui, |ui| {
            ui.menu_button(TOP_MENU_LABELS[0], |ui| {
                self.menu_nav(ui, "New Workspace", Page::Settings);
                self.menu_nav(ui, "Open Workspace...", Page::Settings);
                self.menu_mock(ui, "Save View", "MOCK save current view");
                self.menu_mock(ui, "Import Portfolio CSV", "MOCK import portfolio CSV");
                self.menu_mock(
                    ui,
                    "Export Workspace Snapshot",
                    "MOCK export workspace snapshot",
                );
                self.menu_mock(ui, "Export Positions Table", "MOCK export positions table");
                ui.separator();
                self.menu_nav(ui, "Preferences", Page::Settings);
                if ui.button("Quit").clicked() {
                    ui.ctx().send_viewport_cmd(egui::ViewportCommand::Close);
                    ui.close();
                }
            });

            ui.menu_button(TOP_MENU_LABELS[1], |ui| {
                if ui.button("Copy").on_hover_text("Ctrl/Cmd+C").clicked() {
                    let outcome = self
                        .copy_selected_payload()
                        .map(|payload| self.copy_to_clipboard(payload))
                        .unwrap_or_else(|| {
                            CommandOutcome::Error("nothing selected to copy".to_owned())
                        });
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                if ui.button("Copy Active").clicked() {
                    let outcome = self
                        .copy_active_payload()
                        .map(|payload| self.copy_to_clipboard(payload))
                        .unwrap_or_else(|| {
                            CommandOutcome::Error("nothing active to copy".to_owned())
                        });
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                if ui.button("Copy Chart Row").clicked() {
                    let outcome = match self.charts.selected_point.as_ref() {
                        Some(point) => self
                            .copy_to_clipboard(CopyPayload::new(point.label(), point.copy_text())),
                        None => CommandOutcome::Error("no selected chart row".to_owned()),
                    };
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                ui.separator();
                if ui.button("Clear Selection").clicked() {
                    self.clear_current_table_selection();
                    self.command_feedback = "DONE clear current selection".to_owned();
                    ui.close();
                }
                if ui.button("Clear Chart Selection").clicked() {
                    self.charts.clear_chart_selection();
                    self.command_feedback = "CMD: cleared chart selection".to_owned();
                    ui.close();
                }
                if ui.button("Clear Override").clicked() {
                    let outcome = self.apply_command_action(CommandAction::ClearSelectedOverrides);
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                if ui.button("Clear All Overrides").clicked() {
                    let outcome = self.clear_all_overrides();
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
            });

            ui.menu_button(TOP_MENU_LABELS[2], |ui| {
                if ui
                    .checkbox(&mut self.layout.show_left_navigation, "Show Left Rail")
                    .clicked()
                {
                    self.command_feedback = "DONE toggle left navigation".to_owned();
                }
                if ui
                    .checkbox(&mut self.layout.show_inspector, "Show Inspector")
                    .clicked()
                {
                    self.command_feedback = "DONE toggle inspector".to_owned();
                }
                if ui
                    .checkbox(&mut self.layout.show_context_strip, "Show Context Strip")
                    .clicked()
                {
                    self.command_feedback = "DONE toggle context strip".to_owned();
                }
                if ui
                    .checkbox(&mut self.layout.show_status_bar, "Show Status Bar")
                    .clicked()
                {
                    self.command_feedback = "DONE toggle status bar".to_owned();
                }
                ui.separator();
                if ui.button("Compact Density").clicked() {
                    self.settings.density = settings::DensityPreference::Compact;
                    self.command_feedback = "SETTINGS: density changed to Compact".to_owned();
                    ui.close();
                }
                if ui.button("Comfortable Density").clicked() {
                    self.settings.density = settings::DensityPreference::Comfortable;
                    self.command_feedback = "SETTINGS: density changed to Comfortable".to_owned();
                    ui.close();
                }
                if ui.button("Dark Mode").clicked() {
                    self.settings.theme = settings::ThemePreference::Dark;
                    self.command_feedback = "SETTINGS: theme changed to Dark".to_owned();
                    ui.close();
                }
                ui.separator();
                if ui
                    .button("Zoom In")
                    .on_hover_text("Ctrl/Cmd++ or Ctrl/Cmd+=")
                    .clicked()
                {
                    self.zoom_in();
                    ui.close();
                }
                if ui.button("Zoom Out").on_hover_text("Ctrl/Cmd+-").clicked() {
                    self.zoom_out();
                    ui.close();
                }
                if ui
                    .button("Reset Zoom")
                    .on_hover_text("Ctrl/Cmd+0")
                    .clicked()
                {
                    self.reset_zoom();
                    ui.close();
                }
                if ui.button("Reset Layout").clicked() {
                    self.layout.reset();
                    self.command_feedback = "DONE reset layout".to_owned();
                    ui.close();
                }
                ui.separator();
                if ui.button("Show All Table Columns").clicked() {
                    self.table_layouts.show_all_tables();
                    self.command_feedback = "DONE show all table columns".to_owned();
                    ui.close();
                }
                if ui.button("Reset Table Columns").clicked() {
                    self.table_layouts.reset_all();
                    self.command_feedback = "DONE reset table columns".to_owned();
                    ui.close();
                }
            });

            ui.menu_button(TOP_MENU_LABELS[3], |ui| {
                if ui.button("Back").clicked() {
                    let outcome = self.go_back();
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                if ui.button("Forward").clicked() {
                    let outcome = self.go_forward();
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                if ui.button("Home").clicked() {
                    let outcome = self.go_home();
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                ui.separator();
                self.menu_nav(ui, "Search", Page::Search);
                self.menu_mock(
                    ui,
                    "Command Palette",
                    "CMD: use Ctrl/Cmd+K to focus command",
                );
                if ui.button("Open Selected").clicked() {
                    let outcome = self.apply_command_action(CommandAction::OpenSelected);
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                if ui.button("Open Active").clicked() {
                    let outcome = self.apply_command_action(CommandAction::OpenActive);
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                if ui.button("Open Pinned Inspector").clicked() {
                    let outcome = self.apply_command_action(CommandAction::OpenPinnedInspector);
                    self.command_feedback = outcome.feedback();
                    ui.close();
                }
                ui.separator();
                self.menu_nav(ui, "Portfolio", Page::Portfolio);
                self.menu_nav(ui, "ETFs / Instruments", Page::Etfs);
                self.menu_nav(ui, "Hierarchy", Page::Hierarchy);
                self.menu_nav(ui, "Fund Detail", Page::FundDetail);
                self.menu_nav(ui, "Documents", Page::Documents);
                self.menu_nav(ui, "Data Operations", Page::DataOperations);
                self.menu_nav(ui, "Jobs", Page::Jobs);
                self.menu_nav(ui, "Settings", Page::Settings);
            });

            ui.menu_button(TOP_MENU_LABELS[4], |ui| {
                if ui.button("Refresh All").clicked() {
                    self.refresh_data();
                    ui.close();
                }
                if ui.button("Refresh Active").clicked() {
                    self.refresh_data();
                    self.command_feedback = "CMD: refreshed active context".to_owned();
                    ui.close();
                }
                if ui.button("Refresh Data Operations").clicked() {
                    self.refresh_data();
                    ui.close();
                }
                if ui
                    .selectable_label(self.settings.data_mode == DataMode::Mock, "Use Mock Data")
                    .clicked()
                {
                    self.settings.data_mode = DataMode::Mock;
                    self.refresh_data();
                    ui.close();
                }
                if ui
                    .selectable_label(self.settings.data_mode == DataMode::Api, "Use API Data")
                    .clicked()
                {
                    self.settings.data_mode = DataMode::Api;
                    self.refresh_data();
                    ui.close();
                }
                if ui.button("Test Backend Connection").clicked() {
                    self.test_backend_connection();
                    ui.close();
                }
                self.menu_mock(ui, "Refresh Prices", "QUEUED price refresh");
                self.menu_mock(ui, "Refresh FX", "QUEUED FX refresh");
                self.menu_mock(ui, "Refresh Holdings", "QUEUED holdings refresh");
                self.menu_mock(ui, "Refresh Documents", "QUEUED document refresh");
                ui.separator();
                self.menu_mock(
                    ui,
                    "Source Selection",
                    "MOCK data sources are visible in tables",
                );
                self.menu_mock(
                    ui,
                    "Source Fallback / Provenance",
                    "MOCK provenance tags enabled",
                );
                self.menu_nav(ui, "Data Operations", Page::DataOperations);
                self.menu_nav(ui, "Market Data Plan", Page::DataOperations);
                self.menu_nav(ui, "Source Budgets", Page::DataOperations);
                self.menu_nav(ui, "Fetch Logs", Page::DataOperations);
                self.menu_nav(ui, "Manual Overrides", Page::Analytics);
                self.menu_nav(ui, "Diagnostics", Page::Analytics);
                self.menu_mock(
                    ui,
                    "Source Conflicts",
                    "MOCK source conflicts in diagnostics",
                );
                self.menu_nav(ui, "Changes", Page::Diffs);
                ui.separator();
                self.menu_run_job(ui, "Run Ingestion Jobs", "INGESTION_ALL");
                self.menu_nav(ui, "Scheduler Status", Page::DataOperations);
            });

            ui.menu_button(TOP_MENU_LABELS[5], |ui| {
                self.menu_nav(ui, "Charts", Page::Charts);
                self.menu_nav(ui, "Compare", Page::Compare);
                self.menu_nav(ui, "Spread", Page::Spreads);
                self.menu_nav(ui, "Exposure", Page::Exposure);
                self.menu_nav(ui, "Curves", Page::Curves);
                self.menu_nav(ui, "Analytics", Page::Analytics);
                ui.separator();
                self.menu_nav(ui, "Alerts", Page::Alerts);
                self.menu_nav(ui, "Jobs / Scheduler", Page::Jobs);
                self.menu_nav(ui, "Documents", Page::Documents);
                self.menu_nav(ui, "Add / Resolve Instrument", Page::AddInstrument);
                ui.separator();
                self.menu_mock(ui, "Run Mock Regression", "QUEUED mock regression");
                self.menu_mock(
                    ui,
                    "Explain Selected Number",
                    "MOCK explain selected number",
                );
                self.menu_run_job(ui, "Run Selected Job", "SELECTED_JOB");
                self.menu_run_job(ui, "Run Price Ingestion", "PRICE_INGESTION");
                self.menu_run_job(ui, "Run FX Ingestion", "FX_INGESTION");
            });

            ui.menu_button(TOP_MENU_LABELS[6], |ui| {
                self.menu_nav(ui, "Portfolio", Page::Portfolio);
                self.menu_nav(ui, "Data Operations", Page::DataOperations);
                self.menu_nav(ui, "Jobs / Scheduler", Page::Jobs);
                self.menu_nav(ui, "Search", Page::Search);
                ui.separator();
                if ui
                    .add_enabled(
                        !self.layout.show_inspector,
                        egui::Button::new("Show Inspector"),
                    )
                    .clicked()
                {
                    self.layout.show_inspector = true;
                    self.command_feedback = "DONE show inspector".to_owned();
                    ui.close();
                }
                if ui
                    .add_enabled(
                        self.layout.show_inspector,
                        egui::Button::new("Hide Inspector"),
                    )
                    .clicked()
                {
                    self.layout.show_inspector = false;
                    self.command_feedback = "DONE hide inspector".to_owned();
                    ui.close();
                }
                if ui.button("Reset Layout").clicked() {
                    self.layout.reset();
                    self.command_feedback = "DONE reset layout".to_owned();
                    ui.close();
                }
                ui.separator();
                if ui
                    .checkbox(&mut self.layout.show_left_navigation, "Left Rail")
                    .clicked()
                {
                    self.command_feedback = "DONE toggle left navigation".to_owned();
                }
                if ui
                    .checkbox(&mut self.layout.show_inspector, "Inspector")
                    .clicked()
                {
                    self.command_feedback = "DONE toggle inspector".to_owned();
                }
                if ui
                    .checkbox(&mut self.layout.show_context_strip, "Context Strip")
                    .clicked()
                {
                    self.command_feedback = "DONE toggle context strip".to_owned();
                }
                if ui
                    .checkbox(&mut self.layout.show_status_bar, "Status Bar")
                    .clicked()
                {
                    self.command_feedback = "DONE toggle status bar".to_owned();
                }
            });

            ui.menu_button(TOP_MENU_LABELS[7], |ui| {
                if ui.button("Keyboard Shortcuts").clicked() {
                    self.show_command_help = true;
                    self.command_feedback = CommandOutcome::Help.feedback();
                    ui.close();
                }
                if ui.button("Command Examples").clicked() {
                    self.show_command_help = true;
                    self.command_feedback = CommandOutcome::Help.feedback();
                    ui.close();
                }
                if ui.button("Chart Commands").clicked() {
                    self.show_command_help = true;
                    self.command_feedback = "CMD: chart commands opened".to_owned();
                    ui.close();
                }
                if ui.button("Navigation Commands").clicked() {
                    self.show_command_help = true;
                    self.command_feedback = "CMD: navigation commands opened".to_owned();
                    ui.close();
                }
                if ui.button("Override Behaviour").clicked() {
                    self.show_command_help = true;
                    self.command_feedback = "CMD: override behaviour opened".to_owned();
                    ui.close();
                }
                if ui.button("Data Status Legend").clicked() {
                    self.show_status_legend = true;
                    self.command_feedback = CommandOutcome::Legend.feedback();
                    ui.close();
                }
                if ui.button("About").clicked() {
                    self.show_about = true;
                    self.command_feedback = "CMD: opened About".to_owned();
                    ui.close();
                }
            });
        });
    }

    fn menu_nav(&mut self, ui: &mut egui::Ui, label: &str, page: Page) {
        if ui.button(label).clicked() {
            self.page = page;
            self.command_feedback = format!("DONE open {}", page.label());
            ui.close();
        }
    }

    fn menu_mock(&mut self, ui: &mut egui::Ui, label: &str, feedback: &str) {
        if ui.button(label).clicked() {
            self.command_feedback = feedback.to_owned();
            ui.close();
        }
    }

    fn menu_run_job(&mut self, ui: &mut egui::Ui, label: &str, job_name: &str) {
        if ui.button(label).clicked() {
            self.page = Page::Jobs;
            let message = format!("QUEUED mock job {job_name}");
            self.mock_job_message = Some(message.clone());
            self.command_feedback = message;
            ui.close();
        }
    }

    fn top_toolbar(&mut self, ui: &mut egui::Ui) {
        let mut run_command = false;
        let mut search_clicked = false;
        let mut nav_action = None;
        let workspaces = self.provider.load_workspaces();

        ui.horizontal(|ui| {
            if ui
                .add_enabled(self.navigation.can_go_back(), egui::Button::new("<"))
                .on_hover_text("Back")
                .clicked()
            {
                nav_action = Some(CommandAction::Back);
            }
            if ui
                .add_enabled(self.navigation.can_go_forward(), egui::Button::new(">"))
                .on_hover_text("Forward")
                .clicked()
            {
                nav_action = Some(CommandAction::Forward);
            }
            if ui.button("Home").on_hover_text("Workspace home").clicked() {
                nav_action = Some(CommandAction::Home);
            }
            let workspace_label = workspaces
                .iter()
                .find(|workspace| workspace.id == self.selected_workspace_id)
                .map(|workspace| workspace.name.as_str())
                .unwrap_or("No workspace");
            egui::ComboBox::from_id_salt("toolbar_workspace_select")
                .width(132.0)
                .selected_text(format!("WS: {workspace_label}"))
                .show_ui(ui, |ui| {
                    for workspace in &workspaces {
                        ui.selectable_value(
                            &mut self.selected_workspace_id,
                            workspace.id.clone(),
                            workspace.name.as_str(),
                        );
                    }
                })
                .response
                .on_hover_text("Workspace context. Operational status remains in the status bar.");
            let command_hint = if command_input_has_focus(ui.ctx()) || self.focus_command_next_frame
            {
                "Enter run | Up/Down history | / VUSA search"
            } else {
                "ticker / page / command / search"
            };
            let trailing_width =
                COMMAND_GO_WIDTH + COMMAND_SEARCH_WIDTH + ui.spacing().item_spacing.x;
            let command_width = (ui.available_width() - trailing_width).max(120.0);
            let command_response = ui.add_sized(
                [command_width, metrics::ROW_HEIGHT_COMPACT],
                egui::TextEdit::singleline(&mut self.command_input)
                    .id(egui::Id::new(COMMAND_INPUT_ID))
                    .hint_text(command_hint),
            );
            if self.focus_command_next_frame {
                command_response.request_focus();
                self.focus_command_next_frame = false;
            }
            if command_response.has_focus()
                && ui
                    .input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp))
                && let Some(command) = self.command_history.previous()
            {
                self.command_input = command.to_owned();
            }
            if command_response.has_focus()
                && ui.input_mut(|input| {
                    input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)
                })
                && let Some(command) = self.command_history.next()
            {
                self.command_input = command.to_owned();
            }
            if command_response.lost_focus()
                && ui.input(|input| input.key_pressed(egui::Key::Enter))
            {
                run_command = true;
            }
            if ui
                .add_sized(
                    [COMMAND_GO_WIDTH, metrics::ROW_HEIGHT_COMPACT],
                    egui::Button::new("Go"),
                )
                .on_hover_text("Run command")
                .clicked()
            {
                run_command = true;
            }
            if ui
                .add_sized(
                    [COMMAND_SEARCH_WIDTH, metrics::ROW_HEIGHT_COMPACT],
                    egui::Button::new("Search"),
                )
                .on_hover_text("Open Search with the command text as query")
                .clicked()
            {
                search_clicked = true;
            }
        });

        if let Some(action) = nav_action {
            let outcome = self.apply_command_action(action);
            self.command_feedback = outcome.feedback();
        }
        if run_command {
            self.execute_command();
        }
        if search_clicked {
            let query = self.command_input.trim().to_owned();
            let outcome = self.apply_command_action(CommandAction::Search(query));
            self.command_feedback = outcome.feedback();
        }
    }

    fn context_strip(&mut self, ui: &mut egui::Ui) {
        let active_context = self.active_subject_context();
        let active_kind = self.active_data_kind();
        ui.horizontal(|ui| {
            ui.monospace(self.navigation.breadcrumb_label())
                .on_hover_text("Breadcrumbs for the navigation stack");
            ui.separator();

            match self.snapshot.as_loaded_mut() {
                Some(snapshot) => {
                    if let Some(active) = active_context.as_ref() {
                        ui.label(egui::RichText::new("ACTIVE").weak());
                        ui.monospace(&active.label)
                            .on_hover_text(&active.tooltip)
                            .on_hover_cursor(egui::CursorIcon::Help);
                        let (available, canonical) =
                            mock_available_sources_for(&active.subject, active_kind);
                        let resolved = resolve_source_selection(
                            &available,
                            &SourceSelection::Canonical,
                            Some(canonical),
                        );
                        style::source_resolution_badge(ui, &resolved, active_kind)
                            .on_hover_cursor(egui::CursorIcon::Help);
                        ui.separator();
                    }
                    ui.label(egui::RichText::new("SELECTED").weak());
                    ui.monospace(snapshot.selected_context_label())
                        .on_hover_text(snapshot.selected_subject_tooltip())
                        .on_hover_cursor(egui::CursorIcon::Help);
                    if self.inspector.is_pinned()
                        && let Some(pinned) = self.inspector.pinned_context.as_ref()
                    {
                        ui.separator();
                        ui.label(egui::RichText::new("PINNED").weak());
                        ui.monospace(format!("{}: {}", pinned.kind_label(), pinned.label()))
                            .on_hover_text(pinned.tooltip())
                            .on_hover_cursor(egui::CursorIcon::Help);
                    }
                    if !self.layout.show_inspector {
                        ui.separator();
                        style::badge(
                            ui,
                            "INSPECTOR HIDDEN",
                            if self.inspector.is_pinned() {
                                style::BadgeTone::Warning
                            } else {
                                style::BadgeTone::Neutral
                            },
                            Some(
                                "The inspector is hidden. A pinned context remains pinned and will reappear unchanged."
                                    .to_owned(),
                            ),
                        );
                    }
                    ui.separator();
                    ui.label(egui::RichText::new("PAGE").weak());
                    ui.monospace(self.page.label());
                    if !snapshot.selected.is_empty() && ui.button("Clear").clicked() {
                        snapshot.selected.clear();
                    }
                }
                None => {
                    ui.monospace("Breadcrumbs: - | Selected: - | Page: -");
                }
            }
        });
    }

    fn status_bar(&self, ui: &mut egui::Ui) {
        let running_jobs = self
            .snapshot
            .as_loaded()
            .map(|snapshot| {
                snapshot
                    .job_runs
                    .iter()
                    .filter(|run| run.status == crate::domain::JobStatus::Running)
                    .count()
            })
            .unwrap_or_default();
        let last_refresh = self
            .snapshot
            .as_loaded()
            .map(|snapshot| compact_last_refresh_label(snapshot.last_refresh_at.as_str()))
            .unwrap_or("-");
        let (workspace, mode, data_status, base_currency) = self
            .snapshot
            .as_loaded()
            .map(|snapshot| {
                (
                    snapshot.workspace.name.as_str(),
                    snapshot.data_mode.as_str(),
                    snapshot.data_status.as_str(),
                    snapshot.portfolio_summary.base_currency.as_str(),
                )
            })
            .unwrap_or(("-", "-", "-", "-"));
        let command_has_focus = command_input_has_focus(ui.ctx());

        ui.horizontal(|ui| {
            ui.monospace(format!("WS: {workspace}"));
            ui.separator();
            ui.label(egui::RichText::new("Mode").weak());
            if mode.eq_ignore_ascii_case("mock") {
                style::mock_badge(ui);
            } else {
                style::status_badge(ui, mode);
            }
            ui.separator();
            ui.label(egui::RichText::new("Data").weak());
            style::status_badge(ui, data_status);
            ui.separator();
            ui.label(egui::RichText::new("API").weak());
            style::status_badge(ui, self.api_runtime.connection.as_str());
            ui.separator();
            ui.monospace(format!("Base: {base_currency}"));
            ui.separator();
            ui.monospace(format!("Last refresh: {last_refresh}"));
            ui.separator();
            ui.monospace(format!("{running_jobs} jobs running"));
            ui.separator();
            ui.monospace(self.settings.source_policy.label());
            ui.separator();
            ui.monospace(format!("Zoom: {}%", self.settings.zoom_percent()))
                .on_hover_text("View zoom. Use View menu or Ctrl/Cmd + +, -, 0.");
            ui.separator();
            let inspector_status = if !self.layout.show_inspector {
                if self.inspector.is_pinned() {
                    "Inspector: HIDDEN/PINNED"
                } else {
                    "Inspector: HIDDEN"
                }
            } else if self.inspector.is_pinned() {
                "Inspector: PINNED"
            } else {
                "Inspector: FOLLOW"
            };
            ui.monospace(inspector_status).on_hover_text(
                "Inspector visibility and follow/pin state. Toggle from Window or Ctrl/Cmd+I.",
            );
            if !command_has_focus
                && !self.command_feedback.is_empty()
                && self.command_feedback != COMMAND_SUGGESTIONS
            {
                ui.separator();
                ui.label(egui::RichText::new(&self.command_feedback).weak());
            }
        });
    }

    fn right_inspector(&mut self, ui: &mut egui::Ui) {
        let follow_context = self.resolve_follow_inspector_context();
        self.inspector.set_follow_context(follow_context);
        let context = self.inspector.current_context().cloned();
        let mut action = None;

        ui.vertical(|ui| {
            action = self.inspector_header(ui, context.as_ref());
            ui.separator();
            egui::ScrollArea::vertical()
                .auto_shrink(false)
                .show(ui, |ui| {
                    let Some(snapshot) = self.snapshot.as_loaded() else {
                        style::state_message(ui, "EMPTY", "No data is loaded for inspection.");
                        return;
                    };

                    ui.horizontal_wrapped(|ui| {
                        style::badge(
                            ui,
                            self.page.label().to_ascii_uppercase(),
                            style::BadgeTone::Neutral,
                            Some("Current page; inspector context can remain pinned.".to_owned()),
                        );
                        ui.monospace(&snapshot.workspace.name);
                    });
                    ui.add_space(metrics::SPACE_1);

                    if let Some(context) = context.as_ref() {
                        if action.is_none() {
                            action = Self::render_inspector_context(
                                ui,
                                snapshot,
                                context,
                                &self.portfolio.overrides,
                            );
                        }
                    } else {
                        style::state_message(
                            ui,
                            "EMPTY",
                            "Select a row to inspect it, or open a subject to show active context.",
                        );
                    }
                });
        });

        if let Some(action) = action {
            self.apply_inspector_detail_action(action);
        }
    }

    fn inspector_header(
        &mut self,
        ui: &mut egui::Ui,
        context: Option<&InspectorContext>,
    ) -> Option<InspectorDetailAction> {
        let mut action = None;
        ui.horizontal(|ui| {
            style::section_header(ui, "Inspector");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if crate::ui::actions::action_button(
                    ui,
                    "Hide",
                    "Collapse the inspector. Pinned context is retained.",
                )
                .clicked()
                {
                    self.layout.show_inspector = false;
                    action = Some(InspectorDetailAction::Feedback(
                        "INSPECTOR: hidden".to_owned(),
                    ));
                }
                match self.inspector.mode {
                    InspectorMode::FollowSelection => {
                        if crate::ui::actions::action_button_enabled(
                            ui,
                            context.is_some(),
                            "Pin",
                            "Pin the current inspector context so row selection no longer changes it.",
                        )
                        .clicked()
                            && self.inspector.pin_current()
                        {
                            action = self.inspector.pinned_context.as_ref().map(|pinned| {
                                InspectorDetailAction::Feedback(format!(
                                    "INSPECTOR: pinned {}",
                                    pinned.label()
                                ))
                            });
                        }
                    }
                    InspectorMode::Pinned => {
                        if crate::ui::actions::action_button(
                            ui,
                            "Unpin",
                            "Return to following selection.",
                        )
                        .clicked()
                        {
                            self.inspector.unpin();
                            action = Some(InspectorDetailAction::Feedback(
                                "INSPECTOR: following selection".to_owned(),
                            ));
                        }
                        if crate::ui::actions::action_button_enabled(
                            ui,
                            self.inspector.pinned_context.is_some(),
                            "Open",
                            "Open the pinned context where an in-app target exists.",
                        )
                        .clicked()
                            && let Some(context) = self.inspector.pinned_context.clone()
                        {
                            action = Some(InspectorDetailAction::Open(context));
                        }
                    }
                }
            });
        });
        ui.horizontal_wrapped(|ui| {
            match self.inspector.mode {
                InspectorMode::FollowSelection => {
                    style::badge(
                        ui,
                        "FOLLOW",
                        style::BadgeTone::Info,
                        Some(
                            "Following selection: single-clicking rows updates this panel."
                                .to_owned(),
                        ),
                    );
                }
                InspectorMode::Pinned => {
                    let label = self
                        .inspector
                        .pinned_context
                        .as_ref()
                        .map(|context| {
                            format!("PINNED {}", context.kind_label().to_ascii_uppercase())
                        })
                        .unwrap_or_else(|| "PINNED".to_owned());
                    style::badge(
                        ui,
                        label,
                        style::BadgeTone::Warning,
                        Some(
                            "Pinned: this panel stays fixed while you inspect other rows."
                                .to_owned(),
                        ),
                    );
                }
            }
            if let Some(context) = context {
                style::badge(
                    ui,
                    context.kind_label().to_ascii_uppercase(),
                    style::BadgeTone::Neutral,
                    Some(context.tooltip()),
                );
                ui.monospace(context.label())
                    .on_hover_text(context.tooltip())
                    .on_hover_cursor(egui::CursorIcon::Help);
            }
        });
        action
    }

    fn render_inspector_context(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        context: &InspectorContext,
        overrides: &[crate::compute::portfolio::PositionOverride],
    ) -> Option<InspectorDetailAction> {
        match context {
            InspectorContext::PortfolioPosition { row_index, .. } => {
                let Some(position) = snapshot.positions.get(*row_index) else {
                    ui.label("Pinned position is no longer available in the snapshot.");
                    return None;
                };
                let row_override_count = overrides
                    .iter()
                    .filter(|position_override| position_override.listing_id == position.listing_id)
                    .count();
                ui.label(egui::RichText::new("Position").strong());
                ui.monospace(format!("{} | {}", position.ticker, position.isin));
                ui.monospace(format!("Units: {:.4}", position.units));
                ui.monospace(format!(
                    "Price: {}",
                    crate::format::fmt_money(&position.listing_currency, position.price)
                ));
                ui.monospace(format!(
                    "Value: {}",
                    crate::format::fmt_money(
                        &snapshot.portfolio_summary.base_currency,
                        position.market_value,
                    )
                ));
                ui.monospace(format!(
                    "Weight: {}",
                    crate::format::fmt_percent(position.portfolio_weight_pct)
                ));
                ui.monospace(format!(
                    "Income: {}",
                    crate::format::fmt_money(
                        &snapshot.portfolio_summary.base_currency,
                        position.projected_income,
                    )
                ));
                ui.horizontal_wrapped(|ui| {
                    style::freshness_badge(ui, position.freshness);
                    style::source_badge(ui, &position.source);
                });
                ui.horizontal_wrapped(|ui| {
                    ui.label(egui::RichText::new("Overrides").weak());
                    if row_override_count == 0 {
                        ui.monospace("-");
                    } else {
                        style::manual_badge(ui);
                        ui.monospace(row_override_count.to_string());
                    }
                });
                let trace = crate::compute::explain::market_value_trace(
                    position,
                    &snapshot.portfolio_summary.base_currency,
                    row_override_count,
                );
                ui.separator();
                ui.monospace("Market Value = Units x Price");
                ui.monospace(format!(
                    "{} x {} -> {} | {}",
                    trace
                        .dependencies
                        .first()
                        .map(|dependency| dependency.value_used.as_str())
                        .unwrap_or("-"),
                    trace
                        .dependencies
                        .get(1)
                        .map(|dependency| dependency.value_used.as_str())
                        .unwrap_or("-"),
                    crate::format::fmt_money(&trace.unit, trace.value),
                    if row_override_count > 0 {
                        "DERIVED"
                    } else {
                        "SEED"
                    }
                ));
                ui.separator();
                let mut action = None;
                ui.horizontal_wrapped(|ui| {
                    if crate::ui::actions::action_button(ui, "Open", "Open this position.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Open(context.clone()));
                    }
                    if crate::ui::actions::action_button(ui, "Plot Price", "Plot listing price.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Plot(PlotRequest::new(
                            AnalysisSubject::FundListing {
                                fund_id: position.fund_id.clone(),
                                listing_id: position.listing_id.clone(),
                            },
                            TimeSeriesKind::Price,
                            format!("{} Price", position.ticker),
                        )));
                    }
                    if crate::ui::actions::action_button(ui, "Plot Value", "Plot market value.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Plot(PlotRequest::new(
                            AnalysisSubject::FundListing {
                                fund_id: position.fund_id.clone(),
                                listing_id: position.listing_id.clone(),
                            },
                            TimeSeriesKind::MarketValue,
                            format!("{} Market value", position.ticker),
                        )));
                    }
                    if crate::ui::actions::action_button(ui, "Explain", "Open analytics explain.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Navigate(Page::Analytics));
                    }
                    if crate::ui::actions::action_button_enabled(
                        ui,
                        row_override_count > 0,
                        "Clear Overrides",
                        "Clear local manual overrides on this position row.",
                    )
                    .clicked()
                    {
                        action = Some(InspectorDetailAction::ClearRowOverrides(
                            position.listing_id.clone(),
                        ));
                    }
                    if crate::ui::actions::action_button(ui, "Copy", "Copy row summary.").clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            format!("row {}", position.ticker),
                            portfolio_row_copy_text(
                                position,
                                &snapshot.portfolio_summary.base_currency,
                            ),
                        )));
                    }
                });
                action
            }
            InspectorContext::Fund { subject, .. }
            | InspectorContext::FundListing { subject, .. }
            | InspectorContext::ActiveSubject { subject, .. } => {
                Self::render_subject_inspector(ui, snapshot, context, subject, overrides)
            }
            InspectorContext::Distribution { row_index, .. } => {
                let Some(distribution) = snapshot.distributions.get(*row_index) else {
                    ui.label("Pinned distribution is no longer available in the snapshot.");
                    return None;
                };
                ui.label(egui::RichText::new("Distribution").strong());
                ui.monospace(format!(
                    "{} | ex {} | pay {}",
                    distribution.ticker, distribution.ex_date, distribution.payment_date
                ));
                ui.monospace(format!(
                    "Amount: {}",
                    crate::format::fmt_money(&distribution.currency, distribution.amount)
                ));
                ui.horizontal_wrapped(|ui| {
                    style::status_badge(ui, &distribution.status);
                    style::source_badge(ui, &distribution.source);
                });
                ui.separator();
                let mut action = None;
                ui.horizontal_wrapped(|ui| {
                    if crate::ui::actions::action_button(ui, "Open Fund", "Open affected fund.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Open(context.clone()));
                    }
                    if crate::ui::actions::action_button(
                        ui,
                        "Plot Distributions",
                        "Plot distribution series for this subject.",
                    )
                    .clicked()
                    {
                        action = Some(InspectorDetailAction::Plot(PlotRequest::new(
                            context.subject().cloned().unwrap_or_else(|| {
                                AnalysisSubject::Fund(distribution.fund_id.clone())
                            }),
                            TimeSeriesKind::Distribution,
                            format!("{} Distribution", distribution.ticker),
                        )));
                    }
                    if crate::ui::actions::action_button(ui, "Copy", "Copy distribution summary.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            format!("distribution {}", distribution.ticker),
                            [
                                distribution.ticker.clone(),
                                distribution.ex_date.clone(),
                                distribution.payment_date.clone(),
                                distribution.amount.to_string(),
                                distribution.currency.clone(),
                                distribution.source.clone(),
                            ]
                            .join("\t"),
                        )));
                    }
                });
                action
            }
            InspectorContext::Holding { row_index, .. } => {
                let Some(holding) = snapshot.holdings.get(*row_index) else {
                    ui.label("Pinned holding is no longer available in the snapshot.");
                    return None;
                };
                ui.label(egui::RichText::new("Holding").strong());
                ui.monospace(format!("{} | {}", holding.ticker, holding.company));
                ui.monospace(format!("Country: {}", holding.country));
                ui.monospace(format!("Sector: {}", holding.sector));
                ui.monospace(format!(
                    "Weight: {}",
                    crate::format::fmt_percent(holding.weight_pct)
                ));
                ui.monospace(format!("Source ETF: {}", holding.source_etf));
                ui.horizontal_wrapped(|ui| {
                    ui.monospace(&holding.as_of_date);
                    style::source_badge(ui, &holding.source);
                });
                ui.separator();
                let mut action = None;
                ui.horizontal_wrapped(|ui| {
                    if crate::ui::actions::action_button(
                        ui,
                        "Open Source ETF",
                        "Open the ETF/listing that supplied this holding.",
                    )
                    .clicked()
                        && let Some(subject) = subject_for_ticker(snapshot, &holding.source_etf)
                    {
                        action = Some(InspectorDetailAction::Open(
                            InspectorContext::ActiveSubject {
                                subject,
                                label: holding.source_etf.clone(),
                                tooltip: "Open holding source ETF".to_owned(),
                            },
                        ));
                    }
                    if crate::ui::actions::action_button(ui, "Copy", "Copy holding summary.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            format!("holding {}", holding.ticker),
                            [
                                holding.ticker.clone(),
                                holding.company.clone(),
                                holding.country.clone(),
                                holding.sector.clone(),
                                holding.source_etf.clone(),
                                holding.source.clone(),
                            ]
                            .join("\t"),
                        )));
                    }
                });
                action
            }
            InspectorContext::DocumentSnapshot { row_index, .. } => {
                Self::render_document_inspector(ui, snapshot, context, *row_index)
            }
            InspectorContext::Alert { row_index, .. } => {
                Self::render_alert_inspector(ui, snapshot, context, *row_index)
            }
            InspectorContext::ScheduledJob { row_index, .. } => {
                Self::render_scheduled_job_inspector(ui, snapshot, *row_index)
            }
            InspectorContext::JobRun { row_index, .. } => {
                Self::render_job_run_inspector(ui, snapshot, *row_index)
            }
            InspectorContext::ReadinessStage { row_index, .. } => {
                Self::render_readiness_stage_inspector(ui, snapshot, *row_index)
            }
            InspectorContext::MarketDataPlanItem { row_index, .. } => {
                Self::render_market_data_plan_inspector(ui, snapshot, context, *row_index)
            }
            InspectorContext::SourceBudget { row_index, .. } => {
                Self::render_source_budget_inspector(ui, snapshot, *row_index)
            }
            InspectorContext::FetchLog { row_index, .. } => {
                Self::render_fetch_log_inspector(ui, snapshot, *row_index)
            }
            InspectorContext::ConstituentReadiness { row_index, .. } => {
                Self::render_constituent_readiness_inspector(ui, snapshot, context, *row_index)
            }
            InspectorContext::DataDiagnosticIssue { row_index, .. } => {
                Self::render_data_diagnostic_inspector(ui, snapshot, *row_index)
            }
            InspectorContext::TableRow {
                table_id, details, ..
            } => {
                ui.label(egui::RichText::new("Table row").strong());
                ui.monospace(table_id);
                for line in details.lines() {
                    ui.label(line);
                }
                ui.separator();
                crate::ui::actions::action_button(ui, "Copy", "Copy visible row details.")
                    .clicked()
                    .then(|| {
                        InspectorDetailAction::Copy(CopyPayload::new(
                            context.label().to_owned(),
                            details.clone(),
                        ))
                    })
            }
            InspectorContext::ChartSeries {
                subject,
                series_id,
                series_kind,
                unit,
                source,
                status,
                role,
                ..
            } => {
                ui.label(egui::RichText::new("Chart series").strong());
                ui.monospace(format!(
                    "{} | {}",
                    charts_page::subject_label(snapshot, subject),
                    series_kind
                ));
                ui.monospace(format!("Series ID: {series_id}"))
                    .on_hover_text(series_id)
                    .on_hover_cursor(egui::CursorIcon::Help);
                ui.horizontal_wrapped(|ui| {
                    ui.monospace(role);
                    ui.monospace(unit);
                    style::status_badge(ui, status);
                    style::source_badge(ui, source);
                });
                ui.separator();
                let mut action = None;
                ui.horizontal_wrapped(|ui| {
                    if crate::ui::actions::action_button(
                        ui,
                        "Open Subject",
                        "Open the chart subject.",
                    )
                    .clicked()
                    {
                        action = Some(InspectorDetailAction::Open(context.clone()));
                    }
                    if crate::ui::actions::action_button(ui, "Copy", "Copy chart label.").clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            context.label().to_owned(),
                            context.label().to_owned(),
                        )));
                    }
                });
                action
            }
            InspectorContext::ChartPoint {
                subject,
                series_kind,
                date,
                value,
                raw_value,
                unit,
                raw_unit,
                source,
                status,
                value_mode,
                ..
            } => {
                ui.label(egui::RichText::new("Chart point").strong());
                ui.monospace(format!(
                    "{} | {} | {}",
                    charts_page::subject_label(snapshot, subject),
                    series_kind,
                    date
                ));
                ui.monospace(format!("Display: {value} {unit}"));
                ui.monospace(format!("Raw: {raw_value} {raw_unit}"));
                ui.monospace(format!("Value mode: {value_mode}"));
                ui.horizontal_wrapped(|ui| {
                    style::status_badge(ui, status);
                    style::source_badge(ui, source);
                });
                ui.separator();
                let mut action = None;
                ui.horizontal_wrapped(|ui| {
                    if crate::ui::actions::action_button(ui, "Open Subject", "Open point subject.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Open(context.clone()));
                    }
                    if crate::ui::actions::action_button(ui, "Copy Value", "Copy point value.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            format!("{} {}", context.label(), date),
                            value.clone(),
                        )));
                    }
                    if crate::ui::actions::action_button(ui, "Copy Raw", "Copy raw source value.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            format!("raw {} {}", context.label(), date),
                            raw_value.clone(),
                        )));
                    }
                    if crate::ui::actions::action_button(ui, "Copy Row", "Copy point row.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            context.label().to_owned(),
                            [
                                date.clone(),
                                context.label().to_owned(),
                                value.clone(),
                                unit.clone(),
                                raw_value.clone(),
                                raw_unit.clone(),
                                source.clone(),
                                status.clone(),
                                series_kind.clone(),
                                value_mode.clone(),
                            ]
                            .join("\t"),
                        )));
                    }
                    if crate::ui::actions::action_button(ui, "Copy Source", "Copy point source.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            format!("source {} {}", context.label(), date),
                            source.clone(),
                        )));
                    }
                });
                action
            }
            InspectorContext::ChartWorkspace { subject, mode, .. } => {
                ui.label(egui::RichText::new("Chart workspace").strong());
                ui.monospace(format!("Mode: {mode}"));
                ui.monospace(format!(
                    "Subject: {}",
                    charts_page::subject_label(snapshot, subject)
                ));
                ui.separator();
                let mut action = None;
                ui.horizontal_wrapped(|ui| {
                    if crate::ui::actions::action_button(ui, "Open Subject", "Open chart subject.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Open(context.clone()));
                    }
                    if crate::ui::actions::action_button(ui, "Copy", "Copy chart workspace label.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            context.label().to_owned(),
                            context.label().to_owned(),
                        )));
                    }
                });
                action
            }
            InspectorContext::ChartSpread {
                left_label,
                right_label,
                coverage,
                left_source,
                right_source,
                status,
                ..
            } => {
                ui.label(egui::RichText::new("Spread").strong());
                ui.monospace(format!("{left_label} - {right_label}"));
                ui.monospace(format!("Coverage: {coverage}"));
                ui.horizontal_wrapped(|ui| {
                    style::derived_badge(ui);
                    style::status_badge(ui, status);
                });
                ui.monospace(format!("Source A: {left_source}"));
                ui.monospace(format!("Source B: {right_source}"));
                ui.separator();
                let mut action = None;
                ui.horizontal_wrapped(|ui| {
                    if crate::ui::actions::action_button(
                        ui,
                        "Compare",
                        "Switch to Compare page mode.",
                    )
                    .clicked()
                    {
                        action = Some(InspectorDetailAction::Navigate(Page::Charts));
                    }
                    if crate::ui::actions::action_button(ui, "Copy", "Copy spread summary.")
                        .clicked()
                    {
                        action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                            context.label().to_owned(),
                            [
                                left_label.clone(),
                                right_label.clone(),
                                coverage.clone(),
                                left_source.clone(),
                                right_source.clone(),
                                status.clone(),
                            ]
                            .join("\t"),
                        )));
                    }
                });
                action
            }
        }
    }

    fn render_subject_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        context: &InspectorContext,
        subject: &AnalysisSubject,
        overrides: &[crate::compute::portfolio::PositionOverride],
    ) -> Option<InspectorDetailAction> {
        ui.label(egui::RichText::new("Subject").strong());
        let mut plot_subject = None;
        match subject {
            AnalysisSubject::WorkspacePortfolio(workspace_id) => {
                ui.monospace(format!("Portfolio: {}", snapshot.workspace.name));
                ui.monospace(format!("Workspace ID: {workspace_id}"));
                ui.monospace(format!(
                    "Value: {}",
                    crate::format::fmt_money(
                        &snapshot.portfolio_summary.base_currency,
                        snapshot.portfolio_summary.total_value,
                    )
                ));
                ui.horizontal_wrapped(|ui| {
                    style::status_badge(ui, snapshot.data_status.as_str());
                    style::mock_badge(ui);
                });
                plot_subject = Some((subject.clone(), TimeSeriesKind::PortfolioValue, "Value"));
            }
            AnalysisSubject::Fund(fund_id) | AnalysisSubject::FundListing { fund_id, .. } => {
                let Some(fund) = snapshot.find_fund_by_id(fund_id) else {
                    ui.label("Subject fund is not present in the current snapshot.");
                    return None;
                };
                ui.strong(compact_fund_label(fund));
                ui.label(&fund.name);
                ui.monospace(&fund.isin);
                if let AnalysisSubject::FundListing { listing_id, .. } = subject {
                    if let Some(listing) = fund
                        .listings
                        .iter()
                        .find(|listing| listing.id == listing_id.as_str())
                    {
                        ui.separator();
                        ui.monospace(format!("{} {}", listing.exchange, listing.currency));
                        ui.monospace(format!(
                            "Price: {}",
                            crate::format::fmt_money(&listing.currency, listing.last_price)
                        ));
                        ui.horizontal_wrapped(|ui| {
                            style::status_badge(ui, &listing.status);
                            style::source_badge(ui, &listing.source);
                        });
                        plot_subject = Some((subject.clone(), TimeSeriesKind::Price, "Price"));
                    }
                } else if let Some(listing) = fund.listings.first() {
                    plot_subject = Some((
                        AnalysisSubject::FundListing {
                            fund_id: fund.id.clone(),
                            listing_id: listing.id.clone(),
                        },
                        TimeSeriesKind::Price,
                        "Price",
                    ));
                }
                ui.monospace(format!("Provider: {}", fund.provider));
                ui.horizontal_wrapped(|ui| {
                    style::status_badge(ui, &fund.status);
                    style::source_badge(ui, &fund.source);
                });
                ui.monospace(format!("Last refresh: {}", fund.last_refreshed));

                if let Some(position) = snapshot
                    .positions
                    .iter()
                    .find(|position| position.fund_id == fund.id)
                {
                    ui.separator();
                    ui.monospace(format!(
                        "Value: {}",
                        crate::format::fmt_money(
                            &snapshot.portfolio_summary.base_currency,
                            position.market_value,
                        )
                    ));
                    ui.monospace(format!(
                        "Weight: {}",
                        crate::format::fmt_percent(position.portfolio_weight_pct)
                    ));
                    ui.horizontal_wrapped(|ui| {
                        style::freshness_badge(ui, position.freshness);
                        style::source_badge(ui, &position.source);
                    });
                    if overrides.iter().any(|position_override| {
                        position_override.listing_id == position.listing_id
                    }) {
                        style::manual_badge(ui);
                    }
                }

                let document_count = snapshot
                    .documents
                    .iter()
                    .filter(|document| document.fund_id == fund.id)
                    .count();
                let alert_count = snapshot
                    .alerts
                    .iter()
                    .filter(|alert| {
                        fund.listings.iter().any(|listing| {
                            alert.fund_ticker.as_deref() == Some(listing.ticker.as_str())
                        })
                    })
                    .count();
                ui.separator();
                ui.monospace(format!("Docs: {document_count}"));
                ui.monospace(format!("Alerts: {alert_count}"));
            }
            AnalysisSubject::Holding { ticker, source } => {
                ui.monospace(format!("Holding: {ticker}"));
                ui.monospace(format!("Source ETF: {source}"));
            }
            AnalysisSubject::Cash(currency) => {
                ui.monospace(format!("Cash: {currency}"));
            }
            AnalysisSubject::SyntheticModel(model_id) => {
                ui.monospace(format!("Model: {model_id}"));
            }
        }

        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(ui, "Open", "Open this context.").clicked() {
                action = Some(InspectorDetailAction::Open(context.clone()));
            }
            if let Some((subject, series_kind, label)) = plot_subject.as_ref()
                && crate::ui::actions::action_button(ui, "Plot", "Plot the subject.").clicked()
            {
                action = Some(InspectorDetailAction::Plot(PlotRequest::new(
                    subject.clone(),
                    *series_kind,
                    format!("{} {}", context.label(), label),
                )));
            }
            if crate::ui::actions::action_button(ui, "Compare", "Open Compare.").clicked() {
                action = Some(InspectorDetailAction::Navigate(Page::Compare));
            }
            if crate::ui::actions::action_button(ui, "Documents", "Open Documents.").clicked() {
                action = Some(InspectorDetailAction::Navigate(Page::Documents));
            }
            if crate::ui::actions::action_button(ui, "Jobs", "Open Jobs.").clicked() {
                action = Some(InspectorDetailAction::Navigate(Page::Jobs));
            }
            if crate::ui::actions::action_button(ui, "Copy", "Copy subject label.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    context.label().to_owned(),
                    context.label().to_owned(),
                )));
            }
        });
        action
    }

    fn render_document_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        context: &InspectorContext,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(document) = snapshot.documents.get(row_index) else {
            ui.label("Pinned document is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Document").strong());
        ui.monospace(format!("{} | {}", document.ticker, document.document_type));
        ui.monospace(format!("Date: {}", document.latest_date));
        ui.horizontal_wrapped(|ui| {
            style::status_badge(ui, &document.status);
            style::source_badge(ui, &document.source);
        });
        ui.monospace(format!("Hash/change: {}", document.content_hash_change))
            .on_hover_text("Content hash or change marker from the mock document snapshot.")
            .on_hover_cursor(egui::CursorIcon::Help);
        ui.monospace(format!("Checked: {}", document.last_checked));
        ui.monospace(format!("URI: {}", documents::document_mock_uri(document)))
            .on_hover_text(documents::document_mock_uri(document))
            .on_hover_cursor(egui::CursorIcon::Copy);
        ui.monospace("Previous snapshot: metadata/hash placeholder");
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(
                ui,
                "Open Viewer",
                "Open the in-app metadata viewer.",
            )
            .clicked()
            {
                action = Some(InspectorDetailAction::Open(context.clone()));
            }
            if crate::ui::actions::action_button(ui, "Open Fund", "Open the related fund.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Open(
                    InspectorContext::ActiveSubject {
                        subject: AnalysisSubject::Fund(document.fund_id.clone()),
                        label: document.ticker.clone(),
                        tooltip: "Open document fund".to_owned(),
                    },
                ));
            }
            if crate::ui::actions::action_button(ui, "History", "Show document/entity changes.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Navigate(Page::Diffs));
            }
            if crate::ui::actions::action_button(
                ui,
                "Compare Prev",
                "Compare metadata/hash against the previous snapshot placeholder.",
            )
            .clicked()
            {
                action = Some(InspectorDetailAction::Navigate(Page::Diffs));
            }
            if crate::ui::actions::action_button(ui, "Copy URL", "Copy mock document URI.")
                .clicked()
            {
                let uri = documents::document_mock_uri(document);
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    uri.clone(),
                    uri,
                )));
            }
            if crate::ui::actions::action_button(ui, "Copy Hash", "Copy content hash/change.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    document.content_hash_change.clone(),
                    document.content_hash_change.clone(),
                )));
            }
            if crate::ui::actions::action_button(ui, "Copy Metadata", "Copy document metadata.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    format!("metadata {}", document.ticker),
                    documents::document_metadata_copy_text(
                        document,
                        snapshot.find_fund_by_id(&document.fund_id),
                    ),
                )));
            }
        });
        action
    }

    fn render_alert_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        context: &InspectorContext,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(alert) = snapshot.alerts.get(row_index) else {
            ui.label("Pinned alert is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Alert").strong());
        ui.horizontal_wrapped(|ui| {
            style::alert_severity_badge(ui, alert.severity);
            style::status_badge(ui, &alert.status);
            style::source_badge(ui, &alert.source);
        });
        ui.label(egui::RichText::new(&alert.title).strong());
        ui.label(&alert.message);
        ui.separator();
        ui.monospace(format!("Category: {}", alert.category));
        ui.monospace(format!(
            "Affected: {}",
            alert.fund_ticker.as_deref().unwrap_or("-")
        ));
        ui.monospace(format!("Created: {}", alert.created_time));
        ui.horizontal_wrapped(|ui| {
            style::status_badge(ui, if alert.read { "READ" } else { "UNREAD" });
            style::status_badge(
                ui,
                if alert.dismissed {
                    "DISMISSED"
                } else {
                    "ACTIVE"
                },
            );
        });
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button_enabled(
                ui,
                context.subject().is_some(),
                "Open Affected",
                "Open the affected fund/listing when available.",
            )
            .clicked()
            {
                action = Some(InspectorDetailAction::Open(context.clone()));
            }
            if crate::ui::actions::action_button_enabled(
                ui,
                !alert.read,
                "Mark Read",
                "Mark this alert as read locally.",
            )
            .clicked()
            {
                action = Some(InspectorDetailAction::MarkAlertRead(row_index));
            }
            if crate::ui::actions::action_button_enabled(
                ui,
                !alert.dismissed,
                "Dismiss",
                "Dismiss this alert locally.",
            )
            .clicked()
            {
                action = Some(InspectorDetailAction::DismissAlert(row_index));
            }
            if crate::ui::actions::action_button_enabled(
                ui,
                !alert.status.eq_ignore_ascii_case("resolved"),
                "Resolve",
                "Resolve this alert locally.",
            )
            .clicked()
            {
                action = Some(InspectorDetailAction::ResolveAlert(row_index));
            }
            if crate::ui::actions::action_button(ui, "Run Job", "Queue a related mock job.")
                .clicked()
            {
                let suffix = alert
                    .fund_ticker
                    .as_deref()
                    .map(|ticker| format!(" {ticker}"))
                    .unwrap_or_default();
                action = Some(InspectorDetailAction::RunJob(format!(
                    "{}{}",
                    alert.category.replace(' ', "_").to_ascii_uppercase(),
                    suffix
                )));
            }
            if crate::ui::actions::action_button(ui, "Diagnostics", "Open diagnostics.").clicked() {
                action = Some(InspectorDetailAction::Navigate(Page::Analytics));
            }
            if crate::ui::actions::action_button(ui, "Copy", "Copy alert details.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    format!("alert {}", alert.id),
                    alerts::alert_copy_text(alert),
                )));
            }
        });
        action
    }

    fn render_scheduled_job_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(job) = snapshot.scheduled_jobs.get(row_index) else {
            ui.label("Pinned scheduled job is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Scheduled job").strong());
        ui.horizontal_wrapped(|ui| {
            ui.monospace(&job.name);
            style::source_badge(ui, &job.source);
            style::status_badge(ui, if job.active { "ACTIVE" } else { "DISABLED" });
        });
        ui.monospace(format!("Type: {}", job.job_type));
        ui.monospace(format!("Cron: {}", job.cron_schedule));
        ui.monospace(format!("Last run: {}", job.last_run));
        ui.monospace(format!("Next run: {}", job.next_run));
        ui.monospace("Implementation: mock/local scheduler placeholder");
        ui.monospace("Locked/running: not modeled");
        ui.monospace("Rate limit/backoff: not modeled");
        ui.monospace("Affected object: not modeled");
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(ui, "Run Now", "Queue this mock job.").clicked() {
                action = Some(InspectorDetailAction::RunJob(job.name.clone()));
            }
            if crate::ui::actions::action_button(
                ui,
                "Open Latest Run",
                "Select the latest run matching this job type/source when available.",
            )
            .clicked()
            {
                action = Some(InspectorDetailAction::Feedback(format!(
                    "DETAIL latest run for {} | {}",
                    job.name, job.source
                )));
            }
            if crate::ui::actions::action_button(ui, "Diagnostics", "Open diagnostics.").clicked() {
                action = Some(InspectorDetailAction::Navigate(Page::Analytics));
            }
            if crate::ui::actions::action_button(ui, "Copy", "Copy job row.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    format!("job {}", job.name),
                    scheduled_job_copy_text(job),
                )));
            }
        });
        action
    }

    fn render_job_run_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(run) = snapshot.job_runs.get(row_index) else {
            ui.label("Pinned job run is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Job run").strong());
        ui.horizontal_wrapped(|ui| {
            ui.monospace(&run.id);
            style::job_status_badge(ui, run.status);
            style::source_badge(ui, &run.source);
        });
        ui.monospace(format!("Type: {}", run.job_type));
        ui.monospace(format!("Started: {}", run.started));
        ui.monospace(format!(
            "Finished: {}",
            run.finished.as_deref().unwrap_or("-")
        ));
        ui.monospace(format!(
            "Inserted {} | Updated {} | Failed {}",
            run.inserted, run.updated, run.failed
        ));
        ui.label(&run.message);
        ui.monospace("Fetch logs/source budget: mock placeholder");
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(ui, "Run Similar", "Queue a similar mock job.")
                .clicked()
            {
                action = Some(InspectorDetailAction::RunJob(run.job_type.clone()));
            }
            if crate::ui::actions::action_button(ui, "Diagnostics", "Open diagnostics.").clicked() {
                action = Some(InspectorDetailAction::Navigate(Page::Analytics));
            }
            if crate::ui::actions::action_button(ui, "Copy", "Copy run row.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    format!("job run {}", run.id),
                    job_run_copy_text(run),
                )));
            }
        });
        action
    }

    fn render_readiness_stage_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(stage) = snapshot.data_operations.readiness_stages.get(row_index) else {
            ui.label("Pinned readiness stage is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Readiness stage").strong());
        ui.horizontal_wrapped(|ui| {
            ui.monospace(&stage.label);
            style::status_badge(ui, stage.status.as_str());
            style::source_badge(ui, &stage.source);
        });
        ui.monospace(format!("Key: {}", stage.key));
        ui.monospace(format!(
            "Coverage: {}",
            stage
                .coverage_pct
                .map(|value| crate::format::fmt_percent(f64::from(value)))
                .unwrap_or_else(|| "-".to_owned())
        ));
        ui.monospace(format!("Count: {}", stage.count_label));
        ui.monospace(format!("Freshness: {}", stage.freshness));
        ui.label(&stage.detail);
        ui.monospace(format!("Recommended: {}", stage.recommended_action));
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(ui, "Open Data Ops", "Open Data Operations.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Navigate(Page::DataOperations));
            }
            if crate::ui::actions::action_button(ui, "Open Jobs", "Open scheduler jobs.").clicked()
            {
                action = Some(InspectorDetailAction::Navigate(Page::Jobs));
            }
            if crate::ui::actions::action_button(ui, "Copy", "Copy readiness stage.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    stage.label.clone(),
                    data_operations::readiness_copy_text(stage),
                )));
            }
        });
        action
    }

    fn render_market_data_plan_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        context: &InspectorContext,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(item) = snapshot.data_operations.market_data_plan.get(row_index) else {
            ui.label("Pinned market-data plan item is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Market-data plan item").strong());
        ui.horizontal_wrapped(|ui| {
            ui.monospace(&item.subject_label);
            style::status_badge(ui, item.status.as_str());
            style::source_badge(ui, &item.source);
        });
        ui.monospace(format!("Priority: {}", item.priority));
        ui.monospace(format!("Type: {}", item.item_type));
        ui.monospace(format!("Estimated requests: {}", item.estimated_requests));
        ui.label(&item.reason);
        ui.monospace(format!(
            "Blocker: {}",
            item.blocker.as_deref().unwrap_or("-")
        ));
        ui.monospace(format!("Next action: {}", item.next_action));
        ui.monospace("Boundary: local/mock UI action only; no backend call is made.");
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button_enabled(
                ui,
                context.subject().is_some(),
                "Open Subject",
                "Open the related subject when one is modeled.",
            )
            .clicked()
            {
                action = Some(InspectorDetailAction::Open(context.clone()));
            }
            if crate::ui::actions::action_button(ui, "Run now", "Queue a related mock job.")
                .clicked()
            {
                action = Some(InspectorDetailAction::RunJob(item.item_type.clone()));
            }
            if crate::ui::actions::action_button(ui, "Open Logs", "Open Data Operations logs.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Navigate(Page::DataOperations));
            }
            if crate::ui::actions::action_button(ui, "Copy", "Copy plan item.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    item.subject_label.clone(),
                    data_operations::plan_item_copy_text(item),
                )));
            }
        });
        action
    }

    fn render_source_budget_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(budget) = snapshot.data_operations.source_budgets.get(row_index) else {
            ui.label("Pinned source budget is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Source budget").strong());
        ui.horizontal_wrapped(|ui| {
            style::source_badge(ui, &budget.source);
            style::status_badge(ui, budget.status.as_str());
            style::status_badge(ui, if budget.enabled { "ACTIVE" } else { "DISABLED" });
        });
        ui.monospace(format!(
            "Requests/window: {}/{} {}",
            budget.requests_used, budget.requests_limit, budget.window
        ));
        ui.monospace(format!("Min delay: {}", budget.min_delay));
        ui.monospace(format!(
            "Backoff until: {}",
            budget.backoff_until.as_deref().unwrap_or("-")
        ));
        ui.monospace(format!("Recent failures: {}", budget.recent_failures));
        ui.monospace(format!("Cache hits: {}", budget.cache_hits));
        ui.monospace(format!("Next allowed: {}", budget.next_allowed));
        ui.label(format!("Capabilities: {}", budget.capabilities.join(", ")));
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(ui, "Open Logs", "Open fetch log section.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Navigate(Page::DataOperations));
            }
            if crate::ui::actions::action_button(ui, "Copy Source", "Copy source name.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    budget.source.clone(),
                    budget.source.clone(),
                )));
            }
            if crate::ui::actions::action_button(ui, "Copy Row", "Copy source budget.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    budget.source.clone(),
                    data_operations::source_budget_copy_text(budget),
                )));
            }
        });
        action
    }

    fn render_fetch_log_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(log) = snapshot.data_operations.fetch_logs.get(row_index) else {
            ui.label("Pinned fetch log is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Fetch log").strong());
        ui.horizontal_wrapped(|ui| {
            ui.monospace(&log.id);
            style::status_badge(ui, log.status.as_str());
            style::source_badge(ui, &log.source);
        });
        ui.monospace(format!("Time: {}", log.time));
        ui.monospace(format!("Kind: {}", log.request_kind));
        let masked_key = crate::compute::data_operations::fetch_log_display_key(log);
        ui.monospace(format!("Key: {masked_key}"))
            .on_hover_text("Secrets are masked in display and copy payloads.");
        ui.monospace(format!(
            "HTTP: {} | {} ms",
            log.http_status
                .map(|status| status.to_string())
                .unwrap_or_else(|| "-".to_owned()),
            log.duration_ms
        ));
        ui.monospace(format!(
            "Cache hit: {} | Rate limited: {}",
            crate::pages::bool_text(log.cache_hit),
            crate::pages::bool_text(log.rate_limited)
        ));
        ui.label(log.error.as_deref().unwrap_or("-"));
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(ui, "Open Source", "Open source budget section.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Navigate(Page::DataOperations));
            }
            if crate::ui::actions::action_button(ui, "Open Jobs", "Open jobs page.").clicked() {
                action = Some(InspectorDetailAction::Navigate(Page::Jobs));
            }
            if crate::ui::actions::action_button(ui, "Copy Key", "Copy masked request key.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    log.id.clone(),
                    masked_key.clone(),
                )));
            }
            if crate::ui::actions::action_button(ui, "Copy Row", "Copy masked fetch log.").clicked()
            {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    log.id.clone(),
                    crate::compute::data_operations::fetch_log_copy_text(log),
                )));
            }
        });
        action
    }

    fn render_constituent_readiness_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        context: &InspectorContext,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(row) = snapshot.data_operations.constituent_coverage.get(row_index) else {
            ui.label("Pinned constituent readiness row is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Constituent readiness").strong());
        ui.horizontal_wrapped(|ui| {
            ui.monospace(format!("{} | {}", row.fund_ticker, row.holding_ticker));
            style::status_badge(
                ui,
                crate::compute::data_operations::constituent_readiness_status(row).as_str(),
            );
        });
        ui.label(&row.holding_name);
        ui.monospace(format!(
            "Weight: {}",
            crate::format::fmt_percent(row.weight_pct)
        ));
        ui.horizontal_wrapped(|ui| {
            ui.label("Identity");
            style::status_badge(ui, row.identity_status.as_str());
        });
        ui.monospace(format!(
            "Instrument: {}",
            row.instrument_id.as_deref().unwrap_or("-")
        ));
        ui.monospace(format!(
            "Listing: {}",
            row.listing_id.as_deref().unwrap_or("-")
        ));
        ui.horizontal_wrapped(|ui| {
            ui.label("Price");
            style::status_badge(ui, row.price_status.as_str());
            style::source_badge(ui, &row.price_source);
        });
        ui.monospace(format!(
            "Latest price: {}",
            row.latest_price
                .map(|value| crate::format::fmt_decimal(value, 2))
                .unwrap_or_else(|| "-".to_owned())
        ));
        ui.monospace(format!(
            "Price date: {}",
            row.price_date.as_deref().unwrap_or("-")
        ));
        ui.monospace(format!("Next action: {}", row.next_action));
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(ui, "Open", "Open related subject.").clicked() {
                action = Some(InspectorDetailAction::Open(context.clone()));
            }
            if crate::ui::actions::action_button(ui, "Chart", "Open chart for subject.").clicked() {
                action = Some(InspectorDetailAction::Plot(PlotRequest::new(
                    row.subject.clone(),
                    TimeSeriesKind::Price,
                    format!("{} Price", row.holding_ticker),
                )));
            }
            if crate::ui::actions::action_button(ui, "Copy", "Copy constituent readiness.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    row.holding_ticker.clone(),
                    data_operations::constituent_copy_text(row),
                )));
            }
        });
        action
    }

    fn render_data_diagnostic_inspector(
        ui: &mut egui::Ui,
        snapshot: &DashboardSnapshot,
        row_index: usize,
    ) -> Option<InspectorDetailAction> {
        let Some(issue) = snapshot.data_operations.diagnostic_issues.get(row_index) else {
            ui.label("Pinned data diagnostic is no longer available in the snapshot.");
            return None;
        };
        ui.label(egui::RichText::new("Data diagnostic").strong());
        ui.horizontal_wrapped(|ui| {
            style::alert_severity_badge(ui, issue.severity);
            style::status_badge(ui, issue.status.as_str());
            style::source_badge(ui, &issue.source);
        });
        ui.label(egui::RichText::new(&issue.title).strong());
        ui.label(&issue.detail);
        ui.monospace(format!("Recommended: {}", issue.recommended_action));
        ui.monospace(format!("Related: {}", issue.related_page));
        ui.separator();
        let mut action = None;
        ui.horizontal_wrapped(|ui| {
            if crate::ui::actions::action_button(ui, "Open Data Ops", "Open Data Operations.")
                .clicked()
            {
                action = Some(InspectorDetailAction::Navigate(Page::DataOperations));
            }
            if crate::ui::actions::action_button(ui, "Open Jobs", "Open jobs page.").clicked() {
                action = Some(InspectorDetailAction::Navigate(Page::Jobs));
            }
            if crate::ui::actions::action_button(ui, "Copy", "Copy diagnostic.").clicked() {
                action = Some(InspectorDetailAction::Copy(CopyPayload::new(
                    issue.id.clone(),
                    data_operations::diagnostic_copy_text(issue),
                )));
            }
        });
        action
    }

    fn apply_inspector_detail_action(&mut self, action: InspectorDetailAction) {
        match action {
            InspectorDetailAction::Open(context) => {
                let outcome = self.open_inspector_context(context);
                self.command_feedback = outcome.feedback();
            }
            InspectorDetailAction::Plot(request) => {
                self.open_plot(request);
            }
            InspectorDetailAction::Navigate(page) => {
                self.page = page;
                self.command_feedback = CommandOutcome::Navigated(page).feedback();
            }
            InspectorDetailAction::ClearRowOverrides(listing_id) => {
                self.clear_row_overrides(&listing_id);
            }
            InspectorDetailAction::MarkAlertRead(index) => {
                if let Some(alert) = self
                    .snapshot
                    .as_loaded_mut()
                    .and_then(|snapshot| snapshot.alerts.get_mut(index))
                {
                    alerts::mark_alert_read(alert);
                    self.command_feedback = format!("ALERT: read {}", alert.id);
                }
            }
            InspectorDetailAction::DismissAlert(index) => {
                if let Some(alert) = self
                    .snapshot
                    .as_loaded_mut()
                    .and_then(|snapshot| snapshot.alerts.get_mut(index))
                {
                    alerts::dismiss_alert(alert);
                    self.command_feedback = format!("ALERT: dismissed {}", alert.id);
                }
            }
            InspectorDetailAction::ResolveAlert(index) => {
                if let Some(alert) = self
                    .snapshot
                    .as_loaded_mut()
                    .and_then(|snapshot| snapshot.alerts.get_mut(index))
                {
                    alerts::resolve_alert(alert);
                    self.command_feedback = format!("ALERT: resolved {}", alert.id);
                }
            }
            InspectorDetailAction::RunJob(job_name) => {
                let message = jobs::mock_run_now_message(&job_name);
                self.mock_job_message = Some(message.clone());
                self.page = Page::Jobs;
                self.command_feedback = message;
            }
            InspectorDetailAction::Copy(payload) => {
                let outcome = self.copy_to_clipboard(payload);
                self.command_feedback = outcome.feedback();
            }
            InspectorDetailAction::Feedback(message) => {
                self.command_feedback = message;
            }
        }
    }

    fn open_inspector_context(&mut self, context: InspectorContext) -> CommandOutcome {
        match context {
            InspectorContext::DocumentSnapshot { row_index, .. } => {
                let entry = self.snapshot.as_loaded().and_then(|snapshot| {
                    snapshot
                        .documents
                        .get(row_index)
                        .map(|document| document_navigation_entry(snapshot, document))
                });
                let Some(entry) = entry else {
                    return CommandOutcome::Error("pinned document unavailable".to_owned());
                };
                self.documents.table.select(row_index);
                self.open_navigation_entry(entry);
                CommandOutcome::Navigated(Page::DocumentViewer)
            }
            InspectorContext::ScheduledJob {
                row_index, name, ..
            } => {
                self.jobs.scheduled_table.select(row_index);
                self.jobs.runs_table.clear_selection();
                self.jobs.active_table = crate::table_state::TableId::ScheduledJobs;
                self.page = Page::Jobs;
                CommandOutcome::Source(format!("job {name}"))
            }
            InspectorContext::JobRun {
                row_index, run_id, ..
            } => {
                self.jobs.runs_table.select(row_index);
                self.jobs.scheduled_table.clear_selection();
                self.jobs.active_table = crate::table_state::TableId::JobRuns;
                self.page = Page::Jobs;
                CommandOutcome::Source(format!("job run {run_id}"))
            }
            InspectorContext::ReadinessStage {
                row_index, label, ..
            } => {
                self.data_operations.select_readiness(row_index);
                self.page = Page::DataOperations;
                CommandOutcome::Source(format!("readiness {label}"))
            }
            InspectorContext::MarketDataPlanItem {
                row_index,
                item_id,
                subject,
                label,
            } => {
                self.data_operations
                    .select_table(crate::table_state::TableId::DataOperationsPlan, row_index);
                if let Some(subject) = subject {
                    let page = page_for_subject(&subject);
                    self.open_navigation_entry(
                        NavigationEntry::new(subject, label.clone(), page)
                            .with_breadcrumbs(vec!["Data Operations".to_owned(), label]),
                    );
                    CommandOutcome::Navigated(page)
                } else {
                    self.page = Page::DataOperations;
                    CommandOutcome::Source(format!("plan item {item_id}"))
                }
            }
            InspectorContext::SourceBudget {
                row_index, source, ..
            } => {
                self.data_operations.select_table(
                    crate::table_state::TableId::DataOperationsSources,
                    row_index,
                );
                self.page = Page::DataOperations;
                CommandOutcome::Source(format!("source {source}"))
            }
            InspectorContext::FetchLog {
                row_index, log_id, ..
            } => {
                self.data_operations.select_table(
                    crate::table_state::TableId::DataOperationsFetchLogs,
                    row_index,
                );
                self.page = Page::DataOperations;
                CommandOutcome::Source(format!("fetch log {log_id}"))
            }
            InspectorContext::ConstituentReadiness {
                row_index,
                subject,
                label,
            } => {
                self.data_operations.select_table(
                    crate::table_state::TableId::DataOperationsConstituents,
                    row_index,
                );
                let page = page_for_subject(&subject);
                self.open_navigation_entry(
                    NavigationEntry::new(subject, label.clone(), page)
                        .with_breadcrumbs(vec!["Data Operations".to_owned(), label]),
                );
                CommandOutcome::Navigated(page)
            }
            InspectorContext::DataDiagnosticIssue {
                row_index,
                issue_id,
                ..
            } => {
                self.data_operations.select_table(
                    crate::table_state::TableId::DataOperationsDiagnostics,
                    row_index,
                );
                self.page = Page::DataOperations;
                CommandOutcome::Source(format!("diagnostic {issue_id}"))
            }
            context => {
                let Some(subject) = context.subject().cloned() else {
                    return CommandOutcome::Error(
                        "no open target for inspector context".to_owned(),
                    );
                };
                let page = page_for_subject(&subject);
                self.open_navigation_entry(
                    NavigationEntry::new(subject, context.label().to_owned(), page)
                        .with_breadcrumbs(vec![context.label().to_owned()]),
                );
                CommandOutcome::Navigated(page)
            }
        }
    }

    fn selected_job_command_name(&self) -> Option<String> {
        let snapshot = self.snapshot.as_loaded()?;
        if self.page == Page::DataOperations {
            return self
                .data_operations
                .scheduler_table
                .selected_index()
                .and_then(|index| snapshot.scheduled_jobs.get(index))
                .map(|job| job.name.clone());
        }
        self.jobs
            .scheduled_table
            .selected_index()
            .and_then(|index| snapshot.scheduled_jobs.get(index))
            .map(|job| job.name.clone())
            .or_else(|| {
                self.jobs
                    .runs_table
                    .selected_index()
                    .and_then(|index| snapshot.job_runs.get(index))
                    .map(|run| run.job_type.clone())
            })
    }

    fn left_navigation(&mut self, ui: &mut egui::Ui) {
        let (alerts, jobs, operations) = self
            .snapshot
            .as_loaded()
            .map(|snapshot| {
                (
                    snapshot
                        .alerts
                        .iter()
                        .filter(|alert| {
                            !alert.dismissed && !alert.status.eq_ignore_ascii_case("resolved")
                        })
                        .count(),
                    snapshot
                        .job_runs
                        .iter()
                        .filter(|run| {
                            matches!(
                                run.status,
                                crate::domain::JobStatus::Queued
                                    | crate::domain::JobStatus::Running
                                    | crate::domain::JobStatus::Failed
                            )
                        })
                        .count(),
                    snapshot.data_operations.diagnostic_issues.len(),
                )
            })
            .unwrap_or_default();
        let groups: [(&str, &[Page]); 4] = [
            (
                "Workspace",
                &[
                    Page::Portfolio,
                    Page::Etfs,
                    Page::Hierarchy,
                    Page::FundDetail,
                ],
            ),
            (
                "Analysis",
                &[
                    Page::Charts,
                    Page::Exposure,
                    Page::Analytics,
                    Page::Curves,
                    Page::Compare,
                    Page::Spreads,
                    Page::Diffs,
                ],
            ),
            (
                "Data",
                &[
                    Page::Holdings,
                    Page::Dividends,
                    Page::DataOperations,
                    Page::Alerts,
                    Page::Documents,
                    Page::Jobs,
                ],
            ),
            (
                "Tools",
                &[Page::AddInstrument, Page::Search, Page::Settings],
            ),
        ];

        egui::ScrollArea::vertical()
            .auto_shrink(false)
            .show(ui, |ui| {
                for (group_index, (group_label, pages)) in groups.iter().enumerate() {
                    if group_index > 0 {
                        ui.add_space(metrics::SPACE_1);
                    }
                    style::section_header(ui, group_label);
                    for page in *pages {
                        let count = match page {
                            Page::Alerts => (alerts > 0).then_some(alerts),
                            Page::Jobs => (jobs > 0).then_some(jobs),
                            Page::DataOperations => (operations > 0).then_some(operations),
                            _ => None,
                        };
                        if style::left_rail_item(ui, self.page == *page, page.label(), count)
                            .on_hover_text(format!(
                                "Open {}. Active subject remains distinct from row selection.",
                                page.label()
                            ))
                            .clicked()
                        {
                            self.page = *page;
                        }
                    }
                }
            });
    }

    fn command_help_window(&mut self, ctx: &egui::Context) {
        if !self.show_command_help {
            return;
        }

        egui::Window::new("Command Help")
            .open(&mut self.show_command_help)
            .resizable(false)
            .collapsible(false)
            .show(ctx, |ui| {
                ui.label(COMMAND_SUGGESTIONS);
                ui.separator();
                ui.label(egui::RichText::new("Command examples").strong());
                egui::Grid::new("command_help_grid")
                    .num_columns(2)
                    .striped(true)
                    .show(ui, |ui| {
                        for (command, meaning) in [
                            ("Ctrl/Cmd+K", "focus command/search"),
                            ("Ctrl/Cmd+F", "open Search workspace"),
                            ("Up / Down", "recall command history"),
                            ("/ VUSA", "open Search with query"),
                            ("search VUSA", "open Search with query"),
                            ("VUSA", "select ticker and open detail"),
                            ("select JEPG", "select instrument by ticker"),
                            ("goto jobs", "open a page"),
                            ("data operations", "open Data Operations readiness workspace"),
                            ("market data plan", "open Data Operations plan section"),
                            ("source budgets", "open Data Operations source budget section"),
                            ("fetch logs", "open Data Operations fetch log section"),
                            ("copy price ingestion command", "copy a mock worker command"),
                            ("plot portfolio", "chart portfolio value"),
                            ("plot active", "chart the active page subject"),
                            ("plot selected", "chart the selected subject"),
                            ("plot VUSA", "chart selected listing price"),
                            ("plot nav vs price VUSA", "chart NAV with price overlay"),
                            ("plot VUSA vs ISF", "open Charts with comparison overlay"),
                            ("chart value raw", "show raw chart values"),
                            ("chart value rebased", "rebase compare/overlay series to 100"),
                            ("chart value percent", "show percent change from common base"),
                            ("spread VUSA JEPG", "open 1-to-1 spread workflow"),
                            ("VUSA - JEPG", "same as spread VUSA JEPG"),
                            ("plot spread VUSA JEPG", "plot mock spread context"),
                            ("overlay ISF", "add a comparison to the active chart"),
                            ("clear overlays", "remove chart overlays"),
                            ("copy chart row", "copy selected chart point row"),
                            ("copy chart value", "copy focused chart cell or point value"),
                            ("copy selected series", "copy selected chart series data"),
                            ("clear chart selection", "clear selected chart point/series/cell"),
                            ("select next series", "select next chart series"),
                            ("select previous series", "select previous chart series"),
                            ("select next point", "move chart table selection down"),
                            ("select previous point", "move chart table selection up"),
                            ("Alt/Opt+Up/Down", "cycle chart series"),
                            ("Alt/Opt+Left/Right", "move point within selected chart series"),
                            ("compare VUSA ISF", "open Compare page"),
                            ("diff VUSA holdings", "open Changes for mock diff topic"),
                            ("copy active", "copy active subject label"),
                            ("copy selected", "copy selected row/cell/subject payload"),
                            ("copy focused cell", "copy focused table cell, then row fallback"),
                            ("open focused row", "open the focused/selected row target"),
                            ("clear table focus", "clear focused row/cell but keep active subject"),
                            ("Ctrl/Cmd+C", "copy selected row/cell or active subject"),
                            ("pin inspector", "pin current inspector context"),
                            ("unpin inspector", "return inspector to following selection"),
                            ("open pinned", "open pinned inspector context"),
                            ("open active", "open the active page subject"),
                            ("open selected", "open selected subject/document when available"),
                            ("open document", "open selected document inside Mimer"),
                            ("dismiss selected alert", "dismiss selected alert locally"),
                            ("resolve selected alert", "resolve selected alert locally"),
                            ("run selected job", "queue selected scheduled job or similar run"),
                            ("source / sources", "show active source availability/fallback"),
                            ("zoom in / zoom out / zoom reset", "adjust app zoom"),
                            ("Ctrl/Cmd++ / - / 0", "zoom shortcuts"),
                            ("show overrides", "open Analytics override list"),
                            ("clear override(s)", "clear selected row or all overrides"),
                            ("diagnostics", "open Analytics diagnostics"),
                            ("back / forward / home", "navigate stack"),
                            ("refresh", "reload mock snapshot"),
                            ("legend", "open data status legend"),
                            (
                                "right-click rows",
                                "context menus on Portfolio, ETFs, Documents, Jobs, Alerts",
                            ),
                            ("editable cells", "hover shows edit cursor and override tooltip"),
                            ("date fields", "YYYY-MM-DD input; native DatePickerButton unavailable in current egui_extras"),
                            ("double-click document", "open in-app document viewer"),
                            (
                                "toggle inspector/nav/context/status",
                                "show or hide shell panels",
                            ),
                            ("reset layout", "restore shell visibility and inspector width"),
                            (
                                "Table arrows / Enter / Esc",
                                "move row/cell focus, open row, or clear table focus",
                            ),
                            ("run daily_price_ingestion", "queue local mock job feedback"),
                        ] {
                            ui.monospace(command);
                            ui.label(meaning);
                            ui.end_row();
                        }
                    });
                ui.separator();
                ui.label("Active subject drives page-level actions; selected row/cell drives copy/open/plot selected actions.");
                ui.label("Overrides are local GUI state. Refresh replays them into effective data; clear commands explicitly remove them.");
                ui.label("Source fallback is normal mock behavior: unavailable selected sources fall back visibly to canonical/default.");
            });
    }

    fn status_legend_window(&mut self, ctx: &egui::Context) {
        if !self.show_status_legend {
            return;
        }

        egui::Window::new("Data Status Legend")
            .open(&mut self.show_status_legend)
            .resizable(false)
            .collapsible(false)
            .show(ctx, |ui| {
                egui::Grid::new("status_legend_grid")
                    .num_columns(2)
                    .striped(true)
                    .show(ui, |ui| {
                        for (tag, meaning) in status_legend_entries() {
                            ui.monospace(tag);
                            ui.label(meaning);
                            ui.end_row();
                        }
                    });
            });
    }

    fn about_window(&mut self, ctx: &egui::Context) {
        if !self.show_about {
            return;
        }

        let storage_summary = self.storage_summary();
        let storage_status = self.storage_status.clone();
        egui::Window::new("About Mimer")
            .open(&mut self.show_about)
            .resizable(false)
            .collapsible(false)
            .show(ctx, |ui| {
                ui.strong(app_info::APP_NAME);
                ui.monospace(app_info::version_label());
                ui.separator();
                egui::Grid::new("about_mimer_grid")
                    .num_columns(2)
                    .striped(true)
                    .show(ui, |ui| {
                        ui.label("Status");
                        ui.monospace("alpha / mock+api");
                        ui.end_row();
                        ui.label("Backend");
                        ui.monospace("REST hydration for Data Operations only");
                        ui.end_row();
                        ui.label("API");
                        ui.monospace(self.api_runtime.connection.as_str());
                        ui.end_row();
                        ui.label("Build");
                        ui.monospace(app_info::window_title());
                        ui.end_row();
                        ui.label("Storage");
                        ui.monospace(&storage_status);
                        ui.end_row();
                        if let Some(summary) = storage_summary.as_ref() {
                            ui.label("Storage schema");
                            ui.monospace(summary.schema_version.to_string());
                            ui.end_row();
                            ui.label("Version folder");
                            ui.monospace(&summary.version_dir)
                                .on_hover_text(&summary.version_dir);
                            ui.end_row();
                        }
                    });
                ui.separator();
                ui.label("Not financial advice. Data may be mock, stale, incomplete, or manually overridden.");
            });
    }

    fn central_content(&mut self, ui: &mut egui::Ui) {
        let mut action = None;
        let storage_summary = self.storage_summary();
        match &mut self.snapshot {
            LoadState::Idle => {
                style::state_message(ui, "EMPTY", "No workspace snapshot has been loaded.");
            }
            LoadState::Loading => {
                style::state_message(
                    ui,
                    "LOADING",
                    "Loading workspace data on a background worker.",
                );
            }
            LoadState::Error(message) => {
                style::state_message(ui, "FAILED", message);
            }
            LoadState::Loaded(snapshot) => {
                action = Self::render_loaded_page(
                    ui,
                    snapshot,
                    &self.provider,
                    &mut self.page,
                    &mut self.add_instrument,
                    &mut self.alerts,
                    &mut self.portfolio,
                    &mut self.hierarchy,
                    &mut self.charts,
                    &mut self.etfs,
                    &mut self.exposure,
                    &mut self.dividends,
                    &mut self.documents,
                    &mut self.fund_detail,
                    &mut self.holdings,
                    &mut self.jobs,
                    &mut self.data_operations,
                    &mut self.curves,
                    &mut self.compare,
                    &mut self.spreads,
                    &mut self.search,
                    &mut self.settings,
                    &mut self.layout,
                    &mut self.table_layouts,
                    &mut self.selected_workspace_id,
                    &mut self.mock_job_message,
                    storage_summary.as_ref(),
                    &self.api_runtime,
                    self.refresh_receiver.is_some() || self.api_test_receiver.is_some(),
                );
            }
        }

        if let Some(action) = action {
            self.apply_app_action(action);
        }
    }

    fn apply_app_action(&mut self, action: AppAction) {
        match action {
            AppAction::Open(entry) => {
                self.open_navigation_entry(entry);
            }
            AppAction::OpenDocumentIndex(index) => {
                let entry = self.snapshot.as_loaded().and_then(|snapshot| {
                    snapshot
                        .documents
                        .get(index)
                        .map(|document| document_navigation_entry(snapshot, document))
                });
                if let Some(entry) = entry {
                    self.documents.table.select(index);
                    self.open_navigation_entry(entry);
                } else {
                    self.command_feedback = "CMD: document search result unavailable".to_owned();
                }
            }
            AppAction::Plot(request) => {
                self.open_plot(request);
            }
            AppAction::Navigate(page) => {
                let page = self.redirect_legacy_chart_page(page);
                self.page = page;
                self.command_feedback = CommandOutcome::Navigated(page).feedback();
            }
            AppAction::PinInspector => {
                let context = self.resolve_follow_inspector_context();
                self.inspector.set_follow_context(context);
                if self.inspector.pin_current() {
                    let label = self
                        .inspector
                        .pinned_context
                        .as_ref()
                        .map(InspectorContext::label)
                        .unwrap_or("-");
                    self.command_feedback = format!("SOURCE: inspector pinned {label}");
                } else {
                    self.command_feedback = "CMD: no inspector context to pin".to_owned();
                }
            }
            AppAction::ClearRowOverrides(listing_id) => {
                self.clear_row_overrides(&listing_id);
            }
            AppAction::MakeActive { subject, label } => {
                if let Some(snapshot) = self.snapshot.as_loaded_mut() {
                    apply_subject_selection(snapshot, &subject);
                }
                self.command_feedback = format!("ACTIVE: {label}");
            }
            AppAction::Copy { label, text } => {
                let outcome = self.copy_to_clipboard(CopyPayload::new(label, text));
                self.command_feedback = outcome.feedback();
            }
            AppAction::Feedback(message) => {
                self.command_feedback = message;
            }
            AppAction::RefreshDataOperations => {
                self.refresh_data();
            }
            AppAction::SetDataMode(mode) => {
                self.settings.data_mode = mode;
                self.api_runtime = ApiRuntimeStatus {
                    connection: if mode == DataMode::Mock {
                        ApiConnectionStatus::NotUsed
                    } else {
                        ApiConnectionStatus::Checking
                    },
                    ..Default::default()
                };
                self.refresh_data();
            }
            AppAction::TestBackendConnection => {
                self.test_backend_connection();
            }
            AppAction::Back => {
                let outcome = self.go_back();
                self.command_feedback = outcome.feedback();
            }
        }
    }

    fn redirect_legacy_chart_page(&mut self, page: Page) -> Page {
        match page {
            Page::Compare => {
                self.charts.set_mode(ChartMode::Compare);
                Page::Charts
            }
            Page::Spreads => {
                self.charts.set_mode(ChartMode::Spread);
                Page::Charts
            }
            _ => page,
        }
    }

    fn open_navigation_entry(&mut self, entry: NavigationEntry) {
        if let Some(snapshot) = self.snapshot.as_loaded_mut() {
            apply_subject_selection(snapshot, &entry.subject);
        }
        self.page = entry.page;
        self.command_feedback = format!("CMD: opened {}", entry.label);
        self.navigation.open(entry);
    }

    fn open_plot(&mut self, request: PlotRequest) {
        let label = request.label.clone();
        let breadcrumbs = {
            let mut labels = self.navigation.current().breadcrumbs.clone();
            if labels.last().is_none_or(|last| last != "Charts") {
                labels.push("Charts".to_owned());
            }
            labels
        };
        let entry = NavigationEntry::new(request.subject.clone(), label.clone(), Page::Charts)
            .with_breadcrumbs(breadcrumbs);
        if let Some(snapshot) = self.snapshot.as_loaded_mut() {
            apply_subject_selection(snapshot, &request.subject);
        }
        self.charts.set_plot(request);
        self.page = Page::Charts;
        self.navigation.open(entry);
        self.command_feedback = CommandOutcome::Plot(label).feedback();
    }

    fn selected_document_entry(&mut self) -> Option<NavigationEntry> {
        let snapshot = self.snapshot.as_loaded()?;
        let index = self
            .documents
            .table
            .selected_index()
            .filter(|index| snapshot.documents.get(*index).is_some())
            .or_else(|| {
                let selected_fund_id = snapshot.selected.fund_id.as_deref()?;
                snapshot
                    .documents
                    .iter()
                    .position(|document| document.fund_id == selected_fund_id)
            })
            .or_else(|| (!snapshot.documents.is_empty()).then_some(0))?;
        let document = snapshot.documents.get(index)?;
        self.documents.table.select(index);
        Some(document_navigation_entry(snapshot, document))
    }

    fn go_back(&mut self) -> CommandOutcome {
        match self.navigation.go_back().cloned() {
            Some(entry) => {
                if let Some(snapshot) = self.snapshot.as_loaded_mut() {
                    apply_subject_selection(snapshot, &entry.subject);
                }
                self.page = entry.page;
                CommandOutcome::Navigated(entry.page)
            }
            None => CommandOutcome::Error("no back history".to_owned()),
        }
    }

    fn go_forward(&mut self) -> CommandOutcome {
        match self.navigation.go_forward().cloned() {
            Some(entry) => {
                if let Some(snapshot) = self.snapshot.as_loaded_mut() {
                    apply_subject_selection(snapshot, &entry.subject);
                }
                self.page = entry.page;
                CommandOutcome::Navigated(entry.page)
            }
            None => CommandOutcome::Error("no forward history".to_owned()),
        }
    }

    fn go_home(&mut self) -> CommandOutcome {
        let entry = self.navigation.go_home().clone();
        if let Some(snapshot) = self.snapshot.as_loaded_mut() {
            apply_subject_selection(snapshot, &entry.subject);
        }
        self.page = entry.page;
        CommandOutcome::Navigated(entry.page)
    }

    fn clear_row_overrides(&mut self, listing_id: &str) {
        self.portfolio
            .overrides
            .retain(|position_override| position_override.listing_id != listing_id);
        let provider = self.provider.clone();
        let overrides = self.portfolio.overrides.clone();

        let Some(snapshot) = self.snapshot.as_loaded_mut() else {
            self.command_feedback = "CMD: no data loaded".to_owned();
            return;
        };

        snapshot.positions = provider.load_positions(&snapshot.workspace.id);
        snapshot.portfolio_summary = provider.load_portfolio_summary(&snapshot.workspace.id);

        if !overrides.is_empty() {
            match apply_position_overrides(
                &mut snapshot.positions,
                &snapshot.portfolio_summary.base_currency,
                &overrides,
            ) {
                Ok(summary) => {
                    snapshot.portfolio_summary = summary;
                }
                Err(err) => {
                    self.command_feedback =
                        format!("CMD: clear loaded, override replay failed: {err}");
                    return;
                }
            }
        }

        snapshot.portfolio_tree = build_investable_tree(
            &snapshot.workspace,
            &snapshot.portfolio_summary,
            &snapshot.positions,
            &snapshot.funds,
            &snapshot.holdings,
        );
        self.portfolio.edit_error = None;
        self.command_feedback = "CMD: cleared row overrides".to_owned();
    }

    fn clear_all_overrides(&mut self) -> CommandOutcome {
        self.portfolio.overrides.clear();
        let provider = self.provider.clone();

        let Some(snapshot) = self.snapshot.as_loaded_mut() else {
            return CommandOutcome::Error("no data loaded".to_owned());
        };

        snapshot.positions = provider.load_positions(&snapshot.workspace.id);
        snapshot.portfolio_summary = provider.load_portfolio_summary(&snapshot.workspace.id);
        snapshot.portfolio_tree = build_investable_tree(
            &snapshot.workspace,
            &snapshot.portfolio_summary,
            &snapshot.positions,
            &snapshot.funds,
            &snapshot.holdings,
        );
        self.portfolio.edit_error = None;
        CommandOutcome::Cleared
    }

    #[allow(clippy::too_many_arguments)]
    fn render_loaded_page(
        ui: &mut egui::Ui,
        snapshot: &mut DashboardSnapshot,
        provider: &MockDataProvider,
        page: &mut Page,
        add_instrument_state: &mut AddInstrumentState,
        alerts_state: &mut AlertsState,
        portfolio_state: &mut PortfolioState,
        hierarchy_state: &mut HierarchyState,
        charts_state: &mut ChartPanelState,
        etfs_state: &mut EtfsState,
        exposure_state: &mut ExposureState,
        dividends_state: &mut DividendsState,
        documents_state: &mut DocumentsState,
        fund_detail_state: &mut FundDetailState,
        holdings_state: &mut HoldingsState,
        jobs_state: &mut JobsState,
        data_operations_state: &mut DataOperationsState,
        curves_state: &mut CurvesState,
        compare_state: &mut CompareState,
        spreads_state: &mut SpreadsState,
        search_state: &mut SearchState,
        settings_state: &mut SettingsState,
        layout_state: &mut LayoutState,
        table_layouts: &mut TableLayoutRegistry,
        selected_workspace_id: &mut String,
        mock_job_message: &mut Option<String>,
        storage_summary: Option<&StorageSummary>,
        api_runtime: &ApiRuntimeStatus,
        api_busy: bool,
    ) -> Option<AppAction> {
        match *page {
            Page::Portfolio => {
                if let Some(action) = portfolio::render(
                    ui,
                    &snapshot.workspace,
                    &mut snapshot.portfolio_summary,
                    &mut snapshot.positions,
                    &mut snapshot.selected,
                    portfolio_state,
                    table_layouts,
                ) {
                    return Some(match action {
                        portfolio::PortfolioAction::OpenPosition {
                            subject,
                            label,
                            breadcrumbs,
                        } => AppAction::Open(
                            NavigationEntry::new(subject, label, Page::FundDetail)
                                .with_breadcrumbs(breadcrumbs),
                        ),
                        portfolio::PortfolioAction::Plot(request) => AppAction::Plot(request),
                        portfolio::PortfolioAction::ClearRowOverrides { listing_id } => {
                            AppAction::ClearRowOverrides(listing_id)
                        }
                        portfolio::PortfolioAction::Navigate(page) => AppAction::Navigate(page),
                        portfolio::PortfolioAction::Feedback(message) => {
                            AppAction::Feedback(message)
                        }
                    });
                }
            }
            Page::Etfs => {
                if let Some(action) = etfs::render(
                    ui,
                    &snapshot.funds,
                    &mut snapshot.selected,
                    etfs_state,
                    table_layouts,
                ) {
                    return match action {
                        etfs::EtfsAction::OpenDetail => {
                            selected_navigation_entry(snapshot, Page::FundDetail)
                                .map(AppAction::Open)
                        }
                        etfs::EtfsAction::Plot(request) => Some(AppAction::Plot(request)),
                        etfs::EtfsAction::Navigate(page) => Some(AppAction::Navigate(page)),
                        etfs::EtfsAction::Feedback(message) => Some(AppAction::Feedback(message)),
                    };
                }
            }
            Page::Hierarchy => {
                if let Some(action) = hierarchy_page::render(
                    ui,
                    &snapshot.portfolio_tree,
                    &snapshot.portfolio_summary.base_currency,
                    hierarchy_state,
                ) {
                    return Some(match action {
                        hierarchy_page::HierarchyAction::Open {
                            subject,
                            label,
                            page,
                            breadcrumbs,
                        } => AppAction::Open(
                            NavigationEntry::new(subject, label, page)
                                .with_breadcrumbs(breadcrumbs),
                        ),
                        hierarchy_page::HierarchyAction::Plot(request) => AppAction::Plot(request),
                    });
                }
            }
            Page::FundDetail => {
                if let Some(action) =
                    fund_detail::render(ui, snapshot, fund_detail_state, table_layouts)
                {
                    return Some(match action {
                        fund_detail::FundDetailAction::Plot(request) => AppAction::Plot(request),
                        fund_detail::FundDetailAction::OpenSubject { subject, label } => {
                            let page = page_for_subject(&subject);
                            AppAction::Open(
                                NavigationEntry::new(subject, label.clone(), page)
                                    .with_breadcrumbs(vec![
                                        snapshot.workspace.name.clone(),
                                        "Fund Detail".to_owned(),
                                        label,
                                    ]),
                            )
                        }
                        fund_detail::FundDetailAction::OpenDocumentIndex(index) => {
                            AppAction::OpenDocumentIndex(index)
                        }
                    });
                }
            }
            Page::Charts => {
                if let Some(action) = charts_page::render(ui, charts_state, snapshot) {
                    return Some(map_chart_action(snapshot, action));
                }
            }
            Page::AddInstrument => {
                if add_instrument::render(
                    ui,
                    add_instrument_state,
                    provider,
                    &mut snapshot.selected,
                ) && let Some(entry) = selected_navigation_entry(snapshot, Page::FundDetail)
                {
                    return Some(AppAction::Open(entry));
                }
            }
            Page::Dividends => {
                dividends::render(
                    ui,
                    &snapshot.distributions,
                    &snapshot.portfolio_summary,
                    &snapshot.funds,
                    &mut snapshot.selected,
                    dividends_state,
                );
            }
            Page::Holdings => {
                if holdings::render(
                    ui,
                    &snapshot.holdings,
                    &snapshot.funds,
                    &mut snapshot.selected,
                    holdings_state,
                ) && let Some(entry) = selected_navigation_entry(snapshot, Page::FundDetail)
                {
                    return Some(AppAction::Open(entry));
                }
            }
            Page::Exposure => {
                if let Some(action) =
                    exposure::render(ui, &snapshot.exposures, exposure_state, table_layouts)
                {
                    return Some(match action {
                        exposure::ExposureAction::PinInspector => AppAction::PinInspector,
                        exposure::ExposureAction::Feedback(message) => AppAction::Feedback(message),
                    });
                }
            }
            Page::Alerts => {
                if let Some(action) = alerts::render(
                    ui,
                    &mut snapshot.alerts,
                    &snapshot.funds,
                    &mut snapshot.selected,
                    alerts_state,
                    table_layouts,
                ) {
                    return match action {
                        alerts::AlertsAction::OpenAffected { ticker: _ } => {
                            selected_navigation_entry(snapshot, Page::FundDetail)
                                .map(AppAction::Open)
                        }
                        alerts::AlertsAction::RunRelatedJob { job_name } => {
                            let message = jobs::mock_run_now_message(&job_name);
                            *mock_job_message = Some(message.clone());
                            *page = Page::Jobs;
                            Some(AppAction::Feedback(message))
                        }
                        alerts::AlertsAction::Feedback(message) => {
                            Some(AppAction::Feedback(message))
                        }
                    };
                }
            }
            Page::Documents => {
                if let Some(action) = documents::render(
                    ui,
                    &snapshot.documents,
                    &snapshot.funds,
                    &mut snapshot.selected,
                    documents_state,
                    table_layouts,
                ) {
                    return map_document_action(snapshot, action);
                }
            }
            Page::DocumentViewer => {
                if let Some(action) = documents::render_viewer(
                    ui,
                    &snapshot.documents,
                    &snapshot.funds,
                    documents_state,
                ) {
                    return map_document_action(snapshot, action);
                }
            }
            Page::Jobs => {
                if let Some(action) = jobs::render(
                    ui,
                    &snapshot.scheduled_jobs,
                    &snapshot.job_runs,
                    mock_job_message,
                    jobs_state,
                ) {
                    return Some(match action {
                        jobs::JobsAction::Feedback(message) => AppAction::Feedback(message),
                    });
                }
            }
            Page::DataOperations => {
                if let Some(action) = data_operations::render(
                    ui,
                    snapshot,
                    data_operations_state,
                    settings_state.data_mode,
                    &settings_state.api_config,
                    api_busy,
                    table_layouts,
                ) {
                    return Some(match action {
                        data_operations::DataOperationsAction::Navigate(page) => {
                            AppAction::Navigate(page)
                        }
                        data_operations::DataOperationsAction::OpenSubject { subject, label } => {
                            let page = page_for_subject(&subject);
                            AppAction::Open(
                                NavigationEntry::new(subject, label.clone(), page)
                                    .with_breadcrumbs(vec![
                                        snapshot.workspace.name.clone(),
                                        "Data Operations".to_owned(),
                                        label,
                                    ]),
                            )
                        }
                        data_operations::DataOperationsAction::RunJob(job_name) => {
                            let message = jobs::mock_run_now_message(&job_name);
                            *mock_job_message = Some(message.clone());
                            AppAction::Feedback(message)
                        }
                        data_operations::DataOperationsAction::Copy { label, text } => {
                            AppAction::Copy { label, text }
                        }
                        data_operations::DataOperationsAction::Feedback(message) => {
                            AppAction::Feedback(message)
                        }
                        data_operations::DataOperationsAction::Refresh => {
                            AppAction::RefreshDataOperations
                        }
                        data_operations::DataOperationsAction::SetDataMode(mode) => {
                            AppAction::SetDataMode(mode)
                        }
                    });
                }
            }
            Page::Analytics => {
                if let Some(action) = analytics::render(ui, snapshot, &portfolio_state.overrides) {
                    return Some(match action {
                        analytics::AnalyticsAction::ClearRowOverrides { listing_id } => {
                            AppAction::ClearRowOverrides(listing_id)
                        }
                    });
                }
            }
            Page::Curves => {
                curves::render(ui, curves_state);
            }
            Page::Compare => {
                ensure_compare_chart_workspace(snapshot, charts_state, compare_state);
                if let Some(action) = charts_page::render(ui, charts_state, snapshot) {
                    return Some(map_chart_action(snapshot, action));
                }
            }
            Page::Spreads => {
                ensure_spread_chart_workspace(snapshot, charts_state, spreads_state);
                if let Some(action) = charts_page::render(ui, charts_state, snapshot) {
                    return Some(map_chart_action(snapshot, action));
                }
            }
            Page::Diffs => {
                diffs::render(ui);
            }
            Page::Search => {
                if let Some(action) = search::render(ui, snapshot, search_state) {
                    return map_search_action(snapshot, action);
                }
            }
            Page::Settings => {
                if let Some(action) = settings::render(
                    ui,
                    settings_state,
                    layout_state,
                    &snapshot.workspaces,
                    selected_workspace_id,
                    storage_summary,
                    api_runtime,
                    api_busy,
                ) {
                    return Some(map_settings_action(action));
                }
            }
        }
        None
    }

    fn execute_command(&mut self) {
        let input = self.command_input.trim().to_owned();
        self.command_history.push(&input);
        self.show_command_help = false;

        let action = self
            .snapshot
            .as_loaded()
            .map(|snapshot| match_command(&input, &snapshot.funds))
            .unwrap_or_else(|| match_command(&input, &[]));

        let outcome = self.apply_command_action(action);
        if matches!(outcome, CommandOutcome::NoMatch(_)) {
            self.show_command_help = true;
        }
        self.command_feedback = outcome.feedback();
    }

    fn apply_command_action(&mut self, action: CommandAction) -> CommandOutcome {
        match action {
            CommandAction::Navigate(page) => {
                let page = self.redirect_legacy_chart_page(page);
                self.page = page;
                CommandOutcome::Navigated(page)
            }
            CommandAction::Search(query) => {
                self.search.set_query_now(query.clone());
                self.page = Page::Search;
                CommandOutcome::Search(query)
            }
            CommandAction::SelectInstrument {
                fund_id,
                listing_id,
            } => {
                let (ticker, entry) = {
                    let Some(snapshot) = self.snapshot.as_loaded_mut() else {
                        return CommandOutcome::Error("no data loaded".to_owned());
                    };
                    let ticker = selection_label(snapshot, &fund_id, listing_id.as_deref());
                    select_in_snapshot(snapshot, fund_id.clone(), listing_id.clone());
                    let entry = selected_navigation_entry(snapshot, Page::FundDetail);
                    (ticker, entry)
                };
                if let Some(entry) = entry {
                    self.open_navigation_entry(entry);
                } else {
                    self.page = Page::FundDetail;
                }
                CommandOutcome::SelectedInstrument {
                    ticker,
                    fund_id,
                    listing_id,
                }
            }
            CommandAction::Compare {
                left_fund_id,
                right_fund_id,
            } => {
                let (left, right, request) = {
                    let Some(snapshot) = self.snapshot.as_loaded() else {
                        return CommandOutcome::Error("no data loaded".to_owned());
                    };
                    let left = selection_label(snapshot, &left_fund_id, None);
                    let right = selection_label(snapshot, &right_fund_id, None);
                    let request = compare::plot_comparison_request(
                        &snapshot.funds,
                        &left_fund_id,
                        &right_fund_id,
                    );
                    (left, right, request)
                };
                let Some(request) = request else {
                    return CommandOutcome::Error("comparison plot subject unavailable".to_owned());
                };
                self.compare.left_fund_id = left_fund_id.clone();
                self.compare.right_fund_id = right_fund_id;
                self.open_plot(request.with_mode(ChartMode::Compare));
                CommandOutcome::Compare { left, right }
            }
            CommandAction::Spread {
                left_fund_id,
                right_fund_id,
                plot,
            } => {
                let (left, right, plot_request) = {
                    let Some(snapshot) = self.snapshot.as_loaded() else {
                        return CommandOutcome::Error("no data loaded".to_owned());
                    };
                    let left = selection_label(snapshot, &left_fund_id, None);
                    let right = selection_label(snapshot, &right_fund_id, None);
                    let request = spreads::plot_spread_request(
                        &snapshot.funds,
                        &left_fund_id,
                        &right_fund_id,
                    );
                    (left, right, request)
                };

                let Some(request) = plot_request else {
                    return CommandOutcome::Error("spread plot subject unavailable".to_owned());
                };
                spreads::set_spread(&mut self.spreads, left_fund_id, right_fund_id);
                self.open_plot(request.with_mode(ChartMode::Spread));

                CommandOutcome::Spread { left, right, plot }
            }
            CommandAction::Diff {
                fund_id,
                listing_id,
                topic,
            } => {
                if let Some(fund_id) = fund_id
                    && let Some(snapshot) = self.snapshot.as_loaded_mut()
                {
                    select_in_snapshot(snapshot, fund_id, listing_id);
                }
                self.page = Page::Diffs;
                CommandOutcome::Diff(if topic.is_empty() {
                    "instrument".to_owned()
                } else {
                    topic
                })
            }
            CommandAction::PlotSubject {
                subject,
                series_kind,
                overlay_series_kinds,
                comparison_subjects,
                label,
            } => {
                let mut request = PlotRequest::new(subject, series_kind, label.clone());
                for overlay in overlay_series_kinds {
                    request = request.with_overlay(overlay);
                }
                for comparison in comparison_subjects {
                    request = request.with_comparison(comparison);
                }
                self.open_plot(request);
                CommandOutcome::Plot(label)
            }
            CommandAction::OverlaySubject {
                subject,
                series_kind,
                label,
            } => {
                if self.charts.active_plot.is_none() {
                    self.open_plot(PlotRequest::new(subject, series_kind, label.clone()));
                    return CommandOutcome::Plot(label);
                }
                self.charts.add_comparison_subject(subject);
                self.page = Page::Charts;
                CommandOutcome::Plot(format!("overlay {label}"))
            }
            CommandAction::PlotSelected {
                series_kind,
                overlay_series_kinds,
            } => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                let label = selected_plot_label(snapshot, series_kind);
                let Some(mut request) = crate::charts::plot_request_from_selected(
                    &snapshot.selected,
                    series_kind,
                    label,
                ) else {
                    return CommandOutcome::Error("no selected instrument".to_owned());
                };
                for overlay in overlay_series_kinds {
                    request = request.with_overlay(overlay);
                }
                let feedback_label = request.label.clone();
                self.open_plot(request);
                CommandOutcome::Plot(feedback_label)
            }
            CommandAction::PlotActive => {
                let Some(request) = self.active_plot_request() else {
                    return CommandOutcome::Error("no active subject".to_owned());
                };
                let label = request.label.clone();
                self.open_plot(request);
                CommandOutcome::Plot(label)
            }
            CommandAction::PlotPortfolio { series_kind } => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                let request = PlotRequest::new(
                    AnalysisSubject::WorkspacePortfolio(snapshot.workspace.id.clone()),
                    series_kind,
                    format!("{} {}", snapshot.workspace.name, series_kind.as_str()),
                );
                let label = request.label.clone();
                self.open_plot(request);
                CommandOutcome::Plot(label)
            }
            CommandAction::SetChartMode(mode) => {
                if self.charts.active_plot.is_none()
                    && let Some(request) = self.active_plot_request()
                {
                    self.open_plot(request.with_mode(mode));
                } else {
                    self.charts.set_mode(mode);
                    self.page = Page::Charts;
                }
                CommandOutcome::Source(format!("chart mode {}", mode.label()))
            }
            CommandAction::SetChartValueMode(mode) => {
                self.charts.value_mode = mode;
                self.page = Page::Charts;
                CommandOutcome::Source(format!("chart value {}", mode.label()))
            }
            CommandAction::SwapComparison => {
                if self.charts.swap_operands() {
                    self.page = Page::Charts;
                    CommandOutcome::Source("chart operands swapped".to_owned())
                } else {
                    CommandOutcome::Error("no comparison or spread subject to swap".to_owned())
                }
            }
            CommandAction::RunJob(job_name) => {
                self.page = Page::Jobs;
                let message = format!("QUEUED mock job {job_name}");
                self.mock_job_message = Some(message);
                CommandOutcome::RunJob(job_name)
            }
            CommandAction::AddInstrument(symbol) => {
                self.add_instrument.symbol = symbol.clone();
                self.add_instrument.result = None;
                self.page = Page::AddInstrument;
                CommandOutcome::AddInstrument(symbol)
            }
            CommandAction::ShowHelp => {
                self.show_command_help = true;
                CommandOutcome::Help
            }
            CommandAction::ShowLegend => {
                self.show_status_legend = true;
                CommandOutcome::Legend
            }
            CommandAction::ShowSources => {
                let summary = self
                    .active_source_summary()
                    .unwrap_or_else(|| "no active source context".to_owned());
                CommandOutcome::Source(summary)
            }
            CommandAction::CopyActive => {
                let Some(payload) = self.copy_active_payload() else {
                    return CommandOutcome::Error("nothing active to copy".to_owned());
                };
                self.copy_to_clipboard(payload)
            }
            CommandAction::CopySelected => {
                let Some(payload) = self.copy_selected_payload() else {
                    return CommandOutcome::Error("nothing selected to copy".to_owned());
                };
                self.copy_to_clipboard(payload)
            }
            CommandAction::CopyChartRow => {
                let Some(point) = self.charts.selected_point.as_ref() else {
                    return CommandOutcome::Error("no selected chart row".to_owned());
                };
                self.copy_to_clipboard(CopyPayload::new(point.label(), point.copy_text()))
            }
            CommandAction::CopyChartValue => {
                if let Some(cell) = self.charts.data_table.selected_cell.as_ref() {
                    return self.copy_to_clipboard(CopyPayload::new(
                        format!("chart cell {}", cell.display_value),
                        cell.raw_value.clone(),
                    ));
                }
                let Some(point) = self.charts.selected_point.as_ref() else {
                    return CommandOutcome::Error("no selected chart value".to_owned());
                };
                self.copy_to_clipboard(CopyPayload::new(point.label(), point.copy_value_text()))
            }
            CommandAction::CopySelectedSeries => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                let Some(request) = self.charts.active_plot.as_ref() else {
                    return CommandOutcome::Error("no chart to copy".to_owned());
                };
                let Some(series_id) = self.charts.selected_series_id.as_ref() else {
                    return CommandOutcome::Error("no selected chart series".to_owned());
                };
                let series_set =
                    crate::charts::chart_series_for_request(request, &snapshot.time_series);
                let Some(series) = series_set
                    .series
                    .iter()
                    .find(|series| series.id == *series_id)
                else {
                    return CommandOutcome::Error("selected chart series unavailable".to_owned());
                };
                let value_mode = effective_chart_value_mode(&self.charts, request);
                let base_date = chart_base_date(series_set.series.as_slice(), request, value_mode);
                self.copy_to_clipboard(CopyPayload::new(
                    format!("series {}", series.label),
                    chart_series_copy_text_for_mode(
                        series,
                        request,
                        value_mode,
                        base_date.as_deref(),
                    ),
                ))
            }
            CommandAction::CopyChart => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                let Some(request) = self.charts.active_plot.as_ref() else {
                    return CommandOutcome::Error("no chart to copy".to_owned());
                };
                let series_set =
                    crate::charts::chart_series_for_request(request, &snapshot.time_series);
                self.copy_to_clipboard(CopyPayload::new(
                    request.label.clone(),
                    chart_workspace_copy_text(
                        request,
                        series_set.series.as_slice(),
                        effective_chart_value_mode(&self.charts, request),
                    ),
                ))
            }
            CommandAction::CopyStatic { label, text } => {
                self.copy_to_clipboard(CopyPayload::new(label, text))
            }
            CommandAction::ClearChartSelection => {
                self.charts.clear_chart_selection();
                self.page = Page::Charts;
                CommandOutcome::Cleared
            }
            CommandAction::SelectNextSeries => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                if charts_page::select_relative_series(&mut self.charts, &snapshot.time_series, 1) {
                    self.page = Page::Charts;
                    CommandOutcome::Source("chart series selected".to_owned())
                } else {
                    CommandOutcome::Error("no chart series to select".to_owned())
                }
            }
            CommandAction::SelectPreviousSeries => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                if charts_page::select_relative_series(&mut self.charts, &snapshot.time_series, -1)
                {
                    self.page = Page::Charts;
                    CommandOutcome::Source("chart series selected".to_owned())
                } else {
                    CommandOutcome::Error("no chart series to select".to_owned())
                }
            }
            CommandAction::SelectNextPoint => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                if charts_page::select_relative_point_in_selected_series(
                    &mut self.charts,
                    &snapshot.time_series,
                    1,
                ) || charts_page::select_relative_point(
                    &mut self.charts,
                    &snapshot.time_series,
                    1,
                ) {
                    self.page = Page::Charts;
                    CommandOutcome::Source("chart point selected".to_owned())
                } else {
                    CommandOutcome::Error("no chart point to select".to_owned())
                }
            }
            CommandAction::SelectPreviousPoint => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                if charts_page::select_relative_point_in_selected_series(
                    &mut self.charts,
                    &snapshot.time_series,
                    -1,
                ) || charts_page::select_relative_point(
                    &mut self.charts,
                    &snapshot.time_series,
                    -1,
                ) {
                    self.page = Page::Charts;
                    CommandOutcome::Source("chart point selected".to_owned())
                } else {
                    CommandOutcome::Error("no chart point to select".to_owned())
                }
            }
            CommandAction::ZoomIn => {
                self.settings.zoom_in();
                self.egui_ctx.set_zoom_factor(self.settings.zoom_factor);
                CommandOutcome::Zoom(self.settings.zoom_percent())
            }
            CommandAction::ZoomOut => {
                self.settings.zoom_out();
                self.egui_ctx.set_zoom_factor(self.settings.zoom_factor);
                CommandOutcome::Zoom(self.settings.zoom_percent())
            }
            CommandAction::ZoomReset => {
                self.settings.reset_zoom();
                self.egui_ctx.set_zoom_factor(self.settings.zoom_factor);
                CommandOutcome::Zoom(self.settings.zoom_percent())
            }
            CommandAction::ShowOverrides => {
                self.page = Page::Analytics;
                CommandOutcome::Navigated(Page::Analytics)
            }
            CommandAction::ClearOverrides => self.clear_all_overrides(),
            CommandAction::ClearSelectedOverrides => {
                let listing_id = self
                    .snapshot
                    .as_loaded()
                    .and_then(|snapshot| snapshot.selected.listing_id.clone());
                if let Some(listing_id) = listing_id {
                    self.clear_row_overrides(&listing_id);
                    CommandOutcome::Cleared
                } else {
                    CommandOutcome::Error("no selected row override".to_owned())
                }
            }
            CommandAction::ClearOverlays => {
                if self.charts.clear_overlays() {
                    self.page = Page::Charts;
                    CommandOutcome::Cleared
                } else {
                    CommandOutcome::Error("no overlays to clear".to_owned())
                }
            }
            CommandAction::Diagnostics => {
                self.page = Page::Analytics;
                CommandOutcome::Navigated(Page::Analytics)
            }
            CommandAction::Clear => {
                self.command_input.clear();
                self.show_command_help = false;
                self.show_status_legend = false;
                CommandOutcome::Cleared
            }
            CommandAction::Refresh => {
                self.refresh_data();
                CommandOutcome::Refreshed
            }
            CommandAction::RefreshDataOperations => {
                self.page = Page::DataOperations;
                self.refresh_data();
                CommandOutcome::Refreshed
            }
            CommandAction::UseMockData => {
                self.settings.data_mode = DataMode::Mock;
                self.page = Page::DataOperations;
                self.refresh_data();
                CommandOutcome::Source("mock data mode".to_owned())
            }
            CommandAction::UseApiData => {
                self.settings.data_mode = DataMode::Api;
                self.page = Page::DataOperations;
                self.refresh_data();
                CommandOutcome::Source("API data mode".to_owned())
            }
            CommandAction::TestBackendConnection => {
                self.test_backend_connection();
                CommandOutcome::Source("backend connection test queued".to_owned())
            }
            CommandAction::CopyBackendUrl => self.copy_to_clipboard(CopyPayload::new(
                "backend URL",
                self.settings.api_config.base_url.clone(),
            )),
            CommandAction::Back => self.go_back(),
            CommandAction::Forward => self.go_forward(),
            CommandAction::Home => self.go_home(),
            CommandAction::Drilldown => {
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                let Some(entry) = selected_navigation_entry(snapshot, Page::FundDetail) else {
                    return CommandOutcome::Error("no selected instrument".to_owned());
                };
                self.open_navigation_entry(entry);
                CommandOutcome::Navigated(Page::FundDetail)
            }
            CommandAction::ToggleInspector => {
                self.layout.show_inspector = !self.layout.show_inspector;
                CommandOutcome::Toggled("inspector", self.layout.show_inspector)
            }
            CommandAction::ResetLayout => {
                self.layout.reset();
                CommandOutcome::Source("layout reset".to_owned())
            }
            CommandAction::ResetTableColumns => {
                self.table_layouts.reset_all();
                CommandOutcome::Source("table columns reset".to_owned())
            }
            CommandAction::ShowAllTableColumns => {
                self.table_layouts.show_all_tables();
                CommandOutcome::Source("all table columns shown".to_owned())
            }
            CommandAction::ClearTableFocus => {
                if self.clear_current_table_focus() {
                    CommandOutcome::Cleared
                } else {
                    CommandOutcome::Error("no focused table row or cell".to_owned())
                }
            }
            CommandAction::PinInspector => {
                let context = self.resolve_follow_inspector_context();
                self.inspector.set_follow_context(context);
                if self.inspector.pin_current() {
                    let label = self
                        .inspector
                        .pinned_context
                        .as_ref()
                        .map(InspectorContext::label)
                        .unwrap_or("-");
                    CommandOutcome::Source(format!("inspector pinned {label}"))
                } else {
                    CommandOutcome::Error("no inspector context to pin".to_owned())
                }
            }
            CommandAction::UnpinInspector => {
                self.inspector.unpin();
                CommandOutcome::Source("inspector following selection".to_owned())
            }
            CommandAction::OpenPinnedInspector => {
                let Some(context) = self.inspector.pinned_context.clone() else {
                    return CommandOutcome::Error("no pinned inspector context".to_owned());
                };
                self.open_inspector_context(context)
            }
            CommandAction::ToggleNav => {
                self.layout.show_left_navigation = !self.layout.show_left_navigation;
                CommandOutcome::Toggled("left navigation", self.layout.show_left_navigation)
            }
            CommandAction::ToggleContext => {
                self.layout.show_context_strip = !self.layout.show_context_strip;
                CommandOutcome::Toggled("context strip", self.layout.show_context_strip)
            }
            CommandAction::ToggleStatus => {
                self.layout.show_status_bar = !self.layout.show_status_bar;
                CommandOutcome::Toggled("status bar", self.layout.show_status_bar)
            }
            CommandAction::OpenSelected => {
                if self.snapshot.as_loaded().is_none() {
                    return CommandOutcome::Error("no data loaded".to_owned());
                }
                if let Some(context) = self.resolve_follow_inspector_context() {
                    return self.open_inspector_context(context);
                }
                let Some(snapshot) = self.snapshot.as_loaded() else {
                    return CommandOutcome::Error("no data loaded".to_owned());
                };
                if let Some(entry) = selected_navigation_entry(snapshot, Page::FundDetail) {
                    self.open_navigation_entry(entry);
                    CommandOutcome::Navigated(Page::FundDetail)
                } else {
                    CommandOutcome::Error("no selected instrument".to_owned())
                }
            }
            CommandAction::OpenActive => {
                let Some(active) = self.active_subject_context() else {
                    return CommandOutcome::Error("no active subject".to_owned());
                };
                let page = page_for_subject(&active.subject);
                self.open_navigation_entry(
                    NavigationEntry::new(active.subject, active.label.clone(), page)
                        .with_breadcrumbs(vec![active.label]),
                );
                CommandOutcome::Navigated(page)
            }
            CommandAction::OpenDocument => {
                let Some(entry) = self.selected_document_entry() else {
                    return CommandOutcome::Error("no selected document".to_owned());
                };
                self.open_navigation_entry(entry);
                CommandOutcome::Navigated(Page::DocumentViewer)
            }
            CommandAction::DismissSelectedAlert => {
                let Some(index) = self.alerts.table.selected_index() else {
                    return CommandOutcome::Error("no selected alert".to_owned());
                };
                let Some(alert) = self
                    .snapshot
                    .as_loaded_mut()
                    .and_then(|snapshot| snapshot.alerts.get_mut(index))
                else {
                    return CommandOutcome::Error("selected alert unavailable".to_owned());
                };
                alerts::dismiss_alert(alert);
                CommandOutcome::Source(format!("alert dismissed {}", alert.id))
            }
            CommandAction::ResolveSelectedAlert => {
                let Some(index) = self.alerts.table.selected_index() else {
                    return CommandOutcome::Error("no selected alert".to_owned());
                };
                let Some(alert) = self
                    .snapshot
                    .as_loaded_mut()
                    .and_then(|snapshot| snapshot.alerts.get_mut(index))
                else {
                    return CommandOutcome::Error("selected alert unavailable".to_owned());
                };
                alerts::resolve_alert(alert);
                CommandOutcome::Source(format!("alert resolved {}", alert.id))
            }
            CommandAction::RunSelectedJob => {
                let Some(job_name) = self.selected_job_command_name() else {
                    return CommandOutcome::Error("no selected job".to_owned());
                };
                let message = jobs::mock_run_now_message(&job_name);
                self.mock_job_message = Some(message);
                self.page = Page::Jobs;
                CommandOutcome::RunJob(job_name)
            }
            CommandAction::NoMatch(query) => CommandOutcome::NoMatch(query),
            CommandAction::Empty => CommandOutcome::Empty,
        }
    }
}

fn select_in_snapshot(
    snapshot: &mut DashboardSnapshot,
    fund_id: String,
    listing_id: Option<String>,
) {
    if let Some(listing_id) = listing_id {
        snapshot.selected.select_listing(fund_id, listing_id);
    } else {
        let first_listing_id = snapshot
            .find_fund_by_id(&fund_id)
            .and_then(|fund| fund.listings.first())
            .map(|listing| listing.id.clone());
        if let Some(listing_id) = first_listing_id {
            snapshot.selected.select_listing(fund_id, listing_id);
        } else {
            snapshot.selected.select_fund(fund_id);
        }
    }
}

fn apply_subject_selection(snapshot: &mut DashboardSnapshot, subject: &AnalysisSubject) {
    match subject {
        AnalysisSubject::Fund(fund_id) => {
            select_in_snapshot(snapshot, fund_id.clone(), None);
        }
        AnalysisSubject::FundListing {
            fund_id,
            listing_id,
        } => {
            select_in_snapshot(snapshot, fund_id.clone(), Some(listing_id.clone()));
        }
        AnalysisSubject::WorkspacePortfolio(_)
        | AnalysisSubject::Holding { .. }
        | AnalysisSubject::Cash(_)
        | AnalysisSubject::SyntheticModel(_) => {}
    }
}

fn home_navigation_entry(workspace: &Workspace) -> NavigationEntry {
    NavigationEntry::new(
        AnalysisSubject::WorkspacePortfolio(workspace.id.clone()),
        workspace.name.clone(),
        Page::Portfolio,
    )
}

fn page_for_subject(subject: &AnalysisSubject) -> Page {
    match subject {
        AnalysisSubject::WorkspacePortfolio(_) => Page::Portfolio,
        AnalysisSubject::Fund(_)
        | AnalysisSubject::FundListing { .. }
        | AnalysisSubject::Holding { .. }
        | AnalysisSubject::Cash(_)
        | AnalysisSubject::SyntheticModel(_) => Page::FundDetail,
    }
}

fn selected_navigation_entry(snapshot: &DashboardSnapshot, page: Page) -> Option<NavigationEntry> {
    let fund_id = snapshot.selected.fund_id.clone()?;
    let listing_id = snapshot.selected.listing_id.clone();
    let label = selection_label(snapshot, &fund_id, listing_id.as_deref());
    let subject = match listing_id {
        Some(listing_id) => AnalysisSubject::FundListing {
            fund_id,
            listing_id,
        },
        None => AnalysisSubject::Fund(fund_id),
    };

    Some(
        NavigationEntry::new(subject, label.clone(), page)
            .with_breadcrumbs(vec![snapshot.workspace.name.clone(), label]),
    )
}

fn map_chart_action(
    snapshot: &DashboardSnapshot,
    action: charts_page::ChartPageAction,
) -> AppAction {
    match action {
        charts_page::ChartPageAction::OpenSubject { subject, label } => {
            let page = page_for_subject(&subject);
            AppAction::Open(
                NavigationEntry::new(subject, label.clone(), page)
                    .with_breadcrumbs(vec![snapshot.workspace.name.clone(), label]),
            )
        }
        charts_page::ChartPageAction::Feedback(message) => AppAction::Feedback(message),
        charts_page::ChartPageAction::PinCurrentSelection => AppAction::PinInspector,
    }
}

fn ensure_compare_chart_workspace(
    snapshot: &DashboardSnapshot,
    charts_state: &mut ChartPanelState,
    compare_state: &mut CompareState,
) {
    if charts_state.active_plot.as_ref().is_some_and(|request| {
        request.mode == ChartMode::Compare && !request.comparison_subjects.is_empty()
    }) {
        return;
    }

    ensure_default_pair_ids(
        &mut compare_state.left_fund_id,
        &mut compare_state.right_fund_id,
        snapshot,
    );
    if let Some(request) = compare::plot_comparison_request(
        &snapshot.funds,
        &compare_state.left_fund_id,
        &compare_state.right_fund_id,
    ) {
        charts_state.set_plot(request.with_mode(ChartMode::Compare));
    } else {
        charts_state.set_mode(ChartMode::Compare);
    }
}

fn ensure_spread_chart_workspace(
    snapshot: &DashboardSnapshot,
    charts_state: &mut ChartPanelState,
    spreads_state: &mut SpreadsState,
) {
    if charts_state.active_plot.as_ref().is_some_and(|request| {
        request.mode == ChartMode::Spread && !request.comparison_subjects.is_empty()
    }) {
        return;
    }

    ensure_default_pair_ids(
        &mut spreads_state.left_fund_id,
        &mut spreads_state.right_fund_id,
        snapshot,
    );
    if let Some(request) = spreads::plot_spread_request(
        &snapshot.funds,
        &spreads_state.left_fund_id,
        &spreads_state.right_fund_id,
    ) {
        charts_state.set_plot(request.with_mode(ChartMode::Spread));
    } else {
        charts_state.set_mode(ChartMode::Spread);
    }
}

fn ensure_default_pair_ids(
    left_fund_id: &mut String,
    right_fund_id: &mut String,
    snapshot: &DashboardSnapshot,
) {
    if left_fund_id.is_empty() {
        *left_fund_id = snapshot
            .funds
            .first()
            .map(|fund| fund.id.clone())
            .unwrap_or_default();
    }
    if right_fund_id.is_empty() {
        *right_fund_id = snapshot
            .funds
            .get(1)
            .or_else(|| snapshot.funds.first())
            .map(|fund| fund.id.clone())
            .unwrap_or_default();
    }
}

fn document_navigation_entry(
    snapshot: &DashboardSnapshot,
    document: &crate::domain::DocumentSnapshot,
) -> NavigationEntry {
    let label = format!("{} {}", document.ticker, document.document_type);
    NavigationEntry::new(
        AnalysisSubject::Fund(document.fund_id.clone()),
        label.clone(),
        Page::DocumentViewer,
    )
    .with_breadcrumbs(vec![
        snapshot.workspace.name.clone(),
        "Documents".to_owned(),
        label,
    ])
}

fn map_document_action(
    snapshot: &DashboardSnapshot,
    action: documents::DocumentsAction,
) -> Option<AppAction> {
    match action {
        documents::DocumentsAction::OpenDocument {
            fund_id,
            ticker,
            document_type,
        } => {
            let document = snapshot.documents.iter().find(|document| {
                document.fund_id == fund_id
                    && document.ticker == ticker
                    && document.document_type == document_type
            })?;
            Some(AppAction::Open(document_navigation_entry(
                snapshot, document,
            )))
        }
        documents::DocumentsAction::OpenRelatedFund { fund_id } => {
            let label = selection_label(snapshot, &fund_id, None);
            Some(AppAction::Open(
                NavigationEntry::new(
                    AnalysisSubject::Fund(fund_id),
                    label.clone(),
                    Page::FundDetail,
                )
                .with_breadcrumbs(vec![snapshot.workspace.name.clone(), label]),
            ))
        }
        documents::DocumentsAction::Back => Some(AppAction::Back),
        documents::DocumentsAction::ShowChanges => Some(AppAction::Navigate(Page::Diffs)),
        documents::DocumentsAction::Feedback(message) => Some(AppAction::Feedback(message)),
    }
}

fn map_search_action(
    snapshot: &DashboardSnapshot,
    action: search::SearchAction,
) -> Option<AppAction> {
    match action {
        search::SearchAction::Open(result) => {
            if result.result_type == search::SearchResultType::Document {
                return result.row_index.map(AppAction::OpenDocumentIndex);
            }
            Some(AppAction::Open(
                NavigationEntry::new(
                    result.subject.clone(),
                    result.label.clone(),
                    result.target_page,
                )
                .with_breadcrumbs(vec![
                    snapshot.workspace.name.clone(),
                    "Search".to_owned(),
                    result.label,
                ]),
            ))
        }
        search::SearchAction::MakeActive(result) => Some(AppAction::MakeActive {
            subject: result.subject,
            label: result.label,
        }),
        search::SearchAction::Plot(request) => Some(AppAction::Plot(request)),
        search::SearchAction::Copy { label, text } => Some(AppAction::Copy { label, text }),
        search::SearchAction::ShowSource(message) => {
            Some(AppAction::Feedback(format!("SOURCE: {message}")))
        }
    }
}

fn map_settings_action(action: settings::SettingsAction) -> AppAction {
    match action {
        settings::SettingsAction::ThemeChanged(theme) => {
            AppAction::Feedback(format!("SETTINGS: theme changed to {}", theme.as_str()))
        }
        settings::SettingsAction::DensityChanged(density) => {
            AppAction::Feedback(format!("SETTINGS: density changed to {}", density.as_str()))
        }
        settings::SettingsAction::SourcePolicyChanged(policy) => {
            AppAction::Feedback(format!("SETTINGS: source policy {}", policy.label()))
        }
        settings::SettingsAction::ZoomChanged(percent) => {
            AppAction::Feedback(format!("VIEW: zoom {percent}%"))
        }
        settings::SettingsAction::StoragePathCopied(label) => {
            AppAction::Feedback(format!("COPIED: storage {label}"))
        }
        settings::SettingsAction::DataModeChanged(mode) => AppAction::SetDataMode(mode),
        settings::SettingsAction::AutoRefreshChanged(enabled) => AppAction::Feedback(format!(
            "SETTINGS: Data Operations auto-refresh {}",
            if enabled { "enabled" } else { "disabled" }
        )),
        settings::SettingsAction::RefreshDataOperations => AppAction::RefreshDataOperations,
        settings::SettingsAction::TestConnection => AppAction::TestBackendConnection,
        settings::SettingsAction::PendingApplied => {
            AppAction::Feedback("SETTINGS: saved".to_owned())
        }
        settings::SettingsAction::PendingApplyFailed(message) => {
            AppAction::Feedback(format!("SETTINGS: {message}"))
        }
        settings::SettingsAction::PendingReverted => {
            AppAction::Feedback("SETTINGS: reverted".to_owned())
        }
    }
}

fn selected_plot_label(snapshot: &DashboardSnapshot, series_kind: TimeSeriesKind) -> String {
    snapshot
        .selected
        .fund_id
        .as_deref()
        .map(|fund_id| selection_label(snapshot, fund_id, snapshot.selected.listing_id.as_deref()))
        .map(|label| format!("{label} {}", series_kind.as_str()))
        .unwrap_or_else(|| format!("Selected {}", series_kind.as_str()))
}

fn selection_label(
    snapshot: &DashboardSnapshot,
    fund_id: &str,
    listing_id: Option<&str>,
) -> String {
    listing_id
        .and_then(|listing_id| {
            snapshot
                .funds
                .iter()
                .flat_map(|fund| fund.listings.iter())
                .find(|listing| listing.id == listing_id)
                .map(|listing| listing.ticker.clone())
        })
        .or_else(|| {
            snapshot
                .find_fund_by_id(fund_id)
                .and_then(|fund| fund.listings.first())
                .map(|listing| listing.ticker.clone())
        })
        .or_else(|| {
            snapshot
                .find_fund_by_id(fund_id)
                .map(|fund| fund.name.clone())
        })
        .unwrap_or_else(|| fund_id.to_owned())
}

fn compact_fund_label(fund: &crate::domain::Fund) -> String {
    let tickers = fund
        .listings
        .iter()
        .map(|listing| listing.ticker.as_str())
        .collect::<Vec<_>>()
        .join("/");
    if tickers.is_empty() {
        fund.isin.clone()
    } else {
        tickers
    }
}

fn subject_for_ticker(snapshot: &DashboardSnapshot, ticker: &str) -> Option<AnalysisSubject> {
    snapshot
        .find_fund_by_ticker(ticker)
        .map(|(fund, listing)| AnalysisSubject::FundListing {
            fund_id: fund.id.clone(),
            listing_id: listing.id.clone(),
        })
}

fn subject_for_fund_ticker(
    snapshot: &DashboardSnapshot,
    fund_id: &str,
    ticker: &str,
) -> Option<AnalysisSubject> {
    snapshot.find_fund_by_id(fund_id).and_then(|fund| {
        fund.listings
            .iter()
            .find(|listing| listing.ticker.eq_ignore_ascii_case(ticker))
            .or_else(|| fund.listings.first())
            .map(|listing| AnalysisSubject::FundListing {
                fund_id: fund.id.clone(),
                listing_id: listing.id.clone(),
            })
    })
}

fn active_from_selected(snapshot: &DashboardSnapshot) -> Option<SubjectContext> {
    let fund_id = snapshot.selected.fund_id.clone()?;
    let listing_id = snapshot.selected.listing_id.clone();
    let label = selection_label(snapshot, &fund_id, listing_id.as_deref());
    let subject = match listing_id {
        Some(listing_id) => AnalysisSubject::FundListing {
            fund_id,
            listing_id,
        },
        None => AnalysisSubject::Fund(fund_id),
    };
    Some(SubjectContext {
        subject,
        label,
        tooltip: snapshot.selected_subject_tooltip(),
    })
}

fn selected_subject_payload(snapshot: &DashboardSnapshot) -> Option<CopyPayload> {
    if snapshot.selected.is_empty() {
        return None;
    }
    let label = snapshot.selected_subject_label();
    Some(CopyPayload::new(label.clone(), label))
}

fn chart_workspace_copy_text(
    request: &PlotRequest,
    series: &[ChartSeriesSpec],
    value_mode: ChartValueMode,
) -> String {
    let overlays = request
        .comparison_subjects
        .iter()
        .map(AnalysisSubject::short_id)
        .collect::<Vec<_>>()
        .join(", ");
    [
        format!("label\t{}", request.label),
        format!("mode\t{}", request.mode.label()),
        format!("primary\t{}", request.subject.short_id()),
        format!(
            "comparison_subjects\t{}",
            if overlays.is_empty() {
                "-".to_owned()
            } else {
                overlays
            }
        ),
        format!("series_kind\t{}", request.series_kind.as_str()),
        format!("value_mode\t{}", value_mode.label()),
        format!("range\t{}", request.options.range.as_str()),
        "x_mode\tdate".to_owned(),
        format!("series_count\t{}", series.len()),
    ]
    .join("\n")
}

fn effective_chart_value_mode(
    chart_state: &ChartPanelState,
    request: &PlotRequest,
) -> ChartValueMode {
    if request.mode == ChartMode::Spread {
        ChartValueMode::Raw
    } else {
        chart_state.value_mode
    }
}

fn chart_base_date(
    series: &[ChartSeriesSpec],
    request: &PlotRequest,
    value_mode: ChartValueMode,
) -> Option<String> {
    if value_mode == ChartValueMode::Raw {
        return None;
    }

    let visible = series
        .iter()
        .map(|series| chart_visible_points(series, request.options.range))
        .collect::<Vec<_>>();
    let visible_refs = visible.iter().map(Vec::as_slice).collect::<Vec<_>>();
    crate::compute::charts::first_valid_common_base_date(&visible_refs)
}

fn chart_visible_points(
    series: &ChartSeriesSpec,
    range: crate::charts::PlotRange,
) -> Vec<crate::timeseries::TimeSeriesPoint> {
    crate::charts::visible_points_for_range(&series.points, range)
}

fn chart_series_copy_text_for_mode(
    series: &ChartSeriesSpec,
    request: &PlotRequest,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
) -> String {
    let mut lines = vec![
        "date\tseries\tdisplay_value\tdisplay_unit\traw_value\traw_unit\tsource\tstatus\tkind\trole\tvalue_mode\tx_mode"
            .to_owned(),
    ];
    let display_unit = value_mode.display_unit(&series.unit);
    let visible = chart_visible_points(series, request.options.range);
    lines.extend(
        crate::compute::charts::display_points_for_mode(&visible, value_mode, base_date)
            .into_iter()
            .map(|point| {
                [
                    point.date,
                    series.label.clone(),
                    point.value.to_string(),
                    display_unit.clone(),
                    point.raw_value.to_string(),
                    series.unit.clone(),
                    point.source,
                    point.status,
                    series.kind.as_str().to_owned(),
                    series.role.label().to_owned(),
                    value_mode.label().to_owned(),
                    "date".to_owned(),
                ]
                .join("\t")
            }),
    );
    lines.join("\n")
}

fn portfolio_row_copy_text(position: &crate::domain::Position, base_currency: &str) -> String {
    [
        position.ticker.clone(),
        position.isin.clone(),
        position.name.clone(),
        crate::format::fmt_money(base_currency, position.market_value),
        crate::format::fmt_percent(position.portfolio_weight_pct),
        crate::format::fmt_source(&position.source),
    ]
    .join("\t")
}

fn fund_row_copy_text(fund: &crate::domain::Fund) -> String {
    let tickers = fund
        .listings
        .iter()
        .map(|listing| listing.ticker.as_str())
        .collect::<Vec<_>>()
        .join("/");
    [
        tickers,
        fund.isin.clone(),
        fund.name.clone(),
        fund.provider.clone(),
        crate::format::fmt_source(&fund.source),
    ]
    .join("\t")
}

fn document_row_copy_text(document: &crate::domain::DocumentSnapshot) -> String {
    [
        document.ticker.clone(),
        document.document_type.clone(),
        document.latest_date.clone(),
        crate::format::fmt_status(&document.status),
        documents::document_mock_uri(document),
        document.content_hash_change.clone(),
    ]
    .join("\t")
}

fn job_run_copy_text(run: &crate::domain::JobRun) -> String {
    [
        run.id.clone(),
        run.job_type.clone(),
        run.status.as_str().to_owned(),
        run.started.clone(),
        run.finished.clone().unwrap_or_else(|| "-".to_owned()),
        run.message.clone(),
    ]
    .join("\t")
}

fn scheduled_job_copy_text(job: &crate::domain::ScheduledJob) -> String {
    [
        job.name.clone(),
        job.job_type.clone(),
        job.cron_schedule.clone(),
        job.last_run.clone(),
        job.next_run.clone(),
        crate::pages::bool_text(job.active).to_owned(),
    ]
    .join("\t")
}

fn data_kind_for_series(series_kind: TimeSeriesKind) -> DataKind {
    match series_kind {
        TimeSeriesKind::Price | TimeSeriesKind::MarketValue => DataKind::Price,
        TimeSeriesKind::Nav => DataKind::Nav,
        TimeSeriesKind::Distribution => DataKind::Distributions,
        TimeSeriesKind::FxRate => DataKind::Fx,
        TimeSeriesKind::PortfolioValue
        | TimeSeriesKind::PortfolioPnL
        | TimeSeriesKind::ProjectedIncome
        | TimeSeriesKind::CurvePoint
        | TimeSeriesKind::Yield
        | TimeSeriesKind::Custom => DataKind::Derived,
    }
}

fn consume_shortcut(ctx: &egui::Context, key: egui::Key) -> bool {
    let shortcut = egui::KeyboardShortcut::new(egui::Modifiers::COMMAND, key);
    ctx.input_mut(|input| input.consume_shortcut(&shortcut))
}

fn status_legend_entries() -> Vec<(&'static str, &'static str)> {
    vec![
        ("FRESH", "data is current enough for display"),
        ("STALE", "data needs refresh"),
        ("PENDING", "resolver/backfill pending"),
        ("FAILED", "job/source failed"),
        ("MOCK", "local mock data"),
        ("SEED", "seed data loaded at startup"),
        ("EST", "estimated or computed locally"),
        ("MANUAL", "manually overridden"),
        ("CONFLICT", "multiple sources disagree"),
        ("AMBIG", "ambiguous identifier resolution"),
        ("MISSING", "expected field missing"),
        ("QUEUED", "mock job is waiting to run"),
        ("RUNNING", "job or refresh active"),
        ("DONE", "job completed"),
        ("DERIVED", "computed from effective inputs"),
        ("SRC: seed", "seeded local fixture"),
        ("SRC: mock", "mock-only local provider"),
        ("SRC: issuer", "issuer/provider-originated mock field"),
        ("SRC: stooq", "market price source placeholder"),
        ("SRC: manual", "local manual override"),
        ("SRC: derived", "computed locally from effective data"),
    ]
}

#[derive(Clone, Debug, Default, PartialEq)]
struct AppPreferences {
    selected_workspace_id: Option<String>,
    active_page: Option<Page>,
    show_left_navigation: Option<bool>,
    show_inspector: Option<bool>,
    show_context_strip: Option<bool>,
    show_status_bar: Option<bool>,
    inspector_width: Option<f32>,
    layout_revision: Option<u64>,
    table_layouts: Option<TableLayoutRegistry>,
    density: Option<settings::DensityPreference>,
    theme: Option<settings::ThemePreference>,
    source_policy: Option<SourcePolicy>,
    zoom_factor: Option<f32>,
    base_currency: Option<String>,
    refresh_interval_minutes: Option<u32>,
    api_base_url: Option<String>,
    data_mode: Option<DataMode>,
    api_timeout_ms: Option<u64>,
    auto_refresh_data_operations: Option<bool>,
    api_auth_mode: Option<AuthMode>,
    workspace_header_value: Option<String>,
}

impl AppPreferences {
    fn load(storage: Option<&dyn eframe::Storage>) -> Option<Self> {
        storage
            .and_then(|storage| storage.get_string(PREFERENCES_KEY))
            .map(|encoded| Self::decode(&encoded))
    }

    fn load_from_storage(
        storage: &StorageManager,
    ) -> Result<Option<Self>, crate::storage::StorageError> {
        let settings = storage.load_settings()?;
        let ui_state = storage.load_ui_state()?;
        Ok(Self::from_storage_documents(settings, ui_state))
    }

    fn from_storage_documents(
        settings: Option<StoredAppSettings>,
        ui_state: Option<StoredUiState>,
    ) -> Option<Self> {
        if settings.is_none() && ui_state.is_none() {
            return None;
        }

        let mut preferences = Self::default();
        if let Some(settings) = settings {
            preferences.theme = settings
                .theme
                .as_deref()
                .and_then(settings::ThemePreference::from_str);
            preferences.density = settings
                .density
                .as_deref()
                .and_then(settings::DensityPreference::from_str);
            preferences.zoom_factor = settings.zoom_factor;
            preferences.source_policy = settings
                .source_policy
                .as_deref()
                .and_then(SourcePolicy::decode);
            preferences.base_currency = settings.base_currency;
            preferences.refresh_interval_minutes = settings.refresh_interval_minutes;
            preferences.api_base_url = settings.api_base_url;
            preferences.data_mode = settings.data_mode.as_deref().and_then(DataMode::from_str);
            preferences.api_timeout_ms = settings.api_timeout_ms;
            preferences.auto_refresh_data_operations = settings.auto_refresh_data_operations;
            preferences.api_auth_mode = settings
                .api_auth_mode
                .as_deref()
                .and_then(auth_mode_from_str);
            preferences.workspace_header_value = settings.workspace_header_value;
        }
        if let Some(ui_state) = ui_state {
            preferences.selected_workspace_id = ui_state.selected_workspace_id;
            preferences.active_page = ui_state.last_active_page.as_deref().and_then(page_from_key);
            preferences.show_left_navigation = ui_state.show_left_navigation;
            preferences.show_inspector = ui_state.show_inspector;
            preferences.show_context_strip = ui_state.show_context_strip;
            preferences.show_status_bar = ui_state.show_status_bar;
            preferences.inspector_width = ui_state.inspector_width;
            preferences.layout_revision = ui_state.layout_revision;
            preferences.table_layouts = ui_state.table_layouts;
        }

        Some(preferences)
    }

    fn from_state(
        selected_workspace_id: &str,
        layout: &LayoutState,
        table_layouts: &TableLayoutRegistry,
        settings: &SettingsState,
        active_page: Page,
    ) -> Self {
        Self {
            selected_workspace_id: Some(selected_workspace_id.to_owned()),
            active_page: Some(active_page),
            show_left_navigation: Some(layout.show_left_navigation),
            show_inspector: Some(layout.show_inspector),
            show_context_strip: Some(layout.show_context_strip),
            show_status_bar: Some(layout.show_status_bar),
            inspector_width: Some(layout.inspector_width),
            layout_revision: Some(layout.revision),
            table_layouts: Some(table_layouts.clone()),
            density: Some(settings.density),
            theme: Some(settings.theme),
            source_policy: Some(settings.source_policy.clone()),
            zoom_factor: Some(settings.zoom_factor),
            base_currency: Some(settings.base_currency.clone()),
            refresh_interval_minutes: Some(settings.refresh_interval_minutes),
            api_base_url: Some(settings.api_config.base_url.clone()),
            data_mode: Some(settings.data_mode),
            api_timeout_ms: Some(settings.api_config.timeout_ms),
            auto_refresh_data_operations: Some(settings.auto_refresh_data_operations),
            api_auth_mode: Some(settings.api_config.auth_mode),
            workspace_header_value: Some(settings.api_config.workspace_header_value.clone()),
        }
    }

    fn apply(&self, settings: &mut SettingsState, layout: &mut LayoutState) {
        if let Some(show_left_navigation) = self.show_left_navigation {
            layout.show_left_navigation = show_left_navigation;
        }
        if let Some(show_inspector) = self.show_inspector {
            layout.show_inspector = show_inspector;
        }
        if let Some(show_context_strip) = self.show_context_strip {
            layout.show_context_strip = show_context_strip;
        }
        if let Some(show_status_bar) = self.show_status_bar {
            layout.show_status_bar = show_status_bar;
        }
        if let Some(inspector_width) = self.inspector_width.filter(|width| width.is_finite()) {
            layout.inspector_width = inspector_width.clamp(
                metrics::INSPECTOR_NARROW_MIN_WIDTH,
                metrics::INSPECTOR_MAX_WIDTH,
            );
        }
        if let Some(layout_revision) = self.layout_revision {
            layout.revision = layout_revision;
        }
        if let Some(density) = self.density {
            settings.density = density;
        }
        if let Some(theme) = self.theme {
            settings.theme = theme;
        }
        if let Some(source_policy) = self.source_policy.clone() {
            settings.source_policy = source_policy;
        }
        if let Some(zoom_factor) = self.zoom_factor {
            settings.set_zoom_factor(zoom_factor);
        }
        if let Some(base_currency) = self.base_currency.as_ref() {
            settings.base_currency.clone_from(base_currency);
            settings.pending_base_currency.clone_from(base_currency);
        }
        if let Some(refresh_interval_minutes) = self.refresh_interval_minutes {
            settings.refresh_interval_minutes = refresh_interval_minutes;
            settings.pending_refresh_interval_minutes = refresh_interval_minutes;
        }
        if let Some(api_base_url) = self.api_base_url.as_ref()
            && let Ok(api_base_url) = normalize_backend_base_url(api_base_url)
        {
            settings.api_config.base_url.clone_from(&api_base_url);
            settings.pending_api_config.base_url = api_base_url;
        }
        if let Some(data_mode) = self.data_mode {
            settings.data_mode = data_mode;
        }
        if let Some(api_timeout_ms) = self.api_timeout_ms {
            settings.api_config.timeout_ms = api_timeout_ms;
            settings.pending_api_config.timeout_ms = api_timeout_ms;
        }
        if let Some(auto_refresh_data_operations) = self.auto_refresh_data_operations {
            settings.auto_refresh_data_operations = auto_refresh_data_operations;
        }
        if let Some(api_auth_mode) = self.api_auth_mode {
            settings.api_config.auth_mode = api_auth_mode;
            settings.pending_api_config.auth_mode = api_auth_mode;
        }
        if let Some(workspace_header_value) = self.workspace_header_value.as_ref() {
            settings
                .api_config
                .workspace_header_value
                .clone_from(workspace_header_value);
            settings
                .pending_api_config
                .workspace_header_value
                .clone_from(workspace_header_value);
        }
    }

    fn to_storage_documents(&self) -> (StoredAppSettings, StoredUiState) {
        (
            StoredAppSettings {
                theme: self.theme.map(|value| value.as_str().to_owned()),
                density: self.density.map(|value| value.as_str().to_owned()),
                zoom_factor: self.zoom_factor.map(settings::clamp_zoom_factor),
                source_policy: self.source_policy.as_ref().map(SourcePolicy::encode),
                base_currency: self.base_currency.clone(),
                refresh_interval_minutes: self.refresh_interval_minutes,
                api_base_url: self.api_base_url.clone(),
                data_mode: self.data_mode.map(|value| value.as_str().to_owned()),
                api_timeout_ms: self.api_timeout_ms,
                auto_refresh_data_operations: self.auto_refresh_data_operations,
                api_auth_mode: self.api_auth_mode.map(|value| value.as_str().to_owned()),
                workspace_header_value: self.workspace_header_value.clone(),
            },
            StoredUiState {
                selected_workspace_id: self.selected_workspace_id.clone(),
                last_active_page: self.active_page.map(page_to_key).map(str::to_owned),
                show_left_navigation: self.show_left_navigation,
                show_inspector: self.show_inspector,
                show_context_strip: self.show_context_strip,
                show_status_bar: self.show_status_bar,
                inspector_width: self.inspector_width,
                layout_revision: self.layout_revision,
                table_layouts: self.table_layouts.clone(),
            },
        )
    }

    fn encode(&self) -> String {
        [
            self.selected_workspace_id
                .as_ref()
                .map(|value| format!("workspace={value}")),
            self.active_page
                .map(|value| format!("page={}", page_to_key(value))),
            self.show_left_navigation
                .map(|value| format!("show_left_navigation={}", encode_bool(value))),
            self.show_inspector
                .map(|value| format!("show_inspector={}", encode_bool(value))),
            self.show_context_strip
                .map(|value| format!("show_context_strip={}", encode_bool(value))),
            self.show_status_bar
                .map(|value| format!("show_status_bar={}", encode_bool(value))),
            self.inspector_width
                .map(|value| format!("inspector_width={value:.1}")),
            self.layout_revision
                .map(|value| format!("layout_revision={value}")),
            self.density
                .map(|value| format!("density={}", value.as_str())),
            self.theme.map(|value| format!("theme={}", value.as_str())),
            self.source_policy
                .as_ref()
                .map(|value| format!("source_policy={}", value.encode())),
            self.zoom_factor
                .map(|value| format!("zoom_factor={:.2}", settings::clamp_zoom_factor(value))),
            self.base_currency
                .as_ref()
                .map(|value| format!("base_currency={value}")),
            self.refresh_interval_minutes
                .map(|value| format!("refresh_interval_minutes={value}")),
            self.api_base_url
                .as_ref()
                .map(|value| format!("api_base_url={value}")),
            self.data_mode
                .map(|value| format!("data_mode={}", value.as_str())),
            self.api_timeout_ms
                .map(|value| format!("api_timeout_ms={value}")),
            self.auto_refresh_data_operations
                .map(|value| format!("auto_refresh_data_operations={}", encode_bool(value))),
            self.api_auth_mode
                .map(|value| format!("api_auth_mode={}", value.as_str())),
            self.workspace_header_value
                .as_ref()
                .map(|value| format!("workspace_header_value={value}")),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join("\n")
    }

    fn decode(encoded: &str) -> Self {
        let mut preferences = Self::default();
        for line in encoded.lines() {
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            match key {
                "workspace" if !value.trim().is_empty() => {
                    preferences.selected_workspace_id = Some(value.trim().to_owned());
                }
                "page" => {
                    preferences.active_page = page_from_key(value.trim());
                }
                "show_left_navigation" => {
                    preferences.show_left_navigation = decode_bool(value);
                }
                "show_inspector" => {
                    preferences.show_inspector = decode_bool(value);
                }
                "show_context_strip" => {
                    preferences.show_context_strip = decode_bool(value);
                }
                "show_status_bar" => {
                    preferences.show_status_bar = decode_bool(value);
                }
                "inspector_width" => {
                    preferences.inspector_width = value.trim().parse::<f32>().ok();
                }
                "layout_revision" => {
                    preferences.layout_revision = value.trim().parse::<u64>().ok();
                }
                "density" => {
                    preferences.density = settings::DensityPreference::from_str(value.trim());
                }
                "theme" => {
                    preferences.theme = settings::ThemePreference::from_str(value.trim());
                }
                "source_policy" => {
                    preferences.source_policy = SourcePolicy::decode(value.trim());
                }
                "zoom_factor" => {
                    preferences.zoom_factor = value.trim().parse::<f32>().ok();
                }
                "base_currency" if !value.trim().is_empty() => {
                    preferences.base_currency = Some(value.trim().to_owned());
                }
                "refresh_interval_minutes" => {
                    preferences.refresh_interval_minutes = value.trim().parse::<u32>().ok();
                }
                "api_base_url" if !value.trim().is_empty() => {
                    preferences.api_base_url = Some(value.trim().to_owned());
                }
                "data_mode" => {
                    preferences.data_mode = DataMode::from_str(value);
                }
                "api_timeout_ms" => {
                    preferences.api_timeout_ms = value.trim().parse::<u64>().ok();
                }
                "auto_refresh_data_operations" => {
                    preferences.auto_refresh_data_operations = decode_bool(value);
                }
                "api_auth_mode" => {
                    preferences.api_auth_mode = auth_mode_from_str(value.trim());
                }
                "workspace_header_value" if !value.trim().is_empty() => {
                    preferences.workspace_header_value = Some(value.trim().to_owned());
                }
                _ => {}
            }
        }
        preferences
    }
}

fn encode_bool(value: bool) -> &'static str {
    if value { "1" } else { "0" }
}

fn page_to_key(page: Page) -> &'static str {
    match page {
        Page::Portfolio => "portfolio",
        Page::Etfs => "etfs",
        Page::Hierarchy => "hierarchy",
        Page::FundDetail => "fund_detail",
        Page::Charts => "charts",
        Page::AddInstrument => "add_instrument",
        Page::Dividends => "dividends",
        Page::Holdings => "holdings",
        Page::Exposure => "exposure",
        Page::DataOperations => "data_operations",
        Page::Alerts => "alerts",
        Page::Documents => "documents",
        Page::DocumentViewer => "document_viewer",
        Page::Jobs => "jobs",
        Page::Analytics => "analytics",
        Page::Curves => "curves",
        Page::Compare => "compare",
        Page::Spreads => "spreads",
        Page::Diffs => "changes",
        Page::Search => "search",
        Page::Settings => "settings",
    }
}

fn page_from_key(value: &str) -> Option<Page> {
    match value {
        "portfolio" => Some(Page::Portfolio),
        "etfs" | "instruments" => Some(Page::Etfs),
        "hierarchy" => Some(Page::Hierarchy),
        "fund_detail" | "fund" => Some(Page::FundDetail),
        "charts" => Some(Page::Charts),
        "add_instrument" | "add" => Some(Page::AddInstrument),
        "dividends" => Some(Page::Dividends),
        "holdings" => Some(Page::Holdings),
        "exposure" => Some(Page::Exposure),
        "data_operations" | "operations" => Some(Page::DataOperations),
        "alerts" => Some(Page::Alerts),
        "documents" => Some(Page::Documents),
        "document_viewer" => Some(Page::DocumentViewer),
        "jobs" => Some(Page::Jobs),
        "analytics" => Some(Page::Analytics),
        "curves" => Some(Page::Curves),
        "compare" => Some(Page::Compare),
        "spreads" => Some(Page::Spreads),
        "changes" | "diffs" => Some(Page::Diffs),
        "search" => Some(Page::Search),
        "settings" => Some(Page::Settings),
        _ => None,
    }
}

fn auth_mode_from_str(value: &str) -> Option<AuthMode> {
    AuthMode::ALL
        .into_iter()
        .find(|mode| mode.as_str() == value)
}

fn decode_bool(value: &str) -> Option<bool> {
    match value.trim() {
        "1" | "true" | "yes" => Some(true),
        "0" | "false" | "no" => Some(false),
        _ => None,
    }
}

fn command_input_has_focus(ctx: &egui::Context) -> bool {
    ctx.memory(|memory| memory.has_focus(egui::Id::new(COMMAND_INPUT_ID)))
}

fn compact_last_refresh_label(value: &str) -> &str {
    let value = value.trim();
    value
        .strip_suffix(" mock")
        .or_else(|| value.strip_suffix(" MOCK"))
        .or_else(|| value.strip_suffix(" Mock"))
        .unwrap_or(value)
}

impl eframe::App for MimerApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.poll_refresh_worker();
        self.poll_api_test_worker();
        configure_context(
            &self.egui_ctx,
            self.settings.theme,
            self.settings.density,
            self.settings.zoom_factor,
        );
        self.handle_shortcuts(ui);
        let workspace_before = self.selected_workspace_id.clone();

        egui::Panel::top("mimer_menu_bar")
            .exact_size(metrics::MENU_BAR_HEIGHT)
            .frame(style::shell_bar_frame(ui.style()))
            .show_inside(ui, |ui| self.top_menu_bar(ui));

        egui::Panel::top("mimer_top_toolbar")
            .exact_size(metrics::TOOLBAR_HEIGHT)
            .frame(style::shell_bar_frame(ui.style()))
            .show_inside(ui, |ui| self.top_toolbar(ui));

        if self.layout.show_context_strip {
            egui::Panel::top("mimer_context_strip")
                .exact_size(metrics::CONTEXT_STRIP_HEIGHT)
                .frame(style::context_strip_frame(ui.style()))
                .show_inside(ui, |ui| self.context_strip(ui));
        }

        if self.layout.show_status_bar {
            egui::Panel::bottom("mimer_status_bar")
                .exact_size(metrics::STATUS_BAR_HEIGHT)
                .frame(style::status_bar_frame(ui.style()))
                .show_inside(ui, |ui| self.status_bar(ui));
        }

        let layout_width = ui.max_rect().width();
        if self.layout.show_left_navigation {
            egui::Panel::left(egui::Id::new((
                "mimer_left_navigation",
                self.layout.revision,
            )))
            .default_size(metrics::LEFT_RAIL_DEFAULT_WIDTH)
            .min_size(metrics::LEFT_RAIL_MIN_WIDTH)
            .max_size(metrics::LEFT_RAIL_MAX_WIDTH)
            .resizable(true)
            .show_separator_line(true)
            .frame(style::side_panel_frame(ui.style()))
            .show_inside(ui, |ui| self.left_navigation(ui));
        }

        if self.layout.show_inspector {
            let (inspector_min, inspector_max) = self.layout.inspector_width_bounds(layout_width);
            let inspector_width = self.layout.clamped_inspector_width(layout_width);
            let response = egui::Panel::right(egui::Id::new((
                "mimer_right_inspector",
                self.layout.revision,
            )))
            .default_size(inspector_width)
            .min_size(inspector_min)
            .max_size(inspector_max)
            .resizable(true)
            .show_separator_line(true)
            .frame(style::side_panel_frame(ui.style()))
            .show_inside(ui, |ui| self.right_inspector(ui));
            self.layout
                .record_inspector_width(response.response.rect.width(), layout_width);
        }

        egui::CentralPanel::default()
            .frame(style::content_frame(ui.style()))
            .show_inside(ui, |ui| {
                self.central_content(ui);
            });

        self.command_help_window(ui.ctx());
        self.status_legend_window(ui.ctx());
        self.about_window(ui.ctx());

        if workspace_before != self.selected_workspace_id {
            self.refresh_data();
        }
        let data_operations_open = self.page == Page::DataOperations;
        if data_operations_open
            && !self.data_operations_page_was_open
            && self.settings.data_mode == DataMode::Api
            && self.settings.auto_refresh_data_operations
            && self.refresh_receiver.is_none()
        {
            self.refresh_data();
        }
        self.data_operations_page_was_open = data_operations_open;
        self.schedule_preference_write(ui.ctx());
        configure_context(
            &self.egui_ctx,
            self.settings.theme,
            self.settings.density,
            self.settings.zoom_factor,
        );
    }

    fn save(&mut self, storage: &mut dyn eframe::Storage) {
        let preferences = AppPreferences::from_state(
            &self.selected_workspace_id,
            &self.layout,
            &self.table_layouts,
            &self.settings,
            self.page,
        );
        storage.set_string(PREFERENCES_KEY, preferences.encode());
        self.preference_write.set_committed(preferences.clone());
        self.persist_preferences_to_disk(&preferences);
    }
}

fn load_snapshot(
    provider: &impl DataProvider,
    workspace_id: &str,
    selected: SelectedInstrument,
    last_refresh_at: String,
) -> DashboardSnapshot {
    let workspaces = provider.load_workspaces();
    let workspace = select_workspace(&workspaces, workspace_id);
    let portfolio_summary = provider.load_portfolio_summary(&workspace.id);
    let positions = provider.load_positions(&workspace.id);
    let funds = provider.load_funds();
    let distributions = provider.load_distributions();
    let holdings = provider.load_holdings();
    let exposures = provider.load_exposure(&workspace.id);
    let alerts = provider.load_alerts(&workspace.id);
    let documents = provider.load_documents();
    let (mut scheduled_jobs, mut job_runs) = provider.load_jobs();
    let mut data_operations_load = provider.load_data_operations(&workspace.id);
    if let Some(api_scheduled_jobs) = data_operations_load.scheduled_jobs.take() {
        scheduled_jobs = api_scheduled_jobs;
    }
    if let Some(api_job_runs) = data_operations_load.job_runs.take() {
        job_runs = api_job_runs;
    }
    let mut data_operations = data_operations_load.operations;
    let portfolio_tree = build_investable_tree(
        &workspace,
        &portfolio_summary,
        &positions,
        &funds,
        &holdings,
    );
    let time_series = provider.load_time_series(&workspace.id);
    let data_mode = provider.data_mode();
    if data_mode == DataMode::Mock {
        data_operations.hydration.origin = crate::domain::DataOperationsOrigin::Mock;
        data_operations.hydration.refreshed_at = Some(format!("{last_refresh_at} mock"));
        data_operations.hydration.base_url = None;
        data_operations.hydration.failed_sections.clear();
        data_operations.hydration.last_error = None;
    }
    let data_status = match data_operations.hydration.origin {
        crate::domain::DataOperationsOrigin::Api => RefreshStatus::Fresh,
        crate::domain::DataOperationsOrigin::PartialApi
        | crate::domain::DataOperationsOrigin::StaleApi => RefreshStatus::Stale,
        crate::domain::DataOperationsOrigin::ApiError => RefreshStatus::Error,
        crate::domain::DataOperationsOrigin::Mock => {
            if portfolio_summary.stale_warning_count > 0 {
                RefreshStatus::Stale
            } else {
                RefreshStatus::Fresh
            }
        }
    };

    DashboardSnapshot {
        workspace,
        workspaces,
        portfolio_summary,
        positions,
        funds,
        distributions,
        holdings,
        exposures,
        alerts,
        documents,
        scheduled_jobs,
        job_runs,
        data_operations,
        portfolio_tree,
        time_series,
        selected,
        last_refresh_at,
        data_mode,
        data_status,
    }
}

fn api_runtime_from_snapshot(snapshot: &DashboardSnapshot) -> ApiRuntimeStatus {
    let hydration = &snapshot.data_operations.hydration;
    ApiRuntimeStatus {
        connection: api_connection_status_for_origin(hydration.origin),
        last_checked_at: hydration.refreshed_at.clone(),
        last_error: hydration.last_error.clone(),
    }
}

fn api_connection_status_for_origin(
    origin: crate::domain::DataOperationsOrigin,
) -> ApiConnectionStatus {
    match origin {
        crate::domain::DataOperationsOrigin::Mock => ApiConnectionStatus::NotUsed,
        crate::domain::DataOperationsOrigin::Api => ApiConnectionStatus::Connected,
        crate::domain::DataOperationsOrigin::PartialApi => ApiConnectionStatus::Partial,
        crate::domain::DataOperationsOrigin::StaleApi
        | crate::domain::DataOperationsOrigin::ApiError => ApiConnectionStatus::Disconnected,
    }
}

fn is_current_refresh_response(current_generation: u64, response_generation: u64) -> bool {
    current_generation == response_generation
}

fn select_workspace(workspaces: &[Workspace], workspace_id: &str) -> Workspace {
    workspaces
        .iter()
        .find(|workspace| workspace.id == workspace_id)
        .cloned()
        .or_else(|| workspaces.first().cloned())
        .unwrap_or_else(|| Workspace {
            id: "workspace-missing".to_owned(),
            name: "No workspace".to_owned(),
            base_currency: "GBP".to_owned(),
        })
}

fn configure_context(
    ctx: &egui::Context,
    theme: settings::ThemePreference,
    density: settings::DensityPreference,
    zoom_factor: f32,
) {
    ctx.set_zoom_factor(settings::clamp_zoom_factor(zoom_factor));
    ctx.set_theme(match theme {
        settings::ThemePreference::System => egui::ThemePreference::System,
        settings::ThemePreference::Light => egui::ThemePreference::Light,
        settings::ThemePreference::Dark => egui::ThemePreference::Dark,
    });
    ctx.all_styles_mut(|style| {
        match density {
            settings::DensityPreference::Compact => {
                style.spacing.item_spacing = crate::ui::style::compact_item_spacing();
                style.spacing.button_padding = crate::ui::style::compact_button_padding();
                style.spacing.interact_size = crate::ui::style::compact_interact_size();
            }
            settings::DensityPreference::Comfortable => {
                style.spacing.item_spacing = crate::ui::style::comfortable_item_spacing();
                style.spacing.button_padding = crate::ui::style::comfortable_button_padding();
                style.spacing.interact_size = crate::ui::style::comfortable_interact_size();
            }
        }
        style.interaction.tooltip_delay = crate::ui::style::TOOLTIP_DELAY_SECONDS;
        style.visuals.striped = true;
    });
}

#[cfg(test)]
mod tests {
    use super::{
        AppPreferences, CopyPayload, TOP_MENU_LABELS, api_connection_status_for_origin,
        choose_copy_payload, is_current_refresh_response,
    };
    use crate::api::types::ApiConnectionStatus;
    use crate::domain::DataOperationsOrigin;

    #[test]
    fn top_menu_labels_follow_workstation_model() {
        assert_eq!(
            TOP_MENU_LABELS,
            [
                "File", "Edit", "View", "Navigate", "Data", "Tools", "Window", "Help",
            ]
        );
    }

    #[test]
    fn stale_refresh_generations_are_ignored() {
        assert!(is_current_refresh_response(4, 4));
        assert!(!is_current_refresh_response(4, 3));
    }

    #[test]
    fn maps_data_operations_origin_to_api_connection_status() {
        assert_eq!(
            api_connection_status_for_origin(DataOperationsOrigin::Api),
            ApiConnectionStatus::Connected
        );
        assert_eq!(
            api_connection_status_for_origin(DataOperationsOrigin::PartialApi),
            ApiConnectionStatus::Partial
        );
        assert_eq!(
            api_connection_status_for_origin(DataOperationsOrigin::ApiError),
            ApiConnectionStatus::Disconnected
        );
    }

    #[test]
    fn copy_priority_prefers_cell_then_focused_row_then_selection_then_summary() {
        let payload = choose_copy_payload(
            Some(CopyPayload::new("cell", "cell-value")),
            Some(CopyPayload::new("focused row", "focused-row-value")),
            Some(CopyPayload::new("selected row", "selected-row-value")),
            Some(CopyPayload::new("summary", "summary-value")),
        )
        .expect("copy payload");

        assert_eq!(payload.label, "cell");
        assert_eq!(payload.text, "cell-value");

        let payload = choose_copy_payload(
            None,
            Some(CopyPayload::new("focused row", "focused-row-value")),
            Some(CopyPayload::new("selected row", "selected-row-value")),
            Some(CopyPayload::new("summary", "summary-value")),
        )
        .expect("copy payload");
        assert_eq!(payload.label, "focused row");
    }

    #[test]
    fn preferences_round_trip_layout_width_and_revision() {
        let decoded =
            AppPreferences::decode("show_inspector=0\ninspector_width=356.5\nlayout_revision=4");

        assert_eq!(decoded.show_inspector, Some(false));
        assert_eq!(decoded.inspector_width, Some(356.5));
        assert_eq!(decoded.layout_revision, Some(4));
        assert!(decoded.encode().contains("inspector_width=356.5"));
    }
}
