"""Render smoke for the admin projects templates (Phase 3 PR 10).

Renders every projects page through the same Jinja environment the routes
use, with contexts produced by the real builders — catches template/context
drift that a compile-only check misses.
"""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProjectQuoteStatus,
    Vendor,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceStatus,
)
from app.schemas.project import ProjectCreate, ProjectTaskCreate
from app.services import web_dispatch_work_orders, web_projects
from app.services.projects import project_tasks, projects
from app.web.admin.projects import templates


class _State:
    csrf_token = "test-csrf-token"
    auth: dict = {"permission_keys": {"*"}}


class _URL:
    path = "/admin/projects"

    def __str__(self) -> str:
        return self.path


class DummyRequest:
    state = _State()
    query_params: dict = {}
    headers: dict = {}
    cookies: dict = {}
    url = _URL()
    session: dict = {}
    client = None
    scope: dict = {}

    def url_for(self, *args, **kwargs) -> str:
        return "/"


@pytest.fixture()
def base_context():
    return {
        "request": DummyRequest(),
        "active_page": "projects",
        "active_menu": "operations",
        "current_user": {"name": "Test Admin", "email": "admin@example.com"},
        "sidebar_stats": {},
    }


@pytest.fixture()
def fiber_project(db_session, subscriber):
    return projects.create(
        db_session,
        ProjectCreate(
            name="Fiber install render",
            project_type="fiber_optics_installation",
            subscriber_id=subscriber.id,
            region="Abuja",
        ),
    )


def _render(name: str, base: dict, extra: dict) -> str:
    context = dict(base)
    context.update(extra)
    html = templates.env.get_template(name).render(**context)
    assert html.strip()
    return html


def test_render_index_and_table(db_session, base_context, fiber_project):
    context = web_projects.build_projects_list_context(
        db_session,
        search=None,
        status=None,
        project_type=None,
        priority=None,
        region=None,
        filters=None,
        order_by="created_at",
        order_dir="desc",
        page=1,
        per_page=25,
    )
    html = _render("admin/projects/index.html", base_context, context)
    assert "Fiber install render" in html
    assert "/api/v1/projects/kanban" in html
    assert "/api/v1/projects/gantt" in html
    _render("admin/projects/_table.html", base_context, context)


def test_render_project_detail_with_stages(db_session, base_context, fiber_project):
    work_order = web_dispatch_work_orders.create_from_form(
        db_session,
        {
            "public_id": "sub-render-project-work",
            "subscriber_id": str(fiber_project.subscriber_id),
            "project_id": str(fiber_project.id),
            "title": "Render project visit",
            "status": "scheduled",
        },
    )
    context = web_projects.build_project_detail_context(
        db_session, project=fiber_project, can_read_work_orders=True
    )
    html = _render("admin/projects/project_detail.html", base_context, context)
    assert "Fiber Installation Stages" in html
    assert "Project Plan" in html
    assert work_order.public_id in html
    attachment_input = html.split(
        'data-testid="project-comment-attachments"', maxsplit=1
    )[1].split(">", maxsplit=1)[0]
    assert "cursor-pointer" in attachment_input
    assert "border-dashed" in attachment_input
    assert "data-mention-textarea" in html
    assert "data-mention-menu" in html
    assert "data-mention-select" not in html
    assert "Type <span" in html
    assert "to mention a staff member or team" in html


