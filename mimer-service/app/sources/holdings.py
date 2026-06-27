"""Holdings (look-through constituents) sources, isolated behind a protocol.

A *holding* is a single constituent of a fund's portfolio on a given disclosure
date: its name, identifiers, classification (country/sector/industry/currency),
portfolio weight, and optionally market value / share count. Holdings belong to
the fund (not a listing) — see the identity rules in AGENTS.md.

This module ships a robust offline **fixture** provider plus live issuer adapters
that fetch issuer-published ETF holdings files through ``guarded_fetch`` (source
budget + fetch log + request cache + timeout), store no secrets and never scrape
brittle HTML as a canonical source:

* **iShares / BlackRock** (``blackrock_ishares_holdings``) — the issuer-hosted
  holdings CSV download (``...ajax?dataType=fund&fileName=<TICKER>_holdings&fileType=csv``);
* **J.P. Morgan Asset Management** (``jpmorgan_etf_holdings``) — the daily ETF
  holdings export from ``FundsMarketingHandler`` (content-sniffed: CSV / TSV /
  HTML-table, or an OOXML ``.xlsx`` workbook via the stdlib; the legacy binary
  ``.xls`` (OLE2) is deferred — no pandas / no binary-Excel dependency, see
  docs/data_sources.md);
* **Vanguard exported file** (``vanguard_holdings_export``) — an OFFLINE parser for
  a manually exported official Vanguard holdings file (no live fetch); the live
  ``vanguard_holdings`` adapter stays *planned* until a stable official
  machine-readable endpoint is verified.

The live issuer adapters are **explicit-only**: the configured default stays the
offline fixture (``holdings_fixture``), so the worker/scheduler never makes a
surprise live call. They take an explicit download ``url`` (or fall back to a
small registry of verified known URLs keyed by ISIN); without one they are a
clean no-op (empty), never an error.

COMPUTE BOUNDARY (see AGENTS.md): this layer only *fetches, parses and normalises*
published holdings into the canonical row model. NO look-through analytics, PnL,
total return or index-constituent substitution here or anywhere in the backend —
those belong in the Rust GUI / local pricer. One bad row is isolated (skipped),
never failing the whole file.

Note: this module is a *source adapter* (provider-specific fetch/parse). The
provider-agnostic upsert / provenance / job_runs logic lives in
``app/services/holdings_ingestion.py``.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from app.sources import issuer_source_config, spreadsheet

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_WEIGHT_Q = Decimal("0.00000001")  # 8 dp, matches FundHolding.weight Numeric(12, 8)
_MONEY_Q = Decimal("0.00000001")  # 8 dp, matches FundHolding market_value/shares Numeric(24, 8)

# Column max lengths (mirror the FundHolding model) so an over-long provider cell
# truncates rather than failing the whole row at upsert time.
_MAXLEN: dict[str, int] = {
    "name": 255,
    "ticker": 32,
    "isin": 12,
    "sedol": 7,
    "cusip": 9,
    "figi": 12,
    "country": 64,
    "sector": 64,
    "industry": 64,
    "currency": 8,
}


def holding_identity_key(
    *,
    isin: str | None = None,
    figi: str | None = None,
    cusip: str | None = None,
    sedol: str | None = None,
    name: str,
    ticker: str | None = None,
) -> str:
    """Deterministic identity for a holding within a fund snapshot.

    Idempotent upserts key on (fund, as_of_date, source, this string). The
    preference order — ISIN > FIGI > CUSIP > SEDOL > normalised name+ticker —
    mirrors the resolution rules in the task spec. Deliberately *not* fuzzy:
    a stable normalised fallback, never similarity matching.
    """
    for scheme, value in (
        ("isin", isin),
        ("figi", figi),
        ("cusip", cusip),
        ("sedol", sedol),
    ):
        if value and value.strip():
            return f"{scheme}:{value.strip().upper()}"
    norm_name = " ".join(name.lower().split())
    norm_ticker = (ticker or "").strip().lower()
    return f"name:{norm_name}|{norm_ticker}"


@dataclass(frozen=True)
class HoldingRecord:
    """A normalized fund holding for one disclosure (``as_of_date``) snapshot."""

    as_of_date: date
    holding_name: str
    weight: Decimal
    source: str
    holding_ticker: str | None = None
    holding_isin: str | None = None
    holding_sedol: str | None = None
    holding_cusip: str | None = None
    holding_figi: str | None = None
    country: str | None = None
    sector: str | None = None
    industry: str | None = None
    currency: str | None = None
    market_value: Decimal | None = None
    shares: Decimal | None = None
    # current | estimated | official | official_export | fixture | ... (optional).
    status: str | None = None
    # Reserved for provenance/debugging (raw provider payload + parsed extras such
    # as asset_class / security_type / price / coupon / maturity_date that have no
    # dedicated canonical column).
    raw_payload: dict[str, Any] | None = None

    @property
    def identity_key(self) -> str:
        return holding_identity_key(
            isin=self.holding_isin,
            figi=self.holding_figi,
            cusip=self.holding_cusip,
            sedol=self.holding_sedol,
            name=self.holding_name,
            ticker=self.holding_ticker,
        )


class HoldingsSource(Protocol):
    name: str

    async def fetch(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,
        url: str | None = None,
    ) -> list[HoldingRecord]:
        """Return holdings for a fund ISIN (possibly empty for an unknown fund).

        Offline fixtures ignore ``session``/``url``. Live issuer adapters need the
        ``session`` (for budget/fetch-log/cache) the ingestion service passes, and
        take an explicit ``url`` download override (falling back to a known-URL
        registry); without a URL they return an empty list (a clean no-op).
        """
        ...


def fixture_as_of(today: date | None = None) -> date:
    """Disclosure date for the fixture snapshot: the previous month-end.

    Stable within a calendar month (so re-runs on the same day are idempotent),
    always recent enough to read as ``fresh`` under the holdings freshness
    window, and distinct from the seed rows' ``date.today()`` so the two
    snapshots are separate rows (read-side selection prefers the fixture).
    """
    anchor = today or date.today()
    return anchor.replace(day=1) - timedelta(days=1)


# --- shared value cleaning ---------------------------------------------------

# Provider placeholders that mean "no value".
_BLANKS = {"", "-", "--", "---", "n/a", "na", "null", "none", "—"}


def _norm_header(text: str) -> str:
    """Normalise a header cell to ``[a-z0-9]`` for robust column matching."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _clean_text(value: Any, *, maxlen: int | None = None, upper: bool = False) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _BLANKS:
        return None
    if upper:
        s = s.upper()
    if maxlen is not None:
        s = s[:maxlen]
    return s or None


