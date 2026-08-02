from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.project import Project, ProjectTask
from app.models.subscriber import Subscriber, UserType
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.schemas.dispatch import TechnicianProfileCreate
from app.services import dispatch
from app.services import web_dispatch_work_orders as web_dispatch


def test_work_order_list_query_defaults_come_from_the_owner():
    query = web_dispatch.build_work_order_list_query()
    assert query.sort_by == "created_at"
    assert query.sort_dir == "desc"
    assert query.page == 1
    assert query.per_page == 25
    assert query.search is None
    assert query.filter_value("status") is None


def test_work_order_list_query_normalizes_status_search_and_page_size():
    valid_status = web_dispatch.STATUS_OPTIONS[0]
    assert (
        web_dispatch.build_work_order_list_query(status="bogus").filter_value("status")
        is None
    )
    assert (
        web_dispatch.build_work_order_list_query(status=valid_status).filter_value(
            "status"
        )
        == valid_status
    )
    assert web_dispatch.build_work_order_list_query(per_page=37).per_page == 25
    assert web_dispatch.build_work_order_list_query(per_page=50).per_page == 50
    normalized = web_dispatch.build_work_order_list_query(search="  fibre  ", page=0)
    assert normalized.search == "fibre"
    assert normalized.page == 1


def _subscriber(db_session, *, first_name: str = "Adaeze") -> Subscriber:
    sub = Subscriber(
        first_name=first_name,
        last_name="Nwosu",
        email=f"dispatch-{uuid4().hex[:8]}@example.com",
        account_number=f"DM{uuid4().hex[:6].upper()}",
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def _technician(db_session):
    user = SystemUser(
        first_name="Ade",
        last_name="Tech",
        email=f"tech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.commit()
    return dispatch.technician_profiles.create(
        db_session, TechnicianProfileCreate(system_user_id=user.id, region="Jabi")
    )


def test_list_page_counts_filters_and_options(db_session):
    sub = _subscriber(db_session)
    _technician(db_session)
    project = Project(name="Jabi fibre build", subscriber_id=sub.id)
    db_session.add(project)
    db_session.commit()
    ticket = Ticket(
        number="TKT-DISPATCH-ORIGIN",
        title="Customer outage",
        subscriber_id=sub.id,
        customer_account_id=sub.id,
        status="open",
        priority="high",
    )
    db_session.add(ticket)
    db_session.flush()
    work_order = web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-web-wo-1",
            "subscriber_id": str(sub.id),
            "project_id": str(project.id),
            "requires_as_built_evidence": "0",
            "title": "Fibre install",
            "status": "scheduled",
            "priority": "high",
            "work_type": "install",
            "address": "Plot 14, Jabi",
            "required_skills": "fiber, splicing",
            "tags": "native, install",
        },
    )
    work_order.origin_ticket_id = ticket.id
    db_session.commit()

    page = web_dispatch.list_page(
        db_session, status="scheduled", q="Jabi", page=1, per_page=25
    )

    assert page["total"] == 1
    assert page["counts"]["scheduled"] >= 1
    assert page["items"][0]["work_order"].public_id == "sub-web-wo-1"
    assert page["items"][0]["subscriber_label"]
    assert page["items"][0]["project_label"] == "Jabi fibre build"
    assert page["items"][0]["origin_ticket"].id == ticket.id
    assert page["items"][0]["work_order"].requires_as_built_evidence is False
    assert page["status_filter"] == "scheduled"
    assert page["subscriber_options"]
    assert {item["id"] for item in page["project_options"]} == {str(project.id)}
    assert page["technician_options"]


