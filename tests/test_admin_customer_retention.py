from __future__ import annotations

from jinja2 import nodes

from app.web.admin import build_router
from app.web.admin import customer_retention as retention


def test_customer_retention_routes_are_registered_and_visible_from_hub():
    router = build_router()
    paths = {
        (getattr(route, "path", ""), frozenset(getattr(route, "methods", set())))
        for route in router.routes
    }

    assert ("/admin/customer-retention", frozenset({"GET"})) in paths
    assert ("/admin/customer-retention/{customer_id}", frozenset({"GET"})) in paths

    from app.web.admin import reports

    links = [
        link for section in reports.REPORT_HUB_SECTIONS for link in section["links"]
    ]
    assert {
        "name": "Customer Retention",
        "url": "/admin/customer-retention",
        "description": "Billing-risk accounts prioritized for customer recovery",
        "permission": "reports:billing:read",
    } in links


def test_retention_rows_are_native_billing_risk_only():
    rows = retention._normalize_rows(
        [
            {
                "id": "blocked-1",
                "name": "Blocked Customer",
                "balance": 1200,
                "blocked_date": "2026-08-01",
            },
            {
                "id": "due-1",
                "name": "Due Customer",
                "balance": 500,
            },
            {
                "id": "active-1",
                "name": "Paid Customer",
                "balance": 0,
            },
        ]
    )

    assert [row["customer_id"] for row in rows] == ["blocked-1", "due-1"]
    assert rows[0]["risk_segment"] == "Suspended"
    assert rows[1]["risk_segment"] == "Due Soon"
    assert all("engagement" not in row for row in rows)


def test_retention_templates_only_import_published_ui_macros():
    environment = retention.templates.env
    macro_module = environment.get_template("components/ui/macros.html").module

    for template_name in (
        "admin/reports/customer_retention_tracker.html",
        "admin/reports/customer_retention_profile.html",
    ):
        source, _, _ = environment.loader.get_source(environment, template_name)
        parsed = environment.parse(source)
        imported_names = {
            imported if isinstance(imported, str) else imported[0]
            for node in parsed.find_all(nodes.FromImport)
            if getattr(node.template, "value", None) == "components/ui/macros.html"
            for imported in node.names
        }

        missing = sorted(
            name for name in imported_names if not hasattr(macro_module, name)
        )
        assert missing == [], f"{template_name} imports unavailable macros: {missing}"
