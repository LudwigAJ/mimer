#[derive(Clone, Debug)]
pub struct NavEstimate {
    pub gross_assets: f64,
    pub liabilities: f64,
    pub shares_outstanding: f64,
    pub nav_per_share: f64,
}

pub fn estimate_nav(gross_assets: f64, liabilities: f64, shares_outstanding: f64) -> NavEstimate {
    let nav_per_share = if shares_outstanding <= 0.0 {
        0.0
    } else {
        (gross_assets - liabilities) / shares_outstanding
    };

    NavEstimate {
        gross_assets,
        liabilities,
        shares_outstanding,
        nav_per_share,
    }
}
