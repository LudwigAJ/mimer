use crate::api::client::DataProvider;
use crate::app_model::SelectedInstrument;
use crate::domain::{
    InstrumentCandidate, InstrumentResolutionRequest, InstrumentResolutionResult, JobRun,
    SymbolType,
};
use crate::pages::{format_source, header_cell, page_heading};
use eframe::egui;
use egui_extras::{Column, TableBuilder};

#[derive(Clone, Debug)]
pub struct AddInstrumentState {
    pub symbol: String,
    pub symbol_type: SymbolType,
    pub exchange: String,
    pub currency: String,
    pub result: Option<InstrumentResolutionResult>,
}

impl Default for AddInstrumentState {
    fn default() -> Self {
        Self {
            symbol: String::new(),
            symbol_type: SymbolType::Ticker,
            exchange: String::new(),
            currency: String::new(),
            result: None,
        }
    }
}

impl AddInstrumentState {
    fn request(&self) -> InstrumentResolutionRequest {
        InstrumentResolutionRequest {
            symbol: self.symbol.trim().to_owned(),
            symbol_type: self.symbol_type,
            exchange: optional_text(&self.exchange),
            currency: optional_text(&self.currency),
        }
    }
}

pub fn render(
    ui: &mut egui::Ui,
    state: &mut AddInstrumentState,
    provider: &dyn DataProvider,
    selected: &mut SelectedInstrument,
) -> bool {
    let mut navigated_to_selection = false;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Add Instrument");
            ui.label("Mock resolver examples: VUSA resolves, JEPG/JEGP is ambiguous, BACKFILL queues mock jobs, unknown symbols return not found.");
            ui.add_space(6.0);

            egui::Grid::new("add_instrument_form_grid")
                .num_columns(2)
                .striped(true)
                .show(ui, |ui| {
                    ui.label("Symbol");
                    ui.text_edit_singleline(&mut state.symbol);
                    ui.end_row();

                    ui.label("Symbol type");
                    egui::ComboBox::from_id_salt("symbol_type_select")
                        .selected_text(state.symbol_type.as_str())
                        .show_ui(ui, |ui| {
                            for symbol_type in SymbolType::ALL {
                                ui.selectable_value(
                                    &mut state.symbol_type,
                                    symbol_type,
                                    symbol_type.as_str(),
                                );
                            }
                        });
                    ui.end_row();

                    ui.label("Exchange (optional)");
                    ui.text_edit_singleline(&mut state.exchange);
                    ui.end_row();

                    ui.label("Currency (optional)");
                    ui.text_edit_singleline(&mut state.currency);
                    ui.end_row();
                });

            ui.horizontal(|ui| {
                if ui.button("Resolve / Add").clicked() {
                    let request = state.request();
                    state.result = Some(provider.resolve_instrument(&request));
                }

                if ui.button("Clear").clicked() {
                    *state = AddInstrumentState::default();
                }
            });

            ui.add_space(8.0);
            if let Some(result) = &state.result {
                navigated_to_selection = result_panel(ui, result, selected);
            } else {
                ui.label(egui::RichText::new("No resolution attempted.").weak());
            }
        });
    navigated_to_selection
}

fn result_panel(
    ui: &mut egui::Ui,
    result: &InstrumentResolutionResult,
    selected: &mut SelectedInstrument,
) -> bool {
    let mut navigated_to_selection = false;
    ui.separator();
    ui.label(egui::RichText::new("Resolution result").strong());
    ui.horizontal(|ui| {
        ui.label("Status:");
        ui.monospace(result.status_label());
    });

    match result {
        InstrumentResolutionResult::Resolved {
            fund,
            listing,
            queued_jobs,
        } => {
            if ui.button("Select resolved listing").clicked() {
                selected.select_listing(fund.id.clone(), listing.id.clone());
                navigated_to_selection = true;
            }
            egui::Grid::new("resolved_instrument_grid")
                .num_columns(2)
                .striped(true)
                .show(ui, |ui| {
                    ui.label("Fund");
                    ui.label(&fund.name);
                    ui.end_row();
                    ui.label("Listing");
                    ui.monospace(format!(
                        "{} {} {}",
                        listing.ticker, listing.exchange, listing.currency
                    ));
                    ui.end_row();
                    ui.label("Listing ID");
                    ui.monospace(&listing.id);
                    ui.end_row();
                    ui.label("Fund ID");
                    ui.monospace(&listing.fund_id);
                    ui.end_row();
                    ui.label("Venue");
                    ui.label(&listing.venue_name);
                    ui.end_row();
                    ui.label("ISIN");
                    ui.monospace(&fund.isin);
                    ui.end_row();
                    ui.label("Provider");
                    ui.label(&fund.provider);
                    ui.end_row();
                    ui.label("Source");
                    ui.monospace(format_source(&listing.source));
                    ui.end_row();
                });
            if !queued_jobs.is_empty() {
                ui.add_space(6.0);
                job_runs_table(ui, "resolved_jobs_table", queued_jobs);
            }
        }
        InstrumentResolutionResult::Ambiguous { candidates } => {
            ui.label("Multiple listings or candidates matched the request.");
            if candidates_table(ui, candidates, selected) {
                navigated_to_selection = true;
            }
        }
        InstrumentResolutionResult::NotFound { message } => {
            ui.label(message);
        }
        InstrumentResolutionResult::PendingBackfill {
            fund,
            listing,
            jobs,
        } => {
            if let Some(fund) = fund {
                ui.label(format!("Resolved provisional fund: {}", fund.name));
            }
            if let Some(listing) = listing {
                ui.label(format!(
                    "Provisional listing: {} {} {} ({})",
                    listing.ticker, listing.exchange, listing.currency, listing.venue_name
                ));
                if let Some(fund) = fund
                    && ui.button("Select provisional listing").clicked()
                {
                    selected.select_listing(fund.id.clone(), listing.id.clone());
                    navigated_to_selection = true;
                }
            }
            job_runs_table(ui, "pending_backfill_jobs_table", jobs);
        }
    }
    navigated_to_selection
}

