from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.auth import AuthenticationBinding, AuthProvider, UserCredential
from app.models.party import (
    Party,
    PartyDataClassification,
    PartyIdentityStatus,
    PartyType,
)
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.services.auth_flow import hash_password
from app.services.operator_tenant import OPERATOR_TENANT_ID


def project_staff_login(
    db: Session,
    *,
    user: SystemUser,
    credential: UserCredential,
) -> Party:
    """Install a complete reviewed Party projection for a staff login fixture."""

    if user.person_party_id is not None:
        person = db.get(Party, user.person_party_id)
        if person is None:
            raise AssertionError("bound staff fixture references a missing Party")
    else:
        person = Party(
            party_type=PartyType.person.value,
            display_name=user.display_name or user.email or "Test Staff",
            status=PartyIdentityStatus.active.value,
            data_classification=PartyDataClassification.test.value,
        )
        db.add(person)
    binding = AuthenticationBinding(
        binding_key=f"local.test.{uuid4().hex}",
        mechanism_code=AuthProvider.local.value,
        name="Test local password",
        is_active=True,
    )
    db.add(binding)
    db.flush()

    now = datetime.now(UTC)
    if user.person_party_id is None:
        user.person_party_id = person.id
        user.party_bound_at = now
        user.party_binding_source = "test-fixture"
        user.party_binding_reason = "Reviewed staff authentication fixture"
    credential.party_id = person.id
    credential.authentication_binding_id = binding.id
    credential.tenant_id = OPERATOR_TENANT_ID
    credential.party_bound_at = now
    credential.party_binding_source = "test-fixture"
    credential.party_binding_reason = "Reviewed staff authentication fixture"
    db.add(credential)
    db.flush()
    return person


def add_bound_staff_user(
    db,
    *,
    system_user_id: UUID | None = None,
    email: str | None = None,
    is_active: bool = True,
) -> tuple[SystemUser, Party]:
    """Create a reviewed staff principal → Person Party test binding."""

    person = Party(
        party_type=PartyType.person.value,
        display_name="Test Staff",
        status=PartyIdentityStatus.active.value,
        data_classification=PartyDataClassification.test.value,
    )
    db.add(person)
    db.flush()
    user = SystemUser(
        id=system_user_id or uuid4(),
        first_name="Test",
        last_name="Staff",
        display_name="Test Staff",
        email=email or f"staff-{uuid4().hex}@example.test",
        is_active=is_active,
        person_party_id=person.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="test-fixture",
        party_binding_reason="Reviewed service-team identity fixture",
    )
    db.add(user)
    db.flush()
    return user, person


def add_bound_staff_login(
    db,
    *,
    role_name: str,
    email: str,
    password: str,
) -> tuple[SystemUser, Party]:
    """Create a login-capable Party-bound staff principal for browser tests."""

    user, person = add_bound_staff_user(db, email=email)
    role = db.query(Role).filter(Role.name == role_name, Role.is_active.is_(True)).one()
    credential = UserCredential(
        system_user_id=user.id,
        provider=AuthProvider.local,
        username=email,
        password_hash=hash_password(password),
        password_updated_at=datetime.now(UTC),
        must_change_password=False,
        is_active=True,
    )
    project_staff_login(db, user=user, credential=credential)
    db.add(
        SystemUserRole(
            system_user_id=user.id,
            role_id=role.id,
            scope_type="",
            scope_id="",
            source="test-fixture",
        )
    )
    db.flush()
    return user, person
