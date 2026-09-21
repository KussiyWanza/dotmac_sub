from datetime import UTC, datetime
from uuid import uuid4

from app.models.audit import AuditEvent
from app.models.integration_platform import IntegrationInstallation
from app.services import module_manager, web_control_plane
from app.services.web_integrations import (
    InstalledIntegrationProjection,
    IntegrationOperationalEvidence,
)


def test_control_plane_covers_all_domains_and_explains_each_row(
    db_session, monkeypatch
):
    db_session.add(
        AuditEvent(
            action="settings.update",
            entity_type="domain_setting",
            entity_id="billing.currency",
            status_code=200,
            is_success=True,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        web_control_plane,
        "redis_health_check",
        lambda: {"available": True, "checked_at": "2026-07-14T12:00:00Z"},
    )
    monkeypatch.setattr(
        web_control_plane,
        "build_secrets_index_context",
        lambda **_kwargs: {"openbao_available": True, "secrets_list": []},
    )
    monkeypatch.setattr(
        web_control_plane,
        "installed_integration_projections",
        lambda _db: (),
    )

    context = web_control_plane.build_control_plane_context(db_session)

    assert [section["id"] for section in context["sections"]] == [
        "settings",
        "rbac",
        "sessions",
        "scheduler",
        "secrets",
        "integrations",
        "webhooks",
    ]
    entries = [entry for section in context["sections"] for entry in section["entries"]]
    assert entries
    required_fields = {
        "effective_value",
        "source",
        "precedence",
        "scope",
        "health",
        "last_change",
    }
    assert all(required_fields <= entry.keys() for entry in entries)
    assert all("history" in section for section in context["sections"])
    settings_section = context["sections"][0]
    assert settings_section["history"][0]["action"] == "settings.update"


def test_secret_display_never_returns_the_secret_value():
    assert web_control_plane._display_value("test-only-sentinel", secret=True) == (
        "Configured"
    )


def test_inert_module_switches_are_not_registered():
    inert = {"inventory", "helpdesk", "scheduling", "voice"}

    assert inert.isdisjoint(module_manager.MODULE_KEY_MAP)


def test_control_plane_consumes_typed_integration_evidence_without_health_claim(
    db_session, monkeypatch
):
    observed_at = datetime(2026, 9, 8, 10, 15, tzinfo=UTC)
    installation = IntegrationInstallation(
        id=uuid4(),
        connector_key="dotmac.erp",
        connector_version="1.0.0",
        manifest_digest="0" * 64,
        name="Dotmac ERP",
        environment="production",
        state="enabled",
    )
    installation.updated_at = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    projection = InstalledIntegrationProjection(
        installation=installation,
        title="Dotmac ERP",
        root="integrations",
        integration_type="backoffice",
        operational_evidence=IntegrationOperationalEvidence(
            last_result="The last completed integration job failed.",
            observed_at=observed_at,
            calls=1,
            failed=1,
            last_run_status="failed",
            needs_attention=True,
        ),
        manage_url="/admin/integrations/installed/dotmac-erp",
    )
    monkeypatch.setattr(
        web_control_plane,
        "installed_integration_projections",
        lambda _db: (projection,),
    )

    entries = web_control_plane._integration_entries(db_session)

    assert len(entries) == 1
    assert entries[0]["health"] == "unknown"
    assert entries[0]["last_change"] == observed_at
    assert entries[0]["detail_url"] == projection.manage_url
    assert "Attention required" in entries[0]["effective_value"]
    assert projection.operational_evidence.last_result in entries[0]["effective_value"]


def test_control_plane_marks_only_installation_disablement_as_disabled(
    db_session, monkeypatch
):
    installation = IntegrationInstallation(
        id=uuid4(),
        connector_key="webhook.http",
        connector_version="1.0.0",
        manifest_digest="1" * 64,
        name="Outbound webhook",
        environment="production",
        state="disabled",
    )
    installation.updated_at = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    projection = InstalledIntegrationProjection(
        installation=installation,
        title="HTTP Webhook",
        root="integrations",
        integration_type="webhook",
        operational_evidence=IntegrationOperationalEvidence(
            last_result="No completed job or delivery evidence has been recorded.",
            observed_at=None,
            calls=0,
            failed=0,
            last_run_status=None,
            needs_attention=False,
        ),
        manage_url="/admin/integrations/installed",
    )
    monkeypatch.setattr(
        web_control_plane,
        "installed_integration_projections",
        lambda _db: (projection,),
    )

    entries = web_control_plane._integration_entries(db_session)

    assert entries[0]["health"] == "disabled"
    assert entries[0]["last_change"] == installation.updated_at
