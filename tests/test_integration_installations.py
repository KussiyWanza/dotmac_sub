from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationInstallationState,
    IntegrationValidationStatus,
)
from app.services import web_integrations_whatsapp
from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult
from app.services.integrations.whatsapp_capability import (
    WHATSAPP_RECEIVE_CAPABILITY,
    WHATSAPP_SEND_CAPABILITY,
    WHATSAPP_TEMPLATE_READ_CAPABILITY,
)


def _create_whatsapp_installation(db_session, *, name: str = "WhatsApp primary"):
    return installations.create_draft(
        db_session,
        connector_key="whatsapp",
        name=name,
        actor="test-operator",
    )


def test_create_draft_pins_deployed_manifest(db_session):
    installation = installations.create_draft(
        db_session,
        connector_key="whatsapp",
        name="WhatsApp direct",
        environment="sandbox",
        actor="operator-1",
    )

    assert installation.state == IntegrationInstallationState.draft.value
    assert installation.connector_key == "whatsapp"
    assert installation.connector_version == "1.1.0"
    assert len(installation.manifest_digest) == 64
    assert installation.environment == "sandbox"


def test_catalogue_only_definition_cannot_be_installed(db_session):
    with pytest.raises(installations.InstallationError, match="no approved"):
        installations.create_draft(
            db_session,
            connector_key="3cx",
            name="PBX",
        )


def test_config_revision_stores_only_declared_secret_references(db_session):
    installation = _create_whatsapp_installation(db_session)

    with pytest.raises(installations.InstallationError, match="references only"):
        installations.create_config_revision(
            db_session,
            installation_id=installation.id,
            config={"provider": "meta_cloud_api"},
            secret_refs={"service_credentials": "plaintext-token"},
        )
    with pytest.raises(installations.InstallationError, match="undeclared"):
        installations.create_config_revision(
            db_session,
            installation_id=installation.id,
            config={},
            secret_refs={"other_secret": "bao://secret/integrations/test#token"},
        )

    revision = installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={"provider": "meta_cloud_api"},
        secret_refs={"service_credentials": "bao://secret/integrations/whatsapp#token"},
        actor="operator-1",
    )
    replay = installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={"provider": "meta_cloud_api"},
        secret_refs={"service_credentials": "bao://secret/integrations/whatsapp#token"},
        actor="operator-1",
    )

    assert revision.id == replay.id
    assert revision.revision == 1
    assert revision.validation_status == IntegrationValidationStatus.pending.value
    assert len(revision.config_digest) == 64
    assert installation.current_config_revision_id == revision.id


def test_static_and_connection_validation_gate_enablement(db_session):
    installation = _create_whatsapp_installation(db_session)
    installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={"provider": "meta_cloud_api"},
        secret_refs={"service_credentials": "bao://secret/integrations/whatsapp#token"},
    )

    missing_binding = installations.validate_static(
        db_session,
        installation_id=installation.id,
    )
    assert not missing_binding.valid
    assert "capability_binding_missing" in missing_binding.error_codes
    assert installation.state == IntegrationInstallationState.draft.value

    binding = installations.bind_capability(
        db_session,
        installation_id=installation.id,
        capability_id="messaging.send.v1",
        scope={"audience": "customer"},
        actor="operator-2",
    )
    static_result = installations.validate_static(
        db_session,
        installation_id=installation.id,
        actor="operator-2",
    )

    assert static_result.valid
    assert installation.state == IntegrationInstallationState.disabled.value
    assert binding.state == IntegrationBindingState.disabled.value
    assert installation.current_config_revision.validation_status == (
        IntegrationValidationStatus.valid.value
    )

    with pytest.raises(installations.InstallationError, match="connection"):
        installations.enable_after_connection_validation(
            db_session,
            installation_id=installation.id,
            connection_result=ValidationResult(
                valid=False,
                error_codes=("auth_failed",),
            ),
        )

    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
        actor="operator-2",
    )
    assert installation.state == IntegrationInstallationState.enabled.value
    assert binding.state == IntegrationBindingState.enabled.value
    assert installation.enabled_at is not None


def test_config_change_disables_enabled_capabilities(db_session):
    installation = _create_whatsapp_installation(db_session)
    installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={"provider": "meta_cloud_api"},
        secret_refs={"service_credentials": "bao://secret/integrations/whatsapp#token"},
    )
    binding = installations.bind_capability(
        db_session,
        installation_id=installation.id,
        capability_id="messaging.send.v1",
    )
    installations.validate_static(db_session, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )

    revision = installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={
            "provider": "meta_cloud_api",
            "phone_number": "+2348000000000",
        },
        secret_refs={"service_credentials": "bao://secret/integrations/whatsapp#token"},
    )

    assert revision.revision == 2
    assert installation.state == IntegrationInstallationState.draft.value
    assert installation.validated_at is None
    assert binding.state == IntegrationBindingState.disabled.value


