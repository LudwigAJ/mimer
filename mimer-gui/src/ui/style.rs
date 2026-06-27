#![allow(dead_code)]

use crate::domain::{AlertSeverity, DataFreshness, JobStatus};
use crate::format::{fmt_source, fmt_status};
use crate::source::{DataKind, SourceAvailability, SourceResolutionStatus};
use eframe::egui::{self, Color32, Response, RichText, Stroke};

pub const BADGE_TEXT_SIZE: f32 = 10.0;
pub const BADGE_INNER_MARGIN_X: f32 = 4.0;
pub const BADGE_INNER_MARGIN_Y: f32 = 1.0;
pub const BADGE_CORNER_RADIUS: u8 = 2;
pub const SECTION_LABEL_SIZE: f32 = 10.0;
pub const COMPACT_GRID_X: f32 = 10.0;
pub const COMPACT_GRID_Y: f32 = 4.0;
pub const TOOLTIP_DELAY_SECONDS: f32 = 0.85;
pub const PAGE_TITLE_SIZE: f32 = 15.0;
pub const PAGE_CONTEXT_SIZE: f32 = 11.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BadgeTone {
    Good,
    Warning,
    Danger,
    Info,
    Neutral,
    Source,
    Manual,
    Derived,
    Mock,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BadgePalette {
    pub text: Color32,
    pub fill: Color32,
    pub stroke: Color32,
}

impl BadgeTone {
    pub fn palette(self) -> BadgePalette {
        match self {
            Self::Good => BadgePalette::new((92, 190, 120), (22, 54, 34), (48, 116, 70)),
            Self::Warning => BadgePalette::new((230, 184, 80), (64, 48, 18), (126, 92, 28)),
            Self::Danger => BadgePalette::new((236, 112, 96), (70, 28, 24), (138, 54, 44)),
            Self::Info => BadgePalette::new((112, 168, 226), (24, 44, 68), (48, 88, 132)),
            Self::Neutral => BadgePalette::new((170, 176, 186), (42, 44, 48), (82, 86, 94)),
            Self::Source => BadgePalette::new((116, 178, 230), (18, 42, 64), (42, 86, 126)),
            Self::Manual => BadgePalette::new((192, 148, 234), (54, 32, 74), (102, 66, 140)),
            Self::Derived => BadgePalette::new((130, 206, 190), (20, 58, 54), (44, 116, 106)),
            Self::Mock => BadgePalette::new((226, 204, 92), (62, 54, 20), (126, 108, 34)),
        }
    }
}

impl BadgePalette {
    const fn new(text: (u8, u8, u8), fill: (u8, u8, u8), stroke: (u8, u8, u8)) -> Self {
        Self {
            text: Color32::from_rgb(text.0, text.1, text.2),
            fill: Color32::from_rgb(fill.0, fill.1, fill.2),
            stroke: Color32::from_rgb(stroke.0, stroke.1, stroke.2),
        }
    }
}

pub fn compact_item_spacing() -> egui::Vec2 {
    egui::vec2(6.0, 3.0)
}

pub fn comfortable_item_spacing() -> egui::Vec2 {
    egui::vec2(8.0, 5.0)
}

pub fn compact_button_padding() -> egui::Vec2 {
    egui::vec2(5.0, 1.0)
}

pub fn comfortable_button_padding() -> egui::Vec2 {
    egui::vec2(7.0, 3.0)
}

pub fn compact_interact_size() -> egui::Vec2 {
    egui::vec2(40.0, 18.0)
}

pub fn comfortable_interact_size() -> egui::Vec2 {
    egui::vec2(44.0, 22.0)
}

pub fn shell_bar_frame(style: &egui::Style) -> egui::Frame {
    egui::Frame::new()
        .fill(style.visuals.panel_fill)
        .stroke(Stroke::new(
            1.0,
            style.visuals.widgets.noninteractive.bg_stroke.color,
        ))
        .inner_margin(egui::vec2(6.0, 2.0))
}

pub fn context_strip_frame(style: &egui::Style) -> egui::Frame {
    egui::Frame::new()
        .fill(style.visuals.faint_bg_color)
        .stroke(Stroke::new(
            1.0,
            style.visuals.widgets.noninteractive.bg_stroke.color,
        ))
        .inner_margin(egui::vec2(7.0, 2.0))
}

pub fn status_bar_frame(style: &egui::Style) -> egui::Frame {
    egui::Frame::new()
        .fill(style.visuals.panel_fill)
        .stroke(Stroke::new(
            1.0,
            style.visuals.widgets.noninteractive.bg_stroke.color,
        ))
        .inner_margin(egui::vec2(7.0, 1.0))
}

pub fn side_panel_frame(style: &egui::Style) -> egui::Frame {
    egui::Frame::new()
        .fill(style.visuals.panel_fill)
        .inner_margin(egui::vec2(6.0, 6.0))
}

pub fn content_frame(style: &egui::Style) -> egui::Frame {
    egui::Frame::new()
        .fill(style.visuals.extreme_bg_color)
        .inner_margin(crate::ui::metrics::PAGE_CONTENT_MARGIN)
}

pub fn section_header(ui: &mut egui::Ui, label: &str) {
    ui.label(
        RichText::new(label.to_ascii_uppercase())
            .weak()
            .size(SECTION_LABEL_SIZE),
    );
}

pub fn section_heading(ui: &mut egui::Ui, label: &str, detail: Option<&str>) {
    ui.horizontal_wrapped(|ui| {
        ui.label(
            RichText::new(label)
                .strong()
                .size(PAGE_CONTEXT_SIZE)
                .color(ui.visuals().strong_text_color()),
        );
        if let Some(detail) = detail {
            ui.label(RichText::new(detail).weak().size(PAGE_CONTEXT_SIZE));
        }
    });
}

pub fn page_header(
    ui: &mut egui::Ui,
    title: &str,
    context: Option<&str>,
    subtitle: Option<&str>,
    trailing: impl FnOnce(&mut egui::Ui),
) {
    egui::Frame::new()
        .fill(ui.visuals().faint_bg_color)
        .stroke(Stroke::new(
            1.0,
            ui.visuals().widgets.noninteractive.bg_stroke.color,
        ))
        .inner_margin(egui::vec2(8.0, 5.0))
        .show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.label(
                    RichText::new(page_header_display_title(title, context))
                        .strong()
                        .size(PAGE_TITLE_SIZE),
                );
                trailing(ui);
            });
            if let Some(subtitle) = subtitle.filter(|subtitle| !subtitle.trim().is_empty()) {
                ui.label(RichText::new(subtitle).weak().size(PAGE_CONTEXT_SIZE));
            }
        });
}