def test_task_deep_link_prefills_and_creates_authoritative_bindings(db_session):
    sub = _subscriber(db_session)
    project = Project(name="Gudu install", subscriber_id=sub.id)
    db_session.add(project)
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        number="TASK-GUDU-1",
        title="Install drop fibre",
        description="Splice, test, and document the drop fibre.",
        priority="high",
    )
    db_session.add(task)
    db_session.commit()

    page = web_dispatch.list_page(
        db_session,
        project_task_id=str(task.id),
        can_create=True,
    )
    prefill = page["create_prefill"]

    assert page["create_prefill_error"] is None
    assert prefill.subscriber_id == sub.id
    assert prefill.project_id == project.id
    assert prefill.project_task_id == task.id
    assert prefill.project_task_label == "TASK-GUDU-1"
    assert prefill.title == task.title
    assert prefill.description == task.description
    assert prefill.priority == "high"
    assert prefill.work_type == "install"

    work_order = web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-task-prefill",
            "subscriber_id": str(prefill.subscriber_id),
            "project_id": str(prefill.project_id),
            "project_task_id": str(prefill.project_task_id),
            "title": prefill.title,
            "status": "scheduled",
        },
    )

    assert work_order.project_id == project.id
    assert work_order.project_task_id == task.id


def test_task_deep_link_fails_closed_without_project_subscriber(db_session):
    project = Project(name="Unscoped install")
    db_session.add(project)
    db_session.flush()
    task = ProjectTask(project_id=project.id, title="Survey")
    db_session.add(task)
    db_session.commit()

    page = web_dispatch.list_page(
        db_session,
        project_task_id=str(task.id),
        can_create=True,
    )

    assert page["create_prefill"] is None
    assert page["create_prefill_error"] == (
        "Link a subscriber to the project before creating field work"
    )
    assert page["create_work_order_action"].allowed is False


def test_task_filter_applies_native_scope_with_search_status_and_pagination(
    db_session,
):
    sub = _subscriber(db_session)
    selected_project = Project(name="Selected project", subscriber_id=sub.id)
    other_project = Project(name="Other project", subscriber_id=sub.id)
    db_session.add_all([selected_project, other_project])
    db_session.flush()
    selected_task = ProjectTask(
        project_id=selected_project.id,
        title="Selected task",
    )
    other_task = ProjectTask(project_id=other_project.id, title="Other task")
    db_session.add_all([selected_task, other_task])
    db_session.flush()

    for index in range(11):
        web_dispatch.create_from_form(
            db_session,
            {
                "public_id": f"sub-filter-selected-{index:02d}",
                "subscriber_id": str(sub.id),
                "project_task_id": str(selected_task.id),
                "title": f"Needle visit {index:02d}",
                "status": "scheduled",
            },
        )
    web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-filter-selected-completed",
            "subscriber_id": str(sub.id),
            "project_task_id": str(selected_task.id),
            "title": "Needle completed",
            "status": "draft",
        },
    )
    web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-filter-other",
            "subscriber_id": str(sub.id),
            "project_task_id": str(other_task.id),
            "title": "Needle other task",
            "status": "scheduled",
        },
    )

    first = web_dispatch.list_page(
        db_session,
        project_task_id=str(selected_task.id),
        q="Needle",
        status="scheduled",
        page=1,
        per_page=10,
        can_create=False,
    )
    second = web_dispatch.list_page(
        db_session,
        project_task_id=str(selected_task.id),
        q="Needle",
        status="scheduled",
        page=2,
        per_page=10,
        can_create=False,
    )

    assert first["total"] == 11
    assert first["total_pages"] == 2
    assert len(first["items"]) == 10
    assert len(second["items"]) == 1
    assert first["project_task_filter"] == str(selected_task.id)
    assert first["create_prefill"] is None
    assert all(
        item["work_order"].project_task_id == selected_task.id
        and item["work_order"].status == "scheduled"
        and "Needle" in item["work_order"].title
        for item in [*first["items"], *second["items"]]
    )
    assert first["counts"]["total"] >= 13


def test_task_filter_rejects_invalid_identifier(db_session):
    with pytest.raises(HTTPException) as exc:
        web_dispatch.list_page(db_session, project_task_id="not-a-uuid")

    assert exc.value.status_code == 404


def test_archived_task_filter_remains_readable_but_creation_fails_closed(db_session):
    sub = _subscriber(db_session)
    project = Project(name="Archived task project", subscriber_id=sub.id)
    db_session.add(project)
    db_session.flush()
    task = ProjectTask(project_id=project.id, title="Archive after issue")
    db_session.add(task)
    db_session.flush()
    work_order = web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-archived-task-filter",
            "subscriber_id": str(sub.id),
            "project_task_id": str(task.id),
            "title": "Existing visit",
            "status": "scheduled",
        },
    )
    task.is_active = False
    db_session.commit()

    page = web_dispatch.list_page(
        db_session,
        project_task_id=str(task.id),
        can_create=True,
    )

    assert [item["work_order"].public_id for item in page["items"]] == [
        work_order.public_id
    ]
    assert page["create_prefill"] is None
    assert page["create_work_order_action"].allowed is False
    assert page["create_prefill_error"] == "Project task not found"


