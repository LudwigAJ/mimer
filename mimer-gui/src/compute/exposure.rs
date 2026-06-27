use std::collections::BTreeMap;

use crate::domain::{ExposureSlice, HoldingExposure};

pub fn aggregate_by_country(holdings: &[HoldingExposure]) -> Vec<ExposureSlice> {
    aggregate_by(holdings, |holding| holding.country.as_str())
}

pub fn aggregate_by_sector(holdings: &[HoldingExposure]) -> Vec<ExposureSlice> {
    aggregate_by(holdings, |holding| holding.sector.as_str())
}

fn aggregate_by(
    holdings: &[HoldingExposure],
    key: impl Fn(&HoldingExposure) -> &str,
) -> Vec<ExposureSlice> {
    let mut totals = BTreeMap::<String, f64>::new();
    for holding in holdings {
        *totals.entry(key(holding).to_owned()).or_insert(0.0) += holding.weight_pct;
    }

    let mut slices = totals
        .into_iter()
        .map(|(label, value_pct)| ExposureSlice { label, value_pct })
        .collect::<Vec<_>>();
    slices.sort_by(|left, right| right.value_pct.total_cmp(&left.value_pct));
    slices
}

#[cfg(test)]
mod tests {
    use super::*;

    fn holding(country: &str, sector: &str, weight_pct: f64) -> HoldingExposure {
        HoldingExposure {
            company: "Test".to_owned(),
            ticker: "TST".to_owned(),
            country: country.to_owned(),
            sector: sector.to_owned(),
            weight_pct,
            change_since_previous_pct: None,
            source_etf: "VUSA".to_owned(),
            as_of_date: "2026-06-20".to_owned(),
            source: "test".to_owned(),
        }
    }

    #[test]
    fn aggregates_country_exposure() {
        let rows = vec![
            holding("US", "Tech", 2.0),
            holding("US", "Tech", 3.0),
            holding("UK", "Financials", 1.0),
        ];

        let countries = aggregate_by_country(&rows);
        assert_eq!(countries[0].label, "US");
        assert_eq!(countries[0].value_pct, 5.0);
        assert_eq!(countries[1].label, "UK");
    }
}