pub fn page_header_display_title(title: &str, context: Option<&str>) -> String {
    match context.filter(|context| !context.trim().is_empty()) {
        Some(context) => format!("{title}  ·  {context}"),
        None => title.to_owned(),
    }
}

pub fn state_message(ui: &mut egui::Ui, status: &str, message: &str) {
    let palette = status_tone(status).palette();
    egui::Frame::new()
        .fill(palette.fill)
        .stroke(Stroke::new(1.0, palette.stroke))
        .inner_margin(egui::vec2(8.0, 6.0))
        .show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                status_badge(ui, status);
                ui.label(message);
            });
        });
}

pub fn left_rail_item(
    ui: &mut egui::Ui,
    selected: bool,
    label: &str,
    count: Option<usize>,
) -> Response {
    let text = match count {
        Some(count) => format!("{label}  {count}"),
        None => label.to_owned(),
    };
    ui.add_sized(
        [ui.available_width(), 22.0],
        egui::Button::selectable(selected, RichText::new(text).size(11.0)),
    )
    .on_hover_cursor(egui::CursorIcon::PointingHand)
}

pub fn table_header_text(text: &str) -> RichText {
    RichText::new(text).strong().weak().size(10.5)
}

pub fn numeric_label(ui: &mut egui::Ui, text: impl Into<String>) -> Response {
    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
        ui.label(RichText::new(text.into()).monospace())
    })
    .inner
}