fn candidates_table(
    ui: &mut egui::Ui,
    candidates: &[InstrumentCandidate],
    selected: &mut SelectedInstrument,
) -> bool {
    let mut navigated_to_selection = false;
    TableBuilder::new(ui)
        .id_salt("resolution_candidates_table")
        .striped(true)
        .resizable(true)
        .max_scroll_height(180.0)
        .column(Column::initial(82.0).at_least(68.0))
        .column(Column::initial(300.0).at_least(200.0).clip(true))
        .column(Column::initial(92.0).at_least(72.0))
        .column(Column::initial(72.0).at_least(58.0))
        .column(Column::initial(118.0).at_least(96.0))
        .column(Column::initial(142.0).at_least(110.0).clip(true))
        .column(Column::initial(102.0).at_least(82.0))
        .column(Column::remainder().at_least(180.0).clip(true))
        .header(18.0, |mut header| {
            for label in [
                "Ticker", "Fund", "Exchange", "Ccy", "ISIN", "Venue", "Source", "Reason",
            ] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for candidate in candidates {
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        if ui
                            .selectable_label(false, &candidate.listing.ticker)
                            .clicked()
                        {
                            selected.select_listing(
                                candidate.fund.id.clone(),
                                candidate.listing.id.clone(),
                            );
                            navigated_to_selection = true;
                        }
                    });
                    row.col(|ui| {
                        ui.label(&candidate.fund.name);
                    });
                    row.col(|ui| {
                        ui.label(&candidate.listing.exchange);
                    });
                    row.col(|ui| {
                        ui.label(&candidate.listing.currency);
                    });
                    row.col(|ui| {
                        ui.monospace(&candidate.fund.isin);
                    });
                    row.col(|ui| {
                        ui.label(&candidate.listing.venue_name)
                            .on_hover_text(format!(
                                "listing_id: {}\nfund_id: {}",
                                candidate.listing.id, candidate.listing.fund_id
                            ));
                    });
                    row.col(|ui| {
                        ui.monospace(format_source(&candidate.listing.source));
                    });
                    row.col(|ui| {
                        ui.label(&candidate.match_reason);
                    });
                });
            }
        });
    navigated_to_selection
}

fn job_runs_table(ui: &mut egui::Ui, id: &str, jobs: &[JobRun]) {
    ui.label(egui::RichText::new("Queued job runs").strong());
    TableBuilder::new(ui)
        .id_salt(id)
        .striped(true)
        .resizable(true)
        .max_scroll_height(160.0)
        .column(Column::initial(160.0).at_least(120.0).clip(true))
        .column(Column::initial(90.0).at_least(72.0))
        .column(Column::initial(100.0).at_least(78.0))
        .column(Column::initial(120.0).at_least(100.0))
        .column(Column::initial(112.0).at_least(90.0))
        .column(Column::remainder().at_least(180.0).clip(true))
        .header(18.0, |mut header| {
            for label in ["Run ID", "Type", "Status", "Started", "Source", "Message"] {
                header.col(|ui| header_cell(ui, label));
            }
        })
        .body(|mut body| {
            for job in jobs {
                body.row(20.0, |mut row| {
                    row.col(|ui| {
                        ui.monospace(&job.id);
                    });
                    row.col(|ui| {
                        ui.label(&job.job_type);
                    });
                    row.col(|ui| {
                        ui.label(job.status.as_str());
                    });
                    row.col(|ui| {
                        ui.label(&job.started);
                    });
                    row.col(|ui| {
                        ui.monospace(format_source(&job.source));
                    });
                    row.col(|ui| {
                        ui.label(&job.message);
                    });
                });
            }
        });
}

fn optional_text(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_ascii_uppercase())
    }
}
