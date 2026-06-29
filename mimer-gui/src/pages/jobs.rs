use crate::domain::{JobRun, JobStatus, ScheduledJob};
use crate::filter::any_contains_ci;
use crate::pages::{bool_text, format_number, header_cell, sortable_header_cell};
use crate::table_state::{ColumnDescriptor, SortSpec, TableId, TableLayoutRegistry, TableState};
use crate::ui::table_layout::{managed_column, managed_table_revision, table_layout_controls};
use crate::ui::{metrics, style};
use eframe::egui;
use egui_extras::TableBuilder;
use std::cmp::Ordering;

const COL_NAME: &str = "name";
const COL_TYPE: &str = "type";
const COL_SOURCE: &str = "source";
const COL_ACTIVE: &str = "active";
const COL_LAST_RUN: &str = "last_run";
const COL_NEXT_RUN: &str = "next_run";
const COL_STATUS: &str = "status";
const COL_STARTED: &str = "started";
const COL_FINISHED: &str = "finished";
const COL_INSERTED: &str = "inserted";
const COL_UPDATED: &str = "updated";
const COL_FAILED: &str = "failed";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ScheduledJobColumn {
    Name,
    JobType,
    Source,
    Cron,
    Active,
    LastRun,
    NextRun,
}

impl ScheduledJobColumn {
    const ALL: [Self; 7] = [
        Self::Name,
        Self::JobType,
        Self::Source,
        Self::Cron,
        Self::Active,
        Self::LastRun,
        Self::NextRun,
    ];

    const DESCRIPTORS: [ColumnDescriptor; 8] = [
        ColumnDescriptor::new(COL_NAME, "Name", 210.0, 150.0, 420.0)
            .required()
            .clipped(),
        ColumnDescriptor::new(COL_TYPE, "Type", 92.0, 72.0, 180.0),
        ColumnDescriptor::new(COL_SOURCE, "Source", 150.0, 110.0, 280.0)
            .required()
            .clipped(),
        ColumnDescriptor::new("cron", "Cron", 120.0, 96.0, 240.0),
        ColumnDescriptor::new(COL_ACTIVE, "Active", 68.0, 56.0, 110.0),
        ColumnDescriptor::new(COL_LAST_RUN, "Last run", 128.0, 108.0, 240.0),
        ColumnDescriptor::new(COL_NEXT_RUN, "Next run", 132.0, 108.0, 240.0),
        ColumnDescriptor::new("actions", "Actions", 150.0, 128.0, 280.0).required(),
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("scheduled job column is in ALL")
    }

    fn key(self) -> &'static str {
        match self {
            Self::Name => COL_NAME,
            Self::JobType => COL_TYPE,
            Self::Source => COL_SOURCE,
            Self::Cron => "cron",
            Self::Active => COL_ACTIVE,
            Self::LastRun => COL_LAST_RUN,
            Self::NextRun => COL_NEXT_RUN,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Name => "Name",
            Self::JobType => "Type",
            Self::Source => "Source",
            Self::Cron => "Cron",
            Self::Active => "Active",
            Self::LastRun => "Last run",
            Self::NextRun => "Next run",
        }
    }

    fn payload(self, job: &ScheduledJob) -> (String, String) {
        let value = match self {
            Self::Name => job.name.clone(),
            Self::JobType => job.job_type.clone(),
            Self::Source => job.source.clone(),
            Self::Cron => job.cron_schedule.clone(),
            Self::Active => bool_text(job.active).to_owned(),
            Self::LastRun => job.last_run.clone(),
            Self::NextRun => job.next_run.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum JobRunColumn {
    JobType,
    Source,
    Status,
    Started,
    Finished,
    Inserted,
    Updated,
    Failed,
    Message,
    RunId,
}

impl JobRunColumn {
    const ALL: [Self; 10] = [
        Self::JobType,
        Self::Source,
        Self::Status,
        Self::Started,
        Self::Finished,
        Self::Inserted,
        Self::Updated,
        Self::Failed,
        Self::Message,
        Self::RunId,
    ];

    const DESCRIPTORS: [ColumnDescriptor; 11] = [
        ColumnDescriptor::new(COL_TYPE, "Type", 96.0, 76.0, 180.0).required(),
        ColumnDescriptor::new(COL_SOURCE, "Source", 150.0, 110.0, 280.0)
            .required()
            .clipped(),
        ColumnDescriptor::new(COL_STATUS, "Status", 92.0, 72.0, 160.0).required(),
        ColumnDescriptor::new(COL_STARTED, "Started", 128.0, 104.0, 240.0),
        ColumnDescriptor::new(COL_FINISHED, "Finished", 128.0, 104.0, 240.0),
        ColumnDescriptor::new(COL_INSERTED, "Inserted", 78.0, 60.0, 140.0),
        ColumnDescriptor::new(COL_UPDATED, "Updated", 78.0, 60.0, 140.0),
        ColumnDescriptor::new(COL_FAILED, "Failed", 70.0, 56.0, 120.0),
        ColumnDescriptor::new("message", "Message", 220.0, 160.0, 520.0).clipped(),
        ColumnDescriptor::new("run_id", "Run ID", 180.0, 130.0, 360.0)
            .hidden_by_default()
            .clipped(),
        ColumnDescriptor::new("actions", "Actions", 150.0, 128.0, 280.0).required(),
    ];

    fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|column| *column == self)
            .expect("job run column is in ALL")
    }

    fn key(self) -> &'static str {
        match self {
            Self::JobType => COL_TYPE,
            Self::Source => COL_SOURCE,
            Self::Status => COL_STATUS,
            Self::Started => COL_STARTED,
            Self::Finished => COL_FINISHED,
            Self::Inserted => COL_INSERTED,
            Self::Updated => COL_UPDATED,
            Self::Failed => COL_FAILED,
            Self::Message => "message",
            Self::RunId => "run_id",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::JobType => "Type",
            Self::Source => "Source",
            Self::Status => "Status",
            Self::Started => "Started",
            Self::Finished => "Finished",
            Self::Inserted => "Inserted",
            Self::Updated => "Updated",
            Self::Failed => "Failed",
            Self::Message => "Message",
            Self::RunId => "Run ID",
        }
    }

    fn payload(self, run: &JobRun) -> (String, String) {
        let value = match self {
            Self::JobType => run.job_type.clone(),
            Self::Source => run.source.clone(),
            Self::Status => run.status.as_str().to_owned(),
            Self::Started => run.started.clone(),
            Self::Finished => run.finished.clone().unwrap_or_else(|| "-".to_owned()),
            Self::Inserted => run.inserted.to_string(),
            Self::Updated => run.updated.to_string(),
            Self::Failed => run.failed.to_string(),
            Self::Message => run.message.clone(),
            Self::RunId => run.id.clone(),
        };
        (value.clone(), value)
    }
}