def _clean_decimal(value: Any) -> Decimal | None:
    """Parse a messy numeric cell (commas/%/currency symbols/parentheses) safely."""
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _BLANKS:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    # Strip thousands separators, percent signs, currency symbols and whitespace.
    s = re.sub(r"[,%\s $£€¥]", "", s)
    if s in {"", "-", "+", "."}:
        return None
    try:
        result = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return -result if negative else result


def _percent_to_weight(value: Any) -> Decimal | None:
    """A provider percent (e.g. ``8.10`` for 8.10%) -> fraction (``0.081``)."""
    pct = _clean_decimal(value)
    if pct is None:
        return None
    return (pct / Decimal(100)).quantize(_WEIGHT_Q)


def _clean_money(value: Any) -> Decimal | None:
    money = _clean_decimal(value)
    return money.quantize(_MONEY_Q) if money is not None else None


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%b/%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d-%B-%Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y%m%d",
)

# Date-shaped substrings to pull out of a longer cell like "Fund Holdings as of ...".
_DATE_PATTERNS = (
    r"\d{4}-\d{2}-\d{2}",
    r"\d{1,2}[/-][A-Za-z]{3,9}[/-]\d{2,4}",
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}",
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}",
    r"\d{1,2}/\d{1,2}/\d{4}",
)


