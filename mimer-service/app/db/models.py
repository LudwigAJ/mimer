"""SQLAlchemy ORM models — the canonical Postgres schema.

Identity rules (see AGENTS.md):
  * A *fund* is identified by its internal id and ISIN.
  * A fund has one or more *listings*; a ticker is an exchange/currency-specific
    alias and is never treated as global identity.
  * Distributions, holdings and documents belong to the fund, not a listing.
  * Prices and FX rates belong to a listing/currency pair and always record a
    `source`.

Monetary and weight values use Numeric/Decimal — never float.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Reusable Numeric configurations.
_MONEY = Numeric(24, 8)  # prices, units, amounts, costs
_WEIGHT = Numeric(12, 8)  # holding / exposure weights
_RATE = Numeric(24, 10)  # fx rates
_OCF = Numeric(8, 5)  # ongoing charges figure / TER


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Fund(Base, TimestampMixin):
    __tablename__ = "funds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    isin: Mapped[str] = mapped_column(String(12), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    domicile: Mapped[str | None] = mapped_column(String(2))
    base_currency: Mapped[str | None] = mapped_column(String(3))
    distribution_policy: Mapped[str | None] = mapped_column(String(32))
    strategy: Mapped[str | None] = mapped_column(String(255))
    ocf: Mapped[Decimal | None] = mapped_column(_OCF)
    # Provenance of the *fund facts* (name/provider/domicile/strategy/ocf/...).
    # e.g. seed | issuer_fixture | manual. Distinct from per-price/-distribution
    # sources; ranked via the data_sources priority table.
    source: Mapped[str | None] = mapped_column(String(32))
    # Lifecycle: pending (resolved, awaiting backfill) | active | stale | error.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    listings: Mapped[list[FundListing]] = relationship(
        back_populates="fund", cascade="all, delete-orphan"
    )
    distributions: Mapped[list[Distribution]] = relationship(
        back_populates="fund", cascade="all, delete-orphan"
    )
    holdings: Mapped[list[FundHolding]] = relationship(
        back_populates="fund", cascade="all, delete-orphan"
    )
    documents: Mapped[list[DocumentSnapshot]] = relationship(
        back_populates="fund", cascade="all, delete-orphan"
    )


class FundListing(Base, TimestampMixin):
    __tablename__ = "fund_listings"
    __table_args__ = (
        UniqueConstraint("fund_id", "ticker", "exchange", name="uq_listing_fund_ticker_exchange"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("funds.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(64))
    trading_currency: Mapped[str | None] = mapped_column(String(3))
    # e.g. GBP, GBX (pence), USD, EUR. Distinct from trading_currency so we can
    # model pence-quoted London listings correctly.
    currency_unit: Mapped[str | None] = mapped_column(String(8))
    figi: Mapped[str | None] = mapped_column(String(12))
    sedol: Mapped[str | None] = mapped_column(String(7))
    # Lifecycle: pending | active | stale | error.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_price_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    fund: Mapped[Fund] = relationship(back_populates="listings")
    positions: Mapped[list[PortfolioPosition]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    prices: Mapped[list[Price]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class PortfolioPosition(Base, TimestampMixin):
    __tablename__ = "portfolio_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Workspace-private: positions belong to a workspace, never global.
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fund_listing_id: Mapped[int] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    account_name: Mapped[str | None] = mapped_column(String(128))
    units: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    average_cost: Mapped[Decimal | None] = mapped_column(_MONEY)
    cost_currency: Mapped[str | None] = mapped_column(String(8))

    listing: Mapped[FundListing] = relationship(back_populates="positions")


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (
        UniqueConstraint(
            "fund_listing_id", "price_date", "source", name="uq_price_listing_date_source"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_listing_id: Mapped[int] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    listing: Mapped[FundListing] = relationship(back_populates="prices")


class Distribution(Base):
    __tablename__ = "distributions"
    __table_args__ = (
        # One declared distribution per (fund, ex-date, source) so the
        # distribution_ingestion worker can upsert idempotently. Different
        # sources may assert the same ex-date (provenance differs).
        UniqueConstraint("fund_id", "ex_date", "source", name="uq_distribution_fund_exdate_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("funds.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Identity date for the distribution event (NOT NULL — part of the upsert key).
    # When an issuer feed has no explicit ex-date, the ingestion layer falls back to
    # the distribution/payment date so the event is still keyable; the original
    # issuer dates are preserved verbatim below + in ``raw_payload_json``.
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    record_date: Mapped[date | None] = mapped_column(Date)
    payment_date: Mapped[date | None] = mapped_column(Date)
    # The issuer's labelled "distribution date" when distinct from ex/record/pay.
    distribution_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # income | dividend | capital_gain | return_of_capital | ... (issuer-asserted,
    # optional; stored verbatim, never used for tax treatment — see AGENTS.md).
    distribution_type: Mapped[str | None] = mapped_column(String(32))
    # monthly | quarterly | semi_annual | annual | ... (issuer-asserted, optional).
    frequency: Mapped[str | None] = mapped_column(String(32))
    share_class: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # paid | declared | announced | estimated | ... (provider-asserted, optional).
    status: Mapped[str | None] = mapped_column(String(32))
    # Reserved for provenance/debugging (raw provider payload + any issuer fields
    # without a dedicated canonical column).
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    fund: Mapped[Fund] = relationship(back_populates="distributions")


class FundHolding(Base):
    __tablename__ = "fund_holdings"
    __table_args__ = (
        # Idempotency key for the holdings ingestion upsert: one row per
        # (fund, as-of snapshot date, source, holding identity). ``holding_key``
        # is a deterministic identity string derived by the source/ingestion
        # layer (prefers ISIN > FIGI > CUSIP > SEDOL > normalised name+ticker),
        # so re-runs and backfills never duplicate a holding. Different sources
        # keep their own snapshot rows (provenance differs).
        UniqueConstraint(
            "fund_id", "as_of_date", "source", "holding_key", name="uq_fund_holding_identity"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("funds.id", ondelete="CASCADE"), index=True, nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    security_name: Mapped[str] = mapped_column(String(255), nullable=False)
    security_ticker: Mapped[str | None] = mapped_column(String(32))
    security_isin: Mapped[str | None] = mapped_column(String(12))
    security_sedol: Mapped[str | None] = mapped_column(String(7))
    security_cusip: Mapped[str | None] = mapped_column(String(9))
    security_figi: Mapped[str | None] = mapped_column(String(12))
    country: Mapped[str | None] = mapped_column(String(64))
    sector: Mapped[str | None] = mapped_column(String(64))
    industry: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(8))
    weight: Mapped[Decimal] = mapped_column(_WEIGHT, nullable=False)
    market_value: Mapped[Decimal | None] = mapped_column(_MONEY)
    shares: Mapped[Decimal | None] = mapped_column(_MONEY)
    # paid | declared | current | estimated | ... (provider-asserted, optional).
    status: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Deterministic identity within a (fund, as_of_date, source) snapshot.
    holding_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # --- constituent identity resolution (see app/services/constituent_identity) ---
    # Canonical instrument this constituent resolved to (NULL until resolved). Only
    # set on a sufficiently-confident, unambiguous resolution — ambiguous/not-found
    # rows stay unlinked so look-through never attributes the wrong security.
    holding_instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"), index=True
    )
    # Resolution lifecycle: NULL/unresolved -> resolved | ambiguous | not_found |
    # failed | manual. Distinct from the provider ``status`` above (paid/current/...).
    identity_status: Mapped[str | None] = mapped_column(String(16))
    identity_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Reserved for provenance/debugging (raw provider payload).
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    fund: Mapped[Fund] = relationship(back_populates="holdings")
    instrument: Mapped[Instrument | None] = relationship(back_populates="holdings")


class DocumentSnapshot(Base):
    __tablename__ = "document_snapshots"
    __table_args__ = (
        # One row per content version: re-ingesting the same content is a no-op,
        # a changed ``content_hash`` inserts a NEW snapshot (history preserved),
        # and distinct sources keep their own rows. NULL hashes (legacy seed
        # rows) are treated as distinct, which is intended.
        UniqueConstraint(
            "fund_id",
            "document_type",
            "source",
            "content_hash",
            name="uq_document_snapshot_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("funds.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # factsheet | kid | kiid | prospectus | annual_report | interim_report |
    # holdings | other
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(String(1024))
    document_date: Mapped[date | None] = mapped_column(Date)
    language: Mapped[str | None] = mapped_column(String(16))
    country_or_region: Mapped[str | None] = mapped_column(String(64))
    content_type: Mapped[str | None] = mapped_column(String(64))
    # Deterministic hash of the document content (bytes/text) or, if absent,
    # stable metadata. Central to change detection.
    content_hash: Mapped[str | None] = mapped_column(String(128))
    # Change detection: how this snapshot relates to the prior one for the same
    # (fund, document_type, source). new | changed (stored at creation).
    change_status: Mapped[str | None] = mapped_column(String(16))
    previous_content_hash: Mapped[str | None] = mapped_column(String(128))
    previous_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_snapshots.id", ondelete="SET NULL")
    )
    status: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="seed")
    # Reserved for provenance/debugging (raw provider payload / small content).
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)
    # Last time the document was fetched/verified (bumped on an unchanged re-fetch).
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    fund: Mapped[Fund] = relationship(back_populates="documents")


class Alert(Base, TimestampMixin):
    """Workspace-scoped, idempotent alert.

    Generated by the `alert_generation` worker from backend diagnostics / change
    signals (stale prices, missing FX, changed documents, failed jobs, ...).

    Idempotency is keyed by ``(workspace_id, dedupe_key)``: re-running the
    generator for a still-present issue updates ``last_seen_at`` (and any changed
    content) rather than inserting a duplicate. Read / dismiss / resolve state
    lives on the row itself — there is no separate per-workspace state table.

    Lifecycle (``status``): active -> read (user opened it) / dismissed (user hid
    it) / resolved (the underlying issue is gone). See `app.services.alerts` and
    `app.services.alert_generation` for the transitions.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        # One live alert per distinct underlying issue, per workspace. The
        # ``dedupe_key`` encodes the issue identity (e.g. the changed content
        # hash), so a *materially* different issue gets a new key (and row).
        UniqueConstraint("workspace_id", "dedupe_key", name="uq_alert_workspace_dedupe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # info | warning | error | critical
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    # document | price | fx | holdings | distribution | job | instrument |
    # source | data_quality | system
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    # active | read | dismissed | resolved
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Producer of the alert (e.g. "alert_generation"); provenance, not severity.
    source: Mapped[str | None] = mapped_column(String(32))
    # Free-form pointer to the originating entity, for the GUI to deep-link.
    # related_entity_type: fund | fund_listing | document_snapshot | job_run |
    # position | currency | identifier | ...
    related_entity_type: Mapped[str | None] = mapped_column(String(32))
    related_entity_id: Mapped[str | None] = mapped_column(String(64))
    # Typed FKs for the common cases (nullable; SET NULL keeps the alert if the
    # target is removed). related_entity_type/id stays the generic fallback.
    related_fund_id: Mapped[int | None] = mapped_column(
        ForeignKey("funds.id", ondelete="SET NULL"), index=True
    )
    related_fund_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="SET NULL"), index=True
    )
    related_document_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_snapshots.id", ondelete="SET NULL")
    )
    related_job_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_runs.id", ondelete="SET NULL")
    )
    # Idempotency key (stable per distinct issue); see __table_args__.
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Reserved for provenance/debugging (the rule's raw signal payload).
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint(
            "rate_date", "base_currency", "quote_currency", "source", name="uq_fx_rate"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # rate = units of quote_currency per 1 unit of base_currency.
    rate: Mapped[Decimal] = mapped_column(_RATE, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Provider/derivation provenance: fixture | official | estimated | manual | ...
    # (read-side freshness is derived from ``rate_date``, not stored here).
    status: Mapped[str | None] = mapped_column(String(16))
    # Reserved for provenance/debugging (raw provider payload).
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReferenceRate(Base, TimestampMixin):
    """An official / reference rate *observation* (NOT a curve).

    One row is one rate quoted by an official or reference source for one
    ``rate_date`` — a central-bank policy rate (ECB main refinancing / deposit /
    marginal lending, BoE Bank Rate), an overnight benchmark (€STR, SONIA, SOFR,
    Fed Funds effective) or a published government par yield at a tenor (US
    Treasury 1M/3M/.../30Y). This is *collection + normalisation + persistence*
    only: the backend stores what an official source published. Curve fitting,
    bootstrapping, interpolation, discount factors, forward rates and bond pricing
    are **out of scope** here and belong in the Rust GUI / local pricer (see
    AGENTS.md compute boundary). There is deliberately no curve / discount-factor
    table.

    Idempotency key is ``(rate_date, currency, country_or_region, rate_family,
    rate_name, tenor, source)`` so re-runs and backfills never duplicate an
    observation and distinct sources keep their own rows (provenance differs). A
    NULL ``tenor`` (policy / overnight rates) participates in the key via the
    ingestion upsert's explicit IS NULL match. Read-side freshness is derived from
    ``rate_date`` (see ``app/services/freshness.py``); ``status`` carries the
    provider-asserted provenance (fixture | official | estimated | manual | ...).
    Numeric values are Decimal, never float.
    """

    __tablename__ = "reference_rates"
    __table_args__ = (
        UniqueConstraint(
            "rate_date",
            "currency",
            "country_or_region",
            "rate_family",
            "rate_name",
            "tenor",
            "source",
            name="uq_reference_rate",
        ),
        Index("ix_reference_rates_rate_name", "rate_name"),
        Index("ix_reference_rates_currency", "currency"),
        Index("ix_reference_rates_rate_date", "rate_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    # When the source observed/published the rate (optional; provenance only).
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # euro_area | united_kingdom | united_states | ...
    country_or_region: Mapped[str] = mapped_column(String(32), nullable=False)
    # policy_rate | overnight_rate | treasury_par_yield | benchmark_yield |
    # deposit_facility | lending_facility | reserve_rate | other
    rate_family: Mapped[str] = mapped_column(String(32), nullable=False)
    # ECB_MAIN_REFINANCING_RATE | ECB_DEPOSIT_FACILITY_RATE | ESTR | BOE_BANK_RATE
    # | SONIA | US_TREASURY_PAR_YIELD | SOFR | FED_FUNDS_EFFECTIVE | ...
    rate_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Tenor label (e.g. 1M, 3M, 2Y, 10Y) for par-yield series; NULL for
    # overnight / policy rates. ``tenor_months`` is its numeric sort key.
    tenor: Mapped[str | None] = mapped_column(String(16))
    tenor_months: Mapped[int | None] = mapped_column(Integer)
    rate_value: Mapped[Decimal] = mapped_column(_RATE, nullable=False)
    # percent | decimal | basis_points (how rate_value is expressed).
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="percent")
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # fixture | official | estimated | manual | ... (provider-asserted provenance).
    status: Mapped[str | None] = mapped_column(String(16))
    source_url: Mapped[str | None] = mapped_column(String(1024))
    # Reserved for provenance/debugging (raw provider payload).
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)


class IngestionRun(Base):
    """Legacy ingestion bookkeeping.

    Superseded by `ScheduledJob` + `JobRun` (which generalise this to any
    scheduled/maintenance job and add per-run record counts). Retained for now
    for backward compatibility; new code should write `JobRun`. No ingestion is
    implemented yet.
    """

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str | None] = mapped_column(Text)
    rows_inserted: Mapped[int | None] = mapped_column(Integer)
    rows_updated: Mapped[int | None] = mapped_column(Integer)


class DataSource(Base, TimestampMixin):
    """Registry of data sources, supporting future source ranking/priority."""

    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # issuer | exchange | market_data | fx | broker | manual | derived | seed
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(1024))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# Workspace / user (multi-tenant foundation).
#
# Reference data (funds, listings, prices, distributions, holdings, documents,
# fx_rates, data_sources, ingestion/job runs) is SHARED across workspaces.
# Everything below is WORKSPACE-PRIVATE and carries a workspace_id.
# ---------------------------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GBP")


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkspaceSetting(Base, TimestampMixin):
    __tablename__ = "workspace_settings"
    __table_args__ = (UniqueConstraint("workspace_id", "key", name="uq_workspace_setting_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[Any | None] = mapped_column(JSON)


# ---------------------------------------------------------------------------
# Broker CSV import + canonical transaction / position-reconciliation ledger.
#
# The bridge from the market-data workstation to the *user portfolio*
# workstation: a user's broker export (CSV) is parsed into canonical
# ``portfolio_transactions``, and committed transactions reconcile into a
# bounded ``portfolio_position_snapshots`` read model. All workspace-private.
#
# Compute boundary (see AGENTS.md): this is persistence + bounded SQL
# reconciliation, NOT PnL / tax lots / total return — those belong in the Rust
# GUI / local pricer. Imports never call live resolvers; instruments resolve
# only against existing identity (ISIN/FIGI/ticker), never name-only guesses.
# ---------------------------------------------------------------------------


class BrokerAccount(Base, TimestampMixin):
    """A workspace's broker account (e.g. "ISA" at a named broker).

    Optional grouping for imports/transactions — an import may carry no account.
    """

    __tablename__ = "broker_accounts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "broker_name", "account_label", name="uq_broker_account_label"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    broker_name: Mapped[str] = mapped_column(String(64), nullable=False)
    account_label: Mapped[str | None] = mapped_column(String(128))
    account_currency: Mapped[str | None] = mapped_column(String(8))
    # active | archived
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)


class BrokerImport(Base, TimestampMixin):
    """One committed broker CSV import for a workspace.

    Idempotency: unique ``(workspace_id, source_hash)`` — re-committing the same
    file content returns the existing import (duplicate detection) and never
    duplicates rows/transactions. Preview is read-only and writes no import row.
    """

    __tablename__ = "broker_imports"
    __table_args__ = (
        UniqueConstraint("workspace_id", "source_hash", name="uq_broker_import_workspace_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    broker_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL"), index=True
    )
    broker_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(255))
    # Deterministic digest of the parser name + raw CSV content (idempotency key).
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # previewed | committed | failed | duplicate | partial
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="committed")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parsed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cash_movement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    rows: Mapped[list[BrokerImportRow]] = relationship(
        back_populates="broker_import", cascade="all, delete-orphan"
    )


