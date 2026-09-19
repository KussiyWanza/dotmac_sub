"""Billing overview page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class BillingOverviewPage(BasePage):
    """Page object for the billing overview page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/billing") -> None:
        """Navigate to the billing overview."""
        self.page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded")

    def expect_loaded(self) -> None:
        """Assert the billing overview is loaded."""
        expect(
            self.page.get_by_role("heading", name="Revenue & Billing", exact=True)
        ).to_be_visible()

    def expect_stats_visible(self) -> None:
        """Assert billing stats are displayed."""
        expect(
            self.page.locator("a[href='/admin/billing/payments']").first
        ).to_be_visible()

    def get_total_revenue(self) -> str | None:
        """Get the total revenue displayed."""
        revenue = self.page.locator("[data-stat='revenue'], .total-revenue").first
        if revenue.is_visible():
            return revenue.inner_text()
        return None

    def get_pending_amount(self) -> str | None:
        """Get the pending amount displayed."""
        pending = self.page.locator("[data-stat='pending'], .pending-amount").first
        if pending.is_visible():
            return pending.inner_text()
        return None

    def get_overdue_amount(self) -> str | None:
        """Get the overdue amount displayed."""
        overdue = self.page.locator("[data-stat='overdue'], .overdue-amount").first
        if overdue.is_visible():
            return overdue.inner_text()
        return None

    def click_invoices(self) -> None:
        """Navigate to invoices list."""
        self.page.get_by_role("link", name="Invoices").first.click()

    def click_unpaid(self) -> None:
        """Open the invoice list for the Unpaid KPI cohort."""
        self.page.get_by_role("link", name="Unpaid").click()

    def click_payments(self) -> None:
        """Navigate to payments list."""
        self.page.get_by_role("link", name="Payments").first.click()

    def click_accounts(self) -> None:
        """Navigate to billing accounts."""
        self.page.get_by_role("link", name="Accounts").first.click()

    def expect_recent_invoices_visible(self) -> None:
        """Assert recent invoices table is visible."""
        expect(self.page.locator("table")).to_be_visible()
