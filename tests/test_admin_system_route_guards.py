import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.rbac import Permission, SystemUserPermission
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import web_system_profiles as web_system_profiles_service
from app.services.auth_flow import hash_password
from app.services.credential_crypto import decrypt_credential
from app.services.radius_population import record_device_login_sync_status
from app.web.admin import system as admin_system
from tests.staff_identity_fixtures import add_bound_staff_user


def test_require_system_user_principal_accepts_system_user():
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_type": "system_user"})
    )

    auth = admin_system._require_system_user_principal(request)

    assert auth["principal_type"] == "system_user"


def test_require_system_user_principal_rejects_subscriber():
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_type": "subscriber"})
    )

    with pytest.raises(HTTPException) as exc:
        admin_system._require_system_user_principal(request)

    assert exc.value.status_code == 403


def test_dbi_principal_id_prefers_stable_actor_id(monkeypatch):
    request = SimpleNamespace()
    monkeypatch.setattr(
        "app.web.admin.get_current_user",
        lambda _request: {"subscriber_id": "system-user-1", "id": "system-user-1"},
    )

    assert admin_system._dbi_principal_id(request) == "system-user-1"


def test_profile_state_prefers_system_user_id_for_mfa_status(db_session):
    current_user_record = SystemUser(
        first_name="Current",
        last_name="User",
        email="current@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    stale_user_record = SystemUser(
        first_name="Stale",
        last_name="User",
        email="stale@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add_all([current_user_record, stale_user_record])
    db_session.flush()
    db_session.add(
        MFAMethod(
            system_user_id=current_user_record.id,
            method_type=MFAMethodType.totp,
            label="Authenticator app",
            enabled=True,
            is_primary=True,
            is_active=True,
        )
    )
    db_session.commit()

    state = web_system_profiles_service.build_profile_page_state(
        db_session,
        current_user={"person_id": str(stale_user_record.id)},
        system_user_id=current_user_record.id,
    )

    assert state["person"].id == current_user_record.id
    assert state["mfa_enabled"] is True


def test_profile_state_lists_active_system_user_sessions(db_session):
    system_user, person = add_bound_staff_user(
        db_session,
        email="session-owner@example.com",
    )
    current = AuthSession(
        system_user_id=system_user.id,
        party_id=person.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"current-token").hexdigest(),
        ip_address="203.0.113.10",
        user_agent="Current Browser",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    other = AuthSession(
        system_user_id=system_user.id,
        party_id=person.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"other-token").hexdigest(),
        ip_address="203.0.113.11",
        user_agent="Other Browser",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add_all([current, other])
    db_session.commit()

    state = web_system_profiles_service.build_profile_page_state(
        db_session,
        current_user={"person_id": str(system_user.id)},
        system_user_id=system_user.id,
        current_session_id=str(current.id),
    )

    assert len(state["active_sessions"]) == 2
    assert state["other_session_count"] == 1
    assert any(session.is_current for session in state["active_sessions"])


def test_profile_template_includes_active_sessions_controls():
    template = Path("templates/admin/system/profile.html").read_text()

    assert "Active Sessions" in template
    assert "/admin/system/users/profile/sessions/sign-out-others" in template
    assert "Sign out other sessions" in template
    assert "session.is_current" in template


def test_profile_template_includes_router_device_login_self_service():
    template = Path("templates/admin/system/profile.html").read_text()

    assert "Router Device Login" in template
    assert "/admin/system/users/profile/device-login" in template
    assert "device_login_eligible" in template
    assert "RADIUS Sync" in template
    assert "Rotate Router Password" in template


def test_profile_state_marks_router_permissions_device_login_eligible(db_session):
    router_admin = SystemUser(
        first_name="Router",
        last_name="Admin",
        email="router-admin@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    router_operator = SystemUser(
        first_name="Router",
        last_name="Operator",
        email="router-operator@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    admin_perm = Permission(key="router:admin", is_active=True)
    write_perm = Permission(key="router:write", is_active=True)
    db_session.add_all([router_admin, router_operator, admin_perm, write_perm])
    db_session.flush()
    db_session.add_all(
        [
            SystemUserPermission(
                system_user_id=router_admin.id, permission_id=admin_perm.id
            ),
            SystemUserPermission(
                system_user_id=router_operator.id, permission_id=write_perm.id
            ),
        ]
    )
    record_device_login_sync_status(
        db_session,
        status="ok",
        result={"radcheck_upserts": 1, "app_disabled": 0},
    )

    admin_state = web_system_profiles_service.build_profile_page_state(
        db_session,
        current_user={"person_id": str(router_admin.id)},
        system_user_id=router_admin.id,
    )
    operator_state = web_system_profiles_service.build_profile_page_state(
        db_session,
        current_user={"person_id": str(router_operator.id)},
        system_user_id=router_operator.id,
    )

    assert admin_state["device_login_eligible"] is True
    assert admin_state["device_login_tier"] == "full"
    assert admin_state["device_login_sync_status"]["status"] == "ok"
    assert operator_state["device_login_eligible"] is True
    assert operator_state["device_login_tier"] == "write"


def test_profile_device_login_self_rotation_sets_secret(db_session, monkeypatch):
    system_user = SystemUser(
        first_name="Self",
        last_name="Rotator",
        email="self-rotator@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    permission = Permission(key="router:admin", is_active=True)
    db_session.add_all([system_user, permission])
    db_session.flush()
    db_session.add_all(
        [
            SystemUserPermission(
                system_user_id=system_user.id, permission_id=permission.id
            ),
            UserCredential(
                system_user_id=system_user.id,
                provider=AuthProvider.local,
                username=system_user.email,
                password_hash=hash_password("portal-secret"),
                is_active=True,
            ),
        ]
    )
    db_session.commit()
    sync_calls = []
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "principal_type": "system_user",
                "principal_id": str(system_user.id),
                "session_id": "session-1",
            }
        ),
        client=None,
        headers={},
    )
    monkeypatch.setattr(
        "app.tasks.radius_population.sync_device_login.delay",
        lambda: sync_calls.append(True),
    )

    response = admin_system.user_profile_device_login(
        request,
        form_data={
            "device_login_action": "set",
            "current_password": "portal-secret",
            "device_login_secret": "RouterPass123!",
            "device_login_secret_confirm": "RouterPass123!",
        },
        db=db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/admin/system/users/profile?device_login=updated"
    )
    assert sync_calls == [True]
    db_session.refresh(system_user)
    assert system_user.device_login_enabled is True
    assert decrypt_credential(system_user.device_login_secret) == "RouterPass123!"


