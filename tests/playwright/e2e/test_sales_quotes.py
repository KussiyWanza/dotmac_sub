"""Authenticated browser coverage for the admin Quote-list state contract."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_quote_filters_are_visible_url_backed_and_reset_together(
    admin_page: Page,
    settings,
) -> None:
    admin_page.add_init_script(
        "window.localStorage.setItem('dotmac_admin_tour_seen_v1', '1')"
    )
    admin_page.goto(f"{settings.base_url}/admin/sales/quotes")

    search = admin_page.get_by_label("Search quotes")
    expect(search).to_be_visible()
    search.fill("browser quote state")
    admin_page.get_by_label("Status", exact=True).select_option("draft")
    admin_page.get_by_role("button", name="Filter").click()

    admin_page.wait_for_url("**/admin/sales/quotes?*search=*&status=draft*")
    expect(admin_page.get_by_label("Search quotes")).to_have_value(
        "browser quote state"
    )
    expect(admin_page.get_by_label("Status", exact=True)).to_have_value("draft")
    assert "page=1" in admin_page.url

    admin_page.get_by_role("link", name="Reset").click()
    admin_page.wait_for_url(f"{settings.base_url}/admin/sales/quotes")
    expect(admin_page.get_by_label("Search quotes")).to_have_value("")
    expect(admin_page.get_by_label("Status", exact=True)).to_have_value("")