#[derive(Clone, Debug)]
pub struct JobsState {
    pub filter: String,
    pub scheduled_table: TableState,
    pub runs_table: TableState,
    pub active_table: TableId,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JobsAction {
    Feedback(String),
}

impl Default for JobsState {
    fn default() -> Self {
        Self {
            filter: String::new(),
            scheduled_table: TableState::new(TableId::ScheduledJobs),
            runs_table: TableState::new(TableId::JobRuns),
            active_table: TableId::ScheduledJobs,
        }
    }
}

pub fn render(
    ui: &mut egui::Ui,
    scheduled_jobs: &[ScheduledJob],
    job_runs: &[JobRun],
    mock_job_message: &mut Option<String>,
    state: &mut JobsState,
    layouts: &mut TableLayoutRegistry,
) -> Option<JobsAction> {
    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            let running = job_runs
                .iter()
                .filter(|run| run.status == JobStatus::Running)
                .count();
            let failed = job_runs
                .iter()
                .filter(|run| run.status == JobStatus::Failed)
                .count();
            let subtitle = format!(
                "{} scheduled · {} runs · {} running · {} failed",
                scheduled_jobs.len(),
                job_runs.len(),
                running,
                failed
            );
            style::page_header(ui, "Jobs", Some("Scheduler"), Some(&subtitle), |ui| {
                if running > 0 {
                    style::status_badge(ui, "RUNNING");
                } else {
                    style::status_badge(ui, "IDLE");
                }
                if ui.button("Run now").clicked() {
                    let job_name = selected_job_name(scheduled_jobs, state)
                        .unwrap_or_else(|| "MANUAL_REFRESH".to_owned());
                    let message = mock_run_now_message(&job_name);
                    *mock_job_message = Some(message.clone());
                    action = Some(JobsAction::Feedback(message));
                }
            });
            if let Some(message) = mock_job_message.as_deref() {
                ui.label(egui::RichText::new(message).weak());
            }
            ui.add_space(6.0);
            filters(ui, state);
            ui.add_space(4.0);
            scheduled_jobs_table(
                ui,
                scheduled_jobs,
                mock_job_message,
                state,
                layouts,
                &mut action,
            );
            ui.add_space(8.0);
            job_runs_table(ui, job_runs, mock_job_message, state, layouts, &mut action);
        });
    action
}

pub fn handle_keyboard(
    ctx: &egui::Context,
    scheduled_jobs: &[ScheduledJob],
    job_runs: &[JobRun],
    mock_job_message: &mut Option<String>,
    state: &mut JobsState,
    layouts: &mut TableLayoutRegistry,
) -> bool {
    if ctx.text_edit_focused() {
        return false;
    }

    match state.active_table {
        TableId::JobRuns => {
            handle_job_run_keyboard(ctx, job_runs, mock_job_message, state, layouts)
        }
        _ => handle_scheduled_job_keyboard(ctx, scheduled_jobs, mock_job_message, state, layouts),
    }
}

