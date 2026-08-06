"""Authenticated browser coverage for the Selfcare CRM Leads interface."""

from __future__ import annotations

from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _mark_admin_tour_seen(page: Page) -> None:
    page.add_init_script(
        "window.localStorage.setItem('dotmac_admin_tour_seen_v1', '1')"
    )


class TestSalesLeads:
    def test_customer_name_search_composes_with_status_filter(
        self,
        admin_page: Page,
        settings,
    ) -> None:
        _mark_admin_tour_seen(admin_page)
        suffix = uuid4().hex[:10]
        customer_name = f"Search Customer {suffix}"
        admin_page.goto(f"{settings.base_url}/admin/sales/leads/new")
        admin_page.get_by_label("Display Name").fill(customer_name)
        admin_page.get_by_label("Email 1").fill(f"search-{suffix}@example.com")
        admin_page.get_by_label("Phone 1").fill("08031234567")
        admin_page.get_by_role("button", name="Create Lead").click()
        admin_page.wait_for_url("**/admin/sales/leads/**")

        admin_page.goto(f"{settings.base_url}/admin/sales/leads")
        admin_page.get_by_label("Search leads").fill(customer_name)
        admin_page.get_by_role("button", name="Filter").click()

        admin_page.wait_for_url(
            f"**/admin/sales/leads?*search={customer_name.replace(' ', '+')}*"
        )
        rows = admin_page.locator("tbody tr")
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text(customer_name)
        expect(admin_page.get_by_text("Showing 1 to 1 of 1 leads")).to_be_visible()
        expect(admin_page.get_by_role("alert")).to_have_count(0)

        admin_page.get_by_label("Status", exact=True).select_option("new")
        admin_page.get_by_role("button", name="Filter").click()

        admin_page.wait_for_url("**/admin/sales/leads?*search=*&status=new*")
        assert admin_page.url.find("search=") > -1
        assert admin_page.url.find("status=new") > -1
        expect(admin_page.locator("tbody tr")).to_have_count(1)
        expect(admin_page.locator("tbody tr").first).to_contain_text(customer_name)
        expect(admin_page.get_by_text("Showing 1 to 1 of 1 leads")).to_be_visible()

    def test_admin_can_create_and_open_a_lead(
        self,
        admin_page: Page,
        settings,
    ) -> None:
        _mark_admin_tour_seen(admin_page)
        admin_page.goto(f"{settings.base_url}/admin/sales/leads/new")

        expect(admin_page.get_by_role("heading", name="New Lead")).to_be_visible()
        suffix = uuid4().hex[:10]
        admin_page.get_by_label("Display Name").fill(f"E2E Lead {suffix}")
        admin_page.get_by_label("Email 1").fill(f"lead-{suffix}@example.com")
        admin_page.get_by_label("Phone 1").fill("08031234567")
        admin_page.get_by_label("Probability").fill("55")
        admin_page.get_by_role("button", name="Create Lead").click()

        admin_page.wait_for_url("**/admin/sales/leads/**")
        expect(
            admin_page.get_by_role("heading", name=f"E2E Lead {suffix}")
        ).to_be_visible()
        expect(admin_page.get_by_text("55%")).to_be_visible()

    def test_admin_can_edit_a_lead_email(
        self,
        admin_page: Page,
        settings,
    ) -> None:
        _mark_admin_tour_seen(admin_page)
        suffix = uuid4().hex[:10]
        updated_email = f"updated-lead-{suffix}@example.com"
        admin_page.goto(f"{settings.base_url}/admin/sales/leads/new")
        admin_page.get_by_label("Display Name").fill(f"Editable Lead {suffix}")
        admin_page.get_by_label("Email 1").fill(f"lead-{suffix}@example.com")
        admin_page.get_by_label("Phone 1").fill("08031234567")
        admin_page.get_by_role("button", name="Create Lead").click()
        admin_page.wait_for_url("**/admin/sales/leads/**")

        admin_page.get_by_role("link", name="Edit Lead").click()
        expect(admin_page.get_by_role("heading", name="Edit Lead")).to_be_visible()
        admin_page.get_by_label("Email 1").fill(updated_email)
        admin_page.get_by_role("button", name="Update Lead").click()

        admin_page.wait_for_url("**/admin/sales/leads/**?result=updated")
        expect(admin_page.get_by_text("Lead updated successfully.")).to_be_visible()
        expect(admin_page.get_by_text(updated_email)).to_be_visible()

    def test_new_lead_repeaters_and_mobile_layout(
        self,
        admin_page: Page,
        settings,
    ) -> None:
        _mark_admin_tour_seen(admin_page)
        admin_page.set_viewport_size({"width": 390, "height": 844})
        admin_page.goto(f"{settings.base_url}/admin/sales/leads/new")

        headings = admin_page.get_by_role("heading", level=2)
        expect(headings).to_have_text(
            [
                "Contact / Lead Information",
                "Pipeline and Value",
                "Additional Information",
            ]
        )
        expect(admin_page.get_by_label("Email 1")).to_be_visible()
        admin_page.get_by_role("button", name="Add Email").click()
        expect(admin_page.get_by_label("Email 2")).to_be_visible()
        admin_page.get_by_label("Email 2").fill("second@example.com")
        admin_page.locator('[data-row="emails"]').nth(1).get_by_role(
            "button", name="Remove"
        ).click()
        expect(admin_page.get_by_label("Email 2")).to_have_count(0)

        admin_page.get_by_label("Phone 1").fill("08031234567")
        expect(admin_page.get_by_label("08031234567")).to_be_visible()
        admin_page.locator('[data-row="phones"]').get_by_role(
            "button", name="Remove"
        ).click()
        expect(admin_page.get_by_label("Phone 1")).to_have_value("")

        overflow = admin_page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1

    def test_mobile_list_keeps_primary_workflow_usable(
        self,
        admin_page: Page,
        settings,
    ) -> None:
        _mark_admin_tour_seen(admin_page)
        admin_page.set_viewport_size({"width": 390, "height": 844})
        admin_page.goto(f"{settings.base_url}/admin/sales/leads")

        expect(admin_page.get_by_role("heading", name="Leads")).to_be_visible()
        expect(admin_page.get_by_role("link", name="New Lead")).to_be_visible()
        expect(admin_page.get_by_label("Search leads")).to_be_visible()
        expect(admin_page.get_by_role("button", name="Filter")).to_be_visible()
        overflow = admin_page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1
