from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.event_store import EventStore
from app.models.party import Party, PartyExternalReference, PartyType
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberRole,
)
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.services.owner_commands import CommandContext
from app.services.service_team_party_cutover import (
    AdoptServiceTeamPartyCutover,
    IdentityDecisionKind,
    PlannedServiceTeamMembership,
    PlannedStaffIdentity,
    ServiceTeamPartyCutoverApproval,
    ServiceTeamPartyCutoverError,
    ServiceTeamPartyCutoverPlan,
    adopt_service_team_party_cutover,
    audit_service_team_party_cutover,
)


def _context(plan_digest: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext.system(
        actor="service:service-team-cutover-test",
        scope="service_team_party_cutover:adopt",
        reason="reviewed service-team identity adoption",
        command_id=command_id,
        correlation_id=command_id,
        idempotency_key=plan_digest,
    )


def _legacy_team_and_user(
    db_session,
    *,
    legacy_person_id: UUID,
    active_user: bool = True,
    manager_reference: bool = False,
) -> tuple[UUID, UUID]:
    user = SystemUser(
        first_name="Ada",
        last_name="Operator",
        display_name="Ada Operator",
        email=f"{uuid4()}@example.com",
        is_active=active_user,
    )
    manager_party = (
        Party(
            id=legacy_person_id,
            party_type=PartyType.person.value,
            display_name="Ada Operator",
        )
        if manager_reference
        else None
    )
    team = ServiceTeam(
        name=f"Legacy Team {uuid4()}",
        team_type="support",
        manager_person_id=legacy_person_id if manager_reference else None,
        is_active=True,
    )
    db_session.add_all(
        tuple(row for row in (manager_party, user, team) if row is not None)
    )
    db_session.flush()
    user_id = user.id
    team_id = team.id
    db_session.commit()
    return user_id, team_id


def _plan(
    *,
    legacy_person_id: UUID,
    system_user_id: UUID,
    team_id: UUID,
    membership_id: UUID | None = None,
    membership_active: bool = True,
) -> ServiceTeamPartyCutoverPlan:
    now = datetime.now(UTC)
    return ServiceTeamPartyCutoverPlan(
        source_snapshot_sha256="a" * 64,
        decision_file_sha256="b" * 64,
        planned_at=now,
        identities=(
            PlannedStaffIdentity(
                legacy_person_id=legacy_person_id,
                display_name="Ada Operator",
                decision=IdentityDecisionKind.bind,
                decision_id=uuid4(),
                reason_sha256="c" * 64,
                system_user_id=system_user_id,
            ),
        ),
        memberships=(
            PlannedServiceTeamMembership(
                membership_id=membership_id or uuid4(),
                team_id=team_id,
                legacy_person_id=legacy_person_id,
                role=ServiceTeamMemberRole.lead,
                is_active=membership_active,
                created_at=now - timedelta(days=30),
            ),
        ),
    )


def _command(
    plan: ServiceTeamPartyCutoverPlan,
    *,
    context: CommandContext | None = None,
    approved_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> AdoptServiceTeamPartyCutover:
    now = datetime.now(UTC)
    return AdoptServiceTeamPartyCutover(
        context=context or _context(plan.plan_digest),
        plan=plan,
        approval=ServiceTeamPartyCutoverApproval(
            plan_digest=plan.plan_digest,
            plan_file_sha256="d" * 64,
            decision_file_sha256=plan.decision_file_sha256,
            approved_by="identity-reviewer",
            approved_at=approved_at or now - timedelta(minutes=5),
            expires_at=expires_at or now + timedelta(hours=1),
            reason="approve exact service-team identity snapshot",
            maximum_identities=len(plan.identities),
            maximum_memberships=len(plan.memberships),
        ),
        plan_file_sha256="d" * 64,
        approval_file_sha256="e" * 64,
    )


def test_audit_reports_legacy_manager_blocker_without_identity_values(
    db_session,
) -> None:
    legacy_person_id = uuid4()
    _user_id, _team_id = _legacy_team_and_user(
        db_session,
        legacy_person_id=legacy_person_id,
        manager_reference=True,
    )

    audit = audit_service_team_party_cutover(db_session)
    summary = audit.summary()

    assert audit.ready is False
    assert audit.manager_reference_count == 1
    assert audit.manager_blocked_count == 1
    assert audit.blocker_count == 1
    assert str(legacy_person_id) not in str(summary)


def test_audit_blocks_every_malformed_or_conflicting_legacy_setting(
    db_session,
) -> None:
    legacy_person_id = uuid4()
    user_id, team_id = _legacy_team_and_user(
        db_session,
        legacy_person_id=legacy_person_id,
    )
    missing_team_id = uuid4()
    db_session.add_all(
        (
            DomainSetting(
                domain=SettingDomain.workflow,
                key="support_service_teams",
                value_type=SettingValueType.json,
                value_json=[
                    {"id": str(team_id), "label": "Conflicting Team Name"},
                    {"label": "Missing Identifier"},
                ],
                is_active=True,
            ),
            DomainSetting(
                domain=SettingDomain.workflow,
                key="support_service_team_members",
                value_type=SettingValueType.json,
                value_json={
                    str(missing_team_id): [str(user_id), "not-a-uuid"],
                },
                is_active=True,
            ),
        )
    )
    db_session.commit()

    audit = audit_service_team_party_cutover(db_session)

    assert audit.ready is False
    assert audit.workflow_setting_team_count == 1
    assert audit.workflow_setting_team_blocked_count == 1
    assert audit.workflow_setting_member_count == 1
    assert audit.workflow_setting_member_blocked_count == 1
    assert audit.workflow_setting_member_team_blocked_count == 1
    assert audit.workflow_setting_malformed_entry_count == 2


def test_approved_adoption_is_atomic_and_exact_replay_is_read_only(
    db_session,
) -> None:
    legacy_person_id = uuid4()
    user_id, team_id = _legacy_team_and_user(
        db_session,
        legacy_person_id=legacy_person_id,
    )
    plan = _plan(
        legacy_person_id=legacy_person_id,
        system_user_id=user_id,
        team_id=team_id,
    )
    command = _command(plan)

    outcome = adopt_service_team_party_cutover(db_session, command)

    assert outcome.replayed is False
    assert outcome.parties_created == 1
    assert outcome.principals_bound == 1
    assert outcome.memberships_created == 1
    assert not db_session.in_transaction()
    party = db_session.get(Party, legacy_person_id)
    user = db_session.get(SystemUser, user_id)
    member = db_session.get(ServiceTeamMember, plan.memberships[0].membership_id)
    assert party is not None
    assert party.party_type == PartyType.person.value
    assert user is not None
    assert user.person_party_id == legacy_person_id
    assert member is not None
    assert member.person_id == legacy_person_id
    assert (
        db_session.query(PartyExternalReference)
        .filter(
            PartyExternalReference.party_id == legacy_person_id,
            PartyExternalReference.source_system == "dotmac_crm",
            PartyExternalReference.entity_type == "person",
        )
        .count()
        == 1
    )
    receipt = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "service_team.party_cutover_adopted")
        .one()
    )
    assert receipt.entity_id == plan.plan_digest
    assert receipt.metadata_["identity_count"] == 1
    assert "display_name" not in receipt.metadata_
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "service_team.party_cutover_adopted")
        .count()
        == 1
    )
    db_session.commit()

    replay = adopt_service_team_party_cutover(
        db_session,
        AdoptServiceTeamPartyCutover(
            context=_context(plan.plan_digest),
            plan=command.plan,
            approval=command.approval,
            plan_file_sha256=command.plan_file_sha256,
            approval_file_sha256=command.approval_file_sha256,
        ),
    )

    assert replay.replayed is True
    assert replay.parties_created == 0
    assert replay.principals_bound == 0
    assert replay.memberships_created == 0
    assert db_session.query(Party).count() == 1
    assert db_session.query(ServiceTeamMember).count() == 1
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "service_team.party_cutover_adopted")
        .count()
        == 1
    )
    assert audit_service_team_party_cutover(db_session).ready is True