fn handle_scheduled_job_keyboard(
    ctx: &egui::Context,
    scheduled_jobs: &[ScheduledJob],
    mock_job_message: &mut Option<String>,
    state: &mut JobsState,
    layouts: &mut TableLayoutRegistry,
) -> bool {
    let visible_indices = visible_scheduled_job_indices(scheduled_jobs, state);
    let mut moved = false;
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        state
            .scheduled_table
            .move_focus_row(&visible_indices, -1, Some(0));
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        state
            .scheduled_table
            .move_focus_row(&visible_indices, 1, Some(0));
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        let columns = layouts
            .visible_indices(TableId::ScheduledJobs, &ScheduledJobColumn::DESCRIPTORS)
            .into_iter()
            .filter(|index| *index < ScheduledJobColumn::ALL.len())
            .collect::<Vec<_>>();
        state
            .scheduled_table
            .move_focus_visible_column(&columns, -1);
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        let columns = layouts
            .visible_indices(TableId::ScheduledJobs, &ScheduledJobColumn::DESCRIPTORS)
            .into_iter()
            .filter(|index| *index < ScheduledJobColumn::ALL.len())
            .collect::<Vec<_>>();
        state.scheduled_table.move_focus_visible_column(&columns, 1);
        moved = true;
    }
    if moved {
        state.runs_table.clear_selection();
        state.active_table = TableId::ScheduledJobs;
        sync_scheduled_job_focus(scheduled_jobs, state);
    }

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        if state.scheduled_table.selected_index().is_none() {
            state
                .scheduled_table
                .move_focus_row(&visible_indices, 1, Some(0));
            sync_scheduled_job_focus(scheduled_jobs, state);
        }
        if let Some(job) = state
            .scheduled_table
            .selected_index()
            .and_then(|index| scheduled_jobs.get(index))
        {
            *mock_job_message = Some(format!(
                "DETAIL scheduled job {} | {} | next {}",
                job.name, job.job_type, job.next_run
            ));
            return true;
        }
    }

    false
}

fn handle_job_run_keyboard(
    ctx: &egui::Context,
    job_runs: &[JobRun],
    mock_job_message: &mut Option<String>,
    state: &mut JobsState,
    layouts: &mut TableLayoutRegistry,
) -> bool {
    let visible_indices = visible_job_run_indices(job_runs, state);
    let mut moved = false;
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowUp)) {
        state
            .runs_table
            .move_focus_row(&visible_indices, -1, Some(0));
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowDown)) {
        state
            .runs_table
            .move_focus_row(&visible_indices, 1, Some(0));
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowLeft)) {
        let columns = layouts
            .visible_indices(TableId::JobRuns, &JobRunColumn::DESCRIPTORS)
            .into_iter()
            .filter(|index| *index < JobRunColumn::ALL.len())
            .collect::<Vec<_>>();
        state.runs_table.move_focus_visible_column(&columns, -1);
        moved = true;
    }
    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::ArrowRight)) {
        let columns = layouts
            .visible_indices(TableId::JobRuns, &JobRunColumn::DESCRIPTORS)
            .into_iter()
            .filter(|index| *index < JobRunColumn::ALL.len())
            .collect::<Vec<_>>();
        state.runs_table.move_focus_visible_column(&columns, 1);
        moved = true;
    }
    if moved {
        state.scheduled_table.clear_selection();
        state.active_table = TableId::JobRuns;
        sync_job_run_focus(job_runs, state);
    }

    if ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Enter)) {
        if state.runs_table.selected_index().is_none() {
            state
                .runs_table
                .move_focus_row(&visible_indices, 1, Some(0));
            sync_job_run_focus(job_runs, state);
        }
        if let Some(run) = state
            .runs_table
            .selected_index()
            .and_then(|index| job_runs.get(index))
        {
            *mock_job_message = Some(format!(
                "DETAIL run {} | {} | {}",
                run.id,
                run.job_type,
                run.status.as_str()
            ));
            return true;
        }
    }

    false
}

fn filters(ui: &mut egui::Ui, state: &mut JobsState) {
    ui.horizontal(|ui| {
        ui.label("Filter");
        ui.add_sized(
            [270.0, 20.0],
            egui::TextEdit::singleline(&mut state.filter)
                .hint_text("name / type / source / status"),
        );
        if ui.button("Clear").clicked() {
            state.filter.clear();
            state.scheduled_table.clear_selection();
            state.runs_table.clear_selection();
        }
    });
}

