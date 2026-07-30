"""Customer portal e2e tests."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.playwright.pages.customer import (
    CustomerBillingPage,
    CustomerDashboardPage,
    CustomerProfilePage,
    CustomerServicesPage,
    CustomerSupportPage,
    CustomerTicketPage,
    CustomerUsagePage,
)


class TestCustomerDashboard:
    """Tests for the customer portal dashboard."""

    def test_dashboard_loads(self, customer_page: Page, settings):
        """Customer dashboard should load successfully."""
        page = CustomerDashboardPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_dashboard_shows_account_summary(self, customer_page: Page, settings):
        """Dashboard should show account summary."""
        page = CustomerDashboardPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_account_summary_visible()

    def test_dashboard_shows_service_status(self, customer_page: Page, settings):
        """Dashboard should show service status."""
        page = CustomerDashboardPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_service_status_visible()

    def test_dashboard_navigation_to_billing(self, customer_page: Page, settings):
        """Should navigate to billing from dashboard."""
        page = CustomerDashboardPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.navigate_to_billing()
        customer_page.wait_for_url("**/billing**")


class TestCustomerBilling:
    """Tests for the customer billing page."""

    def test_billing_page_loads(self, customer_page: Page, settings):
        """Billing page should load successfully."""
        page = CustomerBillingPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_billing_shows_invoices(self, customer_page: Page, settings):
        """Billing page should show invoices."""
        page = CustomerBillingPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_invoices_visible()

    def test_billing_shows_balance(self, customer_page: Page, settings):
        """Billing page should show account balance."""
        page = CustomerBillingPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_balance_visible()


class TestCustomerServices:
    """Tests for the customer services page."""

    def test_services_page_loads(self, customer_page: Page, settings):
        """Services page should load successfully."""
        page = CustomerServicesPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_services_shows_active_services(self, customer_page: Page, settings):
        """Services page should show active services."""
        page = CustomerServicesPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_active_services_visible()

    def test_services_shows_details(self, customer_page: Page, settings):
        """Services page should show service details."""
        page = CustomerServicesPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_service_details_visible()

    def test_services_change_plan_page_loads(self, customer_page: Page, settings):
        """Customer can open the change plan page from services."""
        page = CustomerServicesPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.request_upgrade()
        customer_page.wait_for_url("**/portal/services/*/change")
        expect(
            customer_page.get_by_role("heading", name="Change Your Plan")
        ).to_be_visible()


class TestCustomerSupport:
    """Tests for the customer support page."""

    def test_support_page_loads(self, customer_page: Page, settings):
        """Support page should load successfully."""
        page = CustomerSupportPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_support_shows_tickets(self, customer_page: Page, settings):
        """Support page should show tickets."""
        page = CustomerSupportPage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_tickets_visible()

    @pytest.mark.parametrize(
        "viewport",
        (
            {"width": 1440, "height": 900},
            {"width": 390, "height": 844},
        ),
        ids=("desktop", "mobile"),
    )
    def test_create_ticket_flow(self, customer_page: Page, settings, viewport):
        """Should be able to create a new support ticket at supported widths."""
        customer_page.set_viewport_size(viewport)
        support_page = CustomerSupportPage(customer_page, settings.base_url)
        support_page.goto()
        support_page.expect_loaded()
        support_page.click_new_ticket()

        ticket_page = CustomerTicketPage(customer_page, settings.base_url)
        ticket_page.expect_form_loaded()
        ticket_page.fill_subject(f"Test Support Request {viewport['width']}")
        ticket_page.fill_description("This is a test support request for e2e testing.")
        ticket_page.select_region()
        ticket_page.submit_ticket()


class TestCustomerUsage:
    """Tests for the customer usage page."""

    def test_usage_page_loads(self, customer_page: Page, settings):
        """Usage page should load successfully."""
        page = CustomerUsagePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_usage_shows_summary(self, customer_page: Page, settings):
        """Usage page should show usage summary."""
        page = CustomerUsagePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_usage_summary_visible()


class TestCustomerProfile:
    """Tests for the customer profile page."""

    def test_profile_page_loads(self, customer_page: Page, settings):
        """Profile page should load successfully."""
        page = CustomerProfilePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_profile_shows_personal_info(self, customer_page: Page, settings):
        """Profile page should show personal info."""
        page = CustomerProfilePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_personal_info_visible()

    def test_profile_shows_contact_info(self, customer_page: Page, settings):
        """Profile page should show contact info."""
        page = CustomerProfilePage(customer_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_contact_info_visible()


class TestCustomerPortalAPI:
    """Cookie-backed endpoint checks for customer portal."""

    def test_customer_session_endpoint(self, customer_api_context):
        """Session endpoint should resolve for an authenticated customer."""
        from tests.playwright.helpers.api import api_get

        response = api_get(customer_api_context, "/portal/auth/session")
        assert response.status == 200

    def test_customer_services_endpoint(self, customer_api_context):
        """Services page should render for an authenticated customer."""
        from tests.playwright.helpers.api import api_get

        response = api_get(customer_api_context, "/portal/services")
        assert response.status == 200

    def test_customer_usage_endpoint(self, customer_api_context):
        """Usage page should render for an authenticated customer."""
        from tests.playwright.helpers.api import api_get

        response = api_get(customer_api_context, "/portal/usage")
        assert response.status == 200
