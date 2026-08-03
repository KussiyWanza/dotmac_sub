"""Reseller portal end-to-end journey tests.

Covers every reseller portal route as a user-facing journey:

- Authentication: login page, credential login, protected-route guard, logout
- Dashboard: summary cards + recent accounts
- Accounts: list, search, detail, "View as Customer" impersonation
- Invoices: per-account list + invoice detail
- Tickets: per-account CRM tickets
- Profile: settings view + update, MFA setup + confirm-error
- Billing: consolidated billing + revenue report
- Network: fiber map
- Navigation: sidebar links

Requires the standard Playwright E2E environment (see tests/playwright/README.md):
PLAYWRIGHT_BASE_URL plus admin credentials so the reseller identity can be seeded.
"""

from __future__ import annotations

import os
import re

import pytest
from playwright.sync_api import Page, expect

# Reseller MFA setup (/reseller/profile/mfa/setup) returns HTTP 500
# ("TOTP encryption key not configured") unless TOTP_ENCRYPTION_KEY is set, so
# skip the MFA journeys when the app has not been configured with one. The test
# process env is expected to mirror the app's (see tests/playwright/README.md).
requires_totp_key = pytest.mark.skipif(
    not os.getenv("TOTP_ENCRYPTION_KEY"),
    reason="TOTP_ENCRYPTION_KEY not configured; reseller MFA setup returns 500.",
)

from tests.playwright.pages.reseller import (
    ResellerAccountDetailPage,
    ResellerAccountInvoicesPage,
    ResellerAccountsPage,
    ResellerAccountTicketsPage,
    ResellerBillingPage,
    ResellerDashboardPage,
    ResellerFiberMapPage,
    ResellerInvoiceDetailPage,
    ResellerLoginPage,
    ResellerMfaSetupPage,
    ResellerProfilePage,
    ResellerRevenueReportPage,
)


