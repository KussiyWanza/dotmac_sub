from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network.ont_authorization_contracts import (
    ExecuteAssignedOntAuthorization,
    RequestAssignedOntAuthorization,
)
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_provisioning_commands import (
    request_bootstrap_verification,
    request_ont_authorization,
    request_ont_provisioning,
    stage_bootstrap_attempt,
)
from app.services.network.ont_provisioning_execution import (
    execute_bootstrap_verification,
    execute_ont_authorization,
)
from app.services.owner_commands import CommandContext


class _SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return False


def _assigned_ont(db_session, olt: OLTDevice, *, fsp: str, serial: str) -> OntUnit:
    pon = PonPort(olt_id=olt.id, name=fsp, is_active=True)
    ont = OntUnit(
        serial_number=serial,
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        is_active=True,
    )
    db_session.add_all([pon, ont])
    db_session.flush()
    ont.pon_port_id = pon.id
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            active=True,
        )
    )
    db_session.commit()
    return ont


def test_authorization_command_stages_operation_and_typed_dispatch(db_session):
    olt = OLTDevice(name="Command OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    ont = _assigned_ont(
        db_session,
        olt,
        fsp="0/1/2",
        serial="HWTCOMMAND01",
    )

    result = request_ont_authorization(
        db_session,
        RequestAssignedOntAuthorization.from_transport(
            context=CommandContext.system(
                actor="network-admin",
                scope="network:ont:authorize",
                reason="test assigned authorization admission",
            ),
            ont_id=ont.id,
            olt_id=olt.id,
            fsp="0/1/2",
            serial_number="HWTCOMMAND01",
            force_reauthorize=True,
        ),
    )

    assert result.accepted is True
    operation = db_session.get(NetworkOperation, result.operation_id)
    dispatch = db_session.get(NetworkOperationDispatch, result.dispatch_id)
    assert operation is not None
    assert dispatch is not None
    assert operation.operation_type == NetworkOperationType.ont_authorize
    assert operation.target_type == NetworkOperationTargetType.ont
    assert str(operation.target_id) == str(ont.id)
    assert operation.status == NetworkOperationStatus.pending
    assert dispatch.command_name == "ont_authorize.v1"
    assert dispatch.args_payload == []
    assert dispatch.kwargs_payload == {
        "olt_id": str(olt.id),
        "fsp": "0/1/2",
        "serial_number": "HWTCOMMAND01",
        "force_reauthorize": True,
        "preset_id": None,
        "scoped_ont_id": str(ont.id),
        "initiated_by": "network-admin",
        "operation_id": str(operation.id),
    }


def test_authorization_execution_rechecks_exact_assignment_before_device_write(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="Execution Gate OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    ont = _assigned_ont(
        db_session,
        olt,
        fsp="0/1/2",
        serial="HWTCRACECHECK",
    )
    request = RequestAssignedOntAuthorization.from_transport(
        context=CommandContext.system(
            actor="network-admin",
            scope="network:ont:authorize",
            reason="test execution assignment race",
        ),
        ont_id=ont.id,
        olt_id=olt.id,
        fsp="0/1/2",
        serial_number="HWTCRACECHECK",
    )
    admission = request_ont_authorization(db_session, request)
    assert admission.operation_id is not None

    assignment = db_session.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
    ).one()
    assignment.active = False
    db_session.commit()
    device_called = False

    def unexpected_device_write(*_args, **_kwargs):
        nonlocal device_called
        device_called = True
        raise AssertionError("device authorization must not run")

    monkeypatch.setattr(
        "app.services.network.ont_authorization.authorize_and_provision_ont",
        unexpected_device_write,
    )
    outcome = execute_ont_authorization(
        db_session,
        ExecuteAssignedOntAuthorization(
            context=CommandContext.system(
                actor="network-admin",
                scope="network:ont:authorize",
                reason="execute test authorization race",
                command_id=admission.operation_id,
                correlation_id=admission.operation_id,
            ),
            operation_id=admission.operation_id,
            ont_id=request.ont_id,
            target=request.target,
        ),
    )

    assert outcome.authorization.success is False
    assert "active assignment" in outcome.message
    assert device_called is False
    operation = db_session.get(NetworkOperation, admission.operation_id)
    assert operation is not None
    assert operation.status is NetworkOperationStatus.failed


