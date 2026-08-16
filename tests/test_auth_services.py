import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import Mock

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.rbac import Role, SystemUserRole
from app.models.subscriber import UserType
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyUpdate,
    MFAMethodCreate,
    MFAMethodUpdate,
    SessionCreate,
    UserCredentialCreate,
    UserCredentialUpdate,
)
from app.services import auth as auth_service
from app.services import auth_flow as auth_flow_service
from app.services import settings_spec, staff_party_authentication
from app.services import web_auth as web_auth_service
from app.services import web_system_config as web_system_config_service
from app.services.auth_flow import hash_password
from tests.staff_identity_fixtures import project_staff_login


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, _seconds):
        return True


class _FakeRedisLimit:
    def __init__(self, count):
        self.count = count

    def incr(self, _key):
        return self.count

    def expire(self, _key, _seconds):
        return True


def _make_request(user_agent: str = "pytest") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [(b"user-agent", user_agent.encode("utf-8"))],
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def _make_get_request(path: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [(b"user-agent", b"pytest")],
        "client": ("127.0.0.1", 12345),
        "query_string": b"",
    }
    return Request(scope)


def _response_cookies(response) -> dict[str, str]:
    jar = SimpleCookie()
    for header, value in response.raw_headers:
        if header.lower() == b"set-cookie":
            jar.load(value.decode())
    return {key: morsel.value for key, morsel in jar.items()}


def _make_system_user_with_login(db_session, *, email: str):
    user = SystemUser(
        first_name="Admin",
        last_name="User",
        display_name="Admin User",
        email=email,
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    role = Role(name=f"admin-{email}", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))

    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username=email,
        password_hash=hash_password("secret"),
        is_active=True,
    )
    project_staff_login(db_session, user=user, credential=credential)
    db_session.commit()
    return user


def test_admin_login_reseller_tile_links_directly_to_reseller_login():
    response = web_auth_service.login_page(
        _make_get_request("/auth/login"),
        next_url="/admin",
    )

    body = response.body.decode()

    assert 'href="/reseller/auth/login?next=/reseller/dashboard"' in body
    assert 'href="/reseller"' not in body


def test_admin_login_vendor_tile_links_directly_to_vendor_login():
    response = web_auth_service.login_page(
        _make_get_request("/auth/login"),
        next_url="/admin",
    )

    body = response.body.decode()

    assert 'href="/vendor/auth/login?next=/vendor"' in body


def test_admin_login_page_uses_configured_remember_duration(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key="jwt_refresh_ttl_days",
            value_type=SettingValueType.integer,
            value_text="10",
            is_active=True,
        )
    )
    db_session.commit()

    response = web_auth_service.login_page(
        _make_get_request("/auth/login"),
        next_url="/admin",
        db=db_session,
    )

    assert "Remember me for 10 days" in response.body.decode()


def _make_get_request_with_cookie(path: str, cookie: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [
            (b"user-agent", b"pytest"),
            (b"cookie", cookie.encode("utf-8")),
        ],
        "client": ("127.0.0.1", 12345),
        "query_string": b"",
    }
    return Request(scope)


def test_reset_password_page_reads_token_from_cookie(db_session):
    # Forced-reset flow delivers the token via an HttpOnly cookie, not the URL.
    response = web_auth_service.reset_password_page(
        _make_get_request_with_cookie(
            "/auth/reset-password",
            f"{web_auth_service.PASSWORD_RESET_COOKIE}=cookie-token-xyz",
        ),
        db_session,
        None,
    )
    body = response.body.decode()
    assert "cookie-token-xyz" in body


def test_reset_password_page_uses_configured_min_length(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key="password_min_length",
            value_type=SettingValueType.integer,
            value_text="12",
            is_active=True,
        )
    )
    db_session.commit()

    response = web_auth_service.reset_password_page(
        _make_get_request("/auth/reset-password"),
        db_session,
        "reset-token",
    )
    body = response.body.decode()

    assert 'minlength="12"' in body
    assert "Must be at least 12 characters" in body
    assert "password.length >= this.passwordMinLength" in body


def _enable_force_admin_mfa(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key="force_2fa",
            value_type=SettingValueType.string,
            value_text="true",
        )
    )
    db_session.commit()


def test_mfa_template_does_not_link_to_unimplemented_recovery_route():
    template = Path("templates/auth/mfa.html").read_text()

    assert "/auth/mfa/recovery" not in template
    assert "Use a recovery code" not in template
    assert "Contact an administrator to reset MFA" in template


