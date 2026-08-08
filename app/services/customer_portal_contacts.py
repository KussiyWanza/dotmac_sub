"""Customer portal contact management."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber, SubscriberContact
from app.services.customer_context import resolve_customer_context
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_identity_resolution import (
    rebuild_identity_index_for_subscriber,
)
from app.services.validation_api import validate_email_format

CONTACT_TYPES = ("general", "billing", "technical", "installation", "emergency")

_MULTI_ADDRESS = re.compile(r"[,;]")


def validated_contact_email(value: str | None) -> str | None:
    """Return the contact email, or refuse anything not a single address.

    A contact row feeds `Notification.recipient` directly, and that column is
    handed to SMTP as one `RCPT TO`. Two addresses in this one field therefore
    produce an SMTP 501 and the customer silently receives no billing notice at
    all, so refuse the value at the point of entry rather than storing a row
    that can never be delivered.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if _MULTI_ADDRESS.search(raw):
        raise ValueError(
            "Enter a single email address. Add a separate contact for each "
            "additional address."
        )
    valid, message = validate_email_format(raw)
    if not valid:
        raise ValueError(message or "Please enter a valid email address")
    return raw


@dataclass(frozen=True)
class ContactForm:
    full_name: str | None
    phone: str | None
    email: str | None
    whatsapp: str | None
    facebook: str | None
    instagram: str | None
    x_handle: str | None
    telegram: str | None
    linkedin: str | None
    other_social: str | None
    relationship: str | None
    contact_type: str
    is_authorized: bool
    receives_notifications: bool
    is_billing_contact: bool
    notes: str | None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_contact_form(
    *,
    full_name: str | None,
    phone: str | None,
    email: str | None,
    whatsapp: str | None,
    facebook: str | None,
    instagram: str | None,
    x_handle: str | None,
    telegram: str | None,
    linkedin: str | None,
    other_social: str | None,
    relationship: str | None,
    contact_type: str | None,
    is_authorized: bool,
    receives_notifications: bool,
    is_billing_contact: bool,
    notes: str | None,
    allow_authority_flags: bool = True,
) -> ContactForm:
    normalized_type = (contact_type or "general").strip().lower()
    if normalized_type not in CONTACT_TYPES:
        normalized_type = "general"
    return ContactForm(
        full_name=_clean(full_name),
        phone=normalize_phone_identifier(phone),
        email=normalize_email_identifier(email),
        whatsapp=normalize_phone_identifier(whatsapp),
        facebook=_clean(facebook),
        instagram=_clean(instagram),
        x_handle=_clean(x_handle),
        telegram=_clean(telegram),
        linkedin=_clean(linkedin),
        other_social=_clean(other_social),
        relationship=_clean(relationship),
        contact_type=normalized_type,
        is_authorized=bool(is_authorized) if allow_authority_flags else False,
        receives_notifications=bool(receives_notifications),
        is_billing_contact=(
            bool(is_billing_contact or normalized_type == "billing")
            if allow_authority_flags
            else False
        ),
        notes=_clean(notes),
    )


def _require_subscriber_id(customer: dict, db: Session) -> str:
    return resolve_customer_context(db, customer).require_account_id()


def _target_subscriber_id(customer: dict, db: Session) -> str:
    target_id = _require_subscriber_id(customer, db)
    allowed_ids = set(_allowed_subscriber_ids(customer, db))
    if allowed_ids and target_id not in allowed_ids:
        raise ValueError("Unable to resolve customer account.")
    return target_id


def _allowed_subscriber_ids(customer: dict, db: Session) -> list[str]:
    allowed_ids = list(resolve_customer_context(db, customer).allowed_subscriber_ids)
    if allowed_ids:
        return allowed_ids
    return [_require_subscriber_id(customer, db)]


def _allowed_subscriber_uuids(customer: dict, db: Session) -> list[UUID]:
    uuids: list[UUID] = []
    seen: set[UUID] = set()
    for raw_id in _allowed_subscriber_ids(customer, db):
        try:
            subscriber_uuid = UUID(str(raw_id))
        except ValueError:
            continue
        if subscriber_uuid in seen:
            continue
        seen.add(subscriber_uuid)
        uuids.append(subscriber_uuid)
    return uuids