def test_render_project_detail_vendor_delivery_respects_finance_scope(
    db_session, base_context, fiber_project
):
    vendor = Vendor(name="Render Delivery Vendor", code=f"RDV-{uuid4().hex[:8]}")
    db_session.add(vendor)
    db_session.flush()
    installation = InstallationProject(
        project_id=fiber_project.id,
        assigned_vendor_id=vendor.id,
        status="in_progress",
    )
    db_session.add(installation)
    db_session.flush()
    db_session.add(
        ProjectQuote(
            project_id=installation.id,
            vendor_id=vendor.id,
            status=ProjectQuoteStatus.approved.value,
            currency="NGN",
            total=Decimal("750000.00"),
        )
    )
    db_session.add(
        VendorPurchaseInvoice(
            project_id=installation.id,
            vendor_id=vendor.id,
            invoice_number="RENDER-INV-01",
            status=VendorPurchaseInvoiceStatus.approved.value,
            currency="NGN",
            total=Decimal("750000.00"),
        )
    )
    db_session.commit()

    operations_context = web_projects.build_project_detail_context(
        db_session,
        project=fiber_project,
        can_read_vendor_operations=True,
    )
    operations_html = _render(
        "admin/projects/project_detail.html",
        base_context,
        operations_context,
    )
    assert "Vendor Delivery" in operations_html
    assert "Render Delivery Vendor" in operations_html
    assert "Purchase invoice" not in operations_html
    assert "750,000" not in operations_html

    finance_context = web_projects.build_project_detail_context(
        db_session,
        project=fiber_project,
        can_read_vendor_operations=True,
        can_read_vendor_financials=True,
    )
    finance_html = _render(
        "admin/projects/project_detail.html",
        base_context,
        finance_context,
    )
    assert "Purchase invoice" in finance_html
    assert "RENDER-INV-01" in finance_html
    assert "750,000" in finance_html


def test_render_project_forms(db_session, base_context, fiber_project):
    create_ctx = web_projects.build_project_form_context(db_session)
    create_ctx.update({"page_title": "New Project", "form_mode": "create"})
    _render("admin/projects/project_form.html", base_context, create_ctx)

    edit_ctx = web_projects.build_project_form_context(
        db_session, project=fiber_project
    )
    edit_ctx.update({"page_title": "Edit Project", "form_mode": "edit"})
    html = _render("admin/projects/project_form.html", base_context, edit_ctx)
    assert "Fiber install render" in html


def test_render_tasks_pages(db_session, base_context, fiber_project):
    task = project_tasks.create(
        db_session,
        ProjectTaskCreate(project_id=fiber_project.id, title="Render task"),
    )
    list_ctx = web_projects.build_tasks_list_context(
        db_session,
        project_id=None,
        status=None,
        priority=None,
        assigned_to_me=False,
        actor_id=None,
        filters=None,
        page=1,
        per_page=25,
        can_read_work_orders=True,
    )
    list_ctx["assigned"] = ""
    html = _render("admin/projects/tasks.html", base_context, list_ctx)
    assert "Render task" in html
    assert "Create Work Order" in html

    work_order = web_dispatch_work_orders.create_from_form(
        db_session,
        {
            "public_id": "sub-render-task-work",
            "subscriber_id": str(fiber_project.subscriber_id),
            "project_task_id": str(task.id),
            "title": "Render task visit",
            "status": "scheduled",
        },
    )
    detail_ctx = web_projects.build_task_detail_context(
        db_session, task=task, can_read_work_orders=True
    )
    detail_html = _render(
        "admin/projects/project_task_detail.html", base_context, detail_ctx
    )
    assert "Create Work Order" in detail_html
    assert work_order.public_id in detail_html

    form_ctx = web_projects.build_task_form_context(db_session)
    form_ctx.update({"page_title": "New Task", "form_mode": "create"})
    _render("admin/projects/project_task_form.html", base_context, form_ctx)


def test_task_detail_keeps_linked_work_visible_without_dispatch_write(
    db_session, base_context, fiber_project
):
    task = project_tasks.create(
        db_session,
        ProjectTaskCreate(project_id=fiber_project.id, title="Read-only task"),
    )
    work_order = web_dispatch_work_orders.create_from_form(
        db_session,
        {
            "public_id": "sub-read-only-task-work",
            "subscriber_id": str(fiber_project.subscriber_id),
            "project_task_id": str(task.id),
            "title": "Visible visit",
            "status": "scheduled",
        },
    )
    context = web_projects.build_task_detail_context(
        db_session, task=task, can_read_work_orders=True
    )
    request = DummyRequest()
    request.state = SimpleNamespace(
        csrf_token="test-csrf-token",
        auth={"permission_keys": {"operations:dispatch:read"}},
    )
    readonly_context = dict(base_context)
    readonly_context["request"] = request

    html = _render("admin/projects/project_task_detail.html", readonly_context, context)

    assert work_order.public_id in html
    assert "Create Work Order" not in html


