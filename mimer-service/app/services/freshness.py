"""Freshness derivation shared by the GUI-facing aggregate endpoints.

The GUI shows a freshness/state badge on most records. Rather than store it, we
derive a coarse state from the relevant timestamp (``last_price_at``,
``last_refreshed_at``, ``as_of_date``, ...) at read time:

* ``fresh``   — refreshed within the kind's window.
* ``stale``   — older than the window.
* ``missing`` — no timestamp at all (never refreshed / no data).

Windows are deliberately generous and coarse for a first pass; they are not a
real SLA. ``seed`` / fixture data is timestamped ``now()`` on load, so it reads
as ``fresh`` until it ages out, which is the honest signal for placeholder data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

# Default freshness windows in days, per data kind.
FRESH_WINDOW_DAYS: dict[str, int] = {
    "price": 4,  # daily prices; a few days covers weekends/holidays
    "fx": 4,
    "fund_facts": 90,  # issuer facts change rarely
    "distribution": 120,  # quarterly-ish cadence
    "holdings": 45,  # monthly-ish disclosure
    "document": 400,  # annual documents
    "reference_rate": 7,  # daily official rates; a week covers weekends/holidays
}
_DEFAULT_WINDOW_DAYS = 30

FRESH = "fresh"
STALE = "stale"
MISSING = "missing"


def _as_utc_datetime(value: datetime | date | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    # A plain date — treat as midnight UTC.
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def freshness_state(value: datetime | date | None, *, kind: str = "") -> str:
    """Classify a record's freshness from its most recent timestamp."""
    moment = _as_utc_datetime(value)
    if moment is None:
        return MISSING
    window = FRESH_WINDOW_DAYS.get(kind, _DEFAULT_WINDOW_DAYS)
    age_days = (datetime.now(UTC) - moment).days
    return FRESH if age_days <= window else STALE
