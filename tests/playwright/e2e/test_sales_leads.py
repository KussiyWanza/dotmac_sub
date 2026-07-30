"""Authenticated browser coverage for the Selfcare CRM Leads interface."""

from __future__ import annotations

from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from app.models.subscriber import Subscriber

pytestmark = pytest.mark.e2e


def _mark_admin_tour_seen(page: Page) -> None:
    page.add_init_script(
        "window.localStorage.setItem('dotmac_admin_tour_seen_v1', '1')"
    )


@pytest.fixture()
def lead_contact(e2e_db):
    suffix = uuid4().hex[:10]
    subscriber = Subscriber(
        first_name="Lead",
        last_name=f"Contact {suffix}",
        email=f"lead-{suffix}@example.com",
        phone=f"080{suffix[:8]}",
    )
    e2e_db.add(subscriber)
    e2e_db.commit()
    return {
        "id": subscriber.id,
        "name": f"Lead Contact {suffix}",
        "email": subscriber.email,
    }


class TestSalesLeads:
    def test_admin_can_create_and_open_a_lead(
        self,
        admin_page: Page,
        settings,
        lead_contact,
    ) -> None:
        _mark_admin_tour_seen(admin_page)
        admin_page.goto(f"{settings.base_url}/admin/sales/leads/new")

        expect(admin_page.get_by_role("heading", name="New Lead")).to_be_visible()
        admin_page.get_by_label("Lead Name").fill("E2E fibre opportunity")
        contact_search = admin_page.get_by_label("Person/Contact")
        contact_search.fill(lead_contact["email"])
        option = admin_page.get_by_role("option").filter(has_text=lead_contact["email"])
        expect(option).to_be_visible()
        option.click()
        admin_page.get_by_label("Probability").fill("55")
        admin_page.get_by_role("button", name="Create Lead").click()

        admin_page.wait_for_url("**/admin/sales/leads/**")
        expect(
            admin_page.get_by_role("heading", name="E2E fibre opportunity")
        ).to_be_visible()
        expect(admin_page.get_by_text("55%")).to_be_visible()
        expect(admin_page.get_by_text(lead_contact["email"])).to_be_visible()

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
