"""Optional shared Bearer-token auth for the ``/api/v1`` surface.

When ``MIMER_API_TOKEN`` is set (non-empty), every ``/api/v1`` route requires an
``Authorization: Bearer <token>`` header; a missing, malformed or wrong token is
rejected with ``401``. When the token is blank/unset, auth is disabled (local
dev) and the routes are open. The health endpoints are mounted **outside** the
``/api/v1`` router and are always unauthenticated, so liveness/readiness probes
(and the container ``HEALTHCHECK``) never need a token.

This is deliberately minimal: one shared token, constant-time compared. There is
no user database, no sessions, no cookies, no OAuth, and no reverse-proxy
dependency — the future VPS Caddy proxies to the localhost API and does not need
to know the token. The token is only ever compared — **never logged and never
returned** in a response or in diagnostics/capabilities.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header

from app.core.config import Settings, get_settings
from app.core.errors import AppError


class UnauthorizedError(AppError):
    """401 — the request lacks a valid API token (rendered in the error envelope)."""

    status_code = 401
    code = "unauthorized"


def _bearer_token(authorization: str | None) -> str | None:
    """Return the credentials of an ``Authorization: Bearer <token>`` header.

    None for a missing header, a non-Bearer scheme, or an empty credential — all
    of which the caller treats as "no usable token". The scheme match is
    case-insensitive (``Bearer`` / ``bearer``) per RFC 7235.
    """
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    credentials = credentials.strip()
    return credentials or None


async def require_api_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Enforce the shared Bearer token on ``/api/v1`` when one is configured.

    A no-op when auth is disabled (blank ``MIMER_API_TOKEN``) so local dev runs
    unauthenticated. Uses :func:`hmac.compare_digest` so a wrong token leaks no
    length/timing signal. Raises :class:`UnauthorizedError` (401) on a
    missing/malformed/wrong token; the message never contains the supplied or
    expected token.
    """
    if not settings.api_auth_enabled:
        return
    provided = _bearer_token(authorization)
    if provided is None:
        raise UnauthorizedError("Missing or malformed bearer token in the Authorization header.")
    if not hmac.compare_digest(provided, settings.api_token):
        raise UnauthorizedError("Invalid API token.")
