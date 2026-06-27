use eframe::egui;

pub fn date_text_field(ui: &mut egui::Ui, id: &str, value: &mut String) -> egui::Response {
    let response = ui
        .push_id(id, |ui| {
            ui.add_sized(
                [104.0, 20.0],
                egui::TextEdit::singleline(value).hint_text("YYYY-MM-DD"),
            )
        })
        .inner
        .on_hover_text(
            "Date field. Current egui_extras build has no DatePickerButton; use YYYY-MM-DD.",
        );

    if !value.trim().is_empty() && !is_valid_iso_date(value) {
        ui.colored_label(egui::Color32::from_rgb(230, 110, 95), "Invalid date");
    }

    response
}

pub fn is_valid_iso_date(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 10 || bytes[4] != b'-' || bytes[7] != b'-' {
        return false;
    }
    if !bytes
        .iter()
        .enumerate()
        .all(|(index, byte)| matches!(index, 4 | 7) || byte.is_ascii_digit())
    {
        return false;
    }

    let year = parse_u32(&value[0..4]);
    let month = parse_u32(&value[5..7]);
    let day = parse_u32(&value[8..10]);
    let (Some(year), Some(month), Some(day)) = (year, month, day) else {
        return false;
    };

    if year == 0 || !(1..=12).contains(&month) {
        return false;
    }

    let max_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        _ => return false,
    };
    (1..=max_day).contains(&day)
}

fn parse_u32(value: &str) -> Option<u32> {
    value.parse::<u32>().ok()
}

fn is_leap_year(year: u32) -> bool {
    year.is_multiple_of(4) && !year.is_multiple_of(100) || year.is_multiple_of(400)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_iso_dates() {
        assert!(is_valid_iso_date("2026-06-20"));
        assert!(is_valid_iso_date("2024-02-29"));
        assert!(!is_valid_iso_date("2026-02-29"));
        assert!(!is_valid_iso_date("2026/06/20"));
        assert!(!is_valid_iso_date("2026-13-01"));
    }
}
