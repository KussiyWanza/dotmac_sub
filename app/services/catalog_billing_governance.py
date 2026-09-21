"""Governance policy for catalog fields that change customer billing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import (
    AddOnPrice,
    CatalogOffer,
    OfferPrice,
    OfferVersion,
    OfferVersionPrice,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.network_monitoring import AlertSeverity
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.observability import Finding, record_finding, record_metric

logger = logging.getLogger(__name__)

BILLING_CATALOG_PERMISSION = "catalog:billing_write"

_LIVE_SUBSCRIPTION_STATUSES = (
    SubscriptionStatus.pending,
    SubscriptionStatus.active,
    SubscriptionStatus.blocked,
    SubscriptionStatus.suspended,
    SubscriptionStatus.stopped,
)
_OFFER_CRITICAL_FIELDS = frozenset(
    {
        "billing_cycle",
        "billing_mode",
        "contract_term",
        "is_active",
        "prepaid_period",
        "price_basis",
        "status",
        "vat_percent",
        "with_vat",
    }
)
_OFFER_LIVE_IMMUTABLE_FIELDS = _OFFER_CRITICAL_FIELDS - {"is_active", "status"}
_PRICE_CRITICAL_FIELDS = frozenset(
    {
        "amount",
        "billing_cycle",
        "currency",
        "is_active",
        "offer_id",
        "offer_version_id",
        "price_type",
        "unit",
    }
)
_VERSION_CRITICAL_FIELDS = frozenset(
    {
        "billing_cycle",
        "contract_term",
        "effective_end",
        "effective_start",
        "is_active",
        "offer_id",
        "price_basis",
        "status",
    }
)


@dataclass(frozen=True, slots=True)
class ApplyOfferPriceAtRenewal:
    """Apply a base catalog amount to future renewals of linked subscriptions."""

    offer_price_id: UUID
    offer_id: UUID
    previous_amount: Decimal
    next_amount: Decimal


@dataclass(frozen=True, slots=True)
class ApplyOfferPriceAtRenewalOutcome:
    offer_price_id: UUID
    offer_id: UUID
    affected_subscription_ids: tuple[UUID, ...]

    @property
    def affected_subscription_count(self) -> int:
        return len(self.affected_subscription_ids)


def _comparable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    return value


def billing_field_changes(entity: object, data: dict[str, Any]) -> dict[str, Any]:
    """Return only fields whose submitted value differs from persisted state."""
    return {
        key: value
        for key, value in data.items()
        if hasattr(entity, key)
        and _comparable(getattr(entity, key)) != _comparable(value)
    }


def billing_critical_changes(
    entity_type: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    if entity_type == "catalog_offer":
        fields = _OFFER_CRITICAL_FIELDS
    elif entity_type == "offer_version":
        fields = _VERSION_CRITICAL_FIELDS
    elif entity_type == "add_on_price":
        fields = _PRICE_CRITICAL_FIELDS | {"add_on_id"}
    else:
        fields = _PRICE_CRITICAL_FIELDS
    return {key: value for key, value in changes.items() if key in fields}


def _live_offer_subscription_count(db: Session, offer_id: object) -> int:
    return int(
        db.query(func.count(Subscription.id))
        .filter(Subscription.offer_id == offer_id)
        .filter(Subscription.status.in_(_LIVE_SUBSCRIPTION_STATUSES))
        .scalar()
        or 0
    )


def _live_version_subscription_count(db: Session, version_id: object) -> int:
    return int(
        db.query(func.count(Subscription.id))
        .filter(Subscription.offer_version_id == version_id)
        .filter(Subscription.status.in_(_LIVE_SUBSCRIPTION_STATUSES))
        .scalar()
        or 0
    )


def _raise_live_catalog_mutation(
    *,
    entity_type: str,
    entity_id: object,
    fields: set[str],
    subscription_count: int,
    remediation: str = (
        "Create a new offer version and migrate subscriptions explicitly."
    ),
) -> None:
    record_metric(
        domain="catalog",
        signal="billing_critical_mutation_blocked",
        status=entity_type,
    )
    logger.warning(
        "catalog_billing_mutation_blocked entity_type=%s entity_id=%s fields=%s subscriptions=%s",
        entity_type,
        entity_id,
        sorted(fields),
        subscription_count,
    )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "live_catalog_billing_mutation_blocked",
            "message": (
                "This pricing or cadence is referenced by live subscriptions. "
                + remediation
            ),
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "fields": sorted(fields),
            "live_subscription_count": subscription_count,
        },
    )


def assert_offer_update_safe(
    db: Session,
    offer: CatalogOffer,
    changes: dict[str, Any],
) -> None:
    critical = set(changes).intersection(_OFFER_LIVE_IMMUTABLE_FIELDS)
    if not critical:
        return
    count = _live_offer_subscription_count(db, offer.id)
    if count:
        _raise_live_catalog_mutation(
            entity_type="catalog_offer",
            entity_id=offer.id,
            fields=critical,
            subscription_count=count,
        )


def assert_offer_price_create_safe(db: Session, payload: Any) -> None:
    if not bool(payload.is_active):
        return
    price_type = payload.price_type
    existing = (
        db.query(OfferPrice.id)
        .filter(OfferPrice.offer_id == payload.offer_id)
        .filter(OfferPrice.price_type == price_type)
        .filter(OfferPrice.is_active.is_(True))
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_active_offer_price",
                "message": (
                    "An active price of this type already exists. Create an offer "
                    "version instead of adding an ambiguous second active price."
                ),
            },
        )


def assert_offer_price_update_safe(
    db: Session,
    price: OfferPrice,
    changes: dict[str, Any],
) -> None:
    target_offer_id = changes.get("offer_id", price.offer_id)
    target_price_type = changes.get("price_type", price.price_type)
    target_active = changes.get("is_active", price.is_active)
    if target_active:
        duplicate = (
            db.query(OfferPrice.id)
            .filter(OfferPrice.offer_id == target_offer_id)
            .filter(OfferPrice.price_type == target_price_type)
            .filter(OfferPrice.is_active.is_(True))
            .filter(OfferPrice.id != price.id)
            .first()
        )
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_active_offer_price",
                    "message": "An active price of this type already exists.",
                },
            )
    critical = set(changes).intersection(_PRICE_CRITICAL_FIELDS)
    blocked = critical - {"amount"}
    if not blocked:
        return
    offer_ids = {price.offer_id}
    if changes.get("offer_id") is not None:
        offer_ids.add(changes["offer_id"])
    count = sum(_live_offer_subscription_count(db, offer_id) for offer_id in offer_ids)
    if count:
        _raise_live_catalog_mutation(
            entity_type="offer_price",
            entity_id=price.id,
            fields=blocked,
            subscription_count=count,
        )


def apply_offer_price_at_renewal(
    db: Session,
    command: ApplyOfferPriceAtRenewal,
) -> ApplyOfferPriceAtRenewalOutcome:
    """Project a changed base offer amount onto each future renewal.

    Existing invoice lines and billing evidence are immutable and are not
    touched.  The subscription price is the runtime renewal-price projection,
    so changing it before ``next_billing_at`` makes the new amount effective at
    the next charge.  Subscriptions pinned to an offer version are excluded;
    their version price remains authoritative.
    """

    subscriptions = tuple(
        db.scalars(
            select(Subscription)
            .where(
                Subscription.offer_id == command.offer_id,
                Subscription.offer_version_id.is_(None),
                Subscription.status.in_(_LIVE_SUBSCRIPTION_STATUSES),
            )
            .order_by(Subscription.id)
            .with_for_update()
        ).all()
    )
    for subscription in subscriptions:
        subscription.unit_price = command.next_amount
    db.flush()
    return ApplyOfferPriceAtRenewalOutcome(
        offer_price_id=command.offer_price_id,
        offer_id=command.offer_id,
        affected_subscription_ids=tuple(row.id for row in subscriptions),
    )


def _live_add_on_subscription_count(db: Session, add_on_id: object) -> int:
    now = datetime.now(UTC)
    return int(
        db.query(func.count(SubscriptionAddOn.id))
        .join(Subscription, Subscription.id == SubscriptionAddOn.subscription_id)
        .filter(SubscriptionAddOn.add_on_id == add_on_id)
        .filter(or_(SubscriptionAddOn.end_at.is_(None), SubscriptionAddOn.end_at > now))
        .filter(Subscription.status.in_(_LIVE_SUBSCRIPTION_STATUSES))
        .scalar()
        or 0
    )


def assert_add_on_price_create_safe(db: Session, payload: Any) -> None:
    if not bool(payload.is_active):
        return
    price_type = payload.price_type
    existing = (
        db.query(AddOnPrice.id)
        .filter(AddOnPrice.add_on_id == payload.add_on_id)
        .filter(AddOnPrice.price_type == price_type)
        .filter(AddOnPrice.is_active.is_(True))
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_active_add_on_price",
                "message": "An active price of this type already exists.",
            },
        )


def assert_add_on_price_update_safe(
    db: Session,
    price: AddOnPrice,
    changes: dict[str, Any],
) -> None:
    target_add_on_id = changes.get("add_on_id", price.add_on_id)
    target_price_type = changes.get("price_type", price.price_type)
    target_active = changes.get("is_active", price.is_active)
    if target_active:
        duplicate = (
            db.query(AddOnPrice.id)
            .filter(AddOnPrice.add_on_id == target_add_on_id)
            .filter(AddOnPrice.price_type == target_price_type)
            .filter(AddOnPrice.is_active.is_(True))
            .filter(AddOnPrice.id != price.id)
            .first()
        )
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_active_add_on_price",
                    "message": "An active price of this type already exists.",
                },
            )
    critical = set(changes).intersection(_PRICE_CRITICAL_FIELDS | {"add_on_id"})
    if not critical:
        return
    add_on_ids = {price.add_on_id}
    if changes.get("add_on_id") is not None:
        add_on_ids.add(changes["add_on_id"])
    count = sum(
        _live_add_on_subscription_count(db, add_on_id) for add_on_id in add_on_ids
    )
    if count:
        _raise_live_catalog_mutation(
            entity_type="add_on_price",
            entity_id=price.id,
            fields=critical,
            subscription_count=count,
            remediation=(
                "Create a new add-on catalog entry and migrate future purchases "
                "explicitly."
            ),
        )


def assert_offer_version_update_safe(
    db: Session,
    version: OfferVersion,
    changes: dict[str, Any],
) -> None:
    critical = set(changes).intersection(_VERSION_CRITICAL_FIELDS)
    if not critical:
        return
    count = _live_version_subscription_count(db, version.id)
    if count:
        _raise_live_catalog_mutation(
            entity_type="offer_version",
            entity_id=version.id,
            fields=critical,
            subscription_count=count,
        )


def assert_offer_version_price_update_safe(
    db: Session,
    price: OfferVersionPrice,
    changes: dict[str, Any],
) -> None:
    target_version_id = changes.get("offer_version_id", price.offer_version_id)
    target_price_type = changes.get("price_type", price.price_type)
    target_active = changes.get("is_active", price.is_active)
    if target_active:
        duplicate = (
            db.query(OfferVersionPrice.id)
            .filter(OfferVersionPrice.offer_version_id == target_version_id)
            .filter(OfferVersionPrice.price_type == target_price_type)
            .filter(OfferVersionPrice.is_active.is_(True))
            .filter(OfferVersionPrice.id != price.id)
            .first()
        )
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_active_offer_version_price",
                    "message": "An active version price of this type already exists.",
                },
            )
    critical = set(changes).intersection(_PRICE_CRITICAL_FIELDS)
    if not critical:
        return
    version_ids = {price.offer_version_id}
    if changes.get("offer_version_id") is not None:
        version_ids.add(changes["offer_version_id"])
    count = sum(
        _live_version_subscription_count(db, version_id) for version_id in version_ids
    )
    if count:
        _raise_live_catalog_mutation(
            entity_type="offer_version_price",
            entity_id=price.id,
            fields=critical,
            subscription_count=count,
        )


def assert_offer_version_price_create_safe(db: Session, payload: Any) -> None:
    if not bool(payload.is_active):
        return
    existing = (
        db.query(OfferVersionPrice.id)
        .filter(OfferVersionPrice.offer_version_id == payload.offer_version_id)
        .filter(OfferVersionPrice.price_type == payload.price_type)
        .filter(OfferVersionPrice.is_active.is_(True))
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_active_offer_version_price",
                "message": "An active version price of this type already exists.",
            },
        )


def _actor_type(value: str | AuditActorType | None) -> AuditActorType:
    if isinstance(value, AuditActorType):
        return value
    normalized = str(value or "system")
    if normalized in {"subscriber", "system_user"}:
        normalized = "user"
    try:
        return AuditActorType(normalized)
    except ValueError:
        return AuditActorType.system


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value is not None else None


def stage_billing_catalog_change(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: object,
    changes: dict[str, Any] | None = None,
    actor_id: str | None = None,
    actor_type: str | AuditActorType | None = None,
    actor: AuditActor | None = None,
    offer_id: object | None = None,
) -> None:
    """Stage durable audit and operator visibility in the caller transaction.

    New callers pass a typed actor. Scalar arguments remain for existing
    callers until their separately reviewed audit-actor retirement slices.
    """
    if actor is not None and (actor_id is not None or actor_type is not None):
        raise ValueError(
            "typed audit actor cannot be combined with scalar actor fields"
        )
    effective_actor_type = (
        actor.actor_type if actor is not None else _actor_type(actor_type)
    )
    effective_actor_id = actor.actor_id if actor is not None else actor_id
    safe_changes = {key: _json_value(value) for key, value in (changes or {}).items()}
    metadata: dict[str, object] = {
        "action": action,
        "changes": safe_changes,
    }
    if offer_id is not None:
        metadata["offer_id"] = str(offer_id)
    if actor is None:
        stage_audit_event(
            db,
            action=f"catalog_billing_{action}",
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor_type=effective_actor_type,
            actor_id=effective_actor_id,
            metadata=metadata,
        )
    else:
        stage_audit_event(
            db,
            action=f"catalog_billing_{action}",
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor=actor,
            metadata=metadata,
        )
    record_metric(
        domain="catalog",
        signal="billing_critical_change",
        status=action,
    )
    record_finding(
        db,
        Finding(
            fingerprint="catalog:billing-critical-change",
            domain="catalog",
            source="catalog_billing_governance",
            severity=AlertSeverity.info,
            title="Billing-critical catalog changed",
            summary=(
                f"{entity_type} {entity_id} was {action}. Review the audit trail "
                "and affected renewal projections."
            ),
            details={
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                **metadata,
            },
            target_url=(
                f"/admin/catalog/offers/{offer_id}"
                if offer_id is not None
                else "/admin/catalog"
            ),
        ),
    )
    logger.warning(
        "catalog_billing_change action=%s entity_type=%s entity_id=%s actor_type=%s actor_id=%s changes=%s",
        action,
        entity_type,
        entity_id,
        effective_actor_type.value,
        effective_actor_id,
        safe_changes,
    )


__all__ = [
    "ApplyOfferPriceAtRenewal",
    "ApplyOfferPriceAtRenewalOutcome",
    "BILLING_CATALOG_PERMISSION",
    "assert_add_on_price_create_safe",
    "assert_add_on_price_update_safe",
    "assert_offer_price_create_safe",
    "assert_offer_price_update_safe",
    "assert_offer_update_safe",
    "assert_offer_version_price_create_safe",
    "assert_offer_version_price_update_safe",
    "assert_offer_version_update_safe",
    "apply_offer_price_at_renewal",
    "billing_critical_changes",
    "billing_field_changes",
    "stage_billing_catalog_change",
]
