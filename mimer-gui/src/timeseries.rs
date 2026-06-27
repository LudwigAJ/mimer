use crate::domain::AnalysisSubject;

#[derive(Clone, Debug, PartialEq)]
pub struct TimeSeriesPoint {
    pub date: String,
    pub value: f64,
    pub status: String,
    pub source: String,
}

impl TimeSeriesPoint {
    pub fn new(
        date: impl Into<String>,
        value: f64,
        status: impl Into<String>,
        source: impl Into<String>,
    ) -> Self {
        Self {
            date: date.into(),
            value,
            status: status.into(),
            source: source.into(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[allow(dead_code)]
pub enum TimeSeriesKind {
    Price,
    Nav,
    MarketValue,
    Distribution,
    Yield,
    FxRate,
    PortfolioValue,
    PortfolioPnL,
    ProjectedIncome,
    CurvePoint,
    Custom,
}

impl TimeSeriesKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Price => "Price",
            Self::Nav => "NAV",
            Self::MarketValue => "Market value",
            Self::Distribution => "Distribution",
            Self::Yield => "Yield",
            Self::FxRate => "FX rate",
            Self::PortfolioValue => "Portfolio value",
            Self::PortfolioPnL => "Portfolio PnL",
            Self::ProjectedIncome => "Projected income",
            Self::CurvePoint => "Curve point",
            Self::Custom => "Custom",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TimeSeries {
    pub id: String,
    pub subject: AnalysisSubject,
    pub label: String,
    pub kind: TimeSeriesKind,
    pub unit: String,
    pub points: Vec<TimeSeriesPoint>,
    pub source: String,
    pub status: String,
}

impl TimeSeries {
    pub fn new(
        id: impl Into<String>,
        subject: AnalysisSubject,
        label: impl Into<String>,
        kind: TimeSeriesKind,
        unit: impl Into<String>,
        points: Vec<TimeSeriesPoint>,
    ) -> Self {
        Self {
            id: id.into(),
            subject,
            label: label.into(),
            kind,
            unit: unit.into(),
            points,
            source: "mock".to_owned(),
            status: "SEED".to_owned(),
        }
    }

    pub fn with_source(mut self, source: impl Into<String>) -> Self {
        self.source = source.into();
        self
    }

    pub fn with_status(mut self, status: impl Into<String>) -> Self {
        self.status = status.into();
        self
    }
}

pub fn find_series<'a>(
    series: &'a [TimeSeries],
    subject: &AnalysisSubject,
    kind: TimeSeriesKind,
) -> Option<&'a TimeSeries> {
    series
        .iter()
        .find(|candidate| candidate.subject == *subject && candidate.kind == kind)
}

#[allow(dead_code)]
pub fn subject_has_series(
    series: &[TimeSeries],
    subject: &AnalysisSubject,
    kind: TimeSeriesKind,
) -> bool {
    find_series(series, subject, kind).is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_series_by_subject_and_kind() {
        let subject = AnalysisSubject::FundListing {
            fund_id: "fund-vusa".to_owned(),
            listing_id: "listing-vusa".to_owned(),
        };
        let series = vec![TimeSeries::new(
            "vusa-price",
            subject.clone(),
            "VUSA price",
            TimeSeriesKind::Price,
            "GBP",
            vec![TimeSeriesPoint::new("2026-06-20", 92.0, "FRESH", "seed")],
        )];

        assert!(find_series(&series, &subject, TimeSeriesKind::Price).is_some());
        assert!(find_series(&series, &subject, TimeSeriesKind::Nav).is_none());
    }
}
