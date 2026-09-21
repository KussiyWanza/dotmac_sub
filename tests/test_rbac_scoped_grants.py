"""Object-scoped role grants: region/reseller scoping on top of the flat RBAC.

A grant with empty scope (scope_type="") is GLOBAL (historical behaviour); a
grant scoped to a region only authorizes resources in that region.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SystemUserRole,
)
from app.services import auth_dependencies as ad


def _setup(db_session, *, scope_type="", scope_id=""):
    """A system user with a 'regionop' role granting network:nas:write,
    assigned at the given scope."""
    from app.models.system_user import SystemUser

    user = SystemUser(
        email=f"op-{uuid.uuid4().hex[:8]}@example.com",
        first_name="Region",
        last_name="Op",
        is_active=True,
    )
    perm = Permission(key="network:nas:write", description="x", is_active=True)
    role = Role(name=f"regionop-{uuid.uuid4().hex[:6]}", is_active=True)
    db_session.add_all([user, perm, role])
    db_session.commit()
    db_session.add(RolePermission(role_id=role.id, permission_id=perm.id))
    db_session.add(
        SystemUserRole(
            system_user_id=user.id,
            role_id=role.id,
            scope_type=scope_type,
            scope_id=scope_id,
        )
    )
    db_session.commit()
    auth = {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": [role.name],
        "scopes": [],
    }
    return auth


def test_global_grant_is_authorized_everywhere(db_session):
    auth = _setup(db_session)  # scope_type="" → global
    decision = ad.grant_scopes_for_permission(auth, db_session, "network:nas:write")
    assert decision == "global"


def test_region_scoped_grant_returns_its_scope(db_session):
    region = str(uuid.uuid4())
    auth = _setup(db_session, scope_type="region", scope_id=region)
    decision = ad.grant_scopes_for_permission(auth, db_session, "network:nas:write")
    assert decision == {("region", region)}


def test_no_grant_returns_none(db_session):
    from app.models.system_user import SystemUser

    user = SystemUser(
        email="nobody@example.com", first_name="No", last_name="One", is_active=True
    )
    db_session.add(user)
    db_session.commit()
    auth = {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }
    assert ad.grant_scopes_for_permission(auth, db_session, "network:nas:write") is None


def test_admin_is_global(db_session):
    auth = {
        "principal_id": str(uuid.uuid4()),
        "principal_type": "system_user",
        "roles": ["admin"],
        "scopes": [],
    }
    assert ad.grant_scopes_for_permission(auth, db_session, "anything:read") == "global"


def _call_guard(auth, db_session, extractor):
    guard = ad.require_scoped_permission("network:nas:write", extractor)
    return guard(request=None, auth=auth, db=db_session)


def test_guard_global_skips_extractor(db_session):
    auth = _setup(db_session)
    called = {"n": 0}

    def extractor(request, db):
        called["n"] += 1
        return ("region", "x")

    assert _call_guard(auth, db_session, extractor) is auth
    assert called["n"] == 0  # global never consults the resource


def test_guard_caches_effective_permission_keys_for_ui_actions(db_session):
    auth = _setup(db_session)

    assert "permission_keys" not in auth
    assert _call_guard(auth, db_session, lambda r, d: ("region", "x")) is auth
    assert auth["permission_keys"] == frozenset({"network:nas:write"})
    request = SimpleNamespace(state=SimpleNamespace(auth=auth))
    assert ad.can(request, "network:nas:write") is True


def test_guard_region_allows_matching_denies_other(db_session):
    region = str(uuid.uuid4())
    other = str(uuid.uuid4())
    auth = _setup(db_session, scope_type="region", scope_id=region)

    assert _call_guard(auth, db_session, lambda r, d: ("region", region)) is auth
    with pytest.raises(HTTPException) as exc:
        _call_guard(auth, db_session, lambda r, d: ("region", other))
    assert exc.value.status_code == 403


def test_guard_region_denies_scopeless_resource(db_session):
    region = str(uuid.uuid4())
    auth = _setup(db_session, scope_type="region", scope_id=region)
    with pytest.raises(HTTPException) as exc:
        _call_guard(auth, db_session, lambda r, d: None)
    assert exc.value.status_code == 403


def test_guard_no_permission_forbidden(db_session):
    auth = {
        "principal_id": str(uuid.uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }
    with pytest.raises(HTTPException) as exc:
        _call_guard(auth, db_session, lambda r, d: ("region", "x"))
    assert exc.value.status_code == 403
