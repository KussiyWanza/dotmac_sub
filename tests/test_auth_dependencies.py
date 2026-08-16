import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from jose import jwt
from starlette.requests import Request

from app.models.auth import ApiKey, AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberRole,
    SystemUserRole,
)
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import auth_dependencies as auth_dep
from app.services import session_manager as session_manager_service
from app.services import staff_party_authentication
from app.services.auth import hash_api_key
from app.services.auth_dependencies import require_audit_auth, require_user_auth
from app.services.auth_flow import AuthFlow, hash_password, hash_session_token
from app.web.auth.dependencies import (
    AuthenticationRequired,
    require_web_auth,
    validate_session_token,
)
from tests.staff_identity_fixtures import project_staff_login


def _make_access_token(
    person_id: str,
    session_id: str,
    scopes: list[str] | None = None,
    roles: list[str] | None = None,
):
    now = datetime.now(UTC)
    payload = {
        "sub": person_id,
        "session_id": session_id,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    if scopes:
        payload["scopes"] = scopes
    if roles:
        payload["roles"] = roles
    return jwt.encode(payload, "test-secret", algorithm="HS256")


def _make_request():
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth",
        "headers": [(b"user-agent", b"pytest")],
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def _make_web_request(
    *,
    method: str = "GET",
    path: str = "/admin/network/cpes",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
):
    scope = {
        "type": "http",
        "method": method,
        "scheme": "https",
        "path": path,
        "query_string": query_string,
        "headers": headers or [(b"host", b"oss.dotmac.ng")],
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_require_user_auth_accepts_valid_token(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        subscriber_id=person.id,
        provider=AuthProvider.local,
        username="auth@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    tokens = AuthFlow._issue_tokens(db_session, str(person.id), _make_request())
    auth = require_user_auth(
        authorization=f"Bearer {tokens['access_token']}", db=db_session
    )
    assert auth["subscriber_id"] == str(person.id)
    assert auth["session_id"]


def test_require_user_auth_rejects_expired_session(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id))
    with pytest.raises(HTTPException) as exc:
        require_user_auth(authorization=f"Bearer {token}", db=db_session)
    assert exc.value.status_code == 401


def test_require_user_auth_accepts_system_user_token(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    user = SystemUser(
        first_name="System",
        last_name="Admin",
        display_name="System Admin",
        email="sysadmin@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))

    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username="sysadmin@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    project_staff_login(db_session, user=user, credential=credential)
    db_session.commit()

    tokens = AuthFlow._issue_tokens(
        db_session,
        "system_user",
        str(user.id),
        _make_request(),
        staff_binding=staff_party_authentication.binding_for_principal(user),
    )
    auth = require_user_auth(
        authorization=f"Bearer {tokens['access_token']}", db=db_session
    )

    assert auth["principal_type"] == "system_user"
    assert auth["principal_id"] == str(user.id)


def test_validate_session_token_accepts_system_user_cookie(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    user = SystemUser(
        first_name="System",
        last_name="Operator",
        display_name="System Operator",
        email="operator@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))

    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username="operator@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    project_staff_login(db_session, user=user, credential=credential)
    db_session.commit()

    tokens = AuthFlow._issue_tokens(
        db_session,
        "system_user",
        str(user.id),
        _make_request(),
        staff_binding=staff_party_authentication.binding_for_principal(user),
    )
    request = _make_request()
    request._cookies = {"session_token": tokens["access_token"]}

    auth = validate_session_token(request, db_session)

    assert auth is not None
    assert auth["principal_type"] == "system_user"
    assert auth["principal_id"] == str(user.id)


def test_validate_session_token_ignores_stale_cached_session(monkeypatch, db_session):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session_id = str(uuid.uuid4())
    token = _make_access_token("subscriber-1", session_id)
    request = _make_request()
    request._cookies = {"session_token": token}
    invalidations = []

    monkeypatch.setattr(
        "app.web.auth.dependencies.validate_active_session",
        lambda db, resolved_session_id, principal_id: None,
    )
    monkeypatch.setattr(
        "app.web.auth.dependencies.auth_cache.get_session_context",
        lambda resolved_session_id: {
            "principal_id": "subscriber-1",
            "principal_type": "subscriber",
            "roles": ["cached-role"],
            "scopes": ["cached:scope"],
            "user": {
                "id": "subscriber-1",
                "first_name": "Cached",
                "last_name": "User",
                "email": "cached@example.com",
            },
        },
    )
    monkeypatch.setattr(
        "app.web.auth.dependencies.auth_cache.invalidate_session_context",
        lambda *args, **kwargs: invalidations.append((args, kwargs)),
    )

    assert validate_session_token(request, db_session) is None
    assert invalidations == [
        (
            (session_id,),
            {
                "principal_type": "subscriber",
                "principal_id": "subscriber-1",
            },
        )
    ]


def test_revoke_session_invalidates_cached_session_context(
    db_session, person, monkeypatch
):
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    invalidations = []

    monkeypatch.setattr(
        session_manager_service.auth_cache,
        "invalidate_session_context",
        lambda *args, **kwargs: invalidations.append((args, kwargs)),
    )

    session_manager_service.revoke_session(db_session, str(session.id), person.id)

    assert invalidations == [
        (
            (str(session.id),),
            {
                "principal_type": "subscriber",
                "principal_id": str(person.id),
            },
        )
    ]


def test_require_web_auth_redirects_unsafe_requests_to_referer(monkeypatch, db_session):
    monkeypatch.setattr(
        "app.web.auth.dependencies.validate_session_token",
        lambda request, db: None,
    )
    request = _make_web_request(
        method="POST",
        path="/admin/network/cpes",
        headers=[
            (b"host", b"oss.dotmac.ng"),
            (
                b"referer",
                b"https://oss.dotmac.ng/admin/network/cpes/new?subscriber_id=123",
            ),
        ],
    )

    with pytest.raises(AuthenticationRequired) as exc:
        require_web_auth(request=request, db=db_session)

    assert (
        exc.value.redirect_url
        == "/auth/refresh?next=/admin/network/cpes/new%3Fsubscriber_id%3D123"
    )


def test_require_web_auth_redirects_get_requests_to_current_path(
    monkeypatch, db_session
):
    monkeypatch.setattr(
        "app.web.auth.dependencies.validate_session_token",
        lambda request, db: None,
    )
    request = _make_web_request(
        method="GET",
        path="/admin/network/cpes",
        query_string=b"status=active",
        headers=[
            (b"host", b"oss.dotmac.ng"),
            (b"referer", b"https://oss.dotmac.ng/admin/network/cpes/new"),
        ],
    )

    with pytest.raises(AuthenticationRequired) as exc:
        require_web_auth(request=request, db=db_session)

    assert (
        exc.value.redirect_url
        == "/auth/refresh?next=/admin/network/cpes%3Fstatus%3Dactive"
    )


def test_require_web_auth_uses_vendor_refresh_and_rejects_non_vendor_referer(
    monkeypatch, db_session
):
    monkeypatch.setattr(
        "app.web.auth.dependencies.validate_session_token",
        lambda request, db: None,
    )
    request = _make_web_request(
        method="POST",
        path="/vendor/projects/123/route-revisions",
        headers=[
            (b"host", b"oss.dotmac.ng"),
            (b"referer", b"https://oss.dotmac.ng/admin/network/cpes"),
        ],
    )

    with pytest.raises(AuthenticationRequired) as exc:
        require_web_auth(request=request, db=db_session)

    assert exc.value.redirect_url == "/vendor/auth/refresh?next=/vendor"


def test_require_user_auth_loads_roles_and_scopes_from_db_when_missing(
    db_session, person, monkeypatch
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    role = Role(name="operator", is_active=True)
    permission = Permission(key="tickets:read", is_active=True)
    db_session.add_all([role, permission])
    db_session.flush()
    db_session.add(SubscriberRole(subscriber_id=person.id, role_id=role.id))
    db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.commit()

    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id))
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)

    assert "operator" in auth["roles"]
    assert "tickets:read" in auth["scopes"]


def test_require_audit_auth_loads_admin_role_from_db_when_jwt_has_no_claims(
    db_session, monkeypatch
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    user = SystemUser(
        first_name="Audit",
        last_name="Admin",
        display_name="Audit Admin",
        email="audit-admin@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))

    session = AuthSession(
        system_user_id=user.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(user.id),
            "principal_id": str(user.id),
            "principal_type": "system_user",
            "session_id": str(session.id),
            "typ": "access",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
        },
        "test-secret",
        algorithm="HS256",
    )

    auth = require_audit_auth(authorization=f"Bearer {token}", db=db_session)
    assert auth["actor_type"] == "user"
    assert auth["actor_id"] == str(user.id)


def test_require_audit_auth_rejects_api_key(db_session, person):
    # Security fix (2026-06-29): API keys carry no scopes, so they can never
    # satisfy the audit-scope gate that bearer tokens must pass. Accepting them
    # was an unscoped authorization bypass — audit auth now rejects API keys.
    raw_key = "raw-api-key"
    api_key = ApiKey(
        subscriber_id=person.id,
        label="test",
        key_hash=hash_api_key(raw_key),
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(api_key)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        require_audit_auth(
            authorization=None,
            x_session_token=None,
            x_api_key=raw_key,
            db=db_session,
        )
    assert exc.value.status_code == 401


def test_require_audit_auth_requires_scope(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id))
    with pytest.raises(HTTPException) as exc:
        require_audit_auth(authorization=f"Bearer {token}", db=db_session)
    assert exc.value.status_code == 403


def test_require_audit_auth_accepts_scope(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id), scopes=["audit:read"])
    auth = require_audit_auth(authorization=f"Bearer {token}", db=db_session)
    assert auth["actor_type"] == "user"
    assert auth["actor_id"] == str(person.id)


def test_require_audit_auth_accepts_session_token(db_session, person):
    refresh_token = uuid.uuid4().hex
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash=hash_session_token(refresh_token),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    auth = require_audit_auth(
        authorization=None, x_session_token=refresh_token, db=db_session
    )
    assert auth["actor_type"] == "user"


def test_extract_bearer_and_scope_helpers():
    assert auth_dep._extract_bearer_token(None) is None
    assert auth_dep._extract_bearer_token("Bearer token") == "token"
    assert auth_dep._extract_bearer_token("Token token") is None
    assert auth_dep._is_jwt("a.b.c") is True
    assert auth_dep._is_jwt("token") is False
    assert auth_dep._has_audit_scope({"scope": "audit:read"}) is True
    assert auth_dep._has_audit_scope({"roles": ["admin"]}) is True
    assert auth_dep._has_audit_scope({"scopes": ["other"]}) is False
    naive = datetime.now()
    assert auth_dep._as_utc(naive).tzinfo is not None
    aware = datetime.now(UTC)
    assert auth_dep._as_utc(aware) == aware
    assert auth_dep._as_utc(None) is None
    assert auth_dep._has_audit_scope({"role": "admin"}) is True


def test_require_role_and_permission(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id), roles=["support"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)

    role = Role(name="operator", is_active=True)
    permission = Permission(key="tickets:read", is_active=True)
    db_session.add_all([role, permission])
    db_session.commit()

    require_role = auth_dep.require_role("operator")
    with pytest.raises(HTTPException):
        require_role(auth=auth, db=db_session)

    link = SubscriberRole(subscriber_id=person.id, role_id=role.id)
    db_session.add(link)
    db_session.commit()
    assert require_role(auth=auth, db=db_session)["subscriber_id"] == str(person.id)

    require_permission = auth_dep.require_permission("tickets:read")
    with pytest.raises(HTTPException):
        require_permission(auth=auth, db=db_session)

    role_perm = RolePermission(role_id=role.id, permission_id=permission.id)
    db_session.add(role_perm)
    db_session.commit()
    assert require_permission(auth=auth, db=db_session)["subscriber_id"] == str(
        person.id
    )


def test_require_user_auth_missing_token(db_session):
    with pytest.raises(HTTPException):
        require_user_auth(authorization=None, db=db_session)


def test_require_user_auth_missing_claims(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = jwt.encode(
        {"typ": "access", "iat": 1, "exp": 9999999999},
        "test-secret",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        require_user_auth(authorization=f"Bearer {token}", db=db_session)


def test_require_audit_auth_invalid_session(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = _make_access_token(str(person.id), str(uuid.uuid4()), scopes=["audit:read"])
    with pytest.raises(HTTPException):
        require_audit_auth(authorization=f"Bearer {token}", db=db_session)


def test_require_audit_auth_revoked_session(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        revoked_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), scopes=["audit:read"])
    with pytest.raises(HTTPException):
        require_audit_auth(authorization=f"Bearer {token}", db=db_session)


def test_require_audit_auth_sets_actor_id(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), scopes=["audit:read"])
    request = Request({"type": "http", "headers": []})
    auth = require_audit_auth(
        authorization=f"Bearer {token}", request=request, db=db_session
    )
    assert request.state.actor_id == str(person.id)
    assert auth["actor_type"] == "user"


def test_require_audit_auth_session_token_sets_actor_id(db_session, person):
    refresh_token = uuid.uuid4().hex
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash=hash_session_token(refresh_token),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    request = Request({"type": "http", "headers": []})
    auth = require_audit_auth(
        authorization=None,
        x_session_token=refresh_token,
        request=request,
        db=db_session,
    )
    assert request.state.actor_id == str(person.id)
    assert auth["actor_type"] == "user"


def test_require_audit_auth_api_key_no_longer_authenticates(db_session, person):
    # Security fix (2026-06-29): an otherwise-valid API key must NOT grant audit
    # access (no audit scope possible on a key).
    raw_key = "raw-key"
    api_key = ApiKey(
        subscriber_id=person.id,
        label="test",
        key_hash=hash_api_key(raw_key),
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(api_key)
    db_session.commit()
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as exc:
        require_audit_auth(
            authorization=None,
            x_session_token=None,
            x_api_key=raw_key,
            request=request,
            db=db_session,
        )
    assert exc.value.status_code == 401


def test_require_role_missing_role(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), roles=["support"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)

    require_role = auth_dep.require_role("missing")
    with pytest.raises(HTTPException):
        require_role(auth=auth, db=db_session)


def test_require_role_short_circuit(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), roles=["admin"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)
    require_role = auth_dep.require_role("admin")
    assert require_role(auth=auth, db=db_session)["subscriber_id"] == str(person.id)


def test_require_permission_missing_permission(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), roles=["support"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)

    require_permission = auth_dep.require_permission("missing:perm")
    with pytest.raises(HTTPException):
        require_permission(auth=auth, db=db_session)


def test_require_permission_short_circuit(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), roles=["admin"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)
    require_permission = auth_dep.require_permission("any:perm")
    assert require_permission(auth=auth, db=db_session)["subscriber_id"] == str(
        person.id
    )
