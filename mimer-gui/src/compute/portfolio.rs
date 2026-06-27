use crate::domain::{
    AnalysisSubject, DataFreshness, Fund, HoldingExposure, InvestableKind, InvestableNode,
    PortfolioSummary, Position, Workspace,
};
use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PositionOverrideField {
    Units,
    Price,
    TrailingYieldPct,
}

impl PositionOverrideField {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Units => "units",
            Self::Price => "price",
            Self::TrailingYieldPct => "trailing_yield_pct",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PositionOverride {
    pub listing_id: String,
    pub field: PositionOverrideField,
    pub value: f64,
}

impl PositionOverride {
    pub fn new(listing_id: impl Into<String>, field: PositionOverrideField, value: f64) -> Self {
        Self {
            listing_id: listing_id.into(),
            field,
            value,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum PositionOverrideError {
    InvalidValue {
        field: PositionOverrideField,
        value: f64,
    },
    UnknownListing(String),
}

impl fmt::Display for PositionOverrideError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidValue { field, value } => {
                write!(f, "invalid {} override value {value}", field.as_str())
            }
            Self::UnknownListing(listing_id) => write!(f, "unknown listing {listing_id}"),
        }
    }
}

impl std::error::Error for PositionOverrideError {}

pub fn calculate_summary(positions: &[Position], base_currency: &str) -> PortfolioSummary {
    let total_value = positions.iter().map(|position| position.market_value).sum();
    let projected_annual_income = positions
        .iter()
        .map(|position| position.projected_income)
        .sum();
    let trailing_12m_income = projected_annual_income * 0.94;
    let stale_warning_count = positions
        .iter()
        .filter(|position| position.freshness != DataFreshness::Fresh)
        .count();

    PortfolioSummary {
        total_value,
        daily_change: total_value * 0.004,
        unrealised_gain_loss: total_value * 0.124,
        trailing_12m_income,
        projected_annual_income,
        base_currency: base_currency.to_owned(),
        position_count: positions.len(),
        stale_warning_count,
    }
}

pub fn projected_income_yield(summary: &PortfolioSummary) -> f64 {
    if summary.total_value == 0.0 {
        0.0
    } else {
        summary.projected_annual_income / summary.total_value * 100.0
    }
}

pub fn apply_position_override(
    positions: &mut [Position],
    base_currency: &str,
    listing_id: &str,
    field: PositionOverrideField,
    value: f64,
) -> Result<PortfolioSummary, PositionOverrideError> {
    validate_override_value(field, value)?;

    let Some(position) = positions
        .iter_mut()
        .find(|position| position.listing_id == listing_id)
    else {
        return Err(PositionOverrideError::UnknownListing(listing_id.to_owned()));
    };

    apply_position_field(position, field, value);
    Ok(recompute_positions(positions, base_currency))
}

pub fn apply_position_overrides(
    positions: &mut [Position],
    base_currency: &str,
    overrides: &[PositionOverride],
) -> Result<PortfolioSummary, PositionOverrideError> {
    for position_override in overrides {
        validate_override_value(position_override.field, position_override.value)?;
        let Some(position) = positions
            .iter_mut()
            .find(|position| position.listing_id == position_override.listing_id)
        else {
            return Err(PositionOverrideError::UnknownListing(
                position_override.listing_id.clone(),
            ));
        };
        apply_position_field(position, position_override.field, position_override.value);
    }

    Ok(recompute_positions(positions, base_currency))
}

pub fn recompute_positions(positions: &mut [Position], base_currency: &str) -> PortfolioSummary {
    for position in positions.iter_mut() {
        position.market_value = position.units * position.price;
        position.projected_income = position.market_value * position.trailing_yield_pct / 100.0;
    }

    let total_value = positions
        .iter()
        .map(|position| position.market_value)
        .sum::<f64>();
    for position in positions.iter_mut() {
        position.portfolio_weight_pct = if total_value == 0.0 {
            0.0
        } else {
            position.market_value / total_value * 100.0
        };
    }

    calculate_summary(positions, base_currency)
}

pub fn build_investable_tree(
    workspace: &Workspace,
    summary: &PortfolioSummary,
    positions: &[Position],
    funds: &[Fund],
    holdings: &[HoldingExposure],
) -> InvestableNode {
    let mut root = InvestableNode::new(
        format!("portfolio-{}", workspace.id),
        AnalysisSubject::WorkspacePortfolio(workspace.id.clone()),
        workspace.name.clone(),
        InvestableKind::Portfolio,
    )
    .with_currency(summary.base_currency.clone())
    .with_value(summary.total_value)
    .with_weight_pct(100.0)
    .with_status(if summary.stale_warning_count > 0 {
        "STALE"
    } else {
        "FRESH"
    })
    .with_source("seed");

    let mut core = portfolio_bucket(
        "portfolio-core-equity",
        "Core Equity Portfolio",
        workspace,
        summary,
        positions
            .iter()
            .filter(|position| matches!(position.ticker.as_str(), "VUSA" | "ISF")),
    );
    let mut income = portfolio_bucket(
        "portfolio-income",
        "Income Portfolio",
        workspace,
        summary,
        positions
            .iter()
            .filter(|position| !matches!(position.ticker.as_str(), "VUSA" | "ISF")),
    );

    for position in positions {
        let node = listing_node(position, funds, holdings);
        if matches!(position.ticker.as_str(), "VUSA" | "ISF") {
            core.push_child(node)
                .expect("portfolio tree position ids should be unique");
        } else {
            income
                .push_child(node)
                .expect("portfolio tree position ids should be unique");
        }
    }

    root.push_child(core)
        .expect("portfolio tree bucket ids should be unique");
    root.push_child(income)
        .expect("portfolio tree bucket ids should be unique");
    root
}

pub fn effective_node_value(node: &InvestableNode) -> f64 {
    node.value
        .unwrap_or_else(|| node.children.iter().map(effective_node_value).sum::<f64>())
}

pub fn child_effective_weights(node: &InvestableNode) -> Vec<(String, f64)> {
    let total = effective_node_value(node);
    node.children
        .iter()
        .map(|child| {
            let value = effective_node_value(child);
            let weight = if total == 0.0 {
                0.0
            } else {
                value / total * 100.0
            };
            (child.id.clone(), weight)
        })
        .collect()
}

fn validate_override_value(
    field: PositionOverrideField,
    value: f64,
) -> Result<(), PositionOverrideError> {
    if value.is_finite() && value >= 0.0 {
        Ok(())
    } else {
        Err(PositionOverrideError::InvalidValue { field, value })
    }
}

fn apply_position_field(position: &mut Position, field: PositionOverrideField, value: f64) {
    match field {
        PositionOverrideField::Units => position.units = value,
        PositionOverrideField::Price => position.price = value,
        PositionOverrideField::TrailingYieldPct => position.trailing_yield_pct = value,
    }
    position.source = "manual".to_owned();
}

fn portfolio_bucket<'a>(
    id: &str,
    label: &str,
    workspace: &Workspace,
    summary: &PortfolioSummary,
    positions: impl Iterator<Item = &'a Position>,
) -> InvestableNode {
    let positions = positions.collect::<Vec<_>>();
    let value = positions
        .iter()
        .map(|position| position.market_value)
        .sum::<f64>();
    let stale_count = positions
        .iter()
        .filter(|position| position.freshness != DataFreshness::Fresh)
        .count();
    let weight = if summary.total_value == 0.0 {
        0.0
    } else {
        value / summary.total_value * 100.0
    };

    InvestableNode::new(
        id,
        AnalysisSubject::SyntheticModel(id.to_owned()),
        label,
        InvestableKind::Portfolio,
    )
    .with_currency(workspace.base_currency.clone())
    .with_value(value)
    .with_weight_pct(weight)
    .with_status(if stale_count > 0 { "STALE" } else { "FRESH" })
    .with_source("seed")
}