def test_undeclared_capability_is_rejected(db_session):
    installation = _create_whatsapp_installation(db_session)

    with pytest.raises(installations.InstallationError, match="does not declare"):
        installations.bind_capability(
            db_session,
            installation_id=installation.id,
            capability_id="payments.intent.v1",
        )


def test_whatsapp_configuration_rejects_non_meta_provider(db_session):
    installation = _create_whatsapp_installation(db_session)

    with pytest.raises(installations.InstallationError, match="config_enum:provider"):
        installations.create_config_revision(
            db_session,
            installation_id=installation.id,
            config={"provider": "twilio"},
            secret_refs={
                "service_credentials": "bao://secret/integrations/whatsapp#token"
            },
        )


def test_whatsapp_config_save_persists_phone_number_id_and_enables_bindings(
    db_session, monkeypatch
):
    monkeypatch.setenv("WHATSAPP_TEST_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_TEST_SECRET", "test-secret")
    monkeypatch.setenv("WHATSAPP_TEST_VERIFY_TOKEN", "test-verify-token")

    installation = web_integrations_whatsapp.save_config(
        db_session,
        provider="meta_cloud_api",
        phone_number="+2348012345678",
        phone_number_id="445744508632976",
        waba_id="443723705501042",
        webhook_url="https://selfcare.dotmac.io/api/webhooks/whatsapp",
        graph_version="v21.0",
        api_key="env://WHATSAPP_TEST_TOKEN",
        api_secret="env://WHATSAPP_TEST_SECRET",
        webhook_verify_token="env://WHATSAPP_TEST_VERIFY_TOKEN",
        message_templates_json="[]",
        actor="test-operator",
    )

    config = dict(installation.current_config_revision.config_json or {})
    bindings = {
        binding.capability_id: binding for binding in installation.capability_bindings
    }

    assert config["phone_number_id"] == "445744508632976"
    assert installation.state == IntegrationInstallationState.enabled.value
    assert set(bindings) == {
        WHATSAPP_SEND_CAPABILITY,
        WHATSAPP_RECEIVE_CAPABILITY,
        WHATSAPP_TEMPLATE_READ_CAPABILITY,
    }
    for binding in bindings.values():
        assert binding.state == IntegrationBindingState.enabled.value
        assert binding.scope_json == {
            "channel": "whatsapp",
            "phone_number_id": "445744508632976",
        }
        assert binding.policy_json == {"default": True}


def test_quarantine_and_retirement_are_terminal_for_execution_config(db_session):
    installation = _create_whatsapp_installation(db_session)
    installations.quarantine_installation(
        db_session,
        installation_id=installation.id,
        reason="credential_compromise",
        actor="security",
    )
    assert installation.state == IntegrationInstallationState.quarantined.value
    assert installation.quarantined_at is not None

    installations.retire_installation(
        db_session,
        installation_id=installation.id,
        reason="provider_removed",
        actor="security",
    )
    assert installation.state == IntegrationInstallationState.retired.value
    assert installation.retired_at is not None
    with pytest.raises(installations.InstallationError, match="retired"):
        installations.create_config_revision(
            db_session,
            installation_id=installation.id,
            config={},
            secret_refs={},
        )


def test_foundation_migration_is_linear_and_additive() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/376_integration_platform_foundation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_376", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "376_integration_platform_foundation"
    assert module.down_revision == "375_work_order_evidence_policy"
    source = path.read_text(encoding="utf-8")
    assert "integration_installations" in source
    assert "integration_config_revisions" in source
    assert "integration_capability_bindings" in source
    for legacy_table in (
        "connector_configs",
        "integration_targets",
        "integration_jobs",
        "integration_runs",
        "integration_hooks",
        "webhook_deliveries",
    ):
        assert f'op.drop_table("{legacy_table}")' not in source


def test_cutover_migration_is_linear_destructive_and_irreversible() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/380_integration_platform_cutover.py"
    )
    spec = importlib.util.spec_from_file_location("migration_380", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "380_integration_platform_cutover"
    assert module.down_revision == "379_integration_inbox"
    source = path.read_text(encoding="utf-8")
    for retired_table in (
        "webhook_deliveries",
        "webhook_subscriptions",
        "webhook_endpoints",
        "integration_hook_executions",
        "integration_hooks",
        "crm_webhook_deliveries",
        "payment_webhook_dead_letters",
    ):
        assert f'op.drop_table("{retired_table}")' in source
    for retired_setting in (
        "paystack_secret_key",
        "flutterwave_secret_key",
        "crm_phase3_native_sync_enabled",
        "whatsapp_provider",
        "whatsapp_api_key",
        "whatsapp_api_secret",
        "whatsapp_api_timeout_seconds",
        "meta_webhook_verify_token",
    ):
        assert retired_setting in module._RETIRED_SETTING_KEYS
    with pytest.raises(RuntimeError, match="irreversible"):
        module.downgrade()
