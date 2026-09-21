from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.field_job_event import FieldJobEvent
from app.models.field_location import FieldTechLocationPing, FieldTechPresence
from app.models.subscriber import Subscriber, UserType
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.location_tracking import (
    FieldLocationTracking,
    LocationPingCommand,
    field_location_tracking,
)
from app.services.owner_commands import owner_command_active


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


def _profile(
    db_session,
    user: SystemUser,
    *,
    crm_person_id: str = "crm-live-tech",
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
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
    auth = _auth(user)
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
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        auth,
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
    db_session.commit()

    field_location_tracking.record_ping(
        db_session,
        auth,
        command=LocationPingCommand(latitude=9.071, longitude=7.451, captured_at=now),
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
    auth = _auth(user)
    _profile(db_session, user)
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        auth,
        [
            LocationPingCommand(latitude=9.071, longitude=7.451),
            LocationPingCommand(latitude=9.072, longitude=7.452, status="teleporting"),
        ],
    )

    assert result.accepted == 1
    assert result.errors[0].index == 1
    assert db_session.query(FieldTechLocationPing).count() == 1


def test_location_ping_rejects_unassigned_work_order_tag(db_session):
    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-unassigned")
    row_public_id = row.public_id
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        auth,
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                crm_work_order_id=row_public_id,
            )
        ],
    )

    assert result.accepted == 0
    assert result.errors[0].code == "technician_not_assigned"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_location_ping_rejects_terminal_work_order_tag(db_session):
    user = _user(db_session)
    auth = _auth(user)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-completed",
        status="completed",
    )
    row_public_id = row.public_id
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
        auth,
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                crm_work_order_id=row_public_id,
            )
        ],
    )

    assert result.accepted == 0
    assert result.errors[0].code == "work_order_not_trackable"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_location_ping_rejects_timestamp_beyond_clock_skew(db_session):
    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        auth,
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
    old_auth = _auth(old_user)
    old_profile = _profile(db_session, old_user)
    new_user = _user(db_session)
    new_profile = _profile(db_session, new_user, crm_person_id="crm-new-tech")
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-reassigned")
    row_public_id = row.public_id
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
        old_auth,
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                crm_work_order_id=row_public_id,
            )
        ],
    )

    assert result.accepted == 0
    assert result.errors[0].code == "technician_not_assigned"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_geofence_is_disabled_by_default(db_session):
    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-geofence-off")
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        auth,
        [LocationPingCommand(latitude=9.071, longitude=7.451)],
    )

    db_session.refresh(row)
    assert result.transitions == ()
    assert row.status == "dispatched"
    assert db_session.query(FieldJobEvent).count() == 0


