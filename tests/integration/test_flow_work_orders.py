"""Work-order module flow on PostgreSQL: create → assign → start → evidence →
complete, fully on Sub-native identity (no flag — work orders are native
since #1373; ``public_id`` is the job key throughout).

All services are real; only the completion evidence gate is configured off
(the photo/signature path is unit-covered with faked uploads).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.dispatch import TechnicianProfile
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.field_job_event import FieldJobEvent
from app.models.field_note import FieldWorkOrderNote
from app.models.field_worklog import FieldWorkLog
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.schemas.dispatch import WorkOrderAssignmentQueueCreate, WorkOrderHeaderCreate
from app.services import dispatch as dispatch_service
from app.services.db_session_adapter import db_session_adapter
from app.services.field.note_commands import (
    CreateFieldWorkOrderNote,
    create_field_work_order_note,
)
from app.services.field.transitions import field_transitions
from app.services.field.worklogs import field_worklogs
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id


def _user(db) -> SystemUser:
    user = SystemUser(
        first_name="Flow",
        last_name="Tech",
        display_name="Flow Tech",
        email=f"flowtech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db.add(user)
    db.flush()
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


def _profile(db, user: SystemUser) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=f"flow-tech-{uuid4().hex[:6]}",
        title="Installer",
    )
    db.add(profile)
    db.flush()
    return profile


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="Flow",
        last_name="WorkOrder",
        email=f"fwo-{uuid4().hex[:8]}@example.com",
        # subscribers.reseller_id is NOT NULL (migration 116); default to House.
        reseller_id=_default_reseller_id(db),
    )
    db.add(sub)
    db.flush()
    return sub


def _disable_completion_gate(db) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.field,
            key="completion_requires_evidence",
            value_type=SettingValueType.boolean,
            value_text="false",
            is_active=True,
        )
    )
    db.flush()


def test_work_order_lifecycle_native(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    sub = _subscriber(db_session)
    _disable_completion_gate(db_session)

    # 1. Create — native identity minted (sub-<uuid>); the CRM provenance
    # reference remains NULL by the authoritative identity contract.
    row = dispatch_service.work_order_headers.create(
        db_session,
        WorkOrderHeaderCreate(
            title="Flow install",
            subscriber_id=sub.id,
            status="scheduled",
            scheduled_start=datetime.now(UTC),
        ),
    )
    assert row.public_id.startswith("sub-")
    assert row.crm_work_order_id is None

    # 2. Assign — the queue row resolves the header by public_id and stores
    # only the native FK. Its old string-shaped response is a derived projection.
    queue = dispatch_service.assignment_queue.create(
        db_session,
        WorkOrderAssignmentQueueCreate(
            crm_work_order_id=row.public_id,
            assigned_technician_id=profile.id,
            status="assigned",
        ),
    )
    assert queue.work_order_mirror_id == row.id
    assert queue.crm_work_order_id == row.public_id

    # 3. Start — transition engine keyed on public_id; the job event lands
    # FK-linked to the work order.
    started = field_transitions.apply(
        db_session,
        _auth(user),
        row.public_id,
        event="start",
        client_event_id=uuid4(),
    )
    assert started["job"].status == "in_progress"
    event = (
        db_session.query(FieldJobEvent)
        .filter(FieldJobEvent.work_order_mirror_id == row.id)
        .filter(FieldJobEvent.event == "start")
        .one()
    )
    assert event.work_order_mirror_id == row.id

    # 4. Evidence — worklog + note join only through the native FK.
    start_at = datetime.now(UTC) - timedelta(hours=1)
    submitted = field_worklogs.submit(
        db_session,
        _auth(user),
        row.public_id,
        [{"start_at": start_at, "end_at": start_at + timedelta(minutes=45)}],
    )
    assert len(submitted) == 1 and not submitted[0]["duplicate"]
    # The start transition auto-opens a timer worklog too, so resolve the
    # submitted entry by its own id rather than .one() over the FK.
    worklog = db_session.get(FieldWorkLog, submitted[0]["worklog"]["id"])
    assert worklog is not None
    assert worklog.work_order_mirror_id == row.id

    note_request_id = uuid4()
    requester_system_user_id = user.id
    work_order_public_id = row.public_id
    db_session_adapter.release_read_transaction(db_session)
    create_field_work_order_note(
        db_session,
        CreateFieldWorkOrderNote(
            context=CommandContext.system(
                actor=f"user:{requester_system_user_id}",
                scope="field:work_order_notes:write",
                reason="integration_field_note_creation",
                command_id=note_request_id,
                correlation_id=note_request_id,
                idempotency_key=str(note_request_id),
            ),
            requester_system_user_id=requester_system_user_id,
            work_order_public_id=work_order_public_id,
            request_id=note_request_id,
            body="Splice completed at FDH.",
            is_internal=True,
        ),
    )
    note = (
        db_session.query(FieldWorkOrderNote)
        .filter(FieldWorkOrderNote.work_order_mirror_id == row.id)
        .one()
    )
    assert note.work_order_mirror_id == row.id

    # 5. Complete (evidence gate configured off — the gated path is
    # unit-covered) — status lands terminal with a timestamp and the native
    # activity marker.
    completed_at = datetime.now(UTC)
    completed = field_transitions.apply(
        db_session,
        _auth(user),
        row.public_id,
        event="complete",
        client_event_id=uuid4(),
        occurred_at=completed_at,
    )
    assert completed["job"].status == "completed"
    assert completed["job"].completed_at is not None
    assert completed["job"].metadata_["native_field_source"] == "sub"

    # 6. Identity invariant — public_id resolves through the owner service.
    fetched = dispatch_service.work_order_headers.get(db_session, row.public_id)
    assert fetched.id == row.id
