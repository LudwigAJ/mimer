pub fn fmt_money(currency: &str, value: f64) -> String {
    format!("{currency} {}", fmt_decimal(value, 2))
}

pub fn fmt_signed_money(currency: &str, value: f64) -> String {
    let sign = if value >= 0.0 { "+" } else { "-" };
    format!("{currency} {sign}{}", fmt_decimal(value.abs(), 2))
}

pub fn fmt_decimal(value: f64, decimals: usize) -> String {
    let sign = if value.is_sign_negative() { "-" } else { "" };
    let raw = format!("{:.*}", decimals, value.abs());
    let (integer, fraction) = raw.split_once('.').unwrap_or((raw.as_str(), ""));
    let mut grouped_reversed = String::new();

    for (index, digit) in integer.chars().rev().enumerate() {
        if index > 0 && index % 3 == 0 {
            grouped_reversed.push(',');
        }
        grouped_reversed.push(digit);
    }

    let grouped: String = grouped_reversed.chars().rev().collect();
    if decimals == 0 {
        format!("{sign}{grouped}")
    } else {
        format!("{sign}{grouped}.{fraction}")
    }
}

pub fn fmt_percent(value: f64) -> String {
    format!("{}%", fmt_decimal(value, 2))
}

pub fn fmt_date_str(value: &str) -> &str {
    if value.trim().is_empty() { "-" } else { value }
}

pub fn fmt_status(value: &str) -> String {
    value.trim().to_ascii_uppercase()
}

pub fn fmt_source(value: &str) -> String {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        "SRC: -".to_owned()
    } else {
        format!("SRC: {}", trimmed.to_ascii_lowercase())
    }
}

pub fn fmt_optional(value: Option<&str>) -> &str {
    value.filter(|text| !text.trim().is_empty()).unwrap_or("-")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_money_with_grouping() {
        assert_eq!(fmt_money("GBP", 12345.6), "GBP 12,345.60");
        assert_eq!(fmt_signed_money("GBP", -42.1), "GBP -42.10");
    }

    #[test]
    fn formats_optional_and_status() {
        assert_eq!(fmt_optional(Some("XLON")), "XLON");
        assert_eq!(fmt_optional(Some("")), "-");
        assert_eq!(fmt_optional(None), "-");
        assert_eq!(fmt_status(" stale "), "STALE");
        assert_eq!(fmt_source(" Issuer "), "SRC: issuer");
        assert_eq!(fmt_source(""), "SRC: -");
    }
}
