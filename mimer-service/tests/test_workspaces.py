from __future__ import annotations

from httpx import AsyncClient


async def _default_workspace_id(client: AsyncClient) -> int:
    return (await client.get("/api/v1/workspaces")).json()["data"][0]["id"]


async def test_me_returns_user_and_workspaces(client: AsyncClient) -> None:
    response = await client.get("/api/v1/me")
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "owner@example.com"
    assert len(body["workspaces"]) == 1
    assert body["workspaces"][0]["name"] == "Personal"


async def test_workspace_scoped_summary(client: AsyncClient) -> None:
    ws_id = await _default_workspace_id(client)
    response = await client.get(f"/api/v1/workspaces/{ws_id}/portfolio/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["base_currency"] == "GBP"
    assert len(body["positions"]) == 4
    assert body["total_market_value"] == "12381.10"


async def test_unknown_workspace_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspaces/999999/portfolio/summary")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workspace_not_found"


async def test_settings_get_and_merge_put(client: AsyncClient) -> None:
    ws_id = await _default_workspace_id(client)

    initial = await client.get(f"/api/v1/workspaces/{ws_id}/settings")
    assert initial.status_code == 200
    assert initial.json()["settings"]["base_currency"] == "GBP"

    updated = await client.put(
        f"/api/v1/workspaces/{ws_id}/settings",
        json={"settings": {"theme": "dark"}},
    )
    assert updated.status_code == 200
    settings = updated.json()["settings"]
    assert settings["theme"] == "dark"
    assert settings["base_currency"] == "GBP"  # existing key preserved


async def test_positions_are_workspace_scoped(client: AsyncClient) -> None:
    ws_id = await _default_workspace_id(client)
    response = await client.get(f"/api/v1/workspaces/{ws_id}/portfolio/positions")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["count"] == 4
    assert all(p["workspace_id"] == ws_id for p in body["data"])
