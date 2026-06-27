#[derive(Clone, Debug)]
pub struct RegressionRow {
    pub target: String,
    pub factor: String,
    pub beta: f64,
    pub t_stat: f64,
    pub r_squared: f64,
    pub window: String,
    pub status: String,
}

pub fn mock_regressions() -> Vec<RegressionRow> {
    vec![
        RegressionRow {
            target: "VUSA".to_owned(),
            factor: "S&P 500".to_owned(),
            beta: 0.98,
            t_stat: 42.1,
            r_squared: 0.96,
            window: "3Y weekly".to_owned(),
            status: "Mock".to_owned(),
        },
        RegressionRow {
            target: "JEGP".to_owned(),
            factor: "MSCI World".to_owned(),
            beta: 0.63,
            t_stat: 14.7,
            r_squared: 0.71,
            window: "2Y weekly".to_owned(),
            status: "Mock".to_owned(),
        },
    ]
}
