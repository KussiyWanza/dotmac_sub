import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.routing import APIRoute
from jose import jwt
from starlette.requests import Request

from app.api.auth_flow import router as auth_flow_router
from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFARecoveryCode,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import CommunicationIntentRecord, Notification
from app.models.subscriber import UserType
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.services import auth_flow as auth_flow_service
from app.services import credential_recovery, staff_provisioning
from app.services import web_system_user_mutations as web_system_user_mutations_service
from app.services.auth_dependencies import require_user_auth
from app.services.auth_flow import (
    AuthFlow,
    change_password,
    hash_password,
    request_password_reset,
    reset_password,
    verify_password,
)
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import project_staff_login


def _make_request(user_agent: str = "pytest"):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth",
        "headers": [(b"user-agent", user_agent.encode("utf-8"))],
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def _route_requires_auth(path: str) -> bool:
    for route in auth_flow_router.routes:
        if isinstance(route, APIRoute) and route.path == path:
            return any(
                dependency.call is require_user_auth
                for dependency in route.dependant.dependencies
            )
    raise AssertionError(f"Route not found: {path}")


def test_login_and_refresh_reuse_detection(db_session, person, monkeypatch):
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="user@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    db_session.refresh(credential)

    request = _make_request()
    tokens = AuthFlow.login(db_session, "user@example.com", "secret", request, None)
    old_refresh = tokens["refresh_token"]

    rotated = AuthFlow.refresh(db_session, old_refresh, request)
    assert rotated["refresh_token"] != old_refresh

    with pytest.raises(HTTPException) as exc:
        AuthFlow.refresh(db_session, old_refresh, request)
    assert exc.value.status_code == 401
    assert "reuse" in str(exc.value.detail).lower()

    session = db_session.query(AuthSession).first()
    assert session.status == SessionStatus.revoked
    assert session.revoked_at is not None


def test_login_rejects_unsupported_provider(db_session, person):
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="user@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "user@example.com", "secret", request, "sso")
    assert exc.value.status_code == 400


def test_login_local_uses_username_not_subscriber_email(
    db_session, person, monkeypatch
):
    # Subscriber email is non-unique contact info, not a login key. Login must
    # use the credential username; the subscriber email must NOT authenticate.
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    person.email = "person-login@example.com"
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="admin-username",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    tokens = AuthFlow.login(db_session, "admin-username", "secret", request, None)
    assert tokens.get("access_token")
    assert tokens.get("refresh_token")

    # The subscriber email no longer resolves to a login.
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(
            db_session, "person-login@example.com", "secret", _make_request(), None
        )
    assert exc.value.status_code == 401


