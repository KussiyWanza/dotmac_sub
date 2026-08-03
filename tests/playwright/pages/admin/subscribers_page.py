"""Customers list page object kept under the legacy subscriber name."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class SubscribersPage(BasePage):
    """Page object for the customers list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/customers") -> None:
        """Navigate to the customers list."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the customers page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Customers", exact=True)
        ).to_be_visible()

    def search(self, query: str) -> None:
        """Search for customers."""
        search_input = self.page.get_by_placeholder("Search by name, email, phone")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def filter_by_type(self, subscriber_type: str) -> None:
        """Filter by subscriber type (person/organization)."""
        self.page.get_by_label("Type").select_option(subscriber_type)

    def click_new_subscriber(self) -> None:
        """Click the new customer button."""
        self.page.get_by_role("link", name="Add Customer", exact=True).click()

    def click_subscriber_row(self, identifier: str) -> None:
        """Click on a subscriber row by name or number."""
        self.page.get_by_role("row").filter(has_text=identifier).click()

    def expect_subscriber_in_list(self, identifier: str) -> None:
        """Assert a subscriber is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=identifier)).to_be_visible()

    def expect_no_results(self) -> None:
        """Assert no results are shown."""
        expect(
            self.page.get_by_text("No customers found")
            .or_(self.page.get_by_text("No results"))
            .first
        ).to_be_visible()

    def get_subscriber_count(self) -> int:
        """Get the count of subscribers in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def click_page(self, page_num: int) -> None:
        """Click a pagination page number."""
        self.page.get_by_role("link", name=str(page_num)).click()

    def expect_page_active(self, page_num: int) -> None:
        """Assert a pagination page is active."""
        expect(
            self.page.locator("[aria-current='page']").filter(has_text=str(page_num))
        ).to_be_visible()
