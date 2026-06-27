use crate::app_model::SelectedInstrument;
use crate::compute::diff::{DiffStatus, mock_entity_diffs};
use crate::domain::{DocumentSnapshot, Fund};
use crate::filter::any_contains_ci;
use crate::format::{fmt_date_str, fmt_status};
use crate::pages::format_source;
use crate::pages::{header_cell, page_heading, sortable_header_cell};
use crate::table_state::{ColumnDescriptor, SortSpec, TableId, TableLayoutRegistry, TableState};
use crate::ui::metrics;
use crate::ui::style;
use crate::ui::table_layout::{managed_column, managed_table_revision, table_layout_controls};
use eframe::egui;
use egui_extras::{Column, TableBuilder};
use std::cmp::Ordering;

const COL_TICKER: &str = "ticker";
const COL_TYPE: &str = "document_type";
const COL_DATE: &str = "latest_date";
const COL_STATUS: &str = "status";
const COL_SOURCE: &str = "source";
const COL_CHECKED: &str = "last_checked";

const DOCUMENT_COLUMNS: [ColumnDescriptor; 8] = [
    ColumnDescriptor::new(COL_TICKER, "ETF", 78.0, 64.0, 140.0).required(),
    ColumnDescriptor::new(COL_TYPE, "Document type", 150.0, 100.0, 320.0)
        .required()
        .clipped(),
    ColumnDescriptor::new(COL_DATE, "Latest date", 104.0, 86.0, 180.0),
    ColumnDescriptor::new(COL_STATUS, "Status", 94.0, 78.0, 160.0).required(),
    ColumnDescriptor::new(COL_SOURCE, "Source", 118.0, 90.0, 220.0).required(),
    ColumnDescriptor::new("hash_change", "Hash/change", 160.0, 120.0, 360.0)
        .hidden_by_default()
        .clipped(),
    ColumnDescriptor::new("actions", "Actions", 134.0, 108.0, 240.0).required(),
    ColumnDescriptor::new(COL_CHECKED, "Last checked", 130.0, 104.0, 220.0).hidden_by_default(),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DocumentColumn {
    Ticker,
    DocumentType,
    LatestDate,
    Status,
    Source,
}

impl DocumentColumn {
    const ALL: [Self; 5] = [
        Self::Ticker,
        Self::DocumentType,
        Self::LatestDate,
        Self::Status,
        Self::Source,
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("document column is in ALL")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Ticker => COL_TICKER,
            Self::DocumentType => COL_TYPE,
            Self::LatestDate => COL_DATE,
            Self::Status => COL_STATUS,
            Self::Source => COL_SOURCE,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Ticker => "ETF",
            Self::DocumentType => "Document type",
            Self::LatestDate => "Latest date",
            Self::Status => "Status",
            Self::Source => "Source",
        }
    }

    fn payload(self, document: &DocumentSnapshot) -> (String, String) {
        let value = match self {
            Self::Ticker => document.ticker.clone(),
            Self::DocumentType => document.document_type.clone(),
            Self::LatestDate => document.latest_date.clone(),
            Self::Status => document.status.clone(),
            Self::Source => document.source.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Debug)]
pub struct DocumentsState {
    pub table: TableState,
}

impl Default for DocumentsState {
    fn default() -> Self {
        Self {
            table: TableState::new(TableId::Documents),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DocumentsAction {
    OpenDocument {
        fund_id: String,
        ticker: String,
        document_type: String,
    },
    OpenRelatedFund {
        fund_id: String,
    },
    Back,
    ShowChanges,
    Feedback(String),
}

pub fn render(
    ui: &mut egui::Ui,
    documents: &[DocumentSnapshot],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut DocumentsState,
    layouts: &mut TableLayoutRegistry,
) -> Option<DocumentsAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let stale = documents
                .iter()
                .filter(|document| document.status.eq_ignore_ascii_case("stale"))
                .count();
            let subtitle = format!(
                "{} snapshots · {} stale · double-click opens the in-app viewer",
                documents.len(),
                stale
            );
            style::page_header(ui, "Documents", Some("Library"), Some(&subtitle), |ui| {
                style::mock_badge(ui);
                if stale > 0 {
                    style::status_badge(ui, "STALE");
                }
            });
            ui.add_space(6.0);
            filters(ui, state);
            ui.add_space(4.0);

            action = documents_table(ui, documents, funds, selected, state, layouts);
            ui.add_space(metrics::SPACE_2);
            document_diff_panel(ui, documents, state);
        });
    action
}

pub fn render_viewer(
    ui: &mut egui::Ui,
    documents: &[DocumentSnapshot],
    funds: &[Fund],
    state: &mut DocumentsState,
) -> Option<DocumentsAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let Some(document) = selected_document(documents, state) else {
                page_heading(ui, "Document Viewer");
                ui.label("No document selected. Go to Documents and double-click a row.");
                return;
            };
            let fund = funds.iter().find(|fund| fund.id == document.fund_id);
            let uri = document_mock_uri(document);

            page_heading(
                ui,
                &format!(
                    "Document Viewer | {} | {} | {}",
                    document.ticker, document.document_type, document.latest_date
                ),
            );

            ui.horizontal_wrapped(|ui| {
                if ui.button("Back").clicked() {
                    action = Some(DocumentsAction::Back);
                }
                if ui.button("Open Metadata").clicked() {
                    action = Some(DocumentsAction::Feedback(format!(
                        "CMD: metadata {} {}",
                        document.ticker, document.document_type
                    )));
                }
                if ui.button("Show Changes").clicked() {
                    action = Some(DocumentsAction::ShowChanges);
                }
                if ui.button("Open Fund").clicked() {
                    action = Some(DocumentsAction::OpenRelatedFund {
                        fund_id: document.fund_id.clone(),
                    });
                }
                if ui.button("Copy URL/path").clicked() {
                    ui.copy_text(uri.clone());
                    action = Some(DocumentsAction::Feedback(format!("COPIED: {uri}")));
                }
                if ui.button("Copy Hash").clicked() {
                    ui.copy_text(document.content_hash_change.clone());
                    action = Some(DocumentsAction::Feedback(format!(
                        "COPIED: {}",
                        document.content_hash_change
                    )));
                }
                if ui.button("Copy Metadata").clicked() {
                    ui.copy_text(document_metadata_copy_text(document, fund));
                    action = Some(DocumentsAction::Feedback(format!(
                        "COPIED: metadata {}",
                        document.ticker
                    )));
                }
                if ui
                    .button("Open external")
                    .on_hover_text("Secondary placeholder action")
                    .clicked()
                {
                    action = Some(DocumentsAction::Feedback(
                        "MOCK external document open is not wired".to_owned(),
                    ));
                }
            });

            ui.add_space(8.0);
            ui.horizontal_wrapped(|ui| {
                ui.label(egui::RichText::new("Review").weak());
                style::status_badge(ui, &document.status);
                style::source_badge(ui, &document.source);
                ui.monospace(format!("Hash/change: {}", document.content_hash_change))
                    .on_hover_text(
                        "Hash-level change detection only; PDF/OCR rendering is deferred.",
                    );
                ui.monospace("Previous snapshot: metadata/hash placeholder");
            });
            ui.add_space(8.0);
            TableBuilder::new(ui)
                .id_salt("document_viewer_metadata")
                .striped(true)
                .resizable(true)
                .max_scroll_height(180.0)
                .column(Column::initial(160.0).at_least(120.0))
                .column(Column::remainder().at_least(260.0).clip(true))
                .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
                    header.col(|ui| header_cell(ui, "Field"));
                    header.col(|ui| header_cell(ui, "Value"));
                })
                .body(|mut body| {
                    for (field, value) in document_metadata_rows(document, fund) {
                        body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                            row.col(|ui| {
                                ui.label(field);
                            });
                            row.col(|ui| {
                                ui.monospace(value);
                            });
                        });
                    }
                });

            ui.add_space(8.0);
            ui.label(egui::RichText::new("Extracted fields").strong());
            TableBuilder::new(ui)
                .id_salt("document_viewer_extracted_fields")
                .striped(true)
                .resizable(true)
                .max_scroll_height(128.0)
                .column(Column::initial(180.0).at_least(120.0))
                .column(Column::initial(180.0).at_least(120.0))
                .column(Column::remainder().at_least(180.0))
                .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
                    for label in ["Field", "Mock value", "Source"] {
                        header.col(|ui| header_cell(ui, label));
                    }
                })
                .body(|mut body| {
                    for (field, value) in [
                        ("factsheet date", document.latest_date.as_str()),
                        ("document status", document.status.as_str()),
                        ("hash/change", document.content_hash_change.as_str()),
                    ] {
                        body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                            row.col(|ui| {
                                ui.label(field);
                            });
                            row.col(|ui| {
                                ui.monospace(value);
                            });
                            row.col(|ui| {
                                style::source_badge(ui, &document.source);
                            });
                        });
                    }
                });

            ui.add_space(8.0);
            ui.label(egui::RichText::new("Preview").strong());
            ui.group(|ui| {
                ui.set_min_height(220.0);
                ui.set_min_width(ui.available_width());
                ui.centered_and_justified(|ui| {
                    ui.vertical_centered(|ui| {
                        ui.monospace("PDF preview not implemented");
                        ui.label("Future: render PDF/document pages here inside Mimer.");
                    });
                });
            });
        });
    action
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    documents: &[DocumentSnapshot],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut DocumentsState,
    layouts: &mut TableLayoutRegistry,
) -> bool {
    if ctx.text_edit_focused() {
        return false;
    }

    let visible_indices = visible_document_indices(documents, state);
    let mut moved = false;
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        state.table.move_focus_row(&visible_indices, -1, Some(0));
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        state.table.move_focus_row(&visible_indices, 1, Some(0));
        moved = true;
    }
    let visible_columns = layouts
        .visible_indices(TableId::Documents, &DOCUMENT_COLUMNS)
        .into_iter()
        .filter(|index| *index < DocumentColumn::ALL.len())
        .collect::<Vec<_>>();
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        state.table.move_focus_visible_column(&visible_columns, -1);
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        state.table.move_focus_visible_column(&visible_columns, 1);
        moved = true;
    }
    if moved {
        sync_document_focus(documents, funds, selected, state);
    }

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        if state.table.selected_index().is_none() {
            state.table.move_focus_row(&visible_indices, 1, Some(0));
            sync_document_focus(documents, funds, selected, state);
        }
        if let Some(index) = state.table.selected_index()
            && let Some(document) = documents.get(index)
        {
            select_fund_or_listing(funds, document, selected);
            return true;
        }
    }

    false
}

