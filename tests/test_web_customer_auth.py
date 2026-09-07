from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import pyotp
from cryptography.fernet import Fernet
from starlette.requests import Request

from app.models.auth import AuthProvider, UserCredential
from app.models.catalog import AccessCredential
from app.services import customer_portal
from app.services import web_customer_auth as web_customer_auth_service
from app.services.auth_flow import AuthFlow, hash_password


def _request_with_cookie(name: str, value: str) -> Request:
    return _request(cookies={name: value})


def _request(cookies: dict[str, str] | None = None) -> Request:
    headers = []
    if cookies:
        headers.append(
            (
                b"cookie",
                "; ".join(f"{key}={value}" for key, value in cookies.items()).encode(),
            )
        )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/portal",
        "query_string": b"",
        "headers": headers,
    }
    request = Request(scope)
    request.state.csrf_token = "csrf"
    return request


def _response_cookies(response) -> dict[str, str]:
    jar = SimpleCookie()
    for header, value in response.raw_headers:
        if header.lower() == b"set-cookie":
            jar.load(value.decode())
    return {key: morsel.value for key, morsel in jar.items()}


def test_customer_login_template_has_submit_loading_state():
    template = Path("templates/customer/auth/login.html").read_text()

    assert 'x-data="{ loading: false }"' in template
    assert 'x-on:submit="loading = true"' in template
    assert ':disabled="loading"' in template
    assert "loading ? 'Signing in...' : 'Sign in'" in template


def test_customer_mfa_template_has_submit_loading_state():
    template = Path("templates/customer/auth/mfa.html").read_text()

    assert "x-data=\"{ loading: false, code: '' }\"" in template
    assert 'x-on:submit="loading = true"' in template
    assert 'x-model="code"' in template
    assert ':disabled="loading || code.length < 6"' in template
    assert "Verification or recovery code" in template
    assert "loading ? 'Verifying...' : 'Verify'" in template


def test_get_current_customer_enriches_module_flags(monkeypatch):
    monkeypatch.setattr(
        web_customer_auth_service.customer_portal,
        "get_current_customer",
        lambda _token, _db: {"username": "alice"},
    )
    monkeypatch.setattr(
        web_customer_auth_service.module_manager_service,
        "load_module_states",
        lambda _db: {"billing": False},
    )
    monkeypatch.setattr(
        web_customer_auth_service.module_manager_service,
        "load_feature_states",
        lambda _db: {"services_view": False},
    )

    request = _request_with_cookie(
        web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME,
        "session-token",
    )
    current = web_customer_auth_service.get_current_customer_from_request(
        request, db=object()
    )

    assert current is not None
    assert current["username"] == "alice"
    assert current["module_states"]["billing"] is False
    assert current["feature_states"]["services_view"] is False


def test_get_current_customer_returns_none_when_session_missing(monkeypatch):
    monkeypatch.setattr(
        web_customer_auth_service.customer_portal,
        "get_current_customer",
        lambda _token, _db: None,
    )
    request = _request_with_cookie(
        web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME,
        "missing",
    )
    assert (
        web_customer_auth_service.get_current_customer_from_request(
            request, db=object()
        )
        is None
    )


def test_customer_session_can_store_impersonation_marker(db_session, subscriber):
    token = customer_portal.create_customer_session(
        username="impersonate:reseller:test",
        account_id=subscriber.id,
        subscriber_id=subscriber.id,
        return_to="/reseller/accounts",
        is_impersonation=True,
        db=db_session,
    )

    session = customer_portal.get_customer_session(token)

    assert session is not None
    assert session["is_impersonation"] is True
    assert session["return_to"] == "/reseller/accounts"


def test_customer_impersonation_banner_uses_recoverable_exit_link():
    template = Path("templates/layouts/customer.html").read_text()

    assert (
        'href="/portal/auth/stop-impersonation?next={{ impersonation_return_to | urlencode }}"'
        in template
    )
    assert 'method="post" action="/portal/auth/stop-impersonation"' not in template


def test_customer_stop_impersonation_invalidates_session_and_returns_to_admin(
    monkeypatch,
):
    invalidated: list[str] = []
    monkeypatch.setattr(
        web_customer_auth_service.customer_portal,
        "invalidate_customer_session",
        invalidated.append,
    )
    request = _request_with_cookie(
        web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME,
        "impersonation-session",
    )

    response = web_customer_auth_service.customer_stop_impersonation(
        request,
        "/admin/customers/person/customer-id",
    )

    assert invalidated == ["impersonation-session"]
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/customers/person/customer-id"
    assert (
        _response_cookies(response)[
            web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME
        ]
        == ""
    )


def test_customer_login_allows_pppoe_when_local_credential_password_differs(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    local_credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="100024880",
        password_hash=hash_password("portal-secret"),
        is_active=True,
        must_change_password=True,
    )
    access_credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100024880",
        secret_hash="plain:pppoe-secret",
        is_active=True,
    )
    db_session.add_all([local_credential, access_credential])
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "100024880",
        "pppoe-secret",
        False,
        "/portal/dashboard",
    )

    db_session.refresh(local_credential)
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/dashboard"
    assert customer_portal.SESSION_COOKIE_NAME in _response_cookies(response)
    assert local_credential.failed_login_attempts == 0


