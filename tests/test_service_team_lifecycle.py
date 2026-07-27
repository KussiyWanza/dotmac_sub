from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.party import (
    Party,
    PartyDataClassification,
    PartyIdentityStatus,
    PartyType,
)
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberRole,
    ServiceTeamType,
)
from app.models.system_user import SystemUser
from app.services import service_team_lifecycle
from app.services.owner_commands import CommandContext


def _context(operation: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{uuid4()}",
        scope=f"operations.service_team_lifecycle:{operation}",
        reason=f"test {operation}",
        idempotency_key=f"{operation}:{command_id}",
    )


def _staff(
    db_session,
    *,
    email: str,
    is_active: bool = True,
    party_status: PartyIdentityStatus = PartyIdentityStatus.active,
) -> tuple[UUID, UUID]:
    person = Party(
        party_type=PartyType.person.value,
        display_name="Ada Operator",
        status=party_status.value,
        data_classification=PartyDataClassification.test.value,
    )
    db_session.add(person)
    db_session.flush()
    person_id = person.id
    user = SystemUser(
        first_name="Ada",
        last_name="Operator",
        display_name="Ada Operator",
        email=email,
        is_active=is_active,
        person_party_id=person_id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="service-team-test",
        party_binding_reason="Reviewed fixture identity",
    )
    db_session.add(user)
    db_session.flush()
    user_id = user.id
    db_session.commit()
    return user_id, person_id


def _create_team(
    db_session,
    *,
    name: str = "Field Operations",
    manager_system_user_id: UUID | None = None,
) -> UUID:
    team_id = uuid4()
    outcome = service_team_lifecycle.create_team(
        db_session,
        service_team_lifecycle.CreateServiceTeam(
            context=_context("create"),
            team_id=team_id,
            name=name,
            team_type=ServiceTeamType.field_service,
            region="Abuja",
            manager_system_user_id=manager_system_user_id,
        ),
    )
    assert outcome.replayed is False
    return team_id


def test_create_is_idempotent_and_stages_audit_and_event(db_session):
    manager_id, manager_party_id = _staff(db_session, email="manager@example.com")
    team_id = _create_team(db_session, manager_system_user_id=manager_id)

    replay = service_team_lifecycle.create_team(
        db_session,
        service_team_lifecycle.CreateServiceTeam(
            context=_context("create-replay"),
            team_id=team_id,
            name="Field Operations",
            team_type=ServiceTeamType.field_service,
            region="Abuja",
            manager_system_user_id=manager_id,
        ),
    )

    assert replay.replayed is True
    assert db_session.get(ServiceTeam, team_id).manager_person_id == manager_party_id
    assert db_session.query(ServiceTeam).count() == 1
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "service_team.created")
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "service_team.changed")
        .count()
        == 1
    )


def test_create_rejects_identity_collision_and_casefolded_duplicate(db_session):
    team_id = _create_team(db_session)

    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="identifier is already bound",
    ) as collision:
        service_team_lifecycle.create_team(
            db_session,
            service_team_lifecycle.CreateServiceTeam(
                context=_context("collision"),
                team_id=team_id,
                name="Different Team",
                team_type=ServiceTeamType.support,
            ),
        )
    assert collision.value.code == "service_team_identity_collision"

    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="already uses this name",
    ) as duplicate:
        service_team_lifecycle.create_team(
            db_session,
            service_team_lifecycle.CreateServiceTeam(
                context=_context("duplicate"),
                team_id=uuid4(),
                name="field operations",
                team_type=ServiceTeamType.operations,
            ),
        )
    assert duplicate.value.code == "service_team_name_conflict"


def test_create_does_not_replay_a_deactivated_team_as_active(db_session):
    team_id = _create_team(db_session)
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    expected = team.updated_at
    db_session.commit()
    service_team_lifecycle.set_team_active(
        db_session,
        service_team_lifecycle.SetServiceTeamActive(
            context=_context("deactivate-before-create-replay"),
            team_id=team_id,
            expected_updated_at=expected,
            is_active=False,
            reason="Retire the original team.",
        ),
    )

    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="identifier is already bound",
    ) as collision:
        service_team_lifecycle.create_team(
            db_session,
            service_team_lifecycle.CreateServiceTeam(
                context=_context("create-after-deactivation"),
                team_id=team_id,
                name="Field Operations",
                team_type=ServiceTeamType.field_service,
                region="Abuja",
            ),
        )

    assert collision.value.code == "service_team_identity_collision"


