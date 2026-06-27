"""Schemas for the target-fund data-source coverage matrix.

Mirrors ``app/sources/fund_source_coverage.py``: per *(fund, data type)* live
readiness for VUSA / ISF / JEPG, plus per-fund and overall rollups. Public
reference identifiers only — never secrets or tokenised URLs.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.schemas.common import Meta


class FundCoverageRead(BaseModel):
    """One *(fund, data type)* cell of live-readiness truth."""

    fund_symbol: str
    isin: str
    issuer: str
    # facts | listing_price | nav | holdings | distributions | documents
    data_type: str
    source_name: str | None
    # fixture | implemented_live | verified_live | candidate | planned | unsupported
    status: str
    live_fetch_verified: bool
    parse_verified: bool
    stored_verified: bool
    safe_for_scheduler: bool
    last_verified_at: date | None
    known_blocker: str | None
    next_action: str | None
    notes: str | None
    offline_export_available: bool


class FundCoverageFundSummary(BaseModel):
    """Per-fund rollup: which data types are live, and the fund's blockers."""

    fund_symbol: str
    issuer: str
    isin: str
    live_price: bool
    live_holdings: bool
    live_distributions: bool
    live_facts: bool
    live_documents: bool
    data_type_status: dict[str, str]
    blockers: list[str]


class FundCoverageSummary(BaseModel):
    """Compact rollup of the target-fund coverage matrix (also embedded elsewhere)."""

    target_funds_total: int
    target_funds_with_live_price: int
    target_funds_with_live_holdings: int
    target_funds_with_live_distributions: int
    target_funds_with_live_facts: int
    target_funds_with_live_documents: int
    fund_sources_verified_live: int
    fund_sources_candidate: int
    fund_sources_planned: int
    fund_sources_fixture_only: int
    fund_source_blockers: int
    funds: list[FundCoverageFundSummary]


class FundCoverageMatrix(BaseModel):
    """The fund coverage matrix list (``{data, meta}`` envelope) plus its summary."""

    data: list[FundCoverageRead]
    meta: Meta
    summary: FundCoverageSummary
