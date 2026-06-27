from __future__ import annotations

from httpx import AsyncClient


def _collect_ids(node: dict) -> list[str]:
    ids = [node["id"]]
    for child in node["children"]:
        ids.extend(_collect_ids(child))
    return ids


async def test_hierarchy_root_is_portfolio(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspaces/1/hierarchy")
    assert response.status_code == 200
    root = response.json()["root"]
    assert root["id"] == "workspace:1"
    assert root["kind"] == "portfolio"
    assert root["label"] == "Personal"
    assert root["currency"] == "GBP"
    assert root["value"] == "12381.10"
    assert root["weight"] == "1.0"


async def test_positions_appear_as_children(client: AsyncClient) -> None:
    root = (await client.get("/api/v1/workspaces/1/hierarchy")).json()["root"]
    assert len(root["children"]) == 4
    assert {c["kind"] for c in root["children"]} == {"position"}
    tickers = {c["label"] for c in root["children"]}
    assert {"VUSA", "ISF", "JEPG", "JEGP"} == tickers


async def test_fund_holdings_appear_under_positions(client: AsyncClient) -> None:
    root = (await client.get("/api/v1/workspaces/1/hierarchy")).json()["root"]
    vusa = next(c for c in root["children"] if c["label"] == "VUSA")
    assert len(vusa["children"]) == 5  # five seeded holdings
    assert {h["kind"] for h in vusa["children"]} == {"holding"}
    assert vusa["children"][0]["label"] == "Apple Inc"  # top weight first


async def test_hierarchy_has_no_duplicate_or_cyclic_ids(client: AsyncClient) -> None:
    root = (await client.get("/api/v1/workspaces/1/hierarchy")).json()["root"]
    ids = _collect_ids(root)
    # JEPG and JEGP share a fund; holding ids are namespaced by position so the
    # tree still has globally-unique ids and no cycles.
    assert len(ids) == len(set(ids))


async def test_hierarchy_unknown_workspace_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspaces/999999/hierarchy")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workspace_not_found"
