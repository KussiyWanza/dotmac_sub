from pathlib import Path

from app.services.sot_manifest import TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_projects_owners_have_complete_typed_contracts() -> None:
    lifecycle = service_relationship("operations.project_lifecycle")
    assignment = service_relationship("operations.project_assignment_policy")
    projection = service_relationship("ui.project_list_projection")
    vendor_delivery = service_relationship("ui.project_vendor_delivery_projection")
    work_order_projection = service_relationship("ui.work_order_list_projection")

    assert lifecycle.contract is not None
    assert lifecycle.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert assignment.contract is not None
    assert assignment.contract.transaction.mode is TransactionMode.READ_ONLY
    assert projection.contract is not None
    assert projection.contract.transaction.mode is TransactionMode.READ_ONLY
    assert vendor_delivery.contract is not None
    assert vendor_delivery.contract.transaction.mode is TransactionMode.READ_ONLY
    assert work_order_projection.contract is not None
    assert work_order_projection.contract.transaction.mode is TransactionMode.READ_ONLY


def test_ticket_assignment_engine_does_not_write_project_state() -> None:
    source = (ROOT / "app/services/ticket_assignment/engine.py").read_text()
    forbidden = (
        "project.manager_person_id =",
        "project.project_manager_person_id =",
        "project.assistant_manager_person_id =",
        "project.service_team_id =",
        "task.assigned_to_person_id =",
        "task.assignees.append(",
    )
    assert not [token for token in forbidden if token in source]


def test_project_adapters_do_not_complete_transactions() -> None:
    for relative in ("app/api/projects.py", "app/web/admin/projects.py"):
        source = (ROOT / relative).read_text()
        assert ".commit(" not in source


def test_project_ui_does_not_write_or_join_work_order_bindings() -> None:
    route = (ROOT / "app/web/admin/projects.py").read_text()
    projection = (ROOT / "app/services/web_projects.py").read_text()
    templates = "\n".join(
        (ROOT / relative).read_text()
        for relative in (
            "templates/admin/projects/tasks.html",
            "templates/admin/projects/project_detail.html",
            "templates/admin/projects/project_task_detail.html",
        )
    )

    assert "WorkOrder(" not in route
    assert "WorkOrder(" not in projection
    assert ".project_task_id =" not in projection
    assert "crm_project_id" not in projection
    assert "crm_work_order_id" not in templates
    assert "list_task_work_order_summaries_bulk" in projection


def test_project_vendor_delivery_projection_is_read_only_and_permission_scoped() -> (
    None
):
    projection = (ROOT / "app/services/project_vendor_delivery.py").read_text()
    route = (ROOT / "app/web/admin/projects.py").read_text()
    template = (ROOT / "templates/admin/projects/project_detail.html").read_text()

    assert ".commit(" not in projection
    assert ".flush(" not in projection
    assert "InstallationProject(" not in projection
    assert "ProjectQuote(" not in projection
    assert 'can_read_vendor_operations=can(request, "inventory:read")' in route
    assert 'can_read_vendor_routes=can(request, "network:fiber:read")' in route
    assert 'can_read_vendor_financials=can(request, "finance:ap:read")' in route
    assert "vendor_delivery.invoice" in template
    assert "project_vendor_payment_status" in projection
