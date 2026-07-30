"""ONT authorization partial success: stored, rendered, and never blind-retried.

Release gate: an ONT that is genuinely authorized on the OLT must never be
reported to the operator as a plain failure, and a retry must never re-issue a
device write that already landed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntAuthorizationStatus,
    OntUnit,
    PonPort,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network import ont_authorization
from app.services.network.ont_provisioning_commands import (
    ont_authorization_correlation_key,
    request_ont_authorization,
)

HEADLINE = "OLT authorization succeeded; local inventory failed"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    """Minimal OLT protocol adapter that records every device call."""

    def __init__(self, calls: list[tuple], *, ont_id: int | None = 7):
        self.calls = calls
        self._ont_id = ont_id

    def authorize_ont(
        self,
        fsp,
        serial_number,
        *,
        line_profile_id=None,
        service_profile_id=None,
        description=None,
    ):
        self.calls.append(("authorize", fsp, serial_number))
        return SimpleNamespace(success=True, message="Authorized.", ont_id=self._ont_id)

    def find_ont_by_serial(self, serial_number):
        self.calls.append(("find", serial_number))
        return SimpleNamespace(success=True, message="ok", data={"registration": None})

    def deauthorize_ont(self, fsp, ont_id):
        self.calls.append(("deauthorize", fsp, ont_id))
        return SimpleNamespace(success=True, message="Removed.")


def _olt(db_session, label: str) -> OLTDevice:
    suffix = uuid.uuid4().hex[:8]
    olt = OLTDevice(name=f"{label}-{suffix}", is_active=True)
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    return olt


def _assigned_ont(
    db_session,
    *,
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
) -> OntUnit:
    pon = PonPort(olt_id=olt.id, name=fsp, is_active=True)
    ont = OntUnit(
        serial_number=serial_number,
        olt_device_id=olt.id,
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


def _patch_device_stack(monkeypatch, adapter) -> None:
    monkeypatch.setattr(
        ont_authorization,
        "_validate_authorization_dependencies",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda _olt: adapter,
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution."
        "resolve_authorization_profiles_from_import",
        lambda db, olt, *, equipment_id=None: (
            True,
            "Using OLT authorization profiles.",
            SimpleNamespace(
                line_profile_id=10,
                service_profile_id=20,
                message="Using OLT authorization profiles.",
            ),
        ),
    )


def _record_prior_partial_authorization(
    db_session,
    *,
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int = 7,
    status: NetworkOperationStatus = NetworkOperationStatus.warning,
) -> NetworkOperation:
    """Persist a terminal attempt whose OLT write landed but projection failed."""
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_authorize,
        target_type=NetworkOperationTargetType.olt,
        target_id=olt.id,
        status=status,
        correlation_key=ont_authorization_correlation_key(
            olt_id=str(olt.id),
            fsp=fsp,
            serial_number=serial_number,
        ),
        input_payload={"olt_id": str(olt.id), "fsp": fsp, "serial": serial_number},
        output_payload={
            "success": False,
            "completed_authorization": True,
            "partial_success": True,
            "local_inventory_failed": True,
            "ont_id_on_olt": ont_id_on_olt,
            "device_message": "Authorized.",
            "message": f"{HEADLINE}: topology conflict.",
        },
        initiated_by="admin",
    )
    db_session.add(operation)
    db_session.commit()
    return operation


# ---------------------------------------------------------------------------
# Gate 1 + 2: the partial outcome is stored and rendered distinctly
# ---------------------------------------------------------------------------


def test_partial_authorization_is_stored_on_the_operation_and_rendered_distinctly(
    db_session,
    monkeypatch,
):
    """The tracked operation records "OLT ok, local failed" and the UI says so."""
    from app.services import web_network_operations
    from app.services.network.ont_provisioning_execution import (
        execute_ont_authorization,
    )

    olt = _olt(db_session, "OLT-Partial-Store")
    ont = _assigned_ont(
        db_session,
        olt=olt,
        fsp="0/1/6",
        serial_number="HWTCSTORE0001",
    )
    calls: list[tuple] = []
    _patch_device_stack(monkeypatch, _RecordingAdapter(calls))
    monkeypatch.setattr(
        ont_authorization,
        "record_topology_observation_for_authorized_ont",
        lambda *args, **kwargs: (
            False,
            "ONT topology needs reviewed identity repair: observed OLT/PON "
            "conflicts with canonical ONT topology.",
        ),
    )

    command = request_ont_authorization(
        db_session,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCSTORE0001",
        scoped_ont_id=str(ont.id),
        initiated_by="admin",
    )
    assert command.accepted is True

    payload = execute_ont_authorization(
        db_session,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCSTORE0001",
        operation_id=command.operation_id,
    )

    assert ("authorize", "0/1/6", "HWTCSTORE0001") in calls
    assert payload["completed_authorization"] is True
    assert payload["local_inventory_failed"] is True

    operation = db_session.get(NetworkOperation, uuid.UUID(command.operation_id))
    # Degraded success, not failure: the device write landed.
    assert operation.status == NetworkOperationStatus.warning
    stored = operation.output_payload
    assert stored["completed_authorization"] is True
    assert stored["partial_success"] is True
    assert stored["local_inventory_failed"] is True
    assert stored["message"].startswith(HEADLINE)
    # The device leg committed before the local projection is still there.
    assert stored["device_authorization"]["ont_id_on_olt"] == 7
    assert stored["device_authorization"]["fsp"] == "0/1/6"

    history = web_network_operations.build_operation_history(
        db_session, "ont", str(ont.id)
    )
    entry = next(item for item in history if item["id"] == command.operation_id)
    assert entry["local_inventory_failed"] is True
    assert entry["device_authorization_completed"] is True
    assert entry["partial_success_headline"] == HEADLINE
    assert entry["is_failed"] is False


def test_device_rejection_is_not_reported_as_a_local_inventory_failure(
    db_session,
    monkeypatch,
):
    """A real OLT rejection keeps its CLI evidence and never claims completion."""
    from app.services import web_network_operations
    from app.services.network.ont_provisioning_execution import (
        execute_ont_authorization,
    )

    olt = _olt(db_session, "OLT-Rejects")
    ont = _assigned_ont(
        db_session,
        olt=olt,
        fsp="0/1/6",
        serial_number="HWTCREJECT001",
    )
    cli_error = "OLT rejected command: Failure: Configuration conflict on port 0/1/6"

    class RejectingAdapter(_RecordingAdapter):
        def authorize_ont(self, fsp, serial_number, **kwargs):
            self.calls.append(("authorize", fsp, serial_number))
            return SimpleNamespace(success=False, message=cli_error, ont_id=None)

    _patch_device_stack(monkeypatch, RejectingAdapter([]))

    command = request_ont_authorization(
        db_session,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCREJECT001",
        scoped_ont_id=str(ont.id),
        initiated_by="admin",
    )
    payload = execute_ont_authorization(
        db_session,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCREJECT001",
        operation_id=command.operation_id,
    )

    assert payload["completed_authorization"] is False
    assert payload["local_inventory_failed"] is False
    assert payload["device_message"] == cli_error

    operation = db_session.get(NetworkOperation, uuid.UUID(command.operation_id))
    assert operation.status == NetworkOperationStatus.failed
    assert operation.output_payload["completed_authorization"] is False
    assert "device_authorization" not in operation.output_payload
    assert operation.error == cli_error

    history = web_network_operations.build_operation_history(
        db_session, "ont", str(ont.id)
    )
    entry = next(item for item in history if item["id"] == command.operation_id)
    assert entry["local_inventory_failed"] is False
    assert entry["partial_success_headline"] is None
    assert entry["device_message"] == cli_error


def test_landed_device_authorization_survives_a_worker_crash(db_session, monkeypatch):
    """Terminalizing a crashed dispatch must merge, not erase, device evidence."""
    from app.models.network_operation import (
        NetworkOperationDispatch,
        NetworkOperationDispatchStatus,
    )
    from app.services.network_operation_dispatch import fail_dispatch_execution

    olt = _olt(db_session, "OLT-Crash")
    ont = _assigned_ont(
        db_session,
        olt=olt,
        fsp="0/1/6",
        serial_number="HWTCCRASH0001",
    )
    command = request_ont_authorization(
        db_session,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCCRASH0001",
        scoped_ont_id=str(ont.id),
        initiated_by="admin",
    )
    assert command.accepted is True

    ont_authorization.record_device_authorization_landed(
        db_session,
        command.operation_id,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCCRASH0001",
        ont_id_on_olt=11,
        device_message="Authorized.",
    )

    dispatch = db_session.get(NetworkOperationDispatch, uuid.UUID(command.dispatch_id))
    dispatch.status = NetworkOperationDispatchStatus.acknowledged
    db_session.flush()

    fail_dispatch_execution(db_session, command.dispatch_id, "worker died")
    db_session.commit()

    operation = db_session.get(NetworkOperation, uuid.UUID(command.operation_id))
    assert operation.status == NetworkOperationStatus.failed
    assert operation.output_payload["completed_authorization"] is True
    assert operation.output_payload["device_authorization"]["ont_id_on_olt"] == 11


# ---------------------------------------------------------------------------
# Gate 3: retry repairs the projection, it never re-issues the device write
# ---------------------------------------------------------------------------


def test_retry_repairs_local_inventory_without_re_authorizing_the_device(
    db_session,
    monkeypatch,
):
    olt = _olt(db_session, "OLT-No-Blind-Retry")
    db_session.add(PonPort(olt_id=olt.id, name="0/1/6", is_active=True))
    db_session.commit()
    _record_prior_partial_authorization(
        db_session,
        olt=olt,
        fsp="0/1/6",
        serial_number="HWTCREPAIR001",
    )

    calls: list[tuple] = []
    _patch_device_stack(monkeypatch, _RecordingAdapter(calls))

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCREPAIR001",
    )

    assert calls == [], "retry must not touch the OLT after a landed authorization"
    assert result.success is True
    assert result.completed_authorization is True
    assert result.device_authorization_reused_from is not None
    assert result.ont_unit_id is not None
    assert [step.name for step in result.steps] == ["Repair Local ONT Inventory"]

    ont = db_session.get(OntUnit, result.ont_unit_id)
    assert ont.olt_device_id == olt.id
    assert ont.authorization_status == OntAuthorizationStatus.authorized
    assert ont.external_id is not None


def test_repair_that_fails_again_still_reports_the_partial_headline(
    db_session,
    monkeypatch,
):
    olt = _olt(db_session, "OLT-Repair-Fails")
    _record_prior_partial_authorization(
        db_session,
        olt=olt,
        fsp="0/1/6",
        serial_number="HWTCREPAIR002",
    )
    calls: list[tuple] = []
    _patch_device_stack(monkeypatch, _RecordingAdapter(calls))
    monkeypatch.setattr(
        ont_authorization,
        "record_topology_observation_for_authorized_ont",
        lambda *args, **kwargs: (False, "topology still conflicts"),
    )

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCREPAIR002",
    )

    assert calls == []
    assert result.success is False
    assert result.completed_authorization is True
    assert result.local_inventory_failed is True
    assert result.message.startswith(HEADLINE)


def test_force_reauthorize_is_the_only_way_to_re_issue_the_device_command(
    db_session,
    monkeypatch,
):
    """Reuse is a guard against blind retries, not against explicit operator intent."""
    olt = _olt(db_session, "OLT-Force")
    db_session.add(PonPort(olt_id=olt.id, name="0/1/6", is_active=True))
    db_session.commit()
    _record_prior_partial_authorization(
        db_session,
        olt=olt,
        fsp="0/1/6",
        serial_number="HWTCFORCE0001",
    )
    calls: list[tuple] = []
    _patch_device_stack(monkeypatch, _RecordingAdapter(calls))

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCFORCE0001",
        force_reauthorize=True,
    )

    assert ("authorize", "0/1/6", "HWTCFORCE0001") in calls
    assert result.completed_authorization is True
    assert result.device_authorization_reused_from is None


def test_a_revoked_local_authorization_disables_device_authorization_reuse(
    db_session,
    monkeypatch,
):
    olt = _olt(db_session, "OLT-Revoked")
    db_session.add(PonPort(olt_id=olt.id, name="0/1/6", is_active=True))
    db_session.add(
        OntUnit(
            serial_number="HWTCREVOKED01",
            olt_device_id=olt.id,
            is_active=False,
            authorization_status=OntAuthorizationStatus.deauthorized,
        )
    )
    db_session.commit()
    _record_prior_partial_authorization(
        db_session,
        olt=olt,
        fsp="0/1/6",
        serial_number="HWTCREVOKED01",
    )
    calls: list[tuple] = []
    _patch_device_stack(monkeypatch, _RecordingAdapter(calls))

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCREVOKED01",
    )

    assert ("authorize", "0/1/6", "HWTCREVOKED01") in calls
    assert result.device_authorization_reused_from is None


def test_a_previously_successful_authorization_is_not_treated_as_repairable(
    db_session,
    monkeypatch,
):
    olt = _olt(db_session, "OLT-Prior-Success")
    operation = _record_prior_partial_authorization(
        db_session,
        olt=olt,
        fsp="0/1/6",
        serial_number="HWTCPRIOROK01",
        status=NetworkOperationStatus.succeeded,
    )
    operation.output_payload = {
        **operation.output_payload,
        "success": True,
        "local_inventory_failed": False,
        "partial_success": False,
    }
    db_session.commit()

    assert (
        ont_authorization.find_completed_device_authorization(
            db_session,
            olt_id=str(olt.id),
            fsp="0/1/6",
            serial_number="HWTCPRIOROK01",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Gate 4: real errors stay distinguishable
# ---------------------------------------------------------------------------


def test_integrity_errors_are_not_flattened_into_the_generic_log_message(
    db_session,
    monkeypatch,
):
    olt = _olt(db_session, "OLT-Integrity")
    ont = OntUnit(serial_number="HWTCINTEG0001", olt_device_id=olt.id, is_active=True)
    db_session.add(ont)
    db_session.commit()

    def raise_integrity(*args, **kwargs):
        raise IntegrityError(
            "INSERT INTO ont_assignments",
            {},
            Exception("UNIQUE constraint failed: uq_ont_units_olt_serial_number"),
        )

    monkeypatch.setattr(
        "app.services.network.ont_assignment_alignment."
        "project_ont_topology_from_fsp_observation",
        raise_integrity,
    )

    ok, message = ont_authorization.record_topology_observation_for_authorized_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
        fsp="0/1/6",
    )

    assert ok is False
    assert "Check server logs." not in message
    assert "uq_ont_units_olt_serial_number" in message


def test_non_integrity_database_errors_keep_the_generic_message(
    db_session,
    monkeypatch,
):
    olt = _olt(db_session, "OLT-Generic-DB")
    ont = OntUnit(serial_number="HWTCGENDB0001", olt_device_id=olt.id, is_active=True)
    db_session.add(ont)
    db_session.commit()

    def raise_operational(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    monkeypatch.setattr(
        "app.services.network.ont_assignment_alignment."
        "project_ont_topology_from_fsp_observation",
        raise_operational,
    )

    ok, message = ont_authorization.record_topology_observation_for_authorized_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
        fsp="0/1/6",
    )

    assert ok is False
    assert message == "Failed to link ONT to PON port. Check server logs."


# ---------------------------------------------------------------------------
# ONT lookup defects
# ---------------------------------------------------------------------------


def test_serial_lookup_never_hijacks_an_ont_owned_by_another_olt(db_session):
    owner = _olt(db_session, "OLT-Owner")
    other = _olt(db_session, "OLT-Other")
    owned = OntUnit(
        serial_number="HWTC617CA105",
        olt_device_id=owner.id,
        is_active=True,
        external_id="0/1/1:3",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add(owned)
    db_session.commit()

    ont_id, message = ont_authorization.create_or_find_ont_for_authorized_serial(
        db_session,
        olt_id=str(other.id),
        fsp="0/2/2",
        serial_number="48575443617CA105",
        ont_id_on_olt=9,
    )
    db_session.commit()
    db_session.refresh(owned)

    assert ont_id is not None
    assert ont_id != str(owned.id)
    assert "Created ONT record" in message
    # The other OLT's ONT is untouched.
    assert owned.external_id == "0/1/1:3"
    assert owned.olt_device_id == owner.id
    created = db_session.get(OntUnit, ont_id)
    assert created.olt_device_id == other.id


def test_new_rows_are_scoped_to_the_authorizing_olt(db_session):
    olt = _olt(db_session, "OLT-Scoped-Create")

    ont_id, _message = ont_authorization.create_or_find_ont_for_authorized_serial(
        db_session,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCSCOPED001",
        ont_id_on_olt=4,
    )
    db_session.commit()

    created = db_session.get(OntUnit, ont_id)
    assert created.olt_device_id == olt.id

    # A second authorization for the same OLT/serial reuses the scoped row
    # rather than creating an unscoped duplicate.
    again_id, again_message = (
        ont_authorization.create_or_find_ont_for_authorized_serial(
            db_session,
            olt_id=str(olt.id),
            fsp="0/1/6",
            serial_number="HWTCSCOPED001",
            ont_id_on_olt=4,
        )
    )
    assert again_id == ont_id
    assert "Using existing ONT record" in again_message
    rows = db_session.scalars(
        select(OntUnit).where(OntUnit.serial_number == "HWTCSCOPED001")
    ).all()
    assert len(rows) == 1


def test_unclaimed_legacy_rows_are_adopted_by_the_authorizing_olt(db_session):
    olt = _olt(db_session, "OLT-Adopt")
    legacy = OntUnit(serial_number="HWTCLEGACY001", is_active=True)
    db_session.add_all(
        [
            legacy,
            PonPort(olt_id=olt.id, name="0/1/6", is_active=True),
        ]
    )
    db_session.commit()

    ont_id, message = ont_authorization.create_or_find_ont_for_authorized_serial(
        db_session,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCLEGACY001",
        ont_id_on_olt=5,
    )
    db_session.commit()
    db_session.refresh(legacy)

    assert ont_id == str(legacy.id)
    assert "Using existing ONT record" in message
    assert legacy.olt_device_id == olt.id


def test_illegal_authorization_transition_leaves_the_status_untouched():
    """The strict guard rejects before assigning; nothing is silently forced.

    Deliberately session-free: the invariant is about the guard itself, so it
    is proven on a transient instance with no flush and no rollback.
    """
    from app.services.network import ont_status

    ont = OntUnit(
        serial_number="HWTCILLEGAL00",
        authorization_status=OntAuthorizationStatus.failed,
    )
    original_transitions = dict(ont_status._AUTHORIZATION_TRANSITIONS)
    ont_status._AUTHORIZATION_TRANSITIONS[OntAuthorizationStatus.failed] = {
        OntAuthorizationStatus.pending
    }
    try:
        with pytest.raises(ValueError, match="Illegal ONT authorization"):
            ont_status.set_authorization_status(ont, OntAuthorizationStatus.authorized)
    finally:
        ont_status._AUTHORIZATION_TRANSITIONS.clear()
        ont_status._AUTHORIZATION_TRANSITIONS.update(original_transitions)

    assert ont.authorization_status is OntAuthorizationStatus.failed


def test_illegal_authorization_transitions_fail_the_projection_instead_of_forcing(
    db_session,
    monkeypatch,
):
    """A rejected transition is a reported local failure, not a silent override.

    Asserts only on the returned tuple: this path ends in the owner's own
    ``db.rollback()``, and the ``db_session`` fixture binds the session to a
    plain outer transaction (``join_transaction_mode`` resolves to
    ``rollback_only``), so any post-call re-read of test data is unavailable by
    construction. Same shape as the IntegrityError tests above.
    """
    from app.services.network import ont_status

    olt = _olt(db_session, "OLT-Illegal-Transition")
    ont = OntUnit(
        serial_number="HWTCILLEGAL01",
        olt_device_id=olt.id,
        is_active=True,
        authorization_status=OntAuthorizationStatus.failed,
    )
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setitem(
        ont_status._AUTHORIZATION_TRANSITIONS,
        OntAuthorizationStatus.failed,
        {OntAuthorizationStatus.pending},
    )

    ont_id, message = ont_authorization.create_or_find_ont_for_authorized_serial(
        db_session,
        olt_id=str(olt.id),
        fsp="0/1/6",
        serial_number="HWTCILLEGAL01",
        ont_id_on_olt=6,
    )

    # Under the previous ``strict=False`` call this returned the row id with the
    # status forced to authorized and only a log line to show for it.
    assert ont_id is None
    assert "rejected the status change" in message
    assert (
        "Illegal ONT authorization status transition: failed -> authorized" in message
    )


def test_missing_olt_blocks_unscoped_local_inventory_creation(db_session):
    ont_id, message = ont_authorization.create_or_find_ont_for_authorized_serial(
        db_session,
        olt_id=str(uuid.uuid4()),
        fsp="0/1/6",
        serial_number="HWTCNOOLT0001",
        ont_id_on_olt=1,
    )

    assert ont_id is None
    assert "not found for local ONT inventory" in message


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"completed_authorization": False},
        {"completed_authorization": True, "success": True},
    ],
)
def test_reuse_requires_a_recorded_unprojected_device_authorization(
    db_session,
    payload,
):
    olt = _olt(db_session, "OLT-Reuse-Guard")
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_authorize,
        target_type=NetworkOperationTargetType.olt,
        target_id=olt.id,
        status=NetworkOperationStatus.failed,
        correlation_key=ont_authorization_correlation_key(
            olt_id=str(olt.id),
            fsp="0/1/6",
            serial_number="HWTCGUARD0001",
        ),
        output_payload=payload,
        initiated_by="admin",
    )
    db_session.add(operation)
    db_session.commit()

    assert (
        ont_authorization.find_completed_device_authorization(
            db_session,
            olt_id=str(olt.id),
            fsp="0/1/6",
            serial_number="HWTCGUARD0001",
        )
        is None
    )