def _parse_loose_date(value: Any) -> date | None:
    """Parse a date cell across the many issuer formats; ``None`` if not a date."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # ISO with a time component / trailing text.
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    # Pull a date-shaped token out of a longer string and retry.
    for pattern in _DATE_PATTERNS:
        match = re.search(pattern, s)
        if match:
            token = match.group(0)
            for fmt in _DATE_FORMATS:
                try:
                    return datetime.strptime(token, fmt).date()
                except ValueError:
                    continue
    return None


# --- canonical column mapping ------------------------------------------------
#
# Header cells vary across issuers/jurisdictions, so we map a normalised header to
# a canonical field. Exact matches first (handles ambiguous names like Vanguard's
# "Holding name" vs "Holdings"), then ordered substring rules (handles suffixes
# like "Market Value (GBP)" / "% of Net Assets").

_EXACT_HEADERS: dict[str, str] = {
    "ticker": "ticker",
    "tickersymbol": "ticker",
    "symbol": "ticker",
    "name": "name",
    "securityname": "name",
    "securitydescription": "name",
    "securitydesc": "name",
    "description": "name",
    "holdingname": "name",
    "issuername": "name",
    "instrumentname": "name",
    "stockdescription": "name",
    "isin": "isin",
    "isincode": "isin",
    "sedol": "sedol",
    "sedolcode": "sedol",
    "cusip": "cusip",
    "figi": "figi",
    "sector": "sector",
    "gicssector": "sector",
    "industry": "industry",
    "gicsindustry": "industry",
    "assetclass": "asset_class",
    "securitytype": "security_type",
    "type": "security_type",
    "assettype": "security_type",
    "method": "method",
    "country": "country",
    "countryofrisk": "country",
    "domicile": "country",
    "geography": "country",
    "location": "country",
    "currency": "currency",
    "marketcurrency": "currency",
    "localcurrency": "currency",
    "tradingcurrency": "currency",
    "ccy": "currency",
    "shares": "shares",
    "sharespar": "shares",
    "quantity": "shares",
    "quantityheld": "shares",
    "nominal": "shares",
    "holdings": "shares",
    "parvalue": "shares",
    "units": "shares",
    "price": "price",
    "marketprice": "price",
    "localprice": "price",
    "coupon": "coupon",
    "couponrate": "coupon",
    "maturity": "maturity_date",
    "maturitydate": "maturity_date",
    "strikeprice": "strike_price",
    "strike": "strike_price",
    "exchange": "exchange",
    "exchangename": "exchange",
    "weight": "weight",
    "weighting": "weight",
    "asofdate": "as_of",
    "asof": "as_of",
    "positiondate": "as_of",
    "effectivedate": "as_of",
}


def _canonical_field(norm: str) -> str | None:
    """Map a normalised header cell to a canonical field name (or None)."""
    if not norm:
        return None
    exact = _EXACT_HEADERS.get(norm)
    if exact is not None:
        return exact
    # Percent/weight families must be checked before "marketvalue" (since
    # "% of Market Value" normalises to contain "marketvalue").
    if "ofnetasset" in norm:
        return "weight_net"
    if "ofmarketvalue" in norm:
        return "weight_mv"
    if "ofportfolio" in norm:
        return "weight_portfolio"
    if "offund" in norm:
        return "weight_fund"
    if "weight" in norm:
        return "weight"
    if "notional" in norm:
        return "notional_value"
    if "marketvalue" in norm:
        return "market_value"
    if "isin" in norm:
        return "isin"
    if "sedol" in norm:
        return "sedol"
    if "cusip" in norm:
        return "cusip"
    if "maturity" in norm:
        return "maturity_date"
    if "coupon" in norm:
        return "coupon"
    if "shares" in norm or "sharepar" in norm:
        return "shares"
    if "currency" in norm:
        return "currency"
    if "country" in norm:
        return "country"
    if "sector" in norm:
        return "sector"
    if "industry" in norm:
        return "industry"
    if "ticker" in norm:
        return "ticker"
    return None


# Weight-family columns in preference order: an explicit weight, else % of net
# assets, else % of fund, else % of portfolio, else % of market value.
_WEIGHT_FIELDS = ("weight", "weight_net", "weight_fund", "weight_portfolio", "weight_mv")
# Extra parsed fields with no dedicated canonical column — preserved in raw_payload.
_EXTRA_FIELDS = (
    "asset_class",
    "security_type",
    "method",
    "price",
    "coupon",
    "maturity_date",
    "strike_price",
    "notional_value",
    "exchange",
)


def _resolve_weight(canon: dict[str, str]) -> Decimal | None:
    for field in _WEIGHT_FIELDS:
        if field in canon:
            weight = _percent_to_weight(canon[field])
            if weight is not None:
                return weight
    return None


def _build_record(
    canon: dict[str, str], *, source: str, status: str | None, as_of: date
) -> HoldingRecord | None:
    """Build one canonical holding from a mapped row, or None to skip a bad row."""
    name = _clean_text(canon.get("name"), maxlen=_MAXLEN["name"])
    if not name:
        return None
    weight = _resolve_weight(canon)
    if weight is None:
        return None
    raw: dict[str, Any] = {"provider": source}
    raw.update({k: v for k, v in canon.items() if v not in (None, "")})
    for extra in _EXTRA_FIELDS:
        value = _clean_text(canon.get(extra))
        if value is not None:
            raw[extra] = value
    return HoldingRecord(
        as_of_date=as_of,
        holding_name=name,
        weight=weight,
        source=source,
        holding_ticker=_clean_text(canon.get("ticker"), maxlen=_MAXLEN["ticker"], upper=True),
        holding_isin=_clean_text(canon.get("isin"), maxlen=_MAXLEN["isin"], upper=True),
        holding_sedol=_clean_text(canon.get("sedol"), maxlen=_MAXLEN["sedol"], upper=True),
        holding_cusip=_clean_text(canon.get("cusip"), maxlen=_MAXLEN["cusip"], upper=True),
        holding_figi=_clean_text(canon.get("figi"), maxlen=_MAXLEN["figi"], upper=True),
        country=_clean_text(canon.get("country"), maxlen=_MAXLEN["country"]),
        sector=_clean_text(canon.get("sector"), maxlen=_MAXLEN["sector"]),
        industry=_clean_text(canon.get("industry"), maxlen=_MAXLEN["industry"]),
        currency=_clean_text(canon.get("currency"), maxlen=_MAXLEN["currency"], upper=True),
        market_value=_clean_money(canon.get("market_value")),
        shares=_clean_money(canon.get("shares")),
        status=status,
        raw_payload=raw,
    )


def _find_preamble_as_of(preamble_rows: list[list[str]]) -> date | None:
    """Scan metadata rows for an ``as of`` disclosure date (iShares preamble)."""
    for row in preamble_rows:
        joined = " ".join(c for c in row if c)
        if "as of" not in joined.lower():
            continue
        for cell in row:
            parsed = _parse_loose_date(cell)
            if parsed is not None:
                return parsed
        parsed = _parse_loose_date(joined)
        if parsed is not None:
            return parsed
    return None


def _rows_to_records(
    rows: list[list[str]], *, source: str, status: str | None, default_as_of: date
) -> list[HoldingRecord]:
    """Generic tabular -> canonical holdings: scan for the header, then map rows.

    Robust to metadata/preamble rows before the table and to disclaimer rows after
    it (rows that don't map to a name + weight are skipped). A per-row exception is
    isolated (skipped), never failing the whole parse.
    """
    header_idx: int | None = None
    header_fields: list[str | None] = []
    for i, row in enumerate(rows):
        fields = [_canonical_field(_norm_header(c)) for c in row]
        present = [f for f in fields if f]
        has_name = "name" in present
        has_weight = any(f and f.startswith("weight") for f in present)
        if has_name and has_weight and len(present) >= 3:
            header_idx = i
            header_fields = fields
            break
    if header_idx is None:
        return []

    as_of = _find_preamble_as_of(rows[:header_idx]) or default_as_of
    records: list[HoldingRecord] = []
    for row in rows[header_idx + 1 :]:
        try:
            canon: dict[str, str] = {}
            for field, cell in zip(header_fields, row, strict=False):
                if field and cell not in (None, ""):
                    canon.setdefault(field, cell)
            if not canon:
                continue
            row_as_of = _parse_loose_date(canon["as_of"]) if canon.get("as_of") else None
            record = _build_record(canon, source=source, status=status, as_of=row_as_of or as_of)
            if record is not None:
                records.append(record)
        except Exception:  # noqa: BLE001 - isolate one bad row; skip + continue
            continue
    return records


# --- HTML table extraction (for JPM Excel-as-HTML payloads) -------------------


class _TableExtractor(HTMLParser):
    """Collect ``<tr>/<td|th>`` cell text into rows (no third-party HTML dep)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:512].lower()
    if head.startswith(("<!doctype", "<html", "<table", "<?xml")):
        return True
    probe = text[:8192].lower()
    return "<table" in probe or "<tr" in probe