pub fn focused_table_cell(ui: &mut egui::Ui, focused: bool) {
    if !focused {
        return;
    }
    let rect = ui.max_rect().shrink(1.0);
    let visuals = ui.visuals();
    ui.painter()
        .rect_filled(rect, 1.0, visuals.selection.bg_fill.gamma_multiply(0.55));
    ui.painter().rect_stroke(
        rect,
        1.0,
        Stroke::new(1.0, visuals.selection.stroke.color),
        egui::StrokeKind::Inside,
    );
}

pub fn compact_metadata_row(ui: &mut egui::Ui, label: &str, value: impl Into<String>) {
    ui.label(RichText::new(label).weak());
    ui.monospace(value.into());
    ui.end_row();
}

pub fn badge(
    ui: &mut egui::Ui,
    label: impl Into<String>,
    tone: BadgeTone,
    tooltip: impl Into<Option<String>>,
) -> Response {
    let palette = tone.palette();
    let response = egui::Frame::new()
        .fill(palette.fill)
        .stroke(Stroke::new(1.0, palette.stroke))
        .corner_radius(BADGE_CORNER_RADIUS)
        .inner_margin(egui::vec2(BADGE_INNER_MARGIN_X, BADGE_INNER_MARGIN_Y))
        .show(ui, |ui| {
            ui.label(
                RichText::new(label.into())
                    .monospace()
                    .size(BADGE_TEXT_SIZE)
                    .color(palette.text),
            );
        })
        .response;

    if let Some(tooltip) = tooltip.into() {
        response.on_hover_text(tooltip)
    } else {
        response
    }
}

pub fn status_badge(ui: &mut egui::Ui, status: &str) -> Response {
    let label = normalized_status_label(status);
    let tooltip = status_tooltip(&label);
    badge(ui, label, status_tone(status), Some(tooltip))
}

pub fn source_badge(ui: &mut egui::Ui, source: &str) -> Response {
    let label = fmt_source(source);
    badge(
        ui,
        label.clone(),
        BadgeTone::Source,
        Some(format!("Source: {}", source_label(source))),
    )
}

pub fn source_resolution_badge(
    ui: &mut egui::Ui,
    resolution: &SourceAvailability,
    kind: DataKind,
) -> Response {
    let tone = match resolution.status {
        SourceResolutionStatus::Selected => BadgeTone::Source,
        SourceResolutionStatus::Fallback => BadgeTone::Warning,
        SourceResolutionStatus::Missing => BadgeTone::Danger,
    };
    badge(
        ui,
        resolution.effective_label(),
        tone,
        Some(resolution.tooltip(kind)),
    )
}

pub fn freshness_badge(ui: &mut egui::Ui, freshness: DataFreshness) -> Response {
    badge(
        ui,
        freshness.as_str(),
        freshness_tone(freshness),
        Some(freshness_tooltip(freshness).to_owned()),
    )
}

pub fn alert_severity_badge(ui: &mut egui::Ui, severity: AlertSeverity) -> Response {
    badge(
        ui,
        severity.as_str().to_ascii_uppercase(),
        alert_severity_tone(severity),
        Some(format!("Alert severity: {}", severity.as_str())),
    )
}

pub fn job_status_badge(ui: &mut egui::Ui, status: JobStatus) -> Response {
    badge(
        ui,
        status.as_str(),
        job_status_tone(status),
        Some(format!("Job status: {}", job_status_tooltip(status))),
    )
}

pub fn manual_badge(ui: &mut egui::Ui) -> Response {
    badge(
        ui,
        "MANUAL",
        BadgeTone::Manual,
        Some("Manual override: this value is local and survives refresh until cleared.".to_owned()),
    )
}

pub fn derived_badge(ui: &mut egui::Ui) -> Response {
    badge(
        ui,
        "DERIVED",
        BadgeTone::Derived,
        Some("Derived: recomputed from effective inputs, including manual overrides.".to_owned()),
    )
}