def test_generic_creation_state_is_unchanged_without_task_filter(db_session):
    page = web_dispatch.list_page(db_session, can_create=True)

    assert page["project_task_filter"] == ""
    assert page["create_prefill"] is None
    assert page["create_prefill_error"] is None
    assert page["create_work_order_action"].allowed is True


def test_update_and_queue_from_form(db_session):
    sub = _subscriber(db_session)
    tech = _technician(db_session)
    web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-web-wo-2",
            "subscriber_id": str(sub.id),
            "title": "Router swap",
            "status": "scheduled",
        },
    )

    updated = web_dispatch.update_from_form(
        db_session,
        "sub-web-wo-2",
        {
            "title": "Router swap & test",
            "status": "dispatched",
            "priority": "normal",
            "work_type": "repair",
            "assigned_to_name": "Ignored parallel assignment",
            "scheduled_start": "2026-07-09T09:00",
            "scheduled_end": "2026-07-09T10:00",
            "estimated_duration_minutes": "60",
        },
    )
    assert updated.status == "scheduled"
    assert updated.assigned_to_name is None
    assert updated.scheduled_start is not None
    assert updated.requires_as_built_evidence is True

    updated = web_dispatch.update_from_form(
        db_session,
        "sub-web-wo-2",
        {
            "title": "Router swap & test",
            "requires_as_built_evidence": "0",
        },
    )
    assert updated.requires_as_built_evidence is False

    queued = web_dispatch.queue_assignment_from_form(
        db_session,
        "sub-web-wo-2",
        {
            "assigned_technician_id": str(tech.id),
            "status": "assigned",
            "reason": "Morning route",
        },
    )

    assert queued.crm_work_order_id == "sub-web-wo-2"
    assert queued.status == "assigned"
    db_session.refresh(updated)
    assert updated.status == "dispatched"
    assert updated.assigned_to_name == "Ade Tech"


def test_detail_page_exposes_authoritative_context_and_assignment_action(db_session):
    sub = _subscriber(db_session)
    project = Project(name="Lekki rollout", subscriber_id=sub.id)
    db_session.add(project)
    db_session.flush()
    task = ProjectTask(project_id=project.id, number="TASK-LEKKI-1", title="Survey")
    ticket = Ticket(
        number="TKT-WO-DETAIL",
        title="Access fault",
        subscriber_id=sub.id,
        customer_account_id=sub.id,
        status="open",
        priority="normal",
    )
    db_session.add_all([task, ticket])
    db_session.flush()
    work_order = web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-detail-context",
            "subscriber_id": str(sub.id),
            "project_id": str(project.id),
            "project_task_id": str(task.id),
            "title": "Inspect access fibre",
            "status": "scheduled",
        },
    )
    work_order.origin_ticket_id = ticket.id
    db_session.commit()

    detail = web_dispatch.detail_page(db_session, work_order.public_id)

    assert detail["work_order"].id == work_order.id
    assert detail["subscriber"].id == sub.id
    assert detail["project"].id == project.id
    assert detail["project_task"].id == task.id
    assert detail["origin_ticket"].id == ticket.id
    assert detail["queue_action"].allowed is True


def test_detail_page_rejects_unknown_public_id(db_session):
    with pytest.raises(HTTPException) as exc:
        web_dispatch.detail_page(db_session, "sub-missing")

    assert exc.value.status_code == 404


def test_queue_assignment_requires_technician(db_session):
    sub = _subscriber(db_session)
    web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-web-wo-3",
            "subscriber_id": str(sub.id),
            "title": "Install",
            "status": "scheduled",
        },
    )

    with pytest.raises(HTTPException) as exc:
        web_dispatch.queue_assignment_from_form(
            db_session,
            "sub-web-wo-3",
            {"assigned_technician_id": "", "status": "assigned"},
        )
    assert exc.value.status_code == 422
