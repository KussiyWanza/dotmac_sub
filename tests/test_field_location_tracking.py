from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.field_job_event import FieldJobEvent
from app.models.field_location import FieldTechLocationPing
from app.models.subscriber import Subscriber, UserType
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field.location_tracking import (
    LocationPingCommand,
    field_location_tracking,
)


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Live",
        last_name="Tech",
        display_name="Live Tech",
        email=f"live-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _profile(db_session, user: SystemUser) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-live-tech",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Live",
        last_name="Customer",
        email=f"live-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(
    db_session,
    subscriber: Subscriber,
    *,
    crm_work_order_id: str = "wo-geofence",
    status: str = "dispatched",
) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=crm_work_order_id,
        subscriber_id=subscriber.id,
        title="Geofence install",
        status=status,
        assigned_to_crm_person_id="crm-live-tech",
        address="Plot 14, Jabi",
        scheduled_start=datetime.now(UTC),
        metadata_={"location": {"lat": 9.071, "lng": 7.451}},
    )
    db_session.add(row)
    db_session.flush()
    return row


def _field_setting(db_session, key: str, value: str) -> None:
    db_session.add(
        DomainSetting(
            domain=SettingDomain.field,
            key=key,
            value_type=SettingValueType.boolean,
            value_text=value,
        )
    )


def test_record_batch_persists_pings_and_updates_presence(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-live")
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=row.id,
            status="assigned",
            assigned_technician_id=profile.id,
        )
    )
    now = datetime.now(UTC)

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                accuracy_m=10,
                captured_at=now,
                crm_work_order_id="wo-live",
                status="on_shift",
            ),
            LocationPingCommand(
                latitude=9.072,
                longitude=7.452,
                captured_at=now + timedelta(minutes=1),
            ),
        ],
    )

    assert result.accepted == 2
    assert result.errors == ()
    assert result.presence.status == "on_shift"
    assert result.presence.last_latitude == 9.072
    assert (
        db_session.query(FieldTechLocationPing)
        .filter(FieldTechLocationPing.crm_work_order_id == "wo-live")
        .count()
        == 1
    )


def test_stale_ping_does_not_roll_presence_backwards(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    now = datetime.now(UTC)
    auth = _auth(user)

    field_location_tracking.record_ping(
        db_session,
        auth,
        command=LocationPingCommand(
            latitude=9.071, longitude=7.451, captured_at=now
        ),
    )
    field_location_tracking.record_ping(
        db_session,
        auth,
        command=LocationPingCommand(
            latitude=1.0,
            longitude=1.0,
            captured_at=now - timedelta(minutes=5),
        ),
    )

    presence = field_location_tracking.get_or_create_presence(db_session, auth)
    assert presence.last_latitude == 9.071
    assert db_session.query(FieldTechLocationPing).count() == 2


def test_location_batch_collects_per_ping_errors(db_session):
    user = _user(db_session)
    _profile(db_session, user)

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [
            LocationPingCommand(latitude=9.071, longitude=7.451),
            LocationPingCommand(
                latitude=9.072, longitude=7.452, status="teleporting"
            ),
        ],
    )

    assert result.accepted == 1
    assert result.errors[0].index == 1
    assert db_session.query(FieldTechLocationPing).count() == 1


