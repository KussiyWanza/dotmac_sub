"""Canonical owner for system-user role and direct-permission assignments."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.system_user import SystemUser
from app.services import auth_cache
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.entitlement_revocation import (
    PRINCIPAL_SYSTEM_USER,
    revoke_for_entitlement_reduction,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.session_hooks import run_after_commit

LOCAL_ROLE_SOURCE = "local"
ASSIGNMENT_SCOPE = "rbac:assign"

_REPLACE_ASSIGNMENTS = OwnerCommandDefinition(
    owner="auth.system_user_assignments",
    concern="system-user role and direct-permission assignments",
    name="replace_system_user_assignments",
)


class SystemUserAssignmentError(DomainError):
    """Stable, transport-neutral assignment failure."""


class RoleResolutionError(SystemUserAssignmentError):
    """Requested role names or identifiers are not active."""

    def __init__(
        self,
        *,
        role_names: tuple[str, ...] = (),
        role_ids: tuple[UUID, ...] = (),
    ) -> None:
        super().__init__(
            code="auth.system_user_assignments.invalid_roles",
            message="One or more requested roles are not active.",
            details={
                "role_names": list(role_names),
                "role_ids": [str(item) for item in role_ids],
            },
        )
        self.role_names = role_names
        self.role_ids = role_ids


@dataclass(frozen=True)
class SourceRoleSyncResult:
    """Flush-only source-scoped role convergence result."""

    role_names: tuple[str, ...]
    changed: bool


@dataclass(frozen=True)
class ReplaceSystemUserAssignmentsCommand:
    """Authorized request to replace local roles and direct permissions."""

    context: CommandContext
    user_id: UUID
    role_ids: tuple[UUID, ...]
    direct_permission_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class SystemUserAssignmentsOutcome:
    """Committed effective access state for one system user."""

    user_id: UUID
    role_names: tuple[str, ...]
    direct_permission_keys: tuple[str, ...]
    changed: bool
    command_id: UUID
    correlation_id: UUID


def _error(code: str, message: str, **details: object) -> SystemUserAssignmentError:
    return SystemUserAssignmentError(
        code=f"auth.system_user_assignments.{code}",
        message=message,
        details=details,
    )


def _normalize_names(role_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name.strip() for name in role_names if name.strip()))


def _normalize_ids(values: tuple[UUID, ...] | list[UUID]) -> tuple[UUID, ...]:
    return tuple(dict.fromkeys(values))


def _active_roles_by_names(
    db: Session, role_names: list[str] | tuple[str, ...]
) -> tuple[Role, ...]:
    names = _normalize_names(role_names)
    if not names:
        raise RoleResolutionError(role_names=("At least one active role is required",))
    rows = tuple(
        db.execute(
            select(Role).where(Role.name.in_(names), Role.is_active.is_(True))
        ).scalars()
    )
    by_name = {role.name: role for role in rows}
    missing = tuple(name for name in names if name not in by_name)
    if missing:
        raise RoleResolutionError(role_names=missing)
    return tuple(by_name[name] for name in names)


def _active_roles_by_ids(
    db: Session, role_ids: tuple[UUID, ...] | list[UUID]
) -> tuple[Role, ...]:
    ids = _normalize_ids(role_ids)
    if not ids:
        return ()
    rows = tuple(
        db.execute(
            select(Role).where(Role.id.in_(ids), Role.is_active.is_(True))
        ).scalars()
    )
    by_id = {role.id: role for role in rows}
    missing = tuple(role_id for role_id in ids if role_id not in by_id)
    if missing:
        raise RoleResolutionError(role_ids=missing)
    return tuple(by_id[role_id] for role_id in ids)


def _locked_admin_role(db: Session) -> Role | None:
    return db.execute(
        select(Role)
        .where(func.lower(Role.name) == "admin", Role.is_active.is_(True))
        .with_for_update()
    ).scalar_one_or_none()


def _has_role(db: Session, user_id: UUID, role_id: UUID) -> bool:
    return bool(
        db.scalar(
            select(SystemUserRole.id)
            .where(
                SystemUserRole.system_user_id == user_id,
                SystemUserRole.role_id == role_id,
            )
            .limit(1)
        )
    )


def _active_admin_count(db: Session, admin_role_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.count(func.distinct(SystemUserRole.system_user_id)))
            .select_from(SystemUserRole)
            .join(SystemUser, SystemUser.id == SystemUserRole.system_user_id)
            .where(
                SystemUserRole.role_id == admin_role_id,
                SystemUser.is_active.is_(True),
            )
        )
        or 0
    )


def _ensure_admin_remains(
    db: Session,
    *,
    user_id: UUID,
    admin_role: Role | None,
    was_admin: bool,
) -> None:
    if admin_role is None or not was_admin:
        return
    db.flush()
    if _has_role(db, user_id, admin_role.id):
        return
    if _active_admin_count(db, admin_role.id) < 1:
        raise _error(
            "last_admin_required",
            "At least one active system user must retain the admin role.",
            user_id=str(user_id),
        )


def system_user_role_names(db: Session, user_id: UUID) -> tuple[str, ...]:
    rows = (
        db.execute(
            select(Role.name)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .where(
                SystemUserRole.system_user_id == user_id,
                Role.is_active.is_(True),
            )
            .distinct()
            .order_by(Role.name)
        )
        .scalars()
        .all()
    )
    return tuple(rows)


def system_user_direct_permission_keys(db: Session, user_id: UUID) -> tuple[str, ...]:
    rows = (
        db.execute(
            select(Permission.key)
            .join(
                SystemUserPermission,
                SystemUserPermission.permission_id == Permission.id,
            )
            .where(
                SystemUserPermission.system_user_id == user_id,
                Permission.is_active.is_(True),
            )
            .distinct()
            .order_by(Permission.key)
        )
        .scalars()
        .all()
    )
    return tuple(rows)


def _permission_keys_via_roles(db: Session, user_id: UUID) -> tuple[str, ...]:
    rows = (
        db.execute(
            select(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .where(
                SystemUserRole.system_user_id == user_id,
                Role.is_active.is_(True),
                Permission.is_active.is_(True),
            )
            .distinct()
            .order_by(Permission.key)
        )
        .scalars()
        .all()
    )
    return tuple(rows)


def _effective_access(
    db: Session, user_id: UUID
) -> tuple[frozenset[str], frozenset[str]]:
    """What this user can currently do: role names, and every permission key.

    Permissions are the union of direct grants and everything the assigned
    active roles confer, because a reduction has to be judged on effect rather
    than on shape. Swapping one role for another can leave the role *count*
    unchanged while taking permissions away, and that is still a reduction.
    Conversely a rename or a re-grant that lands on the same effective set is
    not a reduction and must not revoke anything.
    """

    return (
        frozenset(system_user_role_names(db, user_id)),
        frozenset(system_user_direct_permission_keys(db, user_id))
        | frozenset(_permission_keys_via_roles(db, user_id)),
    )


def _replace_source_roles(
    db: Session,
    *,
    user_id: UUID,
    roles: tuple[Role, ...],
    source: str,
) -> SourceRoleSyncResult:
    normalized_source = source.strip()
    if not normalized_source or len(normalized_source) > 40:
        raise _error(
            "invalid_command",
            "Role assignment source is invalid.",
            field="source",
        )
    admin_role = _locked_admin_role(db)
    was_admin = bool(admin_role and _has_role(db, user_id, admin_role.id))
    desired_ids = {role.id for role in roles}
    grants = tuple(
        db.execute(
            select(SystemUserRole)
            .where(SystemUserRole.system_user_id == user_id)
            .with_for_update()
        ).scalars()
    )
    changed = False
    surviving_global_ids: set[UUID] = set()
    for grant in grants:
        is_global = not grant.scope_type and not grant.scope_id
        remove = (
            is_global
            and grant.source == normalized_source
            and grant.role_id not in desired_ids
        )
        if remove:
            db.delete(grant)
            changed = True
        elif is_global:
            surviving_global_ids.add(grant.role_id)

    for role_id in desired_ids - surviving_global_ids:
        db.add(
            SystemUserRole(
                system_user_id=user_id,
                role_id=role_id,
                source=normalized_source,
            )
        )
        changed = True

    db.flush()
    _ensure_admin_remains(
        db,
        user_id=user_id,
        admin_role=admin_role,
        was_admin=was_admin,
    )
    return SourceRoleSyncResult(
        role_names=system_user_role_names(db, user_id),
        changed=changed,
    )


def sync_source_roles_by_names(
    db: Session,
    *,
    user_id: UUID,
    role_names: list[str] | tuple[str, ...],
    source: str,
) -> SourceRoleSyncResult:
    """Converge one source's global grants inside a coordinator transaction."""

    return _replace_source_roles(
        db,
        user_id=user_id,
        roles=_active_roles_by_names(db, role_names),
        source=source,
    )