class PortfolioTransaction(Base, TimestampMixin):
    """Canonical, workspace-private transaction ledger row.

    Populated by broker CSV import (and, later, manual entry / other brokers).
    Carries trade *and* cash-movement types; instrument linkage is best-effort
    against existing identity only (``status=unresolved_instrument`` when a row
    cannot be safely resolved — never a name-only guess). Idempotency:
    unique ``(workspace_id, transaction_key, source)`` so re-committing the same
    file never duplicates a transaction.

    This is a ledger, NOT PnL — no realised/unrealised gain, tax lots or total
    return are stored or derived here (see AGENTS.md compute boundary).
    """

    __tablename__ = "portfolio_transactions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "transaction_key", "source", name="uq_portfolio_transaction_key"
        ),
        Index("ix_portfolio_transactions_workspace_id", "workspace_id"),
        Index("ix_portfolio_transactions_broker_import_id", "broker_import_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    broker_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL")
    )
    broker_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_imports.id", ondelete="SET NULL")
    )
    # Deterministic identity within (workspace, source) — see import service.
    transaction_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # buy | sell | dividend | cash_deposit | cash_withdrawal | fee | tax | fx |
    # interest | unknown
    transaction_type: Mapped[str] = mapped_column(String(24), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    settle_date: Mapped[date | None] = mapped_column(Date)
    # --- best-effort instrument linkage (existing identity only) --------------
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL")
    )
    instrument_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_listings.id", ondelete="SET NULL")
    )
    fund_id: Mapped[int | None] = mapped_column(ForeignKey("funds.id", ondelete="SET NULL"))
    fund_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="SET NULL")
    )
    symbol: Mapped[str | None] = mapped_column(String(64))
    isin: Mapped[str | None] = mapped_column(String(12))
    figi: Mapped[str | None] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[Decimal | None] = mapped_column(_MONEY)
    price: Mapped[Decimal | None] = mapped_column(_MONEY)
    gross_amount: Mapped[Decimal | None] = mapped_column(_MONEY)
    fees: Mapped[Decimal | None] = mapped_column(_MONEY)
    taxes: Mapped[Decimal | None] = mapped_column(_MONEY)
    net_amount: Mapped[Decimal | None] = mapped_column(_MONEY)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    cash_currency: Mapped[str | None] = mapped_column(String(8))
    fx_rate: Mapped[Decimal | None] = mapped_column(_RATE)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="broker_csv")
    # parsed | unresolved_instrument | ready | committed | ignored | failed
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="committed")
    notes: Mapped[str | None] = mapped_column(Text)
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)


