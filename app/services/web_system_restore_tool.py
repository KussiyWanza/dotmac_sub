"""System tool helpers for restoring soft-deleted subscribers and related records."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

logger = logging.getLogger(__name__)

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Invoice, Payment
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    IPAssignment,
    OntAssignment,
    SplitterPortAssignment,
)
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber, UserType
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services.catalog import access_credentials as access_credential_service
from app.services.owner_commands import CommandContext
from app.services.subscription_lifecycle_evidence import (
    LifecycleEvidenceSource,
    record_current_state_baseline,
)

DELETED_AT_KEY = "recovery_deleted_at"
DELETED_BY_KEY = "recovery_deleted_by"
PURGE_DUE_AT_KEY = "recovery_purge_due_at"
PURGED_AT_KEY = "recovery_purged_at"
SNAPSHOT_KEY = "recovery_snapshot"
LAST_RESTORED_AT_KEY = "recovery_last_restored_at"
LAST_RESTORED_BY_KEY = "recovery_last_restored_by"
RETENTION_DAYS_KEY = "restore_retention_days"
DEFAULT_RETENTION_DAYS = 90


def _now() -> datetime:
    return datetime.now(UTC)


def _metadata(subscriber: Subscriber) -> dict[str, Any]:
    return dict(subscriber.metadata_ or {})


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def get_retention_days(db: Session) -> int:
    try:
        setting = domain_settings_service.subscriber_settings.get_by_key(
            db, RETENTION_DAYS_KEY
        )
    except Exception as exc:
        logger.warning("Failed to read retention days setting: %s", exc)
        return DEFAULT_RETENTION_DAYS

    if setting.value_json is not None:
        try:
            return max(1, int(setting.value_json))
        except (TypeError, ValueError):
            return DEFAULT_RETENTION_DAYS
    if setting.value_text:
        try:
            return max(1, int(setting.value_text.strip()))
        except (TypeError, ValueError):
            return DEFAULT_RETENTION_DAYS
    return DEFAULT_RETENTION_DAYS


def set_retention_days(db: Session, *, days: int) -> int:
    value = max(1, min(3650, int(days)))
    domain_settings_service.subscriber_settings.upsert_by_key(
        db,
        RETENTION_DAYS_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.integer,
            value_text=str(value),
            value_json=value,
            is_secret=False,
            is_active=True,
        ),
    )
    return value


def _is_soft_deleted(subscriber: Subscriber) -> bool:
    metadata = _metadata(subscriber)
    return bool(metadata.get(DELETED_AT_KEY)) and not bool(metadata.get(PURGED_AT_KEY))


def _build_snapshot(db: Session, subscriber_id: UUID) -> dict[str, Any]:
    subscriptions = db.scalars(
        select(Subscription).where(Subscription.subscriber_id == subscriber_id)
    ).all()
    service_orders = db.scalars(
        select(ServiceOrder).where(ServiceOrder.subscriber_id == subscriber_id)
    ).all()
    cpe_devices = db.scalars(
        select(CPEDevice).where(CPEDevice.subscriber_id == subscriber_id)
    ).all()

    return {
        "subscriptions": [
            {
                "id": str(item.id),
                "status": item.status.value if item.status else None,
                "canceled_at": item.canceled_at.isoformat()
                if item.canceled_at
                else None,
            }
            for item in subscriptions
        ],
        "service_orders": [
            {
                "id": str(item.id),
                "status": item.status.value if item.status else None,
            }
            for item in service_orders
        ],
        "cpe_devices": [
            {
                "id": str(item.id),
                "status": item.status.value if item.status else None,
            }
            for item in cpe_devices
        ],
    }


def _apply_soft_delete_cascade(
    db: Session,
    subscriber_id: UUID,
    *,
    actor_id: str,
) -> dict[str, int]:
    touched = {
        "subscriptions": 0,
        "invoices": 0,
        "payments": 0,
        "service_orders": 0,
        "radius_accounts": 0,
        "radius_users": 0,
        "ip_assignments": 0,
        "ont_assignments": 0,
        "splitter_assignments": 0,
    }

    now = _now()

    subscriptions = db.scalars(
        select(Subscription).where(Subscription.subscriber_id == subscriber_id)
    ).all()
    for subscription in subscriptions:
        if subscription.status != SubscriptionStatus.canceled:
            from app.services.account_lifecycle import cancel_subscription

            cancel_subscription(
                db,
                str(subscription.id),
                "System restore soft-delete cascade",
                "system_restore_tool",
                emit=False,
                generate_credit=False,
            )
            touched["subscriptions"] += 1

    invoices = db.scalars(
        select(Invoice).where(Invoice.account_id == subscriber_id)
    ).all()
    for invoice in invoices:
        if invoice.is_active:
            invoice.is_active = False
            touched["invoices"] += 1

    payments = db.scalars(
        select(Payment).where(Payment.account_id == subscriber_id)
    ).all()
    for payment in payments:
        if payment.is_active:
            payment.is_active = False
            touched["payments"] += 1

    service_orders = db.scalars(
        select(ServiceOrder).where(ServiceOrder.subscriber_id == subscriber_id)
    ).all()
    for service_order in service_orders:
        if service_order.status != ServiceOrderStatus.canceled:
            from app.services import service_order_lifecycle

            changed = service_order_lifecycle.restore_recorded_status(
                db,
                service_order_id=service_order.id,
                target_status=ServiceOrderStatus.canceled,
                actor_id=actor_id,
                reason="Subscriber recovery soft-delete cascade",
            )
            touched["service_orders"] += int(changed)

    credentials = db.scalars(
        select(access_credential_service.model).where(
            access_credential_service.model.subscriber_id == subscriber_id
        )
    ).all()
    for credential in credentials:
        if credential.is_active:
            credential.is_active = False
            touched["radius_accounts"] += 1

    radius_users = db.scalars(
        select(RadiusUser).where(RadiusUser.subscriber_id == subscriber_id)
    ).all()
    for radius_user in radius_users:
        if radius_user.is_active:
            radius_user.is_active = False
            touched["radius_users"] += 1

    ip_assignments = db.scalars(
        select(IPAssignment).where(IPAssignment.subscriber_id == subscriber_id)
    ).all()
    for ip_assignment in ip_assignments:
        if ip_assignment.is_active:
            ip_assignment.is_active = False
            touched["ip_assignments"] += 1

    ont_assignments = db.scalars(
        select(OntAssignment).where(OntAssignment.subscriber_id == subscriber_id)
    ).all()
    for ont_assignment in ont_assignments:
        if ont_assignment.active:
            ont_assignment.active = False
            touched["ont_assignments"] += 1

    splitter_assignments = db.scalars(
        select(SplitterPortAssignment).where(
            SplitterPortAssignment.subscriber_id == subscriber_id
        )
    ).all()
    for splitter_assignment in splitter_assignments:
        if splitter_assignment.active:
            splitter_assignment.active = False
            touched["splitter_assignments"] += 1

    return touched


def mark_subscriber_deleted(
    db: Session,
    *,
    subscriber_id: str,
    actor_id: str | None,
) -> dict[str, Any]:
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    if subscriber.is_active:
        raise HTTPException(
            status_code=409, detail="Deactivate subscriber before deleting."
        )

    metadata = _metadata(subscriber)
    if metadata.get(PURGED_AT_KEY):
        raise HTTPException(
            status_code=409, detail="Subscriber was already purged from recovery queue."
        )

    if not metadata.get(DELETED_AT_KEY):
        metadata[SNAPSHOT_KEY] = _build_snapshot(db, subscriber.id)
        metadata[DELETED_AT_KEY] = _now().isoformat()
        metadata[DELETED_BY_KEY] = actor_id
        metadata[PURGE_DUE_AT_KEY] = (
            _now() + timedelta(days=get_retention_days(db))
        ).isoformat()

    touched = _apply_soft_delete_cascade(
        db,
        subscriber.id,
        actor_id=str(actor_id or "system_restore_tool"),
    )
    subscriber.metadata_ = metadata

    db.commit()
    db.refresh(subscriber)

    return {
        "subscriber_id": str(subscriber.id),
        "deleted_at": metadata.get(DELETED_AT_KEY),
        "touched": touched,
    }


def _by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id")): item
        for item in items
        if isinstance(item, dict) and item.get("id")
    }


def restore_subscriber(
    db: Session,
    *,
    subscriber_id: str,
    actor_id: str | None,
) -> dict[str, Any]:
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    metadata = _metadata(subscriber)
    if metadata.get(PURGED_AT_KEY):
        raise HTTPException(
            status_code=409,
            detail="Subscriber has passed retention and cannot be restored.",
        )
    if not metadata.get(DELETED_AT_KEY):
        raise HTTPException(
            status_code=409, detail="Subscriber is not marked as deleted."
        )

    restored_at = _now()
    restore_command_id = uuid5(
        NAMESPACE_URL,
        f"dotmac:system-restore:{subscriber.id}:{metadata.get(DELETED_AT_KEY)}",
    )

    snapshot = (
        metadata.get(SNAPSHOT_KEY)
        if isinstance(metadata.get(SNAPSHOT_KEY), dict)
        else {}
    )
    subscription_snapshot = (
        _by_id(snapshot.get("subscriptions", [])) if isinstance(snapshot, dict) else {}
    )
    order_snapshot = (
        _by_id(snapshot.get("service_orders", [])) if isinstance(snapshot, dict) else {}
    )
    cpe_snapshot = (
        _by_id(snapshot.get("cpe_devices", [])) if isinstance(snapshot, dict) else {}
    )

    touched = {
        "subscriptions": 0,
        "invoices": 0,
        "payments": 0,
        "service_orders": 0,
        "radius_accounts": 0,
        "radius_users": 0,
        "ip_assignments": 0,
        "ont_assignments": 0,
        "splitter_assignments": 0,
    }

    subscriber.is_active = True

    subscriptions = db.scalars(
        select(Subscription)
        .where(Subscription.subscriber_id == subscriber.id)
        .order_by(Subscription.id)
        .with_for_update()
    ).all()
    for subscription in subscriptions:
        row_snapshot = subscription_snapshot.get(str(subscription.id), {})
        status_value = (
            row_snapshot.get("status") if isinstance(row_snapshot, dict) else None
        )
        canceled_value = (
            row_snapshot.get("canceled_at") if isinstance(row_snapshot, dict) else None
        )
        if status_value:
            try:
                subscription_status = SubscriptionStatus(status_value)
            except ValueError:
                subscription_status = SubscriptionStatus.active
        else:
            subscription_status = SubscriptionStatus.active
        if subscription.status != subscription_status:
            subscription.status = subscription_status
            record_current_state_baseline(
                db,
                subscription=subscription,
                effective_at=restored_at,
                evidence_source=LifecycleEvidenceSource.reconciliation_baseline,
                context=CommandContext.system(
                    command_id=uuid5(
                        restore_command_id,
                        f"subscription:{subscription.id}",
                    ),
                    correlation_id=restore_command_id,
                    actor=str(actor_id or "system_restore_tool"),
                    scope=f"subscription:{subscription.id}",
                    reason="Restore subscription state from recovery snapshot",
                    idempotency_key=(
                        f"system-restore:{restore_command_id}:{subscription.id}"
                    ),
                    causation_id=restore_command_id,
                ),
            )
            touched["subscriptions"] += 1
        subscription.canceled_at = _parse_dt(canceled_value)

    invoices = db.scalars(
        select(Invoice).where(Invoice.account_id == subscriber.id)
    ).all()
    for invoice in invoices:
        if not invoice.is_active:
            invoice.is_active = True
            touched["invoices"] += 1

    payments = db.scalars(
        select(Payment).where(Payment.account_id == subscriber.id)
    ).all()
    for payment in payments:
        if not payment.is_active:
            payment.is_active = True
            touched["payments"] += 1

    service_orders = db.scalars(
        select(ServiceOrder).where(ServiceOrder.subscriber_id == subscriber.id)
    ).all()
    for service_order in service_orders:
        row_snapshot = order_snapshot.get(str(service_order.id), {})
        status_value = (
            row_snapshot.get("status") if isinstance(row_snapshot, dict) else None
        )
        if status_value:
            try:
                order_status = ServiceOrderStatus(status_value)
            except ValueError:
                order_status = ServiceOrderStatus.draft
        else:
            order_status = ServiceOrderStatus.draft
        if service_order.status != order_status:
            from app.services import service_order_lifecycle

            changed = service_order_lifecycle.restore_recorded_status(
                db,
                service_order_id=service_order.id,
                target_status=order_status,
                actor_id=str(actor_id or "system_restore_tool"),
                reason="Restore service-order state from subscriber recovery snapshot",
            )
            touched["service_orders"] += int(changed)

    credentials = db.scalars(
        select(access_credential_service.model).where(
            access_credential_service.model.subscriber_id == subscriber.id
        )
    ).all()
    for credential in credentials:
        if not credential.is_active:
            credential.is_active = True
            touched["radius_accounts"] += 1

    radius_users = db.scalars(
        select(RadiusUser).where(RadiusUser.subscriber_id == subscriber.id)
    ).all()
    for radius_user in radius_users:
        if not radius_user.is_active:
            radius_user.is_active = True
            touched["radius_users"] += 1

    ip_assignments = db.scalars(
        select(IPAssignment).where(IPAssignment.subscriber_id == subscriber.id)
    ).all()
    for ip_assignment in ip_assignments:
        if not ip_assignment.is_active:
            ip_assignment.is_active = True
            touched["ip_assignments"] += 1

    ont_assignments = db.scalars(
        select(OntAssignment).where(OntAssignment.subscriber_id == subscriber.id)
    ).all()
    for ont_assignment in ont_assignments:
        if not ont_assignment.active:
            ont_assignment.active = True
            touched["ont_assignments"] += 1

    splitter_assignments = db.scalars(
        select(SplitterPortAssignment).where(
            SplitterPortAssignment.subscriber_id == subscriber.id
        )
    ).all()
    for splitter_assignment in splitter_assignments:
        if not splitter_assignment.active:
            splitter_assignment.active = True
            touched["splitter_assignments"] += 1

    cpe_devices = db.scalars(
        select(CPEDevice).where(CPEDevice.subscriber_id == subscriber.id)
    ).all()
    for cpe_device in cpe_devices:
        row_snapshot = cpe_snapshot.get(str(cpe_device.id), {})
        status_value = (
            row_snapshot.get("status") if isinstance(row_snapshot, dict) else None
        )
        if status_value:
            try:
                cpe_status = DeviceStatus(status_value)
            except ValueError:
                cpe_status = DeviceStatus.active
        else:
            cpe_status = DeviceStatus.active
        if cpe_device.status != cpe_status:
            cpe_device.status = cpe_status

    metadata.pop(DELETED_AT_KEY, None)
    metadata.pop(DELETED_BY_KEY, None)
    metadata.pop(PURGE_DUE_AT_KEY, None)
    metadata.pop(PURGED_AT_KEY, None)
    metadata[LAST_RESTORED_AT_KEY] = restored_at.isoformat()
    metadata[LAST_RESTORED_BY_KEY] = actor_id
    subscriber.metadata_ = metadata

    db.commit()
    db.refresh(subscriber)

    return {
        "subscriber_id": str(subscriber.id),
        "restored_at": metadata.get(LAST_RESTORED_AT_KEY),
        "touched": touched,
    }


def _matches_query(subscriber: Subscriber, query_text: str) -> bool:
    needle = query_text.strip().lower()
    if not needle:
        return True

    fields = [
        str(subscriber.id),
        subscriber.subscriber_number or "",
        subscriber.account_number or "",
        subscriber.display_name or "",
        subscriber.first_name or "",
        subscriber.last_name or "",
        subscriber.email or "",
        subscriber.phone or "",
    ]

    for item in subscriber.access_credentials:
        fields.append(item.username or "")
    for item in subscriber.subscriptions:
        fields.append(item.login or "")

    return any(needle in value.lower() for value in fields if value)


def _deleted_at_sort_key(subscriber: Subscriber) -> datetime:
    deleted_at = _parse_dt(_metadata(subscriber).get(DELETED_AT_KEY))
    return deleted_at or subscriber.updated_at or subscriber.created_at or _now()


def list_deleted_subscribers(
    db: Session,
    *,
    query: str | None,
    limit: int = 100,
) -> list[Subscriber]:
    candidates = db.scalars(
        select(Subscriber)
        .options(
            selectinload(Subscriber.access_credentials),
            selectinload(Subscriber.subscriptions),
        )
        .where(Subscriber.user_type != UserType.system_user)
        .where(Subscriber.is_active.is_(False))
        .order_by(Subscriber.updated_at.desc())
        .limit(max(50, limit * 5))
    ).all()

    rows = [
        item
        for item in candidates
        if _is_soft_deleted(item) and _matches_query(item, query or "")
    ]
    rows.sort(key=_deleted_at_sort_key, reverse=True)
    return rows[: max(1, limit)]


def list_recently_deleted(db: Session, *, limit: int = 20) -> list[Subscriber]:
    return list_deleted_subscribers(db, query=None, limit=limit)


def build_restore_preview(db: Session, *, subscriber_id: str) -> dict[str, Any]:
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    metadata = _metadata(subscriber)
    if not metadata.get(DELETED_AT_KEY):
        raise HTTPException(
            status_code=404, detail="Subscriber is not marked as deleted"
        )
    if metadata.get(PURGED_AT_KEY):
        raise HTTPException(
            status_code=409,
            detail="Subscriber has already been purged from recovery queue",
        )

    subscriptions = db.scalars(
        select(Subscription).where(Subscription.subscriber_id == subscriber.id)
    ).all()
    invoices = db.scalars(
        select(Invoice).where(Invoice.account_id == subscriber.id)
    ).all()
    payments = db.scalars(
        select(Payment).where(Payment.account_id == subscriber.id)
    ).all()
    service_orders = db.scalars(
        select(ServiceOrder).where(ServiceOrder.subscriber_id == subscriber.id)
    ).all()
    access_credentials = db.scalars(
        select(access_credential_service.model).where(
            access_credential_service.model.subscriber_id == subscriber.id
        )
    ).all()
    radius_users = db.scalars(
        select(RadiusUser).where(RadiusUser.subscriber_id == subscriber.id)
    ).all()
    ip_assignments = db.scalars(
        select(IPAssignment).where(IPAssignment.subscriber_id == subscriber.id)
    ).all()
    ont_assignments = db.scalars(
        select(OntAssignment).where(OntAssignment.subscriber_id == subscriber.id)
    ).all()
    splitter_assignments = db.scalars(
        select(SplitterPortAssignment).where(
            SplitterPortAssignment.subscriber_id == subscriber.id
        )
    ).all()

    return {
        "subscriber": subscriber,
        "deleted_at": metadata.get(DELETED_AT_KEY),
        "purge_due_at": metadata.get(PURGE_DUE_AT_KEY),
        "counts": {
            "subscriptions": {
                "total": len(subscriptions),
                "to_restore": sum(
                    1
                    for row in subscriptions
                    if row.status == SubscriptionStatus.canceled
                ),
            },
            "invoices": {
                "total": len(invoices),
                "to_restore": sum(1 for row in invoices if not row.is_active),
            },
            "payments": {
                "total": len(payments),
                "to_restore": sum(1 for row in payments if not row.is_active),
            },
            "service_orders": {
                "total": len(service_orders),
                "to_restore": sum(
                    1
                    for row in service_orders
                    if row.status == ServiceOrderStatus.canceled
                ),
            },
            "radius_accounts": {
                "total": len(access_credentials) + len(radius_users),
                "to_restore": sum(1 for row in access_credentials if not row.is_active)
                + sum(1 for row in radius_users if not row.is_active),
            },
            "network_assignments": {
                "total": len(ip_assignments)
                + len(ont_assignments)
                + len(splitter_assignments),
                "to_restore": sum(1 for row in ip_assignments if not row.is_active)
                + sum(1 for row in ont_assignments if not row.active)
                + sum(1 for row in splitter_assignments if not row.active),
            },
        },
    }


def purge_expired_from_recovery_queue(db: Session) -> int:
    cutoff = _now()
    candidates = db.scalars(
        select(Subscriber)
        .where(Subscriber.user_type != UserType.system_user)
        .where(Subscriber.is_active.is_(False))
    ).all()

    purged = 0
    for subscriber in candidates:
        metadata = _metadata(subscriber)
        deleted_at = _parse_dt(metadata.get(DELETED_AT_KEY))
        if not deleted_at:
            continue
        if metadata.get(PURGED_AT_KEY):
            continue

        due_at = _parse_dt(metadata.get(PURGE_DUE_AT_KEY))
        if due_at is None:
            due_at = deleted_at + timedelta(days=get_retention_days(db))

        if due_at <= cutoff:
            metadata[PURGED_AT_KEY] = cutoff.isoformat()
            subscriber.metadata_ = metadata
            purged += 1

    if purged:
        db.commit()
    return purged


def soft_deleted_count(db: Session) -> int:
    rows = db.scalars(
        select(Subscriber)
        .where(Subscriber.user_type != UserType.system_user)
        .where(Subscriber.is_active.is_(False))
    ).all()
    return sum(1 for row in rows if _is_soft_deleted(row))


def build_page_state(
    db: Session, *, query: str | None, selected_id: str | None
) -> dict[str, Any]:
    # NOTE: purge_expired_from_recovery_queue should be called from a scheduled
    # Celery task, not on every page render. Leaving it here for now but it
    # should be migrated to app/tasks/ in a follow-up.
    purged_count = purge_expired_from_recovery_queue(db)
    selected_preview = None
    selected = (selected_id or "").strip()
    if selected:
        try:
            selected_preview = build_restore_preview(db, subscriber_id=selected)
        except HTTPException:
            selected_preview = None

    deleted_rows = list_deleted_subscribers(
        db, query=query, limit=100 if (query or "").strip() else 20
    )
    recent_rows = list_recently_deleted(db, limit=20)

    return {
        "query": query or "",
        "deleted_rows": deleted_rows,
        "recent_rows": recent_rows,
        "selected_preview": selected_preview,
        "selected_id": selected,
        "retention_days": get_retention_days(db),
        "soft_deleted_count": soft_deleted_count(db),
        "purged_count": purged_count,
    }
