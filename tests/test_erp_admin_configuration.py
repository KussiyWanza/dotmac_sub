from __future__ import annotations

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
)
from app.models.scheduler import ScheduledTask
from app.services.integrations import installations
from app.services.integrations.backoffice_contracts import (
    ERP_OPERATIONAL_SYNC_CAPABILITY,
    ERP_STATUS_CAPABILITY,
)
from app.services.integrations.runtime import ValidationResult
from app.web.admin.integrations import erp_connector_config_save


def _erp_installation(db_session, monkeypatch):
    monkeypatch.setenv("ERP_TEST_TOKEN", "test-token")
    monkeypatch.setenv("ERP_TEST_WEBHOOK_SECRET", "test-webhook-secret")
    installation = installations.create_draft(
        db_session,
        connector_key="dotmac.erp",
        name="DotMac ERP configuration test",
        environment="test",
    )
    installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={
            "base_url": "https://erp.dotmac.io",
            "timeout_seconds": 5,
            "max_retries": 1,
        },
        secret_refs={
            "service_credentials": "env://ERP_TEST_TOKEN",
            "webhook_signing_secret": "env://ERP_TEST_WEBHOOK_SECRET",
        },
    )
    installations.bind_capability(
        db_session,
        installation_id=installation.id,
        capability_id=ERP_STATUS_CAPABILITY,
        scope={"resource": "status"},
        policy={"default": True},
    )
    installations.validate_static(db_session, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    return installation


def test_erp_profile_save_releases_review_transaction_before_owner_command(
    db_session,
    monkeypatch,
) -> None:
    installation = _erp_installation(db_session, monkeypatch)
    from app.services.integrations import runtime_execution

    monkeypatch.setattr(
        runtime_execution,
        "validate_connection",
        lambda _context: ValidationResult(valid=True),
    )

    # Match a real request, where authentication or other adapter reads may
    # already have opened an implicit read transaction.
    assert db_session.get(type(installation), installation.id) is not None
    assert db_session.in_transaction()

    response = erp_connector_config_save(
        request=object(),
        domains=["project_tasks"],
        db=db_session,
        auth={"user_id": "erp-config-test"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/integrations/erp?saved=1"
    binding = (
        db_session.query(IntegrationCapabilityBinding)
        .filter(
            IntegrationCapabilityBinding.installation_id == installation.id,
            IntegrationCapabilityBinding.capability_id
            == ERP_OPERATIONAL_SYNC_CAPABILITY,
        )
        .one()
    )
    assert binding.state == IntegrationBindingState.enabled.value
    assert binding.scope_json == {"domains": ["projects", "tickets", "project_tasks"]}
    schedule = (
        db_session.query(ScheduledTask)
        .filter(ScheduledTask.name == "dotmac_erp_operational_domain_sync")
        .one()
    )
    assert schedule.enabled is True
    assert schedule.interval_seconds == 300
