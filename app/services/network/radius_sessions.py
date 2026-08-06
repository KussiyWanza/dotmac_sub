"""RADIUS session resolver SOT.

This service answers "is the customer online now, and where?" using the
canonical `radius_active_sessions` table. It does not decide entitlement or
outage impact; higher layers compose those answers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.radius import RadiusClient
from app.models.radius_active_session import RadiusActiveSession
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services.common import coerce_uuid
from app.services.network.identity import NetworkIdentity, identity_for_radius_session

ACTIVE_SESSION_FRESHNESS = timedelta(minutes=15)


class SubscriptionSessionTarget(Protocol):
    """Minimal cross-domain input required for session binding."""

    @property
    def id(self) -> object: ...

    @property
    def subscriber_id(self) -> object: ...

    @property
    def status(self) -> object: ...


class SubscriptionSessionState(StrEnum):
    connected = "connected"
    stale = "stale"
    offline = "offline"
    inactive = "inactive"


class SubscriptionSessionBinding(StrEnum):
    exact_subscription = "exact_subscription"
    single_service_unbound = "single_service_unbound"
    none = "none"


@dataclass(frozen=True, slots=True)
class SubscriptionSessionSnapshot:
    """Subscription-scoped live-session observation.

    An unbound session is used only when the subscriber has one visible service.
    This prevents a single subscriber-level session from being projected onto
    every sibling subscription in a multi-service account.
    """

    subscription_id: object
    state: SubscriptionSessionState
    binding: SubscriptionSessionBinding
    observed_at: datetime | None
    framed_ip_address: str | None
    nas_device_id: object | None
    acct_session_id: str | None

    @property
    def is_online(self) -> bool:
        return self.state in {
            SubscriptionSessionState.connected,
            SubscriptionSessionState.stale,
        }


@dataclass(frozen=True)
class RadiusSessionResolution:
    subscriber_id: object
    sessions: tuple[RadiusActiveSession, ...]
    primary_session: RadiusActiveSession | None
    primary_identity: NetworkIdentity | None

    @property
    def is_online(self) -> bool:
        return self.primary_session is not None


@dataclass(frozen=True)
class HistoricalNasTarget:
    nas_device_id: object
    session_count: int
    last_seen_at: datetime


@dataclass(frozen=True)
class SubscriptionNasHistory:
    subscription_id: object
    targets: tuple[HistoricalNasTarget, ...]


@dataclass(frozen=True, slots=True)
class AccountingCoverageInterval:
    """One exact subscription-bound positive accounting interval."""

    subscription_id: UUID
    source_id: UUID
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class AccountingCoveragePeriodHistory:
    """Positive service observations for exactly one half-open period."""

    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    intervals: tuple[AccountingCoverageInterval, ...]
    complete: bool
    issues: tuple[str, ...]


def _accounting_recency_expression():
    return func.coalesce(
        RadiusAccountingSession.last_update_at,
        RadiusAccountingSession.session_end,
        RadiusAccountingSession.session_start,
        RadiusAccountingSession.created_at,
    )


def latest_accounting_observation_at(db: Session) -> datetime | None:
    """Return the newest imported accounting observation for freshness checks."""
    value = db.scalar(select(func.max(_accounting_recency_expression())))
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def list_active_sessions_for_subscriber(
    db: Session,
    subscriber_id,
    *,
    limit: int = 20,
) -> list[RadiusActiveSession]:
    return list(
        db.scalars(
            select(RadiusActiveSession)
            .where(RadiusActiveSession.subscriber_id == coerce_uuid(subscriber_id))
            .order_by(
                RadiusActiveSession.last_update.desc().nullslast(),
                RadiusActiveSession.session_start.desc(),
                RadiusActiveSession.id,
            )
            .limit(limit)
        ).all()
    )


def resolve_subscriber_radius_sessions(
    db: Session,
    subscriber_id,
    *,
    limit: int = 20,
) -> RadiusSessionResolution:
    sessions = tuple(
        list_active_sessions_for_subscriber(db, subscriber_id, limit=limit)
    )
    primary = sessions[0] if sessions else None
    return RadiusSessionResolution(
        subscriber_id=coerce_uuid(subscriber_id),
        sessions=sessions,
        primary_session=primary,
        primary_identity=identity_for_radius_session(db, primary) if primary else None,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def accounting_coverage_for_period(
    db: Session,
    subscription_id: UUID,
    *,
    period_start: datetime,
    period_end: datetime,
) -> AccountingCoveragePeriodHistory:
    """Return only positive, exact-subscription historical RADIUS evidence.

    An open session ends at its last imported observation.  The resolver never
    extends it to ``now`` and never binds subscriber-level sessions to a
    service retrospectively; both would guess uptime.
    """

    start = _as_utc(period_start)
    end = _as_utc(period_end)
    if start is None or end is None or end <= start:
        raise ValueError("Accounting coverage requires a positive UTC period")

    observed_end = func.coalesce(
        RadiusAccountingSession.session_end,
        RadiusAccountingSession.last_update_at,
    )
    rows = db.scalars(
        select(RadiusAccountingSession)
        .where(
            RadiusAccountingSession.subscription_id == subscription_id,
            RadiusAccountingSession.session_start.isnot(None),
            RadiusAccountingSession.session_start < end,
            observed_end.isnot(None),
            observed_end > start,
        )
        .order_by(
            RadiusAccountingSession.session_start,
            observed_end,
            RadiusAccountingSession.id,
        )
    ).all()
    intervals: list[AccountingCoverageInterval] = []
    issues: list[str] = []
    for row in rows:
        row_start = _as_utc(row.session_start)
        row_end = _as_utc(row.session_end or row.last_update_at)
        if row_start is None or row_end is None or row_end <= row_start:
            issues.append(f"invalid_accounting_interval:{row.id}")
            continue
        interval_start = max(row_start, start)
        interval_end = min(row_end, end)
        if interval_end <= interval_start:
            continue
        intervals.append(
            AccountingCoverageInterval(
                subscription_id=subscription_id,
                source_id=row.id,
                starts_at=interval_start,
                ends_at=interval_end,
            )
        )

    return AccountingCoveragePeriodHistory(
        subscription_id=subscription_id,
        period_start=start,
        period_end=end,
        intervals=tuple(intervals),
        complete=not issues,
        issues=tuple(issues),
    )


def _active_session_observed_at(session: RadiusActiveSession) -> datetime:
    observed_at = _as_utc(
        session.last_update or session.session_start or session.created_at
    )
    if observed_at is None:
        raise ValueError("Active RADIUS session has no observation timestamp")
    return observed_at


def _active_session_order_key(session: RadiusActiveSession) -> tuple[float, float, str]:
    observed_at = _active_session_observed_at(session)
    started_at = _as_utc(session.session_start) or observed_at
    return (-observed_at.timestamp(), -started_at.timestamp(), str(session.id))


def subscription_session_snapshots(
    db: Session,
    subscriptions: Sequence[SubscriptionSessionTarget],
    *,
    now: datetime | None = None,
) -> dict[object, SubscriptionSessionSnapshot]:
    """Resolve one trustworthy live-session observation per subscription.

    Exact subscription bindings always win. A subscriber-level, unbound session
    may backstop only a single-service account; it is deliberately ignored for a
    multi-service account so IP and NAS evidence cannot leak across siblings.
    """
    current = tuple(subscriptions)
    if not current:
        return {}
    observed_now = _as_utc(now) or datetime.now(UTC)
    subscriber_ids = {subscription.subscriber_id for subscription in current}
    rows = list(
        db.scalars(
            select(RadiusActiveSession)
            .where(RadiusActiveSession.subscriber_id.in_(subscriber_ids))
            .order_by(RadiusActiveSession.id)
        ).all()
    )
    sessions_by_subscriber: dict[object, list[RadiusActiveSession]] = defaultdict(list)
    for row in rows:
        if row.subscriber_id is not None:
            sessions_by_subscriber[row.subscriber_id].append(row)

    service_count_by_subscriber: dict[object, int] = defaultdict(int)
    for subscription in current:
        if getattr(subscription.status, "value", subscription.status) == "active":
            service_count_by_subscriber[subscription.subscriber_id] += 1

    snapshots: dict[object, SubscriptionSessionSnapshot] = {}
    for subscription in current:
        if getattr(subscription.status, "value", subscription.status) != "active":
            snapshots[subscription.id] = SubscriptionSessionSnapshot(
                subscription_id=subscription.id,
                state=SubscriptionSessionState.inactive,
                binding=SubscriptionSessionBinding.none,
                observed_at=None,
                framed_ip_address=None,
                nas_device_id=None,
                acct_session_id=None,
            )
            continue

        subscriber_rows = sessions_by_subscriber.get(subscription.subscriber_id, [])
        exact = [
            row for row in subscriber_rows if row.subscription_id == subscription.id
        ]
        binding = SubscriptionSessionBinding.exact_subscription
        candidates = exact
        if (
            not candidates
            and service_count_by_subscriber[subscription.subscriber_id] == 1
        ):
            candidates = [row for row in subscriber_rows if row.subscription_id is None]
            binding = SubscriptionSessionBinding.single_service_unbound

        if not candidates:
            snapshots[subscription.id] = SubscriptionSessionSnapshot(
                subscription_id=subscription.id,
                state=SubscriptionSessionState.offline,
                binding=SubscriptionSessionBinding.none,
                observed_at=None,
                framed_ip_address=None,
                nas_device_id=None,
                acct_session_id=None,
            )
            continue

        session = min(candidates, key=_active_session_order_key)
        observed_at = _active_session_observed_at(session)
        state = (
            SubscriptionSessionState.connected
            if observed_at >= observed_now - ACTIVE_SESSION_FRESHNESS
            else SubscriptionSessionState.stale
        )
        snapshots[subscription.id] = SubscriptionSessionSnapshot(
            subscription_id=subscription.id,
            state=state,
            binding=binding,
            observed_at=observed_at,
            framed_ip_address=session.framed_ip_address,
            nas_device_id=session.nas_device_id,
            acct_session_id=session.acct_session_id,
        )
    return snapshots


def online_summary(db: Session) -> dict[str, int]:
    """Return the live active-session counts for the operations overview.

    Canonical read owner for "who is online now": total active sessions and the
    number of distinct subscribers with at least one session. Dashboards and
    other surfaces consume this instead of querying RadiusActiveSession directly.
    """
    row = db.execute(
        select(
            func.count(RadiusActiveSession.id).label("sessions"),
            func.count(func.distinct(RadiusActiveSession.subscriber_id))
            .filter(RadiusActiveSession.subscriber_id.is_not(None))
            .label("customers"),
        )
    ).one()
    return {"sessions": int(row.sessions or 0), "customers": int(row.customers or 0)}


def active_session_count_for_subscriber(db: Session, subscriber_id) -> int:
    return int(
        db.scalar(
            select(func.count(RadiusActiveSession.id)).where(
                RadiusActiveSession.subscriber_id == coerce_uuid(subscriber_id)
            )
        )
        or 0
    )


def list_all_active_sessions(db: Session) -> list[RadiusActiveSession]:
    """Return the canonical live-session inventory for SOT coordinators."""
    return list(
        db.scalars(select(RadiusActiveSession).order_by(RadiusActiveSession.id)).all()
    )


def live_nas_device_ids_for_subscription(
    db: Session,
    subscription_id,
    subscriber_id,
    *,
    allow_unbound: bool = True,
) -> tuple[object, ...]:
    """Return live NAS evidence from primitive subscription identity."""
    resolved_subscription_id = coerce_uuid(subscription_id)
    resolved_subscriber_id = coerce_uuid(subscriber_id)
    binding_filter = RadiusActiveSession.subscription_id == resolved_subscription_id
    if allow_unbound:
        binding_filter = or_(
            binding_filter,
            RadiusActiveSession.subscription_id.is_(None),
        )
    rows = db.scalars(
        select(RadiusActiveSession.nas_device_id)
        .where(RadiusActiveSession.subscriber_id == resolved_subscriber_id)
        .where(RadiusActiveSession.nas_device_id.is_not(None))
        .where(binding_filter)
        .order_by(
            RadiusActiveSession.last_update.desc().nullslast(),
            RadiusActiveSession.session_start.desc(),
            RadiusActiveSession.id,
        )
    ).all()
    seen: set[object] = set()
    ordered: list[object] = []
    for nas_device_id in rows:
        if nas_device_id is None or nas_device_id in seen:
            continue
        seen.add(nas_device_id)
        ordered.append(nas_device_id)
    return tuple(ordered)


def recent_nas_history_by_subscription(
    db: Session,
    subscription_ids,
    *,
    since: datetime,
) -> dict[object, SubscriptionNasHistory]:
    """Aggregate recent accounting NAS evidence without exposing session rows."""
    normalized_ids = tuple(coerce_uuid(value) for value in subscription_ids)
    if not normalized_ids:
        return {}
    cutoff = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    recency = _accounting_recency_expression()
    resolved_nas_id = func.coalesce(
        RadiusAccountingSession.nas_device_id,
        RadiusClient.nas_device_id,
    )
    rows = db.execute(
        select(
            RadiusAccountingSession.subscription_id,
            resolved_nas_id.label("nas_device_id"),
            func.count(RadiusAccountingSession.id).label("session_count"),
            func.max(recency).label("last_seen_at"),
        )
        .outerjoin(
            RadiusClient,
            RadiusClient.id == RadiusAccountingSession.radius_client_id,
        )
        .where(RadiusAccountingSession.subscription_id.in_(normalized_ids))
        .where(resolved_nas_id.is_not(None))
        .where(recency >= cutoff)
        .group_by(RadiusAccountingSession.subscription_id, resolved_nas_id)
        .order_by(RadiusAccountingSession.subscription_id, resolved_nas_id)
    ).all()
    grouped: dict[object, list[HistoricalNasTarget]] = {}
    for subscription_id, nas_device_id, session_count, last_seen_at in rows:
        if subscription_id is None or nas_device_id is None or last_seen_at is None:
            continue
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=UTC)
        grouped.setdefault(subscription_id, []).append(
            HistoricalNasTarget(
                nas_device_id=nas_device_id,
                session_count=int(session_count or 0),
                last_seen_at=last_seen_at.astimezone(UTC),
            )
        )
    return {
        subscription_id: SubscriptionNasHistory(
            subscription_id=subscription_id,
            targets=tuple(
                sorted(
                    targets,
                    key=lambda target: (
                        -target.last_seen_at.timestamp(),
                        str(target.nas_device_id),
                    ),
                )
            ),
        )
        for subscription_id, targets in grouped.items()
    }


def open_accounting_session_query(db: Session):
    """Base query for open imported accounting-session mirrors.

    ``RadiusAccountingSession`` is a lagging accounting mirror, not the
    canonical live view. Some screens still need its framed IP/session metadata;
    centralizing that query keeps "open session" semantics consistent while the
    live ``radius_active_sessions`` view remains the online-now SOT.
    """
    return db.query(RadiusAccountingSession).filter(
        RadiusAccountingSession.session_end.is_(None),
        RadiusAccountingSession.status_type != AccountingStatus.stop,
    )


def order_open_accounting_sessions_newest(query):
    return query.order_by(
        RadiusAccountingSession.last_update_at.desc().nullslast(),
        RadiusAccountingSession.session_start.desc().nullslast(),
        RadiusAccountingSession.created_at.desc(),
    )


def latest_open_accounting_session_for_subscription(
    db: Session,
    subscription_id,
    *,
    access_credential_id=None,
) -> RadiusAccountingSession | None:
    query = open_accounting_session_query(db)
    if access_credential_id is not None:
        from sqlalchemy import or_

        query = query.filter(
            or_(
                RadiusAccountingSession.subscription_id == coerce_uuid(subscription_id),
                RadiusAccountingSession.access_credential_id
                == coerce_uuid(access_credential_id),
            )
        )
    else:
        query = query.filter(
            RadiusAccountingSession.subscription_id == coerce_uuid(subscription_id)
        )
    return order_open_accounting_sessions_newest(query).first()


def latest_open_accounting_sessions_by_subscription(
    db: Session,
    subscription_ids,
) -> dict[object, RadiusAccountingSession]:
    ids = [coerce_uuid(subscription_id) for subscription_id in subscription_ids]
    if not ids:
        return {}
    rows = (
        open_accounting_session_query(db)
        .filter(RadiusAccountingSession.subscription_id.in_(ids))
        .order_by(
            RadiusAccountingSession.subscription_id.asc(),
            RadiusAccountingSession.last_update_at.desc().nullslast(),
            RadiusAccountingSession.session_start.desc().nullslast(),
            RadiusAccountingSession.created_at.desc(),
        )
        .all()
    )
    sessions: dict[object, RadiusAccountingSession] = {}
    for row in rows:
        if row.subscription_id is not None and row.subscription_id not in sessions:
            sessions[row.subscription_id] = row
    return sessions


def live_framed_ips_by_subscription(db: Session, subscription_ids) -> dict:
    sessions = latest_open_accounting_sessions_by_subscription(db, subscription_ids)
    return {
        subscription_id: session.framed_ip_address
        for subscription_id, session in sessions.items()
        if session.framed_ip_address
    }