def test_login_radius_uses_username_not_subscriber_email(
    monkeypatch, db_session, person
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    person.email = "radius-login@example.com"
    called = {"username": None}

    def _fake_authenticate(db, username, password, server_id):
        called["username"] = username

    monkeypatch.setattr(
        "app.services.auth_flow.radius_auth_service.authenticate", _fake_authenticate
    )
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.radius,
        username="radius-user-001",
        password_hash=None,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    AuthFlow.login(
        db_session,
        "radius-user-001",
        "secret",
        _make_request(),
        AuthProvider.radius,
    )
    assert called["username"] == "radius-user-001"

    # The subscriber email no longer resolves to a RADIUS login.
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(
            db_session,
            "radius-login@example.com",
            "secret",
            _make_request(),
            AuthProvider.radius,
        )
    assert exc.value.status_code == 401


def test_mfa_setup_confirm(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    monkeypatch.setenv("TOTP_ISSUER", "DotmacSM")

    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="mfa@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    secret = setup["secret"]

    # Generate OTP code and verify using an explicit timecode to avoid
    # timezone skew (app imports may change TZ from CEST to Africa/Lagos,
    # causing datetime.now()/mktime() to disagree on the TOTP time step).
    import time as _time

    totp = pyotp.TOTP(secret)
    timecode = int(_time.time()) // totp.interval
    code = totp.generate_otp(timecode)

    # Patch verify to use the same timecode instead of datetime.now()
    _orig_generate = pyotp.TOTP.generate_otp

    def _verify_fixed(self, otp, for_time=None, valid_window=0):
        for i in range(-valid_window, valid_window + 1):
            if _orig_generate(self, timecode + i) == str(otp):
                return True
        return False

    monkeypatch.setattr(pyotp.TOTP, "verify", _verify_fixed)

    method = AuthFlow.mfa_confirm(
        db_session, str(setup["method_id"]), code, str(person.id)
    )

    assert method.enabled is True
    assert method.is_primary is True
    assert method.is_active is True
    assert method.verified_at is not None


def test_admin_mfa_setup_confirm_uses_system_user_id(db_session, monkeypatch):
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    system_user = SystemUser(
        first_name="Admin",
        last_name="Mfa",
        email="admin-mfa@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    credential = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username="admin-mfa@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.admin_mfa_setup(
        db_session, str(system_user.id), label="admin device"
    )
    method = db_session.get(MFAMethod, setup["method_id"])

    assert method is not None
    assert method.system_user_id == system_user.id
    assert method.subscriber_id is None
    assert method.enabled is False

    import time as _time

    totp = pyotp.TOTP(setup["secret"])
    timecode = int(_time.time()) // totp.interval
    code = totp.generate_otp(timecode)
    original_generate = pyotp.TOTP.generate_otp

    def verify_fixed(self, otp, for_time=None, valid_window=0):
        for offset in range(-valid_window, valid_window + 1):
            if original_generate(self, timecode + offset) == str(otp):
                return True
        return False

    monkeypatch.setattr(pyotp.TOTP, "verify", verify_fixed)
    confirmed = AuthFlow.admin_mfa_confirm(
        db_session,
        str(setup["method_id"]),
        code,
        str(system_user.id),
    )

    assert confirmed.enabled is True
    assert confirmed.is_primary is True
    assert confirmed.system_user_id == system_user.id
    assert confirmed.subscriber_id is None


def test_mfa_setup_requires_auth():
    assert _route_requires_auth("/auth/mfa/setup") is True


def test_mfa_confirm_requires_auth():
    assert _route_requires_auth("/auth/mfa/confirm") is True


def test_login_lockout_after_failed_attempts(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="lockout@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    for _ in range(5):
        with pytest.raises(HTTPException) as exc:
            AuthFlow.login(db_session, "lockout@example.com", "wrong", request, None)
        assert exc.value.status_code == 401

    db_session.refresh(credential)
    assert credential.locked_until is not None

    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "lockout@example.com", "secret", request, None)
    assert exc.value.status_code == 403


def test_login_requires_password_reset(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="reset@example.com",
        password_hash=hash_password("secret"),
        must_change_password=True,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "reset@example.com", "secret", request, None)
    assert exc.value.status_code == 428


def test_login_returns_mfa_token_when_enabled(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="mfa-login@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    code = pyotp.TOTP(setup["secret"]).now()
    AuthFlow.mfa_confirm(db_session, str(setup["method_id"]), code, str(person.id))

    request = _make_request()
    result = AuthFlow.login(
        db_session, "mfa-login@example.com", "secret", request, None
    )
    assert result["mfa_required"] is True
    assert result["mfa_token"]


def test_refresh_expired_token_marks_session(db_session, person):
    refresh_token = "refresh-token"
    session = AuthSession(
        person_id=person.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(refresh_token.encode("utf-8")).hexdigest(),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(session)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as exc:
        AuthFlow.refresh(db_session, refresh_token, request)
    assert exc.value.status_code == 401
    db_session.refresh(session)
    assert session.status == SessionStatus.expired


def test_request_and_reset_password(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="reset-flow@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = request_password_reset(db_session, person.email)
    assert result
    reset_at = reset_password(db_session, result["token"], "new-secret")
    assert isinstance(reset_at, datetime)
    db_session.refresh(credential)
    assert credential.must_change_password is False
    assert credential.failed_login_attempts == 0


def test_request_password_reset_accepts_ttl_override(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="reset-ttl@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = request_password_reset(db_session, person.email, ttl_minutes=1440)

    assert result
    payload = jwt.decode(
        result["token"],
        "test-secret",
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert payload["exp"] - payload["iat"] == 1440 * 60


def test_user_invite_uses_configured_invite_ttl(db_session, monkeypatch):
    captured: dict[str, int | None] = {}
    setting = DomainSetting(
        domain=SettingDomain.auth,
        key="user_invite_expiry_minutes",
        value_type=SettingValueType.integer,
        value_text="1440",
        is_active=True,
    )
    db_session.add(setting)
    db_session.commit()

    def _fake_reset_capability(db, email: str, *, ttl_minutes: int | None = None):
        captured["ttl_minutes"] = ttl_minutes
        return credential_recovery.PasswordResetCapability(
            token="reset-token",
            email=email,
            person_name="Invitee",
            principal_type="system_user",
            principal_id=uuid.uuid4(),
            ttl_minutes=int(ttl_minutes or 60),
        )

    monkeypatch.setattr(
        credential_recovery,
        "issue_reset_capability_for_email",
        _fake_reset_capability,
    )
    monkeypatch.setattr(
        "app.services.email.send_user_invite_email",
        lambda *args, **kwargs: True,
    )

    note = web_system_user_mutations_service.send_user_invite(
        db_session,
        email="invitee@example.com",
    )

    assert "invitation sent" in note.lower()
    assert captured["ttl_minutes"] == 1440


def test_password_reset_requires_local_credential(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.radius,
        username="pppoe-user",
        password_hash=None,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    assert request_password_reset(db_session, person.email) is None


def test_reset_password_updates_only_local_credential(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    radius_credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.radius,
        username="pppoe-user",
        password_hash=None,
        is_active=True,
    )
    local_credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="portal@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
        must_change_password=True,
        failed_login_attempts=3,
    )
    db_session.add_all([radius_credential, local_credential])
    db_session.commit()

    result = request_password_reset(db_session, person.email)
    assert result
    reset_password(db_session, result["token"], "new-secret")

    db_session.refresh(radius_credential)
    db_session.refresh(local_credential)
    assert radius_credential.password_hash is None
    assert verify_password("new-secret", local_credential.password_hash)
    assert local_credential.must_change_password is False
    assert local_credential.failed_login_attempts == 0


def test_change_password_updates_only_local_credential(db_session, person):
    radius_credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.radius,
        username="pppoe-user",
        password_hash=None,
        is_active=True,
    )
    local_credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="portal@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
        must_change_password=True,
    )
    db_session.add_all([radius_credential, local_credential])
    db_session.commit()

    change_password(db_session, str(person.id), "secret", "new-secret")

    db_session.refresh(radius_credential)
    db_session.refresh(local_credential)
    assert radius_credential.password_hash is None
    assert verify_password("new-secret", local_credential.password_hash)
    assert local_credential.must_change_password is False


def test_request_password_reset_unknown_email(db_session):
    assert request_password_reset(db_session, "missing@example.com") is None


def test_reset_password_rejects_invalid_token(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    with pytest.raises(HTTPException) as exc:
        reset_password(db_session, "not-a-token", "long-enough-secret")
    assert exc.value.status_code == 401


def _system_user_with_credential(db_session, email: str):
    system_user = SystemUser(
        first_name="Admin",
        last_name="Reset",
        email=email,
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    credential = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username=email,
        password_hash=hash_password("secret"),
        is_active=True,
    )
    project_staff_login(db_session, user=system_user, credential=credential)
    db_session.commit()
    return system_user, credential


def test_reset_password_rejects_short_password(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="short-pw@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = request_password_reset(db_session, person.email)
    assert result
    with pytest.raises(HTTPException) as exc:
        reset_password(db_session, result["token"], "short")
    assert exc.value.status_code == 400


def test_reset_password_uses_configured_min_length(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key="password_min_length",
            value_type=SettingValueType.integer,
            value_text="12",
            is_active=True,
        )
    )
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="configured-min-pw@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = request_password_reset(db_session, person.email)
    assert result
    with pytest.raises(HTTPException) as exc:
        reset_password(db_session, result["token"], "12345678901")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Password must be at least 12 characters"

    reset_at = reset_password(db_session, result["token"], "123456789012")
    assert reset_at.tzinfo is not None


def test_reset_token_is_single_use(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="single-use@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    # Mint the token in the past so the first reset moves
    # password_updated_at strictly past the token's iat.
    past = datetime.now(UTC) - timedelta(minutes=2)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_flow_service, "_now", lambda: past)
        result = request_password_reset(db_session, person.email)
    assert result

    reset_password(db_session, result["token"], "new-secret-one")
    with pytest.raises(HTTPException) as exc:
        reset_password(db_session, result["token"], "new-secret-two")
    assert exc.value.status_code == 401


def test_reset_password_rejects_inactive_principal(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="inactive-reset@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = request_password_reset(db_session, person.email)
    assert result
    person.is_active = False
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        reset_password(db_session, result["token"], "new-secret-one")
    assert exc.value.status_code == 401


def test_reset_password_revokes_active_sessions(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    system_user, credential = _system_user_with_credential(
        db_session, "admin-sessions@example.com"
    )

    request = _make_request()
    tokens = AuthFlow.login(db_session, system_user.email, "secret", request, None)
    assert tokens["refresh_token"]
    session = (
        db_session.query(AuthSession)
        .filter(AuthSession.system_user_id == system_user.id)
        .one()
    )
    assert session.status == SessionStatus.active

    result = request_password_reset(db_session, system_user.email)
    assert result
    reset_password(db_session, result["token"], "brand-new-secret")

    db_session.refresh(session)
    assert session.status == SessionStatus.revoked
    assert session.revoked_at is not None


def test_system_user_reset_token_ttl_capped_at_one_hour(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    system_user, credential = _system_user_with_credential(
        db_session, "admin-ttl@example.com"
    )

    result = request_password_reset(db_session, system_user.email)
    assert result
    assert result["ttl_minutes"] == 60
    payload = jwt.decode(
        result["token"],
        "test-secret",
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert payload["exp"] - payload["iat"] == 3600


def test_system_user_reset_ttl_explicit_override_wins(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    system_user, credential = _system_user_with_credential(
        db_session, "admin-ttl-override@example.com"
    )

    result = request_password_reset(db_session, system_user.email, ttl_minutes=1440)
    assert result
    assert result["ttl_minutes"] == 1440


def test_forgot_password_flow_rate_limited(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    person.email = "rate-limit-forgot@example.com"
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username=person.email,
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    recovery_email = person.email
    db_session.commit()

    for _ in range(5):
        auth_flow_service.forgot_password_flow(db_session, recovery_email)

    intents = (
        db_session.query(CommunicationIntentRecord)
        .filter(CommunicationIntentRecord.event_type == "auth.password_recovery")
        .all()
    )
    assert len(intents) == 3
    assert all(intent.body is None for intent in intents)


def test_web_forgot_password_submit_sends_email(db_session, person, monkeypatch):
    from app.services import web_auth as web_auth_service

    monkeypatch.setenv("JWT_SECRET", "test-secret")
    person.email = "web-forgot@example.com"
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username=person.email,
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    recovery_email = person.email
    db_session.commit()

    response = web_auth_service.forgot_password_submit(
        _make_request(), db_session, recovery_email
    )

    assert response.status_code == 200
    notification = (
        db_session.query(Notification)
        .filter(Notification.event_type == "auth.password_recovery")
        .one()
    )
    assert notification.recipient == recovery_email
    assert notification.body is None


def test_password_reset_does_not_bypass_mfa(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    person.email = "mfa-after-reset@example.com"
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username=person.email,
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    AuthFlow.mfa_confirm(
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        str(person.id),
    )

    result = request_password_reset(db_session, person.email)
    assert result
    reset_password(db_session, result["token"], "brand-new-secret")

    login_result = AuthFlow.login(
        db_session, person.email, "brand-new-secret", _make_request(), None
    )
    assert login_result["mfa_required"] is True
    assert "access_token" not in login_result


def test_admin_login_mfa_verify_issues_tokens(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    system_user, credential = _system_user_with_credential(
        db_session, "admin-mfa-verify@example.com"
    )

    setup = AuthFlow.admin_mfa_setup(db_session, str(system_user.id), label="device")
    AuthFlow.admin_mfa_confirm(
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        str(system_user.id),
    )

    request = _make_request()
    result = AuthFlow.login(db_session, system_user.email, "secret", request, None)
    assert result["mfa_required"] is True

    verified = AuthFlow.mfa_verify(
        db_session,
        result["mfa_token"],
        pyotp.TOTP(setup["secret"]).now(),
        request,
    )
    assert verified["access_token"]


def test_mfa_recovery_code_is_one_time_login_fallback(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="mfa-recovery@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    method = AuthFlow.mfa_confirm(
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        str(person.id),
    )
    recovery_codes = auth_flow_service.generate_mfa_recovery_codes(db_session, method)

    assert len(recovery_codes) == 10
    assert (
        db_session.query(MFARecoveryCode)
        .filter(MFARecoveryCode.mfa_method_id == method.id)
        .filter(MFARecoveryCode.is_active.is_(True))
        .count()
        == 10
    )
    stored_codes = (
        db_session.query(MFARecoveryCode)
        .filter(MFARecoveryCode.mfa_method_id == method.id)
        .all()
    )
    assert all(row.code_hash not in recovery_codes for row in stored_codes)

    request = _make_request()
    result = AuthFlow.login(
        db_session, "mfa-recovery@example.com", "secret", request, None
    )

    verified = AuthFlow.mfa_verify(
        db_session,
        result["mfa_token"],
        recovery_codes[0],
        request,
    )
    assert verified["access_token"]

    reused_login = AuthFlow.login(
        db_session, "mfa-recovery@example.com", "secret", request, None
    )
    with pytest.raises(HTTPException) as exc:
        AuthFlow.mfa_verify(
            db_session,
            reused_login["mfa_token"],
            recovery_codes[0],
            request,
        )
    assert exc.value.status_code == 401


def test_admin_mfa_verify_rejects_wrong_code(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    system_user, credential = _system_user_with_credential(
        db_session, "admin-mfa-wrong@example.com"
    )

    setup = AuthFlow.admin_mfa_setup(db_session, str(system_user.id), label="device")
    code = pyotp.TOTP(setup["secret"]).now()
    AuthFlow.admin_mfa_confirm(
        db_session, str(setup["method_id"]), code, str(system_user.id)
    )

    request = _make_request()
    result = AuthFlow.login(db_session, system_user.email, "secret", request, None)
    wrong_code = "000000" if code != "000000" else "111111"
    with pytest.raises(HTTPException) as exc:
        AuthFlow.mfa_verify(db_session, result["mfa_token"], wrong_code, request)
    assert exc.value.status_code == 401


def test_admin_login_requires_mfa_enrollment_when_forced(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    setting = DomainSetting(
        domain=SettingDomain.auth,
        key="admin_mfa_required",
        value_type=SettingValueType.boolean,
        value_text="true",
        is_active=True,
    )
    db_session.add(setting)
    system_user, credential = _system_user_with_credential(
        db_session, "admin-force-enroll@example.com"
    )

    result = AuthFlow.login(
        db_session, system_user.email, "secret", _make_request(), None
    )
    assert result["mfa_enrollment_required"] is True
    assert result["mfa_enrollment_token"]


def test_admin_login_legacy_force_2fa_still_requires_mfa(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    setting = DomainSetting(
        domain=SettingDomain.auth,
        key="force_2fa",
        value_type=SettingValueType.boolean,
        value_text="true",
        is_active=True,
    )
    db_session.add(setting)
    system_user, _credential = _system_user_with_credential(
        db_session, "admin-legacy-force-enroll@example.com"
    )

    result = AuthFlow.login(
        db_session, system_user.email, "secret", _make_request(), None
    )

    assert result["mfa_enrollment_required"] is True
    assert result["mfa_enrollment_token"]


def test_login_locked_account_is_not_a_password_oracle(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="oracle@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
        failed_login_attempts=5,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    for password in ("secret", "wrong"):
        with pytest.raises(HTTPException) as exc:
            AuthFlow.login(db_session, "oracle@example.com", password, request, None)
        assert exc.value.status_code == 403
        assert "Try again in 10 minutes." in str(exc.value.detail)

    db_session.refresh(credential)
    # Attempts while locked must not extend the lock or bump the counter.
    assert credential.failed_login_attempts == 5


def test_admin_login_lockout_uses_configured_policy(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.auth,
                key="admin_login_max_attempts",
                value_type=SettingValueType.integer,
                value_text="2",
                is_active=True,
            ),
            DomainSetting(
                domain=SettingDomain.auth,
                key="admin_lockout_minutes",
                value_type=SettingValueType.integer,
                value_text="7",
                is_active=True,
            ),
        ]
    )
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="configured-lockout@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            AuthFlow.login(
                db_session,
                "configured-lockout@example.com",
                "wrong",
                request,
                None,
            )
        assert exc.value.status_code == 401

    db_session.refresh(credential)
    assert credential.failed_login_attempts == 2
    assert credential.locked_until is not None
    remaining = auth_flow_service._as_utc(credential.locked_until) - datetime.now(UTC)
    assert timedelta(minutes=6) <= remaining <= timedelta(minutes=7)

    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(
            db_session,
            "configured-lockout@example.com",
            "secret",
            request,
            None,
        )
    assert exc.value.status_code == 403
    assert "Try again in 7 minutes." in str(exc.value.detail)


def test_login_expired_lock_starts_fresh_window(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="fresh-window@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
        failed_login_attempts=5,
        locked_until=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "fresh-window@example.com", "wrong", request, None)
    assert exc.value.status_code == 401

    db_session.refresh(credential)
    # One wrong attempt after an expired lock must not re-lock immediately.
    assert credential.failed_login_attempts == 1
    assert credential.locked_until is None

    tokens = AuthFlow.login(
        db_session, "fresh-window@example.com", "secret", request, None
    )
    assert tokens.get("access_token")


def test_radius_login_failures_lock_credential(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.radius,
        username="pppoe-lockout",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    def _fail(db, username, password, server_id):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    monkeypatch.setattr(
        "app.services.auth_flow.radius_auth_service.authenticate", _fail
    )
    request = _make_request()
    for _ in range(5):
        with pytest.raises(HTTPException) as exc:
            AuthFlow.login(db_session, "pppoe-lockout", "wrong", request, "radius")
        assert exc.value.status_code == 401

    db_session.refresh(credential)
    assert credential.locked_until is not None

    # Even a now-valid RADIUS password is rejected while locked.
    monkeypatch.setattr(
        "app.services.auth_flow.radius_auth_service.authenticate",
        lambda *a, **k: None,
    )
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "pppoe-lockout", "right", request, "radius")
    assert exc.value.status_code == 403


def test_mfa_verify_locks_after_repeated_wrong_codes(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    # This test covers the default lockout behavior. Keep it independent from
    # persisted/cached policy values; the next test covers policy resolution.
    monkeypatch.setattr(auth_flow_service, "_mfa_max_failed_attempts", lambda _db: 5)
    monkeypatch.setattr(auth_flow_service, "_mfa_lockout_minutes", lambda _db: 15)
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="mfa-lockout@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    totp = pyotp.TOTP(setup["secret"])
    AuthFlow.mfa_confirm(
        db_session, str(setup["method_id"]), totp.now(), str(person.id)
    )

    request = _make_request()
    result = AuthFlow.login(
        db_session, "mfa-lockout@example.com", "secret", request, None
    )
    mfa_token = result["mfa_token"]

    for _ in range(5):
        wrong = "000000" if totp.now() != "000000" else "111111"
        with pytest.raises(HTTPException) as exc:
            AuthFlow.mfa_verify(db_session, mfa_token, wrong, request)
        assert exc.value.status_code == 401

    # Locked: even the correct code is rejected with 429 until the lock expires.
    with pytest.raises(HTTPException) as exc:
        AuthFlow.mfa_verify(db_session, mfa_token, totp.now(), request)
    assert exc.value.status_code == 429
    assert "Try again in 15 minutes." in str(exc.value.detail)

    method = db_session.get(MFAMethod, setup["method_id"])
    assert method.locked_until is not None


def test_mfa_lockout_uses_configured_policy(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.auth,
                key="mfa_max_failed_attempts",
                value_type=SettingValueType.integer,
                value_text="2",
                is_active=True,
            ),
            DomainSetting(
                domain=SettingDomain.auth,
                key="mfa_lockout_minutes",
                value_type=SettingValueType.integer,
                value_text="4",
                is_active=True,
            ),
        ]
    )
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="configured-mfa-lockout@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    totp = pyotp.TOTP(setup["secret"])
    AuthFlow.mfa_confirm(
        db_session, str(setup["method_id"]), totp.now(), str(person.id)
    )

    request = _make_request()
    result = AuthFlow.login(
        db_session, "configured-mfa-lockout@example.com", "secret", request, None
    )
    mfa_token = result["mfa_token"]
    wrong = "000000" if totp.now() != "000000" else "111111"
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            AuthFlow.mfa_verify(db_session, mfa_token, wrong, request)
        assert exc.value.status_code == 401

    method = db_session.get(MFAMethod, setup["method_id"])
    assert method.locked_until is not None
    remaining = auth_flow_service._as_utc(method.locked_until) - datetime.now(UTC)
    assert timedelta(minutes=3) <= remaining <= timedelta(minutes=4)

    with pytest.raises(HTTPException) as exc:
        AuthFlow.mfa_verify(db_session, mfa_token, totp.now(), request)
    assert exc.value.status_code == 429
    assert "Try again in 4 minutes." in str(exc.value.detail)


def test_mfa_setup_reuses_pending_method_row(db_session, person, monkeypatch):
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    first = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    second = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    assert str(first["method_id"]) == str(second["method_id"])
    pending = (
        db_session.query(MFAMethod).filter(MFAMethod.subscriber_id == person.id).count()
    )
    assert pending == 1


def test_change_password_revokes_other_sessions(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="rotate@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    AuthFlow.login(db_session, "rotate@example.com", "secret", request, None)
    AuthFlow.login(db_session, "rotate@example.com", "secret", request, None)
    sessions = db_session.query(AuthSession).order_by(AuthSession.created_at).all()
    assert len(sessions) == 2
    current = sessions[0]

    change_password(
        db_session,
        str(person.id),
        "secret",
        "new-secret",
        current_session_id=str(current.id),
    )

    db_session.refresh(sessions[0])
    db_session.refresh(sessions[1])
    assert sessions[0].status == SessionStatus.active
    assert sessions[1].status == SessionStatus.revoked
    db_session.refresh(credential)
    assert verify_password("new-secret", credential.password_hash)


def test_reset_password_rejects_canceled_subscriber(db_session, person, monkeypatch):
    from app.models.subscriber import SubscriberStatus

    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="canceled-reset@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = request_password_reset(db_session, person.email)
    assert result
    person.status = SubscriberStatus.canceled
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        reset_password(db_session, result["token"], "new-secret")
    assert exc.value.status_code == 401


def test_reset_token_replay_rejected_even_same_second(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="same-second@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    # Issue and consume within the same second: the consumption must still
    # spend the token (password_updated_at is nudged past iat).
    result = request_password_reset(db_session, person.email)
    reset_password(db_session, result["token"], "new-secret-1")
    with pytest.raises(HTTPException) as exc:
        reset_password(db_session, result["token"], "new-secret-2")
    assert exc.value.status_code == 401


def test_session_manager_handles_system_user_principals(db_session):
    from app.services import session_manager

    system_user = SystemUser(
        first_name="Sess",
        last_name="Admin",
        email="sess-admin@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    session = AuthSession(
        system_user_id=system_user.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"sess-admin-token").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    listed = session_manager.list_sessions(
        db_session, system_user.id, None, principal_type="system_user"
    )
    assert listed.total == 1

    revoked = session_manager.revoke_all_other_sessions(
        db_session, system_user.id, None, principal_type="system_user"
    )
    assert revoked.revoked_count == 1


def test_admin_reset_password_clears_lockout(db_session, monkeypatch):
    system_user = SystemUser(
        first_name="Locked",
        last_name="Admin",
        email="locked-admin@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    credential = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username="locked-admin@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
        failed_login_attempts=5,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    db_session.add(credential)
    db_session.commit()

    web_system_user_mutations_service.reset_user_password(
        db_session, user_id=str(system_user.id)
    )
    db_session.refresh(credential)
    assert credential.failed_login_attempts == 0
    assert credential.locked_until is None


def test_deactivate_system_user_revokes_sessions(db_session):
    system_user = SystemUser(
        first_name="Gone",
        last_name="Admin",
        email="gone-admin@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    session = AuthSession(
        system_user_id=system_user.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"gone-admin-token").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    system_user_id = system_user.id
    db_session.commit()

    command_id = uuid.uuid4()
    staff_provisioning.set_staff_account_active(
        db_session,
        staff_provisioning.SetStaffAccountActiveCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="service:auth-flow-test",
                scope=staff_provisioning.STAFF_ASSIGN_SCOPE,
                reason="verify session revocation on staff deactivation",
                idempotency_key=f"deactivate:{system_user_id}",
            ),
            user_id=system_user_id,
            is_active=False,
        ),
    )
    db_session.refresh(session)
    assert session.status == SessionStatus.revoked


def test_disabled_principal_login_mutates_nothing(db_session, monkeypatch):
    """A correct password on a disabled principal must leave no trace.

    The eligibility gate used to run AFTER the successful-login mutations were
    committed, so a right password against a deactivated account still cleared
    the lockout window and stamped `last_login_at`. No token was ever issued —
    the leak was the write: it corrupted last-login evidence and left a
    credential-validity oracle in the data.
    """

    monkeypatch.setenv("JWT_SECRET", "test-secret")
    user = SystemUser(
        first_name="Disabled",
        last_name="Principal",
        email="disabled.principal@dotmac.io",
        user_type=UserType.system_user,
        is_active=False,
    )
    db_session.add(user)
    db_session.flush()
    locked_until = datetime.now(UTC) - timedelta(minutes=5)
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username="disabled.principal@dotmac.io",
        password_hash=hash_password("secret"),
        failed_login_attempts=3,
        locked_until=locked_until,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(
            db_session, "disabled.principal@dotmac.io", "secret", request, None
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Account disabled"

    db_session.expire_all()
    persisted = db_session.get(UserCredential, credential.id)
    assert persisted.last_login_at is None, "a refused login must not stamp last_login"
    assert persisted.failed_login_attempts == 3, "counters must be untouched"
    assert persisted.locked_until is not None, "the lockout window must survive"


def test_disabled_principal_is_not_distinguishable_from_a_wrong_password(
    db_session, monkeypatch
):
    """Eligibility stays AFTER password verification, on purpose.

    Checking it first would let an unauthenticated caller tell a disabled
    account from a wrong password without knowing the password — the same
    account-state oracle the lock check is careful to avoid.
    """

    monkeypatch.setenv("JWT_SECRET", "test-secret")
    user = SystemUser(
        first_name="Oracle",
        last_name="Probe",
        email="oracle.probe@dotmac.io",
        user_type=UserType.system_user,
        is_active=False,
    )
    db_session.add(user)
    db_session.flush()
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username="oracle.probe@dotmac.io",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as wrong:
        AuthFlow.login(db_session, "oracle.probe@dotmac.io", "nope", request, None)

    assert wrong.value.status_code == 401, (
        "a wrong password on a disabled account must answer 401 like any other "
        "wrong password, not reveal the account state"
    )
