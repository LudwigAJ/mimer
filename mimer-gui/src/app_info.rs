pub const APP_NAME: &str = "Mimer";
pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const APP_STAGE: &str = "alpha";
pub const APP_DATA_MODE: &str = "mock/api";

pub fn version_label() -> String {
    format!("v{APP_VERSION}-{APP_STAGE}")
}

pub fn window_title() -> String {
    format!("{APP_NAME} - {} ({APP_DATA_MODE})", version_label())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn title_contains_version_and_data_mode_status() {
        let title = window_title();

        assert!(title.contains(APP_NAME));
        assert!(title.contains(APP_VERSION));
        assert!(title.contains(APP_DATA_MODE));
    }
}