fn filters(ui: &mut egui::Ui, state: &mut DocumentsState) {
    ui.horizontal_wrapped(|ui| {
        ui.label("Filter");
        let filter_width = metrics::fit_width(ui.available_width(), 320.0, 180.0, 380.0);
        ui.add_sized(
            [filter_width, 20.0],
            egui::TextEdit::singleline(&mut state.table.filter)
                .hint_text("ticker / type / status / source"),
        )
        .on_hover_text("Filters document rows by visible metadata.");
        if ui.button("Clear").clicked() {
            state.table.filter.clear();
            state.table.clear_selection();
        }
    });
}

fn documents_table(
    ui: &mut egui::Ui,
    documents: &[DocumentSnapshot],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut DocumentsState,
    layouts: &mut TableLayoutRegistry,
) -> Option<DocumentsAction> {
    let filtered_documents = visible_document_indices(documents, state);
    state.table.retain_visible(&filtered_documents);
    let mut action = None;

    ui.label(
        egui::RichText::new(format!(
            "Document snapshots ({}/{})",
            filtered_documents.len(),
            documents.len()
        ))
        .strong(),
    );
    if filtered_documents.is_empty() {
        style::state_message(ui, "EMPTY", "No documents match the current filter.");
        return None;
    }

    let visible_row = state
        .table
        .focused_row_index
        .or(state.table.selected_index())
        .and_then(|index| documents.get(index))
        .map(|document| {
            (
                document.ticker.as_str(),
                layouts.visible_row_text(
                    TableId::Documents,
                    &DOCUMENT_COLUMNS,
                    &[
                        (COL_TICKER, document.ticker.clone()),
                        (COL_TYPE, document.document_type.clone()),
                        (COL_DATE, document.latest_date.clone()),
                        (COL_STATUS, document.status.clone()),
                        (COL_SOURCE, document.source.clone()),
                        ("hash_change", document.content_hash_change.clone()),
                        ("actions", "open/fund/copy".to_owned()),
                        (COL_CHECKED, document.last_checked.clone()),
                    ],
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::Documents,
        &DOCUMENT_COLUMNS,
        state
            .table
            .focused_column_index
            .and_then(|index| DocumentColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.table.clear_focus();
    }

    let table_height = (ui.available_height() - 28.0).clamp(260.0, 620.0);
    let revision = managed_table_revision(layouts, TableId::Documents, &DOCUMENT_COLUMNS);
    let mut table = TableBuilder::new(ui)
        .id_salt(("documents_table", revision))
        .striped(true)
        .resizable(false)
        .auto_shrink(false)
        .max_scroll_height(table_height);

    for descriptor in DOCUMENT_COLUMNS {
        table = table.column(managed_column(
            layouts,
            TableId::Documents,
            &DOCUMENT_COLUMNS,
            descriptor,
        ));
    }

    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.table,
                    COL_TICKER,
                    DocumentColumn::Ticker.label(),
                )
            });
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.table,
                    COL_TYPE,
                    DocumentColumn::DocumentType.label(),
                )
            });
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.table,
                    COL_DATE,
                    DocumentColumn::LatestDate.label(),
                )
            });
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_STATUS, "Status"));
            header.col(|ui| sortable_header_cell(ui, &mut state.table, COL_SOURCE, "Source"));
            header.col(|ui| header_cell(ui, "Hash/change"));
            header.col(|ui| header_cell(ui, "Actions"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.table, COL_CHECKED, "Last checked");
            });
        })
        .body(|mut body| {
            for index in filtered_documents {
                let document = documents[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.table.is_focused_row(index));
                    row.set_selected(state.table.selection.is_selected(index));
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, DocumentColumn::Ticker.index()),
                        );
                        let is_selected = state.table.selection.is_selected(index)
                            || selected.fund_id.as_deref() == Some(document.fund_id.as_str());
                        let response = ui
                            .selectable_label(is_selected, &document.ticker)
                            .on_hover_text(format!(
                                "Double-click to open in-app viewer\nfund_id: {}\nURL/path: {}",
                                document.fund_id,
                                document_mock_uri(&document)
                            ))
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            select_document_cell(state, index, &document, DocumentColumn::Ticker);
                            select_fund_or_listing(funds, &document, selected);
                        }
                        if response.double_clicked() {
                            select_document_cell(state, index, &document, DocumentColumn::Ticker);
                            select_fund_or_listing(funds, &document, selected);
                            action = Some(open_document_action(&document));
                        }
                        response.context_menu(|ui| {
                            if ui.button("Open Document").clicked() {
                                action = Some(open_document_action(&document));
                                ui.close();
                            }
                            if ui.button("Show Metadata").clicked() {
                                action = Some(DocumentsAction::Feedback(format!(
                                    "CMD: metadata {} {}",
                                    document.ticker, document.document_type
                                )));
                                ui.close();
                            }
                            if ui.button("Show Changes").clicked() {
                                action = Some(DocumentsAction::ShowChanges);
                                ui.close();
                            }
                            if ui.button("Open Related Fund").clicked() {
                                select_fund_or_listing(funds, &document, selected);
                                state.table.select(index);
                                action = Some(DocumentsAction::OpenRelatedFund {
                                    fund_id: document.fund_id.clone(),
                                });
                                ui.close();
                            }
                            if ui.button("Copy Title").clicked() {
                                let title =
                                    format!("{} {}", document.ticker, document.document_type);
                                ui.copy_text(title.clone());
                                action =
                                    Some(DocumentsAction::Feedback(format!("COPIED: {title}")));
                                ui.close();
                            }
                            if ui.button("Copy URL/path").clicked() {
                                let uri = document_mock_uri(&document);
                                ui.copy_text(uri.clone());
                                action = Some(DocumentsAction::Feedback(format!("COPIED: {uri}")));
                                ui.close();
                            }
                            if ui.button("Copy Hash").clicked() {
                                ui.copy_text(document.content_hash_change.clone());
                                action = Some(DocumentsAction::Feedback(format!(
                                    "COPIED: {}",
                                    document.content_hash_change
                                )));
                                ui.close();
                            }
                            if ui.button("Copy Metadata").clicked() {
                                let fund = funds.iter().find(|fund| fund.id == document.fund_id);
                                ui.copy_text(document_metadata_copy_text(&document, fund));
                                action = Some(DocumentsAction::Feedback(format!(
                                    "COPIED: metadata {}",
                                    document.ticker
                                )));
                                ui.close();
                            }
                            if ui.button("Copy Row").clicked() {
                                ui.copy_text(document_copy_text(&document));
                                action = Some(DocumentsAction::Feedback(
                                    "COPIED: document row".to_owned(),
                                ));
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, DocumentColumn::DocumentType.index()),
                        );
                        ui.label(&document.document_type);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, DocumentColumn::LatestDate.index()),
                        );
                        ui.label(fmt_date_str(&document.latest_date));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, DocumentColumn::Status.index()),
                        );
                        style::status_badge(ui, &document.status);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .table
                                .is_focused_cell(index, DocumentColumn::Source.index()),
                        );
                        style::source_badge(ui, &document.source);
                    });
                    row.col(|ui| {
                        ui.label(&document.content_hash_change);
                    });
                    row.col(|ui| {
                        ui.horizontal_wrapped(|ui| {
                            if crate::ui::actions::action_button(
                                ui,
                                "Open",
                                "Open in-app document viewer.",
                            )
                            .clicked()
                            {
                                state.table.select(index);
                                select_fund_or_listing(funds, &document, selected);
                                action = Some(open_document_action(&document));
                            }
                            if crate::ui::actions::action_button(
                                ui,
                                "Fund",
                                "Open related fund detail.",
                            )
                            .clicked()
                            {
                                state.table.select(index);
                                select_fund_or_listing(funds, &document, selected);
                                action = Some(DocumentsAction::OpenRelatedFund {
                                    fund_id: document.fund_id.clone(),
                                });
                            }
                            if crate::ui::actions::action_button(
                                ui,
                                "Copy",
                                "Copy document metadata.",
                            )
                            .clicked()
                            {
                                let fund = funds.iter().find(|fund| fund.id == document.fund_id);
                                ui.copy_text(document_metadata_copy_text(&document, fund));
                                state.table.select(index);
                                action = Some(DocumentsAction::Feedback(format!(
                                    "COPIED: metadata {}",
                                    document.ticker
                                )));
                            }
                        });
                    });
                    row.col(|ui| {
                        ui.label(fmt_date_str(&document.last_checked));
                    });
                });
            }
        });
    action
}

