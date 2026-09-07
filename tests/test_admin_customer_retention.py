from __future__ import annotations

from uuid import uuid4

from jinja2 import nodes

from app.models.collections import DunningCase
from app.models.subscriber import Subscriber
from app.services import crm_reporting
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


def test_retention_page_fetches_twenty_rows_at_a_time(db_session):
    subscribers = [
        Subscriber(
            first_name="Retention",
            last_name=f"Customer {index:02d}",
            email=f"retention-{uuid4().hex}@example.com",
        )
        for index in range(30)
    ]
    paid_customer = Subscriber(
        first_name="Paid",
        last_name="Customer",
        email=f"paid-{uuid4().hex}@example.com",
    )
    db_session.add_all([*subscribers, paid_customer])
    db_session.flush()
    db_session.add_all(
        DunningCase(account_id=subscriber.id) for subscriber in subscribers
    )
    db_session.commit()

    first = crm_reporting.get_customer_retention_page(
        db_session, query=crm_reporting.CustomerRetentionPageQuery(page=1)
    )
    second = crm_reporting.get_customer_retention_page(
        db_session, query=crm_reporting.CustomerRetentionPageQuery(page=2)
    )

    assert len(first.rows) == 20
    assert first.total_count == 30
    assert first.has_next
    assert second.page == 2
    assert len(second.rows) == 10
    assert str(paid_customer.id) not in {
        row.customer_id for row in (*first.rows, *second.rows)
    }
    assert {row.customer_id for row in first.rows}.isdisjoint(
        row.customer_id for row in second.rows
    )


def test_retention_tracker_exposes_bottom_right_pagination_controls():
    source, _, _ = retention.templates.env.loader.get_source(
        retention.templates.env, "admin/reports/customer_retention_tracker.html"
    )

    assert 'aria-label="Retention queue pages"' in source
    assert "Page {{ page }} of {{ total_pages }}" in source
    assert "page={{ page + 1 }}" in source
    assert "page={{ page - 1 }}" in source