def test_provisioning_command_is_atomic_and_duplicate_safe(db_session):
    ont = OntUnit(serial_number="PROVISION-COMMAND")
    db_session.add(ont)
    db_session.commit()

    first = request_ont_provisioning(
        db_session,
        str(ont.id),
        initiated_by="network-admin",
    )
    replay = request_ont_provisioning(
        db_session,
        str(ont.id),
        initiated_by="network-admin",
    )

    assert first.accepted is True
    assert replay.accepted is True
    assert replay.duplicate is True
    assert replay.operation_id == first.operation_id
    assert replay.dispatch_id == first.dispatch_id
    assert db_session.query(NetworkOperation).count() == 1
    assert db_session.query(NetworkOperationDispatch).count() == 1


def test_bootstrap_retry_is_a_distinct_delayed_dispatch(db_session):
    ont = OntUnit(serial_number="BOOTSTRAP-RETRY-COMMAND")
    db_session.add(ont)
    db_session.commit()
    initial = request_bootstrap_verification(
        db_session,
        ont_id=str(ont.id),
        parent_operation_id=None,
        initiated_by="system",
    )
    operation = db_session.get(NetworkOperation, initial.operation_id)
    assert operation is not None

    before = datetime.now(UTC)
    retry = stage_bootstrap_attempt(
        db_session,
        operation,
        attempt=1,
        delay_seconds=30,
    )
    db_session.commit()

    assert retry.dispatch_key == "attempt:1"
    assert retry.args_payload == [str(ont.id), str(operation.id), 1]
    assert retry.next_attempt_at is not None
    retry_at = retry.next_attempt_at
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    assert retry_at >= before
    dispatches = db_session.scalars(
        select(NetworkOperationDispatch)
        .where(NetworkOperationDispatch.operation_id == operation.id)
        .order_by(NetworkOperationDispatch.dispatch_key)
    ).all()
    assert [dispatch.dispatch_key for dispatch in dispatches] == [
        "attempt:0",
        "attempt:1",
    ]


def test_waiting_bootstrap_execution_stages_retry_without_direct_publish(
    db_session,
    monkeypatch,
):
    ont = OntUnit(serial_number="BOOTSTRAP-WAITING-COMMAND")
    db_session.add(ont)
    db_session.commit()
    initial = request_bootstrap_verification(
        db_session,
        ont_id=str(ont.id),
        parent_operation_id=None,
        initiated_by="system",
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda *args, **kwargs: StepResult(
            "wait_tr069_bootstrap",
            False,
            "Waiting for ACS Inform.",
            waiting=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provisioning_execution._retry_jitter_random.uniform",
        lambda *_args: 0.0,
    )

    payload = execute_bootstrap_verification(
        db_session,
        ont_id=str(ont.id),
        operation_id=str(initial.operation_id),
        service_retry_count=0,
    )

    operation = db_session.get(NetworkOperation, initial.operation_id)
    assert operation is not None
    assert payload["waiting"] is True
    assert operation.status == NetworkOperationStatus.waiting
    retry = db_session.scalars(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == operation.id,
            NetworkOperationDispatch.dispatch_key == "attempt:1",
        )
    ).one()
    assert payload["retry_dispatch_id"] == str(retry.id)
    assert retry.args_payload == [str(ont.id), str(operation.id), 1]


def test_legacy_authorization_envelope_rehomes_without_device_execution(
    db_session,
    monkeypatch,
):
    from app.tasks import ont_provisioning as task_module

    olt = OLTDevice(name="Legacy Envelope OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    ont = _assigned_ont(
        db_session,
        olt,
        fsp="0/1/0",
        serial="HWTLEGACY01",
    )
    monkeypatch.setattr(
        task_module.db_session_adapter,
        "session",
        lambda: _SessionContext(db_session),
    )
    monkeypatch.setattr(
        "app.services.network.ont_authorization.authorize_and_provision_ont",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy envelope must not enter device code")
        ),
    )

    result = task_module.authorize_ont.run(
        str(olt.id),
        "0/1/0",
        "HWTLEGACY01",
        scoped_ont_id=str(ont.id),
        initiated_by="legacy-worker",
    )

    assert result["success"] is True
    assert result["waiting"] is True
    assert result["legacy_envelope_rehomed"] is True
    assert result["operation_id"]
    assert result["dispatch_id"]


def test_legacy_bootstrap_idempotency_does_not_suppress_durable_attempt():
    from app.tasks.tr069 import _bootstrap_wait_idempotency_key

    legacy = _bootstrap_wait_idempotency_key("ont-1", "operation-1", 0)
    durable = _bootstrap_wait_idempotency_key(
        "ont-1",
        "operation-1",
        0,
        _network_dispatch_id="dispatch-1",
    )

    assert legacy != durable
