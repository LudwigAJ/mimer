use crate::charts::PlotRequest;
use crate::compute::portfolio::{child_effective_weights, effective_node_value};
use crate::domain::{AnalysisSubject, InvestableKind, InvestableNode};
use crate::pages::{Page, format_money, format_pct, format_source, page_heading};
use crate::timeseries::TimeSeriesKind;
use crate::ui::metrics;
use eframe::egui;

const TREE_MIN_WIDTH: f32 = 700.0;
const NODE_COL_WIDTH: f32 = 300.0;
const KIND_COL_WIDTH: f32 = 88.0;
const VALUE_COL_WIDTH: f32 = 120.0;
const WEIGHT_COL_WIDTH: f32 = 80.0;
const SOURCE_COL_WIDTH: f32 = 160.0;

#[derive(Clone, Debug, Default)]
pub struct HierarchyState {
    pub selected_node_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HierarchyAction {
    Open {
        subject: AnalysisSubject,
        label: String,
        page: Page,
        breadcrumbs: Vec<String>,
    },
    Plot(PlotRequest),
}

pub fn render(
    ui: &mut egui::Ui,
    root: &InvestableNode,
    base_currency: &str,
    state: &mut HierarchyState,
) -> Option<HierarchyAction> {
    if state.selected_node_id.is_none() {
        state.selected_node_id = Some(root.id.clone());
    }

    let mut action = None;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            page_heading(ui, "Portfolio Hierarchy");
            ui.label(
                "Investable containers, listings, and look-through holdings from local mock data.",
            );
            ui.add_space(6.0);

            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    ui.set_min_width(TREE_MIN_WIDTH);
                    tree_header(ui);
                    tree_node(ui, root, root, base_currency, state, &mut action, 0);
                });
                ui.separator();
                ui.vertical(|ui| {
                    selected_panel(ui, root, base_currency, state, &mut action);
                });
            });
        });

    action
}

fn tree_header(ui: &mut egui::Ui) {
    ui.horizontal(|ui| {
        ui.add_space(ui.spacing().indent);
        header_cell(ui, "Node", NODE_COL_WIDTH);
        header_cell(ui, "Kind", KIND_COL_WIDTH);
        header_cell(ui, "Value", VALUE_COL_WIDTH);
        header_cell(ui, "Weight", WEIGHT_COL_WIDTH);
        header_cell(ui, "Status / Source", SOURCE_COL_WIDTH);
    });
    ui.separator();
}

fn tree_node(
    ui: &mut egui::Ui,
    root: &InvestableNode,
    node: &InvestableNode,
    base_currency: &str,
    state: &mut HierarchyState,
    action: &mut Option<HierarchyAction>,
    depth: usize,
) {
    let selected = state.selected_node_id.as_deref() == Some(node.id.as_str());

    if node.children.is_empty() {
        let response = ui
            .horizontal(|ui| {
                ui.add_space(ui.spacing().indent);
                node_row(ui, node, base_currency, selected)
            })
            .inner;
        handle_node_response(response, root, node, state, action);
        return;
    }

    let id = ui.make_persistent_id(("hierarchy_tree_node", node.id.as_str()));
    let header =
        egui::collapsing_header::CollapsingState::load_with_default_open(ui.ctx(), id, depth < 2)
            .show_header(ui, |ui| node_row(ui, node, base_currency, selected));

    let (_, header_response, _) = header.body(|ui| {
        for child in &node.children {
            tree_node(ui, root, child, base_currency, state, action, depth + 1);
        }
    });

    handle_node_response(header_response.inner, root, node, state, action);
}

fn node_row(
    ui: &mut egui::Ui,
    node: &InvestableNode,
    base_currency: &str,
    selected: bool,
) -> egui::Response {
    let response = ui
        .add_sized(
            [NODE_COL_WIDTH, metrics::ROW_HEIGHT_COMPACT],
            egui::Button::selectable(selected, node_label(node)).truncate(),
        )
        .on_hover_text(format!(
            "id: {}\nsubject: {}",
            node.id,
            node.subject.kind_label()
        ));
    fixed_label(ui, node.kind.as_str(), KIND_COL_WIDTH);
    fixed_label(ui, format_node_value(node, base_currency), VALUE_COL_WIDTH);
    fixed_label(
        ui,
        node.weight_pct
            .map(format_pct)
            .unwrap_or_else(|| "-".to_owned()),
        WEIGHT_COL_WIDTH,
    );
    fixed_label(
        ui,
        format!("{} | {}", node.status, format_source(&node.source)),
        SOURCE_COL_WIDTH,
    );
    response
}

fn handle_node_response(
    response: egui::Response,
    root: &InvestableNode,
    node: &InvestableNode,
    state: &mut HierarchyState,
    action: &mut Option<HierarchyAction>,
) {
    if response.clicked() {
        state.selected_node_id = Some(node.id.clone());
    }
    if response.double_clicked() {
        state.selected_node_id = Some(node.id.clone());
        *action = Some(open_action(root, node));
    }
    response.context_menu(|ui| {
        if ui.button("Open").clicked() {
            *action = Some(open_action(root, node));
            ui.close();
        }
        if ui.button("Plot Value").clicked() {
            *action = Some(HierarchyAction::Plot(plot_request_for_node(node, true)));
            ui.close();
        }
        if ui.button("Plot Price").clicked() {
            *action = Some(HierarchyAction::Plot(plot_request_for_node(node, false)));
            ui.close();
        }
    });
}

