use crate::app_model::SelectedInstrument;
pub use crate::compute::charts::ChartValueMode;
use crate::compute::charts::{
    DerivedSpreadSeries, derive_matching_date_spread, display_points_for_mode,
    first_valid_common_base_date,
};
use crate::domain::AnalysisSubject;
use crate::source::SourceSelection;
use crate::table_state::{TableId, TableState};
use crate::timeseries::{TimeSeries, TimeSeriesKind, TimeSeriesPoint, find_series};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ChartMode {
    #[default]
    Single,
    Overlay,
    Compare,
    Spread,
}

impl ChartMode {
    pub const ALL: [Self; 4] = [Self::Single, Self::Overlay, Self::Compare, Self::Spread];

    pub fn label(self) -> &'static str {
        match self {
            Self::Single => "Single",
            Self::Overlay => "Overlay",
            Self::Compare => "Compare",
            Self::Spread => "Spread",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChartSeriesRole {
    Primary,
    KindOverlay,
    SubjectOverlay,
    Comparison,
    Spread,
}

impl ChartSeriesRole {
    pub fn label(self) -> &'static str {
        match self {
            Self::Primary => "PRIMARY",
            Self::KindOverlay => "OVERLAY",
            Self::SubjectOverlay => "OVERLAY",
            Self::Comparison => "COMPARE",
            Self::Spread => "SPREAD",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ChartDataColumn {
    #[default]
    Date,
    Series,
    Subject,
    Value,
    RawValue,
    Unit,
    Source,
    Status,
    Kind,
}

impl ChartDataColumn {
    pub const ALL: [Self; 9] = [
        Self::Date,
        Self::Series,
        Self::Subject,
        Self::Value,
        Self::RawValue,
        Self::Unit,
        Self::Source,
        Self::Status,
        Self::Kind,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Date => "date",
            Self::Series => "series",
            Self::Subject => "subject",
            Self::Value => "value",
            Self::RawValue => "raw_value",
            Self::Unit => "unit",
            Self::Source => "source",
            Self::Status => "status",
            Self::Kind => "kind",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Date => "Date",
            Self::Series => "Series",
            Self::Subject => "Subject",
            Self::Value => "Value",
            Self::RawValue => "Raw",
            Self::Unit => "Unit",
            Self::Source => "Source",
            Self::Status => "Status",
            Self::Kind => "Kind",
        }
    }

    pub fn next(self) -> Self {
        let current = Self::ALL
            .iter()
            .position(|column| *column == self)
            .unwrap_or(0);
        Self::ALL[(current + 1).min(Self::ALL.len() - 1)]
    }

    pub fn previous(self) -> Self {
        let current = Self::ALL
            .iter()
            .position(|column| *column == self)
            .unwrap_or(0);
        Self::ALL[current.saturating_sub(1)]
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ChartTableFocus {
    pub row_index: Option<usize>,
    pub column: Option<ChartDataColumn>,
}

impl ChartTableFocus {
    pub fn set(&mut self, row_index: usize, column: ChartDataColumn) {
        self.row_index = Some(row_index);
        self.column = Some(column);
    }

    pub fn clear(&mut self) {
        self.row_index = None;
        self.column = None;
    }

    pub fn column_or_default(&self) -> ChartDataColumn {
        self.column.unwrap_or_default()
    }

    pub fn move_column(&mut self, offset: isize) -> Option<ChartDataColumn> {
        let current = self.column_or_default();
        let next = match offset.cmp(&0) {
            std::cmp::Ordering::Less => current.previous(),
            std::cmp::Ordering::Greater => current.next(),
            std::cmp::Ordering::Equal => current,
        };
        self.column = Some(next);
        Some(next)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct ChartSeriesId(pub String);

impl ChartSeriesId {
    pub fn new(
        role: ChartSeriesRole,
        subject: &AnalysisSubject,
        kind: TimeSeriesKind,
        label: &str,
    ) -> Self {
        Self(format!(
            "{}:{}:{}:{}",
            role.label(),
            subject_key(subject),
            kind.as_str(),
            label
        ))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ChartSeriesSpec {
    pub id: ChartSeriesId,
    pub subject: AnalysisSubject,
    pub kind: TimeSeriesKind,
    pub label: String,
    pub role: ChartSeriesRole,
    pub unit: String,
    pub source: String,
    pub status: String,
    pub points: Vec<TimeSeriesPoint>,
}

impl ChartSeriesSpec {
    pub(crate) fn from_time_series(
        series: &TimeSeries,
        role: ChartSeriesRole,
        label: String,
    ) -> Self {
        Self {
            id: ChartSeriesId::new(role, &series.subject, series.kind, &label),
            subject: series.subject.clone(),
            kind: series.kind,
            label,
            role,
            unit: series.unit.clone(),
            source: series.source.clone(),
            status: series.status.clone(),
            points: series.points.clone(),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SpreadSeriesSummary {
    pub left_label: String,
    pub right_label: String,
    pub left_source: String,
    pub right_source: String,
    pub common_points: usize,
    pub left_points: usize,
    pub right_points: usize,
    pub partial: bool,
    pub status: String,
}

impl SpreadSeriesSummary {
    pub fn coverage_label(&self) -> String {
        format!(
            "{} common / {} left / {} right",
            self.common_points, self.left_points, self.right_points
        )
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ChartSeriesSet {
    pub series: Vec<ChartSeriesSpec>,
    pub spread: Option<SpreadSeriesSummary>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ChartPointDistanceScale {
    pub x: f64,
    pub y: f64,
}

impl ChartPointDistanceScale {
    #[cfg(test)]
    pub const PLOT_SPACE: Self = Self { x: 1.0, y: 1.0 };

    pub fn new(x: f64, y: f64) -> Self {
        Self {
            x: finite_positive_or_one(x),
            y: finite_positive_or_one(y),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ChartNearestPoint {
    pub series_id: ChartSeriesId,
    pub point_index: usize,
    pub date: String,
    pub cursor_date: Option<String>,
    pub x: f64,
    pub y: f64,
    pub distance_screen_or_plot: f64,
    pub subject: AnalysisSubject,
    pub series_label: String,
    pub series_kind: TimeSeriesKind,
    pub value: f64,
    pub raw_value: f64,
    pub unit: String,
    pub raw_unit: String,
    pub source: String,
    pub status: String,
    pub value_mode: ChartValueMode,
}

impl ChartNearestPoint {
    pub fn point_selection(&self) -> ChartPointSelection {
        ChartPointSelection {
            series_id: self.series_id.clone(),
            subject: self.subject.clone(),
            series_label: self.series_label.clone(),
            series_kind: self.series_kind,
            date: self.date.clone(),
            value: self.value,
            raw_value: self.raw_value,
            unit: self.unit.clone(),
            raw_unit: self.raw_unit.clone(),
            source: self.source.clone(),
            status: self.status.clone(),
            value_mode: self.value_mode,
        }
    }

    pub fn copy_value_text(&self) -> String {
        self.point_selection().copy_value_text()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ChartPointSelection {
    pub series_id: ChartSeriesId,
    pub subject: AnalysisSubject,
    pub series_label: String,
    pub series_kind: TimeSeriesKind,
    pub date: String,
    pub value: f64,
    pub raw_value: f64,
    pub unit: String,
    pub raw_unit: String,
    pub source: String,
    pub status: String,
    pub value_mode: ChartValueMode,
}

impl ChartPointSelection {
    pub fn label(&self) -> String {
        format!(
            "{} {} {}",
            self.series_label,
            self.date,
            self.value_mode.compact_label()
        )
    }

    pub fn copy_text(&self) -> String {
        [
            self.date.clone(),
            self.series_label.clone(),
            self.value.to_string(),
            self.unit.clone(),
            self.raw_value.to_string(),
            self.raw_unit.clone(),
            self.source.clone(),
            self.status.clone(),
            self.series_kind.as_str().to_owned(),
            self.value_mode.label().to_owned(),
            "x_mode=date".to_owned(),
        ]
        .join("\t")
    }

    pub fn copy_value_text(&self) -> String {
        if self.value_mode == ChartValueMode::Raw {
            self.value.to_string()
        } else {
            format!("{}\t{}\t{}", self.value, self.unit, self.raw_value)
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PlotRange {
    OneMonth,
    ThreeMonths,
    SixMonths,
    OneYear,
    All,
}

impl PlotRange {
    pub const ALL: [Self; 5] = [
        Self::OneMonth,
        Self::ThreeMonths,
        Self::SixMonths,
        Self::OneYear,
        Self::All,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::OneMonth => "1M",
            Self::ThreeMonths => "3M",
            Self::SixMonths => "6M",
            Self::OneYear => "1Y",
            Self::All => "All",
        }
    }

    pub fn lookback_days(self) -> Option<i64> {
        match self {
            Self::OneMonth => Some(31),
            Self::ThreeMonths => Some(92),
            Self::SixMonths => Some(183),
            Self::OneYear => Some(366),
            Self::All => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct ChartDate {
    year: i32,
    month: u32,
    day: u32,
}

impl ChartDate {
    fn parse_iso(value: &str) -> Option<Self> {
        let bytes = value.as_bytes();
        if bytes.len() != 10 || bytes[4] != b'-' || bytes[7] != b'-' {
            return None;
        }
        if !bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 4 | 7) || byte.is_ascii_digit())
        {
            return None;
        }

        let year = value[0..4].parse::<i32>().ok()?;
        let month = value[5..7].parse::<u32>().ok()?;
        let day = value[8..10].parse::<u32>().ok()?;
        Self::new(year, month, day)
    }

    fn new(year: i32, month: u32, day: u32) -> Option<Self> {
        if year == 0 || !(1..=12).contains(&month) {
            return None;
        }
        let max_day = match month {
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            4 | 6 | 9 | 11 => 30,
            2 if is_leap_year(year) => 29,
            2 => 28,
            _ => return None,
        };
        (1..=max_day)
            .contains(&day)
            .then_some(Self { year, month, day })
    }

    fn epoch_day(self) -> i64 {
        days_from_civil(self.year, self.month, self.day)
    }

    fn from_epoch_day(day: i64) -> Self {
        civil_from_days(day)
    }

    fn iso_label(self) -> String {
        format!("{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }

    fn day_month_label(self) -> String {
        format!("{:02} {}", self.day, month_label(self.month))
    }

    fn month_year_label(self) -> String {
        format!("{} {}", month_label(self.month), self.year)
    }

    fn year_label(self) -> String {
        self.year.to_string()
    }
}

pub struct ChartDateAxis;

impl ChartDateAxis {
    pub fn date_to_x(date: &str) -> Option<f64> {
        ChartDate::parse_iso(date).map(|date| date.epoch_day() as f64)
    }

    pub fn x_to_date(x: f64) -> Option<String> {
        if !x.is_finite() {
            return None;
        }
        let rounded = x.round();
        if rounded < i64::MIN as f64 || rounded > i64::MAX as f64 {
            return None;
        }
        Some(ChartDate::from_epoch_day(rounded as i64).iso_label())
    }

    pub fn format_tick(x: f64, range: PlotRange) -> String {
        let Some(date) = Self::date_from_x(x) else {
            return String::new();
        };
        match range {
            PlotRange::OneMonth | PlotRange::ThreeMonths => date.day_month_label(),
            PlotRange::SixMonths | PlotRange::OneYear => date.month_year_label(),
            PlotRange::All => date.year_label(),
        }
    }

    pub fn format_tick_for_span(x: f64, span_days: f64) -> String {
        let Some(date) = Self::date_from_x(x) else {
            return String::new();
        };
        if !span_days.is_finite() || span_days <= 120.0 {
            date.day_month_label()
        } else if span_days <= 730.0 {
            date.month_year_label()
        } else {
            date.year_label()
        }
    }

    fn date_from_x(x: f64) -> Option<ChartDate> {
        if !x.is_finite() {
            return None;
        }
        let rounded = x.round();
        if rounded < i64::MIN as f64 || rounded > i64::MAX as f64 {
            return None;
        }
        Some(ChartDate::from_epoch_day(rounded as i64))
    }
}

fn is_leap_year(year: i32) -> bool {
    year.rem_euclid(4) == 0 && year.rem_euclid(100) != 0 || year.rem_euclid(400) == 0
}

fn month_label(month: u32) -> &'static str {
    match month {
        1 => "Jan",
        2 => "Feb",
        3 => "Mar",
        4 => "Apr",
        5 => "May",
        6 => "Jun",
        7 => "Jul",
        8 => "Aug",
        9 => "Sep",
        10 => "Oct",
        11 => "Nov",
        12 => "Dec",
        _ => "-",
    }
}

fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let year = i64::from(year) - i64::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month = i64::from(month);
    let month_prime = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * month_prime + 2) / 5 + i64::from(day) - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

fn civil_from_days(days: i64) -> ChartDate {
    let days = days + 719_468;
    let era = if days >= 0 { days } else { days - 146_096 } / 146_097;
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    let year = year + i64::from(month <= 2);

    ChartDate {
        year: year as i32,
        month: month as u32,
        day: day as u32,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChartSeriesPreset {
    Price,
    Nav,
    NavVsPrice,
    MarketValue,
    PortfolioValue,
    Dividends,
    ProjectedIncome,
}

impl ChartSeriesPreset {
    pub const ALL: [Self; 7] = [
        Self::Price,
        Self::Nav,
        Self::NavVsPrice,
        Self::MarketValue,
        Self::PortfolioValue,
        Self::Dividends,
        Self::ProjectedIncome,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Price => "Price",
            Self::Nav => "NAV",
            Self::NavVsPrice => "NAV vs Price",
            Self::MarketValue => "Market value",
            Self::PortfolioValue => "Portfolio value",
            Self::Dividends => "Dividends",
            Self::ProjectedIncome => "Projected income",
        }
    }

    pub fn primary_kind(self) -> TimeSeriesKind {
        match self {
            Self::Price => TimeSeriesKind::Price,
            Self::Nav | Self::NavVsPrice => TimeSeriesKind::Nav,
            Self::MarketValue => TimeSeriesKind::MarketValue,
            Self::PortfolioValue => TimeSeriesKind::PortfolioValue,
            Self::Dividends => TimeSeriesKind::Distribution,
            Self::ProjectedIncome => TimeSeriesKind::ProjectedIncome,
        }
    }

    pub fn overlay_kinds(self) -> Vec<TimeSeriesKind> {
        match self {
            Self::NavVsPrice => vec![TimeSeriesKind::Price],
            Self::Price
            | Self::Nav
            | Self::MarketValue
            | Self::PortfolioValue
            | Self::Dividends
            | Self::ProjectedIncome => Vec::new(),
        }
    }

    pub fn from_request(request: &PlotRequest) -> Self {
        match (request.series_kind, request.overlay_series_kinds.as_slice()) {
            (TimeSeriesKind::Nav, [TimeSeriesKind::Price, ..]) => Self::NavVsPrice,
            (TimeSeriesKind::Price, _) => Self::Price,
            (TimeSeriesKind::Nav, _) => Self::Nav,
            (TimeSeriesKind::MarketValue, _) => Self::MarketValue,
            (TimeSeriesKind::PortfolioValue, _) => Self::PortfolioValue,
            (TimeSeriesKind::Distribution, _) => Self::Dividends,
            (TimeSeriesKind::ProjectedIncome, _) => Self::ProjectedIncome,
            _ => Self::Price,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlotOptions {
    pub range: PlotRange,
    pub show_table: bool,
}

impl Default for PlotOptions {
    fn default() -> Self {
        Self {
            range: PlotRange::OneYear,
            show_table: true,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlotRequest {
    pub subject: AnalysisSubject,
    pub series_kind: TimeSeriesKind,
    pub label: String,
    pub comparison_subjects: Vec<AnalysisSubject>,
    pub overlay_series_kinds: Vec<TimeSeriesKind>,
    pub mode: ChartMode,
    pub options: PlotOptions,
}

impl PlotRequest {
    pub fn new(
        subject: AnalysisSubject,
        series_kind: TimeSeriesKind,
        label: impl Into<String>,
    ) -> Self {
        Self {
            subject,
            series_kind,
            label: label.into(),
            comparison_subjects: Vec::new(),
            overlay_series_kinds: Vec::new(),
            mode: ChartMode::Single,
            options: PlotOptions::default(),
        }
    }

    pub fn with_mode(mut self, mode: ChartMode) -> Self {
        self.mode = mode;
        self
    }

    pub fn with_overlay(mut self, kind: TimeSeriesKind) -> Self {
        if !self.overlay_series_kinds.contains(&kind) {
            self.overlay_series_kinds.push(kind);
        }
        if self.mode == ChartMode::Single {
            self.mode = ChartMode::Overlay;
        }
        self
    }

    pub fn with_comparison(mut self, subject: AnalysisSubject) -> Self {
        if !self.comparison_subjects.contains(&subject) {
            self.comparison_subjects.push(subject);
        }
        if self.mode == ChartMode::Single {
            self.mode = ChartMode::Compare;
        }
        self
    }

    pub fn spread(
        left_subject: AnalysisSubject,
        right_subject: AnalysisSubject,
        series_kind: TimeSeriesKind,
        label: impl Into<String>,
    ) -> Self {
        Self::new(left_subject, series_kind, label)
            .with_comparison(right_subject)
            .with_mode(ChartMode::Spread)
    }
}

#[derive(Clone, Debug)]
pub struct ChartPanelState {
    pub mode: ChartMode,
    pub active_plot: Option<PlotRequest>,
    pub selected_range: PlotRange,
    pub show_table: bool,
    pub recent_plots: Vec<PlotRequest>,
    pub overlay_subject_key: String,
    pub comparison_subject_key: String,
    pub source_selection: SourceSelection,
    pub value_mode: ChartValueMode,
    pub data_table: TableState,
    pub table_focus: ChartTableFocus,
    pub selected_series_id: Option<ChartSeriesId>,
    pub selected_point: Option<ChartPointSelection>,
}

impl Default for ChartPanelState {
    fn default() -> Self {
        Self {
            mode: ChartMode::Single,
            active_plot: None,
            selected_range: PlotRange::OneYear,
            show_table: true,
            recent_plots: Vec::new(),
            overlay_subject_key: String::new(),
            comparison_subject_key: String::new(),
            source_selection: SourceSelection::Canonical,
            value_mode: ChartValueMode::Raw,
            data_table: TableState::new(TableId::ChartSeriesData),
            table_focus: ChartTableFocus::default(),
            selected_series_id: None,
            selected_point: None,
        }
    }
}

impl ChartPanelState {
    pub fn set_plot(&mut self, mut request: PlotRequest) {
        request.options.range = self.selected_range;
        request.options.show_table = self.show_table;
        self.mode = inferred_mode(&request);
        request.mode = self.mode;
        self.clear_chart_selection();
        self.remember_plot(&request);
        self.active_plot = Some(request);
    }

    pub fn sync_options(&mut self) {
        if let Some(request) = &mut self.active_plot {
            request.options.range = self.selected_range;
            request.options.show_table = self.show_table;
            request.mode = self.mode;
        }
    }

    pub fn set_mode(&mut self, mode: ChartMode) {
        self.mode = mode;
        if let Some(request) = &mut self.active_plot {
            request.mode = mode;
            if mode == ChartMode::Single {
                request.overlay_series_kinds.clear();
                request.comparison_subjects.clear();
            }
        }
        self.clear_chart_selection();
    }

    pub fn select_recent(&mut self, index: usize) -> bool {
        let Some(request) = self.recent_plots.get(index).cloned() else {
            return false;
        };
        self.set_plot(request);
        true
    }

    pub fn add_comparison_subject(&mut self, subject: AnalysisSubject) -> bool {
        let Some(request) = &mut self.active_plot else {
            return false;
        };
        if request.subject == subject || request.comparison_subjects.contains(&subject) {
            return false;
        }
        self.mode = ChartMode::Overlay;
        request.mode = ChartMode::Overlay;
        request.comparison_subjects.push(subject);
        let request = request.clone();
        self.remember_plot(&request);
        true
    }

    pub fn set_comparison_subject(&mut self, subject: AnalysisSubject) -> bool {
        let request = {
            let Some(request) = &mut self.active_plot else {
                return false;
            };
            if request.subject == subject {
                return false;
            }
            self.mode = ChartMode::Compare;
            request.mode = ChartMode::Compare;
            request.comparison_subjects.clear();
            request.comparison_subjects.push(subject);
            request.clone()
        };
        self.clear_chart_selection();
        self.remember_plot(&request);
        true
    }

    pub fn set_spread_subject(&mut self, subject: AnalysisSubject) -> bool {
        let request = {
            let Some(request) = &mut self.active_plot else {
                return false;
            };
            if request.subject == subject {
                return false;
            }
            self.mode = ChartMode::Spread;
            request.mode = ChartMode::Spread;
            request.comparison_subjects.clear();
            request.comparison_subjects.push(subject);
            request.overlay_series_kinds.clear();
            request.clone()
        };
        self.clear_chart_selection();
        self.remember_plot(&request);
        true
    }

    pub fn remove_comparison_subject(&mut self, subject: &AnalysisSubject) -> bool {
        let request = {
            let Some(request) = &mut self.active_plot else {
                return false;
            };
            let before = request.comparison_subjects.len();
            request
                .comparison_subjects
                .retain(|comparison| comparison != subject);
            if request.comparison_subjects.len() == before {
                return false;
            }
            if request.comparison_subjects.is_empty() && request.overlay_series_kinds.is_empty() {
                self.mode = ChartMode::Single;
                request.mode = ChartMode::Single;
            }
            request.clone()
        };
        self.clear_chart_selection();
        self.remember_plot(&request);
        true
    }

    pub fn swap_operands(&mut self) -> bool {
        let request = {
            let Some(request) = &mut self.active_plot else {
                return false;
            };
            let Some(first_comparison) = request.comparison_subjects.first_mut() else {
                return false;
            };
            std::mem::swap(&mut request.subject, first_comparison);
            request.label = swap_label(&request.label);
            request.clone()
        };
        self.clear_chart_selection();
        self.remember_plot(&request);
        true
    }

    pub fn clear_overlays(&mut self) -> bool {
        let Some(request) = &mut self.active_plot else {
            return false;
        };
        if request.overlay_series_kinds.is_empty() && request.comparison_subjects.is_empty() {
            return false;
        }
        request.overlay_series_kinds.clear();
        request.comparison_subjects.clear();
        request.mode = ChartMode::Single;
        self.mode = ChartMode::Single;
        let request = request.clone();
        self.clear_chart_selection();
        self.remember_plot(&request);
        true
    }

    pub fn select_series(&mut self, series_id: ChartSeriesId) {
        self.selected_series_id = Some(series_id);
        self.selected_point = None;
        self.data_table.clear_selection();
        self.table_focus.clear();
    }

    pub fn select_point(&mut self, point: ChartPointSelection) {
        self.selected_series_id = Some(point.series_id.clone());
        self.selected_point = Some(point);
    }

    pub fn select_relative_series(&mut self, series_set: &ChartSeriesSet, offset: isize) -> bool {
        let Some(next_series_id) =
            relative_series_id(series_set, self.selected_series_id.as_ref(), offset)
        else {
            return false;
        };

        if self.selected_series_id.as_ref() != Some(&next_series_id) {
            self.selected_series_id = Some(next_series_id);
            self.selected_point = None;
            self.data_table.clear_selection();
            self.table_focus.clear();
        }
        true
    }

    pub fn set_table_focus(&mut self, row_index: usize, column: ChartDataColumn) {
        self.table_focus.set(row_index, column);
    }

    pub fn clear_chart_selection(&mut self) {
        self.selected_series_id = None;
        self.selected_point = None;
        self.data_table.clear_selection();
        self.table_focus.clear();
    }

    fn remember_plot(&mut self, request: &PlotRequest) {
        self.recent_plots
            .retain(|recent| !same_plot_identity(recent, request));
        self.recent_plots.insert(0, request.clone());
        self.recent_plots.truncate(8);
    }
}

pub fn find_nearest_chart_point(
    series_set: &ChartSeriesSet,
    range: PlotRange,
    value_mode: ChartValueMode,
    cursor_x: f64,
    cursor_y: f64,
    max_distance: f64,
    distance_scale: ChartPointDistanceScale,
) -> Option<ChartNearestPoint> {
    if !cursor_x.is_finite() || !cursor_y.is_finite() || max_distance < 0.0 {
        return None;
    }

    let base_date = display_base_date_for_set(series_set, range, value_mode);
    let cursor_date = ChartDateAxis::x_to_date(cursor_x);
    let mut nearest: Option<ChartNearestPoint> = None;

    for series in &series_set.series {
        let display_unit = value_mode.display_unit(&series.unit);
        for (point_index, point) in
            display_points_for_series(series, range, value_mode, base_date.as_deref())
                .into_iter()
                .enumerate()
        {
            if !point.value.is_finite() || !point.raw_value.is_finite() {
                continue;
            }
            let Some(x) = ChartDateAxis::date_to_x(&point.date) else {
                continue;
            };
            let y = point.value;
            let distance = scaled_distance(cursor_x, cursor_y, x, y, distance_scale);
            if distance > max_distance {
                continue;
            }
            let candidate = ChartNearestPoint {
                series_id: series.id.clone(),
                point_index,
                date: point.date,
                cursor_date: cursor_date.clone(),
                x,
                y,
                distance_screen_or_plot: distance,
                subject: series.subject.clone(),
                series_label: series.label.clone(),
                series_kind: series.kind,
                value: point.value,
                raw_value: point.raw_value,
                unit: display_unit.clone(),
                raw_unit: series.unit.clone(),
                source: point.source,
                status: point.status,
                value_mode,
            };
            if nearest
                .as_ref()
                .is_none_or(|current| candidate_is_nearer(&candidate, current))
            {
                nearest = Some(candidate);
            }
        }
    }

    nearest
}

pub fn selected_chart_point_marker(
    series_set: &ChartSeriesSet,
    range: PlotRange,
    value_mode: ChartValueMode,
    selected: &ChartPointSelection,
) -> Option<ChartNearestPoint> {
    let base_date = display_base_date_for_set(series_set, range, value_mode);
    series_set
        .series
        .iter()
        .find(|series| series.id == selected.series_id)
        .and_then(|series| {
            let display_unit = value_mode.display_unit(&series.unit);
            display_points_for_series(series, range, value_mode, base_date.as_deref())
                .into_iter()
                .enumerate()
                .find(|(_, point)| point.date == selected.date)
                .and_then(|(point_index, point)| {
                    let x = ChartDateAxis::date_to_x(&point.date)?;
                    Some(ChartNearestPoint {
                        series_id: series.id.clone(),
                        point_index,
                        cursor_date: None,
                        x,
                        date: point.date,
                        y: point.value,
                        distance_screen_or_plot: 0.0,
                        subject: series.subject.clone(),
                        series_label: series.label.clone(),
                        series_kind: series.kind,
                        value: point.value,
                        raw_value: point.raw_value,
                        unit: display_unit,
                        raw_unit: series.unit.clone(),
                        source: point.source,
                        status: point.status,
                        value_mode,
                    })
                })
        })
}

fn relative_series_id(
    series_set: &ChartSeriesSet,
    selected_series_id: Option<&ChartSeriesId>,
    offset: isize,
) -> Option<ChartSeriesId> {
    if series_set.series.is_empty() {
        return None;
    }

    let len = series_set.series.len();
    let Some(current_index) = selected_series_id.and_then(|selected| {
        series_set
            .series
            .iter()
            .position(|series| series.id == *selected)
    }) else {
        let initial_index = if offset < 0 { len - 1 } else { 0 };
        return series_set
            .series
            .get(initial_index)
            .map(|series| series.id.clone());
    };
    let step = offset.unsigned_abs() % len;
    let next_index = match offset.cmp(&0) {
        std::cmp::Ordering::Less => (current_index + len - step) % len,
        std::cmp::Ordering::Equal => current_index,
        std::cmp::Ordering::Greater => (current_index + step) % len,
    };
    series_set
        .series
        .get(next_index)
        .map(|series| series.id.clone())
}

fn display_base_date_for_set(
    series_set: &ChartSeriesSet,
    range: PlotRange,
    value_mode: ChartValueMode,
) -> Option<String> {
    if value_mode == ChartValueMode::Raw {
        return None;
    }

    let visible = series_set
        .series
        .iter()
        .map(|series| visible_points_for_range(&series.points, range))
        .collect::<Vec<_>>();
    let visible_refs = visible.iter().map(Vec::as_slice).collect::<Vec<_>>();
    first_valid_common_base_date(&visible_refs)
}

fn display_points_for_series(
    series: &ChartSeriesSpec,
    range: PlotRange,
    value_mode: ChartValueMode,
    base_date: Option<&str>,
) -> Vec<crate::compute::charts::DisplayPoint> {
    let visible = visible_points_for_range(&series.points, range);
    display_points_for_mode(&visible, value_mode, base_date)
}

pub fn visible_points_for_range(
    points: &[TimeSeriesPoint],
    range: PlotRange,
) -> Vec<TimeSeriesPoint> {
    let Some(start_x) = date_range_start_x(points, range) else {
        return points.to_vec();
    };
    let mut visible = points
        .iter()
        .filter(|point| ChartDateAxis::date_to_x(&point.date).is_some_and(|x| x >= start_x))
        .cloned()
        .collect::<Vec<_>>();
    visible.sort_by(|left, right| left.date.cmp(&right.date));
    visible
}

fn date_range_start_x(points: &[TimeSeriesPoint], range: PlotRange) -> Option<f64> {
    let lookback_days = range.lookback_days()? as f64;
    let latest = points
        .iter()
        .filter_map(|point| ChartDateAxis::date_to_x(&point.date))
        .max_by(f64::total_cmp)?;
    Some(latest - lookback_days)
}

fn scaled_distance(
    cursor_x: f64,
    cursor_y: f64,
    point_x: f64,
    point_y: f64,
    scale: ChartPointDistanceScale,
) -> f64 {
    let dx = (cursor_x - point_x) * scale.x;
    let dy = (cursor_y - point_y) * scale.y;
    dx.hypot(dy)
}

fn candidate_is_nearer(candidate: &ChartNearestPoint, current: &ChartNearestPoint) -> bool {
    candidate
        .distance_screen_or_plot
        .total_cmp(&current.distance_screen_or_plot)
        .then_with(|| candidate.series_id.0.cmp(&current.series_id.0))
        .then_with(|| candidate.point_index.cmp(&current.point_index))
        == std::cmp::Ordering::Less
}

fn finite_positive_or_one(value: f64) -> f64 {
    if value.is_finite() && value.abs() > f64::EPSILON {
        value.abs()
    } else {
        1.0
    }
}

pub fn chart_series_for_request(
    request: &PlotRequest,
    all_series: &[TimeSeries],
) -> ChartSeriesSet {
    let mut result = ChartSeriesSet::default();

    let Some(primary) = find_series(all_series, &request.subject, request.series_kind) else {
        return result;
    };

    if request.mode == ChartMode::Spread {
        if let Some(right_subject) = request.comparison_subjects.first()
            && let Some(right) = find_series(all_series, right_subject, request.series_kind)
        {
            let spread = derive_spread(primary, right, request);
            result.series.push(ChartSeriesSpec::from_time_series(
                &spread.series,
                ChartSeriesRole::Spread,
                spread.series.label.clone(),
            ));
            result.spread = Some(spread_summary(primary, right, &spread));
        }
        return result;
    }

    result.series.push(ChartSeriesSpec::from_time_series(
        primary,
        ChartSeriesRole::Primary,
        primary.label.clone(),
    ));

    for overlay_kind in &request.overlay_series_kinds {
        if let Some(overlay) = find_series(all_series, &request.subject, *overlay_kind) {
            result.series.push(ChartSeriesSpec::from_time_series(
                overlay,
                ChartSeriesRole::KindOverlay,
                overlay.label.clone(),
            ));
        }
    }

    for subject in &request.comparison_subjects {
        if let Some(comparison) = find_series(all_series, subject, request.series_kind) {
            let role = if request.mode == ChartMode::Compare {
                ChartSeriesRole::Comparison
            } else {
                ChartSeriesRole::SubjectOverlay
            };
            result.series.push(ChartSeriesSpec::from_time_series(
                comparison,
                role,
                comparison.label.clone(),
            ));
        }
    }

    result
}

pub fn build_plot_request(
    subject: AnalysisSubject,
    label_prefix: impl Into<String>,
    preset: ChartSeriesPreset,
) -> PlotRequest {
    let label = format!("{} {}", label_prefix.into(), preset.label());
    let mut request = PlotRequest::new(subject, preset.primary_kind(), label);
    for overlay in preset.overlay_kinds() {
        request = request.with_overlay(overlay);
    }
    request
}

fn same_plot_identity(left: &PlotRequest, right: &PlotRequest) -> bool {
    left.subject == right.subject
        && left.series_kind == right.series_kind
        && left.comparison_subjects == right.comparison_subjects
        && left.overlay_series_kinds == right.overlay_series_kinds
        && left.mode == right.mode
        && left.label == right.label
}

fn inferred_mode(request: &PlotRequest) -> ChartMode {
    if request.mode != ChartMode::Single {
        return request.mode;
    }
    if !request.comparison_subjects.is_empty() {
        ChartMode::Compare
    } else if !request.overlay_series_kinds.is_empty() {
        ChartMode::Overlay
    } else {
        ChartMode::Single
    }
}

fn derive_spread(
    left: &TimeSeries,
    right: &TimeSeries,
    request: &PlotRequest,
) -> DerivedSpreadSeries {
    let right_label = right.label.clone();
    let label = if request.label.trim().is_empty() {
        format!("{} - {}", left.label, right_label)
    } else {
        request.label.clone()
    };
    derive_matching_date_spread(
        left,
        right,
        AnalysisSubject::SyntheticModel(format!("spread:{}:{}", left.id, right.id)),
        label,
        request.series_kind,
    )
}

fn spread_summary(
    left: &TimeSeries,
    right: &TimeSeries,
    spread: &DerivedSpreadSeries,
) -> SpreadSeriesSummary {
    SpreadSeriesSummary {
        left_label: left.label.clone(),
        right_label: right.label.clone(),
        left_source: spread.left_source.clone(),
        right_source: spread.right_source.clone(),
        common_points: spread.common_points,
        left_points: spread.left_points,
        right_points: spread.right_points,
        partial: spread.partial,
        status: spread.series.status.clone(),
    }
}

fn swap_label(label: &str) -> String {
    if let Some((left, right)) = label.split_once(" vs ") {
        format!("{right} vs {left}")
    } else if let Some(rest) = label.strip_prefix("Spread ")
        && let Some((left, right)) = rest.split_once(" - ")
    {
        format!("Spread {right} - {left}")
    } else {
        format!("{label} swapped")
    }
}

fn subject_key(subject: &AnalysisSubject) -> String {
    match subject {
        AnalysisSubject::WorkspacePortfolio(workspace_id) => format!("portfolio:{workspace_id}"),
        AnalysisSubject::Fund(fund_id) => format!("fund:{fund_id}"),
        AnalysisSubject::FundListing {
            fund_id,
            listing_id,
        } => format!("listing:{fund_id}:{listing_id}"),
        AnalysisSubject::Holding { ticker, source } => format!("holding:{ticker}:{source}"),
        AnalysisSubject::Cash(currency) => format!("cash:{currency}"),
        AnalysisSubject::SyntheticModel(model_id) => format!("model:{model_id}"),
    }
}

pub fn plot_request_from_selected(
    selected: &SelectedInstrument,
    series_kind: TimeSeriesKind,
    label: impl Into<String>,
) -> Option<PlotRequest> {
    let fund_id = selected.fund_id.as_ref()?.clone();
    let subject = match selected.listing_id.as_ref() {
        Some(listing_id) => AnalysisSubject::FundListing {
            fund_id,
            listing_id: listing_id.clone(),
        },
        None => AnalysisSubject::Fund(fund_id),
    };
    Some(PlotRequest::new(subject, series_kind, label))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn creates_plot_request_from_selected_listing() {
        let selected = SelectedInstrument {
            fund_id: Some("fund-vusa".to_owned()),
            listing_id: Some("listing-vusa".to_owned()),
        };

        let request = plot_request_from_selected(&selected, TimeSeriesKind::Price, "VUSA")
            .expect("selected listing should create a plot request");

        assert_eq!(request.series_kind, TimeSeriesKind::Price);
        assert_eq!(
            request.subject,
            AnalysisSubject::FundListing {
                fund_id: "fund-vusa".to_owned(),
                listing_id: "listing-vusa".to_owned()
            }
        );
    }

    #[test]
    fn keeps_recent_plot_history_deduplicated() {
        let mut state = ChartPanelState::default();
        let subject = AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned());
        let first = PlotRequest::new(subject.clone(), TimeSeriesKind::PortfolioValue, "Portfolio");
        let second = PlotRequest::new(subject, TimeSeriesKind::ProjectedIncome, "Income");

        state.set_plot(first.clone());
        state.set_plot(second.clone());
        state.set_plot(first);

        assert_eq!(state.recent_plots.len(), 2);
        assert_eq!(state.recent_plots[0].label, "Portfolio");
        assert_eq!(state.recent_plots[1].label, "Income");
    }

    #[test]
    fn builds_nav_vs_price_request_from_preset() {
        let subject = AnalysisSubject::FundListing {
            fund_id: "fund-vusa".to_owned(),
            listing_id: "listing-vusa".to_owned(),
        };

        let request = build_plot_request(subject, "VUSA", ChartSeriesPreset::NavVsPrice);

        assert_eq!(request.series_kind, TimeSeriesKind::Nav);
        assert_eq!(request.overlay_series_kinds, vec![TimeSeriesKind::Price]);
        assert_eq!(
            ChartSeriesPreset::from_request(&request),
            ChartSeriesPreset::NavVsPrice
        );
    }

    #[test]
    fn adds_and_clears_comparison_overlays() {
        let mut state = ChartPanelState::default();
        let overlay = AnalysisSubject::FundListing {
            fund_id: "fund-isf".to_owned(),
            listing_id: "listing-isf".to_owned(),
        };
        state.set_plot(PlotRequest::new(
            AnalysisSubject::FundListing {
                fund_id: "fund-vusa".to_owned(),
                listing_id: "listing-vusa".to_owned(),
            },
            TimeSeriesKind::Price,
            "VUSA Price",
        ));

        assert!(state.add_comparison_subject(overlay.clone()));
        assert!(state.remove_comparison_subject(&overlay));
        assert_eq!(state.mode, ChartMode::Single);

        assert!(state.add_comparison_subject(overlay));
        assert!(state.clear_overlays());
        assert!(
            state
                .active_plot
                .as_ref()
                .is_some_and(|plot| plot.comparison_subjects.is_empty())
        );
        assert_eq!(state.mode, ChartMode::Single);
    }

    #[test]
    fn selected_point_sets_selected_series_without_changing_plot_subject() {
        let mut state = ChartPanelState::default();
        let subject = AnalysisSubject::FundListing {
            fund_id: "fund-vusa".to_owned(),
            listing_id: "listing-vusa".to_owned(),
        };
        state.set_plot(PlotRequest::new(
            subject.clone(),
            TimeSeriesKind::Price,
            "VUSA Price",
        ));
        let series_id = ChartSeriesId::new(
            ChartSeriesRole::Primary,
            &subject,
            TimeSeriesKind::Price,
            "VUSA Price",
        );

        state.select_point(ChartPointSelection {
            series_id: series_id.clone(),
            subject: subject.clone(),
            series_label: "VUSA Price".to_owned(),
            series_kind: TimeSeriesKind::Price,
            date: "2026-06-20".to_owned(),
            value: 92.18,
            raw_value: 92.18,
            unit: "GBp".to_owned(),
            raw_unit: "GBp".to_owned(),
            source: "seed".to_owned(),
            status: "FRESH".to_owned(),
            value_mode: ChartValueMode::Raw,
        });

        assert_eq!(state.selected_series_id, Some(series_id));
        assert_eq!(
            state
                .active_plot
                .as_ref()
                .map(|request| request.subject.clone()),
            Some(subject)
        );
        assert_eq!(
            state
                .selected_point
                .as_ref()
                .map(ChartPointSelection::label),
            Some("VUSA Price 2026-06-20 RAW".to_owned())
        );
    }

    #[test]
    fn chart_table_focus_tracks_row_and_column_movement() {
        let mut focus = ChartTableFocus::default();

        focus.set(2, ChartDataColumn::Value);
        assert_eq!(focus.row_index, Some(2));
        assert_eq!(focus.column, Some(ChartDataColumn::Value));
        assert_eq!(focus.move_column(1), Some(ChartDataColumn::RawValue));
        assert_eq!(focus.move_column(-1), Some(ChartDataColumn::Value));

        focus.clear();
        assert_eq!(focus.row_index, None);
        assert_eq!(focus.column, None);
    }

    #[test]
    fn selecting_series_clears_point_and_table_focus() {
        let mut state = ChartPanelState::default();
        let subject = AnalysisSubject::FundListing {
            fund_id: "fund-vusa".to_owned(),
            listing_id: "listing-vusa".to_owned(),
        };
        let series_id = ChartSeriesId::new(
            ChartSeriesRole::Primary,
            &subject,
            TimeSeriesKind::Price,
            "VUSA Price",
        );
        let point = ChartPointSelection {
            series_id: series_id.clone(),
            subject: subject.clone(),
            series_label: "VUSA Price".to_owned(),
            series_kind: TimeSeriesKind::Price,
            date: "2026-06-20".to_owned(),
            value: 92.18,
            raw_value: 92.18,
            unit: "GBp".to_owned(),
            raw_unit: "GBp".to_owned(),
            source: "seed".to_owned(),
            status: "FRESH".to_owned(),
            value_mode: ChartValueMode::Raw,
        };

        state.select_point(point);
        state.data_table.select_cell(
            3,
            ChartDataColumn::Value as usize,
            "value",
            "92.18",
            "92.18",
        );
        state.set_table_focus(3, ChartDataColumn::Value);
        state.select_series(series_id.clone());

        assert_eq!(state.selected_series_id, Some(series_id));
        assert!(state.selected_point.is_none());
        assert!(state.data_table.selected_cell.is_none());
        assert_eq!(state.table_focus, ChartTableFocus::default());
    }

    #[test]
    fn compare_and_spread_modes_replace_secondary_subject() {
        let mut state = ChartPanelState::default();
        let left = AnalysisSubject::FundListing {
            fund_id: "fund-vusa".to_owned(),
            listing_id: "listing-vusa".to_owned(),
        };
        let right = AnalysisSubject::FundListing {
            fund_id: "fund-isf".to_owned(),
            listing_id: "listing-isf".to_owned(),
        };
        state.set_plot(PlotRequest::new(
            left.clone(),
            TimeSeriesKind::Price,
            "VUSA",
        ));

        assert!(state.set_comparison_subject(right.clone()));
        assert_eq!(state.mode, ChartMode::Compare);
        assert_eq!(
            state
                .active_plot
                .as_ref()
                .map(|plot| plot.comparison_subjects.clone()),
            Some(vec![right.clone()])
        );

        assert!(state.set_spread_subject(right));
        let plot = state.active_plot.as_ref().expect("plot");
        assert_eq!(state.mode, ChartMode::Spread);
        assert_eq!(plot.mode, ChartMode::Spread);
        assert!(plot.overlay_series_kinds.is_empty());
        assert_eq!(plot.subject, left);
    }

    #[test]
    fn swaps_compare_operands() {
        let mut state = ChartPanelState::default();
        let left = AnalysisSubject::Fund("left".to_owned());
        let right = AnalysisSubject::Fund("right".to_owned());
        state.set_plot(
            PlotRequest::new(left.clone(), TimeSeriesKind::Price, "Left vs Right")
                .with_comparison(right.clone()),
        );

        assert!(state.swap_operands());

        let plot = state.active_plot.as_ref().expect("plot");
        assert_eq!(plot.subject, right);
        assert_eq!(plot.comparison_subjects, vec![left]);
        assert_eq!(plot.label, "Right vs Left");
    }

    #[test]
    fn builds_spread_series_from_matching_dates() {
        let left_subject = AnalysisSubject::Fund("left".to_owned());
        let right_subject = AnalysisSubject::Fund("right".to_owned());
        let left = TimeSeries::new(
            "left-price",
            left_subject.clone(),
            "Left",
            TimeSeriesKind::Price,
            "GBP",
            vec![
                TimeSeriesPoint::new("2026-06-19", 10.0, "FRESH", "seed"),
                TimeSeriesPoint::new("2026-06-20", 12.0, "FRESH", "seed"),
            ],
        );
        let right = TimeSeries::new(
            "right-price",
            right_subject.clone(),
            "Right",
            TimeSeriesKind::Price,
            "GBP",
            vec![TimeSeriesPoint::new("2026-06-20", 7.0, "FRESH", "seed")],
        );
        let request = PlotRequest::spread(
            left_subject,
            right_subject,
            TimeSeriesKind::Price,
            "Left - Right",
        );

        let set = chart_series_for_request(&request, &[left, right]);

        assert_eq!(set.series.len(), 1);
        assert_eq!(set.series[0].role, ChartSeriesRole::Spread);
        assert_eq!(set.series[0].points.len(), 1);
        assert_eq!(set.series[0].points[0].value, 5.0);
        assert_eq!(
            set.spread.as_ref().map(|spread| spread.common_points),
            Some(1)
        );
    }

    fn nearest_test_series(
        id: &str,
        role: ChartSeriesRole,
        values: &[(&str, f64)],
    ) -> ChartSeriesSpec {
        let subject = AnalysisSubject::Fund(id.to_owned());
        let series = TimeSeries::new(
            id,
            subject,
            id,
            TimeSeriesKind::Price,
            "GBP",
            values
                .iter()
                .map(|(date, value)| TimeSeriesPoint::new(*date, *value, "FRESH", "seed"))
                .collect(),
        )
        .with_source("seed")
        .with_status("FRESH");
        ChartSeriesSpec::from_time_series(&series, role, id.to_owned())
    }

    fn date_x(date: &str) -> f64 {
        ChartDateAxis::date_to_x(date).expect("valid test date")
    }

    #[test]
    fn date_axis_roundtrips_epoch_days() {
        assert_eq!(ChartDateAxis::date_to_x("1970-01-01"), Some(0.0));
        assert_eq!(
            ChartDateAxis::x_to_date(date_x("2026-06-20")).as_deref(),
            Some("2026-06-20")
        );
    }

    #[test]
    fn date_axis_preserves_ordering_and_calendar_gaps() {
        let friday = date_x("2026-06-19");
        let monday = date_x("2026-06-22");

        assert!(friday < monday);
        assert_eq!(monday - friday, 3.0);
    }

    #[test]
    fn date_axis_formats_ticks_by_range() {
        let x = date_x("2026-06-20");

        assert_eq!(ChartDateAxis::format_tick(x, PlotRange::OneMonth), "20 Jun");
        assert_eq!(
            ChartDateAxis::format_tick(x, PlotRange::OneYear),
            "Jun 2026"
        );
        assert_eq!(ChartDateAxis::format_tick(x, PlotRange::All), "2026");
    }

    #[test]
    fn visible_points_filter_by_date_window() {
        let points = vec![
            TimeSeriesPoint::new("2026-01-20", 1.0, "FRESH", "seed"),
            TimeSeriesPoint::new("2026-05-20", 2.0, "FRESH", "seed"),
            TimeSeriesPoint::new("2026-06-20", 3.0, "FRESH", "seed"),
        ];

        let visible = visible_points_for_range(&points, PlotRange::OneMonth);

        assert_eq!(
            visible
                .iter()
                .map(|point| point.date.as_str())
                .collect::<Vec<_>>(),
            vec!["2026-05-20", "2026-06-20"]
        );
    }

    #[test]
    fn nearest_point_exact_match() {
        let set = ChartSeriesSet {
            series: vec![nearest_test_series(
                "left",
                ChartSeriesRole::Primary,
                &[("2026-06-19", 10.0), ("2026-06-20", 12.0)],
            )],
            spread: None,
        };

        let nearest = find_nearest_chart_point(
            &set,
            PlotRange::All,
            ChartValueMode::Raw,
            date_x("2026-06-20"),
            12.0,
            0.0,
            ChartPointDistanceScale::PLOT_SPACE,
        )
        .expect("point");

        assert_eq!(nearest.point_index, 1);
        assert_eq!(nearest.date, "2026-06-20");
        assert_eq!(nearest.cursor_date.as_deref(), Some("2026-06-20"));
        assert_eq!(nearest.x, date_x("2026-06-20"));
        assert_eq!(nearest.value, 12.0);
    }

    #[test]
    fn nearest_point_respects_threshold_miss() {
        let set = ChartSeriesSet {
            series: vec![nearest_test_series(
                "left",
                ChartSeriesRole::Primary,
                &[("2026-06-19", 10.0)],
            )],
            spread: None,
        };

        assert!(
            find_nearest_chart_point(
                &set,
                PlotRange::All,
                ChartValueMode::Raw,
                10.0,
                10.0,
                2.0,
                ChartPointDistanceScale::PLOT_SPACE,
            )
            .is_none()
        );
    }

    #[test]
    fn nearest_point_chooses_closest_series() {
        let set = ChartSeriesSet {
            series: vec![
                nearest_test_series("left", ChartSeriesRole::Primary, &[("2026-06-19", 10.0)]),
                nearest_test_series(
                    "right",
                    ChartSeriesRole::Comparison,
                    &[("2026-06-19", 14.0)],
                ),
            ],
            spread: None,
        };

        let nearest = find_nearest_chart_point(
            &set,
            PlotRange::All,
            ChartValueMode::Raw,
            date_x("2026-06-19"),
            13.5,
            5.0,
            ChartPointDistanceScale::PLOT_SPACE,
        )
        .expect("nearest");

        assert_eq!(nearest.series_label, "right");
        assert_eq!(nearest.value, 14.0);
    }

    #[test]
    fn nearest_point_chooses_closest_date_value_pair() {
        let set = ChartSeriesSet {
            series: vec![nearest_test_series(
                "left",
                ChartSeriesRole::Primary,
                &[
                    ("2026-06-19", 10.0),
                    ("2026-06-22", 13.0),
                    ("2026-06-30", 11.0),
                ],
            )],
            spread: None,
        };

        let nearest = find_nearest_chart_point(
            &set,
            PlotRange::All,
            ChartValueMode::Raw,
            date_x("2026-06-22") + 0.25,
            12.8,
            2.0,
            ChartPointDistanceScale::PLOT_SPACE,
        )
        .expect("nearest");

        assert_eq!(nearest.date, "2026-06-22");
        assert_eq!(nearest.value, 13.0);
    }

    #[test]
    fn nearest_point_respects_value_mode() {
        let set = ChartSeriesSet {
            series: vec![nearest_test_series(
                "left",
                ChartSeriesRole::Primary,
                &[("2026-06-19", 10.0), ("2026-06-20", 15.0)],
            )],
            spread: None,
        };

        let nearest = find_nearest_chart_point(
            &set,
            PlotRange::All,
            ChartValueMode::Rebased100,
            date_x("2026-06-20"),
            150.0,
            0.01,
            ChartPointDistanceScale::PLOT_SPACE,
        )
        .expect("rebased point");

        assert_eq!(nearest.date, "2026-06-20");
        assert_eq!(nearest.value, 150.0);
        assert_eq!(nearest.raw_value, 15.0);
        assert_eq!(nearest.unit, "index");
    }

    #[test]
    fn nearest_point_ignores_invalid_values() {
        let set = ChartSeriesSet {
            series: vec![nearest_test_series(
                "left",
                ChartSeriesRole::Primary,
                &[
                    ("2026-06-19", f64::NAN),
                    ("2026-06-20", 12.0),
                    ("2026-06-21", f64::INFINITY),
                ],
            )],
            spread: None,
        };

        let nearest = find_nearest_chart_point(
            &set,
            PlotRange::All,
            ChartValueMode::Raw,
            date_x("2026-06-20"),
            12.0,
            100.0,
            ChartPointDistanceScale::PLOT_SPACE,
        )
        .expect("valid point");

        assert_eq!(nearest.date, "2026-06-20");
    }

    #[test]
    fn nearest_point_works_for_spread_series() {
        let left_subject = AnalysisSubject::Fund("left".to_owned());
        let right_subject = AnalysisSubject::Fund("right".to_owned());
        let left = TimeSeries::new(
            "left-price",
            left_subject.clone(),
            "Left",
            TimeSeriesKind::Price,
            "GBP",
            vec![TimeSeriesPoint::new("2026-06-20", 12.0, "FRESH", "seed")],
        );
        let right = TimeSeries::new(
            "right-price",
            right_subject.clone(),
            "Right",
            TimeSeriesKind::Price,
            "GBP",
            vec![TimeSeriesPoint::new("2026-06-20", 7.0, "FRESH", "seed")],
        );
        let request = PlotRequest::spread(
            left_subject,
            right_subject,
            TimeSeriesKind::Price,
            "Left - Right",
        );
        let set = chart_series_for_request(&request, &[left, right]);

        let nearest = find_nearest_chart_point(
            &set,
            PlotRange::All,
            ChartValueMode::Raw,
            date_x("2026-06-20"),
            5.0,
            0.01,
            ChartPointDistanceScale::PLOT_SPACE,
        )
        .expect("spread point");

        assert_eq!(nearest.series_kind, TimeSeriesKind::Price);
        assert_eq!(nearest.value, 5.0);
        assert_eq!(nearest.status, "DERIVED");
        assert_eq!(nearest.source, "derived");
    }

    #[test]
    fn relative_series_selection_clears_stale_point() {
        let left = nearest_test_series("left", ChartSeriesRole::Primary, &[("2026-06-19", 10.0)]);
        let right = nearest_test_series(
            "right",
            ChartSeriesRole::Comparison,
            &[("2026-06-19", 12.0)],
        );
        let set = ChartSeriesSet {
            series: vec![left.clone(), right.clone()],
            spread: None,
        };
        let mut state = ChartPanelState::default();
        state.select_point(ChartPointSelection {
            series_id: left.id,
            subject: left.subject,
            series_label: left.label,
            series_kind: left.kind,
            date: "2026-06-19".to_owned(),
            value: 10.0,
            raw_value: 10.0,
            unit: "GBP".to_owned(),
            raw_unit: "GBP".to_owned(),
            source: "seed".to_owned(),
            status: "FRESH".to_owned(),
            value_mode: ChartValueMode::Raw,
        });

        assert!(state.select_relative_series(&set, 1));

        assert_eq!(state.selected_series_id, Some(right.id));
        assert!(state.selected_point.is_none());
    }

    #[test]
    fn relative_series_selection_cycles_from_edges() {
        let left = nearest_test_series("left", ChartSeriesRole::Primary, &[("2026-06-19", 10.0)]);
        let right = nearest_test_series(
            "right",
            ChartSeriesRole::Comparison,
            &[("2026-06-19", 12.0)],
        );
        let set = ChartSeriesSet {
            series: vec![left.clone(), right.clone()],
            spread: None,
        };
        let mut state = ChartPanelState::default();

        assert!(state.select_relative_series(&set, 1));
        assert_eq!(state.selected_series_id, Some(left.id.clone()));
        assert!(state.select_relative_series(&set, -1));
        assert_eq!(state.selected_series_id, Some(right.id.clone()));
        assert!(state.select_relative_series(&set, 1));
        assert_eq!(state.selected_series_id, Some(left.id));
    }

    #[test]
    fn selected_point_marker_remaps_after_value_mode_change() {
        let series = nearest_test_series(
            "left",
            ChartSeriesRole::Primary,
            &[("2026-06-19", 10.0), ("2026-06-20", 15.0)],
        );
        let selected = ChartPointSelection {
            series_id: series.id.clone(),
            subject: series.subject.clone(),
            series_label: series.label.clone(),
            series_kind: series.kind,
            date: "2026-06-20".to_owned(),
            value: 15.0,
            raw_value: 15.0,
            unit: "GBP".to_owned(),
            raw_unit: "GBP".to_owned(),
            source: "seed".to_owned(),
            status: "FRESH".to_owned(),
            value_mode: ChartValueMode::Raw,
        };
        let set = ChartSeriesSet {
            series: vec![series],
            spread: None,
        };

        let marker = selected_chart_point_marker(
            &set,
            PlotRange::All,
            ChartValueMode::Rebased100,
            &selected,
        )
        .expect("marker");

        assert_eq!(marker.date, "2026-06-20");
        assert_eq!(marker.x, date_x("2026-06-20"));
        assert_eq!(marker.value, 150.0);
        assert_eq!(marker.raw_value, 15.0);
    }

    #[test]
    fn selected_point_marker_clears_when_date_leaves_range() {
        let series = nearest_test_series(
            "left",
            ChartSeriesRole::Primary,
            &[
                ("2026-01-20", 10.0),
                ("2026-05-20", 12.0),
                ("2026-06-20", 15.0),
            ],
        );
        let selected = ChartPointSelection {
            series_id: series.id.clone(),
            subject: series.subject.clone(),
            series_label: series.label.clone(),
            series_kind: series.kind,
            date: "2026-01-20".to_owned(),
            value: 10.0,
            raw_value: 10.0,
            unit: "GBP".to_owned(),
            raw_unit: "GBP".to_owned(),
            source: "seed".to_owned(),
            status: "FRESH".to_owned(),
            value_mode: ChartValueMode::Raw,
        };
        let set = ChartSeriesSet {
            series: vec![series],
            spread: None,
        };

        assert!(
            selected_chart_point_marker(&set, PlotRange::OneMonth, ChartValueMode::Raw, &selected)
                .is_none()
        );
    }

    #[test]
    fn nearest_point_copy_payload_preserves_display_and_raw_values() {
        let set = ChartSeriesSet {
            series: vec![nearest_test_series(
                "left",
                ChartSeriesRole::Primary,
                &[("2026-06-19", 10.0), ("2026-06-20", 15.0)],
            )],
            spread: None,
        };

        let nearest = find_nearest_chart_point(
            &set,
            PlotRange::All,
            ChartValueMode::PercentChange,
            date_x("2026-06-20"),
            0.5,
            0.01,
            ChartPointDistanceScale::PLOT_SPACE,
        )
        .expect("percent point");

        assert_eq!(nearest.copy_value_text(), "0.5\t%\t15");
        assert_eq!(
            nearest.point_selection().copy_text().split('\t').count(),
            11
        );
        assert!(
            nearest
                .point_selection()
                .copy_text()
                .ends_with("\tx_mode=date")
        );
    }
}
