use serde::{Deserialize, Serialize};
use std::cmp::Ordering;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum TableId {
    PortfolioPositions,
    EtfsFunds,
    ExposureCountries,
    ExposureSectors,
    ExposureCurrencies,
    ExposureTopHoldings,
    ExposureDiagnostics,
    Holdings,
    Documents,
    Dividends,
    ScheduledJobs,
    JobRuns,
    Alerts,
    ChartSeriesData,
    SearchResults,
    DataOperationsReadiness,
    DataOperationsActions,
    DataOperationsPlan,
    DataOperationsScheduler,
    DataOperationsSources,
    DataOperationsFetchLogs,
    DataOperationsConstituents,
    DataOperationsDiagnostics,
    DataOperationsApiSections,
    FundDetailListings,
    FundDetailHoldings,
    FundDetailDistributions,
    FundDetailDocuments,
}

impl TableId {
    pub const fn key(self) -> &'static str {
        match self {
            Self::PortfolioPositions => "portfolio.positions",
            Self::EtfsFunds => "etfs.funds",
            Self::ExposureCountries => "exposure.countries",
            Self::ExposureSectors => "exposure.sectors",
            Self::ExposureCurrencies => "exposure.currencies",
            Self::ExposureTopHoldings => "exposure.top_holdings",
            Self::ExposureDiagnostics => "exposure.diagnostics",
            Self::Holdings => "holdings.rows",
            Self::Documents => "documents.snapshots",
            Self::Dividends => "dividends.rows",
            Self::ScheduledJobs => "jobs.scheduled",
            Self::JobRuns => "jobs.runs",
            Self::Alerts => "alerts.rows",
            Self::ChartSeriesData => "charts.series_data",
            Self::SearchResults => "search.results",
            Self::DataOperationsReadiness => "data_operations.readiness",
            Self::DataOperationsActions => "data_operations.actions",
            Self::DataOperationsPlan => "data_operations.market_data_plan",
            Self::DataOperationsScheduler => "data_operations.scheduler",
            Self::DataOperationsSources => "data_operations.source_budgets",
            Self::DataOperationsFetchLogs => "data_operations.fetch_logs",
            Self::DataOperationsConstituents => "data_operations.constituents",
            Self::DataOperationsDiagnostics => "data_operations.diagnostics",
            Self::DataOperationsApiSections => "data_operations.api_sections",
            Self::FundDetailListings => "fund_detail.listings",
            Self::FundDetailHoldings => "fund_detail.holdings",
            Self::FundDetailDistributions => "fund_detail.distributions",
            Self::FundDetailDocuments => "fund_detail.documents",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ColumnDescriptor {
    pub key: &'static str,
    pub label: &'static str,
    pub default_width: f32,
    pub min_width: f32,
    pub max_width: f32,
    pub default_visible: bool,
    pub hideable: bool,
    pub clip: bool,
}

impl ColumnDescriptor {
    pub const fn new(
        key: &'static str,
        label: &'static str,
        default_width: f32,
        min_width: f32,
        max_width: f32,
    ) -> Self {
        Self {
            key,
            label,
            default_width,
            min_width,
            max_width,
            default_visible: true,
            hideable: true,
            clip: false,
        }
    }

    pub const fn required(mut self) -> Self {
        self.hideable = false;
        self
    }

    pub const fn hidden_by_default(mut self) -> Self {
        self.default_visible = false;
        self
    }

    pub const fn clipped(mut self) -> Self {
        self.clip = true;
        self
    }

    pub fn clamp_width(self, width: f32) -> f32 {
        let width = if width.is_finite() {
            width
        } else {
            self.default_width
        };
        width.clamp(self.min_width, self.max_width)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ColumnLayout {
    pub key: String,
    pub width: f32,
    pub visible: bool,
    pub order: usize,
}

impl ColumnLayout {
    fn from_descriptor(descriptor: ColumnDescriptor, order: usize) -> Self {
        Self {
            key: descriptor.key.to_owned(),
            width: descriptor.default_width,
            visible: descriptor.default_visible,
            order,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TableLayoutState {
    pub table_id: String,
    pub columns: Vec<ColumnLayout>,
    #[serde(default)]
    pub revision: u64,
}

impl TableLayoutState {
    pub fn from_descriptors(table_id: TableId, descriptors: &[ColumnDescriptor]) -> Self {
        Self {
            table_id: table_id.key().to_owned(),
            columns: descriptors
                .iter()
                .copied()
                .enumerate()
                .map(|(order, descriptor)| ColumnLayout::from_descriptor(descriptor, order))
                .collect(),
            revision: 0,
        }
    }

    fn reconcile(&mut self, table_id: TableId, descriptors: &[ColumnDescriptor]) {
        self.table_id = table_id.key().to_owned();
        self.columns = descriptors
            .iter()
            .copied()
            .enumerate()
            .map(|(order, descriptor)| {
                let stored = self
                    .columns
                    .iter()
                    .find(|column| column.key == descriptor.key);
                ColumnLayout {
                    key: descriptor.key.to_owned(),
                    width: descriptor.clamp_width(
                        stored.map_or(descriptor.default_width, |column| column.width),
                    ),
                    visible: stored
                        .map(|column| column.visible)
                        .unwrap_or(descriptor.default_visible)
                        || !descriptor.hideable,
                    order,
                }
            })
            .collect();
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct TableLayoutRegistry {
    #[serde(default)]
    pub tables: Vec<TableLayoutState>,
    #[serde(default)]
    generation: u64,
}

impl TableLayoutRegistry {
    pub fn ensure(&mut self, table_id: TableId, descriptors: &[ColumnDescriptor]) {
        if let Some(table) = self
            .tables
            .iter_mut()
            .find(|table| table.table_id == table_id.key())
        {
            table.reconcile(table_id, descriptors);
        } else {
            self.tables
                .push(TableLayoutState::from_descriptors(table_id, descriptors));
        }
    }

    pub fn table_revision(
        &mut self,
        table_id: TableId,
        descriptors: &[ColumnDescriptor],
    ) -> (u64, u64) {
        self.ensure(table_id, descriptors);
        let revision = self.table(table_id).map_or(0, |table| table.revision);
        (self.generation, revision)
    }

    pub fn width(
        &mut self,
        table_id: TableId,
        descriptor: ColumnDescriptor,
        descriptors: &[ColumnDescriptor],
    ) -> f32 {
        self.ensure(table_id, descriptors);
        self.column(table_id, descriptor.key)
            .map_or(descriptor.default_width, |column| {
                descriptor.clamp_width(column.width)
            })
    }

    pub fn is_visible(
        &mut self,
        table_id: TableId,
        descriptor: ColumnDescriptor,
        descriptors: &[ColumnDescriptor],
    ) -> bool {
        self.ensure(table_id, descriptors);
        !descriptor.hideable
            || self
                .column(table_id, descriptor.key)
                .is_none_or(|column| column.visible)
    }

    pub fn visible_indices(
        &mut self,
        table_id: TableId,
        descriptors: &[ColumnDescriptor],
    ) -> Vec<usize> {
        self.ensure(table_id, descriptors);
        descriptors
            .iter()
            .copied()
            .enumerate()
            .filter_map(|(index, descriptor)| {
                self.is_visible(table_id, descriptor, descriptors)
                    .then_some(index)
            })
            .collect()
    }

    pub fn set_visible(
        &mut self,
        table_id: TableId,
        descriptors: &[ColumnDescriptor],
        key: &str,
        visible: bool,
    ) -> bool {
        self.ensure(table_id, descriptors);
        let Some(descriptor) = descriptors.iter().find(|column| column.key == key) else {
            return false;
        };
        if !descriptor.hideable && !visible {
            return false;
        }
        let Some(table) = self.table_mut(table_id) else {
            return false;
        };
        let Some(column) = table.columns.iter_mut().find(|column| column.key == key) else {
            return false;
        };
        if column.visible == visible {
            return false;
        }
        column.visible = visible;
        table.revision = table.revision.wrapping_add(1);
        true
    }

    pub fn adjust_width(
        &mut self,
        table_id: TableId,
        descriptors: &[ColumnDescriptor],
        key: &str,
        delta: f32,
    ) -> bool {
        self.ensure(table_id, descriptors);
        let Some(descriptor) = descriptors.iter().copied().find(|column| column.key == key) else {
            return false;
        };
        let Some(table) = self.table_mut(table_id) else {
            return false;
        };
        let Some(column) = table.columns.iter_mut().find(|column| column.key == key) else {
            return false;
        };
        let next = descriptor.clamp_width(column.width + delta);
        if (next - column.width).abs() < f32::EPSILON {
            return false;
        }
        column.width = next;
        table.revision = table.revision.wrapping_add(1);
        true
    }

    pub fn show_all(&mut self, table_id: TableId, descriptors: &[ColumnDescriptor]) -> bool {
        self.ensure(table_id, descriptors);
        let Some(table) = self.table_mut(table_id) else {
            return false;
        };
        let mut changed = false;
        for column in &mut table.columns {
            if !column.visible {
                column.visible = true;
                changed = true;
            }
        }
        if changed {
            table.revision = table.revision.wrapping_add(1);
        }
        changed
    }

    pub fn show_all_tables(&mut self) {
        let mut changed = false;
        for table in &mut self.tables {
            let mut table_changed = false;
            for column in &mut table.columns {
                if !column.visible {
                    column.visible = true;
                    changed = true;
                    table_changed = true;
                }
            }
            if table_changed {
                table.revision = table.revision.wrapping_add(1);
            }
        }
        if changed {
            self.generation = self.generation.wrapping_add(1);
        }
    }

    pub fn reset(&mut self, table_id: TableId, descriptors: &[ColumnDescriptor]) {
        let revision = self
            .table(table_id)
            .map_or(1, |table| table.revision.wrapping_add(1));
        let mut table = TableLayoutState::from_descriptors(table_id, descriptors);
        table.revision = revision;
        if let Some(existing) = self
            .tables
            .iter_mut()
            .find(|table| table.table_id == table_id.key())
        {
            *existing = table;
        } else {
            self.tables.push(table);
        }
    }

    pub fn reset_all(&mut self) {
        self.tables.clear();
        self.generation = self.generation.wrapping_add(1);
    }

    pub fn visible_row_text(
        &mut self,
        table_id: TableId,
        descriptors: &[ColumnDescriptor],
        cells: &[(&str, String)],
    ) -> String {
        self.ensure(table_id, descriptors);
        descriptors
            .iter()
            .copied()
            .filter(|descriptor| self.is_visible(table_id, *descriptor, descriptors))
            .filter_map(|descriptor| {
                cells
                    .iter()
                    .find(|(key, _)| *key == descriptor.key)
                    .map(|(_, value)| value.clone())
            })
            .collect::<Vec<_>>()
            .join("\t")
    }

    fn table(&self, table_id: TableId) -> Option<&TableLayoutState> {
        self.tables
            .iter()
            .find(|table| table.table_id == table_id.key())
    }

    fn table_mut(&mut self, table_id: TableId) -> Option<&mut TableLayoutState> {
        self.tables
            .iter_mut()
            .find(|table| table.table_id == table_id.key())
    }

    fn column(&self, table_id: TableId, key: &str) -> Option<&ColumnLayout> {
        self.table(table_id)?
            .columns
            .iter()
            .find(|column| column.key == key)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SortDirection {
    Asc,
    Desc,
}

impl SortDirection {
    pub fn apply(self, ordering: Ordering) -> Ordering {
        match self {
            Self::Asc => ordering,
            Self::Desc => ordering.reverse(),
        }
    }

    pub fn marker(self) -> &'static str {
        match self {
            Self::Asc => "^",
            Self::Desc => "v",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SortSpec {
    pub column: String,
    pub direction: SortDirection,
}

impl SortSpec {
    pub fn new(column: impl Into<String>, direction: SortDirection) -> Self {
        Self {
            column: column.into(),
            direction,
        }
    }

    pub fn is_column(&self, column: &str) -> bool {
        self.column == column
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TableSelection {
    pub selected_index: Option<usize>,
}

impl TableSelection {
    pub fn clear(&mut self) {
        self.selected_index = None;
    }

    pub fn select(&mut self, index: usize) {
        self.selected_index = Some(index);
    }

    pub fn is_selected(&self, index: usize) -> bool {
        self.selected_index == Some(index)
    }

    pub fn move_by(&mut self, visible_indices: &[usize], offset: isize) -> Option<usize> {
        if visible_indices.is_empty() {
            self.clear();
            return None;
        }

        let last_position = visible_indices.len() - 1;
        let current_position = self
            .selected_index
            .and_then(|selected| visible_indices.iter().position(|index| *index == selected));

        let next_position = match (current_position, offset.cmp(&0)) {
            (Some(position), Ordering::Less) => position.saturating_sub(offset.unsigned_abs()),
            (Some(position), Ordering::Greater) => {
                position.saturating_add(offset as usize).min(last_position)
            }
            (Some(position), Ordering::Equal) => position,
            (None, Ordering::Less) => last_position,
            (None, Ordering::Equal | Ordering::Greater) => 0,
        };

        let selected = visible_indices[next_position];
        self.select(selected);
        Some(selected)
    }

    pub fn selected_visible<'a>(&self, visible_indices: &'a [usize]) -> Option<&'a usize> {
        let selected = self.selected_index?;
        visible_indices.iter().find(|index| **index == selected)
    }

    pub fn retain_visible(&mut self, visible_indices: &[usize]) {
        if self.selected_visible(visible_indices).is_none() {
            self.clear();
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SelectedCell {
    pub table_id: TableId,
    pub row_index: usize,
    pub column: String,
    pub display_value: String,
    pub raw_value: String,
}

impl SelectedCell {
    pub fn new(
        table_id: TableId,
        row_index: usize,
        column: impl Into<String>,
        display_value: impl Into<String>,
        raw_value: impl Into<String>,
    ) -> Self {
        Self {
            table_id,
            row_index,
            column: column.into(),
            display_value: display_value.into(),
            raw_value: raw_value.into(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EditableCell {
    pub table_id: TableId,
    pub row_index: usize,
    pub column: String,
}

impl EditableCell {
    pub fn new(table_id: TableId, row_index: usize, column: impl Into<String>) -> Self {
        Self {
            table_id,
            row_index,
            column: column.into(),
        }
    }

    pub fn matches(&self, table_id: TableId, row_index: usize, column: &str) -> bool {
        self.table_id == table_id && self.row_index == row_index && self.column == column
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CellEditState {
    pub editing_cell: Option<EditableCell>,
    pub draft: String,
}

impl CellEditState {
    pub fn begin(&mut self, cell: EditableCell, draft: impl Into<String>) {
        self.editing_cell = Some(cell);
        self.draft = draft.into();
    }

    pub fn cancel(&mut self) {
        self.editing_cell = None;
        self.draft.clear();
    }

    pub fn is_editing(&self) -> bool {
        self.editing_cell.is_some()
    }

    pub fn is_editing_cell(&self, table_id: TableId, row_index: usize, column: &str) -> bool {
        self.editing_cell
            .as_ref()
            .is_some_and(|cell| cell.matches(table_id, row_index, column))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TableState {
    pub id: TableId,
    pub selection: TableSelection,
    pub focused_row_index: Option<usize>,
    pub focused_column_index: Option<usize>,
    pub selected_cell: Option<SelectedCell>,
    pub sort: Option<SortSpec>,
    pub filter: String,
    pub edit: CellEditState,
}

impl TableState {
    pub fn new(id: TableId) -> Self {
        Self {
            id,
            selection: TableSelection::default(),
            focused_row_index: None,
            focused_column_index: None,
            selected_cell: None,
            sort: None,
            filter: String::new(),
            edit: CellEditState::default(),
        }
    }

    pub fn selected_index(&self) -> Option<usize> {
        self.selection.selected_index
    }

    pub fn select(&mut self, index: usize) {
        self.selection.select(index);
        self.focused_row_index = Some(index);
        self.focused_column_index = None;
        self.selected_cell = None;
    }

    pub fn clear_selection(&mut self) {
        self.selection.clear();
        self.clear_focus();
    }

    pub fn clear_focus(&mut self) {
        self.focused_row_index = None;
        self.focused_column_index = None;
        self.selected_cell = None;
    }

    pub fn select_cell(
        &mut self,
        row_index: usize,
        column_index: usize,
        column: impl Into<String>,
        display_value: impl Into<String>,
        raw_value: impl Into<String>,
    ) {
        self.selection.select(row_index);
        self.focused_row_index = Some(row_index);
        self.focused_column_index = Some(column_index);
        self.selected_cell = Some(SelectedCell::new(
            self.id,
            row_index,
            column,
            display_value,
            raw_value,
        ));
    }

    pub fn set_focused_cell_payload(
        &mut self,
        column: impl Into<String>,
        display_value: impl Into<String>,
        raw_value: impl Into<String>,
    ) -> bool {
        let (Some(row_index), Some(_)) = (self.focused_row_index, self.focused_column_index) else {
            return false;
        };
        self.selection.select(row_index);
        self.selected_cell = Some(SelectedCell::new(
            self.id,
            row_index,
            column,
            display_value,
            raw_value,
        ));
        true
    }

    pub fn move_focus_row(
        &mut self,
        visible_indices: &[usize],
        offset: isize,
        default_column_index: Option<usize>,
    ) -> Option<usize> {
        if let Some(focused_row_index) = self.focused_row_index {
            self.selection.select(focused_row_index);
        }
        let row_index = self.selection.move_by(visible_indices, offset)?;
        self.focused_row_index = Some(row_index);
        if self.focused_column_index.is_none() {
            self.focused_column_index = default_column_index;
        }
        self.selected_cell = None;
        Some(row_index)
    }

    pub fn move_focus_column(
        &mut self,
        column_count: usize,
        offset: isize,
    ) -> Option<(usize, usize)> {
        if column_count == 0 {
            self.focused_column_index = None;
            self.selected_cell = None;
            return None;
        }
        let row_index = self.focused_row_index.or(self.selection.selected_index)?;
        let last_column = column_count - 1;
        let column_index = match (self.focused_column_index, offset.cmp(&0)) {
            (Some(index), Ordering::Less) => index.saturating_sub(offset.unsigned_abs()),
            (Some(index), Ordering::Greater) => {
                index.saturating_add(offset as usize).min(last_column)
            }
            (Some(index), Ordering::Equal) => index.min(last_column),
            (None, Ordering::Less) => last_column,
            (None, Ordering::Equal | Ordering::Greater) => 0,
        };
        self.selection.select(row_index);
        self.focused_row_index = Some(row_index);
        self.focused_column_index = Some(column_index);
        self.selected_cell = None;
        Some((row_index, column_index))
    }

    pub fn move_focus_visible_column(
        &mut self,
        visible_column_indices: &[usize],
        offset: isize,
    ) -> Option<(usize, usize)> {
        if visible_column_indices.is_empty() {
            self.focused_column_index = None;
            self.selected_cell = None;
            return None;
        }
        let row_index = self.focused_row_index.or(self.selection.selected_index)?;
        let last_position = visible_column_indices.len() - 1;
        let current_position = self.focused_column_index.and_then(|focused| {
            visible_column_indices
                .iter()
                .position(|index| *index == focused)
        });
        let next_position = match (current_position, offset.cmp(&0)) {
            (Some(position), Ordering::Less) => position.saturating_sub(offset.unsigned_abs()),
            (Some(position), Ordering::Greater) => {
                position.saturating_add(offset as usize).min(last_position)
            }
            (Some(position), Ordering::Equal) => position,
            (None, Ordering::Less) => last_position,
            (None, Ordering::Equal | Ordering::Greater) => 0,
        };
        let column_index = visible_column_indices[next_position];
        self.selection.select(row_index);
        self.focused_row_index = Some(row_index);
        self.focused_column_index = Some(column_index);
        self.selected_cell = None;
        Some((row_index, column_index))
    }

    pub fn is_focused_row(&self, row_index: usize) -> bool {
        self.focused_row_index == Some(row_index)
    }

    pub fn is_focused_cell(&self, row_index: usize, column_index: usize) -> bool {
        self.focused_row_index == Some(row_index) && self.focused_column_index == Some(column_index)
    }

    pub fn retain_visible(&mut self, visible_indices: &[usize]) {
        self.selection.retain_visible(visible_indices);
        if self
            .focused_row_index
            .is_some_and(|index| !visible_indices.contains(&index))
        {
            self.clear_focus();
        }
    }

    pub fn toggle_sort(&mut self, column: &str) -> Option<SortDirection> {
        let direction = match self
            .sort
            .as_ref()
            .filter(|sort| sort.is_column(column))
            .map(|sort| sort.direction)
        {
            Some(SortDirection::Asc) => Some(SortDirection::Desc),
            Some(SortDirection::Desc) => None,
            None => Some(SortDirection::Asc),
        };

        self.sort = direction.map(|direction| SortSpec::new(column, direction));
        direction
    }

    pub fn sort_direction(&self, column: &str) -> Option<SortDirection> {
        self.sort
            .as_ref()
            .filter(|sort| sort.is_column(column))
            .map(|sort| sort.direction)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn moves_selection_through_visible_indices() {
        let mut selection = TableSelection::default();
        let visible = [2, 4, 8];

        assert_eq!(selection.move_by(&visible, 1), Some(2));
        assert_eq!(selection.move_by(&visible, 1), Some(4));
        assert_eq!(selection.move_by(&visible, 1), Some(8));
        assert_eq!(selection.move_by(&visible, 1), Some(8));
        assert_eq!(selection.move_by(&visible, -1), Some(4));
    }

    #[test]
    fn clears_selection_when_there_are_no_visible_rows() {
        let mut selection = TableSelection {
            selected_index: Some(3),
        };

        assert_eq!(selection.move_by(&[], 1), None);
        assert_eq!(selection.selected_index, None);
    }

    #[test]
    fn toggles_sort_for_same_column_and_resets_for_new_column() {
        let mut table = TableState::new(TableId::PortfolioPositions);

        assert_eq!(table.toggle_sort("ticker"), Some(SortDirection::Asc));
        assert_eq!(table.toggle_sort("ticker"), Some(SortDirection::Desc));
        assert_eq!(table.toggle_sort("ticker"), None);
        assert_eq!(table.sort_direction("ticker"), None);
        assert_eq!(table.toggle_sort("market_value"), Some(SortDirection::Asc));
        assert_eq!(table.sort_direction("ticker"), None);
        assert_eq!(
            table.sort_direction("market_value"),
            Some(SortDirection::Asc)
        );
    }

    #[test]
    fn tracks_editable_cell_draft() {
        let mut edit = CellEditState::default();
        edit.begin(
            EditableCell::new(TableId::PortfolioPositions, 4, "price"),
            "12.30",
        );

        assert!(edit.is_editing());
        assert!(edit.is_editing_cell(TableId::PortfolioPositions, 4, "price"));
        edit.cancel();
        assert!(!edit.is_editing());
        assert!(edit.draft.is_empty());
    }

    #[test]
    fn tracks_selected_cell_payload() {
        let mut table = TableState::new(TableId::PortfolioPositions);
        table.select_cell(2, 5, "price", "92.18", "92.18");

        let cell = table.selected_cell.as_ref().expect("cell is selected");
        assert_eq!(cell.row_index, 2);
        assert_eq!(cell.column, "price");
        assert_eq!(cell.raw_value, "92.18");
        assert!(table.is_focused_cell(2, 5));

        table.clear_selection();
        assert!(table.selected_cell.is_none());
    }

    #[test]
    fn moves_focus_rows_and_preserves_column() {
        let mut table = TableState::new(TableId::PortfolioPositions);
        let visible = [2, 4, 8];

        assert_eq!(table.move_focus_row(&visible, 1, Some(0)), Some(2));
        assert!(table.is_focused_cell(2, 0));
        assert_eq!(table.move_focus_column(4, 1), Some((2, 1)));
        assert_eq!(table.move_focus_row(&visible, 1, Some(0)), Some(4));
        assert!(table.is_focused_cell(4, 1));
        assert_eq!(table.selected_index(), Some(4));
    }

    #[test]
    fn moves_focus_columns_with_bounds() {
        let mut table = TableState::new(TableId::ScheduledJobs);
        table.select(3);

        assert_eq!(table.move_focus_column(3, 1), Some((3, 0)));
        assert_eq!(table.move_focus_column(3, 1), Some((3, 1)));
        assert_eq!(table.move_focus_column(3, 10), Some((3, 2)));
        assert_eq!(table.move_focus_column(3, -10), Some((3, 0)));
    }

    #[test]
    fn clearing_focus_preserves_selection() {
        let mut table = TableState::new(TableId::Documents);
        table.select_cell(4, 2, "date", "2026-06-20", "2026-06-20");

        table.clear_focus();

        assert_eq!(table.selected_index(), Some(4));
        assert_eq!(table.focused_row_index, None);
        assert!(table.selected_cell.is_none());
    }

    #[test]
    fn stable_table_ids_and_default_layouts_are_deterministic() {
        let descriptors = [
            ColumnDescriptor::new("ticker", "Ticker", 72.0, 54.0, 120.0).required(),
            ColumnDescriptor::new("message", "Message", 240.0, 120.0, 500.0).hidden_by_default(),
        ];
        let layout = TableLayoutState::from_descriptors(TableId::Alerts, &descriptors);

        assert_eq!(TableId::Alerts.key(), "alerts.rows");
        assert_eq!(layout.table_id, "alerts.rows");
        assert_eq!(layout.columns[0].key, "ticker");
        assert_eq!(layout.columns[0].order, 0);
        assert_eq!(layout.columns[1].order, 1);
        assert!(layout.columns[0].visible);
        assert!(!layout.columns[1].visible);
    }

    #[test]
    fn stored_layout_reconciles_to_stable_descriptor_order() {
        let initial = [
            ColumnDescriptor::new("source", "Source", 100.0, 70.0, 180.0),
            ColumnDescriptor::new("ticker", "Ticker", 72.0, 54.0, 120.0),
        ];
        let updated = [
            ColumnDescriptor::new("ticker", "Ticker", 72.0, 54.0, 120.0),
            ColumnDescriptor::new("status", "Status", 80.0, 60.0, 140.0),
            ColumnDescriptor::new("source", "Source", 100.0, 70.0, 180.0),
        ];
        let mut registry = TableLayoutRegistry::default();
        registry.ensure(TableId::EtfsFunds, &initial);
        registry.ensure(TableId::EtfsFunds, &updated);

        let table = registry.table(TableId::EtfsFunds).expect("layout exists");
        assert_eq!(
            table
                .columns
                .iter()
                .map(|column| (column.key.as_str(), column.order))
                .collect::<Vec<_>>(),
            vec![("ticker", 0), ("status", 1), ("source", 2)]
        );
    }

    #[test]
    fn table_layout_clamps_width_and_supports_visibility_reset() {
        let descriptors = [
            ColumnDescriptor::new("ticker", "Ticker", 72.0, 54.0, 120.0).required(),
            ColumnDescriptor::new("message", "Message", 240.0, 120.0, 300.0),
        ];
        let mut registry = TableLayoutRegistry::default();

        assert!(registry.adjust_width(TableId::Alerts, &descriptors, "message", 500.0));
        assert_eq!(
            registry.width(TableId::Alerts, descriptors[1], &descriptors),
            300.0
        );
        assert!(registry.set_visible(TableId::Alerts, &descriptors, "message", false));
        assert!(!registry.is_visible(TableId::Alerts, descriptors[1], &descriptors));
        assert!(registry.show_all(TableId::Alerts, &descriptors));
        registry.reset(TableId::Alerts, &descriptors);
        assert_eq!(
            registry.width(TableId::Alerts, descriptors[1], &descriptors),
            240.0
        );
    }

    #[test]
    fn visible_row_copy_skips_hidden_columns() {
        let descriptors = [
            ColumnDescriptor::new("ticker", "Ticker", 72.0, 54.0, 120.0).required(),
            ColumnDescriptor::new("message", "Message", 240.0, 120.0, 500.0),
            ColumnDescriptor::new("source", "Source", 100.0, 70.0, 180.0).required(),
        ];
        let mut registry = TableLayoutRegistry::default();
        registry.set_visible(TableId::Alerts, &descriptors, "message", false);

        let copied = registry.visible_row_text(
            TableId::Alerts,
            &descriptors,
            &[
                ("ticker", "VUSA".to_owned()),
                ("message", "long detail".to_owned()),
                ("source", "issuer".to_owned()),
            ],
        );

        assert_eq!(copied, "VUSA\tissuer");
    }

    #[test]
    fn focus_moves_only_through_visible_columns() {
        let mut table = TableState::new(TableId::EtfsFunds);
        table.select(2);

        assert_eq!(table.move_focus_visible_column(&[0, 2, 5], 1), Some((2, 0)));
        assert_eq!(table.move_focus_visible_column(&[0, 2, 5], 1), Some((2, 2)));
        assert_eq!(
            table.move_focus_visible_column(&[0, 2, 5], -1),
            Some((2, 0))
        );
    }
}
