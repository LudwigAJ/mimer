"""Safe live verification of a known issuer source config.

A *verify-only* helper: it runs exactly one guarded fetch through the relevant
live source adapter (so the call still flows through the recent-success cache →
source budget → fetch log → fetch), parses the payload, and reports whether the
endpoint returned a clean, machine-readable file with the expected shape — WITHOUT
ingesting anything into the canonical tables.

It exists to answer one question honestly: *can this candidate config be promoted
to verified?* Because the config registry is in-code (``app/sources/issuer_source_config.py``),
verification does not persist a ``verified_at``/status — it returns a
``SourceVerificationReport`` (and the worker prints it). Promotion is a deliberate
code change after a clean live check, never automatic (see AGENTS.md: do not mark a
config verified without a successful live fetch+parse).

Compute boundary: this only fetches + parses + inspects published rows. No
analytics, ingestion or DB writes to canonical tables (the fetch log written by
``guarded_fetch`` is the only side effect, exactly as a normal live call).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.sources import issuer_source_config, spreadsheet
from app.sources.distributions import DistributionRecord, get_distribution_source
from app.sources.holdings import HoldingRecord, get_holdings_source

# Fetch outcomes (mirrors the guarded_fetch fetch-log statuses, read back after the call).
SUCCESS = "success"
CACHE_HIT = "cache_hit"
BUDGET_BLOCKED = "budget_blocked"
FETCH_ERROR = "fetch_error"
NO_URL = "no_url"
UNKNOWN_SOURCE = "unknown_source"

# Verification *reason* codes — the verdict (why a config can/can't be promoted),
# distinct from ``fetch_outcome`` (the HTTP/fetch-log status). Step 5 of the issuer
# source verification slice: a report must distinguish these operationally.
R_VERIFIED = "verified"  # clean live fetch+parse with the expected shape
R_BINARY_UNSUPPORTED = "binary_unsupported"  # 200 but an undecoded binary workbook (.xls/PDF)
R_ZERO_ROWS = "zero_rows"  # 200 + parseable payload, but no rows
R_MISSING_FIELDS = "missing_fields"  # rows parsed, but missing expected identifiers/fields
R_CACHE_HIT = "cache_hit"  # served from the recent-success cache (no live call)
R_BUDGET_BLOCKED = "budget_blocked"  # budget/backoff — no live call made
R_FETCH_ERROR = "fetch_error"  # HTTP/network/parse failure
R_NO_URL = "no_url"  # no --url and no usable known config
R_UNKNOWN_SOURCE = "unknown_source"  # not a recognised live holdings/distribution adapter


@dataclass
class SourceVerificationReport:
    """The outcome of a verify-only run for one (ISIN, source) config."""

    isin: str
    source_name: str
    data_type: str
    config_found: bool
    config_status: str | None
    attempted: bool  # did we have a URL and reach the guarded fetch?
    fetch_outcome: (
        str  # SUCCESS | CACHE_HIT | BUDGET_BLOCKED | FETCH_ERROR | NO_URL | UNKNOWN_SOURCE
    )
    row_count: int = 0
    has_expected_fields: bool = False
    as_of_date: date | None = None  # holdings disclosure / newest distribution ex-date
    sample: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = False  # a clean live fetch+parse with the expected shape
    recommended_status: str | None = None  # verified if ok, else the current/candidate status
    reason: str = ""  # a stable verdict code (R_VERIFIED / R_BINARY_UNSUPPORTED / ...)
    payload_format: str | None = None  # detected payload format (text / xlsx / xls / pdf / ...)
    detail: str = ""

    def message(self) -> str:
        return (
            f"verify source={self.source_name} isin={self.isin} data_type={self.data_type} "
            f"config={'found' if self.config_found else 'missing'}"
            f"({self.config_status or '-'}) outcome={self.fetch_outcome} "
            f"reason={self.reason or '-'} format={self.payload_format or '-'} "
            f"rows={self.row_count} expected_fields={self.has_expected_fields} ok={self.ok} "
            f"recommended_status={self.recommended_status or '-'}"
            + (f" — {self.detail}" if self.detail else "")
        )


def _infer_data_type(source_name: str) -> str | None:
    config = issuer_source_config.configs_for_source(source_name)
    if config:
        return config[0].data_type
    if "distribution" in source_name:
        return issuer_source_config.DATA_TYPE_DISTRIBUTIONS
    if "holdings" in source_name:
        return issuer_source_config.DATA_TYPE_HOLDINGS
    return None


def _holdings_report_fields(records: list[HoldingRecord]) -> tuple[bool, date | None, list[dict]]:
    # Expected: at least one row with a name + weight and a usable identifier.
    has_expected = any(
        r.holding_name and r.weight is not None and (r.holding_isin or r.holding_ticker)
        for r in records
    )
    as_of = max((r.as_of_date for r in records), default=None)
    sample = [
        {
            "name": r.holding_name,
            "ticker": r.holding_ticker,
            "isin": r.holding_isin,
            "weight": str(r.weight),
            "as_of_date": r.as_of_date.isoformat(),
        }
        for r in records[:3]
    ]
    return has_expected, as_of, sample


def _distribution_report_fields(
    records: list[DistributionRecord],
) -> tuple[bool, date | None, list[dict]]:
    # Expected: at least one row with an amount + currency + a parseable date.
    has_expected = any(r.amount is not None and r.currency and r.ex_date for r in records)
    as_of = max((r.ex_date for r in records), default=None)
    sample = [
        {
            "ex_date": r.ex_date.isoformat(),
            "amount": str(r.amount),
            "currency": r.currency,
            "type": r.distribution_type,
            "frequency": r.frequency,
        }
        for r in records[:3]
    ]
    return has_expected, as_of, sample


async def _latest_outcome(session: AsyncSession, source_name: str) -> str | None:
    """Classify the most recent fetch-log row for this source (after the guarded call)."""
    from app.services import source_requests

    logs = await source_requests.list_fetch_logs(session, source=source_name, limit=1)
    if not logs:
        return None
    status = logs[0].status
    if status == source_requests.SUCCESS:
        return SUCCESS
    if status == source_requests.CACHE_HIT:
        return CACHE_HIT
    if status == source_requests.RATE_LIMITED:
        return BUDGET_BLOCKED
    if status == source_requests.FAILED:
        return FETCH_ERROR
    return None


async def verify_issuer_source_config(
    session: AsyncSession,
    *,
    isin: str,
    source_name: str,
    data_type: str | None = None,
    url: str | None = None,
) -> SourceVerificationReport:
    """Run one guarded fetch + parse for a config and report (no ingestion).

    ``url`` overrides the configured URL (so an unregistered/candidate endpoint can
    be probed). Without a ``--url`` and without a registered config URL, the report
    is a clean ``no_url`` (no network). The live source must be a real live adapter;
    a fixture/offline source is reported as ``unknown_source`` for verification.
    """
    resolved_type = data_type or _infer_data_type(source_name)
    config = issuer_source_config.get_source_config(isin, source_name)
    download_url = url or (config.url if config else None)

    report = SourceVerificationReport(
        isin=isin,
        source_name=source_name,
        data_type=resolved_type or "unknown",
        config_found=config is not None,
        config_status=config.source_status if config else None,
        attempted=False,
        fetch_outcome=NO_URL,
        recommended_status=config.source_status if config else None,
    )

    if resolved_type not in (
        issuer_source_config.DATA_TYPE_HOLDINGS,
        issuer_source_config.DATA_TYPE_DISTRIBUTIONS,
    ):
        report.fetch_outcome = UNKNOWN_SOURCE
        report.reason = R_UNKNOWN_SOURCE
        report.detail = f"{source_name!r} is not a recognised live holdings/distribution source"
        return report

    if not download_url:
        report.reason = R_NO_URL
        report.detail = "no --url and no usable known config URL; nothing to verify (clean no-op)"
        return report

    # Resolve the live adapter; a fixture/offline source has no endpoint to verify.
    try:
        if resolved_type == issuer_source_config.DATA_TYPE_HOLDINGS:
            source = get_holdings_source(source_name)
        else:
            source = get_distribution_source(source_name)
    except ValueError:
        report.fetch_outcome = UNKNOWN_SOURCE
        report.reason = R_UNKNOWN_SOURCE
        report.detail = f"unknown source {source_name!r}"
        return report

    if not getattr(source, "requires_live_fetch", False):
        report.fetch_outcome = UNKNOWN_SOURCE
        report.reason = R_UNKNOWN_SOURCE
        report.detail = f"{source_name!r} is not a live adapter (nothing to verify live)"
        return report

    report.attempted = True
    # One guarded fetch through the adapter (cache → budget → fetch log → fetch).
    # The live adapters expose ``fetch_payload`` (the raw payload, so the format can be
    # classified for a precise reason code); a planned/placeholder live source has only
    # ``fetch`` (which raises) — surface that as a fetch error. Never crashes the helper.
    fetch_payload = getattr(source, "fetch_payload", None)
    try:
        if fetch_payload is None:
            records = await source.fetch(isin=isin, session=session, url=download_url)
        else:
            payload = await fetch_payload(isin=isin, session=session, url=download_url)
            if payload is None:  # no-url / budget block / cache hit / fetch error
                records = []
            else:
                report.payload_format = spreadsheet.sniff_format(payload)
                if report.payload_format in spreadsheet.UNSUPPORTED_BINARY:
                    records = []  # a binary workbook we deliberately do not decode
                else:
                    records = source._parse(payload)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - report, never crash a verify run
        report.fetch_outcome = await _latest_outcome(session, source_name) or FETCH_ERROR
        report.reason = R_FETCH_ERROR
        report.detail = f"adapter raised during fetch/parse: {type(exc).__name__}: {exc}"
        return report
    outcome = await _latest_outcome(session, source_name)
    report.fetch_outcome = outcome or (SUCCESS if records else FETCH_ERROR)

    if resolved_type == issuer_source_config.DATA_TYPE_HOLDINGS:
        has_expected, as_of, sample = _holdings_report_fields(
            [r for r in records if isinstance(r, HoldingRecord)]
        )
    else:
        has_expected, as_of, sample = _distribution_report_fields(
            [r for r in records if isinstance(r, DistributionRecord)]
        )

    report.row_count = len(records)
    report.has_expected_fields = has_expected
    report.as_of_date = as_of
    report.sample = sample
    report.ok = report.fetch_outcome == SUCCESS and report.row_count > 0 and has_expected
    # A 200 that returned an undecoded binary workbook (legacy .xls / PDF) is the most
    # specific reason — distinguish it from a genuinely empty (zero-rows) payload.
    binary_unsupported = (
        report.fetch_outcome == SUCCESS and report.payload_format in spreadsheet.UNSUPPORTED_BINARY
    )

    if report.ok:
        report.reason = R_VERIFIED
        report.recommended_status = issuer_source_config.VERIFIED
        report.detail = "clean live fetch+parse with expected shape — safe to promote to verified"
    elif report.fetch_outcome == CACHE_HIT:
        report.reason = R_CACHE_HIT
        report.detail = "served from the recent-success cache (a recent live fetch succeeded)"
    elif report.fetch_outcome == BUDGET_BLOCKED:
        report.reason = R_BUDGET_BLOCKED
        report.detail = "source is budget-blocked/in backoff — no live call made"
    elif binary_unsupported:
        report.reason = R_BINARY_UNSUPPORTED
        report.detail = (
            f"endpoint returned a binary {report.payload_format!r} workbook this backend does "
            "not decode (no pandas / no binary-Excel dependency) — keep candidate; supply a "
            "CSV / HTML-table / OOXML .xlsx endpoint variant"
        )
    elif report.fetch_outcome == SUCCESS and report.row_count == 0:
        report.reason = R_ZERO_ROWS
        report.detail = "endpoint returned no parseable rows — keep candidate, do not promote"
    elif report.fetch_outcome == SUCCESS and not has_expected:
        report.reason = R_MISSING_FIELDS
        report.detail = "rows parsed but missing expected identifiers/fields — keep candidate"
    else:
        report.reason = R_FETCH_ERROR
        report.detail = "fetch failed or returned an unusable payload — keep candidate"

    return report
