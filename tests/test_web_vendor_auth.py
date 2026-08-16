from __future__ import annotations

from http.cookies import SimpleCookie
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.main import _DEFERRED_API_ROUTER_SPECS
from app.models.auth import AuthProvider, UserCredential
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.services.auth_flow import AuthFlow, hash_password
from app.web import vendor_auth_flow as web_vendor_auth
from tests.staff_identity_fixtures import project_staff_login


def _request(
    path: str = "/vendor/auth/login",
    *,
    method: str = "POST",
    cookies: dict[str, str] | None = None,
    referer: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = [
        (b"host", b"testserver"),
        (b"user-agent", b"vendor-auth-test"),
    ]
    if cookies:
        headers.append(
            (
                b"cookie",
                "; ".join(f"{key}={value}" for key, value in cookies.items()).encode(),
            )
        )
    if referer:
        headers.append((b"referer", referer.encode()))
    request = Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "path": path,
            "query_string": b"",
            "headers": headers,
        }
    )
    request.state.csrf_token = "vendor-auth-csrf"
    return request


def _response_cookies(response) -> dict[str, str]:
    jar = SimpleCookie()
    for header, value in response.raw_headers:
        if header.lower() == b"set-cookie":
            jar.load(value.decode())
    return {key: morsel.value for key, morsel in jar.items()}


def _vendor_identity(db_session, *, username: str):
    system_user = SystemUser(
        first_name="Vendor",
        last_name="Operator",
        display_name="Vendor Operator",
        email=username,
        user_type=UserType.vendor,
        is_active=True,
    )
    native_vendor = Vendor(name=f"Vendor {uuid4().hex[:8]}")
    db_session.add_all([system_user, native_vendor])
    db_session.flush()
    field_vendor = FieldVendor(
        name=native_vendor.name,
        code=f"VA-{uuid4().hex[:8]}",
        crm_vendor_id=str(native_vendor.id),
        is_active=True,
    )
    credential = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password("secret-123"),
        is_active=True,
        must_change_password=False,
    )
    db_session.add(field_vendor)
    project_staff_login(db_session, user=system_user, credential=credential)
    db_session.add(
        FieldVendorUser(
            vendor_id=field_vendor.id,
            system_user_id=system_user.id,
            role="owner",
            is_active=True,
        )
    )
    db_session.commit()
    return system_user, credential


def _non_vendor_identity(db_session, *, username: str, email: str | None = None):
    system_user = SystemUser(
        first_name="Staff",
        last_name="Only",
        email=email or username,
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    credential = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password("secret-123"),
        is_active=True,
    )
    project_staff_login(db_session, user=system_user, credential=credential)
    db_session.commit()
    return system_user


def _enable_mfa(db_session, system_user: SystemUser, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "vendor-auth-test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    setup = AuthFlow.admin_mfa_setup(
        db_session, str(system_user.id), label="Vendor test authenticator"
    )
    totp = pyotp.TOTP(setup["secret"])
    AuthFlow.admin_mfa_confirm(
        db_session,
        str(setup["method_id"]),
        totp.now(),
        str(system_user.id),
    )
    return totp


def test_vendor_auth_router_is_registered_before_vendor_portal() -> None:
    auth_spec = ("app.web.vendor_auth", "router", "web", "none")
    portal_spec = ("app.web.vendor_portal", "router", "web", "none")

    assert auth_spec in _DEFERRED_API_ROUTER_SPECS
    assert _DEFERRED_API_ROUTER_SPECS.index(auth_spec) < (
        _DEFERRED_API_ROUTER_SPECS.index(portal_spec)
    )


def test_vendor_password_login_success_and_failure(db_session, monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "vendor-auth-test-secret")
    username = f"vendor-{uuid4().hex[:8]}@example.com"
    _vendor_identity(db_session, username=username)

    success = web_vendor_auth.vendor_login_submit(
        _request(),
        db_session,
        username,
        "secret-123",
        remember=True,
        next_url="/vendor/projects?status=assigned",
    )

    assert isinstance(success, RedirectResponse)
    assert success.status_code == 303
    assert success.headers["location"] == "/vendor/projects?status=assigned"
    assert _response_cookies(success)["session_token"]

    failure = web_vendor_auth.vendor_login_submit(
        _request(),
        db_session,
        username,
        "wrong-password",
        remember=False,
        next_url="/vendor",
    )

    assert failure.status_code == 401
    assert "Invalid credentials" in failure.body.decode()
    assert "session_token" not in _response_cookies(failure)


def test_non_vendor_is_rejected_before_shared_login_runs(
    db_session, monkeypatch
) -> None:
    credential_username = f"staff-{uuid4().hex[:8]}"
    login_email = f"{credential_username}@example.com"
    _non_vendor_identity(
        db_session,
        username=credential_username,
        email=login_email,
    )
    login = MagicMock()
    monkeypatch.setattr(web_vendor_auth.auth_flow_service.auth_flow, "login", login)

    response = web_vendor_auth.vendor_login_submit(
        _request(),
        db_session,
        login_email,
        "secret-123",
        remember=False,
        next_url="/vendor",
    )

    assert response.status_code == 403
    assert web_vendor_auth.VENDOR_ACCESS_MESSAGE in response.body.decode()
    login.assert_not_called()