fn scheduled_jobs_table(
    ui: &mut egui::Ui,
    scheduled_jobs: &[ScheduledJob],
    mock_job_message: &mut Option<String>,
    state: &mut JobsState,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<JobsAction>,
) {
    let filtered_jobs = visible_scheduled_job_indices(scheduled_jobs, state);
    state.scheduled_table.retain_visible(&filtered_jobs);
    ui.label(
        egui::RichText::new(format!(
            "Scheduled jobs ({}/{})",
            filtered_jobs.len(),
            scheduled_jobs.len()
        ))
        .strong(),
    );
    let visible_row = state
        .scheduled_table
        .focused_row_index
        .or(state.scheduled_table.selected_index())
        .and_then(|index| scheduled_jobs.get(index))
        .map(|job| {
            (
                job.name.as_str(),
                layouts.visible_row_text(
                    TableId::ScheduledJobs,
                    &ScheduledJobColumn::DESCRIPTORS,
                    &scheduled_job_cells(job),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::ScheduledJobs,
        &ScheduledJobColumn::DESCRIPTORS,
        state
            .scheduled_table
            .focused_column_index
            .and_then(|index| ScheduledJobColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.scheduled_table.clear_focus();
    }
    if filtered_jobs.is_empty() {
        style::state_message(ui, "EMPTY", "No scheduled jobs match the current filter.");
        return;
    }
    let revision = managed_table_revision(
        layouts,
        TableId::ScheduledJobs,
        &ScheduledJobColumn::DESCRIPTORS,
    );
    let mut table = TableBuilder::new(ui)
        .id_salt(("scheduled_jobs_table", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(210.0);
    for descriptor in ScheduledJobColumn::DESCRIPTORS {
        table = table.column(managed_column(
            layouts,
            TableId::ScheduledJobs,
            &ScheduledJobColumn::DESCRIPTORS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.scheduled_table,
                    COL_NAME,
                    ScheduledJobColumn::Name.label(),
                )
            });
            header.col(|ui| {
                sortable_header_cell(
                    ui,
                    &mut state.scheduled_table,
                    COL_TYPE,
                    ScheduledJobColumn::JobType.label(),
                )
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.scheduled_table, COL_SOURCE, "Source")
            });
            header.col(|ui| header_cell(ui, ScheduledJobColumn::Cron.label()));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.scheduled_table, COL_ACTIVE, "Active")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.scheduled_table, COL_LAST_RUN, "Last run")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.scheduled_table, COL_NEXT_RUN, "Next run")
            });
            header.col(|ui| header_cell(ui, "Actions"));
        })
        .body(|mut body| {
            for index in filtered_jobs {
                let job = scheduled_jobs[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.scheduled_table.is_focused_row(index));
                    row.set_selected(
                        state.scheduled_table.selection.is_selected(index)
                            && state.runs_table.selected_index().is_none(),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .scheduled_table
                                .is_focused_cell(index, ScheduledJobColumn::Name.index()),
                        );
                        let response = ui
                            .selectable_label(
                                state.scheduled_table.selection.is_selected(index)
                                    && state.runs_table.selected_index().is_none(),
                                &job.name,
                            )
                            .on_hover_text("Double-click to open scheduled job detail placeholder.")
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            select_scheduled_job_cell(state, index, &job, ScheduledJobColumn::Name);
                            state.runs_table.clear_selection();
                            state.active_table = TableId::ScheduledJobs;
                        }
                        if response.double_clicked() {
                            select_scheduled_job_cell(state, index, &job, ScheduledJobColumn::Name);
                            state.runs_table.clear_selection();
                            state.active_table = TableId::ScheduledJobs;
                            let message = format!(
                                "DETAIL scheduled job {} | {} | next {}",
                                job.name, job.job_type, job.next_run
                            );
                            *mock_job_message = Some(message.clone());
                            *action = Some(JobsAction::Feedback(message));
                        }
                        response.context_menu(|ui| {
                            if ui.button("Run Now").clicked() {
                                let message = mock_run_now_message(&job.name);
                                *mock_job_message = Some(message.clone());
                                *action = Some(JobsAction::Feedback(message));
                                ui.close();
                            }
                            if ui.button("Open Latest Run").clicked() {
                                let message = format!("DETAIL latest run for {}", job.name);
                                *mock_job_message = Some(message.clone());
                                *action = Some(JobsAction::Feedback(message));
                                ui.close();
                            }
                            if ui.button("Show Diagnostics").clicked() {
                                let message = format!("DIAG mock scheduled job {}", job.name);
                                *mock_job_message = Some(message.clone());
                                *action = Some(JobsAction::Feedback(message));
                                ui.close();
                            }
                            if ui.button("Copy Job").clicked() {
                                ui.copy_text(scheduled_job_copy_text(&job));
                                *action =
                                    Some(JobsAction::Feedback(format!("COPIED: job {}", job.name)));
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .scheduled_table
                                .is_focused_cell(index, ScheduledJobColumn::JobType.index()),
                        );
                        ui.label(&job.job_type);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .scheduled_table
                                .is_focused_cell(index, ScheduledJobColumn::Source.index()),
                        );
                        style::source_badge(ui, &job.source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .scheduled_table
                                .is_focused_cell(index, ScheduledJobColumn::Cron.index()),
                        );
                        ui.monospace(&job.cron_schedule);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .scheduled_table
                                .is_focused_cell(index, ScheduledJobColumn::Active.index()),
                        );
                        ui.label(bool_text(job.active));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .scheduled_table
                                .is_focused_cell(index, ScheduledJobColumn::LastRun.index()),
                        );
                        ui.label(&job.last_run);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .scheduled_table
                                .is_focused_cell(index, ScheduledJobColumn::NextRun.index()),
                        );
                        ui.label(&job.next_run);
                    });
                    row.col(|ui| {
                        ui.horizontal_wrapped(|ui| {
                            if crate::ui::actions::action_button(
                                ui,
                                "Run",
                                "Queue this mock job now.",
                            )
                            .clicked()
                            {
                                let message = mock_run_now_message(&job.name);
                                *mock_job_message = Some(message.clone());
                                state.scheduled_table.select(index);
                                state.runs_table.clear_selection();
                                state.active_table = TableId::ScheduledJobs;
                                *action = Some(JobsAction::Feedback(message));
                            }
                            if crate::ui::actions::action_button(
                                ui,
                                "Open",
                                "Open latest run detail placeholder.",
                            )
                            .clicked()
                            {
                                let message = format!("DETAIL latest run for {}", job.name);
                                *mock_job_message = Some(message.clone());
                                state.scheduled_table.select(index);
                                state.runs_table.clear_selection();
                                state.active_table = TableId::ScheduledJobs;
                                *action = Some(JobsAction::Feedback(message));
                            }
                            if crate::ui::actions::action_button(ui, "Copy", "Copy job row.")
                                .clicked()
                            {
                                ui.copy_text(scheduled_job_copy_text(&job));
                                state.scheduled_table.select(index);
                                *action =
                                    Some(JobsAction::Feedback(format!("COPIED: job {}", job.name)));
                            }
                        });
                    });
                });
            }
        });
}

