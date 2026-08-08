"""Admin read-model coverage for staff login identity state."""

from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import staff_provisioning, web_system_profiles
from app.services.auth_flow import hash_password


def _user(db_session, *, email: str, active: bool) -> SystemUser:
    user = SystemUser(
        first_name="Login",
        last_name="Identity",
        display_name="Login Identity",
        email=email,
        user_type=UserType.system_user,
        is_active=active,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _credential(
    db_session,
    *,
    user: SystemUser,
    username: str,
    active: bool,
) -> UserCredential:
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password("Login-identity-read-model-password"),
        is_active=active,
        must_change_password=True,
    )
    db_session.add(credential)
    return credential


def test_login_card_includes_disabled_local_credential(db_session) -> None:
    user = _user(db_session, email="disabled.card@example.com", active=False)
    _credential(
        db_session,
        user=user,
        username=user.email,
        active=False,
    )
    db_session.commit()

    detail = web_system_profiles.get_user_detail_data(db_session, user.id)

    assert detail is not None
    assert detail["credential"].username == user.email
    assert detail["credential"].status == (
        staff_provisioning.StaffCredentialDisplayStatus.disabled
    )
    assert detail["credential_issue"] is None
    assert detail["credential_recovery"].allowed is False
    assert detail["credential_recovery"].reason == "Activate the staff account first."


def test_login_card_marks_username_mismatch_for_reconciliation(db_session) -> None:
    user = _user(db_session, email="current.card@example.com", active=True)
    _credential(
        db_session,
        user=user,
        username="old.card@example.com",
        active=True,
    )
    db_session.commit()

    detail = web_system_profiles.get_user_detail_data(db_session, user.id)

    assert detail is not None
    assert detail["credential"].status == (
        staff_provisioning.StaffCredentialDisplayStatus.needs_reconciliation
    )
    assert detail["credential_issue"] == (
        "Login username does not match the profile email."
    )
    assert detail["credential_recovery"].allowed is True


def test_login_card_marks_activation_mismatch_as_repairable(db_session) -> None:
    user = _user(db_session, email="activation.card@example.com", active=True)
    _credential(
        db_session,
        user=user,
        username=user.email,
        active=False,
    )
    db_session.commit()

    detail = web_system_profiles.get_user_detail_data(db_session, user.id)

    assert detail is not None
    assert detail["credential"].status == (
        staff_provisioning.StaffCredentialDisplayStatus.needs_reconciliation
    )
    assert detail["credential_issue"] == (
        "Staff account and local credential activation states do not match."
    )
    assert detail["credential_recovery"].allowed is True


def test_login_card_blocks_recovery_when_profile_email_is_occupied(db_session) -> None:
    user = _user(db_session, email="occupied.card@example.com", active=True)
    _credential(
        db_session,
        user=user,
        username="old.occupied.card@example.com",
        active=True,
    )
    owner = _user(db_session, email="other.card@example.com", active=True)
    _credential(
        db_session,
        user=owner,
        username="occupied.card@example.com",
        active=True,
    )
    db_session.commit()

    detail = web_system_profiles.get_user_detail_data(db_session, user.id)

    assert detail is not None
    assert detail["credential_issue"] == (
        "Profile email is already used by another local login credential."
    )
    assert detail["credential_recovery"].allowed is False


def test_login_card_fails_closed_on_missing_local_credential(db_session) -> None:
    user = _user(db_session, email="missing.card@example.com", active=True)
    db_session.commit()

    detail = web_system_profiles.get_user_detail_data(db_session, user.id)

    assert detail is not None
    assert detail["credential"] is None
    assert detail["credential_issue"] == ("No local login credential is configured.")
    assert detail["credential_recovery"].allowed is True
