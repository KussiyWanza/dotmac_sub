from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.schemas.auth_flow import MeUpdateRequest
from app.services import user_profile as user_profile_service
from app.services.auth_flow import hash_password


def test_get_me_supports_system_user(db_session):
    user = SystemUser(
        first_name="Admin",
        last_name="Account",
        display_name="Admin Account",
        email="admin-account@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username=user.email,
        password_hash=hash_password("Profile-owner-test-password"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = user_profile_service.get_me(
        db_session,
        principal_id=user.id,
        principal_type="system_user",
        roles=["admin"],
        scopes=["network:read"],
    )

    assert result.id == user.id
    assert result.email == user.email
    assert result.roles == ["admin"]
    assert result.scopes == ["network:read"]


def test_update_me_supports_system_user(db_session):
    user = SystemUser(
        first_name="Ops",
        last_name="Lead",
        display_name="Ops Lead",
        email="ops-lead@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    user_id = user.id
    db_session.add(
        UserCredential(
            system_user_id=user_id,
            provider=AuthProvider.local,
            username=user.email,
            password_hash=hash_password("Profile-owner-test-password"),
            is_active=True,
        )
    )
    db_session.commit()

    result = user_profile_service.update_me(
        db_session,
        principal_id=user_id,
        principal_type="system_user",
        payload=MeUpdateRequest(first_name="Operations", phone="+2348000000000"),
        roles=["admin"],
        scopes=[],
    )

    assert result.first_name == "Operations"
    assert result.phone == "+2348000000000"


def test_update_me_keeps_email_and_disabled_login_username_aligned(db_session):
    user = SystemUser(
        first_name="Field",
        last_name="Engineer",
        display_name="Field Engineer",
        email="field-engineer@example.com",
        user_type=UserType.system_user,
        is_active=False,
    )
    db_session.add(user)
    db_session.flush()
    user_id = user.id
    credential = UserCredential(
        system_user_id=user_id,
        provider=AuthProvider.local,
        username=user.email,
        password_hash=hash_password("Profile-owner-test-password"),
        is_active=False,
    )
    db_session.add(credential)
    db_session.commit()

    result = user_profile_service.update_me(
        db_session,
        principal_id=user_id,
        principal_type="system_user",
        payload=MeUpdateRequest(email="corrected-field-engineer@example.com"),
        roles=["staff"],
        scopes=[],
    )

    db_session.refresh(credential)
    assert result.email == "corrected-field-engineer@example.com"
    assert credential.username == result.email
    assert credential.is_active is False
