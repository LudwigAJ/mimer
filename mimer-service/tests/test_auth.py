"""Bearer-token API auth behaviour.

The auth dependency (`app.api.security.require_api_token`) reads its token from
``Settings`` via ``Depends(get_settings)``, so these tests flip auth on/off by
overriding that dependency on the app — no env mutation or cache clearing needed.
The default (no override) keeps auth DISABLED, matching local dev and the rest of
the suite.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.core.config import Settings, get_settings
from app.main import app

TOKEN = "s3cret-local-test-token"


@pytest.fixture
def auth_enabled() -> Iterator[None]:
    """Enable Bearer auth with a known token for the duration of a test."""
    app.dependency_overrides[get_settings] = lambda: Settings(api_token=TOKEN, _env_file=None)
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_settings, None)


# --- auth disabled (default) -------------------------------------------------


async def test_auth_disabled_allows_api_without_token(client: AsyncClient) -> None:
    # No token configured => /api/v1 is open (no Authorization header needed).
    response = await client.get("/api/v1/capabilities")
    assert response.status_code == 200


# --- health is always open ---------------------------------------------------


async def test_health_unauthenticated_when_auth_enabled(
    client: AsyncClient, auth_enabled: None
) -> None:
    # Health probes must never require the token (container HEALTHCHECK, Caddy).
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/health/db")).status_code == 200


# --- auth enabled: /api/v1 is protected --------------------------------------


async def test_api_without_token_rejected_when_enabled(
    client: AsyncClient, auth_enabled: None
) -> None:
    response = await client.get("/api/v1/capabilities")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_api_with_wrong_token_rejected(client: AsyncClient, auth_enabled: None) -> None:
    response = await client.get(
        "/api/v1/capabilities", headers={"Authorization": "Bearer not-the-token"}
    )
    assert response.status_code == 401


async def test_api_with_correct_token_accepted(client: AsyncClient, auth_enabled: None) -> None:
    response = await client.get(
        "/api/v1/capabilities", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        TOKEN,  # raw token, no scheme
        f"Token {TOKEN}",  # wrong scheme
        f"Basic {TOKEN}",  # wrong scheme
        "Bearer",  # scheme only, no credential
        "Bearer ",  # scheme + empty credential
        f"bearer{TOKEN}",  # no space separator
    ],
)
async def test_malformed_authorization_header_rejected(
    client: AsyncClient, auth_enabled: None, header: str
) -> None:
    response = await client.get("/api/v1/capabilities", headers={"Authorization": header})
    assert response.status_code == 401


async def test_lowercase_bearer_scheme_accepted(client: AsyncClient, auth_enabled: None) -> None:
    # RFC 7235 auth schemes are case-insensitive; only the scheme, not the token.
    response = await client.get(
        "/api/v1/capabilities", headers={"Authorization": f"bearer {TOKEN}"}
    )
    assert response.status_code == 200


# --- the token is never exposed in a response --------------------------------


async def test_token_not_exposed_in_capabilities(client: AsyncClient, auth_enabled: None) -> None:
    body = (
        await client.get("/api/v1/capabilities", headers={"Authorization": f"Bearer {TOKEN}"})
    ).text
    assert TOKEN not in body


async def test_token_not_exposed_in_401_body(client: AsyncClient, auth_enabled: None) -> None:
    # A rejection must not echo the (wrong) supplied token nor the expected one.
    response = await client.get(
        "/api/v1/capabilities", headers={"Authorization": "Bearer leaky-guess"}
    )
    assert TOKEN not in response.text
    assert "leaky-guess" not in response.text
