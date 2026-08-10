"""Service and usage flows for customer portal."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, TypedDict
from uuid import UUID


class VacationHoldUsage(TypedDict):
    """Usage stats for vacation holds."""

    holds_this_year: int
    last_hold_date: datetime | None
    days_since_last: int | None


from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.bandwidth import BandwidthSample
from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceStatus, OntAssignment, OntUnit
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.usage import RadiusAccountingSession, UsageRecord
from app.services import catalog as catalog_service
from app.services import customer_portal_context
from app.services import provisioning as provisioning_service
from app.services.common import coerce_uuid
from app.services.common import validate_enum as _validate_enum
from app.services.customer_context import (
    optional_customer_account_id,
    resolve_customer_context,
)
from app.services.customer_network_context import resolve_active_customer_ont_assignment
from app.services.customer_portal_flow_changes import (
    get_offer_price_summary,
    get_plan_change_copy,
)
from app.services.customer_portal_flow_common import (
    _compute_total_pages,
)
from app.services.network.ont_desired_config import (
    desired_config,
    get_desired_config_value,
)
from app.services.portal_account_health import build_portal_account_health
from app.services.provisioning_lifecycle import latest_readiness

logger = logging.getLogger(__name__)

_PORTAL_VISIBLE_SERVICE_STATUSES = [
    SubscriptionStatus.pending,
    SubscriptionStatus.active,
    SubscriptionStatus.blocked,
    SubscriptionStatus.suspended,
    SubscriptionStatus.stopped,
    SubscriptionStatus.disabled,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
]


def _get_fup_status(
    db: Session, offer_id: str | None, subscription_id: str
) -> dict | None:
    """Get FUP status for a subscription's offer, if a policy exists."""
    if not offer_id:
        return None
    try:
        offer = db.get(CatalogOffer, coerce_uuid(offer_id))
        if str(getattr(offer, "plan_family", "") or "").strip().lower() == "unlimited":
            return None

        from app.services.fup import FupPolicies

        policy = FupPolicies.get_by_offer(db, offer_id)
        if not policy or not policy.is_active:
            return None

        # Authoritative cycle usage = the same QuotaBucket the mobile app and
        # FUP enforcement key off, so the web portal can't disagree with them.
        # (Was a divergent UsageRecord 30-day rolling sum.)
        from app.services.fup import _threshold_gb
        from app.services.usage_summary import (
            _current_bucket_used_gb,
            _fup_warn_ratio,
            _nearest_enforcement_rule,
        )

        now = datetime.now(UTC)
        usage_gb = _current_bucket_used_gb(db, coerce_uuid(subscription_id))
        if usage_gb is None:
            # No rated bucket on file — fall back to the rolling-window estimate
            # (accounting records, then a bandwidth-sample volume estimate).
            subscription = db.get(Subscription, coerce_uuid(subscription_id))
            period_start = now - timedelta(days=30)
            if subscription and subscription.next_billing_at:
                nba = _as_utc(subscription.next_billing_at) or now
                period_start = nba - timedelta(days=30)
            usage_gb = _usage_total_gb(
                db,
                subscription_id=subscription_id,
                start_at=period_start,
                end_at=now,
            )
            if usage_gb <= 0:
                rows = (
                    db.query(
                        func.avg(BandwidthSample.rx_bps).label("rx"),
                        func.avg(BandwidthSample.tx_bps).label("tx"),
                    )
                    .filter(
                        BandwidthSample.subscription_id == coerce_uuid(subscription_id),
                        BandwidthSample.sample_at >= period_start,
                        BandwidthSample.sample_at <= now,
                    )
                    .first()
                )
                if rows and (rows.rx or rows.tx):
                    span_seconds = max(0, (now - period_start).total_seconds())
                    total_bytes = (
                        (float(rows.rx or 0) + float(rows.tx or 0)) / 8.0
                    ) * span_seconds
                    usage_gb = total_bytes / (1024**3)

        # Allowance = the nearest (lowest-threshold) enforcement rule — the one
        # the customer hits first, matching fup_summary + enforcement. (Was
        # max() across rules, which over-reported headroom.)
        active_rules = [r for r in (policy.rules or []) if r.is_active]
        nearest = _nearest_enforcement_rule(db, offer_id)
        if nearest is not None:
            allowance_gb = _threshold_gb(nearest)
        elif active_rules:
            allowance_gb = min(_threshold_gb(rule) for rule in active_rules)
        else:
            allowance_gb = None

        usage_pct = 0.0
        if allowance_gb and allowance_gb > 0:
            usage_pct = min(100.0, (usage_gb / allowance_gb) * 100)

        # Warning threshold from the usage_warning_thresholds setting (the same
        # knob the 80%-notification and the mobile banner use), not a hardcode.
        warn_pct = _fup_warn_ratio(db) * 100
        status_level = "normal"
        if usage_pct >= 100:
            status_level = "exceeded"
        elif usage_pct >= warn_pct:
            status_level = "warning"

        # Policy doesn't have a name field; use offer or generic label
        policy_label = "Fair Usage Policy"
        if hasattr(policy, "offer") and policy.offer and hasattr(policy.offer, "name"):
            policy_label = f"FUP — {policy.offer.name}"

        return {
            "policy_name": policy_label,
            "usage_gb": round(usage_gb, 2),
            "allowance_gb": round(allowance_gb, 2) if allowance_gb else None,
            "usage_pct": round(usage_pct, 1),
            "status_level": status_level,
            "rules_count": len(active_rules),
        }
    except Exception as exc:
        logger.warning("Failed to get FUP status for offer %s: %s", offer_id, exc)
        return None


