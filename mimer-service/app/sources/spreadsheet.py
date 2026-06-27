"""Stdlib-only spreadsheet sniffing + OOXML (``.xlsx``) row extraction.

No third-party dependency (no pandas / no xlrd / no calamine): an ``.xlsx`` file is
just a ZIP of XML parts, so the Python standard library (``zipfile`` + ``xml.etree``)
reads it — the same "use the stdlib, not a heavy parser" stance as the issuer HTML
table extractor (``html.parser``) elsewhere in this package.

What this module does NOT do (deliberately, see AGENTS.md):

* It does **not** decode the old binary ``.xls`` (OLE2 / BIFF) format. That needs a
  binary-Excel dependency (``xlrd``/``calamine``) the project avoids; it is detected
  and reported as an *unsupported binary format* so a caller can keep a source config
  ``candidate`` with a precise reason ("binary .xls", not a vague empty parse) instead
  of guessing. A CSV/HTML endpoint variant is the preferred resolution.
* It does **not** add analytics. It only turns a workbook's first sheet into the same
  ``list[list[str]]`` of cell strings the CSV/HTML sniffers already produce, which the
  shared header-scan/column-mapping then normalises.

A malformed/garbled workbook is a clean no-op (``[]``), never an exception — one bad
file never fails the worker.
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET

# Detected payload formats (returned by :func:`sniff_format`).
TEXT = "text"  # CSV / TSV / HTML / JSON / any decodable text
EMPTY = "empty"  # zero-length payload
XLSX = "xlsx"  # OOXML workbook (ZIP container) — parsed here
XLS = "xls"  # legacy binary OLE2/BIFF workbook — NOT decoded (deferred)
PDF = "pdf"  # PDF document — NOT a tabular source here
BINARY = "binary"  # some other binary blob (NUL bytes in the head)

# A binary workbook/document we can recognise but do not decode into rows.
UNSUPPORTED_BINARY = (XLS, PDF, BINARY)

# Leading magic bytes.
_ZIP_MAGIC = b"PK\x03\x04"  # ZIP / OOXML (.xlsx, .docx, ...)
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2 compound file (.xls, .doc)
_PDF_MAGIC = b"%PDF-"

# Content-type tokens that mean "this body is a binary workbook/document".
_BINARY_CONTENT_TYPE_TOKENS = (
    "application/vnd.ms-excel",
    "application/x-msexcel",
    "application/vnd.openxmlformats",
    "spreadsheetml",
    "officedocument",
    "application/octet-stream",
    "application/zip",
    "application/pdf",
)


def sniff_format(payload: str | bytes | bytearray | None) -> str:
    """Classify a fetched payload by its leading bytes (``str`` is always text).

    A ``str`` payload is text by construction (real binary arrives as ``bytes`` from
    the content-aware downloader), so only ``bytes`` are inspected for magic numbers.
    """
    if isinstance(payload, str):
        return TEXT
    if not payload:
        return EMPTY
    data = bytes(payload)
    if data[:4] == _ZIP_MAGIC:
        return XLSX
    if data[:8] == _OLE2_MAGIC:
        return XLS
    if data[:5] == _PDF_MAGIC:
        return PDF
    # A NUL byte in the head reliably marks a binary blob (decodable text has none).
    if b"\x00" in data[:4096]:
        return BINARY
    return TEXT


def is_binary_response(content_type: str | None, content: bytes) -> bool:
    """Whether an HTTP response body should be treated as binary (not decoded to str).

    True for an explicit binary content-type or a body whose magic bytes are binary;
    a normal CSV/JSON/HTML response (text content-type, no binary magic) is False.
    """
    ct = (content_type or "").lower()
    if any(token in ct for token in _BINARY_CONTENT_TYPE_TOKENS):
        return True
    return sniff_format(content) not in (TEXT, EMPTY)


# --- OOXML (.xlsx) row extraction (stdlib only) ------------------------------


def _local(tag: str) -> str:
    """The local name of a namespaced ElementTree tag (``{ns}row`` -> ``row``)."""
    return tag.rsplit("}", 1)[-1]


def _column_index(cell_ref: str) -> int:
    """0-based column index from a cell reference like ``"B2"`` -> ``1``.

    Excel omits empty cells, so the ``r`` reference is how column alignment is
    preserved (a sparse row still maps each value to the right header column).
    """
    letters = ""
    for ch in cell_ref:
        if ch.isalpha():
            letters += ch
        else:
            break
    if not letters:
        return 0
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """The shared-string table (cells of type ``s`` reference it by index)."""
    try:
        data = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    strings: list[str] = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        strings.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    return strings


def _first_worksheet(zf: zipfile.ZipFile) -> str | None:
    """The name of the first worksheet part (issuer exports use a single sheet)."""
    sheets = sorted(
        n for n in zf.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")
    )
    return sheets[0] if sheets else None


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    """The string value of a ``<c>`` cell (shared string / inline string / literal)."""
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(x.text or "" for x in cell.iter() if _local(x.tag) == "t")
    value: str | None = None
    for child in cell:
        local = _local(child.tag)
        if local == "is":  # inline string written without t="inlineStr"
            return "".join(x.text or "" for x in child.iter() if _local(x.tag) == "t")
        if local == "v":
            value = child.text
            break
    if value is None:
        return ""
    if cell_type == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    return value


def rows_from_xlsx(data: bytes) -> list[list[str]]:
    """Parse the first worksheet of an OOXML ``.xlsx`` workbook into rows of strings.

    Pure + offline + stdlib only. Robust to a malformed/garbled workbook (returns
    ``[]`` rather than raising). Sparse rows are reconstructed by column reference so
    column alignment with the header is preserved.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            sheet = _first_worksheet(zf)
            if sheet is None:
                return []
            shared = _shared_strings(zf)
            root = ET.fromstring(zf.read(sheet))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError):
        return []

    sheet_data = next((c for c in root if _local(c.tag) == "sheetData"), None)
    if sheet_data is None:
        return []

    rows: list[list[str]] = []
    for row_el in sheet_data:
        if _local(row_el.tag) != "row":
            continue
        values: dict[int, str] = {}
        next_idx = 0
        for cell in row_el:
            if _local(cell.tag) != "c":
                continue
            ref = cell.get("r", "")
            idx = _column_index(ref) if ref else next_idx
            values[idx] = _cell_value(cell, shared)
            next_idx = idx + 1
        if not values:
            rows.append([])
            continue
        width = max(values) + 1
        rows.append([values.get(i, "") for i in range(width)])
    return rows
