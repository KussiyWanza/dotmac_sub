from __future__ import annotations

from app.web.admin import build_router
from app.web.admin import customer_retention as retention


def test_hidden_customer_retention_routes_are_registered_without_hub_link():
    router = build_router()
    paths = {
        (getattr(route, "path", ""), frozenset(getattr(route, "methods", set())))
        for route in router.routes
    }

    assert ("/admin/customer-retention", frozenset({"GET"})) in paths
    assert ("/admin/customer-retention/{customer_id}", frozenset({"GET"})) in paths


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
