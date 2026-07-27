"""Canonical owner for vendor portal logins.

Nothing in Sub created a ``FieldVendorUser``, and that row is what
``field.vendor_auth`` resolves through — so the vendor portal could not be
entered by anyone. The CRM import populated ``vendor_users`` instead, which is
the vestigial table with no consumers. This owner fills the real gap.

One vendor login is three rows that must exist together or not at all:

* ``system_users`` — the authenticating principal, marked ``UserType.vendor``
  so an external contractor is never mistaken for an employee.
* ``user_credentials`` — a local credential with ``must_change_password`` set,
  mirroring ``auth.staff_provisioning``. This owner never mints or delivers a
  usable secret; recovery owns that.
* ``field_vendor_users`` — the membership binding the principal to one vendor
  organisation, carrying the role that
  ``app.services.field.vendor_capabilities`` resolves to a capability set.

Writing any subset produces a broken identity: a principal with no vendor, or
a membership no one can authenticate as. They are staged in one transaction.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, UserCredential
from app.models.field_vendor import (
    DEFAULT_VENDOR_USER_ROLE,
    VENDOR_USER_ROLES,
    FieldVendor,
    FieldVendorUser,
)
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import auth_flow as auth_flow_service
from app.services.common import coerce_uuid
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_OWNER = "auth.vendor_user_provisioning"
_SYSTEM_ACTOR = "system:vendor-admin"
_PROVISION_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="vendor portal login provisioning and revocation",
    name="provision_vendor_user",
)
_REVOKE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="vendor portal login provisioning and revocation",
    name="revoke_vendor_user",
)
_ROLE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="vendor organisation role assignment",
    name="set_vendor_user_role",
)


class VendorUserProvisioningError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def normalize_role(role: str | None) -> str:
    candidate = (role or "").strip().lower() or DEFAULT_VENDOR_USER_ROLE
    if candidate not in VENDOR_USER_ROLES:
        raise VendorUserProvisioningError(
            "unknown_role",
            f"'{candidate}' is not a vendor role ({', '.join(VENDOR_USER_ROLES)}).",
        )
    return candidate


@dataclass(frozen=True, slots=True)
class ProvisionVendorUser:
    field_vendor_id: UUID
    first_name: str
    last_name: str
    email: str
    role: str | None = None


def list_users(db: Session, field_vendor_id: UUID) -> list[FieldVendorUser]:
    return list(
        db.scalars(
            select(FieldVendorUser)
            .where(FieldVendorUser.vendor_id == coerce_uuid(field_vendor_id))
            .order_by(FieldVendorUser.created_at.asc())
        )
    )


def provision(
    db: Session,
    command: ProvisionVendorUser,
    *,
    actor: str = _SYSTEM_ACTOR,
) -> FieldVendorUser:
    """Stage one complete vendor login. Caller owns commit."""

    first_name = _clean(command.first_name)
    last_name = _clean(command.last_name)
    email = (_clean(command.email) or "").lower() or None
    if not first_name or not last_name:
        raise VendorUserProvisioningError(
            "name_required", "Vendor user first and last name are required."
        )
    if not email or "@" not in email:
        raise VendorUserProvisioningError(
            "email_required", "A valid vendor user email is required."
        )
    role = normalize_role(command.role)

    vendor = db.get(FieldVendor, coerce_uuid(command.field_vendor_id))
    if vendor is None:
        raise VendorUserProvisioningError(
            "vendor_not_found", "Vendor not found.", kind="not_found"
        )
    if not vendor.is_active:
        # Provisioning a login for a deactivated vendor would re-open access
        # staff deliberately withdrew.
        raise VendorUserProvisioningError(
            "vendor_inactive", "Cannot add a login to an inactive vendor."
        )

    existing_principal = db.scalars(
        select(SystemUser).where(func.lower(SystemUser.email) == email)
    ).one_or_none()
    if existing_principal is not None:
        # ``system_users.email`` is unique across staff and vendors alike, so a
        # collision here is an identity question, not a form typo. Refuse
        # rather than attach a vendor membership to an employee principal.
        raise VendorUserProvisioningError(
            "email_in_use",
            f"'{email}' already belongs to another account.",
        )

    principal = SystemUser(
        first_name=first_name,
        last_name=last_name,
        display_name=f"{first_name} {last_name}".strip(),
        email=email,
        user_type=UserType.vendor,
        is_active=True,
    )
    db.add(principal)
    db.flush()

    # Same shape as staff provisioning: an unusable placeholder that forces a
    # recovery flow. No usable secret is minted, logged, or returned.
    db.add(
        UserCredential(
            system_user_id=principal.id,
            provider=AuthProvider.local,
            username=email,
            password_hash=auth_flow_service.hash_password(secrets.token_urlsafe(32)),
            must_change_password=True,
            is_active=True,
        )
    )
    membership = FieldVendorUser(
        vendor_id=vendor.id,
        system_user_id=principal.id,
        role=role,
        is_active=True,
    )
    db.add(membership)
    db.flush()
    emit_event(
        db,
        EventType.vendor_user_provisioned,
        {
            "schema_version": 1,
            "vendor_user_id": str(membership.id),
            "field_vendor_id": str(vendor.id),
            "system_user_id": str(principal.id),
            "role": role,
        },
        actor=actor,
    )
    return membership


def provision_committed(
    db: Session,
    command: ProvisionVendorUser,
    *,
    context: CommandContext | None = None,
) -> FieldVendorUser:
    """Provision one login in a complete owner transaction."""
    ctx = context or CommandContext.system(
        actor=_SYSTEM_ACTOR,
        scope=str(command.field_vendor_id),
        reason="vendor_user_provisioning",
    )
    membership = execute_owner_command(
        db,
        definition=_PROVISION_COMMAND,
        context=ctx,
        operation=lambda: provision(db, command, actor=ctx.actor),
    )
    db.refresh(membership)
    return membership


def set_role(
    db: Session,
    membership_id: UUID,
    role: str,
    *,
    actor: str = _SYSTEM_ACTOR,
) -> FieldVendorUser:
    membership = db.get(FieldVendorUser, coerce_uuid(membership_id))
    if membership is None:
        raise VendorUserProvisioningError(
            "membership_not_found", "Vendor user not found.", kind="not_found"
        )
    membership.role = normalize_role(role)
    db.flush()
    emit_event(
        db,
        EventType.vendor_user_role_changed,
        {
            "schema_version": 1,
            "vendor_user_id": str(membership.id),
            "field_vendor_id": str(membership.vendor_id),
            "role": membership.role,
        },
        actor=actor,
    )
    return membership


def set_role_committed(
    db: Session,
    membership_id: UUID,
    role: str,
    *,
    context: CommandContext | None = None,
) -> FieldVendorUser:
    ctx = context or CommandContext.system(
        actor=_SYSTEM_ACTOR,
        scope=str(membership_id),
        reason="vendor_user_role_change",
    )
    membership = execute_owner_command(
        db,
        definition=_ROLE_COMMAND,
        context=ctx,
        operation=lambda: set_role(db, membership_id, role, actor=ctx.actor),
    )
    db.refresh(membership)
    return membership


def revoke(
    db: Session,
    membership_id: UUID,
    *,
    actor: str = _SYSTEM_ACTOR,
) -> FieldVendorUser:
    """Withdraw one vendor login without touching the vendor organisation.

    Both rows are deactivated: ``vendor_auth`` filters the membership, but a
    live principal with no membership is still an authenticable account, and
    leaving it enabled is the same class of half-revocation that made vendor
    deactivation unsafe.
    """
    membership = db.get(FieldVendorUser, coerce_uuid(membership_id))
    if membership is None:
        raise VendorUserProvisioningError(
            "membership_not_found", "Vendor user not found.", kind="not_found"
        )
    membership.is_active = False
    principal = db.get(SystemUser, membership.system_user_id)
    if principal is not None and principal.user_type == UserType.vendor:
        principal.is_active = False
    db.flush()
    emit_event(
        db,
        EventType.vendor_user_revoked,
        {
            "schema_version": 1,
            "vendor_user_id": str(membership.id),
            "field_vendor_id": str(membership.vendor_id),
            "system_user_id": str(membership.system_user_id),
        },
        actor=actor,
    )
    return membership


def revoke_committed(
    db: Session,
    membership_id: UUID,
    *,
    context: CommandContext | None = None,
) -> FieldVendorUser:
    ctx = context or CommandContext.system(
        actor=_SYSTEM_ACTOR,
        scope=str(membership_id),
        reason="vendor_user_revocation",
    )
    membership = execute_owner_command(
        db,
        definition=_REVOKE_COMMAND,
        context=ctx,
        operation=lambda: revoke(db, membership_id, actor=ctx.actor),
    )
    db.refresh(membership)
    return membership
