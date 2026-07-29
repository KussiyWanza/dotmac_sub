from pathlib import Path
from uuid import uuid4

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.party import (
    Party,
    PartyDataClassification,
    PartyIdentityStatus,
    PartyType,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.subscription_engine import SettingValueType
from app.services import service_team_source_retirement
from app.services.owner_commands import CommandContext


def test_source_retirement_gate_is_narrow_and_has_five_native_pointers():
    assert service_team_source_retirement.TEAM_POINTERS == (
        ("support_tickets", "service_team_id"),
        ("projects", "service_team_id"),
        ("dispatch_rules", "service_team_id"),
        ("team_inbox_email_routes", "service_team_id"),
        ("inbox_conversation_teams", "service_team_id"),
    )
    source = Path("app/services/service_team_source_retirement.py").read_text(
        encoding="utf-8"
    )
    assert "delete(ServiceTeamMember)" in source
    assert "db.delete(member)" not in source


def test_deploy_uses_source_retirement_check_not_crm_adoption():
    source = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    gate = "python -m scripts.migration.retire_legacy_service_team_sources --check"
    migration = 'log "Applying migrations (alembic upgrade heads)"'

    assert gate in source
    assert source.index(gate) < source.index(migration)
    assert "audit_service_team_party_cutover" not in source


def test_deploy_gate_short_circuits_once_cutover_is_complete():
    # The gate protects migration 426's one-time cutover. Post-cutover identity
    # drift must never block deploys behind a destructive retirement tool.
    source = Path("scripts/migration/retire_legacy_service_team_sources.py").read_text(
        encoding="utf-8"
    )
    check_body = source[source.index("if args.check") : source.index("command_id =")]
    assert 'has_table("service_team_capability_definitions")' in check_body
    assert check_body.index("has_table") < check_body.index(
        "audit_legacy_service_team_sources"
    )


def test_historical_crm_adoption_surface_is_absent():
    assert not Path("app/services/service_team_party_cutover.py").exists()
    assert not Path("scripts/migration/plan_service_team_party_cutover.py").exists()
    assert not Path("scripts/migration/execute_service_team_party_cutover.py").exists()


def test_retirement_clears_migration_426_blockers_without_creating_identity(
    db_session,
):
    person = Party(
        party_type=PartyType.person.value,
        display_name="Unbound compatibility identity",
        status=PartyIdentityStatus.active.value,
        data_classification=PartyDataClassification.test.value,
    )
    db_session.add(person)
    db_session.flush()
    team = ServiceTeam(
        name=f"Retirement {uuid4()}",
        manager_person_id=person.id,
        is_active=True,
    )
    db_session.add(team)
    db_session.flush()
    member = ServiceTeamMember(
        team_id=team.id,
        person_id=person.id,
        role=None,
        is_active=True,
    )
    setting = DomainSetting(
        domain=SettingDomain.workflow,
        key="support_service_teams",
        value_type=SettingValueType.json,
        value_json=[{"id": str(team.id), "label": team.name}],
        is_active=True,
    )
    db_session.add_all([member, setting])
    db_session.commit()
    member_id = member.id
    team_id = team.id
    setting_id = setting.id

    before = service_team_source_retirement.audit_legacy_service_team_sources(
        db_session
    )
    assert before.manager_pointer_count == 1
    assert before.migration_blocking_membership_count == 1
    assert before.active_legacy_source_count == 1
    db_session.commit()

    command_id = uuid4()
    outcome = service_team_source_retirement.retire_legacy_service_team_sources(
        db_session,
        service_team_source_retirement.RetireLegacyServiceTeamSources(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{uuid4()}",
                scope="operations:service_team:retire",
                reason="test reviewed source retirement",
                idempotency_key=f"source-retirement:{command_id}",
            )
        ),
    )

    assert outcome.cleared_manager_pointer_count == 1
    assert outcome.removed_migration_blocking_membership_count == 1
    assert outcome.retired_source_count == 1
    assert db_session.get(ServiceTeamMember, member_id) is None
    assert db_session.get(ServiceTeam, team_id).manager_person_id is None
    assert db_session.get(DomainSetting, setting_id).is_active is False


def test_duplicate_resolution_restores_native_row_for_active_compat_member(
    db_session,
):
    """Deleting a person's only active row must not end their membership."""

    from datetime import UTC, datetime

    from app.models.system_user import SystemUser

    person = Party(
        party_type=PartyType.person.value,
        display_name="Duplicated operator",
        status=PartyIdentityStatus.active.value,
        data_classification=PartyDataClassification.test.value,
    )
    db_session.add(person)
    db_session.flush()
    # Production pre-426 rows stored SystemUser ids in person_id with no FK.
    # Fresh test schemas enforce the parties FK, so the compat identity is a
    # SystemUser sharing its UUID with a non-person Party: the FK is satisfied
    # while resolution still takes the compatibility-user branch.
    compat_identity_id = uuid4()
    user = SystemUser(
        id=compat_identity_id,
        first_name="Duplicated",
        last_name="Operator",
        display_name="Duplicated Operator",
        email=f"{uuid4()}@example.com",
        is_active=True,
        person_party_id=person.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="retirement-test",
        party_binding_reason="Reviewed fixture identity",
    )
    shadow_org = Party(
        id=compat_identity_id,
        party_type=PartyType.organization.value,
        display_name="Legacy compatibility shadow",
        status=PartyIdentityStatus.active.value,
        data_classification=PartyDataClassification.test.value,
    )
    team = ServiceTeam(name=f"Retirement duplicate {uuid4()}", is_active=True)
    db_session.add_all([user, shadow_org, team])
    db_session.flush()
    native = ServiceTeamMember(
        team_id=team.id,
        person_id=person.id,
        role=None,
        is_active=False,
    )
    compat = ServiceTeamMember(
        team_id=team.id,
        person_id=user.id,
        role=None,
        is_active=True,
    )
    members_setting = DomainSetting(
        domain=SettingDomain.workflow,
        key="support_service_team_members",
        value_type=SettingValueType.json,
        value_json=[{"person": str(user.id), "team": str(team.id)}],
        is_active=True,
    )
    db_session.add_all([native, compat, members_setting])
    db_session.flush()
    native_id = native.id
    compat_id = compat.id
    db_session.commit()

    command_id = uuid4()
    outcome = service_team_source_retirement.retire_legacy_service_team_sources(
        db_session,
        service_team_source_retirement.RetireLegacyServiceTeamSources(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{uuid4()}",
                scope="operations:service_team:retire",
                reason="test duplicate identity resolution",
                idempotency_key=f"source-retirement:{command_id}",
            )
        ),
    )

    assert outcome.removed_migration_blocking_membership_count == 1
    assert outcome.reactivated_native_membership_count == 1
    assert outcome.abandoned_legacy_member_entry_count == 1
    assert db_session.get(ServiceTeamMember, compat_id) is None
    restored = db_session.get(ServiceTeamMember, native_id)
    assert restored is not None
    assert restored.is_active is True