pub fn estimated_badge(ui: &mut egui::Ui) -> Response {
    badge(
        ui,
        "EST",
        BadgeTone::Info,
        Some("Estimated: computed from current assumptions or incomplete source data.".to_owned()),
    )
}

pub fn mock_badge(ui: &mut egui::Ui) -> Response {
    badge(
        ui,
        "MOCK",
        BadgeTone::Mock,
        Some("Mock/local data: no backend request was made.".to_owned()),
    )
}

pub fn warning_label(ui: &mut egui::Ui, text: &str) -> Response {
    ui.colored_label(BadgeTone::Warning.palette().text, text)
}

pub fn error_label(ui: &mut egui::Ui, text: &str) -> Response {
    ui.colored_label(BadgeTone::Danger.palette().text, text)
}

pub fn info_label(ui: &mut egui::Ui, text: &str) -> Response {
    ui.colored_label(BadgeTone::Info.palette().text, text)
}

pub fn normalized_status_label(status: &str) -> String {
    let normalized = fmt_status(status);
    if normalized.is_empty() {
        "-".to_owned()
    } else {
        normalized
    }
}

pub fn status_tone(status: &str) -> BadgeTone {
    match normalized_status_label(status).as_str() {
        "FRESH" | "CURRENT" | "DONE" | "SUCCESS" | "SUCCEEDED" | "PAID" | "OK" | "READY" => {
            BadgeTone::Good
        }
        "STALE" | "STALE API" | "PENDING" | "QUEUED" | "BACKFILL" | "BACKFILL_PENDING"
        | "PARTIAL" | "PARTIAL API" | "PARTIAL_SUCCESS" | "ESTIMATED" | "NEEDED" => {
            BadgeTone::Warning
        }
        "FAILED" | "ERROR" | "MISSING" | "CONFLICT" | "AMBIG" | "CRITICAL" | "BLOCKED"
        | "BUDGET_BLOCKED" | "API ERROR" => BadgeTone::Danger,
        "RUNNING" | "LOADING" | "API" | "INFO" | "OPEN" | "ACTIVE" | "READ" | "PLANNED"
        | "IDLE" | "DISABLED" => BadgeTone::Info,
        "EMPTY" => BadgeTone::Neutral,
        "MOCK" | "SEED" | "FIXTURE" => BadgeTone::Mock,
        "MANUAL" => BadgeTone::Manual,
        "DERIVED" => BadgeTone::Derived,
        _ => BadgeTone::Neutral,
    }
}

pub fn freshness_tone(freshness: DataFreshness) -> BadgeTone {
    match freshness {
        DataFreshness::Fresh => BadgeTone::Good,
        DataFreshness::Stale => BadgeTone::Warning,
        DataFreshness::BackfillPending => BadgeTone::Info,
    }
}

pub fn alert_severity_tone(severity: AlertSeverity) -> BadgeTone {
    match severity {
        AlertSeverity::Info => BadgeTone::Info,
        AlertSeverity::Warning => BadgeTone::Warning,
        AlertSeverity::Critical => BadgeTone::Danger,
    }
}

pub fn job_status_tone(status: JobStatus) -> BadgeTone {
    match status {
        JobStatus::Queued => BadgeTone::Info,
        JobStatus::Running => BadgeTone::Warning,
        JobStatus::Succeeded => BadgeTone::Good,
        JobStatus::Failed => BadgeTone::Danger,
        JobStatus::Unknown => BadgeTone::Neutral,
    }
}

fn source_label(source: &str) -> String {
    let trimmed = source.trim();
    if trimmed.is_empty() {
        "-".to_owned()
    } else {
        trimmed.to_ascii_lowercase()
    }
}

