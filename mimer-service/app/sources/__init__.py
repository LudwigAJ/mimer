"""Price source adapters, isolated behind a small registry.

The rest of the code depends on the `PriceSource` protocol and `get_price_source`,
never on a specific provider.
"""

from __future__ import annotations

from app.sources.base import PricePoint, PriceSource
from app.sources.distributions import (
    DistributionRecord,
    DistributionSource,
    get_distribution_source,
)
from app.sources.documents import (
    DocumentRecord,
    DocumentSource,
    get_document_source,
)
from app.sources.fx import (
    FxRateRecord,
    FxSource,
    get_fx_source,
)
from app.sources.holdings import (
    HoldingRecord,
    HoldingsSource,
    get_holdings_source,
    holding_identity_key,
)
from app.sources.issuer import (
    IssuerFacts,
    IssuerFactsSource,
    get_issuer_facts_source,
)
from app.sources.stooq import StooqSource
from app.sources.yfinance import YFinanceSource

_SOURCES: dict[str, PriceSource] = {
    StooqSource.name: StooqSource(),
    YFinanceSource.name: YFinanceSource(),
}


def get_price_source(name: str | None = None) -> PriceSource:
    from app.core.config import get_settings

    source_name = name or get_settings().price_source_default
    source = _SOURCES.get(source_name)
    if source is None:
        raise ValueError(f"Unknown price source: {source_name!r}")
    return source


__all__ = [
    "DistributionRecord",
    "DistributionSource",
    "DocumentRecord",
    "DocumentSource",
    "FxRateRecord",
    "FxSource",
    "HoldingRecord",
    "HoldingsSource",
    "IssuerFacts",
    "IssuerFactsSource",
    "PricePoint",
    "PriceSource",
    "get_distribution_source",
    "get_document_source",
    "get_fx_source",
    "get_holdings_source",
    "get_issuer_facts_source",
    "get_price_source",
    "holding_identity_key",
]