def _subscriber(db: Session, subscriber_id: str) -> Subscriber | None:
    try:
        return db.get(Subscriber, UUID(str(subscriber_id)))
    except ValueError:
        return None


def duplicate_warnings(
    db: Session,
    *,
    subscriber_id: str,
    email: str | None,
    phone: str | None,
    exclude_contact_id: str | None = None,
) -> list[str]:
    warnings: list[str] = []
    subscriber_uuid = UUID(str(subscriber_id))
    exclude_uuid = UUID(str(exclude_contact_id)) if exclude_contact_id else None
    normalized_email = normalize_email_identifier(email)
    normalized_phone = normalize_phone_identifier(phone)

    if normalized_email:
        subscriber_rows = db.scalars(
            select(Subscriber).where(
                Subscriber.id != subscriber_uuid,
                Subscriber.email.is_not(None),
            )
        ).all()
        if any(
            normalize_email_identifier(item.email) == normalized_email
            for item in subscriber_rows
        ):
            warnings.append("This email is already used by another subscriber account.")
        contact_rows = db.scalars(
            select(SubscriberContact).where(SubscriberContact.email.is_not(None))
        ).all()
        if any(
            contact.id != exclude_uuid
            and normalize_email_identifier(contact.email) == normalized_email
            for contact in contact_rows
        ):
            warnings.append("This email is already used by another linked contact.")

    if normalized_phone:
        subscriber_rows = db.scalars(
            select(Subscriber).where(
                Subscriber.id != subscriber_uuid,
                Subscriber.phone.is_not(None),
            )
        ).all()
        if any(
            normalize_phone_identifier(item.phone) == normalized_phone
            for item in subscriber_rows
        ):
            warnings.append(
                "This phone number is already used by another subscriber account."
            )
        contact_rows = db.scalars(
            select(SubscriberContact).where(
                or_(
                    SubscriberContact.phone.is_not(None),
                    SubscriberContact.whatsapp.is_not(None),
                )
            )
        ).all()
        if any(
            contact.id != exclude_uuid
            and (
                normalize_phone_identifier(contact.phone) == normalized_phone
                or normalize_phone_identifier(contact.whatsapp) == normalized_phone
            )
            for contact in contact_rows
        ):
            warnings.append(
                "This phone number is already used by another linked contact."
            )

    return warnings


def get_contacts_page(db: Session, customer: dict) -> dict:
    allowed_subscriber_ids = _allowed_subscriber_ids(customer, db)
    subscriber = (
        _subscriber(db, allowed_subscriber_ids[0]) if allowed_subscriber_ids else None
    )
    allowed_subscriber_uuids = _allowed_subscriber_uuids(customer, db)
    contacts = db.scalars(
        select(SubscriberContact)
        .where(SubscriberContact.subscriber_id.in_(allowed_subscriber_uuids))
        .order_by(SubscriberContact.created_at.desc())
    ).all()
    return {
        "subscriber": subscriber,
        "contacts": contacts,
        "contact_types": CONTACT_TYPES,
    }


def _has_contact_channel(form: ContactForm) -> bool:
    return any(
        (
            form.phone,
            form.email,
            form.whatsapp,
            form.facebook,
            form.instagram,
            form.x_handle,
            form.telegram,
            form.linkedin,
            form.other_social,
        )
    )


def list_contacts(db: Session, customer: dict) -> list[SubscriberContact]:
    """The caller's subscriber contacts, newest first (self/allowed scoped)."""
    allowed_subscriber_uuids = _allowed_subscriber_uuids(customer, db)
    return list(
        db.scalars(
            select(SubscriberContact)
            .where(SubscriberContact.subscriber_id.in_(allowed_subscriber_uuids))
            .order_by(SubscriberContact.created_at.desc())
        ).all()
    )


def get_owned_contact(
    db: Session, customer: dict, contact_id: str
) -> SubscriberContact | None:
    """Fetch a contact only if it belongs to the caller's allowed subscriber(s)."""
    try:
        contact_uuid = UUID(str(contact_id))
    except ValueError:
        return None
    return db.scalar(
        select(SubscriberContact).where(
            SubscriberContact.id == contact_uuid,
            SubscriberContact.subscriber_id.in_(
                _allowed_subscriber_uuids(customer, db)
            ),
        )
    )


