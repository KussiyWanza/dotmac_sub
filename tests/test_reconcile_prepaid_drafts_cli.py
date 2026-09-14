"""Non-vacuous proof that the CLI resolves a real staff principal's RBAC grant.

A source-grep architecture test can confirm the CLI *calls*
``_resolve_repair_permission_granted``, but it cannot prove the resolver ever
actually returns ``True`` for a real principal. An earlier version of this
resolver called ``app.services.auth_dependencies.user_role_names``, which
reads a ``SystemUser.roles`` attribute that does not exist on that model --
so it always returned ``None`` and the resolver always returned ``False``,
making the whole repair capability permanently inoperable while looking
gated. These tests seed a real ``SystemUser``/``Role``/``SystemUserRole`` row
set and exercise the resolver directly, so a regression back to the dead
``user_role_names`` path fails here even though the source-grep test would
still pass.
"""

from __future__ import annotations

from uuid import uuid4

from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.models.system_user import SystemUser
from scripts.billing import reconcile_prepaid_drafts as cli


def _system_user(db, *, is_active: bool = True) -> SystemUser:
    user = SystemUser(
        id=uuid4(),
        first_name="Test",
        last_name="Staff",
        display_name="Test Staff",
        email=f"staff-{uuid4().hex}@example.test",
        is_active=is_active,
    )
    db.add(user)
    db.flush()
    return user


def test_resolve_repair_permission_granted_true_for_admin_role(db_session):
    user = _system_user(db_session)
    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.flush()

    assert (
        cli._resolve_repair_permission_granted(db_session, actor_system_user_id=user.id)
        is True
    )


def test_resolve_repair_permission_granted_true_for_directly_granted_permission(
    db_session,
):
    """A non-admin role holding the exact permission also resolves True.

    The only prior coverage exercised ``has_permission``'s ``"admin" in
    roles`` short-circuit, which never reaches the real
    ``SystemUserRole.system_user_id == principal_id`` filter. This proves
    that filter actually matches a string ``principal_id`` against the
    ``UUID(as_uuid=True)`` column -- the same shape a colder, unverified
    path could silently and permanently fail-closed on, exactly like the
    original blocker.
    """

    user = _system_user(db_session)
    role = Role(name="billing_prepaid_operator", is_active=True)
    permission = Permission(key=cli.REPAIR_SCOPE, is_active=True)
    db_session.add(role)
    db_session.add(permission)
    db_session.flush()
    db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.flush()

    assert (
        cli._resolve_repair_permission_granted(db_session, actor_system_user_id=user.id)
        is True
    )


def test_resolve_repair_permission_granted_false_without_admin_role(db_session):
    user = _system_user(db_session)
    role = Role(name="billing_operator", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.flush()

    assert (
        cli._resolve_repair_permission_granted(db_session, actor_system_user_id=user.id)
        is False
    )


def test_resolve_repair_permission_granted_false_for_deactivated_staff(db_session):
    user = _system_user(db_session, is_active=False)
    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.flush()

    assert (
        cli._resolve_repair_permission_granted(db_session, actor_system_user_id=user.id)
        is False
    )


def test_resolve_repair_permission_granted_false_for_unknown_user(db_session):
    assert (
        cli._resolve_repair_permission_granted(db_session, actor_system_user_id=uuid4())
        is False
    )


def test_resolve_repair_permission_granted_false_without_identifier(db_session):
    assert (
        cli._resolve_repair_permission_granted(db_session, actor_system_user_id=None)
        is False
    )
