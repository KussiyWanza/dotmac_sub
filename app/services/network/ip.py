"""IP management services."""

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import (
    IPAssignment,
    IpBlock,
    IpPool,
    IPv4Address,
    IPv6Address,
    IPVersion,
)
from app.schemas.network import (
    IPAssignmentCreate,
    IPAssignmentUpdate,
    IpBlockUpdate,
    IpPoolCreate,
    IpPoolUpdate,
    IPv4AddressUpdate,
    IPv6AddressUpdate,
)
from app.services import settings_spec
from app.services.common import coerce_uuid
from app.services.crud import CRUDManager
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
)
from app.services.query_builders import apply_active_state, apply_optional_equals
from app.validators import network as network_validators

logger = logging.getLogger(__name__)
ACTIVE_SUBSCRIPTION_STATUS = "active"
SERVICE_ALLOCATION_TYPES = frozenset({"wan", "static"})


def _subscription_model() -> type[Any]:
    return IPAssignment.subscription.property.mapper.class_


def _is_active_subscription(subscription: Any) -> bool:
    status = getattr(subscription, "status", None)
    return getattr(status, "value", status) == ACTIVE_SUBSCRIPTION_STATUS


def clear_released_ipv4_allocation_type(
    db: Session, address: IPv4Address | None
) -> bool:
    """Clear stale service allocation markers after an IPv4 assignment release."""
    if address is None:
        return False
    allocation_type = str(address.allocation_type or "").strip().lower()
    if allocation_type not in SERVICE_ALLOCATION_TYPES:
        return False
    if bool(address.is_reserved) or address.ont_unit_id is not None:
        return False
    db.flush()
    active_assignment = (
        db.query(IPAssignment.id)
        .filter(IPAssignment.ipv4_address_id == address.id)
        .filter(IPAssignment.is_active.is_(True))
        .first()
    )
    if active_assignment is not None:
        return False
    address.allocation_type = None
    return True