def test_admin_forgot_password_template_has_submit_loading_state():
    template = Path("templates/auth/forgot-password.html").read_text()

    assert 'x-data="{ loading: false }"' in template
    assert 'x-on:submit="loading = true"' in template
    assert ':disabled="loading"' in template
    assert "loading ? 'Sending...' : 'Send reset link'" in template


def test_auth_admin_policy_settings_are_registered():
    expected = {
        "admin_mfa_required": False,
        "admin_login_max_attempts": 5,
        "admin_lockout_minutes": 15,
        "mfa_max_failed_attempts": 5,
        "mfa_lockout_minutes": 15,
        "password_min_length": 8,
    }

    for key, default in expected.items():
        spec = settings_spec.get_spec(SettingDomain.auth, key)
        assert spec is not None
        assert spec.default == default


def test_preferences_context_reads_legacy_force_2fa(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key="force_2fa",
            value_type=SettingValueType.boolean,
            value_text="true",
            is_active=True,
        )
    )
    db_session.commit()

    context = web_system_config_service.get_preferences_context(db_session)

    assert context["preferences"]["admin_mfa_required"] == "true"


def test_preferences_save_writes_canonical_admin_mfa_required(db_session):
    web_system_config_service.save_preferences(
        db_session,
        {
            "default_landing_page": "admin",
            "admin_portal_title": "DotMac Admin",
            "admin_mfa_required": "true",
            "search_debounce_ms": "250",
        },
    )

    setting = (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .filter(DomainSetting.key == "admin_mfa_required")
        .one()
    )
    assert setting.value_text == "true"
    assert (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .filter(DomainSetting.key == "force_2fa")
        .first()
        is None
    )


