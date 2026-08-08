from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select


def test_provisioning_execution_marks_waiting_result_as_waiting_operation(
    db_session,
    monkeypatch,
) -> None:
    from app.models.network import OntUnit
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationDispatch,
        NetworkOperationStatus,
        NetworkOperationType,
    )
    from app.services.network.ont_provisioning.result import StepResult
    from app.services.network.ont_provisioning_commands import (
        request_ont_provisioning,
    )
    from app.services.network.ont_provisioning_execution import (
        execute_ont_provisioning,
    )

    ont = OntUnit(serial_number="WAITING-PROVISION-TASK")
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    def fake_apply_authorization_baseline(db, ont_id, **kwargs):
        return StepResult(
            "authorization_baseline",
            True,
            "Authorization baseline applied; waiting for ACS bootstrap verification.",
            123,
            waiting=True,
            data={"waiting_reason": "acs_bootstrap_verify"},
        )

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.apply_authorization_baseline",
        fake_apply_authorization_baseline,
    )
    command = request_ont_provisioning(
        db_session,
        str(ont.id),
        initiated_by="admin",
    )
    assert command.accepted is True

    result = execute_ont_provisioning(
        db_session,
        ont_id=str(ont.id),
        dry_run=False,
        initiated_by="admin",
        correlation_key=f"provision:{ont.id}",
        bulk_run_id=None,
        bulk_item_id=None,
        allow_low_optical_margin=False,
        operation_id=command.operation_id,
    )

    assert result["success"] is True
    assert result["waiting"] is True
    assert result["operation_id"]

    op = db_session.scalars(
        select(NetworkOperation).where(
            NetworkOperation.operation_type == NetworkOperationType.ont_provision,
            NetworkOperation.target_id == ont.id,
        )
    ).one()
    assert op.status == NetworkOperationStatus.waiting
    assert op.waiting_reason == "acs_bootstrap_verify"
    assert op.output_payload["waiting"] is True
    assert op.output_payload["duration_ms"] == 123
    assert op.output_payload["follow_up_queued"] is True

    child = db_session.scalars(
        select(NetworkOperation).where(NetworkOperation.parent_id == op.id)
    ).one()
    assert child.status == NetworkOperationStatus.waiting
    child_dispatch = db_session.scalars(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == child.id
        )
    ).one()
    assert result["follow_up_dispatch_id"] == str(child_dispatch.id)


