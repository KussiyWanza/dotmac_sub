from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.field_material import FieldInventoryItem, FieldMaterialRequest
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.field import material_requests
from app.services.owner_commands import CommandContext
from app.web.admin import material_requests as material_requests_web


@pytest.fixture(autouse=True)
def _plain_attributes_across_owner_commits(db_session):
    db_session.expire_on_commit = False


def _context(user_id: UUID, command_id: UUID, reason: str) -> CommandContext:
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{user_id}",
        scope="operations:material_request:write",
        reason=reason,
        idempotency_key=f"material-request:{command_id}",
    )


def _assigned_work_order(db_session):
    user = SystemUser(
        first_name="Amina",
        last_name="Installer",
        display_name="Amina Installer",
        email=f"material-admin-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    subscriber = Subscriber(
        first_name="Material",
        last_name="Customer",
        email=f"material-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([user, subscriber])
    db_session.flush()
    technician = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        title="Installer",
    )
    work_order = WorkOrder(
        subscriber_id=subscriber.id,
        public_id=f"sub-material-{uuid4().hex[:8]}",
        title="Install customer drop",
        status="scheduled",
    )
    item = FieldInventoryItem(sku="DROP-100", name="Drop cable", unit="m")
    db_session.add_all([technician, work_order, item])
    db_session.flush()
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=work_order.id,
            status=DispatchQueueStatus.assigned,
            assigned_technician_id=technician.id,
        )
    )
    db_session.commit()
    return user, technician, work_order, item


def test_staff_material_request_create_review_and_replay(db_session):
    user, technician, work_order, item = _assigned_work_order(db_session)
    create_id = uuid4()
    create = material_requests.CreateStaffMaterialRequest(
        context=_context(user.id, create_id, "Request installation materials"),
        work_order_id=work_order.id,
        request_id=create_id,
        priority=material_requests.MaterialRequestPriority.HIGH,
        source_warehouse_code="ABJ-STORES",
        notes="Required for the scheduled installation",
        items=(
            material_requests.MaterialRequestLineInput(
                item_id=item.id,
                quantity=120,
                notes="Allow for service loop",
            ),
        ),
    )

    created = material_requests.create_staff_material_request(db_session, create)
    replayed = material_requests.create_staff_material_request(db_session, create)

    assert replayed.id == created.id
    assert created.status is material_requests.MaterialRequestStatus.SUBMITTED
    assert created.requested_by_person_id == technician.person_id
    assert created.requested_by_system_user_id == user.id
    assert created.items[0].name == "Drop cable"
    assert created.items[0].quantity == 120
    assert db_session.query(FieldMaterialRequest).count() == 1
    db_session.commit()

    approve_id = uuid4()
    approval = material_requests.ReviewMaterialRequest(
        context=_context(user.id, approve_id, "Approve installation materials"),
        request_id=created.id,
    )
    approved = material_requests.approve_material_request(db_session, approval)
    replayed_approval = material_requests.approve_material_request(db_session, approval)

    assert approved.status is material_requests.MaterialRequestStatus.APPROVED
    assert replayed_approval.status is material_requests.MaterialRequestStatus.APPROVED
    row = db_session.get(FieldMaterialRequest, created.id)
    assert row is not None
    events = row.metadata_["manager_events"]
    assert events[-1]["actor"] == f"user:{user.id}"
    assert events[-1]["command_id"] == str(approve_id)


def test_staff_material_request_requires_active_assignment(db_session):
    user = SystemUser(
        first_name="Unassigned",
        last_name="Operator",
        email=f"unassigned-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    subscriber = Subscriber(
        first_name="Unassigned",
        last_name="Customer",
        email=f"unassigned-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([user, subscriber])
    db_session.flush()
    work_order = WorkOrder(
        subscriber_id=subscriber.id,
        title="Unassigned field visit",
        status="scheduled",
    )
    item = FieldInventoryItem(name="Connector", unit="pcs")
    db_session.add_all([work_order, item])
    db_session.commit()
    command_id = uuid4()

    with pytest.raises(material_requests.MaterialRequestError) as exc:
        material_requests.create_staff_material_request(
            db_session,
            material_requests.CreateStaffMaterialRequest(
                context=_context(user.id, command_id, "Request materials"),
                work_order_id=work_order.id,
                request_id=command_id,
                priority=material_requests.MaterialRequestPriority.MEDIUM,
                source_warehouse_code="ABJ-STORES",
                notes=None,
                items=(
                    material_requests.MaterialRequestLineInput(
                        item_id=item.id,
                        quantity=1,
                    ),
                ),
            ),
        )

    assert exc.value.code == "operations.material_dependencies.assignment_required"
    assert db_session.query(FieldMaterialRequest).count() == 0


def _route_has_permission(path: str, method: str, permission: str) -> bool:
    route = next(
        route
        for route in material_requests_web.router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    )
    for dependency in route.dependant.dependencies:
        closure = getattr(dependency.call, "__closure__", None) or ()
        if any(permission in str(cell.cell_contents) for cell in closure):
            return True
    return False


def test_material_request_web_routes_templates_and_navigation_are_complete():
    for path, method, permission in (
        ("/operations/material-requests", "GET", "operations:material_request:read"),
        ("/operations/material-requests", "POST", "operations:material_request:write"),
        (
            "/operations/material-requests/{request_id}",
            "GET",
            "operations:material_request:read",
        ),
        (
            "/operations/material-requests/{request_id}/approve",
            "POST",
            "operations:material_request:write",
        ),
        (
            "/operations/material-requests/{request_id}/reject",
            "POST",
            "operations:material_request:write",
        ),
    ):
        assert _route_has_permission(path, method, permission)

    for template_name in (
        "admin/material_requests/index.html",
        "admin/material_requests/form.html",
        "admin/material_requests/detail.html",
    ):
        material_requests_web.templates.env.get_template(template_name)

    sidebar = Path("templates/components/navigation/admin_sidebar.html").read_text()
    work_order_detail = Path(
        "templates/admin/dispatch/work_order_detail.html"
    ).read_text()
    assert "/admin/operations/material-requests" in sidebar
    assert "'material-requests': 'material-requests'" in sidebar
    assert "/admin/operations/material-requests/new?work_order_id=" in work_order_detail
