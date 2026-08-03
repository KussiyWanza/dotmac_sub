from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_payment_export_route_delegates_complete_csv_scope_to_list_owner():
    route_path = PROJECT_ROOT / "app/web/admin/billing_payments.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "payments_export_csv"
    )
    calls = {
        ast.unparse(node.func) for node in ast.walk(route) if isinstance(node, ast.Call)
    }
    source = ast.unparse(route)

    assert "web_billing_payments_service.build_payments_list_query" in calls
    assert "web_billing_payments_service.stream_payments_csv" in calls
    assert "web_billing_payments_service.build_payments_list_data" not in calls
    assert "web_billing_payments_service.render_payments_csv" not in calls
    assert "10000" not in source
    assert "csv.writer" not in calls
