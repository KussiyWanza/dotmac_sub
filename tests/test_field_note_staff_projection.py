from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.dispatch import TechnicianProfile
from app.models.field_attachment import FieldAttachment
from app.models.field_note import FieldWorkOrderNote
from app.models.project import Project, ProjectTask
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import web_dispatch_work_orders, web_projects
from app.services.field.note_commands import (
    FieldNoteQueryError,
    GetStaffFieldNoteAttachment,
    ListStaffFieldWorkOrderNotes,
    OriginTicketFieldNoteScope,
    ProjectTaskFieldNoteScope,
    StaffFieldNoteAccess,
    WorkOrderFieldNoteScope,
    get_staff_field_note_attachment,
    list_staff_field_work_order_notes,
)
from app.web.admin import field_note_access


def _fixture(db_session):
    subscriber = Subscriber(
        first_name="Field",
        last_name="Customer",
        email=f"field-note-{uuid4().hex[:8]}@example.com",
    )
    user = SystemUser(
        first_name="Ada",
        last_name="Tech",
        display_name="Ada Tech",
        email=f"field-note-tech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add_all([subscriber, user])
    db_session.flush()
    technician = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        workforce_system="test",
        workforce_employee_reference=f"field-note-tech-{uuid4().hex[:8]}",
    )
    project = Project(name="Fibre repair", subscriber_id=subscriber.id)
    ticket = Ticket(
        number=f"TKT-{uuid4().hex[:8]}",
        title="Fibre fault",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        status="open",
        priority="normal",
    )
    db_session.add_all([technician, project, ticket])
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        ticket_id=ticket.id,
        title="Repair drop fibre",
    )
    db_session.add(task)
    db_session.flush()
    ticket_work_order = WorkOrder(
        public_id=f"WO-{uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        project_id=project.id,
        project_task_id=task.id,
        origin_ticket_id=ticket.id,
        title="First visit",
        status="in_progress",
    )
    task_only_work_order = WorkOrder(
        public_id=f"WO-{uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        project_id=project.id,
        project_task_id=task.id,
        title="Follow-up visit",
        status="scheduled",
    )
    unrelated_work_order = WorkOrder(
        public_id=f"WO-{uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        title="Unrelated visit",
        status="scheduled",
    )
    db_session.add_all([ticket_work_order, task_only_work_order, unrelated_work_order])
    db_session.flush()
    now = datetime.now(UTC)
    ticket_note = FieldWorkOrderNote(
        work_order_mirror_id=ticket_work_order.id,
        author_technician_id=technician.id,
        author_person_id=user.id,
        author_system_user_id=user.id,
        author_name="Ada Tech",
        body="Signal restored at the customer premises.",
        is_internal=True,
        created_at=now,
    )
    task_only_note = FieldWorkOrderNote(
        work_order_mirror_id=task_only_work_order.id,
        author_technician_id=technician.id,
        author_person_id=user.id,
        author_system_user_id=user.id,
        author_name="Ada Tech",
        body="Return visit scheduled.",
        is_internal=False,
        created_at=now + timedelta(minutes=1),
    )
    unrelated_note = FieldWorkOrderNote(
        work_order_mirror_id=unrelated_work_order.id,
        author_technician_id=technician.id,
        author_person_id=user.id,
        author_system_user_id=user.id,
        author_name="Ada Tech",
        body="Must not leak into related context.",
        is_internal=True,
        created_at=now + timedelta(minutes=2),
    )
    db_session.add_all([ticket_note, task_only_note, unrelated_note])
    db_session.flush()
    return {
        "subscriber": subscriber,
        "user": user,
        "technician": technician,
        "task": task,
        "ticket": ticket,
        "ticket_work_order": ticket_work_order,
        "task_only_work_order": task_only_work_order,
        "ticket_note": ticket_note,
        "task_only_note": task_only_note,
    }


