"""Billing management e2e tests."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.playwright.pages.admin.billing import (
    BillingOverviewPage,
    InvoiceFormPage,
    InvoicesPage,
    PaymentsPage,
)


class TestBillingOverview:
    """Tests for the billing overview page."""

    def test_billing_overview_loads(self, admin_page: Page, settings):
        """Billing overview page should load."""
        page = BillingOverviewPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_billing_stats_displayed(self, admin_page: Page, settings):
        """Billing stats should be displayed."""
        page = BillingOverviewPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_stats_visible()

    def test_navigate_to_invoices(self, admin_page: Page, settings):
        """Should navigate to invoices from overview."""
        page = BillingOverviewPage(admin_page, settings.base_url)
        page.goto()
        page.click_invoices()
        admin_page.wait_for_url("**/billing/invoices**")

    def test_unpaid_card_opens_visibly_selected_unpaid_filter(
        self, admin_page: Page, settings
    ):
        """The Unpaid KPI must not appear as All on the invoice list."""
        page = BillingOverviewPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

        page.click_unpaid()

        admin_page.wait_for_url(re.compile(r"status=unpaid"))
        expect(admin_page.locator("select[name='status']")).to_have_value("unpaid")


class TestInvoicesList:
    """Tests for the invoices list page."""

    def test_invoices_page_loads(self, admin_page: Page, settings):
        """Invoices list page should load."""
        page = InvoicesPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_invoices_table_visible(self, admin_page: Page, settings):
        """Invoices table should be visible."""
        page = InvoicesPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()

    def test_new_invoice_button(self, admin_page: Page, settings):
        """New invoice button should navigate to form."""
        page = InvoicesPage(admin_page, settings.base_url)
        page.goto()
        page.click_new_invoice()
        admin_page.wait_for_url("**/invoices/new**")

    def test_filter_invoices_by_status(self, admin_page: Page, settings):
        """Unpaid, All, and concrete statuses remain visibly selected."""
        page = InvoicesPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        status_filter = admin_page.locator("select[name='status']")

        page.filter_by_status("unpaid")
        admin_page.wait_for_url(re.compile(r"status=unpaid"))
        expect(status_filter).to_have_value("unpaid")

        page.filter_by_status("")
        admin_page.wait_for_url(re.compile(r"/admin/billing/invoices(?:\?.*)?$"))
        expect(status_filter).to_have_value("")

        page.filter_by_status("paid")
        admin_page.wait_for_url(re.compile(r"status=paid"))
        expect(status_filter).to_have_value("paid")

    def test_proforma_filter_can_be_applied_and_reset(self, admin_page: Page, settings):
        """The checkbox refreshes results and Clear filters restores defaults."""
        page = InvoicesPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

        page.filter_proformas()
        admin_page.wait_for_url(re.compile(r"proforma_only=true"))
        expect(admin_page.get_by_role("link", name="Clear filters")).to_be_visible()

        page.clear_filters()
        admin_page.wait_for_url(re.compile(r"/admin/billing/invoices$"))
        expect(admin_page.locator("input[name='proforma_only']")).not_to_be_checked()


class TestInvoiceForm:
    """Tests for the invoice form."""

    def test_invoice_form_loads(self, admin_page: Page, settings):
        """Invoice form should load."""
        form = InvoiceFormPage(admin_page, settings.base_url)
        form.goto_new()
        form.expect_loaded()

    def test_invoice_form_has_required_fields(self, admin_page: Page, settings):
        """Invoice form should have required fields."""
        form = InvoiceFormPage(admin_page, settings.base_url)
        form.goto_new()
        form.expect_loaded()
        # New flow starts with customer selection, then resolves billing account.
        expect(admin_page.locator("input[data-typeahead-input]").first).to_be_visible()
        expect(admin_page.locator("#customer-account-select")).to_be_visible()

    def test_invoice_form_cancel(self, admin_page: Page, settings):
        """Cancel should return to invoices list."""
        form = InvoiceFormPage(admin_page, settings.base_url)
        form.goto_new()
        form.expect_loaded()
        form.cancel()
        admin_page.wait_for_url("**/billing/invoices**")


class TestPayments:
    """Tests for the payments page."""

    def test_payments_page_loads(self, admin_page: Page, settings):
        """Payments page should load."""
        page = PaymentsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_payments_table_visible(self, admin_page: Page, settings):
        """Payments table should be visible."""
        page = PaymentsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()


class TestBillingAPI:
    """API-level tests for billing operations."""

    def test_list_invoices_api(self, api_context, admin_token):
        """API should return invoice list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/invoices?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_payments_api(self, api_context, admin_token):
        """API should return payment list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/payments?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_accounts_api(self, api_context, admin_token):
        """API should return billing accounts list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/search/accounts?q=cu&limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_get_account_ledger_api(self, api_context, admin_token, test_identities):
        """API should return account ledger entries."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        customer = test_identities["customer"]
        account_id = customer["account"]["id"]

        response = api_get(
            api_context,
            f"/api/v1/ledger-entries?account_id={account_id}&limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data


class TestBillingWorkflows:
    """Tests for billing workflows."""

    def test_navigate_billing_from_dashboard(self, admin_page: Page, settings):
        """Should navigate to a billing surface from dashboard."""
        from tests.playwright.pages.admin.dashboard_page import AdminDashboardPage

        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()
        dashboard.click_billing_link()
        admin_page.wait_for_url("**/admin/billing", wait_until="domcontentloaded")

    def test_billing_to_invoices_navigation(self, admin_page: Page, settings):
        """Should navigate from billing overview to invoices."""
        overview = BillingOverviewPage(admin_page, settings.base_url)
        overview.goto()
        overview.expect_loaded()
        overview.click_invoices()
        admin_page.wait_for_url("**/invoices**")

    def test_create_invoice_workflow(self, admin_page: Page, settings, test_identities):
        """Should be able to start invoice creation workflow."""
        invoices = InvoicesPage(admin_page, settings.base_url)
        invoices.goto()
        invoices.click_new_invoice()

        form = InvoiceFormPage(admin_page, settings.base_url)
        form.expect_loaded()
        expect(admin_page.locator("input[data-typeahead-input]").first).to_be_visible()
        expect(admin_page.locator("#customer-account-select")).to_be_visible()