fn select_document_cell(
    state: &mut DocumentsState,
    index: usize,
    document: &DocumentSnapshot,
    column: DocumentColumn,
) {
    let (display, raw) = column.payload(document);
    state
        .table
        .select_cell(index, column.index(), column.key(), display, raw);
}

fn sync_document_focus(
    documents: &[DocumentSnapshot],
    funds: &[Fund],
    selected: &mut SelectedInstrument,
    state: &mut DocumentsState,
) {
    let (Some(row_index), Some(column_index)) = (
        state.table.focused_row_index,
        state.table.focused_column_index,
    ) else {
        return;
    };
    let Some(document) = documents.get(row_index) else {
        return;
    };
    let Some(column) = DocumentColumn::ALL.get(column_index).copied() else {
        return;
    };
    select_fund_or_listing(funds, document, selected);
    let (display, raw) = column.payload(document);
    state
        .table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn visible_document_indices(documents: &[DocumentSnapshot], state: &DocumentsState) -> Vec<usize> {
    let mut indices = documents
        .iter()
        .enumerate()
        .filter(|(_, document)| document_matches(document, &state.table.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_document_indices(documents, &mut indices, state.table.sort.as_ref());
    indices
}

fn sort_document_indices(
    documents: &[DocumentSnapshot],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };

    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_documents(
                &documents[*left],
                &documents[*right],
                &sort.column,
            ))
            .then_with(|| documents[*left].ticker.cmp(&documents[*right].ticker))
    });
}

