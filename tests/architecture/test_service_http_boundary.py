"""New domain/application owners stay independent of HTTP frameworks."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PURE_SERVICE_MODULES = (
    ROOT / "app/services/dotmac_erp/purchase_invoice_sync.py",
    ROOT / "app/services/vendor_payment_status.py",
    ROOT / "app/services/vendor_portal_operations.py",
    ROOT / "app/services/vendor_as_built_review_proposals.py",
    ROOT / "app/services/vendor_project_review_proposals.py",
    ROOT / "app/services/vendor_route_review_proposals.py",
    ROOT / "app/services/vendor_submission_proposals.py",
    ROOT / "app/services/work_order_errors.py",
    ROOT / "app/services/sales/capture.py",
    ROOT / "app/services/sales/account_conversion.py",
    ROOT / "app/services/sales_fulfillment.py",
    ROOT / "app/services/installation_projects.py",
    ROOT / "app/services/service_order_lifecycle.py",
    ROOT / "app/services/customer_experience_handoffs.py",
    ROOT / "app/services/sales_lifecycle_reconciliation.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return imports


def test_selected_domain_and_application_services_have_no_http_dependency() -> None:
    for path in PURE_SERVICE_MODULES:
        imports = _imports(path)
        coupled = sorted(
            module
            for module in imports
            if module == "fastapi"
            or module.startswith("fastapi.")
            or module == "starlette"
            or module.startswith("starlette.")
        )
        assert not coupled, f"{path.relative_to(ROOT)} imports HTTP types: {coupled}"