def test_staff_note_projection_uses_only_authoritative_relationships(db_session):
    data = _fixture(db_session)

    work_order_result = list_staff_field_work_order_notes(
        db_session,
        ListStaffFieldWorkOrderNotes(
            scope=WorkOrderFieldNoteScope(
                work_order_public_id=data["ticket_work_order"].public_id
            ),
            access=StaffFieldNoteAccess(global_access=True),
        ),
    )
    task_result = list_staff_field_work_order_notes(
        db_session,
        ListStaffFieldWorkOrderNotes(
            scope=ProjectTaskFieldNoteScope(project_task_id=data["task"].id),
            access=StaffFieldNoteAccess(global_access=True),
        ),
    )
    ticket_result = list_staff_field_work_order_notes(
        db_session,
        ListStaffFieldWorkOrderNotes(
            scope=OriginTicketFieldNoteScope(origin_ticket_id=data["ticket"].id),
            access=StaffFieldNoteAccess(global_access=True),
        ),
    )

    assert [item.id for item in work_order_result.items] == [data["ticket_note"].id]
    assert [item.id for item in task_result.items] == [
        data["task_only_note"].id,
        data["ticket_note"].id,
    ]
    assert [item.id for item in ticket_result.items] == [data["ticket_note"].id]
    assert (
        ticket_result.items[0].work_order_public_id
        == data["ticket_work_order"].public_id
    )
    assert ticket_result.items[0].is_internal is True
    assert task_result.total == 2


def test_staff_work_order_and_task_contexts_render_the_shared_projection(db_session):
    data = _fixture(db_session)
    db_session.commit()

    work_order_context = web_dispatch_work_orders.detail_page(
        db_session,
        data["ticket_work_order"].public_id,
        field_note_access=StaffFieldNoteAccess(global_access=True),
    )
    task_context = web_projects.build_task_detail_context(
        db_session,
        task=data["task"],
        can_read_work_orders=True,
        field_note_access=StaffFieldNoteAccess(global_access=True),
    )
    hidden_task_context = web_projects.build_task_detail_context(
        db_session,
        task=data["task"],
        can_read_work_orders=False,
    )

    assert [item.id for item in work_order_context["field_notes"]] == [
        data["ticket_note"].id
    ]
    assert [item.id for item in task_context["field_notes"]] == [
        data["task_only_note"].id,
        data["ticket_note"].id,
    ]
    assert hidden_task_context["field_notes"] == ()


def test_staff_note_projection_honors_region_scope(db_session):
    data = _fixture(db_session)
    data["subscriber"].region = "Abuja"
    db_session.flush()
    query = WorkOrderFieldNoteScope(
        work_order_public_id=data["ticket_work_order"].public_id
    )

    allowed = list_staff_field_work_order_notes(
        db_session,
        ListStaffFieldWorkOrderNotes(
            scope=query,
            access=StaffFieldNoteAccess(regions=("Abuja",)),
        ),
    )
    denied = list_staff_field_work_order_notes(
        db_session,
        ListStaffFieldWorkOrderNotes(
            scope=query,
            access=StaffFieldNoteAccess(regions=("Lagos",)),
        ),
    )
    no_grant = list_staff_field_work_order_notes(
        db_session,
        ListStaffFieldWorkOrderNotes(scope=query, access=StaffFieldNoteAccess()),
    )

    assert [item.id for item in allowed.items] == [data["ticket_note"].id]
    assert denied.items == ()
    assert denied.total == 0
    assert no_grant.items == ()


