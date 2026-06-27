#![allow(dead_code)]

pub const SPACE_1: f32 = 4.0;
pub const SPACE_2: f32 = 8.0;
pub const SPACE_3: f32 = 12.0;
pub const SPACE_4: f32 = 16.0;
pub const SPACE_6: f32 = 24.0;
pub const SPACE_8: f32 = 32.0;

pub const LEFT_RAIL_DEFAULT_WIDTH: f32 = 148.0;
pub const LEFT_RAIL_MIN_WIDTH: f32 = 128.0;
pub const LEFT_RAIL_MAX_WIDTH: f32 = 220.0;

pub const INSPECTOR_DEFAULT_WIDTH: f32 = 292.0;
pub const INSPECTOR_MIN_WIDTH: f32 = 240.0;
pub const INSPECTOR_NARROW_MIN_WIDTH: f32 = 200.0;
pub const INSPECTOR_MAX_WIDTH: f32 = 480.0;
pub const MAIN_CONTENT_MIN_WIDTH: f32 = 520.0;

pub const MENU_BAR_HEIGHT: f32 = 26.0;
pub const TOOLBAR_HEIGHT: f32 = 34.0;
pub const CONTEXT_STRIP_HEIGHT: f32 = 26.0;
pub const STATUS_BAR_HEIGHT: f32 = 22.0;

pub const ROW_HEIGHT_COMPACT: f32 = 20.0;
pub const ROW_HEIGHT_COMFORTABLE: f32 = 24.0;
pub const TABLE_HEADER_HEIGHT: f32 = 20.0;
pub const PAGE_CONTENT_MARGIN: f32 = 8.0;

pub fn is_narrow(width: f32) -> bool {
    width < 900.0
}

pub fn is_medium(width: f32) -> bool {
    (900.0..1200.0).contains(&width)
}

pub fn is_wide(width: f32) -> bool {
    width >= 1200.0
}

pub fn fit_width(available: f32, preferred: f32, min: f32, max: f32) -> f32 {
    let available = available.max(0.0);
    preferred.clamp(min.min(available), max.min(available))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn breakpoints_are_mutually_clear() {
        assert!(is_narrow(899.0));
        assert!(is_medium(900.0));
        assert!(is_medium(1199.0));
        assert!(is_wide(1200.0));
    }

    #[test]
    fn fit_width_never_exceeds_available_width() {
        assert_eq!(fit_width(180.0, 300.0, 220.0, 380.0), 180.0);
        assert_eq!(fit_width(600.0, 300.0, 220.0, 380.0), 300.0);
        assert_eq!(fit_width(900.0, 760.0, 220.0, 380.0), 380.0);
    }
}
