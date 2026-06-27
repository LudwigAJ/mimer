"""Distribution sources, isolated behind a small protocol + registry.

A *distribution* (a.k.a. dividend / income payment) is declared by the fund's
issuer: ex-date, record date, payment date, per-share amount and currency. They
belong to the fund (not a listing) — see the identity rules in AGENTS.md.

This module ships a robust offline **fixture** provider plus live/exported issuer
distribution adapters that fetch issuer-published distribution files through
``guarded_fetch`` (source budget + fetch log + request cache + timeout), store no
secrets and never scrape brittle HTML as a canonical source:

* **J.P. Morgan Asset Management** (``jpmorgan_distributions``) — the fund
  distribution export from ``FundsMarketingHandler`` (``type=fundDistribution`` /
  ``compositionOfFundDistribution``), content-sniffed (CSV / TSV / HTML-table, or
  an OOXML ``.xlsx`` workbook via the stdlib; legacy binary ``.xls`` deferred);
* **Vanguard exported file** (``vanguard_distributions_export``) — an OFFLINE parser
  for a manually exported official Vanguard distribution file (JSON / JSONP / CSV);
* **Vanguard product-data API** (``vanguard_distributions``) — the official Vanguard
  product-data JSON/JSONP dataset's ``distributionHistory`` (explicit-only, guarded).

The live issuer adapters are **explicit-only**: the configured default stays the
offline fixture (``distribution_fixture``), so the worker/scheduler never makes a
surprise live call. They take an explicit download ``url``; without one they are a
clean no-op (empty), never an error.

``blackrock_ishares_distributions`` stays *planned* behind the same protocol: no
clean official iShares distribution download/endpoint has been verified (the
holdings ``...ajax`` pattern is per-fund and does NOT imply a distribution feed),
so a ``--source blackrock_ishares_distributions`` invocation fails cleanly instead
of guessing a URL. See docs/data_sources.md for the exact follow-up.

COMPUTE BOUNDARY (see AGENTS.md): this layer only *fetches, parses and normalises*
published distributions into the canonical row model. NO dividend forecasting,
yield projection, tax treatment, total return or PnL here or anywhere in the
backend — those belong in the Rust GUI / local pricer. One bad row is isolated
(skipped), never failing the whole file.

Note: this module is a *source adapter* (provider-specific fetch/parse). The
provider-agnostic upsert / provenance / job_runs logic lives in
``app/services/distributions_ingestion.py``.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from app.sources import issuer_source_config, spreadsheet

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_AMOUNT_Q = Decimal("0.00000001")  # 8 dp, matches Distribution.amount Numeric(24, 8)

# Column max lengths (mirror the Distribution model) so an over-long provider cell
# truncates rather than failing the whole row at upsert time.
_MAXLEN: dict[str, int] = {
    "currency": 8,
    "distribution_type": 32,
    "frequency": 32,
    "share_class": 64,
    "status": 32,
}


@dataclass(frozen=True)
class DistributionRecord:
    """A normalized declared distribution for a fund (keyed by ISIN)."""

    ex_date: date
    amount: Decimal
    currency: str
    source: str
    record_date: date | None = None
    payment_date: date | None = None
    # The issuer's labelled distribution date when distinct from ex/record/pay.
    distribution_date: date | None = None
    # income | dividend | capital_gain | return_of_capital | ... (issuer-asserted).
    distribution_type: str | None = None
    # monthly | quarterly | semi_annual | annual | ... (issuer-asserted).
    frequency: str | None = None
    share_class: str | None = None
    # paid | declared | announced | estimated | ... (provider-asserted).
    status: str | None = None
    # Reserved for provenance/debugging (raw provider payload + parsed extras).
    raw_payload: dict[str, Any] | None = None


class DistributionSource(Protocol):
    name: str

    async def fetch(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,
        url: str | None = None,
    ) -> list[DistributionRecord]:
        """Return declared distributions for a fund ISIN (possibly empty).

        Offline fixtures ignore ``session``/``url``. Live issuer adapters need the
        ``session`` (for budget/fetch-log/cache) the ingestion service passes, and
        take an explicit ``url`` download override; without a URL they return an
        empty list (a clean no-op).
        """
        ...


# --- shared value cleaning ---------------------------------------------------

# Provider placeholders that mean "no value".
_BLANKS = {"", "-", "--", "---", "n/a", "na", "null", "none", "—", "nil"}

# Currency symbol -> ISO code (only the unambiguous ones).
_SYMBOL_CCY = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}
# Currency codes we recognise in a free-text cell / header suffix.
_KNOWN_CCY = {
    "USD",
    "GBP",
    "GBX",
    "EUR",
    "JPY",
    "CHF",
    "CAD",
    "AUD",
    "SEK",
    "NOK",
    "DKK",
    "HKD",
    "SGD",
    "NZD",
}


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


def _clean_amount(value: Any) -> Decimal | None:
    """Parse a messy money cell (commas / currency symbols / parentheses) safely."""
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _BLANKS:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    # Strip thousands separators, currency symbols, codes and whitespace.
    s = re.sub(r"[,\s$£€¥]", "", s)
    s = re.sub(r"(?i)[a-z]+", "", s)  # drop a trailing/leading currency code like "USD"
    if s in {"", "-", "+", "."}:
        return None
    try:
        result = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    result = -result if negative else result
    return result.quantize(_AMOUNT_Q)


def _detect_currency(*candidates: Any) -> str | None:
    """First recognisable currency from explicit cells / symbols / embedded codes."""
    for candidate in candidates:
        if candidate is None:
            continue
        s = str(candidate).strip()
        if not s:
            continue
        upper = s.upper()
        if upper in _KNOWN_CCY:
            return upper
        for symbol, code in _SYMBOL_CCY.items():
            if symbol in s:
                return code
        match = re.search(r"\b([A-Z]{3})\b", upper)
        if match and match.group(1) in _KNOWN_CCY:
            return match.group(1)
    return None


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
    if not s or s.lower() in _BLANKS:
        return None
    # Epoch-millis / epoch-seconds (some product-data JSON feeds use these).
    if re.fullmatch(r"\d{10,13}", s):
        try:
            ts = int(s)
            seconds = ts / 1000 if len(s) == 13 else ts
            return datetime.fromtimestamp(seconds, tz=UTC).date()
        except (ValueError, OSError, OverflowError):
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
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
# Header/key cells vary across issuers/jurisdictions, so we map a normalised header
# to a canonical field. Exact matches first (handles ambiguous names), then ordered
# substring rules (handles suffixes like "Distribution Amount (USD)").

_EXACT_HEADERS: dict[str, str] = {
    "exdate": "ex_date",
    "exdividenddate": "ex_date",
    "exdistributiondate": "ex_date",
    "exdivdate": "ex_date",
    "recorddate": "record_date",
    "recorddt": "record_date",
    "paymentdate": "payment_date",
    "paydate": "payment_date",
    "paiddate": "payment_date",
    "paymentdt": "payment_date",
    "payabledate": "payment_date",
    "payable": "payment_date",
    "datepayable": "payment_date",
    "distributiondate": "distribution_date",
    "distdate": "distribution_date",
    "declarationdate": "distribution_date",
    "declareddate": "distribution_date",
    "amount": "amount",
    "distributionamount": "amount",
    "dividendamount": "amount",
    "amountpershare": "amount",
    "distributionpershare": "amount",
    "dividendpershare": "amount",
    "cashamount": "amount",
    "grossamount": "amount",
    "netamount": "amount",
    "rate": "amount",
    "ratepershare": "amount",
    "distribution": "amount",
    "dividend": "amount",
    "income": "amount",
    "value": "amount",
    "currency": "currency",
    "currencycode": "currency",
    "ccy": "currency",
    "distributiontype": "distribution_type",
    "dividendtype": "distribution_type",
    "incometype": "distribution_type",
    "paymenttype": "distribution_type",
    "type": "distribution_type",
    "frequency": "frequency",
    "distributionfrequency": "frequency",
    "payfrequency": "frequency",
    "shareclass": "share_class",
    "unitclass": "share_class",
    "class": "share_class",
}


def _canonical_dist_field(norm: str) -> str | None:
    """Map a normalised header/key cell to a canonical distribution field."""
    if not norm:
        return None
    exact = _EXACT_HEADERS.get(norm)
    if exact is not None:
        return exact
    # Dates before amounts (an "exdividend..." contains "dividend").
    if "exdate" in norm or "exdiv" in norm or "exdistribution" in norm:
        return "ex_date"
    if "recorddate" in norm:
        return "record_date"
    if "paymentdate" in norm or "paydate" in norm or "paiddate" in norm:
        return "payment_date"
    if "distributiondate" in norm or "declarat" in norm or "declared" in norm:
        return "distribution_date"
    if "pershare" in norm or "amount" in norm:
        return "amount"
    if "currency" in norm:
        return "currency"
    if "frequency" in norm:
        return "frequency"
    if "shareclass" in norm or "unitclass" in norm:
        return "share_class"
    if "type" in norm:
        return "distribution_type"
    return None


_HEADER_CCY_RE = re.compile(r"\(([A-Za-z]{3})\)")


def _header_currency(header_cells: list[str], fields: list[str | None]) -> str | None:
    """A currency code embedded in the amount column header, e.g. ``Amount (USD)``."""
    for cell, field in zip(header_cells, fields, strict=False):
        if field != "amount":
            continue
        match = _HEADER_CCY_RE.search(cell or "")
        if match and match.group(1).upper() in _KNOWN_CCY:
            return match.group(1).upper()
    return None


def _build_distribution(
    canon: dict[str, str],
    *,
    source: str,
    status: str | None,
    header_currency: str | None,
) -> DistributionRecord | None:
    """Build one canonical distribution from a mapped row, or None to skip it.

    Amount + currency are core (a row missing either is skipped). The identity date
    is the ex-date, falling back to the distribution/payment/record date so the
    event is still keyable; a row with no parseable date at all is skipped. The
    original issuer dates are preserved on the record + in ``raw_payload``.
    """
    amount = _clean_amount(canon.get("amount"))
    if amount is None:
        return None
    currency = (
        _clean_text(canon.get("currency"), maxlen=_MAXLEN["currency"], upper=True)
        or header_currency
        or _detect_currency(canon.get("amount"))
    )
    if not currency:
        return None

    ex_date = _parse_loose_date(canon.get("ex_date"))
    record_date = _parse_loose_date(canon.get("record_date"))
    payment_date = _parse_loose_date(canon.get("payment_date"))
    distribution_date = _parse_loose_date(canon.get("distribution_date"))
    key_date = ex_date or distribution_date or payment_date or record_date
    if key_date is None:
        return None

    raw: dict[str, Any] = {"provider": source}
    raw.update({k: v for k, v in canon.items() if v not in (None, "")})

    return DistributionRecord(
        ex_date=key_date,
        amount=amount,
        currency=currency,
        source=source,
        record_date=record_date,
        payment_date=payment_date,
        distribution_date=distribution_date,
        distribution_type=_clean_text(
            canon.get("distribution_type"), maxlen=_MAXLEN["distribution_type"]
        ),
        frequency=_clean_text(canon.get("frequency"), maxlen=_MAXLEN["frequency"]),
        share_class=_clean_text(canon.get("share_class"), maxlen=_MAXLEN["share_class"]),
        status=status,
        raw_payload=raw,
    )


def _rows_to_distributions(
    rows: list[list[str]], *, source: str, status: str | None
) -> list[DistributionRecord]:
    """Generic tabular -> canonical distributions: scan for the header, then map.

    Robust to metadata/preamble rows before the table and to disclaimer rows after
    it (rows that don't map to an amount + currency/date are skipped). A per-row
    exception is isolated (skipped), never failing the whole parse.
    """
    header_idx: int | None = None
    header_fields: list[str | None] = []
    header_cells: list[str] = []
    for i, row in enumerate(rows):
        fields = [_canonical_dist_field(_norm_header(c)) for c in row]
        present = [f for f in fields if f]
        has_amount = "amount" in present
        has_date = any(f and f.endswith("_date") for f in present)
        if has_amount and has_date:
            header_idx = i
            header_fields = fields
            header_cells = list(row)
            break
    if header_idx is None:
        return []

    header_ccy = _header_currency(header_cells, header_fields)
    records: list[DistributionRecord] = []
    for row in rows[header_idx + 1 :]:
        try:
            canon: dict[str, str] = {}
            for field, cell in zip(header_fields, row, strict=False):
                if field and cell not in (None, ""):
                    canon.setdefault(field, cell)
            if not canon:
                continue
            record = _build_distribution(
                canon, source=source, status=status, header_currency=header_ccy
            )
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


def _looks_like_json(text: str) -> bool:
    head = text.lstrip()[:1]
    return head in ("{", "[")


def _rows_from_tabular(text: str, *, content_type: str | None = None) -> list[list[str]]:
    """Content-sniff a tabular payload into rows: HTML table, else CSV/TSV delimited."""
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
        # A garbled text payload is a clean no-op, not a crash.
        return []


def _rows_from_payload(payload: str | bytes, *, content_type: str | None = None) -> list[list[str]]:
    """Content-sniff a distribution payload (``str`` text or raw ``bytes``) into rows.

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


def parse_jpmorgan_distributions(
    text: str | bytes,
    *,
    source: str = "jpmorgan_distributions",
    status: str | None = "paid",
    content_type: str | None = None,
) -> list[DistributionRecord]:
    """Parse a J.P. Morgan fund distribution export into normalised distributions.

    Content-sniffs the ``FundsMarketingHandler`` payload (CSV / TSV / HTML table, or
    a binary OOXML ``.xlsx`` workbook via the stdlib) into rows, scans for the
    distribution header (Ex-Date / Record Date / Payment Date / Amount / Currency /
    Distribution Type / Frequency / Share Class) and maps each row. The legacy binary
    ``.xls`` (OLE2) is **not** decoded here (returns empty — no pandas / no
    binary-Excel dependency; see docs/data_sources.md). Pure + offline.
    """
    rows = _rows_from_payload(text, content_type=content_type)
    return _rows_to_distributions(rows, source=source, status=status)


# Keys whose value is the list of distribution rows in a Vanguard product dataset.
_JSON_LIST_KEYS = re.compile(r"distribution", re.IGNORECASE)


def _strip_jsonp(text: str) -> str:
    """Remove a JSONP callback wrapper, returning the inner JSON text.

    Handles ``callback({...})``, ``angular.callbacks._0([...])`` and a trailing
    ``;``. If there is no wrapper the text is returned unchanged.
    """
    s = text.strip()
    if _looks_like_json(s):
        return s
    open_idx = min(
        (i for i in (s.find("{"), s.find("[")) if i != -1),
        default=-1,
    )
    if open_idx == -1:
        return s
    close = "}" if s[open_idx] == "{" else "]"
    close_idx = s.rfind(close)
    if close_idx <= open_idx:
        return s
    return s[open_idx : close_idx + 1]


def _find_distribution_rows(payload: Any) -> list[dict[str, Any]]:
    """Locate the distribution-history list anywhere in a parsed JSON document.

    Walks the structure for a list of objects under a key whose name mentions
    ``distribution`` (e.g. ``distributionHistory`` / ``distributions``). Falls back
    to the first list-of-objects that looks distribution-shaped (has a date-ish and
    an amount-ish key). Conservative + provider-agnostic.
    """
    best: list[dict[str, Any]] = []

    def _looks_distributiony(rows: list[Any]) -> bool:
        if not rows or not isinstance(rows[0], dict):
            return False
        keys = {_norm_header(str(k)) for k in rows[0]}
        fields = {_canonical_dist_field(k) for k in keys}
        return "amount" in fields and any(f and f.endswith("_date") for f in fields)

    def _walk(node: Any, key_hint: str | None) -> None:
        nonlocal best
        if isinstance(node, list):
            if (
                key_hint
                and _JSON_LIST_KEYS.search(key_hint)
                and node
                and isinstance(node[0], dict)
                and not best
            ):
                best = [row for row in node if isinstance(row, dict)]
            elif not best and _looks_distributiony(node):
                best = [row for row in node if isinstance(row, dict)]
            for item in node:
                _walk(item, key_hint)
        elif isinstance(node, dict):
            for k, v in node.items():
                _walk(v, str(k))

    _walk(payload, None)
    return best


def parse_vanguard_distributions_json(
    text: str, *, source: str = "vanguard_distributions", status: str | None = "paid"
) -> list[DistributionRecord]:
    """Parse a Vanguard product-data JSON/JSONP distributionHistory payload.

    Strips a JSONP callback wrapper if present, locates the distribution-history
    list (provider-agnostic key search) and maps each row's keys with the shared
    canonical field mapper (ex/record/payment/distribution dates, amount, currency,
    type, frequency, share class). Amount + currency are core; a bad row is isolated.
    Pure + offline.
    """
    try:
        payload = json.loads(_strip_jsonp(text))
    except (json.JSONDecodeError, ValueError):
        return []
    rows = _find_distribution_rows(payload)
    records: list[DistributionRecord] = []
    for row in rows:
        try:
            canon: dict[str, str] = {}
            for key, value in row.items():
                field = _canonical_dist_field(_norm_header(str(key)))
                if field and value not in (None, ""):
                    canon.setdefault(field, str(value))
            if not canon:
                continue
            record = _build_distribution(canon, source=source, status=status, header_currency=None)
            if record is not None:
                records.append(record)
        except Exception:  # noqa: BLE001 - isolate one bad row; skip + continue
            continue
    return records


def parse_vanguard_distributions_csv(
    text: str,
    *,
    source: str = "vanguard_distributions_export",
    status: str | None = "official_export",
) -> list[DistributionRecord]:
    """Parse a manually exported official Vanguard distribution CSV into records.

    Same header-scan/normalisation as the JPM parser. Pure + offline — backs the
    ``vanguard_distributions_export`` manual/exported-file source for CSV exports.
    """
    rows = list(csv.reader(io.StringIO(text)))
    return _rows_to_distributions(rows, source=source, status=status)


def parse_vanguard_distributions(
    text: str,
    *,
    source: str = "vanguard_distributions_export",
    status: str | None = "official_export",
) -> list[DistributionRecord]:
    """Dispatch a Vanguard distribution payload by shape: JSON/JSONP, else CSV."""
    probe = _strip_jsonp(text)
    if _looks_like_json(probe):
        return parse_vanguard_distributions_json(text, source=source, status=status)
    return parse_vanguard_distributions_csv(text, source=source, status=status)


# --- fixture provider --------------------------------------------------------
#
# Keyed by ISIN. Mirrors the seeded distributing funds so the worker has something
# authoritative-but-offline to ingest. Amounts/dates are illustrative (placeholder,
# not guaranteed current) but realistic in shape; each fund gets a quarterly-ish (or
# monthly) cadence with ex/record/payment dates, a type/frequency and a paid status.
_D = Decimal


def _q(
    ex: str,
    record: str,
    pay: str,
    amount: str,
    currency: str,
    *,
    frequency: str,
    dist_type: str = "income",
) -> dict[str, Any]:
    return {
        "ex_date": date.fromisoformat(ex),
        "record_date": date.fromisoformat(record),
        "payment_date": date.fromisoformat(pay),
        "amount": _D(amount).quantize(_AMOUNT_Q),
        "currency": currency,
        "distribution_type": dist_type,
        "frequency": frequency,
        "status": "paid",
    }


_FIXTURES: dict[str, list[dict[str, Any]]] = {
    # VUSA — Vanguard S&P 500 UCITS ETF (USD distributions, quarterly).
    "IE00B3XXRP09": [
        _q("2025-03-20", "2025-03-21", "2025-04-10", "0.3100", "USD", frequency="quarterly"),
        _q("2025-06-19", "2025-06-20", "2025-07-10", "0.3250", "USD", frequency="quarterly"),
        _q("2025-09-18", "2025-09-19", "2025-10-09", "0.3300", "USD", frequency="quarterly"),
        _q("2025-12-18", "2025-12-19", "2026-01-08", "0.3400", "USD", frequency="quarterly"),
    ],
    # ISF — iShares Core FTSE 100 UCITS ETF (GBP distributions, quarterly).
    "IE0005042456": [
        _q("2025-02-27", "2025-02-28", "2025-03-31", "0.0800", "GBP", frequency="quarterly"),
        _q("2025-05-29", "2025-05-30", "2025-06-30", "0.0850", "GBP", frequency="quarterly"),
        _q("2025-08-28", "2025-08-29", "2025-09-30", "0.0780", "GBP", frequency="quarterly"),
        _q("2025-11-27", "2025-11-28", "2025-12-31", "0.0900", "GBP", frequency="quarterly"),
    ],
    # JEPG/JEGP — JPMorgan Global Equity Premium Income (monthly USD income).
    "IE0003UVYC20": [
        _q("2026-01-02", "2026-01-03", "2026-01-15", "0.3500", "USD", frequency="monthly"),
        _q("2026-02-02", "2026-02-03", "2026-02-15", "0.3450", "USD", frequency="monthly"),
        _q("2026-03-02", "2026-03-03", "2026-03-15", "0.3600", "USD", frequency="monthly"),
        _q("2026-04-02", "2026-04-03", "2026-04-15", "0.3550", "USD", frequency="monthly"),
        _q("2026-05-02", "2026-05-03", "2026-05-15", "0.3700", "USD", frequency="monthly"),
    ],
}


class StaticDistributionSource:
    """Offline distribution provider backed by a fixture table (keyed by ISIN)."""

    name = "distribution_fixture"
    is_fixture = True
    requires_live_fetch = False

    async def fetch(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,  # offline fixture ignores it
        url: str | None = None,  # offline fixture ignores it
    ) -> list[DistributionRecord]:
        rows = _FIXTURES.get(isin, [])
        return [DistributionRecord(source=self.name, **row) for row in rows]


# --- live issuer adapters (guarded; explicit-only) ---------------------------

# A host/path *class* for the fetch log — never a tokenised/full URL (no secrets).
_JPM_ENDPOINT_LABEL = "am.jpmorgan.com/FundsMarketingHandler/excel"
_VANGUARD_ENDPOINT_LABEL = "api.vanguard.com/rs/gre/gra/.../datasets"

# Conservative, identifying headers for an unattended official-data client. NO
# cookies, NO browser/TLS fingerprint spoofing (see AGENTS.md) — just an honest UA +
# Accept so a polite official JSON endpoint is happy to answer a research client.
_VANGUARD_REQUEST_HEADERS = {
    "User-Agent": "Mimer-Research/0.1 (Mimer ETF data service; unattended official-data client)",
    "Accept": "application/json,text/javascript,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
}


class _LiveIssuerDistributionsSource:
    """Shared guarded-fetch plumbing for live issuer distribution downloads.

    Explicit-only: the configured distribution default stays the offline fixture, so
    this never fires on the scheduler unless its ``--source`` is named. The download
    goes through ``guarded_fetch`` (recent-success cache -> source budget -> fetch log
    -> fetch); a budget block / cache hit / fetch error yields an empty list (a clean
    no-op), never a surprise retry. Collection only — it returns published
    distributions; it never forecasts dividends or projects yield.
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

        Content-sniffed (content-type + magic bytes) so a JSON/JSONP/CSV export stays
        text while a binary workbook (``.xlsx``/``.xls``) is preserved byte-exact for
        the parser to decode/defer — text decoding would otherwise corrupt a binary body.
        """
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=self.request_headers or None)
            response.raise_for_status()
            content = response.content
            if spreadsheet.is_binary_response(response.headers.get("content-type"), content):
                return content
            return response.text

    def _parse(self, payload: str | bytes) -> list[DistributionRecord]:
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
                "(run it via the distribution_ingestion worker, not directly)."
            )
        download_url = url or known_distribution_url(isin, self.name)
        if not download_url:
            return None  # no --url and no usable known config => clean no-op

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
    ) -> list[DistributionRecord]:
        payload = await self.fetch_payload(isin=isin, session=session, url=url)
        if payload is None:
            return []
        return self._parse(payload)