def test_geofence_auto_starts_arrived_job_once(db_session):
    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-geofence-on")
    _field_setting(db_session, "geofence_auto_status_enabled", "true")
    db_session.commit()

    result = field_location_tracking.record_batch(
        db_session,
        auth,
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

    # record_batch is an owner command and needs a transaction-free session
    # at entry; the reads above (db_session.refresh/query) left an implicit
    # read transaction open on this shared session.
    db_session_adapter.release_read_transaction(db_session)
    replay = field_location_tracking.record_batch(
        db_session,
        auth,
        [LocationPingCommand(latitude=9.0711, longitude=7.4511)],
    )
    assert replay.transitions == ()
    assert db_session.query(FieldJobEvent).count() == 1


def test_set_sharing_updates_presence_status(db_session):
    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    db_session.commit()

    presence = field_location_tracking.set_sharing(
        db_session,
        auth,
        enabled=True,
        status="on_shift",
    )
    assert presence.location_sharing_enabled is True
    assert presence.status == "on_shift"

    # set_sharing is an owner command; reading the attributes above already
    # reopened an implicit read transaction on this shared session.
    db_session_adapter.release_read_transaction(db_session)
    presence = field_location_tracking.set_sharing(db_session, auth, enabled=False)
    assert presence.location_sharing_enabled is False
    assert presence.status == "off_shift"


def test_unknown_status_is_rejected(db_session):
    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_location_tracking.set_sharing(
            db_session,
            auth,
            enabled=True,
            status="teleporting",
        )

    assert exc.value.status_code == 422


def test_location_api_routes(db_session, monkeypatch):
    user = _user(db_session)
    _profile(db_session, user)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    attendance = type(
        "CheckedInAttendance",
        (),
        {"require_checked_in_for_shift": lambda *_args, **_kwargs: None},
    )
    monkeypatch.setattr(
        "app.api.field.locations.WorkforceAttendanceService",
        lambda _db: attendance(),
    )
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


def test_batch_row_flush_conflict_is_isolated_to_its_own_row(db_session, monkeypatch):
    """A row that fails at flush (e.g. a future duplicate-identity
    constraint) is rejected on its own; it neither poisons the rows around
    it nor leaves the session unusable for the rest of the batch.
    """

    user = _user(db_session)
    auth = _auth(user)
    profile = _profile(db_session, user)
    presence = FieldTechPresence(technician_id=profile.id, person_id=profile.person_id)
    db_session.add(presence)
    db_session.commit()

    # execute_owner_savepoint's db.begin_nested() itself triggers one real,
    # no-op flush() call while taking its transaction snapshot, in addition
    # to _ingest_ping's own explicit flush() that actually writes the row —
    # two real flush() invocations per successful ping, not one. Row index 1
    # (the second ping) is targeted by failing its OWN row-writing flush,
    # which is the 4th real invocation: (snapshot, write) for ping 0, then
    # (snapshot, write) for ping 1.
    call_count = {"value": 0}
    original_flush = db_session.flush

    def _flaky_flush(*args, **kwargs):
        call_count["value"] += 1
        if call_count["value"] == 4:
            raise IntegrityError("INSERT", {}, Exception("duplicate ping identity"))
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", _flaky_flush)

    result = field_location_tracking.record_batch(
        db_session,
        auth,
        [
            LocationPingCommand(latitude=9.071, longitude=7.451),
            LocationPingCommand(latitude=9.072, longitude=7.452),
            LocationPingCommand(latitude=9.073, longitude=7.453),
        ],
    )

    assert result.accepted == 2
    assert len(result.errors) == 1
    assert result.errors[0].index == 1
    assert result.errors[0].code == "ping_conflict"

    # The session is usable afterwards: a plain query proves the connection
    # was not left in an aborted-transaction state by the isolated failure.
    remaining_latitudes = {
        round(row.latitude, 3) for row in db_session.query(FieldTechLocationPing).all()
    }
    assert remaining_latitudes == {9.071, 9.073}


def test_batch_row_conflict_over_http_is_a_normal_200(db_session, monkeypatch):
    """The same row-level flush conflict, exercised through the HTTP route,
    is a normal 200 response with a per-row error, never a 500.
    """

    user = _user(db_session)
    profile = _profile(db_session, user)
    presence = FieldTechPresence(technician_id=profile.id, person_id=profile.person_id)
    db_session.add(presence)
    db_session.commit()

    # See test_batch_row_flush_conflict_is_isolated_to_its_own_row: two real
    # flush() calls happen per successful ping (execute_owner_savepoint's
    # begin_nested() snapshot, then _ingest_ping's own row-writing flush).
    # Failing row index 0's own write is the 2nd real invocation.
    call_count = {"value": 0}
    original_flush = db_session.flush

    def _flaky_flush(*args, **kwargs):
        call_count["value"] += 1
        if call_count["value"] == 2:
            raise IntegrityError("INSERT", {}, Exception("duplicate ping identity"))
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", _flaky_flush)

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    response = client.post(
        "/api/v1/field/locations",
        json={
            "pings": [
                {"latitude": 9.071, "longitude": 7.451},
                {"latitude": 9.072, "longitude": 7.452},
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["errors"] == [
        {
            "index": 0,
            "code": "ping_conflict",
            "detail": "Ping conflicted with an existing record",
        }
    ]


def test_geofence_runs_after_the_ingest_owner_command_commits(db_session, monkeypatch):
    """Geofence auto-status is a genuinely separate follow-up operation: it
    must observe no active owner command, because field_transitions.apply
    commits its own root transaction and would trip the owner-command
    boundary guard ("only the active owner command may complete its own
    transaction") if it ran as a participant inside record_batch's owner
    command instead.
    """

    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    _field_setting(db_session, "geofence_auto_status_enabled", "true")
    db_session.commit()

    from app.services.field import geofence as geofence_module

    observed: dict[str, object] = {}
    original_evaluate = geofence_module.evaluate

    def _spy_evaluate(db, principal, latitude, longitude):
        observed["owner_command_active"] = owner_command_active(
            db, owner="operations.field_location_ingest"
        )
        return original_evaluate(db, principal, latitude, longitude)

    monkeypatch.setattr(geofence_module, "evaluate", _spy_evaluate)

    field_location_tracking.record_batch(
        db_session,
        auth,
        [LocationPingCommand(latitude=9.071, longitude=7.451)],
    )

    assert observed["owner_command_active"] is False


def test_location_api_refuses_shift_without_erp_check_in(db_session, monkeypatch):
    user = _user(db_session)
    _profile(db_session, user)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)

    class NotCheckedInAttendance:
        def require_checked_in_for_shift(self, *_args, **_kwargs):
            from app.services.workforce_attendance import WorkforceAttendanceError

            raise WorkforceAttendanceError(
                "check_in_required",
                "Check in before enabling location sharing.",
            )

    monkeypatch.setattr(
        "app.api.field.locations.WorkforceAttendanceService",
        lambda _db: NotCheckedInAttendance(),
    )

    response = TestClient(app).put(
        "/api/v1/field/locations/sharing",
        json={"enabled": True, "status": "on_shift"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "check_in_required"


def test_replayed_ping_persists_once_and_does_not_move_presence(db_session):
    """A retried record_ping call carrying the same client_observation_id is
    an idempotent replay: no second row, no second presence update, and the
    replay resolves to the original row.
    """

    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    db_session.commit()

    client_observation_id = uuid4()
    command = LocationPingCommand(
        latitude=9.071,
        longitude=7.451,
        status="on_shift",
        client_observation_id=client_observation_id,
    )

    first_ping, _, first_replayed = field_location_tracking.record_ping(
        db_session, auth, command=command
    )
    assert first_replayed is False

    # A genuine retry: same client_observation_id, but with a coordinate
    # drift a client's own GPS jitter could plausibly produce between the
    # original send and its retry.
    retry_command = LocationPingCommand(
        latitude=1.0,
        longitude=1.0,
        status="on_shift",
        client_observation_id=client_observation_id,
    )
    second_ping, presence, second_replayed = field_location_tracking.record_ping(
        db_session, auth, command=retry_command
    )

    assert second_replayed is True
    assert second_ping.id == first_ping.id
    assert db_session.query(FieldTechLocationPing).count() == 1
    # The replay never re-ran presence mutation with the retry's payload.
    assert presence.last_latitude == 9.071
    assert presence.last_longitude == 7.451


def test_different_technicians_may_share_a_client_observation_id(db_session):
    """The uniqueness is scoped to (technician_id, client_observation_id),
    not global — this is the deliberate deviation from FieldJobEvent's bare
    global-unique client_event_id index. A test that copied that precedent
    verbatim would fail here because the second technician's ping would
    collide with the first's.
    """

    first_user = _user(db_session)
    first_auth = _auth(first_user)
    _profile(db_session, first_user, crm_person_id="crm-tech-one")

    second_user = _user(db_session)
    second_auth = _auth(second_user)
    _profile(db_session, second_user, crm_person_id="crm-tech-two")
    db_session.commit()

    shared_client_observation_id = uuid4()
    first_ping, _, first_replayed = field_location_tracking.record_ping(
        db_session,
        first_auth,
        command=LocationPingCommand(
            latitude=9.071,
            longitude=7.451,
            client_observation_id=shared_client_observation_id,
        ),
    )
    second_ping, _, second_replayed = field_location_tracking.record_ping(
        db_session,
        second_auth,
        command=LocationPingCommand(
            latitude=8.0,
            longitude=6.0,
            client_observation_id=shared_client_observation_id,
        ),
    )

    assert first_replayed is False
    assert second_replayed is False
    assert first_ping.id != second_ping.id
    assert db_session.query(FieldTechLocationPing).count() == 2


def test_batch_duplicate_counts_as_accepted_not_error(db_session):
    """A batch mixing a genuinely new ping with one that replays an
    already-persisted client_observation_id must count the replay inside
    `accepted`, never as an error, so an already-shipped client's
    `accepted + len(errors) == total_sent` check still holds.
    """

    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    db_session.commit()

    already_sent_id = uuid4()
    existing_ping, _, _ = field_location_tracking.record_ping(
        db_session,
        auth,
        command=LocationPingCommand(
            latitude=9.071,
            longitude=7.451,
            client_observation_id=already_sent_id,
        ),
    )

    result = field_location_tracking.record_batch(
        db_session,
        auth,
        [
            LocationPingCommand(
                latitude=9.071,
                longitude=7.451,
                client_observation_id=already_sent_id,
            ),
            LocationPingCommand(
                latitude=9.09, longitude=7.49, client_observation_id=uuid4()
            ),
        ],
    )

    total_sent = 2
    assert result.accepted == 2
    assert result.errors == ()
    assert result.accepted + len(result.errors) == total_sent
    assert len(result.replays) == 1
    assert result.replays[0].index == 0
    assert result.replays[0].ping_id == existing_ping.id
    assert db_session.query(FieldTechLocationPing).count() == 2


def test_ping_without_client_observation_id_always_creates_a_new_row(db_session):
    """Old-client behavior is unchanged: omitting client_observation_id skips
    dedup entirely, exactly like today, even for two otherwise-identical
    pings.
    """

    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    db_session.commit()

    command = LocationPingCommand(latitude=9.071, longitude=7.451, status="on_shift")
    first_ping, _, first_replayed = field_location_tracking.record_ping(
        db_session, auth, command=command
    )
    second_ping, _, second_replayed = field_location_tracking.record_ping(
        db_session, auth, command=command
    )

    assert first_replayed is False
    assert second_replayed is False
    assert first_ping.id != second_ping.id
    assert db_session.query(FieldTechLocationPing).count() == 2


def test_concurrent_insert_race_recovers_via_replay_not_error(db_session, monkeypatch):
    """Two callers that both pass the dedup pre-check before either has
    committed (the genuine race PR A's savepoints exist to isolate) must not
    surface the resulting IntegrityError: the loser re-reads the winner's row
    and returns it as a replay, mirroring field_transitions.apply's own
    IntegrityError-recovery shape rather than inventing a new one.
    """

    user = _user(db_session)
    auth = _auth(user)
    _profile(db_session, user)
    db_session.commit()

    client_observation_id = uuid4()

    winner_ping, _, winner_replayed = field_location_tracking.record_ping(
        db_session,
        auth,
        command=LocationPingCommand(
            latitude=9.071,
            longitude=7.451,
            client_observation_id=client_observation_id,
        ),
    )
    assert winner_replayed is False

    # Only NOW do we stub the pre-check, and only for the loser's own first
    # lookup: this reproduces "the loser's pre-check runs before the
    # winner's commit is visible to it", the one moment a same-process test
    # cannot otherwise force. The stub's second call — made from
    # `_replay_for`'s post-IntegrityError recovery — is the real
    # implementation, proving recovery reads the actually-committed winner.
    calls = {"n": 0}
    original_find = FieldLocationTracking._find_existing_ping

    def _find_existing_ping_stub(db, *, technician_id, client_observation_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_find(
            db,
            technician_id=technician_id,
            client_observation_id=client_observation_id,
        )

    monkeypatch.setattr(
        FieldLocationTracking,
        "_find_existing_ping",
        staticmethod(_find_existing_ping_stub),
    )

    # The loser's own pre-check is stubbed to miss the winner's row (call #1
    # above); its real db.flush() then hits the live composite unique index
    # for real, and `_replay_for`'s own lookup (call #2, unstubbed) recovers
    # the winner's row.
    loser_ping, _, loser_replayed = field_location_tracking.record_ping(
        db_session,
        auth,
        command=LocationPingCommand(
            latitude=1.0,
            longitude=1.0,
            client_observation_id=client_observation_id,
        ),
    )

    assert loser_replayed is True
    assert loser_ping.id == winner_ping.id
    assert db_session.query(FieldTechLocationPing).count() == 1
