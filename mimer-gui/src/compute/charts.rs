use std::collections::BTreeMap;

use crate::domain::AnalysisSubject;
use crate::timeseries::{TimeSeries, TimeSeriesKind, TimeSeriesPoint};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ChartValueMode {
    #[default]
    Raw,
    Rebased100,
    PercentChange,
}

impl ChartValueMode {
    pub const ALL: [Self; 3] = [Self::Raw, Self::Rebased100, Self::PercentChange];

    pub fn label(self) -> &'static str {
        match self {
            Self::Raw => "Raw",
            Self::Rebased100 => "Rebased 100",
            Self::PercentChange => "% Change",
        }
    }

    pub fn compact_label(self) -> &'static str {
        match self {
            Self::Raw => "RAW",
            Self::Rebased100 => "REBASED 100",
            Self::PercentChange => "% CHANGE",
        }
    }

    pub fn is_transformed(self) -> bool {
        self != Self::Raw
    }

    pub fn display_unit(self, raw_unit: &str) -> String {
        match self {
            Self::Raw => raw_unit.to_owned(),
            Self::Rebased100 => "index".to_owned(),
            Self::PercentChange => "%".to_owned(),
        }
    }

    pub fn tooltip(self) -> &'static str {
        match self {
            Self::Raw => "Raw source values are shown without a display transform.",
            Self::Rebased100 => {
                "Displayed values are rebased from the first valid common point: value / base * 100."
            }
            Self::PercentChange => {
                "Displayed values are percent change from the first valid common point."
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DisplayPoint {
    pub date: String,
    pub value: f64,
    pub raw_value: f64,
    pub status: String,
    pub source: String,
}

impl DisplayPoint {
    fn raw(point: &TimeSeriesPoint) -> Self {
        Self {
            date: point.date.clone(),
            value: point.value,
            raw_value: point.value,
            status: point.status.clone(),
            source: point.source.clone(),
        }
    }
}

pub fn first_valid_common_base_date(series_points: &[&[TimeSeriesPoint]]) -> Option<String> {
    match series_points {
        [] => None,
        [single] => single
            .iter()
            .find(|point| is_valid_base_value(point.value))
            .map(|point| point.date.clone()),
        [first, rest @ ..] => first.iter().find_map(|candidate| {
            if !is_valid_base_value(candidate.value) {
                return None;
            }
            let date = candidate.date.as_str();
            let present_for_all = rest.iter().all(|points| {
                points
                    .iter()
                    .any(|point| point.date == date && is_valid_base_value(point.value))
            });
            present_for_all.then(|| candidate.date.clone())
        }),
    }
}

pub fn display_points_for_mode(
    points: &[TimeSeriesPoint],
    mode: ChartValueMode,
    base_date: Option<&str>,
) -> Vec<DisplayPoint> {
    if mode == ChartValueMode::Raw {
        return points.iter().map(DisplayPoint::raw).collect();
    }

    let Some(base_date) = base_date else {
        return Vec::new();
    };
    let Some(base) = points
        .iter()
        .find(|point| point.date == base_date && is_valid_base_value(point.value))
        .map(|point| point.value)
    else {
        return Vec::new();
    };

    points
        .iter()
        .filter(|point| point.date.as_str() >= base_date)
        .map(|point| {
            let value = match mode {
                ChartValueMode::Raw => point.value,
                ChartValueMode::Rebased100 => point.value / base * 100.0,
                ChartValueMode::PercentChange => point.value / base - 1.0,
            };
            DisplayPoint {
                date: point.date.clone(),
                value,
                raw_value: point.value,
                status: point.status.clone(),
                source: point.source.clone(),
            }
        })
        .collect()
}

fn is_valid_base_value(value: f64) -> bool {
    value.is_finite() && value.abs() > f64::EPSILON
}

#[derive(Clone, Debug, PartialEq)]
pub struct DerivedSpreadSeries {
    pub series: TimeSeries,
    pub left_points: usize,
    pub right_points: usize,
    pub common_points: usize,
    pub left_source: String,
    pub right_source: String,
    pub partial: bool,
}

pub fn derive_matching_date_spread(
    left: &TimeSeries,
    right: &TimeSeries,
    subject: AnalysisSubject,
    label: impl Into<String>,
    kind: TimeSeriesKind,
) -> DerivedSpreadSeries {
    let right_by_date = right
        .points
        .iter()
        .map(|point| (point.date.as_str(), point))
        .collect::<BTreeMap<_, _>>();
    let mut points = left
        .points
        .iter()
        .filter_map(|left_point| {
            let right_point = right_by_date.get(left_point.date.as_str())?;
            Some(TimeSeriesPoint::new(
                left_point.date.clone(),
                left_point.value - right_point.value,
                "DERIVED",
                "derived",
            ))
        })
        .collect::<Vec<_>>();
    points.sort_by(|left, right| left.date.cmp(&right.date));

    let common_points = points.len();
    let left_points = left.points.len();
    let right_points = right.points.len();
    let partial = common_points < left_points || common_points < right_points;
    let status = if common_points == 0 {
        "MISSING"
    } else if partial {
        "PARTIAL"
    } else {
        "DERIVED"
    };
    let unit = if left.unit == right.unit {
        left.unit.clone()
    } else {
        format!("{}-{}", left.unit, right.unit)
    };

    DerivedSpreadSeries {
        series: TimeSeries::new(
            format!("spread:{}:{}", left.id, right.id),
            subject,
            label,
            kind,
            unit,
            points,
        )
        .with_source("derived")
        .with_status(status),
        left_points,
        right_points,
        common_points,
        left_source: left.source.clone(),
        right_source: right.source.clone(),
        partial,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn series(id: &str, values: &[(&str, f64)]) -> TimeSeries {
        TimeSeries::new(
            id,
            AnalysisSubject::Fund(id.to_owned()),
            id,
            TimeSeriesKind::Price,
            "GBP",
            values
                .iter()
                .map(|(date, value)| TimeSeriesPoint::new(*date, *value, "FRESH", "seed"))
                .collect(),
        )
        .with_source("seed")
        .with_status("FRESH")
    }

    #[test]
    fn spread_uses_only_matching_dates() {
        let left = series(
            "left",
            &[
                ("2026-06-18", 10.0),
                ("2026-06-19", 11.0),
                ("2026-06-20", 14.0),
            ],
        );
        let right = series("right", &[("2026-06-19", 9.0), ("2026-06-20", 10.0)]);

        let spread = derive_matching_date_spread(
            &left,
            &right,
            AnalysisSubject::SyntheticModel("spread".to_owned()),
            "Left - Right",
            TimeSeriesKind::Price,
        );

        assert_eq!(spread.common_points, 2);
        assert!(spread.partial);
        assert_eq!(spread.series.status, "PARTIAL");
        assert_eq!(spread.series.points[0].date, "2026-06-19");
        assert_eq!(spread.series.points[0].value, 2.0);
        assert_eq!(spread.series.points[1].value, 4.0);
    }

    #[test]
    fn spread_reports_missing_when_dates_do_not_align() {
        let left = series("left", &[("2026-06-18", 10.0)]);
        let right = series("right", &[("2026-06-20", 9.0)]);

        let spread = derive_matching_date_spread(
            &left,
            &right,
            AnalysisSubject::SyntheticModel("spread".to_owned()),
            "Left - Right",
            TimeSeriesKind::Price,
        );

        assert_eq!(spread.common_points, 0);
        assert_eq!(spread.series.status, "MISSING");
        assert!(spread.series.points.is_empty());
    }

    #[test]
    fn rebased100_uses_first_common_valid_point() {
        let left = series("left", &[("2026-06-18", 10.0), ("2026-06-19", 12.0)]);
        let right = series("right", &[("2026-06-19", 6.0), ("2026-06-20", 9.0)]);
        let base_date =
            first_valid_common_base_date(&[left.points.as_slice(), right.points.as_slice()]);

        assert_eq!(base_date.as_deref(), Some("2026-06-19"));

        let display = display_points_for_mode(
            &right.points,
            ChartValueMode::Rebased100,
            base_date.as_deref(),
        );

        assert_eq!(display.len(), 2);
        assert_eq!(display[0].value, 100.0);
        assert_eq!(display[1].value, 150.0);
        assert_eq!(display[1].raw_value, 9.0);
    }

    #[test]
    fn rebased100_returns_empty_when_base_is_missing_or_zero() {
        let left = series("left", &[("2026-06-18", 0.0), ("2026-06-19", 12.0)]);
        let right = series("right", &[("2026-06-18", 6.0), ("2026-06-20", 9.0)]);
        let base_date =
            first_valid_common_base_date(&[left.points.as_slice(), right.points.as_slice()]);

        assert_eq!(base_date, None);
        assert!(display_points_for_mode(&left.points, ChartValueMode::Rebased100, None).is_empty());
    }

    #[test]
    fn percent_change_uses_common_base_as_zero() {
        let input = series(
            "left",
            &[
                ("2026-06-18", 10.0),
                ("2026-06-19", 12.0),
                ("2026-06-20", 15.0),
            ],
        );

        let display = display_points_for_mode(
            &input.points,
            ChartValueMode::PercentChange,
            Some("2026-06-18"),
        );

        assert_eq!(display[0].value, 0.0);
        assert!((display[1].value - 0.2).abs() < 0.000_001);
        assert!((display[2].value - 0.5).abs() < 0.000_001);
    }
}