def _rows_from_tabular(text: str, *, content_type: str | None = None) -> list[list[str]]:
    """Content-sniff a JPM payload into rows: HTML table, else CSV/TSV delimited."""
    ct = (content_type or "").lower()
    if "html" in ct or "xml" in ct or _looks_like_html(text):
        extractor = _TableExtractor()
        extractor.feed(text)
        if extractor.rows:
            return extractor.rows
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    try:
        return list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        # A garbled text payload is a clean no-op, not a crash. One bad file never
        # fails the worker; it yields zero rows.
        return []


def _rows_from_payload(payload: str | bytes, *, content_type: str | None = None) -> list[list[str]]:
    """Content-sniff a holdings payload (``str`` text or raw ``bytes``) into rows.

    Bytes: an OOXML ``.xlsx`` workbook (ZIP) is parsed with the stdlib; an old binary
    ``.xls`` (OLE2) / PDF / other binary is a clean no-op (deferred — no pandas / no
    binary-Excel dependency, see AGENTS.md); decodable text bytes fall through to the
    text path. Str: the existing CSV / TSV / HTML-table sniffing.
    """
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
        fmt = spreadsheet.sniff_format(data)
        if fmt == spreadsheet.XLSX:
            return spreadsheet.rows_from_xlsx(data)
        if fmt in spreadsheet.UNSUPPORTED_BINARY:
            return []  # binary .xls/PDF deferred -> clean no-op (caller keeps candidate)
        payload = data.decode("utf-8", errors="replace")
    return _rows_from_tabular(payload, content_type=content_type)


# --- pure parsers (offline; testable without network) ------------------------


def parse_ishares_holdings_csv(
    text: str, *, source: str = "blackrock_ishares_holdings", status: str | None = "official"
) -> list[HoldingRecord]:
    """Parse an iShares/BlackRock holdings CSV into normalised holdings.

    Handles the metadata/preamble rows BlackRock prepends (the real holdings
    header is not the first row): scans for the holdings header by column names,
    reads the ``Fund Holdings as of`` disclosure date from the preamble, parses
    weights (``Weight (%)`` percent -> fraction) and identifiers, and isolates
    bad rows. Pure + offline.
    """
    rows = list(csv.reader(io.StringIO(text)))
    return _rows_to_records(rows, source=source, status=status, default_as_of=date.today())


