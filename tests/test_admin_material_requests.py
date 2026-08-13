from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute

from app.api import search as search_api
from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.field_material import FieldInventoryItem, FieldMaterialRequest
from app.models.project import Project, ProjectTask
from app.models.subscriber import Subscriber, UserType
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import backoffice
from app.services.field import material_catalog, material_requests
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
    item = FieldInventoryItem(
        source_system="dotmac_erp",
        source_item_id=str(uuid4()),
        sku="DROP-100",
        name="Drop cable",
        unit="m",
        field_request_eligible=True,
    )
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

    row = db_session.get(FieldMaterialRequest, created.id)
    assert row is not None
    assert row.fulfillment_channel == "erp"
    assert row.approved_at is None
    assert row.status == "submitted"


def test_staff_material_request_does_not_require_assignment(db_session):
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
    item = FieldInventoryItem(
        source_system="dotmac_erp",
        source_item_id=str(uuid4()),
        name="Connector",
        unit="pcs",
        field_request_eligible=True,
    )
    db_session.add_all([work_order, item])
    db_session.commit()
    command_id = uuid4()

    created = material_requests.create_staff_material_request(
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

    assert created.work_order_id == work_order.id
    assert db_session.query(FieldMaterialRequest).count() == 1


def test_material_requests_project_through_ticket_project_and_task(db_session):
    user, _technician, work_order, item = _assigned_work_order(db_session)
    ticket = Ticket(title="Customer signal fault")
    project = Project(name="Restore customer service")
    db_session.add_all([ticket, project])
    db_session.flush()
    task = ProjectTask(project_id=project.id, ticket_id=ticket.id, title="Field repair")
    db_session.add(task)
    db_session.flush()
    work_order.project_id = project.id
    work_order.project_task_id = task.id
    db_session.commit()

    command_id = uuid4()
    created = material_requests.create_staff_material_request(
        db_session,
        material_requests.CreateStaffMaterialRequest(
            context=_context(user.id, command_id, "Request scoped materials"),
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

    for scope in (
        material_requests.MaterialRequestScope(ticket_id=ticket.id),
        material_requests.MaterialRequestScope(project_id=project.id),
        material_requests.MaterialRequestScope(project_task_id=task.id),
    ):
        page = material_requests.list_staff_material_requests(db_session, scope=scope)
        options = material_requests.staff_material_request_form_options(
            db_session, scope=scope
        )
        assert [row.id for row in page.items] == [created.id]
        assert [row.id for row in options.work_orders] == [work_order.id]

    empty = material_requests.list_staff_material_requests(
        db_session,
        scope=material_requests.MaterialRequestScope(ticket_id=uuid4()),
    )
    assert empty.items == ()


def test_material_request_delivery_projection_exposes_retry_state(db_session):
    user, _technician, work_order, item = _assigned_work_order(db_session)
    command_id = uuid4()
    created = material_requests.create_staff_material_request(
        db_session,
        material_requests.CreateStaffMaterialRequest(
            context=_context(user.id, command_id, "Request delivery materials"),
            work_order_id=work_order.id,
            request_id=command_id,
            priority=material_requests.MaterialRequestPriority.HIGH,
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
    ownership = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.material_request.value)
        .one_or_none()
    )
    if ownership is None:
        ownership = SyncFlowOwnership(
            flow=FieldErpSyncFlow.material_request.value,
            owner=SyncFlowOwner.sub.value,
        )
        db_session.add(ownership)
    else:
        ownership.owner = SyncFlowOwner.sub.value
    db_session.add(
        FieldErpSyncEvent(
            flow=FieldErpSyncFlow.material_request.value,
            entity_type="field_material_request",
            entity_id=created.id,
            idempotency_key=f"test-material-{created.id}",
            payload={"omni_id": str(created.id)},
            status=FieldErpSyncStatus.pending.value,
            attempts=2,
            last_error="ERP temporarily unavailable",
        )
    )
    db_session.commit()

    delivery = backoffice.get_material_request_delivery(db_session, created.id)

    assert delivery.sub_owns_delivery is True
    assert delivery.event_status == FieldErpSyncStatus.pending.value
    assert delivery.attempts == 2
    assert delivery.last_error == "ERP temporarily unavailable"


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
            "/operations/material-requests/{request_id}/cancel",
            "POST",
            "operations:material_request:write",
        ),
    ):
        assert _route_has_permission(path, method, permission)

    for template_name in (
        "admin/material_requests/index.html",
        "admin/material_requests/form.html",
        "admin/material_requests/detail.html",
        "admin/material_requests/_context_panel.html",
    ):
        material_requests_web.templates.env.get_template(template_name)

    sidebar = Path("templates/components/navigation/admin_sidebar.html").read_text()
    work_order_detail = Path(
        "templates/admin/dispatch/work_order_detail.html"
    ).read_text()
    ticket_detail = Path("templates/admin/support/tickets/detail.html").read_text()
    project_detail = Path("templates/admin/projects/project_detail.html").read_text()
    task_detail = Path("templates/admin/projects/project_task_detail.html").read_text()
    assert "/admin/operations/material-requests" in sidebar
    assert "'material-requests': 'material-requests'" in sidebar
    assert "/admin/operations/material-requests/new?work_order_id=" in work_order_detail
    for detail_template in (ticket_detail, project_detail, task_detail):
        assert "admin/material_requests/_context_panel.html" in detail_template


def test_material_item_typeahead_searches_only_active_eligible_erp_items(db_session):
    eligible = FieldInventoryItem(
        source_system="dotmac_erp",
        source_item_id="erp-cable-1",
        sku="CAT6-BLUE",
        name="Blue CAT6 Cable",
        category_name="Network Cabling",
        source_is_active=True,
        is_active=True,
        field_request_eligible=True,
    )
    ineligible = FieldInventoryItem(
        source_system="dotmac_erp",
        source_item_id="erp-cable-2",
        sku="CAT6-RED",
        name="Red CAT6 Cable",
        source_is_active=True,
        is_active=True,
        field_request_eligible=False,
    )
    inactive = FieldInventoryItem(
        source_system="dotmac_erp",
        source_item_id="erp-cable-3",
        sku="CAT6-OLD",
        name="Old CAT6 Cable",
        source_is_active=False,
        is_active=False,
        field_request_eligible=True,
    )
    db_session.add_all((eligible, ineligible, inactive))
    db_session.flush()

    matches = material_catalog.search_eligible_material_items(
        db_session,
        material_catalog.SearchEligibleMaterialItems(query="cat6", limit=20),
    )

    assert [(row.id, row.label) for row in matches] == [
        (eligible.id, "Blue CAT6 Cable (CAT6-BLUE)")
    ]
    assert (
        material_catalog.search_eligible_material_items(
            db_session,
            material_catalog.SearchEligibleMaterialItems(query="c", limit=20),
        )
        == ()
    )


def test_material_request_form_uses_dynamic_item_typeahead():
    source = Path("templates/admin/material_requests/form.html").read_text()

    assert 'data-typeahead-url="/api/v1/search/material-items"' in source
    assert "data-typeahead-input" in source
    assert 'name="item_id" data-typeahead-hidden' in source
    assert '<select name="item_id"' not in source
    assert "typeahead.removeAttribute('data-typeahead-ready')" in source
    assert "window.initTypeaheadFields(row)" in source


def test_material_item_typeahead_endpoint_requires_material_write_permission():
    route = next(
        route
        for route in search_api.router.routes
        if isinstance(route, APIRoute) and route.path == "/search/material-items"
    )
    dependencies = route.dependant.dependencies
    assert any(
        "operations:material_request:write" in str(cell.cell_contents)
        for dependency in dependencies
        for cell in (getattr(dependency.call, "__closure__", None) or ())
    )