def test_profile_device_login_self_rotation_rejects_weak_secret(
    db_session, monkeypatch
):
    system_user = SystemUser(
        first_name="Self",
        last_name="Weak",
        email="self-weak@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    permission = Permission(key="router:admin", is_active=True)
    db_session.add_all([system_user, permission])
    db_session.flush()
    db_session.add_all(
        [
            SystemUserPermission(
                system_user_id=system_user.id, permission_id=permission.id
            ),
            UserCredential(
                system_user_id=system_user.id,
                provider=AuthProvider.local,
                username=system_user.email,
                password_hash=hash_password("portal-secret"),
                is_active=True,
            ),
        ]
    )
    db_session.commit()
    mutation_calls = []
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "principal_type": "system_user",
                "principal_id": str(system_user.id),
            }
        ),
        client=None,
        headers={},
    )
    monkeypatch.setattr(
        "app.web.admin.system._user_profile_template_response",
        lambda request, db, **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        "app.web.admin.system.web_system_user_mutations_service.set_device_login",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)),
    )

    response = admin_system.user_profile_device_login(
        request,
        form_data={
            "device_login_action": "set",
            "current_password": "portal-secret",
            "device_login_secret": "NoSymbol1234",
            "device_login_secret_confirm": "NoSymbol1234",
        },
        db=db_session,
    )

    assert response.status_code == 400
    assert response.error == "Router password must include at least one symbol."
    assert mutation_calls == []


def test_profile_device_login_self_rotation_rejects_non_router_user(db_session):
    system_user = SystemUser(
        first_name="Self",
        last_name="Operator",
        email="self-operator@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    permission = Permission(key="customer:read", is_active=True)
    db_session.add_all([system_user, permission])
    db_session.flush()
    db_session.add(
        SystemUserPermission(system_user_id=system_user.id, permission_id=permission.id)
    )
    db_session.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "principal_type": "system_user",
                "principal_id": str(system_user.id),
            }
        )
    )

    with pytest.raises(HTTPException) as exc:
        admin_system.user_profile_device_login(
            request,
            form_data={
                "device_login_action": "set",
                "current_password": "portal-secret",
                "device_login_secret": "RouterPass123!",
                "device_login_secret_confirm": "RouterPass123!",
            },
            db=db_session,
        )

    assert exc.value.status_code == 403


def test_profile_sign_out_other_sessions_keeps_current(db_session):
    system_user, person = add_bound_staff_user(
        db_session,
        email="session-revoker@example.com",
    )
    current = AuthSession(
        system_user_id=system_user.id,
        party_id=person.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"current-revoke-token").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    other = AuthSession(
        system_user_id=system_user.id,
        party_id=person.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"other-revoke-token").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add_all([current, other])
    db_session.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "principal_type": "system_user",
                "principal_id": str(system_user.id),
                "session_id": str(current.id),
            }
        )
    )

    response = admin_system.user_profile_sign_out_other_sessions(request, db=db_session)

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/admin/system/users/profile?sessions=signed-out"
    )
    db_session.refresh(current)
    db_session.refresh(other)
    assert current.status == SessionStatus.active
    assert other.status == SessionStatus.revoked


def test_profile_mfa_confirm_redirects_with_success_flag(monkeypatch, db_session):
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={"principal_type": "system_user", "principal_id": "system-user-1"}
        )
    )
    captured = {}

    def fake_admin_mfa_confirm(db, method_id, code, system_user_id):
        captured["db"] = db
        captured["method_id"] = method_id
        captured["code"] = code
        captured["system_user_id"] = system_user_id

    monkeypatch.setattr(
        "app.services.auth_flow.auth_flow.admin_mfa_confirm",
        fake_admin_mfa_confirm,
    )

    response = admin_system.user_profile_mfa_confirm(
        request,
        method_id="method-1",
        code=" 123456 ",
        db=db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/system/users/profile?mfa=enabled"
    assert captured == {
        "db": db_session,
        "method_id": "method-1",
        "code": "123456",
        "system_user_id": "system-user-1",
    }
