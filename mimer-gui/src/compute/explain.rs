use crate::domain::{ComputedValue, Position, ValueDependency};
use crate::format::{fmt_decimal, fmt_money};

pub fn market_value_trace(
    position: &Position,
    base_currency: &str,
    manual_override_count: usize,
) -> ComputedValue {
    let mut notes = vec![format!(
        "Market Value = Units x Price = {} x {}",
        fmt_decimal(position.units, 4),
        fmt_money(&position.listing_currency, position.price)
    )];

    if manual_override_count > 0 || position.source.eq_ignore_ascii_case("manual") {
        notes.push("Manual override affects this row and dependent portfolio totals.".to_owned());
    }

    ComputedValue {
        label: format!("{} market value", position.ticker),
        value: position.market_value,
        unit: base_currency.to_owned(),
        status: position.freshness.as_str().to_owned(),
        source: position.source.clone(),
        dependencies: vec![
            ValueDependency {
                entity_type: "position".to_owned(),
                entity_id: position.listing_id.clone(),
                field_name: "units".to_owned(),
                value_used: fmt_decimal(position.units, 4),
                source: if position.source.eq_ignore_ascii_case("manual") {
                    "manual".to_owned()
                } else {
                    "portfolio".to_owned()
                },
                status: if position.source.eq_ignore_ascii_case("manual") {
                    "MANUAL".to_owned()
                } else {
                    "SEED".to_owned()
                },
            },
            ValueDependency {
                entity_type: "listing".to_owned(),
                entity_id: position.listing_id.clone(),
                field_name: "price".to_owned(),
                value_used: fmt_money(&position.listing_currency, position.price),
                source: position.source.clone(),
                status: position.freshness.as_str().to_owned(),
            },
        ],
        notes,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::DataFreshness;

    fn position() -> Position {
        Position {
            fund_id: "fund-vusa".to_owned(),
            listing_id: "listing-vusa".to_owned(),
            ticker: "VUSA".to_owned(),
            name: "Vanguard S&P 500 UCITS ETF".to_owned(),
            isin: "IE00B3XXRP09".to_owned(),
            listing_currency: "GBP".to_owned(),
            units: 10.0,
            price: 92.0,
            daily_change: 0.0,
            market_value: 920.0,
            portfolio_weight_pct: 100.0,
            trailing_yield_pct: 1.0,
            projected_income: 9.2,
            freshness: DataFreshness::Fresh,
            source: "manual".to_owned(),
        }
    }

    #[test]
    fn builds_market_value_dependency_trace() {
        let trace = market_value_trace(&position(), "GBP", 1);

        assert_eq!(trace.value, 920.0);
        assert_eq!(trace.dependencies.len(), 2);
        assert_eq!(trace.dependencies[0].field_name, "units");
        assert!(
            trace
                .notes
                .iter()
                .any(|note| note.contains("Manual override"))
        );
    }
}