fn job_runs_table(
    ui: &mut egui::Ui,
    job_runs: &[JobRun],
    mock_job_message: &mut Option<String>,
    state: &mut JobsState,
    layouts: &mut TableLayoutRegistry,
    action: &mut Option<JobsAction>,
) {
    let filtered_runs = visible_job_run_indices(job_runs, state);
    state.runs_table.retain_visible(&filtered_runs);
    ui.label(
        egui::RichText::new(format!(
            "Recent job runs ({}/{})",
            filtered_runs.len(),
            job_runs.len()
        ))
        .strong(),
    );
    let visible_row = state
        .runs_table
        .focused_row_index
        .or(state.runs_table.selected_index())
        .and_then(|index| job_runs.get(index))
        .map(|run| {
            (
                run.id.as_str(),
                layouts.visible_row_text(
                    TableId::JobRuns,
                    &JobRunColumn::DESCRIPTORS,
                    &job_run_cells(run),
                ),
            )
        });
    if table_layout_controls(
        ui,
        layouts,
        TableId::JobRuns,
        &JobRunColumn::DESCRIPTORS,
        state
            .runs_table
            .focused_column_index
            .and_then(|index| JobRunColumn::ALL.get(index))
            .map(|column| column.key()),
        visible_row,
    ) {
        state.runs_table.clear_focus();
    }
    if filtered_runs.is_empty() {
        style::state_message(ui, "EMPTY", "No job runs match the current filter.");
        return;
    }
    let revision = managed_table_revision(layouts, TableId::JobRuns, &JobRunColumn::DESCRIPTORS);
    let mut table = TableBuilder::new(ui)
        .id_salt(("job_runs_table", revision))
        .striped(true)
        .resizable(false)
        .max_scroll_height(300.0);
    for descriptor in JobRunColumn::DESCRIPTORS {
        table = table.column(managed_column(
            layouts,
            TableId::JobRuns,
            &JobRunColumn::DESCRIPTORS,
            descriptor,
        ));
    }
    table
        .header(metrics::TABLE_HEADER_HEIGHT, |mut header| {
            header.col(|ui| sortable_header_cell(ui, &mut state.runs_table, COL_TYPE, "Type"));
            header.col(|ui| sortable_header_cell(ui, &mut state.runs_table, COL_SOURCE, "Source"));
            header.col(|ui| sortable_header_cell(ui, &mut state.runs_table, COL_STATUS, "Status"));
            header
                .col(|ui| sortable_header_cell(ui, &mut state.runs_table, COL_STARTED, "Started"));
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.runs_table, COL_FINISHED, "Finished")
            });
            header.col(|ui| {
                sortable_header_cell(ui, &mut state.runs_table, COL_INSERTED, "Inserted")
            });
            header
                .col(|ui| sortable_header_cell(ui, &mut state.runs_table, COL_UPDATED, "Updated"));
            header.col(|ui| sortable_header_cell(ui, &mut state.runs_table, COL_FAILED, "Failed"));
            header.col(|ui| header_cell(ui, JobRunColumn::Message.label()));
            header.col(|ui| header_cell(ui, JobRunColumn::RunId.label()));
            header.col(|ui| header_cell(ui, "Actions"));
        })
        .body(|mut body| {
            for index in filtered_runs {
                let run = job_runs[index].clone();
                body.row(metrics::ROW_HEIGHT_COMPACT, |mut row| {
                    row.set_overline(state.runs_table.is_focused_row(index));
                    row.set_selected(
                        state.runs_table.selection.is_selected(index)
                            && state.scheduled_table.selected_index().is_none(),
                    );
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::JobType.index()),
                        );
                        let response = ui
                            .selectable_label(
                                state.runs_table.selection.is_selected(index),
                                &run.job_type,
                            )
                            .on_hover_text("Double-click to open job run detail placeholder.")
                            .on_hover_cursor(egui::CursorIcon::PointingHand);
                        if response.clicked() {
                            select_job_run_cell(state, index, &run, JobRunColumn::JobType);
                            state.scheduled_table.clear_selection();
                            state.active_table = TableId::JobRuns;
                        }
                        if response.double_clicked() {
                            select_job_run_cell(state, index, &run, JobRunColumn::JobType);
                            state.scheduled_table.clear_selection();
                            state.active_table = TableId::JobRuns;
                            let message = format!(
                                "DETAIL run {} | {} | {}",
                                run.id,
                                run.job_type,
                                run.status.as_str()
                            );
                            *mock_job_message = Some(message.clone());
                            *action = Some(JobsAction::Feedback(message));
                        }
                        response.context_menu(|ui| {
                            if ui.button("Open Run Detail").clicked() {
                                let message = format!("DETAIL run {}", run.id);
                                *mock_job_message = Some(message.clone());
                                *action = Some(JobsAction::Feedback(message));
                                ui.close();
                            }
                            if ui.button("Run Similar").clicked() {
                                let message = mock_run_now_message(&run.job_type);
                                *mock_job_message = Some(message.clone());
                                *action = Some(JobsAction::Feedback(message));
                                ui.close();
                            }
                            if ui.button("Show Diagnostics").clicked() {
                                let message = format!("DIAG {} | {}", run.id, run.message);
                                *mock_job_message = Some(message.clone());
                                *action = Some(JobsAction::Feedback(message));
                                ui.close();
                            }
                            if ui.button("Copy Run").clicked() {
                                ui.copy_text(job_run_copy_text(&run));
                                *action =
                                    Some(JobsAction::Feedback(format!("COPIED: run {}", run.id)));
                                ui.close();
                            }
                        });
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::Source.index()),
                        );
                        style::source_badge(ui, &run.source);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::Status.index()),
                        );
                        style::job_status_badge(ui, run.status);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::Started.index()),
                        );
                        ui.label(&run.started);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::Finished.index()),
                        );
                        ui.label(run.finished.as_deref().unwrap_or("-"));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::Inserted.index()),
                        );
                        ui.label(format_number(f64::from(run.inserted), 0));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::Updated.index()),
                        );
                        ui.label(format_number(f64::from(run.updated), 0));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::Failed.index()),
                        );
                        ui.label(format_number(f64::from(run.failed), 0));
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::Message.index()),
                        );
                        ui.label(&run.message);
                    });
                    row.col(|ui| {
                        style::focused_table_cell(
                            ui,
                            state
                                .runs_table
                                .is_focused_cell(index, JobRunColumn::RunId.index()),
                        );
                        ui.monospace(&run.id);
                    });
                    row.col(|ui| {
                        ui.horizontal_wrapped(|ui| {
                            if crate::ui::actions::action_button(
                                ui,
                                "Open",
                                "Open run detail placeholder.",
                            )
                            .clicked()
                            {
                                let message = format!("DETAIL run {}", run.id);
                                *mock_job_message = Some(message.clone());
                                state.runs_table.select(index);
                                state.scheduled_table.clear_selection();
                                state.active_table = TableId::JobRuns;
                                *action = Some(JobsAction::Feedback(message));
                            }
                            if crate::ui::actions::action_button(
                                ui,
                                "Run",
                                "Queue a similar mock job.",
                            )
                            .clicked()
                            {
                                let message = mock_run_now_message(&run.job_type);
                                *mock_job_message = Some(message.clone());
                                state.runs_table.select(index);
                                state.scheduled_table.clear_selection();
                                state.active_table = TableId::JobRuns;
                                *action = Some(JobsAction::Feedback(message));
                            }
                            if crate::ui::actions::action_button(ui, "Copy", "Copy run row.")
                                .clicked()
                            {
                                ui.copy_text(job_run_copy_text(&run));
                                state.runs_table.select(index);
                                *action =
                                    Some(JobsAction::Feedback(format!("COPIED: run {}", run.id)));
                            }
                        });
                    });
                });
            }
        });
}