class BrokerImportRow(Base):
    """One raw row of a committed import (provenance + per-row parse outcome)."""

    __tablename__ = "broker_import_rows"
    __table_args__ = (Index("ix_broker_import_rows_import_id", "broker_import_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_import_id: Mapped[int] = mapped_column(
        ForeignKey("broker_imports.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_row_json: Mapped[Any | None] = mapped_column(JSON)
    # parsed | skipped | failed | warning
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text)
    canonical_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio_transactions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    broker_import: Mapped[BrokerImport] = relationship(back_populates="rows")


class PortfolioPositionSnapshot(Base, TimestampMixin):
    """A derived position-reconciliation snapshot from committed transactions.

    Bounded SQL aggregation (buys − sells per instrument; cash per currency),
    NOT PnL. Idempotent: unique ``(workspace_id, as_of_date, input_hash)`` over
    the committed transaction set, mirroring ``ExposureSnapshot`` — an unchanged
    ledger re-reconciles to the same hash and writes nothing.
    """

    __tablename__ = "portfolio_position_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "as_of_date", "input_hash", name="uq_position_snapshot_identity"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="broker_reconciliation")
    # ok | partial | empty
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    rows: Mapped[list[PortfolioPositionSnapshotRow]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class PortfolioPositionSnapshotRow(Base):
    """One reconciled position (instrument) or cash balance (currency) row.

    ``kind`` discriminates: ``position`` rows carry a net ``quantity`` for an
    instrument key; ``cash`` rows carry a net amount in ``quantity`` for a
    ``currency``. No market value / PnL — that is the GUI/local-pricer's job.
    """

    __tablename__ = "portfolio_position_snapshot_rows"
    __table_args__ = (Index("ix_position_snapshot_rows_snapshot_id", "snapshot_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_position_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    # position | cash
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="position")
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL")
    )
    instrument_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_listings.id", ondelete="SET NULL")
    )
    fund_id: Mapped[int | None] = mapped_column(ForeignKey("funds.id", ondelete="SET NULL"))
    fund_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="SET NULL")
    )
    symbol: Mapped[str | None] = mapped_column(String(64))
    isin: Mapped[str | None] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str | None] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0)
    fees_total: Mapped[Decimal | None] = mapped_column(_MONEY)
    taxes_total: Mapped[Decimal | None] = mapped_column(_MONEY)
    # resolved | unresolved_instrument | cash
    status: Mapped[str | None] = mapped_column(String(24))
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    snapshot: Mapped[PortfolioPositionSnapshot] = relationship(back_populates="rows")