def _usage_total_gb(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> float:
    total = (
        db.query(func.coalesce(func.sum(UsageRecord.total_gb), 0))
        .filter(
            UsageRecord.subscription_id == coerce_uuid(subscription_id),
            UsageRecord.recorded_at >= start_at,
            UsageRecord.recorded_at <= end_at,
        )
        .scalar()
    )
    return float(total or 0)


def _subscription_uuid_list(
    subscription_id: str | UUID | None = None,
    subscription_ids: Sequence[str | UUID] | None = None,
) -> list[UUID]:
    raw_ids: list[str | UUID] = []
    if subscription_ids is not None:
        raw_ids.extend(subscription_ids)
    elif subscription_id is not None:
        raw_ids.append(subscription_id)

    seen: set[UUID] = set()
    result: list[UUID] = []
    for raw_id in raw_ids:
        uuid_value = coerce_uuid(raw_id)
        if uuid_value in seen:
            continue
        seen.add(uuid_value)
        result.append(uuid_value)
    return result


def _subscription_filter(column, subscription_ids: Sequence[UUID]):
    if len(subscription_ids) == 1:
        return column == subscription_ids[0]
    return column.in_(subscription_ids)


def _resolve_usage_subscription_ids(db: Session, customer: dict) -> list[str]:
    """Resolve every subscription whose usage belongs to the customer account.

    Usage is account-level in the portal/admin UI. A session may carry a specific
    subscription for live service actions, but historical usage should include
    every subscription attached to the resolved account so prior/canceled service
    records remain part of the customer's daily/monthly totals.
    """
    account_id, session_subscription_id = (
        customer_portal_context.resolve_customer_account(customer, db)
    )
    account_id_str = str(account_id) if account_id else None

    if not account_id_str and session_subscription_id:
        subscription = db.get(Subscription, coerce_uuid(session_subscription_id))
        if subscription and subscription.subscriber_id:
            account_id_str = str(subscription.subscriber_id)

    if not account_id_str:
        return [str(session_subscription_id)] if session_subscription_id else []

    subscriptions = (
        db.query(Subscription.id)
        .filter(Subscription.subscriber_id == coerce_uuid(account_id_str))
        .order_by(Subscription.created_at.desc())
        .all()
    )
    return [str(row[0]) for row in subscriptions if row[0] is not None]


def _daily_usage_records(
    db: Session,
    *,
    subscription_id: str | UUID | None = None,
    subscription_ids: Sequence[str | UUID] | None = None,
    start_at: datetime,
    end_at: datetime,
) -> dict[Any, float]:
    subscription_uuid_list = _subscription_uuid_list(subscription_id, subscription_ids)
    if not subscription_uuid_list:
        return {}

    bucket = func.date_trunc("day", UsageRecord.recorded_at)
    rows = (
        db.query(
            bucket.label("bucket_start"),
            func.coalesce(func.sum(UsageRecord.total_gb), 0).label("total_gb"),
        )
        .filter(
            _subscription_filter(UsageRecord.subscription_id, subscription_uuid_list),
            UsageRecord.recorded_at >= start_at,
            UsageRecord.recorded_at <= end_at,
        )
        .group_by(bucket)
        .all()
    )
    result: dict[Any, float] = {}
    for row in rows:
        bucket_start = _as_utc(row.bucket_start)
        if bucket_start is None:
            continue
        result[bucket_start.date()] = float(row.total_gb or 0)
    return result


def _daily_usage_breakdown_records(
    db: Session,
    *,
    subscription_id: str | UUID | None = None,
    subscription_ids: Sequence[str | UUID] | None = None,
    start_at: datetime,
    end_at: datetime,
) -> dict[Any, tuple[float, float, float]]:
    subscription_uuid_list = _subscription_uuid_list(subscription_id, subscription_ids)
    if not subscription_uuid_list:
        return {}

    bucket = func.date_trunc("day", UsageRecord.recorded_at)
    rows = (
        db.query(
            bucket.label("bucket_start"),
            func.coalesce(func.sum(UsageRecord.input_gb), 0).label("download_gb"),
            func.coalesce(func.sum(UsageRecord.output_gb), 0).label("upload_gb"),
            func.coalesce(func.sum(UsageRecord.total_gb), 0).label("total_gb"),
        )
        .filter(
            _subscription_filter(UsageRecord.subscription_id, subscription_uuid_list),
            UsageRecord.recorded_at >= start_at,
            UsageRecord.recorded_at <= end_at,
        )
        .group_by(bucket)
        .all()
    )
    result: dict[Any, tuple[float, float, float]] = {}
    for row in rows:
        bucket_start = _as_utc(row.bucket_start)
        if bucket_start is None:
            continue
        result[bucket_start.date()] = (
            float(row.download_gb or 0),
            float(row.upload_gb or 0),
            float(row.total_gb or 0),
        )
    return result


def _daily_bandwidth_averages(
    db: Session,
    *,
    subscription_id: str | UUID | None = None,
    subscription_ids: Sequence[str | UUID] | None = None,
    start_at: datetime,
    end_at: datetime,
) -> dict[Any, tuple[float, float]]:
    subscription_uuid_list = _subscription_uuid_list(subscription_id, subscription_ids)
    if not subscription_uuid_list:
        return {}

    bucket = func.date_trunc("day", BandwidthSample.sample_at)
    rows = (
        db.query(
            BandwidthSample.subscription_id,
            bucket.label("bucket_start"),
            func.avg(BandwidthSample.rx_bps).label("rx_bps"),
            func.avg(BandwidthSample.tx_bps).label("tx_bps"),
        )
        .filter(
            _subscription_filter(
                BandwidthSample.subscription_id, subscription_uuid_list
            ),
            BandwidthSample.sample_at >= start_at,
            BandwidthSample.sample_at <= end_at,
        )
        .group_by(BandwidthSample.subscription_id, bucket)
        .all()
    )
    result: dict[Any, tuple[float, float]] = {}
    for row in rows:
        bucket_start = _as_utc(row.bucket_start)
        if bucket_start is None:
            continue
        existing_rx, existing_tx = result.get(bucket_start.date(), (0.0, 0.0))
        result[bucket_start.date()] = (
            existing_rx + float(row.rx_bps or 0),
            existing_tx + float(row.tx_bps or 0),
        )
    return result


def _get_pppoe_credentials(db: Session, subscriber_id: str | None) -> dict | None:
    """Get PPPoE credentials for a subscriber, if any exist."""
    if not subscriber_id:
        return None
    try:
        from app.models.catalog import AccessCredential, ConnectionType

        cred = (
            db.query(AccessCredential)
            .filter(
                AccessCredential.subscriber_id == coerce_uuid(subscriber_id),
                AccessCredential.connection_type == ConnectionType.pppoe,
                AccessCredential.is_active.is_(True),
            )
            .first()
        )
        if not cred:
            return None
        return {
            "username": cred.username,
            "has_password": bool(cred.secret_hash),
        }
    except Exception as exc:
        logger.warning(
            "Failed to get PPPoE credentials for subscriber %s: %s", subscriber_id, exc
        )
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _usage_period_bounds(
    period: str,
    *,
    activated_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    if str(period or "").lower() == "last":
        end = now - timedelta(days=30)
        start = end - timedelta(days=30)
        return start, end
    active_since = _as_utc(activated_at) or (now - timedelta(days=30))
    return min(active_since, now), now


def _resolve_usage_subscription_id(db: Session, customer: dict) -> str | None:
    subscription_ids = _resolve_usage_subscription_ids(db, customer)
    if subscription_ids:
        return subscription_ids[0]

    subscription_id = resolve_customer_context(db, customer).subscription_id
    if subscription_id:
        return str(subscription_id)

    account_id, _ = customer_portal_context.resolve_customer_account(customer, db)
    if not account_id:
        return None
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.subscriber_id == UUID(str(account_id)),
            Subscription.status.in_(_PORTAL_VISIBLE_SERVICE_STATUSES),
        )
        .order_by(Subscription.created_at.desc())
        .first()
    )
    return str(subscription.id) if subscription else None


