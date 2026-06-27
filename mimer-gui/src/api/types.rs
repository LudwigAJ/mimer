#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DataMode {
    Mock,
    Api,
}

impl DataMode {
    pub const ALL: [Self; 2] = [Self::Mock, Self::Api];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Mock => "MOCK",
            Self::Api => "API",
        }
    }

    pub fn from_str(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "mock" => Some(Self::Mock),
            "api" | "live" => Some(Self::Api),
            _ => None,
        }
    }
}

#[derive(Clone, Debug)]
pub struct ApiConfig {
    pub base_url: String,
    pub timeout_ms: u64,
    pub auth_mode: AuthMode,
    pub workspace_header_value: String,
}

impl Default for ApiConfig {
    fn default() -> Self {
        Self {
            base_url: "http://localhost:8080/api/v1".to_owned(),
            timeout_ms: 5_000,
            auth_mode: AuthMode::NoneMock,
            workspace_header_value: "workspace-main".to_owned(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AuthMode {
    NoneMock,
    DevHeader,
    BearerTokenFuture,
}

impl AuthMode {
    pub const ALL: [Self; 3] = [Self::NoneMock, Self::DevHeader, Self::BearerTokenFuture];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::NoneMock => "none/mock",
            Self::DevHeader => "dev header",
            Self::BearerTokenFuture => "bearer token, future",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ApiConnectionStatus {
    NotUsed,
    Checking,
    Disconnected,
    Connected,
    Partial,
}

impl ApiConnectionStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NotUsed => "NOT USED / MOCK",
            Self::Checking => "CHECKING",
            Self::Disconnected => "DISCONNECTED",
            Self::Connected => "CONNECTED",
            Self::Partial => "PARTIAL",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ApiRuntimeStatus {
    pub connection: ApiConnectionStatus,
    pub last_checked_at: Option<String>,
    pub last_error: Option<String>,
}

impl Default for ApiRuntimeStatus {
    fn default() -> Self {
        Self {
            connection: ApiConnectionStatus::NotUsed,
            last_checked_at: None,
            last_error: None,
        }
    }
}
