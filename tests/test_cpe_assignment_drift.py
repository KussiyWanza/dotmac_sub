"""Read-only live-device/no-active-assignment review projection."""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

from app.models.network import (
    CPEDevice,
    DeviceStatus,
    OntAssignment,
    OntAuthorizationStatus,
    OntUnit,
    PonPort,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.network.cpe_assignment_drift import (
    AssignmentDriftSeverity,
    find_live_devices_without_active_assignment,
)


def _unassigned_live_cpe(
    db_session, *, olt_device, subscriber, subscription, informed_at: datetime
):
    pon = PonPort(
        olt_id=olt_device.id,
        name=f"0/1/{uuid.uuid4().int % 100000}",
        is_active=True,
    )
    cpe = CPEDevice(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        status=DeviceStatus.active,
        serial_number=f"DRIFT-CPE-{uuid.uuid4().hex[:10]}",
    )
    server = Tr069AcsServer(
        name=f"Drift ACS {uuid.uuid4().hex[:10]}",
        base_url="http://acs.test.local",
    )
    db_session.add_all([pon, cpe, server])
    db_session.flush()
    ont = OntUnit(
        serial_number=f"DRIFT-ONT-{uuid.uuid4().hex[:10]}",
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
    )
    db_session.add(ont)
    db_session.flush()
    tr069 = Tr069CpeDevice(
        acs_server_id=server.id,
        cpe_device_id=cpe.id,
        ont_unit_id=ont.id,
        serial_number=cpe.serial_number,
        last_inform_at=informed_at,
        is_active=True,
    )
    db_session.add(tr069)
    db_session.commit()
    return cpe, ont, tr069, pon


def test_recent_inform_without_assignment_is_advisory(
    db_session, olt_device, subscriber, subscription
) -> None:
    now = datetime.now(UTC)
    _cpe, ont, _tr069, _pon = _unassigned_live_cpe(
        db_session,
        olt_device=olt_device,
        subscriber=subscriber,
        subscription=subscription,
        informed_at=now - timedelta(minutes=5),
    )

    result = find_live_devices_without_active_assignment(db_session, now=now)

    row = next(item for item in result.rows if item.ont_unit_id == ont.id)
    assert row.severity is AssignmentDriftSeverity.advisory
    assert row.code.endswith("recent_inform_without_assignment")
    assert result.advisory_count >= 1


def test_fresh_exact_subscription_radius_session_is_blocking(
    db_session, olt_device, subscriber, subscription
) -> None:
    now = datetime.now(UTC)
    cpe, ont, _tr069, _pon = _unassigned_live_cpe(
        db_session,
        olt_device=olt_device,
        subscriber=subscriber,
        subscription=subscription,
        informed_at=now - timedelta(minutes=5),
    )
    db_session.add(
        RadiusActiveSession(
            subscriber_id=cpe.subscriber_id,
            subscription_id=cpe.subscription_id,
            username=f"drift-{uuid.uuid4().hex[:10]}",
            acct_session_id=uuid.uuid4().hex,
            session_start=now - timedelta(minutes=10),
            last_update=now - timedelta(minutes=1),
        )
    )
    db_session.commit()

    result = find_live_devices_without_active_assignment(db_session, now=now)

    row = next(item for item in result.rows if item.ont_unit_id == ont.id)
    assert row.severity is AssignmentDriftSeverity.blocking
    assert row.code.endswith("active_radius_without_assignment")
    assert result.blocking_count >= 1


def test_stale_observations_do_not_enter_the_queue(
    db_session, olt_device, subscriber, subscription
) -> None:
    now = datetime.now(UTC)
    cpe, ont, _tr069, _pon = _unassigned_live_cpe(
        db_session,
        olt_device=olt_device,
        subscriber=subscriber,
        subscription=subscription,
        informed_at=now - timedelta(days=3),
    )
    db_session.add(
        RadiusActiveSession(
            subscriber_id=cpe.subscriber_id,
            subscription_id=cpe.subscription_id,
            username=f"stale-{uuid.uuid4().hex[:10]}",
            acct_session_id=uuid.uuid4().hex,
            session_start=now - timedelta(days=3),
            last_update=now - timedelta(days=2),
        )
    )
    db_session.commit()

    result = find_live_devices_without_active_assignment(db_session, now=now)

    assert all(item.ont_unit_id != ont.id for item in result.rows)


def test_active_assignment_removes_device_from_both_detector_arms(
    db_session, olt_device, subscriber, subscription
) -> None:
    now = datetime.now(UTC)
    cpe, ont, _tr069, pon = _unassigned_live_cpe(
        db_session,
        olt_device=olt_device,
        subscriber=subscriber,
        subscription=subscription,
        informed_at=now - timedelta(minutes=5),
    )
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon.id,
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                active=True,
            ),
            RadiusActiveSession(
                subscriber_id=cpe.subscriber_id,
                subscription_id=cpe.subscription_id,
                username=f"assigned-{uuid.uuid4().hex[:10]}",
                acct_session_id=uuid.uuid4().hex,
                session_start=now - timedelta(minutes=10),
                last_update=now - timedelta(minutes=1),
            ),
        ]
    )
    db_session.commit()

    result = find_live_devices_without_active_assignment(db_session, now=now)

    assert all(item.ont_unit_id != ont.id for item in result.rows)


def test_detector_source_contains_no_assignment_write() -> None:
    source = inspect.getsource(find_live_devices_without_active_assignment)

    assert "OntAssignment(" not in source
    assert ".active =" not in source
