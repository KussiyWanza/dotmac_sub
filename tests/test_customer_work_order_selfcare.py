from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.field_location import FieldTechLocationPing, FieldTechPresence
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import customer_work_order_selfcare


def _customer(db_session) -> Subscriber:
    row = Subscriber(
        first_name="Selfcare",
        last_name="Customer",
        email=f"selfcare-{uuid4().hex}@example.com",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _technician(db_session) -> TechnicianProfile:
    user = SystemUser(
        first_name="Field",
        last_name="Engineer",
        display_name="Field Engineer",
        email=f"field-{uuid4().hex}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        title="Field engineer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _visit(db_session, customer: Subscriber, *, status: str) -> WorkOrder:
    row = WorkOrder(
        public_id=f"sub-{uuid4().hex}",
        subscriber_id=customer.id,
        title="Customer field visit",
        status=status,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _location_ping(
    technician: TechnicianProfile,
    visit: WorkOrder,
    *,
    latitude: float,
    longitude: float,
    captured_at: datetime,
) -> FieldTechLocationPing:
    return FieldTechLocationPing(
        technician_id=technician.id,
        person_id=technician.person_id,
        crm_work_order_id=visit.public_id,
        latitude=latitude,
        longitude=longitude,
        captured_at=captured_at,
        received_at=datetime.now(UTC),
        source="mobile",
    )


def test_location_reads_native_assignment_presence_and_public_identity(db_session):
    customer = _customer(db_session)
    technician = _technician(db_session)
    visit = _visit(db_session, customer, status="in_progress")
    db_session.add_all(
        [
            WorkOrderAssignmentQueue(
                work_order_mirror_id=visit.id,
                status="assigned",
                assigned_technician_id=technician.id,
            ),
            FieldTechPresence(
                technician_id=technician.id,
                person_id=technician.person_id,
                status="busy",
                location_sharing_enabled=True,
                last_latitude=9.0765,
                last_longitude=7.3986,
                last_location_at=datetime.now(UTC),
            ),
            _location_ping(
                technician,
                visit,
                latitude=9.0765,
                longitude=7.3986,
                captured_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    result = customer_work_order_selfcare.technician_location(
        db_session, str(customer.id), visit.public_id
    )

    assert result.available is True
    assert result.work_order_id == visit.public_id
    assert result.latitude == 9.0765
    assert result.updated_at is not None


def test_location_hides_stale_presence(db_session):
    customer = _customer(db_session)
    technician = _technician(db_session)
    visit = _visit(db_session, customer, status="in_progress")
    db_session.add_all(
        [
            WorkOrderAssignmentQueue(
                work_order_mirror_id=visit.id,
                status="assigned",
                assigned_technician_id=technician.id,
            ),
            FieldTechPresence(
                technician_id=technician.id,
                person_id=technician.person_id,
                status="busy",
                location_sharing_enabled=True,
                last_latitude=9.0765,
                last_longitude=7.3986,
                last_location_at=datetime.now(UTC) - timedelta(minutes=3),
            ),
            _location_ping(
                technician,
                visit,
                latitude=9.0765,
                longitude=7.3986,
                captured_at=datetime.now(UTC) - timedelta(minutes=3),
            ),
        ]
    )
    db_session.commit()

    result = customer_work_order_selfcare.technician_location(
        db_session, str(customer.id), visit.public_id
    )

    assert result.available is False
    assert result.reason == "stale"
    assert result.latitude is None
    assert result.longitude is None


def test_location_does_not_leak_latest_fix_from_another_work_order(db_session):
    customer = _customer(db_session)
    other_customer = _customer(db_session)
    technician = _technician(db_session)
    visit = _visit(db_session, customer, status="in_progress")
    other_visit = _visit(db_session, other_customer, status="in_progress")
    db_session.add_all(
        [
            WorkOrderAssignmentQueue(
                work_order_mirror_id=visit.id,
                status="assigned",
                assigned_technician_id=technician.id,
            ),
            FieldTechPresence(
                technician_id=technician.id,
                person_id=technician.person_id,
                status="busy",
                location_sharing_enabled=True,
                last_latitude=6.5244,
                last_longitude=3.3792,
                last_location_at=datetime.now(UTC),
            ),
            _location_ping(
                technician,
                other_visit,
                latitude=6.5244,
                longitude=3.3792,
                captured_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    result = customer_work_order_selfcare.technician_location(
        db_session, str(customer.id), visit.public_id
    )

    assert result.available is False
    assert result.reason == "no_fix"
    assert result.latitude is None
    assert result.longitude is None


def test_rating_is_native_audited_and_idempotent(db_session):
    customer = _customer(db_session)
    visit = _visit(db_session, customer, status="completed")
    db_session.commit()

    first = customer_work_order_selfcare.rate_technician(
        db_session,
        str(customer.id),
        visit.public_id,
        rating=5,
        comment="Excellent visit",
    )
    replay = customer_work_order_selfcare.rate_technician(
        db_session,
        str(customer.id),
        visit.public_id,
        rating=1,
        comment="Must not overwrite",
    )

    db_session.refresh(visit)
    assert first.rating == 5
    assert first.work_order_id == visit.public_id
    assert replay.already_rated is True
    assert replay.rating == 5
    assert visit.technician_rating == 5
    assert visit.metadata_["technician_rating"]["source"] == "customer_selfcare"
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order_technician_rated")
        .filter(AuditEvent.entity_id == str(visit.id))
        .count()
        == 1
    )


def test_rating_rejects_unfinished_or_unowned_visit(db_session):
    customer = _customer(db_session)
    other = _customer(db_session)
    visit = _visit(db_session, customer, status="in_progress")
    db_session.commit()

    with pytest.raises(ValueError, match="work_order_not_completed"):
        customer_work_order_selfcare.rate_technician(
            db_session, str(customer.id), visit.public_id, rating=4
        )
    with pytest.raises(LookupError, match="work_order_not_found"):
        customer_work_order_selfcare.rate_technician(
            db_session, str(other.id), visit.public_id, rating=4
        )