def test_bootstrap_confirmation_completes_parent_and_bulk_item(db_session):
    from app.models.network import (
        BulkProvisioningItem,
        BulkProvisioningItemStatus,
        BulkProvisioningRun,
        BulkProvisioningRunStatus,
        OntUnit,
    )
    from app.models.network_operation import (
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network.ont_provisioning_execution import sync_bootstrap_parent
    from app.services.network_operations import network_operations

    ont = OntUnit(serial_number="BOOTSTRAP-CONFIRMED")
    run = BulkProvisioningRun(
        status=BulkProvisioningRunStatus.running,
        correlation_key="bulk-confirmed",
        total_count=1,
    )
    db_session.add_all([ont, run])
    db_session.flush()
    item = BulkProvisioningItem(
        run_id=run.id,
        requested_ont_id=str(ont.id),
        ont_unit_id=ont.id,
        status=BulkProvisioningItemStatus.waiting,
        correlation_key=f"bulk-confirmed:ont:{ont.id}",
    )
    db_session.add(item)
    db_session.flush()

    parent = network_operations.start(
        db_session,
        NetworkOperationType.ont_provision,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"provision:{ont.id}",
        input_payload={"bulk_item_id": str(item.id)},
    )
    network_operations.mark_running(db_session, str(parent.id))
    child = network_operations.start(
        db_session,
        NetworkOperationType.tr069_bootstrap,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"tr069_bootstrap:{ont.id}",
        parent_id=str(parent.id),
    )
    network_operations.mark_running(db_session, str(child.id))
    network_operations.mark_succeeded(
        db_session,
        str(child.id),
        output_payload={"success": True},
    )

    sync_bootstrap_parent(
        db_session,
        operation_id=str(child.id),
        ont_id=str(ont.id),
        payload={"success": True, "waiting": False, "message": "confirmed"},
    )

    assert parent.status == NetworkOperationStatus.succeeded
    assert parent.output_payload["device_confirmation"]["success"] is True
    assert item.status == BulkProvisioningItemStatus.succeeded
    assert run.status == BulkProvisioningRunStatus.succeeded


def test_inform_apply_completes_waiting_bootstrap_parent(db_session):
    from app.models.network import OntUnit
    from app.models.network_operation import (
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network.ont_provisioning.result import StepResult
    from app.services.network.ont_provisioning_execution import (
        complete_waiting_bootstrap_after_inform,
    )
    from app.services.network_operations import network_operations

    ont = OntUnit(serial_number="BOOTSTRAP-INFORM-CONFIRMED")
    db_session.add(ont)
    db_session.flush()
    parent = network_operations.start(
        db_session,
        NetworkOperationType.ont_provision,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"provision:{ont.id}",
    )
    network_operations.mark_running(db_session, str(parent.id))
    child = network_operations.start(
        db_session,
        NetworkOperationType.tr069_bootstrap,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"tr069_bootstrap:{ont.id}",
        parent_id=str(parent.id),
    )
    network_operations.mark_running(db_session, str(child.id))
    network_operations.mark_waiting(db_session, str(child.id), "next_inform")
    network_operations.update_parent_status(db_session, str(parent.id))

    completed = complete_waiting_bootstrap_after_inform(
        db_session,
        ont_id=str(ont.id),
        result=StepResult(
            "apply_saved_service_config",
            True,
            "Saved ONT service config applied.",
            duration_ms=123,
        ),
        reason="stale_inform_reconnect",
    )

    assert completed is True
    assert child.status == NetworkOperationStatus.succeeded
    assert parent.status == NetworkOperationStatus.succeeded
    assert parent.output_payload["device_confirmation"]["confirmation_source"] == (
        "stale_inform_reconnect"
    )


def test_admin_provision_route_queues_device_write(db_session, monkeypatch):
    from app.models.network import OntUnit
    from app.web.admin import network_onts_provisioning

    ont = OntUnit(serial_number="ADMIN-PROVISION-COMMAND")
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        network_onts_provisioning,
        "can_manage_ont_from_request",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "actor_label",
        lambda request: "admin",
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "log_network_action_result",
        lambda *args, **kwargs: None,
    )
    response = network_onts_provisioning.provision_ont_direct(
        SimpleNamespace(headers={}),
        str(ont.id),
        dry_run=False,
        async_execution=False,
        db=db_session,
    )

    payload = json.loads(response.body)
    assert response.status_code == 202
    assert payload["waiting"] is True
    assert payload["data"]["operation_id"]
    assert payload["data"]["dispatch_id"]


def test_admin_provision_browser_redirects_to_operation_progress(
    db_session, monkeypatch
):
    from urllib.parse import parse_qs, urlparse

    from app.models.network import OntUnit
    from app.web.admin import network_onts_provisioning

    ont = OntUnit(serial_number="ADMIN-PROVISION-BROWSER")
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        network_onts_provisioning,
        "can_manage_ont_from_request",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "actor_label",
        lambda request: "admin",
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "log_network_action_result",
        lambda *args, **kwargs: None,
    )

    response = network_onts_provisioning.provision_ont_direct(
        SimpleNamespace(headers={"accept": "text/html,application/xhtml+xml"}),
        str(ont.id),
        dry_run=False,
        async_execution=True,
        db=db_session,
    )

    location = response.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert response.status_code == 303
    assert parsed.path == f"/admin/network/onts/{ont.id}/provision"
    assert query["operation_id"][0]
    assert query["status"] == ["info"]
    assert query["message"] == [
        "ONT provisioning queued; waiting for device confirmation."
    ]
