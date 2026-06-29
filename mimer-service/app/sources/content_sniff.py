"""Byte/content sniffing + secrets-free fetch-metadata for issuer downloads.

Issuer download endpoints lie about their payloads: a route named ``/excel`` may
answer with a real OOXML ``.xlsx``, a legacy binary ``.xls`` (OLE2), a ``%PDF``, an
HTML error page, or plain CSV depending on the params and the day (see the JPMorgan
``FundsMarketingHandler`` notes in docs/data_sources.md). So we never trust the URL,
the filename or the ``Content-Type`` — we classify by the leading **magic bytes** and
record what we actually got.

This module is the small, pure, stdlib-only home for two things:

1. :func:`byte_signature` — a descriptive label for the leading bytes of a payload
   (``xlsx_ooxml_zip`` / ``ole2_legacy_xls`` / ``pdf`` / ``json`` / ``html`` /
   ``csv`` / ``tsv`` / ``text`` / ``binary`` / ``empty``). It is finer-grained than
   :func:`app.sources.spreadsheet.sniff_format` (which only needs the binary/text
   split for the row extractor) and is what a fetch-metadata record stores.
2. :class:`FetchDescriptor` + :func:`describe_fetch` — a **secrets-free** record of
   one issuer fetch: provider / source / fund / a safe ``endpoint_label`` (host/path
   class, NEVER a tokenised URL) / http_status / content_type / content_disposition /
   filename / byte_signature / sha256 / parser_used / row_count / parse_status /
   known_blocker / fetched_at. Nothing here ever holds a cookie, an API key or a
   tokenised URL (see AGENTS.md).

It is a pure leaf (no DB / no network / no other source-adapter imports beyond the
sibling ``spreadsheet`` magic-byte constants), so adapters and services import it
freely.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.sources import spreadsheet

# --- byte-signature labels ---------------------------------------------------
# Descriptive (what the bytes ARE), recorded verbatim on a fetch-metadata row.

SIG_XLSX = "xlsx_ooxml_zip"  # PK\x03\x04 — OOXML workbook (ZIP container)
SIG_OLE2 = "ole2_legacy_xls"  # D0 CF 11 E0 A1 B1 — legacy binary .xls / .doc
SIG_PDF = "pdf"  # %PDF
SIG_JSON = "json"  # leading { or [
SIG_HTML = "html"  # <!DOCTYPE / <html / <div / <table
SIG_XML = "xml"  # <?xml ... (not HTML)
SIG_CSV = "csv"  # decodable text, comma-dominant
SIG_TSV = "tsv"  # decodable text, tab-dominant
SIG_TEXT = "text"  # decodable text, no clear delimiter
SIG_BINARY = "binary"  # some other binary blob (NUL bytes in the head)
SIG_EMPTY = "empty"  # zero-length payload

# Signatures that are NOT a clean machine-readable tabular/structured source the
# stdlib decodes into rows (a caller keeps the source candidate with this blocker).
UNSUPPORTED_SIGNATURES = (SIG_OLE2, SIG_PDF, SIG_BINARY)

# Map the descriptive signature back to the coarse spreadsheet format (so callers
# that already branch on spreadsheet.XLSX/UNSUPPORTED_BINARY stay consistent).
_SIGNATURE_TO_FORMAT = {
    SIG_XLSX: spreadsheet.XLSX,
    SIG_OLE2: spreadsheet.XLS,
    SIG_PDF: spreadsheet.PDF,
    SIG_BINARY: spreadsheet.BINARY,
    SIG_EMPTY: spreadsheet.EMPTY,
}

# --- parse-status vocabulary -------------------------------------------------

PARSE_OK = "ok"  # parsed >= 1 usable row
PARSE_ZERO_ROWS = "zero_rows"  # decoded, but no usable rows
PARSE_BINARY_UNSUPPORTED = "binary_unsupported"  # a binary workbook/PDF we don't decode
PARSE_HTML_ERROR = "html_error"  # an HTML page where a data file was expected
PARSE_NOT_ATTEMPTED = "not_attempted"  # no parse run (e.g. budget block / cache hit)
PARSE_ERROR = "error"  # parser raised / payload unusable


_HTML_HEADS = ("<!doctype", "<html", "<div", "<table", "<body", "<span", "<p ")


def byte_signature(payload: str | bytes | bytearray | None) -> str:
    """Classify a fetched payload by its leading bytes (descriptive label).

    A ``str`` payload is text by construction (real binary arrives as ``bytes`` from
    a content-aware downloader); only ``bytes`` are inspected for magic numbers. The
    text branch distinguishes JSON / HTML / XML / CSV / TSV / plain text so a
    fetch-metadata row records exactly what came back (an HTML error page named
    ``...xlsx`` must never look like a spreadsheet).
    """
    if payload is None:
        return SIG_EMPTY
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
        if not data:
            return SIG_EMPTY
        if data[:4] == b"PK\x03\x04":
            return SIG_XLSX
        if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            return SIG_OLE2
        if data[:5] == b"%PDF-":
            return SIG_PDF
        if b"\x00" in data[:4096]:
            return SIG_BINARY
        text = data.decode("utf-8", errors="replace")
    else:
        text = payload
    return _classify_text(text)


def _classify_text(text: str) -> str:
    stripped = text.lstrip()
    if not stripped:
        return SIG_EMPTY
    head = stripped[:512].lower()
    if head[:1] in ("{", "["):
        return SIG_JSON
    if head.startswith(_HTML_HEADS):
        return SIG_HTML
    if head.startswith("<?xml"):
        # An XHTML/SVG-ish doc that contains a table is still HTML for our purposes.
        return SIG_HTML if ("<html" in head or "<table" in head) else SIG_XML
    probe = stripped[:8192].lower()
    if "<table" in probe or "<tr" in probe:
        return SIG_HTML
    sample = text[:4096]
    tabs, commas = sample.count("\t"), sample.count(",")
    if tabs and tabs >= commas:
        return SIG_TSV
    if commas:
        return SIG_CSV
    return SIG_TEXT


def to_spreadsheet_format(signature: str) -> str:
    """Map a descriptive byte signature to the coarse ``spreadsheet`` format token."""
    return _SIGNATURE_TO_FORMAT.get(signature, spreadsheet.TEXT)


def is_unsupported_binary(signature: str) -> bool:
    """Whether this signature is a binary blob the stdlib does not decode to rows."""
    return signature in UNSUPPORTED_SIGNATURES


_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*[^']*''([^;]+)", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


def filename_from_disposition(content_disposition: str | None) -> str | None:
    """Extract a download filename from a ``Content-Disposition`` header, if present.

    Handles both ``filename="ISF_holdings.csv"`` and RFC 5987
    ``filename*=UTF-8''ISF%20holdings.csv``. Returns a trimmed basename or None.
    Issuer download filenames carry no secrets, but we still strip any path parts.
    """
    if not content_disposition:
        return None
    match = _FILENAME_STAR_RE.search(content_disposition) or _FILENAME_RE.search(
        content_disposition
    )
    if not match:
        return None
    name = match.group(1).strip().strip('"').strip()
    # Drop any path component; keep only the basename.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    return name or None


def sha256_hex(payload: str | bytes | bytearray | None) -> str:
    """Stable SHA-256 of a payload (provenance/dedupe — never the payload itself)."""
    if payload is None:
        data = b""
    elif isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    else:
        data = payload.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class FetchDescriptor:
    """A secrets-free record of one issuer fetch (for fetch-metadata / provenance).

    Every field is safe to log/persist: ``endpoint_label`` is a host/path class (never
    a tokenised URL), and nothing here holds a cookie / API key / query string with a
    secret. ``byte_signature`` is what the bytes actually were (not what the URL or
    ``Content-Type`` claimed).
    """

    provider: str
    source_name: str
    byte_signature: str
    sha256: str
    parse_status: str
    fetched_at: datetime
    fund_symbol: str | None = None
    fund_isin: str | None = None
    endpoint_label: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    content_disposition: str | None = None
    filename: str | None = None
    parser_used: str | None = None
    row_count: int = 0
    known_blocker: str | None = None

    def summary(self) -> str:
        """A compact, secrets-free one-line summary (for logs / job messages)."""
        target = self.fund_symbol or self.fund_isin or "-"
        return (
            f"fetch provider={self.provider} source={self.source_name} fund={target} "
            f"endpoint={self.endpoint_label or '-'} http={self.http_status or '-'} "
            f"content_type={self.content_type or '-'} signature={self.byte_signature} "
            f"parser={self.parser_used or '-'} rows={self.row_count} "
            f"parse_status={self.parse_status}"
            + (f" blocker={self.known_blocker}" if self.known_blocker else "")
        )


def describe_fetch(
    *,
    provider: str,
    source_name: str,
    payload: str | bytes | bytearray | None,
    parse_status: str,
    fund_symbol: str | None = None,
    fund_isin: str | None = None,
    endpoint_label: str | None = None,
    http_status: int | None = None,
    content_type: str | None = None,
    content_disposition: str | None = None,
    parser_used: str | None = None,
    row_count: int = 0,
    known_blocker: str | None = None,
    now: datetime | None = None,
) -> FetchDescriptor:
    """Build a :class:`FetchDescriptor` from a payload + safe HTTP metadata.

    The ``byte_signature`` and ``sha256`` are derived from the *actual* bytes; the
    filename is parsed out of ``content_disposition``. The caller passes only
    secrets-free metadata (a host/path ``endpoint_label``, not a tokenised URL).
    """
    return FetchDescriptor(
        provider=provider,
        source_name=source_name,
        byte_signature=byte_signature(payload),
        sha256=sha256_hex(payload),
        parse_status=parse_status,
        fetched_at=now or datetime.now(UTC),
        fund_symbol=fund_symbol,
        fund_isin=fund_isin,
        endpoint_label=endpoint_label,
        http_status=http_status,
        content_type=content_type,
        content_disposition=content_disposition,
        filename=filename_from_disposition(content_disposition),
        parser_used=parser_used,
        row_count=row_count,
        known_blocker=known_blocker,
    )
