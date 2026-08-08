"""Service helpers for admin system user profile/detail/edit pages."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import ApiKey, AuthProvider, MFAMethod, UserCredential
from app.models.rbac import Permission, Role, SystemUserPermission, SystemUserRole
from app.models.system_user import SystemUser
from app.services import session_manager as session_manager_service
from app.services import staff_provisioning
from app.services.auth_flow import verify_password
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaffCredentialRecoveryView:
    allowed: bool
    reason: str | None = None


_CREDENTIAL_ISSUE_MESSAGES = {
    staff_provisioning.StaffLoginIdentityIssue.missing_credential: (
        "No local login credential is configured."
    ),
    staff_provisioning.StaffLoginIdentityIssue.multiple_credentials: (
        "Multiple local login credentials require reconciliation."
    ),
    staff_provisioning.StaffLoginIdentityIssue.username_mismatch: (
        "Login username does not match the profile email."
    ),
    staff_provisioning.StaffLoginIdentityIssue.username_conflict: (
        "Profile email is already used by another local login credential."
    ),
    staff_provisioning.StaffLoginIdentityIssue.activation_mismatch: (
        "Staff account and local credential activation states do not match."
    ),
}

_RECOVERY_BLOCK_MESSAGES = {
    staff_provisioning.StaffCredentialRecoveryBlock.inactive_account: (
        "Activate the staff account first."
    ),
    staff_provisioning.StaffCredentialRecoveryBlock.multiple_credentials: (
        "Multiple local login credentials require reconciliation."
    ),
    staff_provisioning.StaffCredentialRecoveryBlock.identity_conflict: (
        "Profile email is already used by another local login credential."
    ),
}


def get_subscriber(db: Session, user_id: str | UUID | None) -> SystemUser | None:
    """Resolve a system user by id."""
    if not user_id:
        return None
    try:
        return db.get(SystemUser, coerce_uuid(user_id))
    except (TypeError, ValueError):
        return None


def get_profile_data(db: Session, person_id: str | UUID | None) -> dict[str, Any]:
    """Return profile page data for the logged-in system user."""
    person = get_subscriber(db, person_id)
    if not person:
        return {
            "person": None,
            "credential": None,
            "mfa_enabled": False,
            "api_key_count": 0,
        }

    credential = (
        db.execute(
            select(UserCredential)
            .where(UserCredential.system_user_id == person.id)
            .where(UserCredential.is_active.is_(True))
            .limit(1)
        )
        .scalars()
        .first()
    )

    mfa_enabled = bool(
        db.scalar(
            select(MFAMethod.id)
            .where(MFAMethod.system_user_id == person.id)
            .where(MFAMethod.enabled.is_(True))
            .where(MFAMethod.is_active.is_(True))
            .limit(1)
        )
    )

    api_key_count = (
        db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.system_user_id == person.id)
            .where(ApiKey.is_active.is_(True))
            .where(ApiKey.revoked_at.is_(None))
        )
        or 0
    )

    return {
        "person": person,
        "credential": credential,
        "mfa_enabled": mfa_enabled,
        "api_key_count": api_key_count,
    }


def get_device_login_state(db: Session, person: SystemUser | None) -> dict[str, Any]:
    """Return self-service router device-login state for a system user."""
    from app.services.radius_population import get_device_login_sync_status

    sync_status = get_device_login_sync_status(db)
    if not person:
        return {
            "device_login_tier": None,
            "device_login_eligible": False,
            "device_login_enabled": False,
            "device_login_secret_set_at": None,
            "device_login_revoked_at": None,
            "device_login_sync_status": sync_status,
        }

    from app.services.device_login import derive_router_tier
    from app.services.radius_population import effective_perms, effective_roles

    roles = effective_roles(db, person.id)
    perms = effective_perms(db, person.id)
    tier = derive_router_tier(roles, perms)
    return {
        "device_login_tier": tier,
        "device_login_eligible": tier is not None,
        "device_login_enabled": bool(
            person.device_login_enabled
            and person.device_login_revoked_at is None
            and person.device_login_secret
        ),
        "device_login_secret_set_at": person.device_login_secret_set_at,
        "device_login_revoked_at": person.device_login_revoked_at,
        "device_login_sync_status": sync_status,
    }


def verify_current_password(
    db: Session, *, system_user_id: str | UUID, password: str
) -> bool:
    """Verify the active local portal password for a system user."""
    if not password:
        return False
    credential = (
        db.execute(
            select(UserCredential)
            .where(UserCredential.system_user_id == coerce_uuid(system_user_id))
            .where(UserCredential.provider == AuthProvider.local)
            .where(UserCredential.is_active.is_(True))
            .limit(1)
        )
        .scalars()
        .first()
    )
    return bool(credential and verify_password(password, credential.password_hash))


def get_user_detail_data(
    db: Session, user_id: str | UUID | None
) -> dict[str, Any] | None:
    """Return data needed for the user detail page."""
    user = get_subscriber(db, user_id)
    if not user:
        return None

    roles = (
        db.execute(
            select(Role)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .where(SystemUserRole.system_user_id == user.id)
            .where(Role.is_active.is_(True))
            .order_by(Role.name.asc())
        )
        .scalars()
        .all()
    )

    login_identity = staff_provisioning.get_staff_login_identity_view(
        db,
        user_id=user.id,
    )
    assert login_identity is not None
    credential_issue = (
        _CREDENTIAL_ISSUE_MESSAGES[login_identity.issue]
        if login_identity.issue is not None
        else None
    )
    credential_recovery = StaffCredentialRecoveryView(
        allowed=login_identity.recovery.allowed,
        reason=(
            _RECOVERY_BLOCK_MESSAGES[login_identity.recovery.blocked_by]
            if login_identity.recovery.blocked_by is not None
            else None
        ),
    )

    mfa_methods = (
        db.execute(select(MFAMethod).where(MFAMethod.system_user_id == user.id))
        .scalars()
        .all()
    )

    return {
        "user": user,
        "roles": roles,
        "credential": login_identity.credential,
        "credential_issue": credential_issue,
        "credential_issue_code": (
            login_identity.issue.value if login_identity.issue is not None else None
        ),
        "credential_recovery": credential_recovery,
        "mfa_methods": mfa_methods,
    }


def get_user_edit_data(
    db: Session, user_id: str | UUID | None
) -> dict[str, Any] | None:
    """Return data needed for the user edit page."""
    user = get_subscriber(db, user_id)
    if not user:
        return None

    roles = (
        db.execute(
            select(Role).where(Role.is_active.is_(True)).order_by(Role.name.asc())
        )
        .scalars()
        .all()
    )

    role_grants = tuple(
        db.execute(
            select(SystemUserRole).where(SystemUserRole.system_user_id == user.id)
        ).scalars()
    )
    current_role_ids = {str(grant.role_id) for grant in role_grants}
    managed_role_ids = {
        str(grant.role_id)
        for grant in role_grants
        if grant.source != "local" or grant.scope_type or grant.scope_id
    }

    all_permissions = (
        db.execute(
            select(Permission)
            .where(Permission.is_active.is_(True))
            .where(Permission.is_ui_assignable.is_(True))
            .order_by(Permission.key.asc())
        )
        .scalars()
        .all()
    )

    direct_permission_ids = {
        str(permission_id)
        for permission_id in db.execute(
            select(SystemUserPermission.permission_id).where(
                SystemUserPermission.system_user_id == user.id
            )
        ).scalars()
    }

    return {
        "user": user,
        "roles": roles,
        "current_role_ids": current_role_ids,
        "managed_role_ids": managed_role_ids,
        "all_permissions": all_permissions,
        "direct_permission_ids": direct_permission_ids,
    }


def build_profile_page_state(
    db: Session,
    *,
    current_user: dict | None,
    error: str | None = None,
    success: str | None = None,
    person_id: str | UUID | None = None,
    system_user_id: str | UUID | None = None,
    current_session_id: str | None = None,
) -> dict[str, Any]:
    resolved_person_id = system_user_id or person_id
    if resolved_person_id is None and current_user:
        resolved_person_id = current_user.get("person_id")
    profile_data = get_profile_data(db, resolved_person_id)
    device_login_state = get_device_login_state(db, profile_data["person"])
    active_sessions = []
    if profile_data["person"]:
        active_sessions = session_manager_service.list_sessions(
            db,
            profile_data["person"].id,
            current_session_id,
            principal_type="system_user",
        ).sessions
    other_session_count = sum(
        1 for session in active_sessions if not session.is_current
    )
    return {
        "person": profile_data["person"],
        "credential": profile_data["credential"],
        "mfa_enabled": profile_data["mfa_enabled"],
        "api_key_count": profile_data["api_key_count"],
        "active_sessions": active_sessions,
        "other_session_count": other_session_count,
        **device_login_state,
        "error": error,
        "success": success,
    }
