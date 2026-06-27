"""Reference-rate read schemas (official/reference rate observations).

These shape the GUI-facing ``/api/v1/rates`` reads. They expose the stored
observation + its provenance (source / status / source_url), never a constructed
curve or discount factor — the backend only collects and serves official
observations (see AGENTS.md compute boundary). ``raw_payload_json`` is not
exposed by default, matching the project's other reference-data read schemas.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.common import DecimalStr, ORMModel


class ReferenceRateRead(ORMModel):
    id: int
    rate_date: date
    observed_at: datetime | None = None
    currency: str
    country_or_region: str
    rate_family: str
    rate_name: str
    tenor: str | None = None
    tenor_months: int | None = None
    rate_value: DecimalStr
    unit: str
    source: str
    # fixture | official | estimated | manual (provider-asserted provenance).
    status: str | None = None
    source_url: str | None = None
    created_at: datetime


class ReferenceRatePointRead(BaseModel):
    rate_date: date
    rate_value: DecimalStr
    unit: str
    source: str
    status: str | None = None
    tenor: str | None = None


class ReferenceRateSeriesRead(BaseModel):
    rate_name: str | None
    currency: str | None
    country_or_region: str | None
    tenor: str | None
    source: str | None
    unit: str | None
    points: list[ReferenceRatePointRead]


class ReferenceRateSourceRead(BaseModel):
    source: str
    # implemented | planned
    adapter_status: str
    # True for the offline deterministic fixture; False for live/official adapters.
    is_fixture: bool
    # True when running this source makes official network calls (live adapters).
    requires_live_fetch: bool
    # True for the configured default rates source (the rest are explicit-only).
    is_default: bool
    description: str
    currencies: list[str]
    rate_families: list[str]
