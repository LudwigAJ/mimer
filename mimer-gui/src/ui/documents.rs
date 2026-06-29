use crate::domain::{DocumentSnapshot, Fund};
use crate::format::{fmt_date_str, fmt_source, fmt_status};
use crate::ui::grid_helpers::{KvRow, kv_grid};
use crate::ui::style;
use eframe::egui;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DocumentPreviewAction {
    OpenViewer,
    OpenRelatedFund,
    PinInspector,
    Feedback(String),
}

pub fn document_uri(document: &DocumentSnapshot) -> String {
    format!(
        "mock://documents/{}/{}/{}",
        document.ticker, document.document_type, document.latest_date
    )
}

pub fn metadata_rows(document: &DocumentSnapshot, fund: Option<&Fund>) -> Vec<KvRow> {
    vec![
        KvRow::new(
            "Title",
            format!("{} {}", document.ticker, document.document_type),
        ),
        KvRow::new("Document type", document.document_type.clone()),
        KvRow::new("Subject", document_subject(document, fund)),
        KvRow::new(
            "Issuer / provider",
            fund.map(|fund| fund.provider.clone())
                .unwrap_or_else(|| "-".to_owned()),
        ),
        KvRow::new("Source", fmt_source(&document.source)).with_tooltip(&document.source),
        KvRow::new("Status / freshness", fmt_status(&document.status))
            .with_tooltip(&document.status),
        KvRow::new("Publication / as-of", fmt_date_str(&document.latest_date)),
        KvRow::new("Last checked", fmt_date_str(&document.last_checked)),
        KvRow::new("URL / path", document_uri(document)).copyable(),
        KvRow::new("Raw fund id", document.fund_id.clone()).copyable(),
        KvRow::new(
            "Content hash / change",
            document.content_hash_change.clone(),
        )
        .copyable(),
    ]
}

pub fn metadata_copy_text(document: &DocumentSnapshot, fund: Option<&Fund>) -> String {
    metadata_rows(document, fund)
        .into_iter()
        .map(|row| format!("{}: {}", row.label, row.full_value))
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn render_preview(
    ui: &mut egui::Ui,
    document: Option<&DocumentSnapshot>,
    fund: Option<&Fund>,
    pinned: bool,
) -> Option<DocumentPreviewAction> {
    let Some(document) = document else {
        style::state_message(
            ui,
            "PREVIEW",
            "Focus or select a document row to inspect its metadata.",
        );
        return None;
    };

    let mut action = None;
    egui::ScrollArea::vertical()
        .id_salt("document_metadata_preview_scroll")
        .auto_shrink(false)
        .show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.heading(format!("{} {}", document.ticker, document.document_type));
                if pinned {
                    style::status_badge(ui, "PINNED");
                }
            });
            ui.label(egui::RichText::new(document_subject(document, fund)).weak());
            ui.horizontal_wrapped(|ui| {
                style::status_badge(ui, &document.status);
                style::source_badge(ui, &document.source);
                ui.monospace(fmt_date_str(&document.latest_date));
            });
            ui.add_space(8.0);

            ui.horizontal_wrapped(|ui| {
                if ui.button("Open viewer").clicked() {
                    action = Some(DocumentPreviewAction::OpenViewer);
                }
                if ui.button("Open fund").clicked() {
                    action = Some(DocumentPreviewAction::OpenRelatedFund);
                }
                if ui.button("Copy URL/path").clicked() {
                    let uri = document_uri(document);
                    ui.copy_text(uri.clone());
                    action = Some(DocumentPreviewAction::Feedback(format!("COPIED: {uri}")));
                }
                if ui.button("Copy metadata").clicked() {
                    ui.copy_text(metadata_copy_text(document, fund));
                    action = Some(DocumentPreviewAction::Feedback(format!(
                        "COPIED: metadata {}",
                        document.ticker
                    )));
                }
                if ui
                    .add_enabled(!pinned, egui::Button::new("Pin inspector"))
                    .clicked()
                {
                    action = Some(DocumentPreviewAction::PinInspector);
                }
            });

            ui.separator();
            kv_grid(
                ui,
                ("document_metadata_preview", &document.fund_id, &document.document_type),
                &metadata_rows(document, fund),
            );

            ui.add_space(8.0);
            ui.group(|ui| {
                ui.set_min_height(110.0);
                ui.set_min_width(ui.available_width());
                ui.centered_and_justified(|ui| {
                    ui.vertical_centered(|ui| {
                        ui.monospace("Metadata preview");
                        ui.label(
                            "Full PDF rendering is intentionally deferred; URL, provenance, and change metadata remain available.",
                        );
                    });
                });
            });
        });
    action
}

fn document_subject(document: &DocumentSnapshot, fund: Option<&Fund>) -> String {
    fund.map(|fund| format!("{} · {}", fund.name, document.ticker))
        .unwrap_or_else(|| format!("{} · {}", document.fund_id, document.ticker))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn document() -> DocumentSnapshot {
        DocumentSnapshot {
            fund_id: "fund-vusa".to_owned(),
            ticker: "VUSA".to_owned(),
            document_type: "factsheet".to_owned(),
            latest_date: "2026-06-20".to_owned(),
            status: "Current".to_owned(),
            content_hash_change: "unchanged".to_owned(),
            source: "issuer".to_owned(),
            last_checked: "2026-06-21".to_owned(),
        }
    }

    #[test]
    fn metadata_copy_includes_provenance_and_raw_identifier() {
        let text = metadata_copy_text(&document(), None);

        assert!(text.contains("Source: SRC: issuer"));
        assert!(text.contains("Status / freshness: CURRENT"));
        assert!(text.contains("Raw fund id: fund-vusa"));
        assert!(text.contains("URL / path: mock://documents/VUSA/factsheet/2026-06-20"));
    }
}
