"""Tracked Huawei ONT firmware control-plane tests."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from app.models.network import OntFirmwareImage, OntUnit
from app.models.network_operation import (
    NetworkOperationDispatch,
    NetworkOperationDispatchStatus,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network.ont_firmware import request_firmware_upgrade
from app.services.network_operations import network_operations
from app.services.queue_adapter import QueueDispatchResult


def _inventory(db_session):
    ont = OntUnit(
        serial_number="HWTC-FW-001",
        vendor="Huawei",
        model="EG8145V5",
        firmware_version="V1R1",
        software_version="V1R1",
        desired_config={},
        is_active=True,
    )
    image = OntFirmwareImage(
        vendor="Huawei",
        model="EG8145V5",
        version="V1R2",
        file_url="https://firmware.example/eg8145v5.bin",
        filename="eg8145v5.bin",
        checksum="sha256:abc",
        is_active=True,
    )
    db_session.add_all([ont, image])
    db_session.commit()
    return ont, image


def _task_session(db_session):
    @contextmanager
    def session():
        yield db_session

    return session


def test_request_stages_operation_without_claiming_device_success(db_session):
    ont, image = _inventory(db_session)

    result = request_firmware_upgrade(db_session, str(ont.id), str(image.id))

    assert result.success is True
    assert result.waiting is True
    assert result.data["verified"] is False
    operation = network_operations.get(db_session, result.data["operation_id"])
    assert operation.status == NetworkOperationStatus.pending
    assert operation.input_payload["target_version"] == "V1R2"
    dispatch = db_session.get(
        NetworkOperationDispatch,
        result.data["dispatch_id"],
    )
    assert dispatch is not None
    assert dispatch.operation_id == operation.id
    assert dispatch.status == NetworkOperationDispatchStatus.pending
    assert dispatch.args_payload == [str(ont.id), str(image.id), str(operation.id)]
    db_session.refresh(ont)
    assert ont.firmware_version == "V1R1"


def test_request_rejects_cross_model_image(db_session):
    ont, image = _inventory(db_session)
    image.model = "HG8546M"
    db_session.commit()

    result = request_firmware_upgrade(db_session, str(ont.id), str(image.id))

    assert result.success is False
    assert "does not match" in result.message


def test_apply_waits_for_readback_and_does_not_update_inventory(
    db_session, monkeypatch
):
    from app.tasks.ont_firmware import apply_huawei_ont_firmware

    ont, image = _inventory(db_session)
    operation = network_operations.start(
        db_session,
        NetworkOperationType.ont_firmware_upgrade,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"ont_firmware_upgrade:{ont.id}",
        input_payload={"firmware_image_id": str(image.id)},
    )
    db_session.commit()

    calls = {"downloads": 0}

    def _download(*args, **kwargs):
        calls["downloads"] += 1
        return {"_id": "acs-task-1"}

    client = SimpleNamespace(download_and_wait=_download)
    monkeypatch.setattr(
        "app.services.network.ont_action_common.get_ont_client_or_error",
        lambda db, ont_id: ((ont, client, "acs-device-1"), None),
    )
    monkeypatch.setattr(
        "app.tasks.ont_firmware.enqueue_task",
        lambda *args, **kwargs: QueueDispatchResult(queued=True, task_id="verify-1"),
    )
    monkeypatch.setattr(
        "app.tasks.ont_firmware.db_session_adapter.session",
        _task_session(db_session),
    )

    result = apply_huawei_ont_firmware(str(ont.id), str(image.id), str(operation.id))

    assert result["waiting"] is True
    db_session.expire_all()
    operation = network_operations.get(db_session, str(operation.id))
    assert operation.status == NetworkOperationStatus.waiting
    assert operation.output_payload["verified"] is False
    assert db_session.get(OntUnit, ont.id).firmware_version == "V1R1"

    replay = apply_huawei_ont_firmware(str(ont.id), str(image.id), str(operation.id))
    assert replay["recovered"] is True
    assert calls["downloads"] == 1


def test_verify_commits_inventory_only_after_matching_acs_version(
    db_session, monkeypatch
):
    from app.services.network.reconcile.readers import ReadResult
    from app.tasks.ont_firmware import verify_huawei_ont_firmware

    ont, image = _inventory(db_session)
    operation = network_operations.start(
        db_session,
        NetworkOperationType.ont_firmware_upgrade,
        NetworkOperationTargetType.ont,
        str(ont.id),
        correlation_key=f"ont_firmware_upgrade:{ont.id}",
        input_payload={"firmware_image_id": str(image.id)},
    )
    network_operations.mark_waiting(db_session, str(operation.id), "rebooting")
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_action_common.get_ont_client_or_error",
        lambda db, ont_id: ((ont, object(), "acs-device-1"), None),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.adapters.desired_from_ont_unit",
        lambda db, unit: object(),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.read_acs_state",
        lambda client, desired: ReadResult(
            status="present",
            observed=SimpleNamespace(acs_observed_software_version="V1R2"),
            error=None,
        ),
    )
    monkeypatch.setattr(
        "app.tasks.ont_firmware.db_session_adapter.session",
        _task_session(db_session),
    )

    result = verify_huawei_ont_firmware(
        str(ont.id), str(image.id), str(operation.id), 0
    )

    assert result["verified"] is True
    db_session.expire_all()
    operation = network_operations.get(db_session, str(operation.id))
    assert operation.status == NetworkOperationStatus.succeeded
    assert operation.output_payload["observed_version"] == "V1R2"
    assert db_session.get(OntUnit, ont.id).firmware_version == "V1R2"
