"""Reseller portal revenue report page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerRevenueReportPage(BasePage):
    """Page object for the reseller revenue summary report."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/reseller/reports/revenue") -> None:
        """Navigate to the revenue report."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the revenue report is loaded."""
        expect(
            self.page.get_by_role("heading", name="Customer billing summary")
        ).to_be_visible()
        # KPI captions come from the reseller-portal owner (Kpi.label), so
        # assert what it publishes rather than inventing wording.
        expect(self.page.get_by_text("Customer billing (paid)").first).to_be_visible()
