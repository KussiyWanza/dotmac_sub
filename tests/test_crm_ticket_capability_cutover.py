from __future__ import annotations

import pytest

from app.models.event_store import EventStore
from app.models.integration import (
    IntegrationJob,
    IntegrationJobType,
    IntegrationScheduleType,
    IntegrationTarget,
    IntegrationTargetType,
)
from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallationState,
)
from app.services import control_registry
from app.services import integration as integration_jobs
from app.services.integrations import installations
from app.services.integrations.connectors.dotmac_crm import (
    CRM_TICKET_OBSERVATION_CAPABILITY,
)
from app.services.integrations.crm_ticket_readiness import (
    preview_crm_ticket_cutover,
    resolve_crm_ticket_pull_readiness,
)
from app.services.integrations.runtime import ValidationResult
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_crm_inbound


def _context(*, scope: str, key: str) -> CommandContext:
    return CommandContext.system(
        actor="operator:crm-cutover-test",
        scope=scope,
        reason="Complete reviewed CRM ticket capability cutover",
        idempotency_key=key,
    )


def _legacy_production_state(db_session, monkeypatch):
    inbound = enable_crm_inbound(
        db_session,
        monkeypatch,
        signing_secret="test-webhook-signing-secret",
    )
    installation = inbound.installation
    installation.environment = "production"
    target = IntegrationTarget(
        name="DotMac CRM",
        target_type=IntegrationTargetType.crm,
        is_active=True,
    )
    db_session.add(target)
    db_session.flush()
    job = IntegrationJob(
        target_id=target.id,
        name="Pull CRM Tickets",
        job_type=IntegrationJobType.sync,
        schedule_type=IntegrationScheduleType.manual,
        is_active=False,
        capability_binding_id=None,
    )
    db_session.add(job)
    db_session.commit()
    return installation.id, inbound.id, job.id


def _provision_binding(db_session, monkeypatch, installation_id):
    from app.services.integrations import runtime_execution

    monkeypatch.setattr(
        runtime_execution,
        "validate_connection",
        lambda _context: ValidationResult(valid=True),
    )
    installation = installations.get_installation(db_session, installation_id)
    pin = installations.ManifestPin(
        connector_version=installation.connector_version,
        manifest_digest=installation.manifest_digest,
    )
    db_session.commit()
    return installations.provision_installation_capability(
        db_session,
        installations.ProvisionCapabilityCommand(
            installation_id=installation_id,
            capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
            expected_installed_pin=pin,
            expected_binding_id=None,
            expected_binding_state=None,
            capability_scope={},
            policy={"default": True},
        ),
        context=_context(
            scope=installations.CAPABILITY_PROVISIONING_SCOPE,
            key="crm-ticket-binding-v1",
        ),
    )


def test_cutover_preview_matches_the_production_gap(
    db_session,
    monkeypatch,
) -> None:
    installation_id, inbound_binding_id, job_id = _legacy_production_state(
        db_session,
        monkeypatch,
    )
    control_registry.update_canonical_feature_controls(
        db_session,
        payload={"crm.ticket_pull": True},
    )

    preview = preview_crm_ticket_cutover(
        db_session,
        installation_id=installation_id,
        job_id=job_id,
    )

    assert preview.eligible is True
    assert preview.already_ready is False
    assert preview.binding_id is None
    assert preview.job_binding_id is None
    assert preview.job_is_active is False
    assert preview.readiness.issue_codes == (
        "enabled_ticket_observation_binding_count:0",
        "active_ticket_observation_job_count:0",
    )
    assert len(preview.fingerprint) == 64


def test_capability_provisioning_is_atomic_audited_and_replay_safe(
    db_session,
    monkeypatch,
) -> None:
    installation_id, inbound_binding_id, _job_id = _legacy_production_state(
        db_session,
        monkeypatch,
    )

    result = _provision_binding(db_session, monkeypatch, installation_id)

    assert result.replayed is False
    installation = installations.get_installation(db_session, installation_id)
    assert installation.state == IntegrationInstallationState.enabled.value
    bindings = {
        binding.capability_id: binding for binding in installation.capability_bindings
    }
    assert set(bindings) == {
        "crm.events.receive.v1",
        CRM_TICKET_OBSERVATION_CAPABILITY,
    }
    assert bindings["crm.events.receive.v1"].id == inbound_binding_id
    assert {binding.state for binding in bindings.values()} == {
        IntegrationBindingState.enabled.value
    }
    events = (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == "integration.installation.capability_provisioned"
        )
        .all()
    )
    assert len(events) == 1
    assert "secret" not in events[0].payload
    expected_pin = installations.ManifestPin(
        connector_version=installation.connector_version,
        manifest_digest=installation.manifest_digest,
    )
    db_session.commit()

    replay = installations.provision_installation_capability(
        db_session,
        installations.ProvisionCapabilityCommand(
            installation_id=installation_id,
            capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
            expected_installed_pin=expected_pin,
            expected_binding_id=result.capability_binding_id,
            expected_binding_state=IntegrationBindingState.enabled,
            capability_scope={},
            policy={"default": True},
        ),
        context=_context(
            scope=installations.CAPABILITY_PROVISIONING_SCOPE,
            key="crm-ticket-binding-v1",
        ),
    )

    assert replay.replayed is True
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == "integration.installation.capability_provisioned"
        )
        .count()
        == 1
    )


