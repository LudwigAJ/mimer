"""Known issuer source-config registry, verification helper, worker + planner wiring.

All offline — the live adapters' single HTTP call (``_download``) is stubbed so the
guarded fetch path (recent-success cache → source budget → fetch log) is exercised
without touching the network. Covers the registry lookups, the verify-only helper,
the worker's known-config lookup (live ``--source`` with no ``--url``), the planner's
known-config/needs-url awareness, diagnostics counts and capabilities metadata.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Distribution, Fund, FundHolding
from app.services import diagnostics as diagnostics_service
from app.services import issuer_source_verification as verification
from app.services import market_data_planner as planner
from app.services import source_budget, source_requests
from app.sources import issuer_source_config as cfg
from app.sources.distributions import VanguardDistributionsSource
from app.sources.holdings import IsharesHoldingsSource, JPMorganHoldingsSource
from app.workers.run import run_job

_ISF = "IE0005042456"  # iShares Core FTSE 100 (candidate holdings config)
_JEPG = "IE0003UVYC20"  # JPM Global Equity Premium Income (candidate holdings config)
_VUSA = "IE00B3XXRP09"  # Vanguard S&P 500 (candidate distribution config)
_ISHARES = "blackrock_ishares_holdings"
_JPM_HOLD = "jpmorgan_etf_holdings"
_VANGUARD_DIST = "vanguard_distributions"


# --- realistic provider samples ----------------------------------------------

_ISHARES_CSV = (
    "Holdings\n"
    "Ticker,Name,Weight (%),ISIN\n"
    "AZN,ASTRAZENECA PLC,8.10,GB0009895292\n"
    "SHEL,SHELL PLC,7.60,GB00BP6MXD84\n"
    "HSBA,HSBC HOLDINGS PLC,7.00,GB0005405286\n"
)

_JPM_CSV = (
    "JPM Global Equity Premium Income\n"
    "Ticker,Security Description,% of Net Assets,ISIN\n"
    "MSFT,MICROSOFT CORP,1.90,US5949181045\n"
    "AAPL,APPLE INC,1.80,US0378331005\n"
)

_VANGUARD_JSONP = (
    "callback("
    '{"fundData": {"portId": "9503", "distributionHistory": ['
    '{"exDividendDate": "2025-03-20", "distributionAmount": "0.3100", "currency": "USD", '
    '"frequency": "Quarterly"},'
    '{"exDividendDate": "2025-06-19", "distributionAmount": "0.3250", "currency": "USD", '
    '"frequency": "Quarterly"}'
    "]}});"
)

# A clean JSON payload with NO distribution-history list (parser finds nothing) — the
# Vanguard candidate must stay candidate, never promoted.
_VANGUARD_NO_ROWS = '{"fundData": {"portId": "9503", "fundName": "Vanguard S&P 500"}}'


# --- registry lookups --------------------------------------------------------


def test_registry_lookup_by_isin_and_source() -> None:
    isf = cfg.get_source_config(_ISF, _ISHARES)
    assert isf is not None
    assert isf.data_type == cfg.DATA_TYPE_HOLDINGS
    assert isf.provider == "blackrock_ishares"
    assert isf.ticker == "ISF"
    # Promoted to verified after a clean live --verify-source check (107 holdings).
    assert isf.source_status == cfg.VERIFIED
    assert isf.verified_at is not None
    assert "ISF_holdings" in isf.url
    # Wrong source for this ISIN -> no match.
    assert cfg.get_source_config(_ISF, _JPM_HOLD) is None
    assert cfg.get_source_config("ZZ0000000000", _ISHARES) is None


def test_registry_data_type_and_status_filtering() -> None:
    holdings = cfg.list_source_configs(data_type=cfg.DATA_TYPE_HOLDINGS)
    distributions = cfg.list_source_configs(data_type=cfg.DATA_TYPE_DISTRIBUTIONS)
    assert {c.source_name for c in holdings} == {_ISHARES, _JPM_HOLD}
    assert {c.source_name for c in distributions} == {_VANGUARD_DIST}
    # ISF holdings is verified (clean live check); JEPG holdings + VUSA dist stay candidate.
    assert {c.source_name for c in cfg.list_source_configs(status=cfg.VERIFIED)} == {_ISHARES}
    assert len(cfg.list_source_configs(status=cfg.CANDIDATE)) == 2


def test_known_source_url_respects_usable_status() -> None:
    # A candidate config is usable: its URL is returned for the matching source.
    assert cfg.known_source_url(_ISF, _ISHARES) is not None
    assert cfg.known_source_name(_ISF, cfg.DATA_TYPE_HOLDINGS) == _ISHARES
    assert cfg.known_source_name(_VUSA, cfg.DATA_TYPE_DISTRIBUTIONS) == _VANGUARD_DIST
    # A planned/disabled config would not be usable.
    planned = cfg.IssuerSourceConfig(
        fund_isin="XX",
        provider="x",
        data_type=cfg.DATA_TYPE_HOLDINGS,
        source_name="x",
        url="u",
        source_status=cfg.PLANNED,
    )
    assert planned.is_usable is False


def test_example_identifiers_and_status_counts() -> None:
    assert cfg.example_identifiers(_ISHARES) == [f"ISF:{_ISF}"]
    counts = cfg.status_counts()
    assert counts[cfg.VERIFIED] == 1  # ISF holdings
    assert counts[cfg.CANDIDATE] == 2  # JEPG holdings + VUSA distributions
    assert cfg.status_counts(data_type=cfg.DATA_TYPE_DISTRIBUTIONS)[cfg.CANDIDATE] == 1


# --- patch helpers -----------------------------------------------------------


def _patch_ishares(monkeypatch, *, text=_ISHARES_CSV, calls=None):  # noqa: ANN001
    async def fake_download(self, url: str) -> str:  # noqa: ANN001
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
            calls["url"] = url
        return text

    monkeypatch.setattr(IsharesHoldingsSource, "_download", fake_download)


def _patch_jpmorgan(monkeypatch, *, text=_JPM_CSV):  # noqa: ANN001
    async def fake_download(self, url: str) -> str:  # noqa: ANN001
        return text

    monkeypatch.setattr(JPMorganHoldingsSource, "_download", fake_download)


def _patch_vanguard(monkeypatch, *, text=_VANGUARD_JSONP, calls=None):  # noqa: ANN001
    async def fake_download(self, url: str) -> str:  # noqa: ANN001
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
            calls["url"] = url
        return text

    monkeypatch.setattr(VanguardDistributionsSource, "_download", fake_download)


async def _fund(session: AsyncSession, isin: str) -> Fund:
    fund = await session.scalar(select(Fund).where(Fund.isin == isin))
    assert fund is not None
    return fund


# --- hardening: a binary/garbled payload is a clean no-op, never a crash ------


def test_content_sniff_binary_payload_is_clean_noop() -> None:
    from app.sources.distributions import parse_jpmorgan_distributions
    from app.sources.holdings import parse_jpmorgan_holdings

    # A real .xls body (binary; csv.reader would raise on the embedded NULs). Both
    # content-sniffing parsers must return [] (binary Excel is deferred), never raise.
    binary = "PK\x03\x04" + ("\x00" * 64) + "rId1 binary-xls-body \x00 sheet1"
    assert parse_jpmorgan_holdings(binary) == []
    assert parse_jpmorgan_distributions(binary) == []


async def test_verify_binary_payload_keeps_candidate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The JPM endpoint sometimes returns a binary .xls -> verify reports 0 rows and
    # keeps the config candidate (never crashes, never promotes).
    _patch_jpmorgan(monkeypatch, text="PK\x03\x04" + ("\x00" * 32) + "binary")
    fund = await _fund(session, _JEPG)
    report = await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_JPM_HOLD, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    assert report.ok is False
    assert report.row_count == 0
    assert report.recommended_status == cfg.CANDIDATE


# --- worker: known-config lookup (live --source, no --url) -------------------


async def test_distribution_worker_runs_known_config_without_url(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_vanguard(monkeypatch, calls=calls)
    fund = await _fund(session, _VUSA)
    # No --url: the adapter resolves the candidate Vanguard config URL for this ISIN.
    run = await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_VANGUARD_DIST
    )
    assert run.status == "success"
    assert run.records_inserted == 2
    assert calls["n"] == 1
    assert "portId:9503" in calls["url"]  # the configured product-data URL was used
    rows = await session.scalar(
        select(func.count())
        .select_from(Distribution)
        .where(Distribution.fund_id == fund.id, Distribution.source == _VANGUARD_DIST)
    )
    assert rows == 2


async def test_distribution_missing_config_is_clean_noop(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # JEPG has no Vanguard distribution config, and no --url -> clean no-op (no call).
    async def boom(self, url: str):  # noqa: ANN001
        raise AssertionError("no config and no --url, yet a download was attempted")

    monkeypatch.setattr(VanguardDistributionsSource, "_download", boom)
    fund = await _fund(session, _JEPG)
    run = await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_VANGUARD_DIST
    )
    assert run.status == "success"
    assert run.records_inserted == 0
    assert "no_provider_match=1" in (run.message or "")


async def test_distribution_url_override_wins_over_config(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_vanguard(monkeypatch, calls=calls)
    fund = await _fund(session, _VUSA)
    override = "https://api.vanguard.com/rs/gre/gra/1.7.0/datasets/OTHER.json?vars=portId:1234"
    run = await run_job(
        session,
        "distribution_ingestion",
        fund_id=fund.id,
        source_name=_VANGUARD_DIST,
        url=override,
    )
    assert run.records_inserted == 2
    assert calls["url"] == override  # explicit --url beats the configured URL


async def test_distribution_workspace_scope_selects_only_configured_funds(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_vanguard(monkeypatch)
    # Workspace 1 holds the seeded funds; only VUSA has a Vanguard distribution config,
    # so a workspace run fetches VUSA only (the rest are a clean no-op in the same run).
    run = await run_job(
        session, "distribution_ingestion", workspace_id=1, source_name=_VANGUARD_DIST
    )
    assert run.status == "success"
    assert run.records_inserted == 2  # VUSA only
    assert "selected_funds=" in (run.message or "")


async def test_distribution_workspace_limit_zero_selects_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_vanguard(monkeypatch)
    run = await run_job(
        session, "distribution_ingestion", workspace_id=1, source_name=_VANGUARD_DIST, limit=0
    )
    assert run.status == "success"
    assert run.records_inserted == 0  # --limit 0 selects no funds (bounded)
    assert "No eligible funds" in (run.message or "")


# --- verification helper -----------------------------------------------------


async def test_verify_ishares_mocked_payload_ok(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ishares(monkeypatch)
    fund = await _fund(session, _ISF)
    report = await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_ISHARES, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    assert report.ok is True
    assert report.fetch_outcome == verification.SUCCESS
    assert report.row_count == 3
    assert report.has_expected_fields is True
    assert report.recommended_status == cfg.VERIFIED
    assert report.config_found is True and report.config_status == cfg.VERIFIED
    assert report.sample  # small canonical sample, not the whole payload
    assert len(report.sample) <= 3


async def test_verify_jpmorgan_mocked_payload_ok(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_jpmorgan(monkeypatch)
    fund = await _fund(session, _JEPG)
    report = await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_JPM_HOLD, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    assert report.ok is True
    assert report.row_count == 2
    assert report.recommended_status == cfg.VERIFIED


async def test_verify_vanguard_candidate_stays_candidate_without_expected_rows(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The endpoint returns clean JSON but with NO distribution history -> not verifiable.
    _patch_vanguard(monkeypatch, text=_VANGUARD_NO_ROWS)
    fund = await _fund(session, _VUSA)
    report = await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_VANGUARD_DIST, data_type=cfg.DATA_TYPE_DISTRIBUTIONS
    )
    assert report.ok is False
    assert report.row_count == 0
    # Stays candidate — never auto-promoted to verified on an empty/unusable payload.
    assert report.recommended_status == cfg.CANDIDATE


async def test_verify_does_not_ingest(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ishares(monkeypatch)
    fund = await _fund(session, _ISF)
    await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_ISHARES, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    # Verify-only: NO canonical holdings rows are written for the live source.
    count = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.fund_id == fund.id, FundHolding.source == _ISHARES)
    )
    assert count == 0


async def test_verify_budget_block_makes_no_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await source_budget.apply_backoff(session, _ISHARES, seconds=120)
    await session.commit()

    async def boom(self, url: str):  # noqa: ANN001
        raise AssertionError("a live call was attempted while in backoff")

    monkeypatch.setattr(IsharesHoldingsSource, "_download", boom)
    fund = await _fund(session, _ISF)
    report = await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_ISHARES, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    assert report.fetch_outcome == verification.BUDGET_BLOCKED
    assert report.ok is False
    rate_limited = await source_requests.list_fetch_logs(
        session, source=_ISHARES, status="rate_limited"
    )
    assert rate_limited


async def test_verify_fetch_log_is_safe(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ishares(monkeypatch)
    fund = await _fund(session, _ISF)
    await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_ISHARES, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    logs = await source_requests.list_fetch_logs(session, source=_ISHARES, status="success")
    assert logs
    log = logs[0]
    assert log.endpoint_label and "?" not in log.endpoint_label
    assert "APIKEY" not in (log.request_key or "").upper()


async def test_verify_unknown_source_is_reported(session: AsyncSession) -> None:
    # An offline fixture source has no live endpoint to verify (even with a URL).
    report = await verification.verify_issuer_source_config(
        session,
        isin=_ISF,
        source_name="holdings_fixture",
        data_type=cfg.DATA_TYPE_HOLDINGS,
        url="https://example.com/holdings.csv",
    )
    assert report.fetch_outcome == verification.UNKNOWN_SOURCE
    assert report.ok is False


# --- worker --verify-source path ---------------------------------------------


async def test_holdings_verify_source_worker(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ishares(monkeypatch)
    fund = await _fund(session, _ISF)
    run = await run_job(
        session,
        "issuer_holdings_ingestion",
        fund_id=fund.id,
        source_name=_ISHARES,
        verify_source=True,
    )
    assert run.status == "success"
    assert run.records_inserted == 0  # verify-only: nothing ingested
    assert "ok=True" in (run.message or "")
    assert "recommended_status=verified" in (run.message or "")
    # No canonical holdings written.
    count = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.fund_id == fund.id, FundHolding.source == _ISHARES)
    )
    assert count == 0


async def test_verify_source_requires_fund_id(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_vanguard(monkeypatch)
    run = await run_job(
        session,
        "distribution_ingestion",
        workspace_id=1,
        source_name=_VANGUARD_DIST,
        verify_source=True,
    )
    assert run.status == "failed"
    assert "--fund-id" in (run.message or "")


# --- planner integration -----------------------------------------------------


async def test_planner_refresh_holdings_uses_known_config(
    session: AsyncSession,
) -> None:
    fund = await _fund(session, _ISF)
    await session.execute(FundHolding.__table__.delete().where(FundHolding.fund_id == fund.id))
    await session.commit()
    plan = await planner.build_plan(session, 1)
    items = [
        i for i in plan.items if i.item_type == "refresh_holdings" and i.related_fund_id == fund.id
    ]
    assert len(items) == 1
    item = items[0]
    assert item.known_config is True
    assert item.config_status == cfg.VERIFIED  # ISF holdings is verified
    assert item.needs_url_config is False
    assert "issuer_holdings_ingestion" in (item.recommended_command or "")
    assert _ISHARES in (item.recommended_command or "")
    assert "--url" not in (item.recommended_command or "")


async def test_planner_refresh_holdings_flags_missing_config(
    session: AsyncSession,
) -> None:
    fund = await _fund(session, _VUSA)  # no holdings config for VUSA
    await session.execute(FundHolding.__table__.delete().where(FundHolding.fund_id == fund.id))
    await session.commit()
    plan = await planner.build_plan(session, 1)
    items = [
        i for i in plan.items if i.item_type == "refresh_holdings" and i.related_fund_id == fund.id
    ]
    assert len(items) == 1
    item = items[0]
    assert item.known_config is False
    assert item.config_status is None
    assert item.needs_url_config is True
    assert "configure" in (item.recommended_command or "").lower()


# --- diagnostics -------------------------------------------------------------


async def test_diagnostics_issuer_source_config_counts(session: AsyncSession) -> None:
    diag = await diagnostics_service.workspace_diagnostics(session, 1)
    assert diag.issuer_source_configs == 3
    assert diag.verified_issuer_source_configs == 1  # ISF holdings
    assert diag.candidate_issuer_source_configs == 2  # JEPG holdings + VUSA distributions
    # Workspace 1 holds funds without a holdings config (e.g. VUSA) and without a
    # distribution config (e.g. ISF/JEPG) -> some "missing" coverage, informational.
    assert diag.missing_holdings_source_config >= 1
    assert diag.missing_distribution_source_config >= 1


# --- capabilities metadata ---------------------------------------------------


async def test_capabilities_expose_known_config_metadata(client) -> None:
    body = (await client.get("/api/v1/data-sources/capabilities?data_type=holdings")).json()
    by_name = {c["source_name"]: c for c in body["data"]}

    ishares = by_name[_ISHARES]
    assert ishares["requires_url"] is True
    assert ishares["known_config_available"] is True
    assert ishares["config_status"] == cfg.VERIFIED  # ISF promoted after live check
    assert f"ISF:{_ISF}" in ishares["example_fund_identifiers"]

    # The offline fixture needs no URL and has no per-fund config.
    fixture = by_name["holdings_fixture"]
    assert fixture["requires_url"] is False
    assert fixture["known_config_available"] is False
    assert fixture["example_fund_identifiers"] == []

    dist = (await client.get("/api/v1/data-sources/capabilities?data_type=distributions")).json()
    vanguard = {c["source_name"]: c for c in dist["data"]}[_VANGUARD_DIST]
    assert vanguard["requires_url"] is True
    assert vanguard["known_config_available"] is True
    assert vanguard["config_status"] == cfg.CANDIDATE
