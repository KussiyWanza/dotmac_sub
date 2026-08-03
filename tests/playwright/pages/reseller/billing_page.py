"""Reseller portal consolidated billing page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerBillingPage(BasePage):
    """Page object for the reseller consolidated billing page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/reseller/billing") -> None:
        """Navigate to the billing overview."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the billing page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Consolidated billing")
        ).to_be_visible()

    def go_to_revenue_report(self) -> None:
        """Follow the revenue report link in the page header."""
        self.page.get_by_role("link", name="Customer billing summary").first.click()
