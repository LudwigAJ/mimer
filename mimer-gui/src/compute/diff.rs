#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[allow(dead_code)]
pub enum DiffSide {
    Left,
    Right,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DiffStatus {
    Unchanged,
    Added,
    Removed,
    Changed,
}

impl DiffStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unchanged => "Unchanged",
            Self::Added => "Added",
            Self::Removed => "Removed",
            Self::Changed => "Changed",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FieldDiff {
    pub field_name: String,
    pub left_value: Option<String>,
    pub right_value: Option<String>,
    pub status: DiffStatus,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EntityDiff {
    pub entity_type: String,
    pub entity_name: String,
    pub left_label: String,
    pub right_label: String,
    pub fields: Vec<FieldDiff>,
}

pub fn field_diff(field_name: &str, left: Option<&str>, right: Option<&str>) -> FieldDiff {
    let status = match (left, right) {
        (None, Some(_)) => DiffStatus::Added,
        (Some(_), None) => DiffStatus::Removed,
        (Some(left), Some(right)) if left == right => DiffStatus::Unchanged,
        (Some(_), Some(_)) => DiffStatus::Changed,
        (None, None) => DiffStatus::Unchanged,
    };

    FieldDiff {
        field_name: field_name.to_owned(),
        left_value: left.map(str::to_owned),
        right_value: right.map(str::to_owned),
        status,
    }
}

pub fn mock_entity_diffs() -> Vec<EntityDiff> {
    vec![
        EntityDiff {
            entity_type: "Document".to_owned(),
            entity_name: "JEGP factsheet".to_owned(),
            left_label: "2026-04-30".to_owned(),
            right_label: "2026-05-31".to_owned(),
            fields: vec![
                field_diff("factsheet date", Some("2026-04-30"), Some("2026-05-31")),
                field_diff("OCF", Some("0.35%"), Some("0.35%")),
                field_diff("top holding weight", Some("4.10%"), Some("4.92%")),
                field_diff("distribution amount", Some("0.141"), Some("0.146")),
                field_diff(
                    "fund name",
                    Some("JPMorgan Global Equity Premium Income Active UCITS ETF"),
                    Some("JPMorgan Global Equity Premium Income Active UCITS ETF"),
                ),
            ],
        },
        EntityDiff {
            entity_type: "Holding".to_owned(),
            entity_name: "NVDA look-through".to_owned(),
            left_label: "previous snapshot".to_owned(),
            right_label: "latest snapshot".to_owned(),
            fields: vec![
                field_diff("weight", Some("2.18%"), Some("3.00%")),
                field_diff("sector", Some("Technology"), Some("Technology")),
                field_diff("source ETF", Some("VUSA"), Some("VUSA")),
            ],
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generates_field_diff_statuses() {
        assert_eq!(
            field_diff("x", Some("1"), Some("1")).status,
            DiffStatus::Unchanged
        );
        assert_eq!(
            field_diff("x", Some("1"), Some("2")).status,
            DiffStatus::Changed
        );
        assert_eq!(field_diff("x", None, Some("2")).status, DiffStatus::Added);
        assert_eq!(field_diff("x", Some("1"), None).status, DiffStatus::Removed);
    }
}