def test_user_credentials_soft_delete(db_session, person):
    payload = UserCredentialCreate(
        person_id=person.id,
        username="user@example.com",
        password_hash=hash_password("secret"),
    )
    credential = auth_service.user_credentials.create(db_session, payload)
    active = auth_service.user_credentials.list(
        db_session,
        person_id=str(person.id),
        provider=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    assert len(active) == 1
    auth_service.user_credentials.delete(db_session, str(credential.id))
    active = auth_service.user_credentials.list(
        db_session,
        person_id=str(person.id),
        provider=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    inactive = auth_service.user_credentials.list(
        db_session,
        person_id=str(person.id),
        provider=None,
        is_active=False,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    assert active == []
    assert len(inactive) == 1


def test_user_credentials_require_valid_person(db_session):
    payload = UserCredentialCreate(
        person_id=uuid.uuid4(),
        username="user@example.com",
        password_hash=hash_password("secret"),
    )
    with pytest.raises(HTTPException) as exc:
        auth_service.user_credentials.create(db_session, payload)
    assert exc.value.status_code == 404


def test_mfa_primary_switch(db_session, person):
    payload = MFAMethodCreate(
        person_id=person.id,
        method_type="totp",
        label="primary",
        secret="encrypted",
        is_primary=True,
        enabled=True,
    )
    first = auth_service.mfa_methods.create(db_session, payload)
    second = auth_service.mfa_methods.create(
        db_session,
        MFAMethodCreate(
            person_id=person.id,
            method_type="totp",
            label="secondary",
            secret="encrypted2",
            is_primary=True,
            enabled=True,
        ),
    )
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.is_primary is False
    assert second.is_primary is True


def test_mfa_update_primary_clears_previous(db_session, person):
    first = auth_service.mfa_methods.create(
        db_session,
        MFAMethodCreate(
            person_id=person.id,
            method_type="totp",
            label="primary",
            secret="encrypted",
            is_primary=True,
            enabled=True,
        ),
    )
    second = auth_service.mfa_methods.create(
        db_session,
        MFAMethodCreate(
            person_id=person.id,
            method_type="totp",
            label="secondary",
            secret="encrypted2",
            is_primary=False,
            enabled=True,
        ),
    )
    updated = auth_service.mfa_methods.update(
        db_session,
        str(second.id),
        MFAMethodUpdate(
            person_id=person.id,
            is_primary=True,
        ),
    )
    db_session.refresh(first)
    db_session.refresh(updated)
    assert first.is_primary is False
    assert updated.is_primary is True


def test_session_delete_revokes(db_session, person):
    payload = SessionCreate(
        person_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        ip_address="127.0.0.1",
        user_agent="pytest",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    session = auth_service.sessions.create(db_session, payload)
    auth_service.sessions.delete(db_session, str(session.id))
    db_session.refresh(session)
    assert session.status == SessionStatus.revoked
    assert session.revoked_at is not None


def test_user_credentials_update_requires_radius_server(db_session, person):
    payload = UserCredentialCreate(
        person_id=person.id,
        username="user@example.com",
        password_hash=hash_password("secret"),
    )
    credential = auth_service.user_credentials.create(db_session, payload)
    update = UserCredentialUpdate(radius_server_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        auth_service.user_credentials.update(db_session, str(credential.id), update)
    assert exc.value.status_code == 404


def test_api_key_generate_with_redis(monkeypatch, db_session):
    fake = _FakeRedis()
    monkeypatch.setattr(auth_service, "_get_redis_client", lambda: fake)
    payload = ApiKeyGenerateRequest(label="test")
    result = auth_service.api_keys.generate_with_rate_limit(db_session, payload, None)
    raw_key = result["key"]
    api_key = result["api_key"]
    # Scheme-agnostic: matches whatever hash_api_key produces (HMAC when a
    # server secret is configured, legacy sha256 otherwise).
    assert auth_service.hash_api_key(raw_key) == api_key.key_hash


def test_api_key_rate_limit_requires_redis(monkeypatch, db_session):
    monkeypatch.setattr(auth_service, "_get_redis_client", lambda: None)
    with pytest.raises(HTTPException) as exc:
        auth_service.api_keys.generate_with_rate_limit(
            db_session, ApiKeyGenerateRequest(label="test"), None
        )
    assert exc.value.status_code == 503


def test_api_key_rate_limit_exceeded(monkeypatch, db_session):
    fake = _FakeRedisLimit(count=2)
    monkeypatch.setattr(auth_service, "_get_redis_client", lambda: fake)
    monkeypatch.setattr(auth_service, "_auth_int_setting", lambda _db, key, default: 1)
    with pytest.raises(HTTPException) as exc:
        auth_service.api_keys.generate_with_rate_limit(
            db_session, ApiKeyGenerateRequest(label="test"), None
        )
    assert exc.value.status_code == 429


def test_api_key_update_and_revoke(db_session, person):
    created = auth_service.api_keys.create(
        db_session,
        ApiKeyCreate(
            person_id=person.id,
            label="test",
            key_hash="raw-key",
        ),
    )
    updated = auth_service.api_keys.update(
        db_session,
        str(created.id),
        ApiKeyUpdate(key_hash="new-key"),
    )
    assert updated.key_hash == auth_service.hash_api_key("new-key")

    auth_service.api_keys.revoke(db_session, str(created.id))
    db_session.refresh(created)
    assert created.is_active is False
    assert created.revoked_at is not None


def test_web_login_submit_redirects_to_mfa_when_required(monkeypatch, db_session):
    monkeypatch.setattr(
        web_auth_service.auth_flow_service.auth_flow,
        "login",
        lambda **_kwargs: {"mfa_required": True, "mfa_token": "mfa-token"},
    )

    response = web_auth_service.login_submit(
        _make_request(),
        db_session,
        "admin",
        "secret",
        False,
        "",
    )

    assert response.status_code == 303
    assert response.headers.get("location") == "/auth/mfa"
    assert "mfa_pending=mfa-token" in response.headers.get("set-cookie", "")


def test_web_login_submit_supports_system_user(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    user = SystemUser(
        first_name="Admin",
        last_name="User",
        display_name="Admin User",
        email="admin-system@example.com",
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
        username="admin-system@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    project_staff_login(db_session, user=user, credential=credential)
    db_session.commit()

    response = web_auth_service.login_submit(
        _make_request(),
        db_session,
        "admin-system@example.com",
        "secret",
        False,
        "",
    )

    assert response.status_code == 303
    assert response.headers.get("location") == "/admin/dashboard"
    assert "session_token=" in response.headers.get("set-cookie", "")


def test_web_login_submit_forces_admin_mfa_enrollment(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    _enable_force_admin_mfa(db_session)
    _make_system_user_with_login(db_session, email="forced-admin@example.com")

    response = web_auth_service.login_submit(
        _make_request(),
        db_session,
        "forced-admin@example.com",
        "secret",
        False,
        "/admin/system",
    )

    assert response.status_code == 303
    assert response.headers.get("location") == "/auth/mfa/enroll?next=/admin/system"
    cookies = _response_cookies(response)
    assert web_auth_service.MFA_ENROLLMENT_COOKIE in cookies
    assert "session_token" not in cookies


def test_web_mfa_enroll_confirm_creates_admin_session(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    system_user = _make_system_user_with_login(
        db_session, email="enroll-admin@example.com"
    )
    setup = auth_flow_service.auth_flow.admin_mfa_setup(
        db_session, str(system_user.id), "Authenticator app"
    )
    enrollment_token = auth_flow_service._issue_mfa_enrollment_token(  # noqa: SLF001
        db_session,
        str(system_user.id),
        "system_user",
        staff_binding=staff_party_authentication.binding_for_principal(system_user),
    )
    request = _make_request()
    request.scope["headers"].append(
        (
            b"cookie",
            f"{web_auth_service.MFA_ENROLLMENT_COOKIE}={enrollment_token}".encode(),
        )
    )

    invalid_response = web_auth_service.mfa_enroll_confirm(
        request,
        db_session,
        str(setup["method_id"]),
        "000000",
        "/admin/system",
    )
    assert invalid_response.status_code == 401

    valid_response = web_auth_service.mfa_enroll_confirm(
        request,
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        "/admin/system",
    )

    assert valid_response.status_code == 200
    assert valid_response.context["continue_url"] == "/admin/system"
    assert len(valid_response.context["recovery_codes"]) == 10
    cookies = _response_cookies(valid_response)
    assert "session_token" in cookies
    assert web_auth_service.MFA_ENROLLMENT_COOKIE in cookies
    assert cookies[web_auth_service.MFA_ENROLLMENT_COOKIE] == ""


def test_web_login_submit_issues_lean_session_cookie_for_system_user(
    db_session, monkeypatch
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    user = SystemUser(
        first_name="Large",
        last_name="Admin",
        display_name="Large Admin",
        email="large-admin@example.com",
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
        username="large-admin@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    project_staff_login(db_session, user=user, credential=credential)
    db_session.commit()

    response = web_auth_service.login_submit(
        _make_request(),
        db_session,
        "large-admin@example.com",
        "secret",
        False,
        "",
    )

    cookie_header = response.headers.get("set-cookie", "")
    assert "session_token=" in cookie_header
    assert "roles" not in cookie_header
    assert "scopes" not in cookie_header


def test_web_refresh_issues_session_cookie_via_module_helper(monkeypatch, db_session):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/refresh",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
        }
    )

    monkeypatch.setattr(
        web_auth_service.AuthFlow,
        "resolve_refresh_token",
        lambda _request, _authorization, _db: "refresh-token",
    )
    monkeypatch.setattr(
        web_auth_service.auth_flow_service.auth_flow,
        "refresh",
        lambda _db, _refresh_token, _request: {
            "access_token": "access-token",
            "refresh_token": "rotated-refresh-token",
        },
    )
    issue_session = Mock(return_value="web-session-token")
    monkeypatch.setattr(
        web_auth_service.auth_flow_service,
        "issue_web_session_token",
        issue_session,
    )

    response = web_auth_service.refresh(
        request, db_session, next_url="/admin/dashboard"
    )

    assert response.status_code == 303
    assert response.headers.get("location") == "/admin/dashboard"
    issue_session.assert_called_once_with(db_session, "access-token")
    cookie_headers = [
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key.lower() == b"set-cookie"
    ]
    assert any("session_token=web-session-token" in header for header in cookie_headers)
    assert any("rotated-refresh-token" in header for header in cookie_headers)


def test_user_credentials_duplicate_username_is_409_not_500(db_session, person):
    """A taken username is a client error, not a server error.

    The unique constraint on local-provider usernames used to escape as an
    unhandled IntegrityError, so POST /api/v1/user-credentials answered 500 and
    leaked a database constraint name instead of naming the bad input.
    """
    payload = UserCredentialCreate(
        person_id=person.id,
        username="taken@example.com",
        password_hash=hash_password("secret"),
    )
    auth_service.user_credentials.create(db_session, payload)

    duplicate = UserCredentialCreate(
        person_id=person.id,
        username="taken@example.com",
        password_hash=hash_password("other"),
    )
    with pytest.raises(HTTPException) as excinfo:
        auth_service.user_credentials.create(db_session, duplicate)

    assert excinfo.value.status_code == 409
    assert "username" in str(excinfo.value.detail).lower()

    # The session must still be usable: an un-rolled-back failed flush poisons
    # every later query on it with PendingRollbackError. Deliberately not a
    # query about `person` -- the rollback discarded the transaction the
    # fixture created that row in.
    assert db_session.query(UserCredential).count() >= 0
