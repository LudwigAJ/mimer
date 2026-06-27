"""Defensive secret masking for observability responses.

Anything surfaced through the read models that echo stored job/fetch data —
``job_runs.message`` / ``payload_json``, ``source_fetch_logs`` request keys /
endpoint labels / error strings — is run through here before it leaves the API.

The fetch-log / request-cache layer is already built to be secrets-free (request
keys drop credential params, only an ``endpoint_label`` + hashes are stored — see
``app/services/source_requests.py`` and AGENTS.md). This module is a *defence in
depth* second pass: even if a credential ever leaked into a message or a payload,
it never reaches a client. It is deterministic, allocation-light and never raises.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***"

# Mapping keys (and inline ``key=value`` tokens) treated as credential-bearing.
# Compared case-insensitively with ``-``/``_`` folded together.
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "x-openfigi-apikey",
    "openfigi_api_key",
    "authorization",
    "auth",
    "token",
    "access_token",
    "refresh_token",
    "key",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "pwd",
}
_SENSITIVE_KEYS_NORM = {k.replace("-", "_") for k in _SENSITIVE_KEYS}

# Inline ``<key>=<value>`` / ``<key>: <value>`` (also covers URL query strings).
# A leading non-word lookbehind keeps benign suffixes like ``holding_key=`` /
# ``request_key=`` / ``identity_key=`` from being treated as a credential ``key``.
_INLINE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    r"(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|authorization|auth|"
    r"client[_-]?secret|secret|password|passwd|pwd|openfigi[_-]?api[_-]?key|key)"
    r"(\s*[=:]\s*)"
    r"([^\s,&;\"']+)"
)

# ``Bearer <token>`` / ``Basic <token>`` style auth values.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]+)")


def _is_sensitive_key(key: str) -> bool:
    return key.strip().lower().replace("-", "_") in _SENSITIVE_KEYS_NORM


def mask_text(value: str | None) -> str | None:
    """Redact inline credential tokens in a free-text string (None-safe)."""
    if not isinstance(value, str) or not value:
        return value
    masked = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
    masked = _INLINE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", masked)
    return masked


def mask_json(obj: Any) -> Any:
    """Recursively redact a JSON-ish structure (dicts/lists/strings).

    A mapping value whose *key* looks sensitive is fully redacted; every string
    value (and nested string) also has inline tokens masked. Non-string scalars
    pass through unchanged. Returns a new structure (never mutates the input).
    """
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                out[key] = REDACTED
            else:
                out[key] = mask_json(value)
        return out
    if isinstance(obj, list):
        return [mask_json(v) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj
