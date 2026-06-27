"""Live issuer distribution adapters: pure parsers + guarded live ingestion.

All offline — the live adapters' single HTTP call (``_download``) is stubbed so the
call still flows through ``guarded_fetch`` (recent-success cache + source budget +
fetch log) while never touching the network. Mirrors the holdings/rates
live-adapter tests.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Distribution, Fund
from app.services import market_data_planner as planner
from app.services import source_budget, source_requests
from app.sources import registry
from app.sources.distributions import (
    JPMorganDistributionsSource,
    VanguardDistributionsSource,
    get_distribution_source,
    known_distribution_source,
    parse_jpmorgan_distributions,
    parse_vanguard_distributions,
    parse_vanguard_distributions_json,
)
from app.workers.run import run_job

_VUSA = "IE00B3XXRP09"  # Vanguard S&P 500 (export sample + fixture)
_JEPG = "IE0003UVYC20"  # JPM Global Equity Premium Income (fixture)
_JPM = "jpmorgan_distributions"
_VANGUARD = "vanguard_distributions"
_VANGUARD_EXPORT = "vanguard_distributions_export"


# --- realistic provider samples ----------------------------------------------

# JPM CSV-like distribution export: a fund-name preamble, ex/record/payment dates,
# amount + currency + type + frequency + share class, and a bad (non-numeric amount)
# row that must be isolated.
_JPM_CSV = (
    "JPMorgan Global Equity Premium Income Active UCITS ETF — Distributions\n"
    "Ex-Date,Record Date,Payment Date,Distribution Amount,Currency,Distribution Type,"
    "Frequency,Share Class\n"
    "2026-01-02,2026-01-03,2026-01-15,0.3500,USD,Income,Monthly,Acc USD\n"
    "2026-02-02,2026-02-03,2026-02-15,0.3450,USD,Income,Monthly,Acc USD\n"
    "2026-03-02,2026-03-03,2026-03-15,n/a,USD,Income,Monthly,Acc USD\n"  # bad amount
)

# JPM Excel-as-HTML distribution export (content-sniffed): a real <table>, a "$"
# amount with no separate currency column, US-style dates.
_JPM_HTML = (
    "<html><head><title>Distributions</title></head><body>"
    "<table>"
    "<tr><th>Ex Date</th><th>Pay Date</th><th>Distribution Amount (USD)</th>"
    "<th>Type</th></tr>"
    "<tr><td>03/20/2025</td><td>04/10/2025</td><td>$0.3100</td><td>Income</td></tr>"
    "<tr><td>06/19/2025</td><td>07/10/2025</td><td>$0.3250</td><td>Income</td></tr>"
    "</table></body></html>"
)

# Vanguard product-data distributionHistory JSON (nested under fundData), with a
# JSONP wrapper to exercise the unwrap path, and a bad row (missing amount).
_VANGUARD_JSONP = (
    "callback("
    '{"fundData": {"portId": "9503", "distributionHistory": ['
    '{"exDividendDate": "2025-03-20", "recordDate": "2025-03-21", '
    '"payableDate": "2025-04-10", "distributionAmount": "0.3100", "currency": "USD", '
    '"distributionType": "income", "frequency": "Quarterly"},'
    '{"exDividendDate": "2025-06-19", "recordDate": "2025-06-20", '
    '"payableDate": "2025-07-10", "distributionAmount": "0.3250", "currency": "USD", '
    '"distributionType": "income", "frequency": "Quarterly"},'
    '{"exDividendDate": "2025-09-18", "currency": "USD"}'  # bad row: no amount
    "]}});"
)

_VANGUARD_CSV = (
    "Vanguard S&P 500 UCITS ETF — Distribution history\n"
    "Ex-dividend date,Record date,Payable date,Distribution per share,Currency\n"
    "20/03/2025,21/03/2025,10/04/2025,0.3100,USD\n"
    "19/06/2025,20/06/2025,10/07/2025,0.3250,USD\n"
)


# --- JPM parser --------------------------------------------------------------


def test_jpmorgan_parser_maps_columns_and_isolates_bad_rows() -> None:
    records = parse_jpmorgan_distributions(_JPM_CSV)
    # Two good rows; the "n/a" amount row is isolated.
    assert len(records) == 2
    first = records[0]
    assert first.ex_date == date(2026, 1, 2)
    assert first.record_date == date(2026, 1, 3)
    assert first.payment_date == date(2026, 1, 15)
    assert first.amount == Decimal("0.35000000")
    assert first.currency == "USD"
    assert first.distribution_type == "Income"
    assert first.frequency == "Monthly"
    assert first.share_class == "Acc USD"
    assert first.source == _JPM
    assert first.status == "paid"


def test_jpmorgan_parser_content_sniffs_html_and_header_currency() -> None:
    records = parse_jpmorgan_distributions(_JPM_HTML)
    assert len(records) == 2
    first = records[0]
    # US-style date, "$" amount, currency from the "(USD)" header suffix.
    assert first.ex_date == date(2025, 3, 20)
    assert first.payment_date == date(2025, 4, 10)
    assert first.amount == Decimal("0.31000000")
    assert first.currency == "USD"
    assert first.distribution_type == "Income"


def test_jpmorgan_parser_tolerates_missing_optional_columns() -> None:
    minimal = "Ex Date,Amount,Currency\n2025-03-20,0.31,USD\n2025-06-19,0.33,USD\n"
    records = parse_jpmorgan_distributions(minimal)
    assert len(records) == 2
    assert records[0].record_date is None  # absent, not an error
    assert records[0].payment_date is None
    assert records[0].amount == Decimal("0.31000000")


def test_jpmorgan_parser_empty_on_garbage() -> None:
    assert parse_jpmorgan_distributions("not,a,distribution,file\n1,2,3,4\n") == []


def test_distribution_row_missing_currency_is_skipped() -> None:
    # No currency column, no symbol, no header suffix -> the row cannot be keyed.
    records = parse_jpmorgan_distributions("Ex Date,Amount\n2025-03-20,0.31\n")
    assert records == []


# --- Vanguard parser ---------------------------------------------------------


def test_vanguard_json_parser_parses_jsonp_and_nested_history() -> None:
    records = parse_vanguard_distributions_json(_VANGUARD_JSONP, source=_VANGUARD)
    # Two good rows; the amount-less row is isolated.
    assert len(records) == 2
    first = records[0]
    assert first.ex_date == date(2025, 3, 20)
    assert first.record_date == date(2025, 3, 21)
    assert first.payment_date == date(2025, 4, 10)
    assert first.amount == Decimal("0.31000000")
    assert first.currency == "USD"
    assert first.distribution_type == "income"
    assert first.frequency == "Quarterly"
    assert first.source == _VANGUARD


def test_vanguard_dispatcher_parses_csv_export() -> None:
    records = parse_vanguard_distributions(_VANGUARD_CSV, source=_VANGUARD_EXPORT)
    assert len(records) == 2
    apple = records[0]
    assert apple.ex_date == date(2025, 3, 20)
    assert apple.amount == Decimal("0.31000000")  # "Distribution per share"
    assert apple.currency == "USD"
    assert apple.source == _VANGUARD_EXPORT
    assert apple.status == "official_export"


def test_vanguard_json_falls_back_to_pay_date_for_key() -> None:
    # No ex-date, only a payable date -> the event is still keyable on the pay date.
    payload = (
        '{"distributionHistory": [{"payableDate": "2025-04-10", '
        '"distributionAmount": "0.31", "currency": "USD"}]}'
    )
    records = parse_vanguard_distributions_json(payload, source=_VANGUARD)
    assert len(records) == 1
    assert records[0].ex_date == date(2025, 4, 10)
    assert records[0].payment_date == date(2025, 4, 10)


# --- known-source registry ---------------------------------------------------


def test_known_distribution_source_config() -> None:
    # VUSA now has a *candidate* Vanguard distribution config (usable without --url
    # when the live --source is named); JEPG has no distribution config.
    assert known_distribution_source(_VUSA) == _VANGUARD
    assert known_distribution_source(_JEPG) is None
    assert known_distribution_source(None) is None


# --- live adapter ingestion (guarded; HTTP stubbed) --------------------------


def _patch_jpmorgan(monkeypatch: pytest.MonkeyPatch, *, text: str = _JPM_CSV, calls=None):
    async def fake_download(self, url: str) -> str:  # noqa: ANN001
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
            calls["url"] = url
        return text

    monkeypatch.setattr(JPMorganDistributionsSource, "_download", fake_download)


def _patch_vanguard(monkeypatch: pytest.MonkeyPatch, *, text: str = _VANGUARD_JSONP, calls=None):
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


async def test_jpmorgan_worker_ingests_via_url_override(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_jpmorgan(monkeypatch, calls=calls)
    fund = await _fund(session, _JEPG)
    override = "https://am.jpmorgan.com/FundsMarketingHandler/excel?type=fundDistribution"
    run = await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_JPM, url=override
    )
    assert run.status == "success"
    assert run.source == _JPM
    assert run.records_inserted == 2  # the bad row was isolated
    assert calls["n"] == 1  # exactly one guarded download
    assert calls["url"] == override

    rows = (
        (
            await session.execute(
                select(Distribution).where(
                    Distribution.fund_id == fund.id, Distribution.source == _JPM
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(r.currency == "USD" and r.distribution_type == "Income" for r in rows)


async def test_jpmorgan_worker_uses_guarded_fetch_log(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_jpmorgan(monkeypatch)
    fund = await _fund(session, _JEPG)
    override = (
        "https://am.jpmorgan.com/FundsMarketingHandler/excel?type=fundDistribution&apikey=SECRET"
    )
    await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_JPM, url=override
    )
    logs = await source_requests.list_fetch_logs(session, source=_JPM, status="success")
    assert logs
    log = logs[0]
    assert log.request_kind == "fetch_jpmorgan_distributions"
    # Safe fetch log: a host/path class, no query/secrets, and only the ISIN key.
    assert log.endpoint_label and "?" not in log.endpoint_label
    assert "SECRET" not in (log.request_key or "")
    assert "APIKEY" not in (log.request_key or "").upper()
    assert log.raw_payload_hash


async def test_jpmorgan_recent_cache_avoids_repeat_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_jpmorgan(monkeypatch, calls=calls)
    fund = await _fund(session, _JEPG)
    url = "https://am.jpmorgan.com/FundsMarketingHandler/excel?type=fundDistribution"
    first = await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_JPM, url=url
    )
    second = await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_JPM, url=url
    )
    # Second run is served from the recent-success cache: no extra HTTP call and
    # (the real guarantee) no duplicate rows.
    assert calls["n"] == 1
    assert first.records_inserted == 2
    assert second.records_inserted == 0
    count = await session.scalar(
        select(func.count())
        .select_from(Distribution)
        .where(Distribution.fund_id == fund.id, Distribution.source == _JPM)
    )
    assert count == 2


async def test_jpmorgan_ingest_idempotent_and_updates_changed_rows(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import distributions_ingestion as dist_service

    # Bypass the recent-success cache + min-delay so each run re-fetches and the
    # idempotent UPSERT is the thing preventing duplicates.
    monkeypatch.setattr(source_requests, "should_skip_recent_success", _no_cache)
    budget = await source_budget.get_budget(session, _JPM)
    assert budget is not None
    budget.min_delay_ms = 0
    await session.commit()
    fund = await _fund(session, _JEPG)
    url = "https://am.jpmorgan.com/FundsMarketingHandler/excel?type=fundDistribution"

    _patch_jpmorgan(monkeypatch, text=_JPM_CSV)
    src = get_distribution_source(_JPM)
    first = await dist_service.ingest_distributions_for_fund(session, fund, src, url=url)
    assert first.inserted == 2
    # The bad ("n/a" amount) row is isolated at parse time, so ingestion only ever
    # sees the 2 good records (fetched=2, failed=0 at the upsert stage).
    assert first.fetched == 2
    assert first.failed == 0

    # Same ex-dates, one corrected amount -> update, not insert.
    changed = _JPM_CSV.replace("0.3500", "0.4000")
    _patch_jpmorgan(monkeypatch, text=changed)
    second = await dist_service.ingest_distributions_for_fund(session, fund, src, url=url)
    assert second.inserted == 0
    assert second.updated == 1
    assert second.skipped == 1

    row = await session.scalar(
        select(Distribution).where(
            Distribution.fund_id == fund.id,
            Distribution.source == _JPM,
            Distribution.ex_date == date(2026, 1, 2),
        )
    )
    assert row is not None and row.amount == Decimal("0.40000000")


async def test_vanguard_worker_ingests_via_url_override(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_vanguard(monkeypatch, calls=calls)
    fund = await _fund(session, _VUSA)
    url = (
        "https://api.vanguard.com/rs/gre/gra/1.7.0/datasets/"
        "urd-product-port-specific.json?vars=portId:9503,issueType:F"
    )
    run = await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_VANGUARD, url=url
    )
    assert run.status == "success"
    assert run.records_inserted == 2  # the amount-less row was isolated
    assert calls["n"] == 1
    rows = (
        (
            await session.execute(
                select(Distribution).where(
                    Distribution.fund_id == fund.id, Distribution.source == _VANGUARD
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.frequency for r in rows} == {"Quarterly"}


async def test_distribution_budget_block_makes_no_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Put the JPM distribution budget in backoff so guarded_fetch blocks BEFORE fetch.
    await source_budget.apply_backoff(session, _JPM, seconds=120)
    await session.commit()

    async def boom(self, url: str):  # noqa: ANN001 - must never run in backoff
        raise AssertionError("a live JPM distribution call was attempted while in backoff")

    monkeypatch.setattr(JPMorganDistributionsSource, "_download", boom)
    fund = await _fund(session, _JEPG)
    url = "https://am.jpmorgan.com/FundsMarketingHandler/excel?type=fundDistribution"
    run = await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_JPM, url=url
    )
    assert run.records_inserted == 0
    assert run.status == "success"  # clean no-op, not a failure
    rate_limited = await source_requests.list_fetch_logs(
        session, source=_JPM, status="rate_limited"
    )
    assert rate_limited


async def test_distribution_missing_url_is_clean_noop(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No --url is given for a live adapter -> clean no-op (no network).
    async def boom(self, url: str):  # noqa: ANN001 - must never be reached
        raise AssertionError("no URL configured, yet a download was attempted")

    monkeypatch.setattr(JPMorganDistributionsSource, "_download", boom)
    fund = await _fund(session, _JEPG)
    run = await run_job(session, "distribution_ingestion", fund_id=fund.id, source_name=_JPM)
    assert run.status == "success"
    assert run.records_inserted == 0
    assert "no_provider_match=1" in (run.message or "")


async def test_ishares_distributions_is_planned_and_fails_cleanly(
    session: AsyncSession,
) -> None:
    fund = await _fund(session, _VUSA)
    run = await run_job(
        session,
        "distribution_ingestion",
        fund_id=fund.id,
        source_name="blackrock_ishares_distributions",
    )
    assert run.status == "failed"
    assert "planned" in (run.message or "").lower()


async def test_distribution_no_live_calls_in_tests(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Belt-and-braces: even the configured default must never reach the network.
    async def explode(*args, **kwargs):  # pragma: no cover
        raise AssertionError("a live HTTP call was attempted")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    fund = await _fund(session, _JEPG)
    run = await run_job(session, "distribution_ingestion", fund_id=fund.id)  # fixture default
    assert run.status == "success"
    assert run.records_inserted == 5  # offline monthly fixture, no network


# --- Vanguard export adapter (offline) ---------------------------------------


async def test_vanguard_export_worker_ingests_bundled_sample(
    session: AsyncSession,
) -> None:
    fund = await _fund(session, _VUSA)
    run = await run_job(
        session, "distribution_ingestion", fund_id=fund.id, source_name=_VANGUARD_EXPORT
    )
    assert run.status == "success"
    assert run.records_inserted == 2
    rows = (
        (
            await session.execute(
                select(Distribution).where(
                    Distribution.fund_id == fund.id, Distribution.source == _VANGUARD_EXPORT
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(r.status == "fixture" for r in rows)  # bundled sample marker


# --- workspace scope ---------------------------------------------------------


async def test_distribution_workspace_scope_is_clean_noop_without_url(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_jpmorgan(monkeypatch)
    # Workspace 1 holds the seeded funds; no live distribution URL is configured, and
    # --url only applies to single-fund runs, so a workspace run is a clean no-op.
    run = await run_job(
        session,
        "distribution_ingestion",
        workspace_id=1,
        source_name=_JPM,
    )
    assert run.status == "success"
    assert run.records_inserted == 0
    assert "selected_funds=" in (run.message or "")


# --- planner / capabilities / registry ---------------------------------------


def test_planner_distribution_source_candidates() -> None:
    vusa = Fund(isin=_VUSA, name="Vanguard S&P 500", distribution_policy="distributing")
    candidates = planner._distribution_source_candidates(vusa, default="distribution_fixture")
    # VUSA has a candidate Vanguard distribution config -> fixture default + live.
    assert candidates == ["distribution_fixture", _VANGUARD]
    # A fund with no configured live distribution source gets only the fixture default.
    other = Fund(isin="ZZ0000000000", name="Unknown ETF", distribution_policy="distributing")
    assert planner._distribution_source_candidates(other, default="distribution_fixture") == [
        "distribution_fixture"
    ]


async def test_planner_emits_refresh_distributions_for_distributing_fund(
    session: AsyncSession,
) -> None:
    # A distributing held fund with no distributions yet -> a refresh_distributions item.
    plan = await planner.build_plan(session, 1)
    dist_items = [i for i in plan.items if i.item_type == "refresh_distributions"]
    # Seeded funds carry seed distributions (fresh), so none should be flagged...
    assert dist_items == []
    # ...but after deleting one fund's distributions it should surface.
    fund = await _fund(session, _VUSA)
    await session.execute(Distribution.__table__.delete().where(Distribution.fund_id == fund.id))
    await session.commit()
    plan2 = await planner.build_plan(session, 1)
    vusa_items = [
        i
        for i in plan2.items
        if i.item_type == "refresh_distributions" and i.related_fund_id == fund.id
    ]
    assert len(vusa_items) == 1
    item = vusa_items[0]
    # VUSA carries a candidate Vanguard distribution config: the live source is a
    # candidate, the recommended command needs no --url, needs_url_config is False.
    assert item.source_candidates == ["distribution_fixture", _VANGUARD]
    assert item.known_config is True
    assert item.config_status == "candidate"
    assert item.needs_url_config is False
    assert _VANGUARD in (item.recommended_command or "")
    assert "--url" not in (item.recommended_command or "")


def test_registry_distribution_source_statuses() -> None:
    jpm = registry.get_capability(_JPM)
    vanguard = registry.get_capability(_VANGUARD)
    export = registry.get_capability(_VANGUARD_EXPORT)
    planned = registry.get_capability("blackrock_ishares_distributions")
    assert jpm and jpm.adapter_status == "implemented" and jpm.supports_live
    assert vanguard and vanguard.adapter_status == "implemented" and vanguard.supports_live
    assert export and export.adapter_status == "implemented" and not export.supports_live
    assert planned and planned.adapter_status == "planned"
    for cap in (jpm, vanguard, export, planned):
        assert "distributions" in cap.data_types


async def test_capabilities_endpoint_lists_live_distribution_sources(client) -> None:
    body = (await client.get("/api/v1/data-sources/capabilities?data_type=distributions")).json()
    names = {c["source_name"] for c in body["data"]}
    assert {_JPM, _VANGUARD, _VANGUARD_EXPORT, "blackrock_ishares_distributions"} <= names
    assert {"distribution_fixture"} <= names


def test_live_distribution_budgets_are_conservative() -> None:
    specs = source_budget.default_budget_specs()
    for name in (_JPM, _VANGUARD):
        assert specs[name]["max_concurrency"] == 1
        assert int(specs[name]["min_delay_ms"]) >= 1000  # type: ignore[arg-type]
        assert int(specs[name]["max_requests_per_minute"]) <= 10  # type: ignore[arg-type]


# --- diagnostics -------------------------------------------------------------


async def test_distribution_diagnostics_counts(session: AsyncSession) -> None:
    from app.services import diagnostics as diagnostics_service

    fund = await _fund(session, _VUSA)
    # Seeded distributing funds have fresh distributions -> none missing/stale.
    before = await diagnostics_service.workspace_diagnostics(session, 1)
    assert before.distributions > 0
    assert before.missing_distributions == 0
    assert before.latest_distribution_date is not None

    # Drop one held distributing fund's distributions -> it is now "missing".
    await session.execute(Distribution.__table__.delete().where(Distribution.fund_id == fund.id))
    await session.commit()
    after = await diagnostics_service.workspace_diagnostics(session, 1)
    assert after.missing_distributions >= 1


# --- helpers -----------------------------------------------------------------


async def _no_cache(*args, **kwargs):  # noqa: ANN002, ANN003
    """Stand-in for ``should_skip_recent_success`` that never caches."""
    return None