def test_customer_login_records_local_failure_when_pppoe_fallback_fails(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    local_credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="100024881",
        password_hash=hash_password("portal-secret"),
        is_active=True,
    )
    access_credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100024881",
        secret_hash="plain:pppoe-secret",
        is_active=True,
    )
    db_session.add_all([local_credential, access_credential])
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "100024881",
        "wrong-secret",
        False,
        "/portal/dashboard",
    )

    db_session.refresh(local_credential)
    assert response.status_code == 401
    assert customer_portal.SESSION_COOKIE_NAME not in _response_cookies(response)
    assert local_credential.failed_login_attempts == 1


def test_customer_login_locked_account_shows_remaining_cooldown(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    local_credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="100024882",
        password_hash=hash_password("portal-secret"),
        is_active=True,
        failed_login_attempts=5,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    db_session.add(local_credential)
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "100024882",
        "portal-secret",
        False,
        "/portal/dashboard",
    )

    assert response.status_code == 401
    body = response.body.decode()
    assert "Account locked. Try again in 10 minutes." in body


def test_customer_login_radius_throttle_shows_remaining_cooldown(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service,
        "allow_operation",
        lambda *_args, **_kwargs: SimpleNamespace(
            allowed=False,
            retry_after_seconds=125,
        ),
    )

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "100024883",
        "portal-secret",
        False,
        "/portal/dashboard",
    )

    assert response.status_code == 401
    body = response.body.decode()
    assert "Account locked. Try again in 3 minutes." in body


def test_customer_login_redirects_to_mfa_when_enabled(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="mfa-customer@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(subscriber.id), label="device")
    AuthFlow.mfa_confirm(
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        str(subscriber.id),
    )

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "mfa-customer@example.com",
        "secret",
        True,
        "/portal/billing",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/portal/auth/mfa"
    cookies = _response_cookies(response)
    assert "customer_mfa_pending" in cookies
    assert "customer_mfa_context" in cookies
    assert customer_portal.SESSION_COOKIE_NAME not in cookies


def test_customer_mfa_submit_creates_customer_session(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="mfa-submit@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(subscriber.id), label="device")
    AuthFlow.mfa_confirm(
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        str(subscriber.id),
    )
    login_response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "mfa-submit@example.com",
        "secret",
        False,
        "/portal/profile",
    )
    pending_cookies = _response_cookies(login_response)

    invalid_response = web_customer_auth_service.customer_mfa_submit(
        _request(cookies=pending_cookies),
        db_session,
        "000000",
    )
    assert invalid_response.status_code == 401

    valid_response = web_customer_auth_service.customer_mfa_submit(
        _request(cookies=pending_cookies),
        db_session,
        pyotp.TOTP(setup["secret"]).now(),
    )

    assert valid_response.status_code == 303
    assert valid_response.headers["location"] == "/portal/profile"
    cookies = _response_cookies(valid_response)
    assert customer_portal.SESSION_COOKIE_NAME in cookies
    assert customer_portal.get_customer_session(
        cookies[customer_portal.SESSION_COOKIE_NAME]
    )


def test_customer_forgot_password_submit_uses_shared_flow(monkeypatch, db_session):
    from unittest.mock import MagicMock

    recovery_request = MagicMock()
    monkeypatch.setattr(
        web_customer_auth_service.credential_recovery,
        "request_password_recovery",
        recovery_request,
    )

    response = web_customer_auth_service.customer_forgot_password_submit(
        _request(), db_session, "customer@example.com"
    )

    assert response.status_code == 200
    assert "Check your email" in response.body.decode()
    recovery_request.assert_called_once()
    called_db, command = recovery_request.call_args.args
    assert called_db is db_session
    assert command.email == "customer@example.com"
    assert command.next_login_path == "/portal/auth/login?next=/portal/dashboard"


def test_customer_forgot_password_page_renders_form(monkeypatch, db_session):
    monkeypatch.setattr(
        web_customer_auth_service,
        "get_current_customer_from_request",
        lambda request, db: None,
    )

    response = web_customer_auth_service.customer_forgot_password_page(
        _request(), db_session
    )

    assert response.status_code == 200
    body = response.body.decode()
    assert 'action="/portal/auth/forgot-password"' in body
    assert "_csrf_token" in body
    assert 'x-data="{ loading: false }"' in body
    assert 'x-on:submit="loading = true"' in body
    assert ':disabled="loading"' in body
    assert "loading ? 'Sending...' : 'Send reset link'" in body


def test_customer_forgot_password_page_redirects_signed_in(monkeypatch, db_session):
    monkeypatch.setattr(
        web_customer_auth_service,
        "get_current_customer_from_request",
        lambda request, db: {"subscriber_id": "x"},
    )

    response = web_customer_auth_service.customer_forgot_password_page(
        _request(), db_session
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/portal/dashboard"