def parse_jpmorgan_holdings(
    text: str | bytes,
    *,
    source: str = "jpmorgan_etf_holdings",
    status: str | None = "official",
    content_type: str | None = None,
) -> list[HoldingRecord]:
    """Parse a J.P. Morgan ETF holdings export into normalised holdings.

    Content-sniffs the ``FundsMarketingHandler`` payload (CSV / TSV / HTML table, or
    a binary OOXML ``.xlsx`` workbook via the stdlib) into rows, then maps columns
    (``% of Net Assets`` preferred over ``% of Market Value`` for the weight;
    Shares/Par, Country, Currency, Sector, Industry and any ISIN/CUSIP/SEDOL). The
    legacy binary ``.xls`` (OLE2) is **not** decoded here (returns empty — no pandas /
    no binary-Excel dependency; see docs/data_sources.md). Pure + offline.
    """
    rows = _rows_from_payload(text, content_type=content_type)
    return _rows_to_records(rows, source=source, status=status, default_as_of=date.today())


def parse_vanguard_holdings_csv(
    text: str, *, source: str = "vanguard_holdings_export", status: str | None = "official_export"
) -> list[HoldingRecord]:
    """Parse a manually exported official Vanguard holdings CSV into holdings.

    Same header-scan/normalisation as the iShares parser (Vanguard exports also
    carry a preamble and ``% of fund`` weights). Pure + offline — this backs the
    ``vanguard_holdings_export`` manual/exported-file source; live ``vanguard_holdings``
    stays planned.
    """
    rows = list(csv.reader(io.StringIO(text)))
    return _rows_to_records(rows, source=source, status=status, default_as_of=date.today())


# --- fixture provider --------------------------------------------------------

# Keyed by fund ISIN. Mirrors the seeded funds so the worker has something
# authoritative-but-offline to ingest. Each row is a pipe-delimited
# "name|ticker|isin|sedol|country|sector|industry|currency|weight"; identifiers
# are real-looking and weights are an illustrative top-holdings subset that sums
# to a known partial fraction (NOT 100%), spanning mixed sectors/countries/
# currencies. Placeholder values — realistic in shape, not guaranteed current.
_COLUMNS: tuple[str, ...] = (
    "holding_name",
    "holding_ticker",
    "holding_isin",
    "holding_sedol",
    "country",
    "sector",
    "industry",
    "currency",
)


def _row(line: str) -> dict[str, Any]:
    *fields, weight = line.split("|")
    record: dict[str, Any] = dict(zip(_COLUMNS, fields, strict=True))
    record["weight"] = Decimal(weight)
    record["status"] = "current"
    return record


