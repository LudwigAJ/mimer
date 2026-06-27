"""Schemas for the source-capability registry + service capability discovery."""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.common import ORMModel
from app.schemas.source_readiness import SourceReadinessSummary


class SourceCapabilityRead(ORMModel):
    """One row of the data-source capability catalogue (`app/sources/registry`)."""

    source_name: str
    source_type: str
    asset_classes: list[str]
    data_types: list[str]
    reliability_tier: str
    requires_api_key: bool
    supports_history: bool
    supports_intraday: bool
    supports_live: bool
    supports_identifiers: bool
    adapter_status: str
    notes: str | None
    tags: list[str]
    # Known issuer source-config awareness (live issuer holdings/distribution sources).
    # ``requires_url`` marks an explicit-only live adapter that needs a download URL
    # (known config or ``--url``); ``known_config_available`` is True when at least one
    # usable (verified/candidate) per-fund config is registered for this source;
    # ``config_status`` is the best registered status (verified > candidate);
    # ``example_fund_identifiers`` lists a few configured funds (ticker:ISIN). Public
    # issuer identifiers only — never secrets/tokenised URLs.
    requires_url: bool = False
    known_config_available: bool = False
    config_status: str | None = None
    example_fund_identifiers: list[str] = []


class DataTypeStatus(BaseModel):
    name: str
    # real | fixture | stub | planned
    status: str


class CapabilitiesEnvironment(BaseModel):
    """Non-secret runtime configuration (never exposes keys/DSNs)."""

    base_currency: str
    resolver_default_provider: str
    price_source_default: str
    distribution_source_default: str
    issuer_facts_source_default: str
    holdings_source_default: str
    fx_source_default: str
    document_source_default: str
    openfigi_api_key_configured: bool


class CapabilitiesResponse(BaseModel):
    service: str
    version: str
    migration_head: str
    environment: CapabilitiesEnvironment
    # feature/worker -> "real" | "fixture" | "stub" | "planned"
    features: dict[str, str]
    # status -> job types with that status
    workers: dict[str, list[str]]
    # logical area -> configured provider adapter (None for stub/planned)
    configured_sources: dict[str, str | None]
    supported_asset_classes: list[str]
    planned_asset_classes: list[str]
    data_types: list[DataTypeStatus]
    sources: list[SourceCapabilityRead]
    # Production data-source readiness rollup: makes it obvious what is fixture-only vs live
    # verified vs candidate vs planned, and which sources are safe to put on the scheduler.
    source_readiness: SourceReadinessSummary
