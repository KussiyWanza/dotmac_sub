"""Customer portal ticket creation page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class CustomerTicketPage(BasePage):
    """Page object for creating a customer support ticket."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/portal/support/new") -> None:
        super().goto(path)

    def expect_form_loaded(self) -> None:
        expect(
            self.page.get_by_role("heading", name="Create Support Ticket", exact=True)
            .or_(self.page.get_by_role("heading", name="Create Ticket", exact=True))
            .or_(self.page.get_by_text("New Ticket", exact=False))
            .first
        ).to_be_visible()

    def fill_subject(self, value: str) -> None:
        self.page.get_by_label("Subject").or_(
            self.page.get_by_placeholder("Subject")
        ).first.fill(value)

    def fill_description(self, value: str) -> None:
        self.page.get_by_label("Description").or_(
            self.page.get_by_placeholder("Description")
        ).or_(self.page.locator("textarea").first).first.fill(value)

    def select_region(self) -> None:
        region = self.page.get_by_label("Region")
        option_values = region.locator("option").evaluate_all(
            "(options) => options.map((option) => option.value).filter(Boolean)"
        )
        assert option_values, "The portal ticket form has no Region options."
        region.select_option(option_values[0])

    def submit_ticket(self) -> None:
        self.page.get_by_role("button", name="Submit").or_(
            self.page.get_by_role("button", name="Create")
        ).or_(self.page.get_by_role("button", name="Send")).first.click()
