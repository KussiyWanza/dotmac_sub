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

from app.models.auth import AuthenticationBinding, AuthProvider, UserCredential
from app.models.field_vendor import (
    DEFAULT_VENDOR_USER_ROLE,
    VENDOR_USER_ROLES,
    FieldVendor,
    FieldVendorUser,
)
from app.models.party import PartyDataClassification, PartyType
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import auth_flow as auth_flow_service
from app.services import credential_party_binding, staff_provisioning
from app.services import party as party_registry
from app.services.common import coerce_uuid
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.operator_tenant import operator_tenant_id
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
_ENABLE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="vendor portal login provisioning and revocation",
    name="enable_vendor_user_login",
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


@dataclass(frozen=True, slots=True)
class EnableVendorUserLogin:
    membership_id: UUID


@dataclass(frozen=True, slots=True)
class VendorUserLoginEnablement:
    membership_id: UUID
    system_user_id: UUID
    credential_id: UUID
    party_id: UUID
    repaired_projection: bool


def list_users(db: Session, field_vendor_id: UUID) -> list[FieldVendorUser]:
    return list(
        db.scalars(
            select(FieldVendorUser)
            .where(FieldVendorUser.vendor_id == coerce_uuid(field_vendor_id))
            .order_by(FieldVendorUser.created_at.asc())
        )
    )


def _local_authentication_binding(db: Session) -> AuthenticationBinding:
    binding = db.scalar(
        select(AuthenticationBinding)
        .where(AuthenticationBinding.binding_key == "local.default")
        .where(AuthenticationBinding.mechanism_code == AuthProvider.local.value)
        .where(AuthenticationBinding.is_active.is_(True))
    )
    if binding is not None:
        return binding

    binding = AuthenticationBinding(
        binding_key="local.default",
        mechanism_code=AuthProvider.local.value,
        name="Local password",
        description="Password verified against the stored hash.",
        is_active=True,
    )
    db.add(binding)
    db.flush()
    return binding


def _ensure_principal_party(
    db: Session,
    principal: SystemUser,
    *,
    context: CommandContext,
) -> UUID:
    if principal.person_party_id is not None:
        return principal.person_party_id

    display_name = principal.display_name or principal.email
    person = party_registry.create_party(
        db,
        party_type=PartyType.person,
        display_name=display_name,
        data_classification=PartyDataClassification.production,
        metadata={
            "vendor_identity_bootstrap": {
                "schema_version": 1,
                "owner": _OWNER,
                "command_id": str(context.command_id),
            }
        },
    )
    party_registry.bind_system_user_principal(
        db,
        system_user_id=principal.id,
        person_party_id=person.id,
        source="vendor-user-provisioning",
        reason=context.reason,
    )
    return person.id


def _project_local_credential(
    db: Session,
    credential: UserCredential,
    *,
    party_id: UUID,
    binding: AuthenticationBinding,
    reason: str,
) -> bool:
    if credential.system_user_id is None:
        raise VendorUserProvisioningError(
            "principal_not_vendor", "Vendor user credential is not system-user backed."
        )
    current = (
        credential.party_id,
        credential.authentication_binding_id,
        credential.tenant_id,
        credential.party_bound_at,
        credential.party_binding_source,
        credential.party_binding_reason,
    )
    if all(value is None for value in current):
        credential_party_binding.stage_credential_party_binding(
            db,
            credential_party_binding.CredentialPartyBinding(
                context=CommandContext.system(
                    actor=_SYSTEM_ACTOR,
                    scope="party:credential_authentication_projection",
                    reason=reason,
                ),
                credential_id=credential.id,
                expected_principal_kind=(
                    credential_party_binding.CredentialPrincipalKind.system_user
                ),
                expected_principal_id=credential.system_user_id,
                party_id=party_id,
                authentication_binding_id=binding.id,
                tenant_id=operator_tenant_id(),
                binding_source="vendor-user-provisioning",
                binding_reason=reason,
            ),
        )
        return True
    expected = (
        party_id,
        binding.id,
        operator_tenant_id(),
        "vendor-user-provisioning",
    )
    complete_identity = (
        credential.party_id,
        credential.authentication_binding_id,
        credential.tenant_id,
        credential.party_binding_source,
    )
    if (
        complete_identity != expected
        or credential.party_bound_at is None
        or not credential.party_binding_reason
    ):
        raise VendorUserProvisioningError(
            "credential_projection_conflict",
            "Vendor login credential has a conflicting Party projection.",
        )
    return False


def _active_local_credential(
    db: Session, principal: SystemUser
) -> UserCredential | None:
    return db.scalar(
        select(UserCredential)
        .where(UserCredential.system_user_id == principal.id)
        .where(UserCredential.provider == AuthProvider.local)
        .where(UserCredential.is_active.is_(True))
        .order_by(UserCredential.created_at.desc())
        .limit(1)
    )