class JPMorganDistributionsSource(_LiveIssuerDistributionsSource):
    """Live J.P. Morgan distribution adapter (FundsMarketingHandler, guarded)."""

    name = "jpmorgan_distributions"
    request_kind = "fetch_jpmorgan_distributions"
    endpoint_label = _JPM_ENDPOINT_LABEL
    description = (
        "J.P. Morgan AM fund distribution export (FundsMarketingHandler "
        "?type=fundDistribution; CSV/TSV/HTML-table or OOXML .xlsx, legacy binary .xls "
        "deferred). Explicit-only; needs a configured download URL (--url)."
    )

    def _parse(self, payload: str | bytes) -> list[DistributionRecord]:
        return parse_jpmorgan_distributions(payload, source=self.name)


class VanguardDistributionsSource(_LiveIssuerDistributionsSource):
    """Live Vanguard distribution adapter (product-data JSON/JSONP, guarded)."""

    name = "vanguard_distributions"
    request_kind = "fetch_vanguard_distributions"
    endpoint_label = _VANGUARD_ENDPOINT_LABEL
    # Conservative official headers give the product-data API its best honest chance
    # (no cookies / no TLS fingerprint spoofing — the prior failure was at the TLS
    # handshake, a transport-layer rejection headers alone may not resolve).
    request_headers = _VANGUARD_REQUEST_HEADERS
    description = (
        "Vanguard product-data dataset distributionHistory (official JSON/JSONP "
        "product-data API). Explicit-only; needs a configured product-data URL "
        "(--url, e.g. urd-product-port-specific.json?vars=portId:<id>,issueType:F)."
    )

    def _parse(self, payload: str | bytes) -> list[DistributionRecord]:
        if isinstance(payload, (bytes, bytearray)):  # JSON endpoint, but stay defensive
            payload = bytes(payload).decode("utf-8", errors="replace")
        return parse_vanguard_distributions_json(payload, source=self.name)


