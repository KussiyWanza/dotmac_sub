"""Atomicity and safety contracts for the canonical RBAC catalog owner."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import rbac as rbac_api
from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.models.system_user import SystemUser
from app.schemas.rbac import RoleCreate
from app.services import rbac_catalog
from app.services.owner_commands import CommandContext
from app.services.web_system_role_forms import (
    build_role_create_payload,
    create_role_with_permissions,
)


def _context(scope: str, key: str = "rbac-catalog-owner-test") -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:rbac-catalog-test",
        scope=scope,
        reason="verify canonical RBAC catalog semantics",
        idempotency_key=f"{key}:{command_id}",
    )


def _create_role(db_session, name: str, permission_ids: tuple[UUID, ...] = ()):
    return rbac_catalog.create_role(
        db_session,
        rbac_catalog.CreateRoleCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            name=name,
            permission_ids=permission_ids,
        ),
    )


def _create_permission(
    db_session,
    key: str,
    *,
    is_ui_assignable: bool = True,
):
    return rbac_catalog.create_permission(
        db_session,
        rbac_catalog.CreatePermissionCommand(
            context=_context(rbac_catalog.PERMISSION_WRITE_SCOPE),
            key=key,
            is_ui_assignable=is_ui_assignable,
        ),
    )


def test_create_role_commits_policy_audit_and_event_together(db_session) -> None:
    permission = Permission(key="tickets:catalog_read", is_active=True)
    db_session.add(permission)
    db_session.flush()
    permission_id = permission.id
    db_session.commit()

    result = _create_role(db_session, "support", (permission_id,))

    assert result.name == "support"
    assert result.permission_ids == (permission_id,)
    assert not db_session.in_transaction()
    assert db_session.get(Role, result.id).name == "support"
    assert (
        db_session.query(RolePermission)
        .filter_by(role_id=result.id, permission_id=permission_id)
        .count()
        == 1
    )
    audit = db_session.query(AuditEvent).filter_by(entity_id=str(result.id)).one()
    event = (
        db_session.query(EventStore)
        .filter_by(event_type="rbac.role_catalog_changed")
        .one()
    )
    assert audit.action == "auth.rbac_role_created"
    assert audit.metadata_["schema_version"] == 1
    assert event.payload["aggregate_id"] == str(result.id)
    assert event.payload["schema_version"] == 1


def test_invalid_permission_rolls_back_role_and_policy(db_session) -> None:
    missing_permission_id = uuid4()

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        _create_role(db_session, "invalid-policy", (missing_permission_id,))

    assert captured.value.code == "auth.rbac_catalog.invalid_permissions"
    assert not db_session.in_transaction()
    assert db_session.query(Role).filter_by(name="invalid-policy").count() == 0
    assert db_session.query(RolePermission).count() == 0
    assert db_session.query(AuditEvent).count() == 0
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="rbac.role_catalog_changed")
        .count()
        == 0
    )


def test_late_audit_failure_rolls_back_catalog_change(db_session, monkeypatch) -> None:
    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(rbac_catalog, "stage_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        _create_role(db_session, "rollback-catalog")

    assert not db_session.in_transaction()
    assert db_session.query(Role).filter_by(name="rollback-catalog").count() == 0
    assert db_session.query(RolePermission).count() == 0
    assert db_session.query(EventStore).count() == 0


def test_catalog_identities_are_normalized_and_case_unique(db_session) -> None:
    role = _create_role(db_session, "  NOC-Lead  ")
    permission = _create_permission(db_session, "  NETWORK:OLT_READ  ")

    assert role.name == "noc-lead"
    assert permission.key == "network:olt_read"
    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        _create_role(db_session, "noc-LEAD")
    assert captured.value.code == "auth.rbac_catalog.role_conflict"
    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        _create_permission(db_session, "Network:OLT_Read")
    assert captured.value.code == "auth.rbac_catalog.permission_conflict"


def test_assigned_role_identity_and_active_state_are_protected(db_session) -> None:
    role = Role(name="field-ops", is_active=True)
    user = SystemUser(
        first_name="Field",
        last_name="Operator",
        email="rbac-catalog-field-ops@dotmac.io",
        is_active=True,
    )
    db_session.add_all((role, user))
    db_session.flush()
    role_id = role.id
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role_id))
    db_session.commit()

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.update_role(
            db_session,
            rbac_catalog.UpdateRoleCommand(
                context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=role_id,
                name="field-operations",
            ),
        )
    assert captured.value.code == "auth.rbac_catalog.role_in_use"

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.deactivate_role(
            db_session,
            rbac_catalog.DeactivateRoleCommand(
                context=_context(rbac_catalog.ROLE_DELETE_SCOPE),
                role_id=role_id,
            ),
        )
    assert captured.value.code == "auth.rbac_catalog.role_in_use"
    assert db_session.get(Role, role_id).is_active is True


@pytest.mark.parametrize("submitted_name", ["NOC", "noc"])
def test_assigned_legacy_role_can_update_permissions_without_rename(
    db_session, submitted_name
) -> None:
    role = Role(name="NOC", is_active=True)
    permission = Permission(key="network:noc_read", is_active=True)
    user = SystemUser(
        first_name="NOC",
        last_name="Operator",
        email=f"noc-{uuid4().hex}@dotmac.io",
        is_active=True,
    )
    db_session.add_all((role, permission, user))
    db_session.flush()
    role_id = role.id
    permission_id = permission.id
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role_id))
    db_session.commit()

    result = rbac_catalog.update_role(
        db_session,
        rbac_catalog.UpdateRoleCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=role_id,
            name=submitted_name,
            permission_ids=(permission_id,),
        ),
    )

    assert result.name == "NOC"
    assert result.permission_ids == (permission_id,)
    audit = db_session.query(AuditEvent).filter_by(entity_id=str(role_id)).one()
    assert audit.metadata_["role_name"] == "NOC"


def test_real_assigned_legacy_role_rename_rolls_back_permission_changes(
    db_session,
) -> None:
    role = Role(name="NOC", is_active=True)
    old_permission = Permission(key="network:noc_read", is_active=True)
    new_permission = Permission(key="network:noc_update", is_active=True)
    user = SystemUser(
        first_name="NOC",
        last_name="Operator",
        email=f"noc-rename-{uuid4().hex}@dotmac.io",
        is_active=True,
    )
    db_session.add_all((role, old_permission, new_permission, user))
    db_session.flush()
    role_id = role.id
    old_permission_id = old_permission.id
    new_permission_id = new_permission.id
    db_session.add_all(
        (
            SystemUserRole(system_user_id=user.id, role_id=role_id),
            RolePermission(role_id=role_id, permission_id=old_permission_id),
        )
    )
    db_session.commit()

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.update_role(
            db_session,
            rbac_catalog.UpdateRoleCommand(
                context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=role_id,
                name="network_admin",
                permission_ids=(new_permission_id,),
            ),
        )

    assert captured.value.code == "auth.rbac_catalog.role_in_use"
    assert db_session.get(Role, role_id).name == "NOC"
    persisted_permission_ids = tuple(
        db_session.scalars(
            select(RolePermission.permission_id).where(
                RolePermission.role_id == role_id
            )
        )
    )
    assert persisted_permission_ids == (old_permission_id,)


def test_unassigned_legacy_role_can_be_renamed_and_normalized_duplicates_fail(
    db_session,
) -> None:
    legacy = Role(name="legacy_noc", is_active=True)
    duplicate = Role(name=" network_admin ", is_active=True)
    db_session.add_all((legacy, duplicate))
    db_session.flush()
    legacy_id = legacy.id
    db_session.commit()

    renamed = rbac_catalog.update_role(
        db_session,
        rbac_catalog.UpdateRoleCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=legacy_id,
            name="noc_team",
        ),
    )
    assert renamed.name == "noc_team"

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.update_role(
            db_session,
            rbac_catalog.UpdateRoleCommand(
                context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=legacy_id,
                name="NETWORK_ADMIN",
            ),
        )
    assert captured.value.code == "auth.rbac_catalog.role_conflict"


def test_admin_identity_and_non_assignable_permission_policy_are_protected(
    db_session,
) -> None:
    admin = _create_role(db_session, "admin")
    operator = _create_role(db_session, "operator")
    hidden = _create_permission(
        db_session,
        "network:admin",
        is_ui_assignable=False,
    )

    admin_grant = rbac_catalog.grant_role_permission(
        db_session,
        rbac_catalog.GrantRolePermissionCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=admin.id,
            permission_id=hidden.id,
        ),
    )
    assert admin_grant.changed is True

    form_replacement = rbac_catalog.update_role(
        db_session,
        rbac_catalog.UpdateRoleCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=admin.id,
            permission_ids=(),
        ),
    )
    assert hidden.id in form_replacement.permission_ids

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.grant_role_permission(
            db_session,
            rbac_catalog.GrantRolePermissionCommand(
                context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=operator.id,
                permission_id=hidden.id,
            ),
        )
    assert captured.value.code == "auth.rbac_catalog.protected_permission"

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.update_role(
            db_session,
            rbac_catalog.UpdateRoleCommand(
                context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=admin.id,
                name="super-admin",
            ),
        )
    assert captured.value.code == "auth.rbac_catalog.protected_role"

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.revoke_role_permission(
            db_session,
            rbac_catalog.RevokeRolePermissionCommand(
                context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
                link_id=admin_grant.id,
            ),
        )
    assert captured.value.code == "auth.rbac_catalog.protected_permission"


def test_referenced_permission_identity_and_active_state_are_protected(
    db_session,
) -> None:
    role = Role(name="billing-agent", is_active=True)
    permission = Permission(key="billing:invoice_read", is_active=True)
    db_session.add_all((role, permission))
    db_session.flush()
    permission_id = permission.id
    db_session.add(RolePermission(role_id=role.id, permission_id=permission_id))
    db_session.commit()

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.update_permission(
            db_session,
            rbac_catalog.UpdatePermissionCommand(
                context=_context(rbac_catalog.PERMISSION_WRITE_SCOPE),
                permission_id=permission_id,
                key="billing:invoice_view",
            ),
        )
    assert captured.value.code == "auth.rbac_catalog.permission_in_use"

    with pytest.raises(rbac_catalog.RbacCatalogError) as captured:
        rbac_catalog.deactivate_permission(
            db_session,
            rbac_catalog.DeactivatePermissionCommand(
                context=_context(rbac_catalog.PERMISSION_DELETE_SCOPE),
                permission_id=permission_id,
            ),
        )
    assert captured.value.code == "auth.rbac_catalog.permission_in_use"
    assert db_session.get(Permission, permission_id).is_active is True


def test_duplicate_role_permission_grant_is_an_idempotent_noop(db_session) -> None:
    role = _create_role(db_session, "assurance")
    permission = _create_permission(db_session, "network:assurance_read")
    command = rbac_catalog.GrantRolePermissionCommand(
        context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
        role_id=role.id,
        permission_id=permission.id,
    )

    first = rbac_catalog.grant_role_permission(db_session, command)
    second = rbac_catalog.grant_role_permission(
        db_session,
        rbac_catalog.GrantRolePermissionCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=role.id,
            permission_id=permission.id,
        ),
    )

    assert first.id == second.id
    assert first.changed is True
    assert second.changed is False
    assert (
        db_session.query(RolePermission)
        .filter_by(role_id=role.id, permission_id=permission.id)
        .count()
        == 1
    )


def test_seed_policy_replacement_converges_admin_to_exact_desired_set(
    db_session,
) -> None:
    admin = Role(name="admin", is_active=True)
    wildcard = Permission(key="*", is_active=True, is_ui_assignable=False)
    obsolete = Permission(
        key="network:admin",
        is_active=True,
        is_ui_assignable=False,
    )
    db_session.add_all((admin, wildcard, obsolete))
    db_session.flush()
    db_session.add_all(
        (
            RolePermission(role_id=admin.id, permission_id=wildcard.id),
            RolePermission(role_id=admin.id, permission_id=obsolete.id),
        )
    )
    wildcard_id = wildcard.id
    admin_id = admin.id
    db_session.commit()

    changed = rbac_catalog.replace_seeded_role_permissions(
        db_session,
        role=admin,
        permission_ids=(wildcard_id,),
    )
    db_session.commit()

    assert changed is True
    assert set(
        db_session.scalars(
            select(RolePermission.permission_id).where(
                RolePermission.role_id == admin_id
            )
        )
    ) == {wildcard_id}


def test_api_adapter_maps_catalog_conflicts_to_http_409(db_session) -> None:
    auth = {
        "principal_id": "rbac-api-test",
        "principal_type": "system_user",
    }
    payload = RoleCreate(name="api-role")

    created = rbac_api.create_role(payload, auth, db_session)

    assert created.name == "api-role"
    with pytest.raises(HTTPException) as captured:
        rbac_api.create_role(payload, auth, db_session)
    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "auth.rbac_catalog.role_conflict"


def test_web_role_form_delegates_one_atomic_policy_command(db_session) -> None:
    permission = Permission(key="support:case_read", is_active=True)
    db_session.add(permission)
    db_session.flush()
    permission_id = permission.id
    db_session.commit()

    result = create_role_with_permissions(
        db_session,
        payload=build_role_create_payload(
            name="support-lead",
            description="Support lead",
            is_active=True,
        ),
        permission_ids=[str(permission_id)],
        context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
    )

    assert result.name == "support-lead"
    assert result.permission_ids == (permission_id,)
