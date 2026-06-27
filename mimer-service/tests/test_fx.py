"""FX: fixture provider, ingestion, idempotency, lookup/conversion, valuation.

All offline — the fixture provider never touches the network, mirroring the
price/distribution/holdings test pattern.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Fund,
    FundListing,
    FxRate,
    JobRun,
    PortfolioPosition,
    Price,
    ScheduledJob,
    Workspace,
)
from app.services import diagnostics as diagnostics_service
from app.services import fx_ingestion as fx_service
from app.services.fx import FRESH, MISSING, FxIndex
from app.sources.fx import FxRateRecord, StaticFxSource, get_fx_source
from app.workers.run import run_job

_TODAY = date.today()


def _index(*rows: tuple[str, str, str, str, int]) -> FxIndex:
    """Build an FxIndex from (base, quote, rate, source, days_ago) tuples."""
    return FxIndex.from_rows(
        [
            FxRate(
                rate_date=_TODAY - timedelta(days=days_ago),
                base_currency=base,
                quote_currency=quote,
                rate=Decimal(rate),
                source=source,
                status="fixture",
            )
            for (base, quote, rate, source, days_ago) in rows
        ]
    )


# --- fixture provider --------------------------------------------------------


async def test_fx_fixture_returns_consistent_cross_rates() -> None:
    source = StaticFxSource()
    records = await source.fetch_rates(
        base_currency="GBP",
        quote_currencies=["USD", "EUR", "SEK", "ZZZ"],  # ZZZ is unknown
        start_date=_TODAY - timedelta(days=6),
        end_date=_TODAY,
    )
    pairs = {(r.base_currency, r.quote_currency) for r in records}
    assert pairs == {("GBP", "USD"), ("GBP", "EUR"), ("GBP", "SEK")}  # ZZZ skipped
    assert all(r.source == "fx_fixture" and r.status == "fixture" for r in records)

    latest = {r.quote_currency: r.rate for r in records if r.rate_date == _TODAY}
    # The most recent day is the clean anchor (no modulation).
    assert latest["USD"] == Decimal("1.2700000000")
    # GBP/SEK = (USD per GBP) / (USD per SEK) = 1.27 / 0.095 ≈ 13.37.
    assert Decimal("13.0") < latest["SEK"] < Decimal("13.7")
    # Internal consistency: GBP/EUR is between USD and SEK magnitudes, ~1.176.
    assert Decimal("1.16") < latest["EUR"] < Decimal("1.19")


async def test_fx_fixture_skips_unknown_base() -> None:
    records = await StaticFxSource().fetch_rates(base_currency="ZZZ", quote_currencies=["USD"])
    assert records == []


def test_fx_source_registry_unknown_raises() -> None:
    assert get_fx_source("fx_fixture").name == "fx_fixture"
    try:
        get_fx_source("does-not-exist")
    except ValueError as exc:
        assert "Unknown FX source" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# --- lookup / conversion -----------------------------------------------------


def test_lookup_same_currency_is_identity() -> None:
    result = _index(("GBP", "USD", "1.27", "seed", 0)).get_fx_rate("GBP", "GBP")
    assert result.rate == Decimal("1")
    assert result.is_direct and result.status == FRESH
    assert not result.is_inverse and not result.is_triangulated


def test_lookup_direct_pair() -> None:
    result = _index(("GBP", "USD", "1.27", "fx_fixture", 0)).get_fx_rate("GBP", "USD")
    assert result.rate == Decimal("1.27")
    assert result.is_direct and not result.is_inverse
    assert result.status == FRESH
    assert result.source == "fx_fixture"


def test_lookup_inverse_pair() -> None:
    result = _index(("GBP", "USD", "1.27", "seed", 0)).get_fx_rate("USD", "GBP")
    assert result.is_inverse and not result.is_direct
    assert result.rate == Decimal("1") / Decimal("1.27")


def test_lookup_triangulation_via_pivot() -> None:
    # Only GBP-anchored rates stored; USD -> SEK must triangulate via GBP.
    index = _index(
        ("GBP", "USD", "1.27", "fx_fixture", 0),
        ("GBP", "SEK", "13.37", "fx_fixture", 0),
    )
    result = index.get_fx_rate("USD", "SEK")
    assert result.is_triangulated
    assert not result.is_direct and not result.is_inverse
    # USD->GBP (1/1.27) * GBP->SEK (13.37) ≈ 10.53 SEK per USD.
    assert result.rate is not None and Decimal("10") < result.rate < Decimal("11")


def test_lookup_missing_is_explicit_not_one() -> None:
    result = _index(("GBP", "USD", "1.27", "seed", 0)).get_fx_rate("JPY", "SEK")
    assert result.rate is None
    assert result.converted_amount is None
    assert result.status == MISSING
    assert result.missing_reason == "no_fx_path"


def test_convert_amount_normalises_pence() -> None:
    index = _index(("GBP", "USD", "1.27", "seed", 0))
    result = index.convert_amount(Decimal("170000"), "GBX", "GBP")
    # 170000 pence -> £1700, base == local so rate 1.
    assert result.from_currency == "GBP"
    assert result.amount == Decimal("1700")
    assert result.converted_amount == Decimal("1700")


def test_convert_amount_inverse_value() -> None:
    index = _index(("GBP", "USD", "1.27", "seed", 0))
    result = index.convert_amount(Decimal("1500"), "USD", "GBP")
    assert result.is_inverse
    assert result.converted_amount is not None
    assert result.converted_amount.quantize(Decimal("0.01")) == Decimal("1181.10")


def test_source_policy_falls_back_with_metadata() -> None:
    index = _index(("GBP", "USD", "1.27", "seed", 0))
    # Ask for a source that does not hold this pair -> fall back, flagged.
    result = index.get_fx_rate("GBP", "USD", source_policy="ecb")
    assert result.requested_source == "ecb"
    assert result.effective_source == "seed"
    assert result.fallback_used is True
    assert "seed" in result.available_sources


def test_source_priority_prefers_fixture_over_seed() -> None:
    # Same pair/date from two sources: fx_fixture (20) beats seed (100).
    index = _index(
        ("GBP", "USD", "1.30", "seed", 0),
        ("GBP", "USD", "1.27", "fx_fixture", 0),
    )
    result = index.get_fx_rate("GBP", "USD")
    assert result.source == "fx_fixture"
    assert result.rate == Decimal("1.27")


# --- ingestion via the worker ------------------------------------------------


async def test_fx_ingestion_single_pair(session: AsyncSession) -> None:
    run = await run_job(session, "fx_ingestion", base_currency="GBP", quote_currencies=["USD"])
    assert run.status == "success"
    assert run.source == "fx_fixture"
    assert run.records_inserted == 30  # 30-day default window
    assert run.records_updated == 0
    assert run.records_failed == 0

    pairs = {
        (r.base_currency, r.quote_currency)
        for r in (await session.execute(select(FxRate).where(FxRate.source == "fx_fixture")))
        .scalars()
        .all()
    }
    assert pairs == {("GBP", "USD")}


async def test_fx_ingestion_infers_currencies(session: AsyncSession) -> None:
    run = await run_job(session, "fx_ingestion")
    assert run.status == "success"
    # Seeded data uses GBP (base) + USD + EUR => two inferred quote pairs.
    pairs = {
        (r.base_currency, r.quote_currency)
        for r in (await session.execute(select(FxRate).where(FxRate.source == "fx_fixture")))
        .scalars()
        .all()
    }
    assert pairs == {("GBP", "USD"), ("GBP", "EUR")}
    assert run.records_inserted == 60


async def test_fx_ingestion_is_idempotent(session: AsyncSession) -> None:
    await run_job(session, "fx_ingestion", base_currency="GBP", quote_currencies=["USD"])
    run2 = await run_job(session, "fx_ingestion", base_currency="GBP", quote_currencies=["USD"])
    assert run2.records_inserted == 0
    assert run2.records_updated == 0
    assert run2.status == "success"

    count = await session.scalar(
        select(func.count()).select_from(FxRate).where(FxRate.source == "fx_fixture")
    )
    assert count == 30


async def test_fx_ingestion_updates_only_on_real_change(session: AsyncSession) -> None:
    class Fake:
        name = "fx_fixture"

        def __init__(self, rate: str) -> None:
            self._rate = rate

        async def fetch_rates(self, *, base_currency, quote_currencies, start_date, end_date):
            return [
                FxRateRecord(
                    rate_date=_TODAY,
                    base_currency="GBP",
                    quote_currency="USD",
                    rate=Decimal(self._rate),
                    source="fx_fixture",
                    status="fixture",
                )
            ]

    first = await fx_service.ingest_fx_rates(
        session, Fake("1.30"), base_currency="GBP", quote_currencies=["USD"]
    )
    assert (first.inserted, first.updated) == (1, 0)

    second = await fx_service.ingest_fx_rates(
        session, Fake("1.31"), base_currency="GBP", quote_currencies=["USD"]
    )
    assert (second.inserted, second.updated) == (0, 1)  # corrected rate

    third = await fx_service.ingest_fx_rates(
        session, Fake("1.31"), base_currency="GBP", quote_currencies=["USD"]
    )
    assert (third.inserted, third.updated) == (0, 0)  # identical -> no change


async def test_fx_ingestion_one_bad_pair_does_not_fail_job(session: AsyncSession) -> None:
    class Fake:
        name = "fx_fixture"

        async def fetch_rates(self, *, base_currency, quote_currencies, start_date, end_date):
            good = FxRateRecord(_TODAY, "GBP", "USD", Decimal("1.27"), "fx_fixture", "fixture")
            bad = FxRateRecord(_TODAY, "GBP", "EUR", None, "fx_fixture", "fixture")  # rate None
            return [good, bad]

    counts = await fx_service.ingest_fx_rates(
        session, Fake(), base_currency="GBP", quote_currencies=["USD", "EUR"]
    )
    assert counts.inserted == 1
    assert counts.failed == 1


async def test_fx_ingestion_claims_queued_backfill(session: AsyncSession) -> None:
    queued = JobRun(job_type="fx_ingestion", status="queued")
    session.add(queued)
    await session.commit()
    queued_id = queued.id

    run = await run_job(session, "fx_ingestion", base_currency="GBP", quote_currencies=["USD"])
    assert run.id == queued_id
    assert run.status == "success"


async def test_scheduled_fx_job_runs_real_not_stub(session: AsyncSession) -> None:
    from app.services import jobs as jobs_service

    job = await session.scalar(select(ScheduledJob).where(ScheduledJob.job_type == "fx_ingestion"))
    assert job is not None
    run = await jobs_service.trigger_job(session, job.id)
    assert run.status == "success"
    assert run.status != "success_stub"
    assert (run.records_inserted or 0) > 0


# --- currency-aware valuation ------------------------------------------------


async def test_dashboard_position_uses_fx_for_non_base_currency(
    client: AsyncClient,
) -> None:
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    jegp = next(p for p in body["positions"] if p["ticker"] == "JEGP")  # USD listing
    assert jegp["listing_currency"] == "USD"
    assert jegp["base_currency"] == "GBP"
    assert jegp["market_value_local"] == "1500.00"  # 30 * 50 USD
    assert jegp["market_value_base"] == "1181.10"  # / 1.27
    assert jegp["fx_status"] == "fresh"
    assert jegp["fx_source"] == "seed"
    assert jegp["fx_rate"] is not None

    vusa = next(p for p in body["positions"] if p["ticker"] == "VUSA")  # GBP listing
    assert vusa["fx_rate"] == "1.0000000000"
    assert vusa["market_value_base"] == "7500.00"
    # Total is in base currency and unchanged by the FX overlay.
    assert body["portfolio_summary"]["total_market_value"] == "12381.10"


async def test_dashboard_distributions_carry_base_currency_overlay(
    client: AsyncClient,
) -> None:
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    usd_dist = next(d for d in body["distributions"] if d["currency"] == "USD")
    assert usd_dist["base_currency"] == "GBP"
    assert usd_dist["amount_base"] is not None
    assert usd_dist["fx_status"] in {"fresh", "stale"}
    # The original declared amount/currency is preserved.
    assert usd_dist["currency"] == "USD"


async def _isolated_position(
    session: AsyncSession,
    *,
    isin: str,
    currency: str,
    workspace_id: int,
    price: str = "100",
    units: str = "100",
) -> FundListing:
    listing = FundListing(
        ticker=isin[-4:],
        trading_currency=currency,
        currency_unit=currency,
        status="active",
        prices=[Price(price_date=_TODAY, price=Decimal(price), currency=currency, source="stooq")],
    )
    fund = Fund(isin=isin, name=f"Fund {isin}", status="active", listings=[listing])
    session.add(fund)
    await session.flush()
    session.add(
        PortfolioPosition(
            workspace_id=workspace_id, fund_listing_id=listing.id, units=Decimal(units)
        )
    )
    await session.flush()
    return listing


async def test_dashboard_marks_missing_fx(client: AsyncClient, session: AsyncSession) -> None:
    # A JPY position with no JPY rate anywhere -> base value unavailable, flagged.
    await _isolated_position(session, isin="JP00MISSING1", currency="JPY", workspace_id=1)
    await session.commit()

    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    jpy = next(p for p in body["positions"] if p["currency"] == "JPY")
    assert jpy["market_value_local"] is not None  # local value still shown
    assert jpy["market_value_base"] is None  # not silently converted
    assert jpy["fx_status"] == "missing"

    diag = body["data_quality"]
    assert diag["unconverted_positions"] >= 1
    assert diag["missing_fx_rates"] >= 1


async def test_diagnostics_counts_stale_fx(session: AsyncSession) -> None:
    ws = Workspace(name="StaleFx", base_currency="GBP")
    session.add(ws)
    await session.flush()
    await _isolated_position(session, isin="SE00STALE001", currency="SEK", workspace_id=ws.id)
    # Only a long-stale GBP/SEK rate exists -> conversion works but is stale.
    session.add(
        FxRate(
            rate_date=_TODAY - timedelta(days=40),
            base_currency="GBP",
            quote_currency="SEK",
            rate=Decimal("13.37"),
            source="fx_fixture",
            status="fixture",
        )
    )
    await session.commit()

    diag = await diagnostics_service.workspace_diagnostics(session, ws.id)
    assert diag.stale_fx_rates >= 1
    assert diag.unconverted_positions == 0  # stale, but still converted


# --- endpoints ---------------------------------------------------------------


async def test_fx_rates_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    await run_job(session, "fx_ingestion", base_currency="GBP", quote_currencies=["USD"])
    body = (await client.get("/api/v1/fx/rates?base=GBP&quote=USD&source=fx_fixture")).json()
    assert body["meta"]["count"] >= 1
    assert all(r["base_currency"] == "GBP" and r["quote_currency"] == "USD" for r in body["data"])
    assert all(r["status"] == "fixture" for r in body["data"])


async def test_fx_time_series_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    await run_job(session, "fx_ingestion", base_currency="GBP", quote_currencies=["USD"])
    body = (await client.get("/api/v1/fx/time-series?base=GBP&quote=USD&range=1m")).json()
    assert body["subject"] == {"type": "fx_pair", "id": "GBP/USD", "label": "GBP/USD"}
    assert body["kind"] == "fx"
    assert body["status"] == "active"
    assert len(body["points"]) >= 28  # ~30-day window
    # Most recent point is the clean anchor.
    assert body["points"][-1]["value"] == "1.2700000000"


async def test_fx_time_series_inverts_when_only_opposite_stored(
    client: AsyncClient, session: AsyncSession
) -> None:
    await run_job(session, "fx_ingestion", base_currency="GBP", quote_currencies=["USD"])
    body = (await client.get("/api/v1/fx/time-series?base=USD&quote=GBP&range=1m")).json()
    assert body["subject"]["id"] == "USD/GBP"
    assert body["points"]
    # Inverted: USD/GBP ≈ 0.79 (< 1).
    assert Decimal(body["points"][-1]["value"]) < Decimal("1")


async def test_fx_convert_endpoint_metadata(client: AsyncClient) -> None:
    # Seed ships GBP/USD = 1.27; USD->GBP is the inverse.
    body = (await client.get("/api/v1/fx/convert?from=USD&to=GBP&amount=100")).json()
    assert body["from_currency"] == "USD"
    assert body["to_currency"] == "GBP"
    assert body["is_inverse"] is True
    assert Decimal(body["converted_amount"]).quantize(Decimal("0.01")) == Decimal("78.74")
    assert body["status"] == "fresh"


async def test_fx_convert_missing_is_explicit(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/fx/convert?from=JPY&to=SEK&amount=100")).json()
    assert body["converted_amount"] is None
    assert body["status"] == "missing"
    assert body["missing_reason"] == "no_fx_path"


# --- capability registry -----------------------------------------------------


async def test_fx_capability_registered_and_implemented(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/data-sources/capabilities?data_type=fx_rates")).json()
    names = {c["source_name"] for c in body["data"]}
    assert "fx_fixture" in names
    fixture = next(c for c in body["data"] if c["source_name"] == "fx_fixture")
    assert fixture["adapter_status"] == "implemented"
    assert fixture["source_type"] == "fx"


async def test_capabilities_endpoint_marks_fx_fixture(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["fx_ingestion"] == "fixture"
    fx_status = {d["name"]: d["status"] for d in body["data_types"]}["fx_rates"]
    assert fx_status == "fixture"
    assert body["configured_sources"]["fx"] == "fx_fixture"
    assert body["environment"]["fx_source_default"] == "fx_fixture"