def _daily_bandwidth_usage(
    db: Session,
    *,
    subscription_id: str | UUID | None = None,
    subscription_ids: Sequence[str | UUID] | None = None,
    start_at: datetime,
    end_at: datetime,
    page: int,
    per_page: int,
) -> tuple[list[Any], int]:
    daily_records = _daily_bandwidth_usage_records(
        db,
        subscription_id=subscription_id,
        subscription_ids=subscription_ids,
        start_at=start_at,
        end_at=end_at,
    )
    total = len(daily_records)
    page_start = (page - 1) * per_page
    page_end = page_start + per_page
    records = daily_records[page_start:page_end]

    return records, int(total)


def _daily_bandwidth_usage_records(
    db: Session,
    *,
    subscription_id: str | UUID | None = None,
    subscription_ids: Sequence[str | UUID] | None = None,
    start_at: datetime,
    end_at: datetime,
) -> list[Any]:
    subscription_uuid_list = _subscription_uuid_list(subscription_id, subscription_ids)
    if not subscription_uuid_list:
        return []

    by_day_usage = _daily_usage_breakdown_records(
        db,
        subscription_ids=subscription_uuid_list,
        start_at=start_at,
        end_at=end_at,
    )

    start_day_dt = _as_utc(start_at) or start_at
    end_day_dt = _as_utc(end_at) or end_at
    start_day = start_day_dt.date()
    end_day = end_day_dt.date()
    total_days = max(1, (end_day - start_day).days + 1)
    radius_by_day = (
        _daily_radius_accounting_usage(
            db,
            subscription_ids=subscription_uuid_list,
            start_at=start_at,
            end_at=end_at,
        )
        if len(by_day_usage) < total_days
        else {}
    )
    bandwidth_by_day = (
        _daily_bandwidth_averages(
            db,
            subscription_ids=subscription_uuid_list,
            start_at=start_at,
            end_at=end_at,
        )
        if len(by_day_usage) < total_days
        else {}
    )

    daily_records: list[Any] = []
    day = end_day
    while day >= start_day:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        effective_start = max(start_at, day_start)
        effective_end = min(end_at, day_end)
        span_seconds = max(0.0, (effective_end - effective_start).total_seconds())

        usage_breakdown = by_day_usage.get(day)
        if usage_breakdown is not None:
            download_gb, upload_gb, total_gb = usage_breakdown
        else:
            if day in bandwidth_by_day:
                rx_bps, tx_bps = bandwidth_by_day[day]
                # rx = NAS ingress = subscriber upload; tx = egress = download.
                download_gb = ((tx_bps / 8.0) * span_seconds) / (1024**3)
                upload_gb = ((rx_bps / 8.0) * span_seconds) / (1024**3)
                total_gb = download_gb + upload_gb
            else:
                # Fallback for days with no sampled bandwidth history. RADIUS
                # accounting is coarser, but it preserves real historical
                # usage instead of rendering prior active days as empty.
                download_gb, upload_gb, total_gb = radius_by_day.get(
                    day,
                    (0.0, 0.0, 0.0),
                )

        daily_records.append(
            SimpleNamespace(
                recorded_at=day_start,
                usage_type="Daily Usage",
                amount=total_gb,
                usage_amount=total_gb,
                download_amount=download_gb,
                upload_amount=upload_gb,
                unit="GB",
                description=f"Total usage for {day.isoformat()}",
            )
        )
        day -= timedelta(days=1)

    return daily_records


