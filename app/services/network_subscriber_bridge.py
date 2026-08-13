"""Bridge adapter between the network domain and the subscriber domain.

The ``app.services.network`` package is forbidden (via import-linter) from
importing ``app.models.subscriber`` directly. This module lives OUTSIDE the
``network`` package so it can freely import from both domains and provide a
concrete ``SubscriberValidator`` implementation to network services that need
subscriber integration at runtime.

Network services should accept the bridge (or any object implementing the
``SubscriberValidator`` protocol) via their constructor and fall back to a
no-op / standalone path when one is not supplied.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import Select

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import (
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.services.owner_commands import execute_owner_savepoint, owner_command_active
from app.validators import network as network_validators

logger = logging.getLogger(__name__)

__all__ = [
    "AssignmentSubscriptionSnapshot",
    "DefaultSubscriberValidator",
    "assignment_subscription_snapshot",
    "assignment_subscription_snapshots",
    "default_subscriber_validator",
]


_TERMINAL_ASSIGNMENT_STATUSES = frozenset(
    {
        SubscriptionStatus.archived,
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
        SubscriptionStatus.hidden,
    }
)


@dataclass(frozen=True)
class AssignmentSubscriptionSnapshot:
    """Subscription-owned identity projected for network assignment decisions."""

    id: uuid.UUID
    subscriber_id: uuid.UUID
    offer_id: uuid.UUID
    status: str
    assignment_eligible: bool


def _assignment_snapshot(row: Subscription) -> AssignmentSubscriptionSnapshot:
    return AssignmentSubscriptionSnapshot(
        id=row.id,
        subscriber_id=row.subscriber_id,
        offer_id=row.offer_id,
        status=str(getattr(row.status, "value", row.status)),
        assignment_eligible=row.status not in _TERMINAL_ASSIGNMENT_STATUSES,
    )


def assignment_subscription_snapshot(
    db: Session,
    subscription_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> AssignmentSubscriptionSnapshot | None:
    """Project one subscription identity without exporting its ORM authority."""

    statement = select(Subscription).where(Subscription.id == subscription_id)
    if for_update:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    return _assignment_snapshot(row) if row is not None else None


def assignment_subscription_snapshots(
    db: Session,
    subscription_ids: set[uuid.UUID],
) -> dict[uuid.UUID, AssignmentSubscriptionSnapshot]:
    """Project exact subscription identities for a read-only assignment audit."""

    if not subscription_ids:
        return {}
    rows = db.scalars(
        select(Subscription)
        .where(Subscription.id.in_(subscription_ids))
        .order_by(Subscription.id)
    ).all()
    return {row.id: _assignment_snapshot(row) for row in rows}


class DefaultSubscriberValidator:
    """Default ``SubscriberValidator`` implementation using the ORM.

    Delegates existence and address-link checks to ``app.validators.network``
    and performs ONT search joins against the ``Subscriber`` model. Intended
    to be wired into ``app.services.network.olt`` as the production validator.
    """

    def validate_assignment_customer_links(
        self,
        db: Session,
        *,
        subscriber_id: object | None,
        service_address_id: object | None,
    ) -> None:
        """Mirror of the legacy ``_validate_assignment_customer_links`` helper."""
        from fastapi import HTTPException

        if subscriber_id is None:
            if service_address_id is not None:
                raise HTTPException(
                    status_code=400,
                    detail="Service address requires a subscriber",
                )
            return
        network_validators.validate_cpe_device_links(
            db,
            str(subscriber_id),
            str(service_address_id) if service_address_id is not None else None,
        )

    def resolve_assignment_subscription(
        self,
        db: Session,
        *,
        subscription_id: object,
        subscriber_id: object | None,
    ) -> tuple[object, object]:
        """Validate an ONT's subscription binding and derive its subscriber."""
        subscription = db.get(Subscription, subscription_id)
        if subscription is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if subscriber_id is not None and subscription.subscriber_id != subscriber_id:
            raise HTTPException(
                status_code=400,
                detail="Subscription does not belong to the assignment subscriber",
            )
        return subscription.id, subscription.subscriber_id

    def validate_active_assignment_subscription(
        self,
        db: Session,
        *,
        subscription_id: object,
        subscriber_id: object,
    ) -> tuple[object, object]:
        """Validate active subscription and subscriber state for network commands."""
        subscription_stmt = select(Subscription).where(
            Subscription.id == subscription_id
        )
        subscriber_stmt = select(Subscriber).where(Subscriber.id == subscriber_id)
        subscription = db.scalar(subscription_stmt.with_for_update())
        if subscription is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        subscriber = db.scalar(subscriber_stmt.with_for_update())
        if subscriber is None:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if subscription.subscriber_id != subscriber.id:
            raise HTTPException(
                status_code=409,
                detail="Subscription does not belong to the selected subscriber",
            )
        if subscription.status != SubscriptionStatus.active:
            raise HTTPException(
                status_code=409,
                detail="Subscription must be active for ONT reassignment",
            )
        if subscriber.status != SubscriberStatus.active:
            raise HTTPException(
                status_code=409,
                detail="Subscriber must be active for ONT reassignment",
            )
        return subscription.id, subscriber.id

    def apply_subscription_device_intent(
        self,
        db: Session,
        *,
        subscription_id: object,
        ont: Any,
    ) -> bool:
        """Copy the latest staged sales intent into ONT desired config."""
        from app.models.network import OntSyncStatus
        from app.models.provisioning import ServiceOrder, ServiceOrderStatus
        from app.services.network.ont_desired_config import set_desired_config_values

        order = (
            db.query(ServiceOrder)
            .filter(ServiceOrder.subscription_id == subscription_id)
            .filter(
                ServiceOrder.status.in_(
                    (
                        ServiceOrderStatus.submitted,
                        ServiceOrderStatus.scheduled,
                        ServiceOrderStatus.provisioning,
                    )
                )
            )
            .order_by(ServiceOrder.created_at.desc())
            .first()
        )
        context = order.execution_context if order else None
        device_intent = (
            context.get("device_intent") if isinstance(context, dict) else None
        )
        desired = (
            device_intent.get("desired_config")
            if isinstance(device_intent, dict)
            else None
        )
        if not isinstance(desired, dict) or not desired:
            return False
        set_desired_config_values(ont, desired)
        ont.sync_status = OntSyncStatus.out_of_sync
        ont.last_error = "Staged from provisioning service order; awaiting reconcile"
        return True

    def validate_business_owner(
        self,
        db: Session,
        *,
        owner_subscriber_id: object | None,
    ) -> None:
        """Validate that a network owner reference points to a business account."""
        if owner_subscriber_id is None:
            return
        owner = db.get(Subscriber, owner_subscriber_id)
        if not owner or owner.category != SubscriberCategory.business:
            raise HTTPException(
                status_code=400,
                detail="Provisioning profile owner must be a business account.",
            )

    def augment_ont_search(
        self,
        stmt: Select,
        term: str,
        *,
        assignment_alias: Any,
    ) -> tuple[Select, Sequence[Any]]:
        """Add a ``Subscriber`` outer-join to the ONT search query.

        Returns the augmented statement and a list of clause elements to be
        OR'd with the caller's existing search predicates.
        """
        search_subscriber = aliased(Subscriber)
        stmt = stmt.outerjoin(
            search_subscriber,
            search_subscriber.id == assignment_alias.subscriber_id,
        )
        extra_conditions = [
            search_subscriber.display_name.ilike(term),
            search_subscriber.subscriber_number.ilike(term),
            search_subscriber.email.ilike(term),
        ]
        return stmt, extra_conditions

    def get_template_context(
        self,
        db: Session,
        *,
        subscriber_id: object,
    ) -> dict[str, str]:
        """Return subscriber values used by network-domain template rendering."""
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            return {}
        return {
            "subscriber_code": getattr(subscriber, "external_code", "") or "",
            "subscriber_name": getattr(subscriber, "name", "") or "",
        }

    def get_or_create_inventory_subscriber(self, db: Session) -> Subscriber:
        """Return the synthetic subscriber used for inventory-owned CPE records."""
        inventory_email = "network-inventory@dotmac.local"
        subscriber = (
            db.query(Subscriber)
            .filter(Subscriber.email == inventory_email)
            .limit(1)
            .first()
        )
        if subscriber is not None:
            return subscriber

        new_subscriber = Subscriber(
            first_name="Network",
            last_name="Inventory",
            display_name="Network Inventory",
            email=inventory_email,
            status=SubscriberStatus.active,
            user_type=UserType.system_user,
            is_active=True,
            billing_enabled=False,
        )

        def create_inventory_subscriber() -> Subscriber:
            db.add(new_subscriber)
            db.flush()
            return new_subscriber

        try:
            if owner_command_active(db):
                return execute_owner_savepoint(db, create_inventory_subscriber)
            with db.begin_nested():
                db.add(new_subscriber)
                db.flush()
            return new_subscriber
        except IntegrityError:
            subscriber = (
                db.query(Subscriber)
                .filter(Subscriber.email == inventory_email)
                .limit(1)
                .first()
            )
            if subscriber is not None:
                return subscriber
            raise

    def get_inventory_subscriber(self, db: Session) -> Subscriber | None:
        """Return the synthetic inventory subscriber when it already exists."""
        inventory_email = "network-inventory@dotmac.local"
        return (
            db.query(Subscriber)
            .filter(Subscriber.email == inventory_email)
            .limit(1)
            .first()
        )

    def get_inventory_subscriber_id(self, db: Session) -> object | None:
        """Return the synthetic inventory subscriber identifier when present."""
        subscriber = self.get_inventory_subscriber(db)
        return subscriber.id if subscriber is not None else None


default_subscriber_validator = DefaultSubscriberValidator()