def create_contact_returning(
    db: Session, customer: dict, form: ContactForm
) -> tuple[SubscriberContact, list[str]]:
    """Create a contact, returning the saved row plus duplicate warnings.

    Shared core for both the web flow (``create_contact``) and the customer
    self-care API. Scoping/validation are identical."""
    if not _has_contact_channel(form):
        raise ValueError("Enter at least one phone, email, or social media contact.")
    email = validated_contact_email(form.email)
    subscriber_id = _target_subscriber_id(customer, db)
    warnings = duplicate_warnings(
        db,
        subscriber_id=subscriber_id,
        email=email,
        phone=form.phone,
    )
    contact = SubscriberContact(
        subscriber_id=UUID(str(subscriber_id)),
        full_name=form.full_name,
        phone=form.phone,
        email=email,
        whatsapp=form.whatsapp,
        facebook=form.facebook,
        instagram=form.instagram,
        x_handle=form.x_handle,
        telegram=form.telegram,
        linkedin=form.linkedin,
        other_social=form.other_social,
        relationship=form.relationship,
        contact_type=form.contact_type,
        is_authorized=form.is_authorized,
        receives_notifications=form.receives_notifications,
        is_billing_contact=form.is_billing_contact,
        notes=form.notes,
    )
    db.add(contact)
    db.flush()
    rebuild_identity_index_for_subscriber(db, contact.subscriber_id)
    db.commit()
    db.refresh(contact)
    return contact, warnings


def create_contact(db: Session, customer: dict, form: ContactForm) -> list[str]:
    _contact, warnings = create_contact_returning(db, customer, form)
    return warnings


def update_contact_returning(
    db: Session, customer: dict, contact_id: str, form: ContactForm
) -> tuple[SubscriberContact, list[str]]:
    """Update a contact (full replace), returning the saved row plus warnings.

    Shared core for both the web flow (``update_contact``) and the API."""
    if not _has_contact_channel(form):
        raise ValueError("Enter at least one phone, email, or social media contact.")
    email = validated_contact_email(form.email)
    try:
        contact_uuid = UUID(str(contact_id))
    except ValueError as exc:
        raise ValueError("Contact not found.") from exc
    contact = db.scalar(
        select(SubscriberContact).where(
            SubscriberContact.id == contact_uuid,
            SubscriberContact.subscriber_id.in_(
                _allowed_subscriber_uuids(customer, db)
            ),
        )
    )
    if not contact:
        raise ValueError("Contact not found.")
    warnings = duplicate_warnings(
        db,
        subscriber_id=str(contact.subscriber_id),
        email=email,
        phone=form.phone,
        exclude_contact_id=contact_id,
    )
    contact.full_name = form.full_name
    contact.phone = form.phone
    contact.email = email
    contact.whatsapp = form.whatsapp
    contact.facebook = form.facebook
    contact.instagram = form.instagram
    contact.x_handle = form.x_handle
    contact.telegram = form.telegram
    contact.linkedin = form.linkedin
    contact.other_social = form.other_social
    contact.relationship = form.relationship
    contact.contact_type = form.contact_type
    contact.is_authorized = form.is_authorized
    contact.receives_notifications = form.receives_notifications
    contact.is_billing_contact = form.is_billing_contact
    contact.notes = form.notes
    rebuild_identity_index_for_subscriber(db, contact.subscriber_id)
    db.commit()
    db.refresh(contact)
    return contact, warnings


def update_contact(
    db: Session, customer: dict, contact_id: str, form: ContactForm
) -> list[str]:
    _contact, warnings = update_contact_returning(db, customer, contact_id, form)
    return warnings


def delete_contact(db: Session, customer: dict, contact_id: str) -> None:
    try:
        contact_uuid = UUID(str(contact_id))
    except ValueError as exc:
        raise ValueError("Contact not found.") from exc

    contact = db.scalar(
        select(SubscriberContact).where(
            SubscriberContact.id == contact_uuid,
            SubscriberContact.subscriber_id.in_(
                _allowed_subscriber_uuids(customer, db)
            ),
        )
    )
    if not contact:
        raise ValueError("Contact not found.")

    subscriber_id = contact.subscriber_id
    db.delete(contact)
    db.flush()
    rebuild_identity_index_for_subscriber(db, subscriber_id)
    db.commit()
