"""Prevent web adapters from becoming a second Lead date-policy owner."""

import ast
from pathlib import Path


def _function(path, name):
    tree = ast.parse(Path(path).read_text())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_success_and_failure_delegate_date_interpretation_to_sales_owner():
    owner = ast.unparse(
        _function("app/services/sales/service.py", "_normalize_lead_list_query")
    )
    assert "normalize_lead_date_range(request)" in owner
    recovery = ast.unparse(
        _function("app/services/web_sales.py", "build_leads_failure_context")
    )
    assert "sales_service.normalize_lead_date_range(" in recovery
    assert "sales_service.LeadListQueryInput(" in recovery
    route = ast.unparse(_function("app/web/admin/sales.py", "leads_list"))
    assert "date_preset=date_preset" in route
    assert "timedelta" not in route
    assert "fromisoformat" not in route


def test_created_date_predicate_is_shared_and_index_friendly():
    predicate = ast.unparse(
        _function("app/services/sales/service.py", "_lead_list_predicates")
    )
    assert "Lead.created_at >= created_from" in predicate
    assert "Lead.created_at < created_to_exclusive" in predicate
    assert "func.date" not in predicate
    for name in ("build_leads_list_context", "build_leads_failure_context"):
        adapter = ast.unparse(_function("app/services/web_sales.py", name))
        assert "fromisoformat" not in adapter
        assert "timedelta" not in adapter


def test_date_controls_keep_native_submission_and_existing_csp():
    html = Path("templates/admin/sales/leads/index.html").read_text()
    assert 'name="page" value="1"' in html
    assert 'nonce="{{ csp_nonce }}"' in html
    assert "admin/sales/leads/_date_filters.html" in html
    controls = Path("templates/admin/sales/leads/_date_filters.html").read_text()
    for name in ("date_preset", "date_from", "date_to"):
        assert f'name="{name}"' in controls