def _daily_radius_accounting_usage(
    db: Session,
    *,
    subscription_id: str | UUID | None = None,
    subscription_ids: Sequence[str | UUID] | None = None,
    start_at: datetime,
    end_at: datetime,
) -> dict[Any, tuple[float, float, float]]:
    subscription_uuid_list = _subscription_uuid_list(subscription_id, subscription_ids)
    if not subscription_uuid_list:
        return {}

    accounting_day = func.date_trunc(
        "day",
        func.coalesce(
            RadiusAccountingSession.session_end,
            RadiusAccountingSession.session_start,
        ),
    )
    accounting_time = func.coalesce(
        RadiusAccountingSession.session_end,
        RadiusAccountingSession.session_start,
    )

    rows = (
        db.query(
            accounting_day.label("bucket_start"),
            func.sum(func.coalesce(RadiusAccountingSession.input_octets, 0)).label(
                "download_octets"
            ),
            func.sum(func.coalesce(RadiusAccountingSession.output_octets, 0)).label(
                "upload_octets"
            ),
        )
        .filter(
            _subscription_filter(
                RadiusAccountingSession.subscription_id,
                subscription_uuid_list,
            )
        )
        .filter(accounting_time >= start_at)
        .filter(accounting_time <= end_at)
        .group_by(accounting_day)
        .all()
    )

    usage_by_day: dict[Any, tuple[float, float, float]] = {}
    for row in rows:
        bucket_start = _as_utc(getattr(row, "bucket_start", None))
        if bucket_start is None:
            continue
        download_gb = float(getattr(row, "download_octets", 0) or 0) / (1024**3)
        upload_gb = float(getattr(row, "upload_octets", 0) or 0) / (1024**3)
        usage_by_day[bucket_start.date()] = (
            download_gb,
            upload_gb,
            download_gb + upload_gb,
        )
    return usage_by_day


def _serialize_usage_chart_records(records: list[Any]) -> list[dict[str, Any]]:
    ordered_records = sorted(
        records,
        key=lambda record: (
            _as_utc(getattr(record, "recorded_at", None))
            or datetime.min.replace(tzinfo=UTC)
        ),
    )

    chart_records: list[dict[str, Any]] = []
    for record in ordered_records:
        recorded_at = _as_utc(getattr(record, "recorded_at", None))
        if recorded_at is None:
            continue
        amount = float(
            getattr(record, "amount", None) or getattr(record, "usage_amount", 0) or 0
        )
        chart_records.append(
            {
                "label": recorded_at.strftime("%b %d"),
                "full_label": recorded_at.strftime("%b %d, %Y"),
                "value": round(amount, 2),
                "download_value": round(
                    float(getattr(record, "download_amount", 0) or 0),
                    2,
                ),
                "upload_value": round(
                    float(getattr(record, "upload_amount", 0) or 0),
                    2,
                ),
                "unit": str(getattr(record, "unit", None) or "GB"),
            }
        )
    return chart_records


def _usage_summary_stats(
    db: Session,
    *,
    subscription_id: str | UUID | None = None,
    subscription_ids: Sequence[str | UUID] | None = None,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, float]:
    subscription_uuid_list = _subscription_uuid_list(subscription_id, subscription_ids)
    if not subscription_uuid_list:
        return {
            "average_daily_usage_gb": 0.0,
            "average_speed_mbps": 0.0,
            "average_download_mbps": 0.0,
            "average_upload_mbps": 0.0,
        }

    avg_rows = (
        db.query(
            BandwidthSample.subscription_id,
            func.avg(BandwidthSample.rx_bps).label("rx_bps"),
            func.avg(BandwidthSample.tx_bps).label("tx_bps"),
        )
        .filter(
            _subscription_filter(
                BandwidthSample.subscription_id, subscription_uuid_list
            ),
            BandwidthSample.sample_at >= start_at,
            BandwidthSample.sample_at <= end_at,
        )
        .group_by(BandwidthSample.subscription_id)
        .all()
    )
    avg_rx_bps = sum(float(row.rx_bps or 0) for row in avg_rows)
    avg_tx_bps = sum(float(row.tx_bps or 0) for row in avg_rows)

    bucket = func.date_trunc("day", BandwidthSample.sample_at)
    rows = (
        db.query(
            BandwidthSample.subscription_id,
            bucket.label("bucket_start"),
            func.avg(BandwidthSample.rx_bps).label("rx_bps"),
            func.avg(BandwidthSample.tx_bps).label("tx_bps"),
        )
        .filter(
            _subscription_filter(
                BandwidthSample.subscription_id, subscription_uuid_list
            ),
            BandwidthSample.sample_at >= start_at,
            BandwidthSample.sample_at <= end_at,
        )
        .group_by(BandwidthSample.subscription_id, bucket)
        .all()
    )

    by_day: dict[Any, tuple[float, float]] = {}
    for row in rows:
        bucket_start = _as_utc(row.bucket_start)
        if bucket_start is None:
            continue
        existing_rx, existing_tx = by_day.get(bucket_start.date(), (0.0, 0.0))
        by_day[bucket_start.date()] = (
            existing_rx + float(row.rx_bps or 0),
            existing_tx + float(row.tx_bps or 0),
        )

    start_day_dt = _as_utc(start_at) or start_at
    end_day_dt = _as_utc(end_at) or end_at
    start_day = start_day_dt.date()
    end_day = end_day_dt.date()
    total_days = max(1, (end_day - start_day).days + 1)
    total_bytes = 0.0
    day = start_day
    while day <= end_day:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        effective_start = max(start_at, day_start)
        effective_end = min(end_at, day_end)
        span_seconds = max(0.0, (effective_end - effective_start).total_seconds())
        rx_bps, tx_bps = by_day.get(day, (0.0, 0.0))
        total_bytes += ((rx_bps + tx_bps) / 8.0) * span_seconds
        day += timedelta(days=1)

    avg_daily_usage_gb = (total_bytes / (1024**3)) / total_days
    # rx_bps/tx_bps are stored from the NAS-interface perspective: rx = NAS
    # ingress = subscriber UPLOAD, tx = NAS egress = subscriber DOWNLOAD. So the
    # subscriber-facing download is avg_tx and upload is avg_rx.
    average_download_mbps = float(avg_tx_bps or 0) / 1_000_000
    average_upload_mbps = float(avg_rx_bps or 0) / 1_000_000
    average_speed_mbps = average_download_mbps + average_upload_mbps

    return {
        "average_daily_usage_gb": avg_daily_usage_gb,
        "average_speed_mbps": average_speed_mbps,
        "average_download_mbps": average_download_mbps,
        "average_upload_mbps": average_upload_mbps,
    }