fn select_scheduled_job_cell(
    state: &mut JobsState,
    index: usize,
    job: &ScheduledJob,
    column: ScheduledJobColumn,
) {
    let (display, raw) = column.payload(job);
    state
        .scheduled_table
        .select_cell(index, column.index(), column.key(), display, raw);
}

fn sync_scheduled_job_focus(scheduled_jobs: &[ScheduledJob], state: &mut JobsState) {
    let (Some(row_index), Some(column_index)) = (
        state.scheduled_table.focused_row_index,
        state.scheduled_table.focused_column_index,
    ) else {
        return;
    };
    let Some(job) = scheduled_jobs.get(row_index) else {
        return;
    };
    let Some(column) = ScheduledJobColumn::ALL.get(column_index).copied() else {
        return;
    };
    let (display, raw) = column.payload(job);
    state
        .scheduled_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn select_job_run_cell(state: &mut JobsState, index: usize, run: &JobRun, column: JobRunColumn) {
    let (display, raw) = column.payload(run);
    state
        .runs_table
        .select_cell(index, column.index(), column.key(), display, raw);
}

fn sync_job_run_focus(job_runs: &[JobRun], state: &mut JobsState) {
    let (Some(row_index), Some(column_index)) = (
        state.runs_table.focused_row_index,
        state.runs_table.focused_column_index,
    ) else {
        return;
    };
    let Some(run) = job_runs.get(row_index) else {
        return;
    };
    let Some(column) = JobRunColumn::ALL.get(column_index).copied() else {
        return;
    };
    let (display, raw) = column.payload(run);
    state
        .runs_table
        .set_focused_cell_payload(column.key(), display, raw);
}

fn visible_scheduled_job_indices(scheduled_jobs: &[ScheduledJob], state: &JobsState) -> Vec<usize> {
    let mut indices = scheduled_jobs
        .iter()
        .enumerate()
        .filter(|(_, job)| scheduled_job_matches(job, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_scheduled_job_indices(
        scheduled_jobs,
        &mut indices,
        state.scheduled_table.sort.as_ref(),
    );
    indices
}

fn visible_job_run_indices(job_runs: &[JobRun], state: &JobsState) -> Vec<usize> {
    let mut indices = job_runs
        .iter()
        .enumerate()
        .filter(|(_, run)| job_run_matches(run, &state.filter))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    sort_job_run_indices(job_runs, &mut indices, state.runs_table.sort.as_ref());
    indices
}

fn sort_scheduled_job_indices(
    scheduled_jobs: &[ScheduledJob],
    indices: &mut [usize],
    sort: Option<&SortSpec>,
) {
    let Some(sort) = sort else {
        return;
    };

    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_scheduled_jobs(
                &scheduled_jobs[*left],
                &scheduled_jobs[*right],
                &sort.column,
            ))
            .then_with(|| scheduled_jobs[*left].name.cmp(&scheduled_jobs[*right].name))
    });
}

