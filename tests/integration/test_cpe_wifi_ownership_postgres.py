"""PostgreSQL proof for the CPE-detail WiFi ownership cutover."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.network import (
    CPEDevice,
    DeviceStatus,
    OntAssignment,
    OntAuthorizationStatus,
    OntUnit,
    PonPort,
)
from app.models.network_operation import NetworkOperationDispatch
from app.models.ont_service_configuration import OntServiceConfigurationRevision
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.network import cpe_action_wifi
from app.services.network.ont_service_configuration import (
    resolve_cpe_wifi_admission_scope,
)
from app.services.owner_commands import CommandContext


def test_cpe_ssid_stages_the_ont_owner_revision_on_migrated_postgres(
    db_session,
    monkeypatch,
    olt_device,
    active_subscription,
    subscriber,
) -> None:
    """The real schema accepts the owner path and no direct ACS writer runs."""

    olt_device.is_active = True
    pon = PonPort(
        olt_id=olt_device.id,
        name=f"0/1/{uuid.uuid4().int % 100000}",
        is_active=True,
    )
    ont = OntUnit(
        serial_number=f"CPE-WIFI-PG-{uuid.uuid4().hex[:10]}",
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        olt_device_id=olt_device.id,
        pon_port=pon,
    )
    assignment = OntAssignment(
        ont_unit=ont,
        subscriber_id=subscriber.id,
        subscription_id=active_subscription.id,
        pon_port=pon,
        active=True,
    )
    cpe = CPEDevice(
        subscriber_id=subscriber.id,
        status=DeviceStatus.active,
        serial_number=f"CPE-PG-{uuid.uuid4().hex[:10]}",
    )
    server = Tr069AcsServer(name="PostgreSQL ACS", base_url="http://acs.test.local")
    tr069_device = Tr069CpeDevice(
        acs_server=server,
        cpe_device=cpe,
        ont_unit=ont,
        serial_number=cpe.serial_number,
        is_active=True,
    )
    db_session.add_all([assignment, tr069_device])
    db_session.commit()

    def _direct_acs_write_forbidden(*_args, **_kwargs):
        raise AssertionError("CPE-detail WiFi must not write directly to GenieACS")

    monkeypatch.setattr(
        cpe_action_wifi,
        "set_and_verify",
        _direct_acs_write_forbidden,
    )

    command_id = uuid.uuid4()
    scope = resolve_cpe_wifi_admission_scope(db_session, cpe.id)
    result = cpe_action_wifi.set_wifi_ssid(
        db_session,
        str(cpe.id),
        "PostgresOwnerPath",
        admission_scope=scope,
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="test:postgres",
            scope="network:ont:write",
            reason="Prove the CPE-detail WiFi ownership cutover on PostgreSQL",
            idempotency_key=f"cpe-wifi-postgres:{command_id}",
        ),
        permission_granted=True,
        object_scope_granted=True,
    )
    password_command_id = uuid.uuid4()
    password_result = cpe_action_wifi.set_wifi_password(
        db_session,
        str(cpe.id),
        "postgres-composed-password",
        admission_scope=scope,
        context=CommandContext(
            command_id=password_command_id,
            correlation_id=password_command_id,
            actor="test:postgres",
            scope="network:ont:write",
            reason="Prove sparse WiFi updates compose on PostgreSQL",
            idempotency_key=f"cpe-wifi-postgres:{password_command_id}",
        ),
        permission_granted=True,
        object_scope_granted=True,
    )

    assert result.success is True
    assert password_result.success is True
    assert result.data is not None
    operation_id = uuid.UUID(str(result.data["operation_id"]))
    revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.operation_id == operation_id
        )
    )
    dispatch = db_session.scalar(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == operation_id
        )
    )
    assert revision is not None
    assert dispatch is not None
    db_session.refresh(ont)
    assert ont.desired_config["wifi"]["ssid"] == "PostgresOwnerPath"
    assert ont.desired_config["wifi"]["password"] != "postgres-composed-password"