_FIXTURES: dict[str, list[str]] = {
    # VUSA — Vanguard S&P 500 UCITS ETF: US large-cap equity top holdings (USD).
    "IE00B3XXRP09": [
        "Apple Inc|AAPL|US0378331005|2046251|US|Technology|Hardware|USD|0.07100000",
        "Microsoft Corp|MSFT|US5949181045|2588173|US|Technology|Software|USD|0.06600000",
        "NVIDIA Corp|NVDA|US67066G1040|2379504|US|Technology|Semiconductors|USD|0.06100000",
        "Amazon.com Inc|AMZN|US0231351067|2000019|US|Consumer Discretionary|Retail|USD|0.03600000",
        "Meta Platforms|META|US30303M1027|B7TL820|US|Communication Services|Media|USD|0.02500000",
        "Alphabet Inc A|GOOGL|US02079K3059|BYVY8G0|US|Communication Services|Media|USD|0.02100000",
        "Alphabet Inc C|GOOG|US02079K1079|BYY88Y7|US|Communication Services|Media|USD|0.01900000",
        "Broadcom Inc|AVGO|US11135F1012|BDZ78H9|US|Technology|Semiconductors|USD|0.01800000",
        "Berkshire Hathaway B|BRK.B|US0846707026|2073390|US|Financials|Diversified|USD|0.01700000",
        "JPMorgan Chase & Co|JPM|US46625H1005|2190385|US|Financials|Banks|USD|0.01400000",
    ],
    # ISF — iShares Core FTSE 100 UCITS ETF: UK large-cap equity top holdings (GBP).
    "IE0005042456": [
        "AstraZeneca PLC|AZN|GB0009895292|0989529|GB|Health Care|Pharmaceuticals|GBP|0.08100000",
        "Shell PLC|SHEL|GB00BP6MXD84|BP6MXD8|GB|Energy|Oil & Gas|GBP|0.07600000",
        "HSBC Holdings PLC|HSBA|GB0005405286|0540528|GB|Financials|Banks|GBP|0.07000000",
        "Unilever PLC|ULVR|GB00B10RZP78|B10RZP7|GB|Consumer Staples|Household|GBP|0.05000000",
        "BP PLC|BP|GB0007980591|0798059|GB|Energy|Oil & Gas|GBP|0.03900000",
        "GSK PLC|GSK|GB00BN7SWP63|BN7SWP6|GB|Health Care|Pharmaceuticals|GBP|0.03500000",
        "Rio Tinto PLC|RIO|GB0007188757|0718875|GB|Materials|Metals & Mining|GBP|0.03100000",
        "Diageo PLC|DGE|GB0002374006|0237400|GB|Consumer Staples|Beverages|GBP|0.02800000",
        "BAE Systems PLC|BA|GB0002634946|0263494|GB|Industrials|Aerospace & Defense|GBP|0.02600000",
        "RELX PLC|REL|GB00B2B0DG97|B2B0DG9|GB|Industrials|Business Services|GBP|0.02500000",
    ],
    # JPMorgan Global Equity Premium Income: global equity top holdings (mixed
    # countries/sectors/currencies; covered-call overlay fund).
    "IE0003UVYC20": [
        "Microsoft Corp|MSFT|US5949181045|2588173|US|Technology|Software|USD|0.01900000",
        "Apple Inc|AAPL|US0378331005|2046251|US|Technology|Hardware|USD|0.01800000",
        "Amazon.com Inc|AMZN|US0231351067|2000019|US|Consumer Discretionary|Retail|USD|0.01500000",
        "ASML Holding NV|ASML|NL0010273215|B929F46|NL|Technology|Semiconductors|EUR|0.01400000",
        "Nestle SA|NESN|CH0038863350|7123870|CH|Consumer Staples|Food|CHF|0.01300000",
        "Novo Nordisk B|NOVO-B|DK0062498333|BPVPQH0|DK|Health Care|Pharmaceuticals|DKK|0.01300000",
        "Mastercard Inc A|MA|US57636Q1040|B121557|US|Financials|Payments|USD|0.01200000",
        "Progressive Corp|PGR|US7433151039|2683732|US|Financials|Insurance|USD|0.01100000",
        "Trane Technologies|TT|IE00BK9ZQ967|BK9ZQ96|IE|Industrials|Machinery|USD|0.01000000",
        "Unilever PLC|ULVR|GB00B10RZP78|B10RZP7|GB|Consumer Staples|Household|GBP|0.01000000",
    ],
}


class StaticHoldingsSource:
    """Offline holdings provider backed by a fixture table (keyed by ISIN)."""

    name = "holdings_fixture"
    is_fixture = True
    requires_live_fetch = False

    async def fetch(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,  # offline fixture ignores it
        url: str | None = None,  # offline fixture ignores it
    ) -> list[HoldingRecord]:
        rows = _FIXTURES.get(isin, [])
        as_of = fixture_as_of()
        return [HoldingRecord(as_of_date=as_of, source=self.name, **_row(line)) for line in rows]


# --- known issuer holdings download URLs -------------------------------------
#
# The per-fund verified/candidate download URLs live in the shared
# ``issuer_source_config`` registry (keyed by ISIN + source, with a source_status);
# these thin wrappers keep the holdings-specific call sites stable. The live
# adapters use an explicit ``url`` override first, then a *usable* (verified/
# candidate) known config; without either they are a clean no-op.


def known_holdings_url(isin: str | None, source: str) -> str | None:
    """A verified/candidate issuer download URL for this ISIN + source, if usable."""
    return issuer_source_config.known_source_url(isin, source)


def has_known_holdings_url(isin: str | None) -> bool:
    """Whether any usable live holdings config is registered for this ISIN."""
    return issuer_source_config.has_source_config(
        isin, issuer_source_config.DATA_TYPE_HOLDINGS, usable_only=True
    )


def known_holdings_source(isin: str | None) -> str | None:
    """The usable live holdings source configured for this ISIN, if any (planner hint)."""
    return issuer_source_config.known_source_name(isin, issuer_source_config.DATA_TYPE_HOLDINGS)


# --- live issuer adapters (guarded; explicit-only) ---------------------------

# A host/path *class* for the fetch log — never a tokenised/full URL (no secrets).
_ISHARES_ENDPOINT_LABEL = "blackrock.com/.../products/.../ajax"
_JPM_ENDPOINT_LABEL = "am.jpmorgan.com/FundsMarketingHandler/excel"