class IPAssignments(CRUDManager[IPAssignment]):
    model = IPAssignment
    not_found_detail = "IP assignment not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def _resolve_owner(db: Session, data: dict) -> None:
        subscription_id = data.get("subscription_id")
        subscriber_id = data.get("subscriber_id")
        if subscription_id is None:
            return
        subscription = db.get(_subscription_model(), subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if subscriber_id is None:
            data["subscriber_id"] = subscription.subscriber_id
        elif str(subscriber_id) != str(subscription.subscriber_id):
            raise HTTPException(
                status_code=400, detail="Subscription does not belong to subscriber"
            )

    @staticmethod
    def _single_active_subscription_for_subscriber(db: Session, subscriber_id) -> Any:
        subscription_model = _subscription_model()
        active_subscriptions = (
            db.query(subscription_model)
            .filter(subscription_model.subscriber_id == subscriber_id)
            .filter(subscription_model.status == ACTIVE_SUBSCRIPTION_STATUS)
            .all()
        )
        if len(active_subscriptions) != 1:
            return None
        return active_subscriptions[0]

    @classmethod
    def _sync_subscription_ipv4(
        cls,
        db: Session,
        assignment: IPAssignment,
        *,
        released_ip: str | None = None,
    ) -> None:
        if assignment.ip_version != IPVersion.ipv4:
            return
        subscription = assignment.subscription
        if subscription is None and assignment.subscriber_id is not None:
            subscription = cls._single_active_subscription_for_subscriber(
                db, assignment.subscriber_id
            )
        if subscription is None or not _is_active_subscription(subscription):
            return

        # Only the PRIMARY assignment decides the served address. A service may
        # hold several addresses; writing the column for any of them let a
        # generic CRUD call silently re-address a live customer just by adding
        # an additional holding.
        #
        # Where this is the service's ONLY active IPv4 holding there is exactly
        # one possible answer, so ask the owner to mark it rather than skipping
        # -- otherwise provisioning a first address through this path would
        # leave the customer with no served address at all. Requesting is not
        # deciding: with several holdings and none marked, this fails closed and
        # leaves the column alone for the assignment owner to adjudicate.
        if assignment.is_active and not assignment.is_primary:
            from app.services.ip_assignment_lifecycle import (
                mark_primary_ipv4_assignment,
            )

            siblings = (
                db.query(IPAssignment)
                .filter(IPAssignment.subscription_id == subscription.id)
                .filter(IPAssignment.is_active.is_(True))
                .filter(IPAssignment.ipv4_address_id.is_not(None))
                .count()
            )
            if siblings == 1 and assignment.ipv4_address_id is not None:
                mark_primary_ipv4_assignment(
                    db,
                    subscription_id=subscription.id,
                    ipv4_address_id=assignment.ipv4_address_id,
                )
        if not assignment.is_primary:
            return

        if not assignment.is_active:
            if released_ip and subscription.ipv4_address == released_ip:
                subscription.ipv4_address = None
            return

        address = assignment.ipv4_address
        if address is not None:
            subscription.ipv4_address = address.address

    @staticmethod
    def create(db: Session, payload: IPAssignmentCreate):
        data = payload.model_dump()
        IPAssignments._resolve_owner(db, data)
        if data.get("subscriber_id") is not None:
            network_validators.validate_ip_assignment_links(
                db,
                str(data["subscriber_id"]),
                str(data["service_address_id"])
                if data.get("service_address_id")
                else None,
                str(data["subscription_id"]) if data.get("subscription_id") else None,
            )
        elif data.get("service_address_id") is not None:
            raise HTTPException(
                status_code=400,
                detail="service_address_id requires subscriber_id",
            )
        assignment = IPAssignment(**data)
        db.add(assignment)
        db.flush()
        IPAssignments._sync_subscription_ipv4(db, assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @classmethod
    def get(cls, db: Session, assignment_id: str):
        return super().get(db, assignment_id)

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None = None,
        subscription_id: str | None = None,
        ip_version: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
        is_active: bool | None = None,
    ):
        query = db.query(IPAssignment)
        query = apply_optional_equals(
            query,
            {
                IPAssignment.subscriber_id: subscriber_id,
                IPAssignment.subscription_id: subscription_id,
                IPAssignment.ip_version: ip_version,
            },
        )
        query = apply_active_state(query, IPAssignment.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": IPAssignment.created_at,
                "ipv4_address_id": IPAssignment.ipv4_address_id,
            },
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, assignment_id: str, payload: IPAssignmentUpdate):
        assignment = db.get(IPAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="IP assignment not found")
        previous_ipv4 = (
            assignment.ipv4_address.address
            if assignment.ip_version == IPVersion.ipv4 and assignment.ipv4_address
            else None
        )
        previous_subscription = assignment.subscription
        if previous_subscription is None and assignment.subscriber_id is not None:
            previous_subscription = (
                IPAssignments._single_active_subscription_for_subscriber(
                    db, assignment.subscriber_id
                )
            )
        data = payload.model_dump(exclude_unset=True)
        IPAssignments._resolve_owner(db, data)
        resolved_subscriber_id = data.get("subscriber_id", assignment.subscriber_id)
        resolved_subscription_id = data.get(
            "subscription_id", assignment.subscription_id
        )
        service_address_id = data.get(
            "service_address_id", assignment.service_address_id
        )
        if resolved_subscriber_id is not None:
            network_validators.validate_ip_assignment_links(
                db,
                str(resolved_subscriber_id),
                str(service_address_id) if service_address_id else None,
                str(resolved_subscription_id) if resolved_subscription_id else None,
            )
        elif service_address_id is not None:
            raise HTTPException(
                status_code=400,
                detail="service_address_id requires subscriber_id",
            )
        for key, value in data.items():
            setattr(assignment, key, value)
        db.flush()
        if (
            previous_subscription is not None
            and previous_ipv4
            and previous_subscription != assignment.subscription
            and previous_subscription.ipv4_address == previous_ipv4
        ):
            previous_subscription.ipv4_address = None
        IPAssignments._sync_subscription_ipv4(db, assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @classmethod
    def delete(cls, db: Session, assignment_id: str):
        assignment = cls._get_or_404(db, assignment_id)
        released_ip = (
            assignment.ipv4_address.address
            if assignment.ip_version == IPVersion.ipv4 and assignment.ipv4_address
            else None
        )
        assignment.is_active = False
        db.flush()
        if assignment.ip_version == IPVersion.ipv4:
            clear_released_ipv4_allocation_type(db, assignment.ipv4_address)
        cls._sync_subscription_ipv4(db, assignment, released_ip=released_ip)
        db.commit()


class IpPools(CRUDManager[IpPool]):
    model = IpPool
    not_found_detail = "IP pool not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: IpPoolCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "ip_version" not in fields_set:
            default_version = settings_spec.resolve_value(
                db, SettingDomain.network, "default_ip_version"
            )
            if default_version:
                data["ip_version"] = _validate_enum(
                    default_version, IPVersion, "ip_version"
                )
        pool = IpPool(**data)
        db.add(pool)
        db.commit()
        db.refresh(pool)
        return pool

    @classmethod
    def get(cls, db: Session, pool_id: str):
        return cls._get_or_404(db, coerce_uuid(pool_id))

    @staticmethod
    def list(
        db: Session,
        ip_version: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IpPool)
        query = apply_optional_equals(query, {IpPool.ip_version: ip_version})
        query = apply_active_state(query, IpPool.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IpPool.created_at, "name": IpPool.name},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def update(cls, db: Session, pool_id: str, payload: IpPoolUpdate):
        return super().update(db, pool_id, payload)

    @classmethod
    def delete(cls, db: Session, pool_id: str):
        return super().delete(db, pool_id)


class IpBlocks(CRUDManager[IpBlock]):
    model = IpBlock
    not_found_detail = "IP block not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IpBlock)
        query = apply_optional_equals(query, {IpBlock.pool_id: pool_id})
        query = apply_active_state(query, IpBlock.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IpBlock.created_at, "cidr": IpBlock.cidr},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, block_id: str):
        return super().get(db, block_id)

    @classmethod
    def update(cls, db: Session, block_id: str, payload: IpBlockUpdate):
        return super().update(db, block_id, payload)

    @classmethod
    def delete(cls, db: Session, block_id: str):
        return super().delete(db, block_id)


