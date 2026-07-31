"""Behavior gates for explicit assignment-free ONT commissioning."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntProvisioningStatus,
    OntUnit,
    PonPort,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.ont_autofind import OltAutofindCandidate
from app.models.ont_commissioning import (
    OntCommissioningIntent,
    OntCommissioningState,
)
from app.services.domain_errors import DomainError
from app.services.network.olt_batched_mgmt import (
    BatchedMgmtSpec,
    build_management_command_batch,
)
from app.services.network.olt_protocol_adapters import OltConnectionConfig
from app.services.network.ont_authorization_contracts import (
    OntFsp,
    OntSerialNumber,
    RegisterCommissioningOnt,
    RequestAssignedOntAuthorization,
)
from app.services.network.ont_commissioning import (
    RequestOntCommissioning,
    _exact_live_autofind_preflight,
    assignment_is_blocked_by_commissioning,
    cleanup_ont_commissioning,
    complete_commissioning_after_inform,
    execute_ont_commissioning,
    reconcile_ont_commissioning,
    record_external_write_reconciliation_required,
    request_ont_commissioning,
)
from app.services.network.ont_provisioning_commands import request_ont_authorization
from app.services.owner_commands import CommandContext


def _candidate(db_session, *, serial: str = "HWTC7D4607C3", fsp: str = "0/1/3"):
    olt_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    olt = OLTDevice(
        id=olt_id,
        name=f"commissioning-olt-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    candidate = OltAutofindCandidate(
        id=candidate_id,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial,
        is_active=True,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    db_session.add(candidate)
    db_session.commit()
    return (
        olt,
        candidate,
        SimpleNamespace(
            olt_id=olt_id,
            candidate_id=candidate_id,
            serial=serial,
            fsp=fsp,
        ),
    )


def test_detached_olt_config_access_does_not_reopen_transaction(db_session):
    olt, _candidate_row, _target = _candidate(db_session)
    config = OltConnectionConfig.from_model(olt)
    olt_id = olt.id
    db_session.commit()

    assert db_session.in_transaction() is False
    assert config.id == olt_id
    assert config.name.startswith("commissioning-olt-")
    assert db_session.in_transaction() is False


def _request(
    target,
    *,
    expected_fsp: str | None = None,
) -> RequestOntCommissioning:
    return RequestOntCommissioning(
        context=CommandContext.system(
            actor="noc.operator",
            scope="network:ont:commission",
            reason="test commissioning admission",
            idempotency_key=f"commission:{target.candidate_id}",
        ),
        candidate_id=target.candidate_id,
        expected_olt_id=target.olt_id,
        expected_fsp=OntFsp.parse(expected_fsp or target.fsp),
        expected_serial=OntSerialNumber.parse(target.serial),
        reason="Pre-stage ACS management for field installation",
        reference="WO-100013286",
    )


def test_commissioning_admission_atomically_stages_intent_operation_and_dispatch(
    db_session,
):
    olt, candidate, target = _candidate(db_session)
    request = _request(target)

    outcome = request_ont_commissioning(db_session, request)

    intent = db_session.get(OntCommissioningIntent, outcome.intent_id)
    operation = db_session.get(NetworkOperation, outcome.operation_id)
    assert intent is not None
    assert intent.state is OntCommissioningState.commissioning
    assert intent.olt_id == target.olt_id
    assert intent.fsp == "0/1/3"
    assert intent.canonical_serial == "HWTC7D4607C3"
    assert intent.reference == "WO-100013286"
    assert (
        timedelta(hours=23)
        < intent.expires_at - intent.created_at
        <= timedelta(hours=24)
    )
    assert operation is not None
    assert operation.operation_type is NetworkOperationType.ont_commission
    assert operation.input_payload["management_only"] is True
    assert str(intent.latest_operation_id) == str(operation.id)
    assert any(dispatch.id == outcome.dispatch_id for dispatch in operation.dispatches)


def test_commissioning_duplicate_replays_the_active_intent(db_session):
    _olt, _candidate_row, target = _candidate(db_session)
    request = _request(target)
    first = request_ont_commissioning(db_session, request)

    second = request_ont_commissioning(db_session, request)

    assert second.duplicate is True
    assert second.intent_id == first.intent_id
    assert second.operation_id == first.operation_id
    assert second.dispatch_id == first.dispatch_id


def test_commissioning_rejects_stale_exact_target(db_session):
    _olt, _candidate_row, target = _candidate(db_session)
    request = _request(target, expected_fsp="0/1/9")

    with pytest.raises(DomainError) as exc_info:
        request_ont_commissioning(db_session, request)

    assert exc_info.value.code == "network.ont_commissioning.stale_target"
    assert db_session.query(OntCommissioningIntent).count() == 0


def test_normal_authorization_rejects_assignment_free_request(db_session):
    _olt, candidate, target = _candidate(db_session)
    ont = OntUnit(
        serial_number=target.serial,
        olt_device_id=target.olt_id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.commit()
    candidate.ont_unit_id = ont.id
    db_session.commit()

    result = request_ont_authorization(
        db_session,
        RequestAssignedOntAuthorization.from_transport(
            context=CommandContext.system(
                actor="noc.operator",
                scope="network:ont:authorize",
                reason="test assignment-free rejection",
            ),
            ont_id=ont.id,
            olt_id=target.olt_id,
            fsp=target.fsp,
            serial_number=target.serial,
        ),
    )

    assert result.accepted is False
    assert "active assignment" in result.message
    assert "Commission ONT" in result.message


def test_management_batch_has_no_customer_service_commands():
    spec = BatchedMgmtSpec(
        fsp="0/1/3",
        ont_id_on_olt=7,
        mgmt_vlan_tag=450,
        mgmt_gem_index=2,
        ip_mode="dhcp",
        tr069_profile_id=4,
        internet_config_ip_index=None,
        wan_config_profile_id=None,
    )

    descriptions = [
        description for _command, description in build_management_command_batch(spec)
    ]

    assert "create_mgmt_service_port" in descriptions
    assert "configure_iphost" in descriptions
    assert "bind_tr069" in descriptions
    assert "activate_internet_config" not in descriptions
    assert "configure_wan" not in descriptions


def test_live_autofind_preflight_requires_exact_fsp_and_serial(
    db_session,
    monkeypatch,
):
    olt, _candidate_row, target = _candidate(db_session)
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        olt_id=target.olt_id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.commissioning,
        reason="test",
        requested_by="noc.operator",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.autofind.query_ont_autofind",
        lambda _olt, port=None: (
            True,
            "found",
            [
                SimpleNamespace(
                    fsp="0/1/9",
                    serial_number=target.serial,
                    serial_hex="",
                )
            ],
        ),
    )

    olt_config = OltConnectionConfig.from_model(olt)
    ok, message = _exact_live_autofind_preflight(
        canonical_serial_value=intent.canonical_serial,
        fsp=intent.fsp,
        olt_config=olt_config,
    )

    assert ok is False
    assert "no OLT write was attempted" in message


def test_assignment_is_blocked_until_commissioning_is_management_ready(db_session):
    _olt, _candidate_row, target = _candidate(db_session)
    ont = OntUnit(
        serial_number=target.serial,
        olt_device_id=target.olt_id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        ont_unit_id=ont.id,
        olt_id=target.olt_id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.awaiting_acs,
        reason="test",
        requested_by="noc.operator",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(intent)
    db_session.commit()

    assert (
        assignment_is_blocked_by_commissioning(db_session, ont_unit_id=ont.id)
        is not None
    )

    intent.state = OntCommissioningState.management_ready
    db_session.commit()

    assert (
        assignment_is_blocked_by_commissioning(db_session, ont_unit_id=ont.id) is None
    )


def test_assignment_is_blocked_by_serial_before_intent_projects_ont_id(db_session):
    _olt, _candidate_row, target = _candidate(db_session)
    ont = OntUnit(
        serial_number=target.serial,
        olt_device_id=target.olt_id,
        is_active=True,
    )
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        ont_unit_id=None,
        olt_id=target.olt_id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.authorizing,
        reason="test",
        requested_by="noc.operator",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add_all([ont, intent])
    db_session.commit()

    blocked = assignment_is_blocked_by_commissioning(
        db_session,
        ont_unit_id=ont.id,
    )

    assert blocked is not None
    assert blocked.id == intent.id


def test_tr069_inform_marks_commissioning_management_ready_without_assignment(
    db_session,
):
    _olt, _candidate_row, target = _candidate(db_session)
    ont = OntUnit(
        serial_number=target.serial,
        olt_device_id=target.olt_id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_commission,
        target_type=NetworkOperationTargetType.olt,
        target_id=target.olt_id,
        status=NetworkOperationStatus.waiting,
    )
    db_session.add(operation)
    db_session.flush()
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        ont_unit_id=ont.id,
        olt_id=target.olt_id,
        latest_operation_id=operation.id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.awaiting_acs,
        reason="test",
        requested_by="noc.operator",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        device_authorized_at=datetime.now(UTC),
    )
    db_session.add(intent)
    db_session.commit()

    completed = complete_commissioning_after_inform(
        db_session,
        ont_id=str(ont.id),
        reason="test_inform",
    )
    db_session.commit()

    stored = db_session.get(OntCommissioningIntent, intent.id)
    assert completed is True
    assert stored is not None
    assert stored.state is OntCommissioningState.management_ready
    assert stored.management_ready_at is not None
    assert db_session.get(NetworkOperation, operation.id).status is (
        NetworkOperationStatus.succeeded
    )


def test_reconciler_converts_management_ready_intent_to_assignment(db_session):
    _olt, _candidate_row, target = _candidate(db_session)
    pon = PonPort(olt_id=target.olt_id, name=target.fsp, is_active=True)
    ont = OntUnit(
        serial_number=target.serial,
        olt_device_id=target.olt_id,
        provisioning_status=OntProvisioningStatus.unprovisioned,
        is_active=True,
    )
    db_session.add_all([pon, ont])
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            active=True,
        )
    )
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        ont_unit_id=ont.id,
        olt_id=target.olt_id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.management_ready,
        reason="test",
        requested_by="noc.operator",
        expires_at=datetime.now(UTC) + timedelta(hours=12),
    )
    db_session.add(intent)
    db_session.commit()
    intent_id = intent.id
    db_session.commit()

    result = reconcile_ont_commissioning(
        db_session,
        context=CommandContext.system(
            actor="test-reconciler",
            scope="network:ont:commission",
            reason="test assignment conversion",
        ),
    )

    assert result.assigned == 1
    assert (
        db_session.get(OntCommissioningIntent, intent_id).state
        is OntCommissioningState.assigned
    )


def test_reconciler_redrives_recorded_partial_authorization_once(db_session):
    _olt, _candidate_row, target = _candidate(db_session, fsp="0/1/13")
    ont = OntUnit(
        serial_number=target.serial,
        olt_device_id=target.olt_id,
        board="0/1",
        port="13",
        external_id="1",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    prior = NetworkOperation(
        operation_type=NetworkOperationType.ont_commission,
        target_type=NetworkOperationTargetType.olt,
        target_id=target.olt_id,
        status=NetworkOperationStatus.failed,
        max_retries=3,
        input_payload={
            "intent_id": "pending",
            "olt_id": str(target.olt_id),
            "fsp": target.fsp,
            "serial_number": target.serial,
        },
    )
    db_session.add(prior)
    db_session.flush()
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        ont_unit_id=ont.id,
        olt_id=target.olt_id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.failed,
        reason="partial success",
        requested_by="noc.operator",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        device_authorized_at=datetime.now(UTC) - timedelta(minutes=5),
        latest_operation_id=prior.id,
        failure_code="external_write_reconciliation_required",
    )
    db_session.add(intent)
    db_session.flush()
    intent_id = intent.id
    prior_id = prior.id
    db_session.commit()

    reconcile_ont_commissioning(
        db_session,
        context=CommandContext.system(
            actor="test-reconciler",
            scope="network:ont:commission",
            reason="recover recorded partial authorization",
        ),
    )
    recovered = db_session.get(OntCommissioningIntent, intent_id)
    recovery_id = recovered.latest_operation_id
    assert recovered.state is OntCommissioningState.authorizing
    assert recovery_id != prior_id
    assert db_session.get(NetworkOperation, recovery_id).redrive_of_id == prior_id
    assert (
        db_session.query(NetworkOperationDispatch)
        .filter_by(operation_id=recovery_id)
        .count()
        == 1
    )
    db_session.commit()

    reconcile_ont_commissioning(
        db_session,
        context=CommandContext.system(
            actor="test-reconciler",
            scope="network:ont:commission",
            reason="replay partial authorization reconciliation",
        ),
    )
    assert db_session.get(OntCommissioningIntent, intent_id).latest_operation_id == (
        recovery_id
    )


def test_reconciler_expires_intent_without_device_write_or_cleanup(db_session):
    _olt, _candidate_row, target = _candidate(db_session)
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        olt_id=target.olt_id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.failed,
        reason="test",
        requested_by="noc.operator",
        created_at=datetime.now(UTC) - timedelta(hours=2),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        failure_code="live_autofind_mismatch",
        failure_message="No device write attempted.",
    )
    db_session.add(intent)
    db_session.commit()
    intent_id = intent.id
    db_session.commit()

    result = reconcile_ont_commissioning(
        db_session,
        context=CommandContext.system(
            actor="test-reconciler",
            scope="network:ont:commission",
            reason="test no-write expiry",
        ),
    )

    stored = db_session.get(OntCommissioningIntent, intent_id)
    assert result.expired_without_device_write == 1
    assert stored.state is OntCommissioningState.expired
    assert stored.cleanup_operation_id is None


def test_landed_authorization_without_local_target_requires_cleanup_review(
    db_session,
    monkeypatch,
):
    _olt, _candidate_row, target = _candidate(db_session)
    admission = request_ont_commissioning(db_session, _request(target))

    def exact_preflight_without_transaction(**_kwargs):
        assert db_session.in_transaction() is False
        return True, "exact"

    monkeypatch.setattr(
        "app.services.network.ont_commissioning._exact_live_autofind_preflight",
        exact_preflight_without_transaction,
    )
    authorization_commands: list[RegisterCommissioningOnt] = []

    def fail_local_inventory(_db, command):
        authorization_commands.append(command)
        return SimpleNamespace(
            success=False,
            message="OLT authorization succeeded; local inventory failed",
            ont_unit_id=None,
            ont_id_on_olt=7,
            completed_authorization=True,
            local_inventory_failed=True,
        )

    monkeypatch.setattr(
        "app.services.network.ont_authorization.register_ont_for_commissioning",
        fail_local_inventory,
    )

    result = execute_ont_commissioning(
        db_session,
        intent_id=str(admission.intent_id),
        operation_id=str(admission.operation_id),
    )

    assert result["failure_code"] == "local_inventory_failed"
    intent = db_session.get(OntCommissioningIntent, admission.intent_id)
    assert intent is not None
    assert intent.device_authorized_at is not None
    assert intent.ont_unit_id is None
    assert len(authorization_commands) == 1
    authorization_command = authorization_commands[0]
    assert authorization_command.operation_id == admission.operation_id
    assert authorization_command.intent_id == admission.intent_id
    assert authorization_command.target.olt_id == target.olt_id
    assert authorization_command.target.fsp.value == target.fsp
    assert authorization_command.target.serial_number.value == target.serial
    intent.created_at = datetime.now(UTC) - timedelta(hours=2)
    intent.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    reconciliation = reconcile_ont_commissioning(
        db_session,
        context=CommandContext.system(
            actor="test-reconciler",
            scope="network:ont:commission",
            reason="test landed-write cleanup safety",
        ),
    )

    stored = db_session.get(OntCommissioningIntent, admission.intent_id)
    assert reconciliation.expired_without_device_write == 0
    assert stored is not None
    assert stored.state is OntCommissioningState.cleanup_pending
    assert stored.failure_code == "cleanup_target_missing"
    assert stored.cleanup_operation_id is None


def test_reliability_handler_records_external_write_with_fresh_session(db_session):
    _olt, _candidate_row, target = _candidate(db_session)
    admission = request_ont_commissioning(db_session, _request(target))
    intent = db_session.get(OntCommissioningIntent, admission.intent_id)
    operation = db_session.get(NetworkOperation, admission.operation_id)
    assert intent is not None
    assert operation is not None
    intent.state = OntCommissioningState.authorizing
    intent.device_authorized_at = datetime.now(UTC)
    operation.status = NetworkOperationStatus.running
    db_session.commit()

    recorded = record_external_write_reconciliation_required(
        db_session,
        intent_id=str(admission.intent_id),
        operation_id=str(admission.operation_id),
    )

    stored_intent = db_session.get(OntCommissioningIntent, admission.intent_id)
    stored_operation = db_session.get(NetworkOperation, admission.operation_id)
    assert recorded is True
    assert stored_intent is not None
    assert stored_intent.state is OntCommissioningState.failed
    assert stored_intent.failure_code == "external_write_reconciliation_required"
    assert stored_operation is not None
    assert stored_operation.status is NetworkOperationStatus.failed
    assert stored_operation.output_payload["reconciliation_required"] is True


def test_cleanup_fails_closed_when_projected_fsp_drifted(db_session):
    _olt, _candidate_row, target = _candidate(db_session)
    ont = OntUnit(
        serial_number=target.serial,
        olt_device_id=target.olt_id,
        board="1",
        port="9",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_commission_cleanup,
        target_type=NetworkOperationTargetType.ont,
        target_id=ont.id,
        status=NetworkOperationStatus.pending,
    )
    db_session.add(operation)
    db_session.flush()
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        ont_unit_id=ont.id,
        olt_id=target.olt_id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.cleanup_pending,
        reason="test",
        requested_by="noc.operator",
        created_at=datetime.now(UTC) - timedelta(hours=2),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        device_authorized_at=datetime.now(UTC) - timedelta(hours=1),
        cleanup_operation_id=operation.id,
    )
    db_session.add(intent)
    db_session.commit()

    result = cleanup_ont_commissioning(
        db_session,
        intent_id=str(intent.id),
        operation_id=str(operation.id),
    )

    stored = db_session.get(OntCommissioningIntent, intent.id)
    assert result["failure_code"] == "cleanup_identity_mismatch"
    assert stored is not None
    assert stored.state is OntCommissioningState.cleanup_pending
    assert db_session.get(NetworkOperation, operation.id).status is (
        NetworkOperationStatus.failed
    )


def test_cleanup_normalizes_board_that_already_contains_frame_prefix(
    db_session, monkeypatch
):
    _olt, _candidate_row, target = _candidate(db_session, fsp="0/1/13")
    ont = OntUnit(
        serial_number=target.serial,
        olt_device_id=target.olt_id,
        board="0/1",
        port="13",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_commission_cleanup,
        target_type=NetworkOperationTargetType.ont,
        target_id=ont.id,
        status=NetworkOperationStatus.pending,
    )
    db_session.add(operation)
    db_session.flush()
    intent = OntCommissioningIntent(
        autofind_candidate_id=target.candidate_id,
        ont_unit_id=ont.id,
        olt_id=target.olt_id,
        canonical_serial=target.serial,
        fsp=target.fsp,
        state=OntCommissioningState.cleanup_pending,
        reason="test canonical FSP cleanup",
        requested_by="noc.operator",
        created_at=datetime.now(UTC) - timedelta(hours=2),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        device_authorized_at=datetime.now(UTC) - timedelta(hours=1),
        cleanup_operation_id=operation.id,
    )
    db_session.add(intent)
    db_session.flush()
    intent_id = intent.id
    operation_id = operation.id
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.ont_inventory.return_ont_to_inventory",
        lambda _db, _ont_id: SimpleNamespace(success=True, message="returned"),
    )

    result = cleanup_ont_commissioning(
        db_session,
        intent_id=str(intent_id),
        operation_id=str(operation_id),
    )

    assert result["success"] is True
    assert db_session.get(OntCommissioningIntent, intent_id).state is (
        OntCommissioningState.expired
    )
