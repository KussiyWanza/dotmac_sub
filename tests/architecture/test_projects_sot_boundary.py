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
    creation_email = next(
        concern
        for concern in lifecycle.contract.concerns
        if concern.name == "project creation customer email consequence"
    )
    assert creation_email.input_names == (
        "canonical project aggregate",
        "customer communication delivery intent",
    )
    relationship_integrity = next(
        concern
        for concern in lifecycle.contract.concerns
        if concern.name
        == "project-task relationship integrity and completion readiness"
    )
    assert relationship_integrity.input_names == (
        "canonical project aggregate",
        "project transition protocol",
        "authorized project command",
    )
    status_change = next(
        concern
        for concern in lifecycle.contract.concerns
        if concern.name
        == "project and task status-change customer notification consequence"
    )
    assert status_change.input_names == (
        "canonical project aggregate",
        "project transition protocol",
        "customer communication delivery intent",
    )
    finance_completion = next(
        concern
        for concern in lifecycle.contract.concerns
        if concern.name == "project completion finance email consequence"
    )
    assert finance_completion.input_names == (
        "canonical project aggregate",
        "project transition protocol",
        "project completion finance notification policy",
        "staff notification delivery queue",
    )
    reassignment = next(
        concern
        for concern in lifecycle.contract.concerns
        if concern.name == "project and task staff assignment notification consequence"
    )
    assert reassignment.input_names == (
        "canonical project aggregate",
        "active project assignment audience",
        "staff notification delivery queue",
    )
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


def test_project_task_relationship_integrity_stays_in_lifecycle_owner() -> None:
    service = (ROOT / "app/services/projects.py").read_text()
    schemas = (ROOT / "app/schemas/project.py").read_text()
    api = (ROOT / "app/api/projects.py").read_text()

    assert "class ProjectTaskStatusTransition" in schemas
    assert "class ProjectTaskDependenciesReplace" in schemas
    assert "def _lock_project_task_scope(" in service
    assert "def _require_task_completion_ready(" in service
    assert "def _dependency_graph_has_cycle(" in service
    assert "def transition_status(" in service
    assert '"status" in payload.model_fields_set and not _status_transition' in service
    assert "def replace_dependencies(" in service
    assert '"/project-tasks/{task_id}/transition"' in api
    assert '"/project-tasks/{task_id}/dependencies"' in api
    assert "ProjectTaskDependency(" not in api


def test_task_reassignment_email_is_owned_by_project_lifecycle() -> None:
    service = (ROOT / "app/services/projects.py").read_text()
    adapters = "\n".join(
        (ROOT / relative).read_text()
        for relative in ("app/api/projects.py", "app/web/admin/projects.py")
    )

    assert "_notify_new_project_task_assignees(" in service
    assert "previous_assignee_ids = frozenset(task.assigned_to_person_ids)" in service
    assert "execute_owner_savepoint(" in service
    assert 'action="assignment_notification_failed"' in service
    assert "include_push=True" in service
    assert "queue_staff_email(" not in adapters


def test_project_creation_customer_email_is_owned_by_project_lifecycle() -> None:
    service = (ROOT / "app/services/projects.py").read_text()

    assert "_stage_customer_project_created_email(" in service
    assert 'event_type="project_created"' in service
    assert "default_channels=(NotificationChannel.email,)" in service
    assert 'action="creation_customer_email_failed"' in service


def test_customer_status_notifications_are_owned_by_project_lifecycle() -> None:
    service = (ROOT / "app/services/projects.py").read_text()
    adapters = "\n".join(
        (ROOT / relative).read_text()
        for relative in ("app/api/projects.py", "app/web/admin/projects.py")
    )

    assert "class ProjectCustomerStatusNotificationCommand" in service
    assert "def _stage_customer_status_transition(" in service
    assert 'event_type = "project_status_changed"' in service
    assert 'event_type = "project_task_status_changed"' in service
    assert 'action="customer_status_notification_failed"' in service
    assert "request_update(" not in adapters


def test_project_completion_finance_notification_is_configured_not_hardcoded() -> None:
    service = (ROOT / "app/services/projects.py").read_text()
    settings = (ROOT / "app/services/settings_spec.py").read_text()

    assert "def _notify_finance_project_completed(" in service
    assert '"project_completed_finance"' in service
    assert '"project_completion_finance_email_recipients"' in service
    assert '"project_completion_finance_permission_key"' in service
    assert "finance@test.local" not in service
    assert "finance@dotmac" not in service
    assert 'key="project_completion_finance_email_recipients"' in settings


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
    project_detail = (ROOT / "templates/admin/projects/project_detail.html").read_text()
    assert "/admin/projects/tasks/{{ task.id }}" in project_detail
    assert "/admin/projects/tasks/{{ task.number or task.id }}" not in project_detail


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
