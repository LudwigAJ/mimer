"""Stdlib OOXML (.xlsx) workbook support + binary-format verify reason codes.

All offline — no third-party spreadsheet dependency (no pandas / xlrd / calamine).
The ``.xlsx`` fixtures are built in-process with the stdlib (``zipfile`` + XML), the
legacy binary ``.xls`` is represented by its OLE2 magic bytes, and the live adapters'
single HTTP call (``_download``) is stubbed to return ``bytes`` so the guarded fetch
path (cache → budget → fetch log) is exercised without touching the network.
"""

from __future__ import annotations

import io
import zipfile
from decimal import Decimal
from xml.sax.saxutils import escape

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Fund
from app.services import issuer_source_verification as verification
from app.sources import issuer_source_config as cfg
from app.sources import spreadsheet
from app.sources.distributions import (
    JPMorganDistributionsSource,
    VanguardDistributionsSource,
    parse_jpmorgan_distributions,
)
from app.sources.holdings import (
    IsharesHoldingsSource,
    JPMorganHoldingsSource,
    parse_jpmorgan_holdings,
)

_JEPG = "IE0003UVYC20"  # JPM Global Equity Premium Income (candidate holdings config)
_JPM_HOLD = "jpmorgan_etf_holdings"

# Old binary .xls (OLE2/BIFF) — the format JEPG actually serves; deliberately NOT decoded.
_OLE2_XLS = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + b"Workbook"


# --- a tiny stdlib .xlsx builder (test fixtures only) ------------------------

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _col_letter(idx0: int) -> str:
    idx = idx0 + 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def build_xlsx(rows: list[list[object | None]]) -> bytes:
    """A minimal valid OOXML ``.xlsx`` workbook (shared strings + numbers).

    A ``None`` cell is *omitted* (no ``<c>``) so the parser's column-reference
    reconstruction (sparse rows) is exercised, exactly like a real issuer export.
    """
    shared: list[str] = []
    shared_index: dict[str, int] = {}

    def _share(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared)
            shared.append(value)
        return shared_index[value]

    row_xml: list[str] = []
    for r, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c, value in enumerate(row):
            if value is None:
                continue
            ref = f"{_col_letter(c)}{r}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{_share(str(value))}</v></c>')
        row_xml.append(f'<row r="{r}">{"".join(cells)}</row>')

    sheet = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS}"><sheetData>{"".join(row_xml)}</sheetData></worksheet>'
    )
    sst = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_NS}" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared)
        + "</sst>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
        f'2006/relationships"><sheets><sheet name="Holdings" sheetId="1" r:id="rId1"/></sheets>'
        f"</workbook>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/sharedStrings.xml", sst)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


# JPM holdings .xlsx shape (header + two good rows + a bad non-numeric-weight row).
_JPM_HOLDINGS_XLSX_ROWS: list[list[object | None]] = [
    ["Ticker", "Security Description", "% of Net Assets", "ISIN"],
    ["MSFT", "MICROSOFT CORP", 1.90, "US5949181045"],
    ["AAPL", "APPLE INC", 1.80, "US0378331005"],
    ["BAD", "BROKEN ROW", "n/a", None],  # bad weight -> isolated
]


# --- spreadsheet module: sniff_format ----------------------------------------


def test_sniff_format_classifies_payloads() -> None:
    assert spreadsheet.sniff_format("any text") == spreadsheet.TEXT
    assert spreadsheet.sniff_format(b"") == spreadsheet.EMPTY
    assert spreadsheet.sniff_format(build_xlsx(_JPM_HOLDINGS_XLSX_ROWS)) == spreadsheet.XLSX
    assert spreadsheet.sniff_format(_OLE2_XLS) == spreadsheet.XLS
    assert spreadsheet.sniff_format(b"%PDF-1.7\n...") == spreadsheet.PDF
    assert (
        spreadsheet.sniff_format(b"Ticker,Name,Weight\nAZN,AstraZeneca,8.1\n") == spreadsheet.TEXT
    )
    assert spreadsheet.sniff_format(b"row\x00with\x00nuls") == spreadsheet.BINARY


def test_is_binary_response() -> None:
    xlsx = build_xlsx(_JPM_HOLDINGS_XLSX_ROWS)
    # Text content-types / bodies are not binary.
    assert spreadsheet.is_binary_response("text/csv; charset=utf-8", b"a,b\n1,2\n") is False
    assert spreadsheet.is_binary_response("application/json", b"{}") is False
    assert spreadsheet.is_binary_response("text/html", b"<table></table>") is False
    # Explicit binary content-types, or binary magic bytes, are binary.
    assert spreadsheet.is_binary_response("application/vnd.ms-excel", b"whatever") is True
    assert spreadsheet.is_binary_response("application/octet-stream", xlsx) is True
    assert spreadsheet.is_binary_response(None, xlsx) is True
    assert spreadsheet.is_binary_response(None, _OLE2_XLS) is True


# --- spreadsheet module: rows_from_xlsx --------------------------------------


def test_rows_from_xlsx_parses_cells_and_reconstructs_gaps() -> None:
    rows = spreadsheet.rows_from_xlsx(
        build_xlsx(
            [
                ["A", "B", "C"],
                ["one", None, "three"],  # middle cell omitted -> must be padded with ""
                ["x", "y", 42],
            ]
        )
    )
    assert rows[0] == ["A", "B", "C"]
    assert rows[1] == ["one", "", "three"]  # gap reconstructed by column reference
    assert rows[2] == ["x", "y", "42"]


