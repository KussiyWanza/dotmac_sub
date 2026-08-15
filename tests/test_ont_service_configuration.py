"""Focused lifecycle tests for the ONT Configure application owner."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.catalog import SubscriptionStatus
from app.models.network import (
    OntAssignment,
    OntAuthorizationStatus,
    OntProvisioningEvent,
    OntProvisioningEventStatus,
    OntSyncStatus,
    OntUnit,
    OntWanServiceInstance,
    OntWanServiceLifecycle,
    PonPort,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.ont_service_configuration import (
    OntServiceConfigurationHead,
    OntServiceConfigurationPhase,
    OntServiceConfigurationRevision,
)
from app.services.domain_errors import DomainError
from app.services.network.ont_service_configuration import (
    ConfigureOntServiceCommand,
    ExecuteOntServiceConfigurationCommand,
    LanConfigurationChange,
    OntConfigurationChange,
    OntConfigurationSection,
    RetryOntServiceConfigurationCommand,
    WanConfigurationChange,
    WifiConfigurationChange,
    configure_ont_service,
    execute_ont_service_configuration,
    get_ont_service_configuration_projection,
    retry_ont_service_configuration,
)
from app.services.network.reconcile.lifecycle import (
    RetireOntReconcileProjectionForInventory,
    retire_ont_reconcile_projection_for_inventory,
)
from app.services.owner_commands import CommandContext


def _operation(db_session, ont: OntUnit, suffix: str) -> NetworkOperation:
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_service_config,
        target_type=NetworkOperationTargetType.ont,
        target_id=ont.id,
        status=NetworkOperationStatus.pending,
        correlation_key=f"ont-config-test:{suffix}:{uuid.uuid4()}",
        input_payload={},
        initiated_by="test",
    )
    db_session.add(operation)
    db_session.flush()
    return operation


def _admission_scope(
    db_session,
    monkeypatch,
    *,
    olt_device,
    subscription,
    subscriber,
) -> tuple[uuid.UUID, uuid.UUID]:
    subscription.status = SubscriptionStatus.active
    olt_device.is_active = True
    pon = PonPort(
        olt_id=olt_device.id,
        name=f"0/1/{uuid.uuid4().int % 100000}",
        is_active=True,
    )
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"ADMIT-{uuid.uuid4().hex[:10]}",
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        pon_port_id=pon.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.flush()
    ont_id = ont.id
    assignment_id = assignment.id
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": {"id": "test-pack"},
            "values": {"wan_mode": "dhcp", "wan_vlan": 321},
        },
    )
    return ont_id, assignment_id


def _configure_command(
    ont_id: uuid.UUID,
    *,
    idempotency_key: str,
    section: OntConfigurationSection = OntConfigurationSection.wan,
    change: OntConfigurationChange | None = None,
) -> ConfigureOntServiceCommand:
    command_id = uuid.uuid4()
    return ConfigureOntServiceCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="test:operator",
            scope="network:ont:write",
            reason="reviewed ONT customer-service configuration",
            idempotency_key=idempotency_key,
        ),
        ont_unit_id=ont_id,
        permission_granted=True,
        section=section,
        change=change
        or WanConfigurationChange(
            mode="dhcp",
            ip_protocol="ipv4",
            static_ip=None,
            static_subnet=None,
            static_gateway=None,
            static_dns=None,
        ),
    )


def test_configuration_admission_commits_intent_operation_and_dispatch_atomically(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    command = _configure_command(ont_id, idempotency_key="atomic-admission")

    outcome = configure_ont_service(db_session, command)

    head = db_session.get(OntServiceConfigurationHead, outcome.configuration_head_id)
    revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.revision == outcome.revision,
        )
    )
    intent = db_session.scalar(
        select(OntWanServiceInstance).where(
            OntWanServiceInstance.ont_id == ont_id,
            OntWanServiceInstance.subscription_id == subscription.id,
            OntWanServiceInstance.lifecycle_state == OntWanServiceLifecycle.active,
        )
    )
    operation = db_session.get(NetworkOperation, outcome.operation_id)
    dispatch = db_session.scalar(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == outcome.operation_id
        )
    )

    assert head is not None and head.assignment_id == assignment_id
    assert operation is not None
    assert revision is not None and revision.operation_id == operation.id
    assert intent is not None and intent.s_vlan == 321
    assert operation.operation_type is NetworkOperationType.ont_service_config
    assert dispatch is not None
    assert head.phase is OntServiceConfigurationPhase.queued


def test_configuration_admission_rollback_leaves_no_partial_records(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration.stage_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated dispatch staging failure")
        ),
    )

    with pytest.raises(RuntimeError, match="dispatch staging failure"):
        configure_ont_service(
            db_session,
            _configure_command(ont_id, idempotency_key="atomic-rollback"),
        )

    assert db_session.scalar(select(func.count(OntServiceConfigurationHead.id))) == 0
    assert (
        db_session.scalar(
            select(func.count(OntWanServiceInstance.id)).where(
                OntWanServiceInstance.ont_id == ont_id
            )
        )
        == 0
    )
    assert (
        db_session.scalar(
            select(func.count(NetworkOperation.id)).where(
                NetworkOperation.operation_type
                == NetworkOperationType.ont_service_config
            )
        )
        == 0
    )
    assert db_session.scalar(select(func.count(NetworkOperationDispatch.id))) == 0


def test_duplicate_replays_and_changed_input_with_same_key_conflicts(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    original = _configure_command(ont_id, idempotency_key="stable-request-key")
    first = configure_ont_service(db_session, original)

    replay = configure_ont_service(
        db_session,
        _configure_command(ont_id, idempotency_key="stable-request-key"),
    )

    assert replay.replayed is True
    assert replay.operation_id == first.operation_id
    assert replay.revision == first.revision
    assert (
        db_session.scalar(select(func.count(OntServiceConfigurationRevision.id))) == 1
    )
    db_session.commit()

    with pytest.raises(DomainError) as exc_info:
        configure_ont_service(
            db_session,
            _configure_command(
                ont_id,
                idempotency_key="stable-request-key",
                section=OntConfigurationSection.lan,
                change=LanConfigurationChange(
                    gateway_ip="192.168.44.1",
                    subnet_mask="255.255.255.0",
                    dhcp_enabled=True,
                    dhcp_start="192.168.44.10",
                    dhcp_end="192.168.44.200",
                ),
            ),
        )

    assert exc_info.value.code.endswith(".idempotency_conflict")
    assert (
        db_session.scalar(select(func.count(OntServiceConfigurationRevision.id))) == 1
    )


def test_failed_revision_blocks_same_material_but_not_deliberate_next_revision(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    original = _configure_command(ont_id, idempotency_key="revision-one")
    first = configure_ont_service(db_session, original)
    head = db_session.get(OntServiceConfigurationHead, first.configuration_head_id)
    revision_one = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head.id,
            OntServiceConfigurationRevision.revision == 1,
        )
    )
    operation_one = db_session.get(NetworkOperation, first.operation_id)
    assert head is not None and revision_one is not None and operation_one is not None
    head.phase = OntServiceConfigurationPhase.failed
    revision_one.phase = OntServiceConfigurationPhase.failed
    operation_one.status = NetworkOperationStatus.failed
    db_session.commit()

    with pytest.raises(DomainError) as exc_info:
        configure_ont_service(
            db_session,
            _configure_command(ont_id, idempotency_key="ordinary-same-material"),
        )
    assert exc_info.value.code.endswith(".repair_required")

    second = configure_ont_service(
        db_session,
        _configure_command(
            ont_id,
            idempotency_key="revision-two",
            section=OntConfigurationSection.lan,
            change=LanConfigurationChange(
                gateway_ip="192.168.55.1",
                subnet_mask="255.255.255.0",
                dhcp_enabled=True,
                dhcp_start="192.168.55.10",
                dhcp_end="192.168.55.200",
            ),
        ),
    )

    assert second.revision == 2
    assert second.operation_id != first.operation_id
    assert revision_one.phase is OntServiceConfigurationPhase.superseded


def _lifecycle(
    db_session,
    ont: OntUnit,
    assignment: OntAssignment,
    *,
    phase: OntServiceConfigurationPhase,
    suffix: str,
    section: OntConfigurationSection = OntConfigurationSection.wan,
    desired_change_evidence: dict[str, object] | None = None,
) -> tuple[
    OntServiceConfigurationHead,
    OntServiceConfigurationRevision,
    NetworkOperation,
]:
    operation = _operation(db_session, ont, suffix)
    head = OntServiceConfigurationHead(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        current_revision=1,
        latest_operation_id=operation.id,
        phase=phase,
    )
    db_session.add(head)
    db_session.flush()
    revision = OntServiceConfigurationRevision(
        head_id=head.id,
        assignment_id=assignment.id,
        revision=1,
        section=section.value,
        command_fingerprint=suffix.ljust(64, "0")[:64],
        idempotency_key=f"key-{suffix}",
        desired_change_evidence=desired_change_evidence or {},
        operation_id=operation.id,
        phase=phase,
    )
    db_session.add(revision)
    db_session.flush()
    return head, revision, operation


def test_wifi_admission_records_redacted_delivery_evidence(
    db_session,
    monkeypatch,
    olt_device,
    subscription,
    subscriber,
):
    ont_id, _assignment_id = _admission_scope(
        db_session,
        monkeypatch,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    command = _configure_command(
        ont_id,
        idempotency_key="wifi-redacted-delivery-evidence",
        section=OntConfigurationSection.wifi,
        change=WifiConfigurationChange(
            enabled=True,
            ssid="Subscriber WiFi",
            channel="auto",
            security_mode="WPA2-PSK",
            password="not-persisted-in-evidence",
        ),
    )

    outcome = configure_ont_service(db_session, command)

    revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.operation_id == outcome.operation_id
        )
    )
    assert revision is not None
    assert revision.desired_change_evidence["wifi.password"] == "changed"
    assert "not-persisted-in-evidence" not in str(revision.desired_change_evidence)


def test_wifi_worker_passes_typed_redacted_delivery_scope(db_session, monkeypatch):
    ont = OntUnit(serial_number=f"WIFI-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="wifi-scope",
        section=OntConfigurationSection.wifi,
        desired_change_evidence={
            "wifi.enabled": True,
            "wifi.ssid": "Subscriber WiFi",
            "wifi.channel": "auto",
            "wifi.security_mode": "WPA2-PSK",
            "wifi.password": "changed",
        },
    )
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session.commit()
    calls: list[dict[str, object]] = []

    def reconciled(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr("app.services.network.reconcile.core.reconcile_ont", reconciled)
    command_id = uuid.uuid4()

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test WiFi delivery scope",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="wifi-delivery-scope",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert outcome.phase is OntServiceConfigurationPhase.verified
    scope = calls[-1]["wifi_delivery_scope"]
    assert scope is not None
    assert scope.changed_fields == frozenset(
        {
            "wifi_enabled",
            "wifi_ssid",
            "wifi_channel",
            "wifi_security_mode",
            "wifi_password_ref",
        }
    )
    assert "Subscriber WiFi" not in str(scope)


def test_inventory_retirement_clears_only_current_projection_and_keeps_history(
    db_session, monkeypatch
):
    emitted: list[object] = []
    monkeypatch.setattr(
        "app.services.network.reconcile.lifecycle.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    ont = OntUnit(serial_number=f"RETIRE-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.failed,
        suffix="retire",
    )
    event = OntProvisioningEvent(
        ont_unit_id=ont.id,
        assignment_id=assignment.id,
        configuration_head_id=head.id,
        configuration_revision=revision.revision,
        operation_id=operation.id,
        step_name="ont_service_configuration",
        action="configuration_phase_changed",
        status=OntProvisioningEventStatus.failed,
        message="historical failure",
    )
    db_session.add(event)
    ont.sync_status = OntSyncStatus.out_of_sync
    ont.last_error = "current assignment failed"
    ont.reconcile_assignment_id = assignment.id
    ont.reconcile_configuration_head_id = head.id
    ont.reconcile_desired_revision = 1
    ont.reconcile_operation_id = operation.id
    db_session.flush()

    outcome = retire_ont_reconcile_projection_for_inventory(
        db_session,
        RetireOntReconcileProjectionForInventory(
            ont_unit_id=ont.id,
            assignment_ids=(assignment.id,),
            actor="test:inventory",
            reason="returned_to_inventory",
        ),
    )

    assert outcome.retired_head_ids == (head.id,)
    assert head.phase is OntServiceConfigurationPhase.retired
    assert revision.phase is OntServiceConfigurationPhase.retired
    assert ont.sync_status is OntSyncStatus.synced
    assert ont.last_error is None
    assert ont.reconcile_assignment_id is None
    assert db_session.get(OntProvisioningEvent, event.id) is event
    assert emitted


def test_current_projection_excludes_legacy_failure(db_session):
    ont = OntUnit(serial_number=f"REUSE-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    new_assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(new_assignment)
    db_session.flush()
    new_head, new_revision, new_operation = _lifecycle(
        db_session,
        ont,
        new_assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="new",
    )
    db_session.add_all(
        (
            OntProvisioningEvent(
                ont_unit_id=ont.id,
                assignment_id=None,
                configuration_head_id=None,
                configuration_revision=None,
                operation_id=None,
                step_name="ont_service_configuration",
                action="configuration_phase_changed",
                status=OntProvisioningEventStatus.failed,
                message="old assignment failure",
            ),
            OntProvisioningEvent(
                ont_unit_id=ont.id,
                assignment_id=new_assignment.id,
                configuration_head_id=new_head.id,
                configuration_revision=new_revision.revision,
                operation_id=new_operation.id,
                step_name="ont_service_configuration",
                action="configuration_phase_changed",
                status=OntProvisioningEventStatus.waiting,
                message="current assignment queued",
            ),
        )
    )
    db_session.flush()

    projection = get_ont_service_configuration_projection(
        db_session, ont_unit_id=ont.id
    )

    assert projection.assignment_id == new_assignment.id
    assert projection.configuration_head_id == new_head.id
    assert projection.phase is OntServiceConfigurationPhase.queued
    assert [item.message for item in projection.current_events] == [
        "current assignment queued"
    ]
    assert "old assignment failure" in {
        item.message for item in projection.historical_events
    }
    assert projection.failure_message is None


def test_configure_without_active_assignment_fails_before_mutation(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"UNASSIGNED-{uuid.uuid4().hex[:8]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    ont_id = ont.id
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.ont_service_configuration._command_fingerprint",
        lambda _command: "f" * 64,
    )
    command_id = uuid.uuid4()

    with pytest.raises(DomainError) as exc_info:
        configure_ont_service(
            db_session,
            ConfigureOntServiceCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor="test:operator",
                    scope="network:ont:write",
                    reason="test unassigned refusal",
                    idempotency_key="test-unassigned",
                ),
                ont_unit_id=ont_id,
                permission_granted=True,
                section=OntConfigurationSection.wan,
                change=WanConfigurationChange(
                    mode="pppoe",
                    ip_protocol="ipv4",
                    static_ip=None,
                    static_subnet=None,
                    static_gateway=None,
                    static_dns=None,
                ),
            ),
        )

    assert exc_info.value.code.endswith(".active_assignment_required")
    assert db_session.scalar(select(func.count(OntServiceConfigurationHead.id))) == 0
    assert (
        db_session.scalar(
            select(func.count(NetworkOperation.id)).where(
                NetworkOperation.operation_type
                == NetworkOperationType.ont_service_config
            )
        )
        == 0
    )


def test_stale_worker_refuses_released_assignment_before_device_call(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"STALE-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="stale",
    )
    assignment.active = False
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    revision_number = revision.revision
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.reconcile.core.reconcile_ont",
        lambda *_args, **_kwargs: pytest.fail("stale worker contacted device"),
    )
    command_id = uuid.uuid4()

    outcome = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="test stale worker refusal",
                command_id=command_id,
                correlation_id=operation_id,
                idempotency_key="stale-worker",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=revision_number,
        ),
    )

    assert outcome.stale is True
    assert outcome.executed is False
    assert outcome.phase is OntServiceConfigurationPhase.superseded
    assert (
        db_session.get(NetworkOperation, operation_id).status
        is NetworkOperationStatus.canceled
    )


def test_readback_pending_is_not_verified_until_exact_revision_readback_succeeds(
    db_session, monkeypatch
):
    ont = OntUnit(serial_number=f"READBACK-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="readback",
    )
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": 1,
    }
    ont_id = ont.id
    operation_id = operation.id
    head_id = head.id
    db_session.commit()
    calls: list[dict] = []

    def pending_reconcile(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            drift_after=("wan.mode",),
            failure=SimpleNamespace(
                reason="verification_mismatch",
                message="Device write queued; waiting for a fresh observation.",
                evidence={"readback_pending": True},
            ),
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.reconcile_ont", pending_reconcile
    )
    first_command_id = uuid.uuid4()
    pending = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="initial delivery",
                command_id=first_command_id,
                correlation_id=operation_id,
                idempotency_key="readback-initial",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=1,
        ),
    )

    assert pending.phase is OntServiceConfigurationPhase.readback_pending
    assert calls[-1]["readback_only"] is False

    def verified_reconcile(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.reconcile_ont", verified_reconcile
    )
    verify_command_id = uuid.uuid4()
    verified = execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="fresh readback",
                command_id=verify_command_id,
                correlation_id=operation_id,
                idempotency_key="readback-attempt-one",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=1,
            verification_attempt=1,
        ),
    )

    assert verified.phase is OntServiceConfigurationPhase.verified
    assert calls[-1]["readback_only"] is True
    exact_revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head_id,
            OntServiceConfigurationRevision.revision == 1,
        )
    )
    assert exact_revision is not None
    assert exact_revision.phase is OntServiceConfigurationPhase.verified
    assert exact_revision.verified_at is not None
    assert (
        db_session.get(NetworkOperation, operation_id).status
        is NetworkOperationStatus.succeeded
    )


def test_failed_current_revision_requires_explicit_retry_command(db_session):
    ont = OntUnit(serial_number=f"RETRY-{uuid.uuid4().hex[:10]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, old_operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.failed,
        suffix="retry",
    )
    old_operation.status = NetworkOperationStatus.failed
    ont_id = ont.id
    head_id = head.id
    revision_number = revision.revision
    old_operation_id = old_operation.id
    db_session.commit()
    command_id = uuid.uuid4()

    outcome = retry_ont_service_configuration(
        db_session,
        RetryOntServiceConfigurationCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="test:operator",
                scope="network:ont:write",
                reason="reviewed current revision repair",
                idempotency_key="retry-current-revision",
            ),
            ont_unit_id=ont_id,
            expected_head_id=head_id,
            expected_revision=revision_number,
        ),
    )

    assert outcome.revision == 1
    assert outcome.operation_id != old_operation_id
    assert outcome.phase is OntServiceConfigurationPhase.queued
    assert (
        db_session.get(OntServiceConfigurationHead, head_id).latest_operation_id
        == outcome.operation_id
    )
