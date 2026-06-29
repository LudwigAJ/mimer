use crate::ui::metrics;

#[derive(Clone, Debug)]
pub struct LayoutState {
    pub show_left_navigation: bool,
    pub show_inspector: bool,
    pub show_context_strip: bool,
    pub show_status_bar: bool,
    pub inspector_width: f32,
    pub revision: u64,
}

impl Default for LayoutState {
    fn default() -> Self {
        Self {
            show_left_navigation: true,
            show_inspector: true,
            show_context_strip: true,
            show_status_bar: true,
            inspector_width: metrics::INSPECTOR_DEFAULT_WIDTH,
            revision: 0,
        }
    }
}

impl LayoutState {
    pub fn inspector_width_bounds(&self, available_width: f32) -> (f32, f32) {
        let rail_reserve = if self.show_left_navigation {
            metrics::LEFT_RAIL_MIN_WIDTH
        } else {
            0.0
        };
        let available_for_inspector =
            (available_width - rail_reserve - metrics::MAIN_CONTENT_MIN_WIDTH)
                .max(metrics::INSPECTOR_NARROW_MIN_WIDTH);
        let max_width = available_for_inspector.min(metrics::INSPECTOR_MAX_WIDTH);
        let min_width = metrics::INSPECTOR_MIN_WIDTH.min(max_width);
        (min_width, max_width)
    }

    pub fn clamped_inspector_width(&self, available_width: f32) -> f32 {
        let (min_width, max_width) = self.inspector_width_bounds(available_width);
        self.inspector_width.clamp(min_width, max_width)
    }

    pub fn record_inspector_width(&mut self, width: f32, available_width: f32) {
        let (min_width, max_width) = self.inspector_width_bounds(available_width);
        let displayed_preference = self.inspector_width.clamp(min_width, max_width);
        let constrained_by_window =
            self.inspector_width < min_width || self.inspector_width > max_width;
        if constrained_by_window && (width - displayed_preference).abs() < 0.5 {
            return;
        }
        self.inspector_width = (width.clamp(min_width, max_width) * 2.0).round() / 2.0;
    }

    pub fn reset(&mut self) {
        let revision = self.revision.wrapping_add(1);
        *self = Self {
            revision,
            ..Self::default()
        };
    }
}

use crate::app_model::SelectedInstrument;
use crate::charts::PlotRequest;
use crate::navigation::NavigationStack;
use crate::pages::Page;

// Future tab/view state. The app currently runs a single view, but these
// structs document the split between global app state and per-view state.
#[allow(dead_code)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkspaceViewId(pub String);

#[allow(dead_code)]
#[derive(Clone, Debug)]
pub struct WorkspaceView {
    pub id: WorkspaceViewId,
    pub title: String,
    pub active_page: Page,
    pub navigation: NavigationStack,
    pub selected_subject: SelectedInstrument,
    pub active_plot: Option<PlotRequest>,
    pub table_namespace: String,
}

#[allow(dead_code)]
impl WorkspaceView {
    pub fn single(
        title: impl Into<String>,
        active_page: Page,
        navigation: NavigationStack,
    ) -> Self {
        let title = title.into();
        Self {
            id: WorkspaceViewId("view-main".to_owned()),
            table_namespace: "view-main".to_owned(),
            title,
            active_page,
            navigation,
            selected_subject: SelectedInstrument::default(),
            active_plot: None,
        }
    }

    pub fn is_single_view(&self) -> bool {
        self.id.0 == "view-main"
    }
}

#[allow(dead_code)]
#[derive(Clone, Debug)]
pub struct AppModel {
    pub views: Vec<WorkspaceView>,
    pub active_view_id: WorkspaceViewId,
}

#[allow(dead_code)]
impl AppModel {
    pub fn single(view: WorkspaceView) -> Self {
        Self {
            active_view_id: view.id.clone(),
            views: vec![view],
        }
    }

    pub fn active_view(&self) -> Option<&WorkspaceView> {
        self.views
            .iter()
            .find(|view| view.id == self.active_view_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::AnalysisSubject;
    use crate::navigation::NavigationEntry;

    #[test]
    fn app_model_keeps_single_active_workspace_view() {
        let home = NavigationEntry::new(
            AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
            "Main Portfolio",
            Page::Portfolio,
        );
        let view = WorkspaceView::single("Main", Page::Portfolio, NavigationStack::new(home));
        let model = AppModel::single(view);

        assert_eq!(model.views.len(), 1);
        assert!(
            model
                .active_view()
                .is_some_and(WorkspaceView::is_single_view)
        );
    }

    #[test]
    fn inspector_width_clamps_to_available_workspace() {
        let layout = LayoutState {
            inspector_width: 700.0,
            ..LayoutState::default()
        };

        assert_eq!(
            layout.clamped_inspector_width(1600.0),
            metrics::INSPECTOR_MAX_WIDTH
        );
        assert_eq!(
            layout.clamped_inspector_width(760.0),
            metrics::INSPECTOR_NARROW_MIN_WIDTH
        );
    }

    #[test]
    fn reset_restores_layout_defaults_and_advances_revision() {
        let mut layout = LayoutState {
            show_inspector: false,
            inspector_width: 410.0,
            revision: 7,
            ..LayoutState::default()
        };

        layout.reset();

        assert!(layout.show_inspector);
        assert_eq!(layout.inspector_width, metrics::INSPECTOR_DEFAULT_WIDTH);
        assert_eq!(layout.revision, 8);
    }

    #[test]
    fn narrow_window_does_not_overwrite_preferred_inspector_width() {
        let mut layout = LayoutState {
            inspector_width: 340.0,
            ..LayoutState::default()
        };

        layout.record_inspector_width(metrics::INSPECTOR_NARROW_MIN_WIDTH, 760.0);

        assert_eq!(layout.inspector_width, 340.0);
    }
}