def get_usage_page(
    db: Session,
    customer: dict,
    period: str = "current",
    page: int = 1,
    per_page: int = 25,
    allow_postgres_fallback: bool = True,
) -> dict:
    """Get usage page data for the customer portal."""
    subscription_ids = _resolve_usage_subscription_ids(db, customer)
    subscription_id_str = subscription_ids[0] if subscription_ids else None

    empty_result: dict[str, Any] = {
        "usage_records": [],
        "chart_records": [],
        "period": period,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
        "usage_summary": {
            "average_daily_usage_gb": 0.0,
            "average_speed_mbps": 0.0,
            "average_download_mbps": 0.0,
            "average_upload_mbps": 0.0,
        },
        "fup_status": None,
        "usage_source": "none",
        "has_subscription": False,
        "period_total_gb": 0.0,
    }
    if not subscription_id_str:
        return empty_result

    subscription_uuid_list = _subscription_uuid_list(subscription_ids=subscription_ids)
    subscriptions = (
        db.query(Subscription)
        .filter(_subscription_filter(Subscription.id, subscription_uuid_list))
        .all()
    )
    subscription = customer_portal_context.resolve_customer_subscription(db, customer)
    if not subscription:
        subscription = db.get(Subscription, coerce_uuid(subscription_id_str))
    activated_at = None
    activation_candidates = [
        candidate
        for candidate in (
            _as_utc(getattr(item, "start_at", None))
            or _as_utc(getattr(item, "created_at", None))
            for item in subscriptions
        )
        if candidate is not None
    ]
    if activation_candidates:
        activated_at = min(activation_candidates)
    start_at, end_at = _usage_period_bounds(period, activated_at=activated_at)

    usage_source = "postgres"
    chart_source_records: list[Any] = []
    period_total_gb = 0.0
    if not allow_postgres_fallback:
        return {
            **empty_result,
            "period": period,
            "page": page,
            "per_page": per_page,
            "usage_source": "unavailable",
            "has_subscription": True,
        }
    else:
        chart_source_records = _daily_bandwidth_usage_records(
            db,
            subscription_ids=subscription_ids,
            start_at=start_at,
            end_at=end_at,
        )
        total = len(chart_source_records)
        page_start = (page - 1) * per_page
        page_end = page_start + per_page
        usage_records = chart_source_records[page_start:page_end]
        # Period total is summed over the COMPLETE record set, not the paginated
        # page — the same per-row amount the template used to sum, moved to the
        # owner so the figure is right on every page.
        period_total_gb = sum(
            float(getattr(r, "amount", None) or getattr(r, "usage_amount", 0) or 0)
            for r in chart_source_records
        )
        usage_summary = _usage_summary_stats(
            db,
            subscription_ids=subscription_ids,
            start_at=start_at,
            end_at=end_at,
        )

    # Resolve FUP status from subscriber's primary subscription offer
    fup_status = None
    if subscription and allow_postgres_fallback:
        fup_status = _get_fup_status(
            db,
            str(subscription.offer_id) if subscription.offer_id else None,
            str(subscription.id),
        )

    return {
        "usage_records": usage_records,
        "chart_records": _serialize_usage_chart_records(chart_source_records),
        "period": period,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
        "usage_summary": usage_summary,
        "fup_status": fup_status,
        "usage_source": usage_source,
        "has_subscription": True,
        "period_total_gb": period_total_gb,
    }


def get_usage_history(db: Session, customer: dict, months: int = 12) -> dict:
    """Monthly long-history usage for the portal usage page.

    Aggregated from the historical daily rollup (Splynx ``traffic_counter``
    backfill via ``SubscriberDailyUsage``), which reaches years further back
    than the live per-session / bandwidth sources. Returns Chart.js-ready
    monthly records (same shape as ``_serialize_usage_chart_records`` so the
    existing records-chart Alpine component can render them) plus the monthly
    average and the lifetime total.
    """
    empty: dict[str, Any] = {
        "has_history": False,
        "chart_records": [],
        "average_gb": 0.0,
        "total_gb": 0.0,
        "since": None,
        "months_shown": 0,
    }
    subscription_ids = _resolve_usage_subscription_ids(db, customer)
    subscription_uuid_list = _subscription_uuid_list(subscription_ids=subscription_ids)
    if not subscription_uuid_list:
        return empty

    from app.models.usage import SubscriberDailyUsage

    rows = (
        db.query(
            SubscriberDailyUsage.usage_date,
            SubscriberDailyUsage.upload_bytes,
            SubscriberDailyUsage.download_bytes,
        )
        .filter(
            _subscription_filter(
                SubscriberDailyUsage.subscription_id,
                subscription_uuid_list,
            )
        )
        .order_by(SubscriberDailyUsage.usage_date)
        .all()
    )
    if not rows:
        return empty

    gb = 1024**3
    # (year, month) -> [download_bytes, upload_bytes]
    by_month: dict[tuple[int, int], list[int]] = {}
    total_bytes = 0
    for usage_date, up, down in rows:
        up_i, down_i = int(up or 0), int(down or 0)
        total_bytes += up_i + down_i
        agg = by_month.setdefault((usage_date.year, usage_date.month), [0, 0])
        agg[0] += down_i
        agg[1] += up_i

    month_abbr = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]  # fmt: skip
    ordered = sorted(by_month.items())
    chart = ordered[-months:] if len(ordered) > months else ordered
    chart_records: list[dict[str, Any]] = []
    monthly_gb: list[float] = []
    for (y, m), (down, up) in chart:
        value = round((down + up) / gb, 2)
        monthly_gb.append(value)
        chart_records.append(
            {
                "label": month_abbr[m - 1],
                "full_label": f"{month_abbr[m - 1]} {y}",
                "value": value,
                "download_value": round(down / gb, 2),
                "upload_value": round(up / gb, 2),
                "unit": "GB",
            }
        )
    avg_gb = round(sum(monthly_gb) / len(monthly_gb), 2) if monthly_gb else 0.0
    (first_y, first_m), _ = ordered[0]
    return {
        "has_history": True,
        "chart_records": chart_records,
        "average_gb": avg_gb,
        "total_gb": round(total_bytes / gb, 2),
        "since": f"{month_abbr[first_m - 1]} {first_y}",
        "months_shown": len(chart_records),
    }


