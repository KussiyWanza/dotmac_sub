import pytest

from app.models.auth import AuthProvider, UserCredential
from app.models.party import Party, PartyDataClassification, PartyType
from app.models.rbac import Role, SystemUserRole
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services.auth_flow import verify_password
from scripts.seed.seed_admin import seed_admin_user
from scripts.seed.seed_rbac import _ensure_system_user_role


def _add_admin_role(db_session) -> Role:
    role = Role(name="admin", description="Full system access", is_active=True)
    db_session.add(role)
    db_session.commit()
    return role


def test_seed_admin_user_creates_canonical_system_user(db_session):
    admin_role = _add_admin_role(db_session)

    message = seed_admin_user(
        db_session,
        email="admin@example.com",
        first_name="Admin",
        last_name="User",
        username="admin",
        password="AdminPass123!",
        force_reset=False,
    )

    system_user = (
        db_session.query(SystemUser)
        .filter(SystemUser.email == "admin@example.com")
        .one()
    )
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == system_user.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .one()
    )
    role_link = (
        db_session.query(SystemUserRole)
        .filter(SystemUserRole.system_user_id == system_user.id)
        .one()
    )

    assert message == "Admin user created."
    assert system_user.display_name == "Admin User"
    assert system_user.is_active is True
    assert system_user.person_party_id is not None
    person_party = db_session.get(Party, system_user.person_party_id)
    assert person_party is not None
    assert person_party.party_type == PartyType.person.value
    assert person_party.data_classification == PartyDataClassification.production.value
    assert system_user.party_binding_source == "admin_seeder"
    assert (
        system_user.party_binding_reason
        == "Operator-seeded local administrator identity"
    )
    assert credential.username == "admin"
    assert credential.subscriber_id is None
    assert verify_password("AdminPass123!", credential.password_hash) is True
    assert credential.must_change_password is False
    assert credential.password_updated_at is not None
    assert role_link.role_id == admin_role.id
    assert role_link.scope_type == ""
    assert role_link.scope_id == ""
    assert (
        db_session.query(Subscriber)
        .filter(Subscriber.email == "admin@example.com")
        .count()
        == 0
    )


def test_seed_admin_user_updates_existing_system_user_and_role(db_session):
    admin_role = _add_admin_role(db_session)
    system_user = SystemUser(
        first_name="Old",
        last_name="Name",
        email="admin@example.com",
        is_active=False,
    )
    db_session.add(system_user)
    db_session.flush()
    existing = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username="old-admin",
        password_hash="old-hash",
        must_change_password=False,
        is_active=False,
        failed_login_attempts=5,
    )
    db_session.add(existing)
    db_session.commit()

    message = seed_admin_user(
        db_session,
        email=system_user.email,
        first_name="New",
        last_name="Admin",
        username="admin",
        password="NewAdminPass123!",
        force_reset=True,
    )

    db_session.refresh(existing)
    db_session.refresh(system_user)

    assert message == "Admin user updated."
    assert system_user.first_name == "New"
    assert system_user.last_name == "Admin"
    assert system_user.display_name == "New Admin"
    assert system_user.is_active is True
    assert system_user.person_party_id is not None
    assert system_user.party_binding_source == "admin_seeder"
    assert existing.username == "admin"
    assert verify_password("NewAdminPass123!", existing.password_hash) is True
    assert existing.password_updated_at is not None
    assert existing.must_change_password is True
    assert existing.is_active is True
    assert existing.failed_login_attempts == 0
    assert existing.locked_until is None
    assert (
        db_session.query(SystemUserRole)
        .filter(
            SystemUserRole.system_user_id == system_user.id,
            SystemUserRole.role_id == admin_role.id,
        )
        .count()
        == 1
    )


def test_seed_admin_user_requires_seeded_admin_role(db_session):
    email = "missing-role-admin@example.com"
    with pytest.raises(RuntimeError, match="Active admin role not found"):
        seed_admin_user(
            db_session,
            email=email,
            first_name="Admin",
            last_name="User",
            username="missing-role-admin",
            password="AdminPass123!",
        )

    assert db_session.query(SystemUser).filter(SystemUser.email == email).count() == 0


def test_seed_admin_user_rejects_username_owned_by_subscriber(db_session, subscriber):
    email = "subscriber-takeover-admin@example.com"
    username = "subscriber-owned-admin"
    _add_admin_role(db_session)
    db_session.add(
        UserCredential(
            subscriber_id=subscriber.id,
            provider=AuthProvider.local,
            username=username,
            password_hash="existing-hash",
        )
    )
    db_session.commit()

    with pytest.raises(ValueError, match="assigned to another principal"):
        seed_admin_user(
            db_session,
            email=email,
            first_name="Admin",
            last_name="User",
            username=username,
            password="AdminPass123!",
        )

    assert db_session.query(SystemUser).filter(SystemUser.email == email).count() == 0


def test_seed_rbac_system_user_role_helper_is_idempotent(db_session):
    admin_role = _add_admin_role(db_session)
    system_user = SystemUser(
        first_name="Admin",
        last_name="User",
        email="admin@example.com",
        is_active=True,
    )
    db_session.add(system_user)
    db_session.commit()

    _ensure_system_user_role(db_session, system_user.id, admin_role.id)
    db_session.commit()
    _ensure_system_user_role(db_session, system_user.id, admin_role.id)
    db_session.commit()

    assert (
        db_session.query(SystemUserRole)
        .filter(
            SystemUserRole.system_user_id == system_user.id,
            SystemUserRole.role_id == admin_role.id,
        )
        .count()
        == 1
    )