class TestResellerLogin:
    """Authentication journeys for the reseller portal."""

    def test_reseller_login_page_loads(self, anon_page: Page, settings):
        """Reseller login page should load."""
        page = ResellerLoginPage(anon_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_reseller_login_requires_credentials(self, anon_page: Page, settings):
        """Login should require valid credentials."""
        page = ResellerLoginPage(anon_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        # Should stay on login page without credentials
        page.expect_url_contains("/reseller/auth/login")

    def test_reseller_login_succeeds_with_valid_credentials(
        self, anon_page: Page, settings, reseller_credentials
    ):
        """Logging in with seeded reseller credentials lands on the dashboard."""
        username, password = reseller_credentials
        page = ResellerLoginPage(anon_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.login(username, password)
        page.expect_login_success()
        expect(
            anon_page.get_by_role("heading", name="Dashboard", exact=True)
        ).to_be_visible()

    def test_reseller_login_rejects_bad_credentials(self, anon_page: Page, settings):
        """Invalid credentials keep the user on the login page with an error."""
        page = ResellerLoginPage(anon_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.login("e2e.reseller@example.com", "wrong-password")
        page.expect_url_contains("/reseller/auth/login")


class TestResellerAccessControl:
    """Unauthenticated access to reseller pages should redirect to login."""

    def test_dashboard_requires_auth(self, anon_page: Page, settings):
        anon_page.goto(
            f"{settings.base_url}/reseller/dashboard", wait_until="domcontentloaded"
        )
        expect(anon_page).to_have_url(re.compile(r".*/reseller/auth/login.*"))

    def test_accounts_requires_auth(self, anon_page: Page, settings):
        anon_page.goto(
            f"{settings.base_url}/reseller/accounts", wait_until="domcontentloaded"
        )
        expect(anon_page).to_have_url(re.compile(r".*/reseller/auth/login.*"))

    def test_billing_requires_auth(self, anon_page: Page, settings):
        anon_page.goto(
            f"{settings.base_url}/reseller/billing", wait_until="domcontentloaded"
        )
        expect(anon_page).to_have_url(re.compile(r".*/reseller/auth/login.*"))


class TestResellerDashboard:
    """Dashboard journeys."""

    def test_dashboard_loads(self, reseller_page: Page, settings):
        page = ResellerDashboardPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_dashboard_shows_summary_cards(self, reseller_page: Page, settings):
        page = ResellerDashboardPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        for label in ("Accounts", "Open Balance", "Open Invoices", "Open Tickets"):
            expect(reseller_page.get_by_text(label, exact=True).first).to_be_visible()

    def test_dashboard_recent_accounts_table(self, reseller_page: Page, settings):
        page = ResellerDashboardPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(
            reseller_page.get_by_role("heading", name="Recent Accounts")
        ).to_be_visible()

    def test_dashboard_view_all_accounts_link(self, reseller_page: Page, settings):
        page = ResellerDashboardPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        reseller_page.get_by_role("link", name="View all accounts").first.click()
        expect(reseller_page).to_have_url(re.compile(r".*/reseller/accounts.*"))


class TestResellerAccounts:
    """Accounts list journeys."""

    def test_accounts_page_loads(self, reseller_page: Page, settings):
        page = ResellerAccountsPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_accounts_list_visible(self, reseller_page: Page, settings):
        page = ResellerAccountsPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(reseller_page.locator("table")).to_be_visible()

    def test_accounts_search_preserves_query(self, reseller_page: Page, settings):
        """Submitting the search box round-trips the query into the URL."""
        page = ResellerAccountsPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        reseller_page.get_by_placeholder("Search accounts...").fill("Customer")
        reseller_page.keyboard.press("Enter")
        expect(reseller_page).to_have_url(re.compile(r".*search=Customer.*"))
        page.expect_loaded()


class TestResellerAccountDetail:
    """Account detail + impersonation journeys."""

    def test_account_detail_loads(
        self, reseller_page: Page, settings, reseller_account_id
    ):
        page = ResellerAccountDetailPage(reseller_page, settings.base_url)
        page.goto(reseller_account_id)
        page.expect_loaded()

    def test_navigate_to_detail_from_accounts_list(self, reseller_page: Page, settings):
        """Clicking 'Details' from the accounts list opens the detail page."""
        accounts = ResellerAccountsPage(reseller_page, settings.base_url)
        accounts.goto()
        accounts.expect_loaded()
        details_links = reseller_page.get_by_role("link", name="Details")
        if details_links.count() == 0:
            pytest.skip("No accounts assigned to the reseller to drill into.")
        details_links.first.click()
        expect(reseller_page).to_have_url(
            re.compile(r".*/reseller/accounts/[0-9a-f-]+$")
        )
        ResellerAccountDetailPage(reseller_page, settings.base_url).expect_loaded()

    def test_view_as_customer_impersonation(
        self, reseller_page: Page, settings, reseller_account_id
    ):
        """'View as Customer' starts a customer portal session."""
        page = ResellerAccountDetailPage(reseller_page, settings.base_url)
        page.goto(reseller_account_id)
        page.expect_loaded()
        page.view_as_customer()
        expect(reseller_page).to_have_url(re.compile(r".*/portal/dashboard.*"))


class TestResellerInvoices:
    """Invoice journeys."""

    def test_account_invoices_loads(
        self, reseller_page: Page, settings, reseller_account_id
    ):
        page = ResellerAccountInvoicesPage(reseller_page, settings.base_url)
        page.goto(reseller_account_id)
        page.expect_loaded()

    def test_invoice_detail_when_present(
        self, reseller_page: Page, settings, reseller_account_id
    ):
        """Open the first invoice's detail page if any invoice exists."""
        invoices = ResellerAccountInvoicesPage(reseller_page, settings.base_url)
        invoices.goto(reseller_account_id)
        invoices.expect_loaded()
        if not invoices.has_invoices():
            pytest.skip("Seeded customer has no invoices to drill into.")
        invoices.open_first_invoice()
        ResellerInvoiceDetailPage(reseller_page, settings.base_url).expect_loaded()


class TestResellerTickets:
    """Per-account support ticket journey."""

    def test_account_tickets_loads(
        self, reseller_page: Page, settings, reseller_account_id
    ):
        page = ResellerAccountTicketsPage(reseller_page, settings.base_url)
        page.goto(reseller_account_id)
        page.expect_loaded()


class TestResellerProfile:
    """Profile + MFA journeys."""

    def test_profile_loads(self, reseller_page: Page, settings):
        page = ResellerProfilePage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_profile_update_contact(self, reseller_page: Page, settings):
        """Updating contact details submits and re-renders the profile page."""
        page = ResellerProfilePage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.update_contact(
            email="e2e.reseller@example.com",
            phone="+1 555 0100",
            notes="Updated by Playwright journey test.",
        )
        page.expect_loaded()
        expect(reseller_page.locator("#contact_phone")).to_have_value("+1 555 0100")

    @requires_totp_key
    def test_mfa_setup_page_loads(self, reseller_page: Page, settings):
        page = ResellerMfaSetupPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    @requires_totp_key
    def test_mfa_confirm_rejects_invalid_code(self, reseller_page: Page, settings):
        """An invalid TOTP code does not enable MFA; the user stays in setup."""
        page = ResellerMfaSetupPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.submit_code("000000")
        # Stays on the MFA setup/profile flow rather than navigating elsewhere.
        expect(reseller_page).to_have_url(re.compile(r".*/reseller/profile.*"))


class TestResellerBilling:
    """Consolidated billing journeys."""

    def test_billing_loads(self, reseller_page: Page, settings):
        page = ResellerBillingPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_billing_links_to_revenue_report(self, reseller_page: Page, settings):
        page = ResellerBillingPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.go_to_revenue_report()
        ResellerRevenueReportPage(reseller_page, settings.base_url).expect_loaded()


class TestResellerRevenueReport:
    """Revenue report journey."""

    def test_revenue_report_loads(self, reseller_page: Page, settings):
        page = ResellerRevenueReportPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()


class TestResellerFiberMap:
    """Fiber map journey."""

    def test_fiber_map_loads(self, reseller_page: Page, settings):
        page = ResellerFiberMapPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()


class TestResellerNavigation:
    """Sidebar navigation journeys."""

    def test_sidebar_links_navigate(self, reseller_page: Page, settings):
        ResellerDashboardPage(reseller_page, settings.base_url).goto()
        nav = reseller_page.locator("nav, aside").first
        for name, fragment in (
            ("Accounts", "/reseller/accounts"),
            ("Billing", "/reseller/billing"),
            ("Fiber Map", "/reseller/fiber-map"),
        ):
            nav.get_by_role("link", name=name).first.click()
            expect(reseller_page).to_have_url(re.compile(rf".*{re.escape(fragment)}.*"))

    def test_logout_clears_session(self, reseller_page: Page, settings):
        """Logging out returns the user to the login screen."""
        reseller_page.goto(
            f"{settings.base_url}/reseller/auth/logout", wait_until="domcontentloaded"
        )
        expect(reseller_page).to_have_url(re.compile(r".*/reseller/auth/login.*"))
        # Protected pages should now redirect back to login.
        reseller_page.goto(
            f"{settings.base_url}/reseller/dashboard", wait_until="domcontentloaded"
        )
        expect(reseller_page).to_have_url(re.compile(r".*/reseller/auth/login.*"))
