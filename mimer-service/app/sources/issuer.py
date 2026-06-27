"""Issuer fact sources, isolated behind a small protocol + registry.

Issuer facts (official name, provider, domicile, base currency, distribution
policy, strategy, OCF/TER) are authoritative fund metadata published by the
fund's issuer (Vanguard, iShares/BlackRock, J.P. Morgan AM, ...).

This iteration ships a robust **fixture** provider so the worker, job plumbing
and tests work with no live network access. Real per-issuer scraping/API
adapters slot in behind the same `IssuerFactsSource` protocol later (see the
roadmap); the worker and API never depend on a specific provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class IssuerFacts:
    isin: str
    source: str
    official_name: str | None = None
    provider: str | None = None
    domicile: str | None = None
    base_currency: str | None = None
    distribution_policy: str | None = None
    strategy: str | None = None
    ocf: Decimal | None = None


class IssuerFactsSource(Protocol):
    name: str

    async def fetch(self, *, isin: str) -> IssuerFacts | None:
        """Return authoritative facts for an ISIN, or None if unknown."""
        ...


# --- fixture provider --------------------------------------------------------

# Keyed by ISIN. Deliberately authoritative-but-offline: values mirror the
# seeded funds (so re-runs are idempotent) while the `strategy` strings are a
# touch fuller than the seed to exercise a real field update on first run.
_FIXTURES: dict[str, dict[str, object]] = {
    "IE00B3XXRP09": {
        "official_name": "Vanguard S&P 500 UCITS ETF",
        "provider": "Vanguard",
        "domicile": "IE",
        "base_currency": "USD",
        "distribution_policy": "distributing",
        "strategy": "Replicates the S&P 500 Index (large-cap US equities)",
        "ocf": Decimal("0.07000"),
    },
    "IE0005042456": {
        "official_name": "iShares Core FTSE 100 UCITS ETF",
        "provider": "iShares (BlackRock)",
        "domicile": "IE",
        "base_currency": "GBP",
        "distribution_policy": "distributing",
        "strategy": "Replicates the FTSE 100 Index (large-cap UK equities)",
        "ocf": Decimal("0.07000"),
    },
    "IE0003UVYC20": {
        "official_name": "JPMorgan Global Equity Premium Income Active UCITS ETF",
        "provider": "J.P. Morgan Asset Management",
        "domicile": "IE",
        "base_currency": "USD",
        "distribution_policy": "distributing",
        "strategy": "Active global equity with a covered-call (premium income) overlay",
        "ocf": Decimal("0.35000"),
    },
}


class StaticIssuerFactsSource:
    """Offline issuer-facts provider backed by a fixture table."""

    name = "issuer_fixture"

    async def fetch(self, *, isin: str) -> IssuerFacts | None:
        data = _FIXTURES.get(isin)
        if data is None:
            return None
        return IssuerFacts(isin=isin, source=self.name, **data)  # type: ignore[arg-type]


# --- registry ----------------------------------------------------------------

_SOURCES: dict[str, IssuerFactsSource] = {
    StaticIssuerFactsSource.name: StaticIssuerFactsSource(),
}


def get_issuer_facts_source(name: str | None = None) -> IssuerFactsSource:
    from app.core.config import get_settings

    source_name = name or get_settings().issuer_facts_source_default
    source = _SOURCES.get(source_name)
    if source is None:
        raise ValueError(f"Unknown issuer facts source: {source_name!r}")
    return source