def test_staff_note_access_adapter_preserves_global_and_scoped_grants(
    db_session, monkeypatch
):
    reseller_id = uuid4()
    decisions = iter(
        [
            "global",
            {
                ("region", "Abuja"),
                ("reseller", str(reseller_id)),
                ("reseller", "not-a-uuid"),
                ("unsupported", "ignored"),
            },
            None,
        ]
    )
    monkeypatch.setattr(
        field_note_access,
        "grant_scopes_for_permission",
        lambda *_args, **_kwargs: next(decisions),
    )
    auth = {"principal_id": str(uuid4()), "principal_type": "system_user"}

    global_access = field_note_access.resolve_staff_field_note_access(db_session, auth)
    scoped_access = field_note_access.resolve_staff_field_note_access(db_session, auth)
    denied_access = field_note_access.resolve_staff_field_note_access(db_session, auth)

    assert global_access.global_access is True
    assert scoped_access.reseller_ids == (reseller_id,)
    assert scoped_access.regions == ("Abuja",)
    assert denied_access == StaffFieldNoteAccess()


def test_staff_note_projection_exposes_only_active_note_attachments(db_session):
    data = _fixture(db_session)
    stored_file = StoredFile(
        owner_subscriber_id=data["subscriber"].id,
        entity_type="field_attachment",
        entity_id=str(data["ticket_work_order"].id),
        original_filename="signal-reading.jpg",
        storage_key_or_relative_path=f"field/{uuid4().hex}",
        file_size=12,
        content_type="image/jpeg",
        storage_provider="s3",
        uploaded_by=data["subscriber"].id,
    )
    db_session.add(stored_file)
    db_session.flush()
    attachment = FieldAttachment(
        work_order_mirror_id=data["ticket_work_order"].id,
        note_id=data["ticket_note"].id,
        stored_file_id=stored_file.id,
        kind="photo",
        file_name="signal-reading.jpg",
        mime_type="image/jpeg",
        size_bytes=12,
        uploaded_by_technician_id=data["technician"].id,
        uploaded_by_person_id=data["user"].id,
        uploaded_by_system_user_id=data["user"].id,
    )
    inactive = FieldAttachment(
        work_order_mirror_id=data["ticket_work_order"].id,
        note_id=data["ticket_note"].id,
        stored_file_id=stored_file.id,
        kind="photo",
        file_name="deleted.jpg",
        mime_type="image/jpeg",
        size_bytes=12,
        uploaded_by_technician_id=data["technician"].id,
        uploaded_by_person_id=data["user"].id,
        uploaded_by_system_user_id=data["user"].id,
        is_active=False,
    )
    db_session.add_all([attachment, inactive])
    db_session.flush()

    result = list_staff_field_work_order_notes(
        db_session,
        ListStaffFieldWorkOrderNotes(
            scope=WorkOrderFieldNoteScope(
                work_order_public_id=data["ticket_work_order"].public_id
            ),
            access=StaffFieldNoteAccess(global_access=True),
        ),
    )
    access = get_staff_field_note_attachment(
        db_session,
        GetStaffFieldNoteAttachment(
            work_order_public_id=data["ticket_work_order"].public_id,
            attachment_id=attachment.id,
        ),
    )

    assert [item.id for item in result.items[0].attachments] == [attachment.id]
    assert result.items[0].attachments[0].download_path.endswith(str(attachment.id))
    assert access.stored_file_id == stored_file.id
    with pytest.raises(FieldNoteQueryError):
        get_staff_field_note_attachment(
            db_session,
            GetStaffFieldNoteAttachment(
                work_order_public_id=data["task_only_work_order"].public_id,
                attachment_id=attachment.id,
            ),
        )


@pytest.mark.parametrize("limit, offset", [(0, 0), (201, 0), (20, -1)])
def test_staff_note_projection_rejects_invalid_paging(db_session, limit, offset):
    data = _fixture(db_session)

    with pytest.raises(FieldNoteQueryError):
        list_staff_field_work_order_notes(
            db_session,
            ListStaffFieldWorkOrderNotes(
                scope=WorkOrderFieldNoteScope(
                    work_order_public_id=data["ticket_work_order"].public_id
                ),
                access=StaffFieldNoteAccess(global_access=True),
                limit=limit,
                offset=offset,
            ),
        )
