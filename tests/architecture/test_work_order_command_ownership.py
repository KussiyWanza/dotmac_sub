from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.source_index import (
    call_lines,
    python_ast,
    python_files,
    source_text,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICES = PROJECT_ROOT / "app/services"
OWNER = SERVICES / "work_order_commands.py"
DISPATCH = SERVICES / "dispatch.py"
MANAGER = SERVICES / "field/manager.py"
WEB = SERVICES / "web_dispatch_work_orders.py"


def _constructors(path: Path) -> set[str]:
    return set(call_lines(path))


def test_work_order_rows_have_one_native_constructor():
    native_constructors: list[str] = []
    queue_constructors: list[str] = []
    for path in python_files(SERVICES):
        constructors = _constructors(path)
        if "WorkOrder" in constructors:
            native_constructors.append(str(path.relative_to(PROJECT_ROOT)))
        if "WorkOrderAssignmentQueue" in constructors:
            queue_constructors.append(str(path.relative_to(PROJECT_ROOT)))

    assert native_constructors == ["app/services/work_order_commands.py"]
    assert queue_constructors == ["app/services/work_order_commands.py"]


def test_assignment_projection_has_no_parallel_service_writer():
    assignment_fields = {
        "assigned_to_crm_person_id",
        "assigned_to_name",
        "technician_name",
        "assigned_technician_id",
    }
    writers: set[str] = set()
    for path in python_files(SERVICES):
        tree = python_ast(path)
        targets = [
            target
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        ]
        if any(
            isinstance(target, ast.Attribute) and target.attr in assignment_fields
            for target in targets
        ):
            writers.add(str(path.relative_to(PROJECT_ROOT)))

    assert writers == {"app/services/work_order_commands.py"}


def test_dispatch_adapters_delegate_and_cannot_write_assignment_state_directly():
    dispatch_source = source_text(DISPATCH)
    manager_source = source_text(MANAGER)
    web_source = source_text(WEB)

    assert "work_order_commands.create(" in dispatch_source
    assert "work_order_commands.update_header(" in dispatch_source
    assert "work_order_commands.create_queue_entry(" in dispatch_source
    assert "work_order_commands.update_queue_entry(" in dispatch_source
    assert "work_order_commands.assign(" in manager_source
    assert "assigned_to_name=" not in web_source
    for source in (dispatch_source, manager_source, web_source):
        assert "WorkOrder(" not in source
        assert "WorkOrderAssignmentQueue(" not in source


def test_retired_crm_work_order_importer_is_absent():
    assert not (SERVICES / "work_orders_mirror.py").exists()


def test_assignment_readers_ignore_non_assigned_queue_rows():
    for relative_path in (
        "field/jobs.py",
        "field/manager.py",
        "customer_work_order_selfcare.py",
        "workqueue/providers/work_orders.py",
    ):
        source = source_text(SERVICES / relative_path)
        assert "DispatchQueueStatus.assigned" in source