class IPv4Addresses(CRUDManager[IPv4Address]):
    model = IPv4Address
    not_found_detail = "IPv4 address not found"

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None = None,
        is_reserved: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        query = db.query(IPv4Address)
        query = apply_optional_equals(
            query,
            {
                IPv4Address.pool_id: pool_id,
                IPv4Address.is_reserved: is_reserved,
            },
        )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPv4Address.created_at, "address": IPv4Address.address},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, address_id: str):
        return super().get(db, address_id)

    @classmethod
    def update(cls, db: Session, address_id: str, payload: IPv4AddressUpdate):
        return super().update(db, address_id, payload)

    @classmethod
    def delete(cls, db: Session, address_id: str):
        return super().delete(db, address_id)


class IPv6Addresses(CRUDManager[IPv6Address]):
    model = IPv6Address
    not_found_detail = "IPv6 address not found"

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None = None,
        is_reserved: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        query = db.query(IPv6Address)
        query = apply_optional_equals(
            query,
            {
                IPv6Address.pool_id: pool_id,
                IPv6Address.is_reserved: is_reserved,
            },
        )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPv6Address.created_at, "address": IPv6Address.address},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, address_id: str):
        return super().get(db, address_id)

    @classmethod
    def update(cls, db: Session, address_id: str, payload: IPv6AddressUpdate):
        return super().update(db, address_id, payload)

    @classmethod
    def delete(cls, db: Session, address_id: str):
        return super().delete(db, address_id)


ip_assignments = IPAssignments()
ip_pools = IpPools()
ip_blocks = IpBlocks()
ipv4_addresses = IPv4Addresses()
ipv6_addresses = IPv6Addresses()