fn status_tooltip(label: &str) -> String {
    match label {
        "FRESH" | "CURRENT" => "Status: fresh/current enough for display.".to_owned(),
        "STALE" | "STALE API" => "Status: stale; refresh or inspect source fallback.".to_owned(),
        "PARTIAL" | "PARTIAL API" => {
            "Status: partial; some sections succeeded and others retained fallback data.".to_owned()
        }
        "API ERROR" => "Status: API failed; fallback or previous data remains visible.".to_owned(),
        "API" => "Status: hydrated from the configured REST API.".to_owned(),
        "LOADING" => "Status: loading on a background worker.".to_owned(),
        "EMPTY" => "Status: no rows match the current view or filter.".to_owned(),
        "MISSING" => "Status: missing expected data.".to_owned(),
        "BLOCKED" | "BUDGET_BLOCKED" => {
            "Status: blocked; inspect source budget, fetch logs, or related job.".to_owned()
        }
        "FAILED" | "ERROR" => "Status: failed; inspect source or related job.".to_owned(),
        "READY" | "OK" => "Status: ready for the current workflow.".to_owned(),
        "MANUAL" => {
            "Manual override: this value is local and survives refresh until cleared.".to_owned()
        }
        "DERIVED" => "Derived: recomputed from effective inputs.".to_owned(),
        "EST" | "ESTIMATED" => {
            "Estimated: computed from assumptions or incomplete data.".to_owned()
        }
        "MOCK" | "SEED" | "FIXTURE" => {
            "Mock/fixture data from local development sources.".to_owned()
        }
        "QUEUED" | "RUNNING" | "PENDING" | "NEEDED" | "PLANNED" => {
            "Operational state: work is not complete yet.".to_owned()
        }
        _ => format!("Status: {label}"),
    }
}

fn freshness_tooltip(freshness: DataFreshness) -> &'static str {
    match freshness {
        DataFreshness::Fresh => "Status: fresh/current enough for display.",
        DataFreshness::Stale => "Status: stale; refresh or inspect source fallback.",
        DataFreshness::BackfillPending => "Status: pending backfill.",
    }
}

fn job_status_tooltip(status: JobStatus) -> &'static str {
    match status {
        JobStatus::Queued => "queued",
        JobStatus::Running => "running",
        JobStatus::Succeeded => "completed successfully",
        JobStatus::Failed => "failed",
        JobStatus::Unknown => "unknown backend status",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_common_statuses_to_badge_tones() {
        assert_eq!(status_tone("fresh"), BadgeTone::Good);
        assert_eq!(status_tone("stale"), BadgeTone::Warning);
        assert_eq!(status_tone("failed"), BadgeTone::Danger);
        assert_eq!(status_tone("manual"), BadgeTone::Manual);
        assert_eq!(status_tone("derived"), BadgeTone::Derived);
        assert_eq!(status_tone("mock"), BadgeTone::Mock);
        assert_eq!(status_tone("partial api"), BadgeTone::Warning);
        assert_eq!(status_tone("api error"), BadgeTone::Danger);
        assert_eq!(status_tone("loading"), BadgeTone::Info);
        assert_eq!(status_tone("empty"), BadgeTone::Neutral);
    }

    #[test]
    fn maps_domain_statuses_to_badge_tones() {
        assert_eq!(freshness_tone(DataFreshness::Fresh), BadgeTone::Good);
        assert_eq!(freshness_tone(DataFreshness::Stale), BadgeTone::Warning);
        assert_eq!(job_status_tone(JobStatus::Failed), BadgeTone::Danger);
        assert_eq!(
            alert_severity_tone(AlertSeverity::Critical),
            BadgeTone::Danger
        );
    }

    #[test]
    fn normalizes_blank_status_labels() {
        assert_eq!(normalized_status_label(" stale "), "STALE");
        assert_eq!(normalized_status_label(""), "-");
    }

    #[test]
    fn page_header_labels_include_optional_context() {
        assert_eq!(
            page_header_display_title("Charts", Some("Main Portfolio")),
            "Charts  ·  Main Portfolio"
        );
        assert_eq!(page_header_display_title("Settings", None), "Settings");
    }
}
