"""Fund document sources, isolated behind a small protocol + registry.

A *document* is a published fund artefact — factsheet, KID/KIID, prospectus,
annual/interim report, holdings file — identified by type, URL and a content
hash. Documents belong to the *fund* (not a listing) — see the identity rules in
AGENTS.md.

This iteration ships a robust **fixture** provider so the worker, job plumbing
and tests work with no live network access. Real issuer document adapters
(Vanguard / iShares / JPMAM product pages + PDFs) slot in behind the same
`DocumentSource` protocol later; the worker and API never depend on a specific
provider. There is intentionally **no PDF text extraction / OCR** here — the
fixture ships small deterministic text and the ingestion layer hashes it.

Note: this module is a *source adapter* (provider-specific fetch/parse). The
provider-agnostic hashing / change-detection / upsert / job_runs logic lives in
``app/services/document_ingestion.py`` (and ``app/services/documents.py`` for the
hash helper).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

# Controlled vocabulary for document types (kept in sync with docs).
DOCUMENT_TYPES = (
    "factsheet",
    "kid",
    "kiid",
    "prospectus",
    "annual_report",
    "interim_report",
    "holdings",
    "other",
)


@dataclass(frozen=True)
class DocumentRecord:
    """A normalized fund document as published by a source.

    The adapter returns metadata plus a *small* content payload (``content_text``
    or ``content_bytes``); the ingestion layer derives ``content_hash`` from it
    (or from stable metadata when no content is supplied). No blobs are stored.
    """

    document_type: str
    title: str
    source: str
    document_url: str | None = None
    document_date: date | None = None
    language: str | None = None
    country_or_region: str | None = None
    content_type: str | None = None
    content_bytes: bytes | None = None
    content_text: str | None = None
    # Optional pre-computed hash; if None the ingestion layer computes one.
    content_hash: str | None = None
    # current | superseded | withdrawn | ... (provider-asserted, optional).
    status: str | None = None
    # Reserved for future provenance/debugging (raw provider payload).
    raw_payload: dict[str, Any] | None = None


class DocumentSource(Protocol):
    name: str

    async def fetch_documents(self, *, isin: str) -> list[DocumentRecord]:
        """Return published documents for a fund ISIN (possibly empty)."""
        ...


# --- fixture provider --------------------------------------------------------
#
# Keyed by fund ISIN. Mirrors the seeded funds so the worker has something
# authoritative-but-offline to ingest. Content is a short deterministic text line
# per document (realistic in shape, not a real PDF) so the content hash is stable
# across runs. Each row is
# "document_type|title|url|document_date|language|country_or_region|content_type".
_COLUMNS: tuple[str, ...] = (
    "document_type",
    "title",
    "document_url",
    "document_date",
    "language",
    "country_or_region",
    "content_type",
)


def _row(line: str, content_text: str) -> dict[str, Any]:
    fields = dict(zip(_COLUMNS, line.split("|"), strict=True))
    document_date = fields.pop("document_date") or None
    return {
        **fields,
        "document_date": date.fromisoformat(document_date) if document_date else None,
        "content_text": content_text,
        "status": "current",
    }


# Per-fund document sets. The trailing text is the (small) deterministic content
# the ingestion layer hashes — change it and the worker records a "changed"
# snapshot, which is exactly what the change-detection tests exercise.
_FIXTURES: dict[str, list[tuple[str, str]]] = {
    # VUSA — Vanguard S&P 500 UCITS ETF.
    "IE00B3XXRP09": [
        (
            "factsheet|Vanguard S&P 500 UCITS ETF — Factsheet|https://www.vanguard.co.uk/factsheet/VUSA.pdf|2026-05-31|en|UK|application/pdf",
            "VUSA factsheet 2026-05-31: TER 0.07%, USD, distributing, S&P 500.",
        ),
        (
            "kiid|Vanguard S&P 500 UCITS ETF — KIID|https://www.vanguard.co.uk/kiid/VUSA.pdf|2026-01-15|en|UK|application/pdf",
            "VUSA KIID 2026-01-15: risk 5/7, ongoing charge 0.07%.",
        ),
        (
            "prospectus|Vanguard Funds plc — Prospectus|https://www.vanguard.co.uk/prospectus/VUSA.pdf|2025-12-01|en|IE|application/pdf",
            "Vanguard Funds plc prospectus 2025-12-01 (umbrella).",
        ),
    ],
    # ISF — iShares Core FTSE 100 UCITS ETF.
    "IE0005042456": [
        (
            "factsheet|iShares Core FTSE 100 UCITS ETF — Factsheet|https://www.ishares.com/factsheet/ISF.pdf|2026-05-31|en|UK|application/pdf",
            "ISF factsheet 2026-05-31: TER 0.07%, GBP, distributing, FTSE 100.",
        ),
        (
            "kiid|iShares Core FTSE 100 UCITS ETF — KIID|https://www.ishares.com/kiid/ISF.pdf|2026-02-10|en|UK|application/pdf",
            "ISF KIID 2026-02-10: risk 6/7, ongoing charge 0.07%.",
        ),
        (
            "prospectus|iShares plc — Prospectus|https://www.ishares.com/prospectus/ISF.pdf|2025-11-20|en|IE|application/pdf",
            "iShares plc prospectus 2025-11-20 (umbrella).",
        ),
        (
            "annual_report|iShares plc — Annual Report 2025|https://www.ishares.com/annual/ISF-2025.pdf|2026-03-31|en|IE|application/pdf",
            "iShares plc annual report FY2025.",
        ),
    ],
    # JPMorgan Global Equity Premium Income.
    "IE0003UVYC20": [
        (
            "factsheet|JPM Global Equity Premium Income — Factsheet|https://am.jpmorgan.com/factsheet/JEPG.pdf|2026-05-31|en|UK|application/pdf",
            "JEPG factsheet 2026-05-31: TER 0.35%, USD, monthly income overlay.",
        ),
        (
            "kid|JPM Global Equity Premium Income — KID|https://am.jpmorgan.com/kid/JEPG.pdf|2026-01-20|en|UK|application/pdf",
            "JEPG KID 2026-01-20: risk 4/7, ongoing charge 0.35%.",
        ),
        (
            "prospectus|JPMorgan ETFs (Ireland) ICAV — Prospectus|https://am.jpmorgan.com/prospectus/JEPG.pdf|2025-10-05|en|IE|application/pdf",
            "JPMorgan ETFs (Ireland) ICAV prospectus 2025-10-05.",
        ),
    ],
}


class StaticDocumentSource:
    """Offline document provider backed by a fixture table (keyed by ISIN)."""

    name = "document_fixture"

    async def fetch_documents(self, *, isin: str) -> list[DocumentRecord]:
        rows = _FIXTURES.get(isin, [])
        return [
            DocumentRecord(source=self.name, **_row(line, content_text))
            for line, content_text in rows
        ]


# --- registry ----------------------------------------------------------------

_SOURCES: dict[str, DocumentSource] = {
    StaticDocumentSource.name: StaticDocumentSource(),
}


def get_document_source(name: str | None = None) -> DocumentSource:
    from app.core.config import get_settings

    source_name = name or get_settings().document_source_default
    source = _SOURCES.get(source_name)
    if source is None:
        raise ValueError(f"Unknown document source: {source_name!r}")
    return source
