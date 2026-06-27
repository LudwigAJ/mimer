"""Bounded, safe live verification of a target fund's data sources.

All offline: the live adapters' single HTTP hop (``_download`` / ``StooqSource.fetch``)
is stubbed so every call still flows through ``guarded_fetch`` (cache → budget → fetch
log) while never touching the network. Guards the verifier's honesty contract: facts/
nav/documents are never fetched (no live adapter), a blocked provider never fails the
run, ``verified`` only follows a real fetch+parse, and nothing is promoted or stored.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FundHolding
from app.services import fund_source_verification as fsv
from app.sources import issuer_source_config
from app.sources.base import PricePoint
from app.sources.holdings import IsharesHoldingsSource, JPMorganHoldingsSource
from app.sources.stooq import StooqSource
from app.workers.run import run_job

_JEPG = "IE0003UVYC20"

# A clean iShares holdings CSV (name + ISIN + weight => the expected shape).
_ISHARES_CSV = (
    "as of 2026-06-20\n"
    "Name,ISIN,Sector,Weight (%)\n"
    "ASTRAZENECA PLC,GB0009895292,Health Care,8.00\n"
    "SHELL PLC,GB00BP6MXD84,Energy,7.50\n"
)
# Old binary .xls (OLE2/BIFF) — exactly what JEPG serves; deliberately NOT decoded.
_OLE2_XLS = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + b"Workbook"


def _patch_download(monkeypatch: pytest.MonkeyPatch, cls: type, payload) -> dict:  # noqa: ANN001
    calls: dict = {"n": 0}

    async def fake_download(self, url: str):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return payload

    monkeypatch.setattr(cls, "_download", fake_download)
    return calls


def _patch_stooq(
    monkeypatch: pytest.MonkeyPatch, *, points: list[PricePoint] | None = None
) -> dict:
    calls: dict = {"n": 0}

    async def fake_fetch(self, *, ticker, exchange=None, currency=None):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return (
            points
            if points is not None
            else [
                PricePoint(price_date=date(2026, 6, 20), price=Decimal("75.00"), currency=currency)
            ]
        )

    monkeypatch.setattr(StooqSource, "fetch", fake_fetch)
    return calls


def _cell(fund: fsv.FundVerification, data_type: str) -> fsv.DataTypeVerification:
    matches = [r for r in fund.results if r.data_type == data_type]
    assert matches, data_type
    return matches[0]


# --- facts / nav / documents: never fetched ----------------------------------


async def test_facts_nav_documents_are_skipped_no_live_source(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with the network hard-blocked, the no-live-source cells must not attempt a fetch.
    async def explode(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("verifier must not touch the network for facts/nav/documents")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    monkeypatch.setattr(httpx.AsyncClient, "post", explode)
    # Block the price probe too (we only assert the no-source cells here).
    monkeypatch.setattr(StooqSource, "fetch", explode)

    report = await fsv.verify_fund_sources(session, fund_symbol="ISF")
    fund = report.funds[0]
    for data_type in ("facts", "nav", "documents"):
        cell = _cell(fund, data_type)
        assert cell.outcome == fsv.SKIPPED_NO_LIVE_SOURCE
        assert cell.attempted_live is False
        assert cell.ok is False


# --- holdings: verified vs blocked vs zero-rows ------------------------------


async def test_isf_holdings_verifies_with_live_csv(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch, IsharesHoldingsSource, _ISHARES_CSV)
    _patch_stooq(monkeypatch)
    report = await fsv.verify_fund_sources(session, fund_symbol="ISF")
    holdings = _cell(report.funds[0], "holdings")
    assert holdings.outcome == fsv.VERIFIED
    assert holdings.ok and holdings.attempted_live
    assert holdings.row_count >= 1
    assert report.verified_count >= 1


async def test_jepg_holdings_blocked_by_binary_xls(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch, JPMorganHoldingsSource, _OLE2_XLS)
    report = await fsv.verify_fund_sources(session, fund_symbol="JEPG")
    holdings = _cell(report.funds[0], "holdings")
    # A 200 that returns an undecoded binary workbook is a blocker, never a verify.
    assert holdings.outcome == fsv.BLOCKED
    assert holdings.ok is False


async def test_zero_row_payload_does_not_verify(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reachable endpoint that returns no usable rows must stay blocked, not verified.
    _patch_download(monkeypatch, IsharesHoldingsSource, "Name,ISIN,Weight (%)\n")
    report = await fsv.verify_fund_sources(session, fund_symbol="ISF")
    holdings = _cell(report.funds[0], "holdings")
    assert holdings.outcome != fsv.VERIFIED
    assert holdings.ok is False


# --- distributions / planned: no usable config => blocked, no network -------


async def test_planned_distributions_blocked_without_network(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ISF distributions is planned (no usable config): blocked, and it must NOT fetch.
    async def explode(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("must not fetch a data type with no usable live config")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    report = await fsv.verify_fund_sources(session, fund_symbol="ISF")
    dist = _cell(report.funds[0], "distributions")
    assert dist.outcome == fsv.BLOCKED
    assert dist.attempted_live is False
    assert dist.known_blocker


# --- partial failures never fail the whole run ------------------------------


async def test_partial_failure_isolated_run_still_succeeds(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch, IsharesHoldingsSource, _ISHARES_CSV)  # ISF verifies

    async def boom(self, url: str):  # noqa: ANN001, ANN202 - JPM provider raises
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(JPMorganHoldingsSource, "_download", boom)
    _patch_stooq(monkeypatch)

    report = await fsv.verify_fund_sources(session, all_target_funds=True)
    assert len(report.funds) == 3
    assert report.verified_count >= 1  # ISF holdings still verified
    # The run as a whole is not aborted by JPM blowing up — every fund produced cells.
    for fund in report.funds:
        assert len(fund.results) == len(fsv.fund_coverage.FUND_DATA_TYPES)


# --- verify never promotes or stores ----------------------------------------


async def test_verify_does_not_promote_config_or_store_rows(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch, JPMorganHoldingsSource, _OLE2_XLS)
    before = issuer_source_config.config_status(_JEPG, "jpmorgan_etf_holdings")
    holdings_before = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.source == "jpmorgan_etf_holdings")
    )
    await fsv.verify_fund_sources(session, fund_symbol="JEPG")
    after = issuer_source_config.config_status(_JEPG, "jpmorgan_etf_holdings")
    holdings_after = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.source == "jpmorgan_etf_holdings")
    )
    assert before == after == issuer_source_config.CANDIDATE  # never auto-promoted
    assert holdings_before == holdings_after  # verify-only: nothing ingested


# --- listing price probe -----------------------------------------------------


async def test_listing_price_probe_verifies_with_mocked_stooq(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_stooq(monkeypatch)
    report = await fsv.verify_fund_sources(session, fund_symbol="VUSA")
    price = _cell(report.funds[0], "listing_price")
    assert price.outcome == fsv.VERIFIED
    assert price.source_name == "stooq"
    assert price.row_count >= 1


# --- worker entry point ------------------------------------------------------


async def test_worker_records_verification_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch, IsharesHoldingsSource, _ISHARES_CSV)
    _patch_stooq(monkeypatch)
    run = await run_job(session, "verify_fund_sources", all_target_funds=True, limit=10)
    assert run.job_type == "verify_fund_sources"
    assert run.status in ("success", "partial_success")
    # Verify-only: nothing is ever stored.
    assert run.records_inserted == 0 and run.records_updated == 0
    assert "verify_fund_sources" in (run.message or "")
    assert "ISF[" in run.message


async def test_worker_rejects_unknown_fund_symbol(session: AsyncSession) -> None:
    run = await run_job(session, "verify_fund_sources", fund_symbol="NOPE")
    assert run.status == "failed"
    assert "not a target fund" in (run.message or "")


async def test_worker_limit_bounds_fund_count(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_stooq(monkeypatch)
    report = await fsv.verify_fund_sources(session, all_target_funds=True, limit=1)
    assert len(report.funds) == 1
