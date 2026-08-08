"""Service layer for user profile (/me) endpoints."""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.models.subscriber import ContactMethod, Gender, Subscriber
from app.models.system_user import SystemUser
from app.schemas.auth_flow import AvatarUploadResponse, MeResponse, MeUpdateRequest
from app.services import avatar as avatar_service
from app.services import staff_provisioning
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


def _build_me_response(
    person: Subscriber | SystemUser,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Build a MeResponse from a subscriber or system user and auth claims."""
    gender = "unknown"
    preferred_contact_method: str | None = None
    if isinstance(person, Subscriber):
        if person.gender is not None:
            gender = person.gender.value
        if person.preferred_contact_method is not None:
            preferred_contact_method = person.preferred_contact_method.value

    return MeResponse(
        id=person.id,
        first_name=person.first_name,
        last_name=person.last_name,
        display_name=person.display_name,
        avatar_url=getattr(person, "avatar_url", None),
        email=person.email,
        email_verified=getattr(person, "email_verified", False),
        phone=person.phone,
        date_of_birth=getattr(person, "date_of_birth", None),
        gender=gender,
        preferred_contact_method=preferred_contact_method,
        locale=getattr(person, "locale", None),
        timezone=getattr(person, "timezone", None),
        address_line1=getattr(person, "address_line1", None),
        address_line2=getattr(person, "address_line2", None),
        city=getattr(person, "city", None),
        region=getattr(person, "region", None),
        postal_code=getattr(person, "postal_code", None),
        country_code=getattr(person, "country_code", None),
        user_type=getattr(getattr(person, "user_type", None), "value", None)
        or "customer",
        roles=roles,
        scopes=scopes,
    )


def _get_subscriber_or_404(db: Session, subscriber_id: UUID) -> Subscriber:
    """Fetch a subscriber by ID or raise 404."""
    person = db.get(Subscriber, subscriber_id)
    if not person:
        raise HTTPException(status_code=404, detail="User not found")
    return person


def _get_system_user_or_404(db: Session, user_id: UUID) -> SystemUser:
    """Fetch a system user by ID or raise 404."""
    user = db.get(SystemUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def get_me(
    db: Session,
    principal_id: UUID,
    principal_type: str,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Return the current user's profile information."""
    principal: Subscriber | SystemUser
    if principal_type == "system_user":
        principal = _get_system_user_or_404(db, principal_id)
    else:
        principal = _get_subscriber_or_404(db, principal_id)
    return _build_me_response(principal, roles, scopes)


def update_me(
    db: Session,
    principal_id: UUID,
    principal_type: str,
    payload: MeUpdateRequest,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Update the current user's profile and return the updated profile."""
    update_data = payload.model_dump(exclude_unset=True)
    if principal_type == "system_user":
        system_disallowed_fields = {
            "date_of_birth",
            "gender",
            "preferred_contact_method",
            "locale",
            "timezone",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "country_code",
        }
        supported = {
            field.value: field for field in staff_provisioning.StaffIdentityField
        }
        fields = frozenset(
            supported[name]
            for name in update_data
            if name in supported and name not in system_disallowed_fields
        )
        if fields:
            desired_fingerprint = hashlib.sha256(
                "\x1f".join(
                    f"{field.value}={update_data.get(field.value)!s}"
                    for field in sorted(fields, key=lambda item: item.value)
                ).encode()
            ).hexdigest()
            command_id = uuid4()
            try:
                staff_provisioning.update_staff_identity(
                    db,
                    staff_provisioning.UpdateStaffIdentityCommand(
                        context=CommandContext(
                            command_id=command_id,
                            correlation_id=command_id,
                            actor=f"user:{principal_id}",
                            scope=staff_provisioning.STAFF_PROFILE_SCOPE,
                            reason="Authenticated staff API profile update",
                            idempotency_key=(
                                f"staff-api-profile:{principal_id}:"
                                f"{desired_fingerprint}"
                            ),
                        ),
                        user_id=principal_id,
                        fields=fields,
                        first_name=update_data.get("first_name"),
                        last_name=update_data.get("last_name"),
                        display_name=update_data.get("display_name"),
                        email=update_data.get("email"),
                        phone=update_data.get("phone"),
                    ),
                )
            except DomainError as exc:
                status_code = (
                    404 if exc.code.endswith("staff_account_not_found") else 409
                )
                raise HTTPException(
                    status_code=status_code, detail=exc.message
                ) from exc
        system_user = _get_system_user_or_404(db, principal_id)
        return _build_me_response(system_user, roles, scopes)

    person = _get_subscriber_or_404(db, principal_id)
    disallowed_fields: set[str] = set()

    # Email is identity-bearing (unique, NOT NULL) and re-arms verification, so
    # it goes through the shared helper rather than a raw setattr. This is what
    # lets a customer who has no email yet add one from the app and verify it.
    email_change = None
    if principal_type != "system_user":
        email_change = update_data.pop("email", None)

    for field, value in update_data.items():
        if field in disallowed_fields:
            continue
        # The DB columns are native enums / a normalised country code, but the
        # request carries plain strings — coerce so a raw setattr is valid.
        if field == "gender" and isinstance(value, str):
            value = Gender(value)
        elif field == "preferred_contact_method":
            value = ContactMethod(value) if value else None
        elif field == "country_code" and isinstance(value, str):
            value = value.strip().upper() or None
        setattr(person, field, value)

    db.flush()

    # Back-fill service-location coordinates from a self-service address edit so
    # the typed address lands on the map (best-effort; never blocks the save).
    if isinstance(person, Subscriber):
        _address_fields = {
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "country_code",
        }
        if _address_fields & set(update_data.keys()):
            from app.services import customer_location_requests as location_service

            location_service.geocode_service_address(db, person)

    if email_change is not None:
        from app.services import auth_flow

        auth_flow.set_subscriber_email(db, str(person.id), email_change)

    db.refresh(person)

    return _build_me_response(person, roles, scopes)


async def upload_avatar(
    db: Session,
    subscriber_id: UUID,
    file: UploadFile,
) -> AvatarUploadResponse:
    """Upload and replace the current user's avatar."""
    person = _get_subscriber_or_404(db, subscriber_id)

    # Delete old avatar if exists
    avatar_service.delete_avatar(person.avatar_url)

    # Save new avatar
    avatar_url = await avatar_service.save_avatar(file, str(person.id))

    # Update person record
    person.avatar_url = avatar_url
    db.flush()

    return AvatarUploadResponse(avatar_url=avatar_url)


def delete_avatar(
    db: Session,
    subscriber_id: UUID,
) -> None:
    """Delete the current user's avatar."""
    person = _get_subscriber_or_404(db, subscriber_id)

    avatar_service.delete_avatar(person.avatar_url)
    person.avatar_url = None
    db.flush()
