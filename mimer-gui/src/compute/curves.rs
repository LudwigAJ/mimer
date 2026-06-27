#[derive(Clone, Debug)]
pub struct CurvePoint {
    pub tenor: String,
    pub years: f64,
    pub value_pct: f64,
    pub discount_factor: f64,
    pub par_rate_pct: f64,
    pub spread_bps: f64,
    pub source: String,
    pub status: String,
}

pub fn mock_curve(currency: &str, curve_type: &str) -> Vec<CurvePoint> {
    let shift = match (currency, curve_type) {
        ("GBP", "zero") => 0.0,
        ("GBP", _) => -0.08,
        ("USD", "zero") => 0.35,
        ("USD", _) => 0.22,
        _ => 0.1,
    };

    [
        ("1M", 1.0 / 12.0, 4.55),
        ("3M", 0.25, 4.48),
        ("6M", 0.5, 4.32),
        ("1Y", 1.0, 4.05),
        ("2Y", 2.0, 3.82),
        ("5Y", 5.0, 3.71),
        ("10Y", 10.0, 3.86),
        ("30Y", 30.0, 4.28),
    ]
    .into_iter()
    .map(|(tenor, years, value_pct)| {
        let zero_rate = value_pct + shift;
        CurvePoint {
            tenor: tenor.to_owned(),
            years,
            value_pct: zero_rate,
            discount_factor: 1.0 / (1.0 + zero_rate / 100.0).powf(years),
            par_rate_pct: zero_rate + 0.06,
            spread_bps: (zero_rate - 3.75) * 100.0,
            source: "mock".to_owned(),
            status: "SEED".to_owned(),
        }
    })
    .collect()
}
