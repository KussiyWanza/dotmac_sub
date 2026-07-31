"""Canonical owner for RBAC role, permission, and role-policy catalogs."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberPermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.services import auth_cache
from app.services.audit_adapter import stage_audit_event
from app.services.common import apply_ordering, apply_pagination
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.session_hooks import run_after_commit

ROLE_WRITE_SCOPE = "rbac:roles:write"
ROLE_DELETE_SCOPE = "rbac:roles:delete"
PERMISSION_WRITE_SCOPE = "rbac:permissions:write"
PERMISSION_DELETE_SCOPE = "rbac:permissions:delete"

_ROLE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_PERMISSION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:[:.][a-z][a-z0-9_]*){1,2}$")

_ROLE_COMMAND = OwnerCommandDefinition(
    owner="auth.rbac_catalog",
    concern="role catalog and role-permission policy",
    name="change_role_catalog",
)
_PERMISSION_COMMAND = OwnerCommandDefinition(
    owner="auth.rbac_catalog",
    concern="permission catalog",
    name="change_permission_catalog",
)


class RbacCatalogError(DomainError):
    """Stable, transport-neutral catalog failure."""


@dataclass(frozen=True)
class CreateRoleCommand:
    context: CommandContext
    name: str
    description: str | None = None
    is_active: bool = True
    permission_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class UpdateRoleCommand:
    context: CommandContext
    role_id: UUID
    name: str | None = None
    description: str | None = None
    update_description: bool = False
    is_active: bool | None = None
    permission_ids: tuple[UUID, ...] | None = None


@dataclass(frozen=True)
class DeactivateRoleCommand:
    context: CommandContext
    role_id: UUID


@dataclass(frozen=True)
class GrantRolePermissionCommand:
    context: CommandContext
    role_id: UUID
    permission_id: UUID


@dataclass(frozen=True)
class UpdateRolePermissionCommand:
    context: CommandContext
    link_id: UUID
    role_id: UUID | None = None
    permission_id: UUID | None = None


@dataclass(frozen=True)
class RevokeRolePermissionCommand:
    context: CommandContext
    link_id: UUID


@dataclass(frozen=True)
class CreatePermissionCommand:
    context: CommandContext
    key: str
    description: str | None = None
    is_active: bool = True
    is_ui_assignable: bool = True


@dataclass(frozen=True)
class UpdatePermissionCommand:
    context: CommandContext
    permission_id: UUID
    key: str | None = None
    description: str | None = None
    update_description: bool = False
    is_active: bool | None = None
    is_ui_assignable: bool | None = None


@dataclass(frozen=True)
class DeactivatePermissionCommand:
    context: CommandContext
    permission_id: UUID


@dataclass(frozen=True)
class RoleCatalogOutcome:
    id: UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    permission_ids: tuple[UUID, ...]
    changed: bool
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class PermissionCatalogOutcome:
    id: UUID
    key: str
    description: str | None
    is_active: bool
    is_ui_assignable: bool
    created_at: datetime
    updated_at: datetime
    changed: bool
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class RolePermissionOutcome:
    id: UUID
    role_id: UUID
    permission_id: UUID
    changed: bool
    command_id: UUID
    correlation_id: UUID


def _error(code: str, message: str, **details: object) -> RbacCatalogError:
    return RbacCatalogError(
        code=f"auth.rbac_catalog.{code}",
        message=message,
        details=details,
    )


def _normalize_role_name(value: str) -> str:
    name = value.strip().lower()
    if not _ROLE_NAME_PATTERN.fullmatch(name):
        raise _error(
            "invalid_role_name",
            "Role name must be a lowercase identifier using letters, numbers, underscores, or hyphens.",
            field="name",
        )
    return name


def _normalize_permission_key(value: str) -> str:
    key = value.strip().lower()
    if key != "*" and not _PERMISSION_KEY_PATTERN.fullmatch(key):
        raise _error(
            "invalid_permission_key",
            "Permission key must be a lowercase domain:action identifier.",
            field="key",
        )
    return key


def _normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_context(
    context: CommandContext,
    *,
    allowed_scopes: frozenset[str],
) -> tuple[AuditActorType, str]:
    if context.scope not in allowed_scopes:
        raise _error(
            "invalid_command",
            "RBAC catalog command lacks the required authorization scope.",
            scope=context.scope,
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "RBAC catalog actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "RBAC catalog actor identity is incomplete.",
            field="actor",
        )
    return actor_type, actor_id.strip()


def _metadata(context: CommandContext, **values: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "causation_id": str(context.causation_id) if context.causation_id else None,
        "idempotency_key_sha256": (
            hashlib.sha256(context.idempotency_key.encode()).hexdigest()
            if context.idempotency_key
            else None
        ),
        "scope": context.scope,
        "reason": context.reason,
        **values,
    }


def _stage_change(
    db: Session,
    *,
    context: CommandContext,
    actor_type: AuditActorType,
    actor_id: str,
    action: str,
    entity_type: str,
    entity_id: UUID,
    event_type: EventType,
    metadata: dict[str, object],
    changed: bool,
) -> None:
    evidence = _metadata(context, changed=changed, **metadata)
    stage_audit_event(
        db,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=str(context.correlation_id),
        metadata=evidence,
    )
    if changed:
        emit_event(
            db,
            event_type,
            {
                **evidence,
                "aggregate_type": entity_type,
                "aggregate_id": str(entity_id),
                "aggregate_version": str(context.command_id),
            },
            actor=context.actor,
        )

        def invalidate(_callback_db: Session) -> None:
            auth_cache.invalidate_all_auth_cache()

        run_after_commit(db, invalidate)


def _role_permission_ids(db: Session, role_id: UUID) -> tuple[UUID, ...]:
    return tuple(
        db.execute(
            select(RolePermission.permission_id)
            .where(RolePermission.role_id == role_id)
            .order_by(RolePermission.permission_id)
        ).scalars()
    )


def _role_outcome(
    db: Session,
    role: Role,
    *,
    changed: bool,
    context: CommandContext,
) -> RoleCatalogOutcome:
    db.flush()
    return RoleCatalogOutcome(
        id=role.id,
        name=role.name,
        description=role.description,
        is_active=role.is_active,
        created_at=role.created_at,
        updated_at=role.updated_at,
        permission_ids=_role_permission_ids(db, role.id),
        changed=changed,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def _permission_outcome(
    db: Session,
    permission: Permission,
    *,
    changed: bool,
    context: CommandContext,
) -> PermissionCatalogOutcome:
    db.flush()
    return PermissionCatalogOutcome(
        id=permission.id,
        key=permission.key,
        description=permission.description,
        is_active=permission.is_active,
        is_ui_assignable=permission.is_ui_assignable,
        created_at=permission.created_at,
        updated_at=permission.updated_at,
        changed=changed,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def _locked_role(db: Session, role_id: UUID) -> Role:
    role = db.execute(
        select(Role).where(Role.id == role_id).with_for_update()
    ).scalar_one_or_none()
    if role is None:
        raise _error("role_not_found", "Role was not found.", role_id=str(role_id))
    return role


def _locked_permission(db: Session, permission_id: UUID) -> Permission:
    permission = db.execute(
        select(Permission).where(Permission.id == permission_id).with_for_update()
    ).scalar_one_or_none()
    if permission is None:
        raise _error(
            "permission_not_found",
            "Permission was not found.",
            permission_id=str(permission_id),
        )
    return permission


def _role_grant_count(db: Session, role_id: UUID) -> int:
    return int(
        (
            db.scalar(
                select(func.count())
                .select_from(SubscriberRole)
                .where(SubscriberRole.role_id == role_id)
            )
            or 0
        )
        + (
            db.scalar(
                select(func.count())
                .select_from(SystemUserRole)
                .where(SystemUserRole.role_id == role_id)
            )
            or 0
        )
    )


def _permission_reference_count(db: Session, permission_id: UUID) -> int:
    models = (RolePermission, SubscriberPermission, SystemUserPermission)
    return sum(
        int(
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(model.permission_id == permission_id)
            )
            or 0
        )
        for model in models
    )


def _active_permissions(
    db: Session,
    permission_ids: tuple[UUID, ...],
) -> dict[UUID, Permission]:
    desired = set(permission_ids)
    if not desired:
        return {}
    permissions = tuple(
        db.execute(
            select(Permission)
            .where(Permission.id.in_(desired), Permission.is_active.is_(True))
            .with_for_update()
        ).scalars()
    )
    resolved = {permission.id: permission for permission in permissions}
    missing = desired - set(resolved)
    if missing:
        raise _error(
            "invalid_permissions",
            "One or more requested permissions are not active.",
            permission_ids=[str(item) for item in sorted(missing, key=str)],
        )
    return resolved


def _ensure_role_permission_policy(role: Role, permission: Permission) -> None:
    if role.name != "admin" and not permission.is_ui_assignable:
        raise _error(
            "protected_permission",
            "Non-assignable permissions may be granted only to the admin role.",
            role_id=str(role.id),
            permission_id=str(permission.id),
        )


def _replace_role_permissions(
    db: Session,
    *,
    role: Role,
    permission_ids: tuple[UUID, ...],
    preserve_protected_admin_grants: bool = True,
) -> bool:
    desired = set(permission_ids)
    permissions = _active_permissions(db, tuple(desired))
    for permission in permissions.values():
        _ensure_role_permission_policy(role, permission)
    existing = tuple(
        db.execute(
            select(RolePermission)
            .where(RolePermission.role_id == role.id)
            .with_for_update()
        ).scalars()
    )
    existing_by_permission = {link.permission_id: link for link in existing}
    changed = False
    for permission_id, link in existing_by_permission.items():
        if permission_id in desired:
            continue
        permission = link.permission
        if (
            preserve_protected_admin_grants
            and role.name == "admin"
            and not permission.is_ui_assignable
        ):
            continue
        db.delete(link)
        changed = True
    for permission_id in desired - set(existing_by_permission):
        db.add(RolePermission(role_id=role.id, permission_id=permission_id))
        changed = True
    db.flush()
    return changed


def _execute(
    db: Session,
    *,
    definition: OwnerCommandDefinition,
    context: CommandContext,
    operation,
):
    try:
        return execute_owner_command(
            db,
            definition=definition,
            context=context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "catalog_conflict",
            "RBAC catalog identity or relationship already exists.",
        ) from exc


def create_role(db: Session, command: CreateRoleCommand) -> RoleCatalogOutcome:
    def operation() -> RoleCatalogOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({ROLE_WRITE_SCOPE}),
        )
        name = _normalize_role_name(command.name)
        duplicate = db.scalar(
            select(Role.id)
            .where(func.lower(func.trim(Role.name)) == name)
            .with_for_update()
        )
        if duplicate is not None:
            raise _error("role_conflict", "Role name already exists.", name=name)
        role = Role(
            name=name,
            description=_normalize_description(command.description),
            is_active=command.is_active,
        )
        db.add(role)
        db.flush()
        _replace_role_permissions(
            db,
            role=role,
            permission_ids=tuple(dict.fromkeys(command.permission_ids)),
        )
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_role_created",
            entity_type="role",
            entity_id=role.id,
            event_type=EventType.rbac_role_catalog_changed,
            metadata={"operation": "create", "role_name": role.name},
            changed=True,
        )
        return _role_outcome(db, role, changed=True, context=command.context)

    return _execute(
        db,
        definition=_ROLE_COMMAND,
        context=command.context,
        operation=operation,
    )


def update_role(db: Session, command: UpdateRoleCommand) -> RoleCatalogOutcome:
    def operation() -> RoleCatalogOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({ROLE_WRITE_SCOPE}),
        )
        role = _locked_role(db, command.role_id)
        changed = False
        if command.name is not None:
            submitted_name = _normalize_role_name(command.name)
            current_name = _normalize_role_name(role.name)
            if submitted_name != current_name:
                if current_name == "admin":
                    raise _error(
                        "protected_role",
                        "The canonical admin role cannot be renamed.",
                        role_id=str(role.id),
                    )
                if _role_grant_count(db, role.id):
                    raise _error(
                        "role_in_use",
                        "An assigned role cannot be renamed.",
                        role_id=str(role.id),
                    )
                duplicate = db.scalar(
                    select(Role.id).where(
                        Role.id != role.id,
                        func.lower(func.trim(Role.name)) == submitted_name,
                    )
                )
                if duplicate is not None:
                    raise _error(
                        "role_conflict",
                        "Role name already exists.",
                        name=submitted_name,
                    )
                role.name = submitted_name
                changed = True
        if command.update_description:
            description = _normalize_description(command.description)
            if role.description != description:
                role.description = description
                changed = True
        if command.is_active is not None and role.is_active != command.is_active:
            if not command.is_active:
                _ensure_role_can_deactivate(db, role)
            role.is_active = command.is_active
            changed = True
        if command.permission_ids is not None:
            changed = bool(
                _replace_role_permissions(
                    db,
                    role=role,
                    permission_ids=tuple(dict.fromkeys(command.permission_ids)),
                )
                or changed
            )
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_role_updated",
            entity_type="role",
            entity_id=role.id,
            event_type=EventType.rbac_role_catalog_changed,
            metadata={"operation": "update", "role_name": role.name},
            changed=changed,
        )
        return _role_outcome(db, role, changed=changed, context=command.context)

    return _execute(
        db,
        definition=_ROLE_COMMAND,
        context=command.context,
        operation=operation,
    )


def _ensure_role_can_deactivate(db: Session, role: Role) -> None:
    if role.name == "admin":
        raise _error(
            "protected_role",
            "The canonical admin role cannot be deactivated.",
            role_id=str(role.id),
        )
    if _role_grant_count(db, role.id):
        raise _error(
            "role_in_use",
            "Assigned roles must be unassigned before deactivation.",
            role_id=str(role.id),
        )


def deactivate_role(db: Session, command: DeactivateRoleCommand) -> RoleCatalogOutcome:
    def operation() -> RoleCatalogOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({ROLE_DELETE_SCOPE}),
        )
        role = _locked_role(db, command.role_id)
        changed = role.is_active
        if changed:
            _ensure_role_can_deactivate(db, role)
            role.is_active = False
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_role_deactivated",
            entity_type="role",
            entity_id=role.id,
            event_type=EventType.rbac_role_catalog_changed,
            metadata={"operation": "deactivate", "role_name": role.name},
            changed=changed,
        )
        return _role_outcome(db, role, changed=changed, context=command.context)

    return _execute(
        db,
        definition=_ROLE_COMMAND,
        context=command.context,
        operation=operation,
    )


def create_permission(
    db: Session, command: CreatePermissionCommand
) -> PermissionCatalogOutcome:
    def operation() -> PermissionCatalogOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({PERMISSION_WRITE_SCOPE}),
        )
        key = _normalize_permission_key(command.key)
        duplicate = db.scalar(
            select(Permission.id)
            .where(func.lower(func.trim(Permission.key)) == key)
            .with_for_update()
        )
        if duplicate is not None:
            raise _error(
                "permission_conflict", "Permission key already exists.", key=key
            )
        permission = Permission(
            key=key,
            description=_normalize_description(command.description),
            is_active=command.is_active,
            is_ui_assignable=command.is_ui_assignable,
        )
        db.add(permission)
        db.flush()
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_permission_created",
            entity_type="permission",
            entity_id=permission.id,
            event_type=EventType.rbac_permission_catalog_changed,
            metadata={"operation": "create", "permission_key": permission.key},
            changed=True,
        )
        return _permission_outcome(
            db, permission, changed=True, context=command.context
        )

    return _execute(
        db,
        definition=_PERMISSION_COMMAND,
        context=command.context,
        operation=operation,
    )


def _ensure_permission_can_change_identity(db: Session, permission: Permission) -> None:
    if _permission_reference_count(db, permission.id):
        raise _error(
            "permission_in_use",
            "Assigned permissions must be unlinked before identity or active-state changes.",
            permission_id=str(permission.id),
        )


def update_permission(
    db: Session, command: UpdatePermissionCommand
) -> PermissionCatalogOutcome:
    def operation() -> PermissionCatalogOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({PERMISSION_WRITE_SCOPE}),
        )
        permission = _locked_permission(db, command.permission_id)
        changed = False
        if command.key is not None:
            key = _normalize_permission_key(command.key)
            if key != permission.key:
                _ensure_permission_can_change_identity(db, permission)
                duplicate = db.scalar(
                    select(Permission.id).where(
                        Permission.id != permission.id,
                        func.lower(func.trim(Permission.key)) == key,
                    )
                )
                if duplicate is not None:
                    raise _error(
                        "permission_conflict",
                        "Permission key already exists.",
                        key=key,
                    )
                permission.key = key
                changed = True
        if command.update_description:
            description = _normalize_description(command.description)
            if permission.description != description:
                permission.description = description
                changed = True
        if command.is_active is not None and permission.is_active != command.is_active:
            if not command.is_active:
                _ensure_permission_can_change_identity(db, permission)
            permission.is_active = command.is_active
            changed = True
        if (
            command.is_ui_assignable is not None
            and permission.is_ui_assignable != command.is_ui_assignable
        ):
            if not command.is_ui_assignable:
                _ensure_permission_can_change_identity(db, permission)
            permission.is_ui_assignable = command.is_ui_assignable
            changed = True
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_permission_updated",
            entity_type="permission",
            entity_id=permission.id,
            event_type=EventType.rbac_permission_catalog_changed,
            metadata={"operation": "update", "permission_key": permission.key},
            changed=changed,
        )
        return _permission_outcome(
            db, permission, changed=changed, context=command.context
        )

    return _execute(
        db,
        definition=_PERMISSION_COMMAND,
        context=command.context,
        operation=operation,
    )


def deactivate_permission(
    db: Session, command: DeactivatePermissionCommand
) -> PermissionCatalogOutcome:
    def operation() -> PermissionCatalogOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({PERMISSION_DELETE_SCOPE}),
        )
        permission = _locked_permission(db, command.permission_id)
        changed = permission.is_active
        if changed:
            _ensure_permission_can_change_identity(db, permission)
            permission.is_active = False
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_permission_deactivated",
            entity_type="permission",
            entity_id=permission.id,
            event_type=EventType.rbac_permission_catalog_changed,
            metadata={"operation": "deactivate", "permission_key": permission.key},
            changed=changed,
        )
        return _permission_outcome(
            db, permission, changed=changed, context=command.context
        )

    return _execute(
        db,
        definition=_PERMISSION_COMMAND,
        context=command.context,
        operation=operation,
    )


def _role_permission_outcome(
    link: RolePermission,
    *,
    changed: bool,
    context: CommandContext,
) -> RolePermissionOutcome:
    return RolePermissionOutcome(
        id=link.id,
        role_id=link.role_id,
        permission_id=link.permission_id,
        changed=changed,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def grant_role_permission(
    db: Session, command: GrantRolePermissionCommand
) -> RolePermissionOutcome:
    def operation() -> RolePermissionOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({ROLE_WRITE_SCOPE}),
        )
        role = _locked_role(db, command.role_id)
        permission = _locked_permission(db, command.permission_id)
        if not role.is_active or not permission.is_active:
            raise _error(
                "invalid_permissions",
                "Role-permission links require active catalog entries.",
            )
        _ensure_role_permission_policy(role, permission)
        link = db.execute(
            select(RolePermission)
            .where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id == permission.id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        changed = link is None
        if link is None:
            link = RolePermission(role_id=role.id, permission_id=permission.id)
            db.add(link)
            db.flush()
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_role_permission_granted",
            entity_type="role",
            entity_id=role.id,
            event_type=EventType.rbac_role_catalog_changed,
            metadata={
                "operation": "grant_permission",
                "role_name": role.name,
                "permission_key": permission.key,
            },
            changed=changed,
        )
        return _role_permission_outcome(link, changed=changed, context=command.context)

    return _execute(
        db,
        definition=_ROLE_COMMAND,
        context=command.context,
        operation=operation,
    )


def update_role_permission(
    db: Session, command: UpdateRolePermissionCommand
) -> RolePermissionOutcome:
    def operation() -> RolePermissionOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({ROLE_WRITE_SCOPE}),
        )
        link = db.execute(
            select(RolePermission)
            .where(RolePermission.id == command.link_id)
            .with_for_update()
        ).scalar_one_or_none()
        if link is None:
            raise _error(
                "role_permission_not_found",
                "Role-permission link was not found.",
                link_id=str(command.link_id),
            )
        old_role = _locked_role(db, link.role_id)
        old_permission = _locked_permission(db, link.permission_id)
        if old_role.name == "admin" and not old_permission.is_ui_assignable:
            raise _error(
                "protected_permission",
                "Protected admin grants cannot be moved.",
                link_id=str(link.id),
            )
        role = _locked_role(db, command.role_id or link.role_id)
        permission = _locked_permission(db, command.permission_id or link.permission_id)
        if not role.is_active or not permission.is_active:
            raise _error(
                "invalid_permissions",
                "Role-permission links require active catalog entries.",
            )
        _ensure_role_permission_policy(role, permission)
        changed = bool(link.role_id != role.id or link.permission_id != permission.id)
        link.role_id = role.id
        link.permission_id = permission.id
        db.flush()
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_role_permission_updated",
            entity_type="role",
            entity_id=role.id,
            event_type=EventType.rbac_role_catalog_changed,
            metadata={
                "operation": "move_permission",
                "role_name": role.name,
                "permission_key": permission.key,
            },
            changed=changed,
        )
        return _role_permission_outcome(link, changed=changed, context=command.context)

    return _execute(
        db,
        definition=_ROLE_COMMAND,
        context=command.context,
        operation=operation,
    )


def revoke_role_permission(
    db: Session, command: RevokeRolePermissionCommand
) -> RolePermissionOutcome:
    def operation() -> RolePermissionOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=frozenset({ROLE_WRITE_SCOPE}),
        )
        link = db.execute(
            select(RolePermission)
            .where(RolePermission.id == command.link_id)
            .with_for_update()
        ).scalar_one_or_none()
        if link is None:
            raise _error(
                "role_permission_not_found",
                "Role-permission link was not found.",
                link_id=str(command.link_id),
            )
        role = _locked_role(db, link.role_id)
        permission = _locked_permission(db, link.permission_id)
        if role.name == "admin" and not permission.is_ui_assignable:
            raise _error(
                "protected_permission",
                "Protected admin grants cannot be revoked.",
                link_id=str(link.id),
            )
        outcome = _role_permission_outcome(link, changed=True, context=command.context)
        db.delete(link)
        db.flush()
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            action="auth.rbac_role_permission_revoked",
            entity_type="role",
            entity_id=role.id,
            event_type=EventType.rbac_role_catalog_changed,
            metadata={
                "operation": "revoke_permission",
                "role_name": role.name,
                "permission_key": permission.key,
            },
            changed=True,
        )
        return outcome

    return _execute(
        db,
        definition=_ROLE_COMMAND,
        context=command.context,
        operation=operation,
    )


def get_role(db: Session, role_id: UUID) -> Role | None:
    return db.get(Role, role_id)


def list_roles(
    db: Session,
    *,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
) -> list[Role]:
    query = db.query(Role)
    query = query.filter(
        Role.is_active.is_(True) if is_active is None else Role.is_active == is_active
    )
    query = apply_ordering(
        query,
        order_by,
        order_dir,
        {"created_at": Role.created_at, "name": Role.name},
    )
    return apply_pagination(query, limit, offset).all()


def list_roles_response(db: Session, **kwargs) -> dict[str, object]:
    items = list_roles(db, **kwargs)
    return {
        "items": items,
        "count": len(items),
        "limit": kwargs["limit"],
        "offset": kwargs["offset"],
    }


def get_permission(db: Session, permission_id: UUID) -> Permission | None:
    return db.get(Permission, permission_id)


def list_permissions(
    db: Session,
    *,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
) -> list[Permission]:
    query = db.query(Permission)
    query = query.filter(
        Permission.is_active.is_(True)
        if is_active is None
        else Permission.is_active == is_active
    )
    query = apply_ordering(
        query,
        order_by,
        order_dir,
        {"created_at": Permission.created_at, "key": Permission.key},
    )
    return apply_pagination(query, limit, offset).all()


def list_permissions_response(db: Session, **kwargs) -> dict[str, object]:
    items = list_permissions(db, **kwargs)
    return {
        "items": items,
        "count": len(items),
        "limit": kwargs["limit"],
        "offset": kwargs["offset"],
    }


def get_role_permission(db: Session, link_id: UUID) -> RolePermission | None:
    return db.get(RolePermission, link_id)


def list_role_permissions(
    db: Session,
    *,
    role_id: UUID | None,
    permission_id: UUID | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
) -> list[RolePermission]:
    query = db.query(RolePermission)
    if role_id:
        query = query.filter(RolePermission.role_id == role_id)
    if permission_id:
        query = query.filter(RolePermission.permission_id == permission_id)
    query = apply_ordering(
        query,
        order_by,
        order_dir,
        {"role_id": RolePermission.role_id},
    )
    return apply_pagination(query, limit, offset).all()


def list_role_permissions_response(db: Session, **kwargs) -> dict[str, object]:
    items = list_role_permissions(db, **kwargs)
    return {
        "items": items,
        "count": len(items),
        "limit": kwargs["limit"],
        "offset": kwargs["offset"],
    }


def ensure_role(db: Session, *, name: str, description: str | None) -> Role:
    """Flush-only seed collaborator for one canonical role identity."""

    normalized = _normalize_role_name(name)
    role = db.execute(
        select(Role)
        .where(func.lower(func.trim(Role.name)) == normalized)
        .with_for_update()
    ).scalar_one_or_none()
    if role is None:
        role = Role(
            name=normalized,
            description=_normalize_description(description),
            is_active=True,
        )
        db.add(role)
    else:
        role.name = normalized
        role.is_active = True
        if description and not role.description:
            role.description = _normalize_description(description)
    db.flush()
    return role


def ensure_permission(
    db: Session,
    *,
    key: str,
    description: str | None,
    is_ui_assignable: bool,
) -> Permission:
    """Flush-only seed collaborator for one canonical permission identity."""

    normalized = _normalize_permission_key(key)
    permission = db.execute(
        select(Permission)
        .where(func.lower(func.trim(Permission.key)) == normalized)
        .with_for_update()
    ).scalar_one_or_none()
    if permission is None:
        permission = Permission(
            key=normalized,
            description=_normalize_description(description),
            is_active=True,
            is_ui_assignable=is_ui_assignable,
        )
        db.add(permission)
    else:
        permission.key = normalized
        permission.is_active = True
        permission.is_ui_assignable = is_ui_assignable
        if description and not permission.description:
            permission.description = _normalize_description(description)
    db.flush()
    return permission


def ensure_role_permission(
    db: Session,
    *,
    role_id: UUID,
    permission_id: UUID,
) -> RolePermission:
    """Flush-only additive seed collaborator for one role policy link."""

    role = _locked_role(db, role_id)
    permission = _locked_permission(db, permission_id)
    if not role.is_active or not permission.is_active:
        raise _error(
            "invalid_permissions",
            "Seeded role-permission links require active catalog entries.",
        )
    _ensure_role_permission_policy(role, permission)
    link = db.execute(
        select(RolePermission)
        .where(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == permission_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if link is None:
        link = RolePermission(role_id=role_id, permission_id=permission_id)
        db.add(link)
        db.flush()
    return link


def replace_seeded_role_permissions(
    db: Session,
    *,
    role: Role,
    permission_ids: tuple[UUID, ...],
) -> bool:
    """Flush-only seed convergence through the canonical policy writer."""

    return _replace_role_permissions(
        db,
        role=role,
        permission_ids=tuple(dict.fromkeys(permission_ids)),
        preserve_protected_admin_grants=False,
    )
