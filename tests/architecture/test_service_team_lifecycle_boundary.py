import re
from pathlib import Path

from fastapi.routing import APIRoute

from app.services.sot_relationships import service_relationship
from app.web.admin import service_teams
from scripts.seed import seed_rbac

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_service_team_owner_has_complete_typed_contract() -> None:
    owner = service_relationship("operations.service_team_lifecycle")
    assert owner.module == "app.services.service_team_lifecycle"
    assert owner.contract is not None
    assert {item.name for item in owner.contract.concerns} >= {
        "service-team lifecycle",
        "service-team membership lifecycle",
        "staff service-team resolution",
    }
    inputs = {item.name: item.owner for item in owner.contract.authoritative_inputs}
    assert inputs["canonical Person Party identity"] == "party.registry"
    assert inputs["active staff authentication principal"] == "auth.staff_provisioning"


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


def test_native_admin_routes_are_thin_and_do_not_offer_hard_delete() -> None:
    source = _source("app/web/admin/service_teams.py")
    assert "db.add(" not in source
    assert "db.query(" not in source
    assert ".commit(" not in source
    assert ".delete(" not in source
    paths = {
        route.path
        for route in service_teams.router.routes
        if isinstance(route, APIRoute)
    }
    assert "/system/service-teams" in paths
    assert "/system/service-teams/{team_id}/active" in paths
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


def test_local_admin_bootstrap_uses_the_party_registry_binding() -> None:
    source = _source("scripts/seed/seed_admin.py")
    assert "party_service.create_party(" in source
    assert "party_service.bind_system_user_principal(" in source
    assert "system_user.person_party_id =" not in source


def test_service_team_browser_journey_does_not_open_the_application_database() -> None:
    source = _source("tests/playwright/e2e/test_service_teams.py")
    assert "SessionLocal" not in source
    assert "test_identities" not in source
    assert "add_bound_staff_login(" in source


def test_party_identity_is_enforced_at_team_storage_and_consumers() -> None:
    model = _source("app/models/service_team.py")
    migration = _source("alembic/versions/426_service_team_lifecycle.py")
    assert "fk_service_team_members_person_id_parties" in model
    assert "fk_service_teams_manager_person_id_parties" in model
    consumer = _source("app/services/team_inbox_assignment.py")
    assert "SystemUser.person_party_id" in consumer
    assert "ServiceTeamMember.person_id" in consumer
    assert "_rewrite_compatibility_person_ids" in migration
    assert "raise RuntimeError" in migration
    assert migration.rindex(
        "_rewrite_compatibility_person_ids(bind)"
    ) < migration.rindex("_backfill_setting_members(bind)")
    field_job = _source("app/services/team_inbox_field_job.py")
    assert "resolve_staff_service_team" in field_job
    assert "ServiceTeamMember" not in field_job


def test_no_second_application_service_team_writer_exists() -> None:
    allowed = {
        ROOT / "app/models/service_team.py",
        ROOT / "app/services/service_team_lifecycle.py",
        ROOT / "app/services/service_team_party_cutover.py",
    }
    constructor = re.compile(r"\b(?:ServiceTeam|ServiceTeamMember)\(")
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if path not in allowed and constructor.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_service_team_person_id_consumers_cross_the_reviewed_party_binding() -> None:
    owner = ROOT / "app/services/service_team_lifecycle.py"
    consumers = {
        path
        for path in (ROOT / "app/services").rglob("*.py")
        if path != owner
        and "ServiceTeamMember.person_id" in path.read_text(encoding="utf-8")
    }

    assert consumers
    for path in consumers:
        source = path.read_text(encoding="utf-8")
        assert "SystemUser.person_party_id" in source, path.relative_to(ROOT)
        assert "ServiceTeamMember.person_id == SystemUser.id" not in source
        assert "ServiceTeamMember.person_id == profile.person_id" not in source
