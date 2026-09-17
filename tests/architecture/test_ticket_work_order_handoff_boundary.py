import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ticket_tags_and_metadata_have_no_work_order_command_authority():
    support_source = (ROOT / "app/services/support.py").read_text()

    assert "_ensure_field_visit_work_order" not in support_source
    assert 'metadata["work_order_id"]' not in support_source
    assert "field_visit" not in support_source


def test_only_handoff_owner_passes_native_origin_to_work_order_command():
    callsites = []
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "work_order_commands.py":
            continue
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Call)
            and "work_order_commands" in ast.unparse(node.func)
            and any(keyword.arg == "origin_ticket_id" for keyword in node.keywords)
            for node in ast.walk(tree)
        ):
            callsites.append(path.relative_to(ROOT).as_posix())

    assert callsites == [
        "app/services/ticket_work_order_handoff.py",
        "app/services/topology/outage_work_order_handoff.py",
    ]


def test_generic_dispatch_write_contract_cannot_change_native_ticket_origin():
    schema_source = (ROOT / "app/schemas/dispatch.py").read_text()
    create_section = schema_source.split("class WorkOrderHeaderCreate", 1)[1].split(
        "class WorkOrderHeaderUpdate", 1
    )[0]
    update_section = schema_source.split("class WorkOrderHeaderUpdate", 1)[1].split(
        "class WorkOrderHeaderRead", 1
    )[0]

    assert "origin_ticket_id" not in create_section
    assert "origin_ticket_id" not in update_section