fn sort_job_run_indices(job_runs: &[JobRun], indices: &mut [usize], sort: Option<&SortSpec>) {
    let Some(sort) = sort else {
        return;
    };

    indices.sort_by(|left, right| {
        sort.direction
            .apply(compare_job_runs(
                &job_runs[*left],
                &job_runs[*right],
                &sort.column,
            ))
            .then_with(|| job_runs[*left].id.cmp(&job_runs[*right].id))
    });
}

fn compare_scheduled_jobs(left: &ScheduledJob, right: &ScheduledJob, column: &str) -> Ordering {
    match column {
        COL_NAME => left.name.cmp(&right.name),
        COL_TYPE => left.job_type.cmp(&right.job_type),
        COL_SOURCE => left.source.cmp(&right.source),
        COL_ACTIVE => left.active.cmp(&right.active),
        COL_LAST_RUN => left.last_run.cmp(&right.last_run),
        COL_NEXT_RUN => left.next_run.cmp(&right.next_run),
        _ => Ordering::Equal,
    }
}

fn compare_job_runs(left: &JobRun, right: &JobRun, column: &str) -> Ordering {
    match column {
        COL_TYPE => left.job_type.cmp(&right.job_type),
        COL_SOURCE => left.source.cmp(&right.source),
        COL_STATUS => job_status_rank(left.status).cmp(&job_status_rank(right.status)),
        COL_STARTED => left.started.cmp(&right.started),
        COL_FINISHED => left.finished.cmp(&right.finished),
        COL_INSERTED => left.inserted.cmp(&right.inserted),
        COL_UPDATED => left.updated.cmp(&right.updated),
        COL_FAILED => left.failed.cmp(&right.failed),
        _ => Ordering::Equal,
    }
}

fn job_status_rank(status: JobStatus) -> u8 {
    match status {
        JobStatus::Queued => 0,
        JobStatus::Running => 1,
        JobStatus::Succeeded => 2,
        JobStatus::Failed => 3,
        JobStatus::Unknown => 4,
    }
}

fn scheduled_job_matches(job: &ScheduledJob, filter: &str) -> bool {
    any_contains_ci(
        [
            job.name.as_str(),
            job.job_type.as_str(),
            job.source.as_str(),
            job.cron_schedule.as_str(),
            job.last_run.as_str(),
            job.next_run.as_str(),
            if job.active { "active" } else { "inactive" },
        ],
        filter,
    )
}

fn job_run_matches(run: &JobRun, filter: &str) -> bool {
    any_contains_ci(
        [
            run.id.as_str(),
            run.job_type.as_str(),
            run.source.as_str(),
            run.status.as_str(),
            run.started.as_str(),
            run.finished.as_deref().unwrap_or("-"),
            run.message.as_str(),
        ],
        filter,
    )
}

pub(crate) fn mock_run_now_message(job_name: &str) -> String {
    format!("QUEUED mock job {}", job_name.trim())
}

