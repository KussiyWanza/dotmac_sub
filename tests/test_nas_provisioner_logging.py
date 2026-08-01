from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.models.catalog import (
    ConnectionType,
    NasVendor,
    ProvisioningAction,
    ProvisioningLogStatus,
)
from app.services.nas.provisioner import DeviceProvisioner


def test_provision_user_logs_structured_lifecycle(monkeypatch, caplog):
    device_id = uuid4()
    template_id = uuid4()
    log_id = uuid4()
    device = SimpleNamespace(
        id=device_id,
        name="NAS-1",
        vendor=NasVendor.mikrotik,
        # DHCP, not PPPoE: a MikroTik PPPoE create_user is refused at the
        # local-secret boundary before any template runs, so this lifecycle
        # test uses a connection type the boundary has no opinion on.
        default_connection_type=ConnectionType.dhcp,
    )
    template = SimpleNamespace(
        id=template_id,
        timeout_seconds=30,
        execution_method="ssh",
    )
    log = SimpleNamespace(id=log_id)

    monkeypatch.setattr(
        "app.services.nas.devices.NasDevices.get",
        lambda *_args, **_kwargs: device,
    )
    monkeypatch.setattr(
        "app.services.nas.templates.ProvisioningTemplates.find_template",
        lambda *_args, **_kwargs: template,
    )
    monkeypatch.setattr(
        "app.services.nas.templates.ProvisioningTemplates.render",
        lambda *_args, **_kwargs: "/ip dhcp-server lease add address=10.0.0.2",
    )
    monkeypatch.setattr(
        "app.services.nas.logs.ProvisioningLogs.create",
        lambda *_args, **_kwargs: log,
    )
    monkeypatch.setattr(
        "app.services.nas.logs.ProvisioningLogs.update_status",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.nas.logs.ProvisioningLogs.get",
        lambda *_args, **_kwargs: SimpleNamespace(
            id=log_id, status=ProvisioningLogStatus.success
        ),
    )
    monkeypatch.setattr(
        DeviceProvisioner,
        "_execute_ssh",
        lambda *_args, **_kwargs: "ok",
    )
    monkeypatch.setattr(
        DeviceProvisioner,
        "_handle_queue_mapping",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.nas.provisioner._emit_nas_event",
        lambda *_args, **_kwargs: None,
    )

    caplog.set_level("INFO")

    DeviceProvisioner.provision_user(
        db=object(),
        nas_device_id=device_id,
        action=ProvisioningAction.create_user,
        variables={"username": "alice"},
        triggered_by="admin:alice",
    )

    start_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "nas_provisioning_start"
    )
    success_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "nas_provisioning_success"
    )

    assert start_record.event == "nas_provisioning"
    assert start_record.device_id == str(device_id)
    assert start_record.action == ProvisioningAction.create_user.value
    assert success_record.template_id == str(template_id)
    assert success_record.provisioning_log_id == str(log_id)
