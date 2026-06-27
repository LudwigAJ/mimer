use crate::domain::AnalysisSubject;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum InspectorMode {
    #[default]
    FollowSelection,
    Pinned,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum InspectorContext {
    PortfolioPosition {
        row_index: usize,
        subject: AnalysisSubject,
        label: String,
    },
    Fund {
        row_index: usize,
        subject: AnalysisSubject,
        label: String,
    },
    FundListing {
        subject: AnalysisSubject,
        label: String,
    },
    Holding {
        row_index: usize,
        subject: AnalysisSubject,
        label: String,
    },
    Distribution {
        row_index: usize,
        subject: AnalysisSubject,
        label: String,
    },
    DocumentSnapshot {
        row_index: usize,
        subject: AnalysisSubject,
        label: String,
    },
    Alert {
        row_index: usize,
        alert_id: String,
        affected_subject: Option<AnalysisSubject>,
        label: String,
    },
    ScheduledJob {
        row_index: usize,
        name: String,
        label: String,
    },
    JobRun {
        row_index: usize,
        run_id: String,
        label: String,
    },
    ReadinessStage {
        row_index: usize,
        key: String,
        label: String,
    },
    MarketDataPlanItem {
        row_index: usize,
        item_id: String,
        subject: Option<AnalysisSubject>,
        label: String,
    },
    SourceBudget {
        row_index: usize,
        source: String,
        label: String,
    },
    FetchLog {
        row_index: usize,
        log_id: String,
        label: String,
    },
    ConstituentReadiness {
        row_index: usize,
        subject: AnalysisSubject,
        label: String,
    },
    DataDiagnosticIssue {
        row_index: usize,
        issue_id: String,
        label: String,
    },
    TableRow {
        table_id: String,
        row_index: usize,
        label: String,
        details: String,
    },
    ChartSeries {
        subject: AnalysisSubject,
        series_id: String,
        series_kind: String,
        label: String,
        unit: String,
        source: String,
        status: String,
        role: String,
    },
    ChartPoint {
        subject: AnalysisSubject,
        series_id: String,
        series_kind: String,
        label: String,
        date: String,
        value: String,
        raw_value: String,
        unit: String,
        raw_unit: String,
        source: String,
        status: String,
        value_mode: String,
    },
    ChartWorkspace {
        subject: AnalysisSubject,
        mode: String,
        label: String,
    },
    ChartSpread {
        left_label: String,
        right_label: String,
        label: String,
        coverage: String,
        left_source: String,
        right_source: String,
        status: String,
    },
    ActiveSubject {
        subject: AnalysisSubject,
        label: String,
        tooltip: String,
    },
}

impl InspectorContext {
    pub fn kind_label(&self) -> &'static str {
        match self {
            Self::PortfolioPosition { .. } => "Position",
            Self::Fund { .. } => "Fund",
            Self::FundListing { .. } => "Listing",
            Self::Holding { .. } => "Holding",
            Self::Distribution { .. } => "Distribution",
            Self::DocumentSnapshot { .. } => "Document",
            Self::Alert { .. } => "Alert",
            Self::ScheduledJob { .. } => "Job",
            Self::JobRun { .. } => "Job run",
            Self::ReadinessStage { .. } => "Readiness",
            Self::MarketDataPlanItem { .. } => "Market data plan",
            Self::SourceBudget { .. } => "Source budget",
            Self::FetchLog { .. } => "Fetch log",
            Self::ConstituentReadiness { .. } => "Constituent",
            Self::DataDiagnosticIssue { .. } => "Diagnostic",
            Self::TableRow { .. } => "Table row",
            Self::ChartSeries { .. } => "Chart",
            Self::ChartPoint { .. } => "Chart point",
            Self::ChartWorkspace { .. } => "Chart workspace",
            Self::ChartSpread { .. } => "Spread",
            Self::ActiveSubject { .. } => "Active subject",
        }
    }

    pub fn label(&self) -> &str {
        match self {
            Self::PortfolioPosition { label, .. }
            | Self::Fund { label, .. }
            | Self::FundListing { label, .. }
            | Self::Holding { label, .. }
            | Self::Distribution { label, .. }
            | Self::DocumentSnapshot { label, .. }
            | Self::Alert { label, .. }
            | Self::ScheduledJob { label, .. }
            | Self::JobRun { label, .. }
            | Self::ReadinessStage { label, .. }
            | Self::MarketDataPlanItem { label, .. }
            | Self::SourceBudget { label, .. }
            | Self::FetchLog { label, .. }
            | Self::ConstituentReadiness { label, .. }
            | Self::DataDiagnosticIssue { label, .. }
            | Self::TableRow { label, .. }
            | Self::ChartSeries { label, .. }
            | Self::ChartPoint { label, .. }
            | Self::ChartWorkspace { label, .. }
            | Self::ChartSpread { label, .. }
            | Self::ActiveSubject { label, .. } => label,
        }
    }

    pub fn subject(&self) -> Option<&AnalysisSubject> {
        match self {
            Self::PortfolioPosition { subject, .. }
            | Self::Fund { subject, .. }
            | Self::FundListing { subject, .. }
            | Self::Holding { subject, .. }
            | Self::Distribution { subject, .. }
            | Self::DocumentSnapshot { subject, .. }
            | Self::ChartSeries { subject, .. }
            | Self::ChartPoint { subject, .. }
            | Self::ChartWorkspace { subject, .. }
            | Self::ConstituentReadiness { subject, .. }
            | Self::ActiveSubject { subject, .. } => Some(subject),
            Self::MarketDataPlanItem { subject, .. } => subject.as_ref(),
            Self::Alert {
                affected_subject, ..
            } => affected_subject.as_ref(),
            Self::ScheduledJob { .. }
            | Self::JobRun { .. }
            | Self::ReadinessStage { .. }
            | Self::SourceBudget { .. }
            | Self::FetchLog { .. }
            | Self::DataDiagnosticIssue { .. }
            | Self::TableRow { .. }
            | Self::ChartSpread { .. } => None,
        }
    }

    pub fn tooltip(&self) -> String {
        match self {
            Self::PortfolioPosition {
                row_index, subject, ..
            } => format!(
                "Inspector context: portfolio row {}\nSubject: {}",
                row_index + 1,
                subject.kind_label()
            ),
            Self::Fund {
                row_index, subject, ..
            } => format!(
                "Inspector context: fund row {}\nSubject: {}",
                row_index + 1,
                subject.kind_label()
            ),
            Self::FundListing { subject, .. }
            | Self::Holding { subject, .. }
            | Self::Distribution { subject, .. }
            | Self::DocumentSnapshot { subject, .. }
            | Self::ChartSeries { subject, .. }
            | Self::ChartPoint { subject, .. }
            | Self::ChartWorkspace { subject, .. } => {
                format!("Inspector context subject: {}", subject.kind_label())
            }
            Self::ChartSpread {
                coverage,
                left_source,
                right_source,
                status,
                ..
            } => format!(
                "Spread context\nCoverage: {coverage}\nSource A: {left_source}\nSource B: {right_source}\nStatus: {status}"
            ),
            Self::Alert {
                row_index,
                alert_id,
                affected_subject,
                ..
            } => format!(
                "Inspector context: alert row {}\nAlert ID: {}\nAffected: {}",
                row_index + 1,
                alert_id,
                affected_subject
                    .as_ref()
                    .map(AnalysisSubject::kind_label)
                    .unwrap_or("-")
            ),
            Self::ScheduledJob {
                row_index, name, ..
            } => format!(
                "Inspector context: scheduled job row {}\nJob: {}",
                row_index + 1,
                name
            ),
            Self::JobRun {
                row_index, run_id, ..
            } => format!(
                "Inspector context: job run row {}\nRun ID: {}",
                row_index + 1,
                run_id
            ),
            Self::ReadinessStage { row_index, key, .. } => format!(
                "Inspector context: readiness stage {}\nKey: {}",
                row_index + 1,
                key
            ),
            Self::MarketDataPlanItem {
                row_index,
                item_id,
                subject,
                ..
            } => format!(
                "Inspector context: market-data plan row {}\nItem: {}\nSubject: {}",
                row_index + 1,
                item_id,
                subject
                    .as_ref()
                    .map(AnalysisSubject::kind_label)
                    .unwrap_or("-")
            ),
            Self::SourceBudget {
                row_index, source, ..
            } => format!(
                "Inspector context: source budget row {}\nSource: {}",
                row_index + 1,
                source
            ),
            Self::FetchLog {
                row_index, log_id, ..
            } => format!(
                "Inspector context: fetch log row {}\nLog ID: {}",
                row_index + 1,
                log_id
            ),
            Self::ConstituentReadiness {
                row_index, subject, ..
            } => format!(
                "Inspector context: constituent readiness row {}\nSubject: {}",
                row_index + 1,
                subject.kind_label()
            ),
            Self::DataDiagnosticIssue {
                row_index,
                issue_id,
                ..
            } => format!(
                "Inspector context: data diagnostic row {}\nIssue: {}",
                row_index + 1,
                issue_id
            ),
            Self::TableRow {
                table_id,
                row_index,
                details,
                ..
            } => format!(
                "Inspector context: table row {}\nTable: {}\n{}",
                row_index + 1,
                table_id,
                details
            ),
            Self::ActiveSubject { tooltip, .. } => tooltip.clone(),
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct InspectorState {
    pub mode: InspectorMode,
    pub pinned_context: Option<InspectorContext>,
    pub last_follow_context: Option<InspectorContext>,
}

impl InspectorState {
    pub fn set_follow_context(&mut self, context: Option<InspectorContext>) {
        self.last_follow_context = context;
    }

    pub fn current_context(&self) -> Option<&InspectorContext> {
        match self.mode {
            InspectorMode::FollowSelection => self.last_follow_context.as_ref(),
            InspectorMode::Pinned => self
                .pinned_context
                .as_ref()
                .or(self.last_follow_context.as_ref()),
        }
    }

    pub fn pin_current(&mut self) -> bool {
        let Some(context) = self.current_context().cloned() else {
            return false;
        };
        self.pinned_context = Some(context);
        self.mode = InspectorMode::Pinned;
        true
    }

    pub fn unpin(&mut self) {
        self.mode = InspectorMode::FollowSelection;
        self.pinned_context = None;
    }

    pub fn is_pinned(&self) -> bool {
        self.mode == InspectorMode::Pinned && self.pinned_context.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn subject(id: &str) -> AnalysisSubject {
        AnalysisSubject::Fund(id.to_owned())
    }

    fn active(id: &str) -> InspectorContext {
        InspectorContext::ActiveSubject {
            subject: subject(id),
            label: id.to_owned(),
            tooltip: "active".to_owned(),
        }
    }

    #[test]
    fn inspector_follows_selection_by_default() {
        let mut state = InspectorState::default();
        state.set_follow_context(Some(active("VUSA")));

        assert_eq!(
            state.current_context().map(InspectorContext::label),
            Some("VUSA")
        );
    }

    #[test]
    fn pinned_inspector_ignores_selection_changes() {
        let mut state = InspectorState::default();
        state.set_follow_context(Some(active("VUSA")));
        assert!(state.pin_current());

        state.set_follow_context(Some(active("ISF")));

        assert_eq!(
            state.current_context().map(InspectorContext::label),
            Some("VUSA")
        );
        assert!(state.is_pinned());
    }

    #[test]
    fn unpin_returns_to_follow_mode() {
        let mut state = InspectorState::default();
        state.set_follow_context(Some(active("VUSA")));
        assert!(state.pin_current());
        state.set_follow_context(Some(active("ISF")));

        state.unpin();

        assert_eq!(state.mode, InspectorMode::FollowSelection);
        assert_eq!(
            state.current_context().map(InspectorContext::label),
            Some("ISF")
        );
    }

    #[test]
    fn chart_contexts_expose_labels_and_subjects() {
        let subject = subject("fund-vusa");
        let series = InspectorContext::ChartSeries {
            subject: subject.clone(),
            series_id: "series-1".to_owned(),
            series_kind: "price".to_owned(),
            label: "VUSA Price".to_owned(),
            unit: "GBp".to_owned(),
            source: "seed".to_owned(),
            status: "FRESH".to_owned(),
            role: "PRIMARY".to_owned(),
        };
        let point = InspectorContext::ChartPoint {
            subject: subject.clone(),
            series_id: "series-1".to_owned(),
            series_kind: "price".to_owned(),
            label: "VUSA Price".to_owned(),
            date: "2026-06-20".to_owned(),
            value: "92.18".to_owned(),
            raw_value: "92.18".to_owned(),
            unit: "GBp".to_owned(),
            raw_unit: "GBp".to_owned(),
            source: "seed".to_owned(),
            status: "FRESH".to_owned(),
            value_mode: "Raw".to_owned(),
        };
        let spread = InspectorContext::ChartSpread {
            left_label: "VUSA".to_owned(),
            right_label: "JEPG".to_owned(),
            label: "Spread VUSA - JEPG".to_owned(),
            coverage: "3 common / 4 left / 3 right".to_owned(),
            left_source: "seed".to_owned(),
            right_source: "mock".to_owned(),
            status: "PARTIAL".to_owned(),
        };

        assert_eq!(series.kind_label(), "Chart");
        assert_eq!(series.subject(), Some(&subject));
        assert_eq!(point.kind_label(), "Chart point");
        assert_eq!(point.subject(), Some(&subject));
        assert_eq!(spread.kind_label(), "Spread");
        assert_eq!(spread.subject(), None);
        assert!(spread.tooltip().contains("Coverage"));
    }
}