# --- Vanguard exported-file parser (offline/manual) --------------------------

# A small illustrative exported-file sample (Vanguard product-data distributionHistory
# JSON shape) so ``vanguard_distributions_export`` is demonstrable offline. Placeholder
# values — realistic in shape, NOT guaranteed current. Status is "fixture" for the
# bundled sample; a real exported file parsed via ``--url`` is "official_export".
_VANGUARD_EXPORT_SAMPLES: dict[str, str] = {
    # VUSA — Vanguard S&P 500 UCITS ETF USD Distributing.
    "IE00B3XXRP09": json.dumps(
        {
            "fundData": {
                "portId": "9503",
                "distributionHistory": [
                    {
                        "exDividendDate": "2025-03-20",
                        "recordDate": "2025-03-21",
                        "payableDate": "2025-04-10",
                        "distributionAmount": "0.3100",
                        "currency": "USD",
                        "distributionType": "income",
                        "frequency": "Quarterly",
                    },
                    {
                        "exDividendDate": "2025-06-19",
                        "recordDate": "2025-06-20",
                        "payableDate": "2025-07-10",
                        "distributionAmount": "0.3250",
                        "currency": "USD",
                        "distributionType": "income",
                        "frequency": "Quarterly",
                    },
                ],
            }
        }
    ),
}