def test_task_list_renders_open_and_many_labels(
    db_session, base_context, fiber_project
):
    one = project_tasks.create(
        db_session,
        ProjectTaskCreate(project_id=fiber_project.id, title="One field visit"),
    )
    many = project_tasks.create(
        db_session,
        ProjectTaskCreate(project_id=fiber_project.id, title="Many field visits"),
    )
    for public_id, task in (
        ("sub-render-list-one", one),
        ("sub-render-list-many-1", many),
        ("sub-render-list-many-2", many),
    ):
        web_dispatch_work_orders.create_from_form(
            db_session,
            {
                "public_id": public_id,
                "subscriber_id": str(fiber_project.subscriber_id),
                "project_task_id": str(task.id),
                "title": public_id,
                "status": "scheduled",
            },
        )
    context = web_projects.build_tasks_list_context(
        db_session,
        project_id=str(fiber_project.id),
        status=None,
        priority=None,
        assigned_to_me=False,
        actor_id=None,
        filters=None,
        page=1,
        per_page=25,
        can_read_work_orders=True,
    )
    context["assigned"] = ""

    html = _render("admin/projects/tasks.html", base_context, context)

    assert "Open Work Order" in html
    assert "View 2 Work Orders" in html


def test_render_template_admin_pages(db_session, base_context):
    template = web_projects.create_template_from_form(
        db_session, name="Render template"
    )
    web_projects.save_template_tasks_from_editor(
        db_session,
        template_id=str(template.id),
        tasks_json=(
            '[{"client_id": "a", "title": "Alpha", "description": "",'
            ' "effort_hours": 2, "dependencies": []},'
            ' {"client_id": "b", "title": "Beta", "description": "",'
            ' "effort_hours": null, "dependencies": ["a"]}]'
        ),
    )

    read_only_list_html = _render(
        "admin/projects/project_templates.html",
        base_context,
        {
            **web_projects.build_templates_list_context(db_session),
            "can_manage_project_templates": False,
        },
    )
    assert "New Template" not in read_only_list_html
    assert "Edit Tasks" not in read_only_list_html

    detail_context = web_projects.build_template_detail_context(
        db_session, template_id=str(template.id)
    )
    read_only_detail_html = _render(
        "admin/projects/project_template_detail.html",
        base_context,
        {**detail_context, "can_manage_project_templates": False},
    )
    assert "Alpha" in read_only_detail_html
    assert "Add Task" not in read_only_detail_html
    assert "Delete Template" not in read_only_detail_html

    manager_detail_html = _render(
        "admin/projects/project_template_detail.html",
        base_context,
        {**detail_context, "can_manage_project_templates": True},
    )
    assert "Edit Tasks" in manager_detail_html
    assert "Add Task" in manager_detail_html
    assert "Delete Template" in manager_detail_html

    form_ctx = web_projects.build_template_form_context(db_session)
    form_ctx.update({"page_title": "New Template", "form_mode": "create"})
    _render("admin/projects/project_template_form.html", base_context, form_ctx)

    task_form_ctx = web_projects.build_template_task_form_context(
        db_session, template=template
    )
    task_form_ctx.update({"page_title": "New Template Task", "form_mode": "create"})
    _render(
        "admin/projects/project_template_task_form.html", base_context, task_form_ctx
    )

    editor_ctx = {
        "template": template,
        "tasks_payload": web_projects.build_template_tasks_editor_payload(
            db_session, str(template.id)
        ),
    }
    editor_html = _render(
        "admin/projects/project_template_tasks_editor.html", base_context, editor_ctx
    )
    assert "templateTasksEditor" in editor_html