def sync_source_roles_by_ids(
    db: Session,
    *,
    user_id: UUID,
    role_ids: tuple[UUID, ...] | list[UUID],
    source: str,
) -> SourceRoleSyncResult:
    """Converge source-scoped global grants by canonical role identifiers."""

    return _replace_source_roles(
        db,
        user_id=user_id,
        roles=_active_roles_by_ids(db, role_ids),
        source=source,
    )


def ensure_source_role_by_id(
    db: Session,
    *,
    user_id: UUID,
    role_id: UUID,
    source: str,
) -> SourceRoleSyncResult:
    """Add one source-owned global grant without replacing its sibling grants."""

    roles = _active_roles_by_ids(db, (role_id,))
    normalized_source = source.strip()
    if not normalized_source or len(normalized_source) > 40:
        raise _error(
            "invalid_command",
            "Role assignment source is invalid.",
            field="source",
        )
    _locked_admin_role(db)
    existing = db.execute(
        select(SystemUserRole)
        .where(
            SystemUserRole.system_user_id == user_id,
            SystemUserRole.role_id == role_id,
            SystemUserRole.scope_type == "",
            SystemUserRole.scope_id == "",
        )
        .with_for_update()
    ).scalar_one_or_none()
    changed = existing is None
    if existing is None:
        db.add(
            SystemUserRole(
                system_user_id=user_id,
                role_id=roles[0].id,
                source=normalized_source,
            )
        )
        db.flush()
    return SourceRoleSyncResult(
        role_names=system_user_role_names(db, user_id),
        changed=changed,
    )


