"""Broker CSV import parsing — pure, deterministic, no DB / no network.

This is the *source adapter* half of broker import (see AGENTS.md two-layer
split): it parses one broker export format into normalized
``ParsedTransaction`` dataclasses with per-row provenance and parse outcomes. It
must NOT touch the database, job bookkeeping, or instrument resolution — that is
the provider-agnostic ingestion service's job
(``app/services/broker_imports.py``).

The first format is ``generic_csv_v1``: a forgiving generic CSV with a small set
of column aliases. It is deliberately conservative — it does not try to be every
real broker. A bad row (bad date / decimal / missing required field / unknown
type) is isolated and flagged, never crashing the whole import.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# --- canonical transaction-type vocabulary -----------------------------------

BUY = "buy"
SELL = "sell"
DIVIDEND = "dividend"
CASH_DEPOSIT = "cash_deposit"
CASH_WITHDRAWAL = "cash_withdrawal"
FEE = "fee"
TAX = "tax"
FX = "fx"
INTEREST = "interest"
UNKNOWN = "unknown"

TRADE_TYPES = frozenset({BUY, SELL})
# Cash movements affect a currency balance but not an instrument position.
CASH_MOVEMENT_TYPES = frozenset({DIVIDEND, CASH_DEPOSIT, CASH_WITHDRAWAL, FEE, TAX, INTEREST})

# Aliases -> canonical transaction type (matched on lower-cased, stripped value).
_TYPE_ALIASES: dict[str, str] = {
    "buy": BUY,
    "b": BUY,
    "bought": BUY,
    "purchase": BUY,
    "buy to open": BUY,
    "sell": SELL,
    "s": SELL,
    "sold": SELL,
    "sale": SELL,
    "sell to close": SELL,
    "dividend": DIVIDEND,
    "div": DIVIDEND,
    "dividends": DIVIDEND,
    "cash_deposit": CASH_DEPOSIT,
    "cash deposit": CASH_DEPOSIT,
    "deposit": CASH_DEPOSIT,
    "transfer in": CASH_DEPOSIT,
    "funds received": CASH_DEPOSIT,
    "cash_withdrawal": CASH_WITHDRAWAL,
    "cash withdrawal": CASH_WITHDRAWAL,
    "withdrawal": CASH_WITHDRAWAL,
    "transfer out": CASH_WITHDRAWAL,
    "fee": FEE,
    "commission": FEE,
    "charge": FEE,
    "tax": TAX,
    "withholding": TAX,
    "withholding tax": TAX,
    "interest": INTEREST,
    "fx": FX,
    "forex": FX,
    "currency exchange": FX,
}

# Header aliases -> canonical column name (matched on lower-cased, stripped header).
_COLUMN_ALIASES: dict[str, str] = {
    "date": "date",
    "trade date": "date",
    "trade_date": "date",
    "settle date": "settle_date",
    "settlement date": "settle_date",
    "settle_date": "settle_date",
    "type": "type",
    "transaction type": "type",
    "activity": "type",
    "action": "type",
    "symbol": "symbol",
    "ticker": "symbol",
    "isin": "isin",
    "figi": "figi",
    "name": "name",
    "security name": "name",
    "description": "name",
    "quantity": "quantity",
    "qty": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "price": "price",
    "unit price": "price",
    "gross_amount": "gross_amount",
    "gross amount": "gross_amount",
    "gross": "gross_amount",
    "amount": "gross_amount",
    "fees": "fees",
    "fee": "fees",
    "commission": "fees",
    "taxes": "taxes",
    "tax": "taxes",
    "net_amount": "net_amount",
    "net amount": "net_amount",
    "net": "net_amount",
    "total": "net_amount",
    "currency": "currency",
    "ccy": "currency",
    "curr": "currency",
    "cash_currency": "cash_currency",
    "cash currency": "cash_currency",
    "cash ccy": "cash_currency",
    "fx_rate": "fx_rate",
    "fx rate": "fx_rate",
    "rate": "fx_rate",
    "broker_account": "broker_account",
    "broker account": "broker_account",
    "account": "broker_account",
    "notes": "notes",
    "memo": "notes",
    "comment": "notes",
}

# Accepted date formats (deterministic; ISO first). DD/MM ordering is assumed for
# slash/dash European inputs — documented in docs/data_sources.md.
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y")

# Parse-status vocabulary for a row.
PARSED = "parsed"
WARNING = "warning"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class ParsedTransaction:
    """A normalized transaction extracted from one CSV row (no identity yet)."""

    transaction_type: str
    trade_date: date
    currency: str
    settle_date: date | None = None
    symbol: str | None = None
    isin: str | None = None
    figi: str | None = None
    name: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    gross_amount: Decimal | None = None
    fees: Decimal | None = None
    taxes: Decimal | None = None
    net_amount: Decimal | None = None
    cash_currency: str | None = None
    fx_rate: Decimal | None = None
    broker_account: str | None = None
    notes: str | None = None

    @property
    def is_cash_movement(self) -> bool:
        return self.transaction_type in CASH_MOVEMENT_TYPES

    @property
    def is_trade(self) -> bool:
        return self.transaction_type in TRADE_TYPES


@dataclass
class ParsedBrokerRow:
    """One CSV row + its parse outcome (provenance for ``broker_import_rows``)."""

    row_number: int
    raw: dict[str, str]
    parse_status: str
    parse_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    transaction: ParsedTransaction | None = None


@dataclass
class BrokerImportParseResult:
    """The full, deterministic parse of one CSV payload."""

    parser_name: str
    rows: list[ParsedBrokerRow] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def parsed_rows(self) -> list[ParsedBrokerRow]:
        return [r for r in self.rows if r.transaction is not None]

    @property
    def parsed_count(self) -> int:
        return len(self.parsed_rows)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.rows if r.parse_status == FAILED)

    @property
    def warning_count(self) -> int:
        return sum(1 for r in self.rows if r.parse_status == WARNING)

    @property
    def cash_movement_count(self) -> int:
        return sum(
            1 for r in self.rows if r.transaction is not None and r.transaction.is_cash_movement
        )


class BrokerImportError(Exception):
    """A whole-payload failure (e.g. no header / empty file) — distinct from a
    per-row parse failure, which is isolated and counted, not raised."""


# --- helpers (pure) ----------------------------------------------------------


def _clean(value: str | None) -> str:
    return value.strip() if value else ""


def _parse_decimal(value: str | None) -> Decimal | None:
    """Parse a money/quantity cell, or None if blank. Raises on a bad number."""
    text = _clean(value)
    if not text:
        return None
    # Strip common currency symbols, thousands separators and whitespace.
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    for symbol in ("$", "£", "€", ",", " "):
        text = text.replace(symbol, "")
    if not text:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    return -result if negative else result


def _parse_date(value: str | None) -> date:
    text = _clean(value)
    if not text:
        raise ValueError("missing date")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r}")


def normalise_transaction_type(value: str | None) -> str:
    """Map a raw type cell to the canonical vocabulary (``unknown`` if unmapped)."""
    return _TYPE_ALIASES.get(_clean(value).lower(), UNKNOWN)


def compute_source_hash(parser_name: str, csv_text: str) -> str:
    """Deterministic digest of (parser, content) — the import idempotency key."""
    digest = hashlib.sha256()
    digest.update(parser_name.encode("utf-8"))
    digest.update(b"\n")
    digest.update(csv_text.encode("utf-8"))
    return digest.hexdigest()


# --- generic_csv_v1 parser ---------------------------------------------------


class GenericCsvV1Parser:
    """A forgiving generic broker CSV parser (see module docstring)."""

    name = "generic_csv_v1"

    def parse(self, csv_text: str) -> BrokerImportParseResult:
        result = BrokerImportParseResult(parser_name=self.name)
        reader = csv.DictReader(io.StringIO(csv_text))
        if reader.fieldnames is None:
            raise BrokerImportError("CSV has no header row.")
        # Map raw headers -> canonical column names (unknown headers preserved raw).
        header_map = {h: _COLUMN_ALIASES.get((h or "").strip().lower()) for h in reader.fieldnames}
        if "type" not in header_map.values() or "date" not in header_map.values():
            raise BrokerImportError(
                "CSV must contain at least 'date' and 'type' columns (aliases allowed)."
            )

        for index, raw_row in enumerate(reader, start=1):
            result.rows.append(self._parse_row(index, raw_row, header_map))
        return result

    def _parse_row(
        self, row_number: int, raw_row: dict[str, str], header_map: dict[str, str | None]
    ) -> ParsedBrokerRow:
        raw = {(k or ""): (v or "") for k, v in raw_row.items()}
        # Canonical view of the row (only mapped columns).
        cols: dict[str, str] = {}
        for header, canonical in header_map.items():
            if canonical is not None:
                cols[canonical] = raw_row.get(header) or ""

        # A fully blank line is skipped (not an error).
        if not any(_clean(v) for v in cols.values()):
            return ParsedBrokerRow(row_number, raw, SKIPPED, parse_error="blank row")

        warnings: list[str] = []
        try:
            trade_date = _parse_date(cols.get("date"))
            settle_date = self._optional_date(cols.get("settle_date"), warnings)
            quantity = _parse_decimal(cols.get("quantity"))
            price = _parse_decimal(cols.get("price"))
            gross_amount = _parse_decimal(cols.get("gross_amount"))
            fees = _parse_decimal(cols.get("fees"))
            taxes = _parse_decimal(cols.get("taxes"))
            net_amount = _parse_decimal(cols.get("net_amount"))
            fx_rate = _parse_decimal(cols.get("fx_rate"))
        except ValueError as exc:
            return ParsedBrokerRow(row_number, raw, FAILED, parse_error=str(exc))

        currency = _clean(cols.get("currency")).upper()
        if not currency:
            return ParsedBrokerRow(row_number, raw, FAILED, parse_error="missing currency")

        ttype = normalise_transaction_type(cols.get("type"))
        if ttype == UNKNOWN:
            warnings.append(f"unknown transaction type {cols.get('type')!r}")

        # Type-specific required fields (isolate, never crash).
        if ttype in TRADE_TYPES and quantity is None:
            return ParsedBrokerRow(
                row_number, raw, FAILED, parse_error=f"{ttype} row missing quantity"
            )
        if ttype in CASH_MOVEMENT_TYPES and net_amount is None and gross_amount is None:
            return ParsedBrokerRow(
                row_number, raw, FAILED, parse_error=f"{ttype} row missing an amount"
            )

        transaction = ParsedTransaction(
            transaction_type=ttype,
            trade_date=trade_date,
            settle_date=settle_date,
            currency=currency,
            symbol=_clean(cols.get("symbol")).upper() or None,
            isin=_clean(cols.get("isin")).upper() or None,
            figi=_clean(cols.get("figi")).upper() or None,
            name=_clean(cols.get("name")) or None,
            quantity=quantity,
            price=price,
            gross_amount=gross_amount,
            fees=fees,
            taxes=taxes,
            net_amount=net_amount,
            cash_currency=_clean(cols.get("cash_currency")).upper() or None,
            fx_rate=fx_rate,
            broker_account=_clean(cols.get("broker_account")) or None,
            notes=_clean(cols.get("notes")) or None,
        )
        status = WARNING if warnings else PARSED
        return ParsedBrokerRow(row_number, raw, status, warnings=warnings, transaction=transaction)

    @staticmethod
    def _optional_date(value: str | None, warnings: list[str]) -> date | None:
        if not _clean(value):
            return None
        try:
            return _parse_date(value)
        except ValueError:
            warnings.append(f"ignored unparseable settle date {value!r}")
            return None


# --- registry ----------------------------------------------------------------

_PARSERS: dict[str, GenericCsvV1Parser] = {GenericCsvV1Parser.name: GenericCsvV1Parser()}

DEFAULT_PARSER = GenericCsvV1Parser.name


def get_broker_parser(name: str | None) -> GenericCsvV1Parser:
    """Return the parser for ``name`` (default ``generic_csv_v1``)."""
    parser = _PARSERS.get(name or DEFAULT_PARSER)
    if parser is None:
        raise BrokerImportError(f"Unknown broker import format: {name!r}")
    return parser


def available_parsers() -> list[str]:
    return sorted(_PARSERS)


# --- bundled offline fixture sample (for the worker / docs / demos) ----------
# Mirrors the seeded funds (VUSA, ISF) so resolution-by-ISIN/ticker works offline,
# plus a dividend, a cash deposit, an unresolved direct equity, and a bad row to
# exercise the full preview/commit funnel without any network.
SAMPLE_GENERIC_CSV_V1 = (
    "date,settle_date,type,symbol,isin,name,quantity,price,gross_amount,fees,"
    "taxes,net_amount,currency,fx_rate,broker_account,notes\n"
    "2026-06-01,2026-06-03,buy,VUSA,IE00B3XXRP09,Vanguard S&P 500 UCITS ETF,10,80.50,"
    "805.00,1.00,0,-806.00,GBP,,ISA,initial buy\n"
    "2026-06-02,2026-06-04,buy,ISF,IE0005042456,iShares Core FTSE 100 UCITS ETF,50,8.50,"
    "425.00,1.00,0,-426.00,GBP,,ISA,\n"
    "2026-06-10,,dividend,VUSA,IE00B3XXRP09,Vanguard S&P 500 UCITS ETF,,,3.00,0,0.45,"
    "2.55,GBP,,ISA,quarterly distribution\n"
    "2026-06-12,,cash_deposit,,,,,,500.00,0,0,500.00,GBP,,ISA,top up\n"
    "2026-06-15,2026-06-17,buy,TSLA,US88160R1014,Tesla Inc,5,210.00,1050.00,1.00,0,"
    "-1051.00,USD,1.27,GIA,direct equity (unresolved)\n"
    "2026-06-20,,sell,VUSA,IE00B3XXRP09,Vanguard S&P 500 UCITS ETF,4,82.00,328.00,1.00,"
    "0,327.00,GBP,,ISA,trim\n"
)