class PortfolioValuationSnapshot(Base, TimestampMixin):
    """A derived, cacheable portfolio *valuation/readiness* snapshot.

    Sits one layer above ``portfolio_position_snapshots``: it takes the bounded
    reconciliation (net quantity per instrument; cash per currency) and joins the
    *latest already-ingested* fund/instrument price and FX (at/before ``as_of_date``)
    to answer "what can be valued now, and what is blocking the rest". It is a
    bounded SQL-backed read model, NOT PnL — no realised/unrealised gain, tax lots,
    total return or performance attribution (those live in the Rust GUI / local
    pricer; see AGENTS.md compute boundary). It calls **no** live price/FX source
    and **no** identity resolver — it only consumes existing rows.

    Idempotent like ``ExposureSnapshot`` / ``PortfolioPositionSnapshot``:
    ``input_hash`` is a deterministic digest of the reconciled positions/cash plus
    every price and FX rate used, so an unchanged input set re-values to the same
    hash and writes nothing, while a new price/FX (or a (re)resolution) yields a new
    snapshot. Old snapshots are kept as history. Unique on
    ``(workspace_id, as_of_date, input_hash)``.
    """

    __tablename__ = "portfolio_valuation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "as_of_date", "input_hash", name="uq_valuation_snapshot_identity"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Optional broker-account scope (NULL = the whole workspace ledger).
    broker_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="portfolio_valuation")
    # ok | partial | empty (coverage/validity of the valuation).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    # Deterministic digest of all material inputs (idempotency key).
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    positions_selected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positions_valued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_price_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_fx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ambiguous_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale_price_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale_fx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cash_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Sum of the base-currency market value of the *valued* rows only (NULL when
    # nothing could be valued). Not a portfolio "total return" — a coverage figure.
    total_market_value_base: Mapped[Decimal | None] = mapped_column(_MONEY)
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    rows: Mapped[list[PortfolioValuationRow]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class PortfolioValuationRow(Base):
    """One valued/blocked position (or cash balance) within a valuation snapshot.

    ``position_type`` discriminates: ``fund_listing`` / ``instrument_listing`` /
    ``instrument`` / ``fund`` rows carry a net ``quantity`` priced via the latest
    fund/instrument price + FX; ``cash`` rows carry a net amount in ``quantity`` for
    a ``local_currency``; ``unresolved`` / ``ambiguous`` rows carry the broker
    symbol/ISIN but no price (the blocker is reported, never invented).

    No PnL / cost-basis / gain columns — this is a market-value *context* row, with
    explicit price/FX provenance + freshness so the GUI can show why a row is or
    isn't valued. Richer per-row detail (e.g. ``blocking_reasons``) lives in
    ``raw_payload_json``.
    """

    __tablename__ = "portfolio_valuation_rows"
    __table_args__ = (
        Index("ix_portfolio_valuation_rows_snapshot_id", "snapshot_id"),
        Index(
            "ix_portfolio_valuation_rows_snapshot_status",
            "snapshot_id",
            "valuation_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_valuation_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    # Stable grouping key (instrument:N | fund_listing:N | cash:GBP | symbol:X | ...).
    position_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # fund_listing | instrument_listing | instrument | fund | cash | unresolved |
    # ambiguous
    position_type: Mapped[str] = mapped_column(String(24), nullable=False)
    fund_id: Mapped[int | None] = mapped_column(ForeignKey("funds.id", ondelete="SET NULL"))
    fund_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="SET NULL")
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL")
    )
    instrument_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_listings.id", ondelete="SET NULL")
    )
    symbol: Mapped[str | None] = mapped_column(String(64))
    isin: Mapped[str | None] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0)
    local_currency: Mapped[str | None] = mapped_column(String(8))
    base_currency: Mapped[str | None] = mapped_column(String(8))
    # --- price context (NULL when missing) ---
    latest_price: Mapped[Decimal | None] = mapped_column(_MONEY)
    latest_price_date: Mapped[date | None] = mapped_column(Date)
    latest_price_source: Mapped[str | None] = mapped_column(String(32))
    # fresh | stale | missing (derived at recompute from the price date).
    latest_price_status: Mapped[str | None] = mapped_column(String(16))
    # --- fx context (NULL when missing) ---
    fx_rate_to_base: Mapped[Decimal | None] = mapped_column(_RATE)
    fx_rate_date: Mapped[date | None] = mapped_column(Date)
    fx_rate_source: Mapped[str | None] = mapped_column(String(32))
    # fresh | stale | missing | same_currency.
    fx_status: Mapped[str | None] = mapped_column(String(16))
    market_value_local: Mapped[Decimal | None] = mapped_column(_MONEY)
    market_value_base: Mapped[Decimal | None] = mapped_column(_MONEY)
    # valued | missing_price | missing_fx | unresolved_instrument |
    # ambiguous_instrument | cash_only | zero_quantity | stale_price | stale_fx
    valuation_status: Mapped[str] = mapped_column(String(24), nullable=False)
    # ready | blocked | stale | cash (GUI-facing readiness rollup).
    readiness_status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str | None] = mapped_column(String(32))
    # Generic row status (ok); reserved for future per-row lifecycle.
    status: Mapped[str | None] = mapped_column(String(16))
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    snapshot: Mapped[PortfolioValuationSnapshot] = relationship(back_populates="rows")


