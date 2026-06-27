pub fn contains_ci(haystack: &str, needle: &str) -> bool {
    let needle = needle.trim();
    needle.is_empty()
        || haystack
            .to_ascii_lowercase()
            .contains(&needle.to_ascii_lowercase())
}

pub fn any_contains_ci<'a>(values: impl IntoIterator<Item = &'a str>, needle: &str) -> bool {
    let needle = needle.trim();
    needle.is_empty() || values.into_iter().any(|value| contains_ci(value, needle))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_case_insensitive_substrings() {
        assert!(contains_ci("Daily Price Ingestion", "price"));
        assert!(contains_ci("Vanguard S&P 500", "VANG"));
        assert!(contains_ci("anything", ""));
        assert!(!contains_ci("Holdings", "jobs"));
    }

    #[test]
    fn matches_any_value() {
        assert!(any_contains_ci(["VUSA", "IE00B3XXRP09"], "b3xx"));
        assert!(!any_contains_ci(["VUSA", "IE00B3XXRP09"], "ISF"));
    }
}