def get_services_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get services page data for the customer portal."""
    account_id = optional_customer_account_id(db, customer)
    account_id_str = str(account_id) if account_id else None

    empty_result: dict[str, Any] = {
        "services": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str:
        return empty_result

    query = (
        db.query(Subscription)
        .options(
            selectinload(Subscription.offer),
            selectinload(Subscription.offer_version),
        )
        .filter(
            Subscription.subscriber_id == coerce_uuid(account_id_str),
            Subscription.status.in_(_PORTAL_VISIBLE_SERVICE_STATUSES),
        )
    )
    if status:
        query = query.filter(
            Subscription.status == _validate_enum(status, SubscriptionStatus, "status")
        )

    services = (
        query.order_by(Subscription.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    stmt = select(func.count(Subscription.id)).where(
        Subscription.subscriber_id == coerce_uuid(account_id_str),
        Subscription.status.in_(_PORTAL_VISIBLE_SERVICE_STATUSES),
    )
    if status:
        stmt = stmt.where(
            Subscription.status == _validate_enum(status, SubscriptionStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "services": services,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_service_detail(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Get service detail data for the customer portal."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return None

    account_id = optional_customer_account_id(db, customer)
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return None

    current_offer = None
    if subscription.offer_id:
        current_offer = db.get(CatalogOffer, subscription.offer_id)

    copy = get_plan_change_copy(subscription)

    fup_status = _get_fup_status(
        db, str(current_offer.id) if current_offer else None, subscription_id
    )
    pppoe_creds = _get_pppoe_credentials(
        db, str(subscription.subscriber_id) if subscription else None
    )
    customer_assignment = _resolve_customer_subscription_assignment(db, subscription)
    customer_ont = customer_assignment.ont_unit if customer_assignment else None
    customer_cpe = (
        db.query(CPEDevice)
        .filter(
            CPEDevice.subscription_id == subscription.id,
            CPEDevice.status == DeviceStatus.active,
        )
        .order_by(CPEDevice.created_at.desc())
        .first()
    )
    uisp_target = None
    if customer_ont is not None and customer_ont.uisp_device_id:
        uisp_target = ("ont", customer_ont.id)
    elif customer_cpe is not None and customer_cpe.uisp_device_id:
        uisp_target = ("cpe", customer_cpe.id)
    uisp_intent = None
    if uisp_target is not None:
        from app.models.uisp_control import UispDeviceIntent, UispIntentTargetType

        uisp_intent = (
            db.query(UispDeviceIntent)
            .filter(
                UispDeviceIntent.target_type == UispIntentTargetType(uisp_target[0]),
                UispDeviceIntent.target_id == uisp_target[1],
            )
            .one_or_none()
        )
    customer_ont_is_uisp = bool(customer_ont and customer_ont.uisp_device_id)
    account_health = build_portal_account_health(
        db,
        coerce_uuid(account_id),
    ).for_subscription(subscription.id)

    # Renewal context: show renewal banner when contract nearing expiration
    renewal_context: dict[str, Any] = {"show_renewal": False}
    if subscription.end_at:
        days_remaining = (subscription.end_at - datetime.now(UTC)).days
        if days_remaining <= 30 and subscription.status in (
            SubscriptionStatus.active,
            SubscriptionStatus.suspended,
        ):
            renewal_context = {
                "show_renewal": True,
                "days_remaining": max(days_remaining, 0),
                "end_date": subscription.end_at,
                "offer_id": str(current_offer.id) if current_offer else None,
                "offer_name": current_offer.name if current_offer else "Service",
            }

    # Billing mode for display
    billing_mode = "postpaid"
    if subscription.billing_mode:
        billing_mode = subscription.billing_mode.value

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "current_offer_summary": get_offer_price_summary(current_offer),
        "account_health": account_health,
        "fup_status": fup_status,
        "pppoe_credentials": pppoe_creds,
        "customer_ont": customer_ont,
        "customer_cpe": customer_cpe,
        "customer_wifi_ssid": _customer_wifi_ssid(customer_ont),
        "can_reboot_ont": bool(
            customer_ont is not None
            and not customer_ont_is_uisp
            and subscription.status == SubscriptionStatus.active
        ),
        "can_update_wifi": bool(
            customer_ont is not None
            and not customer_ont_is_uisp
            and subscription.status == SubscriptionStatus.active
        ),
        "uisp_control_status": (
            uisp_intent.status.value if uisp_intent is not None else None
        ),
        "billing_mode": billing_mode,
        "billing_mode_display": "Prepaid" if billing_mode == "prepaid" else "Postpaid",
        **renewal_context,
        **copy,
    }


def _customer_wifi_ssid(ont: OntUnit | None) -> str | None:
    """Return the customer-visible SSID from canonical ONT desired state."""
    if ont is None:
        return None
    value = get_desired_config_value(desired_config(ont), "wifi", "ssid")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_customer_subscription_ont(
    db: Session,
    subscription: Subscription,
) -> OntUnit | None:
    """Resolve the active ONT currently assigned to a subscription's subscriber."""
    assignment = _resolve_customer_subscription_assignment(db, subscription)
    return assignment.ont_unit if assignment else None


def _resolve_customer_subscription_assignment(
    db: Session,
    subscription: Subscription,
) -> OntAssignment | None:
    """Resolve the active ONT assignment for a subscription's subscriber."""
    return resolve_active_customer_ont_assignment(
        db,
        subscription.subscriber_id,
        subscription_id=subscription.id,
    )