def test_membership_conflict_rolls_back_party_binding_and_receipt(db_session) -> None:
    legacy_person_id = uuid4()
    user_id, team_id = _legacy_team_and_user(
        db_session,
        legacy_person_id=legacy_person_id,
    )
    membership_id = uuid4()
    other_person = Party(
        party_type=PartyType.person.value,
        display_name="Different Person",
    )
    db_session.add(other_person)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(
            id=membership_id,
            team_id=team_id,
            person_id=other_person.id,
            role=ServiceTeamMemberRole.member.value,
            is_active=False,
        )
    )
    db_session.commit()
    plan = _plan(
        legacy_person_id=legacy_person_id,
        system_user_id=user_id,
        team_id=team_id,
        membership_id=membership_id,
    )

    with pytest.raises(ServiceTeamPartyCutoverError) as captured:
        adopt_service_team_party_cutover(db_session, _command(plan))

    assert captured.value.code.endswith(".membership_conflict")
    assert not db_session.in_transaction()
    assert db_session.get(Party, legacy_person_id) is None
    assert db_session.get(SystemUser, user_id).person_party_id is None
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "service_team.party_cutover_adopted")
        .count()
        == 0
    )


def test_expired_approval_refuses_before_any_write(db_session) -> None:
    legacy_person_id = uuid4()
    user_id, team_id = _legacy_team_and_user(
        db_session,
        legacy_person_id=legacy_person_id,
    )
    plan = _plan(
        legacy_person_id=legacy_person_id,
        system_user_id=user_id,
        team_id=team_id,
    )
    now = datetime.now(UTC)

    with pytest.raises(ServiceTeamPartyCutoverError) as captured:
        adopt_service_team_party_cutover(
            db_session,
            _command(
                plan,
                approved_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1),
            ),
            executed_at=now,
        )

    assert captured.value.code.endswith(".approval_invalid")
    assert db_session.get(Party, legacy_person_id) is None
    assert db_session.get(SystemUser, user_id).person_party_id is None


def test_active_membership_cannot_use_identity_only_decision(db_session) -> None:
    legacy_person_id = uuid4()
    _user_id, team_id = _legacy_team_and_user(
        db_session,
        legacy_person_id=legacy_person_id,
    )
    now = datetime.now(UTC)
    plan = ServiceTeamPartyCutoverPlan(
        source_snapshot_sha256="a" * 64,
        decision_file_sha256="b" * 64,
        planned_at=now,
        identities=(
            PlannedStaffIdentity(
                legacy_person_id=legacy_person_id,
                display_name="Historical Person",
                decision=IdentityDecisionKind.identity_only,
                decision_id=uuid4(),
                reason_sha256="c" * 64,
            ),
        ),
        memberships=(
            PlannedServiceTeamMembership(
                membership_id=uuid4(),
                team_id=team_id,
                legacy_person_id=legacy_person_id,
                role=ServiceTeamMemberRole.member,
                is_active=True,
                created_at=now,
            ),
        ),
    )

    with pytest.raises(ServiceTeamPartyCutoverError) as captured:
        adopt_service_team_party_cutover(db_session, _command(plan))

    assert captured.value.code.endswith(".invalid_plan")