class _LiveIssuerHoldingsSource:
    """Shared guarded-fetch plumbing for live issuer holdings downloads.

    Explicit-only: the configured holdings default stays the offline fixture, so
    this never fires on the scheduler unless its ``--source`` is named. The
    download goes through ``guarded_fetch`` (recent-success cache -> source budget
    -> fetch log -> fetch); a budget block / cache hit / fetch error yields an
    empty list (a clean no-op), never a surprise retry. Collection only — it
    returns published holdings; it never builds look-through/PnL/analytics.
    """

    name: str = ""
    is_fixture = False
    requires_live_fetch = True
    request_kind: str = ""
    endpoint_label: str = ""
    # Conservative identifying request headers (no cookies / no fingerprint spoofing).
    request_headers: dict[str, str] | None = None

    async def _download(self, url: str) -> str | bytes:
        """Fetch the issuer file; return ``str`` for text, ``bytes`` for a binary body.

        Content-sniffed (content-type + magic bytes) so a CSV/HTML export stays text
        while a binary workbook (``.xlsx``/``.xls``) is preserved byte-exact for the
        parser to decode/defer — text decoding would otherwise corrupt a binary body.
        """
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=self.request_headers or None)
            response.raise_for_status()
            content = response.content
            if spreadsheet.is_binary_response(response.headers.get("content-type"), content):
                return content
            return response.text

    def _parse(self, payload: str | bytes) -> list[HoldingRecord]:
        raise NotImplementedError

    async def fetch_payload(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,
        url: str | None = None,
    ) -> str | bytes | None:
        """Run ONE guarded download and return the raw payload (or None for a no-op).

        Returns ``None`` for a missing URL / budget block / cache hit / fetch error
        (all clean no-ops). The raw payload (text or binary workbook bytes) lets the
        verify helper inspect the payload format without a second fetch.
        """
        if session is None:  # the live path needs the budget/fetch-log session
            raise RuntimeError(
                f"{self.name} requires a database session "
                "(run it via the issuer_holdings_ingestion worker, not directly)."
            )
        download_url = url or known_holdings_url(isin, self.name)
        if not download_url:
            return None  # no configured URL for this fund => clean no-op

        from app.core.config import get_settings
        from app.services import source_budget, source_requests

        ttl_seconds = get_settings().request_cache_ttl_seconds
        # Safe request key: source + isin only (no full tokenised URL persisted).
        params = {"isin": isin.strip().upper()}
        try:
            fetch_result, payload = await source_budget.guarded_fetch(
                session,
                source=self.name,
                request_kind=self.request_kind,
                params=params,
                endpoint_label=self.endpoint_label,
                method="GET",
                ttl_seconds=ttl_seconds,
                fetch=lambda u=download_url: self._download(u),
            )
        except Exception:  # noqa: BLE001 - guarded_fetch logged it; treat as no-op
            return None
        # Budget-blocked or served from the recent-success cache => no payload.
        if fetch_result.status == source_requests.RATE_LIMITED or payload is None:
            return None
        return payload

    async def fetch(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,
        url: str | None = None,
    ) -> list[HoldingRecord]:
        payload = await self.fetch_payload(isin=isin, session=session, url=url)
        if payload is None:
            return []
        return self._parse(payload)


class IsharesHoldingsSource(_LiveIssuerHoldingsSource):
    """Live iShares/BlackRock holdings adapter (issuer CSV download, guarded)."""

    name = "blackrock_ishares_holdings"
    request_kind = "fetch_ishares_holdings"
    endpoint_label = _ISHARES_ENDPOINT_LABEL
    description = (
        "iShares/BlackRock issuer-hosted holdings CSV download. Explicit-only "
        "(holdings default stays the offline fixture); needs a configured/known URL."
    )

    def _parse(self, payload: str | bytes) -> list[HoldingRecord]:
        if isinstance(payload, (bytes, bytearray)):  # CSV endpoint, but stay defensive
            payload = bytes(payload).decode("utf-8", errors="replace")
        return parse_ishares_holdings_csv(payload, source=self.name)


class JPMorganHoldingsSource(_LiveIssuerHoldingsSource):
    """Live J.P. Morgan ETF holdings adapter (FundsMarketingHandler, guarded)."""

    name = "jpmorgan_etf_holdings"
    request_kind = "fetch_jpmorgan_holdings"
    endpoint_label = _JPM_ENDPOINT_LABEL
    description = (
        "J.P. Morgan AM daily ETF holdings export (CSV/TSV/HTML-table or OOXML .xlsx; "
        "legacy binary .xls deferred). Explicit-only; needs a configured/known URL."
    )

    def _parse(self, payload: str | bytes) -> list[HoldingRecord]:
        return parse_jpmorgan_holdings(payload, source=self.name)


# --- Vanguard exported-file parser (offline/manual) + planned live -----------