def test_unknown_user_response_matches_vendor_bad_password(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "vendor-auth-test-secret")
    username = f"vendor-{uuid4().hex[:8]}@example.com"
    _vendor_identity(db_session, username=username)

    bad_password = web_vendor_auth.vendor_login_submit(
        _request(),
        db_session,
        username,
        "wrong-password",
        remember=False,
        next_url="/vendor",
    )
    unknown_user = web_vendor_auth.vendor_login_submit(
        _request(),
        db_session,
        f"unknown-{uuid4().hex[:8]}@example.com",
        "wrong-password",
        remember=False,
        next_url="/vendor",
    )

    assert unknown_user.status_code == bad_password.status_code == 401
    assert unknown_user.body == bad_password.body


def test_vendor_mfa_success_and_invalid_code(db_session, monkeypatch) -> None:
    username = f"vendor-mfa-{uuid4().hex[:8]}@example.com"
    system_user, _credential = _vendor_identity(db_session, username=username)
    totp = _enable_mfa(db_session, system_user, monkeypatch)

    login = web_vendor_auth.vendor_login_submit(
        _request(),
        db_session,
        username,
        "secret-123",
        remember=True,
        next_url="/vendor/projects",
    )
    pending_cookies = _response_cookies(login)

    assert login.status_code == 303
    assert login.headers["location"] == "/vendor/auth/mfa?next=%2Fvendor%2Fprojects"
    assert pending_cookies["vendor_mfa_pending"]

    valid_code = totp.now()
    invalid_code = "000000" if valid_code != "000000" else "111111"
    invalid = web_vendor_auth.vendor_mfa_submit(
        _request(path="/vendor/auth/mfa", cookies=pending_cookies),
        db_session,
        invalid_code,
        next_url="/vendor/projects",
    )
    assert invalid.status_code == 401
    assert "Invalid verification code" in invalid.body.decode()

    success = web_vendor_auth.vendor_mfa_submit(
        _request(path="/vendor/auth/mfa", cookies=pending_cookies),
        db_session,
        valid_code,
        next_url="/vendor/projects",
    )
    assert success.status_code == 303
    assert success.headers["location"] == "/vendor/projects"
    assert _response_cookies(success)["session_token"]


def test_vendor_mfa_lockout_returns_429(db_session, monkeypatch) -> None:
    username = f"vendor-lockout-{uuid4().hex[:8]}@example.com"
    system_user, _credential = _vendor_identity(db_session, username=username)
    totp = _enable_mfa(db_session, system_user, monkeypatch)
    login = web_vendor_auth.vendor_login_submit(
        _request(),
        db_session,
        username,
        "secret-123",
        remember=False,
        next_url="/vendor",
    )
    pending_cookies = _response_cookies(login)
    valid_code = totp.now()
    invalid_code = "000000" if valid_code != "000000" else "111111"

    for _ in range(5):
        response = web_vendor_auth.vendor_mfa_submit(
            _request(path="/vendor/auth/mfa", cookies=pending_cookies),
            db_session,
            invalid_code,
            next_url="/vendor",
        )
        assert response.status_code == 401

    locked = web_vendor_auth.vendor_mfa_submit(
        _request(path="/vendor/auth/mfa", cookies=pending_cookies),
        db_session,
        invalid_code,
        next_url="/vendor",
    )

    assert locked.status_code == 429
    assert "Too many incorrect codes" in locked.body.decode()


def test_vendor_forced_reset_returns_to_vendor_login(db_session, monkeypatch) -> None:
    username = f"vendor-reset-{uuid4().hex[:8]}@example.com"
    _vendor_identity(db_session, username=username)

    def _password_reset_required(**_kwargs):
        raise HTTPException(
            status_code=428,
            detail={
                "code": "PASSWORD_RESET_REQUIRED",
                "message": "Password reset required",
            },
        )

    monkeypatch.setattr(
        web_vendor_auth.auth_flow_service.auth_flow,
        "login",
        _password_reset_required,
    )
    monkeypatch.setattr(
        web_vendor_auth.credential_recovery,
        "issue_reset_capability_for_email",
        MagicMock(return_value=MagicMock(token="reset-capability")),
    )

    response = web_vendor_auth.vendor_login_submit(
        _request(),
        db_session,
        username,
        "secret-123",
        remember=False,
        next_url="/vendor",
    )

    assert response.status_code == 303
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/auth/reset-password"
    assert parse_qs(parsed.query)["next_login"] == ["/vendor/auth/login?next=/vendor"]
    assert _response_cookies(response)["pwd_reset_token"] == "reset-capability"


def test_vendor_forgot_password_uses_vendor_login_recovery_path(
    db_session, monkeypatch
) -> None:
    recovery = MagicMock()
    monkeypatch.setattr(
        web_vendor_auth.credential_recovery,
        "request_password_recovery",
        recovery,
    )

    response = web_vendor_auth.vendor_forgot_password_submit(
        _request(path="/vendor/auth/forgot-password"),
        db_session,
        "vendor@example.com",
    )

    assert response.status_code == 200
    assert "Check your email" in response.body.decode()
    recovery.assert_called_once()
    called_db, command = recovery.call_args.args
    assert called_db is db_session
    assert command.email == "vendor@example.com"
    assert command.next_login_path == "/vendor/auth/login?next=/vendor"


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("/vendor", "/vendor"),
        ("/vendor/projects/123?tab=route", "/vendor/projects/123?tab=route"),
        ("/admin", "/vendor"),
        ("/vendorish", "/vendor"),
        ("//attacker.example/vendor", "/vendor"),
        ("/\\attacker.example/vendor", "/vendor"),
        ("https://attacker.example/vendor", "/vendor"),
        ("", "/vendor"),
    ],
)
def test_vendor_next_path_validation(candidate: str, expected: str) -> None:
    assert web_vendor_auth._safe_vendor_next(candidate) == expected
