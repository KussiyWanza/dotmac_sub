import re
from pathlib import Path

from fastapi.routing import APIRoute

from app.services.sot_manifest import AuthorityMigrationState, OwnerRole
from app.services.sot_registry.registry import DOMAIN_SOT_RELATIONSHIPS
from app.services.sot_relationships import service_relationship
from app.web.admin import service_teams
from scripts.seed import seed_rbac

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_service_team_owners_have_complete_typed_contracts() -> None:
    lifecycle = service_relationship("operations.service_team_lifecycle")
    assert lifecycle.module == "app.services.service_team_lifecycle"
    assert lifecycle.contract is not None
    assert {item.name for item in lifecycle.contract.concerns} >= {
        "service-team lifecycle",
        "service-team membership lifecycle",
        "set-valued staff service-team membership resolution",
    }

    composition = service_relationship("operations.service_team_composition")
    assert composition.module == "app.services.service_team_composition"
    assert composition.contract is not None
    concerns = {item.name: item.role for item in composition.contract.concerns}
    assert concerns["service-team composition lifecycle"] is OwnerRole.COMMAND_WRITER
    assert concerns["explicit service-team routing policy"] is OwnerRole.COMMAND_WRITER
    assert (
        concerns[
            "set-valued service-team capability, responsibility, and scope resolution"
        ]
        is OwnerRole.RESOLVER
    )
    assert composition.contract.migration.state is AuthorityMigrationState.SHADOWING
    assert "ROUTING_POLICY_CONTRACTS" in _source(
        "app/services/service_team_composition.py"
    )

    retirement = service_relationship("operations.service_team_source_retirement")
    assert retirement.module == "app.services.service_team_source_retirement"
    assert retirement.contract is not None
    assert retirement.contract.migration.fallback_retirement is not None
    assert "CRM membership planner" in retirement.contract.migration.fallback_retirement


def test_ticket_settings_has_no_parallel_team_writer_or_payload() -> None:
    service = _source("app/services/support_ticket_settings.py")
    route = _source("app/web/admin/system.py")
    template = _source("templates/admin/system/ticket_settings.html")
    for token in (
        "support_service_teams",
        "support_service_team_members",
        "_sync_service_team_tables",
        "service_team_labels",
        "team_member_team_ids",
    ):
        assert token not in service
        assert token not in route
        assert token not in template
    assert "service_team_lifecycle.list_active_team_options" in service


def test_native_admin_routes_are_thin_and_do_not_offer_scalar_role_or_delete() -> None:
    source = _source("app/web/admin/service_teams.py")
    assert "db.add(" not in source
    assert "db.query(" not in source
    assert ".commit(" not in source
    assert ".delete(" not in source
    assert "ServiceTeamType" not in source
    assert "ServiceTeamMemberRole" not in source
    paths = {
        route.path
        for route in service_teams.router.routes
        if isinstance(route, APIRoute)
    }
    assert "/system/service-teams" in paths
    assert "/system/service-teams/{team_id}/capabilities" in paths
    assert (
        "/system/service-teams/{team_id}/members/{member_id}/responsibilities" in paths
    )
    assert not any(path.endswith("/role") for path in paths)
    assert not any(path.endswith("/delete") for path in paths)


def test_service_team_permissions_are_seeded_and_assignable() -> None:
    expected = {
        service_teams.READ_PERMISSION,
        service_teams.CREATE_PERMISSION,
        service_teams.UPDATE_PERMISSION,
        service_teams.MEMBERSHIP_PERMISSION,
        service_teams.RETIRE_PERMISSION,
    }
    seeded = {key for key, _description in seed_rbac.DEFAULT_PERMISSIONS}
    assert expected <= seeded
    assert expected.isdisjoint(seed_rbac.ADMIN_ONLY_PERMISSION_KEYS)


def test_named_consumers_do_not_read_scalar_team_identity() -> None:
    consumers = (
        "app/services/team_outbound.py",
        "app/services/team_inbox_field_job.py",
        "app/services/team_inbox_metrics.py",
        "app/services/team_inbox_read.py",
        "app/services/topology/outage_operations.py",
        "app/services/workqueue/scope.py",
    )
    forbidden = (
        "ServiceTeamType",
        "ServiceTeamMemberRole",
        "ServiceTeam.team_type",
        ".team_type",
        "manager_person_id",
        "ServiceTeamMember.role",
    )
    for path in consumers:
        source = _source(path)
        for token in forbidden:
            assert token not in source, (path, token)

    assert "WorkOrderAssignmentQueue" in _source("app/services/team_inbox_field_job.py")
    outage = _source("app/services/topology/outage_operations.py")
    assert "resolve_routing_team" in outage
    assert "created_at.asc()" not in outage
    workqueue = _source("app/services/workqueue/scope.py")
    assert "resolve_staff_capability_scope" in workqueue
    permissions = _source("app/services/workqueue/permissions.py")
    assert "leads_team and AUDIENCE_TEAM_SCOPE in principal.scopes" in permissions


def test_lifecycle_admin_projection_consumes_composition_owner_queries() -> None:
    source = _source("app/services/service_team_lifecycle.py")
    assert "service_team_composition.capabilities_by_team" in source
    assert "service_team_composition.scope_bindings_by_team" in source
    assert "service_team_composition.responsibilities_by_membership" in source
    for token in (
        "ServiceTeamCapability.",
        "ServiceTeamMemberResponsibility.",
        "ServiceTeamScopeBinding.",
    ):
        assert token not in source


def test_legacy_scalar_columns_are_shadow_only_until_verified_contract() -> None:
    model = _source("app/models/service_team.py")
    migration = _source("alembic/versions/440_composable_service_teams.py")
    design = _source("docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md")
    for name in (
        "ServiceTeamCapability",
        "ServiceTeamMemberResponsibility",
        "ServiceTeamRelationship",
        "ServiceTeamScopeBinding",
        "ServiceTeamExternalReference",
        "ServiceTeamRoutingPolicy",
    ):
        assert f"class {name}" in model
    assert "op.drop_column" not in migration
    assert "complete-cohort shadow run" in design
    assert "Only then may a later forward migration drop" in design


def test_no_second_application_service_team_identity_writer_exists() -> None:
    allowed = {
        ROOT / "app/models/service_team.py",
        ROOT / "app/services/service_team_lifecycle.py",
    }
    constructor = re.compile(r"\b(?:ServiceTeam|ServiceTeamMember)\(")
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if path not in allowed and constructor.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_historical_party_adoption_path_cannot_return() -> None:
    assert not (ROOT / "app/services/service_team_party_cutover.py").exists()
    assert not (ROOT / "scripts/migration/plan_service_team_party_cutover.py").exists()
    assert not (
        ROOT / "scripts/migration/execute_service_team_party_cutover.py"
    ).exists()
    assert "service_team_party_cutover" not in repr(DOMAIN_SOT_RELATIONSHIPS)