# A small illustrative exported-file sample (Vanguard UK product page export
# shape) so ``vanguard_holdings_export`` is demonstrable offline. Placeholder
# values — realistic in shape, NOT guaranteed current. Status is "fixture" for the
# bundled sample; a real exported file parsed via ``--url`` is "official_export".
_VANGUARD_EXPORT_SAMPLES: dict[str, str] = {
    # VUSA — Vanguard S&P 500 UCITS ETF USD Distributing.
    "IE00B3XXRP09": (
        "Vanguard S&P 500 UCITS ETF\n"
        "Holdings as of,30/04/2026\n"
        "\n"
        "Holding name,Ticker,SEDOL,ISIN,Sector,Country,Currency,Shares,Market value,% of fund\n"
        'Apple Inc,AAPL,2046251,US0378331005,Technology,US,USD,"1,234,567","250,000,000",7.10\n'
        'Microsoft Corp,MSFT,2588173,US5949181045,Technology,US,USD,"987,654","232,000,000",6.60\n'
        'NVIDIA Corp,NVDA,2379504,US67066G1040,Technology,US,USD,"2,345,678","214,000,000",6.10\n'
        'Amazon Inc,AMZN,2000019,US0231351067,Consumer Disc,US,USD,"654,321","126,000,000",3.60\n'
        "\n"
        "The information contained herein is for general guidance only.\n"
    ),
}


class VanguardExportHoldingsSource:
    """Offline parser for a manually exported official Vanguard holdings file.

    NOT a live adapter: it parses a local exported CSV path passed as ``url``
    (status ``official_export``), or — for offline demo/tests — a small bundled
    sample keyed by ISIN (status ``fixture``). The live ``vanguard_holdings``
    adapter stays planned until a stable official machine-readable endpoint is
    verified; we never scrape Vanguard's brittle HTML as a canonical source.
    """

    name = "vanguard_holdings_export"
    is_fixture = False  # parses real exported files; bundled sample is illustrative
    requires_live_fetch = False

    async def fetch(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,  # offline; no budget/fetch-log needed
        url: str | None = None,
    ) -> list[HoldingRecord]:
        if url:
            # Treat ``url`` as a local exported-file path (file:// or plain path).
            path = url[len("file://") :] if url.startswith("file://") else url
            try:
                text = Path(path).read_text(encoding="utf-8-sig")
            except OSError:
                return []  # unreadable export => clean no-op
            return parse_vanguard_holdings_csv(text, source=self.name, status="official_export")
        sample = _VANGUARD_EXPORT_SAMPLES.get(isin.strip().upper())
        if sample is None:
            return []
        return parse_vanguard_holdings_csv(sample, source=self.name, status="fixture")


class _PlannedHoldingsSource:
    """A named-but-unimplemented live holdings adapter.

    Recognised so ``--source vanguard_holdings`` resolves to a clear, offline
    failure (recorded as a clean failed job_run) instead of a crash or a surprise
    live call. Wiring it means fetching an official machine-readable Vanguard
    file/API through ``guarded_fetch`` (budget + fetch log + cache + timeout),
    storing no secrets and never scraping a brittle page — see docs/data_sources.md.
    """

    is_fixture = False
    requires_live_fetch = True

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    async def fetch(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,
        url: str | None = None,
    ) -> list[HoldingRecord]:
        raise NotImplementedError(
            f"{self.name} live adapter is planned, not implemented. Use "
            "'vanguard_holdings_export' (offline exported-file parser) instead."
        )


# --- registry ----------------------------------------------------------------

_FIXTURE = StaticHoldingsSource()
_ISHARES = IsharesHoldingsSource()
_JPMORGAN = JPMorganHoldingsSource()
_VANGUARD_EXPORT = VanguardExportHoldingsSource()
_PLANNED: dict[str, _PlannedHoldingsSource] = {
    "vanguard_holdings": _PlannedHoldingsSource(
        "vanguard_holdings",
        "Vanguard live holdings (planned: no stable official machine-readable "
        "endpoint verified; use vanguard_holdings_export for now).",
    ),
}

_SOURCES: dict[str, HoldingsSource] = {
    _FIXTURE.name: _FIXTURE,
    _ISHARES.name: _ISHARES,
    _JPMORGAN.name: _JPMORGAN,
    _VANGUARD_EXPORT.name: _VANGUARD_EXPORT,
    **_PLANNED,
}


def get_holdings_source(name: str | None = None) -> HoldingsSource:
    from app.core.config import get_settings

    source_name = name or get_settings().holdings_source_default
    source = _SOURCES.get(source_name)
    if source is None:
        raise ValueError(f"Unknown holdings source: {source_name!r}")
    return source


def list_holdings_sources() -> list[str]:
    """Names of all registered holdings sources (fixture + live + planned)."""
    return list(_SOURCES)
