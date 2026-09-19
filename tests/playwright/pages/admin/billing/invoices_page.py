"""Invoices list page object."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class InvoicesPage(BasePage):
    """Page object for the invoices list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/billing/invoices") -> None:
        """Navigate to the invoices list."""
        url = f"{self.base_url}{path}"
        timeout_ms = 120000
        for attempt in range(2):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return
            except PlaywrightError as exc:
                if attempt == 1 or "Timeout" not in str(exc):
                    raise
                self.page.wait_for_timeout(500)

    def expect_loaded(self) -> None:
        """Assert the invoices page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Invoices", exact=True)
        ).to_be_visible()

    def filter_by_status(self, status: str) -> None:
        """Filter invoices by status."""
        self.page.locator("select[name='status']").select_option(status)

    def filter_proformas(self) -> None:
        """Limit the list to proforma invoices."""
        self.page.locator("input[name='proforma_only']").check()

    def clear_filters(self) -> None:
        """Clear user-selected filters while retaining any account scope."""
        self.page.get_by_role("link", name="Clear filters").click()

    def search(self, query: str) -> None:
        """Search invoices."""
        search_input = self.page.get_by_placeholder("Search")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def click_new_invoice(self) -> None:
        """Click new invoice button."""
        self.page.get_by_role("link", name="New Invoice").click()

    def click_invoice_row(self, invoice_number: str) -> None:
        """Click on an invoice row."""
        self.page.get_by_role("row").filter(has_text=invoice_number).click()

    def expect_invoice_in_list(self, invoice_number: str) -> None:
        """Assert an invoice is visible in the list."""
        expect(
            self.page.get_by_role("row").filter(has_text=invoice_number)
        ).to_be_visible()

    def expect_no_invoices(self) -> None:
        """Assert no invoices message is shown."""
        expect(self.page.get_by_text("No invoices")).to_be_visible()

    def get_invoice_count(self) -> int:
        """Get the count of invoices in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def click_generate_batch(self) -> None:
        """Click the generate batch button."""
        self.page.get_by_role("button", name="Generate").click()
