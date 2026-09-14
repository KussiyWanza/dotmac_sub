"""Typed, transaction-neutral canonical Customer profile patch owner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import Address, AddressType, Gender, Subscriber
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType


class CanonicalCustomerProfileField(StrEnum):
    """Customer fields writable by this explicit participant."""

    name = "name"
    phone = "phone"
    address = "address"
    email = "email"
    organization = "organization"
    city_region = "city_region"
    country = "country"
    date_of_birth = "date_of_birth"
    gender = "gender"
    nin = "nin"


@dataclass(frozen=True, slots=True)
class CanonicalCustomerProfileValues:
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    email: str | None = None
    organization: str | None = None
    city_region: str | None = None
    country: str | None = None
    date_of_birth: date | None = None
    gender: Gender | None = None
    nin: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyCanonicalCustomerProfilePatch:
    subscriber_id: UUID
    submitted_fields: frozenset[CanonicalCustomerProfileField]
    values: CanonicalCustomerProfileValues
    source: str
    actor_id: UUID | None


@dataclass(frozen=True, slots=True)
class CanonicalCustomerProfilePatchOutcome:
    subscriber: Subscriber
    service_address: Address | None


class CanonicalCustomerProfilePatchError(DomainError):
    """The canonical Customer profile patch owner refused a patch."""


def apply_canonical_customer_profile_patch(
    db: Session, command: ApplyCanonicalCustomerProfilePatch
) -> CanonicalCustomerProfilePatchOutcome:
    """Apply validated Customer fields inside a registered coordinator.

    This participant cannot create a Customer and never completes the caller's
    transaction.
    """

    subscriber = db.scalar(
        select(Subscriber)
        .where(Subscriber.id == command.subscriber_id)
        .with_for_update()
    )
    if subscriber is None:
        raise CanonicalCustomerProfilePatchError(
            code="customer.accounts.customer_not_found",
            message="The Customer was not found.",
            details={"customer_id": str(command.subscriber_id)},
        )
    fields = command.submitted_fields
    values = command.values
    if CanonicalCustomerProfileField.name in fields:
        if not values.name:
            raise CanonicalCustomerProfilePatchError(
                code="customer.accounts.invalid_name",
                message="Name cannot be blank.",
            )
        parts = values.name.split(" ", 1)
        subscriber.first_name = parts[0]
        subscriber.last_name = parts[1] if len(parts) > 1 else ""
        subscriber.display_name = values.name
    if CanonicalCustomerProfileField.phone in fields:
        subscriber.phone = values.phone
    if CanonicalCustomerProfileField.address in fields:
        subscriber.address_line1 = values.address
    if CanonicalCustomerProfileField.email in fields:
        if not values.email:
            raise CanonicalCustomerProfilePatchError(
                code="customer.accounts.invalid_email",
                message="Email cannot be blank.",
            )
        email_changed = (subscriber.email or "").casefold() != values.email.casefold()
        subscriber.email = values.email
        if email_changed:
            subscriber.email_verified = False
    if CanonicalCustomerProfileField.organization in fields:
        subscriber.company_name = values.organization
    if CanonicalCustomerProfileField.city_region in fields:
        subscriber.city = values.city_region
    if CanonicalCustomerProfileField.country in fields:
        subscriber.country_code = values.country
    if CanonicalCustomerProfileField.date_of_birth in fields:
        subscriber.date_of_birth = values.date_of_birth
    if CanonicalCustomerProfileField.gender in fields:
        subscriber.gender = values.gender or Gender.unknown
    if CanonicalCustomerProfileField.nin in fields:
        if bool((subscriber.metadata_ or {}).get("nin_verified")) and (
            subscriber.nin or ""
        ) != (values.nin or ""):
            raise CanonicalCustomerProfilePatchError(
                code="customer.accounts.verified_nin_locked",
                message="A verified NIN cannot be replaced from Inbox.",
            )
        subscriber.nin = values.nin

    service_address = db.scalar(
        select(Address)
        .where(
            Address.subscriber_id == subscriber.id,
            Address.address_type == AddressType.service,
        )
        .order_by(Address.is_primary.desc(), Address.created_at, Address.id)
        .with_for_update()
    )
    address_fields = {
        CanonicalCustomerProfileField.address,
        CanonicalCustomerProfileField.city_region,
        CanonicalCustomerProfileField.country,
    }
    if fields.intersection(address_fields):
        if service_address is None and subscriber.address_line1:
            service_address = Address(
                subscriber_id=subscriber.id,
                address_type=AddressType.service,
                address_line1=subscriber.address_line1,
                is_primary=True,
            )
            db.add(service_address)
        if service_address is not None:
            service_address.address_line1 = subscriber.address_line1 or ""
            service_address.address_line2 = subscriber.address_line2
            service_address.city = subscriber.city
            service_address.region = subscriber.region
            service_address.postal_code = subscriber.postal_code
            service_address.country_code = subscriber.country_code
    db.flush()
    emit_event(
        db,
        EventType.subscriber_updated,
        {
            "schema_version": 1,
            "subscriber_id": str(subscriber.id),
            "changed_fields": sorted(field.value for field in fields),
            "source": command.source,
        },
        actor=str(command.actor_id) if command.actor_id else command.source,
        subscriber_id=subscriber.id,
    )
    return CanonicalCustomerProfilePatchOutcome(
        subscriber=subscriber,
        service_address=service_address,
    )