fn compare_documents(left: &DocumentSnapshot, right: &DocumentSnapshot, column: &str) -> Ordering {
    match column {
        COL_TICKER => left.ticker.cmp(&right.ticker),
        COL_TYPE => left.document_type.cmp(&right.document_type),
        COL_DATE => left.latest_date.cmp(&right.latest_date),
        COL_STATUS => left.status.cmp(&right.status),
        COL_SOURCE => left.source.cmp(&right.source),
        COL_CHECKED => left.last_checked.cmp(&right.last_checked),
        _ => Ordering::Equal,
    }
}

fn document_matches(document: &DocumentSnapshot, filter: &str) -> bool {
    any_contains_ci(
        [
            document.ticker.as_str(),
            document.document_type.as_str(),
            document.latest_date.as_str(),
            document.status.as_str(),
            document.content_hash_change.as_str(),
            document.source.as_str(),
            document.last_checked.as_str(),
        ],
        filter,
    )
}

fn document_diff_panel(ui: &mut egui::Ui, documents: &[DocumentSnapshot], state: &DocumentsState) {
    ui.label(egui::RichText::new("Changes preview").strong());
    ui.label(egui::RichText::new("Mock document/entity changes. Spreads are separate.").weak());

    if let Some(document) = state
        .table
        .selected_index()
        .and_then(|index| documents.get(index))
    {
        ui.separator();
        ui.monospace(format!(
            "{} | {} | {}",
            document.ticker,
            document.document_type,
            fmt_status(&document.status)
        ));
        ui.monospace(format!("Hash: {}", document.content_hash_change));
    }

    let diffs = mock_entity_diffs();
    for diff in diffs.iter().take(2) {
        ui.separator();
        ui.monospace(format!(
            "{} | {} -> {}",
            diff.entity_name, diff.left_label, diff.right_label
        ));
        for field in &diff.fields {
            if field.status != DiffStatus::Unchanged {
                ui.horizontal_wrapped(|ui| {
                    ui.label(&field.field_name);
                    ui.monospace(field.left_value.as_deref().unwrap_or("-"));
                    ui.label(">");
                    ui.monospace(field.right_value.as_deref().unwrap_or("-"));
                });
            }
        }
    }
}

