"""Staff-account provisioning API (ERP staff sync) — service-level behavior.

Exercises the route handlers directly against the test DB (the router's
permission dependencies are covered by the shared require_permission tests).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.staff_sync import (
    ErpDepartmentReference,
    StaffAccountCreate,
    StaffAccountErpDepartmentUpdate,
    StaffAccountRolesUpdate,
    create_staff_account,
    deactivate_staff_account,
    get_staff_account,
    sync_staff_erp_department,
    update_staff_account_roles,
)
from app.models.auth import AuthProvider
from app.models.rbac import Role, SystemUserRole
from app.models.service_team import ServiceTeamExternalReference, ServiceTeamMember
from app.models.system_user import SystemUser
from app.services import credential_party_binding, service_team_lifecycle
from app.services.operator_tenant import provision_operator_tenant
from app.services.owner_commands import CommandContext

_AUTH = {
    "principal_id": "erp-hr-test-key",
    "principal_type": "api_key",
    "scopes": [
        "rbac:assign",
        "rbac:roles:read",
        "operations:service_team:membership",
    ],
}


@pytest.fixture(autouse=True)
def local_authentication_binding(db_session) -> None:
    provision_operator_tenant(db_session)
    if db_session.in_transaction():
        db_session.rollback()
    credential_party_binding.install_authentication_binding(
        db_session,
        credential_party_binding.AuthenticationBindingInstallation(
            context=CommandContext.system(
                actor="operator:test",
                scope=credential_party_binding.AUTHENTICATION_BINDING_INSTALL_SCOPE,
                reason="staff sync tests require local authentication",
                idempotency_key="authentication-binding:local.staff-sync-test",
            ),
            binding_key="local.staff-sync-test",
            mechanism_code=AuthProvider.local.value,
            name="Staff sync local authentication",
            description="Test verifier for ERP staff sync local credentials",
        ),
    )


@pytest.fixture()
def staff_role(db_session):
    role = db_session.query(Role).filter(Role.name == "staff").first()
    if not role:
        role = Role(name="staff", description="Baseline staff role")
        db_session.add(role)
        db_session.commit()
    return role


@pytest.fixture()
def field_role(db_session):
    role = db_session.query(Role).filter(Role.name == "field_technician").first()
    if not role:
        role = Role(name="field_technician", description="Field technician")
        db_session.add(role)
        db_session.commit()
    return role


def _payload(**overrides):
    base = dict(
        email="new.hire@dotmac.io",
        first_name="New",
        last_name="Hire",
        role="staff",
        send_invite=False,  # no SMTP in tests
    )
    base.update(overrides)
    return StaffAccountCreate(**base)


def test_create_is_idempotent_on_email(db_session, staff_role):
    first = create_staff_account(_payload(), auth=_AUTH, db=db_session)
    assert first.created is True
    assert first.is_active is True

    again = create_staff_account(_payload(), auth=_AUTH, db=db_session)
    assert again.created is False
    assert again.id == first.id

    user = db_session.get(SystemUser, first.id)
    assert user.email == "new.hire@dotmac.io"
    assert user.display_name == "New Hire"
    assert first.roles == ["staff"]


def test_create_unknown_role_is_422(db_session):
    with pytest.raises(HTTPException) as exc:
        create_staff_account(_payload(role="does-not-exist"), auth=_AUTH, db=db_session)
    assert exc.value.status_code == 422


def test_deactivate_disables_account(db_session, staff_role):
    created = create_staff_account(
        _payload(email="leaver@dotmac.io"), auth=_AUTH, db=db_session
    )
    result = deactivate_staff_account(str(created.id), auth=_AUTH, db=db_session)
    assert result.is_active is False
    assert db_session.get(SystemUser, created.id).is_active is False


def test_get_by_email_and_404(db_session, staff_role):
    created = create_staff_account(
        _payload(email="lookup@dotmac.io"), auth=_AUTH, db=db_session
    )
    found = get_staff_account(email="lookup@dotmac.io", _auth=_AUTH, db=db_session)
    assert found.id == created.id

    with pytest.raises(HTTPException) as exc:
        get_staff_account(email="ghost@dotmac.io", _auth=_AUTH, db=db_session)
    assert exc.value.status_code == 404


def test_role_sync_replaces_only_erp_managed_roles(db_session, staff_role, field_role):
    created = create_staff_account(_payload(), auth=_AUTH, db=db_session)
    local_role = Role(name="incident_commander", description="Local emergency grant")
    db_session.add(local_role)
    db_session.flush()
    db_session.add(
        SystemUserRole(
            system_user_id=created.id,
            role_id=local_role.id,
            source="local",
        )
    )
    db_session.commit()

    result = update_staff_account_roles(
        str(created.id),
        StaffAccountRolesUpdate(roles=["field_technician"]),
        auth=_AUTH,
        db=db_session,
    )

    assert result.roles == ["field_technician", "incident_commander"]
    grants = (
        db_session.query(SystemUserRole, Role.name)
        .join(Role, Role.id == SystemUserRole.role_id)
        .filter(SystemUserRole.system_user_id == created.id)
        .all()
    )
    assert {(name, grant.source) for grant, name in grants} == {
        ("field_technician", "erp_hr"),
        ("incident_commander", "local"),
    }


def test_role_sync_rejects_unknown_role_without_partial_update(db_session, staff_role):
    created = create_staff_account(_payload(), auth=_AUTH, db=db_session)

    with pytest.raises(HTTPException) as exc:
        update_staff_account_roles(
            str(created.id),
            StaffAccountRolesUpdate(roles=["missing_role"]),
            auth=_AUTH,
            db=db_session,
        )

    assert exc.value.status_code == 422
    assert get_staff_account(email=created.email, _auth=_AUTH, db=db_session).roles == [
        "staff"
    ]


def test_erp_department_endpoint_assigns_service_team_membership(
    db_session,
    staff_role,
):
    created = create_staff_account(
        _payload(email="dept.endpoint@dotmac.io"),
        auth=_AUTH,
        db=db_session,
    )
    command_id = uuid4()
    team_id = service_team_lifecycle.create_team(
        db_session,
        service_team_lifecycle.CreateServiceTeam(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="user:00000000-0000-0000-0000-000000000001",
                scope="operations:service_team:create",
                reason="test create",
                idempotency_key="api-sync-team-create",
            ),
            team_id=uuid4(),
            name="ERP Endpoint Support",
        ),
    ).team_id
    db_session.add(
        ServiceTeamExternalReference(
            team_id=team_id,
            provider=service_team_lifecycle.ERP_DEPARTMENT_PROVIDER,
            account_scope="erp-org-api",
            external_id="dept-api",
            provenance="reviewed ERP mapping",
            observed_at=datetime.now(UTC),
            is_active=True,
        )
    )
    db_session.commit()

    result = sync_staff_erp_department(
        str(created.id),
        StaffAccountErpDepartmentUpdate(
            erp_employee_id="employee-api",
            employee_code="EMP-API",
            account_scope="erp-org-api",
            department=ErpDepartmentReference(
                department_id="dept-api",
                department_code="SUPPORT",
                department_name="Support",
            ),
        ),
        auth=_AUTH,
        db=db_session,
    )

    assert result.service_team_id == team_id
    assert result.changed is True
    assert result.replayed is False
    assert (
        db_session.query(ServiceTeamMember)
        .filter(ServiceTeamMember.team_id == team_id)
        .filter(ServiceTeamMember.is_active.is_(True))
        .count()
        == 1
    )
