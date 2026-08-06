"""Customer detail page object kept under the legacy subscriber name."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class SubscriberDetailPage(BasePage):
    """Page object for the customer detail page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/customers") -> None:
        """Navigate to a specific customer detail page."""
        # Backwards compatible with existing tests: `goto(subscriber_id)`.
        if path and not path.startswith("/"):
            super().goto(f"/admin/customers/person/{path}")
            return
        super().goto(path)

    def goto_subscriber(self, subscriber_id: str) -> None:
        self.goto(subscriber_id)

    def expect_loaded(self) -> None:
        """Assert the detail page is loaded."""
        expect(
            self.page.get_by_role("link", name="Customers")
            .or_(self.page.get_by_role("link", name="Edit"))
            .first
        ).to_be_visible()

    def expect_subscriber_name(self, name: str) -> None:
        """Assert the subscriber name is displayed."""
        expect(self.page.get_by_text(name)).to_be_visible()

    def click_edit(self) -> None:
        """Click the edit button."""
        self.page.get_by_role("link", name="Edit").first.click()

    def click_delete(self) -> None:
        """Click the delete button."""
        self.page.get_by_role("button", name="Delete").click()

    def confirm_delete(self) -> None:
        """Confirm deletion in dialog."""
        self.page.get_by_role("button", name="Confirm").click()

    def cancel_delete(self) -> None:
        """Cancel deletion in dialog."""
        self.page.get_by_role("button", name="Cancel").click()

    def click_suspend(self) -> None:
        """Click the suspend button."""
        self.page.get_by_role("link", name="Suspend").click()

    def expect_subscriptions_section(self) -> None:
        """Assert the subscriptions section is visible."""
        expect(self.page.get_by_text("Subscriptions")).to_be_visible()

    def expect_invoices_section(self) -> None:
        """Assert the invoices section is visible."""
        expect(self.page.get_by_text("Invoices")).to_be_visible()

    def expect_tickets_section(self) -> None:
        """Assert the tickets section is visible."""
        expect(self.page.get_by_text("Tickets")).to_be_visible()

    def get_balance_due(self) -> str | None:
        """Get the balance due amount displayed."""
        balance_locator = self.page.locator("[data-stat='balance'], .balance-due")
        if balance_locator.is_visible():
            return balance_locator.inner_text()
        return None

    def get_monthly_bill(self) -> str | None:
        """Get the monthly bill amount displayed."""
        bill_locator = self.page.locator("[data-stat='monthly-bill'], .monthly-bill")
        if bill_locator.is_visible():
            return bill_locator.inner_text()
        return None

    def click_new_subscription(self) -> None:
        """Click to add a new subscription."""
        self.page.get_by_role("link", name="New Subscription").click()

    def click_new_ticket(self) -> None:
        """Click to create a new ticket."""
        self.page.get_by_role("link", name="New Ticket").click()

    def expect_address_displayed(self, address: str) -> None:
        """Assert an address is displayed."""
        expect(self.page.get_by_text(address)).to_be_visible()

    def expect_overview_map_fills_container(self) -> None:
        """Assert the single customer map uses the full Overview container."""
        overview_map = self.page.locator("#customer-overview-map")
        expect(overview_map).to_be_visible()
        expect(overview_map).to_have_css("position", "relative")
        expect(overview_map).to_have_css("height", "400px")
        expect(overview_map.locator(".leaflet-map-pane")).to_have_css(
            "position", "absolute"
        )

        dimensions = overview_map.evaluate(
            """element => ({
                width: element.clientWidth,
                parentWidth: element.parentElement.clientWidth,
            })"""
        )
        assert dimensions["width"] == dimensions["parentWidth"]
        expect(self.page.locator("#customer-mini-map")).to_have_count(0)