fn selected_document<'a>(
    documents: &'a [DocumentSnapshot],
    state: &DocumentsState,
) -> Option<&'a DocumentSnapshot> {
    state
        .table
        .selected_index()
        .and_then(|index| documents.get(index))
}

fn open_document_action(document: &DocumentSnapshot) -> DocumentsAction {
    DocumentsAction::OpenDocument {
        fund_id: document.fund_id.clone(),
        ticker: document.ticker.clone(),
        document_type: document.document_type.clone(),
    }
}

pub(crate) fn document_mock_uri(document: &DocumentSnapshot) -> String {
    format!(
        "mock://documents/{}/{}/{}",
        document.ticker, document.document_type, document.latest_date
    )
}

fn document_copy_text(document: &DocumentSnapshot) -> String {
    [
        document.ticker.clone(),
        document.document_type.clone(),
        document.latest_date.clone(),
        document.status.clone(),
        document_mock_uri(document),
        document.content_hash_change.clone(),
    ]
    .join("\t")
}

pub(crate) fn document_metadata_copy_text(
    document: &DocumentSnapshot,
    fund: Option<&Fund>,
) -> String {
    document_metadata_rows(document, fund)
        .into_iter()
        .map(|(field, value)| format!("{field}: {value}"))
        .collect::<Vec<_>>()
        .join("\n")
}