fn listing_node(
    position: &Position,
    funds: &[Fund],
    holdings: &[HoldingExposure],
) -> InvestableNode {
    let fund = funds.iter().find(|fund| fund.id == position.fund_id);
    let mut node = InvestableNode::new(
        format!("listing-{}", position.listing_id),
        AnalysisSubject::FundListing {
            fund_id: position.fund_id.clone(),
            listing_id: position.listing_id.clone(),
        },
        position.ticker.clone(),
        InvestableKind::Listing,
    )
    .with_ticker(position.ticker.clone())
    .with_isin(position.isin.clone())
    .with_currency(position.listing_currency.clone())
    .with_value(position.market_value)
    .with_weight_pct(position.portfolio_weight_pct)
    .with_status(position.freshness.as_str())
    .with_source(position.source.clone());

    if let Some(fund) = fund {
        node.label = format!("{} | {}", position.ticker, fund.name);
    }

    for holding in holdings
        .iter()
        .filter(|holding| holding.source_etf == position.ticker)
        .take(8)
    {
        let holding_value = position.market_value * holding.weight_pct / 100.0;
        node.push_child(
            InvestableNode::new(
                format!("holding-{}-{}", position.ticker, holding.ticker),
                AnalysisSubject::Holding {
                    ticker: holding.ticker.clone(),
                    source: position.ticker.clone(),
                },
                holding.company.clone(),
                InvestableKind::Holding,
            )
            .with_ticker(holding.ticker.clone())
            .with_currency(position.listing_currency.clone())
            .with_value(holding_value)
            .with_weight_pct(holding.weight_pct)
            .with_status("SEED")
            .with_source(holding.source.clone()),
        )
        .expect("portfolio tree holding ids should be unique");
    }

    node
}

#[cfg(test)]
mod tests {
    use super::*;