def test_connection_failure_restores_the_existing_inbound_binding(
    db_session,
    monkeypatch,
) -> None:
    installation_id, inbound_binding_id, _job_id = _legacy_production_state(
        db_session,
        monkeypatch,
    )
    from app.services.integrations import runtime_execution

    monkeypatch.setattr(
        runtime_execution,
        "validate_connection",
        lambda _context: ValidationResult(
            valid=False,
            error_codes=("crm_unreachable",),
        ),
    )
    installation = installations.get_installation(db_session, installation_id)
    pin = installations.ManifestPin(
        connector_version=installation.connector_version,
        manifest_digest=installation.manifest_digest,
    )
    db_session.commit()

    with pytest.raises(
        installations.CapabilityProvisioningError,
        match="Connector connection validation failed",
    ):
        installations.provision_installation_capability(
            db_session,
            installations.ProvisionCapabilityCommand(
                installation_id=installation_id,
                capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
                expected_installed_pin=pin,
                expected_binding_id=None,
                expected_binding_state=None,
                capability_scope={},
                policy={"default": True},
            ),
            context=_context(
                scope=installations.CAPABILITY_PROVISIONING_SCOPE,
                key="crm-ticket-binding-failure",
            ),
        )

    installation = installations.get_installation(db_session, installation_id)
    assert installation.state == IntegrationInstallationState.enabled.value
    inbound = db_session.get(IntegrationCapabilityBinding, inbound_binding_id)
    assert inbound is not None
    assert inbound.state == IntegrationBindingState.enabled.value
    assert (
        db_session.query(IntegrationCapabilityBinding)
        .filter(
            IntegrationCapabilityBinding.installation_id == installation_id,
            IntegrationCapabilityBinding.capability_id
            == CRM_TICKET_OBSERVATION_CAPABILITY,
        )
        .count()
        == 0
    )


def test_job_activation_completes_readiness_and_replays(
    db_session,
    monkeypatch,
) -> None:
    installation_id, inbound_binding_id, job_id = _legacy_production_state(
        db_session,
        monkeypatch,
    )
    control_registry.update_canonical_feature_controls(
        db_session,
        payload={"crm.ticket_pull": True},
    )
    binding = _provision_binding(db_session, monkeypatch, installation_id)

    result = integration_jobs.activate_capability_job(
        db_session,
        integration_jobs.ActivateCapabilityJobCommand(
            job_id=job_id,
            capability_binding_id=binding.capability_binding_id,
            capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
            expected_target_type=IntegrationTargetType.crm,
            expected_existing_binding_id=None,
            expected_is_active=False,
        ),
        context=_context(
            scope=integration_jobs.CAPABILITY_JOB_ACTIVATION_SCOPE,
            key="crm-ticket-job-v1",
        ),
    )

    assert result.replayed is False
    job = db_session.get(IntegrationJob, job_id)
    assert job is not None
    assert job.is_active is True
    assert job.capability_binding_id == binding.capability_binding_id
    assert job.schedule_type == IntegrationScheduleType.manual
    readiness = resolve_crm_ticket_pull_readiness(db_session)
    assert readiness.ready is True
    assert readiness.schedule_enabled is True
    assert readiness.active_job_ids == (job_id,)
    job.schedule_type = IntegrationScheduleType.interval
    db_session.flush()
    drifted = resolve_crm_ticket_pull_readiness(db_session)
    assert drifted.ready is False
    assert drifted.issue_codes == ("active_ticket_observation_job_count:0",)
    job.schedule_type = IntegrationScheduleType.manual
    db_session.commit()

    replay = integration_jobs.activate_capability_job(
        db_session,
        integration_jobs.ActivateCapabilityJobCommand(
            job_id=job_id,
            capability_binding_id=binding.capability_binding_id,
            capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
            expected_target_type=IntegrationTargetType.crm,
            expected_existing_binding_id=None,
            expected_is_active=False,
        ),
        context=_context(
            scope=integration_jobs.CAPABILITY_JOB_ACTIVATION_SCOPE,
            key="crm-ticket-job-v1",
        ),
    )

    assert replay.replayed is True
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "integration.job.capability_activated")
        .count()
        == 1
    )


def test_readiness_is_not_required_when_ticket_pull_control_is_disabled(
    db_session,
    monkeypatch,
) -> None:
    _legacy_production_state(db_session, monkeypatch)
    control_registry.update_canonical_feature_controls(
        db_session,
        payload={"crm.ticket_pull": False},
    )

    readiness = resolve_crm_ticket_pull_readiness(db_session)

    assert readiness.control_enabled is False
    assert readiness.ready is True
    assert readiness.schedule_enabled is False


def test_job_activation_rejects_changed_reviewed_state(
    db_session,
    monkeypatch,
) -> None:
    installation_id, inbound_binding_id, job_id = _legacy_production_state(
        db_session,
        monkeypatch,
    )
    binding = _provision_binding(db_session, monkeypatch, installation_id)
    job = db_session.get(IntegrationJob, job_id)
    assert job is not None
    job.is_active = True
    job.capability_binding_id = inbound_binding_id
    db_session.commit()

    with pytest.raises(
        integration_jobs.IntegrationJobCommandError,
        match="changed after capability activation review",
    ):
        integration_jobs.activate_capability_job(
            db_session,
            integration_jobs.ActivateCapabilityJobCommand(
                job_id=job_id,
                capability_binding_id=binding.capability_binding_id,
                capability_id=CRM_TICKET_OBSERVATION_CAPABILITY,
                expected_target_type=IntegrationTargetType.crm,
                expected_existing_binding_id=None,
                expected_is_active=False,
            ),
            context=_context(
                scope=integration_jobs.CAPABILITY_JOB_ACTIVATION_SCOPE,
                key="crm-ticket-job-stale",
            ),
        )