def test_location_ping_rejects_unassigned_work_order_tag(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-unassigned")
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                crm_work_order_id=row.public_id,
            )
        ],
    )

    assert result.accepted == 0
    assert result.errors[0].code == "technician_not_assigned"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_location_ping_rejects_terminal_work_order_tag(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-completed",
        status="completed",
    )
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=row.id,
            status="assigned",
            assigned_technician_id=profile.id,
        )
    )
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                crm_work_order_id=row.public_id,
            )
        ],
    )

    assert result.accepted == 0
    assert result.errors[0].code == "work_order_not_trackable"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_location_ping_rejects_timestamp_beyond_clock_skew(db_session):
    user = _user(db_session)
    _profile(db_session, user)

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                captured_at=datetime.now(UTC) + timedelta(minutes=6),
            )
        ],
    )

    assert result.accepted == 0
    assert result.errors[0].code == "captured_at_in_future"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_location_ping_rejects_technician_after_reassignment(db_session):
    old_user = _user(db_session)
    old_profile = _profile(db_session, old_user)
    new_user = _user(db_session)
    new_profile = _profile(db_session, new_user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-reassigned")
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=row.id,
            status="assigned",
            assigned_technician_id=old_profile.id,
            updated_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    db_session.flush()
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=row.id,
            status="assigned",
            assigned_technician_id=new_profile.id,
            updated_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        _auth(old_user),
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                crm_work_order_id=row.public_id,
            )
        ],
    )

    assert result.accepted == 0
    assert result.errors[0].code == "technician_not_assigned"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_geofence_is_disabled_by_default(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-geofence-off")
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [LocationPingCommand(latitude=9.071, longitude=7.451)],
    )

    db_session.refresh(row)
    assert result.transitions == ()
    assert row.status == "dispatched"
    assert db_session.query(FieldJobEvent).count() == 0


def test_geofence_auto_starts_arrived_job_once(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-geofence-on")
    _field_setting(db_session, "geofence_auto_status_enabled", "true")
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [LocationPingCommand(latitude=9.0711, longitude=7.4511)],
    )

    db_session.refresh(row)
    assert len(result.transitions) == 1
    assert result.transitions[0].crm_work_order_id == "wo-geofence-on"
    assert result.transitions[0].event == "start"
    assert result.transitions[0].distance_m < 25
    assert row.status == "in_progress"
    event = db_session.query(FieldJobEvent).one()
    assert event.event == "start"
    assert event.payload["source"] == "geofence"
    assert row.metadata_["native_field_source"] == "sub"
    assert row.metadata_["native_field_activity"]["transition"]["event"] == "start"

    replay = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [LocationPingCommand(latitude=9.0711, longitude=7.4511)],
    )
    assert replay.transitions == ()
    assert db_session.query(FieldJobEvent).count() == 1


def test_set_sharing_updates_presence_status(db_session):
    user = _user(db_session)
    _profile(db_session, user)

    presence = field_location_tracking.set_sharing(
        db_session,
        _auth(user),
        enabled=True,
        status="on_shift",
    )
    assert presence.location_sharing_enabled is True
    assert presence.status == "on_shift"

    presence = field_location_tracking.set_sharing(
        db_session, _auth(user), enabled=False
    )
    assert presence.location_sharing_enabled is False
    assert presence.status == "off_shift"


def test_unknown_status_is_rejected(db_session):
    user = _user(db_session)
    _profile(db_session, user)

    with pytest.raises(HTTPException) as exc:
        field_location_tracking.set_sharing(
            db_session,
            _auth(user),
            enabled=True,
            status="teleporting",
        )

    assert exc.value.status_code == 422


def test_location_api_routes(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    sharing = client.put(
        "/api/v1/field/locations/sharing",
        json={"enabled": True, "status": "on_shift"},
    )
    assert sharing.status_code == 200
    assert sharing.json()["status"] == "on_shift"

    ingest = client.post(
        "/api/v1/field/locations",
        json={"pings": [{"latitude": 9.071, "longitude": 7.451}]},
    )
    assert ingest.status_code == 200
    assert ingest.json()["accepted"] == 1

    presence = client.get("/api/v1/field/locations/me")
    assert presence.status_code == 200
    assert presence.json()["last_latitude"] == 9.071


def test_location_api_returns_typed_job_tag_rejection(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    response = client.post(
        "/api/v1/field/locations",
        json={
            "pings": [
                {
                    "latitude": 9.071,
                    "longitude": 7.451,
                    "crm_work_order_id": "missing-job",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert response.json()["errors"] == [
        {
            "index": 0,
            "code": "work_order_not_found",
            "detail": "Tagged work order was not found",
        }
    ]
