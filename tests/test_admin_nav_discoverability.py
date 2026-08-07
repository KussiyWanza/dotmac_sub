"""Guard that shipped admin surfaces stay reachable from navigation.

These pages were built and mounted but linked from nowhere, so a human could
only reach them by typing the URL. Assert the sidebar/landing links exist so a
future edit can't silently re-orphan them.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _read(relative: str) -> str:
    return (TEMPLATES / relative).read_text(encoding="utf-8")


def test_inbox_and_work_orders_are_linked_in_the_sidebar() -> None:
    sidebar = _read("components/navigation/admin_sidebar.html")
    assert "/admin/inbox" in sidebar
    assert "/admin/dispatch/work-orders" in sidebar
    # Highlight mapping so the item lights up on its own pages.
    assert "'team-inbox'" in sidebar
    assert "'dispatch-work-orders'" in sidebar


def test_vendor_navigation_is_grouped_and_collapsed_by_default() -> None:
    sidebar = _read("components/navigation/admin_sidebar.html")

    assert "vendorMenuOpen" in sidebar
    assert ':aria-expanded="vendorMenuOpen.toString()"' in sidebar
    assert 'aria-controls="admin-vendor-navigation"' in sidebar
    assert 'x-show="vendorMenuOpen && !sidebarCollapsed"' in sidebar
    assert 'subnav_link("Vendor Records", "/admin/vendors", "vendors")' in sidebar
    assert (
        'subnav_link("Vendor Review Quotes", "/admin/vendors/operations", '
        '"vendor-operations")' in sidebar
    )
    assert (
        'subnav_link("Vendor Routes", "/admin/vendors/routes", "vendor-routes")'
        in sidebar
    )
    assert "vendor_group_active | tojson" in sidebar


def test_quotes_and_sales_orders_are_reachable_from_the_sales_landing() -> None:
    leads = _read("admin/sales/leads/index.html")
    assert "/admin/sales/quotes" in leads
    assert "/admin/sales/sales-order" in leads
