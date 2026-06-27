const SECRET_KEYS: [&str; 11] = [
    "apikey",
    "api_key",
    "openfigi_api_key",
    "token",
    "access_token",
    "authorization",
    "password",
    "secret",
    "client_secret",
    "auth",
    "key",
];

pub fn mask_secret_fragments(input: &str) -> String {
    let mut output = mask_bearer_tokens(&mask_url_userinfo(input));
    for key in SECRET_KEYS {
        output = mask_secret_assignment(&output, key, '=');
        output = mask_secret_assignment(&output, key, ':');
    }
    output
}

pub fn contains_secret_material(input: &str) -> bool {
    let lower = input.to_ascii_lowercase();
    SECRET_KEYS
        .iter()
        .any(|key| lower.contains(&format!("{key}=")) || lower.contains(&format!("{key}:")))
        || lower.contains("bearer ")
        || url_contains_userinfo(input)
}

fn mask_secret_assignment(input: &str, key: &str, delimiter: char) -> String {
    let mut output = input.to_owned();
    let pattern = format!("{key}{delimiter}");
    let mut search_start = 0;

    loop {
        let lower = output.to_ascii_lowercase();
        let Some(relative_pos) = lower[search_start..].find(&pattern) else {
            break;
        };
        let match_start = search_start + relative_pos;
        if match_start > 0
            && output[..match_start]
                .chars()
                .next_back()
                .is_some_and(|ch| ch.is_ascii_alphanumeric() || ch == '_')
        {
            search_start = match_start + pattern.len();
            continue;
        }

        let mut value_start = match_start + pattern.len();
        while output[value_start..].starts_with([' ', '\t']) {
            value_start += 1;
        }
        if key == "authorization"
            && output[value_start..]
                .to_ascii_lowercase()
                .starts_with("bearer ")
        {
            search_start = value_start + "bearer ".len();
            continue;
        }
        let value_end = output[value_start..]
            .find(secret_value_separator)
            .map(|offset| value_start + offset)
            .unwrap_or_else(|| output.len());

        if value_end > value_start {
            output.replace_range(value_start..value_end, "***");
            search_start = value_start + 3;
        } else {
            search_start = value_start;
        }
    }

    output
}

fn mask_bearer_tokens(input: &str) -> String {
    let mut output = input.to_owned();
    let mut search_start = 0;
    loop {
        let lower = output.to_ascii_lowercase();
        let Some(relative_pos) = lower[search_start..].find("bearer ") else {
            break;
        };
        let value_start = search_start + relative_pos + "bearer ".len();
        let value_end = output[value_start..]
            .find(secret_value_separator)
            .map(|offset| value_start + offset)
            .unwrap_or_else(|| output.len());
        if value_end > value_start {
            output.replace_range(value_start..value_end, "***");
            search_start = value_start + 3;
        } else {
            search_start = value_start;
        }
    }
    output
}

fn mask_url_userinfo(input: &str) -> String {
    let Some(scheme_end) = input.find("://") else {
        return input.to_owned();
    };
    let authority_start = scheme_end + 3;
    let authority_end = input[authority_start..]
        .find(['/', '?', '#', ' '])
        .map(|offset| authority_start + offset)
        .unwrap_or(input.len());
    let authority = &input[authority_start..authority_end];
    let Some(at_offset) = authority.rfind('@') else {
        return input.to_owned();
    };

    let mut output = input.to_owned();
    output.replace_range(authority_start..authority_start + at_offset, "***");
    output
}

fn url_contains_userinfo(input: &str) -> bool {
    let Some(scheme_end) = input.find("://") else {
        return false;
    };
    let authority_start = scheme_end + 3;
    let authority_end = input[authority_start..]
        .find(['/', '?', '#', ' '])
        .map(|offset| authority_start + offset)
        .unwrap_or(input.len());
    input[authority_start..authority_end].contains('@')
}

fn secret_value_separator(ch: char) -> bool {
    matches!(
        ch,
        '&' | ';' | '\t' | '\n' | '\r' | ' ' | '|' | ',' | '"' | '\'' | ')' | ']'
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masks_query_header_bearer_and_url_userinfo_secrets() {
        let input = "https://user:pass@example.test/data?apikey=dev-secret \
                     Authorization: Bearer token-value password=hunter2";
        let masked = mask_secret_fragments(input);

        assert!(!masked.contains("user:pass"));
        assert!(!masked.contains("dev-secret"));
        assert!(!masked.contains("token-value"));
        assert!(!masked.contains("hunter2"));
        assert!(masked.contains("apikey=***"));
        assert!(masked.contains("Bearer ***"));
    }

    #[test]
    fn detects_secret_material_before_persistence() {
        assert!(contains_secret_material(
            "https://example.test/api?access_token=abc"
        ));
        assert!(contains_secret_material("https://user:pass@example.test"));
        assert!(!contains_secret_material("http://localhost:8080/api/v1"));
    }
}