fn document_metadata_rows(
    document: &DocumentSnapshot,
    fund: Option<&Fund>,
) -> Vec<(&'static str, String)> {
    vec![
        ("Title/type", document.document_type.clone()),
        ("Fund/ticker", document.ticker.clone()),
        (
            "Fund name",
            fund.map(|fund| fund.name.clone())
                .unwrap_or_else(|| document.fund_id.clone()),
        ),
        (
            "Document date",
            fmt_date_str(&document.latest_date).to_owned(),
        ),
        ("Source", format_source(&document.source)),
        ("Status", fmt_status(&document.status)),
        ("URL/path", document_mock_uri(document)),
        ("Content hash/change", document.content_hash_change.clone()),
        (
            "Last checked",
            fmt_date_str(&document.last_checked).to_owned(),
        ),
    ]
}

fn select_fund_or_listing(
    funds: &[Fund],
    document: &DocumentSnapshot,
    selected: &mut SelectedInstrument,
) {
    let Some(fund) = funds.iter().find(|fund| fund.id == document.fund_id) else {
        selected.select_fund(document.fund_id.clone());
        return;
    };

    if let Some(listing) = fund
        .listings
        .iter()
        .find(|listing| listing.ticker == document.ticker)
        .or_else(|| fund.listings.first())
    {
        selected.select_listing(fund.id.clone(), listing.id.clone());
    } else {
        selected.select_fund(fund.id.clone());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn document(ticker: &str, latest_date: &str, source: &str) -> DocumentSnapshot {
        DocumentSnapshot {
            fund_id: format!("fund-{ticker}"),
            ticker: ticker.to_owned(),
            document_type: "factsheet".to_owned(),
            latest_date: latest_date.to_owned(),
            status: "Current".to_owned(),
            content_hash_change: "unchanged".to_owned(),
            source: source.to_owned(),
            last_checked: latest_date.to_owned(),
        }
    }

    #[test]
    fn sorts_documents_by_date_desc() {
        let documents = vec![
            document("VUSA", "2026-05-31", "issuer"),
            document("ISF", "2026-06-20", "issuer"),
        ];
        let mut state = DocumentsState::default();
        state.table.toggle_sort(COL_DATE);
        state.table.toggle_sort(COL_DATE);

        let indices = visible_document_indices(&documents, &state);

        assert_eq!(indices, vec![1, 0]);
    }

    #[test]
    fn document_viewer_uri_is_stable() {
        let uri = document_mock_uri(&document("VUSA", "2026-05-31", "issuer"));

        assert_eq!(uri, "mock://documents/VUSA/factsheet/2026-05-31");
    }

    #[test]
    fn document_columns_have_stable_managed_layout_keys() {
        assert_eq!(DocumentColumn::ALL.len(), 5);
        assert_eq!(DOCUMENT_COLUMNS.len(), 8);
        assert_eq!(DocumentColumn::DocumentType.label(), "Document type");
        assert_eq!(DocumentColumn::Source.key(), COL_SOURCE);
        assert_eq!(DocumentColumn::Source.index(), 4);
        assert!(!DOCUMENT_COLUMNS[5].default_visible);
        assert!(!DOCUMENT_COLUMNS[7].default_visible);
    }
}
