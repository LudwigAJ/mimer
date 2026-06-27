"""Constituent identity resolvers, isolated behind a protocol.

Resolve an ETF/fund *constituent* (a holding row: name + whatever identifiers the
issuer disclosed) to a canonical instrument identity. Two providers ship:

* ``constituent_identity_fixture`` — an offline, deterministic resolver keyed by
  ISIN and normalised name. It knows the seeded/fixture constituents and a few
  intentionally ambiguous / not-found / failing cases, so the worker, budgets,
  fetch logs and tests all work with no network access or API key. Also handy for
  local demos.
* ``openfigi`` — the live OpenFIGI v3 mapping API, in **batches** (up to 10 jobs
  per request). Every call goes through ``guarded_fetch`` (cache → budget →
  fetch-log → fetch); the API key only ever travels in the request header, never
  in the request key / fetch log. Name-only resolution is never attempted here
  (it cannot be done safely) — that is left to the offline fixture / manual entry.

This module is a *source adapter*: it only fetches/parses and returns normalized
``ResolutionCandidate`` dataclasses. The provider-agnostic upsert / linking /
job-bookkeeping lives in ``app/services/constituent_identity.py``. See AGENTS.md
(no uncontrolled per-holding source loops; two-layer ingestion split).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

# Resolution outcome statuses. ``resolved``/``ambiguous``/``not_found``/``failed``
# describe what the resolver concluded; ``skipped_*`` mean no live attempt was
# made (the guarded-fetch layer cached or budget-blocked the request).
RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
FAILED = "failed"
SKIPPED_BUDGET = "skipped_budget"
SKIPPED_CACHED = "skipped_cached"

# Schemes OpenFIGI can resolve strongly (ticker handled separately, only when an
# exchange code *and* currency narrow it). Name-only is never sent to OpenFIGI.
_STRONG_SCHEMES = ("isin", "figi", "composite_figi", "share_class_figi", "cusip", "sedol")

_OPENFIGI_ID_TYPE = {
    "isin": "ID_ISIN",
    "figi": "ID_BB_GLOBAL",
    "composite_figi": "ID_BB_GLOBAL",
    "share_class_figi": "ID_BB_GLOBAL",
    "cusip": "ID_CUSIP",
    "sedol": "ID_SEDOL",
    "ticker": "TICKER",
}
_OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


@dataclass(frozen=True)
class ConstituentRequest:
    """A deduped resolver request derived from one or more holding rows.

    ``input_key`` is the dedupe identity (e.g. one Apple request even if held via
    several funds); the orchestration maps it back to the holding ids it covers.
    ``scheme``/``value`` are the *primary* identifier chosen by priority; the
    remaining fields are passed through for fixture fallbacks / OpenFIGI hints.
    """

    input_key: str
    scheme: str
    value: str
    name: str | None = None
    ticker: str | None = None
    isin: str | None = None
    figi: str | None = None
    cusip: str | None = None
    sedol: str | None = None
    exchange: str | None = None
    mic: str | None = None
    currency: str | None = None
    country: str | None = None


@dataclass(frozen=True)
class ResolutionCandidate:
    """A normalized resolver outcome for one ``input_key``."""

    input_key: str
    status: str
    confidence: str = "none"  # high | medium | low | none
    source: str = "unknown"
    name: str | None = None
    legal_name: str | None = None
    ticker: str | None = None
    exchange: str | None = None
    mic: str | None = None
    currency: str | None = None
    country: str | None = None
    figi: str | None = None
    composite_figi: str | None = None
    share_class_figi: str | None = None
    isin: str | None = None
    cusip: str | None = None
    sedol: str | None = None
    security_type: str | None = None
    market_sector: str | None = None
    raw_payload_json: dict[str, Any] | None = None


class ConstituentResolver(Protocol):
    name: str

    def is_request_safe(self, request: ConstituentRequest) -> bool:
        """Whether this resolver can safely attempt ``request`` (else leave it)."""
        ...

    async def resolve_batch(
        self,
        session: AsyncSession,
        requests: list[ConstituentRequest],
        *,
        batch_size: int,
        ttl_seconds: int,
    ) -> list[ResolutionCandidate]:
        """Resolve a list of (already safe) requests; one candidate per request."""
        ...


def _normalize_name(name: str | None) -> str:
    return " ".join((name or "").lower().split())


# --- fixture provider --------------------------------------------------------

# One offline record per known constituent. Identifiers are real-looking;
# realistic in shape, not guaranteed current. Pipe-delimited:
# name|ticker|isin|cusip|sedol|mic|exchange|currency|country|figi|composite_figi|share_class_figi|type|sector
_FIXTURE_ROWS: tuple[str, ...] = (
    "Apple Inc|AAPL|US0378331005|037833100|2046251|XNAS|NASDAQ|USD|US|BBG000B9XRY4|BBG000B9XVV8|BBG001S5N8V8|Common Stock|Equity",
    "Microsoft Corp|MSFT|US5949181045|594918104|2588173|XNAS|NASDAQ|USD|US|BBG000BPH459|BBG000BPH459|BBG001S5TD05|Common Stock|Equity",
    "NVIDIA Corp|NVDA|US67066G1040|67066G104|2379504|XNAS|NASDAQ|USD|US|BBG000BBJQV0|BBG000BBJQV0|BBG001S5TZJ6|Common Stock|Equity",
    "Amazon.com Inc|AMZN|US0231351067|023135106|2000019|XNAS|NASDAQ|USD|US|BBG000BVPV84|BBG000BVPV84|BBG001S5PQL7|Common Stock|Equity",
    "Meta Platforms Inc|META|US30303M1027|30303M102|B7TL820|XNAS|NASDAQ|USD|US|BBG000MM2P62|BBG000MM2P62|BBG001S8X6S8|Common Stock|Equity",
    "Alphabet Inc|GOOGL|US02079K3059|02079K305|BYVY8G0|XNAS|NASDAQ|USD|US|BBG009S39JX6|BBG009S39JX6|BBG009S39JY5|Common Stock|Equity",
    "Alphabet Inc C|GOOG|US02079K1079|02079K107|BYY88Y7|XNAS|NASDAQ|USD|US|BBG009S3NB30|BBG009S3NB30|BBG009S3NB49|Common Stock|Equity",
    "Broadcom Inc|AVGO|US11135F1012|11135F101|BDZ78H9|XNAS|NASDAQ|USD|US|BBG00KHY5S69|BBG00KHY5S69|BBG00KHY5S78|Common Stock|Equity",
    "Berkshire Hathaway Inc B|BRK.B|US0846707026|084670702|2073390|XNYS|NYSE|USD|US|BBG000DN7P92|BBG000DN7P92|BBG000DN7PT5|Common Stock|Equity",
    "JPMorgan Chase & Co|JPM|US46625H1005|46625H100|2190385|XNYS|NYSE|USD|US|BBG000DMBXR2|BBG000DMBXR2|BBG001S5S399|Common Stock|Equity",
    "Mastercard Inc A|MA|US57636Q1040|57636Q104|B121557|XNYS|NYSE|USD|US|BBG000F1ZSN5|BBG000F1ZSN5|BBG001S6FXR0|Common Stock|Equity",
    "Progressive Corp|PGR|US7433151039|743315103|2683732|XNYS|NYSE|USD|US|BBG000BNN758|BBG000BNN758|BBG001S5SXX1|Common Stock|Equity",
    "Trane Technologies PLC|TT|IE00BK9ZQ967|G8994E103|BK9ZQ96|XNYS|NYSE|USD|IE|BBG00KGN23K3|BBG00KGN23K3|BBG00KGN23L2|Common Stock|Equity",
    "Shell PLC|SHEL|GB00BP6MXD84|G8060K102|BP6MXD8|XLON|LSE|GBP|GB|BBG00KP6PB35|BBG00KP6PB35|BBG00KP6PBM6|Common Stock|Equity",
    "AstraZeneca PLC|AZN|GB0009895292|046353108|0989529|XLON|LSE|GBP|GB|BBG000C0YGH4|BBG000C0YGH4|BBG001S5VNK6|Common Stock|Equity",
    "HSBC Holdings PLC|HSBA|GB0005405286|404280406|0540528|XLON|LSE|GBP|GB|BBG000BD3SC0|BBG000BD3SC0|BBG001S5R3T6|Common Stock|Equity",
    "Unilever PLC|ULVR|GB00B10RZP78|904767704|B10RZP7|XLON|LSE|GBP|GB|BBG00H4M2NX4|BBG00H4M2NX4|BBG00H4M2P26|Common Stock|Equity",
    "BP PLC|BP|GB0007980591|055622104|0798059|XLON|LSE|GBP|GB|BBG000C05BD1|BBG000C05BD1|BBG001S5QNL5|Common Stock|Equity",
    "GSK PLC|GSK|GB00BN7SWP63|37733W105|BN7SWP6|XLON|LSE|GBP|GB|BBG000CT5GN5|BBG000CT5GN5|BBG001S5TG78|Common Stock|Equity",
    "Rio Tinto PLC|RIO|GB0007188757|767204100|0718875|XLON|LSE|GBP|GB|BBG000DZG3J1|BBG000DZG3J1|BBG001S60RH6|Common Stock|Equity",
    "Diageo PLC|DGE|GB0002374006|25243Q205|0237400|XLON|LSE|GBP|GB|BBG000BS69D5|BBG000BS69D5|BBG001S5R5L3|Common Stock|Equity",
    "BAE Systems PLC|BA|GB0002634946|05523F101|0263494|XLON|LSE|GBP|GB|BBG000BF7D29|BBG000BF7D29|BBG001S5QF77|Common Stock|Equity",
    "RELX PLC|REL|GB00B2B0DG97|759530108|B2B0DG9|XLON|LSE|GBP|GB|BBG000D03N94|BBG000D03N94|BBG001S5VND1|Common Stock|Equity",
    "ASML Holding NV|ASML|NL0010273215|N07059202|B929F46|XAMS|Euronext Amsterdam|EUR|NL|BBG000C1HQS5|BBG000C1HQS5|BBG001S6PD03|Common Stock|Equity",
    "Nestle SA|NESN|CH0038863350|H57312649|7123870|XSWX|SIX|CHF|CH|BBG000CPBD63|BBG000CPBD63|BBG001S6XB97|Common Stock|Equity",
    "Novo Nordisk A/S B|NOVO-B|DK0062498333|670100205|BPVPQH0|XCSE|Nasdaq Copenhagen|DKK|DK|BBG000F8Z1D0|BBG000F8Z1D0|BBG001S6XB23|Common Stock|Equity",
    # Common *directly-held* imported instruments (broker CSV), not just ETF
    # constituents: a US equity (Tesla) and a UCITS ETF that is not in the seeded
    # funds universe (JEPG). The seeded ETFs VUSA/ISF deliberately stay out — an
    # imported VUSA/ISF row resolves to the existing *fund* listing (existing
    # identity), never a duplicate instrument. See imported_instrument_resolution.
    "Tesla Inc|TSLA|US88160R1014|88160R101|B616C79|XNAS|NASDAQ|USD|US|BBG000N9MNX3|BBG000N9MNX3|BBG001SQKGD7|Common Stock|Equity",
    "JPMorgan Global Equity Premium Income Active UCITS ETF|JEPG|IE0003UVYC20||BMGYZF1|XLON|LSE|USD|IE|BBG01HBLP1K9|BBG01HBLP1K9|BBG01HBLP1L8|ETP|Equity",
)

_FIXTURE_COLUMNS = (
    "name",
    "ticker",
    "isin",
    "cusip",
    "sedol",
    "mic",
    "exchange",
    "currency",
    "country",
    "figi",
    "composite_figi",
    "share_class_figi",
    "security_type",
    "market_sector",
)

# Intentionally ambiguous / failing constituents for tests + demos (matched by
# normalised name, so a test can insert a holding with one of these names).
_FIXTURE_AMBIGUOUS_NAMES = frozenset({"ambiguous holdco", "ambiguous co"})
_FIXTURE_FAILED_NAMES = frozenset({"force failure plc", "broken constituent"})


def _build_fixture_record(line: str) -> dict[str, Any]:
    record = dict(zip(_FIXTURE_COLUMNS, line.split("|"), strict=True))
    return {k: (v or None) for k, v in record.items()}


_FIXTURE_BY_ISIN: dict[str, dict[str, Any]] = {}
_FIXTURE_BY_NAME: dict[str, dict[str, Any]] = {}
_FIXTURE_BY_TICKER: dict[str, dict[str, Any]] = {}
for _line in _FIXTURE_ROWS:
    _rec = _build_fixture_record(_line)
    _FIXTURE_BY_ISIN[_rec["isin"].upper()] = _rec
    _FIXTURE_BY_NAME[_normalize_name(_rec["name"])] = _rec
    # Last writer wins per ticker; ticker is a weak key only used as a fallback.
    _FIXTURE_BY_TICKER.setdefault(_rec["ticker"].upper(), _rec)


class FixtureConstituentResolver:
    """Offline deterministic constituent resolver (no network, no API key)."""

    name = "constituent_identity_fixture"

    def is_request_safe(self, request: ConstituentRequest) -> bool:
        # The fixture is offline and deterministic, so every scheme — including
        # name-only — is safe to attempt against it.
        return True

    def _lookup(self, request: ConstituentRequest) -> dict[str, Any] | None:
        isin = (request.isin or (request.value if request.scheme == "isin" else "") or "").upper()
        if isin and isin in _FIXTURE_BY_ISIN:
            return _FIXTURE_BY_ISIN[isin]
        norm = _normalize_name(request.name)
        if norm and norm in _FIXTURE_BY_NAME:
            return _FIXTURE_BY_NAME[norm]
        ticker = (
            request.ticker or (request.value if request.scheme == "ticker" else "") or ""
        ).upper()
        if ticker and ticker in _FIXTURE_BY_TICKER:
            return _FIXTURE_BY_TICKER[ticker]
        return None

    def _candidate(self, request: ConstituentRequest) -> ResolutionCandidate:
        norm = _normalize_name(request.name)
        if norm in _FIXTURE_FAILED_NAMES:
            return ResolutionCandidate(input_key=request.input_key, status=FAILED, source=self.name)
        if norm in _FIXTURE_AMBIGUOUS_NAMES:
            return ResolutionCandidate(
                input_key=request.input_key,
                status=AMBIGUOUS,
                confidence="low",
                source=self.name,
                name=request.name,
            )
        record = self._lookup(request)
        if record is None:
            return ResolutionCandidate(
                input_key=request.input_key,
                status=NOT_FOUND,
                source=self.name,
                name=request.name,
            )
        return ResolutionCandidate(
            input_key=request.input_key,
            status=RESOLVED,
            confidence="high",
            source=self.name,
            name=record["name"],
            ticker=record["ticker"],
            exchange=record["exchange"],
            mic=record["mic"],
            currency=record["currency"],
            country=record["country"],
            figi=record["figi"],
            composite_figi=record["composite_figi"],
            share_class_figi=record["share_class_figi"],
            isin=record["isin"],
            cusip=record["cusip"],
            sedol=record["sedol"],
            security_type=record["security_type"],
            market_sector=record["market_sector"],
            raw_payload_json={"resolver": self.name, "matched": record["isin"]},
        )

    async def resolve_batch(
        self,
        session: AsyncSession,
        requests: list[ConstituentRequest],
        *,
        batch_size: int,
        ttl_seconds: int,
    ) -> list[ResolutionCandidate]:
        # Offline + permissive: no guarded_fetch needed (no network, no budget to
        # protect). One deterministic candidate per request.
        return [self._candidate(request) for request in requests]


# --- openfigi provider -------------------------------------------------------


class OpenFigiConstituentResolver:
    """Live OpenFIGI batch resolver, guarded by cache + budget + fetch logs."""

    name = "openfigi"

    def is_request_safe(self, request: ConstituentRequest) -> bool:
        if request.scheme in _STRONG_SCHEMES:
            return True
        # A bare ticker is only safe when an exchange/MIC *and* currency narrow it.
        if request.scheme == "ticker":
            return bool((request.exchange or request.mic) and request.currency)
        return False  # name-only is never safe for OpenFIGI

    def _job(self, request: ConstituentRequest) -> dict[str, str]:
        job: dict[str, str] = {
            "idType": _OPENFIGI_ID_TYPE[request.scheme],
            "idValue": request.value,
        }
        if request.scheme == "ticker":
            if request.exchange or request.mic:
                job["exchCode"] = request.exchange or request.mic or ""
            if request.currency:
                job["currency"] = request.currency
        return job

    async def _call(self, jobs: list[dict[str, str]], headers: dict[str, str]) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(_OPENFIGI_URL, json=jobs, headers=headers)
            response.raise_for_status()
            return response.json()

    def _candidate_from_rows(
        self, request: ConstituentRequest, rows: list[dict[str, Any]]
    ) -> ResolutionCandidate:
        if not rows:
            return ResolutionCandidate(
                input_key=request.input_key, status=NOT_FOUND, source=self.name
            )
        strong = request.scheme in _STRONG_SCHEMES
        if len(rows) > 1:
            # A strong identifier (ISIN/CUSIP/SEDOL/FIGI) commonly maps to several
            # *listings of the same security* across venues — that is not genuine
            # ambiguity. The share-class FIGI is the globally-unique per-security
            # id (composite FIGI is per country/venue, so multi-country listings
            # differ), so collapse on it (else name). A bare ticker, by contrast,
            # really can mean different companies => ambiguous.
            keys = {(r.get("shareClassFIGI") or r.get("name") or "").upper() for r in rows}
            if not strong or len(keys) > 1:
                return ResolutionCandidate(
                    input_key=request.input_key,
                    status=AMBIGUOUS,
                    confidence="low",
                    source=self.name,
                    name=rows[0].get("name"),
                )
            # Same instrument, several listings: resolve to it (primary = first row).
        row = rows[0]
        # OpenFIGI never returns ISIN; only echo the input ISIN when we sent one.
        isin = request.value if request.scheme == "isin" else None
        return ResolutionCandidate(
            input_key=request.input_key,
            status=RESOLVED,
            confidence="high" if strong else "medium",
            source=self.name,
            name=row.get("name"),
            ticker=row.get("ticker"),
            exchange=row.get("exchCode"),
            mic=request.mic,
            currency=request.currency,
            country=request.country,
            figi=row.get("figi"),
            composite_figi=row.get("compositeFIGI"),
            share_class_figi=row.get("shareClassFIGI"),
            isin=isin,
            cusip=request.cusip,
            sedol=request.sedol,
            security_type=row.get("securityType"),
            market_sector=row.get("marketSector"),
            raw_payload_json={"figi": row.get("figi"), "ticker": row.get("ticker")},
        )

    async def resolve_batch(
        self,
        session: AsyncSession,
        requests: list[ConstituentRequest],
        *,
        batch_size: int,
        ttl_seconds: int,
    ) -> list[ResolutionCandidate]:
        from app.core.config import get_settings
        from app.services import source_budget

        settings = get_settings()
        headers = {"Content-Type": "application/json"}
        if settings.openfigi_api_key:
            # Header only — never the request key / fetch log.
            headers["X-OPENFIGI-APIKEY"] = settings.openfigi_api_key

        size = max(1, batch_size or 10)
        out: list[ResolutionCandidate] = []
        for start in range(0, len(requests), size):
            chunk = requests[start : start + size]
            jobs = [self._job(r) for r in chunk]
            # Deterministic, secrets-free batch params for the request key.
            params = {
                "batch": [
                    f"{j['idType']}:{j['idValue']}:{j.get('exchCode', '')}:{j.get('currency', '')}"
                    for j in jobs
                ]
            }
            result, payload = await source_budget.guarded_fetch(
                session,
                source=self.name,
                request_kind="resolve_constituent_identity",
                params=params,
                endpoint_label="api.openfigi.com/v3/mapping",
                method="POST",
                ttl_seconds=ttl_seconds,
                fetch=lambda jobs=jobs: self._call(jobs, headers),
            )
            if result.status == source_budget.source_requests.RATE_LIMITED:
                out.extend(
                    ResolutionCandidate(
                        input_key=r.input_key, status=SKIPPED_BUDGET, source=self.name
                    )
                    for r in chunk
                )
                continue
            if result.cache_hit or payload is None:
                out.extend(
                    ResolutionCandidate(
                        input_key=r.input_key, status=SKIPPED_CACHED, source=self.name
                    )
                    for r in chunk
                )
                continue
            # Payload is a list aligned with jobs order; each entry has data/error.
            for request, entry in zip(chunk, payload, strict=False):
                if not isinstance(entry, dict) or entry.get("error"):
                    out.append(
                        ResolutionCandidate(
                            input_key=request.input_key, status=FAILED, source=self.name
                        )
                    )
                    continue
                out.append(self._candidate_from_rows(request, entry.get("data", []) or []))
        return out


# --- registry ----------------------------------------------------------------

_RESOLVERS: dict[str, ConstituentResolver] = {
    FixtureConstituentResolver.name: FixtureConstituentResolver(),
    OpenFigiConstituentResolver.name: OpenFigiConstituentResolver(),
}


def get_constituent_resolver(name: str | None = None) -> ConstituentResolver:
    from app.core.config import get_settings

    resolver_name = name or get_settings().constituent_identity_source_default
    resolver = _RESOLVERS.get(resolver_name)
    if resolver is None:
        raise ValueError(f"Unknown constituent resolver: {resolver_name!r}")
    return resolver