fn selected_panel(
    ui: &mut egui::Ui,
    root: &InvestableNode,
    base_currency: &str,
    state: &HierarchyState,
    action: &mut Option<HierarchyAction>,
) {
    ui.set_min_width(280.0);
    ui.label(egui::RichText::new("Selected node").strong());
    let selected_id = state
        .selected_node_id
        .as_deref()
        .unwrap_or(root.id.as_str());
    let Some(node) = root.find(selected_id) else {
        ui.label("Selected node not found.");
        return;
    };

    egui::Grid::new("hierarchy_selected_node_grid")
        .num_columns(2)
        .striped(true)
        .show(ui, |ui| {
            ui.label("Label");
            ui.label(&node.label);
            ui.end_row();
            ui.label("Kind");
            ui.label(node.kind.as_str());
            ui.end_row();
            ui.label("Ticker");
            ui.monospace(node.ticker.as_deref().unwrap_or("-"));
            ui.end_row();
            ui.label("ISIN");
            ui.monospace(node.isin.as_deref().unwrap_or("-"));
            ui.end_row();
            ui.label("Value");
            ui.monospace(format_node_value(node, base_currency));
            ui.end_row();
            ui.label("Effective value");
            ui.monospace(format_money(base_currency, effective_node_value(node)));
            ui.end_row();
            ui.label("Weight");
            ui.monospace(
                node.weight_pct
                    .map(format_pct)
                    .unwrap_or_else(|| "-".to_owned()),
            );
            ui.end_row();
            ui.label("Status");
            ui.label(&node.status);
            ui.end_row();
            ui.label("Source");
            ui.monospace(format_source(&node.source));
            ui.end_row();
        });

    ui.separator();
    ui.horizontal_wrapped(|ui| {
        if ui.button("Open").clicked() {
            *action = Some(open_action(root, node));
        }
        if ui.button("Plot Value").clicked() {
            *action = Some(HierarchyAction::Plot(plot_request_for_node(node, true)));
        }
        if ui.button("Plot Price").clicked() {
            *action = Some(HierarchyAction::Plot(plot_request_for_node(node, false)));
        }
        if ui.button("Compare").clicked() {
            *action = Some(open_action(root, node));
        }
        if ui.button("Explain").clicked() {
            *action = Some(open_action(root, node));
        }
    });

    if !node.children.is_empty() {
        ui.separator();
        ui.monospace(format!("Children: {}", node.children.len()));
        for (child_id, weight) in child_effective_weights(node).iter().take(4) {
            if let Some(child) = node.find(child_id) {
                ui.monospace(format!("{} eff wt {}", child.label, format_pct(*weight)));
            }
        }
    }
}

fn open_action(root: &InvestableNode, node: &InvestableNode) -> HierarchyAction {
    HierarchyAction::Open {
        subject: node.subject.clone(),
        label: node_label(node),
        page: page_for_node(node),
        breadcrumbs: root
            .path_labels(&node.id)
            .unwrap_or_else(|| vec![node.label.clone()]),
    }
}

fn plot_request_for_node(node: &InvestableNode, value_plot: bool) -> PlotRequest {
    let kind = if value_plot {
        match node.kind {
            InvestableKind::Portfolio => TimeSeriesKind::PortfolioValue,
            InvestableKind::Fund | InvestableKind::Listing | InvestableKind::Holding => {
                TimeSeriesKind::MarketValue
            }
            InvestableKind::Cash => TimeSeriesKind::FxRate,
            InvestableKind::Synthetic => TimeSeriesKind::MarketValue,
        }
    } else {
        TimeSeriesKind::Price
    };

    PlotRequest::new(
        node.subject.clone(),
        kind,
        format!("{} {}", node.label, kind.as_str()),
    )
}

fn page_for_node(node: &InvestableNode) -> Page {
    match &node.subject {
        AnalysisSubject::WorkspacePortfolio(_) => Page::Portfolio,
        AnalysisSubject::Fund(_) | AnalysisSubject::FundListing { .. } => Page::FundDetail,
        AnalysisSubject::Holding { .. } => Page::Holdings,
        AnalysisSubject::Cash(_) | AnalysisSubject::SyntheticModel(_) => Page::Hierarchy,
    }
}

fn node_label(node: &InvestableNode) -> String {
    node.ticker
        .as_ref()
        .map(|ticker| format!("{ticker} | {}", node.label))
        .unwrap_or_else(|| node.label.clone())
}

fn format_node_value(node: &InvestableNode, base_currency: &str) -> String {
    let currency = node.currency.as_deref().unwrap_or(base_currency);
    node.value
        .map(|value| format_money(currency, value))
        .unwrap_or_else(|| "-".to_owned())
}

fn header_cell(ui: &mut egui::Ui, value: &str, width: f32) {
    ui.add_sized(
        [width, metrics::ROW_HEIGHT_COMPACT],
        egui::Label::new(egui::RichText::new(value).strong()).truncate(),
    );
}

fn fixed_label(ui: &mut egui::Ui, value: impl Into<String>, width: f32) {
    ui.add_sized(
        [width, metrics::ROW_HEIGHT_COMPACT],
        egui::Label::new(value.into()).truncate(),
    );
}
