"""Reseller portal account detail page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerAccountDetailPage(BasePage):
    """Page object for the reseller account detail page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, account_id: str) -> None:
        """Navigate to a specific account's detail page."""
        super().goto(f"/reseller/accounts/{account_id}")

    def expect_loaded(self) -> None:
        """Assert the account detail page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Customer context")
        ).to_be_visible()
        expect(self.page.get_by_role("button", name="View as customer")).to_be_visible()

    def view_as_customer(self) -> None:
        """Click the 'View as Customer' impersonation button."""
        self.page.get_by_role("button", name="View as customer").first.click()