class VanguardDistributionsExportSource:
    """Offline parser for a manually exported official Vanguard distribution file.

    NOT a live adapter: it parses a local exported JSON/JSONP/CSV path passed as
    ``url`` (status ``official_export``), or — for offline demo/tests — a small
    bundled JSON sample keyed by ISIN (status ``fixture``). We never scrape
    Vanguard's brittle HTML as a canonical source; the live ``vanguard_distributions``
    adapter fetches the official product-data JSON only with an explicit URL.
    """

    name = "vanguard_distributions_export"
    is_fixture = False  # parses real exported files; bundled sample is illustrative
    requires_live_fetch = False

    async def fetch(
        self,
        *,
        isin: str,
        session: AsyncSession | None = None,  # offline; no budget/fetch-log needed
        url: str | None = None,
    ) -> list[DistributionRecord]:
        if url:
            # Treat ``url`` as a local exported-file path (file:// or plain path).
            path = url[len("file://") :] if url.startswith("file://") else url
            try:
                text = Path(path).read_text(encoding="utf-8-sig")
            except OSError:
                return []  # unreadable export => clean no-op
            return parse_vanguard_distributions(text, source=self.name, status="official_export")
        sample = _VANGUARD_EXPORT_SAMPLES.get(isin.strip().upper())
        if sample is None:
            return []
        return parse_vanguard_distributions(sample, source=self.name, status="fixture")


