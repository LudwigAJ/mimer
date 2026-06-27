"""Idempotent seed data for local development.

Run with::

    uv run python -m app.seed.seed_data

All rows are tagged ``source = "seed"`` (or an equivalent marker) and use
placeholder prices / holdings / distributions. The data is realistic in *shape*
but not guaranteed to be current. Re-running is a no-op once funds exist.

Modelling note: the JPMorgan fund is intentionally seeded as ONE fund with
multiple listings (JEPG in GBP, JEGP in USD, plus a EUR Xetra line) to exercise
the "ticker is not identity" rule.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import (
    DataSource,
    Distribution,
    DocumentSnapshot,
    Fund,
    FundHolding,
    FundListing,
    FxRate,
    IngestionRun,
    PortfolioPosition,
    Price,
    ScheduledJob,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceSetting,
)
from app.db.session import get_engine, get_sessionmaker
from app.services import source_budget
from app.sources.holdings import holding_identity_key

SEED = "seed"
_TODAY = date.today()


def _d(value: str) -> Decimal:
    return Decimal(value)


def _days_ago(days: int) -> date:
    return _TODAY - timedelta(days=days)


def _build_data_sources() -> list[DataSource]:
    return [
        DataSource(
            name="vanguard",
            source_type="issuer",
            base_url="https://www.vanguard.co.uk",
            priority=10,
            notes="Issuer source of truth for Vanguard fund facts/holdings/docs.",
        ),
        DataSource(
            name="ishares",
            source_type="issuer",
            base_url="https://www.ishares.com",
            priority=10,
            notes="BlackRock/iShares issuer source.",
        ),
        DataSource(
            name="jpmam",
            source_type="issuer",
            base_url="https://am.jpmorgan.com",
            priority=10,
            notes="J.P. Morgan Asset Management issuer source.",
        ),
        DataSource(
            name="lse",
            source_type="exchange",
            base_url="https://www.londonstockexchange.com",
            priority=30,
            notes="London Stock Exchange delayed prices.",
        ),
        DataSource(
            name="stooq",
            source_type="market_data",
            base_url="https://stooq.com",
            priority=40,
            notes="Free market data; not authoritative for fund facts.",
        ),
        DataSource(
            name="ecb",
            source_type="fx",
            base_url="https://www.ecb.europa.eu",
            priority=20,
            notes="ECB reference FX rates.",
        ),
        DataSource(name="manual", source_type="manual", priority=5, notes="Manual overrides."),
        DataSource(name="broker_csv", source_type="broker", priority=5, notes="Broker imports."),
        DataSource(name=SEED, source_type="seed", priority=100, notes="Placeholder seed data."),
    ]


def _build_funds() -> dict[str, Fund]:
    """Construct funds with their listings, prices, holdings, distributions, docs."""

    # --- VUSA: Vanguard S&P 500 UCITS ETF (USD fund, London GBP line) ---------
    vusa_gbp = FundListing(
        ticker="VUSA",
        exchange="London Stock Exchange",
        trading_currency="GBP",
        currency_unit="GBP",
        sedol="B810P85",
        prices=[Price(price_date=_TODAY, price=_d("75.00"), currency="GBP", source=SEED)],
    )
    vusa = Fund(
        isin="IE00B3XXRP09",
        name="Vanguard S&P 500 UCITS ETF",
        provider="Vanguard",
        domicile="IE",
        base_currency="USD",
        distribution_policy="distributing",
        strategy="S&P 500",
        ocf=_d("0.07000"),
        listings=[vusa_gbp],
        distributions=[
            Distribution(
                ex_date=_days_ago(d),
                amount=_d("0.30"),
                currency="USD",
                source=SEED,
                status="paid",
            )
            for d in (30, 120, 210, 300)
        ],
        holdings=[
            FundHolding(
                as_of_date=_TODAY,
                security_name=name,
                security_ticker=ticker,
                country="US",
                sector=sector,
                weight=_d(weight),
                source=SEED,
                holding_key=holding_identity_key(name=name, ticker=ticker),
            )
            for name, ticker, sector, weight in [
                ("Apple Inc", "AAPL", "Technology", "0.07000000"),
                ("Microsoft Corp", "MSFT", "Technology", "0.06500000"),
                ("NVIDIA Corp", "NVDA", "Technology", "0.05000000"),
                ("Amazon.com Inc", "AMZN", "Consumer Discretionary", "0.03500000"),
                ("Alphabet Inc", "GOOGL", "Communication Services", "0.04000000"),
            ]
        ],
        documents=[
            DocumentSnapshot(
                document_type="factsheet",
                url="https://www.vanguard.co.uk/factsheet/VUSA",
                document_date=_days_ago(20),
                status="current",
                source=SEED,
            ),
            DocumentSnapshot(
                document_type="KID",
                url="https://www.vanguard.co.uk/kid/VUSA",
                document_date=_days_ago(60),
                status="current",
                source=SEED,
            ),
        ],
    )

    # --- ISF: iShares Core FTSE 100 UCITS ETF (GBP fund, London GBX line) ------
    isf_gbx = FundListing(
        ticker="ISF",
        exchange="London Stock Exchange",
        trading_currency="GBP",
        currency_unit="GBX",  # quoted in pence on the LSE
        sedol="B53HP60",
        prices=[Price(price_date=_TODAY, price=_d("850.00"), currency="GBX", source=SEED)],
    )
    isf = Fund(
        isin="IE0005042456",
        name="iShares Core FTSE 100 UCITS ETF",
        provider="iShares (BlackRock)",
        domicile="IE",
        base_currency="GBP",
        distribution_policy="distributing",
        strategy="FTSE 100",
        ocf=_d("0.07000"),
        listings=[isf_gbx],
        distributions=[
            Distribution(
                ex_date=_days_ago(d),
                amount=_d("0.08"),
                currency="GBP",
                source=SEED,
                status="paid",
            )
            for d in (45, 135, 225, 315)
        ],
        holdings=[
            FundHolding(
                as_of_date=_TODAY,
                security_name=name,
                security_ticker=ticker,
                country="GB",
                sector=sector,
                weight=_d(weight),
                source=SEED,
                holding_key=holding_identity_key(name=name, ticker=ticker),
            )
            for name, ticker, sector, weight in [
                ("AstraZeneca PLC", "AZN", "Health Care", "0.08000000"),
                ("Shell PLC", "SHEL", "Energy", "0.07500000"),
                ("HSBC Holdings PLC", "HSBA", "Financials", "0.07000000"),
                ("Unilever PLC", "ULVR", "Consumer Staples", "0.05000000"),
                ("BP PLC", "BP", "Energy", "0.04000000"),
            ]
        ],
        documents=[
            DocumentSnapshot(
                document_type="factsheet",
                url="https://www.ishares.com/factsheet/ISF",
                document_date=_days_ago(15),
                status="current",
                source=SEED,
            ),
            DocumentSnapshot(
                document_type="KID",
                url="https://www.ishares.com/kid/ISF",
                document_date=_days_ago(90),
                status="current",
                source=SEED,
            ),
        ],
    )

    # --- JPMorgan Global Equity Premium Income: ONE fund, MULTIPLE listings ----
    jepg_gbp = FundListing(
        ticker="JEPG",
        exchange="London Stock Exchange",
        trading_currency="GBP",
        currency_unit="GBP",
        prices=[Price(price_date=_TODAY, price=_d("40.00"), currency="GBP", source=SEED)],
    )
    jegp_usd = FundListing(
        ticker="JEGP",
        exchange="London Stock Exchange",
        trading_currency="USD",
        currency_unit="USD",
        prices=[Price(price_date=_TODAY, price=_d("50.00"), currency="USD", source=SEED)],
    )
    jepg_eur = FundListing(
        ticker="JEPG",
        exchange="Xetra",
        trading_currency="EUR",
        currency_unit="EUR",
        prices=[Price(price_date=_TODAY, price=_d("47.00"), currency="EUR", source=SEED)],
    )
    jpm = Fund(
        isin="IE0003UVYC20",
        name="JPMorgan Global Equity Premium Income Active UCITS ETF",
        provider="J.P. Morgan Asset Management",
        domicile="IE",
        base_currency="USD",
        distribution_policy="distributing",
        strategy="Global equity premium income (covered call overlay)",
        ocf=_d("0.35000"),
        listings=[jepg_gbp, jegp_usd, jepg_eur],
        distributions=[
            Distribution(
                ex_date=_days_ago(d),
                amount=_d("0.35"),
                currency="USD",
                source=SEED,
                status="paid",
            )
            for d in (30, 120, 210, 300)
        ],
        holdings=[
            FundHolding(
                as_of_date=_TODAY,
                security_name=name,
                security_ticker=ticker,
                country=country,
                sector=sector,
                weight=_d(weight),
                source=SEED,
                holding_key=holding_identity_key(name=name, ticker=ticker),
            )
            for name, ticker, country, sector, weight in [
                ("Apple Inc", "AAPL", "US", "Technology", "0.03000000"),
                ("Microsoft Corp", "MSFT", "US", "Technology", "0.03000000"),
                ("ASML Holding NV", "ASML", "NL", "Technology", "0.02000000"),
                ("Nestle SA", "NESN", "CH", "Consumer Staples", "0.02000000"),
                ("Novo Nordisk A/S", "NOVO-B", "DK", "Health Care", "0.02000000"),
            ]
        ],
        documents=[
            DocumentSnapshot(
                document_type="factsheet",
                url="https://am.jpmorgan.com/factsheet/JEPG",
                document_date=_days_ago(10),
                status="current",
                source=SEED,
            ),
            DocumentSnapshot(
                document_type="KID",
                url="https://am.jpmorgan.com/kid/JEPG",
                document_date=_days_ago(40),
                status="current",
                source=SEED,
            ),
        ],
    )

    funds = {"vusa": vusa, "isf": isf, "jpm": jpm}
    # Seeded funds ship with placeholder data, so mark them active/fresh.
    now = datetime.now(UTC)
    for fund in funds.values():
        fund.status = "active"
        fund.source = SEED
        fund.last_refreshed_at = now
        for listing in fund.listings:
            listing.status = "active"
            listing.last_price_at = now
            listing.last_resolved_at = now
    return funds


def _build_user_and_workspace() -> tuple[User, Workspace]:
    user = User(email="owner@example.com", display_name="Default Owner", is_active=True)
    workspace = Workspace(name="Personal", base_currency="GBP")
    return user, workspace


def _build_workspace_children(
    user: User, workspace: Workspace
) -> list[WorkspaceMember | WorkspaceSetting]:
    return [
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"),
        WorkspaceSetting(workspace_id=workspace.id, key="base_currency", value_json="GBP"),
    ]


def _build_positions(funds: dict[str, Fund], workspace_id: int) -> list[PortfolioPosition]:
    vusa_gbp = funds["vusa"].listings[0]
    isf_gbx = funds["isf"].listings[0]
    jepg_gbp = funds["jpm"].listings[0]
    jegp_usd = funds["jpm"].listings[1]
    return [
        PortfolioPosition(
            workspace_id=workspace_id,
            listing=vusa_gbp,
            account_name="ISA",
            units=_d("100"),
            average_cost=_d("70.00"),
            cost_currency="GBP",
        ),
        PortfolioPosition(
            workspace_id=workspace_id,
            listing=isf_gbx,
            account_name="ISA",
            units=_d("200"),
            average_cost=_d("750.00"),
            cost_currency="GBX",
        ),
        PortfolioPosition(
            workspace_id=workspace_id,
            listing=jepg_gbp,
            account_name="GIA",
            units=_d("50"),
            average_cost=_d("38.00"),
            cost_currency="GBP",
        ),
        PortfolioPosition(
            workspace_id=workspace_id,
            listing=jegp_usd,
            account_name="GIA",
            units=_d("30"),
            average_cost=_d("48.00"),
            cost_currency="USD",
        ),
    ]


_DAY = 86400
_WEEK = 604800
_MONTH = 2592000  # ~30 days


def _build_scheduled_jobs() -> list[ScheduledJob]:
    """Recurring ingestion jobs the scheduler claims and runs.

    ``schedule_kind`` drives recurrence (the cron strings are display-only
    forward-compat). Each is seeded with ``next_run_at`` one interval out so it is
    NOT immediately due — a freshly-seeded ``scheduler --once`` is a safe no-op
    that never makes a surprise live request. Lower its ``next_run_at`` (or POST
    ``/scheduler/run-once``) to exercise a run.
    """
    now = datetime.now(UTC)

    def soon(interval: int) -> datetime:
        return now + timedelta(seconds=interval)

    return [
        ScheduledJob(
            name="daily_price_ingestion",
            job_type="price_ingestion",
            source="market_data",
            schedule_cron="0 18 * * 1-5",
            schedule_kind="daily",
            interval_seconds=_DAY,
            is_active=True,
            next_run_at=soon(_DAY),
        ),
        ScheduledJob(
            name="daily_fx_ingestion",
            job_type="fx_ingestion",
            source="fx",
            schedule_cron="0 17 * * 1-5",
            schedule_kind="daily",
            interval_seconds=_DAY,
            is_active=True,
            next_run_at=soon(_DAY),
        ),
        ScheduledJob(
            name="weekly_issuer_facts_ingestion",
            job_type="issuer_facts_ingestion",
            source="issuer",
            schedule_kind="weekly",
            interval_seconds=_WEEK,
            is_active=True,
            next_run_at=soon(_WEEK),
        ),
        ScheduledJob(
            name="weekly_distribution_ingestion",
            job_type="distribution_ingestion",
            source="issuer",
            schedule_kind="weekly",
            interval_seconds=_WEEK,
            is_active=True,
            next_run_at=soon(_WEEK),
        ),
        ScheduledJob(
            name="weekly_holdings_ingestion",
            job_type="issuer_holdings_ingestion",
            source="issuer",
            schedule_cron="0 6 * * 1",
            schedule_kind="weekly",
            interval_seconds=_WEEK,
            is_active=True,
            next_run_at=soon(_WEEK),
        ),
        ScheduledJob(
            name="monthly_document_snapshot_check",
            job_type="document_snapshot_ingestion",
            source="issuer",
            schedule_cron="0 7 1 * *",
            schedule_kind="interval",
            interval_seconds=_MONTH,
            is_active=True,
            next_run_at=soon(_MONTH),
        ),
    ]


def _build_alert_generation_job() -> ScheduledJob:
    """Daily, database-only alert_generation job. Seeded *due now* so a first
    ``scheduler --once`` does real but safe (offline) work."""
    return ScheduledJob(
        name="daily_alert_generation",
        job_type="alert_generation",
        source=None,
        schedule_cron="0 8 * * *",
        schedule_kind="daily",
        interval_seconds=_DAY,
        is_active=True,
        next_run_at=datetime.now(UTC),
    )


def _build_exposure_recompute_job() -> ScheduledJob:
    """Daily, database-only exposure_recompute job. Seeded *due now* (offline)."""
    return ScheduledJob(
        name="daily_exposure_recompute",
        job_type="exposure_recompute",
        source=None,
        schedule_cron="0 9 * * *",
        schedule_kind="daily",
        interval_seconds=_DAY,
        is_active=True,
        next_run_at=datetime.now(UTC),
    )


def _build_constituent_identity_job() -> ScheduledJob:
    """Daily constituent identity-resolution job.

    Defaults to the offline fixture resolver (``constituent_identity_source_default``),
    so a scheduler-driven run never makes a surprise live OpenFIGI call. Seeded
    one day out (NOT due now) so a freshly-seeded ``scheduler --once`` is a safe
    no-op; lower its ``next_run_at`` or run the worker manually to exercise it.
    """
    return ScheduledJob(
        name="daily_constituent_identity_resolution",
        job_type="constituent_identity_resolution",
        source="identifier",
        schedule_cron="0 5 * * *",
        schedule_kind="daily",
        interval_seconds=_DAY,
        is_active=True,
        next_run_at=datetime.now(UTC) + timedelta(seconds=_DAY),
    )


def _build_constituent_price_job() -> ScheduledJob:
    """Daily constituent EOD price-ingestion job.

    Defaults to the offline fixture provider (``constituent_price_source_default``),
    so a scheduler-driven run never makes a surprise live Stooq/yfinance call (the
    scheduler passes ``source_name=None``, letting the worker pick its configured
    provider). Seeded one day out (NOT due now) so a freshly-seeded
    ``scheduler --once`` is a safe no-op; lower its ``next_run_at`` or run the
    worker manually to exercise it.
    """
    return ScheduledJob(
        name="daily_constituent_eod_price_ingestion",
        job_type="constituent_eod_price_ingestion",
        source="market_data",
        schedule_cron="0 19 * * 1-5",
        schedule_kind="daily",
        interval_seconds=_DAY,
        is_active=True,
        next_run_at=datetime.now(UTC) + timedelta(seconds=_DAY),
    )


def _build_instrument_onboarding_job() -> ScheduledJob:
    """Manual instrument-onboarding / data-readiness orchestration job.

    Seeded ``manual`` so the scheduler NEVER auto-runs it — onboarding coordinates
    several ingestion stages and is best triggered explicitly (CLI or
    ``POST /jobs/{id}/run``). When triggered, the worker defaults to the offline
    ``fixture`` source mode (the scheduler/job trigger passes no source), so a run
    is fully offline unless ``--source-mode live`` is requested on the CLI.
    """
    return ScheduledJob(
        name="instrument_onboarding",
        job_type="instrument_onboarding",
        source="orchestration",
        schedule_kind="manual",
        is_active=True,
    )


def _build_fx_rates() -> list[FxRate]:
    # rate = units of quote_currency per 1 unit of base_currency.
    return [
        FxRate(
            rate_date=_TODAY,
            base_currency="GBP",
            quote_currency="USD",
            rate=_d("1.2700000000"),
            source=SEED,
        ),
        FxRate(
            rate_date=_TODAY,
            base_currency="GBP",
            quote_currency="EUR",
            rate=_d("1.1700000000"),
            source=SEED,
        ),
    ]


async def seed() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.scalar(select(func.count()).select_from(Fund))
        if existing:
            print(f"Seed skipped: {existing} fund(s) already present.")
            return

        session.add_all(_build_data_sources())

        funds = _build_funds()
        session.add_all(funds.values())

        user, workspace = _build_user_and_workspace()
        session.add_all([user, workspace])
        await session.flush()  # assign fund / listing / user / workspace ids

        session.add_all(_build_workspace_children(user, workspace))
        session.add_all(_build_positions(funds, workspace.id))
        session.add_all(_build_fx_rates())
        session.add_all(_build_scheduled_jobs())
        session.add_all(
            [
                _build_alert_generation_job(),
                _build_exposure_recompute_job(),
                _build_constituent_identity_job(),
                _build_constituent_price_job(),
                _build_instrument_onboarding_job(),
            ]
        )

        # Conservative request budgets so future live adapters never spam a
        # source. Fixtures/local are permissive; openfigi/yfinance/stooq are not.
        await source_budget.seed_source_rate_limits(session)

        # Alerts are derived data: run the alert_generation worker to populate the
        # workspace-scoped ``alerts`` table from the signals seeded above. The
        # base seed is intentionally clean (fresh prices, key documents present),
        # so a generation run over it yields few/no alerts — an honest baseline.

        now = datetime.now(UTC)
        session.add(
            IngestionRun(
                source=SEED,
                job_type="seed_load",
                status="success",
                started_at=now,
                finished_at=now,
                message="Initial seed load",
                rows_inserted=len(funds),
            )
        )

        await session.commit()
        print(f"Seed complete: inserted {len(funds)} funds with listings, prices, and metadata.")

    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(seed())