    fn position(value: f64, income: f64, freshness: DataFreshness) -> Position {
        Position {
            fund_id: "fund".to_owned(),
            listing_id: "listing".to_owned(),
            ticker: "TST".to_owned(),
            name: "Test".to_owned(),
            isin: "IE000TEST".to_owned(),
            listing_currency: "GBP".to_owned(),
            units: 1.0,
            price: value,
            daily_change: 0.0,
            market_value: value,
            portfolio_weight_pct: 0.0,
            trailing_yield_pct: 0.0,
            projected_income: income,
            freshness,
            source: "test".to_owned(),
        }
    }

    #[test]
    fn calculates_summary_totals() {
        let positions = vec![
            position(100.0, 4.0, DataFreshness::Fresh),
            position(50.0, 2.0, DataFreshness::Stale),
        ];
        let summary = calculate_summary(&positions, "GBP");

        assert_eq!(summary.total_value, 150.0);
        assert_eq!(summary.projected_annual_income, 6.0);
        assert_eq!(summary.position_count, 2);
        assert_eq!(summary.stale_warning_count, 1);
        assert_eq!(projected_income_yield(&summary), 4.0);
    }

    #[test]
    fn applies_price_override_and_recomputes_dependent_values() {
        let mut positions = vec![position(100.0, 4.0, DataFreshness::Fresh)];

        let summary = apply_position_override(
            &mut positions,
            "GBP",
            "listing",
            PositionOverrideField::Price,
            120.0,
        )
        .expect("override should apply");

        assert_eq!(positions[0].market_value, 120.0);
        assert_eq!(positions[0].projected_income, 0.0);
        assert_eq!(positions[0].portfolio_weight_pct, 100.0);
        assert_eq!(positions[0].source, "manual");
        assert_eq!(summary.total_value, 120.0);
    }

    #[test]
    fn applies_stored_overrides_in_order() {
        let mut positions = vec![position(100.0, 4.0, DataFreshness::Fresh)];
        let overrides = vec![
            PositionOverride::new("listing", PositionOverrideField::Units, 2.0),
            PositionOverride::new("listing", PositionOverrideField::TrailingYieldPct, 5.0),
        ];

        let summary = apply_position_overrides(&mut positions, "GBP", &overrides)
            .expect("overrides should apply");

        assert_eq!(positions[0].market_value, 200.0);
        assert_eq!(positions[0].projected_income, 10.0);
        assert_eq!(summary.projected_annual_income, 10.0);
    }

    #[test]
    fn rejects_invalid_override_values() {
        let mut positions = vec![position(100.0, 4.0, DataFreshness::Fresh)];

        let err = apply_position_override(
            &mut positions,
            "GBP",
            "listing",
            PositionOverrideField::Units,
            -1.0,
        )
        .expect_err("negative units should be rejected");

        assert_eq!(
            err,
            PositionOverrideError::InvalidValue {
                field: PositionOverrideField::Units,
                value: -1.0
            }
        );
    }

    #[test]
    fn sums_effective_values_for_nested_nodes_without_parent_value() {
        let mut parent = InvestableNode::new(
            "parent",
            AnalysisSubject::SyntheticModel("parent".to_owned()),
            "Parent",
            InvestableKind::Portfolio,
        );
        parent
            .push_child(
                InvestableNode::new(
                    "child-a",
                    AnalysisSubject::SyntheticModel("child-a".to_owned()),
                    "Child A",
                    InvestableKind::Synthetic,
                )
                .with_value(40.0),
            )
            .expect("child should insert");
        parent
            .push_child(
                InvestableNode::new(
                    "child-b",
                    AnalysisSubject::SyntheticModel("child-b".to_owned()),
                    "Child B",
                    InvestableKind::Synthetic,
                )
                .with_value(60.0),
            )
            .expect("child should insert");

        assert_eq!(effective_node_value(&parent), 100.0);
        assert_eq!(
            child_effective_weights(&parent),
            vec![("child-a".to_owned(), 40.0), ("child-b".to_owned(), 60.0)]
        );
    }

    #[test]
    fn builds_tree_from_effective_position_values() {
        let workspace = Workspace {
            id: "workspace-main".to_owned(),
            name: "Main Portfolio".to_owned(),
            base_currency: "GBP".to_owned(),
        };
        let mut positions = vec![position(100.0, 4.0, DataFreshness::Fresh)];
        positions[0].fund_id = "fund-vusa".to_owned();
        positions[0].listing_id = "listing-vusa".to_owned();
        positions[0].ticker = "VUSA".to_owned();
        positions[0].trailing_yield_pct = 4.0;
        let summary = apply_position_override(
            &mut positions,
            "GBP",
            "listing-vusa",
            PositionOverrideField::Price,
            120.0,
        )
        .expect("override should apply");

        let tree = build_investable_tree(&workspace, &summary, &positions, &[], &[]);
        let node = tree
            .find("listing-listing-vusa")
            .expect("tree should include overridden listing");

        assert_eq!(node.value, Some(120.0));
        assert_eq!(node.source, "manual");
    }
}