class Watchlist(Base, TimestampMixin):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fund_id: Mapped[int | None] = mapped_column(ForeignKey("funds.id", ondelete="CASCADE"))
    fund_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="CASCADE")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Scheduled jobs / automation. A real in-process scheduler claims + leases due
# jobs and runs them (see app/workers/scheduler.py); unimplemented job types
# still record a success_stub JobRun.
# ---------------------------------------------------------------------------


class ScheduledJob(Base, TimestampMixin):
    """A recurring/maintenance job the scheduler worker claims and runs.

    Schedule semantics (see `app.workers.scheduler`):
      * ``schedule_kind`` drives recurrence: manual | hourly | daily | weekly |
        interval. ``manual`` jobs never run automatically. ``interval`` uses
        ``interval_seconds``; the named kinds map to fixed intervals.
      * ``schedule_cron`` is retained for display/forward-compat (cron is not the
        primary driver yet) and is never used to compute ``next_run_at``.
      * ``next_run_at`` is the canonical "due" signal; the scheduler initialises a
        null one for active non-manual jobs.

    Leasing (duplicate-run prevention): a due job is claimed with a single atomic
    conditional UPDATE that stamps ``locked_by``/``locked_at``/``lock_expires_at``
    only if the row is unlocked or its lease has expired. So even if several
    scheduler processes exist, exactly one runs a given job; a crashed lease is
    reclaimable after ``lock_expires_at``.
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str | None] = mapped_column(String(32))
    schedule_cron: Mapped[str | None] = mapped_column(String(64))
    # manual | hourly | daily | weekly | interval (default manual = no auto-run).
    schedule_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    # Recurrence period when schedule_kind == "interval" (else derived from kind).
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    # IANA tz for future display; internal scheduling is always UTC.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Outcome of the most recent scheduler-driven run (success/partial/failed).
    last_status: Mapped[str | None] = mapped_column(String(32))
    # --- lease / duplicate-run prevention ---
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lease length / watchdog window (seconds); defaults applied by the scheduler.
    max_runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    # run_once_then_schedule (default) — see scheduler misfire handling.
    misfire_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="run_once_then_schedule"
    )
    # none (default) — retry policy is a forward-compat hook, not yet enforced.
    retry_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="none")


class JobRun(Base):
    """Execution record for a scheduled/maintenance job (supersedes
    `IngestionRun`)."""

    __tablename__ = "job_runs"

    __table_args__ = (
        # Bounded "latest runs of a job type" listing (e.g. the onboarding run
        # history read model): WHERE job_type=:t ORDER BY id DESC LIMIT :n.
        Index("ix_job_runs_job_type_id", "job_type", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheduled_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_jobs.id", ondelete="SET NULL"), index=True
    )
    # Optional target of a backfill run (e.g. a newly resolved instrument).
    fund_id: Mapped[int | None] = mapped_column(
        ForeignKey("funds.id", ondelete="SET NULL"), index=True
    )
    fund_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="SET NULL"), index=True
    )
    # Optional workspace scope (set for workspace-scoped orchestration runs such
    # as instrument_onboarding). Mirrors ``fund_id``; lets the onboarding run
    # history be filtered by workspace with a bounded, indexed query.
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str | None] = mapped_column(Text)
    records_inserted: Mapped[int | None] = mapped_column(Integer)
    records_updated: Mapped[int | None] = mapped_column(Integer)
    records_failed: Mapped[int | None] = mapped_column(Integer)
    # Structured orchestration metadata (typed stage rows + scope + source mode +
    # next action) for parent onboarding runs. NULL for legacy / non-onboarding
    # runs — the read model surfaces those as ``legacy_metadata`` and falls back
    # to ``message``. NOT a free-text channel: do not parse ``message`` for core
    # logic when this is present (see AGENTS.md).
    payload_json: Mapped[Any | None] = mapped_column(JSON)


class SecurityIdentifier(Base, TimestampMixin):
    """Crosswalk + provenance for instrument identifiers.

    Records how an external identifier (ISIN/FIGI/SEDOL/CUSIP/ticker) maps to a
    fund/listing, which `source` asserted it, and at what `confidence`. Tickers
    are intentionally NOT globally unique — uniqueness is per
    (scheme, value, source, exchange, currency).
    """

    __tablename__ = "security_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "scheme", "value", "source", "exchange", "currency", name="uq_security_identifier"
        ),
        # Crosswalk lookups resolve by (scheme, value) — e.g. ISIN -> fund. This
        # index is created by migration 0003; declaring it here keeps the model
        # and migration history in sync so Alembic autogenerate stays quiet.
        Index("ix_security_identifiers_scheme_value", "scheme", "value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # isin | figi | sedol | cusip | ticker
    scheme: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    fund_id: Mapped[int | None] = mapped_column(
        ForeignKey("funds.id", ondelete="CASCADE"), index=True
    )
    fund_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund_listings.id", ondelete="CASCADE"), index=True
    )
    exchange: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # high | medium | low
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="high")
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)


# ---------------------------------------------------------------------------
# Canonical instrument master (constituent securities).
#
# A *generic* security/entity model, deliberately separate from the fund-centric
# funds/fund_listings tables: ETF/fund *constituents* (Apple, Shell, ...) resolve
# here so look-through exposure and future EOD price ingestion have a stable
# identity to attach to. Built generic (equity today; bond/future/option/index
# later) because those asset classes are on the roadmap — but it does NOT replace
# the funds model. Populated idempotently by the constituent_identity_resolution
# worker from holdings rows via OpenFIGI / an offline fixture resolver.
# ---------------------------------------------------------------------------


class Instrument(Base, TimestampMixin):
    """A canonical real-world security/entity (e.g. Apple Inc, Shell PLC).

    Identity dedupes on ``identity_key`` — a deterministic string derived by the
    resolution layer preferring strong identifiers (ISIN > share-class FIGI >
    composite FIGI > FIGI) and only falling back to a normalised name+country+
    currency for offline/manual fixtures. So re-running the resolver never
    duplicates an instrument, and the same constituent held by several funds maps
    to one row. Crosswalk identifiers live in ``instrument_identifiers``; tradable
    listings in ``instrument_listings``.
    """

    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("identity_key", name="uq_instrument_identity_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Deterministic identity (see class docstring). Globally unique.
    identity_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # equity | fund | bond | future | option | index | cash | fx_pair | unknown
    instrument_type: Mapped[str] = mapped_column(String(16), nullable=False, default="equity")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(8))
    # active | pending | ambiguous | unresolved | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Producer/provenance of the canonical row (resolver source or "manual").
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    listings: Mapped[list[InstrumentListing]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan"
    )
    identifiers: Mapped[list[InstrumentIdentifier]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan"
    )
    holdings: Mapped[list[FundHolding]] = relationship(back_populates="instrument")


class InstrumentListing(Base, TimestampMixin):
    """A tradable listing of an instrument (e.g. AAPL / XNAS / USD).

    Future constituent EOD price ingestion fetches by (ticker, mic/exchange,
    currency), so each listing carries enough to drive that without re-resolving.
    Dedupes on ``(instrument_id, listing_key)`` where ``listing_key`` prefers the
    composite FIGI, else ``ticker|mic`` — so re-runs never duplicate a listing.
    """

    __tablename__ = "instrument_listings"
    __table_args__ = (
        UniqueConstraint("instrument_id", "listing_key", name="uq_instrument_listing_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Deterministic identity within an instrument (see class docstring).
    listing_key: Mapped[str] = mapped_column(String(128), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(32))
    exchange: Mapped[str | None] = mapped_column(String(64))
    mic: Mapped[str | None] = mapped_column(String(16))
    currency: Mapped[str | None] = mapped_column(String(8))
    country: Mapped[str | None] = mapped_column(String(64))
    figi: Mapped[str | None] = mapped_column(String(12))
    composite_figi: Mapped[str | None] = mapped_column(String(12))
    share_class_figi: Mapped[str | None] = mapped_column(String(12))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # active | pending | stale | error
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Last time constituent EOD price ingestion stored a price for this listing.
    # Drives read-side freshness + the market-data planner (mirrors
    # ``FundListing.last_price_at``); NULL until the first price lands.
    last_price_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    instrument: Mapped[Instrument] = relationship(back_populates="listings")
    prices: Mapped[list[InstrumentPrice]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class InstrumentIdentifier(Base, TimestampMixin):
    """Crosswalk + provenance for an instrument's external identifiers.

    Separate from ``security_identifiers`` (which crosswalks *funds/listings*).
    Dedupes on ``(instrument_id, scheme, value, source)`` so a new identifier for
    a known instrument inserts only that row, never a duplicate instrument.
    """

    __tablename__ = "instrument_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "scheme", "value", "source", name="uq_instrument_identifier"
        ),
        Index("ix_instrument_identifiers_scheme_value", "scheme", "value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # isin | figi | composite_figi | share_class_figi | cusip | sedol | ticker |
    # ric | openfigi_id | ...
    scheme: Mapped[str] = mapped_column(String(24), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # active | superseded (forward-compat; resolution writes active).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    instrument: Mapped[Instrument] = relationship(back_populates="identifiers")


class InstrumentPrice(Base, TimestampMixin):
    """An end-of-day price for a constituent ``instrument_listing``.

    Deliberately separate from ``prices`` (which is fund-listing oriented and
    stores a single close): an ETF constituent is a generic security and we want
    OHLC + adjusted close + volume for stock detail pages, constituent charts and
    future true look-through valuation. Both tables follow the same rules — a row
    belongs to a *listing*, always records a ``source``, and is monetary/Decimal.

    Idempotency key is ``(instrument_listing_id, price_date, source)`` so re-runs
    and backfills never duplicate a bar, and distinct sources coexist for the same
    listing/date (provenance differs). Read-side freshness is derived from
    ``price_date`` (see ``app/services/freshness.py``); ``status`` carries the
    provider-asserted provenance (fixture | official | estimated | manual | ...).
    Populated idempotently by the ``constituent_eod_price_ingestion`` worker, which
    only fetches resolved listings under the source budget / fetch-log guard.
    """

    __tablename__ = "instrument_prices"
    __table_args__ = (
        UniqueConstraint(
            "instrument_listing_id",
            "price_date",
            "source",
            name="uq_instrument_price_listing_date_source",
        ),
        Index("ix_instrument_prices_price_date", "price_date"),
        Index("ix_instrument_prices_source", "source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_listing_id: Mapped[int] = mapped_column(
        ForeignKey("instrument_listings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal | None] = mapped_column(_MONEY)
    high: Mapped[Decimal | None] = mapped_column(_MONEY)
    low: Mapped[Decimal | None] = mapped_column(_MONEY)
    close: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    adjusted_close: Mapped[Decimal | None] = mapped_column(_MONEY)
    volume: Mapped[Decimal | None] = mapped_column(_MONEY)
    currency: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Provider-asserted provenance: fixture | official | estimated | manual | ...
    status: Mapped[str | None] = mapped_column(String(16))
    # Reserved for provenance/debugging (raw provider payload).
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    listing: Mapped[InstrumentListing] = relationship(back_populates="prices")


# ---------------------------------------------------------------------------
# Derived / cached look-through exposure (workspace-scoped).
#
# Produced by the `exposure_recompute` worker from current positions, latest
# prices, FX and selected holdings snapshots. Deliberately *generic*
# (``dimension``/``bucket``/``label``) so it can carry fund/holding/country/
# sector/industry/currency/source today and later asset_class, direct equities,
# bonds, cash, etc. without a schema change.
# ---------------------------------------------------------------------------


class ExposureSnapshot(Base, TimestampMixin):
    """One derived exposure computation for a workspace at a point in time.

    Idempotent: ``input_hash`` is a deterministic digest of the inputs that
    materially affect exposure (positions/units, prices used, FX used, holdings
    snapshots, base currency, as-of date, source policy). Re-running with the
    same ``input_hash`` as the latest snapshot inserts nothing. Old snapshots are
    preserved as history (drift detection later); the latest is ordered by
    ``(as_of_date, id)``.
    """

    __tablename__ = "exposure_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "as_of_date", "input_hash", name="uq_exposure_snapshot_identity"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Producer of the snapshot (e.g. "exposure_recompute"); provenance.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="exposure_recompute")
    # ok | partial | empty (coverage/validity of the computation).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    # Deterministic digest of all material inputs (idempotency key).
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Component digests (diagnostics / future drift attribution).
    holdings_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    fx_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    position_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    total_market_value_base: Mapped[Decimal | None] = mapped_column(_MONEY)
    # Fraction of portfolio value with a holdings snapshot (looked through).
    coverage_weight: Mapped[Decimal | None] = mapped_column(_WEIGHT)
    # Fraction of portfolio value with no holdings snapshot.
    unclassified_weight: Mapped[Decimal | None] = mapped_column(_WEIGHT)
    missing_holdings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_fx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # --- true constituent look-through coverage (added 0014) ------------------
    # Weight-based fractions of *total portfolio value*, nested so that
    # ``coverage_weight`` (holdings) >= identity >= price >= fx. ``identity`` is
    # the looked-through weight whose constituent resolved to an instrument;
    # ``price`` additionally has a constituent EOD price (any freshness); ``fx``
    # additionally converts that price's currency to base. NULL on pre-0014
    # snapshots / when no look-through ran. The counts are by *distinct resolved
    # instrument* (deduped across funds — Apple held via two ETFs counts once).
    identity_coverage_weight: Mapped[Decimal | None] = mapped_column(_WEIGHT)
    price_coverage_weight: Mapped[Decimal | None] = mapped_column(_WEIGHT)
    fx_coverage_weight: Mapped[Decimal | None] = mapped_column(_WEIGHT)
    constituent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_constituent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priced_constituent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale_constituent_price_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_constituent_price_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    constituent_fx_missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    rows: Mapped[list[ExposureRow]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class ExposureRow(Base):
    """One exposure bucket within a snapshot (queryable by ``dimension``)."""

    __tablename__ = "exposure_rows"
    __table_args__ = (
        Index("ix_exposure_rows_snapshot_dimension", "exposure_snapshot_id", "dimension"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Lookups go through the (exposure_snapshot_id, dimension) composite index
    # below, which also serves snapshot-only queries as a prefix.
    exposure_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("exposure_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    # fund | holding | constituent | country | sector | industry | currency |
    # source | constituent_price_status | constituent_source | asset_class
    # (widened to 32 in 0014 for the constituent_* dimensions).
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    # Stable identity within the dimension (e.g. ISIN, holding_key, "US", "GBP",
    # "instrument:42", a price-status code).
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    weight: Mapped[Decimal] = mapped_column(_WEIGHT, nullable=False)
    # Weight-based *implied* market value (position_mv_base x holding_weight); for
    # constituent rows this is NOT a share/price-derived notional — see
    # ``valuation_method`` and app/services/constituent_valuation.py.
    market_value_base: Mapped[Decimal | None] = mapped_column(_MONEY)
    currency: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str | None] = mapped_column(String(32))
    # ok | approximate | unclassified | missing_holdings | fx_missing |
    # price_missing | stale_price | missing_listing | unresolved_identity
    # (widened to 32 in 0014).
    status: Mapped[str | None] = mapped_column(String(32))
    # --- constituent look-through context (added 0014; NULL on legacy rows) ----
    # Resolved instrument/listing/fund the row pertains to, plus the constituent
    # EOD price + FX used as *valuation context*. Typed so the GUI can deep-link
    # and the read side can filter without unpacking raw_payload_json.
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL")
    )
    instrument_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_listings.id", ondelete="SET NULL")
    )
    fund_id: Mapped[int | None] = mapped_column(ForeignKey("funds.id", ondelete="SET NULL"))
    price_date: Mapped[date | None] = mapped_column(Date)
    price_source: Mapped[str | None] = mapped_column(String(32))
    price_status: Mapped[str | None] = mapped_column(String(16))
    fx_rate: Mapped[Decimal | None] = mapped_column(_RATE)
    fx_source: Mapped[str | None] = mapped_column(String(32))
    # fund_weight_lookthrough | fund_weight_with_constituent_price_context |
    # holding_market_value | holding_shares_x_price | unclassified
    valuation_method: Mapped[str | None] = mapped_column(String(48))
    raw_payload_json: Mapped[Any | None] = mapped_column(JSON)

    snapshot: Mapped[ExposureSnapshot] = relationship(back_populates="rows")


# ---------------------------------------------------------------------------
# Operational foundation for safe external data fetching.
#
# These make recurring jobs + external fetches rate-limited, observable, and
# idempotent BEFORE broad constituent identifier resolution / stock EOD pulls.
# An ETF can hold hundreds of stocks, so naive per-holding loops against
# OpenFIGI / yfinance / Stooq / issuer sites are forbidden — see AGENTS.md.
# ---------------------------------------------------------------------------


class SourceRateLimit(Base, TimestampMixin):
    """Per-source request budget / rate-limit + backoff state.

    Answers, for a source: may it make a request now? how long to wait? is it in
    backoff? what batch size should it use? Fixture/local sources get permissive
    budgets; external sources (openfigi/yfinance/stooq) get conservative ones.
    Nullable constraints mean "unbounded for this window". No secrets here.
    """

    __tablename__ = "source_rate_limits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_requests_per_minute: Mapped[int | None] = mapped_column(Integer)
    max_requests_per_hour: Mapped[int | None] = mapped_column(Integer)
    max_requests_per_day: Mapped[int | None] = mapped_column(Integer)
    max_concurrency: Mapped[int | None] = mapped_column(Integer)
    # Minimum spacing between consecutive requests (politeness delay).
    min_delay_ms: Mapped[int | None] = mapped_column(Integer)
    # Suggested batch size for chunked work (e.g. OpenFIGI accepts batches).
    batch_size: Mapped[int | None] = mapped_column(Integer)
    # Cooldown applied after a rate-limit/failure, and the instant it lifts.
    backoff_seconds: Mapped[int | None] = mapped_column(Integer)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class SourceFetchLog(Base):
    """One external fetch attempt: observability + request-cache metadata.

    Deterministic ``request_key`` (= source + request_kind + normalised params)
    lets the service skip recently-successful identical requests and reason about
    rate budgets. SECURITY: never stores API keys, auth headers, or token-bearing
    URLs — only a short ``endpoint_label`` and hashed payload provenance.
    """

    __tablename__ = "source_fetch_logs"
    __table_args__ = (
        Index("ix_source_fetch_logs_source_kind", "source_name", "request_kind"),
        Index("ix_source_fetch_logs_request_key", "request_key"),
        Index("ix_source_fetch_logs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Logical operation, e.g. "resolve_identity" | "fetch_prices" | "fetch_fx".
    request_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # Deterministic, human-readable key (safe params only, no secrets).
    request_key: Mapped[str] = mapped_column(String(512), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Safe label for the endpoint (host/path class), never a tokenised URL.
    endpoint_label: Mapped[str | None] = mapped_column(String(255))
    method: Mapped[str | None] = mapped_column(String(8))
    # started | success | failed | rate_limited | cache_hit
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="started")
    http_status: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    records_inserted: Mapped[int | None] = mapped_column(Integer)
    records_updated: Mapped[int | None] = mapped_column(Integer)
    records_failed: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    rate_limited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Hash of the raw provider payload (provenance/dedupe) — never the payload.
    raw_payload_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