def provision(
    db: Session,
    command: ProvisionVendorUser,
    *,
    actor: str = _SYSTEM_ACTOR,
    context: CommandContext | None = None,
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
    ctx = context or CommandContext.system(
        actor=actor,
        scope=str(command.field_vendor_id),
        reason="vendor_user_provisioning",
    )
    party_id = _ensure_principal_party(db, principal, context=ctx)
    binding = _local_authentication_binding(db)

    # Same shape as staff provisioning: an unusable placeholder that forces a
    # recovery flow. No usable secret is minted, logged, or returned.
    credential = UserCredential(
        system_user_id=principal.id,
        provider=AuthProvider.local,
        username=email,
        password_hash=auth_flow_service.hash_password(secrets.token_urlsafe(32)),
        must_change_password=True,
        is_active=True,
    )
    db.add(credential)
    db.flush()
    credential_party_binding.stage_credential_party_binding(
        db,
        credential_party_binding.CredentialPartyBinding(
            context=CommandContext.system(
                actor=ctx.actor,
                scope="party:credential_authentication_projection",
                reason=ctx.reason,
                correlation_id=ctx.correlation_id,
                causation_id=ctx.command_id,
            ),
            credential_id=credential.id,
            expected_principal_kind=(
                credential_party_binding.CredentialPrincipalKind.system_user
            ),
            expected_principal_id=principal.id,
            party_id=party_id,
            authentication_binding_id=binding.id,
            tenant_id=operator_tenant_id(),
            binding_source="vendor-user-provisioning",
            binding_reason=ctx.reason,
        ),
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
        operation=lambda: provision(db, command, actor=ctx.actor, context=ctx),
    )
    db.refresh(membership)
    return membership


def enable_login(
    db: Session,
    command: EnableVendorUserLogin,
    *,
    actor: str = _SYSTEM_ACTOR,
    context: CommandContext | None = None,
) -> VendorUserLoginEnablement:
    membership = db.get(FieldVendorUser, coerce_uuid(command.membership_id))
    if membership is None:
        raise VendorUserProvisioningError(
            "membership_not_found", "Vendor user not found.", kind="not_found"
        )
    if not membership.is_active:
        raise VendorUserProvisioningError(
            "membership_inactive", "Vendor user access is revoked."
        )
    vendor = db.get(FieldVendor, membership.vendor_id)
    if vendor is None or not vendor.is_active:
        raise VendorUserProvisioningError(
            "vendor_inactive", "Cannot enable login for an inactive vendor."
        )
    principal = db.get(SystemUser, membership.system_user_id)
    if principal is None or principal.user_type != UserType.vendor:
        raise VendorUserProvisioningError(
            "principal_not_vendor", "Vendor user principal is not available."
        )
    if not principal.is_active:
        principal.is_active = True
    ctx = context or CommandContext.system(
        actor=actor,
        scope=str(command.membership_id),
        reason="vendor_user_login_enablement",
    )
    party_id = _ensure_principal_party(db, principal, context=ctx)
    binding = _local_authentication_binding(db)
    credential = _active_local_credential(db, principal)
    if credential is None:
        raise VendorUserProvisioningError(
            "credential_not_found", "An active local credential was not found."
        )
    repaired = _project_local_credential(
        db,
        credential,
        party_id=party_id,
        binding=binding,
        reason=ctx.reason,
    )
    emit_event(
        db,
        EventType.vendor_user_role_changed,
        {
            "schema_version": 1,
            "vendor_user_id": str(membership.id),
            "field_vendor_id": str(membership.vendor_id),
            "role": membership.role,
            "login_enabled": True,
        },
        actor=actor,
    )
    return VendorUserLoginEnablement(
        membership_id=membership.id,
        system_user_id=principal.id,
        credential_id=credential.id,
        party_id=party_id,
        repaired_projection=repaired,
    )


def enable_login_committed(
    db: Session,
    command: EnableVendorUserLogin,
    *,
    context: CommandContext | None = None,
) -> VendorUserLoginEnablement:
    ctx = context or CommandContext.system(
        actor=_SYSTEM_ACTOR,
        scope=str(command.membership_id),
        reason="vendor_user_login_enablement",
    )
    return execute_owner_command(
        db,
        definition=_ENABLE_COMMAND,
        context=ctx,
        operation=lambda: enable_login(db, command, actor=ctx.actor, context=ctx),
    )


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
        # Deactivating the principal is not the revocation — it is the flag. The
        # consequence (every credential mechanism closed, every live session
        # revoked) belongs to one owner, so it cannot drift between the staff
        # path and this one. Without it this was the half-revocation the
        # docstring above warns about, one level down: no membership, no active
        # principal, and a still-usable credential.
        staff_provisioning.close_principal_access(db, principal.id)
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
