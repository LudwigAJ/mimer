"""Issuer facts ingestion service.

Enriches a fund's metadata (official name, provider, domicile, base currency,
distribution policy, strategy, OCF/TER) from an `IssuerFactsSource`, recording
provenance and respecting source priority:

* never overwrites a higher-priority source (e.g. ``manual``);
* an issuer *does* outrank ``seed`` placeholder facts;
* always stamps ``last_refreshed_at`` and flips ``pending`` -> ``active`` on a
  successful fetch, since the issuer has now asserted the facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Fund
from app.sources.issuer import IssuerFacts, IssuerFactsSource

# Lower number = higher priority (mirrors the data_sources priority convention).
_PRIORITY: dict[str | None, int] = {
    "manual": 5,
    "issuer": 10,
    "issuer_fixture": 10,
    "derived": 50,
    "seed": 100,
    None: 100,
}

# (facts attribute, fund attribute) pairs that may be enriched.
_FACT_FIELDS: list[tuple[str, str]] = [
    ("official_name", "name"),
    ("provider", "provider"),
    ("domicile", "domicile"),
    ("base_currency", "base_currency"),
    ("distribution_policy", "distribution_policy"),
    ("strategy", "strategy"),
    ("ocf", "ocf"),
]


@dataclass
class IssuerFactsCounts:
    # Funds activated (pending -> active) by this run.
    inserted: int = 0
    # Total fund fields changed.
    updated: int = 0
    # Funds with no issuer match.
    failed: int = 0


def _priority(source: str | None) -> int:
    return _PRIORITY.get(source, 100)


def _apply_facts(fund: Fund, facts: IssuerFacts) -> tuple[int, bool]:
    """Apply facts to a fund respecting priority. Returns (#fields changed, activated)."""
    new_p = _priority(facts.source)
    old_p = _priority(fund.source)
    outranks = new_p <= old_p

    changed = 0
    for facts_attr, fund_attr in _FACT_FIELDS:
        value = getattr(facts, facts_attr)
        if value is None:
            continue
        current = getattr(fund, fund_attr)
        # Fill empties always; overwrite only when the issuer outranks the
        # incumbent source (so we never clobber a manual override).
        if current is None or outranks:
            if current != value:
                setattr(fund, fund_attr, value)
                changed += 1

    # The issuer has asserted these facts: record provenance + refresh stamp.
    if outranks or fund.source is None:
        fund.source = facts.source
    fund.last_refreshed_at = datetime.now(UTC)
    activated = False
    if fund.status == "pending":
        fund.status = "active"
        activated = True
    return changed, activated


async def ingest_issuer_facts_for_fund(
    session: AsyncSession, fund: Fund, source: IssuerFactsSource
) -> IssuerFactsCounts:
    counts = IssuerFactsCounts()
    facts = await source.fetch(isin=fund.isin)
    if facts is None:
        counts.failed = 1
        return counts
    changed, activated = _apply_facts(fund, facts)
    counts.updated = changed
    counts.inserted = 1 if activated else 0
    await session.flush()
    return counts