def test_update_replays_exact_state_and_rejects_stale_change(db_session):
    team_id = _create_team(db_session)
    original = db_session.get(ServiceTeam, team_id)
    assert original is not None
    expected = original.updated_at
    db_session.commit()

    replay = service_team_lifecycle.update_team(
        db_session,
        service_team_lifecycle.UpdateServiceTeam(
            context=_context("update-replay"),
            team_id=team_id,
            expected_updated_at=expected - timedelta(days=1),
            name="Field Operations",
            team_type=ServiceTeamType.field_service,
            region="Abuja",
        ),
    )
    assert replay.replayed is True

    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="changed after this form",
    ) as stale:
        service_team_lifecycle.update_team(
            db_session,
            service_team_lifecycle.UpdateServiceTeam(
                context=_context("stale-update"),
                team_id=team_id,
                expected_updated_at=expected - timedelta(days=1),
                name="Field Delivery",
                team_type=ServiceTeamType.field_service,
                region="Lagos",
            ),
        )
    assert stale.value.code == "service_team_stale"


def test_membership_and_team_deactivation_are_fail_closed(db_session):
    staff_id, staff_party_id = _staff(db_session, email="field@example.com")
    team_id = _create_team(db_session)

    added = service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("add-member"),
            team_id=team_id,
            system_user_id=staff_id,
            role=ServiceTeamMemberRole.lead,
        ),
    )
    assert added.member_id is not None
    stored_member = db_session.get(ServiceTeamMember, added.member_id)
    assert stored_member.person_id == staff_party_id
    assert stored_member.role == ServiceTeamMemberRole.lead.value

    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    expected = team.updated_at
    db_session.commit()
    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="active member",
    ) as active_members:
        service_team_lifecycle.set_team_active(
            db_session,
            service_team_lifecycle.SetServiceTeamActive(
                context=_context("deactivate-blocked"),
                team_id=team_id,
                expected_updated_at=expected,
                is_active=False,
                reason="Team was replaced.",
            ),
        )
    assert active_members.value.code == "service_team_has_active_members"

    removed = service_team_lifecycle.remove_member(
        db_session,
        service_team_lifecycle.RemoveServiceTeamMember(
            context=_context("remove-member"),
            team_id=team_id,
            member_id=added.member_id,
            reason="Staff moved to another team.",
        ),
    )
    assert removed.replayed is False
    member = db_session.get(ServiceTeamMember, added.member_id)
    assert member is not None
    assert member.is_active is False
    db_session.commit()

    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    expected = team.updated_at
    db_session.commit()
    deactivated = service_team_lifecycle.set_team_active(
        db_session,
        service_team_lifecycle.SetServiceTeamActive(
            context=_context("deactivate"),
            team_id=team_id,
            expected_updated_at=expected,
            is_active=False,
            reason="Team was replaced.",
        ),
    )
    assert deactivated.replayed is False
    assert db_session.get(ServiceTeam, team_id).is_active is False


def test_native_projections_resolve_staff_and_members(db_session):
    manager_id, manager_party_id = _staff(db_session, email="projection@example.com")
    team_id = _create_team(db_session, manager_system_user_id=manager_id)
    service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("projection-member"),
            team_id=team_id,
            system_user_id=manager_id,
            role=ServiceTeamMemberRole.manager,
        ),
    )

    result = service_team_lifecycle.list_teams(
        db_session,
        search="field",
        is_active=True,
    )
    detail = service_team_lifecycle.get_team(db_session, team_id)

    assert result.total == 1
    assert result.active_count == 1
    assert result.inactive_count == 0
    assert result.items[0].manager_label == "Ada Operator"
    assert result.items[0].manager_person_id == manager_party_id
    assert result.items[0].manager_system_user_id == manager_id
    assert result.items[0].manager_identity_active is True
    assert result.items[0].active_member_count == 1
    assert detail.team.team_id == team_id
    assert detail.members[0].person_email == "projection@example.com"
    assert detail.members[0].role is ServiceTeamMemberRole.manager
    assert detail.members[0].staff_identity_active is True
    assert detail.available_staff == ()
    assert detail.actions.can_add_member is True
    assert detail.actions.can_deactivate is False


def test_inactive_staff_cannot_be_assigned(db_session):
    inactive_id, _inactive_party_id = _staff(
        db_session,
        email="inactive@example.com",
        is_active=False,
    )
    team_id = _create_team(db_session)

    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="not active",
    ) as rejected:
        service_team_lifecycle.add_member(
            db_session,
            service_team_lifecycle.AddServiceTeamMember(
                context=_context("inactive-member"),
                team_id=team_id,
                system_user_id=inactive_id,
                role=ServiceTeamMemberRole.member,
            ),
        )
    assert rejected.value.code == "service_team_staff_not_found"