def get_service_orders_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get service orders page data for the customer portal."""
    context = resolve_customer_context(db, customer)
    account_id = context.account_id or context.subscriber_id
    subscription_id = context.subscription_id
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    empty_result: dict[str, Any] = {
        "service_orders": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str and not subscription_id_str:
        return empty_result

    service_orders = provisioning_service.service_orders.list(
        db=db,
        subscriber_id=account_id_str,
        subscription_id=subscription_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = select(func.count(ServiceOrder.id))
    if account_id_str:
        stmt = stmt.where(ServiceOrder.subscriber_id == coerce_uuid(account_id_str))
    if subscription_id_str:
        stmt = stmt.where(
            ServiceOrder.subscription_id == coerce_uuid(subscription_id_str)
        )
    if status:
        stmt = stmt.where(
            ServiceOrder.status == _validate_enum(status, ServiceOrderStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "service_orders": service_orders,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_service_order_detail(
    db: Session,
    customer: dict,
    service_order_id: str,
) -> dict | None:
    """Get service order detail data for the customer portal."""
    context = resolve_customer_context(db, customer)
    account_id = context.account_id or context.subscriber_id
    subscription_id = context.subscription_id
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    service_order = provisioning_service.service_orders.get(
        db=db, entity_id=service_order_id
    )
    if not service_order:
        return None

    so_subscriber = str(getattr(service_order, "subscriber_id", ""))
    so_subscription = str(getattr(service_order, "subscription_id", ""))
    if not account_id_str and not subscription_id_str:
        return None
    if (account_id_str and so_subscriber != account_id_str) or (
        subscription_id_str and so_subscription != subscription_id_str
    ):
        return None

    appointments = provisioning_service.install_appointments.list(
        db=db,
        service_order_id=service_order_id,
        status=None,
        order_by="scheduled_start",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    provisioning_tasks = provisioning_service.provisioning_tasks.list(
        db=db,
        service_order_id=service_order_id,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    return {
        "service_order": service_order,
        "readiness": latest_readiness(db, service_order.id),
        "appointments": appointments,
        "provisioning_tasks": provisioning_tasks,
    }


def get_installation_detail(
    db: Session,
    customer: dict,
    appointment_id: str,
) -> dict | None:
    """Get installation appointment detail data for the customer portal."""
    context = resolve_customer_context(db, customer)
    account_id = context.account_id or context.subscriber_id
    subscription_id = context.subscription_id
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    appointment = provisioning_service.install_appointments.get(
        db=db, entity_id=appointment_id
    )
    if not appointment:
        return None

    service_order = provisioning_service.service_orders.get(
        db=db, entity_id=str(appointment.service_order_id)
    )
    if not service_order:
        return None

    so_subscriber = str(getattr(service_order, "subscriber_id", ""))
    so_subscription = str(getattr(service_order, "subscription_id", ""))
    if not account_id_str and not subscription_id_str:
        return None
    if (account_id_str and so_subscriber != account_id_str) or (
        subscription_id_str and so_subscription != subscription_id_str
    ):
        return None

    return {
        "appointment": appointment,
        "service_order": service_order,
    }


__all__ = [
    "get_usage_page",
    "get_services_page",
    "get_service_detail",
    "get_service_orders_page",
    "get_service_order_detail",
    "get_installation_detail",
    "get_suspend_page",
    "apply_service_suspend",
    "get_resume_page",
    "apply_service_resume",
]


def _get_vacation_hold_usage(
    db: Session,
    subscription_id: str,
) -> VacationHoldUsage:
    """Get vacation hold usage stats for a subscription this calendar year."""
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        resolve_vacation_hold_policy,
    )

    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if subscription is None:
        return {"holds_this_year": 0, "last_hold_date": None, "days_since_last": None}
    decision = resolve_vacation_hold_policy(
        db,
        subscription,
        command_kind=SubscriptionCommandKind.vacation_hold,
        requested_days=1,
    )

    return {
        "holds_this_year": decision.holds_this_year,
        "last_hold_date": decision.last_hold_at,
        "days_since_last": decision.days_since_last,
    }


def _vacation_reason_message(reason: str, decision) -> str:
    if reason == "vacation_hold_annual_limit_reached":
        return (
            f"You have reached the maximum of {decision.max_holds_per_year} "
            "vacation holds per year."
        )
    if reason == "vacation_hold_cooldown_active":
        remaining = max(
            0, (decision.cooldown_days or 0) - (decision.days_since_last or 0)
        )
        return (
            f"Please wait {remaining} more day(s) before using another vacation hold."
        )
    if reason == "vacation_hold_duration_out_of_range":
        return f"Suspension must be between 1 and {decision.max_days} days"
    if reason == "active_customer_hold_missing":
        return "Cannot resume: no customer-initiated hold was found."
    return "This subscription is not eligible for that vacation-hold action."


def get_suspend_page(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Build context for the vacation hold confirmation page."""
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        resolve_vacation_hold_policy,
    )

    account_id = optional_customer_account_id(db, customer)
    subscription = db.get(Subscription, subscription_id)
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        return None

    if subscription.status != SubscriptionStatus.active:
        return None

    decision = resolve_vacation_hold_policy(
        db,
        subscription,
        command_kind=SubscriptionCommandKind.vacation_hold,
        requested_days=1,
    )
    max_days = decision.max_days
    max_holds_per_year = decision.max_holds_per_year or 0
    cooldown_days = decision.cooldown_days or 0
    usage = {
        "holds_this_year": decision.holds_this_year,
        "days_since_last": decision.days_since_last,
    }
    holds_remaining = None
    if max_holds_per_year > 0:
        holds_remaining = max(0, max_holds_per_year - decision.holds_this_year)

    # Check if user can actually suspend (usage limits)
    can_suspend = True
    block_reason = None

    policy_reasons = tuple(
        reason
        for reason in decision.reasons
        if reason != "vacation_hold_duration_out_of_range"
    )
    if policy_reasons:
        can_suspend = False
        block_reason = _vacation_reason_message(policy_reasons[0], decision)

    offer = subscription.offer
    return {
        "subscription": subscription,
        "offer_name": offer.name if offer else "Service",
        "billing_mode": subscription.billing_mode.value
        if subscription.billing_mode
        else "postpaid",
        "max_days": max_days,
        "holds_this_year": usage["holds_this_year"],
        "holds_remaining": holds_remaining,
        "max_holds_per_year": max_holds_per_year if max_holds_per_year > 0 else None,
        "cooldown_days": cooldown_days if cooldown_days > 0 else None,
        "days_since_last": usage["days_since_last"],
        "can_suspend": can_suspend,
        "block_reason": block_reason,
    }