class _PlannedDistributionsSource:
    """A named-but-unimplemented live distribution adapter.

    Recognised so ``--source blackrock_ishares_distributions`` resolves to a clear,
    offline failure (recorded as a clean failed job_run) instead of a crash or a
    surprise live call. Wiring it means fetching an official machine-readable iShares
    distribution file/endpoint through ``guarded_fetch`` (budget + fetch log + cache +
    timeout), storing no secrets and never scraping a brittle page — and crucially NOT
    guessing a URL from the holdings ``...ajax`` pattern. See docs/data_sources.md.
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
    ) -> list[DistributionRecord]:
        raise NotImplementedError(
            f"{self.name} live adapter is planned, not implemented. No clean official "
            "iShares distribution endpoint has been verified; use 'jpmorgan_distributions' "
            "or 'vanguard_distributions(_export)', or the offline 'distribution_fixture'."
        )


# --- registry ----------------------------------------------------------------

_FIXTURE = StaticDistributionSource()
_JPMORGAN = JPMorganDistributionsSource()
_VANGUARD = VanguardDistributionsSource()
_VANGUARD_EXPORT = VanguardDistributionsExportSource()
_PLANNED: dict[str, _PlannedDistributionsSource] = {
    "blackrock_ishares_distributions": _PlannedDistributionsSource(
        "blackrock_ishares_distributions",
        "iShares/BlackRock live distributions (planned: no clean official "
        "machine-readable distribution endpoint verified; do NOT guess the holdings "
        "ajax URL pattern). See docs/data_sources.md.",
    ),
}

_SOURCES: dict[str, DistributionSource] = {
    _FIXTURE.name: _FIXTURE,
    _JPMORGAN.name: _JPMORGAN,
    _VANGUARD.name: _VANGUARD,
    _VANGUARD_EXPORT.name: _VANGUARD_EXPORT,
    **_PLANNED,
}


def get_distribution_source(name: str | None = None) -> DistributionSource:
    from app.core.config import get_settings

    source_name = name or get_settings().distribution_source_default
    source = _SOURCES.get(source_name)
    if source is None:
        raise ValueError(f"Unknown distribution source: {source_name!r}")
    return source


def list_distribution_sources() -> list[str]:
    """Names of all registered distribution sources (fixture + live + planned)."""
    return list(_SOURCES)


# --- known live distribution sources (per-fund config; planner hint) ----------
#
# The per-fund verified/candidate distribution download URLs live in the shared
# ``issuer_source_config`` registry (keyed by ISIN + source, with a source_status).
# Unlike the holdings registry, distribution endpoints (JPM ?type=fundDistribution,
# Vanguard product-data) must each be verified per product before being trusted as
# canonical, so any config seeded here is ``candidate`` until a live fetch+parse
# confirms it — and we NEVER guess a distribution URL from a holdings URL. A live
# adapter uses an explicit ``--url`` first, then a *usable* (verified/candidate)
# known config; without either it is a clean no-op.


def known_distribution_source(isin: str | None) -> str | None:
    """The usable live distribution source configured for this ISIN (planner hint)."""
    return issuer_source_config.known_source_name(
        isin, issuer_source_config.DATA_TYPE_DISTRIBUTIONS
    )


def known_distribution_url(isin: str | None, source: str) -> str | None:
    """A verified/candidate issuer distribution URL for this ISIN + source, if usable."""
    return issuer_source_config.known_source_url(isin, source)