def test_rows_from_xlsx_malformed_is_clean_noop() -> None:
    assert spreadsheet.rows_from_xlsx(b"PK\x03\x04 not really a zip") == []
    assert spreadsheet.rows_from_xlsx(_OLE2_XLS) == []
    assert spreadsheet.rows_from_xlsx(b"") == []


# --- parsers accept .xlsx bytes; legacy .xls is a clean no-op -----------------


def test_jpmorgan_holdings_parses_xlsx_bytes() -> None:
    records = parse_jpmorgan_holdings(build_xlsx(_JPM_HOLDINGS_XLSX_ROWS))
    # Two good rows; the "n/a" weight row is isolated.
    assert len(records) == 2
    msft = records[0]
    assert msft.holding_name == "MICROSOFT CORP"
    assert msft.holding_ticker == "MSFT"
    assert msft.holding_isin == "US5949181045"
    assert str(msft.weight) == "0.01900000"  # % of Net Assets 1.90 -> 0.019


def test_jpmorgan_holdings_legacy_xls_bytes_is_empty() -> None:
    # Old binary .xls (OLE2) is deferred -> clean no-op (no pandas / xlrd / calamine).
    assert parse_jpmorgan_holdings(_OLE2_XLS) == []


def test_jpmorgan_distributions_parses_xlsx_bytes() -> None:
    rows: list[list[object | None]] = [
        ["Ex-Date", "Payment Date", "Distribution Amount", "Currency", "Frequency"],
        ["2026-01-02", "2026-01-15", 0.3500, "USD", "Monthly"],
        ["2026-02-02", "2026-02-15", 0.3450, "USD", "Monthly"],
    ]
    records = parse_jpmorgan_distributions(build_xlsx(rows))
    assert len(records) == 2
    assert records[0].amount == Decimal("0.35000000")
    assert records[0].currency == "USD"
    assert records[0].frequency == "Monthly"


# --- conservative Vanguard headers (no cookies / no fingerprint spoofing) -----


def test_vanguard_sends_conservative_official_headers() -> None:
    headers = VanguardDistributionsSource.request_headers
    assert headers is not None
    assert "Mimer" in headers["User-Agent"]
    assert "json" in headers["Accept"]
    assert headers["Accept-Language"].startswith("en")
    # No cookies, no fingerprint spoofing — keys are limited to honest identifying headers.
    lowered = {k.lower() for k in headers}
    assert "cookie" not in lowered
    assert lowered <= {"user-agent", "accept", "accept-language"}
    # The other live adapters send no special headers by default.
    assert JPMorganHoldingsSource.request_headers is None
    assert IsharesHoldingsSource.request_headers is None
    assert JPMorganDistributionsSource.request_headers is None


# --- verify-source reason codes (offline; _download stubbed with bytes) -------


def _patch_download(monkeypatch: pytest.MonkeyPatch, cls, payload: str | bytes) -> None:
    async def fake_download(self, url: str):  # noqa: ANN001
        return payload

    monkeypatch.setattr(cls, "_download", fake_download)


async def _fund(session: AsyncSession, isin: str) -> Fund:
    fund = await session.scalar(select(Fund).where(Fund.isin == isin))
    assert fund is not None
    return fund


async def test_verify_jpmorgan_xlsx_payload_promotes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real (binary) .xlsx workbook parses end-to-end -> verifiable.
    _patch_download(monkeypatch, JPMorganHoldingsSource, build_xlsx(_JPM_HOLDINGS_XLSX_ROWS))
    fund = await _fund(session, _JEPG)
    report = await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_JPM_HOLD, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    assert report.ok is True
    assert report.payload_format == spreadsheet.XLSX
    assert report.reason == verification.R_VERIFIED
    assert report.row_count == 2
    assert report.recommended_status == cfg.VERIFIED


async def test_verify_jpmorgan_binary_xls_reports_binary_unsupported(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The legacy binary .xls (OLE2) JEPG actually serves -> a precise reason, candidate.
    _patch_download(monkeypatch, JPMorganHoldingsSource, _OLE2_XLS)
    fund = await _fund(session, _JEPG)
    report = await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_JPM_HOLD, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    assert report.ok is False
    assert report.fetch_outcome == verification.SUCCESS  # HTTP 200 succeeded
    assert report.payload_format == spreadsheet.XLS
    assert report.reason == verification.R_BINARY_UNSUPPORTED
    assert report.row_count == 0
    assert report.recommended_status == cfg.CANDIDATE
    assert "binary_unsupported" in report.message()


async def test_verify_missing_fields_reason(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rows parse (name + weight) but carry no identifier -> missing_fields, not zero_rows.
    # JEPG's JPM holdings config is candidate, so a non-promotable verify stays candidate.
    csv_text = (
        "Security Description,% of Net Assets,Sector\n"
        "MICROSOFT CORP,1.90,Technology\n"
        "APPLE INC,1.80,Technology\n"
    )
    _patch_download(monkeypatch, JPMorganHoldingsSource, csv_text)
    fund = await _fund(session, _JEPG)
    report = await verification.verify_issuer_source_config(
        session, isin=fund.isin, source_name=_JPM_HOLD, data_type=cfg.DATA_TYPE_HOLDINGS
    )
    assert report.row_count == 2
    assert report.has_expected_fields is False
    assert report.reason == verification.R_MISSING_FIELDS
    assert report.recommended_status == cfg.CANDIDATE
