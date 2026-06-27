"""Workspace alert read side: list, filter, and read/dismiss/resolve state.

Alerts are workspace-scoped rows produced by `app.services.alert_generation`.
This module is the GUI-facing read/mutate surface — listing with filters and the
user state transitions (read / dismiss / resolve / mark-all-read). Generation
(the rules + idempotent upsert) lives in `alert_generation`.

Lists are bounded and ordered most-severe-then-newest first so the GUI can show
the most important alerts without paging.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import Alert
from app.schemas.alert import AlertCounts, AlertRead, AlertSummary
from app.services import alert_rules

# Most-severe-first ordering for SQL (critical -> error -> warning -> info).
_SEVERITY_ORDER = case(
    (Alert.severity == alert_rules.CRITICAL, 3),
    (Alert.severity == alert_rules.ERROR, 2),
    (Alert.severity == alert_rules.WARNING, 1),
    else_=0,
)

# Statuses a user still sees in the default ("open") list.
_OPEN_STATUSES = (alert_rules.STATUS_ACTIVE, alert_rules.STATUS_READ)


async def _get_scoped(session: AsyncSession, workspace_id: int, alert_id: int) -> Alert:
    alert = await session.scalar(
        select(Alert).where(Alert.id == alert_id, Alert.workspace_id == workspace_id)
    )
    if alert is None:
        raise NotFoundError("Alert not found", code="alert_not_found")
    return alert


async def list_alerts(
    session: AsyncSession,
    workspace_id: int,
    *,
    status: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    limit: int = 200,
) -> list[AlertRead]:
    stmt = select(Alert).where(Alert.workspace_id == workspace_id)
    if status is not None:
        stmt = stmt.where(Alert.status == status)
    if category is not None:
        stmt = stmt.where(Alert.category == category)
    if severity is not None:
        stmt = stmt.where(Alert.severity == severity)
    stmt = stmt.order_by(_SEVERITY_ORDER.desc(), Alert.last_seen_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [AlertRead.model_validate(a) for a in rows]


async def recent_active_alerts(
    session: AsyncSession, workspace_id: int, *, limit: int = 20
) -> list[AlertRead]:
    """Active + read alerts for the dashboard (dismissed/resolved hidden)."""
    stmt = (
        select(Alert)
        .where(Alert.workspace_id == workspace_id, Alert.status.in_(_OPEN_STATUSES))
        .order_by(_SEVERITY_ORDER.desc(), Alert.last_seen_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [AlertRead.model_validate(a) for a in rows]


# --- state transitions -------------------------------------------------------


async def mark_read(session: AsyncSession, workspace_id: int, alert_id: int) -> AlertRead:
    alert = await _get_scoped(session, workspace_id, alert_id)
    now = datetime.now(UTC)
    if alert.read_at is None:
        alert.read_at = now
    if alert.status == alert_rules.STATUS_ACTIVE:
        alert.status = alert_rules.STATUS_READ
    await session.commit()
    await session.refresh(alert)
    return AlertRead.model_validate(alert)


async def mark_dismissed(session: AsyncSession, workspace_id: int, alert_id: int) -> AlertRead:
    alert = await _get_scoped(session, workspace_id, alert_id)
    alert.status = alert_rules.STATUS_DISMISSED
    alert.dismissed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(alert)
    return AlertRead.model_validate(alert)


async def mark_resolved(session: AsyncSession, workspace_id: int, alert_id: int) -> AlertRead:
    alert = await _get_scoped(session, workspace_id, alert_id)
    alert.status = alert_rules.STATUS_RESOLVED
    alert.resolved_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(alert)
    return AlertRead.model_validate(alert)


async def mark_all_read(session: AsyncSession, workspace_id: int) -> int:
    """Mark every active alert in the workspace as read; return the count."""
    now = datetime.now(UTC)
    alerts = list(
        (
            await session.execute(
                select(Alert).where(
                    Alert.workspace_id == workspace_id,
                    Alert.status == alert_rules.STATUS_ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    for alert in alerts:
        alert.status = alert_rules.STATUS_READ
        if alert.read_at is None:
            alert.read_at = now
    await session.commit()
    return len(alerts)


# --- aggregates (dashboard / diagnostics) ------------------------------------


async def alert_counts(session: AsyncSession, workspace_id: int | None = None) -> AlertCounts:
    """Counts used by diagnostics; ``workspace_id=None`` aggregates all."""
    stmt = select(Alert.status, Alert.severity, Alert.category)
    if workspace_id is not None:
        stmt = stmt.where(Alert.workspace_id == workspace_id)
    rows = (await session.execute(stmt)).all()

    counts = AlertCounts()
    for status, severity, category in rows:
        is_open = status in _OPEN_STATUSES
        if status == alert_rules.STATUS_ACTIVE:
            counts.active_alerts += 1
            counts.unread_alerts += 1
        if not is_open:
            continue  # severity/category counts cover only open alerts
        if severity == alert_rules.CRITICAL:
            counts.critical_alerts += 1
        elif severity == alert_rules.ERROR:
            counts.error_alerts += 1
        elif severity == alert_rules.WARNING:
            counts.warning_alerts += 1
        if category == alert_rules.CAT_DOCUMENT:
            counts.document_alerts += 1
        elif category == alert_rules.CAT_PRICE:
            counts.price_alerts += 1
        elif category == alert_rules.CAT_FX:
            counts.fx_alerts += 1
        elif category == alert_rules.CAT_JOB:
            counts.job_alerts += 1
    return counts


# async def alert_counts(
#     session: AsyncSession,
#     workspace_id: int | None = None,
# ) -> AlertCounts:
#     """Counts used by diagnostics; ``workspace_id=None`` aggregates all."""
#     stmt = select(Alert.status, Alert.severity, Alert.category)
#     if workspace_id is not None:
#         stmt = stmt.where(Alert.workspace_id == workspace_id)
#     rows = (await session.execute(stmt)).all()

#     counts = AlertCounts()

#     for status, severity, category in rows:
#         if status == alert_rules.STATUS_ACTIVE:
#             counts.active_alerts += 1
#             counts.unread_alerts += 1
#         if status not in _OPEN_STATUSES:
#             continue  # severity/category counts cover only open alerts
#         match severity:
#             case alert_rules.CRITICAL:
#                 counts.critical_alerts += 1
#             case alert_rules.ERROR:
#                 counts.error_alerts += 1
#             case alert_rules.WARNING:
#                 counts.warning_alerts += 1
#         match category:
#             case alert_rules.CAT_DOCUMENT:
#                 counts.document_alerts += 1
#             case alert_rules.CAT_PRICE:
#                 counts.price_alerts += 1
#             case alert_rules.CAT_FX:
#                 counts.fx_alerts += 1
#             case alert_rules.CAT_JOB:
#                 counts.job_alerts += 1
#     return counts


async def alert_summary(session: AsyncSession, workspace_id: int) -> AlertSummary:
    """Compact dashboard summary: open counts, highest severity, breakdowns."""
    stmt = select(Alert.status, Alert.severity, Alert.category).where(
        Alert.workspace_id == workspace_id
    )
    rows = (await session.execute(stmt)).all()

    by_severity: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    active = unread = 0
    highest: str | None = None
    for status, severity, category in rows:
        if status == alert_rules.STATUS_ACTIVE:
            active += 1
            unread += 1
        if status not in _OPEN_STATUSES:
            continue
        by_severity[severity] += 1
        by_category[category] += 1
        if highest is None or alert_rules.SEVERITY_RANK.get(
            severity, 0
        ) > alert_rules.SEVERITY_RANK.get(highest, 0):
            highest = severity
    return AlertSummary(
        active=active,
        unread=unread,
        highest_severity=highest,
        by_severity=dict(by_severity),
        by_category=dict(by_category),
    )


async def unread_count(session: AsyncSession, workspace_id: int) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.workspace_id == workspace_id,
                Alert.status == alert_rules.STATUS_ACTIVE,
            )
        )
    ) or 0