fn selected_job_name(scheduled_jobs: &[ScheduledJob], state: &JobsState) -> Option<String> {
    state
        .scheduled_table
        .selected_index()
        .and_then(|index| scheduled_jobs.get(index))
        .map(|job| job.name.clone())
}

fn scheduled_job_cells(job: &ScheduledJob) -> Vec<(&'static str, String)> {
    let mut cells = ScheduledJobColumn::ALL
        .iter()
        .map(|column| (column.key(), column.payload(job).1))
        .collect::<Vec<_>>();
    cells.push(("actions", "run/open/copy".to_owned()));
    cells
}

fn job_run_cells(run: &JobRun) -> Vec<(&'static str, String)> {
    let mut cells = JobRunColumn::ALL
        .iter()
        .map(|column| (column.key(), column.payload(run).1))
        .collect::<Vec<_>>();
    cells.push(("actions", "open/run/copy".to_owned()));
    cells
}

fn scheduled_job_copy_text(job: &ScheduledJob) -> String {
    [
        job.name.clone(),
        job.job_type.clone(),
        job.source.clone(),
        job.cron_schedule.clone(),
        job.last_run.clone(),
        job.next_run.clone(),
        crate::pages::bool_text(job.active).to_owned(),
    ]
    .join("\t")
}

fn job_run_copy_text(run: &JobRun) -> String {
    [
        run.id.clone(),
        run.job_type.clone(),
        run.source.clone(),
        run.status.as_str().to_owned(),
        run.started.clone(),
        run.finished.clone().unwrap_or_else(|| "-".to_owned()),
        run.inserted.to_string(),
        run.updated.to_string(),
        run.failed.to_string(),
        run.message.clone(),
    ]
    .join("\t")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scheduled_job(name: &str, next_run: &str) -> ScheduledJob {
        ScheduledJob {
            name: name.to_owned(),
            job_type: "prices".to_owned(),
            source: "mock".to_owned(),
            cron_schedule: "* * * * *".to_owned(),
            active: true,
            last_run: "2026-06-19".to_owned(),
            next_run: next_run.to_owned(),
        }
    }

    fn job_run(id: &str, failed: u32) -> JobRun {
        JobRun {
            id: id.to_owned(),
            job_type: "prices".to_owned(),
            source: "mock".to_owned(),
            status: JobStatus::Succeeded,
            started: "2026-06-20".to_owned(),
            finished: Some("2026-06-20".to_owned()),
            inserted: 0,
            updated: 0,
            failed,
            message: "done".to_owned(),
        }
    }

    #[test]
    fn mock_run_now_message_names_job() {
        assert_eq!(
            mock_run_now_message("PRICE_INGESTION"),
            "QUEUED mock job PRICE_INGESTION"
        );
    }

    #[test]
    fn sorts_scheduled_jobs_by_next_run() {
        let jobs = vec![
            scheduled_job("late", "2026-06-22"),
            scheduled_job("early", "2026-06-21"),
        ];
        let mut state = JobsState::default();
        state.scheduled_table.toggle_sort(COL_NEXT_RUN);

        assert_eq!(visible_scheduled_job_indices(&jobs, &state), vec![1, 0]);
    }

    #[test]
    fn sorts_job_runs_by_failed_desc() {
        let runs = vec![job_run("a", 0), job_run("b", 2)];
        let mut state = JobsState::default();
        state.runs_table.toggle_sort(COL_FAILED);
        state.runs_table.toggle_sort(COL_FAILED);

        assert_eq!(visible_job_run_indices(&runs, &state), vec![1, 0]);
    }

    #[test]
    fn job_column_descriptors_cover_copyable_fields() {
        assert_eq!(ScheduledJobColumn::ALL.len(), 7);
        assert_eq!(ScheduledJobColumn::DESCRIPTORS.len(), 8);
        assert_eq!(ScheduledJobColumn::NextRun.key(), COL_NEXT_RUN);
        assert_eq!(JobRunColumn::ALL.len(), 10);
        assert_eq!(JobRunColumn::DESCRIPTORS.len(), 11);
        assert_eq!(JobRunColumn::Message.label(), "Message");
        assert_eq!(JobRunColumn::Failed.index(), 7);
        assert_eq!(JobRunColumn::RunId.key(), "run_id");
        assert!(!JobRunColumn::DESCRIPTORS[9].default_visible);
    }

    #[test]
    fn hidden_job_columns_are_excluded_from_visible_copy() {
        let run = job_run("run-1", 0);
        let mut layouts = TableLayoutRegistry::default();
        let text = layouts.visible_row_text(
            TableId::JobRuns,
            &JobRunColumn::DESCRIPTORS,
            &job_run_cells(&run),
        );

        assert!(!text.contains("run-1"));
        layouts.show_all(TableId::JobRuns, &JobRunColumn::DESCRIPTORS);
        let text = layouts.visible_row_text(
            TableId::JobRuns,
            &JobRunColumn::DESCRIPTORS,
            &job_run_cells(&run),
        );
        assert!(text.contains("run-1"));
    }
}