def apply_service_suspend(
    db: Session,
    customer: dict,
    subscription_id: str,
    days: int,
) -> dict:
    """Apply a customer-initiated vacation hold on a subscription."""
    from app.models.audit import AuditActorType
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        SubscriptionEffectiveTiming,
        SubscriptionLifecycleCommand,
        resolve_subscription_lifecycle,
    )
    from app.services.subscription_lifecycle_commands import (
        execute_subscription_command,
    )

    account_id = optional_customer_account_id(db, customer)
    subscription = db.get(Subscription, subscription_id)
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        raise ValueError("Subscription not found")

    subscriber_id = str(subscription.subscriber_id)
    snapshot = resolve_subscription_lifecycle(db, subscription_id)
    outcome = execute_subscription_command(
        db,
        SubscriptionLifecycleCommand(
            subscription_id=subscription_id,
            kind=SubscriptionCommandKind.vacation_hold,
            source=f"customer:{subscriber_id}:vacation_hold",
            effective_timing=SubscriptionEffectiveTiming.immediate,
            reason=f"Customer-initiated vacation hold for {days} days",
            expected_head=snapshot.head,
            idempotency_key=(
                f"customer-vacation-hold:{subscription_id}:{snapshot.head}:{days}"
            ),
            vacation_hold_days=days,
        ),
        actor_id=subscriber_id,
        actor_type=AuditActorType.user,
    )
    if outcome.status.value not in {"applied", "skipped"}:
        from app.services.subscription_lifecycle import resolve_vacation_hold_policy

        decision = resolve_vacation_hold_policy(
            db,
            subscription,
            command_kind=SubscriptionCommandKind.vacation_hold,
            requested_days=days,
        )
        raise ValueError(_vacation_reason_message(outcome.error_code or "", decision))
    lock_id = outcome.artifact_ids[0] if outcome.artifact_ids else None
    from app.models.enforcement_lock import EnforcementLock

    lock = db.get(EnforcementLock, coerce_uuid(lock_id)) if lock_id else None
    if lock is None or lock.resume_at is None:
        raise ValueError("Vacation hold did not return exact lock evidence")
    resume_at = lock.resume_at

    logger.info(
        "Customer %s suspended subscription %s for %d days (vacation hold, resume_at=%s)",
        subscriber_id,
        subscription_id,
        days,
        resume_at.isoformat(),
    )

    return {
        "subscription_id": subscription_id,
        "days": days,
        "lock_id": str(lock.id),
        "resume_at": resume_at.isoformat(),
    }


def get_resume_page(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Build context for the resume service confirmation page."""
    from app.models.enforcement_lock import EnforcementLock
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        resolve_vacation_hold_policy,
    )

    account_id = optional_customer_account_id(db, customer)
    subscription = db.get(Subscription, subscription_id)
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        return None

    decision = resolve_vacation_hold_policy(
        db,
        subscription,
        command_kind=SubscriptionCommandKind.vacation_resume,
    )
    if not decision.eligible or decision.active_lock_id is None:
        return None
    lock = db.get(EnforcementLock, coerce_uuid(decision.active_lock_id))
    if lock is None:
        return None

    offer = subscription.offer
    return {
        "subscription": subscription,
        "offer_name": offer.name if offer else "Service",
        "lock": lock,
        "suspended_since": lock.created_at,
        "resume_at": lock.resume_at,
    }


def apply_service_resume(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict:
    """Resume a customer-initiated vacation hold on a subscription."""
    from app.models.audit import AuditActorType
    from app.models.enforcement_lock import EnforcementLock
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        SubscriptionEffectiveTiming,
        SubscriptionLifecycleCommand,
        resolve_subscription_lifecycle,
        resolve_vacation_hold_policy,
    )
    from app.services.subscription_lifecycle_commands import (
        execute_subscription_command,
    )

    account_id = optional_customer_account_id(db, customer)
    subscription = db.get(Subscription, subscription_id)
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        raise ValueError("Subscription not found")

    decision = resolve_vacation_hold_policy(
        db,
        subscription,
        command_kind=SubscriptionCommandKind.vacation_resume,
    )
    if not decision.eligible or decision.active_lock_id is None:
        reason = (
            decision.reasons[0] if decision.reasons else "active_customer_hold_missing"
        )
        raise ValueError(_vacation_reason_message(reason, decision))
    lock = db.get(EnforcementLock, coerce_uuid(decision.active_lock_id))
    if lock is None:
        raise ValueError("Cannot resume: exact customer-hold evidence is missing")

    subscriber_id = str(subscription.subscriber_id)
    snapshot = resolve_subscription_lifecycle(db, subscription_id)
    outcome = execute_subscription_command(
        db,
        SubscriptionLifecycleCommand(
            subscription_id=subscription_id,
            kind=SubscriptionCommandKind.vacation_resume,
            source=f"customer:{subscriber_id}:vacation_resume",
            effective_timing=SubscriptionEffectiveTiming.immediate,
            reason="Customer-initiated resume via portal",
            expected_head=snapshot.head,
            idempotency_key=f"customer-vacation-resume:{lock.id}",
        ),
        actor_id=subscriber_id,
        actor_type=AuditActorType.user,
    )
    if outcome.status.value not in {"applied", "skipped"}:
        raise ValueError(outcome.message)
    db.refresh(subscription)
    restored = subscription.status == SubscriptionStatus.active

    logger.info(
        "Customer %s resumed subscription %s (vacation hold lifted, restored=%s)",
        subscriber_id,
        subscription_id,
        restored,
    )

    return {
        "subscription_id": subscription_id,
        "restored": restored,
    }
