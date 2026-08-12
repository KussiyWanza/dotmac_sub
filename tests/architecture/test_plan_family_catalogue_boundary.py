from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_plan_family_catalogue_has_one_contracted_command_owner():
    service = _source("app/services/catalog/plan_family_catalogues.py")
    settings_route = _source("app/web/admin/catalog_settings.py")
    inbox_route = _source("app/web/admin/inbox.py")

    owner = service_relationship("service_intent.plan_family_catalogues")
    assert owner.module == "app.services.catalog.plan_family_catalogues"
    assert owner.contract is not None
    assert "execute_owner_command(" in service
    assert "file_uploads.prepare_upload(" in service
    assert "file_uploads.stage_prepared_upload(" in service
    assert "db.commit(" not in service
    assert "db.rollback(" not in service
    assert "PlanFamilyCatalogue(" not in settings_route
    assert "PlanFamilyCatalogue(" not in inbox_route


def test_inbox_actions_render_owner_eligibility_and_catalogue_options():
    projection = _source("app/services/team_inbox_projection.py")
    template = _source("templates/admin/inbox/_conversation.html")
    drawer = _source("templates/admin/inbox/_contact_drawer.html")

    assert "manual_invitation_eligibility(" in projection
    assert "list_catalogue_options(" in projection
    assert "action_eligibility.can_issue_lead_form" in template
    assert "Share catalogue" in template
    assert "/share-catalogue" in template
    assert "/lead-intake/issue" not in drawer


def test_catalogue_schema_is_versioned_and_has_one_current_publication():
    migration = _source("alembic/versions/495_plan_family_catalogues.py")
    model = _source("app/models/plan_family_catalogue.py")
    storage = _source("app/services/file_storage.py")

    assert (
        'down_revision: str | None = "494_team_inbox_agent_introductions"' in migration
    )
    assert "uq_plan_family_catalogues_family_version" in migration
    assert "uq_plan_family_catalogues_one_published_family" in migration
    assert "postgresql_where=sa.text(\"status = 'published'\")" in migration
    assert "status IN ('published', 'superseded', 'withdrawn')" in model
    assert '"catalogues": FileDomainConfig(' in storage
    assert 'allowed_mime_types=frozenset({"application/pdf"})' in storage
