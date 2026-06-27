"""Market-data planner: dedupe, prioritise, estimate — without live fetches."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FundListing, FxRate, PortfolioPosition, Workspace
from app.services import market_data_planner


async def _default_workspace_id(session: AsyncSession) -> int:
    return (await session.execute(select(Workspace.id))).scalars().first()


async def test_plan_dedupes_repeated_constituents(session: AsyncSession) -> None:
    wid = await _default_workspace_id(session)
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)

    resolve_items = [i for i in plan.items if i.item_type == "resolve_constituent_identity"]
    # Apple is held via VUSA *and* JPM in the seed — exactly one plan item for it.
    apple = [i for i in resolve_items if i.label and "Apple" in i.label]
    assert len(apple) == 1
    # plan_keys are unique (dedupe invariant).
    keys = [i.plan_key for i in plan.items]
    assert len(keys) == len(set(keys))


async def test_plan_reports_unresolved_constituents_and_openfigi_estimate(
    session: AsyncSession,
) -> None:
    wid = await _default_workspace_id(session)
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert plan.summary.unresolved_constituents >= 1
    assert plan.summary.constituent_count >= 1
    # Identity resolution is estimated against OpenFIGI, deduped.
    by_source = plan.summary.estimated_requests_by_source
    assert by_source.get("openfigi", 0) == plan.summary.unresolved_constituents


async def test_plan_prioritises_held_prices_and_top_weight(session: AsyncSession) -> None:
    wid = await _default_workspace_id(session)
    # Force a held listing's price to be missing so a high-priority item appears.
    listing = (
        (await session.execute(select(FundListing).order_by(FundListing.id))).scalars().first()
    )
    listing.last_price_at = None
    await session.commit()

    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    price_items = [i for i in plan.items if i.item_type == "fetch_listing_price"]
    assert price_items, "a missing held price should produce a fetch_listing_price item"
    assert all(i.priority == 1 for i in price_items)  # held positions are top priority
    assert plan.summary.missing_prices >= 1
    # Items are returned in priority order (most useful first).
    priorities = [i.priority for i in plan.items]
    assert priorities == sorted(priorities)


async def test_plan_reports_missing_fx(session: AsyncSession) -> None:
    wid = await _default_workspace_id(session)
    # Remove every GBP<->USD rate so the held USD listing has no path to base.
    rates = (await session.execute(select(FxRate))).scalars().all()
    for rate in rates:
        if "USD" in (rate.base_currency, rate.quote_currency):
            await session.delete(rate)
    await session.commit()

    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    fx_items = [i for i in plan.items if i.item_type == "fetch_fx_rate"]
    assert fx_items, "a held USD position with no FX path should produce a fetch_fx_rate item"
    assert plan.summary.missing_fx >= 1
    assert all(i.priority == 1 for i in fx_items)


async def test_plan_can_exclude_constituents(session: AsyncSession) -> None:
    wid = await _default_workspace_id(session)
    plan = await market_data_planner.build_plan(session, wid, include_constituents=False)
    assert plan.include_constituents is False
    assert not any(i.item_type == "resolve_constituent_identity" for i in plan.items)


async def test_plan_reports_missing_holdings(session: AsyncSession) -> None:
    wid = await _default_workspace_id(session)
    # Point a position at a brand-new fund/listing with no holdings snapshot.
    from app.db.models import Fund

    fund = Fund(isin="IE00NEWFUND01", name="New Fund", status="pending", source="seed")
    listing = FundListing(fund=fund, ticker="NEW", trading_currency="GBP", currency_unit="GBP")
    session.add_all([fund, listing])
    await session.flush()
    session.add(PortfolioPosition(workspace_id=wid, fund_listing_id=listing.id, units=10))
    await session.commit()

    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    holdings_items = [
        i for i in plan.items if i.item_type == "refresh_holdings" and i.related_fund_id == fund.id
    ]
    assert holdings_items, "a held fund with no holdings snapshot should be flagged"


async def test_plan_surfaces_target_fund_followons(session: AsyncSession) -> None:
    # The seeded workspace holds the target funds (VUSA/ISF/JEPG) on seed-provenance facts.
    # The planner must make the consequence chain visible: a refresh_fund_facts follow-on for
    # each target fund (seed/placeholder provenance) is the prerequisite the cascade hangs off.
    from app.db.models import Fund

    wid = await _default_workspace_id(session)
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    facts = {i.related_fund_id for i in plan.items if i.item_type == "refresh_fund_facts"}
    target_isins = {"IE00B3XXRP09", "IE0005042456", "IE0003UVYC20"}
    target_ids = {
        f.id
        for f in (await session.execute(select(Fund).where(Fund.isin.in_(target_isins)))).scalars()
    }
    assert target_ids <= facts, "every seed-provenance target fund needs a refresh_fund_facts item"


async def test_holdings_refresh_recommends_live_source_for_isf(session: AsyncSession) -> None:
    # A holdings refresh for ISF must recommend the verified live issuer source (not just the
    # fixture) — the holdings → identity → price → exposure cascade hangs off this source choice.
    from datetime import date

    from app.db.models import Fund, FundHolding

    isf = await session.scalar(select(Fund).where(Fund.isin == "IE0005042456"))
    assert isf is not None
    # Age ISF's holdings snapshot so the planner emits a refresh_holdings follow-on.
    for holding in (
        await session.execute(select(FundHolding).where(FundHolding.fund_id == isf.id))
    ).scalars():
        holding.as_of_date = date(2020, 1, 1)
    await session.commit()

    wid = await _default_workspace_id(session)
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    refresh = [
        i for i in plan.items if i.item_type == "refresh_holdings" and i.related_fund_id == isf.id
    ]
    assert refresh, "stale ISF holdings should produce a refresh_holdings follow-on"
    assert "blackrock_ishares_holdings" in refresh[0].source_candidates


async def test_plan_does_not_touch_network(session: AsyncSession, monkeypatch) -> None:
    # Hard guarantee: building a plan performs no HTTP I/O.
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - only fires on a real call
        raise AssertionError("planner attempted a network call")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    monkeypatch.setattr(httpx.AsyncClient, "post", explode)

    wid = await _default_workspace_id(session)
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert plan.summary.total_items >= 1


async def test_plan_makes_the_etf_cascade_visible(session: AsyncSession) -> None:
    # The ETF/fund consequence chain must be visible+runnable as plan items: a held fund's
    # holdings -> constituent identity -> constituent EOD price -> FX -> reference rates.
    wid = await _default_workspace_id(session)
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    item_types = {i.item_type for i in plan.items}
    # Holdings are seeded but constituents are unresolved -> identity step is present.
    assert "resolve_constituent_identity" in item_types
    # Reference-rate collection (none seeded) is part of the chain for held currencies.
    assert "fetch_reference_rates" in item_types
    # Every item names the source candidate(s) that would run it (runnable, not opaque).
    for item in plan.items:
        assert item.source_candidates, item.item_type