def ensure_can_deactivate_system_user(db: Session, user_id: UUID) -> None:
    """Fail closed before deactivating the final active admin principal."""

    admin_role = _locked_admin_role(db)
    if admin_role is None or not _has_role(db, user_id, admin_role.id):
        return
    user = db.get(SystemUser, user_id)
    if user is None or not user.is_active:
        return
    if _active_admin_count(db, admin_role.id) <= 1:
        raise _error(
            "last_admin_required",
            "The final active admin account cannot be deactivated.",
            user_id=str(user_id),
        )


def remove_all_for_system_user(db: Session, user_id: UUID) -> None:
    """Remove assignment dependents inside the principal deletion transaction."""

    ensure_can_deactivate_system_user(db, user_id)
    db.query(SystemUserRole).filter(SystemUserRole.system_user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(SystemUserPermission).filter(
        SystemUserPermission.system_user_id == user_id
    ).delete(synchronize_session=False)
    db.flush()


def _validate_context(context: CommandContext) -> tuple[AuditActorType, str]:
    if context.scope != ASSIGNMENT_SCOPE:
        raise _error(
            "invalid_command",
            "System-user assignment requires RBAC assignment authorization.",
            field="scope",
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Assignment actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "Assignment actor identity is incomplete.",
            field="actor",
        )
    return actor_type, actor_id.strip()


def _replace_direct_permissions(
    db: Session,
    *,
    user_id: UUID,
    permission_ids: tuple[UUID, ...],
    granted_by: UUID | None,
) -> bool:
    desired_ids = set(_normalize_ids(permission_ids))
    permissions = tuple(
        db.execute(
            select(Permission).where(
                Permission.id.in_(desired_ids),
                Permission.is_active.is_(True),
                Permission.is_ui_assignable.is_(True),
            )
        ).scalars()
    )
    resolved_ids = {permission.id for permission in permissions}
    missing = desired_ids - resolved_ids
    if missing:
        raise _error(
            "invalid_permissions",
            "One or more direct permissions are not assignable.",
            permission_ids=[str(item) for item in sorted(missing, key=str)],
        )

    existing = tuple(
        db.execute(
            select(SystemUserPermission)
            .where(SystemUserPermission.system_user_id == user_id)
            .with_for_update()
        ).scalars()
    )
    existing_by_id = {item.permission_id: item for item in existing}
    changed = False
    for permission_id, grant in existing_by_id.items():
        if permission_id not in desired_ids:
            db.delete(grant)
            changed = True
    for permission_id in desired_ids - set(existing_by_id):
        db.add(
            SystemUserPermission(
                system_user_id=user_id,
                permission_id=permission_id,
                granted_by_system_user_id=granted_by,
            )
        )
        changed = True
    db.flush()
    return changed


def _granted_by(actor_type: AuditActorType, actor_id: str) -> UUID | None:
    if actor_type is not AuditActorType.user:
        return None
    try:
        return UUID(actor_id)
    except ValueError:
        return None


def replace_system_user_assignments(
    db: Session,
    command: ReplaceSystemUserAssignmentsCommand,
) -> SystemUserAssignmentsOutcome:
    """Replace editable access state in one manifest-verified transaction."""

    def operation() -> SystemUserAssignmentsOutcome:
        actor_type, actor_id = _validate_context(command.context)
        user = db.execute(
            select(SystemUser).where(SystemUser.id == command.user_id).with_for_update()
        ).scalar_one_or_none()
        if user is None:
            raise _error(
                "system_user_not_found",
                "System user was not found.",
                user_id=str(command.user_id),
            )
        if len(command.role_ids) > 50 or len(command.direct_permission_ids) > 500:
            raise _error(
                "invalid_command",
                "Assignment command exceeds the supported cardinality.",
            )

        before_roles, before_permissions = _effective_access(db, user.id)
        role_result = sync_source_roles_by_ids(
            db,
            user_id=user.id,
            role_ids=command.role_ids,
            source=LOCAL_ROLE_SOURCE,
        )
        permission_changed = _replace_direct_permissions(
            db,
            user_id=user.id,
            permission_ids=command.direct_permission_ids,
            granted_by=_granted_by(actor_type, actor_id),
        )
        permission_keys = system_user_direct_permission_keys(db, user.id)
        changed = bool(role_result.changed or permission_changed)

        # A reduction is the only transition an already-issued token can
        # outlive. Current tokens reload RBAC, while compatibility tokens may
        # still embed the roles and scopes just taken away. Revoking sessions in
        # THIS transaction gives both forms the same fail-closed next-request
        # rule. Widening, equivalent and no-op changes take nothing away, so
        # they revoke nothing — logging an operator out for being granted
        # something is not security.
        after_roles, after_permissions = _effective_access(db, user.id)
        removed_roles = tuple(sorted(before_roles - after_roles))
        removed_permissions = tuple(sorted(before_permissions - after_permissions))
        revoked_session_ids: tuple[str, ...] = ()
        if removed_roles or removed_permissions:
            revoked_session_ids = revoke_for_entitlement_reduction(
                db,
                principal_type=PRINCIPAL_SYSTEM_USER,
                principal_id=user.id,
                reason="system_user_assignments_reduced",
                correlation_id=str(command.context.correlation_id),
                actor=command.context.actor,
            )
        metadata = {
            "schema_version": 1,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "causation_id": (
                str(command.context.causation_id)
                if command.context.causation_id
                else None
            ),
            "idempotency_key_sha256": (
                hashlib.sha256(command.context.idempotency_key.encode()).hexdigest()
                if command.context.idempotency_key
                else None
            ),
            "scope": command.context.scope,
            "reason": command.context.reason,
            "role_names": list(role_result.role_names),
            "direct_permission_keys": list(permission_keys),
            "changed": changed,
            "removed_role_names": list(removed_roles),
            "removed_permission_keys": list(removed_permissions),
            "revoked_session_ids": list(revoked_session_ids),
        }
        stage_audit_event(
            db,
            action="auth.system_user_assignments_replaced",
            entity_type="system_user",
            entity_id=str(user.id),
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=str(command.context.correlation_id),
            metadata=metadata,
        )
        if changed:
            emit_event(
                db,
                EventType.system_user_assignments_changed,
                {
                    **metadata,
                    "aggregate_type": "system_user",
                    "aggregate_id": str(user.id),
                    "aggregate_version": str(command.context.command_id),
                    "user_id": str(user.id),
                },
                actor=command.context.actor,
            )

        def invalidate(_callback_db: Session) -> None:
            auth_cache.invalidate_principal("system_user", str(user.id))

        run_after_commit(db, invalidate)
        return SystemUserAssignmentsOutcome(
            user_id=user.id,
            role_names=role_result.role_names,
            direct_permission_keys=permission_keys,
            changed=changed,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    return execute_owner_command(
        db,
        definition=_REPLACE_ASSIGNMENTS,
        context=command.context,
        operation=operation,
    )