def test_inactive_party_is_not_selectable_and_member_role_change_fails_closed(
    db_session,
):
    staff_id, staff_party_id = _staff(
        db_session,
        email="identity-state@example.com",
    )
    team_id = _create_team(db_session)
    added = service_team_lifecycle.add_member(
        db_session,
        service_team_lifecycle.AddServiceTeamMember(
            context=_context("add-before-party-retirement"),
            team_id=team_id,
            system_user_id=staff_id,
            role=ServiceTeamMemberRole.member,
        ),
    )
    party = db_session.get(Party, staff_party_id)
    assert party is not None
    party.status = PartyIdentityStatus.archived.value
    db_session.commit()

    assert all(
        option.system_user_id != staff_id
        for option in service_team_lifecycle.list_staff_options(db_session)
    )
    db_session.commit()
    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="Person Party binding is not active",
    ) as rejected:
        service_team_lifecycle.update_member(
            db_session,
            service_team_lifecycle.UpdateServiceTeamMember(
                context=_context("role-after-party-retirement"),
                team_id=team_id,
                member_id=added.member_id,
                role=ServiceTeamMemberRole.lead,
            ),
        )

    assert rejected.value.code == "service_team_staff_identity_invalid"


def test_reactivation_eligibility_and_command_recheck_manager_identity(db_session):
    manager_id, manager_party_id = _staff(
        db_session,
        email="retired-manager@example.com",
    )
    team_id = _create_team(db_session, manager_system_user_id=manager_id)
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    expected = team.updated_at
    db_session.commit()
    service_team_lifecycle.set_team_active(
        db_session,
        service_team_lifecycle.SetServiceTeamActive(
            context=_context("deactivate-before-manager-retirement"),
            team_id=team_id,
            expected_updated_at=expected,
            is_active=False,
            reason="Pause this team.",
        ),
    )
    party = db_session.get(Party, manager_party_id)
    assert party is not None
    party.status = PartyIdentityStatus.archived.value
    db_session.commit()

    detail = service_team_lifecycle.get_team(db_session, team_id)
    assert detail.team.manager_identity_active is False
    assert detail.actions.can_activate is False
    assert "active, Party-bound manager" in str(detail.actions.lifecycle_block_reason)
    db_session.commit()

    with pytest.raises(
        service_team_lifecycle.ServiceTeamLifecycleError,
        match="Person Party binding is not active",
    ) as rejected:
        service_team_lifecycle.set_team_active(
            db_session,
            service_team_lifecycle.SetServiceTeamActive(
                context=_context("reactivate-after-manager-retirement"),
                team_id=team_id,
                expected_updated_at=detail.team.updated_at,
                is_active=True,
                reason="Resume this team.",
            ),
        )

    assert rejected.value.code == "service_team_staff_identity_invalid"


def test_timezone_compatible_timestamp_comparison(db_session):
    team_id = _create_team(db_session)
    team = db_session.get(ServiceTeam, team_id)
    assert team is not None
    expected = team.updated_at
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=UTC)
    db_session.commit()

    with pytest.raises(service_team_lifecycle.ServiceTeamLifecycleError) as stale:
        service_team_lifecycle.update_team(
            db_session,
            service_team_lifecycle.UpdateServiceTeam(
                context=_context("timestamp"),
                team_id=team_id,
                expected_updated_at=datetime.now(UTC),
                name="Updated",
                team_type=ServiceTeamType.support,
            ),
        )
    assert stale.value.code == "service_team_stale"


def test_staff_team_resolution_translates_principal_to_party_and_fails_ambiguous(
    db_session,
):
    staff_id, staff_party_id = _staff(
        db_session,
        email="team-resolution@example.com",
    )
    first_team_id = _create_team(db_session, name="Resolution One")
    first_member = ServiceTeamMember(
        team_id=first_team_id,
        person_id=staff_party_id,
        role=ServiceTeamMemberRole.member.value,
        is_active=True,
    )
    db_session.add(first_member)
    db_session.commit()

    resolved = service_team_lifecycle.resolve_staff_service_team(
        db_session,
        staff_id,
    )

    assert resolved.kind is service_team_lifecycle.ServiceTeamResolutionKind.resolved
    assert resolved.person_party_id == staff_party_id
    assert resolved.team_id == first_team_id
    assert resolved.candidate_team_ids == (first_team_id,)
    db_session.commit()

    second_team_id = _create_team(db_session, name="Resolution Two")
    db_session.add(
        ServiceTeamMember(
            team_id=second_team_id,
            person_id=staff_party_id,
            role=ServiceTeamMemberRole.member.value,
            is_active=True,
        )
    )
    db_session.commit()

    ambiguous = service_team_lifecycle.resolve_staff_service_team(
        db_session,
        staff_id,
    )

    assert ambiguous.kind is service_team_lifecycle.ServiceTeamResolutionKind.ambiguous
    assert ambiguous.team_id is None
    assert set(ambiguous.candidate_team_ids) == {first_team_id, second_team_id}
